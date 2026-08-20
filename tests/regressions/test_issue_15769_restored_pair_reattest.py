"""Redmine #15769 — write-side governed re-attest for a restored pair (j#108766).

After a Herdr/tmux server loss, a lane's pair can be restored: the processes
survive, the server-owned pane STAMPS (the mzb1 assigned names) still name the
lane's slots, but the terminal ids are NEW (and the pane locators may have
moved). The launch-generation rows still record the OLD terminal, so the
read-side ``verified_generation_token`` — deliberately byte-unchanged — fails
its exact ``generation.terminal_id == live_terminal_id`` conjunct and every
governed ``handoff send`` refuses ``target_unavailable`` forever (measured
#15631 j#108621/j#108741, #15693 j#108747).

These regressions build the "server swap" state in temp homes (attested rows +
startup transaction + attestations at the OLD terminal, a fake live inventory
at the NEW terminal, same stamps — no real Herdr) and pin:

1. the deadlock itself AND that the verifier is NOT widened (a stale-terminal
   row still yields ``""`` until the rail re-attests it);
2. the rail's re-attest: generation-row CAS (terminal + locator), the
   participant-side locator re-pin when the pane moved, the declared-pin CAS,
   and the durable old->new lineage in the structured outcome;
3. every security invariant: server-owned identity join only, foreign /
   duplicate / conflicting / non-present shapes never re-attested, byte-exact
   CAS in the store and the fence (no upsert, no coercion), single-slot mode
   widening only the pair-completeness rule.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_MISSING,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationError,
    HerdrLaunchGenerationStore,
    verified_generation_token,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_LAUNCHING,
    PHASE_ROLLBACK_OWED,
    Participant,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind import (  # noqa: E501
    SublaneRestoredPairRebindUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind_cli import (  # noqa: E501
    register_sublane_rebind_restored_pair_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind_live import (  # noqa: E501
    LiveRestoredPairRebindOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    RestoredPairRebindRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
    encode_assigned_name,
)

ISSUE = "15769"
JOURNAL = "108766"
WS = "ws_main"
LANE = "issue_15769_lane"
TOKEN = "wt_issue_15769_token"
GW_PROVIDER = "codex"
WK_PROVIDER = "claude"
GW_NAME = encode_assigned_name(WS, GW_PROVIDER, LANE)
WK_NAME = encode_assigned_name(WS, WK_PROVIDER, LANE)
GW_OLD = "w1:%1"
WK_OLD = "w1:%2"
GW_NEW = "w9:%11"
WK_NEW = "w9:%12"
GW_TERM_OLD = "term-gw-1"
WK_TERM_OLD = "term-wk-1"
GW_TERM_NEW = "term-gw-2"
WK_TERM_NEW = "term-wk-2"
KEY = LaneLifecycleKey(WS, LANE)
DECISION = DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL)
OBSERVED_AT = "2026-08-20T04:05:06+00:00"


def _pin(role: str, provider: str, name: str, locator: str) -> ProcessGenerationPin:
    return ProcessGenerationPin(
        role=role, provider=provider, assigned_name=name, locator=locator
    )


def _old_pair() -> tuple[ProcessGenerationPin, ProcessGenerationPin]:
    return (
        _pin("gateway", GW_PROVIDER, GW_NAME, GW_OLD),
        _pin("worker", WK_PROVIDER, WK_NAME, WK_OLD),
    )


def _row(name: str, locator: str, terminal: str, provider: str) -> dict:
    return {
        "name": name,
        "pane_id": locator,
        "terminal_id": terminal,
        "provider": provider,
        "agent": provider,
    }


class _TestOps(LiveRestoredPairRebindOps):
    """Live ops with the host-probe seams faked; every store join stays real."""

    def __init__(self, home: Path, rows, *, providers=(GW_PROVIDER, WK_PROVIDER)):
        super().__init__(
            repo_root=Path("/lane/issue_15769"),
            env={},
            lifecycle_home=home,
            attestation_home=home,
        )
        self.test_rows = list(rows)
        self.test_providers = providers

    def _resolve_root(self):
        return self.repo_root

    def _workspace_id(self, root):
        return WS

    def _worktree_identity(self, root, lane):
        return TOKEN

    def _worktree_readable(self, root):
        return True

    def _branch(self, root):
        return LANE

    def _providers(self, root):
        return self.test_providers

    def _rows(self):
        return list(self.test_rows)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mzb-15769-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    # -- fixture: the launch-time truth (everything bound to the OLD terminal) --

    def declare(self, *, slots=None):
        outcome = LaneDeclarationStore(home=self.home).declare_lane(
            KEY,
            decision=DECISION,
            binding_kind=BINDING_KIND_ISSUE,
            issue_id=ISSUE,
            declared_slots=_old_pair() if slots is None else slots,
            worktree_identity=TOKEN,
        )
        self.assertTrue(outcome.applied, outcome.reason)

    def seed_action(self, *, phase: str = PHASE_COMPLETED_SUCCESS) -> str:
        """One session-start action that launched BOTH slots at the OLD locators."""
        fence = StartupTransactionFence(home=self.home)
        action = fence.reserve(
            StartupUnit(
                workspace_id=WS, lane_id=LANE, providers=(GW_PROVIDER, WK_PROVIDER)
            ),
            "nonce-15769",
        )
        for provider, name, locator, terminal in (
            (GW_PROVIDER, GW_NAME, GW_OLD, GW_TERM_OLD),
            (WK_PROVIDER, WK_NAME, WK_OLD, WK_TERM_OLD),
        ):
            fence.record_participant(
                action.action_id,
                Participant(
                    role=provider,
                    assigned_name=name,
                    locator=locator,
                    receipt=pane_bound_receipt(
                        target_workspace="w1",
                        target_tab="w1:t1",
                        native_name=native_name_for(name),
                        terminal_id=terminal,
                    ),
                ),
            )
            assert append_execution_event(
                fence,
                action.action_id,
                STAGE_ATTESTATION_WRITE_SUCCEEDED,
                participant=name,
            )
        fence.set_phase(action.action_id, phase)
        return action.action_id

    def seed_generation(
        self,
        token: str,
        *,
        name: str,
        provider: str,
        locator: str,
        terminal: str,
        lane: str = LANE,
    ) -> None:
        store = HerdrLaunchGenerationStore(home=self.home)
        store.reserve_pending(
            assigned_name=name,
            startup_action_id=token,
            workspace_id=WS,
            role=provider,
            lane_id=lane,
        )
        store.finalize(
            assigned_name=name,
            startup_action_id=token,
            workspace_id=WS,
            role=provider,
            lane_id=lane,
            locator=locator,
            terminal_id=terminal,
            verdict=VERDICT_PRESENT,
            observed_at=OBSERVED_AT,
        )

    def attest(
        self,
        name: str,
        provider: str,
        locator: str,
        terminal: str,
        *,
        workspace: str = WS,
        verdict: str = VERDICT_PRESENT,
    ) -> None:
        HerdrIdentityAttestationStore(home=self.home).upsert(
            IdentityAttestationRecord(
                assigned_name=name,
                workspace_id=workspace,
                role=provider,
                lane_id=LANE,
                locator=locator,
                verdict=verdict,
                observed_at=OBSERVED_AT,
                terminal_id=terminal,
            )
        )

    def seed_launch_time_pair(self, *, phase: str = PHASE_COMPLETED_SUCCESS) -> str:
        """The full pre-restore truth: fence + generation + attestation at OLD values."""
        self.declare()
        token = self.seed_action(phase=phase)
        self.seed_generation(
            token, name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
            terminal=GW_TERM_OLD,
        )
        self.seed_generation(
            token, name=WK_NAME, provider=WK_PROVIDER, locator=WK_OLD,
            terminal=WK_TERM_OLD,
        )
        self.attest(GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD)
        self.attest(WK_NAME, WK_PROVIDER, WK_OLD, WK_TERM_OLD)
        return token

    # -- helpers ----------------------------------------------------------------

    def run_rail(self, ops, *, execute: bool, allow_single_slot: bool = False):
        return SublaneRestoredPairRebindUseCase(ops).run(
            RestoredPairRebindRequest(
                issue=ISSUE,
                lane=LANE,
                journal=JOURNAL,
                allow_single_slot=allow_single_slot,
            ),
            execute=execute,
        )

    def send_level_token(self, *, name: str, provider: str, locator: str,
                         live_terminal: str) -> str:
        """The handoff-send-level verification (the verified_generation_token path)."""
        return verified_generation_token(
            self.home,
            assigned_name=name,
            workspace_id=WS,
            role=provider,
            lane_id=LANE,
            locator=locator,
            live_terminal_id=live_terminal,
            norm=_norm,
            norm_lane=_norm_lane,
        )

    def generation(self, name: str):
        return HerdrLaunchGenerationStore(home=self.home).read(name)

    def assert_generation_unchanged(self):
        gw = self.generation(GW_NAME)
        wk = self.generation(WK_NAME)
        self.assertEqual((gw.locator, gw.terminal_id), (GW_OLD, GW_TERM_OLD))
        self.assertEqual((wk.locator, wk.terminal_id), (WK_OLD, WK_TERM_OLD))


# -- rows for the two measured restore shapes -----------------------------------

def _preserved_pane_rows() -> list[dict]:
    """The #15769 measured shape: same stamps, same pane ids, NEW terminals."""
    return [
        _row(GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER),
        _row(WK_NAME, WK_OLD, WK_TERM_NEW, WK_PROVIDER),
    ]


