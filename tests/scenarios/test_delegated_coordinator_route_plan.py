"""Delegated-coordinator route-plan acceptance scenarios (Redmine #12491).

Parent Feature #12531 ``120_シナリオ・受入テスト基盤``. These classical
(Detroit-school) scenarios drive the **real** pure planner
(:func:`~mozyo_bridge.domain.delegated_coordinator_route_plan.plan_delegated_coordinator_route`)
end to end — composing the real grandchild dispatch decision, launch/adopt
selector, realization gate, read-boundary classifier, and role-profile chain —
and fake only the side-effecting boundary (the executor in
``support.delegation_route_fakes``). They encode the #12491 acceptance:

- parent -> delegated coordinator -> grandchild gateway/worker use-case
  (``FullDelegatedRouteScenarioTest`` / ``FakeExecutorIntegrationTest``);
- same-lane worker fallback detected as ``blocked``
  (``SameLaneWorkerFallbackScenarioTest``);
- context contamination / insufficient read stopped *before* the route decision
  (``ReadBoundaryGateScenarioTest``);
- the ``delegated_coordinator`` / ``implementation_gateway`` /
  ``implementation_worker`` role-profile chain verified end to end
  (``RoleProfileChainScenarioTest``).

This module is cross-cutting (it spans several bounded contexts), so per the
tests-placement policy it lives in ``tests/scenarios/`` and is not subdivided by
bounded context.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the repo-local ``src`` and the ``tests`` package root importable for
# isolated / single-file discovery (harmless under full discover).
_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.domain.delegated_coordinator_route_plan import (  # noqa: E402
    DEFAULT_ROLE_PROFILE_CHAIN,
    PLAN_BLOCKED,
    PLAN_PROCEED,
    STEP_BLOCKED,
    STEP_DISPATCH_DECISION,
    STEP_LAUNCH_OR_ADOPT,
    STEP_READ_BOUNDARY,
    STEP_REALIZATION_GATE,
    STEP_SEND_SAME_LANE_WORKER,
    STEP_SEND_TO_GRANDCHILD_GATEWAY,
    STEP_STAMP,
    RoutePlanError,
    plan_delegated_coordinator_route,
)
from mozyo_bridge.domain.delegation_launch_adopt import (  # noqa: E402
    LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
    REASON_AMBIGUOUS_CANDIDATES,
)
from mozyo_bridge.domain.grandchild_dispatch import (  # noqa: E402
    REASON_MASTER_GATE_DISABLED,
    DelegationPolicy,
)
from mozyo_bridge.domain.grandchild_stamp import GATE_BLOCKED, GATE_REALIZED  # noqa: E402
from mozyo_bridge.domain.role_profile import (  # noqa: E402
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
)

from support.delegation_route_fakes import (  # noqa: E402
    DURABLE_ANCHOR,
    FakeDelegationExecutor,
    allowed_read,
    base_request,
    contaminated_read,
    gateway_candidate,
    insufficient_read,
)


class ReadBoundaryGateScenarioTest(unittest.TestCase):
    """A non-allowed read stops the route BEFORE any dispatch decision."""

    def test_allowed_read_reaches_the_route_decision(self) -> None:
        plan = plan_delegated_coordinator_route(base_request())
        self.assertEqual(STEP_READ_BOUNDARY, plan.steps[0])
        self.assertIn(STEP_DISPATCH_DECISION, plan.steps)
        self.assertIsNotNone(plan.dispatch_decision)

    def test_contaminated_read_blocks_before_route_decision(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(read_boundary=contaminated_read())
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(STEP_BLOCKED, plan.terminal_step)
        # The route decision was never computed.
        self.assertIsNone(plan.dispatch_decision)
        self.assertNotIn(STEP_DISPATCH_DECISION, plan.steps)
        self.assertEqual("read_boundary_contaminated", plan.blocked_reason)

    def test_insufficient_read_blocks_before_route_decision(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(read_boundary=insufficient_read())
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertIsNone(plan.dispatch_decision)
        self.assertEqual("read_boundary_insufficient", plan.blocked_reason)
        # The read-boundary gate is the only step taken before blocking.
        self.assertEqual((STEP_READ_BOUNDARY, STEP_BLOCKED), plan.steps)


class FullDelegatedRouteScenarioTest(unittest.TestCase):
    """parent -> delegated coordinator -> grandchild gateway/worker, realized."""

    def test_realized_grandchild_proceeds_to_gateway_in_order(self) -> None:
        plan = plan_delegated_coordinator_route(base_request())
        self.assertEqual(PLAN_PROCEED, plan.verdict)
        self.assertTrue(plan.grandchild_required)
        self.assertEqual(STEP_SEND_TO_GRANDCHILD_GATEWAY, plan.terminal_step)
        self.assertEqual(GATE_REALIZED, plan.realization_gate.verdict)
        # The full ordered plan the #12474 j#64217 contract requires.
        self.assertEqual(
            (
                STEP_READ_BOUNDARY,
                STEP_DISPATCH_DECISION,
                STEP_LAUNCH_OR_ADOPT,
                STEP_STAMP,
                STEP_REALIZATION_GATE,
                STEP_SEND_TO_GRANDCHILD_GATEWAY,
            ),
            plan.steps,
        )

    def test_dispatch_lands_at_codex_gateway_never_claude(self) -> None:
        plan = plan_delegated_coordinator_route(base_request())
        selected = plan.dispatch_decision.selected
        self.assertIsNotNone(selected)
        self.assertEqual("codex", selected.role)


class SameLaneWorkerFallbackScenarioTest(unittest.TestCase):
    """A required grandchild that is not realized must block, never PASS."""

    def test_same_lane_fallback_is_blocked(self) -> None:
        # Grandchild dispatch is required (adopt), but no realized depth-2 lane
        # exists: the same-lane worker fallback must be blocked.
        plan = plan_delegated_coordinator_route(base_request(realized_units=[]))
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertTrue(plan.grandchild_required)
        self.assertEqual(GATE_BLOCKED, plan.realization_gate.verdict)
        self.assertTrue(plan.blocked_reason.startswith("grandchild_required_but_not_realized"))
        # The realization gate ran, but no gateway send step was planned.
        self.assertIn(STEP_REALIZATION_GATE, plan.steps)
        self.assertNotIn(STEP_SEND_TO_GRANDCHILD_GATEWAY, plan.steps)
        self.assertNotIn(STEP_SEND_SAME_LANE_WORKER, plan.steps)

    def test_wrong_parent_realized_lane_does_not_satisfy(self) -> None:
        # A depth-2 implementation lane realized under a DIFFERENT delegated
        # coordinator is not this route's grandchild — still blocked.
        plan = plan_delegated_coordinator_route(
            base_request(
                realized_units=[
                    ("ws-child-project/lane-other", "implementation", 2, "ws-other/lane-x", "derived")
                ]
            )
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(GATE_BLOCKED, plan.realization_gate.verdict)


class NoDispatchSameLaneScenarioTest(unittest.TestCase):
    """When no grandchild is required, the same-lane worker is legitimate."""

    def test_no_dispatch_proceeds_to_same_lane_worker(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(no_dispatch_reason="context_cost_low")
        )
        self.assertEqual(PLAN_PROCEED, plan.verdict)
        self.assertFalse(plan.grandchild_required)
        self.assertEqual(STEP_SEND_SAME_LANE_WORKER, plan.terminal_step)
        self.assertTrue(plan.dispatch_decision.is_no_dispatch)
        self.assertNotIn(STEP_SEND_TO_GRANDCHILD_GATEWAY, plan.steps)


class DispatchFailClosedScenarioTest(unittest.TestCase):
    """A policy / selection fail-closed yields a blocked plan, not a crash."""

    def test_ambiguous_gateway_candidates_block(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(
                launch_adopt_mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
                candidates=[
                    gateway_candidate(pane_id="%21", lane_id="lane-a"),
                    gateway_candidate(pane_id="%22", lane_id="lane-b"),
                ],
            )
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(REASON_AMBIGUOUS_CANDIDATES, plan.blocked_reason)
        # The dispatch decision WAS computed (fail-closed), unlike a read-boundary block.
        self.assertIsNotNone(plan.dispatch_decision)
        self.assertTrue(plan.dispatch_decision.is_fail_closed)

    def test_master_gate_disabled_blocks_with_policy_reason(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(policy=DelegationPolicy(enable_delegated_coordinator=False))
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(REASON_MASTER_GATE_DISABLED, plan.blocked_reason)


class RoleProfileChainScenarioTest(unittest.TestCase):
    """The fixed three-hop role-profile chain is verified end to end."""

    def test_chain_is_complete_and_ordered(self) -> None:
        plan = plan_delegated_coordinator_route(base_request())
        self.assertEqual(
            (ROLE_DELEGATED_COORDINATOR, ROLE_IMPLEMENTATION_GATEWAY, ROLE_IMPLEMENTATION_WORKER),
            plan.role_profile_chain,
        )
        self.assertEqual(
            DEFAULT_ROLE_PROFILE_CHAIN,
            tuple(r.role_profile for r in plan.role_profile_resolutions),
        )

    def test_each_hop_binds_its_role_profile(self) -> None:
        plan = plan_delegated_coordinator_route(base_request())
        self.assertEqual(
            ROLE_DELEGATED_COORDINATOR, plan.role_profile_for_hop("parent_to_child").role_profile
        )
        self.assertEqual(
            ROLE_IMPLEMENTATION_GATEWAY, plan.role_profile_for_hop("child_to_gateway").role_profile
        )
        self.assertEqual(
            ROLE_IMPLEMENTATION_WORKER, plan.role_profile_for_hop("gateway_to_worker").role_profile
        )

    def test_durable_anchor_is_injected_into_each_hop(self) -> None:
        # The chain resolves the <durable_anchor> placeholder from the request, so
        # no hop is left with an unresolved anchor (the receiver always gets it).
        plan = plan_delegated_coordinator_route(base_request())
        for resolution in plan.role_profile_resolutions:
            self.assertNotIn("durable_anchor", resolution.unresolved_placeholders)
        gateway = plan.role_profile_for_hop("child_to_gateway")
        self.assertIn(DURABLE_ANCHOR, gateway.resolved_text)

    def test_incomplete_chain_is_invalid(self) -> None:
        # Role profile omitted -> plan invalid (a caller error, fail closed).
        with self.assertRaises(RoutePlanError):
            plan_delegated_coordinator_route(
                base_request(role_profile_chain=(ROLE_DELEGATED_COORDINATOR, ROLE_IMPLEMENTATION_GATEWAY))
            )

    def test_missing_durable_anchor_is_invalid(self) -> None:
        with self.assertRaises(RoutePlanError):
            plan_delegated_coordinator_route(base_request(durable_anchor="  "))


class FakeExecutorIntegrationTest(unittest.TestCase):
    """The fake executor proves the side-effect contract: blocked -> no send."""

    def test_realized_route_stamps_then_sends_to_gateway(self) -> None:
        plan = plan_delegated_coordinator_route(base_request())
        trace = FakeDelegationExecutor().execute(plan)
        # Stamp happens, then exactly one gateway send under the gateway profile.
        self.assertEqual(1, len(trace.stamp_commands))
        self.assertEqual(1, len(trace.pane_sends))
        send = trace.pane_sends[0]
        self.assertEqual("grandchild_codex_gateway", send["target_kind"])
        self.assertEqual(ROLE_IMPLEMENTATION_GATEWAY, send["role_profile"])
        self.assertIn("route_plan", trace.recorded_kinds)
        self.assertNotIn("blocked", trace.recorded_kinds)

    def test_blocked_route_records_blocked_and_performs_no_send(self) -> None:
        plan = plan_delegated_coordinator_route(base_request(realized_units=[]))
        trace = FakeDelegationExecutor().execute(plan)
        self.assertIn("blocked", trace.recorded_kinds)
        self.assertFalse(trace.sent)
        self.assertEqual([], trace.stamp_commands)

    def test_contaminated_route_performs_no_send(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(read_boundary=contaminated_read())
        )
        trace = FakeDelegationExecutor().execute(plan)
        self.assertIn("blocked", trace.recorded_kinds)
        self.assertFalse(trace.sent)

    def test_same_lane_route_sends_to_worker_without_stamp(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(no_dispatch_reason="single_pass_no_iteration")
        )
        trace = FakeDelegationExecutor().execute(plan)
        self.assertEqual([], trace.stamp_commands)
        self.assertEqual(1, len(trace.pane_sends))
        send = trace.pane_sends[0]
        self.assertEqual("same_lane_worker", send["target_kind"])
        self.assertEqual(ROLE_IMPLEMENTATION_WORKER, send["role_profile"])


if __name__ == "__main__":
    unittest.main()
