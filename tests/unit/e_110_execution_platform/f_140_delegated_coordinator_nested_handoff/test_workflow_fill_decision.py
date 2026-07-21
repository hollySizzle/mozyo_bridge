"""Advisory Post-Dispatch Fill Loop policy tests (Redmine #12855).

Pins the pure :func:`evaluate_fill_decision` policy and the lane-state /
fill-decision vocabulary defined by the coordinator-sublane spine
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### Post-Dispatch
Fill Loop`):

- the fixed decision vocabulary (``dispatch_next`` + the five concrete stop reasons)
  and the coordinator-blocking lane-state classification;
- the single most important invariant — an active ``implementing`` lane is **not** a
  stop reason — so an ``implementing``-only lane set with ready work + capacity
  dispatches;
- the precedence between the stop reasons (owner/release gate > coordinator-blocking >
  overlap/no-ready-work > soft-profile-full);
- that the outcome is always advisory.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ACTIONABILITY_DELEGATED_IN_FLIGHT,
    DELIVERY_SENT,
    OWNER_DEDICATED_GATEWAY,
    ActionabilityClaim,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (
    DISPATCH_ACK_WORKER_CONFIRMED,
    SURFACE_MANAGED_SUBLANE,
    LaneProvenance,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    COORDINATOR_BLOCKING_STATES,
    FILL_DISPATCH_NEXT,
    FILL_STOP_ACTUATION_UNAVAILABLE,
    FILL_STOP_COORDINATOR_BLOCKING,
    FILL_STOP_NO_READY_WORK,
    FILL_STOP_OVERLAP,
    FILL_STOP_OWNER_OR_RELEASE_GATE,
    FILL_STOP_SOFT_PROFILE_FULL,
    LANE_STATE_BLOCKED,
    LANE_STATE_CALLBACK_DUE,
    LANE_STATE_CLOSE_WAITING,
    LANE_STATE_IDLE,
    LANE_STATE_IMPLEMENTING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_RETIRE_READY,
    LANE_STATE_REVIEW_WAITING,
    MAIN_COORDINATOR_OWNED_STATES,
    NEXT_DRAIN_NONE,
    NEXT_DRAIN_OWNER,
    NEXT_DRAIN_RETIREMENT,
    NEXT_DRAIN_REVIEW,
    FillDecisionInputs,
    LaneState,
    evaluate_fill_decision,
    is_coordinator_blocking,
)

# A lane that can legitimately be delegated: a verified managed sublane whose review is
# in flight on its dedicated gateway.
_VERIFIED_SUBLANE = LaneProvenance(
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
_VERIFIED_DELEGATION = ActionabilityClaim(
    actionability=ACTIONABILITY_DELEGATED_IN_FLIGHT,
    next_action_owner=OWNER_DEDICATED_GATEWAY,
    delivery_state=DELIVERY_SENT,
    callback_expected=True,
)


def _inputs(lanes=(), **overrides):
    base = dict(
        lanes=tuple(lanes),
        ready_independent_work=0,
        ready_overlapping_work=0,
        capacity_remaining=0,
        owner_or_release_gate_active=False,
    )
    base.update(overrides)
    return FillDecisionInputs(**base)


def _lane(issue, state):
    return LaneState(issue=issue, state_class=state)


class CoordinatorBlockingClassificationTest(unittest.TestCase):
    def test_implementing_is_not_coordinator_blocking(self):
        self.assertFalse(is_coordinator_blocking(LANE_STATE_IMPLEMENTING))
        self.assertNotIn(LANE_STATE_IMPLEMENTING, COORDINATOR_BLOCKING_STATES)

    def test_retire_ready_and_idle_are_not_coordinator_blocking(self):
        self.assertFalse(is_coordinator_blocking(LANE_STATE_RETIRE_READY))
        self.assertFalse(is_coordinator_blocking(LANE_STATE_IDLE))

    def test_blocking_states_classify_as_blocking(self):
        for state in (
            LANE_STATE_CALLBACK_DUE,
            LANE_STATE_REVIEW_WAITING,
            LANE_STATE_OWNER_WAITING,
            LANE_STATE_INTEGRATION_WAITING,
            LANE_STATE_CLOSE_WAITING,
            LANE_STATE_BLOCKED,
        ):
            self.assertTrue(is_coordinator_blocking(state), state)

    def test_unknown_lane_state_treated_as_blocking(self):
        # A misread / unrecognized state class is conservatively coordinator-blocking.
        self.assertTrue(_lane("9999", "totally_unknown").coordinator_blocking())


class DispatchNextTest(unittest.TestCase):
    def test_implementing_only_with_ready_work_and_capacity_dispatches(self):
        # The core invariant: an active implementing lane alone is not a stop reason.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_lane("12855", LANE_STATE_IMPLEMENTING)],
                ready_independent_work=1,
                capacity_remaining=2,
            )
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)
        self.assertTrue(out.should_dispatch)
        self.assertEqual(out.next_drain_action, NEXT_DRAIN_NONE)
        self.assertEqual(out.active_implementing, ("12855",))
        self.assertEqual(out.coordinator_blocking, ())

    def test_multiple_implementing_lanes_still_dispatch(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    _lane("1", LANE_STATE_IMPLEMENTING),
                    _lane("2", LANE_STATE_IMPLEMENTING),
                ],
                ready_independent_work=3,
                capacity_remaining=1,
            )
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)

    def test_empty_lane_set_with_ready_work_dispatches(self):
        out = evaluate_fill_decision(
            _inputs(ready_independent_work=2, capacity_remaining=3)
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)


class StopReasonTest(unittest.TestCase):
    def test_owner_or_release_gate_wins_over_everything(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_lane("12855", LANE_STATE_IMPLEMENTING)],
                ready_independent_work=5,
                capacity_remaining=5,
                owner_or_release_gate_active=True,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_OWNER_OR_RELEASE_GATE)
        self.assertEqual(out.next_drain_action, NEXT_DRAIN_OWNER)

    def test_coordinator_blocking_stops_even_with_ready_work_and_capacity(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    _lane("12855", LANE_STATE_IMPLEMENTING),
                    _lane("12700", LANE_STATE_REVIEW_WAITING),
                ],
                ready_independent_work=2,
                capacity_remaining=2,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(out.next_drain_action, NEXT_DRAIN_REVIEW)
        self.assertEqual(out.coordinator_blocking, ("12700",))

    def test_owner_waiting_drains_before_review(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    _lane("a", LANE_STATE_REVIEW_WAITING),
                    _lane("b", LANE_STATE_OWNER_WAITING),
                ],
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(out.next_drain_action, NEXT_DRAIN_OWNER)

    def test_no_ready_work_stops(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_lane("12855", LANE_STATE_IMPLEMENTING)],
                ready_independent_work=0,
                capacity_remaining=3,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_NO_READY_WORK)

    def test_overlap_only_ready_work_serializes(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_lane("12855", LANE_STATE_IMPLEMENTING)],
                ready_independent_work=0,
                ready_overlapping_work=2,
                capacity_remaining=3,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_OVERLAP)

    def test_independent_work_dispatches_even_when_overlap_also_present(self):
        out = evaluate_fill_decision(
            _inputs(
                ready_independent_work=1,
                ready_overlapping_work=2,
                capacity_remaining=1,
            )
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)

    def test_soft_profile_full_stops(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_lane("12855", LANE_STATE_IMPLEMENTING)],
                ready_independent_work=2,
                capacity_remaining=0,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_SOFT_PROFILE_FULL)

    def test_retire_ready_lane_sets_retirement_drain_when_stopped(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_lane("done", LANE_STATE_RETIRE_READY)],
                ready_independent_work=0,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_NO_READY_WORK)
        self.assertEqual(out.next_drain_action, NEXT_DRAIN_RETIREMENT)


class ActionabilityIntegrationTest(unittest.TestCase):
    """The #13756 axes as the fill policy sees them (the axis detail is pinned in
    ``test_lane_actionability`` / ``test_lane_execution_surface``)."""

    def test_main_owned_states_are_a_subset_of_the_blocking_states(self):
        # A state that cannot be delegated must be one that blocks in the first place;
        # otherwise the "not delegable" rule would be describing a lane that never stops.
        self.assertLessEqual(MAIN_COORDINATOR_OWNED_STATES, COORDINATOR_BLOCKING_STATES)

    def test_review_waiting_is_delegable_but_owner_waiting_is_not(self):
        self.assertNotIn(LANE_STATE_REVIEW_WAITING, MAIN_COORDINATOR_OWNED_STATES)
        self.assertIn(LANE_STATE_OWNER_WAITING, MAIN_COORDINATOR_OWNED_STATES)
        self.assertIn(LANE_STATE_INTEGRATION_WAITING, MAIN_COORDINATOR_OWNED_STATES)
        self.assertIn(LANE_STATE_CLOSE_WAITING, MAIN_COORDINATOR_OWNED_STATES)

    def test_unknown_state_class_is_main_owned_so_no_claim_rescues_it(self):
        # If the state cannot be read, the claim attached to it cannot be trusted either.
        lane = LaneState(
            issue="9999",
            state_class="totally_unknown",
            claim=_VERIFIED_DELEGATION,
            provenance=_VERIFIED_SUBLANE,
        )
        self.assertTrue(lane.state_is_main_owned())
        self.assertTrue(lane.coordinator_blocking())
        out = evaluate_fill_decision(
            _inputs(lanes=[lane], ready_independent_work=1, capacity_remaining=1)
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)

    def test_delegated_review_lane_is_not_a_stop_reason(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    LaneState(
                        issue="13441",
                        state_class=LANE_STATE_REVIEW_WAITING,
                        claim=_VERIFIED_DELEGATION,
                        provenance=_VERIFIED_SUBLANE,
                    )
                ],
                ready_independent_work=1,
                capacity_remaining=1,
            )
        )
        self.assertEqual(out.fill_decision, FILL_DISPATCH_NEXT)
        self.assertEqual(out.delegated_in_flight, ("13441",))
        self.assertEqual(out.coordinator_blocking, ())

    def test_blocking_reason_names_the_refusal(self):
        # The stop reason must say *why* a claimed delegation was refused, so the
        # coordinator can journal the verdict instead of re-deriving it.
        out = evaluate_fill_decision(
            _inputs(
                lanes=[
                    LaneState(
                        issue="13441",
                        state_class=LANE_STATE_REVIEW_WAITING,
                        claim=_VERIFIED_DELEGATION,
                        provenance=LaneProvenance(),
                    )
                ]
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertIn("surface_not_verified_managed_sublane", out.reason)

    def test_actuation_unavailable_is_a_fixed_blocked_result(self):
        out = evaluate_fill_decision(
            _inputs(
                ready_independent_work=5,
                capacity_remaining=5,
                managed_sublane_actuation_available=False,
            )
        )
        self.assertEqual(out.fill_decision, FILL_STOP_ACTUATION_UNAVAILABLE)
        self.assertFalse(out.should_dispatch)


class AdvisoryAndPayloadTest(unittest.TestCase):
    def test_outcome_is_always_advisory(self):
        for inputs in (
            _inputs(ready_independent_work=1, capacity_remaining=1),
            _inputs(owner_or_release_gate_active=True),
            _inputs(lanes=[_lane("x", LANE_STATE_BLOCKED)]),
        ):
            self.assertTrue(evaluate_fill_decision(inputs).advisory)

    def test_payload_round_trips_fixed_fields(self):
        out = evaluate_fill_decision(
            _inputs(
                lanes=[_lane("12855", LANE_STATE_IMPLEMENTING)],
                ready_independent_work=1,
                capacity_remaining=2,
            )
        )
        payload = out.as_payload()
        self.assertEqual(payload["fill_decision"], FILL_DISPATCH_NEXT)
        self.assertTrue(payload["advisory"])
        self.assertTrue(payload["should_dispatch"])
        self.assertEqual(payload["active_implementing"], ["12855"])
        self.assertEqual(payload["ready_independent_work"], 1)
        self.assertEqual(payload["capacity_remaining"], 2)


if __name__ == "__main__":
    unittest.main()
