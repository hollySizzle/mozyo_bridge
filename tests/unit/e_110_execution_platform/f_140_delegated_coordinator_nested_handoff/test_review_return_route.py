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
    RETURN_MALFORMED_REVIEW_HEAD,
    RETURN_MISSING_REVIEW_HEAD,
    RETURN_OK,
    RETURN_PREVIOUS_GENERATION,
    RETURN_REVIEW_HEAD_DRIFT,
    RETURN_REVIEW_REQUEST_UNCONFIRMED,
    RETURN_SELF_ROUTE,
    REVIEW_RETURN_FENCE_HEAD_DRIFT,
    REVIEW_RETURN_FENCE_HEAD_UNCONFIRMED,
    REVIEW_RETURN_FENCE_MALFORMED_HEAD,
    REVIEW_RETURN_FENCE_NO_CORRELATION,
    REVIEW_RETURN_FENCE_PREVIOUS_GENERATION,
    REVIEW_RETURN_FENCE_REQ_UNCONFIRMED,
    REVIEW_RETURN_FENCE_UNRESOLVED_ANCHOR,
    REVIEW_RETURN_ROUTE_PREFIX,
    OwningLaneBinding,
    correlated_review_request_journal,
    current_review_generation_head,
    current_review_generation_request,
    decode_review_return_payload,
    decode_review_return_target_head,
    encode_review_return_payload,
    is_full_commit_head,
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
#: Well-formed FULL commit heads (40 hex) for the v2 head fence (#13974 / j#81487 F1).
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _review_result(
    journal: str, *, issue: str = ISSUE, conclusion: str = "approved", head: str = "", req: str = ""
):
    return build_marker(
        issue, journal, "review_result", review_conclusion=conclusion,
        target_head=head, review_request_journal=req,
    )


