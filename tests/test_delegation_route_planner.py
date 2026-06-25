"""Classical tests for the delegated coordinator route planner (Redmine #12550).

These are the hermetic, no-side-effect tests the planner / actuator integration
must pass *before* the #12546 real-machine smoke is ever run. They exercise the
pure plan layer (:mod:`mozyo_bridge.domain.delegation_route_planner`) against the
contracts fixed by:

- ``vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md``
  (``## Classical Test Obligations``)
- ``vibes/docs/logics/delegated-coordinator-smoke-test-frame.md``
  (``## Classical tests へ落とすべき層``)

and cross-check the planner's disposition against the #12547 acceptance oracle
(``tests/test_delegated_coordinator_acceptance_oracle.py``) so the two cannot
silently drift.

Hermetic by construction: no live tmux, no Redmine reads/writes, no private pane
ids, no host paths, no cockpit composition, no private project names. Fixtures
use neutral placeholder identifiers only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Allow importing the sibling acceptance-oracle test module by name for the
# cross-check below, regardless of how this suite is discovered.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mozyo_bridge.domain.delegation_project_config import (  # noqa: E402
    ChildCandidate,
    ChildCandidateResolution,
    DelegationConfig,
    resolve_child_candidate,
    STATUS_AMBIGUOUS,
    STATUS_MISSING,
    STATUS_RESOLVED,
    CHILD_CANDIDATE_MISSING,
    CHILD_CANDIDATE_AMBIGUOUS,
)
from mozyo_bridge.domain.role_profile import (  # noqa: E402
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
)
from mozyo_bridge.domain.delegation_route_planner import (  # noqa: E402
    DelegationRoutePlanError,
    EXEC_AUTO,
    EXEC_DURABLE_RECORD,
    EXEC_OPERATOR_CONFIRMED,
    OUTPUT_RECOMMEND_ONLY,
    PLAN_BLOCKED,
    PLAN_EXECUTABLE,
    PLAN_FAILED,
    PLAN_INSUFFICIENT,
    PLAN_OPERATOR_CONFIRM,
    REALIZE_ADOPT,
    REALIZE_AMBIGUOUS,
    REALIZE_LAUNCH,
    REALIZE_NOT_APPLICABLE,
    REALIZE_NOT_LAUNCHED,
    REALIZE_SAME_LANE_FALLBACK,
    ROUTE_CLAUDE_DIRECT,
    STEP_CALLBACK_RECORD,
    STEP_CHILD_HANDOFF,
    STEP_GRANDCHILD_STAMP,
    STEP_PARENT_DECISION,
    STEP_WORKER_HANDOFF,
    TARGET_SAME_LANE_WORKER,
    RealizationCandidateView,
    RoutePlan,
    RouteRequest,
    decide_child_realization,
    decide_grandchild_realization,
    plan_delegation_route,
)

# Neutral, public-safe fixtures — no real project names, pane ids, or paths.
CHILD_PROJECT = "child-project-alpha"
DURABLE_ANCHOR = "durable:#0000 j#0"


def _resolved(child_project: str = CHILD_PROJECT) -> ChildCandidateResolution:
    """A resolved single-candidate resolution for ``child_project``."""
    config = DelegationConfig(
        child_candidates=(ChildCandidate(child_project=child_project),)
    )
    return resolve_child_candidate(config, child_project=child_project)


def _request(**overrides) -> RouteRequest:
    """A baseline route request; override one field per test."""
    base = dict(
        durable_anchor=DURABLE_ANCHOR,
        child_project=CHILD_PROJECT,
        grandchild_required=False,
        cross_project=True,
        parent_project="parent-project-omega",
        parent_issue="#0001",
        redmine_project="redmine-project-neutral",
        parent_callback_target="parent_coordinator",
        upstream_coordinator="upstream_coordinator",
        gateway_callback_target="grandchild_gateway",
        lane="lane-neutral",
    )
    base.update(overrides)
    return RouteRequest(**base)


def _match(n: int) -> tuple[RealizationCandidateView, ...]:
    """``n`` fully-matching discovery candidates."""
    return tuple(
        RealizationCandidateView(
            repo_match=True, lane_match=True, role_match=True, candidate_ref=f"c{i}"
        )
        for i in range(n)
    )


class RealizationDecisionTest(unittest.TestCase):
    """decide_child / decide_grandchild realization over read-only discovery."""

    def test_one_match_adopts(self) -> None:
        self.assertEqual(
            REALIZE_ADOPT, decide_child_realization(_match(1), can_launch=True)
        )

    def test_no_match_launches_when_launchable(self) -> None:
        self.assertEqual(
            REALIZE_LAUNCH, decide_child_realization((), can_launch=True)
        )

    def test_no_match_not_launchable_is_not_launched(self) -> None:
        self.assertEqual(
            REALIZE_NOT_LAUNCHED, decide_child_realization((), can_launch=False)
        )

    def test_multiple_matches_are_ambiguous(self) -> None:
        self.assertEqual(
            REALIZE_AMBIGUOUS, decide_child_realization(_match(2), can_launch=True)
        )

    def test_partial_match_does_not_count(self) -> None:
        # repo+lane match but role does not -> not a match -> launch.
        candidates = (
            RealizationCandidateView(
                repo_match=True, lane_match=True, role_match=False
            ),
        )
        self.assertEqual(
            REALIZE_LAUNCH, decide_child_realization(candidates, can_launch=True)
        )

    def test_grandchild_not_required_is_not_applicable(self) -> None:
        self.assertEqual(
            REALIZE_NOT_APPLICABLE,
            decide_grandchild_realization(
                (), required=False, can_launch=True, same_lane_worker_available=True
            ),
        )

    def test_grandchild_same_lane_fallback_when_only_same_lane_available(self) -> None:
        self.assertEqual(
            REALIZE_SAME_LANE_FALLBACK,
            decide_grandchild_realization(
                (),
                required=True,
                can_launch=False,
                same_lane_worker_available=True,
            ),
        )

    def test_grandchild_not_launched_when_nothing_available(self) -> None:
        self.assertEqual(
            REALIZE_NOT_LAUNCHED,
            decide_grandchild_realization(
                (),
                required=True,
                can_launch=False,
                same_lane_worker_available=False,
            ),
        )

    def test_malformed_listing_fails_closed(self) -> None:
        with self.assertRaises(DelegationRoutePlanError):
            decide_child_realization("not-a-sequence", can_launch=True)  # type: ignore[arg-type]

    def test_malformed_candidate_element_fails_closed(self) -> None:
        with self.assertRaises(DelegationRoutePlanError):
            decide_child_realization([object()], can_launch=True)  # type: ignore[list-item]


class CandidateListingNotPassTest(unittest.TestCase):
    """Obligation: candidate listing alone must not satisfy PASS (Required #5)."""

    def test_missing_resolution_is_failed_even_with_candidates_listed(self) -> None:
        # Discovery candidates exist, but the resolver could not resolve the
        # child project: the plan must fail closed, never PASS off the listing.
        resolution = ChildCandidateResolution(
            status=STATUS_MISSING,
            diagnostic=CHILD_CANDIDATE_MISSING,
            requested_child_project=CHILD_PROJECT,
            requested_capability=None,
        )
        plan = plan_delegation_route(
            resolution, _request(), child_candidates=_match(1)
        )
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual(CHILD_CANDIDATE_MISSING, plan.diagnostic)
        self.assertFalse(plan.is_pass_eligible)
        self.assertEqual((), plan.steps)

    def test_pass_eligible_requires_resolved_and_realized(self) -> None:
        plan = plan_delegation_route(
            _resolved(), _request(), child_candidates=_match(1)
        )
        self.assertTrue(plan.is_pass_eligible)
        self.assertEqual(PLAN_EXECUTABLE, plan.disposition)


class ResolverFailClosedTest(unittest.TestCase):
    """Missing / ambiguous resolver results stay fail-closed (Required #1)."""

    def test_missing_candidate_failed_acceptance(self) -> None:
        resolution = ChildCandidateResolution(
            status=STATUS_MISSING,
            diagnostic=CHILD_CANDIDATE_MISSING,
            requested_child_project=CHILD_PROJECT,
            requested_capability=None,
        )
        plan = plan_delegation_route(resolution, _request())
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual(CHILD_CANDIDATE_MISSING, plan.diagnostic)

    def test_ambiguous_candidate_failed_acceptance(self) -> None:
        resolution = ChildCandidateResolution(
            status=STATUS_AMBIGUOUS,
            diagnostic=CHILD_CANDIDATE_AMBIGUOUS,
            requested_child_project=CHILD_PROJECT,
            requested_capability=None,
        )
        plan = plan_delegation_route(resolution, _request())
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual(CHILD_CANDIDATE_AMBIGUOUS, plan.diagnostic)

    def test_resolved_without_candidate_is_malformed(self) -> None:
        bad = ChildCandidateResolution(
            status=STATUS_RESOLVED,
            diagnostic="child_candidate_resolved",
            requested_child_project=CHILD_PROJECT,
            requested_capability=None,
            candidate=None,
        )
        with self.assertRaises(DelegationRoutePlanError):
            plan_delegation_route(bad, _request())

    def test_child_project_mismatch_is_malformed(self) -> None:
        with self.assertRaises(DelegationRoutePlanError):
            plan_delegation_route(
                _resolved("child-project-alpha"),
                _request(child_project="child-project-beta"),
                child_candidates=_match(1),
            )

    def test_non_resolution_input_is_malformed(self) -> None:
        with self.assertRaises(DelegationRoutePlanError):
            plan_delegation_route(object(), _request())  # type: ignore[arg-type]

    def test_unknown_status_fails_closed(self) -> None:
        # An unknown / internally inconsistent resolution status must never fall
        # through into realization and surface as a PASS-eligible plan
        # (Required behavior #1). ChildCandidateResolution is a plain dataclass
        # and does not enforce its status vocabulary, so the planner must.
        bogus = ChildCandidateResolution(
            status="bogus",
            diagnostic="x",
            requested_child_project=CHILD_PROJECT,
            requested_capability=None,
            candidate=ChildCandidate(child_project=CHILD_PROJECT),
        )
        with self.assertRaises(DelegationRoutePlanError):
            plan_delegation_route(bogus, _request(), child_candidates=_match(1))


class CodexGatewayRoutingTest(unittest.TestCase):
    """parent -> child handoff targets the Codex gateway, never Claude (Required #4)."""

    def test_claude_direct_route_target_failed_acceptance(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(route_target_role=ROUTE_CLAUDE_DIRECT),
            child_candidates=_match(1),
        )
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual("cross_project_claude_direct_send", plan.diagnostic)
        self.assertFalse(plan.is_pass_eligible)

    def test_no_planned_step_is_a_cross_boundary_claude_send(self) -> None:
        # The only Claude-targeted step is the same-lane worker handoff; every
        # cross-boundary handoff targets a Codex gateway.
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=_match(1),
        )
        claude_steps = [
            s for s in plan.steps if s.route_target == TARGET_SAME_LANE_WORKER
        ]
        self.assertEqual(1, len(claude_steps))
        # The child + grandchild gateway handoffs are codex-targeted (not the
        # same-lane worker target).
        gateway_handoffs = [
            s
            for s in plan.steps
            if s.kind in (STEP_CHILD_HANDOFF, STEP_WORKER_HANDOFF)
            and s.route_target != TARGET_SAME_LANE_WORKER
        ]
        self.assertTrue(gateway_handoffs)


