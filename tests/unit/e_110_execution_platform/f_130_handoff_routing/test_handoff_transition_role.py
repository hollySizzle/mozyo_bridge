"""Tests for the ticketless transition role/action boundary payload (Redmine #12706).

GK3500 smoke #12698 surfaced a lane-boundary defect: a receiver inferred its lane
role from docs-readable context and made the parent project gateway's
``no_dispatch`` decision itself. The fix carries an explicit transition role/action
boundary (``current_role`` / ``allowed_actions`` / ``forbidden_actions`` /
``handoff_target_role``) on the standard handoff transition payload and the durable
delivery record so the receiver never infers its role. Unknown / malformed
boundaries fail closed; omitting the boundary is the explicit fallback.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RedmineAnchor,
    build_delivery_record,
    build_notification_body,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    GATEWAY_CALLBACK_PRIMITIVE_RETURN,
    GATEWAY_LOCAL_ANSWER_NOT_CALLBACK,
    PROJECT_DOMAIN_DECISIONS,
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
    TRANSITION_ROLE_BOUNDARIES,
    TRANSITION_ROLE_TOKENS,
    TransitionRoleBoundary,
    TransitionRoleError,
    resolve_transition_role,
    transition_role_from_payload,
)


class ResolveTransitionRoleTest(unittest.TestCase):
    def test_every_token_resolves_to_a_boundary(self) -> None:
        for token in TRANSITION_ROLE_TOKENS:
            boundary = resolve_transition_role(token)
            self.assertEqual(boundary.current_role, token)
            self.assertTrue(boundary.allowed_actions)
            self.assertTrue(boundary.forbidden_actions)
            self.assertTrue(boundary.handoff_target_role)

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            resolve_transition_role("definitely_not_a_role")

    def test_grandparent_boundary_matches_the_issue_spec(self) -> None:
        boundary = resolve_transition_role(ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(
            boundary.allowed_actions,
            (
                "classify_ticketless_consultation",
                "resolve_or_start_parent_project_gateway",
                "handoff_to_parent_gateway",
                "return_blocked_if_gateway_unavailable",
            ),
        )
        self.assertEqual(
            boundary.forbidden_actions,
            (
                "project_domain_decision",
                "parent_gateway_no_dispatch_decision",
                "local_probe",
                "implementation",
                "direct_Claude_send",
            ),
        )
        self.assertEqual(boundary.handoff_target_role, ROLE_PROJECT_GATEWAY)

    def test_project_domain_decision_crosses_the_boundary_correctly(self) -> None:
        # The #12698 defect: the grandparent must NOT make the project-domain /
        # no_dispatch decision; the project gateway OWNS it. Encode that invariant.
        grandparent = resolve_transition_role(ROLE_GRANDPARENT_COORDINATOR)
        gateway = resolve_transition_role(ROLE_PROJECT_GATEWAY)
        for decision in PROJECT_DOMAIN_DECISIONS:
            self.assertIn(decision, grandparent.forbidden_actions)
            self.assertNotIn(decision, grandparent.allowed_actions)
            self.assertIn(decision, gateway.allowed_actions)
            self.assertNotIn(decision, gateway.forbidden_actions)

    def test_project_gateway_must_return_via_callback_primitive(self) -> None:
        # #12737: the gateway returns its consultation result through the product
        # callback primitive (ticketless-callback / q-enter consultation_callback)
        # and must not treat a local pane answer as the callback. Encode both.
        gateway = resolve_transition_role(ROLE_PROJECT_GATEWAY)
        self.assertIn(GATEWAY_CALLBACK_PRIMITIVE_RETURN, gateway.allowed_actions)
        self.assertNotIn(GATEWAY_CALLBACK_PRIMITIVE_RETURN, gateway.forbidden_actions)
        self.assertIn(GATEWAY_LOCAL_ANSWER_NOT_CALLBACK, gateway.forbidden_actions)
        self.assertNotIn(GATEWAY_LOCAL_ANSWER_NOT_CALLBACK, gateway.allowed_actions)

    def test_local_answer_not_callback_is_gateway_only(self) -> None:
        # The local-answer-not-callback prohibition belongs to the gateway (the
        # lane that produces the consultation result), not the grandparent caller.
        grandparent = resolve_transition_role(ROLE_GRANDPARENT_COORDINATOR)
        self.assertNotIn(GATEWAY_LOCAL_ANSWER_NOT_CALLBACK, grandparent.forbidden_actions)
        self.assertNotIn(GATEWAY_CALLBACK_PRIMITIVE_RETURN, grandparent.allowed_actions)

    def test_worker_dispatch_anchor_action_is_unchanged(self) -> None:
        # #12737 must not relax worker dispatch: the anchored worker-dispatch action
        # stays an allowed gateway action and is not folded into the ticketless path.
        gateway = resolve_transition_role(ROLE_PROJECT_GATEWAY)
        self.assertIn("dispatch_redmine_anchored_worker", gateway.allowed_actions)

    def test_builtin_boundaries_keep_allowed_and_forbidden_disjoint(self) -> None:
        for boundary in TRANSITION_ROLE_BOUNDARIES.values():
            self.assertFalse(
                set(boundary.allowed_actions) & set(boundary.forbidden_actions)
            )


class TransitionRoleBoundaryValidationTest(unittest.TestCase):
    def test_blank_current_role_fails_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            TransitionRoleBoundary(
                current_role="  ",
                allowed_actions=("a",),
                forbidden_actions=("b",),
                handoff_target_role="t",
            )

    def test_empty_allowed_actions_fails_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            TransitionRoleBoundary(
                current_role="r",
                allowed_actions=(),
                forbidden_actions=("b",),
                handoff_target_role="t",
            )

    def test_blank_action_token_fails_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            TransitionRoleBoundary(
                current_role="r",
                allowed_actions=("ok", "  "),
                forbidden_actions=("b",),
                handoff_target_role="t",
            )

    def test_blank_handoff_target_fails_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            TransitionRoleBoundary(
                current_role="r",
                allowed_actions=("a",),
                forbidden_actions=("b",),
                handoff_target_role="",
            )

    def test_allowed_forbidden_overlap_fails_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            TransitionRoleBoundary(
                current_role="r",
                allowed_actions=("shared",),
                forbidden_actions=("shared",),
                handoff_target_role="t",
            )

    def test_duplicate_action_tokens_are_deduped_in_order(self) -> None:
        boundary = TransitionRoleBoundary(
            current_role="r",
            allowed_actions=("a", "b", "a"),
            forbidden_actions=("c",),
            handoff_target_role="t",
        )
        self.assertEqual(boundary.allowed_actions, ("a", "b"))


class TransitionRolePayloadTest(unittest.TestCase):
    def test_structured_dict_round_trips(self) -> None:
        for token in TRANSITION_ROLE_TOKENS:
            boundary = resolve_transition_role(token)
            self.assertEqual(
                transition_role_from_payload(boundary.to_structured_dict()), boundary
            )

    def test_structured_dict_is_free_text_free_tokens(self) -> None:
        payload = resolve_transition_role(ROLE_GRANDPARENT_COORDINATOR).to_structured_dict()
        self.assertEqual(
            set(payload),
            {
                "current_role",
                "allowed_actions",
                "forbidden_actions",
                "handoff_target_role",
            },
        )
        self.assertIsInstance(payload["allowed_actions"], list)

    def test_payload_missing_field_fails_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            transition_role_from_payload(
                {
                    "current_role": "r",
                    "allowed_actions": ["a"],
                    "forbidden_actions": ["b"],
                    # handoff_target_role missing
                }
            )

    def test_payload_non_sequence_actions_fail_closed(self) -> None:
        with self.assertRaises(TransitionRoleError):
            transition_role_from_payload(
                {
                    "current_role": "r",
                    "allowed_actions": "not-a-list",
                    "forbidden_actions": ["b"],
                    "handoff_target_role": "t",
                }
            )

    def test_pointer_clause_is_single_line(self) -> None:
        for token in TRANSITION_ROLE_TOKENS:
            clause = resolve_transition_role(token).pointer_clause()
            self.assertNotIn("\n", clause)
            self.assertIn(token, clause)


class TransitionRoleHandoffExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor = RedmineAnchor(issue="12706", journal="67045")
        self.boundary = resolve_transition_role(ROLE_GRANDPARENT_COORDINATOR)

    def test_notification_body_appends_single_line_pointer(self) -> None:
        body = build_notification_body(
            self.anchor,
            "design_consultation",
            "consult gateway",
            "codex",
            transition_role=self.boundary,
        )
        self.assertIn(self.boundary.pointer_clause(), body)
        # The body is delivered via one `send-keys -l`; it must stay single-line.
        self.assertNotIn("\n", body)

    def test_notification_body_without_boundary_is_unchanged(self) -> None:
        without = build_notification_body(
            self.anchor, "design_consultation", "consult gateway", "codex"
        )
        self.assertNotIn("transition role:", without)

    def test_make_outcome_carries_structured_boundary(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%71",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
            transition_role=self.boundary,
        )
        self.assertEqual(outcome.transition_role, self.boundary.to_structured_dict())
        self.assertIn("transition_role", outcome.to_json())

    def test_make_outcome_without_boundary_is_none(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%71",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
        )
        self.assertIsNone(outcome.transition_role)

    def test_delivery_record_renders_full_action_boundary(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%71",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
            transition_role=self.boundary,
        )
        record = build_delivery_record(outcome)
        self.assertIn(
            "- Transition role: `grandparent_coordinator` — handoff target: "
            "`project_gateway`",
            record,
        )
        # Full allowed/forbidden tokens render in the durable record (fixed tokens,
        # no operator free text), so the receiver reads the boundary it must obey.
        for action in self.boundary.allowed_actions:
            self.assertIn(action, record)
        for action in self.boundary.forbidden_actions:
            self.assertIn(action, record)

    def test_delivery_record_dash_when_no_boundary(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%71",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
        )
        self.assertIn("- Transition role: —", build_delivery_record(outcome))


if __name__ == "__main__":
    unittest.main()
