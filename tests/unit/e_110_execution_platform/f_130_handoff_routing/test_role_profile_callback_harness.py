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

Acceptance #4 requires the synthetic scenario to fix *direction-specific*
routing (review_request -> local same-lane review, changes_requested -> the
same-lane worker, approved -> upstream coordinator) AND the ``-> correction``
return, not merely that *some* delivery happened. So the oracle models an
explicit event -> expected-actor matrix, fails closed on any unknown/mismatched
event / actor / delivery, and models the ``changes_requested`` correction being
returned to and durably recorded by the same-lane worker (#13745 review j#77428
P2: a single ``designated`` token could not tell an approved-to-worker misroute
from a correct upstream callback, and unknown inputs were mislabelled delivered).

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
# Two non-defect outcomes (a legitimate delivery to the *correct* actor, and a
# correct fail-closed halt) plus the invariant-violation verdicts the harness
# must never mislabel as either. ``FAIL_CLOSED`` is the *expected* result of the
# self / foreign / ambiguous / uncertain scenarios — a correct halt, not a defect.
# ---------------------------------------------------------------------------
ROUTE_DELIVERED = "route_delivered"
ROUTE_FAIL_CLOSED = "route_fail_closed"

# Invariant / direction / precondition violations.
V_CROSS_LANE_CLAUDE = "cross_lane_claude_direct"
V_DUPLICATE_REVIEW = "main_duplicate_review_requested"
V_COMPLETION_MISREAD = "completion_signal_misread"
V_OWNERSHIP_UNCONFIRMED = "same_lane_ownership_unconfirmed"
V_MULTI_SEND = "changes_requested_not_single_send"
V_CORRECTION_NOT_RETURNED = "correction_not_returned_to_gateway"
V_WRONG_DIRECTION = "route_wrong_direction_for_event"
V_UNKNOWN_EVENT = "unknown_event"
V_UNKNOWN_TARGET = "unknown_route_target"
V_UNKNOWN_DELIVERY = "unknown_delivery_state"
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
        V_CORRECTION_NOT_RETURNED,
        V_WRONG_DIRECTION,
        V_UNKNOWN_EVENT,
        V_UNKNOWN_TARGET,
        V_UNKNOWN_DELIVERY,
        V_BLIND_RETRY,
        V_UNRECORDED_OUTCOME,
    }
)

# Inbound gateway events (the state the gateway is routing).
EV_REVIEW_REQUEST = "review_request"
EV_CHANGES_REQUESTED = "changes_requested"
EV_APPROVED = "approved"
EV_BLOCKED = "blocked"

# Actor / direction tokens the route can actually target. The three good actors
# each correspond to exactly one direction; the three problematic ones are the
# fail-closed triggers.
ACTOR_LOCAL_GATEWAY_REVIEW = "local_gateway_review"  # same-lane review of a review_request
ACTOR_SAME_LANE_WORKER = "same_lane_worker"  # changes_requested lands here
ACTOR_UPSTREAM_COORDINATOR = "upstream_coordinator"  # approved / blocked state callback
ACTOR_SELF = "self"
ACTOR_FOREIGN_LANE = "foreign_lane"
ACTOR_AMBIGUOUS = "ambiguous"

KNOWN_GOOD_ACTORS = frozenset(
    {ACTOR_LOCAL_GATEWAY_REVIEW, ACTOR_SAME_LANE_WORKER, ACTOR_UPSTREAM_COORDINATOR}
)
UNROUTABLE_ACTORS = frozenset({ACTOR_SELF, ACTOR_FOREIGN_LANE, ACTOR_AMBIGUOUS})

# The event -> correct-direction matrix. This is the load-bearing acceptance-#4
# pin: each event has exactly one legitimate actor, so an approved-to-worker or
# changes_requested-to-upstream misroute is caught rather than passed.
EVENT_EXPECTED_ACTOR = {
    EV_REVIEW_REQUEST: ACTOR_LOCAL_GATEWAY_REVIEW,
    EV_CHANGES_REQUESTED: ACTOR_SAME_LANE_WORKER,
    EV_APPROVED: ACTOR_UPSTREAM_COORDINATOR,
    EV_BLOCKED: ACTOR_UPSTREAM_COORDINATOR,
}
KNOWN_EVENTS = frozenset(EVENT_EXPECTED_ACTOR)