class GrandchildRealizationGateTest(unittest.TestCase):
    """Grandchild realization gate runs before same-lane dispatch (Required #6)."""

    def test_same_lane_fallback_is_blocked(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=(),
            grandchild_can_launch=False,
            same_lane_worker_available=True,
        )
        self.assertEqual(PLAN_BLOCKED, plan.disposition)
        self.assertEqual("grandchild_required_but_not_realized", plan.diagnostic)
        self.assertFalse(plan.is_pass_eligible)
        # Blocked plan emits no worker handoff command.
        self.assertFalse(
            any(s.kind == STEP_WORKER_HANDOFF for s in plan.steps)
        )

    def test_grandchild_required_but_not_launchable_failed(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=(),
            grandchild_can_launch=False,
            same_lane_worker_available=False,
        )
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual("grandchild_window_not_launched", plan.diagnostic)

    def test_grandchild_visible_lane_passes(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=_match(1),
        )
        self.assertTrue(plan.is_pass_eligible)


class ChildWindowRealizationTest(unittest.TestCase):
    """The child delegated-coordinator window must be adoptable/launchable."""

    def test_child_window_not_launchable_failed(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(),
            child_candidates=(),
            child_can_launch=False,
        )
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual("child_window_not_launched", plan.diagnostic)

    def test_child_window_ambiguous_failed(self) -> None:
        plan = plan_delegation_route(
            _resolved(), _request(), child_candidates=_match(2)
        )
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual("child_window_ambiguous", plan.diagnostic)


