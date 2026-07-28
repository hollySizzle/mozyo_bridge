"""Unit tests for the multi-goal workstream dispatch plan (Redmine #14636).

Covers the acceptance conditions of #14636 and the boundaries it must not cross:

- **order independence / stable digest**: every permutation of a 5-goal request produces a
  byte-identical payload and the same ``plan_digest``, and the per-workstream identity
  digest is stable across situations;
- **individual detection**: independent (parallel), same-project reuse, file overlap,
  invariant overlap, declared dependency, merge-order conflict, and the ambiguous / missing
  intake defects are each detected and told apart;
- **negative safety**: coordinator management load, callback-miss worry, broad bucket and
  *the number of goals itself* never serialize anything; an unreadable / blocked dependency
  and a typed intake defect fail closed;
- **authority reuse**: every disposition traces back to a #12921 ``ADMIT_*`` decision over
  the closed risk vocabulary, and the relation vocabulary maps into it rather than beside it.
"""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
    ADMISSION_DECISIONS,
    ADMIT_ALLOW_DISPATCH,
    ADMIT_BLOCKED,
    ADMIT_NEEDS_OWNER_DECISION,
    ADMIT_SERIALIZE,
    INVALID_SERIALIZATION_NONREASONS,
    NONREASON_COORDINATOR_MANAGEMENT_LOAD,
    NONREASON_GOAL_COUNT,
    RISK_BLOCKED_OR_CALLBACK_FAILURE,
    RISK_FILE_OVERLAP,
    RISK_INVARIANT_OVERLAP,
    RISK_MERGE_ORDER_CONFLICT,
    RISK_RELEASE_PUBLISH_GATE,
    VALID_ADMISSION_RISKS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_identity import (
    workstream_identity_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_plan import (
    build_multi_goal_workstream_plan,
    render_multi_goal_plan_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_records import (
    ACTIONABLE_DISPOSITIONS,
    INTAKE_AMBIGUOUS_DEPENDENCY,
    INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE,
    INTAKE_AMBIGUOUS_GOAL_ID,
    INTAKE_AMBIGUOUS_LANE_SIGNAL,
    INTAKE_DEFECTS,
    INTAKE_MISSING_GOAL_ID,
    INTAKE_MISSING_OBJECTIVE,
    INTAKE_MISSING_PROJECT_IDENTITY,
    PLAN_SCHEMA_VERSION,
    REJECTING_INTAKE_DEFECTS,
    RELATION_DECLARED_DEPENDENCY,
    RELATION_FILE_OVERLAP,
    RELATION_INVARIANT_OVERLAP,
    RELATION_MERGE_ORDER_CONFLICT,
    RELATION_SAME_PROJECT,
    WORKSTREAM_BLOCKED,
    WORKSTREAM_DISPOSITIONS,
    WORKSTREAM_NEEDS_OWNER_DECISION,
    WORKSTREAM_PARALLEL,
    WORKSTREAM_RELATIONS,
    WORKSTREAM_REUSE,
    WORKSTREAM_SERIALIZE,
    ExistingWorkstream,
    GoalCandidate,
    MultiGoalWorkstreamPlanError,
    _ADMISSION_TO_DISPOSITION,
    _RELATION_TO_RISK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_NONE,
    GATE_REVIEW,
    LaneSignal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    LANE_STATE_BLOCKED,
)


def _goal(goal_id: str, project: str, **kwargs) -> GoalCandidate:
    """A minimally valid goal candidate (id + project identity + objective)."""
    kwargs.setdefault("objective", f"advance {project}")
    return GoalCandidate(goal_id=goal_id, project_identity=project, **kwargs)


def _by_key(plan) -> dict:
    return {workstream.workstream_key: workstream for workstream in plan.workstreams}


# The 5-goal acceptance fixture: one independent workstream, two goals folded onto one
# project, a file overlap, a declared dependency, and an invariant overlap.
def _five_goal_request() -> tuple[GoalCandidate, ...]:
    return (
        _goal("g1", "proj-a", file_surfaces=("src/a.py",)),
        _goal("g2", "proj-a", file_surfaces=("src/a2.py",)),
        _goal("g3", "proj-b", file_surfaces=("src/a.py",)),
        _goal("g4", "proj-c", depends_on_goals=("g1",)),
        _goal(
            "g5",
            "proj-d",
            invariant_surfaces=("send-safety",),
        ),
    )


def _five_goal_request_with_invariant_peer() -> tuple[GoalCandidate, ...]:
    goals = list(_five_goal_request())
    goals[4] = _goal("g5", "proj-d", invariant_surfaces=("send-safety",))
    goals.append(_goal("g6", "proj-e", invariant_surfaces=("send-safety",)))
    return tuple(goals)


class VocabularyTest(unittest.TestCase):
    def test_every_admission_decision_maps_to_a_disposition(self):
        self.assertEqual(set(_ADMISSION_TO_DISPOSITION), set(ADMISSION_DECISIONS))
        for disposition in _ADMISSION_TO_DISPOSITION.values():
            self.assertIn(disposition, WORKSTREAM_DISPOSITIONS)

    def test_reuse_is_the_only_disposition_without_an_admission_decision(self):
        mapped = set(_ADMISSION_TO_DISPOSITION.values())
        self.assertEqual(WORKSTREAM_DISPOSITIONS - mapped, {WORKSTREAM_REUSE})

    def test_relations_map_into_the_closed_risk_vocabulary_not_beside_it(self):
        for relation, risk in _RELATION_TO_RISK.items():
            self.assertIn(relation, WORKSTREAM_RELATIONS)
            self.assertIn(risk, VALID_ADMISSION_RISKS)

    def test_same_project_is_a_relation_but_never_a_risk(self):
        self.assertIn(RELATION_SAME_PROJECT, WORKSTREAM_RELATIONS)
        self.assertNotIn(RELATION_SAME_PROJECT, _RELATION_TO_RISK)

    def test_dependency_and_merge_order_share_a_risk_but_stay_distinct_evidence(self):
        self.assertNotEqual(RELATION_DECLARED_DEPENDENCY, RELATION_MERGE_ORDER_CONFLICT)
        self.assertEqual(
            _RELATION_TO_RISK[RELATION_DECLARED_DEPENDENCY],
            _RELATION_TO_RISK[RELATION_MERGE_ORDER_CONFLICT],
        )
        self.assertEqual(
            _RELATION_TO_RISK[RELATION_DECLARED_DEPENDENCY], RISK_MERGE_ORDER_CONFLICT
        )

    def test_actionable_dispositions_are_the_dispatchable_ones_only(self):
        self.assertEqual(
            ACTIONABLE_DISPOSITIONS, {WORKSTREAM_PARALLEL, WORKSTREAM_REUSE}
        )

    def test_rejecting_defects_are_a_subset_of_the_defect_vocabulary(self):
        self.assertTrue(REJECTING_INTAKE_DEFECTS <= INTAKE_DEFECTS)


class OrderIndependenceTest(unittest.TestCase):
    def test_five_goal_permutations_produce_an_identical_plan(self):
        goals = _five_goal_request()
        baseline = build_multi_goal_workstream_plan(goals)
        self.assertEqual(len(baseline.workstreams), 4)
        for permutation in itertools.permutations(goals):
            plan = build_multi_goal_workstream_plan(permutation)
            self.assertEqual(plan.as_payload(), baseline.as_payload())
            self.assertEqual(plan.plan_digest, baseline.plan_digest)

    def test_shuffled_lane_signals_and_repeated_tuples_do_not_change_the_plan(self):
        signals = (
            LaneSignal(issue="900", latest_gate=GATE_NONE, blocker_recorded=True),
            LaneSignal(issue="901", latest_gate=GATE_NONE),
        )
        goals = (
            _goal(
                "g1",
                "proj-a",
                file_surfaces=("src/a.py", "src/a.py", " src/a.py "),
                dependency_lanes=("901", "901"),
            ),
        )
        first = build_multi_goal_workstream_plan(goals, active_lane_signals=signals)
        second = build_multi_goal_workstream_plan(
            goals, active_lane_signals=tuple(reversed(signals))
        )
        self.assertEqual(first.as_payload(), second.as_payload())
        self.assertEqual(
            _by_key(first)["proj-a"].file_surfaces, ("src/a.py",)
        )

    def test_rejected_goals_without_ids_are_ordered_by_content_not_arrival(self):
        a = GoalCandidate(goal_id="", project_identity="proj-a", objective="x")
        b = GoalCandidate(goal_id="", project_identity="proj-b", objective="y")
        first = build_multi_goal_workstream_plan((a, b))
        second = build_multi_goal_workstream_plan((b, a))
        self.assertEqual(first.as_payload(), second.as_payload())
        self.assertEqual(first.plan_digest, second.plan_digest)


class DigestTest(unittest.TestCase):
    def test_workstream_identity_digest_ignores_goal_order_and_repeats(self):
        self.assertEqual(
            workstream_identity_digest("proj-a", ("g2", "g1")),
            workstream_identity_digest("proj-a", ("g1", "g2", "g1")),
        )

    def test_workstream_identity_digest_separates_distinct_units(self):
        self.assertNotEqual(
            workstream_identity_digest("proj-a", ("g1",)),
            workstream_identity_digest("proj-b", ("g1",)),
        )
        self.assertNotEqual(
            workstream_identity_digest("proj-a", ("g1",)),
            workstream_identity_digest("proj-a", ("g1", "g2")),
        )

    def test_length_prefixing_keeps_ambiguous_item_splits_apart(self):
        # Without the length prefix these two encode identically: two goals "a" and "b"
        # concatenate to the item separator sequence that one goal literally named "ai=b"
        # also produces, so the second request would be silently suppressed as a replay of
        # the first. (Verified adversarially: dropping the prefix from _encode_field turns
        # this assertion red.)
        self.assertNotEqual(
            workstream_identity_digest("p", ("a", "b")),
            workstream_identity_digest("p", ("ai=b",)),
        )

    def test_schema_version_participates_in_the_identity_digest(self):
        self.assertNotEqual(
            workstream_identity_digest("proj-a", ("g1",)),
            workstream_identity_digest(
                "proj-a", ("g1",), schema_version=PLAN_SCHEMA_VERSION + 1
            ),
        )

    def test_workstream_digest_is_stable_while_the_disposition_changes(self):
        goals = (_goal("g1", "proj-a", file_surfaces=("src/a.py",)),)
        free = build_multi_goal_workstream_plan(goals)
        held = build_multi_goal_workstream_plan(
            (
                _goal(
                    "g1",
                    "proj-a",
                    file_surfaces=("src/a.py",),
                    release_publish_gate_active=True,
                ),
            )
        )
        self.assertEqual(
            _by_key(free)["proj-a"].workstream_digest,
            _by_key(held)["proj-a"].workstream_digest,
        )
        self.assertNotEqual(free.plan_digest, held.plan_digest)

    def test_plan_digest_ignores_reworded_objectives(self):
        first = build_multi_goal_workstream_plan((_goal("g1", "proj-a"),))
        second = build_multi_goal_workstream_plan(
            (GoalCandidate(goal_id="g1", project_identity="proj-a", objective="reworded"),)
        )
        self.assertEqual(first.plan_digest, second.plan_digest)

    def test_plan_digest_changes_when_a_relation_appears(self):
        independent = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", file_surfaces=("src/a.py",)),
                _goal("g2", "proj-b", file_surfaces=("src/b.py",)),
            )
        )
        overlapping = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", file_surfaces=("src/a.py",)),
                _goal("g2", "proj-b", file_surfaces=("src/a.py",)),
            )
        )
        self.assertNotEqual(independent.plan_digest, overlapping.plan_digest)