def _moved_pane_rows() -> list[dict]:
    """The restore also moved the panes: same stamps, NEW locators + terminals."""
    return [
        _row(GW_NAME, GW_NEW, GW_TERM_NEW, GW_PROVIDER),
        _row(WK_NAME, WK_NEW, WK_TERM_NEW, WK_PROVIDER),
    ]


class MeasuredDeadlockAndVerifierUnchanged(_Base):
    """Pin the deadlock and that the fix is the RAIL, not a widened verifier."""

    def test_stale_terminal_row_yields_no_token_until_the_rail_reattests(self):
        token = self.seed_launch_time_pair()
        # Sanity: the launch-time join is complete — the OLD terminal verifies.
        self.assertEqual(
            self.send_level_token(
                name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
                live_terminal=GW_TERM_OLD,
            ),
            token,
        )
        # The measured deadlock: the live terminal is NEW, the row is stale, and
        # the read-side verifier — deliberately unmodified — yields "".
        self.assertEqual(
            self.send_level_token(
                name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
                live_terminal=GW_TERM_NEW,
            ),
            "",
        )

    def test_preflight_is_read_only_and_reports_the_reattest(self):
        self.seed_launch_time_pair()
        outcome = self.run_rail(
            _TestOps(self.home, _preserved_pane_rows()), execute=False
        )
        self.assertTrue(outcome.plan.may_rebind, outcome.plan.blocked_reasons)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.plan.gateway.generation_state, "reattest_needed")
        self.assertEqual(outcome.plan.worker.generation_state, "reattest_needed")
        self.assertEqual(len(outcome.plan.reattest_lineage), 2)
        # Read-only: nothing moved in any store.
        self.assert_generation_unchanged()