class OutputModeTest(unittest.TestCase):
    """Read-only recommendation is insufficient, never PASS (Required #2)."""

    def test_recommend_only_is_insufficient(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(output_mode=OUTPUT_RECOMMEND_ONLY),
            child_candidates=_match(1),
        )
        self.assertEqual(PLAN_INSUFFICIENT, plan.disposition)
        self.assertEqual("read_only_recommendation_only", plan.diagnostic)
        self.assertFalse(plan.is_pass_eligible)

    def test_unknown_output_mode_is_malformed(self) -> None:
        with self.assertRaises(DelegationRoutePlanError):
            plan_delegation_route(
                _resolved(),
                _request(output_mode="bogus"),
                child_candidates=_match(1),
            )


class ExecutableVsOperatorConfirmTest(unittest.TestCase):
    """Adopt -> executable; launch (new topology) -> operator-confirmed (Required #7)."""

    def test_adopt_only_is_executable(self) -> None:
        plan = plan_delegation_route(
            _resolved(), _request(), child_candidates=_match(1)
        )
        self.assertEqual(PLAN_EXECUTABLE, plan.disposition)
        self.assertFalse(plan.requires_operator_confirmation)

    def test_launch_requires_operator_confirmation(self) -> None:
        plan = plan_delegation_route(
            _resolved(), _request(), child_candidates=(), child_can_launch=True
        )
        self.assertEqual(PLAN_OPERATOR_CONFIRM, plan.disposition)
        self.assertTrue(plan.requires_operator_confirmation)
        # The launch step is operator-confirmed; the durable records are not.
        child_handoff = next(
            s for s in plan.steps if s.kind == STEP_CHILD_HANDOFF
        )
        self.assertEqual(EXEC_OPERATOR_CONFIRMED, child_handoff.execution_mode)

    def test_grandchild_launch_makes_plan_operator_confirmed(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=(),
            grandchild_can_launch=True,
        )
        self.assertEqual(PLAN_OPERATOR_CONFIRM, plan.disposition)