class IndependentAndReuseTest(unittest.TestCase):
    def test_independent_goals_are_each_dispatched_in_parallel(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", file_surfaces=("src/a.py",)),
                _goal("g2", "proj-b", file_surfaces=("src/b.py",)),
                _goal("g3", "proj-c", file_surfaces=("src/c.py",)),
            )
        )
        self.assertEqual(
            [w.disposition for w in plan.workstreams],
            [WORKSTREAM_PARALLEL] * 3,
        )
        for workstream in plan.workstreams:
            self.assertEqual(workstream.admission_decision, ADMIT_ALLOW_DISPATCH)
            self.assertEqual(workstream.relations, ())
            self.assertTrue(workstream.actionable)
            self.assertTrue(workstream.creates_new_coordinator)

    def test_same_project_goals_fold_into_one_reused_workstream(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", file_surfaces=("src/a.py",)),
                _goal("g2", "proj-a", file_surfaces=("src/a.py",)),
            )
        )
        self.assertEqual(len(plan.workstreams), 1)
        workstream = plan.workstreams[0]
        self.assertEqual(workstream.disposition, WORKSTREAM_REUSE)
        self.assertEqual(workstream.goal_ids, ("g1", "g2"))
        self.assertTrue(workstream.folded_goals)
        self.assertEqual(
            [r.relation for r in workstream.relations], [RELATION_SAME_PROJECT]
        )
        # The shared file surface between two same-project goals is NOT an overlap risk:
        # they are one unit, and a unit cannot be serialized behind itself.
        self.assertEqual(workstream.risk_reasons, ())
        self.assertEqual(workstream.admission_decision, ADMIT_ALLOW_DISPATCH)

    def test_existing_coordinator_is_adopted_instead_of_duplicated(self):
        plan = build_multi_goal_workstream_plan(
            (_goal("g1", "proj-a"),),
            existing_workstreams=(
                ExistingWorkstream(project_identity="proj-a", coordinator_lane="lane-77"),
            ),
        )
        workstream = plan.workstreams[0]
        self.assertEqual(workstream.disposition, WORKSTREAM_REUSE)
        self.assertEqual(workstream.reuse_target, "lane-77")
        self.assertFalse(workstream.creates_new_coordinator)
        self.assertIn("lane-77", workstream.next_safe_action)

    def test_held_workstream_still_records_where_it_would_land(self):
        plan = build_multi_goal_workstream_plan(
            (_goal("g1", "proj-a", release_publish_gate_active=True),),
            existing_workstreams=(
                ExistingWorkstream(project_identity="proj-a", coordinator_lane="lane-77"),
            ),
        )
        workstream = plan.workstreams[0]
        self.assertEqual(workstream.disposition, WORKSTREAM_NEEDS_OWNER_DECISION)
        self.assertEqual(workstream.reuse_target, "lane-77")
        self.assertFalse(workstream.actionable)

    def test_unusable_existing_workstream_entry_is_refused(self):
        with self.assertRaises(MultiGoalWorkstreamPlanError):
            build_multi_goal_workstream_plan(
                (_goal("g1", "proj-a"),),
                existing_workstreams=(
                    ExistingWorkstream(project_identity="proj-a", coordinator_lane=""),
                ),
            )

    def test_two_coordinators_claiming_one_project_identity_is_refused(self):
        with self.assertRaises(MultiGoalWorkstreamPlanError):
            build_multi_goal_workstream_plan(
                (_goal("g1", "proj-a"),),
                existing_workstreams=(
                    ExistingWorkstream(project_identity="proj-a", coordinator_lane="l1"),
                    ExistingWorkstream(project_identity="proj-a", coordinator_lane="l2"),
                ),
            )