class PreservedPaneReattest(_Base):
    """Shape A: pane ids survived, terminals are new — generation-row CAS only."""

    def test_rail_reattests_and_the_existing_verifier_passes_naturally(self):
        token = self.seed_launch_time_pair()
        outcome = self.run_rail(
            _TestOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_COMPLETED, outcome.detail)
        self.assertTrue(outcome.applied)
        for name, provider, terminal in (
            (GW_NAME, GW_PROVIDER, GW_TERM_NEW),
            (WK_NAME, WK_PROVIDER, WK_TERM_NEW),
        ):
            generation = self.generation(name)
            self.assertEqual(generation.terminal_id, terminal)
            self.assertEqual(generation.startup_action_id, token)
            # Acceptance 1: the handoff-send-level verification with the NEW
            # live terminal now succeeds through the UNCHANGED verifier.
            self.assertEqual(
                self.send_level_token(
                    name=name,
                    provider=provider,
                    locator=GW_OLD if name == GW_NAME else WK_OLD,
                    live_terminal=terminal,
                ),
                token,
            )
        # ...and the OLD terminal no longer verifies (the row moved with the pane).
        self.assertEqual(
            self.send_level_token(
                name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
                live_terminal=GW_TERM_OLD,
            ),
            "",
        )

    def test_lineage_records_old_to_new_terminal_and_evidence(self):
        token = self.seed_launch_time_pair()
        outcome = self.run_rail(
            _TestOps(self.home, _preserved_pane_rows()), execute=True
        )
        lineage = {e["slot_role"]: e for e in outcome.plan.reattest_lineage}
        gw = lineage["gateway"]
        self.assertEqual(gw["old_terminal_id"], GW_TERM_OLD)
        self.assertEqual(gw["new_terminal_id"], GW_TERM_NEW)
        self.assertEqual(gw["old_locator"], GW_OLD)
        self.assertEqual(gw["new_locator"], GW_OLD)
        self.assertEqual(gw["startup_action_id"], token)
        self.assertFalse(gw["participant_locator_repin"])
        self.assertIn("unique_live_terminal_identity", gw["evidence"])
        self.assertIn("attestation_restore_stale_present", gw["evidence"])
        # The lineage also rides the journal-ready structured payload.
        payload = outcome.as_payload()
        self.assertEqual(len(payload["plan"]["reattest_lineage"]), 2)

    def test_second_run_is_a_typed_noop(self):
        self.seed_launch_time_pair()
        ops = _TestOps(self.home, _preserved_pane_rows())
        self.assertTrue(self.run_rail(ops, execute=True).applied)
        again = self.run_rail(ops, execute=True)
        self.assertEqual(again.status, STATUS_BLOCKED)
        joined = ",".join(again.plan.blocked_reasons)
        self.assertIn("terminal_unchanged_noop:gateway", joined)
        self.assertIn("terminal_unchanged_noop:worker", joined)


