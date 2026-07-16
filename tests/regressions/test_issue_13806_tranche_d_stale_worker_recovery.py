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
        already_landed=False,
    ):
        self._observation = observation
        self.send_result = send_result
        self.confirm_after_send = confirm_after_send
        self.sends: list = []
        self._landed = already_landed

    def observe_target(self, request) -> RecoveryObservation:
        return self._observation

    def redispatch_gate(self, continuation) -> str:
        self.sends.append(continuation)
        if self.send_result == DRAIN_SEND_OK and self.confirm_after_send:
            self._landed = True
        return self.send_result

    def gate_redispatched(self, continuation) -> bool:
        return self._landed


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
            expected_gate="review_request", next_semantic_action="dispatch_once",
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
                expected_gate="review_request", next_semantic_action="dispatch_once",
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


if __name__ == "__main__":
    unittest.main()