# Delivery certainty vocabulary. Anything else is an unknown state, not "fine".
DELIVERY_CERTAIN = "certain"
DELIVERY_UNCERTAIN = "uncertain"
KNOWN_DELIVERIES = frozenset({DELIVERY_CERTAIN, DELIVERY_UNCERTAIN})

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
    # The direction/actor the route actually targeted. Empty == unresolved.
    actual_actor: str = ""
    # Did the gateway read the durable anchor and confirm this is its lane's request?
    same_lane_ownership: bool = True
    # For review_request / approved: did the gateway ask *main* to re-review the diff?
    requests_main_duplicate_review: bool = False
    # For changes_requested: how many sends reached the same-lane worker.
    worker_send_count: int = 1
    # For changes_requested: did the worker return the correction to the same-lane
    # gateway, and durably record it? (the "-> correction" half of acceptance #4)
    worker_returns_correction: bool = True
    correction_recorded: bool = True
    # Delivery certainty of the send (a landing marker observed, or not).
    delivery: str = DELIVERY_CERTAIN
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


def classify_gateway_route(route: GatewayRoute) -> RouteVerdict:
    """Classify a gateway callback-route decision.

    Precedence (first match wins) — fixed so mixed scenarios are deterministic:

    1. ``cross_lane_claude_direct`` — the hardest invariant; lane crossing stays
       Codex-to-Codex regardless.
    2. ``completion_signal_misread`` — reading worker completion / pane state /
       transport ACK as a Review Gate or integration completion.
    3. ``unknown_event`` — an event outside the fixed vocabulary fails closed.
    4. ``unknown_delivery_state`` — a delivery token outside {certain, uncertain}
       fails closed (never silently treated as delivered).
    5. ``main_duplicate_review_requested`` — asking main to re-review a diff the
       same-lane gateway already owns (review_request / approved events).
    6. ``same_lane_ownership_unconfirmed`` — routing without confirming the
       durable anchor is this lane's request.
    7. unroutable actor / uncertain delivery:
       ``blind_retry_on_unroutable_target`` if it blind-retried,
       ``callback_outcome_not_recorded`` if it went silent, else the correct
       ``route_fail_closed`` halt.
    8. ``unknown_route_target`` — an actor outside the known-good set fails closed.
    9. ``route_wrong_direction_for_event`` — a known-good actor that is not the
       event's expected actor (e.g. approved routed to the worker).
    10. ``changes_requested`` specifics: ``changes_requested_not_single_send`` if
        it did not reach the worker in exactly one send;
        ``correction_not_returned_to_gateway`` if the worker did not return /
        durably record the correction.
    11. ``callback_outcome_not_recorded`` if the clean route went unrecorded.
    12. ``route_delivered`` to the verified correct actor.

    Pure and deterministic over its input.
    """
    if route.sends_cross_lane_claude_direct:
        return RouteVerdict(V_CROSS_LANE_CLAUDE, "cross_lane_claude_direct_send")

    if route.completion_read_from in NON_DURABLE_COMPLETION:
        return RouteVerdict(
            V_COMPLETION_MISREAD, f"read_completion_from:{route.completion_read_from}"
        )

    if route.event not in KNOWN_EVENTS:
        return RouteVerdict(V_UNKNOWN_EVENT, f"event:{route.event}")

    if route.delivery not in KNOWN_DELIVERIES:
        return RouteVerdict(V_UNKNOWN_DELIVERY, f"delivery:{route.delivery}")

    if (
        route.event in (EV_REVIEW_REQUEST, EV_APPROVED)
        and route.requests_main_duplicate_review
    ):
        return RouteVerdict(V_DUPLICATE_REVIEW, "requested_main_duplicate_review")

    if not route.same_lane_ownership:
        return RouteVerdict(V_OWNERSHIP_UNCONFIRMED, "did_not_confirm_same_lane_ownership")

    # Unroutable actor / uncertain delivery -> correct fail-closed halt (or its
    # two failure modes). Checked before direction so a self/foreign/ambiguous
    # target is the fail-closed scenario, not a "wrong direction" nitpick.
    if route.actual_actor in UNROUTABLE_ACTORS or route.delivery == DELIVERY_UNCERTAIN:
        if route.retry_on_failure == "blind":
            return RouteVerdict(V_BLIND_RETRY, "blind_retry_instead_of_fail_closed")
        if not route.callback_outcome_recorded:
            return RouteVerdict(V_UNRECORDED_OUTCOME, "unroutable_target_went_silent")
        return RouteVerdict(ROUTE_FAIL_CLOSED, _unroutable_reason(route))

    if route.actual_actor not in KNOWN_GOOD_ACTORS:
        return RouteVerdict(V_UNKNOWN_TARGET, f"actor:{route.actual_actor}")

    expected = EVENT_EXPECTED_ACTOR[route.event]
    if route.actual_actor != expected:
        return RouteVerdict(
            V_WRONG_DIRECTION, f"event:{route.event}->actor:{route.actual_actor}!={expected}"
        )

    if route.event == EV_CHANGES_REQUESTED:
        if route.worker_send_count != 1:
            return RouteVerdict(V_MULTI_SEND, f"worker_send_count:{route.worker_send_count}")
        if not route.worker_returns_correction:
            return RouteVerdict(V_CORRECTION_NOT_RETURNED, "correction_not_returned")
        if not route.correction_recorded:
            return RouteVerdict(V_CORRECTION_NOT_RETURNED, "correction_not_durably_recorded")

    if not route.callback_outcome_recorded:
        return RouteVerdict(V_UNRECORDED_OUTCOME, "clean_route_but_outcome_not_recorded")

    return RouteVerdict(ROUTE_DELIVERED, _delivered_reason(route))


