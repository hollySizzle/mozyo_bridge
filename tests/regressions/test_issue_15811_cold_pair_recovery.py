"""Redmine #15811 — governed recovery for a restored cold pair with ABSENT declared pins.

**Primary diagnosis (measured on the operator lifecycle store, 2026-08-20, read-only).**
The four rails that refused the two production lanes (``issue_15745_fleet_rehydrate`` /
``issue_15748_startup_strand_settlement``) did NOT face a degraded pin record of the #15774
class. ``read_declared_pin_pair`` reports the typed ``declared_pins_absent`` — the row never
carried pins at all — because the create path
(:func:`...sublane_create_lifecycle_declaration.declare_created_lane_lifecycle`) writes the
owner row with an EMPTY ``declared_slots`` snapshot by design; pins land only when an adopt
/ repair rail observes the live pair. 43 of the store's 55 rows share that shape.

So the 2026-08-20 herdr server generation change (#15795) restored a pair whose lane row had
nothing pinned, and every rail refused for its own correct reason:

- ``rebind-restored-pair`` (#15656/#15769) has no exact old pair for its replace-CAS
  (``declared_slots_unresolved``);
- ``sublane create`` adopt (#13809) refuses the restore-stale self-attestation
  (``unattested_slot``, surfaced as ``adopt_owner_unbound``);
- ``rehydrate-fleet`` (#15745) and the ``retire`` drain edge both read the
  current-generation proof, which is empty while the launch-generation row still records
  the pre-restore terminal.

The design consequence is recorded here because it is the reason the rail looks the way it
does: option (a) of the Start Gate — "identify the old pair to replace from surrounding
evidence" — has no referent, since no old pair was ever declared; feeding the replace-CAS
would require FABRICATING one. And the pane-close reading of option (b) contradicts the
goal (recover WITHOUT owner pane operations, ADR-0013). What remains is option (b) narrowed:
accept ``declared_pins_absent`` as a typed subject and declare the pair for the first time
from the same server-owned restore evidence #15769 already demands, through the existing
empty-only binding CAS.

These regressions build that state on REAL stores in a temp home (lifecycle row + startup
transaction + launch-generation rows + attestations at the OLD terminal, a fake live
inventory at the NEW terminal — no real Herdr) and pin:

1. the diagnosis itself (create-path row -> ``declared_pins_absent``);
2. the pre-fix refusal of the rails, with zero writes;
3. the new rail's recovery, and that the UNCHANGED read-side current-generation verifier
   passes afterwards — the conjunct the drain / heal paths read;
4. the fail-closed invariants: a non-empty snapshot of ANY shape is never overwritten,
   foreign / different-generation / unidentifiable slots are zero-write, and the rebind
   rail's pin-resolvable path is unchanged.
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
from mozyo_bridge.core.state.herdr_launch_generation import HerdrLaunchGenerationStore
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_pin_role import (
    PIN_PAIR_ABSENT,
    PIN_PAIR_FOREIGN,
    read_declared_pin_pair,
)
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    ADOPT_DECL_OWNER_UNBOUND,
    ADOPT_DECL_UNATTESTED,
    declare_adopted_owner_row,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt import (  # noqa: E501
    SublaneRestoredPairAdoptUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt_cli import (  # noqa: E501
    register_sublane_adopt_restored_pair_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt_live import (  # noqa: E501
    LiveRestoredPairAdoptOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind import (  # noqa: E501
    SublaneRestoredPairRebindUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind_live import (  # noqa: E501
    LiveRestoredPairRebindOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_adopt import (  # noqa: E501
    ADOPT_BLOCK_DECLARED_PINS_PRESENT,
    ADOPT_SLOT_GENERATION_ABSENT,
    RestoredPairAdoptRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    REBIND_BLOCK_DECLARED_SLOTS_UNRESOLVED,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    RestoredPairRebindRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

ISSUE = "15811"
JOURNAL = "109241"
WS = "ws_main"
LANE = "issue_15811_lane"
TOKEN = "wt_issue_15811_token"
GW_PROVIDER = "codex"
WK_PROVIDER = "claude"
GW_NAME = encode_assigned_name(WS, GW_PROVIDER, LANE)
WK_NAME = encode_assigned_name(WS, WK_PROVIDER, LANE)
GW_OLD, WK_OLD = "w1:%1", "w1:%2"
GW_NEW, WK_NEW = "w9:%11", "w9:%12"
GW_TERM_OLD, WK_TERM_OLD = "term-gw-1", "term-wk-1"
GW_TERM_NEW, WK_TERM_NEW = "term-gw-2", "term-wk-2"
KEY = LaneLifecycleKey(WS, LANE)
DECISION = DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL)
OBSERVED_AT = "2026-08-20T09:31:00+00:00"


def _row(
    name: str,
    locator: str,
    terminal: str,
    provider: str,
    *,
    surfaced_provider: str | None = None,
    detected_agent: str | None = None,
) -> dict:
    """One raw ``agent list`` row. The surfaced provider / detected agent default to the
    slot's provider and are overridable so a squatting / residue shape can be built."""
    return {
        "name": name,
        "pane_id": locator,
        "terminal_id": terminal,
        "provider": provider if surfaced_provider is None else surfaced_provider,
        "agent": provider if detected_agent is None else detected_agent,
    }


