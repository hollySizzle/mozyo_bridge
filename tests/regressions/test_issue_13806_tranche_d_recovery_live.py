"""Redmine #13806 tranche D R1-F1 — live stale-worker recovery adapters (isolated runtime).

The public ``sublane recover-stale`` command must actually observe the live inventory and drive
the real close/launch/attest + redispatch (review j#79528 F1). These tests pin the live adapters
with an ISOLATED / fake herdr runtime (a fake ``agent list`` + an isolated delivery ledger) — no
real managed worker is ever actuated (the tranche boundary), yet the real classification and the
real fail-closed rails are exercised: an unreadable / ambiguous inventory is never a positive
absence, a same-name recycle is never closed, and the redispatch is ledger-confirmed exactly once.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_delivery_ledger import (  # noqa: E402
    HerdrDeliveryLedger,
    HerdrDeliveryLedgerRecord,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live as live  # noqa: E402,E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E402,E501
    RecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    ATTEST_PENDING,
    CLOSE_DONE,
    CLOSE_ERROR,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_ABSENT,
    OLD_SLOT_AMBIGUOUS,
    OLD_SLOT_PRESENT,
    OLD_SLOT_RECYCLED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E402,E501
    RECOVER_ACTIONABLE,
    RECOVER_BLOCK_GATEWAY_OR_FOREIGN,
    RECOVER_BLOCK_NOT_STALE,
    RECOVER_BLOCK_PRODUCTIVE,
    RECOVER_BLOCK_STALE_GENERATION,
    RECOVER_BLOCK_UNKNOWN,
    RECOVER_BLOCK_WRONG_ISSUE_LANE,
    decide_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E402,E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

WS = "wsX"
LANE = "issue_13806_x"
ROLE = "claude"
LOCATOR = "w28:p35"
NAME = encode_assigned_name(WS, ROLE, LANE)


def _row(**overrides):
    row = {
        "name": NAME,
        "pane_id": LOCATOR,
        "agent": "",  # blank => shell residue (stale)
        "status": "unknown",  # RUNTIME_UNKNOWN => not productive
        "revision": 3,
        "foreground_cwd": str(ROOT),  # a real git checkout => worktree readable
    }
    row.update(overrides)
    return row


def _request(**overrides):
    base = dict(
        issue="13806", lane=LANE, role=ROLE, provider=ROLE, assigned_name=NAME,
        locator=LOCATOR, journal="79485", action_id="", action_generation=7,
        lane_revision="3", lane_generation="2", expected_gate="review_request",
        next_semantic_action="dispatch_once",
    )
    base.update(overrides)
    return RecoveryRequest(**base)


class _LiveCase(unittest.TestCase):
    def setUp(self):
        self._orig_rows = live.list_herdr_agent_rows
        self._orig_ws = live.repo_scope_workspace_id
        live.repo_scope_workspace_id = lambda root: WS

    def tearDown(self):
        live.list_herdr_agent_rows = self._orig_rows
        live.repo_scope_workspace_id = self._orig_ws

    def _ops(self, rows):
        live.list_herdr_agent_rows = lambda env: rows
        return live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=_request())


class ObserveTargetTests(_LiveCase):
    def test_full_stale_row_is_actionable(self):
        obs = self._ops([_row()]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_ACTIONABLE)

    def test_no_rows_is_identity_unknown(self):
        obs = self._ops([]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_UNKNOWN)

    def test_ambiguous_name_is_identity_unknown(self):
        rows = [_row(), _row(pane_id="w28:p99")]  # same name, two locators
        obs = self._ops(rows).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_UNKNOWN)

    def test_default_lane_is_gateway_or_foreign(self):
        name = encode_assigned_name(WS, ROLE, "default")
        obs = self._ops([_row(name=name)]).observe_target(
            _request(lane="default", assigned_name=name)
        )
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_GATEWAY_OR_FOREIGN)

    def test_same_issue_lane_gateway_is_gateway_or_foreign(self):
        # R2-F1: the same-issue-lane Codex GATEWAY sits in a non-default lane but is the gateway
        # provider — it must be protected, never classified as a standard worker.
        gw_name = encode_assigned_name(WS, "codex", LANE)
        row = {
            "name": gw_name, "pane_id": "w28:p34", "agent": "", "status": "unknown",
            "revision": 3, "foreground_cwd": str(ROOT),
        }
        live.list_herdr_agent_rows = lambda env: [row]
        ops = live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=_request())
        obs = ops.observe_target(
            _request(role="codex", provider="codex", assigned_name=gw_name, locator="w28:p34")
        )
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_GATEWAY_OR_FOREIGN)

    def test_wrong_issue_lane(self):
        name = encode_assigned_name(WS, ROLE, "issue_99999_other")
        obs = self._ops([_row(name=name)]).observe_target(
            _request(lane="issue_99999_other", assigned_name=name)
        )
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_WRONG_ISSUE_LANE)

    def test_live_agent_present_is_not_stale(self):
        # a detected provider agent => live, not shell residue
        obs = self._ops([_row(agent="claude", status="idle")]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_NOT_STALE)

    def test_working_agent_is_productive(self):
        obs = self._ops([_row(agent="claude", status="working")]).observe_target(_request())
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_PRODUCTIVE)

    def test_revision_mismatch_is_stale_generation(self):
        obs = self._ops([_row(revision=9)]).observe_target(_request(lane_revision="3"))
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_STALE_GENERATION)


class ObserveOldSlotTests(_LiveCase):
    def _port(self, rows):
        live.list_herdr_agent_rows = lambda env: rows
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        key = ReplacementTransactionKey(WS, "recover:k")
        return live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=_request(), store=store, key=key,
            lifecycle_home=Path(tempfile.mkdtemp()),  # isolated (empty) lane lifecycle store
        )

    def _pin(self):
        return ParticipantPin(
            lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
            lane_revision="3", lane_generation="2",
        )

    def _pin_no_lifecycle(self):
        return ParticipantPin(
            lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
        )

    def test_present(self):
        self.assertEqual(self._port([_row()]).observe_old_slot(self._pin()), OLD_SLOT_PRESENT)

    def test_absent(self):
        self.assertEqual(self._port([]).observe_old_slot(self._pin()), OLD_SLOT_ABSENT)

    def test_recycled_same_name_different_locator(self):
        self.assertEqual(
            self._port([_row(pane_id="w28:p77")]).observe_old_slot(self._pin()),
            OLD_SLOT_RECYCLED,
        )

    def test_ambiguous_multiple_exact(self):
        self.assertEqual(
            self._port([_row(), _row()]).observe_old_slot(self._pin()), OLD_SLOT_AMBIGUOUS
        )

    def test_unreadable_inventory_is_ambiguous_never_absent(self):
        def boom(env):
            raise RuntimeError("herdr down")

        live.list_herdr_agent_rows = boom
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        port = live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=_request(), store=store,
            key=ReplacementTransactionKey(WS, "recover:k"),
        )
        self.assertEqual(port.observe_old_slot(self._pin()), OLD_SLOT_AMBIGUOUS)

    def test_preservation_running_process_blocks(self):
        obs = self._port([_row(agent="claude", status="working")]).observe_preservation(
            self._pin_no_lifecycle()
        )
        self.assertTrue(obs.running_process)

    def test_preservation_identity_match_on_exact_row(self):
        # Observable identity (lane / role / name / locator) matches; the pin carries no lane
        # lifecycle evidence here, so the lifecycle fence is not exercised.
        obs = self._port([_row()]).observe_preservation(self._pin_no_lifecycle())
        self.assertTrue(obs.identity_matches)
        self.assertFalse(obs.running_process)

    def test_preservation_lifecycle_mismatch_blocks_close(self):
        # R1-F2: a pin carrying a lane lifecycle (revision, generation) that the LIVE lane
        # lifecycle store does not back (here: no record at all) fails the identity fence — the
        # close is blocked. Never a silent pass on missing lifecycle evidence.
        obs = self._port([_row()]).observe_preservation(self._pin())  # pin has revision/gen
        self.assertFalse(obs.identity_matches)


class ActuatorDelegationTests(_LiveCase):
    """close / launch / verify delegate to the reused #13763 live ops (injected fake here)."""

    def _port(self, fake_q, *, attestation_home=None):
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        store.plan_transaction(
            ReplacementTransactionKey(WS, "recover:k"),
            action_generation=7,
            decision=DecisionPointer(source="redmine", issue_id="13806", journal_id="79485"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="79485",
                expected_gate="g", next_semantic_action="n",
            ),
            participants=[ParticipantPin(
                lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
                lane_revision="3", lane_generation="2",
            )],
        )
        port = live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=_request(), store=store,
            key=ReplacementTransactionKey(WS, "recover:k"),
            attestation_home=attestation_home,
        )
        port._q = lambda: fake_q
        return port

    def _pin(self):
        return ParticipantPin(
            lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=LOCATOR,
            lane_revision="3", lane_generation="2",
        )

    def test_close_maps_closed_to_close_done(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            CloseReceiverResult, FreshReceiverVerification,
        )

        class FakeQ:
            def close_receiver(self, req, pin): return CloseReceiverResult(closed=True)
            def heal_receiver(self, req): return None
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        self.assertEqual(self._port(FakeQ()).close_exact_generation(self._pin()), CLOSE_DONE)

    def test_close_failure_maps_to_error(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            CloseReceiverResult,
        )

        class FakeQ:
            def close_receiver(self, req, pin):
                return CloseReceiverResult(closed=False, old_absent=False, detail="close_failed")

        self.assertEqual(self._port(FakeQ()).close_exact_generation(self._pin()), CLOSE_ERROR)

    def test_launch_error_on_exception(self):
        # launch_action_bound constructs the herdr lane actuator with the replacement action id
        # (never the plain heal_receiver, which drops it). A launch failure => LAUNCH_ERROR.
        class FailingActuator:
            def __init__(self, **kwargs):
                # the recovery MUST carry the exact action id into the fresh launch
                assert kwargs.get("replacement_action_id") == "a"
            def heal_lane_column(self, worktree_path):
                raise RuntimeError("launch failed")

        orig = live.HerdrSublaneActuatorOps
        live.HerdrSublaneActuatorOps = FailingActuator
        try:
            self.assertEqual(
                self._port(object()).launch_action_bound("a", self._pin()), LAUNCH_ERROR
            )
        finally:
            live.HerdrSublaneActuatorOps = orig

    def test_launch_carries_action_id(self):
        seen = {}

        class CapturingActuator:
            def __init__(self, **kwargs):
                seen["action_id"] = kwargs.get("replacement_action_id")
            def heal_lane_column(self, worktree_path):
                return None

        orig = live.HerdrSublaneActuatorOps
        live.HerdrSublaneActuatorOps = CapturingActuator
        try:
            self.assertEqual(
                self._port(object()).launch_action_bound("act-9", self._pin()), LAUNCH_DONE
            )
        finally:
            live.HerdrSublaneActuatorOps = orig
        self.assertEqual(seen["action_id"], "act-9")

    def _seed_attestation(self, home, *, action_id):
        HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
            assigned_name=NAME, workspace_id=WS, role=ROLE, lane_id=LANE, locator="w28:p88",
            verdict="present", replacement_action_id=action_id,
        ))

    def test_verify_bound_on_exact_action_match(self):
        # R2-F2: fresh identity AND the fresh startup attestation binds THIS action -> bound.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class OkQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        att = Path(tempfile.mkdtemp())
        self._seed_attestation(att, action_id="act-1")
        port = self._port(OkQ(), attestation_home=att)
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_BOUND)

    def test_verify_mismatch_on_different_action(self):
        # R2-F2: a fresh, attested slot whose startup bound a DIFFERENT action -> mismatch.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class OkQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        att = Path(tempfile.mkdtemp())
        self._seed_attestation(att, action_id="other-action")
        port = self._port(OkQ(), attestation_home=att)
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_MISMATCH)

    def test_verify_pending_when_no_attestation_record(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class OkQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=True, locator="w28:p88")

        port = self._port(OkQ(), attestation_home=Path(tempfile.mkdtemp()))  # empty store
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_PENDING)

    def test_verify_pending_when_not_fresh(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            FreshReceiverVerification,
        )

        class PendingQ:
            def verify_fresh_receiver(self, req, *, fresh_after):
                return FreshReceiverVerification(ok=False, detail="not_fresh")

        att = Path(tempfile.mkdtemp())
        self._seed_attestation(att, action_id="act-1")
        port = self._port(PendingQ(), attestation_home=att)
        self.assertEqual(port.verify_attestation("act-1", self._pin()), ATTEST_PENDING)


