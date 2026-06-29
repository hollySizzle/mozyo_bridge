"""Stateful workflow runtime (first vertical slice) policy tests (Redmine #12857).

Pins the pure event-replay runtime that sits on top of the #12856 admission classifier
and the #12855 fill decision
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### 設計思想`):

- :func:`replay_events` folds an ordered event log into per-lane state with **duplicate
  suppression** (a repeated ``event_id`` is suppressed; replay is idempotent) and
  last-applied-event-per-issue-wins;
- :func:`evaluate_workflow_runtime` classifies the replayed state via the single #12856
  authority and derives the per-lane owed action + one overall next action;
- the carried invariant — an active ``implementing`` lane is not a stop reason — so an
  ``implementing``-only event set with ready work + capacity dispatches the next sublane;
- a coordinator-blocking lane drives the overall next action to its precise owed action
  (with abstract owner role + target issue), never a dispatch;
- roles are abstract workflow roles, never a runtime provider.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ACTION_AGGREGATE_OWNER_APPROVAL,
    ACTION_AWAIT_IMPLEMENTATION,
    ACTION_DISPATCH_NEXT_SUBLANE,
    ACTION_HOLD,
    ACTION_INTEGRATE,
    ACTION_PERFORM_REVIEW,
    ACTION_REDELIVER_CALLBACK,
    ACTION_RESOLVE_BLOCKER,
    ACTION_RESOLVE_OWNER_OR_RELEASE_GATE,
    ACTION_RETIRE_LANE,
    ROLE_AUDITOR,
    ROLE_COORDINATOR,
    ROLE_IMPLEMENTER,
    ROLE_NONE,
    ROLE_OWNER,
    LaneEvent,
    evaluate_workflow_runtime,
    pending_action_for,
    render_runtime_journal,
    replay_events,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_BLOCKED,
    GATE_CLOSE,
    GATE_OWNER_CLOSE_APPROVAL,
    GATE_REVIEW,
    GATE_REVIEW_REQUEST,
    GATE_START,
    REVIEW_APPROVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    LANE_STATE_BLOCKED,
    LANE_STATE_IDLE,
    LANE_STATE_IMPLEMENTING,
)


def _ev(event_id, issue, gate, **kw):
    return LaneEvent(event_id=event_id, issue=issue, gate=gate, **kw)


class ReplayDedupTest(unittest.TestCase):
    def test_duplicate_event_id_is_suppressed_and_replay_is_idempotent(self):
        log = [
            _ev("a", "12857", GATE_START),
            _ev("a", "12857", GATE_START),  # same durable anchor -> suppressed
            _ev("b", "12858", GATE_REVIEW_REQUEST),
        ]
        first = replay_events(log)
        self.assertEqual(first.applied_event_ids, ("a", "b"))
        self.assertEqual(first.suppressed_event_ids, ("a",))
        self.assertEqual(first.event_count, 3)
        # Two lanes, one signal each.
        self.assertEqual({s.issue for s in first.signals}, {"12857", "12858"})
        # Replaying the same log again yields the same partition (idempotent).
        again = replay_events(log)
        self.assertEqual(again.applied_event_ids, first.applied_event_ids)
        self.assertEqual(again.suppressed_event_ids, first.suppressed_event_ids)
        self.assertEqual(
            tuple(s.latest_gate for s in again.signals),
            tuple(s.latest_gate for s in first.signals),
        )

    def test_last_applied_event_per_issue_wins(self):
        result = replay_events(
            [
                _ev("a", "12857", GATE_START),
                _ev("b", "12857", GATE_REVIEW_REQUEST),  # newer state for same lane
            ]
        )
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].latest_gate, GATE_REVIEW_REQUEST)
        self.assertEqual(result.applied_event_ids, ("a", "b"))

    def test_first_seen_issue_order_preserved(self):
        result = replay_events(
            [
                _ev("a", "12858", GATE_START),
                _ev("b", "12857", GATE_START),
                _ev("c", "12858", GATE_REVIEW_REQUEST),
            ]
        )
        self.assertEqual([s.issue for s in result.signals], ["12858", "12857"])


class PendingActionMappingTest(unittest.TestCase):
    def test_known_states_map_to_action_and_owner(self):
        self.assertEqual(
            pending_action_for(LANE_STATE_IMPLEMENTING),
            (ACTION_AWAIT_IMPLEMENTATION, ROLE_IMPLEMENTER),
        )
        self.assertEqual(pending_action_for(LANE_STATE_IDLE)[1], ROLE_NONE)

    def test_unknown_state_fails_closed_to_blocker_resolution(self):
        action, owner = pending_action_for("totally_unknown_state")
        self.assertEqual(action, ACTION_RESOLVE_BLOCKER)
        self.assertEqual(owner, ROLE_COORDINATOR)


class NextActionTest(unittest.TestCase):
    def test_implementing_only_with_ready_work_dispatches(self):
        state = evaluate_workflow_runtime(
            [_ev("a", "12857", GATE_START)],
            ready_independent_work=1,
            capacity_remaining=1,
        )
        self.assertEqual(state.next_action.action, ACTION_DISPATCH_NEXT_SUBLANE)
        self.assertEqual(state.next_action.owner_role, ROLE_COORDINATOR)
        self.assertEqual(state.next_action.target_issue, "")
        self.assertTrue(state.admission.should_dispatch)
        # Per-lane read model still shows the implementing lane's owed action.
        self.assertEqual(state.lane_actions[0].action, ACTION_AWAIT_IMPLEMENTATION)

    def test_review_request_lane_drives_perform_review(self):
        state = evaluate_workflow_runtime([_ev("a", "12857", GATE_REVIEW_REQUEST)])
        self.assertEqual(state.next_action.action, ACTION_PERFORM_REVIEW)
        self.assertEqual(state.next_action.owner_role, ROLE_AUDITOR)
        self.assertEqual(state.next_action.target_issue, "12857")

    def test_owner_release_gate_targets_owner(self):
        state = evaluate_workflow_runtime(
            [_ev("a", "12857", GATE_START)],
            ready_independent_work=1,
            capacity_remaining=1,
            owner_or_release_gate_active=True,
        )
        self.assertEqual(
            state.next_action.action, ACTION_RESOLVE_OWNER_OR_RELEASE_GATE
        )
        self.assertEqual(state.next_action.owner_role, ROLE_OWNER)

    def test_drain_order_picks_owner_waiting_over_blocked(self):
        # Two coordinator-blocking lanes; the spine drain order selects owner aggregation
        # first. (review approved -> owner_waiting; explicit blocker -> blocked.)
        state = evaluate_workflow_runtime(
            [
                _ev("a", "12858", GATE_BLOCKED),
                _ev("b", "12857", GATE_REVIEW, review_conclusion=REVIEW_APPROVED),
            ]
        )
        self.assertEqual(state.next_action.action, ACTION_AGGREGATE_OWNER_APPROVAL)
        self.assertEqual(state.next_action.target_issue, "12857")

    def test_callback_delivery_failed_lane_drives_redeliver(self):
        state = evaluate_workflow_runtime(
            [_ev("a", "12857", GATE_START, callback_state="delivery_failed")]
        )
        self.assertEqual(state.next_action.action, ACTION_REDELIVER_CALLBACK)
        self.assertEqual(state.next_action.owner_role, ROLE_COORDINATOR)
        self.assertEqual(state.next_action.target_issue, "12857")

    def test_integration_waiting_lane_drives_integrate(self):
        state = evaluate_workflow_runtime(
            [_ev("a", "12857", GATE_CLOSE, commit_bearing=True)]
        )
        self.assertEqual(state.next_action.action, ACTION_INTEGRATE)
        self.assertEqual(state.next_action.target_issue, "12857")

    def test_no_blocking_no_ready_work_with_implementing_awaits(self):
        # implementing lane, but no ready independent work -> not a stop *reason* per se,
        # but nothing to dispatch; the overall action surfaces the implementing lane.
        state = evaluate_workflow_runtime([_ev("a", "12857", GATE_START)])
        self.assertEqual(state.next_action.action, ACTION_AWAIT_IMPLEMENTATION)
        self.assertEqual(state.next_action.owner_role, ROLE_IMPLEMENTER)
        self.assertEqual(state.next_action.target_issue, "12857")

    def test_retire_ready_preferred_over_implementing_when_idle(self):
        state = evaluate_workflow_runtime(
            [
                _ev("a", "12857", GATE_START),
                _ev(
                    "b",
                    "12858",
                    GATE_OWNER_CLOSE_APPROVAL,
                    issue_open=False,
                ),  # owner approval + closed + no commit -> retire_ready
            ]
        )
        self.assertEqual(state.next_action.action, ACTION_RETIRE_LANE)
        self.assertEqual(state.next_action.target_issue, "12858")

    def test_empty_log_holds(self):
        state = evaluate_workflow_runtime([])
        self.assertEqual(state.next_action.action, ACTION_HOLD)
        self.assertEqual(state.next_action.owner_role, ROLE_NONE)
        self.assertEqual(state.lane_actions, ())

    def test_unknown_gate_classifies_blocked_and_drives_blocker_resolution(self):
        # An event whose gate cannot be placed is fail-closed to blocked by #12856.
        state = evaluate_workflow_runtime(
            [LaneEvent(event_id="a", issue="12857", gate="wat_gate")]
        )
        self.assertEqual(state.lane_actions[0].state_class, LANE_STATE_BLOCKED)
        self.assertEqual(state.next_action.action, ACTION_RESOLVE_BLOCKER)


class PayloadAndJournalTest(unittest.TestCase):
    def test_payload_carries_state_and_next_action(self):
        state = evaluate_workflow_runtime(
            [
                _ev("a", "12857", GATE_START),
                _ev("a", "12857", GATE_START),
            ],
            ready_independent_work=1,
            capacity_remaining=1,
        )
        payload = state.as_payload()
        self.assertTrue(payload["advisory"])
        self.assertIn("next_action", payload)
        self.assertIn("state", payload)
        self.assertEqual(
            payload["next_action"]["action"], ACTION_DISPATCH_NEXT_SUBLANE
        )
        self.assertEqual(payload["state"]["applied_event_ids"], ["a"])
        self.assertEqual(payload["state"]["suppressed_event_ids"], ["a"])
        self.assertIn("admission", payload["state"])

    def test_journal_render_includes_next_action_and_suppression(self):
        state = evaluate_workflow_runtime(
            [
                _ev("a", "12857", GATE_REVIEW_REQUEST),
                _ev("a", "12857", GATE_REVIEW_REQUEST),
            ]
        )
        text = render_runtime_journal(state)
        self.assertIn("## Workflow runtime next action", text)
        self.assertIn(f"- next_action: {ACTION_PERFORM_REVIEW}", text)
        self.assertIn("- owner_role: auditor", text)
        self.assertIn("- target_issue: 12857", text)
        self.assertIn("- suppressed_event_ids: a", text)
        # Reuses the #12856 Bandwidth Record Template.
        self.assertIn("## Sublane dispatch decision", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