def _preserved_pane_rows() -> list[dict]:
    """The measured shape: same stamps, same pane ids, NEW terminals."""
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


def seed_restored_lane_fixture(home: Path, *, slots=()) -> str:
    """Build the #15811 shape on REAL stores under ``home`` and return the action token.

    Module level (not a ``TestCase`` method) so the operator-view acceptance suite reuses
    the identical fixture instead of re-deriving it: an active issue lifecycle row whose
    ``declared_slots`` is ``slots`` (EMPTY by default — the create-path shape), a completed
    startup transaction, launch-generation rows and self-attestations all recorded at the
    PRE-restore locator / terminal.
    """
    outcome = LaneDeclarationStore(home=home).declare_lane(
        KEY,
        decision=DECISION,
        binding_kind=BINDING_KIND_ISSUE,
        issue_id=ISSUE,
        declared_slots=slots,
        worktree_identity=TOKEN,
    )
    assert outcome.applied, outcome.reason
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id=WS, lane_id=LANE, providers=(GW_PROVIDER, WK_PROVIDER)),
        "nonce-15811",
    )
    generations = HerdrLaunchGenerationStore(home=home)
    attestations = HerdrIdentityAttestationStore(home=home)
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
            fence, action.action_id, STAGE_ATTESTATION_WRITE_SUCCEEDED, participant=name
        )
        generations.reserve_pending(
            assigned_name=name,
            startup_action_id=action.action_id,
            workspace_id=WS,
            role=provider,
            lane_id=LANE,
        )
        generations.finalize(
            assigned_name=name,
            startup_action_id=action.action_id,
            workspace_id=WS,
            role=provider,
            lane_id=LANE,
            locator=locator,
            terminal_id=terminal,
            verdict=VERDICT_PRESENT,
            observed_at=OBSERVED_AT,
        )
        attestations.upsert(
            IdentityAttestationRecord(
                assigned_name=name,
                workspace_id=WS,
                role=provider,
                lane_id=LANE,
                locator=locator,
                verdict=VERDICT_PRESENT,
                observed_at=OBSERVED_AT,
                terminal_id=terminal,
            )
        )
    fence.set_phase(action.action_id, PHASE_COMPLETED_SUCCESS)
    return action.action_id


class _FakeHostProbes:
    """Host-probe seams faked; every store join stays real against the temp home."""

    def _resolve_root(self):
        return self.repo_root

    def _workspace_id(self, root):
        return self.test_workspace

    def _worktree_identity(self, root, lane):
        return self.test_token

    def _worktree_readable(self, root):
        return True

    def _branch(self, root):
        return self.test_branch

    def _providers(self, root):
        return self.test_providers

    def _rows(self):
        return list(self.test_rows)


