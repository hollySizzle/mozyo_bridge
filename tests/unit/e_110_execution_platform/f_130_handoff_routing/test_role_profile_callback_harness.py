"""Synthetic scenario oracle for the sublane gateway/worker callback harness (Redmine #13745).

#13745 (parent #13490 ``人間・coordinator・gateway・worker の単一入口 E2E``) syncs the
fixed ``implementation_gateway`` / ``implementation_worker`` role-profile harness to
the durable-callback + duplicate-control contract that worked in #13569
(j#77346 R4 APPROVED, j#77347 self-route correction, j#77348 semantic upstream
callback). The role profile is the *agent behavior contract* (custom
instruction), not the runtime supervisor / outbox / correlation machinery, which
is owned by #13683 / #13684 and is explicitly NOT reimplemented here.

This module pins the acceptance-#4 scenarios as a pure, hermetic oracle over a
gateway callback-route decision, the same posture the delegated-coordinator
acceptance oracle (#12547) uses: the runtime does not yet expose a gateway-route
actuator, so the classical test *pins the oracle / classifier* that a future
actuator must conform to, rather than driving live tmux / Redmine.

The oracle is anchored to *shipped* code where the contract already exists: the
``implementation_gateway`` / ``implementation_worker`` template bodies resolved by
the real
:mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile`
resolver must carry the load-bearing clauses that encode each classified
scenario, so the oracle cannot silently drift from the contract the handoff
runtime actually sends.

Hermetic by construction: no live tmux, no Redmine reads/writes, no private pane
ids, no host paths. Fixtures use neutral placeholder tokens only.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (  # noqa: E402
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
    ROLE_PROFILE_SOURCE,
    ROLE_PROFILE_TOKENS,
    ROLE_PROFILE_VERSION,
    resolve_role_profile,
)

# ---------------------------------------------------------------------------
# Classification vocabulary (the oracle's fixed output language).
#
# Two non-defect outcomes (a legitimate delivery, and a correct fail-closed
# halt) plus the invariant-violation verdicts the harness must never mislabel as
# either. ``FAIL_CLOSED`` is the *expected* result of the self / foreign /
# ambiguous / uncertain scenarios — a correct halt, not a defect.
# ---------------------------------------------------------------------------
ROUTE_DELIVERED = "route_delivered"
ROUTE_FAIL_CLOSED = "route_fail_closed"

# Invariant violations.
V_CROSS_LANE_CLAUDE = "cross_lane_claude_direct"
V_DUPLICATE_REVIEW = "main_duplicate_review_requested"
V_COMPLETION_MISREAD = "completion_signal_misread"
V_OWNERSHIP_UNCONFIRMED = "same_lane_ownership_unconfirmed"
V_MULTI_SEND = "changes_requested_not_single_send"
V_BLIND_RETRY = "blind_retry_on_unroutable_target"
V_UNRECORDED_OUTCOME = "callback_outcome_not_recorded"

VERDICTS = frozenset(
    {
        ROUTE_DELIVERED,
        ROUTE_FAIL_CLOSED,
        V_CROSS_LANE_CLAUDE,
        V_DUPLICATE_REVIEW,
        V_COMPLETION_MISREAD,
        V_OWNERSHIP_UNCONFIRMED,
        V_MULTI_SEND,
        V_BLIND_RETRY,
        V_UNRECORDED_OUTCOME,
    }
)

# Inbound gateway events (the state the gateway is routing).
EV_REVIEW_REQUEST = "review_request"
EV_CHANGES_REQUESTED = "changes_requested"
EV_APPROVED = "approved"
EV_BLOCKED = "blocked"

# Resolved actual callback/route target shapes. ``designated`` is the intended
# actor (same-lane worker for changes_requested, upstream coordinator for an
# approved / blocked callback). The other three are the fail-closed triggers.
TARGET_DESIGNATED = "designated"
TARGET_SELF = "self"
TARGET_FOREIGN = "foreign_lane"
TARGET_AMBIGUOUS = "ambiguous"
UNROUTABLE_TARGETS = frozenset({TARGET_SELF, TARGET_FOREIGN, TARGET_AMBIGUOUS})

# Completion evidence surfaces. Only the durable record legitimately advances a
# Review Gate / integration reading; the others are notifications, not verdicts.
COMPLETION_DURABLE = "durable_record"
NON_DURABLE_COMPLETION = frozenset({"worker_ack", "pane_state", "transport_ack"})


@dataclass(frozen=True)
class GatewayRoute:
    """A single gateway callback-route decision, as the oracle sees it.

    Every field is a durable-record-safe token (no pane ids, no paths). The
    baselines below are non-defect outcomes; each test perturbs exactly one
    dimension to assert the matching verdict.
    """

    event: str
    # Did the gateway read the durable anchor and confirm this is its lane's request?
    same_lane_ownership: bool = True
    # For review_request / approved: did the gateway ask *main* to re-review the diff?
    requests_main_duplicate_review: bool = False
    # For changes_requested: how many sends reached the same-lane worker.
    worker_send_count: int = 1
    # The gateway's resolved actual route/callback target.
    resolved_target: str = TARGET_DESIGNATED
    # Delivery certainty of the send (a landing marker observed, or not).
    delivery: str = "certain"  # certain | uncertain
    # Was the callback outcome (sent / blocked / not-attempted) durably recorded?
    callback_outcome_recorded: bool = True
    # On an unroutable target / uncertain delivery: fail closed, or blind-retry?
    retry_on_failure: str = "none"  # none | blind
    # Did the gateway send directly to a cross-lane Claude (never allowed)?
    sends_cross_lane_claude_direct: bool = False
    # What the gateway treated as Review-Gate / integration completion.
    completion_read_from: str = COMPLETION_DURABLE


@dataclass(frozen=True)
class RouteVerdict:
    classification: str
    reason: str

    @property
    def is_delivered(self) -> bool:
        return self.classification == ROUTE_DELIVERED

    @property
    def is_fail_closed(self) -> bool:
        return self.classification == ROUTE_FAIL_CLOSED

    @property
    def is_violation(self) -> bool:
        return self.classification not in (ROUTE_DELIVERED, ROUTE_FAIL_CLOSED)


def _target_is_unroutable(route: GatewayRoute) -> bool:
    return route.resolved_target in UNROUTABLE_TARGETS or route.delivery == "uncertain"


def classify_gateway_route(route: GatewayRoute) -> RouteVerdict:
    """Classify a gateway callback-route decision.

    Precedence (first match wins) — fixed so mixed scenarios are deterministic:

    1. ``cross_lane_claude_direct`` — a cross-lane Claude direct send is the
       hardest invariant; lane crossing stays Codex-to-Codex regardless.
    2. ``completion_signal_misread`` — reading worker completion / pane state /
       transport ACK as a Review Gate or integration completion.
    3. ``main_duplicate_review_requested`` — asking main to re-review a diff the
       same-lane gateway already owns (review_request / approved events).
    4. ``same_lane_ownership_unconfirmed`` — routing without confirming the
       durable anchor is this lane's request.
    5. ``changes_requested_not_single_send`` — a ``changes_requested`` that did
       not reach the same-lane worker in exactly one send.
    6. unroutable target / uncertain delivery:
       ``blind_retry_on_unroutable_target`` if it blind-retried,
       ``callback_outcome_not_recorded`` if it went silent, else the correct
       ``route_fail_closed`` halt.
    7. clean route: ``callback_outcome_not_recorded`` if unrecorded, else
       ``route_delivered``.

    Pure and deterministic over its input.
    """
    if route.sends_cross_lane_claude_direct:
        return RouteVerdict(V_CROSS_LANE_CLAUDE, "cross_lane_claude_direct_send")

    if route.completion_read_from in NON_DURABLE_COMPLETION:
        return RouteVerdict(
            V_COMPLETION_MISREAD, f"read_completion_from:{route.completion_read_from}"
        )

    if (
        route.event in (EV_REVIEW_REQUEST, EV_APPROVED)
        and route.requests_main_duplicate_review
    ):
        return RouteVerdict(V_DUPLICATE_REVIEW, "requested_main_duplicate_review")

    if not route.same_lane_ownership:
        return RouteVerdict(V_OWNERSHIP_UNCONFIRMED, "did_not_confirm_same_lane_ownership")

    if route.event == EV_CHANGES_REQUESTED and route.worker_send_count != 1:
        return RouteVerdict(
            V_MULTI_SEND, f"worker_send_count:{route.worker_send_count}"
        )

    if _target_is_unroutable(route):
        if route.retry_on_failure == "blind":
            return RouteVerdict(V_BLIND_RETRY, "blind_retry_instead_of_fail_closed")
        if not route.callback_outcome_recorded:
            return RouteVerdict(V_UNRECORDED_OUTCOME, "unroutable_target_went_silent")
        return RouteVerdict(ROUTE_FAIL_CLOSED, _unroutable_reason(route))

    if not route.callback_outcome_recorded:
        return RouteVerdict(V_UNRECORDED_OUTCOME, "clean_route_but_outcome_not_recorded")

    return RouteVerdict(ROUTE_DELIVERED, _delivered_reason(route))


def _unroutable_reason(route: GatewayRoute) -> str:
    if route.delivery == "uncertain":
        return "fail_closed_uncertain_delivery"
    return f"fail_closed_{route.resolved_target}"


def _delivered_reason(route: GatewayRoute) -> str:
    if route.event == EV_APPROVED:
        return "approved_state_only_upstream"
    if route.event == EV_CHANGES_REQUESTED:
        return "changes_requested_single_send_to_worker"
    if route.event == EV_REVIEW_REQUEST:
        return "review_request_designated_same_lane_review"
    return "state_callback_upstream"


# ---------------------------------------------------------------------------
# Baseline factories: each is a non-defect outcome; override one field per test.
# ---------------------------------------------------------------------------
def approved_upstream_route(**overrides) -> GatewayRoute:
    """review_request -> designated same-lane review -> approved state-only upstream."""
    return replace(
        GatewayRoute(
            event=EV_APPROVED,
            same_lane_ownership=True,
            requests_main_duplicate_review=False,
            resolved_target=TARGET_DESIGNATED,
            delivery="certain",
            callback_outcome_recorded=True,
            retry_on_failure="none",
            completion_read_from=COMPLETION_DURABLE,
        ),
        **overrides,
    )


def changes_requested_route(**overrides) -> GatewayRoute:
    """changes_requested -> exactly one send to the same-lane worker -> correction."""
    return replace(
        GatewayRoute(
            event=EV_CHANGES_REQUESTED,
            same_lane_ownership=True,
            worker_send_count=1,
            resolved_target=TARGET_DESIGNATED,
            delivery="certain",
            callback_outcome_recorded=True,
            retry_on_failure="none",
            completion_read_from=COMPLETION_DURABLE,
        ),
        **overrides,
    )


class BaselineTest(unittest.TestCase):
    """The clean baselines must be non-defect, else every negative test is vacuous."""

    def test_review_request_to_approved_upstream_is_delivered(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route())
        self.assertTrue(verdict.is_delivered, verdict)
        self.assertEqual("approved_state_only_upstream", verdict.reason)

    def test_review_request_event_routes_to_same_lane_review(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(event=EV_REVIEW_REQUEST)
        )
        self.assertTrue(verdict.is_delivered, verdict)
        self.assertEqual("review_request_designated_same_lane_review", verdict.reason)

    def test_changes_requested_single_send_is_delivered(self) -> None:
        verdict = classify_gateway_route(changes_requested_route())
        self.assertTrue(verdict.is_delivered, verdict)
        self.assertEqual("changes_requested_single_send_to_worker", verdict.reason)

    def test_every_verdict_is_in_the_fixed_vocabulary(self) -> None:
        scenarios = [
            approved_upstream_route(),
            changes_requested_route(),
            approved_upstream_route(requests_main_duplicate_review=True),
            approved_upstream_route(sends_cross_lane_claude_direct=True),
            approved_upstream_route(resolved_target=TARGET_SELF),
            approved_upstream_route(resolved_target=TARGET_FOREIGN, retry_on_failure="blind"),
            changes_requested_route(worker_send_count=2),
            approved_upstream_route(completion_read_from="worker_ack"),
        ]
        for route in scenarios:
            self.assertIn(classify_gateway_route(route).classification, VERDICTS)


class DuplicateControlTest(unittest.TestCase):
    """Acceptance #4: main duplicate review is never requested."""

    def test_review_request_requesting_main_duplicate_review_is_violation(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(
                event=EV_REVIEW_REQUEST, requests_main_duplicate_review=True
            )
        )
        self.assertTrue(verdict.is_violation)
        self.assertEqual(V_DUPLICATE_REVIEW, verdict.classification)

    def test_approved_requesting_main_duplicate_review_is_violation(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(requests_main_duplicate_review=True)
        )
        self.assertEqual(V_DUPLICATE_REVIEW, verdict.classification)

    def test_no_duplicate_review_on_the_happy_path(self) -> None:
        self.assertTrue(classify_gateway_route(approved_upstream_route()).is_delivered)


