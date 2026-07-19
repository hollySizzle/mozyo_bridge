"""Redmine #13806 tranche D — public stale standard-sublane worker recovery surface.

The coordinator-alive recovery the residual j#79435 found missing (Implementation Request
j#79485): a public ``sublane recover-stale`` use case that recovers the exact stale worker of
a lane — read-only preflight by default, an owner-approved guarded close → same-slot fresh
launch → action-bound attestation → exactly-once redispatch of the original gate under
``--execute`` — connected to the existing tranche A/B/C primitives. It never closes the lane
gateway / a foreign slot / the current coordinator, byte-preserves the worktree, and never
blind-resends the redispatch.

Pins the Implementation Request verification matrix: shell-residue success, foreground
provider / tool-child conflict, stale generation, wrong issue-lane, gateway / foreign
protection, dirty-worktree byte preservation, close-then-launch-failure partial, attestation
failure, duplicate replay, the original-anchor redispatch fence, and restart recovery — plus
the guarded worker-recovery DAG tail (a self-replacement can never take the shortcut). All
state lives under an isolated home; the live process mutation is faked (non-scope, j#79485).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PRESERVE_IDENTITY_MISMATCH,
    PRESERVE_RUNNING_PROCESS,
    PreservationObservation,
    assess_worker_recovery_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANT_LAUNCH_OWED,
    PARTICIPANT_REPLACED,
    PARTICIPANT_VERIFY_OWED,
    PHASE_CLAIMED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
    transaction_phase_prerequisite_met,
    transaction_transition_allowed,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E402,E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E402,E501
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E402,E501
    RECOVERY_COMPLETED,
    RECOVERY_PREFLIGHT,
    RECOVERY_REFUSED,
    RECOVERY_STOPPED,
    REDISPATCH_CONFIRMED,
    REDISPATCH_UNCERTAIN,
    RecoveryRequest,
    StaleWorkerRecoveryOps,
    StaleWorkerRecoveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ACTUATION_INVALID_TOPOLOGY,
    ACTUATION_RECOVERED,
    ATTEST_BOUND,
    ATTEST_PENDING,
    CLOSE_DONE,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_PRESENT,
    OLD_SLOT_RECYCLED,
    is_self_replacement_topology,
    is_worker_recovery_topology,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E402,E501
    RECOVER_ACTIONABLE,
    RECOVER_BLOCK_AUTHORITY_CONFLICT,
    RECOVER_BLOCK_DIRTY_UNREADABLE,
    RECOVER_BLOCK_GATEWAY_OR_FOREIGN,
    RECOVER_BLOCK_NOT_STALE,
    RECOVER_BLOCK_PRODUCTIVE,
    RECOVER_BLOCK_STALE_GENERATION,
    RECOVER_BLOCK_UNKNOWN,
    RECOVER_BLOCK_WRONG_ISSUE_LANE,
    RecoveryObservation,
    decide_recovery,
    stale_worker_recovery_action_id,
    worker_close_committed,
)

GEN = 7
FIXED = "2026-07-15T12:00:00+00:00"

WORKER = dict(
    lane_id="l", role="worker", provider="claude", assigned_name="wk", old_locator="w:2"
)
ACTION_ID = "recover:l:worker:claude:wk:w:2"


def _all_clear(**overrides) -> RecoveryObservation:
    facts = dict(
        identity_resolved=True,
        is_standard_sublane_worker=True,
        issue_lane_matches=True,
        generation_matches=True,
        not_productive=True,
        is_stale=True,
        worktree_readable=True,
        no_authority_conflict=True,
    )
    facts.update(overrides)
    return RecoveryObservation(**facts)


class FakeActuatorPort:
    """A synthetic ExactGenerationActuatorPort — no live process, no DB."""

    def __init__(self):
        self.old: dict[tuple, str] = {}
        self.attest: dict[tuple, str] = {}
        self.close_result: dict[tuple, str] = {}
        self.launch_result: dict[tuple, str] = {}
        self.pres: dict[tuple, PreservationObservation] = {}
        self.closed: list[tuple] = []
        self.launched: list[tuple[str, tuple]] = []
        self._default_pres = PreservationObservation(
            identity_matches=True, attestation_fresh=True
        )

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        return self.old.get(pin.identity, OLD_SLOT_PRESENT)

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        return self.pres.get(pin.identity, self._default_pres)

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        self.closed.append(pin.identity)
        return self.close_result.get(pin.identity, CLOSE_DONE)

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        self.launched.append((action_id, pin.identity))
        return self.launch_result.get(pin.identity, LAUNCH_DONE)

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        return self.attest.get(pin.identity, ATTEST_BOUND)


class FakeRecoveryOps:
    """A synthetic StaleWorkerRecoveryOps — a fixed observation + a redispatch rail."""

    def __init__(
        self, observation, *, send_result=DRAIN_SEND_OK, confirm_after_send=True,
        already_landed=False, resume_lane_authority=True, lane_free_of_live_process=True,
    ):
        self._observation = observation
        self.send_result = send_result
        self.confirm_after_send = confirm_after_send
        self.sends: list = []
        self._landed = already_landed
        #: The exact action-time lane authority a resume re-joins immediately before each owed
        #: effect (R3-F1). A bool (constant) or a list consumed per call (then default True) so a
        #: test can make the authority move BETWEEN the launch and the send.
        self._resume_lane_authority = resume_lane_authority
        self.authority_checks: list = []
        #: The pre-launch lane-free-of-live fence (R3-F1). Default free; a test sets it False to
        #: simulate a foreign live process (busy OR idle) holding the lane's name.
        self._lane_free_of_live_process = lane_free_of_live_process
        self.free_of_live_checks: list = []

    def observe_target(self, request) -> RecoveryObservation:
        return self._observation

    def redispatch_gate(self, continuation) -> str:
        self.sends.append(continuation)
        if self.send_result == DRAIN_SEND_OK and self.confirm_after_send:
            self._landed = True
        return self.send_result

    def gate_redispatched(self, continuation) -> bool:
        return self._landed

    def resume_lane_authority(self, request) -> bool:
        self.authority_checks.append(request)
        v = self._resume_lane_authority
        if isinstance(v, list):
            return v.pop(0) if v else True
        return v

    def lane_free_of_live_process(self, request) -> bool:
        self.free_of_live_checks.append(request)
        return self._lane_free_of_live_process


class _RecoveryCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.workspace_id = "ws"
        self.port = FakeActuatorPort()

    def _request(self, **overrides) -> RecoveryRequest:
        base = dict(
            issue="13806", lane=WORKER["lane_id"], role=WORKER["role"],
            provider=WORKER["provider"], assigned_name=WORKER["assigned_name"],
            locator=WORKER["old_locator"], journal="79485", action_id=ACTION_ID,
            action_generation=GEN, lane_revision="3", lane_generation="2",
            expected_gate="implementation_request", next_semantic_action="dispatch_once",
        )
        base.update(overrides)
        return RecoveryRequest(**base)

    def _use_case(self, ops):
        return StaleWorkerRecoveryUseCase(
            self.store, self.port, ops, workspace_id=self.workspace_id,
            clock=lambda: FIXED,
        )

    def _worker_pin_phase(self):
        key = ReplacementTransactionKey(self.workspace_id, ACTION_ID)
        rec = self.store.get(key)
        if rec is None:
            return None
        pin = rec.find_participant(
            (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        )
        return pin.phase if pin else None

    def _phase(self):
        rec = self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID))
        return rec.phase if rec else None


# -- 1. shell-residue success (full happy path) ---------------------------------


class HappyPathTests(_RecoveryCase):
    def test_preflight_actionable_writes_nothing(self):
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.status, RECOVERY_PREFLIGHT)
        self.assertEqual(outcome.verdict, RECOVER_ACTIONABLE)
        self.assertFalse(outcome.executed)
        self.assertFalse(outcome.is_blocked)
        # zero store write on preflight
        self.assertIsNone(self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID)))
        self.assertEqual(self.port.closed, [])

    def test_execute_closes_relaunches_attests_and_redispatches_once(self):
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)
        self.assertEqual(outcome.recovery_status, ACTUATION_RECOVERED)
        self.assertEqual(outcome.redispatch_status, REDISPATCH_CONFIRMED)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertTrue(outcome.closed_old_worker)
        self.assertFalse(outcome.is_blocked)
        # the exact worker was closed once, relaunched action-bound, and completed
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.assertEqual(self.port.closed, [wk_id])
        self.assertEqual([a for a, _ in self.port.launched], [ACTION_ID])
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_REPLACED)
        self.assertEqual(self._phase(), PHASE_COMPLETED)
        # redispatched exactly once
        self.assertEqual(len(ops.sends), 1)

    def test_lease_released_on_completion(self):
        ops = FakeRecoveryOps(_all_clear())
        self._use_case(ops).run(self._request(), execute=True)
        rec = self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID))
        self.assertEqual(rec.lease_holder, "")  # handed back after completion


# -- 2-6. typed zero-close blockers ---------------------------------------------


class BlockerTests(_RecoveryCase):
    def _execute_block(self, observation):
        ops = FakeRecoveryOps(observation)
        return self._use_case(ops).run(self._request(), execute=True)

    def test_productive_provider_or_tool_child_is_zero_close(self):
        outcome = self._execute_block(_all_clear(not_productive=False))
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_PRODUCTIVE)
        self.assertTrue(outcome.is_blocked)
        self.assertEqual(self.port.closed, [])
        self.assertIsNone(self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID)))

    def test_stale_generation_is_zero_close(self):
        outcome = self._execute_block(_all_clear(generation_matches=False))
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_STALE_GENERATION)
        self.assertEqual(self.port.closed, [])

    def test_wrong_issue_lane_is_zero_close(self):
        outcome = self._execute_block(_all_clear(issue_lane_matches=False))
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_WRONG_ISSUE_LANE)
        self.assertEqual(self.port.closed, [])

    def test_gateway_or_foreign_is_zero_close(self):
        outcome = self._execute_block(_all_clear(is_standard_sublane_worker=False))
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_GATEWAY_OR_FOREIGN)
        self.assertEqual(self.port.closed, [])

    def test_unknown_identity_is_zero_close(self):
        outcome = self._execute_block(_all_clear(identity_resolved=False))
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_UNKNOWN)
        self.assertEqual(self.port.closed, [])

    def test_unreadable_worktree_is_zero_close(self):
        outcome = self._execute_block(_all_clear(worktree_readable=False))
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_DIRTY_UNREADABLE)
        self.assertEqual(self.port.closed, [])

    def test_not_stale_is_zero_close(self):
        outcome = self._execute_block(_all_clear(is_stale=False))
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_NOT_STALE)
        self.assertEqual(self.port.closed, [])

    def test_dirty_but_readable_worktree_is_actionable_and_preserved(self):
        # A DIRTY (but readable) worktree is recovered and byte-preserved: the actuator has no
        # worktree-mutating surface at all, so the fresh launch touches only the process.
        ops = FakeRecoveryOps(_all_clear(worktree_readable=True))
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)
        self.assertEqual(outcome.verdict, RECOVER_ACTIONABLE)

    def test_stale_worker_with_dirty_unrecorded_worktree_closes_present_slot(self):
        # The CENTRAL tranche D case: the stale worker's pane is still PRESENT (shell residue),
        # its worktree is DIRTY + UNRECORDED, and the old slot's attestation is stale — exactly
        # what the self-replacement preservation fence blocks on. The worker-recovery fence must
        # NOT block it (the dirty worktree is preserved by never being touched): the dead pane
        # is closed and a fresh worker relaunched.
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.pres[wk_id] = PreservationObservation(
            dirty_diff=True, unrecorded_journal=True,
            identity_matches=True, attestation_fresh=False,  # stale slot => no fresh attest
        )
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)
        self.assertEqual(self.port.closed, [wk_id])  # the dead pane WAS closed

    def test_running_worker_is_still_blocked_at_close_boundary(self):
        # Defence-in-depth: if the slot is actually WORKING at the close boundary (running
        # process), the worker-recovery fence still refuses the close — never destroy live work.
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.pres[wk_id] = PreservationObservation(
            running_process=True, identity_matches=True, attestation_fresh=True,
        )
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_STOPPED)
        self.assertEqual(outcome.recovery_status, "preservation_blocked")
        self.assertEqual(self.port.closed, [])  # zero close

    def test_identity_mismatch_is_blocked_at_close_boundary(self):
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.pres[wk_id] = PreservationObservation(
            identity_matches=False, attestation_fresh=True,  # observed slot is not the pin
        )
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_STOPPED)
        self.assertEqual(self.port.closed, [])


# -- owner-approval fences (before any close) -----------------------------------


class ApprovalFenceTests(_RecoveryCase):
    def test_action_id_mismatch_refused_zero_close(self):
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(
            self._request(action_id="recover:wrong"), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("action id", outcome.detail)
        self.assertEqual(self.port.closed, [])

    def test_incomplete_approval_journal_refused(self):
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(journal=""), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertEqual(self.port.closed, [])

    def test_non_positive_generation_refused(self):
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(
            self._request(action_generation=0), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertEqual(self.port.closed, [])

    def test_missing_lane_revision_is_zero_close(self):
        # R1-F2: a destructive worker recovery must carry the exact lane lifecycle evidence.
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(lane_revision=""), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("lifecycle", outcome.detail)
        self.assertEqual(self.port.closed, [])
        # nothing planned
        self.assertIsNone(self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID)))

    def test_missing_lane_generation_is_zero_close(self):
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(lane_generation=""), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertEqual(self.port.closed, [])

    def test_non_implementation_request_gate_is_zero_send(self):
        # R3-F1: the redispatch only delivers an implementation_request to the worker, so a
        # continuation pointing at a different gate is a zero-send typed blocker (the send kind
        # and the pointer gate kind must be one token).
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(
            self._request(expected_gate="review_request"), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("redispatchable worker gate", outcome.detail)
        self.assertEqual(self.port.closed, [])

    def test_wrong_semantic_action_is_zero_close_send_write(self):
        # R4-F1: the redispatch drives a fixed dispatch-once effect, so a continuation declaring
        # a different next_semantic_action is a zero-close / zero-send typed blocker; nothing is
        # closed, sent, or planned to the durable transaction.
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(
            self._request(next_semantic_action="not_dispatch_once"), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("redispatchable worker action", outcome.detail)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(ops.sends, [])
        self.assertIsNone(self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID)))

    def test_authority_conflict_when_foreign_generation_already_planned(self):
        # A DIFFERENT approved generation already owns this worker's transaction key.
        key = ReplacementTransactionKey(self.workspace_id, ACTION_ID)
        self.store.plan_transaction(
            key, action_generation=GEN + 1,
            decision=DecisionPointer(source="redmine", issue_id="13806", journal_id="1"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="1",
                expected_gate="g", next_semantic_action="n",
            ),
            participants=[ParticipantPin(**WORKER)],
        )
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("different recovery authority", outcome.detail)
        self.assertEqual(self.port.closed, [])

    def test_same_key_but_different_lane_evidence_is_refused_not_silently_resumed(self):
        # The key omits the lane (revision, generation) evidence; an approval that matches on
        # gen/decision/continuation but differs ONLY in the lane pins must be refused, never
        # silently resume the stored worker's evidence.
        key = ReplacementTransactionKey(self.workspace_id, ACTION_ID)
        self.store.plan_transaction(
            key, action_generation=GEN,
            decision=DecisionPointer(source="redmine", issue_id="13806", journal_id="79485"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="79485",
                expected_gate="implementation_request", next_semantic_action="dispatch_once",
            ),
            participants=[ParticipantPin(**WORKER, lane_revision="99", lane_generation="99")],
        )
        ops = FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(
            self._request(lane_revision="3", lane_generation="2"), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("different recovery authority", outcome.detail)
        self.assertEqual(self.port.closed, [])


# -- 7-8, 11. partial legs + restart recovery -----------------------------------


class PartialReplayTests(_RecoveryCase):
    def test_close_then_launch_failure_is_partial_then_resumes(self):
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.launch_result[wk_id] = LAUNCH_ERROR
        ops = FakeRecoveryOps(_all_clear())
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertTrue(first.is_blocked)
        self.assertTrue(first.closed_old_worker)  # the close committed
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)
        self.assertEqual(ops.sends, [])  # never redispatched on a partial
        # restart recovery: the launch now succeeds; a re-run resumes and never re-closes.
        self.port.launch_result.pop(wk_id)
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertEqual(self.port.closed.count(wk_id), 1)  # closed exactly once
        self.assertEqual(len(ops.sends), 1)

    def test_attestation_pending_is_partial_then_resumes(self):
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.attest[wk_id] = ATTEST_PENDING
        ops = FakeRecoveryOps(_all_clear())
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertEqual(self._worker_pin_phase(), "verify_owed")
        self.assertEqual(ops.sends, [])
        # the fresh slot now attests; a re-run completes.
        self.port.attest[wk_id] = ATTEST_BOUND
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.status, RECOVERY_COMPLETED)


# -- 9. duplicate replay --------------------------------------------------------


class DuplicateReplayTests(_RecoveryCase):
    def test_rerun_of_completed_recovery_is_idempotent(self):
        ops = FakeRecoveryOps(_all_clear())
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_COMPLETED)
        closed_after_first = list(self.port.closed)
        sends_after_first = len(ops.sends)
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        # no additional close / launch / redispatch on a completed transaction
        self.assertEqual(self.port.closed, closed_after_first)
        self.assertEqual(len(ops.sends), sends_after_first)


# -- 10. original-anchor redispatch fence (no blind resend) ---------------------


class RedispatchFenceTests(_RecoveryCase):
    def test_send_without_confirm_is_uncertain_and_never_resends(self):
        # The send goes out but the gate is not yet confirmed: the transaction stays at
        # draining_continuation (attempted) and a resume must NOT blind-resend.
        ops = FakeRecoveryOps(_all_clear(), confirm_after_send=False)
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertEqual(first.redispatch_status, REDISPATCH_UNCERTAIN)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)
        self.assertEqual(len(ops.sends), 1)
        # resume: still unconfirmed -> uncertain, NO second send (the "never blind-resend" fence)
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.redispatch_status, REDISPATCH_UNCERTAIN)
        self.assertEqual(len(ops.sends), 1)
        # the gate finally lands out of band; a resume now confirms with no further send
        ops._landed = True
        third = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(third.status, RECOVERY_COMPLETED)
        self.assertEqual(len(ops.sends), 1)

    def test_gate_already_landed_before_send_completes_without_sending(self):
        # An idempotent redispatch: the gate is already present, so the leg completes with no
        # send at all (never a duplicate dispatch).
        ops = FakeRecoveryOps(_all_clear(), already_landed=True)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)
        self.assertEqual(ops.sends, [])

    def test_send_error_stays_attempted_and_is_stopped(self):
        ops = FakeRecoveryOps(_all_clear(), send_result=DRAIN_SEND_ERROR)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_STOPPED)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)


# -- actuator topology + guarded DAG tail ---------------------------------------


class ActuatorTopologyTests(unittest.TestCase):
    def _store_with(self, participants, home=None):
        home = home or Path(tempfile.mkdtemp())
        store = ReplacementTransactionStore(home=home)
        key = ReplacementTransactionKey("ws", "k")
        store.plan_transaction(
            key, action_generation=GEN,
            decision=DecisionPointer(source="redmine", issue_id="13806", journal_id="1"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="1",
                expected_gate="g", next_semantic_action="n",
            ),
            participants=participants,
        )
        return store, key

    def test_drive_worker_recovery_refuses_self_bearing_topology(self):
        # A self participant makes this a self-replacement, never a worker recovery.
        selfp = ParticipantPin(
            lane_id="d", role="coordinator", provider="codex",
            assigned_name="cx", old_locator="w:3", is_self=True,
        )
        store, key = self._store_with([ParticipantPin(**WORKER), selfp])
        port = FakeActuatorPort()
        result = ReplacementActuatorUseCase(store, port, clock=lambda: FIXED).drive_worker_recovery(
            key, holder="H", expected_action_generation=GEN
        )
        self.assertEqual(result.status, ACTUATION_INVALID_TOPOLOGY)
        self.assertEqual(port.closed, [])  # zero destructive effect
        self.assertEqual(store.get(key).lease_holder, "")  # nothing claimed

    def test_worker_recovery_arms_at_replacing_nonself(self):
        store, key = self._store_with([ParticipantPin(**WORKER)])
        port = FakeActuatorPort()
        result = ReplacementActuatorUseCase(store, port, clock=lambda: FIXED).drive_worker_recovery(
            key, holder="H", expected_action_generation=GEN
        )
        self.assertEqual(result.status, ACTUATION_RECOVERED)
        rec = store.get(key)
        self.assertEqual(rec.phase, PHASE_REPLACING_NONSELF)  # no self-close leg
        self.assertEqual(
            rec.find_participant(ParticipantPin(**WORKER).identity).phase,
            PARTICIPANT_REPLACED,
        )

    def test_recycled_old_slot_is_zero_actuation(self):
        store, key = self._store_with([ParticipantPin(**WORKER)])
        port = FakeActuatorPort()
        port.old[ParticipantPin(**WORKER).identity] = OLD_SLOT_RECYCLED
        result = ReplacementActuatorUseCase(store, port, clock=lambda: FIXED).drive_worker_recovery(
            key, holder="H", expected_action_generation=GEN
        )
        self.assertEqual(result.status, "recycled")
        self.assertEqual(port.closed, [])

    def test_self_replacement_cannot_take_worker_recovery_shortcut(self):
        # The guarded DAG tail: replacing_nonself -> draining_continuation requires EVERY
        # participant replaced. A self-replacement at replacing_nonself always has an
        # un-replaced self, so the shortcut is unrepresentable for it.
        selfp = ParticipantPin(
            lane_id="d", role="coordinator", provider="codex",
            assigned_name="cx", old_locator="w:3", is_self=True,
        )
        worker = ParticipantPin(**WORKER)
        # self still close_owed; only the worker replaced
        self.assertTrue(transaction_transition_allowed(
            PHASE_REPLACING_NONSELF, PHASE_DRAINING_CONTINUATION
        ))
        self.assertFalse(transaction_phase_prerequisite_met(
            [worker.with_phase(PARTICIPANT_REPLACED), selfp], PHASE_DRAINING_CONTINUATION
        ))
        # a no-self recovery whose worker is replaced MAY take it
        self.assertTrue(transaction_phase_prerequisite_met(
            [worker.with_phase(PARTICIPANT_REPLACED)], PHASE_DRAINING_CONTINUATION
        ))


# -- pure domain ----------------------------------------------------------------


class PureDomainTests(unittest.TestCase):
    def test_decide_recovery_all_clear_is_actionable(self):
        self.assertEqual(decide_recovery(_all_clear()), RECOVER_ACTIONABLE)

    def test_decide_recovery_is_ordered_most_fundamental_first(self):
        # identity before everything else
        self.assertEqual(
            decide_recovery(_all_clear(identity_resolved=False, is_standard_sublane_worker=False)),
            RECOVER_BLOCK_UNKNOWN,
        )
        # gateway/foreign before issue-lane
        self.assertEqual(
            decide_recovery(_all_clear(is_standard_sublane_worker=False, issue_lane_matches=False)),
            RECOVER_BLOCK_GATEWAY_OR_FOREIGN,
        )
        # productive before "not stale" (a live worker never falls through as residue)
        self.assertEqual(
            decide_recovery(_all_clear(not_productive=False, is_stale=False)),
            RECOVER_BLOCK_PRODUCTIVE,
        )

    def test_missing_observation_fails_closed(self):
        self.assertNotEqual(decide_recovery(RecoveryObservation()), RECOVER_ACTIONABLE)
        self.assertEqual(decide_recovery(RecoveryObservation()), RECOVER_BLOCK_UNKNOWN)

    def test_authority_conflict_is_last_gate(self):
        self.assertEqual(
            decide_recovery(_all_clear(no_authority_conflict=False)),
            RECOVER_BLOCK_AUTHORITY_CONFLICT,
        )

    def test_action_id_requires_all_components(self):
        with self.assertRaises(ValueError):
            stale_worker_recovery_action_id(
                lane_id="l", role="", provider="p", assigned_name="n", locator="w:1"
            )

    def test_topology_helpers(self):
        worker = ParticipantPin(**WORKER)
        selfp = ParticipantPin(
            lane_id="d", role="coordinator", provider="codex",
            assigned_name="cx", old_locator="w:3", is_self=True,
        )
        self.assertTrue(is_worker_recovery_topology([worker]))
        self.assertFalse(is_worker_recovery_topology([worker, selfp]))  # has self
        self.assertFalse(is_worker_recovery_topology([]))  # empty
        self.assertFalse(is_self_replacement_topology([worker]))  # zero self

    def test_worker_recovery_preservation_preserves_dirty_blocks_live(self):
        # dirty + unrecorded + stale-attestation => may close (worktree preserved in place)
        v = assess_worker_recovery_preservation(PreservationObservation(
            dirty_diff=True, unrecorded_journal=True,
            identity_matches=True, attestation_fresh=False,
        ))
        self.assertTrue(v.may_close)
        # a running process still blocks (never destroy live work)
        v = assess_worker_recovery_preservation(PreservationObservation(
            running_process=True, identity_matches=True,
        ))
        self.assertFalse(v.may_close)
        self.assertIn(PRESERVE_RUNNING_PROCESS, v.reasons)
        # an identity mismatch still blocks (never close the wrong slot)
        v = assess_worker_recovery_preservation(PreservationObservation(
            identity_matches=False,
        ))
        self.assertFalse(v.may_close)
        self.assertIn(PRESERVE_IDENTITY_MISMATCH, v.reasons)

    def test_ops_protocol_is_runtime_checkable(self):
        self.assertIsInstance(FakeRecoveryOps(_all_clear()), StaleWorkerRecoveryOps)

    def test_outcome_payload_shape(self):
        ops = FakeRecoveryOps(_all_clear())
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        outcome = StaleWorkerRecoveryUseCase(
            store, FakeActuatorPort(), ops, workspace_id="ws", clock=lambda: FIXED,
        ).run(
            RecoveryRequest(
                issue="13806", lane="l", role="worker", provider="claude",
                assigned_name="wk", locator="w:2",
            ),
            execute=False,
        )
        payload = outcome.as_payload()
        self.assertEqual(payload["verdict"], RECOVER_ACTIONABLE)
        self.assertEqual(payload["status"], RECOVERY_PREFLIGHT)
        self.assertIn("observation", payload)


# -- post-close resume correction (Redmine #13806 close-success -> launch-failure -> replay) ----
#
# The installed dogfood (#13811 j#81809) broke exactly here: the recovery superseded the worker
# to a fresh generation, closed the old worker (close=true), then the fresh launch failed
# (effect_failed: launch). The public outcome said "re-run resumes", but the same immutable
# replay re-resolved the pinned OLD locator against a live inventory that no longer held it, so
# the entry preflight classified it identity_unknown and REFUSED "target not actionable" — never
# reaching the durable transaction's launch_owed resume. These pins prove the replay now reaches
# the resume and drives launch -> attest -> exactly-once redispatch, while every fail-closed fence
# (no durable txn, a still-close_owed txn, a wrong generation, a malformed re-approval) still
# stands (Implementation Request j#81810 §1-§5).


class PostCloseResumeTests(_RecoveryCase):
    def _gone(self):
        """The post-close live inventory: the pinned OLD worker was closed and is now absent.

        The old-locator-pinned observation resolves nothing (every positive fact defaults to the
        unsafe side), so ``decide_recovery`` returns ``identity_unknown`` — exactly what the live
        ops report once the recovery has closed the old slot (the fresh slot sits at a new
        locator the pin does not name).
        """
        return RecoveryObservation()

    def test_close_then_launch_failure_replay_after_old_worker_gone_resumes(self):
        # THE central reproduction (j#81810): close committed, launch failed, then on replay the
        # old worker is GONE (identity_unknown) — the resume must still reach launch_owed, not
        # refuse.
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.launch_result[wk_id] = LAUNCH_ERROR
        ops = FakeRecoveryOps(_all_clear())
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertIn("re-run resumes", first.detail)
        self.assertTrue(first.closed_old_worker)
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)
        # The old worker is now gone; the launch now succeeds. The ACTUAL next command (item 4)
        # observes identity_unknown yet must reach the durable launch_owed resume and complete.
        ops._observation = self._gone()
        self.port.launch_result.pop(wk_id)
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.verdict, RECOVER_BLOCK_UNKNOWN)  # honest preflight fact
        self.assertTrue(second.post_close_resume)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertFalse(second.is_blocked)
        self.assertEqual(self.port.closed.count(wk_id), 1)  # NEVER re-closed on the resume
        # Every launch is action-bound (the failed first attempt + the resume's retry); a
        # launch_owed resume re-launches, it never adopts blind and never re-closes.
        self.assertEqual({a for a, _ in self.port.launched}, {ACTION_ID})
        self.assertEqual(len(ops.sends), 1)  # redispatched exactly once
        self.assertEqual(self._phase(), PHASE_COMPLETED)

    def test_attestation_pending_replay_after_old_worker_gone_resumes(self):
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.attest[wk_id] = ATTEST_PENDING
        ops = FakeRecoveryOps(_all_clear())
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_VERIFY_OWED)
        # Old worker gone; the fresh slot now attests. The verify_owed resume completes.
        ops._observation = self._gone()
        self.port.attest[wk_id] = ATTEST_BOUND
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(second.post_close_resume)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertEqual(self.port.closed.count(wk_id), 1)

    def test_redispatch_uncertain_replay_after_old_worker_gone_resumes_no_blind_resend(self):
        # The worker is already replaced (draining_continuation); the send went out but is
        # unconfirmed. On replay the old worker is gone — the resume must reach the redispatch
        # idempotency fence, NOT refuse, and never blind-resend.
        ops = FakeRecoveryOps(_all_clear(), confirm_after_send=False)
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertEqual(first.redispatch_status, REDISPATCH_UNCERTAIN)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)
        self.assertEqual(len(ops.sends), 1)
        ops._observation = self._gone()
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(second.post_close_resume)
        self.assertEqual(second.redispatch_status, REDISPATCH_UNCERTAIN)
        self.assertEqual(len(ops.sends), 1)  # no blind resend
        # The gate lands out of band; a third replay (still worker-gone) confirms, no new send.
        ops._landed = True
        third = self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(third.post_close_resume)
        self.assertEqual(third.status, RECOVERY_COMPLETED)
        self.assertEqual(len(ops.sends), 1)

    def test_completed_recovery_replay_after_old_worker_gone_is_idempotent(self):
        ops = FakeRecoveryOps(_all_clear())
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_COMPLETED)
        closed_after = list(self.port.closed)
        sends_after = len(ops.sends)
        ops._observation = self._gone()
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(second.post_close_resume)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertEqual(self.port.closed, closed_after)  # zero additional close
        self.assertEqual(len(ops.sends), sends_after)  # zero additional send

    # -- fail-closed fences that must NOT be weakened by the resume admission (§3) ----

    def test_identity_unknown_with_no_durable_transaction_still_refuses_zero_close(self):
        # A genuinely unknown identity with NO durable transaction is a fresh recovery whose
        # block is real — never planned / launched blind as a "resume".
        ops = FakeRecoveryOps(self._gone())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_UNKNOWN)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(self.port.closed, [])
        self.assertIsNone(self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID)))

    def test_close_owed_zero_effect_transaction_is_not_resumed_on_identity_unknown(self):
        # A durable transaction exists but NOTHING was closed yet (participant close_owed). The
        # old worker being absent is not a post-close state — the preflight block must stand and
        # never launch on the unknown identity.
        key = ReplacementTransactionKey(self.workspace_id, ACTION_ID)
        self.store.plan_transaction(
            key, action_generation=GEN,
            decision=DecisionPointer(source="redmine", issue_id="13806", journal_id="79485"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="79485",
                expected_gate="implementation_request", next_semantic_action="dispatch_once",
            ),
            participants=[ParticipantPin(**WORKER, lane_revision="3", lane_generation="2")],
        )
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_CLOSE_OWED)
        ops = FakeRecoveryOps(self._gone())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(self.port.closed, [])  # never launched / closed on the block

    def test_wrong_generation_committed_transaction_is_not_admitted_as_resume(self):
        # A committed-effect transaction exists at GEN, but the replay names a DIFFERENT
        # generation — a foreign / superseding authority, never admitted past the block.
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.launch_result[wk_id] = LAUNCH_ERROR
        ops = FakeRecoveryOps(_all_clear())
        self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)
        closed_before = list(self.port.closed)
        # replay at a different generation with the old worker gone
        ops._observation = self._gone()
        outcome = self._use_case(ops).run(
            self._request(action_generation=GEN + 5), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(self.port.closed, closed_before)  # zero additional effect

    # -- R3-F1: admission is closed to expected old-locator absence; owed launch/send re-verify --

    def _committed_launch_owed(self):
        """Drive to a post-close ``launch_owed`` (close committed, launch failed once)."""
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.launch_result[wk_id] = LAUNCH_ERROR
        ops = FakeRecoveryOps(_all_clear())
        self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)
        self.port.launch_result.pop(wk_id)
        return ops, wk_id

    def test_worktree_unreadable_blocker_is_not_admitted_as_resume(self):
        # A committed-close transaction exists, but the current observation RESOLVES the worker
        # and reports the worktree unreadable (verdict dirty_state_unreadable). That is a real
        # current-state fence — NOT the expected old-locator absence — so the resume must not
        # bypass it (R3-F1): zero launch / send.
        ops, _wk = self._committed_launch_owed()
        launched_before = list(self.port.launched)
        ops._observation = _all_clear(worktree_readable=False)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_DIRTY_UNREADABLE)
        self.assertFalse(outcome.post_close_resume)  # never admitted as a resume
        self.assertEqual(self.port.launched, launched_before)  # zero launch
        self.assertEqual(ops.sends, [])  # zero send

    def test_stale_generation_blocker_is_not_admitted_as_resume(self):
        # Likewise a resolved worker at a stale generation is a real fence, never bypassed.
        ops, _wk = self._committed_launch_owed()
        launched_before = list(self.port.launched)
        ops._observation = _all_clear(generation_matches=False)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertEqual(outcome.verdict, RECOVER_BLOCK_STALE_GENERATION)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(self.port.launched, launched_before)
        self.assertEqual(ops.sends, [])

    def test_lane_authority_moved_blocks_launch_zero_effect(self):
        # The expected old-locator absence IS admitted, but the exact lane authority (lifecycle /
        # worktree token / branch) is moved when re-joined ACTION-TIME immediately before the
        # launch — zero launch/send, the durable transaction preserved (Review j#82731 F1/F2).
        ops, _wk = self._committed_launch_owed()
        launched_before = list(self.port.launched)
        ops._observation = self._gone()  # identity_unknown => admitted
        ops._resume_lane_authority = False  # the lane authority moved
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_STOPPED)
        self.assertTrue(outcome.post_close_resume)
        self.assertIn("launch_authority_moved", outcome.detail)
        self.assertTrue(ops.authority_checks)  # the authority was re-joined before the launch
        self.assertEqual(self.port.launched, launched_before)  # zero launch
        self.assertEqual(ops.sends, [])  # zero send
        # a later re-run (authority restored) resumes from the same durable owed state
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)

    def test_foreign_live_process_blocks_launch_zero_effect(self):
        # Any foreign live process (busy OR idle) at the lane's name when re-joined immediately
        # before the launch stops it zero-effect (Answer j#82708 "foreign OR productive live").
        ops, _wk = self._committed_launch_owed()
        launched_before = list(self.port.launched)
        ops._observation = self._gone()
        ops._lane_free_of_live_process = False  # a foreign live process holds the name
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_STOPPED)
        self.assertTrue(outcome.post_close_resume)
        self.assertIn("launch_authority_moved", outcome.detail)
        self.assertTrue(ops.free_of_live_checks)  # the liveness fence was re-joined
        self.assertEqual(self.port.launched, launched_before)  # zero launch
        self.assertEqual(ops.sends, [])  # zero send
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)

    def test_authority_moves_between_launch_and_send_blocks_send_no_blind_send(self):
        # THE effect-bound race (Review j#82731 F1): the authority is current at the launch but
        # moves BEFORE the send. The launch/attest complete, but the send re-joins the authority
        # action-time and stops zero-send, leaving the phase not-attempted so a later re-run (with
        # authority restored) sends exactly once — never a blind send.
        ops, _wk = self._committed_launch_owed()
        ops._observation = self._gone()
        # authority: True for the launch probe, False for the send probe (moved in between).
        ops._resume_lane_authority = [True, False]
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertTrue(first.post_close_resume)
        # launched (the setup's failed attempt + this run's success) but...
        self.assertEqual([i for _a, i in self.port.launched].count(_wk), 2)
        self.assertEqual(ops.sends, [])  # ...NEVER sent (authority moved before the send)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)  # not-attempted, re-sendable
        # authority restored (list drained -> default True); a re-run sends exactly once
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertEqual(len(ops.sends), 1)

    def test_authority_moves_during_attempted_cas_before_transport_blocks_send(self):
        # THE last-mile race (Review j#82760 F1): authority is current through the launch AND at
        # the moment the redispatch records `attempted`, but moves DURING the attempted CAS —
        # after it, before the transport. The send re-joins the authority as the LAST check
        # immediately before the transport (after the CAS + lease re-auth), so it catches the
        # move, sends nothing, and UN-RECORDS the attempt (draining_continuation ->
        # replacing_nonself) so a re-run re-attempts exactly once — never stuck at uncertain.
        ops, wk = self._committed_launch_owed()
        ops._observation = self._gone()
        # Hook the store's attempted-CAS (the -> draining_continuation move) to flip the live
        # authority to moved, reproducing a lane change between the CAS and the transport.
        orig = self.store.transition_phase

        def flip(key, **kw):
            out = orig(key, **kw)
            if kw.get("target") == PHASE_DRAINING_CONTINUATION:
                ops._resume_lane_authority = False
            return out

        self.store.transition_phase = flip
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, RECOVERY_STOPPED)
        self.assertEqual(ops.sends, [])  # zero send — the last-mile check caught the move
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)  # attempt un-recorded, re-sendable
        self.assertEqual([i for _a, i in self.port.launched].count(wk), 2)  # launched, not sent
        # authority restored + hook removed; a re-run sends exactly once (never a blind resend)
        self.store.transition_phase = orig
        ops._resume_lane_authority = True
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertEqual(len(ops.sends), 1)

    def test_admitted_resume_reverifies_authority_effect_bound_then_completes(self):
        # The legitimate path: expected absence admitted, authority exact and current at BOTH the
        # launch and the send — the resume re-joins them action-time and completes.
        ops, _wk = self._committed_launch_owed()
        ops._observation = self._gone()
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(outcome.post_close_resume)
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)
        # authority re-joined both before the launch and before the send (>= 2 action-time checks)
        self.assertGreaterEqual(len(ops.authority_checks), 2)
        self.assertTrue(ops.free_of_live_checks)  # pre-launch liveness fence consulted

    def test_dirty_but_readable_resume_completes_byte_preserved(self):
        # The tranche D byte-preservation contract holds on the resume (Answer j#82708 Option A):
        # a DIRTY (but readable) worktree is the un-durable-ized work the recovery preserves —
        # dirtiness is not an authority axis, so with the lane authority exact and no foreign live
        # process, the resume relaunches and completes, exactly as the first-success fresh-recovery
        # path (test_stale_worker_with_dirty_unrecorded_worktree_closes_present_slot). The launch-
        # failure timing never changes the byte-preservation outcome.
        ops, _wk = self._committed_launch_owed()
        ops._observation = self._gone()
        ops._resume_lane_authority = True  # dirtiness is not an axis => authority holds
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)

    # -- §5 dual-anchor: resume re-approval is a separate authority from the CAS anchor ----

    def test_resume_with_distinct_reapproval_journal_completes_without_tripping_divergence(self):
        # The stored decision/continuation anchor is journal 79485 (--journal). A post-close
        # resume re-approved by a FRESH journal (--resume-journal 82649) must complete: the CAS
        # anchor stays 79485 (matched via --journal), the fresh re-approval is recorded, and the
        # divergence / supersede fence is NEVER tripped.
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.launch_result[wk_id] = LAUNCH_ERROR
        ops = FakeRecoveryOps(_all_clear())
        self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)
        ops._observation = self._gone()
        self.port.launch_result.pop(wk_id)
        second = self._use_case(ops).run(
            self._request(resume_journal="82649"), execute=True
        )
        self.assertTrue(second.post_close_resume)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertEqual(second.resume_authorization, "82649")
        self.assertNotIn("different recovery authority", second.detail)
        # The stored immutable decision anchor is UNCHANGED (still the original 79485).
        rec = self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID))
        self.assertEqual(rec.decision.journal_id, "79485")
        self.assertEqual(rec.continuation.journal_id, "79485")

    def test_malformed_resume_journal_is_zero_close_launch_send(self):
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.launch_result[wk_id] = LAUNCH_ERROR
        ops = FakeRecoveryOps(_all_clear())
        self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(self._worker_pin_phase(), PARTICIPANT_LAUNCH_OWED)
        closed_before = list(self.port.closed)
        launched_before = list(self.port.launched)
        ops._observation = self._gone()
        self.port.launch_result.pop(wk_id)
        outcome = self._use_case(ops).run(
            self._request(resume_journal="not-a-journal"), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertTrue(outcome.post_close_resume)  # it WAS an admitted resume, refused on §5
        self.assertEqual(self.port.closed, closed_before)  # zero close
        self.assertEqual(self.port.launched, launched_before)  # zero launch
        self.assertEqual(ops.sends, [])  # zero send

    def test_same_journal_resume_records_no_distinct_reauthorization(self):
        wk_id = (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        self.port.launch_result[wk_id] = LAUNCH_ERROR
        ops = FakeRecoveryOps(_all_clear())
        self._use_case(ops).run(self._request(), execute=True)
        ops._observation = self._gone()
        self.port.launch_result.pop(wk_id)
        second = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertTrue(second.post_close_resume)
        self.assertEqual(second.resume_authorization, "")  # same-journal resume


class PostCloseResumePureTests(unittest.TestCase):
    def test_worker_close_committed_is_true_only_past_close_owed(self):
        self.assertFalse(worker_close_committed(PARTICIPANT_CLOSE_OWED))
        self.assertTrue(worker_close_committed(PARTICIPANT_LAUNCH_OWED))
        self.assertTrue(worker_close_committed(PARTICIPANT_VERIFY_OWED))
        self.assertTrue(worker_close_committed(PARTICIPANT_REPLACED))
        self.assertFalse(worker_close_committed(""))
        self.assertFalse(worker_close_committed("nonsense"))


if __name__ == "__main__":
    unittest.main()