class _AdoptOps(_FakeHostProbes, LiveRestoredPairAdoptOps):
    def __init__(
        self,
        home: Path,
        rows,
        *,
        providers=(GW_PROVIDER, WK_PROVIDER),
        workspace: str = WS,
        token: str = TOKEN,
        branch: str = LANE,
    ):
        super().__init__(
            repo_root=Path("/lane/issue_15811"),
            env={},
            lifecycle_home=home,
            attestation_home=home,
        )
        self.test_rows = list(rows)
        self.test_providers = providers
        self.test_workspace = workspace
        self.test_token = token
        self.test_branch = branch


class _RebindOps(_FakeHostProbes, LiveRestoredPairRebindOps):
    def __init__(self, home: Path, rows):
        super().__init__(
            repo_root=Path("/lane/issue_15811"),
            env={},
            lifecycle_home=home,
            attestation_home=home,
        )
        self.test_rows = list(rows)
        self.test_providers = (GW_PROVIDER, WK_PROVIDER)
        self.test_workspace = WS
        self.test_token = TOKEN
        self.test_branch = LANE


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mzb-15811-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    # -- fixture: the launch-time truth, with the row's pins NEVER declared ----

    def declare(self, *, slots=()):
        outcome = LaneDeclarationStore(home=self.home).declare_lane(
            KEY,
            decision=DECISION,
            binding_kind=BINDING_KIND_ISSUE,
            issue_id=ISSUE,
            declared_slots=slots,
            worktree_identity=TOKEN,
        )
        self.assertTrue(outcome.applied, outcome.reason)

    def seed_action(self, *, phase: str = PHASE_COMPLETED_SUCCESS) -> str:
        fence = StartupTransactionFence(home=self.home)
        action = fence.reserve(
            StartupUnit(
                workspace_id=WS, lane_id=LANE, providers=(GW_PROVIDER, WK_PROVIDER)
            ),
            "nonce-15811",
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
        finalize: bool = True,
    ) -> None:
        store = HerdrLaunchGenerationStore(home=self.home)
        store.reserve_pending(
            assigned_name=name,
            startup_action_id=token,
            workspace_id=WS,
            role=provider,
            lane_id=lane,
        )
        if not finalize:
            return
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

    def seed_unpinned_restored_lane(self, *, slots=()) -> str:
        """The full #15811 shape: launch-time stores at OLD values, row pins ABSENT."""
        return seed_restored_lane_fixture(self.home, slots=slots)

    # -- helpers ---------------------------------------------------------------

    def record(self):
        return LaneLifecycleStore(home=self.home).get(KEY)

    def run_adopt(self, ops, *, execute: bool):
        return SublaneRestoredPairAdoptUseCase(ops).run(
            RestoredPairAdoptRequest(issue=ISSUE, lane=LANE, journal=JOURNAL),
            execute=execute,
        )

    def generation(self, name: str):
        return HerdrLaunchGenerationStore(home=self.home).read(name)

    def current_generation_token(self, *, name, provider, locator, terminal) -> str:
        """The proof the drain / rehydrate edges read (unchanged read-side verifier)."""
        return verified_terminal_generation_token(
            self.home,
            assigned_name=name,
            workspace_id=WS,
            role=provider,
            lane_id=LANE,
            locator=locator,
            terminal_id=terminal,
        )

    def assert_zero_write(self, *, revision: int = 1, encoded_slots: str = ""):
        """Nothing moved: the lifecycle row AND every restore store are untouched."""
        record = self.record()
        self.assertEqual(record.revision, revision)
        self.assertEqual(record.declared_slots, encoded_slots)
        for name, locator, terminal in (
            (GW_NAME, GW_OLD, GW_TERM_OLD),
            (WK_NAME, WK_OLD, WK_TERM_OLD),
        ):
            generation = self.generation(name)
            if generation is not None and generation.phase == "attested":
                self.assertEqual((generation.locator, generation.terminal_id),
                                 (locator, terminal))
            attestation = HerdrIdentityAttestationStore(home=self.home).read(name)
            if attestation is not None:
                self.assertEqual(
                    (attestation.locator, attestation.terminal_id), (locator, terminal)
                )