def _unroutable_reason(route: GatewayRoute) -> str:
    if route.delivery == DELIVERY_UNCERTAIN:
        return "fail_closed_uncertain_delivery"
    return f"fail_closed_{route.actual_actor}"


def _delivered_reason(route: GatewayRoute) -> str:
    # Reached only after the actor was verified against the event, so the reason
    # reflects the *verified direction*, not the event token alone.
    if route.event == EV_APPROVED:
        return "approved_state_only_upstream"
    if route.event == EV_CHANGES_REQUESTED:
        return "changes_requested_single_send_correction_returned"
    if route.event == EV_REVIEW_REQUEST:
        return "review_request_local_same_lane_review"
    return "blocked_state_callback_upstream"


# ---------------------------------------------------------------------------
# Baseline factories: each is a non-defect outcome routed to its correct actor;
# override one field per test.
# ---------------------------------------------------------------------------
def approved_upstream_route(**overrides) -> GatewayRoute:
    """review_request -> designated same-lane review -> approved state-only upstream."""
    return replace(
        GatewayRoute(event=EV_APPROVED, actual_actor=ACTOR_UPSTREAM_COORDINATOR),
        **overrides,
    )


def review_request_route(**overrides) -> GatewayRoute:
    """review_request routed to the designated same-lane review."""
    return replace(
        GatewayRoute(event=EV_REVIEW_REQUEST, actual_actor=ACTOR_LOCAL_GATEWAY_REVIEW),
        **overrides,
    )


def changes_requested_route(**overrides) -> GatewayRoute:
    """changes_requested -> exactly one send to the same-lane worker -> correction returned."""
    return replace(
        GatewayRoute(
            event=EV_CHANGES_REQUESTED,
            actual_actor=ACTOR_SAME_LANE_WORKER,
            worker_send_count=1,
            worker_returns_correction=True,
            correction_recorded=True,
        ),
        **overrides,
    )