class MovedPaneReattest(_Base):
    """Shape B: the restore moved the panes — pins + generation + participant."""

    def test_rail_repins_declared_generation_and_participant(self):
        token = self.seed_launch_time_pair()
        outcome = self.run_rail(_TestOps(self.home, _moved_pane_rows()), execute=True)
        self.assertEqual(outcome.status, STATUS_COMPLETED, outcome.detail)
        self.assertTrue(outcome.applied)
        # Acceptance 3: declared pins AND generation rows are CAS-updated.
        pair = read_declared_pin_pair(LaneLifecycleStore(home=self.home).get(KEY))
        self.assertEqual(pair.gateway.locator, GW_NEW)
        self.assertEqual(pair.worker.locator, WK_NEW)
        gw = self.generation(GW_NAME)
        self.assertEqual((gw.locator, gw.terminal_id), (GW_NEW, GW_TERM_NEW))
        # The startup-transaction participants were re-pinned alongside (design
        # point 3), so the UNCHANGED participant-locator conjunct holds again.
        action = StartupTransactionFence(home=self.home).read(token)
        self.assertEqual(action.participant_for(GW_PROVIDER).locator, GW_NEW)
        self.assertEqual(action.participant_for(WK_PROVIDER).locator, WK_NEW)
        # The launch-time receipts stay byte-identical (history, not identity).
        self.assertIn(GW_TERM_OLD, action.participant_for(GW_PROVIDER).receipt)
        self.assertEqual(
            self.send_level_token(
                name=GW_NAME, provider=GW_PROVIDER, locator=GW_NEW,
                live_terminal=GW_TERM_NEW,
            ),
            token,
        )

    def test_lineage_marks_the_participant_repin(self):
        self.seed_launch_time_pair()
        outcome = self.run_rail(_TestOps(self.home, _moved_pane_rows()), execute=True)
        for entry in outcome.plan.reattest_lineage:
            self.assertTrue(entry["participant_locator_repin"], entry)

    def test_rollback_owed_action_is_repinned_too(self):
        # The #15712 live-preserved rollback_owed shape can also be restored;
        # the participant re-pin is admitted for it (the debt keeps pointing at
        # the pane the process actually occupies).
        token = self.seed_launch_time_pair(phase=PHASE_ROLLBACK_OWED)
        outcome = self.run_rail(_TestOps(self.home, _moved_pane_rows()), execute=True)
        self.assertEqual(outcome.status, STATUS_COMPLETED, outcome.detail)
        action = StartupTransactionFence(home=self.home).read(token)
        self.assertEqual(action.participant_for(GW_PROVIDER).locator, GW_NEW)


