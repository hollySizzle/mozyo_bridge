"""Enriched ``workflow.next_action`` policy tests (Redmine #12671).

Pins the pure enrichment that turns the #12857 overall next action into the command-result
``workflow.next_action`` the spine roadmap US #12671
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### 設計思想`) requires:

- the risk / requires_confirmation policy per action token, and its fail-closed default for
  an unknown action (critical + confirm + ``unknown_action``);
- the route identity / anchor enrichment from caller-supplied lookups, with the public-safe
  pointer (never a pane id) carried through;
- the fail-closed escalation when a lane-targeted routing action has no resolved route
  (``route_identity_unresolved`` + requires_confirmation + risk floor);
- the ``WorkflowCommandResult`` envelope nesting ``workflow.{state,next_action}``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
    BLOCKED_ROUTE_IDENTITY_UNRESOLVED,
    BLOCKED_UNKNOWN_ACTION,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_NONE,
    RouteCandidate,
    WorkflowCommandResult,
    derive_workflow_next_action,
    render_command_result_journal,
    risk_policy_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ACTION_AGGREGATE_OWNER_APPROVAL,
    ACTION_AWAIT_IMPLEMENTATION,
    ACTION_DISPATCH_NEXT_SUBLANE,
    ACTION_PERFORM_REVIEW,
    LaneEvent,
    evaluate_workflow_runtime,
)


def _state(events, **kwargs):
    return evaluate_workflow_runtime(events, **kwargs)


class RiskPolicyTest(unittest.TestCase):
    def test_owner_release_gate_is_critical_and_confirms(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
            ACTION_RESOLVE_OWNER_OR_RELEASE_GATE,
        )

        risk, confirm, _suggested, blocked = risk_policy_for(
            ACTION_RESOLVE_OWNER_OR_RELEASE_GATE
        )
        self.assertEqual(risk, RISK_CRITICAL)
        self.assertTrue(confirm)
        self.assertEqual(blocked, "")

    def test_await_implementation_is_low_risk_no_confirm(self):
        risk, confirm, _suggested, blocked = risk_policy_for(ACTION_AWAIT_IMPLEMENTATION)
        self.assertEqual(risk, RISK_NONE)
        self.assertFalse(confirm)
        self.assertEqual(blocked, "")

    def test_unknown_action_fails_closed(self):
        risk, confirm, suggested, blocked = risk_policy_for("not_a_real_action")
        self.assertEqual(risk, RISK_CRITICAL)
        self.assertTrue(confirm)
        self.assertEqual(suggested, "")
        self.assertEqual(blocked, BLOCKED_UNKNOWN_ACTION)


class DeriveTest(unittest.TestCase):
    def test_dispatch_targets_no_lane_and_is_not_route_blocked(self):
        # implementing-only (start gate) with ready work + capacity -> dispatch_next_sublane.
        state = _state(
            [LaneEvent(event_id="a", issue="12671", gate="start")],
            ready_independent_work=1,
            capacity_remaining=2,
        )
        na = derive_workflow_next_action(state)
        self.assertEqual(na.action, ACTION_DISPATCH_NEXT_SUBLANE)
        self.assertEqual(na.target_issue, "")
        self.assertEqual(na.route_identity, "")
        self.assertEqual(na.blocked_reason, "")
        self.assertTrue(na.requires_confirmation)

    def test_routing_action_resolves_route_and_anchor(self):
        # review_request -> perform_review (auditor routing action); auditor expects codex.
        state = _state(
            [LaneEvent(event_id="12671:68864", issue="12671", gate="review_request", commit_bearing=True)]
        )
        na = derive_workflow_next_action(
            state,
            issue_routes={
                "12671": [RouteCandidate("codex", "route=r1 ws=ws1 lane=default role=codex pane_name=gw")]
            },
            issue_anchors={"12671": "12671:68864"},
        )
        self.assertEqual(na.action, ACTION_PERFORM_REVIEW)
        self.assertEqual(na.target_issue, "12671")
        self.assertEqual(na.anchor, "12671:68864")
        self.assertIn("pane_name=gw", na.route_identity)
        self.assertNotIn("%", na.route_identity)  # never a pane id
        self.assertEqual(na.blocked_reason, "")

    def test_routing_action_without_route_fails_closed(self):
        state = _state(
            [LaneEvent(event_id="12671:68864", issue="12671", gate="review_request", commit_bearing=True)]
        )
        na = derive_workflow_next_action(state)  # no routes supplied
        self.assertEqual(na.action, ACTION_PERFORM_REVIEW)
        self.assertEqual(na.blocked_reason, BLOCKED_ROUTE_IDENTITY_UNRESOLVED)
        self.assertTrue(na.requires_confirmation)
        # The medium-risk review is escalated to at least high when route is unresolved.
        self.assertEqual(na.risk_level, RISK_HIGH)
        self.assertTrue(na.is_blocked)

    def test_owner_aware_selection_picks_provider_match_not_key_order(self):
        # An auditor action with BOTH a worker(claude) and a gateway(codex) route for the
        # same issue must select the codex route, never the (alphabetically earlier) worker.
        state = _state(
            [LaneEvent(event_id="12671:68864", issue="12671", gate="review_request", commit_bearing=True)]
        )
        na = derive_workflow_next_action(
            state,
            issue_routes={
                "12671": [
                    RouteCandidate("claude", "route=z-worker ws=ws1 lane=default role=claude pane_name=worker"),
                    RouteCandidate("codex", "route=a-gateway ws=ws1 lane=default role=codex pane_name=gateway"),
                ]
            },
        )
        self.assertEqual(na.action, ACTION_PERFORM_REVIEW)
        self.assertIn("pane_name=gateway", na.route_identity)
        self.assertEqual(na.blocked_reason, "")

    def test_owner_aware_selection_last_write_wins_among_matching(self):
        # Two codex routes for the issue: the most-recently-recorded (last) one wins.
        state = _state(
            [LaneEvent(event_id="12671:68864", issue="12671", gate="review_request", commit_bearing=True)]
        )
        na = derive_workflow_next_action(
            state,
            issue_routes={
                "12671": [
                    RouteCandidate("codex", "route=old ws=ws1 lane=default role=codex pane_name=old"),
                    RouteCandidate("codex", "route=new ws=ws1 lane=default role=codex pane_name=new"),
                ]
            },
        )
        self.assertIn("pane_name=new", na.route_identity)

    def test_owner_route_provider_mismatch_fails_closed(self):
        # Only a worker(claude) route exists for an auditor action -> no provider match.
        state = _state(
            [LaneEvent(event_id="12671:68864", issue="12671", gate="review_request", commit_bearing=True)]
        )
        na = derive_workflow_next_action(
            state,
            issue_routes={
                "12671": [RouteCandidate("claude", "route=w ws=ws1 lane=default role=claude pane_name=worker")]
            },
        )
        self.assertEqual(na.route_identity, "")
        self.assertEqual(na.blocked_reason, BLOCKED_ROUTE_IDENTITY_UNRESOLVED)
        self.assertTrue(na.requires_confirmation)

    def test_owner_waiting_aggregates_owner_approval(self):
        state = _state(
            [
                LaneEvent(event_id="r", issue="12671", gate="review", review_conclusion="approved", commit_bearing=True),
            ]
        )
        na = derive_workflow_next_action(
            state,
            issue_routes={
                "12671": [RouteCandidate("codex", "route=r1 ws=ws1 lane=default role=codex pane_name=gw")]
            },
            issue_anchors={"12671": "r"},
        )
        self.assertEqual(na.action, ACTION_AGGREGATE_OWNER_APPROVAL)
        self.assertEqual(na.owner_role, "coordinator")
        self.assertEqual(na.risk_level, RISK_HIGH)
        self.assertTrue(na.requires_confirmation)


class EnvelopeTest(unittest.TestCase):
    def test_command_result_nests_workflow_state_and_next_action(self):
        state = _state([LaneEvent(event_id="a", issue="12671", gate="start")])
        na = derive_workflow_next_action(state)
        payload = WorkflowCommandResult(state=state, next_action=na).as_payload()
        self.assertIn("workflow", payload)
        wf = payload["workflow"]
        self.assertIn("state", wf)
        self.assertIn("next_action", wf)
        self.assertEqual(wf["next_action"]["action"], na.action)
        # the enriched fields are present on the next_action payload
        for key in (
            "owner_role",
            "route_identity",
            "anchor",
            "suggested_command",
            "risk_level",
            "requires_confirmation",
            "blocked_reason",
        ):
            self.assertIn(key, wf["next_action"])

    def test_journal_renders_enriched_fields_without_pane_id(self):
        state = _state(
            [LaneEvent(event_id="12671:68864", issue="12671", gate="review_request", commit_bearing=True)]
        )
        na = derive_workflow_next_action(
            state,
            issue_routes={
                "12671": [RouteCandidate("codex", "route=r1 ws=ws1 lane=default role=codex pane_name=gw")]
            },
            issue_anchors={"12671": "12671:68864"},
        )
        text = render_command_result_journal(WorkflowCommandResult(state=state, next_action=na))
        self.assertIn("risk_level:", text)
        self.assertIn("requires_confirmation:", text)
        self.assertIn("route_identity:", text)
        self.assertNotIn("%", text)  # no pane id in the durable record


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