class OverlapAndDependencyTest(unittest.TestCase):
    def test_file_overlap_serializes_both_sides_against_each_other(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", file_surfaces=("src/shared.py", "src/a.py")),
                _goal("g2", "proj-b", file_surfaces=("src/shared.py",)),
            )
        )
        by_key = _by_key(plan)
        for key, peer in (("proj-a", "proj-b"), ("proj-b", "proj-a")):
            workstream = by_key[key]
            self.assertEqual(workstream.disposition, WORKSTREAM_SERIALIZE)
            self.assertEqual(workstream.admission_decision, ADMIT_SERIALIZE)
            self.assertIn(RISK_FILE_OVERLAP, workstream.risk_reasons)
            self.assertEqual(workstream.blocked_by, (peer,))
            overlap = [
                r for r in workstream.relations if r.relation == RELATION_FILE_OVERLAP
            ]
            self.assertEqual(len(overlap), 1)
            self.assertEqual(overlap[0].peer, peer)
            self.assertEqual(overlap[0].shared, ("src/shared.py",))

    def test_invariant_overlap_is_detected_separately_from_file_overlap(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", invariant_surfaces=("send-safety",)),
                _goal("g2", "proj-b", invariant_surfaces=("send-safety",)),
            )
        )
        workstream = _by_key(plan)["proj-a"]
        self.assertEqual(workstream.disposition, WORKSTREAM_SERIALIZE)
        self.assertEqual(
            [r.relation for r in workstream.relations], [RELATION_INVARIANT_OVERLAP]
        )
        self.assertIn(RISK_INVARIANT_OVERLAP, workstream.risk_reasons)
        self.assertNotIn(RISK_FILE_OVERLAP, workstream.risk_reasons)

    def test_declared_dependency_serializes_only_the_dependent_side(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a"),
                _goal("g2", "proj-b", depends_on_goals=("g1",)),
            )
        )
        by_key = _by_key(plan)
        self.assertEqual(by_key["proj-a"].disposition, WORKSTREAM_PARALLEL)
        dependent = by_key["proj-b"]
        self.assertEqual(dependent.disposition, WORKSTREAM_SERIALIZE)
        self.assertEqual(
            [r.relation for r in dependent.relations], [RELATION_DECLARED_DEPENDENCY]
        )
        self.assertEqual(dependent.relations[0].peer, "proj-a")
        self.assertEqual(dependent.relations[0].shared, ("g1",))
        self.assertIn(RISK_MERGE_ORDER_CONFLICT, dependent.risk_reasons)

    def test_merge_order_conflict_is_distinguishable_from_a_dependency(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a"),
                _goal("g2", "proj-b", merge_order_conflict_goals=("g1",)),
                _goal("g3", "proj-c", depends_on_goals=("g1",)),
            )
        )
        by_key = _by_key(plan)
        self.assertEqual(
            [r.relation for r in by_key["proj-b"].relations],
            [RELATION_MERGE_ORDER_CONFLICT],
        )
        self.assertEqual(
            [r.relation for r in by_key["proj-c"].relations],
            [RELATION_DECLARED_DEPENDENCY],
        )
        # Same authority risk, different evidence.
        self.assertEqual(
            by_key["proj-b"].risk_reasons, by_key["proj-c"].risk_reasons
        )

    def test_a_dependency_on_a_same_project_sibling_is_not_a_relation(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a"),
                _goal("g2", "proj-a", depends_on_goals=("g1",)),
            )
        )
        workstream = plan.workstreams[0]
        self.assertEqual(
            [r.relation for r in workstream.relations], [RELATION_SAME_PROJECT]
        )
        self.assertEqual(workstream.disposition, WORKSTREAM_REUSE)
        self.assertEqual(workstream.risk_reasons, ())

    def test_blocked_active_lane_dependency_blocks_the_workstream(self):
        plan = build_multi_goal_workstream_plan(
            (_goal("g1", "proj-a", dependency_lanes=("900",)),),
            active_lane_signals=(
                LaneSignal(issue="900", latest_gate=GATE_NONE, blocker_recorded=True),
            ),
        )
        workstream = plan.workstreams[0]
        self.assertEqual(workstream.disposition, WORKSTREAM_BLOCKED)
        self.assertEqual(workstream.admission_decision, ADMIT_BLOCKED)
        self.assertIn(RISK_BLOCKED_OR_CALLBACK_FAILURE, workstream.risk_reasons)
        self.assertEqual(workstream.blocked_by, ("900",))
        self.assertEqual(
            [lane.state_class for lane in plan.classified_lanes], [LANE_STATE_BLOCKED]
        )

    def test_dependency_on_an_unsupplied_lane_fails_closed_to_blocked(self):
        plan = build_multi_goal_workstream_plan(
            (_goal("g1", "proj-a", dependency_lanes=("999",)),)
        )
        self.assertEqual(plan.workstreams[0].disposition, WORKSTREAM_BLOCKED)


