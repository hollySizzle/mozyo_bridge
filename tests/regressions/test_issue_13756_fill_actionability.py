"""Regression pins for #13756 fill actionability / execution-surface taxonomy.

Two defects, both of which let the fill gate report something that was not true.

**Defect 1 (#13756 description).** The lane vocabulary had only a state class, so
``review_waiting`` / ``blocked`` were unconditionally coordinator-blocking. On
2026-07-13 that stopped an independent, ready #13682 dispatch while #13441's US audit
was running on a *dedicated gateway* (a duplicate main-coordinator review was forbidden)
and #13734 was waiting on an *external supersede condition* (the main coordinator had no
action available). Capacity remained and the work was independent; the pipeline
serialized anyway. ``ActionabilityRegressionTest`` pins the five cases the issue names.

**Defect 2 (#13756 j#78320).** ``lane`` was a free-form label, so when a coordinator hit
a real dispatch blocker it substituted internal parallel task agents and narrated them as
lanes. ``ExecutionSurfaceRegressionTest`` pins the six cases j#78320 requires: an
internal task agent is never a sublane, an ACK is never a worker, an unverifiable claim
fails closed, and an unavailable actuation rail returns a fixed blocked result instead of
a substitution.

Both defects share one fail-closed rule, pinned here rather than assumed: a lane may only
be treated as *not* the main coordinator's problem when it can prove it — verified
managed-sublane provenance, a confirmed delivery, a durable callback expectation, and a
callback that is not overdue. Every partial claim degrades to coordinator-blocking.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ACTIONABILITY_COORDINATOR_ACTIONABLE,
    ACTIONABILITY_DELEGATED_IN_FLIGHT,
    ACTIONABILITY_NON_ACTIONABLE_WAIT,
    DELIVERY_FAILED,
    DELIVERY_NOT_ATTEMPTED,
    DELIVERY_SENT,
    OWNER_DEDICATED_GATEWAY,
    OWNER_EXTERNAL_CONDITION,
    OWNER_MAIN_COORDINATOR,
    OWNER_UNKNOWN,
    REASON_CALLBACK_OVERDUE,
    REASON_DELIVERY_FAILED,
    REASON_MAIN_OWNED_STATE,
    REASON_SURFACE_NOT_VERIFIED,
    ActionabilityClaim,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (
    DISPATCH_ACK_GATEWAY_ACKED,
    DISPATCH_ACK_NONE,
    DISPATCH_ACK_WORKER_CONFIRMED,
    SURFACE_INTERNAL_TASK_AGENT,
    SURFACE_MANAGED_SUBLANE,
    LaneProvenance,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FILL_DISPATCH_NEXT,
    FILL_STOP_ACTUATION_UNAVAILABLE,
    FILL_STOP_COORDINATOR_BLOCKING,
    FILL_STOP_UNVERIFIED_SURFACE,
    FILL_STOP_OWNER_OR_RELEASE_GATE,
    FILL_STOP_SOFT_PROFILE_FULL,
    LANE_STATE_BLOCKED,
    LANE_STATE_IMPLEMENTING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_REVIEW_WAITING,
    FillDecisionInputs,
    LaneState,
    evaluate_fill_decision,
)


def _provenance(**overrides) -> LaneProvenance:
    """Provenance that verifies as a managed sublane unless a field is knocked out."""
    base = dict(
        execution_surface=SURFACE_MANAGED_SUBLANE,
        workspace="w19",
        lane="issue_13441_provider_registry",
        issue_generation="1",
        lifecycle_revision="3",
        durable_anchor="13441#77503",
        gateway_identity="w19:p1",
        worker_identity="w19:p2",
        dispatch_ack=DISPATCH_ACK_WORKER_CONFIRMED,
    )
    base.update(overrides)
    return LaneProvenance(**base)


def _delegated_claim(**overrides) -> ActionabilityClaim:
    """A `delegated_in_flight` claim that verifies unless a field is knocked out."""
    base = dict(
        actionability=ACTIONABILITY_DELEGATED_IN_FLIGHT,
        next_action_owner=OWNER_DEDICATED_GATEWAY,
        delivery_state=DELIVERY_SENT,
        callback_expected=True,
        callback_overdue=False,
    )
    base.update(overrides)
    return ActionabilityClaim(**base)


def _delegated_review_lane(issue="13441", **claim_overrides) -> LaneState:
    """#13441's US audit: review_request delivered to the dedicated same-lane gateway."""
    return LaneState(
        issue=issue,
        state_class=LANE_STATE_REVIEW_WAITING,
        claim=_delegated_claim(**claim_overrides),
        provenance=_provenance(),
    )