class BaselineTest(unittest.TestCase):
    """The clean baselines must be non-defect, else every negative test is vacuous."""

    def test_review_request_routes_to_local_same_lane_review(self) -> None:
        verdict = classify_gateway_route(review_request_route())
        self.assertTrue(verdict.is_delivered, verdict)
        self.assertEqual("review_request_local_same_lane_review", verdict.reason)

    def test_approved_state_only_upstream_is_delivered(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route())
        self.assertTrue(verdict.is_delivered, verdict)
        self.assertEqual("approved_state_only_upstream", verdict.reason)

    def test_changes_requested_single_send_correction_returned_is_delivered(self) -> None:
        verdict = classify_gateway_route(changes_requested_route())
        self.assertTrue(verdict.is_delivered, verdict)
        self.assertEqual(
            "changes_requested_single_send_correction_returned", verdict.reason
        )

    def test_every_verdict_is_in_the_fixed_vocabulary(self) -> None:
        scenarios = [
            review_request_route(),
            approved_upstream_route(),
            changes_requested_route(),
            approved_upstream_route(requests_main_duplicate_review=True),
            approved_upstream_route(sends_cross_lane_claude_direct=True),
            approved_upstream_route(actual_actor=ACTOR_SELF),
            approved_upstream_route(actual_actor=ACTOR_FOREIGN_LANE, retry_on_failure="blind"),
            approved_upstream_route(actual_actor=ACTOR_SAME_LANE_WORKER),  # wrong direction
            approved_upstream_route(actual_actor="typo_actor"),  # unknown target
            approved_upstream_route(event="garbage_event"),  # unknown event
            approved_upstream_route(delivery="garbage"),  # unknown delivery
            changes_requested_route(worker_send_count=2),
            changes_requested_route(worker_returns_correction=False),
            approved_upstream_route(completion_read_from="worker_ack"),
        ]
        for route in scenarios:
            self.assertIn(classify_gateway_route(route).classification, VERDICTS, route)


class DirectionMatrixTest(unittest.TestCase):
    """Acceptance #4: each event routes to its one correct direction; misroutes fail closed."""

    def test_each_event_delivers_to_its_expected_actor(self) -> None:
        cases = {
            EV_REVIEW_REQUEST: ACTOR_LOCAL_GATEWAY_REVIEW,
            EV_CHANGES_REQUESTED: ACTOR_SAME_LANE_WORKER,
            EV_APPROVED: ACTOR_UPSTREAM_COORDINATOR,
            EV_BLOCKED: ACTOR_UPSTREAM_COORDINATOR,
        }
        for event, actor in cases.items():
            verdict = classify_gateway_route(GatewayRoute(event=event, actual_actor=actor))
            self.assertTrue(verdict.is_delivered, f"{event}->{actor}: {verdict}")

    def test_approved_routed_to_worker_is_wrong_direction(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(actual_actor=ACTOR_SAME_LANE_WORKER)
        )
        self.assertEqual(V_WRONG_DIRECTION, verdict.classification)

    def test_changes_requested_routed_upstream_is_wrong_direction(self) -> None:
        verdict = classify_gateway_route(
            changes_requested_route(actual_actor=ACTOR_UPSTREAM_COORDINATOR)
        )
        self.assertEqual(V_WRONG_DIRECTION, verdict.classification)

    def test_review_request_routed_upstream_is_wrong_direction(self) -> None:
        verdict = classify_gateway_route(
            review_request_route(actual_actor=ACTOR_UPSTREAM_COORDINATOR)
        )
        self.assertEqual(V_WRONG_DIRECTION, verdict.classification)

    def test_every_cross_event_misroute_is_caught(self) -> None:
        # Any known-good actor that is not the event's expected actor fails.
        for event, expected in EVENT_EXPECTED_ACTOR.items():
            for actor in KNOWN_GOOD_ACTORS - {expected}:
                verdict = classify_gateway_route(
                    GatewayRoute(event=event, actual_actor=actor)
                )
                self.assertEqual(
                    V_WRONG_DIRECTION, verdict.classification, f"{event}->{actor}"
                )

    def test_unknown_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(actual_actor="some_typo"))
        self.assertEqual(V_UNKNOWN_TARGET, verdict.classification)

    def test_unresolved_empty_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(GatewayRoute(event=EV_APPROVED, actual_actor=""))
        self.assertEqual(V_UNKNOWN_TARGET, verdict.classification)


class UnknownEventDeliveryTest(unittest.TestCase):
    """Acceptance #4: unknown event / delivery are fail-closed, never 'delivered'."""

    def test_unknown_event_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(event="garbage_event"))
        self.assertEqual(V_UNKNOWN_EVENT, verdict.classification)

    def test_unknown_delivery_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(delivery="garbage"))
        self.assertEqual(V_UNKNOWN_DELIVERY, verdict.classification)

    def test_known_event_and_delivery_are_not_flagged(self) -> None:
        self.assertTrue(classify_gateway_route(approved_upstream_route()).is_delivered)


