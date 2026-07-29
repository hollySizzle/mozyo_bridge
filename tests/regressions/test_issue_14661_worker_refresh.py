"""Regression: guarded live-worker refresh use case (Redmine #14661).

Pins ``sublane refresh-worker``'s execute discipline over the #13806 replacement-transaction
machinery: preflight never writes; every approval axis is fail-closed; the actuation closes
ONLY the exact pinned worker generation; the resume drives the EXISTING durable anchor exactly
once through the shared continuation-drain authority (idempotency-first, record attempted
before the send, action-time authority re-join, never a blind resend); a stopped leg holds the
durable replay fence and a post-close replay is admitted ONLY on the expected
``identity_unknown`` + a committed-close transaction.

It also pins the #14661 acceptance's zero-close / zero-send matrix directly: durable progress
already landed, busy/working, delivery uncertain, and identity / generation / branch / worktree
drift each close nothing and send nothing. Fakes only — no live process, no herdr.
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E402,E501
    WORKER_REFRESH_STATUS_COMPLETED,
    WORKER_REFRESH_STATUS_PREFLIGHT,
    WORKER_REFRESH_STATUS_REFUSED,
    WORKER_REFRESH_STATUS_STOPPED,
    WorkerRefreshRequest,
    WorkerRefreshUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E402,E501
    TURN_CLASS_FAILED,
    TURN_CLASS_NOT_SETTLED,
    TURN_CLASS_PRODUCTIVE,
    TURN_CLASS_UNCONFIRMED,
    TURN_CLASS_UNOBSERVABLE,
    TURN_REASON_RATE_LIMIT,
    TURN_REASON_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E402,E501
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
    launch_authority_current,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    CLOSE_DONE,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_PRESENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E402,E501
    WORKER_REFRESH_ACTIONABLE,
    WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE,
    WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN,
    WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY,
    WORKER_REFRESH_BLOCK_NOT_SETTLED,
    WORKER_REFRESH_BLOCK_TURN_NOT_FAILED,
    WORKER_REFRESH_BLOCK_UNKNOWN,
    WorkerRefreshObservation,
    WorkerTurnObservation,
)

GEN = 3
FIXED = "2026-07-29T12:00:00+00:00"
WORKER = dict(
    lane_id="issue_14661_lane", role="claude", provider="claude", assigned_name="wk",
    old_locator="w4B:p10",
)
ACTION_ID = "refresh-worker:issue_14661_lane:claude:claude:wk:w4B:p10:r4"
#: The owner-approval journal — a THIRD authority, distinct from the Start / Implementation
#: Request (j#92369) and from the resume anchor (j#92366).
APPROVAL_JOURNAL = "92500"


def _turn(**overrides) -> WorkerTurnObservation:
    facts = dict(
        delivery_confirmed=True, turn_started=True, settled_turn_ended=True,
        expected_gate_absent=True, durable_source_fresh=True,
        anchor_bound=True, lane_generation_bound=True, participant_revision_bound=True,
    )
    facts.update(overrides)
    return WorkerTurnObservation(**facts)


def _target(**overrides) -> WorkerRefreshObservation:
    facts = dict(
        identity_resolved=True, is_standard_sublane_worker=True,
        issue_lane_matches=True, generation_matches=True, settled_idle=True,
        composer_clear=True, resume_anchor_present=True, worktree_readable=True,
        gateway_distinct_preserved=True, no_authority_conflict=True,
        # The use case JOINS this axis from ``ops.lane_authority_reason`` and overwrites
        # whatever the target observer carried, so this value is inert here — kept green so
        # the canonical builder stays an all-facts-hold observation (#14475).
        launch_authority_current=True,
    )
    facts.update(overrides)
    return WorkerRefreshObservation(**facts)


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


class FakeWorkerOps:
    """A synthetic WorkerRefreshOps — fixed observations + a recorded resume rail."""

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
        approval_ok=True,
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
        self._approval_ok = approval_ok
        self.approval_checks: list = []

    def approval_verified(self, request) -> bool:
        """The positive owner-approval authority (review j#92443 F2).

        Defaults to a verified approval so the existing happy-path cases still describe an
        AUTHORIZED refresh; the refusal cases below drive it False explicitly. The real
        adapter proves this against a fresh durable read.
        """
        self.approval_checks.append(request)
        return self._approval_ok

    def observe_turn(self, request) -> WorkerTurnObservation:
        return self._turn

    def observe_target(self, request) -> WorkerRefreshObservation:
        return self._target

    def lane_authority_reason(self, request) -> str:
        """The #14475 typed axis, driven by the SAME ``_lane_authority`` script.

        The fake mirrors the live adapter's structure: one evaluator, and
        ``resume_lane_authority`` is its boolean projection, so a test that scripts the
        authority moving mid-run moves it for the preflight axis and the action-time fence
        alike — exactly as the real join would.
        """
        self.authority_checks.append(request)
        value = self._lane_authority
        if isinstance(value, list):
            # A scripted sequence REPEATS its last element once exhausted: a lane authority
            # that moved does not spontaneously come back, and the use case legitimately
            # re-reads the evaluator once more to report the action-time axis at a refusal
            # (#14475 j#88485). A script that reverted to green on exhaustion would make that
            # re-read report ``ok`` for a lane whose authority is in fact still broken.
            current = value.pop(0) if len(value) > 1 else (value[0] if value else True)
        else:
            current = value
        return LAUNCH_AUTHORITY_OK if current else LAUNCH_AUTHORITY_WORKTREE_UNBOUND

    def resume_lane_authority(self, request) -> bool:
        return launch_authority_current(self.lane_authority_reason(request))

    def worker_name_free_of_live_process(self, request) -> bool:
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

    def _request(self, **overrides) -> WorkerRefreshRequest:
        base = dict(
            issue="14661", lane=WORKER["lane_id"], role=WORKER["role"],
            provider=WORKER["provider"], assigned_name=WORKER["assigned_name"],
            # Review j#92443 F2: this is an OWNER APPROVAL journal, distinct from both the
            # dispatch anchor and the Start / Implementation Request. An earlier revision of
            # this fixture reused the Start journal here, which made the suite assert that
            # any non-approval journal authorizes a destructive close.
            locator=WORKER["old_locator"], journal=APPROVAL_JOURNAL, action_id=ACTION_ID,
            action_generation=GEN, worker_revision="4",
            lane_revision="5", lane_generation="2",
            resume_anchor_journal="92366", resume_gate="review_result",
        )
        base.update(overrides)
        return WorkerRefreshRequest(**base)

    def _use_case(self, ops):
        return WorkerRefreshUseCase(
            self.store, self.port, ops, workspace_id=self.workspace_id, clock=lambda: FIXED,
        )

    def _row(self):
        return self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID))

    def _assert_zero_effect(self, ops):
        self.assertIsNone(self._row())          # zero writes
        self.assertEqual(self.port.closed, [])  # zero closes
        self.assertEqual(self.port.launched, [])
        self.assertEqual(ops.resumes, [])       # zero sends


class PreflightTests(_RefreshCase):
    def test_preflight_classifies_and_writes_nothing(self):
        ops = FakeWorkerOps()
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_PREFLIGHT)
        self.assertEqual(outcome.turn_class, TURN_CLASS_FAILED)
        self.assertEqual(outcome.verdict, WORKER_REFRESH_ACTIONABLE)
        self.assertFalse(outcome.executed)
        self.assertFalse(outcome.is_blocked)
        self._assert_zero_effect(ops)

    def test_the_reason_is_normalized_fail_closed(self):
        ops = FakeWorkerOps(turn=_turn(reason_token="429 raw provider text"))
        self.assertEqual(
            self._use_case(ops).run(self._request(), execute=False).turn_reason,
            TURN_REASON_UNKNOWN,
        )
        ops = FakeWorkerOps(turn=_turn(reason_token=TURN_REASON_RATE_LIMIT))
        self.assertEqual(
            self._use_case(ops).run(self._request(), execute=False).turn_reason,
            TURN_REASON_RATE_LIMIT,
        )

    def test_the_typed_authority_axis_is_emitted_on_every_outcome(self):
        for authority, expected in ((True, LAUNCH_AUTHORITY_OK), (False, LAUNCH_AUTHORITY_WORKTREE_UNBOUND)):
            with self.subTest(authority=authority):
                ops = FakeWorkerOps(lane_authority=authority)
                outcome = self._use_case(ops).run(self._request(), execute=False)
                self.assertEqual(outcome.launch_authority_reason, expected)
                self.assertIn("launch_authority_reason", outcome.as_payload())

    def test_the_observation_payloads_are_carried_for_replay(self):
        outcome = self._use_case(FakeWorkerOps()).run(self._request(), execute=False)
        # The three #14661 identity bindings are replayable from the durable outcome.
        for axis in ("anchor_bound", "lane_generation_bound", "participant_revision_bound"):
            self.assertIs(outcome.turn_observation[axis], True)
        self.assertIs(outcome.observation["worktree_readable"], True)


class ZeroCloseZeroSendMatrixTests(_RefreshCase):
    """The #14661 acceptance's refusal matrix, stated one row per rule."""

    def _refuses(self, ops, expected_verdict):
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_REFUSED)
        self.assertEqual(outcome.verdict, expected_verdict)
        self.assertTrue(outcome.is_blocked)
        self._assert_zero_effect(ops)
        return outcome

    def test_durable_progress_already_landed_is_zero_close_zero_send(self):
        ops = FakeWorkerOps(turn=_turn(expected_gate_landed=True, expected_gate_absent=False))
        outcome = self._refuses(ops, WORKER_REFRESH_BLOCK_TURN_NOT_FAILED)
        self.assertEqual(outcome.turn_class, TURN_CLASS_PRODUCTIVE)

    def test_a_busy_working_worker_is_zero_close_zero_send(self):
        ops = FakeWorkerOps(turn=_turn(settled_turn_ended=False))
        outcome = self._refuses(ops, WORKER_REFRESH_BLOCK_TURN_NOT_FAILED)
        self.assertEqual(outcome.turn_class, TURN_CLASS_NOT_SETTLED)

    def test_an_uncertain_delivery_is_zero_close_zero_send(self):
        ops = FakeWorkerOps(turn=_turn(delivery_confirmed=False))
        outcome = self._refuses(ops, WORKER_REFRESH_BLOCK_TURN_NOT_FAILED)
        self.assertEqual(outcome.turn_class, TURN_CLASS_UNCONFIRMED)

    def test_each_identity_drift_is_zero_close_zero_send(self):
        for axis in ("anchor_bound", "lane_generation_bound", "participant_revision_bound"):
            with self.subTest(axis=axis):
                self.setUp()
                ops = FakeWorkerOps(turn=_turn(**{axis: False}))
                outcome = self._refuses(ops, WORKER_REFRESH_BLOCK_TURN_NOT_FAILED)
                self.assertEqual(outcome.turn_class, TURN_CLASS_UNOBSERVABLE)

    def test_a_gateway_or_foreign_slot_is_never_closed(self):
        ops = FakeWorkerOps(target=_target(is_standard_sublane_worker=False))
        self._refuses(ops, WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN)

    def test_an_unreadable_worktree_is_zero_close(self):
        ops = FakeWorkerOps(target=_target(worktree_readable=False))
        self._refuses(ops, WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE)

    def test_an_unsettled_slot_at_action_time_is_zero_close(self):
        ops = FakeWorkerOps(target=_target(settled_idle=False))
        self._refuses(ops, WORKER_REFRESH_BLOCK_NOT_SETTLED)

    def test_a_branch_or_worktree_drift_is_zero_close_before_any_close(self):
        ops = FakeWorkerOps(lane_authority=False)
        outcome = self._refuses(ops, WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY)
        self.assertIn("lane launch authority", outcome.detail)
        self.assertEqual(outcome.launch_authority_reason, LAUNCH_AUTHORITY_WORKTREE_UNBOUND)
        self.assertTrue(outcome.launch_authority_runbook)


