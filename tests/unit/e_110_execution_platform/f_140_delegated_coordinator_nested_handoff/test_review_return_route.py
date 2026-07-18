"""Correlated review_result return routing — pure policy (Redmine #13684).

The fail-closed decision matrix for returning a coordinator-recorded ``review_result`` to its
owning-lane Codex gateway (design answer j#77892 corrections 2 + 3 + 4): the route derivation, the
latest-review fence, the owning-lane target authority, and the self-route / no-gateway /
blank-generation refusals. Pure — no I/O, no store.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    build_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    COORDINATOR_LANE,
    OWNER_ABSENT,
    OWNER_AMBIGUOUS,
    OWNER_RESOLVED,
    RETURN_AMBIGUOUS_OWNER,
    RETURN_BLANK_GENERATION,
    RETURN_NO_GATEWAY,
    RETURN_NO_OWNER,
    RETURN_NO_REVIEW_REQUEST,
    RETURN_NOT_LATEST,
    RETURN_NOT_REVIEW_RESULT,
    RETURN_OK,
    RETURN_PREVIOUS_GENERATION,
    RETURN_SELF_ROUTE,
    REVIEW_RETURN_FENCE_NO_CORRELATION,
    REVIEW_RETURN_FENCE_PREVIOUS_GENERATION,
    REVIEW_RETURN_FENCE_UNRESOLVED_ANCHOR,
    REVIEW_RETURN_ROUTE_PREFIX,
    OwningLaneBinding,
    correlated_review_request_journal,
    decode_review_return_payload,
    encode_review_return_payload,
    is_review_return_route,
    latest_review_result_journal,
    make_review_return_send_edge_fence,
    plan_review_return,
    plan_review_returns,
    review_return_callback_route,
    review_return_is_current,
    review_round_within_generation,
)

ISSUE = "13684"


def _review_result(journal: str, *, issue: str = ISSUE, conclusion: str = "approved"):
    return build_marker(issue, journal, "review_result", review_conclusion=conclusion)


def _review_request(journal: str, *, issue: str = ISSUE):
    return build_marker(issue, journal, "review_request")


def _impl_done(journal: str, *, issue: str = ISSUE):
    return build_marker(issue, journal, "implementation_done")


def _owner(**kw) -> OwningLaneBinding:
    base = dict(status=OWNER_RESOLVED, lane_id="issue_13684", generation="3", gateway_receiver="codex")
    base.update(kw)
    return OwningLaneBinding(**base)


#: A valid review round on ISSUE: a review_request (j10) then the review_result (j20) it answers, so
#: a plan for j20 passes the round-correlation fence and the test isolates the dimension it varies.
_ROUND = [_review_request("10"), _review_result("20")]


class ReviewReturnRouteTest(unittest.TestCase):
    def test_callback_route_encodes_owning_lane(self) -> None:
        route = review_return_callback_route("issue_13684")
        self.assertEqual(route, f"{REVIEW_RETURN_ROUTE_PREFIX}:issue_13684")
        self.assertTrue(is_review_return_route(route))
        self.assertFalse(is_review_return_route("coordinator"))

    def test_blank_lane_route_is_a_fail_closed_error(self) -> None:
        with self.assertRaises(ValueError):
            review_return_callback_route("   ")

    def test_emit_ok_carries_the_durable_correlation(self) -> None:
        markers = [_review_request("10"), _review_result("20")]
        plan = plan_review_return(markers, ISSUE, "20", _owner())
        self.assertTrue(plan.emit)
        self.assertEqual(plan.reason, RETURN_OK)
        self.assertEqual(plan.callback_route, "review_return:issue_13684")
        self.assertEqual(plan.target_lane, "issue_13684")
        self.assertEqual(plan.target_receiver, "codex")
        self.assertEqual(plan.target_generation, "3")
        self.assertEqual(plan.review_journal, "20")
        # R1-F2: the return correlates to the review_request it answers (action identity).
        self.assertEqual(plan.review_request_journal, "10")

    def test_review_result_without_a_review_request_is_refused(self) -> None:
        # R1-F2: an uncorrelated review outcome (no preceding review_request) is never returned.
        plan = plan_review_return([_review_result("20")], ISSUE, "20", _owner())
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NO_REVIEW_REQUEST)

    def test_correlated_review_request_is_the_latest_before_the_result(self) -> None:
        markers = [_review_request("5"), _review_request("10"), _review_result("20"), _review_request("30")]
        # The request the result answers is the newest request BEFORE the result (10, not 30).
        self.assertEqual(correlated_review_request_journal(markers, ISSUE, "20"), "10")
        self.assertEqual(correlated_review_request_journal([_review_result("20")], ISSUE, "20"), "")

    def test_latest_review_result_journal_picks_the_newest(self) -> None:
        markers = [_review_result("20"), _review_result("35"), _review_result("9")]
        self.assertEqual(latest_review_result_journal(markers, ISSUE), "35")
        self.assertEqual(latest_review_result_journal([], ISSUE), "")

    def test_stale_result_shadowed_by_newer_result_is_refused(self) -> None:
        markers = [_review_request("10"), _review_result("20"), _review_result("35")]
        plan = plan_review_return(markers, ISSUE, "20", _owner())
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NOT_LATEST)
        # ...and the newest result IS returnable.
        self.assertTrue(plan_review_return(markers, ISSUE, "35", _owner()).emit)

    def test_review_return_is_current_action_time_fence(self) -> None:
        markers = [_review_request("10"), _review_result("20")]
        # Reserved round is still current.
        self.assertTrue(review_return_is_current(markers, ISSUE, "20", "10"))
        # A newer review_request landed after reserve -> the round restarted -> stale.
        self.assertFalse(review_return_is_current(markers + [_review_request("30")], ISSUE, "20", "10"))
        # A newer review_result landed -> stale.
        self.assertFalse(review_return_is_current(markers + [_review_result("40")], ISSUE, "20", "10"))
        # R1-re-review F1: a newer implementation_done correction landed -> stale.
        self.assertFalse(review_return_is_current(markers + [_impl_done("30")], ISSUE, "20", "10"))
        # The recorded correlation drifted from the current one -> stale.
        self.assertFalse(review_return_is_current(markers, ISSUE, "20", "7"))
        # R1-re-review F2: a blank recorded correlation is fail-closed (never a wildcard), even with a
        # valid live review round present.
        self.assertFalse(review_return_is_current(markers, ISSUE, "20", ""))
        # An uncorrelated result (no request) is not current.
        self.assertFalse(review_return_is_current([_review_result("20")], ISSUE, "20", ""))

    def test_newer_implementation_done_correction_refuses_at_discovery(self) -> None:
        # R1-re-review F1: a correction (implementation_done j30) after the result stales the return.
        markers = [_review_request("10"), _review_result("20"), _impl_done("30")]
        plan = plan_review_return(markers, ISSUE, "20", _owner())
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NOT_LATEST)
        # An OLDER implementation_done (before the result) does not shadow it.
        self.assertTrue(plan_review_return([_impl_done("5"), _review_request("10"), _review_result("20")], ISSUE, "20", _owner()).emit)

    def test_payload_round_trips_the_correlation(self) -> None:
        self.assertEqual(decode_review_return_payload(encode_review_return_payload("10")), "10")
        self.assertEqual(encode_review_return_payload(""), "")
        self.assertEqual(decode_review_return_payload(""), "")
        self.assertEqual(decode_review_return_payload("not json"), "")

    def test_newer_review_request_restarts_the_round_and_refuses_old_result(self) -> None:
        # A review round restarted after the result (j30 review_request > j20 review_result).
        markers = [_review_request("10"), _review_result("20"), _review_request("30")]
        plan = plan_review_return(markers, ISSUE, "20", _owner())
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NOT_LATEST)

    def test_older_review_request_does_not_shadow_the_result(self) -> None:
        markers = [_review_request("10"), _review_result("20")]
        self.assertTrue(plan_review_return(markers, ISSUE, "20", _owner()).emit)

    def test_non_review_result_journal_is_refused(self) -> None:
        markers = [_review_request("10"), _review_result("20")]
        # journal 10 carries a review_request, not a review_result.
        plan = plan_review_return(markers, ISSUE, "10", _owner())
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NOT_REVIEW_RESULT)
        # A journal with no marker at all.
        self.assertEqual(
            plan_review_return(markers, ISSUE, "999", _owner()).reason, RETURN_NOT_REVIEW_RESULT
        )

    def test_absent_owner_is_refused(self) -> None:
        plan = plan_review_return(_ROUND, ISSUE, "20", OwningLaneBinding(status=OWNER_ABSENT))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NO_OWNER)

    def test_ambiguous_owner_is_refused(self) -> None:
        plan = plan_review_return(_ROUND, ISSUE, "20", OwningLaneBinding(status=OWNER_AMBIGUOUS))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_AMBIGUOUS_OWNER)

    def test_self_route_to_coordinator_lane_is_refused(self) -> None:
        plan = plan_review_return(_ROUND, ISSUE, "20", _owner(lane_id=COORDINATOR_LANE))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_SELF_ROUTE)

    def test_blank_gateway_receiver_is_refused(self) -> None:
        plan = plan_review_return(_ROUND, ISSUE, "20", _owner(gateway_receiver=""))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NO_GATEWAY)

    def test_blank_owning_generation_is_refused(self) -> None:
        plan = plan_review_return(_ROUND, ISSUE, "20", _owner(generation=""))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_BLANK_GENERATION)

    def test_plan_review_returns_collapses_to_the_single_latest(self) -> None:
        markers = [_review_result("20"), _review_result("35"), _review_request("10")]
        plans = plan_review_returns(markers, ISSUE, _owner())
        emitted = [p for p in plans if p.emit]
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].review_journal, "35")

    def test_cross_issue_review_result_is_not_this_issues(self) -> None:
        # A review_result on a different issue must never be returned for ISSUE.
        markers = [_review_result("20", issue="99999")]
        plan = plan_review_return(markers, ISSUE, "20", _owner())
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_NOT_REVIEW_RESULT)


class GenerationFenceTest(unittest.TestCase):
    """Redmine #13974: the dispatch-anchor generation fence for review_return discovery + send."""

    #: The #13974 repro round: an OLD generation's review_request (j10) / review_result (j20) that is
    #: STILL the newest review MARKER on the issue, even though a NEW generation opened its dispatch
    #: anchor at j100 (a higher journal — Redmine ids are monotonic). The latest-review fence alone
    #: (comparing review markers to each other) passes j20, so only the generation fence catches it.
    _OLD_ROUND = [_review_request("10"), _review_result("20")]

    def test_within_generation_truth_table(self) -> None:
        # request >= anchor -> current generation.
        self.assertTrue(review_round_within_generation("100", "100"))
        self.assertTrue(review_round_within_generation("101", "100"))
        # request < anchor -> previous generation.
        self.assertFalse(review_round_within_generation("10", "100"))
        # fail-closed: blank / non-numeric anchor or request is never "within".
        self.assertFalse(review_round_within_generation("10", ""))
        self.assertFalse(review_round_within_generation("", "100"))
        self.assertFalse(review_round_within_generation("abc", "100"))
        self.assertFalse(review_round_within_generation("10", "xyz"))

    def test_previous_generation_review_is_refused_at_discovery(self) -> None:
        # The core #13974 repro: j20 is the latest review marker, but its round (j10) predates the
        # current generation's dispatch anchor (j100) -> refused, NOT retargeted onto the new lane.
        plan = plan_review_return(self._OLD_ROUND, ISSUE, "20", _owner(), dispatch_anchor_journal="100")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_PREVIOUS_GENERATION)

    def test_current_generation_review_still_emits(self) -> None:
        # A review round produced UNDER the current generation (request j110 >= anchor j100) emits.
        markers = [_review_request("110"), _review_result("120")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertTrue(plan.emit)
        self.assertEqual(plan.reason, RETURN_OK)
        self.assertEqual(plan.review_request_journal, "110")

    def test_request_exactly_at_anchor_is_current(self) -> None:
        # Boundary: a request journal equal to the anchor is part of the current generation.
        markers = [_review_request("100"), _review_result("120")]
        self.assertTrue(
            plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100").emit
        )

    def test_unresolvable_anchor_under_fence_is_fail_closed(self) -> None:
        # A supplied-but-blank anchor (fenced mode, generation unpinnable) fails closed.
        plan = plan_review_return(self._OLD_ROUND, ISSUE, "20", _owner(), dispatch_anchor_journal="")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_PREVIOUS_GENERATION)

    def test_none_anchor_is_unfenced_behavior_unchanged(self) -> None:
        # The pre-#13974 unfenced caller (anchor=None) still emits the old round — behavior unchanged.
        self.assertTrue(plan_review_return(self._OLD_ROUND, ISSUE, "20", _owner()).emit)
        self.assertTrue(
            plan_review_return(self._OLD_ROUND, ISSUE, "20", _owner(), dispatch_anchor_journal=None).emit
        )

    def test_plan_review_returns_threads_the_anchor(self) -> None:
        plans = plan_review_returns(self._OLD_ROUND, ISSUE, _owner(), dispatch_anchor_journal="100")
        self.assertEqual([p.emit for p in plans], [False])
        self.assertEqual(plans[0].reason, RETURN_PREVIOUS_GENERATION)