def _external_wait_lane(issue="13734") -> LaneState:
    """#13734's tiered CI: waiting on an external supersede safety condition."""
    return LaneState(
        issue=issue,
        state_class=LANE_STATE_BLOCKED,
        claim=ActionabilityClaim(
            actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
            next_action_owner=OWNER_EXTERNAL_CONDITION,
            unblock_condition="supersede safety precondition: predecessor lane idle",
        ),
        provenance=_provenance(lane="issue_13734_tiered_ci"),
    )


def _inputs(lanes=(), **overrides) -> FillDecisionInputs:
    base = dict(
        lanes=tuple(lanes),
        ready_independent_work=1,
        ready_overlapping_work=0,
        capacity_remaining=2,
        owner_or_release_gate_active=False,
    )
    base.update(overrides)
    return FillDecisionInputs(**base)


class ActionabilityRegressionTest(unittest.TestCase):
    """The five regression cases named in the #13756 description."""

    def test_case1_delegated_review_with_independent_ready_work_dispatches(self):
        # review_request sent to the same-lane gateway, next_owner=gateway, a main
        # duplicate review is forbidden, independent work is ready, capacity remains.
        out = evaluate_fill_decision(_inputs(lanes=[_delegated_review_lane()]))
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)
        self.assertEqual(out.coordinator_blocking, ())
        self.assertEqual(out.delegated_in_flight, ("13441",))

    def test_case1_still_occupies_capacity(self):
        # `delegated_in_flight` is not a free pass: the lane is still resident, so the
        # hard cap still counts it and can still stop the dispatch.
        out = evaluate_fill_decision(
            _inputs(lanes=[_delegated_review_lane()], sublane_hard_cap=1)
        )
        self.assertEqual(out.fill_decision, FILL_STOP_SOFT_PROFILE_FULL)
        self.assertEqual(out.capacity_projection.resident_managed_sublanes, 1)

    def test_case2_undelivered_review_request_stops(self):
        # The review_request was never delivered: the send is still the coordinator's.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_delegated_review_lane(delivery_state=DELIVERY_NOT_ATTEMPTED)]
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(out.coordinator_blocking, ("13441",))

    def test_case2_main_owned_review_stops(self):
        # The review is owned by the main coordinator: it is coordinator debt, whatever
        # label it carries.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    _delegated_review_lane(next_action_owner=OWNER_MAIN_COORDINATOR)
                ]
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_case3_delegated_callback_overdue_stops(self):
        lane = _delegated_review_lane(callback_overdue=True)
        self.assertEqual(lane.verdict().reason, REASON_CALLBACK_OVERDUE)
        out = evaluate_fill_decision(_inputs(lanes=[lane]))
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(out.delegated_in_flight, ())

    def test_case3_delivery_failed_stops(self):
        lane = _delegated_review_lane(delivery_state=DELIVERY_FAILED)
        self.assertEqual(lane.verdict().reason, REASON_DELIVERY_FAILED)
        out = evaluate_fill_decision(_inputs(lanes=[lane]))
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_case3_ack_alone_is_not_completion(self):
        # Delivered, but no durable callback is expected back: there is no completion
        # signal, so the coordinator still owns the lane.
        out = evaluate_fill_decision(
            _inputs(lanes=[_delegated_review_lane(callback_expected=False)])
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_case4_external_wait_does_not_stop_independent_work(self):
        # The supersede lane waits on an external idle condition and no recovery has been
        # dispatched; independent work must still be dispatchable.
        out = evaluate_fill_decision(_inputs(lanes=[_external_wait_lane()]))
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)
        self.assertEqual(out.non_actionable_wait, ("13734",))

    def test_case4_wait_without_durable_unblock_condition_stops(self):
        # An unfalsifiable wait — nothing durable says what would end it.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    LaneState(
                        issue="13734",
                        state_class=LANE_STATE_BLOCKED,
                        claim=ActionabilityClaim(
                            actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
                            next_action_owner=OWNER_EXTERNAL_CONDITION,
                        ),
                        provenance=_provenance(),
                    )
                ]
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_case5_owner_or_release_gate_always_stops(self):
        # Even with every lane verifiably delegated / externally waiting.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_delegated_review_lane(), _external_wait_lane()],
                owner_or_release_gate_active=True,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_OWNER_OR_RELEASE_GATE)

    def test_the_2026_07_13_incident_lane_set_dispatches(self):
        # The exact incident: #13441 delegated to a gateway, #13734 waiting on an
        # external condition, #13682 independent and ready, capacity remaining.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    _delegated_review_lane("13441"),
                    _external_wait_lane("13734"),
                ],
                ready_independent_work=1,
                capacity_remaining=8,
                sublane_hard_cap=10,
            )
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)

    def test_unknown_owner_fails_closed(self):
        out = evaluate_fill_decision(
            _inputs(lanes=[_delegated_review_lane(next_action_owner=OWNER_UNKNOWN)])
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_main_owned_states_are_never_delegable(self):
        # integration_waiting is coordinator authority by construction: a perfectly
        # formed delegation claim must not move it.
        lane = LaneState(
            issue="13682",
            state_class=LANE_STATE_INTEGRATION_WAITING,
            claim=_delegated_claim(),
            provenance=_provenance(),
        )
        self.assertEqual(lane.verdict().reason, REASON_MAIN_OWNED_STATE)
        self.assertEqual(
            lane.verdict().actionability, ACTIONABILITY_COORDINATOR_ACTIONABLE
        )
        out = evaluate_fill_decision(_inputs(lanes=[lane]))
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_legacy_lane_state_is_unchanged(self):
        # The pre-#13756 constructor: a blocking state still blocks, an implementing lane
        # still dispatches. The compatibility contract for `--lane ISSUE:STATE`.
        blocked = evaluate_fill_decision(
            _inputs(lanes=[LaneState(issue="1", state_class=LANE_STATE_REVIEW_WAITING)])
        )
        self.assertEqual(blocked.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        dispatch = evaluate_fill_decision(
            _inputs(lanes=[LaneState(issue="1", state_class=LANE_STATE_IMPLEMENTING)])
        )
        self.assertEqual(dispatch.fill_decision, FILL_DISPATCH_NEXT)


class ExecutionSurfaceRegressionTest(unittest.TestCase):
    """The six regression cases required by #13756 j#78320."""

    def test_task_agents_present_with_zero_managed_sublanes(self):
        # The incident: internal task agents narrated as lanes. They are counted
        # separately and never as sublanes — resident stays 0.
        lanes = [
            LaneState(
                issue=str(issue),
                state_class=LANE_STATE_IMPLEMENTING,
                provenance=LaneProvenance(
                    execution_surface=SURFACE_INTERNAL_TASK_AGENT
                ),
            )
            for issue in (1, 2, 3)
        ]
        out = evaluate_fill_decision(_inputs(lanes=lanes))
        projection = out.capacity_projection
        self.assertEqual(projection.resident_managed_sublanes, 0)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 0)
        self.assertEqual(projection.internal_task_agents, 3)

    def test_task_agents_neither_consume_nor_fill_sublane_capacity(self):
        # They cannot claim the pipeline is full (cap untouched)...
        agents = [
            LaneState(
                issue=str(issue),
                state_class=LANE_STATE_IMPLEMENTING,
                provenance=LaneProvenance(
                    execution_surface=SURFACE_INTERNAL_TASK_AGENT
                ),
            )
            for issue in range(12)
        ]
        out = evaluate_fill_decision(
            _inputs(lanes=agents, capacity_remaining=10, sublane_hard_cap=10)
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)
        self.assertEqual(out.capacity_remaining, 10)
        # ...nor can they be presented as productive sublanes.
        self.assertEqual(
            out.capacity_projection.worker_confirmed_productive_sublanes, 0
        )

    def test_task_agent_cannot_claim_a_delegated_review(self):
        # A task agent has no dedicated gateway to delegate to: the claim fails closed.
        lane = LaneState(
            issue="13441",
            state_class=LANE_STATE_REVIEW_WAITING,
            claim=_delegated_claim(),
            provenance=LaneProvenance(execution_surface=SURFACE_INTERNAL_TASK_AGENT),
        )
        self.assertEqual(lane.verdict().reason, REASON_SURFACE_NOT_VERIFIED)
        out = evaluate_fill_decision(_inputs(lanes=[lane]))
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_live_pair_without_dispatch_is_not_productive(self):
        # A live pair that has not been dispatched: the pair is named (that is what makes
        # it a resident managed sublane), only the dispatch has not happened.
        lane = LaneState(
            issue="13756",
            state_class=LANE_STATE_IMPLEMENTING,
            provenance=_provenance(dispatch_ack=DISPATCH_ACK_NONE),
        )
        projection = evaluate_fill_decision(_inputs(lanes=[lane])).capacity_projection
        self.assertEqual(projection.resident_managed_sublanes, 1)
        self.assertEqual(projection.gateway_dispatched_sublanes, 0)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 0)
        self.assertEqual(projection.blocked_or_undispatched_sublanes, 1)

    def test_gateway_ack_without_worker_ack_is_not_productive(self):
        # The exact shape of this lane's own j#78365: gateway acked, worker never
        # confirmed. The pair is still named; it must not read as productive work.
        lane = LaneState(
            issue="13756",
            state_class=LANE_STATE_IMPLEMENTING,
            provenance=_provenance(dispatch_ack=DISPATCH_ACK_GATEWAY_ACKED),
        )
        projection = evaluate_fill_decision(_inputs(lanes=[lane])).capacity_projection
        self.assertEqual(projection.gateway_dispatched_sublanes, 1)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 0)
        self.assertEqual(projection.blocked_or_undispatched_sublanes, 1)

    def test_worker_confirmed_but_blocked_is_not_productive(self):
        # A worker-confirmed lane that owes the coordinator a review is resident, but it
        # is not "work moving".
        lane = LaneState(issue="13756", state_class=LANE_STATE_REVIEW_WAITING,
                         provenance=_provenance())
        projection = evaluate_fill_decision(_inputs(lanes=[lane])).capacity_projection
        self.assertEqual(projection.resident_managed_sublanes, 1)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 0)
        self.assertEqual(projection.blocked_or_undispatched_sublanes, 1)

    def test_hibernated_or_detached_lane_is_not_a_sublane(self):
        # A bare worktree with no lifecycle identity: not a managed sublane, and it
        # cannot delegate.
        lane = LaneState(
            issue="13756",
            state_class=LANE_STATE_REVIEW_WAITING,
            claim=_delegated_claim(),
            provenance=LaneProvenance(
                execution_surface="detached_worktree", lane="issue_13756"
            ),
        )
        out = evaluate_fill_decision(_inputs(lanes=[lane]))
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(out.capacity_projection.resident_managed_sublanes, 0)
        self.assertEqual(out.capacity_projection.other_surface, 1)

    def test_unverifiable_managed_sublane_claim_fails_closed(self):
        # Claims `managed_sublane` but cannot name its generation / revision / anchor /
        # pair: it is counted as unverified, never as a sublane, its delegation is
        # refused, and the unverifiable surface fails the whole lane set closed
        # (Review j#78471 finding 1).
        lane = LaneState(
            issue="13756",
            state_class=LANE_STATE_REVIEW_WAITING,
            claim=_delegated_claim(),
            provenance=LaneProvenance(
                execution_surface=SURFACE_MANAGED_SUBLANE,
                workspace="w19",
                lane="issue_13756",
            ),
        )
        out = evaluate_fill_decision(_inputs(lanes=[lane]))
        self.assertEqual(out.fill_decision, FILL_STOP_UNVERIFIED_SURFACE)
        self.assertEqual(out.capacity_projection.resident_managed_sublanes, 0)
        self.assertEqual(out.capacity_projection.unverified_surface, 1)

    def test_free_form_surface_fails_closed(self):
        # A free-form "lane" label — the exact j#78320 incident shape — fails the lane
        # set closed rather than dispatching over it (Review j#78471 finding 1).
        lane = LaneState(
            issue="13756",
            state_class=LANE_STATE_REVIEW_WAITING,
            claim=_delegated_claim(),
            provenance=LaneProvenance(execution_surface="parallel lane (task agent)"),
        )
        out = evaluate_fill_decision(_inputs(lanes=[lane]))
        self.assertEqual(out.fill_decision, FILL_STOP_UNVERIFIED_SURFACE)
        self.assertEqual(out.capacity_projection.unverified_surface, 1)

    def test_free_form_surface_on_an_implementing_lane_still_fails_closed(self):
        # Finding 1's exact probe: an implementing lane (not itself blocking) with a
        # free-form surface must NOT dispatch_next — the unverifiable surface stops it.
        lane = LaneState(
            issue="1",
            state_class=LANE_STATE_IMPLEMENTING,
            provenance=LaneProvenance(execution_surface="free-form lane"),
        )
        out = evaluate_fill_decision(
            _inputs(
                lanes=[lane],
                ready_independent_work=1,
                capacity_remaining=10,
                sublane_hard_cap=10,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_UNVERIFIED_SURFACE)
        self.assertFalse(out.should_dispatch)

    def test_legacy_unspecified_surface_does_not_trigger_unverified_stop(self):
        # The compatibility boundary: a legacy `--lane ISSUE:STATE` makes no surface
        # claim (unspecified), which must NOT be treated as unverified.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[LaneState(issue="1", state_class=LANE_STATE_IMPLEMENTING)],
                ready_independent_work=1,
                capacity_remaining=2,
            )
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)
        self.assertEqual(out.capacity_projection.unverified_surface, 0)

    def test_duplicate_recovery_lane_is_counted_once_per_generation(self):
        # A superseded lane and its recovery lane are distinct generations of the same
        # issue: both are resident, so the cap sees the real occupancy.
        superseded = LaneState(
            issue="13756",
            state_class=LANE_STATE_BLOCKED,
            provenance=_provenance(issue_generation="1", lifecycle_revision="2"),
        )
        recovery = LaneState(
            issue="13756",
            state_class=LANE_STATE_IMPLEMENTING,
            provenance=_provenance(issue_generation="2", lifecycle_revision="5"),
        )
        projection = evaluate_fill_decision(
            _inputs(lanes=[superseded, recovery])
        ).capacity_projection
        self.assertEqual(projection.resident_managed_sublanes, 2)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 1)

    def test_capacity_optimization_counts_only_verified_sublanes(self):
        # cap 10 with 9 real sublanes + 4 task agents: one slot remains, and the task
        # agents do not steal it.
        lanes = [
            LaneState(
                issue=f"real{i}",
                state_class=LANE_STATE_IMPLEMENTING,
                provenance=_provenance(lane=f"issue_real{i}"),
            )
            for i in range(9)
        ] + [
            LaneState(
                issue=f"agent{i}",
                state_class=LANE_STATE_IMPLEMENTING,
                provenance=LaneProvenance(
                    execution_surface=SURFACE_INTERNAL_TASK_AGENT
                ),
            )
            for i in range(4)
        ]
        out = evaluate_fill_decision(
            _inputs(lanes=lanes, capacity_remaining=5, sublane_hard_cap=10)
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)
        self.assertEqual(out.capacity_remaining, 1)
        self.assertEqual(out.capacity_projection.resident_managed_sublanes, 9)

    def test_capacity_hard_cap_stops_at_ten_verified_sublanes(self):
        lanes = [
            LaneState(
                issue=f"real{i}",
                state_class=LANE_STATE_IMPLEMENTING,
                provenance=_provenance(lane=f"issue_real{i}"),
            )
            for i in range(10)
        ]
        out = evaluate_fill_decision(
            _inputs(lanes=lanes, capacity_remaining=5, sublane_hard_cap=10)
        )
        self.assertEqual(out.fill_decision, FILL_STOP_SOFT_PROFILE_FULL)

    def test_unavailable_actuation_returns_fixed_blocked_result(self):
        # j#78320 item 4: when the high-level rail is unavailable the answer is a fixed
        # blocked result — never "dispatch something else instead".
        out = evaluate_fill_decision(
            _inputs(
                lanes=[],
                ready_independent_work=5,
                capacity_remaining=10,
                managed_sublane_actuation_available=False,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_ACTUATION_UNAVAILABLE)
        self.assertFalse(out.should_dispatch)

    def test_owner_or_release_gate_outranks_unavailable_actuation(self):
        out = evaluate_fill_decision(
            _inputs(
                managed_sublane_actuation_available=False,
                owner_or_release_gate_active=True,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_OWNER_OR_RELEASE_GATE)


if __name__ == "__main__":
    unittest.main()
