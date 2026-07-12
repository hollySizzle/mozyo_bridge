"""Pure herdr forward-route matrix tests (Redmine #13583 Increment 3).

Pins the pure one-step forward plan for the two resolved coordinator roles (grandparent ->
consultation, project_gateway -> child work-intake; every other role has no forward), the
direction-specific primitive / reason tokens (the two legs are never conflated), and the pure
send / zero-send decision over the resolved target status + durable fence state (only an ``ok``
target on an ``open`` fence sends; every other combination is a fixed zero-send reason).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (
    FENCE_HELD,
    FENCE_OPEN,
    FENCE_UNAVAILABLE,
    FORWARD_GATEWAY_TO_CHILD,
    FORWARD_GRANDPARENT_TO_GATEWAY,
    PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE,
    PRIMITIVE_HERDR_FORWARD_CONSULT,
    REASON_HERDR_FORWARD_CHILD_INTAKE_READY,
    REASON_HERDR_FORWARD_CONSULT_READY,
    REASON_HERDR_FORWARD_DUPLICATE,
    REASON_HERDR_FORWARD_FENCE_UNAVAILABLE,
    REASON_HERDR_FORWARD_SELF_ROUTE,
    REASON_HERDR_FORWARD_TARGET_AMBIGUOUS,
    REASON_HERDR_FORWARD_TARGET_LOCATOR_MISSING,
    REASON_HERDR_FORWARD_TARGET_MISSING,
    SELECT_CHILD_WITH_SELF_FENCE,
    SELECT_SINGLE_LIVE_GATEWAY,
    SEND,
    TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING,
    TARGET_MISSING,
    TARGET_OK,
    TARGET_SELF,
    TICKETLESS_CONSULTATION,
    TICKETLESS_WORK_INTAKE,
    ZERO_SEND,
    decide_forward_send,
    plan_forward_route,
)


class PlanForwardRouteTest(unittest.TestCase):
    def test_grandparent_plans_consultation_to_single_live_gateway(self):
        plan = plan_forward_route(ROLE_GRANDPARENT_COORDINATOR)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, FORWARD_GRANDPARENT_TO_GATEWAY)
        self.assertEqual(plan.from_role, ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(plan.to_role, ROLE_PROJECT_GATEWAY)
        self.assertEqual(plan.primitive, PRIMITIVE_HERDR_FORWARD_CONSULT)
        self.assertEqual(plan.ready_reason, REASON_HERDR_FORWARD_CONSULT_READY)
        self.assertEqual(plan.select_mode, SELECT_SINGLE_LIVE_GATEWAY)
        self.assertEqual(plan.ticketless_kind, TICKETLESS_CONSULTATION)
        self.assertEqual(plan.project_scope, "")  # target scope is the resolved gateway's, not here

    def test_project_gateway_plans_child_intake_with_self_fence(self):
        plan = plan_forward_route(ROLE_PROJECT_GATEWAY, "cloud-drive-management")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, FORWARD_GATEWAY_TO_CHILD)
        self.assertEqual(plan.from_role, ROLE_PROJECT_GATEWAY)
        self.assertEqual(plan.to_role, ROLE_DELEGATED_COORDINATOR)
        self.assertEqual(plan.primitive, PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE)
        self.assertEqual(plan.ready_reason, REASON_HERDR_FORWARD_CHILD_INTAKE_READY)
        self.assertEqual(plan.select_mode, SELECT_CHILD_WITH_SELF_FENCE)
        self.assertEqual(plan.ticketless_kind, TICKETLESS_WORK_INTAKE)
        self.assertEqual(plan.project_scope, "cloud-drive-management")

    def test_two_legs_have_distinct_primitives_and_reasons(self):
        gp = plan_forward_route(ROLE_GRANDPARENT_COORDINATOR)
        pg = plan_forward_route(ROLE_PROJECT_GATEWAY, "x")
        self.assertNotEqual(gp.primitive, pg.primitive)
        self.assertNotEqual(gp.ready_reason, pg.ready_reason)
        self.assertNotEqual(gp.direction, pg.direction)

    def test_non_coordinator_roles_have_no_forward(self):
        self.assertIsNone(plan_forward_route("implementation_worker"))
        self.assertIsNone(plan_forward_route("delegated_coordinator"))
        self.assertIsNone(plan_forward_route(""))
        self.assertIsNone(plan_forward_route("  "))

    def test_role_token_is_trimmed(self):
        self.assertIsNotNone(plan_forward_route("  grandparent_coordinator  "))


class DecideForwardSendTest(unittest.TestCase):
    def test_ok_target_open_fence_is_the_only_send(self):
        d = decide_forward_send(TARGET_OK, FENCE_OPEN)
        self.assertEqual(d.decision, SEND)
        self.assertTrue(d.sends)
        self.assertEqual(d.reason, "")

    def test_ok_target_held_fence_is_duplicate_zero_send(self):
        d = decide_forward_send(TARGET_OK, FENCE_HELD)
        self.assertEqual(d.decision, ZERO_SEND)
        self.assertFalse(d.sends)
        self.assertEqual(d.reason, REASON_HERDR_FORWARD_DUPLICATE)

    def test_ok_target_unavailable_fence_fails_closed(self):
        d = decide_forward_send(TARGET_OK, FENCE_UNAVAILABLE)
        self.assertEqual(d.decision, ZERO_SEND)
        self.assertEqual(d.reason, REASON_HERDR_FORWARD_FENCE_UNAVAILABLE)

    def test_bad_target_never_sends_regardless_of_fence(self):
        cases = {
            TARGET_MISSING: REASON_HERDR_FORWARD_TARGET_MISSING,
            TARGET_AMBIGUOUS: REASON_HERDR_FORWARD_TARGET_AMBIGUOUS,
            TARGET_LOCATOR_MISSING: REASON_HERDR_FORWARD_TARGET_LOCATOR_MISSING,
            TARGET_SELF: REASON_HERDR_FORWARD_SELF_ROUTE,
        }
        for status, reason in cases.items():
            for fence in (FENCE_OPEN, FENCE_HELD, FENCE_UNAVAILABLE):
                d = decide_forward_send(status, fence)
                self.assertEqual(d.decision, ZERO_SEND, f"{status}/{fence}")
                self.assertEqual(d.reason, reason, f"{status}/{fence}")

    def test_unknown_fence_state_fails_closed(self):
        d = decide_forward_send(TARGET_OK, "garbage")
        self.assertEqual(d.decision, ZERO_SEND)
        self.assertEqual(d.reason, REASON_HERDR_FORWARD_FENCE_UNAVAILABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