class IntakeDefectTest(unittest.TestCase):
    def test_missing_goal_id_is_rejected_before_planning(self):
        plan = build_multi_goal_workstream_plan(
            (GoalCandidate(goal_id="  ", project_identity="proj-a", objective="x"),)
        )
        self.assertEqual(plan.workstreams, ())
        self.assertEqual(
            [(g.goal_id, g.reason) for g in plan.rejected_goals],
            [("", INTAKE_MISSING_GOAL_ID)],
        )

    def test_missing_project_identity_is_rejected(self):
        plan = build_multi_goal_workstream_plan(
            (GoalCandidate(goal_id="g1", project_identity="", objective="x"),)
        )
        self.assertEqual(plan.workstreams, ())
        self.assertEqual(
            plan.rejected_goals[0].reason, INTAKE_MISSING_PROJECT_IDENTITY
        )

    def test_missing_objective_is_rejected(self):
        plan = build_multi_goal_workstream_plan(
            (GoalCandidate(goal_id="g1", project_identity="proj-a", objective="  "),)
        )
        self.assertEqual(plan.workstreams, ())
        self.assertEqual(plan.rejected_goals[0].reason, INTAKE_MISSING_OBJECTIVE)

    def test_every_rejection_reason_is_a_rejecting_defect(self):
        plan = build_multi_goal_workstream_plan(
            (
                GoalCandidate(goal_id="", project_identity="proj-a", objective="x"),
                GoalCandidate(goal_id="g1", project_identity="", objective="x"),
                GoalCandidate(goal_id="g2", project_identity="proj-b", objective=""),
                _goal("g3", "proj-c", file_surfaces=("src/a.py",)),
                _goal("g3", "proj-c", file_surfaces=("src/b.py",)),
            )
        )
        self.assertEqual(len(plan.rejected_goals), 4)
        for rejected in plan.rejected_goals:
            self.assertIn(rejected.reason, REJECTING_INTAKE_DEFECTS)

    def test_identical_duplicate_goal_collapses_as_an_idempotent_replay(self):
        goal = _goal("g1", "proj-a", file_surfaces=("src/a.py",))
        plan = build_multi_goal_workstream_plan((goal, goal))
        self.assertEqual(plan.rejected_goals, ())
        self.assertEqual(plan.collapsed_duplicate_goals, ("g1",))
        self.assertEqual(plan.workstreams[0].goal_ids, ("g1",))
        self.assertEqual(plan.workstreams[0].disposition, WORKSTREAM_PARALLEL)

    def test_conflicting_duplicate_goal_id_is_ambiguous_and_admits_neither_copy(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", file_surfaces=("src/a.py",)),
                _goal("g1", "proj-a", file_surfaces=("src/b.py",)),
            )
        )
        self.assertEqual(plan.workstreams, ())
        self.assertEqual(plan.rejected_goals[0].reason, INTAKE_AMBIGUOUS_GOAL_ID)
        self.assertEqual(plan.collapsed_duplicate_goals, ())

    def test_dependency_on_an_unknown_goal_is_ambiguous_and_holds_the_workstream(self):
        plan = build_multi_goal_workstream_plan(
            (_goal("g1", "proj-a", depends_on_goals=("ghost",)),)
        )
        workstream = plan.workstreams[0]
        self.assertEqual(workstream.disposition, WORKSTREAM_BLOCKED)
        self.assertEqual(workstream.admission_decision, ADMIT_BLOCKED)
        self.assertEqual(
            [d.reason for d in workstream.intake_defects], [INTAKE_AMBIGUOUS_DEPENDENCY]
        )
        self.assertEqual(workstream.blocked_by, ("g1",))
        self.assertEqual(workstream.relations, ())

    def test_dependency_on_a_rejected_goal_is_ambiguous_not_silently_dropped(self):
        plan = build_multi_goal_workstream_plan(
            (
                GoalCandidate(goal_id="g1", project_identity="proj-a", objective=""),
                _goal("g2", "proj-b", depends_on_goals=("g1",)),
            )
        )
        self.assertEqual(_by_key(plan)["proj-b"].disposition, WORKSTREAM_BLOCKED)

    def test_dependency_cycle_holds_every_workstream_on_it(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", depends_on_goals=("g2",)),
                _goal("g2", "proj-b", depends_on_goals=("g1",)),
                _goal("g3", "proj-c"),
            )
        )
        by_key = _by_key(plan)
        for key in ("proj-a", "proj-b"):
            self.assertEqual(by_key[key].disposition, WORKSTREAM_BLOCKED)
            self.assertIn(
                INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE,
                [d.reason for d in by_key[key].intake_defects],
            )
        self.assertEqual(by_key["proj-c"].disposition, WORKSTREAM_PARALLEL)

    def test_a_long_acyclic_dependency_chain_is_not_reported_as_a_cycle(self):
        goals = tuple(
            _goal(f"g{i}", f"proj-{i}", depends_on_goals=(f"g{i - 1}",) if i else ())
            for i in range(40)
        )
        plan = build_multi_goal_workstream_plan(goals)
        for workstream in plan.workstreams:
            self.assertNotIn(
                INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE,
                [d.reason for d in workstream.intake_defects],
            )

    def test_merge_order_conflict_is_not_read_as_a_directed_cycle(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", merge_order_conflict_goals=("g2",)),
                _goal("g2", "proj-b", merge_order_conflict_goals=("g1",)),
            )
        )
        for workstream in plan.workstreams:
            self.assertEqual(workstream.disposition, WORKSTREAM_SERIALIZE)
            self.assertEqual(workstream.intake_defects, ())

    def test_conflicting_lane_signals_are_dropped_with_a_plan_level_defect(self):
        plan = build_multi_goal_workstream_plan(
            (_goal("g1", "proj-a", dependency_lanes=("900",)),),
            active_lane_signals=(
                LaneSignal(issue="900", latest_gate=GATE_NONE),
                LaneSignal(issue="900", latest_gate=GATE_REVIEW),
            ),
        )
        self.assertEqual(
            [d.reason for d in plan.plan_intake_defects], [INTAKE_AMBIGUOUS_LANE_SIGNAL]
        )
        self.assertEqual(plan.classified_lanes, ())
        # The dependency now resolves against nothing, so #12921 fails it closed.
        self.assertEqual(plan.workstreams[0].disposition, WORKSTREAM_BLOCKED)

    def test_identical_lane_signals_collapse_without_a_defect(self):
        signal = LaneSignal(issue="900", latest_gate=GATE_NONE)
        plan = build_multi_goal_workstream_plan(
            (_goal("g1", "proj-a"),), active_lane_signals=(signal, signal)
        )
        self.assertEqual(plan.plan_intake_defects, ())
        self.assertEqual(len(plan.classified_lanes), 1)

    def test_a_defect_never_relaxes_a_more_severe_admission_decision(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal(
                    "g1",
                    "proj-a",
                    depends_on_goals=("ghost",),
                    release_publish_gate_active=True,
                ),
            )
        )
        workstream = plan.workstreams[0]
        self.assertEqual(workstream.disposition, WORKSTREAM_NEEDS_OWNER_DECISION)
        self.assertEqual(workstream.admission_decision, ADMIT_NEEDS_OWNER_DECISION)
        self.assertIn(RISK_RELEASE_PUBLISH_GATE, workstream.risk_reasons)
        self.assertEqual(
            [d.reason for d in workstream.intake_defects], [INTAKE_AMBIGUOUS_DEPENDENCY]
        )