class StepOrderAndRoleChainTest(unittest.TestCase):
    """Command planner emits the fixed step order + role chain (Required #3)."""

    def test_no_grandchild_step_order(self) -> None:
        plan = plan_delegation_route(
            _resolved(), _request(), child_candidates=_match(1)
        )
        kinds = [s.kind for s in plan.steps]
        self.assertEqual(
            [STEP_PARENT_DECISION, STEP_CHILD_HANDOFF, STEP_CALLBACK_RECORD], kinds
        )
        self.assertEqual((ROLE_DELEGATED_COORDINATOR,), plan.role_profile_chain)

    def test_grandchild_step_order_and_full_chain(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=_match(1),
        )
        kinds = [s.kind for s in plan.steps]
        self.assertEqual(
            [
                STEP_PARENT_DECISION,
                STEP_CHILD_HANDOFF,
                STEP_GRANDCHILD_STAMP,
                STEP_WORKER_HANDOFF,  # parent -> grandchild gateway
                STEP_WORKER_HANDOFF,  # gateway -> same-lane worker
                STEP_CALLBACK_RECORD,
            ],
            kinds,
        )
        self.assertEqual(
            (
                ROLE_DELEGATED_COORDINATOR,
                ROLE_IMPLEMENTATION_GATEWAY,
                ROLE_IMPLEMENTATION_WORKER,
            ),
            plan.role_profile_chain,
        )

    def test_durable_record_steps_are_not_auto_executed(self) -> None:
        plan = plan_delegation_route(
            _resolved(), _request(), child_candidates=_match(1)
        )
        for kind in (STEP_PARENT_DECISION, STEP_CALLBACK_RECORD):
            step = next(s for s in plan.steps if s.kind == kind)
            self.assertEqual(EXEC_DURABLE_RECORD, step.execution_mode)

    def test_role_profiles_carry_pinned_source_and_resolved_fields(self) -> None:
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=_match(1),
        )
        child_handoff = next(
            s for s in plan.steps if s.kind == STEP_CHILD_HANDOFF
        )
        self.assertIsNotNone(child_handoff.role_profile)
        assert child_handoff.role_profile is not None
        self.assertEqual(
            ROLE_DELEGATED_COORDINATOR, child_handoff.role_profile.role_profile
        )
        # The supplied child_project field is substituted (not left unresolved).
        self.assertIn(CHILD_PROJECT, child_handoff.role_profile.resolved_text)
        self.assertNotIn("child_project", child_handoff.role_profile.unresolved_placeholders)

    def test_no_private_topology_tokens_in_route_targets(self) -> None:
        # Route targets are logical identity tokens, never a pane id (``%NN``)
        # or a host path. Guards Required behavior #8 at the plan boundary.
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=_match(1),
        )
        for step in plan.steps:
            self.assertNotIn("%", step.route_target)
            self.assertNotIn("/", step.route_target)