class ExecuteRefusalTests(_RefreshCase):
    def _refused(self, ops, request, needle: str):
        outcome = self._use_case(ops).run(request, execute=True)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_REFUSED)
        self.assertIn(needle, outcome.detail)
        self._assert_zero_effect(ops)
        return outcome

    def test_an_incomplete_approval_pointer_refuses(self):
        self._refused(FakeWorkerOps(), self._request(journal=""), "approval journal")

    def test_a_mismatched_action_id_refuses(self):
        self._refused(
            FakeWorkerOps(), self._request(action_id="refresh-worker:other:x:y:z:w:1:r4"),
            "action id does not match",
        )

    def test_a_gateway_refresh_action_id_never_drives_a_worker_refresh(self):
        # The two surfaces must not share a transaction key; presenting the sibling's id for
        # the same slot shape is a refusal, not a silently-accepted alias.
        self._refused(
            FakeWorkerOps(),
            self._request(
                action_id="refresh-gateway:issue_14661_lane:claude:claude:wk:w4B:p10:r4"
            ),
            "action id does not match",
        )

    def test_a_non_positive_generation_refuses(self):
        self._refused(FakeWorkerOps(), self._request(action_generation=0), "positive exact")

    def test_a_boolean_generation_is_not_an_integer_generation(self):
        self._refused(FakeWorkerOps(), self._request(action_generation=True), "positive exact")

    def test_missing_lane_lifecycle_evidence_refuses(self):
        self._refused(FakeWorkerOps(), self._request(lane_revision=""), "lane lifecycle")
        self.setUp()
        self._refused(FakeWorkerOps(), self._request(lane_generation=""), "lane lifecycle")

    def test_a_missing_worker_revision_refuses_before_any_write(self):
        # An empty row-revision pin can never name one exact generation, so the action id
        # cannot even be derived — refused before the port is exercised.
        self._refused(
            FakeWorkerOps(), self._request(worker_revision=""), "one exact worker generation",
        )

    def test_a_non_resumable_gate_refuses(self):
        self._refused(FakeWorkerOps(), self._request(resume_gate="not_a_gate"), "not a resumable")

    def test_a_missing_resume_anchor_refuses(self):
        self._refused(
            FakeWorkerOps(target=_target(resume_anchor_present=True)),
            self._request(resume_anchor_journal=""),
            "resume anchor pointer is incomplete",
        )

    def test_an_unverified_owner_approval_refuses_before_any_close(self):
        # Review j#92443 F2: a well-shaped pointer is NOT an approval. Without a positive
        # durable approval naming this exact action + generation, nothing is closed.
        ops = FakeWorkerOps(approval_ok=False)
        outcome = self._refused(ops, self._request(), "not a positive durable owner approval")
        self.assertIn(ACTION_ID, outcome.detail)  # the refusal names what must be approved

    def test_an_ops_adapter_that_raises_is_never_treated_as_approved(self):
        class _Raising(FakeWorkerOps):
            def approval_verified(self, request):
                raise RuntimeError("durable source down")

        self._refused(_Raising(), self._request(), "not a positive durable owner approval")

    def test_the_approval_is_verified_before_the_resume_rail_probe(self):
        # Authority to act at all is more fundamental than the ability to finish: an
        # unapproved request must not be reported as a rail problem.
        ops = FakeWorkerOps(approval_ok=False, rail_ready=False)
        self._refused(ops, self._request(), "not a positive durable owner approval")

    def test_the_approval_seam_is_consulted_on_every_execute(self):
        ops = FakeWorkerOps()
        self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(ops.approval_checks)

    def test_a_preflight_never_consults_the_approval_seam(self):
        # A read-only preflight authorizes nothing, so it must not need an approval to run —
        # otherwise an operator could not produce the evidence the approval is written from.
        ops = FakeWorkerOps(approval_ok=False)
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_PREFLIGHT)
        self.assertEqual(outcome.verdict, WORKER_REFRESH_ACTIONABLE)
        self.assertEqual(ops.approval_checks, [])
        self._assert_zero_effect(ops)

    def test_an_unready_resume_rail_refuses_before_any_close(self):
        self._refused(
            FakeWorkerOps(rail_ready=False), self._request(), "resume_rail_unavailable"
        )

    def test_a_diverged_preexisting_row_is_an_authority_conflict(self):
        ops = FakeWorkerOps()
        self._use_case(ops).run(self._request(), execute=True)
        self.assertIsNotNone(self._row())
        # A second authority presenting the same action id at a different anchor.
        outcome = self._use_case(FakeWorkerOps()).run(
            self._request(resume_anchor_journal="99999"), execute=True
        )
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_REFUSED)
        self.assertIn("different refresh authority", outcome.detail)


