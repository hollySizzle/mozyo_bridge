"""Redmine journal -> pending workflow action intake policy tests (Redmine #12672).

Pins the pure event-watcher intake that turns structured Redmine journal markers into a
pending workflow action, with the duplicate-suppression / ambiguity / fail-closed posture
the spine roadmap US #12672
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### ロードマップUS` step 3)
fixes first:

- the durable ``redmine:<issue>:<journal>`` event id and its empty-anchor rejection;
- structured-marker validation: the ``review_result`` -> ``review`` gate alias, and the
  fail-closed rejection of an unknown gate / conclusion / callback (never a prose guess);
- duplicate suppression at intake (anchor already recorded, or repeated in-batch);
- the watcher's stricter, ambiguity-aware route selection (a single match resolves; two
  distinct provider-matching routes fail closed ``route_ambiguous``), reusing the #12671
  role->provider binding;
- the pending-action classification precedence (blocked -> failed, ambiguous -> failed,
  confirm -> needs_confirmation, else ready) and the end-to-end fold with the anchor.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    FAILED_ROUTE_AMBIGUOUS,
    INTAKE_ACCEPTED,
    INTAKE_SUPPRESSED,
    PENDING_FAILED,
    PENDING_NEEDS_CONFIRMATION,
    PENDING_READY,
    JournalMarkerError,
    build_marker,
    classify_intake,
    classify_pending_action,
    evaluate_event_intake,
    redmine_event_id,
    select_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_REVIEW,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
    BLOCKED_ROUTE_IDENTITY_UNRESOLVED,
    BLOCKED_UNKNOWN_ACTION,
    RISK_HIGH,
    RouteCandidate,
    WorkflowNextAction,
    derive_workflow_next_action,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ACTION_AWAIT_IMPLEMENTATION,
    ACTION_PERFORM_REVIEW,
    ROLE_AUDITOR,
    ROLE_IMPLEMENTER,
    LaneEvent,
    evaluate_workflow_runtime,
)


# ---------------------------------------------------------------------------
# Event id / durable anchor.
# ---------------------------------------------------------------------------


class EventIdTest(unittest.TestCase):
    def test_event_id_is_redmine_issue_journal(self):
        self.assertEqual(redmine_event_id("12672", "68978"), "redmine:12672:68978")

    def test_event_id_strips_whitespace(self):
        self.assertEqual(redmine_event_id(" 12672 ", " 68978 "), "redmine:12672:68978")

    def test_empty_issue_or_journal_rejected(self):
        with self.assertRaises(ValueError):
            redmine_event_id("", "68978")
        with self.assertRaises(ValueError):
            redmine_event_id("12672", "")

    def test_marker_event_id_property_matches(self):
        marker = build_marker("12672", "68978", "review_request")
        self.assertEqual(marker.event_id, "redmine:12672:68978")
        self.assertEqual(marker.to_lane_event().event_id, "redmine:12672:68978")


# ---------------------------------------------------------------------------
# Structured marker validation + alias.
# ---------------------------------------------------------------------------


class BuildMarkerTest(unittest.TestCase):
    def test_review_result_alias_maps_to_review_gate(self):
        marker = build_marker("12672", "1", "review_result", review_conclusion="approved")
        self.assertEqual(marker.gate, GATE_REVIEW)
        self.assertEqual(marker.review_conclusion, "approved")

    def test_known_gate_passthrough(self):
        self.assertEqual(build_marker("12672", "1", "implementation_done").gate,
                         "implementation_done")

    def test_unknown_gate_fails_closed(self):
        with self.assertRaises(JournalMarkerError):
            build_marker("12672", "1", "frobnicate")

    def test_unknown_conclusion_fails_closed(self):
        with self.assertRaises(JournalMarkerError):
            build_marker("12672", "1", "review", review_conclusion="maybe")

    def test_unknown_callback_fails_closed(self):
        with self.assertRaises(JournalMarkerError):
            build_marker("12672", "1", "review_request", callback_state="someday")

    def test_empty_anchor_rejected(self):
        with self.assertRaises(ValueError):
            build_marker("", "1", "review_request")


# ---------------------------------------------------------------------------
# Intake duplicate suppression.
# ---------------------------------------------------------------------------


class ClassifyIntakeTest(unittest.TestCase):
    def test_new_anchor_accepted(self):
        m = build_marker("12672", "68978", "review_request")
        records = classify_intake([m], known_event_ids=[])
        self.assertEqual(records[0].disposition, INTAKE_ACCEPTED)

    def test_already_recorded_anchor_suppressed(self):
        m = build_marker("12672", "68978", "review_request")
        records = classify_intake([m], known_event_ids=["redmine:12672:68978"])
        self.assertEqual(records[0].disposition, INTAKE_SUPPRESSED)

    def test_in_batch_repeat_suppressed(self):
        m1 = build_marker("12672", "68978", "review_request")
        m2 = build_marker("12672", "68978", "review_request")
        records = classify_intake([m1, m2], known_event_ids=[])
        self.assertEqual(records[0].disposition, INTAKE_ACCEPTED)
        self.assertEqual(records[1].disposition, INTAKE_SUPPRESSED)


# ---------------------------------------------------------------------------
# Ambiguity-aware route selection.
# ---------------------------------------------------------------------------


class SelectRouteTest(unittest.TestCase):
    def test_single_provider_match_resolves(self):
        pointer, reason = select_route(
            ROLE_AUDITOR, [RouteCandidate(provider_role="codex", pointer="p-codex")]
        )
        self.assertEqual(pointer, "p-codex")
        self.assertEqual(reason, "")

    def test_same_pointer_twice_is_not_ambiguous(self):
        pointer, reason = select_route(
            ROLE_AUDITOR,
            [
                RouteCandidate(provider_role="codex", pointer="p-codex"),
                RouteCandidate(provider_role="codex", pointer="p-codex"),
            ],
        )
        self.assertEqual(pointer, "p-codex")
        self.assertEqual(reason, "")

    def test_two_distinct_provider_matches_fail_ambiguous(self):
        pointer, reason = select_route(
            ROLE_AUDITOR,
            [
                RouteCandidate(provider_role="codex", pointer="p-a"),
                RouteCandidate(provider_role="codex", pointer="p-b"),
            ],
        )
        self.assertEqual(pointer, "")
        self.assertEqual(reason, FAILED_ROUTE_AMBIGUOUS)

    def test_non_matching_provider_is_no_match_not_ambiguous(self):
        # auditor expects codex; a claude-only candidate set does not match (and a single
        # mismatch is "missing", surfaced as the #12671 unresolved reason by the caller).
        pointer, reason = select_route(
            ROLE_AUDITOR, [RouteCandidate(provider_role="claude", pointer="p-claude")]
        )
        self.assertEqual(pointer, "")
        self.assertEqual(reason, "")

    def test_implementer_expects_claude(self):
        pointer, reason = select_route(
            ROLE_IMPLEMENTER, [RouteCandidate(provider_role="claude", pointer="p-claude")]
        )
        self.assertEqual(pointer, "p-claude")
        self.assertEqual(reason, "")


# ---------------------------------------------------------------------------
# Pending action classification precedence.
# ---------------------------------------------------------------------------


def _next_action(**kwargs) -> WorkflowNextAction:
    base = dict(
        action=ACTION_PERFORM_REVIEW,
        owner_role=ROLE_AUDITOR,
        target_issue="12672",
        route_identity="route=r ws=w lane=default role=codex pane_name=a",
        anchor="redmine:12672:68978",
        suggested_command="mozyo-bridge workflow step",
        risk_level="medium",
        requires_confirmation=False,
        blocked_reason="",
        reason="review owed",
    )
    base.update(kwargs)
    return WorkflowNextAction(**base)


class ClassifyPendingActionTest(unittest.TestCase):
    def test_blocked_reason_is_failed(self):
        na = _next_action(
            route_identity="", blocked_reason=BLOCKED_ROUTE_IDENTITY_UNRESOLVED,
            requires_confirmation=True,
        )
        pending = classify_pending_action(na)
        self.assertEqual(pending.status, PENDING_FAILED)
        self.assertEqual(pending.failed_reason, BLOCKED_ROUTE_IDENTITY_UNRESOLVED)

    def test_unknown_action_is_failed(self):
        na = _next_action(action="frobnicate", blocked_reason=BLOCKED_UNKNOWN_ACTION)
        self.assertEqual(classify_pending_action(na).status, PENDING_FAILED)

    def test_ambiguous_route_is_failed_route_ambiguous(self):
        routes = {
            "12672": [
                RouteCandidate(provider_role="codex", pointer="p-a"),
                RouteCandidate(provider_role="codex", pointer="p-b"),
            ]
        }
        pending = classify_pending_action(_next_action(), issue_routes=routes)
        self.assertEqual(pending.status, PENDING_FAILED)
        self.assertEqual(pending.failed_reason, FAILED_ROUTE_AMBIGUOUS)
        # The fail-closed result escalates risk and forces confirmation.
        self.assertTrue(pending.next_action.requires_confirmation)
        self.assertEqual(pending.next_action.risk_level, RISK_HIGH)

    def test_ambiguous_route_failure_preserves_provider_under_rebind(self):
        # j#71977: the fail-closed route_ambiguous rebuild must keep the enrichment's
        # resolved provider — under an auditor -> claude rebind the failed pending action
        # still reports provider "claude", not the dataclass default "".
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
            RoleProviderBinding,
        )

        binding = RoleProviderBinding.default().with_overrides({"auditor": "claude"})
        routes = {
            "12672": [
                RouteCandidate(provider_role="claude", pointer="p-a"),
                RouteCandidate(provider_role="claude", pointer="p-b"),
            ]
        }
        pending = classify_pending_action(
            _next_action(provider="claude"), issue_routes=routes, binding=binding
        )
        self.assertEqual(pending.status, PENDING_FAILED)
        self.assertEqual(pending.failed_reason, FAILED_ROUTE_AMBIGUOUS)
        self.assertEqual(pending.next_action.provider, "claude")

    def test_single_route_requires_confirmation_is_needs_confirmation(self):
        routes = {"12672": [RouteCandidate(provider_role="codex", pointer="p-a")]}
        pending = classify_pending_action(
            _next_action(requires_confirmation=True), issue_routes=routes
        )
        self.assertEqual(pending.status, PENDING_NEEDS_CONFIRMATION)

    def test_single_route_no_confirmation_is_ready(self):
        routes = {"12672": [RouteCandidate(provider_role="codex", pointer="p-a")]}
        pending = classify_pending_action(_next_action(), issue_routes=routes)
        self.assertEqual(pending.status, PENDING_READY)
        self.assertEqual(pending.failed_reason, "")


# ---------------------------------------------------------------------------
# End-to-end intake fold.
# ---------------------------------------------------------------------------


class EvaluateEventIntakeTest(unittest.TestCase):
    def test_review_request_with_route_is_ready_and_anchored(self):
        markers = [build_marker("12672", "68978", "review_request")]
        routes = {"12672": [RouteCandidate(provider_role="codex", pointer="p-codex")]}
        outcome = evaluate_event_intake(markers, issue_routes=routes)
        self.assertEqual(outcome.pending_action.status, PENDING_READY)
        self.assertEqual(outcome.pending_action.next_action.action, ACTION_PERFORM_REVIEW)
        # The durable anchor is the redmine:<issue>:<journal> event id.
        self.assertEqual(outcome.pending_action.anchor, "redmine:12672:68978")
        self.assertEqual(len(outcome.accepted), 1)

    def test_missing_route_is_failed_never_resolved(self):
        markers = [build_marker("12672", "68978", "review_request")]
        outcome = evaluate_event_intake(markers, issue_routes={})
        self.assertEqual(outcome.pending_action.status, PENDING_FAILED)
        self.assertEqual(
            outcome.pending_action.failed_reason, BLOCKED_ROUTE_IDENTITY_UNRESOLVED
        )

    def test_recorded_event_kept_and_duplicate_marker_suppressed(self):
        # The lane already has implementation_done recorded; re-observing it is suppressed,
        # while a new review_request advances the lane.
        recorded = [
            LaneEvent(
                event_id="redmine:12672:68978",
                issue="12672",
                gate="implementation_done",
            )
        ]
        markers = [
            build_marker("12672", "68978", "implementation_done"),  # duplicate anchor
            build_marker("12672", "69001", "review_request"),  # new
        ]
        outcome = evaluate_event_intake(
            markers,
            recorded_events=recorded,
            issue_routes={"12672": [RouteCandidate(provider_role="codex", pointer="p")]},
        )
        dispositions = {r.event_id: r.disposition for r in outcome.intake}
        self.assertEqual(dispositions["redmine:12672:68978"], INTAKE_SUPPRESSED)
        self.assertEqual(dispositions["redmine:12672:69001"], INTAKE_ACCEPTED)
        # Only the new event is persisted-worthy.
        self.assertEqual(
            [e.event_id for e in outcome.accepted_events], ["redmine:12672:69001"]
        )
        # The latest event is the anchor.
        self.assertEqual(outcome.pending_action.anchor, "redmine:12672:69001")

    def test_payload_nests_workflow_envelope(self):
        outcome = evaluate_event_intake(
            [build_marker("12672", "68978", "review_request")], issue_routes={}
        )
        payload = outcome.as_payload()
        self.assertIn("workflow", payload)
        self.assertIn("state", payload["workflow"])
        self.assertIn("next_action", payload["workflow"])
        self.assertIn("pending_action", payload)
        self.assertIn("intake", payload)

    def test_implementation_done_only_awaits_implementation_no_route_needed(self):
        # A lane mid-implementation (start gate) is positive occupancy: the await action is
        # not a routing action, so a missing route does not fail it.
        markers = [build_marker("12672", "68978", "start")]
        outcome = evaluate_event_intake(
            markers, issue_routes={}, ready_independent_work=0, capacity_remaining=0
        )
        self.assertEqual(
            outcome.pending_action.next_action.action, ACTION_AWAIT_IMPLEMENTATION
        )
        self.assertEqual(outcome.pending_action.status, PENDING_READY)


class ConsistencyWithDeriveTest(unittest.TestCase):
    def test_watcher_anchor_matches_derive_next_action(self):
        # The intake's anchor must match what derive_workflow_next_action computes directly.
        events = [LaneEvent(event_id="redmine:12672:68978", issue="12672",
                            gate="review_request")]
        state = evaluate_workflow_runtime(events)
        direct = derive_workflow_next_action(
            state, issue_anchors={"12672": "redmine:12672:68978"}
        )
        outcome = evaluate_event_intake(
            [build_marker("12672", "68978", "review_request")], issue_routes={}
        )
        self.assertEqual(outcome.pending_action.anchor, direct.anchor)


if __name__ == "__main__":
    unittest.main()