class AcceptanceOracleCrossCheckTest(unittest.TestCase):
    """The planner disposition maps 1:1 to the #12547 oracle classification.

    Importing the pinned oracle keeps the planner from drifting from the
    executable spec it must conform to (#12550 Required behavior #1).
    """

    def _oracle(self):
        import test_delegated_coordinator_acceptance_oracle as oracle

        return oracle

    def test_executable_plan_maps_to_oracle_pass(self) -> None:
        oracle = self._oracle()
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=_match(1),
        )
        self.assertTrue(plan.is_pass_eligible)
        verdict = oracle.classify_acceptance_run(
            oracle.clean_autonomous_pass_scenario()
        )
        self.assertEqual(oracle.CLASS_PASS, verdict.classification)

    def test_missing_candidate_maps_to_oracle_failed_acceptance(self) -> None:
        oracle = self._oracle()
        resolution = ChildCandidateResolution(
            status=STATUS_MISSING,
            diagnostic=CHILD_CANDIDATE_MISSING,
            requested_child_project=CHILD_PROJECT,
            requested_capability=None,
        )
        plan = plan_delegation_route(resolution, _request())
        verdict = oracle.classify_acceptance_run(
            oracle.clean_autonomous_pass_scenario(child_candidate="missing")
        )
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual(oracle.CLASS_FAILED_ACCEPTANCE, verdict.classification)
        # Same reason string on both sides.
        self.assertEqual(plan.diagnostic, verdict.reason)

    def test_claude_direct_maps_to_oracle_failed_acceptance(self) -> None:
        oracle = self._oracle()
        plan = plan_delegation_route(
            _resolved(),
            _request(route_target_role=ROUTE_CLAUDE_DIRECT),
            child_candidates=_match(1),
        )
        verdict = oracle.classify_acceptance_run(
            oracle.clean_autonomous_pass_scenario(route_target_role="claude_direct")
        )
        self.assertEqual(PLAN_FAILED, plan.disposition)
        self.assertEqual(oracle.CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual(plan.diagnostic, verdict.reason)

    def test_same_lane_fallback_maps_to_oracle_blocked(self) -> None:
        oracle = self._oracle()
        plan = plan_delegation_route(
            _resolved(),
            _request(grandchild_required=True),
            child_candidates=_match(1),
            grandchild_candidates=(),
            grandchild_can_launch=False,
            same_lane_worker_available=True,
        )
        verdict = oracle.classify_acceptance_run(
            oracle.clean_autonomous_pass_scenario(
                grandchild_realization="same_lane_fallback"
            )
        )
        self.assertEqual(PLAN_BLOCKED, plan.disposition)
        self.assertEqual(oracle.CLASS_BLOCKED, verdict.classification)
        self.assertEqual(plan.diagnostic, verdict.reason)

    def test_recommend_only_maps_to_oracle_insufficient(self) -> None:
        oracle = self._oracle()
        plan = plan_delegation_route(
            _resolved(),
            _request(output_mode=OUTPUT_RECOMMEND_ONLY),
            child_candidates=_match(1),
        )
        verdict = oracle.classify_acceptance_run(
            oracle.clean_autonomous_pass_scenario(
                resolver_output="read_only_recommendation"
            )
        )
        self.assertEqual(PLAN_INSUFFICIENT, plan.disposition)
        self.assertEqual(oracle.CLASS_INSUFFICIENT, verdict.classification)
        self.assertEqual(plan.diagnostic, verdict.reason)


class FrozenPlanTest(unittest.TestCase):
    """The plan is an immutable value object (safe to pass around / record)."""

    def test_plan_is_frozen(self) -> None:
        plan = plan_delegation_route(
            _resolved(), _request(), child_candidates=_match(1)
        )
        self.assertIsInstance(plan, RoutePlan)
        with self.assertRaises(Exception):
            plan.disposition = "mutated"  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
