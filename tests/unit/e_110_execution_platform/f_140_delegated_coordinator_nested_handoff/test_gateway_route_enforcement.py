"""Unit tests for the #12918 gateway-route enforcement decision (pure).

The governed route is coordinator Codex -> sublane Codex gateway -> same-lane
Claude worker. These tests pin the pure ``decide_gateway_route`` policy that makes
the recorded failure mode — a coordinator dispatching an ``implementation_request``
/ ``review_result`` *directly* to a cross-lane sublane Claude worker (#12670
j#68733) — fail closed, while leaving every legitimate route untouched:

- only ``implementation_request`` / ``review_result`` are governed; every other
  handoff kind (design consultation / review_request / reply / implementation_done
  / custom) is always allowed, so main-lane Claude read-only / design / summary
  uses are never blocked;
- a governed kind to the Codex gateway (``receiver=codex``) is the governed route
  head and is allowed;
- a governed kind to a Claude worker is allowed only when it is the same-lane
  ``gateway -> worker`` terminal hop, or when the sender's own lane Unit could not
  be resolved (the gate cannot prove a cross-lane bypass and stays out of the way);
- a governed kind to a *cross-lane* Claude worker fails closed with the
  ``coordinator_to_sublane_worker_bypass`` reason and a suggested safe route,
  unless an explicit durable exception releases it as a distinct
  ``gateway_route_exception``.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_route_enforcement import (  # noqa: E402
    BLOCKED_DIRECT_WORKER_BYPASS,
    GATEWAY_GOVERNED_KINDS,
    ROUTE_ALLOWED,
    ROUTE_BLOCKED,
    ROUTE_EXCEPTION,
    GatewayRouteDecision,
    GatewayRouteRequest,
    decide_gateway_route,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E402
    KIND_LABELS,
)


def _decide(**kwargs) -> GatewayRouteDecision:
    return decide_gateway_route(GatewayRouteRequest(**kwargs))


class GovernedKindVocabularyTest(unittest.TestCase):
    def test_governed_kinds_are_exactly_impl_request_and_review_result(self) -> None:
        self.assertEqual(
            {"implementation_request", "review_result"}, set(GATEWAY_GOVERNED_KINDS)
        )

    def test_governed_kinds_are_real_handoff_kinds(self) -> None:
        # Defends against a future rename silently emptying the governed set.
        self.assertTrue(GATEWAY_GOVERNED_KINDS <= KIND_LABELS)


class FailureModeBlockTest(unittest.TestCase):
    """The exact previous failure: coordinator -> cross-lane sublane Claude worker."""

    def _bypass(self, kind: str) -> GatewayRouteDecision:
        return _decide(
            kind=kind,
            receiver="claude",
            sender_identity_known=True,
            sender_workspace_id="ws-a",
            sender_lane_id="lane-coordinator",
            target_workspace_id="ws-a",
            target_lane_id="lane-sub-12642",
            target_role="claude",
        )

    def test_implementation_request_direct_to_cross_lane_worker_blocks(self) -> None:
        decision = self._bypass("implementation_request")
        self.assertTrue(decision.is_blocked)
        self.assertEqual(ROUTE_BLOCKED, decision.verdict)
        self.assertEqual(BLOCKED_DIRECT_WORKER_BYPASS, decision.blocked_reason)
        self.assertFalse(decision.same_unit)
        self.assertFalse(decision.exception_applied)
        self.assertTrue(decision.governed)

    def test_review_result_direct_to_cross_lane_worker_blocks(self) -> None:
        decision = self._bypass("review_result")
        self.assertTrue(decision.is_blocked)
        self.assertEqual(BLOCKED_DIRECT_WORKER_BYPASS, decision.blocked_reason)

    def test_blocked_decision_carries_public_safe_suggested_route(self) -> None:
        decision = self._bypass("implementation_request")
        route = decision.suggested_safe_route or ""
        # Names the governed gateway hop and the (public-safe) lane, never a pane id.
        self.assertIn("Codex gateway", route)
        self.assertIn("lane-sub-12642", route)
        self.assertNotIn("%", route)

    def test_coordinator_with_no_lane_option_still_blocks_cross_lane_worker(self) -> None:
        # The coordinator pane carries no @mozyo_lane_id (normalizes to `default`);
        # the sublane worker carries a real lane. Still a cross-lane bypass.
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=True,
            sender_lane_id=None,
            target_lane_id="lane-sub-12669",
            target_role="claude",
        )
        self.assertTrue(decision.is_blocked)


class AllowedRouteTest(unittest.TestCase):
    def test_governed_kind_to_codex_gateway_is_allowed(self) -> None:
        # coordinator -> sublane Codex gateway is the governed route head.
        decision = _decide(
            kind="implementation_request",
            receiver="codex",
            sender_identity_known=True,
            sender_lane_id="lane-coordinator",
            target_lane_id="lane-sub-12642",
            target_role="codex",
        )
        self.assertEqual(ROUTE_ALLOWED, decision.verdict)
        self.assertTrue(decision.governed)

    def test_same_lane_gateway_to_worker_is_allowed(self) -> None:
        # The gateway hands off to its OWN same-lane worker (same Unit): allowed.
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=True,
            sender_workspace_id="ws-a",
            sender_lane_id="lane-sub-12642",
            target_workspace_id="ws-a",
            target_lane_id="lane-sub-12642",
            target_role="claude",
        )
        self.assertEqual(ROUTE_ALLOWED, decision.verdict)
        self.assertTrue(decision.same_unit)

    def test_default_lane_both_sides_reads_as_same_lane(self) -> None:
        # A non-cockpit gateway -> worker dispatch: neither pane carries a lane
        # option, both normalize to `default`, so it is NOT a bypass.
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=True,
            sender_lane_id=None,
            target_lane_id=None,
            target_role="claude",
        )
        self.assertEqual(ROUTE_ALLOWED, decision.verdict)
        self.assertTrue(decision.same_unit)

    def test_unknown_sender_identity_does_not_block(self) -> None:
        # Run outside tmux / from a pane the inventory does not carry: the gate
        # cannot prove a cross-lane bypass, mirroring the cross-session gate's
        # skip-when-sender-session-unknown posture.
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=False,
            target_lane_id="lane-sub-12642",
            target_role="claude",
        )
        self.assertEqual(ROUTE_ALLOWED, decision.verdict)
        self.assertIsNone(decision.same_unit)

    def test_cross_workspace_same_lane_token_is_not_same_unit(self) -> None:
        # Two different workspaces that coincidentally share the lane token are not
        # one Unit; a governed cross-workspace worker send is not treated same-lane.
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=True,
            sender_workspace_id="ws-a",
            sender_lane_id="default",
            target_workspace_id="ws-b",
            target_lane_id="default",
            target_role="claude",
        )
        self.assertTrue(decision.is_blocked)


class NonGovernedKindTest(unittest.TestCase):
    def test_non_governed_kinds_to_cross_lane_claude_are_allowed(self) -> None:
        # design_consultation / review_request / reply / implementation_done /
        # custom are never gated, so main-lane Claude read-only / design / summary
        # uses keep working even across lanes.
        for kind in ("design_consultation", "review_request", "reply", "implementation_done", "custom"):
            with self.subTest(kind=kind):
                decision = _decide(
                    kind=kind,
                    receiver="claude",
                    sender_identity_known=True,
                    sender_lane_id="lane-coordinator",
                    target_lane_id="lane-sub-12642",
                    target_role="claude",
                )
                self.assertEqual(ROUTE_ALLOWED, decision.verdict)
                self.assertFalse(decision.governed)


class ExplicitExceptionTest(unittest.TestCase):
    def test_allow_direct_worker_releases_block_as_distinct_exception(self) -> None:
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=True,
            sender_lane_id="lane-coordinator",
            target_lane_id="lane-sub-12642",
            target_role="claude",
            allow_direct_worker=True,
        )
        self.assertEqual(ROUTE_EXCEPTION, decision.verdict)
        self.assertTrue(decision.is_exception)
        self.assertTrue(decision.exception_applied)
        self.assertFalse(decision.is_blocked)
        # The exception is NOT recorded as a clean normal route: it still carries
        # the suggested safe route so the bypass remains auditable.
        self.assertIsNotNone(decision.suggested_safe_route)

    def test_exception_flag_is_inert_on_an_already_allowed_route(self) -> None:
        # A same-lane delivery is allowed regardless of the exception flag; it does
        # not get mislabeled as an exception.
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=True,
            sender_lane_id="lane-sub-12642",
            target_lane_id="lane-sub-12642",
            target_role="claude",
            allow_direct_worker=True,
        )
        self.assertEqual(ROUTE_ALLOWED, decision.verdict)
        self.assertFalse(decision.exception_applied)


class DecisionShapeTest(unittest.TestCase):
    def test_to_dict_is_record_safe_and_complete(self) -> None:
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            sender_identity_known=True,
            sender_lane_id="lane-coordinator",
            target_lane_id="lane-sub-12642",
            target_role="claude",
        )
        payload = decision.to_dict()
        self.assertEqual(
            {
                "verdict",
                "governed",
                "kind",
                "resolved_receiver",
                "blocked_reason",
                "suggested_safe_route",
                "same_unit",
                "exception_applied",
            },
            set(payload),
        )
        # No pane id leaks into the structured record.
        self.assertNotIn("%", payload["suggested_safe_route"] or "")


class WorkerProviderRebindTest(unittest.TestCase):
    """Role-based worker discrimination (Redmine #13174).

    The authority decision — worker (terminal same-lane hop) vs gateway (route head)
    — keys on the implementer (worker) *role*'s runtime provider, which the caller
    resolves from the binding. ``worker_provider`` unset falls back to ``claude`` so
    the default binding is byte-identical; a rebind moves which receiver is treated as
    the worker.
    """

    def test_unset_worker_provider_is_byte_identical_to_claude(self) -> None:
        base = dict(
            kind="implementation_request",
            sender_identity_known=True,
            sender_workspace_id="ws-a",
            sender_lane_id="lane-coordinator",
            target_workspace_id="ws-a",
            target_lane_id="lane-sub-12642",
        )
        # Default: receiver=claude is the worker -> cross-lane bypass blocks.
        self.assertTrue(_decide(receiver="claude", **base).is_blocked)
        self.assertTrue(
            _decide(receiver="claude", worker_provider="claude", **base).is_blocked
        )
        # Explicit worker_provider="claude" matches the unset fallback exactly.
        self.assertEqual(
            _decide(receiver="claude", **base).to_dict(),
            _decide(receiver="claude", worker_provider="claude", **base).to_dict(),
        )

    def test_rebound_worker_provider_blocks_that_receiver_cross_lane(self) -> None:
        # implementer rebound to codex: a governed kind `--to codex` to a cross-lane
        # pane is now the worker bypass and fails closed.
        decision = _decide(
            kind="implementation_request",
            receiver="codex",
            worker_provider="codex",
            sender_identity_known=True,
            sender_lane_id="lane-coordinator",
            target_lane_id="lane-sub-12642",
            target_role="codex",
        )
        self.assertTrue(decision.is_blocked)
        self.assertEqual(BLOCKED_DIRECT_WORKER_BYPASS, decision.blocked_reason)

    def test_rebound_leaves_other_provider_as_gateway_head(self) -> None:
        # With the worker bound to codex, `--to claude` is now the non-worker
        # (gateway/coordinator) route head and is always allowed.
        decision = _decide(
            kind="implementation_request",
            receiver="claude",
            worker_provider="codex",
            sender_identity_known=True,
            sender_lane_id="lane-coordinator",
            target_lane_id="lane-sub-12642",
            target_role="claude",
        )
        self.assertEqual(ROUTE_ALLOWED, decision.verdict)
        self.assertTrue(decision.governed)

    def test_rebound_worker_same_lane_terminal_hop_allowed(self) -> None:
        # The same-lane gateway -> worker terminal hop is still allowed under a rebind.
        decision = _decide(
            kind="implementation_request",
            receiver="codex",
            worker_provider="codex",
            sender_identity_known=True,
            sender_workspace_id="ws-a",
            sender_lane_id="lane-sub-12642",
            target_workspace_id="ws-a",
            target_lane_id="lane-sub-12642",
            target_role="codex",
        )
        self.assertEqual(ROUTE_ALLOWED, decision.verdict)
        self.assertTrue(decision.same_unit)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