def _review_request(journal: str, *, issue: str = ISSUE, head: str = ""):
    return build_marker(issue, journal, "review_request", target_head=head)


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
        # The live review_result marker DECLARES its req (j#81496 F1) — the action identity authority.
        markers = [_review_request("10"), _review_result("20", req="10")]
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
        # j#81496 F1: the LIVE marker declares no req -> fail-closed even if the recorded req matches.
        self.assertFalse(review_return_is_current([_review_request("10"), _review_result("20")], ISSUE, "20", "10"))
        # j#81496 F1: the LIVE marker's declared req drifted from the correlated request -> stale.
        self.assertFalse(review_return_is_current([_review_request("10"), _review_result("20", req="7")], ISSUE, "20", "10"))
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
        # A review round produced UNDER the current generation (request j110 >= anchor j100) emits,
        # carrying the reviewed head (both request + result marker heads agree, result declares its
        # req; #13974 / j#81454 A + j#81487 F1).
        markers = [_review_request("110", head=HEAD_A), _review_result("120", head=HEAD_A, req="110")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertTrue(plan.emit)
        self.assertEqual(plan.reason, RETURN_OK)
        self.assertEqual(plan.review_request_journal, "110")
        self.assertEqual(plan.target_head, HEAD_A)

    def test_request_exactly_at_anchor_is_current(self) -> None:
        # Boundary: a request journal equal to the anchor is part of the current generation.
        markers = [_review_request("100", head=HEAD_A), _review_result("120", head=HEAD_A, req="100")]
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


class ReviewHeadFenceTest(unittest.TestCase):
    """Redmine #13974 / j#81454 A + j#81487 F1: the review-gate v2 head + req conjunction at discovery."""

    def test_missing_head_on_result_is_fail_closed(self) -> None:
        # Current-generation round, but the review_result marker carries no head -> fail-closed.
        markers = [_review_request("110", head=HEAD_A), _review_result("120", head="", req="110")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_MISSING_REVIEW_HEAD)

    def test_missing_head_on_request_is_fail_closed(self) -> None:
        markers = [_review_request("110", head=""), _review_result("120", head=HEAD_A, req="110")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_MISSING_REVIEW_HEAD)

    def test_head_drift_between_request_and_result_is_refused(self) -> None:
        # The result reviewed a DIFFERENT head than the request pinned -> drift -> refused.
        markers = [_review_request("110", head=HEAD_A), _review_result("120", head=HEAD_B, req="110")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_REVIEW_HEAD_DRIFT)

    def test_malformed_head_is_refused(self) -> None:
        # j#81487 F1: a matching but NON-full-hex head (truncated / not a full SHA) is malformed.
        markers = [_review_request("110", head="not-a-full-sha"), _review_result("120", head="not-a-full-sha", req="110")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_MALFORMED_REVIEW_HEAD)

    def test_missing_declared_req_on_result_is_refused(self) -> None:
        # j#81487 F1: the review_result marker must DECLARE the req it answers, even if derivable.
        markers = [_review_request("110", head=HEAD_A), _review_result("120", head=HEAD_A, req="")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_REVIEW_REQUEST_UNCONFIRMED)

    def test_mismatched_declared_req_is_refused(self) -> None:
        # j#81487 F1: a declared req that does NOT match the provider-correlated request is fail-closed
        # (never silently re-derived as a substitute). The correlated request is j110 (latest < 120).
        markers = [_review_request("110", head=HEAD_A), _review_result("120", head=HEAD_A, req="999")]
        plan = plan_review_return(markers, ISSUE, "120", _owner(), dispatch_anchor_journal="100")
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, RETURN_REVIEW_REQUEST_UNCONFIRMED)

    def test_head_fence_skipped_when_unfenced(self) -> None:
        # An unfenced caller (anchor=None) does not enforce the v2 head/req dimension (unchanged).
        markers = [_review_request("110", head=""), _review_result("120", head="", req="")]
        self.assertTrue(plan_review_return(markers, ISSUE, "120", _owner()).emit)

    def test_full_commit_head_truth_table(self) -> None:
        self.assertTrue(is_full_commit_head("a" * 40))
        self.assertTrue(is_full_commit_head("0" * 64))
        self.assertFalse(is_full_commit_head("a" * 39))  # truncated
        self.assertFalse(is_full_commit_head("A" * 40))  # upper-case not accepted
        self.assertFalse(is_full_commit_head("g" * 40))  # non-hex
        self.assertFalse(is_full_commit_head(""))

    def test_current_review_generation_head_is_latest_request_head(self) -> None:
        markers = [_review_request("110", head=HEAD_A), _review_request("130", head=HEAD_B)]
        self.assertEqual(current_review_generation_head(markers, ISSUE), HEAD_B)
        self.assertEqual(current_review_generation_head([], ISSUE), "")

    def test_current_review_generation_request_is_live_declared_req(self) -> None:
        # j#81496 F1: the live authoritative req is the latest review_result's DECLARED req when it
        # equals its correlated request.
        markers = [_review_request("110", head=HEAD_A), _review_result("120", head=HEAD_A, req="110")]
        self.assertEqual(current_review_generation_request(markers, ISSUE), "110")
        # A live marker declaring no req -> fail-closed blank.
        m2 = [_review_request("110", head=HEAD_A), _review_result("120", head=HEAD_A, req="")]
        self.assertEqual(current_review_generation_request(m2, ISSUE), "")
        # A live marker whose declared req drifted from the correlated request -> fail-closed blank.
        m3 = [_review_request("110", head=HEAD_A), _review_result("120", head=HEAD_A, req="999")]
        self.assertEqual(current_review_generation_request(m3, ISSUE), "")
        self.assertEqual(current_review_generation_request([], ISSUE), "")


class _Row:
    """The minimal outbox-row shape the send-edge fence reads (route + payload)."""

    def __init__(self, route: str, payload: str = "") -> None:
        self.callback_route = route
        self.payload = payload