class PrimaryDiagnosis(_Base):
    """The measured cause: the pins were never declared, not degraded."""

    def test_create_path_row_reads_declared_pins_absent(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_create_lifecycle_declaration import (  # noqa: E501
            declare_created_lane_lifecycle,
        )
        from unittest import mock

        with mock.patch(
            "mozyo_bridge.core.state.lane_lifecycle.LaneLifecycleStore",
            lambda *a, **k: LaneLifecycleStore(home=self.home),
        ):
            skipped = declare_created_lane_lifecycle(
                repo_workspace_id=WS,
                lane_label=LANE,
                issue=ISSUE,
                journal=JOURNAL,
                worktree_identity=TOKEN,
            )
        self.assertIsNone(skipped, "the create declaration reached the store")
        pair = read_declared_pin_pair(self.record())
        # NOT unreadable / foreign / mixed / duplicate / incomplete: the create path simply
        # never writes pins, so the row is pin-ABSENT from birth (#15774 is a different
        # mechanism — a field LOST from a row that had one).
        self.assertEqual(pair.reason, PIN_PAIR_ABSENT)
        self.assertFalse(pair.ok)
        self.assertEqual(self.record().declared_slots, "")


class PreFixFourRailRefusal(_Base):
    """Every rail refused this shape, each for its own correct reason — zero-write."""

    def test_rebind_rail_has_no_old_pair_to_replace(self):
        self.seed_unpinned_restored_lane()
        outcome = SublaneRestoredPairRebindUseCase(
            _RebindOps(self.home, _preserved_pane_rows())
        ).run(
            RestoredPairRebindRequest(issue=ISSUE, lane=LANE, journal=JOURNAL),
            execute=True,
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            REBIND_BLOCK_DECLARED_SLOTS_UNRESOLVED, outcome.plan.blocked_reasons
        )
        self.assert_zero_write()

    def test_create_adopt_refuses_the_restore_stale_attestation(self):
        self.seed_unpinned_restored_lane()
        status = declare_adopted_owner_row(
            journal=JOURNAL,
            issue=ISSUE,
            lane_label=LANE,
            worktree_path=str(self.home),
            workspace_id=WS,
            lane_id=LANE,
            providers=(GW_PROVIDER, WK_PROVIDER),
            rows=_preserved_pane_rows(),
            attestation_home=self.home,
            store_factory=lambda: LaneDeclarationStore(home=self.home),
        )
        self.assertEqual(status, ADOPT_DECL_UNATTESTED)
        self.assertIn(status, ADOPT_DECL_OWNER_UNBOUND)
        self.assert_zero_write()

    def test_the_current_generation_proof_the_drain_and_heal_edges_read_is_empty(self):
        # `rehydrate-fleet`'s same-generation attribution and the `retire` drain's
        # current-generation proof both resolve through this verifier. Before the rail it
        # is empty for the LIVE terminal, which is why both refused typed.
        self.seed_unpinned_restored_lane()
        for name, provider, locator, terminal in (
            (GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_NEW),
            (WK_NAME, WK_PROVIDER, WK_OLD, WK_TERM_NEW),
        ):
            self.assertEqual(
                self.current_generation_token(
                    name=name, provider=provider, locator=locator, terminal=terminal
                ),
                "",
            )
        self.assert_zero_write()


class RailRecoversThePair(_Base):
    """The rail declares the absent pins and re-attests, with no pane operation."""

    def test_preflight_is_read_only_and_reports_the_reattest(self):
        self.seed_unpinned_restored_lane()
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=False
        )
        self.assertTrue(outcome.plan.may_adopt, outcome.plan.blocked_reasons)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.plan.gateway.generation_state, "reattest_needed")
        self.assertEqual(outcome.plan.worker.generation_state, "reattest_needed")
        self.assertEqual(len(outcome.plan.reattest_lineage), 2)
        self.assert_zero_write()

    def test_execute_declares_the_pair_and_the_unchanged_verifier_passes(self):
        token = self.seed_unpinned_restored_lane()
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_COMPLETED, outcome.detail)
        self.assertTrue(outcome.applied)
        record = self.record()
        pair = read_declared_pin_pair(record)
        self.assertTrue(pair.ok, pair.reason)
        self.assertEqual(pair.gateway.assigned_name, GW_NAME)
        self.assertEqual(pair.gateway.locator, GW_OLD)
        self.assertEqual(pair.worker.assigned_name, WK_NAME)
        self.assertEqual(pair.worker.locator, WK_OLD)
        # The restored processes are the SAME agent-session incarnation: the lane
        # generation must not move, so existing dispatch-marker anchors stay valid.
        self.assertEqual(record.lane_generation, 1)
        # Acceptance: the conjunct the drain / heal edges read now resolves, through the
        # read-side verifier this change did NOT touch.
        for name, provider, locator, terminal in (
            (GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_NEW),
            (WK_NAME, WK_PROVIDER, WK_OLD, WK_TERM_NEW),
        ):
            self.assertEqual(
                self.current_generation_token(
                    name=name, provider=provider, locator=locator, terminal=terminal
                ),
                token,
            )
        # ...and the pre-restore terminal no longer borrows it.
        self.assertEqual(
            self.current_generation_token(
                name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
                terminal=GW_TERM_OLD,
            ),
            "",
        )

    def test_moved_pane_shape_pins_the_live_locators_and_repins_the_participant(self):
        token = self.seed_unpinned_restored_lane()
        outcome = self.run_adopt(
            _AdoptOps(self.home, _moved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_COMPLETED, outcome.detail)
        pair = read_declared_pin_pair(self.record())
        self.assertEqual(pair.gateway.locator, GW_NEW)
        self.assertEqual(pair.worker.locator, WK_NEW)
        action = StartupTransactionFence(home=self.home).read(token)
        self.assertEqual(action.participant_for(GW_PROVIDER).locator, GW_NEW)
        self.assertEqual(action.participant_for(WK_PROVIDER).locator, WK_NEW)
        for entry in outcome.plan.reattest_lineage:
            self.assertTrue(entry["participant_locator_repin"], entry)
            self.assertIn("declared_pins_absent_subject", entry["evidence"])

    def test_lineage_records_the_old_to_new_terminal_and_carries_no_receipt_bytes(self):
        token = self.seed_unpinned_restored_lane()
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        lineage = {e["slot_role"]: e for e in outcome.plan.reattest_lineage}
        gateway = lineage["gateway"]
        self.assertEqual(gateway["old_terminal_id"], GW_TERM_OLD)
        self.assertEqual(gateway["new_terminal_id"], GW_TERM_NEW)
        self.assertEqual(gateway["startup_action_id"], token)
        self.assertIn("attested_generation_row_required", gateway["evidence"])
        self.assertIn("attestation_restore_stale_present", gateway["evidence"])
        self.assertNotIn("pane_bound", str(outcome.as_payload()))

    def test_a_retry_after_a_partial_reattest_failure_completes_the_declaration(self):
        # The write order is retry-safe by construction (#15769): if the worker slot's
        # generation CAS fails after the gateway's committed, NO pin is declared, and a
        # re-run must re-observe, skip the already-repaired gateway, finish the worker and
        # declare the pair. A rail that deadlocked on its own partial progress would leave
        # the lane needing the owner pane close this issue exists to remove.
        from unittest import mock

        from mozyo_bridge.core.state.herdr_launch_generation import (
            HerdrLaunchGenerationStore as _Store,
        )

        self.seed_unpinned_restored_lane()
        ops = _AdoptOps(self.home, _preserved_pane_rows())
        real = _Store.reattest_restored_terminal

        def worker_explodes(store, **kwargs):
            if kwargs.get("assigned_name") == WK_NAME:
                raise RuntimeError("injected worker failure")
            return real(store, **kwargs)

        with mock.patch.object(_Store, "reattest_restored_terminal", worker_explodes):
            first = self.run_adopt(ops, execute=True)
        self.assertFalse(first.applied)
        self.assertIn("slot_reattest_refused:worker", first.detail)
        # No pin was declared: the declaration is the LAST step, after both slots.
        self.assertEqual(self.record().declared_slots, "")
        self.assertEqual(self.generation(GW_NAME).terminal_id, GW_TERM_NEW)
        self.assertEqual(self.generation(WK_NAME).terminal_id, WK_TERM_OLD)

        second = self.run_adopt(ops, execute=True)
        self.assertEqual(second.status, STATUS_COMPLETED, second.detail)
        self.assertTrue(second.applied)
        self.assertEqual(self.generation(WK_NAME).terminal_id, WK_TERM_NEW)
        self.assertTrue(read_declared_pin_pair(self.record()).ok)

    def test_second_run_is_a_typed_refusal_not_a_second_write(self):
        self.seed_unpinned_restored_lane()
        ops = _AdoptOps(self.home, _preserved_pane_rows())
        first = self.run_adopt(ops, execute=True)
        self.assertTrue(first.applied)
        revision = self.record().revision
        encoded = self.record().declared_slots
        again = self.run_adopt(ops, execute=True)
        self.assertEqual(again.status, STATUS_BLOCKED)
        self.assertIn(
            f"{ADOPT_BLOCK_DECLARED_PINS_PRESENT}:declared_pin_pair_ok",
            again.plan.blocked_reasons,
        )
        self.assertEqual(self.record().revision, revision)
        self.assertEqual(self.record().declared_slots, encoded)


class NonEmptySnapshotIsNeverOverwritten(_Base):
    """The subject gate: this rail only ever touches a pin-ABSENT row."""

    def test_a_resolvable_pair_is_refused_with_the_pin_reason_named(self):
        pins = (
            ProcessGenerationPin(
                role="gateway", provider=GW_PROVIDER, assigned_name=GW_NAME,
                locator=GW_OLD,
            ),
            ProcessGenerationPin(
                role="worker", provider=WK_PROVIDER, assigned_name=WK_NAME,
                locator=WK_OLD,
            ),
        )
        self.seed_unpinned_restored_lane(slots=pins)
        encoded = self.record().declared_slots
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            f"{ADOPT_BLOCK_DECLARED_PINS_PRESENT}:declared_pin_pair_ok",
            outcome.plan.blocked_reasons,
        )
        self.assert_zero_write(encoded_slots=encoded)

    def test_a_degraded_snapshot_is_refused_and_preserved_byte_exact(self):
        # A foreign pin role is exactly the #13920 degradation family. Overwriting it from
        # live observation would destroy the evidence `sublane repair-pins` / an owner
        # decision needs — "pin unresolvable" must never read as "recover anything".
        pins = (
            ProcessGenerationPin(
                role="orphan_slot", provider=GW_PROVIDER, assigned_name=GW_NAME,
                locator=GW_OLD,
            ),
            ProcessGenerationPin(
                role="worker", provider=WK_PROVIDER, assigned_name=WK_NAME,
                locator=WK_OLD,
            ),
        )
        self.seed_unpinned_restored_lane(slots=pins)
        encoded = self.record().declared_slots
        self.assertEqual(read_declared_pin_pair(self.record()).reason, PIN_PAIR_FOREIGN)
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            f"{ADOPT_BLOCK_DECLARED_PINS_PRESENT}:{PIN_PAIR_FOREIGN}",
            outcome.plan.blocked_reasons,
        )
        self.assert_zero_write(encoded_slots=encoded)