class NegativeSafetyTest(unittest.TestCase):
    def test_goal_count_alone_never_serializes_anything(self):
        goals = tuple(
            _goal(f"g{i}", f"proj-{i}", goal_count_only=True) for i in range(1, 13)
        )
        plan = build_multi_goal_workstream_plan(goals)
        self.assertEqual(len(plan.workstreams), 12)
        for workstream in plan.workstreams:
            self.assertEqual(workstream.disposition, WORKSTREAM_PARALLEL)
            self.assertEqual(workstream.risk_reasons, ())
            self.assertIn(NONREASON_GOAL_COUNT, workstream.rejected_nonreasons)

    def test_coordinator_management_load_never_serializes_anything(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal("g1", "proj-a", coordinator_management_load=True),
                _goal("g2", "proj-b", coordinator_management_load=True),
            )
        )
        for workstream in plan.workstreams:
            self.assertEqual(workstream.disposition, WORKSTREAM_PARALLEL)
            self.assertIn(
                NONREASON_COORDINATOR_MANAGEMENT_LOAD, workstream.rejected_nonreasons
            )

    def test_every_convenience_nonreason_together_still_dispatches(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal(
                    "g1",
                    "proj-a",
                    callback_miss_concern=True,
                    coordinator_management_load=True,
                    broad_bucket_only=True,
                    goal_count_only=True,
                ),
            )
        )
        workstream = plan.workstreams[0]
        self.assertEqual(workstream.disposition, WORKSTREAM_PARALLEL)
        self.assertEqual(
            set(workstream.rejected_nonreasons), set(INVALID_SERIALIZATION_NONREASONS)
        )

    def test_a_nonreason_does_not_suppress_a_real_overlap(self):
        plan = build_multi_goal_workstream_plan(
            (
                _goal(
                    "g1",
                    "proj-a",
                    file_surfaces=("src/shared.py",),
                    goal_count_only=True,
                ),
                _goal("g2", "proj-b", file_surfaces=("src/shared.py",)),
            )
        )
        held = _by_key(plan)["proj-a"]
        self.assertEqual(held.disposition, WORKSTREAM_SERIALIZE)
        self.assertIn(RISK_FILE_OVERLAP, held.risk_reasons)
        self.assertIn(NONREASON_GOAL_COUNT, held.rejected_nonreasons)


