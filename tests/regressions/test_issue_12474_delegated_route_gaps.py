"""Regression pins for the #12460 / #12474 delegated-coordinator route gaps.

Redmine #12491 (parent Feature #12531). Each test pins a confirmed failure shape
the minimal-context smoke kept re-discovering, so it can never silently regress:

- the "#12460 failure shape" (#12473 j#64151 / #12474 j#64147 / j#64152): the
  delegated coordinator resolves a grandchild dispatch decision and then falls
  through to a **same-lane worker handoff**, leaving the grandchild unrealized and
  ``KIND``/``DEPTH``/``PARENT`` blank — a same-lane handoff alone must be
  ``blocked``, never display acceptance; and
- the contaminated / insufficient minimal-context read (#12474 j#64160 / j#64172
  / j#64185): a read that reaches parent journals / the management issue, or that
  never reads the target anchor, must not feed a route decision (no PASS/FAIL).

These are characterization pins, not new behavior; the planner / classifier are
defined in ``src/mozyo_bridge/domain``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_coordinator_route_plan import (  # noqa: E402
    PLAN_BLOCKED,
    PLAN_PROCEED,
    STEP_DISPATCH_DECISION,
    STEP_SEND_SAME_LANE_WORKER,
    STEP_SEND_TO_GRANDCHILD_GATEWAY,
    plan_delegated_coordinator_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (  # noqa: E402
    GATE_BLOCKED,
    GATE_REALIZED,
    GrandchildTargetIdentity,
)

from support.delegation_route_fakes import (  # noqa: E402
    DELEGATED_COORDINATOR_UNIT,
    GRANDCHILD_UNIT,
    CHILD_REPO_IDENTITY,
    FakeDelegationExecutor,
    base_request,
    contaminated_read,
    insufficient_read,
    realized_grandchild_rows,
)


def _stale_sibling_row():
    """A depth-2 implementation sibling under the SAME coordinator, different lane.

    Same coordinator parent / role / depth as the real grandchild, so the old
    "first depth-2 implementation lane" match could bind to it — a stale/unrelated
    false PASS. The exact-identity binding must ignore it (Redmine #13571).
    """
    return (
        "ws-child-project/lane-stale",
        "implementation",
        2,
        DELEGATED_COORDINATOR_UNIT,
        "derived",
        CHILD_REPO_IDENTITY,
    )


class StaleSiblingBindingRegressionTest(unittest.TestCase):
    """#13571 / #12454 j#75444 F1: bind the exact dispatch-selected grandchild.

    A stale/unrelated sibling under the same coordinator must never be treated as
    the realized grandchild, and the verdict must not depend on inventory order.
    """

    def test_only_stale_sibling_present_blocks(self) -> None:
        # ONLY a stale sibling is visible; the exact dispatch-selected grandchild
        # is not. The old first-match returned the sibling -> false realized. Now
        # the route blocks.
        plan = plan_delegated_coordinator_route(
            base_request(realized_units=[_stale_sibling_row()])
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(GATE_BLOCKED, plan.realization_gate.verdict)

    def test_stale_sibling_before_target_is_order_independent(self) -> None:
        # The exact target IS present, with a stale sibling ordered before it. The
        # gate binds to the exact target regardless of scan order.
        plan = plan_delegated_coordinator_route(
            base_request(
                realized_units=[_stale_sibling_row(), *realized_grandchild_rows()]
            )
        )
        self.assertEqual(PLAN_PROCEED, plan.verdict)
        self.assertEqual(GATE_REALIZED, plan.realization_gate.verdict)
        self.assertEqual(GRANDCHILD_UNIT, plan.realization_gate.realized_grandchild_unit)

    def test_stale_sibling_after_target_is_order_independent(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(
                realized_units=[*realized_grandchild_rows(), _stale_sibling_row()]
            )
        )
        self.assertEqual(PLAN_PROCEED, plan.verdict)
        self.assertEqual(GRANDCHILD_UNIT, plan.realization_gate.realized_grandchild_unit)


class DispatchSelectedAuthorityRegressionTest(unittest.TestCase):
    """#13571 j#75462 F1: an explicit target cannot override the adopt selection.

    For an adopt dispatch the selected candidate is authoritative; an explicit
    ``grandchild_target`` that names a DIFFERENT lane must fail closed rather than
    open the gate on an unrelated sibling's display evidence.
    """

    def test_explicit_target_disagreeing_with_selection_blocks(self) -> None:
        # Dispatch selects the real grandchild (lane-grandchild); an explicit
        # target names a different lane (lane-other) present in the inventory.
        other = GrandchildTargetIdentity(
            unit_id="ws-child-project/lane-other",
            delegation_parent=DELEGATED_COORDINATOR_UNIT,
            repo_identity=CHILD_REPO_IDENTITY,
        )
        plan = plan_delegated_coordinator_route(
            base_request(
                grandchild_target=other,
                realized_units=[
                    (
                        "ws-child-project/lane-other",
                        "implementation",
                        2,
                        DELEGATED_COORDINATOR_UNIT,
                        "derived",
                        CHILD_REPO_IDENTITY,
                    )
                ],
            )
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(GATE_BLOCKED, plan.realization_gate.verdict)

    def test_explicit_target_agreeing_with_selection_proceeds(self) -> None:
        # An explicit target that names the SAME lane the dispatch selected is
        # fine (it is the dispatch-selected identity).
        same = GrandchildTargetIdentity(
            unit_id=GRANDCHILD_UNIT,
            delegation_parent=DELEGATED_COORDINATOR_UNIT,
            repo_identity=CHILD_REPO_IDENTITY,
        )
        plan = plan_delegated_coordinator_route(
            base_request(grandchild_target=same, realized_units=realized_grandchild_rows())
        )
        self.assertEqual(PLAN_PROCEED, plan.verdict)
        self.assertEqual(GATE_REALIZED, plan.realization_gate.verdict)

    def test_launch_without_target_is_unbound_blocked(self) -> None:
        # A launch dispatch (no selectable candidate) with no explicit target
        # cannot bind an exact grandchild -> blocked, never same-lane acceptance.
        plan = plan_delegated_coordinator_route(
            base_request(candidates=[], realized_units=realized_grandchild_rows())
        )
        self.assertTrue(plan.dispatch_decision.is_launch)
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(GATE_BLOCKED, plan.realization_gate.verdict)

    def test_launch_with_authoritative_target_proceeds(self) -> None:
        # A launch dispatch WITH the runtime-supplied post-launch identity binds
        # to that exact lane and proceeds.
        created = GrandchildTargetIdentity(
            unit_id=GRANDCHILD_UNIT,
            delegation_parent=DELEGATED_COORDINATOR_UNIT,
            repo_identity=CHILD_REPO_IDENTITY,
        )
        plan = plan_delegated_coordinator_route(
            base_request(
                candidates=[],
                grandchild_target=created,
                realized_units=realized_grandchild_rows(),
            )
        )
        self.assertTrue(plan.dispatch_decision.is_launch)
        self.assertEqual(PLAN_PROCEED, plan.verdict)
        self.assertEqual(GATE_REALIZED, plan.realization_gate.verdict)


class SameLaneFallbackRegressionTest(unittest.TestCase):
    """#12460 / #12474 j#64147: same-lane worker fallback is blocked, not PASS."""

    def test_decision_plus_same_lane_only_is_blocked_not_acceptance(self) -> None:
        # The exact smoke shape: dispatch decision requires a grandchild, but the
        # runtime only has a same-lane worker (no realized depth-2 lane).
        plan = plan_delegated_coordinator_route(base_request(realized_units=[]))
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertEqual(GATE_BLOCKED, plan.realization_gate.verdict)
        self.assertTrue(
            plan.blocked_reason.startswith("grandchild_required_but_not_realized")
        )
        # No worker/gateway send was planned — the fallback never became a route.
        self.assertNotIn(STEP_SEND_TO_GRANDCHILD_GATEWAY, plan.steps)
        self.assertNotIn(STEP_SEND_SAME_LANE_WORKER, plan.steps)

    def test_blocked_fallback_executes_no_pane_send(self) -> None:
        plan = plan_delegated_coordinator_route(base_request(realized_units=[]))
        trace = FakeDelegationExecutor().execute(plan)
        self.assertFalse(trace.sent, "a blocked fallback must not perform a pane send")
        self.assertIn("blocked", trace.recorded_kinds)


class ContaminatedReadRegressionTest(unittest.TestCase):
    """#12474 j#64160 / j#64185: an out-of-bounds read does not form a route."""

    def test_contaminated_read_never_reaches_dispatch(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(read_boundary=contaminated_read())
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertNotIn(STEP_DISPATCH_DECISION, plan.steps)
        self.assertIsNone(plan.dispatch_decision)
        self.assertEqual("read_boundary_contaminated", plan.blocked_reason)

    def test_insufficient_read_never_reaches_dispatch(self) -> None:
        plan = plan_delegated_coordinator_route(
            base_request(read_boundary=insufficient_read())
        )
        self.assertEqual(PLAN_BLOCKED, plan.verdict)
        self.assertIsNone(plan.dispatch_decision)
        self.assertEqual("read_boundary_insufficient", plan.blocked_reason)


if __name__ == "__main__":
    unittest.main()