class ForeignAndUnprovableSlotsAreZeroWrite(_Base):
    """Acceptance 2: nothing this rail cannot PROVE belongs to the lane is written."""

    def test_a_generation_row_foreign_to_the_slot_is_never_adopted(self):
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
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "live_identity_join_failed:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_zero_write()

    def test_no_attested_generation_row_is_a_hard_refusal_never_a_fallthrough(self):
        # The rebind rail treats "no usable attested row" as its pre-#15769 shape and falls
        # through to the declared pin. This rail HAS no declared pin, so the server-owned
        # row is the only thing tying the live process to this lane beyond its name.
        self.declare()
        token = self.seed_action()
        self.seed_generation(
            token, name=GW_NAME, provider=GW_PROVIDER, locator=GW_OLD,
            terminal=GW_TERM_OLD, finalize=False,
        )
        self.seed_generation(
            token, name=WK_NAME, provider=WK_PROVIDER, locator=WK_OLD,
            terminal=WK_TERM_OLD,
        )
        self.attest(GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD)
        self.attest(WK_NAME, WK_PROVIDER, WK_OLD, WK_TERM_OLD)
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            f"{ADOPT_SLOT_GENERATION_ABSENT}:gateway",
            ",".join(outcome.plan.blocked_reasons),
        )
        self.assert_zero_write()

    def test_a_duplicate_live_name_is_never_guessed_past(self):
        self.seed_unpinned_restored_lane()
        rows = _preserved_pane_rows() + [
            _row(GW_NAME, "w9:%99", "term-gw-dup", GW_PROVIDER)
        ]
        outcome = self.run_adopt(_AdoptOps(self.home, rows), execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "duplicate_live_candidates:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_zero_write()

    def test_a_foreign_workspace_attestation_stays_refused(self):
        self.seed_unpinned_restored_lane()
        self.attest(GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD, workspace="ws_other")
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "unattested_slot:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assertEqual(self.record().revision, 1)
        self.assertEqual(self.record().declared_slots, "")

    def test_a_missing_verdict_attestation_is_not_the_restore_signature(self):
        self.seed_unpinned_restored_lane()
        self.attest(
            GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD, verdict=VERDICT_MISSING
        )
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "unattested_slot:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assertEqual(self.record().declared_slots, "")

    def test_a_shell_residue_is_never_a_slot(self):
        self.seed_unpinned_restored_lane()
        rows = [
            _row(
                GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER,
                surfaced_provider="", detected_agent="",
            ),
            _row(WK_NAME, WK_OLD, WK_TERM_NEW, WK_PROVIDER),
        ]
        outcome = self.run_adopt(_AdoptOps(self.home, rows), execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "stale_named_slot:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_zero_write()

    def test_a_foreign_provider_squatting_on_the_name_is_refused(self):
        self.seed_unpinned_restored_lane()
        rows = [
            _row(
                GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER,
                surfaced_provider="intruder",
            ),
            _row(WK_NAME, WK_OLD, WK_TERM_NEW, WK_PROVIDER),
        ]
        outcome = self.run_adopt(_AdoptOps(self.home, rows), execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "provider_mismatch:gateway", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_zero_write()

    def test_a_fabricated_live_bound_generation_row_does_not_bypass_the_lineage(self):
        # #15769 round-2 finding 2, re-pinned for this rail: a generation row that already
        # claims the live values while the fence participants still hold the launch-time
        # receipt is not this restore's lineage.
        self.declare()
        token = self.seed_action()
        self.seed_generation(
            token, name=GW_NAME, provider=GW_PROVIDER, locator=GW_NEW,
            terminal=GW_TERM_NEW,
        )
        self.seed_generation(
            token, name=WK_NAME, provider=WK_PROVIDER, locator=WK_OLD,
            terminal=WK_TERM_OLD,
        )
        self.attest(GW_NAME, GW_PROVIDER, GW_OLD, GW_TERM_OLD)
        self.attest(WK_NAME, WK_PROVIDER, WK_OLD, WK_TERM_OLD)
        outcome = self.run_adopt(
            _AdoptOps(self.home, _moved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "participant_repin_unresolved:gateway",
            ",".join(outcome.plan.blocked_reasons),
        )
        self.assertEqual(self.record().declared_slots, "")

    def test_half_a_live_pair_is_never_declared_as_a_pair(self):
        # There is no single-slot mode: with no declared pin the row holds no record of
        # what the other half was, so half an observation cannot become a pair.
        self.seed_unpinned_restored_lane()
        rows = [_row(GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER)]
        outcome = self.run_adopt(_AdoptOps(self.home, rows), execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn(
            "incomplete_live_pair:worker", ",".join(outcome.plan.blocked_reasons)
        )
        self.assert_zero_write()


class LaneLevelGatesStayFailClosed(_Base):
    """The lane row must be the exact governed target before any slot is observed."""

    def test_a_non_active_row_is_refused(self):
        self.seed_unpinned_restored_lane()
        moved = LaneLifecycleStore(home=self.home).transition_disposition(
            KEY,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=DECISION,
        )
        self.assertTrue(moved.applied, moved.reason)
        outcome = self.run_adopt(
            _AdoptOps(self.home, _preserved_pane_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("lane_not_active", outcome.plan.blocked_reasons)

    def test_a_different_issue_is_refused(self):
        self.seed_unpinned_restored_lane()
        outcome = SublaneRestoredPairAdoptUseCase(
            _AdoptOps(self.home, _preserved_pane_rows())
        ).run(
            RestoredPairAdoptRequest(issue="99999", lane=LANE, journal=JOURNAL),
            execute=True,
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("issue_mismatch", outcome.plan.blocked_reasons)
        self.assert_zero_write()

    def test_a_drifted_worktree_identity_is_refused(self):
        self.seed_unpinned_restored_lane()
        ops = _AdoptOps(
            self.home, _preserved_pane_rows(), token="wt_some_other_lane_token"
        )
        outcome = self.run_adopt(ops, execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("worktree_identity_mismatch", outcome.plan.blocked_reasons)
        self.assert_zero_write()

    def test_a_drifted_branch_is_refused(self):
        self.seed_unpinned_restored_lane()
        ops = _AdoptOps(self.home, _preserved_pane_rows(), branch="some_other_branch")
        outcome = self.run_adopt(ops, execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("branch_drifted", outcome.plan.blocked_reasons)
        self.assert_zero_write()

    def test_an_unresolved_provider_is_refused(self):
        self.seed_unpinned_restored_lane()
        ops = _AdoptOps(self.home, _preserved_pane_rows(), providers=("", ""))
        outcome = self.run_adopt(ops, execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("provider_unresolved", outcome.plan.blocked_reasons)
        self.assert_zero_write()


class ExistingRebindRailUnchanged(_Base):
    """Acceptance 3: the pin-RESOLVABLE path of the neighbouring rail still works."""

    def test_a_pin_resolvable_restored_pair_still_rebinds(self):
        pins = (
            ProcessGenerationPin(
                role="gateway", provider=GW_PROVIDER, assigned_name=GW_NAME,
                locator=GW_OLD,
            ),
            ProcessGenerationPin(
                role="worker", provider=WK_PROVIDER, assigned_name=WK_NAME,
                locator=WK_OLD,
            ),
        )
        self.seed_unpinned_restored_lane(slots=pins)
        outcome = SublaneRestoredPairRebindUseCase(
            _RebindOps(self.home, _moved_pane_rows())
        ).run(
            RestoredPairRebindRequest(issue=ISSUE, lane=LANE, journal=JOURNAL),
            execute=True,
        )
        self.assertEqual(outcome.status, STATUS_COMPLETED, outcome.detail)
        pair = read_declared_pin_pair(self.record())
        self.assertEqual(pair.gateway.locator, GW_NEW)
        self.assertEqual(pair.worker.locator, WK_NEW)


class CliContract(unittest.TestCase):
    def test_parser_registers_the_rail_with_execute_off_by_default(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="sublane_command")
        register_sublane_adopt_restored_pair_parser(sub)
        args = parser.parse_args(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertFalse(args.execute)
        self.assertFalse(args.json)
        args = parser.parse_args(
            [
                "adopt-restored-pair", "--issue", ISSUE, "--lane", LANE,
                "--journal", JOURNAL, "--execute", "--json",
            ]
        )
        self.assertTrue(args.execute)
        self.assertTrue(args.json)
        self.assertEqual(args.journal, JOURNAL)

    def test_the_rail_is_registered_on_the_sublane_group(self):
        from mozyo_bridge.application.cli_common import add_repo_option
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_sublane_group import (  # noqa: E501
            register_sublane_group,
        )

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register_sublane_group(
            sub,
            add_repo_option=add_repo_option,
            add_lifecycle_json=lambda p: p.add_argument(
                "--lifecycle-json", action="store_true"
            ),
        )
        args = parser.parse_args(
            ["sublane", "adopt-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertEqual(args.sublane_command, "adopt-restored-pair")
        self.assertFalse(args.execute)


if __name__ == "__main__":
    unittest.main()