class FiveGoalAcceptanceTest(unittest.TestCase):
    def test_the_five_goal_request_detects_each_finding_individually(self):
        plan = build_multi_goal_workstream_plan(_five_goal_request_with_invariant_peer())
        by_key = _by_key(plan)
        self.assertEqual(
            sorted(by_key), ["proj-a", "proj-b", "proj-c", "proj-d", "proj-e"]
        )

        # same-project reuse (g1 + g2) AND a file overlap with proj-b: the overlap decides,
        # the reuse basis is still recorded.
        proj_a = by_key["proj-a"]
        self.assertEqual(proj_a.goal_ids, ("g1", "g2"))
        self.assertIn(
            RELATION_SAME_PROJECT, [r.relation for r in proj_a.relations]
        )
        self.assertIn(
            RELATION_FILE_OVERLAP, [r.relation for r in proj_a.relations]
        )
        self.assertEqual(proj_a.disposition, WORKSTREAM_SERIALIZE)

        # file overlap
        self.assertEqual(by_key["proj-b"].disposition, WORKSTREAM_SERIALIZE)
        self.assertIn(RISK_FILE_OVERLAP, by_key["proj-b"].risk_reasons)

        # declared dependency
        self.assertEqual(by_key["proj-c"].disposition, WORKSTREAM_SERIALIZE)
        self.assertEqual(
            [r.relation for r in by_key["proj-c"].relations],
            [RELATION_DECLARED_DEPENDENCY],
        )

        # invariant overlap (proj-d <-> proj-e)
        self.assertEqual(by_key["proj-d"].disposition, WORKSTREAM_SERIALIZE)
        self.assertIn(RISK_INVARIANT_OVERLAP, by_key["proj-d"].risk_reasons)
        self.assertEqual(by_key["proj-e"].blocked_by, ("proj-d",))

    def test_ambiguous_and_missing_ride_alongside_a_healthy_five_goal_request(self):
        goals = _five_goal_request() + (
            GoalCandidate(goal_id="", project_identity="proj-x", objective="x"),
            _goal("g9", "proj-y", depends_on_goals=("ghost",)),
        )
        plan = build_multi_goal_workstream_plan(goals)
        by_key = _by_key(plan)
        self.assertEqual(
            [g.reason for g in plan.rejected_goals], [INTAKE_MISSING_GOAL_ID]
        )
        self.assertEqual(by_key["proj-y"].disposition, WORKSTREAM_BLOCKED)
        # The healthy independent workstream is unaffected by its neighbours' defects.
        self.assertEqual(by_key["proj-d"].disposition, WORKSTREAM_PARALLEL)

    def test_counts_and_actionable_projection_agree_with_the_dispositions(self):
        plan = build_multi_goal_workstream_plan(_five_goal_request())
        counts = plan.counts_by_disposition
        self.assertEqual(sum(counts.values()), len(plan.workstreams))
        self.assertEqual(
            len(plan.actionable_workstreams),
            counts[WORKSTREAM_PARALLEL] + counts[WORKSTREAM_REUSE],
        )
        for workstream in plan.workstreams:
            self.assertIn(workstream.disposition, WORKSTREAM_DISPOSITIONS)
            self.assertIn(workstream.admission_decision, ADMISSION_DECISIONS)


class JournalRenderTest(unittest.TestCase):
    def test_journal_names_every_workstream_relation_and_defect(self):
        plan = build_multi_goal_workstream_plan(
            _five_goal_request()
            + (GoalCandidate(goal_id="", project_identity="proj-x", objective="x"),)
        )
        text = render_multi_goal_plan_journal(plan)
        self.assertIn("## Multi-goal workstream dispatch plan", text)
        self.assertIn(plan.plan_digest, text)
        for key in ("proj-a", "proj-b", "proj-c", "proj-d"):
            self.assertIn(key, text)
        self.assertIn(RELATION_SAME_PROJECT, text)
        self.assertIn(RELATION_FILE_OVERLAP, text)
        self.assertIn(RELATION_DECLARED_DEPENDENCY, text)
        self.assertIn(INTAKE_MISSING_GOAL_ID, text)
        self.assertIn("advisory: true", text)

    def test_empty_request_renders_without_a_hole(self):
        plan = build_multi_goal_workstream_plan(())
        text = render_multi_goal_plan_journal(plan)
        self.assertIn("- workstreams:", text)
        self.assertIn("  - none", text)
        self.assertEqual(plan.workstreams, ())
        self.assertTrue(plan.plan_digest)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