class ExecuteTests(_RefreshCase):
    def test_close_launch_attest_resume_exactly_once(self):
        ops = FakeWorkerOps()
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_COMPLETED)
        self.assertEqual(outcome.resume_status, CONTINUATION_CONFIRMED)
        self.assertTrue(outcome.closed_old_worker)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertFalse(outcome.is_blocked)
        # Exactly ONE close of exactly ONE participant, and one send.
        self.assertEqual(len(self.port.closed), 1)
        self.assertEqual(len(self.port.launched), 1)
        self.assertEqual(self.port.launched[0][0], ACTION_ID)
        self.assertEqual(len(ops.resumes), 1)
        # The resume points at the EXISTING anchor, never a regenerated gate.
        self.assertEqual(ops.resumes[0].journal_id, "92366")
        self.assertEqual(ops.resumes[0].expected_gate, "review_result")
        # The name-collision fence ran before the launch.
        self.assertTrue(ops.name_free_checks)

    def test_a_rerun_after_completion_is_idempotent_zero_send(self):
        ops = FakeWorkerOps()
        self._use_case(ops).run(self._request(), execute=True)
        sends = len(ops.resumes)
        again = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(again.status, WORKER_REFRESH_STATUS_COMPLETED)
        self.assertEqual(len(ops.resumes), sends)  # no second send

    def test_an_already_landed_resume_completes_with_zero_send(self):
        ops = FakeWorkerOps(already_landed=True)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_COMPLETED)
        self.assertEqual(ops.resumes, [])

    def test_a_failed_launch_stops_with_the_replay_fence_held(self):
        self.port.launch_result[
            (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        ] = LAUNCH_ERROR
        ops = FakeWorkerOps()
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_STOPPED)
        self.assertTrue(outcome.is_blocked)
        self.assertIn("re-run resumes", outcome.detail)
        self.assertEqual(ops.resumes, [])         # never resumes past a failed launch
        self.assertIsNotNone(self._row())         # the durable replay fence is held
        self.assertTrue(outcome.closed_old_worker)

    def test_a_failed_resume_send_stops_without_blind_resend(self):
        ops = FakeWorkerOps(send_result=DRAIN_SEND_ERROR, confirm_after_send=False)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_STOPPED)
        self.assertEqual(outcome.resume_status, CONTINUATION_SEND_FAILED)
        self.assertEqual(len(ops.resumes), 1)     # exactly one attempt, never a blind retry

    def test_an_authority_move_before_the_resume_is_a_typed_zero_send(self):
        # A completed run reads the authority exactly three times — the preflight axis, the
        # launch leg's re-join, and the resume leg's re-join. Scripting the THIRD read False
        # moves the authority after the close/launch and before the send.
        probe = FakeWorkerOps()
        self._use_case(probe).run(self._request(), execute=True)
        self.assertEqual(len(probe.authority_checks), 3)
        self.setUp()
        ops = FakeWorkerOps(lane_authority=[True, True, False])
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_STOPPED)
        self.assertEqual(outcome.resume_status, CONTINUATION_AUTHORITY_MOVED)
        self.assertEqual(ops.resumes, [])
        # The reported axis is the ACTION-TIME state, re-read at the refusal — not the
        # preflight-time observation the run no longer reflects (#14475 j#88485).
        self.assertEqual(outcome.launch_authority_reason, LAUNCH_AUTHORITY_WORKTREE_UNBOUND)

    def test_a_post_close_replay_is_admitted_only_on_identity_unknown(self):
        # Drive a run whose launch fails: the close is committed, the launch owed.
        identity = (
            WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"]
        )
        self.port.launch_result[identity] = LAUNCH_ERROR
        first = self._use_case(FakeWorkerOps()).run(self._request(), execute=True)
        self.assertEqual(first.status, WORKER_REFRESH_STATUS_STOPPED)
        closes_after_first = len(self.port.closed)
        # The pinned old worker is now expectedly ABSENT — the replay must be admitted and
        # must NOT close anything a second time.
        self.port.launch_result.pop(identity)
        ops = FakeWorkerOps(target=WorkerRefreshObservation())
        replay = self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(replay.post_close_resume)
        self.assertEqual(replay.verdict, WORKER_REFRESH_BLOCK_UNKNOWN)
        self.assertEqual(replay.status, WORKER_REFRESH_STATUS_COMPLETED)
        self.assertEqual(len(self.port.closed), closes_after_first)  # zero extra closes

    def test_a_non_identity_unknown_blocker_is_never_a_post_close_replay(self):
        self.port.launch_result[
            (WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"])
        ] = LAUNCH_ERROR
        self._use_case(FakeWorkerOps()).run(self._request(), execute=True)
        closes = len(self.port.closed)
        ops = FakeWorkerOps(target=_target(is_standard_sublane_worker=False))
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_REFUSED)
        self.assertEqual(len(self.port.closed), closes)

    def test_a_launch_authority_blocker_stands_before_the_first_close(self):
        # While the pinned worker still RESOLVES, a broken lane authority is the verdict and
        # it refuses outright — it must never be reinterpreted as the post-close absence
        # signal, because closing here would leave a worker that can never be relaunched.
        ops = FakeWorkerOps(lane_authority=False)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(outcome.verdict, WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_REFUSED)
        self._assert_zero_effect(ops)

    def test_a_replay_under_a_broken_authority_relaunches_and_sends_nothing(self):
        # After a committed close the pinned worker is expectedly absent, so the replay IS
        # admitted (that is the only way a half-finished transaction can ever finish). What
        # must hold is that a broken authority still yields zero additional close, zero
        # launch, and zero send — the fence is re-joined action-time inside the actuator.
        identity = (
            WORKER["lane_id"], WORKER["role"], WORKER["provider"], WORKER["assigned_name"]
        )
        self.port.launch_result[identity] = LAUNCH_ERROR
        self._use_case(FakeWorkerOps()).run(self._request(), execute=True)
        closes = len(self.port.closed)
        launches = len(self.port.launched)
        self.port.launch_result.pop(identity)
        ops = FakeWorkerOps(target=WorkerRefreshObservation(), lane_authority=False)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertTrue(outcome.post_close_resume)
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_STOPPED)
        self.assertEqual(len(self.port.closed), closes)
        self.assertEqual(len(self.port.launched), launches)
        self.assertEqual(ops.resumes, [])


class AnchorIssueSplitTests(_RefreshCase):
    def test_the_anchor_issue_is_a_separate_authority_from_the_lane_issue(self):
        ops = FakeWorkerOps()
        outcome = self._use_case(ops).run(
            self._request(anchor_issue="14658"), execute=True
        )
        self.assertEqual(outcome.status, WORKER_REFRESH_STATUS_COMPLETED)
        self.assertEqual(ops.resumes[0].issue_id, "14658")
        # The lane's OWNING issue stays the destructive authorization boundary.
        self.assertEqual(outcome.issue, "14661")

    def test_an_absent_anchor_issue_falls_back_to_the_lane_issue(self):
        ops = FakeWorkerOps()
        self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(ops.resumes[0].issue_id, "14661")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