class ExactlyOneWorkerSendTest(unittest.TestCase):
    """Acceptance #4: changes_requested reaches the same-lane worker in exactly one send."""

    def test_two_sends_is_multi_send_violation(self) -> None:
        verdict = classify_gateway_route(changes_requested_route(worker_send_count=2))
        self.assertEqual(V_MULTI_SEND, verdict.classification)

    def test_zero_sends_is_multi_send_violation(self) -> None:
        verdict = classify_gateway_route(changes_requested_route(worker_send_count=0))
        self.assertEqual(V_MULTI_SEND, verdict.classification)

    def test_exactly_one_send_is_delivered(self) -> None:
        self.assertTrue(
            classify_gateway_route(changes_requested_route(worker_send_count=1)).is_delivered
        )


class SelfForeignAmbiguousFailClosedTest(unittest.TestCase):
    """Acceptance #4: self / foreign / ambiguous / uncertain -> fail closed, zero blind retry."""

    def test_self_route_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(resolved_target=TARGET_SELF))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_self", verdict.reason)

    def test_foreign_lane_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(resolved_target=TARGET_FOREIGN))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_foreign_lane", verdict.reason)

    def test_ambiguous_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(resolved_target=TARGET_AMBIGUOUS))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_ambiguous", verdict.reason)

    def test_uncertain_delivery_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(delivery="uncertain"))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_uncertain_delivery", verdict.reason)

    def test_fail_closed_is_not_a_violation(self) -> None:
        # A correct halt is not a defect: it must not be mislabelled as a violation.
        for target in UNROUTABLE_TARGETS:
            verdict = classify_gateway_route(approved_upstream_route(resolved_target=target))
            self.assertFalse(verdict.is_violation, f"{target} should be a clean halt")

    def test_blind_retry_on_unroutable_target_is_violation(self) -> None:
        for target in UNROUTABLE_TARGETS:
            verdict = classify_gateway_route(
                approved_upstream_route(resolved_target=target, retry_on_failure="blind")
            )
            self.assertEqual(V_BLIND_RETRY, verdict.classification, target)

    def test_blind_retry_on_uncertain_delivery_is_violation(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(delivery="uncertain", retry_on_failure="blind")
        )
        self.assertEqual(V_BLIND_RETRY, verdict.classification)

    def test_unroutable_target_going_silent_is_unrecorded_outcome(self) -> None:
        # Fail-closed still requires a recorded outcome; silence is not valid.
        verdict = classify_gateway_route(
            approved_upstream_route(
                resolved_target=TARGET_SELF, callback_outcome_recorded=False
            )
        )
        self.assertEqual(V_UNRECORDED_OUTCOME, verdict.classification)