class RedispatchLedgerTests(_LiveCase):
    """R2-F3: gate_redispatched confirms ONLY a real, fresh-target, post-launch, accepted send."""

    ISSUE, JOURNAL = "13806", "79485"
    LAUNCH_AT = "2026-07-15T12:00:00+00:00"
    AFTER = "2026-07-15T12:05:00+00:00"
    BEFORE = "2026-07-15T11:00:00+00:00"

    def _ops(self, ledger, att_home):
        live.list_herdr_agent_rows = lambda env: [_row()]
        return live.LiveStaleWorkerRecoveryOps(
            repo_root=ROOT, request=_request(), ledger=ledger, attestation_home=att_home,
        )

    def _continuation(self):
        return ContinuationPointer(
            source="redmine", issue_id=self.ISSUE, journal_id=self.JOURNAL,
            expected_gate="review_request", next_semantic_action="dispatch_once",
        )

    def _seed_launch(self, att_home, observed_at=LAUNCH_AT):
        HerdrIdentityAttestationStore(home=att_home).upsert(IdentityAttestationRecord(
            assigned_name=NAME, workspace_id=WS, role=ROLE, lane_id=LANE, locator=LOCATOR,
            verdict="present", observed_at=observed_at, replacement_action_id="a",
        ))

    def _delivered(self, **over):
        base = dict(
            issue_id=self.ISSUE, journal_id=self.JOURNAL, status="sent", reason="ok",
            receiver=ROLE, target=LOCATOR, recorded_at=self.AFTER,
        )
        base.update(over)
        return HerdrDeliveryLedgerRecord(**base)

    def test_confirmed_when_accepted_delivery_after_launch(self):
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        att = Path(tempfile.mkdtemp())
        self._seed_launch(att)
        ops = self._ops(ledger, att)
        self.assertFalse(ops.gate_redispatched(self._continuation()))  # nothing yet
        ledger.append(self._delivered())
        self.assertTrue(ops.gate_redispatched(self._continuation()))

    def test_queue_enter_reason_is_not_confirmed(self):
        # A submit-unconfirmed (queue_enter) delivery is NOT a landing (R2-F3 cond 4).
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        att = Path(tempfile.mkdtemp())
        self._seed_launch(att)
        ledger.append(self._delivered(reason="queue_enter"))
        self.assertFalse(self._ops(ledger, att).gate_redispatched(self._continuation()))

    def test_delivery_before_launch_is_the_old_worker_not_confirmed(self):
        # The INITIAL delivery to the now-vanished old worker (same anchor, before the recovery
        # launch) must never be mistaken for the redispatch (R2-F3 cond 3 / "same anchor").
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        att = Path(tempfile.mkdtemp())
        self._seed_launch(att)
        ledger.append(self._delivered(recorded_at=self.BEFORE))
        self.assertFalse(self._ops(ledger, att).gate_redispatched(self._continuation()))

    def test_no_fresh_attestation_is_not_confirmed(self):
        # Without a fresh-launch boundary the redispatch cannot be distinguished => unconfirmed.
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        ledger.append(self._delivered())
        self.assertFalse(
            self._ops(ledger, Path(tempfile.mkdtemp())).gate_redispatched(self._continuation())
        )

    def test_wrong_receiver_is_not_confirmed(self):
        ledger = HerdrDeliveryLedger(home=Path(tempfile.mkdtemp()))
        att = Path(tempfile.mkdtemp())
        self._seed_launch(att)
        ledger.append(self._delivered(receiver="codex"))  # gateway, not the worker
        self.assertFalse(self._ops(ledger, att).gate_redispatched(self._continuation()))

    def test_redispatch_gate_without_fresh_worker_is_error(self):
        # No fresh worker resolved (only the old locator) => never dispatch blind.
        live.list_herdr_agent_rows = lambda env: []
        ops = live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=_request())
        self.assertEqual(ops.redispatch_gate(self._continuation()), DRAIN_SEND_ERROR)