class ReviewReturnSendEdgeFenceTest(unittest.TestCase):
    """Redmine #13974 / j#81454 A: terminal send-edge fence (generation + head) for backlog rows."""

    def _fence(self, anchor="100", head=HEAD_A, req="110"):
        return make_review_return_send_edge_fence(anchor, head, req)

    def _row(self, request_journal: str, head: str = HEAD_A, lane: str = "issue_13684"):
        return _Row(
            review_return_callback_route(lane),
            encode_review_return_payload(request_journal, head),
        )

    def test_previous_generation_row_is_fenced_terminal(self) -> None:
        blocked, reason = self._fence()(self._row("10"))  # round j10 predates anchor j100
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_PREVIOUS_GENERATION)

    def test_current_generation_current_head_and_req_row_is_exempt(self) -> None:
        # Current generation (round >= anchor) AND head + req match the current review generation ->
        # NOT fenced; the #13684 send authorities own its exactly-once delivery.
        blocked, _ = self._fence(head=HEAD_A, req="110")(self._row("110", head=HEAD_A))
        self.assertFalse(blocked)

    def test_current_generation_req_drift_row_is_fenced_terminal(self) -> None:
        # j#81496 F1: the recorded req drifted from the current live request -> fenced terminal.
        blocked, reason = self._fence(req="999")(self._row("110", head=HEAD_A))
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_REQ_UNCONFIRMED)

    def test_unresolvable_current_req_fences_current_generation_row(self) -> None:
        # j#81496 F1: a blank current review request (live marker req missing/drifted) fails closed.
        blocked, reason = self._fence(req="")(self._row("110", head=HEAD_A))
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_REQ_UNCONFIRMED)

    def test_current_generation_head_drift_row_is_fenced_terminal(self) -> None:
        # Current generation round + req but the recorded head drifted from the current head -> fenced.
        blocked, reason = self._fence(head=HEAD_B, req="110")(self._row("110", head=HEAD_A))
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_HEAD_DRIFT)

    def test_malformed_recorded_head_is_fenced_terminal(self) -> None:
        # j#81487 F1: a non-full-hex recorded head is fenced at the send edge (action-time terminal).
        blocked, reason = self._fence()(self._row("110", head="deadbeef"))  # abbreviated, not full
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_MALFORMED_HEAD)

    def test_missing_recorded_head_is_fenced_terminal(self) -> None:
        # A legacy row with no recorded head (pre-#13974 payload) fails closed at the current generation.
        row = _Row(review_return_callback_route("issue_13684"), encode_review_return_payload("110"))
        blocked, reason = self._fence()(row)
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_HEAD_UNCONFIRMED)

    def test_unresolvable_current_head_fences_current_generation_row(self) -> None:
        # A blank current review head (head-less generation) fails a would-be-current row closed.
        blocked, reason = self._fence(head="")(self._row("110", head=HEAD_A))
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_HEAD_UNCONFIRMED)

    def test_coordinator_row_is_exempt(self) -> None:
        blocked, _ = self._fence()(_Row("coordinator", ""))
        self.assertFalse(blocked)

    def test_unresolvable_anchor_fences_every_review_return_row(self) -> None:
        blocked, reason = make_review_return_send_edge_fence(None, HEAD_A, "110")(self._row("110"))
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_UNRESOLVED_ANCHOR)

    def test_missing_round_correlation_is_fenced(self) -> None:
        blocked, reason = self._fence()(_Row(review_return_callback_route("issue_13684"), payload=""))
        self.assertTrue(blocked)
        self.assertEqual(reason, REVIEW_RETURN_FENCE_NO_CORRELATION)

    def test_payload_round_trips_the_head(self) -> None:
        p = encode_review_return_payload("110", HEAD_A)
        self.assertEqual(decode_review_return_payload(p), "110")
        self.assertEqual(decode_review_return_target_head(p), HEAD_A)
        # legacy head-less payload decodes to a blank head (fail-closed at the fence).
        self.assertEqual(decode_review_return_target_head(encode_review_return_payload("110")), "")


if __name__ == "__main__":
    unittest.main()