class CrossLaneClaudeDirectTest(unittest.TestCase):
    """Acceptance #4: no cross-lane Claude direct send, even under other faults."""

    def test_cross_lane_claude_direct_is_violation(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(sends_cross_lane_claude_direct=True)
        )
        self.assertEqual(V_CROSS_LANE_CLAUDE, verdict.classification)

    def test_cross_lane_claude_outranks_other_findings(self) -> None:
        # Even if the route also duplicates review, the hardest invariant wins.
        verdict = classify_gateway_route(
            approved_upstream_route(
                sends_cross_lane_claude_direct=True, requests_main_duplicate_review=True
            )
        )
        self.assertEqual(V_CROSS_LANE_CLAUDE, verdict.classification)


class CompletionMisreadTest(unittest.TestCase):
    """Acceptance scope: worker completion / pane state / transport ACK are not Review/integration done."""

    def test_worker_ack_misread_is_violation(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(completion_read_from="worker_ack")
        )
        self.assertEqual(V_COMPLETION_MISREAD, verdict.classification)

    def test_every_non_durable_completion_surface_is_violation(self) -> None:
        for surface in NON_DURABLE_COMPLETION:
            verdict = classify_gateway_route(
                approved_upstream_route(completion_read_from=surface)
            )
            self.assertEqual(V_COMPLETION_MISREAD, verdict.classification, surface)

    def test_durable_record_completion_is_not_a_violation(self) -> None:
        self.assertTrue(
            classify_gateway_route(
                approved_upstream_route(completion_read_from=COMPLETION_DURABLE)
            ).is_delivered
        )


