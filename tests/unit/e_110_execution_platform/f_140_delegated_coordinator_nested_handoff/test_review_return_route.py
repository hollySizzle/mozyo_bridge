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
    RETURN_SELF_ROUTE,
    REVIEW_RETURN_ROUTE_PREFIX,
    OwningLaneBinding,
    correlated_review_request_journal,
    decode_review_return_payload,
    encode_review_return_payload,
    is_review_return_route,
    latest_review_result_journal,
    plan_review_return,
    plan_review_returns,
    review_return_callback_route,
    review_return_is_current,
)

ISSUE = "13684"


def _review_result(journal: str, *, issue: str = ISSUE, conclusion: str = "approved"):
    return build_marker(issue, journal, "review_result", review_conclusion=conclusion)


def _review_request(journal: str, *, issue: str = ISSUE):
    return build_marker(issue, journal, "review_request")


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
        # The recorded correlation drifted from the current one -> stale.
        self.assertFalse(review_return_is_current(markers, ISSUE, "20", "7"))
        # An uncorrelated result (no request) is not current.
        self.assertFalse(review_return_is_current([_review_result("20")], ISSUE, "20", ""))

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


if __name__ == "__main__":
    unittest.main()