class LaunchArgvActionIdTest(unittest.TestCase):
    """R2-F2: the wrapper argv carries --replacement-action-id ONLY for a replacement launch."""

    def _build(self, replacement_action_id):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            build_agent_start_argv,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            ResolvedProviderLaunch,
        )

        return build_agent_start_argv(
            assigned_name=NAME, provider=ROLE, repo_root=ROOT, workspace_id=WS, lane=LANE,
            target_workspace="wZ", target_tab="", split="", focus=False, binary="/x/herdr",
            attest_launcher="/x/mozyo-bridge", store_home="/tmp/h",
            resolved=ResolvedProviderLaunch(
                provider_id=ROLE, executable="/x/claude", managed_argv=("/x/claude",)
            ),
            launch_argv_extra=(),
            replacement_action_id=replacement_action_id,
        )

    def test_replacement_launch_emits_flag(self):
        argv = self._build("recover:xyz")
        self.assertIn("--replacement-action-id", argv)
        self.assertEqual(argv[argv.index("--replacement-action-id") + 1], "recover:xyz")
        # the capability marker (--assigned-name) is still the first wrapper flag
        self.assertLess(argv.index("--assigned-name"), argv.index("--replacement-action-id"))

    def test_normal_launch_is_byte_invariant(self):
        self.assertNotIn("--replacement-action-id", self._build(""))


if __name__ == "__main__":
    unittest.main()
