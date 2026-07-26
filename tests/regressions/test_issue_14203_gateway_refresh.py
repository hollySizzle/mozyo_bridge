"""Regression: guarded gateway refresh use case (Redmine #14203).

Pins ``sublane recover-gateway``'s execute discipline over the #13806 replacement-transaction
machinery: preflight never writes; every approval axis is fail-closed; the actuation closes
ONLY the exact pinned gateway generation; the resume drives the EXISTING durable anchor
exactly once through the shared continuation-drain authority (idempotency-first, record
attempted before the send, action-time authority re-join, never a blind resend); a stopped
leg holds the durable replay fence and a post-close replay is admitted ONLY on the expected
``identity_unknown`` + a committed-close transaction. Fakes only — no live process, no herdr.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PreservationObservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E402,E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_continuation_drain import (  # noqa: E402,E501
    CONTINUATION_AUTHORITY_MOVED,
    CONTINUATION_CONFIRMED,
    CONTINUATION_SEND_FAILED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E402,E501
    GatewayRefreshRequest,
    GatewayRefreshUseCase,
    REFRESH_STATUS_COMPLETED,
    REFRESH_STATUS_PREFLIGHT,
    REFRESH_STATUS_REFUSED,
    REFRESH_STATUS_STOPPED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E402,E501
    GatewayRefreshObservation,
    GatewayTurnObservation,
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_NON_GATEWAY,
    REFRESH_BLOCK_TURN_NOT_FAILED,
    REFRESH_BLOCK_UNKNOWN,
    TURN_CLASS_FAILED,
    TURN_CLASS_UNCONFIRMED,
    TURN_REASON_RATE_LIMIT,
    TURN_REASON_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    CLOSE_DONE,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_PRESENT,
)

GEN = 3
FIXED = "2026-07-24T12:00:00+00:00"
GATEWAY = dict(
    lane_id="issue_x_lane", role="codex", provider="codex", assigned_name="gw",
    old_locator="w:3",
)
ACTION_ID = "refresh-gateway:issue_x_lane:codex:codex:gw:w:3:r4"


def _turn(**overrides) -> GatewayTurnObservation:
    facts = dict(
        delivery_confirmed=True, turn_started=True, settled_turn_ended=True,
        expected_gate_absent=True, durable_source_fresh=True,
    )
    facts.update(overrides)
    return GatewayTurnObservation(**facts)


def _target(**overrides) -> GatewayRefreshObservation:
    facts = dict(
        identity_resolved=True, is_lane_implementation_gateway=True,
        issue_lane_matches=True, generation_matches=True, settled_idle=True,
        composer_clear=True, resume_anchor_present=True,
        worker_distinct_preserved=True, no_authority_conflict=True,
    )
    facts.update(overrides)
    return GatewayRefreshObservation(**facts)


class FakeActuatorPort:
    """A synthetic ExactGenerationActuatorPort — no live process, no DB."""

    def __init__(self):
        self.close_result: dict[tuple, str] = {}
        self.launch_result: dict[tuple, str] = {}
        self.closed: list[tuple] = []
        self.launched: list[tuple[str, tuple]] = []
        self._pres = PreservationObservation(identity_matches=True, attestation_fresh=True)

    def observe_old_slot(self, pin) -> str:
        return OLD_SLOT_PRESENT

    def observe_preservation(self, pin) -> PreservationObservation:
        return self._pres

    def close_exact_generation(self, pin) -> str:
        self.closed.append(pin.identity)
        return self.close_result.get(pin.identity, CLOSE_DONE)

    def launch_action_bound(self, action_id: str, pin) -> str:
        self.launched.append((action_id, pin.identity))
        return self.launch_result.get(pin.identity, LAUNCH_DONE)

    def verify_attestation(self, action_id: str, pin) -> str:
        return ATTEST_BOUND


class FakeGatewayOps:
    """A synthetic GatewayRecoveryOps — fixed observations + a recorded resume rail."""

    def __init__(
        self,
        turn=None,
        target=None,
        *,
        send_result=DRAIN_SEND_OK,
        confirm_after_send=True,
        already_landed=False,
        lane_authority=True,
        name_free=True,
        rail_ready=True,
    ):
        self._turn = turn if turn is not None else _turn()
        self._target = target if target is not None else _target()
        self.send_result = send_result
        self.confirm_after_send = confirm_after_send
        self.resumes: list = []
        self._landed = already_landed
        self._lane_authority = lane_authority
        self.authority_checks: list = []
        self._name_free = name_free
        self.name_free_checks: list = []
        self._rail_ready = rail_ready

    def observe_turn(self, request) -> GatewayTurnObservation:
        return self._turn

    def observe_target(self, request) -> GatewayRefreshObservation:
        return self._target

    def resume_lane_authority(self, request) -> bool:
        self.authority_checks.append(request)
        v = self._lane_authority
        if isinstance(v, list):
            return v.pop(0) if v else True
        return v

    def gateway_name_free_of_live_process(self, request) -> bool:
        self.name_free_checks.append(request)
        return self._name_free

    def resume_rail_ready(self, request) -> bool:
        return self._rail_ready

    def resume_confirmed(self, continuation) -> bool:
        return self._landed

    def resume_once(self, continuation) -> str:
        self.resumes.append(continuation)
        if self.send_result == DRAIN_SEND_OK and self.confirm_after_send:
            self._landed = True
        return self.send_result


class _RefreshCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.workspace_id = "ws"
        self.port = FakeActuatorPort()

    def _request(self, **overrides) -> GatewayRefreshRequest:
        base = dict(
            issue="14203", lane=GATEWAY["lane_id"], role=GATEWAY["role"],
            provider=GATEWAY["provider"], assigned_name=GATEWAY["assigned_name"],
            locator=GATEWAY["old_locator"], journal="84223", action_id=ACTION_ID,
            action_generation=GEN, gateway_revision="4",
            lane_revision="5", lane_generation="2",
            resume_anchor_journal="87251", resume_gate="review_request",
        )
        base.update(overrides)
        return GatewayRefreshRequest(**base)

    def _use_case(self, ops):
        return GatewayRefreshUseCase(
            self.store, self.port, ops, workspace_id=self.workspace_id,
            clock=lambda: FIXED,
        )

    def _row(self):
        return self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID))


class PreflightTests(_RefreshCase):
    def test_preflight_classifies_and_writes_nothing(self):
        ops = FakeGatewayOps()
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.status, REFRESH_STATUS_PREFLIGHT)
        self.assertEqual(outcome.turn_class, TURN_CLASS_FAILED)
        self.assertEqual(outcome.verdict, REFRESH_ACTIONABLE)
        self.assertFalse(outcome.executed)
        self.assertFalse(outcome.is_blocked)
        self.assertIsNone(self._row())          # zero writes
        self.assertEqual(self.port.closed, [])  # zero closes
        self.assertEqual(ops.resumes, [])       # zero sends

    def test_the_reason_is_normalized_fail_closed(self):
        ops = FakeGatewayOps(turn=_turn(reason_token="429 raw provider text"))
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.turn_reason, TURN_REASON_UNKNOWN)
        ops = FakeGatewayOps(turn=_turn(reason_token=TURN_REASON_RATE_LIMIT))
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.turn_reason, TURN_REASON_RATE_LIMIT)


class ExecuteRefusalTests(_RefreshCase):
    def _refused(self, ops, request, needle: str):
        outcome = self._use_case(ops).run(request, execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertIn(needle, outcome.detail)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(ops.resumes, [])
        return outcome

    def test_a_not_actionable_target_refuses_with_zero_close(self):
        ops = FakeGatewayOps(target=_target(is_lane_implementation_gateway=False))
        outcome = self._refused(ops, self._request(), "not actionable")
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_NON_GATEWAY)

    def test_an_unconfirmed_turn_refuses_even_with_a_clean_slot(self):
        # The #14219 false-negative made structural: delivered_not_started-shaped evidence
        # (no positive delivery confirmation) NEVER closes a gateway.
        ops = FakeGatewayOps(turn=_turn(delivery_confirmed=False))
        outcome = self._refused(ops, self._request(), "not actionable")
        self.assertEqual(outcome.turn_class, TURN_CLASS_UNCONFIRMED)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_TURN_NOT_FAILED)

    def test_an_incomplete_approval_pointer_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(journal=""), "not a complete Redmine pointer"
        )

    def test_a_mismatched_action_id_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(action_id="refresh-gateway:other"),
            "does not match",
        )

    def test_a_non_positive_generation_refuses(self):
        self._refused(FakeGatewayOps(), self._request(action_generation=0), "generation")

    def test_missing_lane_lifecycle_evidence_refuses(self):
        self._refused(FakeGatewayOps(), self._request(lane_revision=""), "lifecycle")

    def test_a_non_resumable_gate_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(resume_gate="bogus_gate"), "not a resumable"
        )

    def test_a_missing_resume_anchor_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(resume_anchor_journal=""),
            "resume anchor pointer is incomplete",
        )

    def test_a_missing_gateway_revision_refuses_before_any_write(self):
        # Review j#87364 F5: the row revision is a REQUIRED destructive authority component.
        self._refused(
            FakeGatewayOps(),
            self._request(gateway_revision="", action_id="refresh-gateway:x"),
            "exact gateway generation",
        )

    def test_an_unready_resume_rail_refuses_before_any_close(self):
        # Review j#87364 F2: the resume capability is verified BEFORE the destructive close.
        ops = FakeGatewayOps(rail_ready=False)
        outcome = self._refused(ops, self._request(), "resume_rail_unavailable")
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)

    def test_a_diverged_preexisting_row_is_an_authority_conflict(self):
        ops = FakeGatewayOps()
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, REFRESH_STATUS_COMPLETED)
        # A second authority for the SAME slot at a different resume anchor must refuse.
        ops2 = FakeGatewayOps()
        outcome = self._use_case(ops2).run(
            self._request(resume_anchor_journal="99999"), execute=True
        )
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertIn("different refresh authority", outcome.detail)
        self.assertEqual(ops2.resumes, [])


class HappyPathTests(_RefreshCase):
    def test_close_launch_attest_resume_exactly_once(self):
        ops = FakeGatewayOps()
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertTrue(outcome.closed_old_gateway)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertEqual(outcome.resume_status, CONTINUATION_CONFIRMED)
        # Exactly the pinned gateway was closed / relaunched — nothing else.
        identity = (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        )
        self.assertEqual(self.port.closed, [identity])
        self.assertEqual([pin for _a, pin in self.port.launched], [identity])
        # The launch was bound to THIS refresh action.
        self.assertEqual(self.port.launched[0][0], ACTION_ID)
        # The resume fired exactly once, carrying the EXISTING anchor (never regenerated).
        self.assertEqual(len(ops.resumes), 1)
        continuation = ops.resumes[0]
        self.assertEqual(continuation.journal_id, "87251")
        self.assertEqual(continuation.expected_gate, "review_request")

    def test_a_rerun_after_completion_is_idempotent_zero_send(self):
        ops = FakeGatewayOps()
        self._use_case(ops).run(self._request(), execute=True)
        rerun_ops = FakeGatewayOps(already_landed=True, target=_target())
        outcome = self._use_case(rerun_ops).run(self._request(), execute=True)
        # The transaction is already completed; the drive confirms with ZERO new close/send.
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(rerun_ops.resumes, [])
        self.assertEqual(self.port.closed, [ (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        ) ])  # only the FIRST run's close — no second close

    def test_an_already_landed_resume_completes_with_zero_send(self):
        ops = FakeGatewayOps(already_landed=True)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(ops.resumes, [])  # idempotency-first: never a duplicate delivery


class StoppedLegTests(_RefreshCase):
    def test_a_failed_launch_stops_with_the_replay_fence_held(self):
        identity = (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        )
        self.port.launch_result[identity] = LAUNCH_ERROR
        ops = FakeGatewayOps()
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertTrue(outcome.closed_old_gateway)   # the close committed
        self.assertFalse(outcome.fresh_slot_attested)
        self.assertEqual(ops.resumes, [])             # NO resume behind a failed launch
        self.assertIsNotNone(self._row())             # the durable replay fence stands

    def test_a_post_close_replay_is_admitted_only_on_identity_unknown(self):
        # Crash between close and launch: the old gateway is expectedly absent. A replay
        # whose preflight blocks identity_unknown + a committed-close transaction resumes
        # the owed launch/attest/resume; any OTHER blocker stands.
        identity = (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        )
        self.port.launch_result[identity] = LAUNCH_ERROR
        self._use_case(FakeGatewayOps()).run(self._request(), execute=True)
        del self.port.launch_result[identity]
        # Replay: the pinned old locator no longer resolves -> identity_unknown preflight.
        replay_ops = FakeGatewayOps(target=GatewayRefreshObservation())
        outcome = self._use_case(replay_ops).run(self._request(), execute=True)
        self.assertTrue(outcome.post_close_resume)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(len(replay_ops.resumes), 1)
        # A NON-identity-unknown blocker is a real fence: it refuses even with the txn.
        blocked_ops = FakeGatewayOps(
            target=_target(composer_clear=False), already_landed=True
        )
        blocked = self._use_case(blocked_ops).run(self._request(), execute=True)
        self.assertEqual(blocked.status, REFRESH_STATUS_REFUSED)
        self.assertFalse(blocked.post_close_resume)

    def test_a_failed_resume_send_stops_without_blind_resend(self):
        ops = FakeGatewayOps(send_result=DRAIN_SEND_ERROR)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertEqual(outcome.resume_status, CONTINUATION_SEND_FAILED)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertEqual(len(ops.resumes), 1)  # exactly one attempt — never repeated blind

    def test_an_authority_move_before_the_resume_is_a_typed_zero_send(self):
        # Authority holds for the launch probe (twice: lane + name checks share the fn) and
        # MOVES immediately before the resume transport: the attempt is un-recorded and the
        # resume reports authority_moved with ZERO send.
        ops = FakeGatewayOps(lane_authority=[True, False])
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertEqual(outcome.resume_status, CONTINUATION_AUTHORITY_MOVED)
        self.assertEqual(ops.resumes, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
