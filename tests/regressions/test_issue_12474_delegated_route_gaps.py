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

from mozyo_bridge.domain.delegated_coordinator_route_plan import (  # noqa: E402
    PLAN_BLOCKED,
    STEP_DISPATCH_DECISION,
    STEP_SEND_SAME_LANE_WORKER,
    STEP_SEND_TO_GRANDCHILD_GATEWAY,
    plan_delegated_coordinator_route,
)
from mozyo_bridge.domain.grandchild_stamp import GATE_BLOCKED  # noqa: E402

from support.delegation_route_fakes import (  # noqa: E402
    FakeDelegationExecutor,
    base_request,
    contaminated_read,
    insufficient_read,
)


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