class SingleSlotMode(_Base):
    """#15769 1a: one slot missing entirely; the survivor is still recoverable."""

    def test_missing_worker_blocks_without_the_flag(self):
        self.seed_launch_time_pair()
        rows = [_row(GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER)]
        outcome = self.run_rail(_TestOps(self.home, rows), execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "live_slot_absent:worker", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_generation_unchanged()

    def test_allow_single_slot_reattests_the_survivor_and_types_the_missing(self):
        token = self.seed_launch_time_pair()
        rows = [_row(GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER)]
        outcome = self.run_rail(
            _TestOps(self.home, rows), execute=True, allow_single_slot=True
        )
        self.assertEqual(outcome.status, STATUS_COMPLETED, outcome.detail)
        self.assertTrue(outcome.applied)
        # The missing slot is a typed, separate fact — never a refusal here.
        self.assertTrue(outcome.plan.worker.skipped)
        self.assertEqual(
            outcome.plan.worker.reason, "missing_live_slot:worker"
        )
        # Its declared pin AND generation row stay byte-unchanged.
        pair = read_declared_pin_pair(LaneLifecycleStore(home=self.home).get(KEY))
        self.assertEqual(pair.worker.locator, WK_OLD)
        wk = self.generation(WK_NAME)
        self.assertEqual((wk.locator, wk.terminal_id), (WK_OLD, WK_TERM_OLD))
        # The survivor is re-attested and verifies through the unchanged path.
        self.assertEqual(
            self.send_level_token(
                name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
                live_terminal=GW_TERM_NEW,
            ),
            token,
        )

    def test_both_slots_missing_stays_a_plain_refusal(self):
        self.seed_launch_time_pair()
        outcome = self.run_rail(
            _TestOps(self.home, []), execute=True, allow_single_slot=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        joined = ",".join(outcome.plan.blocked_reasons)
        self.assertIn("live_slot_absent:gateway", joined)
        self.assertIn("live_slot_absent:worker", joined)
        self.assert_generation_unchanged()


class SecurityInvariants(_Base):
    """Foreign / duplicate / conflicting shapes are never re-attested."""

    def test_duplicate_live_candidates_refuse_with_zero_cas(self):
        self.seed_launch_time_pair()
        rows = _preserved_pane_rows() + [
            _row(GW_NAME, "w9:%99", "term-gw-dup", GW_PROVIDER)
        ]
        outcome = self.run_rail(_TestOps(self.home, rows), execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "duplicate_live_candidates:gateway",
            ",".join(outcome.plan.blocked_reasons),
        )
        self.assert_generation_unchanged()

    def test_a_generation_row_foreign_to_the_slot_is_never_reattested(self):
        # The stored row's lane is not this slot's lane: the identity join must
        # refuse typed rather than re-attest a foreign generation.
        self.declare()
        token = self.seed_action()
        self.seed_generation(
            token, name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
            terminal=GW_TERM_OLD, lane="other_lane",
        )
        self.seed_generation(
            token, name=WK_NAME, provider=WK_PROVIDER, locator=WK_OLD,
            terminal=WK_TERM_OLD,
        )
        self.attest(GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD)
        self.attest(WK_NAME, WK_PROVIDER, WK_OLD, WK_TERM_OLD)
        outcome = self.run_rail(
            _TestOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "live_identity_join_failed:gateway",
            ",".join(outcome.plan.blocked_reasons),
        )
        gw = self.generation(GW_NAME)
        self.assertEqual((gw.locator, gw.terminal_id), (GW_OLD, GW_TERM_OLD))

    def test_a_foreign_attestation_record_stays_refused(self):
        # The recorded attestation belongs to another workspace: the join reads
        # CONFLICT (foreign rejection unchanged) and the restore-stale
        # acceptance never applies to it.
        self.seed_launch_time_pair()
        self.attest(GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD, workspace="ws_other")
        outcome = self.run_rail(
            _TestOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "unattested_slot:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_generation_unchanged()

    def test_a_missing_verdict_attestation_stays_refused(self):
        # The agent booted without its identity triplet: `missing` is not the
        # restore signature and never rides the stale-acceptance.
        self.seed_launch_time_pair()
        self.attest(
            GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD, verdict=VERDICT_MISSING
        )
        outcome = self.run_rail(
            _TestOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "unattested_slot:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_generation_unchanged()

    def test_a_pending_generation_row_is_never_reattested(self):
        # A newer reservation superseded the row mid-relaunch: launch-rail
        # property, not restore evidence.
        self.declare()
        token = self.seed_action()
        store = HerdrLaunchGenerationStore(home=self.home)
        store.reserve_pending(
            assigned_name=GW_NAME, startup_action_id=token, workspace_id=WS,
            role=GW_PROVIDER, lane_id=LANE,
        )
        self.seed_generation(
            token, name=WK_NAME, provider=WK_PROVIDER, locator=WK_OLD,
            terminal=WK_TERM_OLD,
        )
        self.attest(GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD)
        self.attest(WK_NAME, WK_PROVIDER, WK_OLD, WK_TERM_OLD)
        outcome = self.run_rail(
            _TestOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        # With no usable attested row, the slot is simply not-drifted +
        # stale-attested: the pre-#15769 vocabulary, no re-attest invented.
        joined = ",".join(outcome.plan.blocked_reasons)
        self.assertIn("unattested_slot:gateway", joined)
        gw = self.generation(GW_NAME)
        self.assertEqual(gw.phase, "pending")


class StoreCasFailClosed(_Base):
    """The narrow re-attest CAS: exact expected old row required, no upsert."""

    def _store(self):
        return HerdrLaunchGenerationStore(home=self.home)

    def test_wrong_expected_terminal_refuses(self):
        token = self.seed_launch_time_pair()
        with self.assertRaises(HerdrLaunchGenerationError):
            self._store().reattest_restored_terminal(
                assigned_name=GW_NAME, startup_action_id=token, workspace_id=WS,
                role=GW_PROVIDER, lane_id=LANE, verdict=VERDICT_PRESENT,
                expected_locator=GW_OLD, expected_terminal_id="term-wrong",
                live_locator=GW_OLD, live_terminal_id=GW_TERM_NEW,
            )
        self.assert_generation_unchanged()

    def test_absent_row_is_never_upserted(self):
        token = self.seed_launch_time_pair()
        with self.assertRaises(HerdrLaunchGenerationError):
            self._store().reattest_restored_terminal(
                assigned_name=encode_assigned_name(WS, "codex", "ghost_lane"),
                startup_action_id=token, workspace_id=WS, role=GW_PROVIDER,
                lane_id=LANE, verdict=VERDICT_PRESENT,
                expected_locator=GW_OLD, expected_terminal_id=GW_TERM_OLD,
                live_locator=GW_OLD, live_terminal_id=GW_TERM_NEW,
            )
        self.assertIsNone(
            self.generation(encode_assigned_name(WS, "codex", "ghost_lane"))
        )

    def test_noop_reattest_is_refused_typed(self):
        token = self.seed_launch_time_pair()
        with self.assertRaises(HerdrLaunchGenerationError):
            self._store().reattest_restored_terminal(
                assigned_name=GW_NAME, startup_action_id=token, workspace_id=WS,
                role=GW_PROVIDER, lane_id=LANE, verdict=VERDICT_PRESENT,
                expected_locator=GW_OLD, expected_terminal_id=GW_TERM_OLD,
                live_locator=GW_OLD, live_terminal_id=GW_TERM_OLD,
            )


class ParticipantRepinFailClosed(_Base):
    """The fence-side re-pin: field-scoped, CAS-guarded, phase-restricted."""

    def test_wrong_expected_locator_refuses(self):
        token = self.seed_launch_time_pair()
        fence = StartupTransactionFence(home=self.home)
        with self.assertRaises(StartupTransactionError):
            fence.repin_restored_participant_locator(
                token, GW_PROVIDER, assigned_name=GW_NAME,
                expected_locator="w1:%99", new_locator=GW_NEW,
            )
        action = fence.read(token)
        self.assertEqual(action.participant_for(GW_PROVIDER).locator, GW_OLD)

    def test_foreign_assigned_name_refuses(self):
        token = self.seed_launch_time_pair()
        fence = StartupTransactionFence(home=self.home)
        with self.assertRaises(StartupTransactionError):
            fence.repin_restored_participant_locator(
                token, GW_PROVIDER, assigned_name=WK_NAME,
                expected_locator=GW_OLD, new_locator=GW_NEW,
            )

    def test_mid_startup_phase_refuses(self):
        self.declare()
        token = self.seed_action(phase=PHASE_LAUNCHING)
        fence = StartupTransactionFence(home=self.home)
        with self.assertRaises(StartupTransactionError):
            fence.repin_restored_participant_locator(
                token, GW_PROVIDER, assigned_name=GW_NAME,
                expected_locator=GW_OLD, new_locator=GW_NEW,
            )

    def test_repin_moves_only_the_locator(self):
        token = self.seed_launch_time_pair()
        fence = StartupTransactionFence(home=self.home)
        before = fence.read(token).participant_for(GW_PROVIDER)
        fence.repin_restored_participant_locator(
            token, GW_PROVIDER, assigned_name=GW_NAME,
            expected_locator=GW_OLD, new_locator=GW_NEW,
        )
        after = fence.read(token).participant_for(GW_PROVIDER)
        self.assertEqual(after.locator, GW_NEW)
        self.assertEqual(after.receipt, before.receipt)
        self.assertEqual(after.assigned_name, before.assigned_name)
        self.assertFalse(after.closed)
        # The sibling participant is byte-untouched.
        self.assertEqual(
            fence.read(token).participant_for(WK_PROVIDER).locator, WK_OLD
        )


class CliContract(unittest.TestCase):
    def test_parser_registers_the_single_slot_flag_off_by_default(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="sublane_command")
        register_sublane_rebind_restored_pair_parser(sub)
        args = parser.parse_args(
            ["rebind-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertFalse(args.allow_single_slot)
        self.assertFalse(args.execute)
        args = parser.parse_args(
            [
                "rebind-restored-pair", "--issue", ISSUE, "--lane", LANE,
                "--allow-single-slot", "--execute",
            ]
        )
        self.assertTrue(args.allow_single_slot)
        self.assertTrue(args.execute)


if __name__ == "__main__":
    unittest.main()