class _Row:
    """The minimal outbox-row shape the send-edge fence reads (route + payload)."""

    def __init__(self, route: str, payload: str = "") -> None:
        self.callback_route = route
        self.payload = payload


class ReviewReturnSendEdgeFenceTest(unittest.TestCase):
    """Redmine #13974: the terminal send-edge fence for pre-existing review_return backlog rows."""

    def _row(self, request_journal: str, lane: str = "issue_13684"):
        return _Row(review_return_callback_route(lane), encode_review_return_payload(request_journal))

    def test_previous_generation_row_is_fenced_terminal(self) -> None:
        fence = make_review_return_send_edge_fence("100")
        blocked, reason = fence(self._row("10"))  # round j10 predates anchor j100
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_PREVIOUS_GENERATION)

    def test_current_generation_row_is_exempt(self) -> None:
        # A current-generation row (round >= anchor) is NOT fenced here — the #13684 send authorities
        # own its exactly-once delivery. Fencing it would break exactly-once for the live round.
        fence = make_review_return_send_edge_fence("100")
        blocked, _ = fence(self._row("110"))
        self.assertFalse(blocked)

    def test_coordinator_row_is_exempt(self) -> None:
        fence = make_review_return_send_edge_fence("100")
        blocked, _ = fence(_Row("coordinator", ""))
        self.assertFalse(blocked)

    def test_unresolvable_anchor_fences_every_review_return_row(self) -> None:
        fence = make_review_return_send_edge_fence(None)
        blocked, reason = fence(self._row("110"))  # even a would-be-current row fails closed
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_UNRESOLVED_ANCHOR)

    def test_missing_round_correlation_is_fenced(self) -> None:
        fence = make_review_return_send_edge_fence("100")
        blocked, reason = fence(_Row(review_return_callback_route("issue_13684"), payload=""))
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_NO_CORRELATION)


if __name__ == "__main__":
    unittest.main()