class DuplicateControlTest(unittest.TestCase):
    """Acceptance #4: main duplicate review is never requested."""

    def test_review_request_requesting_main_duplicate_review_is_violation(self) -> None:
        verdict = classify_gateway_route(
            review_request_route(requests_main_duplicate_review=True)
        )
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


class CorrectionReturnTest(unittest.TestCase):
    """Acceptance #4: changes_requested -> ... -> correction returned to the same-lane gateway."""

    def test_worker_not_returning_correction_is_violation(self) -> None:
        verdict = classify_gateway_route(
            changes_requested_route(worker_returns_correction=False)
        )
        self.assertEqual(V_CORRECTION_NOT_RETURNED, verdict.classification)
        self.assertEqual("correction_not_returned", verdict.reason)

    def test_correction_not_durably_recorded_is_violation(self) -> None:
        verdict = classify_gateway_route(
            changes_requested_route(correction_recorded=False)
        )
        self.assertEqual(V_CORRECTION_NOT_RETURNED, verdict.classification)
        self.assertEqual("correction_not_durably_recorded", verdict.reason)

    def test_returned_and_recorded_correction_is_delivered(self) -> None:
        self.assertTrue(
            classify_gateway_route(
                changes_requested_route(
                    worker_returns_correction=True, correction_recorded=True
                )
            ).is_delivered
        )

    def test_correction_fields_do_not_gate_non_changes_events(self) -> None:
        # approved/review_request carry no correction obligation; the fields are
        # ignored for them (no false positive).
        verdict = classify_gateway_route(
            approved_upstream_route(worker_returns_correction=False, correction_recorded=False)
        )
        self.assertTrue(verdict.is_delivered, verdict)


class SelfForeignAmbiguousFailClosedTest(unittest.TestCase):
    """Acceptance #4: self / foreign / ambiguous / uncertain -> fail closed, zero blind retry."""

    def test_self_route_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(actual_actor=ACTOR_SELF))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_self", verdict.reason)

    def test_foreign_lane_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(actual_actor=ACTOR_FOREIGN_LANE))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_foreign_lane", verdict.reason)

    def test_ambiguous_target_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(actual_actor=ACTOR_AMBIGUOUS))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_ambiguous", verdict.reason)

    def test_uncertain_delivery_fails_closed(self) -> None:
        verdict = classify_gateway_route(approved_upstream_route(delivery=DELIVERY_UNCERTAIN))
        self.assertTrue(verdict.is_fail_closed, verdict)
        self.assertEqual("fail_closed_uncertain_delivery", verdict.reason)

    def test_fail_closed_is_not_a_violation(self) -> None:
        # A correct halt is not a defect: it must not be mislabelled as a violation.
        for actor in UNROUTABLE_ACTORS:
            verdict = classify_gateway_route(approved_upstream_route(actual_actor=actor))
            self.assertFalse(verdict.is_violation, f"{actor} should be a clean halt")

    def test_blind_retry_on_unroutable_target_is_violation(self) -> None:
        for actor in UNROUTABLE_ACTORS:
            verdict = classify_gateway_route(
                approved_upstream_route(actual_actor=actor, retry_on_failure="blind")
            )
            self.assertEqual(V_BLIND_RETRY, verdict.classification, actor)

    def test_blind_retry_on_uncertain_delivery_is_violation(self) -> None:
        verdict = classify_gateway_route(
            approved_upstream_route(delivery=DELIVERY_UNCERTAIN, retry_on_failure="blind")
        )
        self.assertEqual(V_BLIND_RETRY, verdict.classification)

    def test_unroutable_target_going_silent_is_unrecorded_outcome(self) -> None:
        # Fail-closed still requires a recorded outcome; silence is not valid.
        verdict = classify_gateway_route(
            approved_upstream_route(
                actual_actor=ACTOR_SELF, callback_outcome_recorded=False
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

    def test_worker_contract_returns_correction_to_same_lane_gateway(self) -> None:
        # The "-> correction returned to same-lane gateway" half of acceptance #4.
        self.assertIn("correction", self.worker)
        self.assertIn("same-lane gateway", self.worker)
        self.assertIn("へ返す", self.worker)

    def test_worker_contract_keeps_hierarchical_route(self) -> None:
        self.assertIn(
            "main coordinator や foreign lane へ直接 callback せず", self.worker
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