class OwnershipConfirmationTest(unittest.TestCase):
    """Acceptance scope: the gateway confirms same-lane ownership before routing."""

    def test_unconfirmed_ownership_is_violation(self) -> None:
        verdict = classify_gateway_route(
            changes_requested_route(same_lane_ownership=False)
        )
        self.assertEqual(V_OWNERSHIP_UNCONFIRMED, verdict.classification)


class RoleProfileContractAnchorTest(unittest.TestCase):
    """Anchor the oracle to the *shipped* gateway/worker role-profile contract.

    If the template bodies drift from the behaviors this oracle classifies, these
    fail so the contract and the oracle stay in lockstep rather than diverging.
    """

    def setUp(self) -> None:
        self.gateway = resolve_role_profile(ROLE_IMPLEMENTATION_GATEWAY, {}).resolved_text
        self.worker = resolve_role_profile(ROLE_IMPLEMENTATION_WORKER, {}).resolved_text

    def test_both_roles_are_real_shipped_tokens(self) -> None:
        for role in (ROLE_IMPLEMENTATION_GATEWAY, ROLE_IMPLEMENTATION_WORKER):
            self.assertIn(role, ROLE_PROFILE_TOKENS, role)

    def test_source_and_version_are_pinned(self) -> None:
        for role in (ROLE_IMPLEMENTATION_GATEWAY, ROLE_IMPLEMENTATION_WORKER):
            resolution = resolve_role_profile(role, {})
            self.assertEqual(ROLE_PROFILE_SOURCE, resolution.profile_source)
            self.assertEqual(ROLE_PROFILE_VERSION, resolution.profile_version)

    def test_gateway_contract_encodes_duplicate_control(self) -> None:
        self.assertIn("main coordinator に重複 review を要求しない", self.gateway)

    def test_gateway_contract_encodes_single_send_changes_requested(self) -> None:
        self.assertIn("`changes_requested` は same-lane worker へ単回送達", self.gateway)

    def test_gateway_contract_encodes_approved_state_only_upstream(self) -> None:
        self.assertIn("`approved` のときは上位", self.gateway)
        self.assertIn("状態だけを callback", self.gateway)

    def test_gateway_contract_encodes_self_foreign_ambiguous_fail_closed(self) -> None:
        self.assertIn("self-route / foreign lane / ambiguous", self.gateway)
        self.assertIn("fail-closed で停止し、blind retry しない", self.gateway)

    def test_gateway_contract_encodes_no_completion_misread(self) -> None:
        self.assertIn(
            "Review Gate approval や integration 完了と読み替えない", self.gateway
        )

    def test_worker_contract_returns_to_same_lane_gateway(self) -> None:
        self.assertIn("same-lane gateway", self.worker)
        self.assertIn("へ返す", self.worker)

    def test_worker_contract_keeps_hierarchical_route(self) -> None:
        self.assertIn(
            "main coordinator や foreign lane へ直接 callback せず", self.worker
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
