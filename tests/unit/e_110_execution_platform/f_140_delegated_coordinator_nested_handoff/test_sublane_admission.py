"""Redmine-aware sublane admission/fill preflight policy tests (Redmine #12856).

Pins the pure classifier and admission preflight that sit on top of the #12855
fill-decision vocabulary (``vibes/docs/logics/coordinator-sublane-development-flow.md``
`### Lane State Classes` / `### Admission Rule` / `### Post-Dispatch Fill Loop`):

- :func:`classify_lane_state` maps each durable-gate signal onto the documented lane
  state class, including the integration / close / retirement nuances and the
  fail-closed behaviour for an unrecognized gate;
- the single most important invariant — an active ``implementing`` lane (start /
  progress, or a review returning changes) is **not** a stop reason — so an
  ``implementing``-only signal set with ready work + capacity dispatches;
- :func:`evaluate_sublane_admission` delegates the decision to the single #12855
  authority and derives the admission decision token;
- the Bandwidth Record Template renderer emits the documented fields;
- ``callback_delivery_failed`` is a coordinator-blocking lane state.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    ADMISSION_DISPATCH_SUBLANE,
    ADMISSION_STOP_AND_DRAIN,
    GATE_BLOCKED,
    GATE_CLOSE,
    GATE_IMPLEMENTATION_DONE,
    GATE_NONE,
    GATE_OWNER_CLOSE_APPROVAL,
    GATE_PROGRESS,
    GATE_REVIEW,
    GATE_REVIEW_REQUEST,
    GATE_START,
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    REVIEW_PENDING,
    CALLBACK_DELIVERY_FAILED,
    CALLBACK_DUE,
    LaneSignal,
    SublaneAdmissionInputs,
    classify_lane_state,
    evaluate_sublane_admission,
    render_admission_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    COORDINATOR_BLOCKING_STATES,
    FILL_DISPATCH_NEXT,
    FILL_STOP_COORDINATOR_BLOCKING,
    FILL_STOP_NO_READY_WORK,
    FILL_STOP_OWNER_OR_RELEASE_GATE,
    LANE_STATE_BLOCKED,
    LANE_STATE_CALLBACK_DELIVERY_FAILED,
    LANE_STATE_CALLBACK_DUE,
    LANE_STATE_CLOSE_WAITING,
    LANE_STATE_IDLE,
    LANE_STATE_IMPLEMENTING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_RETIRE_READY,
    LANE_STATE_REVIEW_WAITING,
)


def _signal(issue="12856", gate=GATE_NONE, **overrides):
    return LaneSignal(issue=issue, latest_gate=gate, **overrides)


class ClassifyImplementingTest(unittest.TestCase):
    def test_start_and_progress_are_implementing(self):
        self.assertEqual(classify_lane_state(_signal(gate=GATE_START)), LANE_STATE_IMPLEMENTING)
        self.assertEqual(classify_lane_state(_signal(gate=GATE_PROGRESS)), LANE_STATE_IMPLEMENTING)

    def test_review_requesting_changes_returns_to_implementing(self):
        # A review that requested changes sends work back to the implementer — it is
        # positive pipeline occupancy, not a coordinator-blocking stop.
        sig = _signal(gate=GATE_REVIEW, review_conclusion=REVIEW_CHANGES_REQUESTED)
        self.assertEqual(classify_lane_state(sig), LANE_STATE_IMPLEMENTING)


class ClassifyReviewTest(unittest.TestCase):
    def test_implementation_done_and_review_request_are_review_waiting(self):
        self.assertEqual(
            classify_lane_state(_signal(gate=GATE_IMPLEMENTATION_DONE)),
            LANE_STATE_REVIEW_WAITING,
        )
        self.assertEqual(
            classify_lane_state(_signal(gate=GATE_REVIEW_REQUEST)),
            LANE_STATE_REVIEW_WAITING,
        )

    def test_review_pending_is_review_waiting(self):
        sig = _signal(gate=GATE_REVIEW, review_conclusion=REVIEW_PENDING)
        self.assertEqual(classify_lane_state(sig), LANE_STATE_REVIEW_WAITING)

    def test_review_approved_is_owner_waiting(self):
        sig = _signal(gate=GATE_REVIEW, review_conclusion=REVIEW_APPROVED)
        self.assertEqual(classify_lane_state(sig), LANE_STATE_OWNER_WAITING)


class ClassifyIntegrationCloseRetireTest(unittest.TestCase):
    def test_owner_approval_commit_unmerged_is_integration_waiting(self):
        sig = _signal(
            gate=GATE_OWNER_CLOSE_APPROVAL, commit_bearing=True, integration_recorded=False
        )
        self.assertEqual(classify_lane_state(sig), LANE_STATE_INTEGRATION_WAITING)

    def test_owner_approval_integrated_open_is_close_waiting(self):
        sig = _signal(
            gate=GATE_OWNER_CLOSE_APPROVAL,
            commit_bearing=True,
            integration_recorded=True,
            issue_open=True,
        )
        self.assertEqual(classify_lane_state(sig), LANE_STATE_CLOSE_WAITING)

    def test_owner_approval_integrated_closed_is_retire_ready(self):
        sig = _signal(
            gate=GATE_OWNER_CLOSE_APPROVAL,
            commit_bearing=True,
            integration_recorded=True,
            issue_open=False,
        )
        self.assertEqual(classify_lane_state(sig), LANE_STATE_RETIRE_READY)

    def test_closed_issue_with_unmerged_commit_is_integration_waiting(self):
        # Spine: a closed issue whose only artifact is unmerged sublane commits is
        # integration_waiting, NOT retire_ready.
        sig = _signal(gate=GATE_CLOSE, commit_bearing=True, integration_recorded=False)
        self.assertEqual(classify_lane_state(sig), LANE_STATE_INTEGRATION_WAITING)

    def test_close_with_integration_recorded_is_retire_ready(self):
        sig = _signal(gate=GATE_CLOSE, commit_bearing=True, integration_recorded=True)
        self.assertEqual(classify_lane_state(sig), LANE_STATE_RETIRE_READY)

    def test_no_gate_is_idle(self):
        self.assertEqual(classify_lane_state(_signal(gate=GATE_NONE)), LANE_STATE_IDLE)


class ClassifyBlockingTest(unittest.TestCase):
    def test_recorded_blocker_is_blocked(self):
        self.assertEqual(
            classify_lane_state(_signal(gate=GATE_START, blocker_recorded=True)),
            LANE_STATE_BLOCKED,
        )

    def test_blocked_gate_is_blocked(self):
        self.assertEqual(classify_lane_state(_signal(gate=GATE_BLOCKED)), LANE_STATE_BLOCKED)

    def test_callback_states_classify(self):
        self.assertEqual(
            classify_lane_state(_signal(gate=GATE_START, callback_state=CALLBACK_DUE)),
            LANE_STATE_CALLBACK_DUE,
        )
        self.assertEqual(
            classify_lane_state(
                _signal(gate=GATE_START, callback_state=CALLBACK_DELIVERY_FAILED)
            ),
            LANE_STATE_CALLBACK_DELIVERY_FAILED,
        )

    def test_unknown_gate_fails_closed_to_blocked(self):
        # Fail-closed: an unrecognized gate is drained, not dispatched past.
        self.assertEqual(classify_lane_state(_signal(gate="nonsense")), LANE_STATE_BLOCKED)

    def test_callback_delivery_failed_is_coordinator_blocking(self):
        self.assertIn(LANE_STATE_CALLBACK_DELIVERY_FAILED, COORDINATOR_BLOCKING_STATES)


class AdmissionDecisionTest(unittest.TestCase):
    def test_implementing_only_with_ready_work_dispatches(self):
        inputs = SublaneAdmissionInputs(
            lane_signals=(_signal(issue="12856", gate=GATE_START),),
            ready_independent_work=1,
            capacity_remaining=2,
        )
        outcome = evaluate_sublane_admission(inputs)
        self.assertEqual(outcome.admission_decision, ADMISSION_DISPATCH_SUBLANE)
        self.assertEqual(outcome.fill_decision, FILL_DISPATCH_NEXT)
        self.assertTrue(outcome.should_dispatch)
        self.assertTrue(outcome.advisory)
        self.assertEqual(
            outcome.classified_lanes[0].state_class, LANE_STATE_IMPLEMENTING
        )

    def test_review_waiting_lane_stops_and_drains(self):
        inputs = SublaneAdmissionInputs(
            lane_signals=(
                _signal(issue="12856", gate=GATE_START),
                _signal(issue="12700", gate=GATE_REVIEW_REQUEST),
            ),
            ready_independent_work=1,
            capacity_remaining=2,
        )
        outcome = evaluate_sublane_admission(inputs)
        self.assertEqual(outcome.admission_decision, ADMISSION_STOP_AND_DRAIN)
        self.assertEqual(outcome.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertFalse(outcome.should_dispatch)
        self.assertIn("12700", outcome.fill.coordinator_blocking)

    def test_no_ready_work_stops(self):
        inputs = SublaneAdmissionInputs(
            lane_signals=(_signal(issue="12856", gate=GATE_START),),
            ready_independent_work=0,
            capacity_remaining=2,
        )
        outcome = evaluate_sublane_admission(inputs)
        self.assertEqual(outcome.fill_decision, FILL_STOP_NO_READY_WORK)

    def test_owner_or_release_gate_forces_stop(self):
        inputs = SublaneAdmissionInputs(
            lane_signals=(),
            ready_independent_work=1,
            capacity_remaining=2,
            owner_or_release_gate_active=True,
        )
        outcome = evaluate_sublane_admission(inputs)
        self.assertEqual(outcome.fill_decision, FILL_STOP_OWNER_OR_RELEASE_GATE)
        self.assertEqual(outcome.admission_decision, ADMISSION_STOP_AND_DRAIN)


class JournalRenderTest(unittest.TestCase):
    def test_journal_has_template_fields(self):
        inputs = SublaneAdmissionInputs(
            lane_signals=(
                _signal(issue="12856", gate=GATE_START),
                _signal(issue="12700", gate=GATE_REVIEW_REQUEST),
            ),
            ready_independent_work=1,
            capacity_remaining=2,
        )
        text = render_admission_journal(evaluate_sublane_admission(inputs))
        self.assertIn("## Sublane dispatch decision", text)
        self.assertIn("- current_lanes:", text)
        self.assertIn("12856: implementing", text)
        self.assertIn("12700: review_waiting", text)
        self.assertIn("admission_decision: stop_and_drain", text)
        self.assertIn("fill_decision: stop_coordinator_blocking", text)
        self.assertIn("advisory: true", text)

    def test_payload_round_trips_classification(self):
        inputs = SublaneAdmissionInputs(
            lane_signals=(_signal(issue="12856", gate=GATE_START),),
            ready_independent_work=1,
            capacity_remaining=2,
        )
        payload = evaluate_sublane_admission(inputs).as_payload()
        self.assertEqual(payload["admission_decision"], ADMISSION_DISPATCH_SUBLANE)
        self.assertTrue(payload["advisory"])
        self.assertEqual(
            payload["classified_lanes"],
            [{"issue": "12856", "state_class": LANE_STATE_IMPLEMENTING}],
        )
        self.assertEqual(payload["fill"]["fill_decision"], FILL_DISPATCH_NEXT)


if __name__ == "__main__":
    unittest.main()
