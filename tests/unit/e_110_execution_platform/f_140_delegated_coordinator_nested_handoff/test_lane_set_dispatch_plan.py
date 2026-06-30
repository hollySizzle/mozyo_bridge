"""Unit tests for the Version-bucket lane-set dispatch plan (Redmine #12920).

Covers the pure plan builder and its two reused authorities:

- candidate classification maps the #12921 admission decision onto the plan vocabulary
  (dispatchable / standby / blocked / needs_owner_decision), per candidate, against the
  shared active lanes and the per-candidate risk facts;
- the coordinator-owned queue projection groups active lanes by #12856 state class
  (review / owner / integration waiting and the rest);
- closed / non-leaf bucket issues are recorded as skipped, not silently dropped;
- a bucket-level skip (closed / missing Version) yields an unresolved plan with no
  candidates;
- the acceptance per-candidate fields (issue / tracker / parent / bucket / expected
  surface / skip reason / route) and the mode invariant.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
    ADMIT_ALLOW_DISPATCH,
    ADMIT_BLOCKED,
    ADMIT_NEEDS_OWNER_DECISION,
    ADMIT_SERIALIZE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_set_dispatch_plan import (
    MODE_DRY_RUN,
    MODE_EXECUTE,
    PLAN_BLOCKED,
    PLAN_DISPATCHABLE,
    PLAN_NEEDS_OWNER_DECISION,
    PLAN_STANDBY,
    RECOMMENDED_ROUTE,
    CandidateDispatchFacts,
    LaneSetDispatchPlanError,
    _ADMISSION_TO_PLAN,
    build_dispatch_plan,
    project_coordinator_queue,
    render_dispatch_plan_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    LaneSignal,
)
from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.lane_bucket_provider import (
    SKIP_VERSION_CLOSED,
    BucketResolution,
    BucketSkip,
    LaneBucket,
    LaneBucketIssue,
    mark_leaves,
)


def _bucket(*issues: LaneBucketIssue, bucket_id: str = "292", name: str = "枠") -> LaneBucket:
    marked = mark_leaves(issues)
    return LaneBucket(
        bucket_id=bucket_id,
        source_kind="fixed_version",
        name=name,
        status="open",
        issues=marked,
    )


def _leaf(issue_id: str, parent: str = "99", tracker: str = "Task") -> LaneBucketIssue:
    return LaneBucketIssue(issue_id=issue_id, tracker=tracker, parent_id=parent)


class AdmissionMappingTest(unittest.TestCase):
    def test_every_admission_decision_maps_to_a_plan_class(self):
        self.assertEqual(_ADMISSION_TO_PLAN[ADMIT_ALLOW_DISPATCH], PLAN_DISPATCHABLE)
        self.assertEqual(_ADMISSION_TO_PLAN[ADMIT_SERIALIZE], PLAN_STANDBY)
        self.assertEqual(_ADMISSION_TO_PLAN[ADMIT_BLOCKED], PLAN_BLOCKED)
        self.assertEqual(
            _ADMISSION_TO_PLAN[ADMIT_NEEDS_OWNER_DECISION], PLAN_NEEDS_OWNER_DECISION
        )


class QueueProjectionTest(unittest.TestCase):
    def test_groups_lanes_by_state_class(self):
        queue = project_coordinator_queue(
            (
                LaneSignal(issue="100", latest_gate="review_request"),
                LaneSignal(
                    issue="101", latest_gate="review", review_conclusion="approved"
                ),
                LaneSignal(
                    issue="102",
                    latest_gate="owner_close_approval",
                    commit_bearing=True,
                ),
                LaneSignal(issue="103", latest_gate="start"),
            )
        )
        self.assertEqual(queue.total_active, 4)
        self.assertEqual(queue.review_waiting, ("100",))
        self.assertEqual(queue.owner_waiting, ("101",))
        self.assertEqual(queue.integration_waiting, ("102",))
        self.assertEqual(queue.implementing, ("103",))
        # active_lanes preserves input order with the resolved state class.
        self.assertEqual(queue.active_lanes[0], ("100", "review_waiting"))


class ClassificationTest(unittest.TestCase):
    def test_no_risk_is_dispatchable(self):
        plan = build_dispatch_plan(BucketResolution.of(_bucket(_leaf("1"))))
        self.assertTrue(plan.resolved)
        self.assertEqual(len(plan.candidates), 1)
        candidate = plan.candidates[0]
        self.assertEqual(candidate.classification, PLAN_DISPATCHABLE)
        self.assertTrue(candidate.dispatchable)
        self.assertEqual(candidate.skip_reason, "")
        self.assertEqual(candidate.recommended_route, RECOMMENDED_ROUTE)

    def test_file_overlap_is_standby(self):
        plan = build_dispatch_plan(
            BucketResolution.of(_bucket(_leaf("1"))),
            active_lane_signals=(LaneSignal(issue="500", latest_gate="start"),),
            candidate_facts={"1": CandidateDispatchFacts(file_overlap_lanes=("500",))},
        )
        candidate = plan.candidates[0]
        self.assertEqual(candidate.classification, PLAN_STANDBY)
        self.assertEqual(candidate.admission_decision, ADMIT_SERIALIZE)
        self.assertIn("file_overlap", candidate.skip_reason)

    def test_dependency_on_blocked_lane_is_blocked(self):
        plan = build_dispatch_plan(
            BucketResolution.of(_bucket(_leaf("1"))),
            active_lane_signals=(LaneSignal(issue="500", latest_gate="blocked"),),
            candidate_facts={"1": CandidateDispatchFacts(dependency_lanes=("500",))},
        )
        candidate = plan.candidates[0]
        self.assertEqual(candidate.classification, PLAN_BLOCKED)
        self.assertEqual(candidate.admission_decision, ADMIT_BLOCKED)

    def test_release_gate_needs_owner_decision(self):
        plan = build_dispatch_plan(
            BucketResolution.of(_bucket(_leaf("1"))),
            candidate_facts={
                "1": CandidateDispatchFacts(release_publish_gate_active=True)
            },
        )
        candidate = plan.candidates[0]
        self.assertEqual(candidate.classification, PLAN_NEEDS_OWNER_DECISION)

    def test_coordinator_convenience_alone_stays_dispatchable(self):
        # The owner correction (#12670 j#69283), end to end: a convenience-only flag is
        # recorded but never moves the candidate off dispatchable.
        plan = build_dispatch_plan(
            BucketResolution.of(_bucket(_leaf("1"))),
            candidate_facts={"1": CandidateDispatchFacts(broad_bucket_only=True)},
        )
        candidate = plan.candidates[0]
        self.assertEqual(candidate.classification, PLAN_DISPATCHABLE)
        self.assertIn("broad_bucket", candidate.rejected_nonreasons)

    def test_expected_surface_carried_through(self):
        plan = build_dispatch_plan(
            BucketResolution.of(_bucket(_leaf("1"))),
            candidate_facts={
                "1": CandidateDispatchFacts(expected_changed_surface="src/foo.py")
            },
        )
        self.assertEqual(plan.candidates[0].expected_changed_surface, "src/foo.py")


class EnumerationTest(unittest.TestCase):
    def test_closed_and_non_leaf_issues_are_skipped(self):
        bucket = _bucket(
            _leaf("1"),
            _leaf("2"),
            LaneBucketIssue(issue_id="99", tracker="User Story"),  # parent -> non-leaf
            LaneBucketIssue(issue_id="3", tracker="Task", is_closed=True),
        )
        plan = build_dispatch_plan(BucketResolution.of(bucket))
        candidate_ids = {c.issue_id for c in plan.candidates}
        skipped = {s.issue_id: s.reason for s in plan.skipped_issues}
        self.assertEqual(candidate_ids, {"1", "2"})
        self.assertEqual(skipped["99"], "not_leaf")
        self.assertEqual(skipped["3"], "issue_closed")

    def test_counts_by_classification(self):
        plan = build_dispatch_plan(
            BucketResolution.of(_bucket(_leaf("1"), _leaf("2"))),
            active_lane_signals=(LaneSignal(issue="500", latest_gate="start"),),
            candidate_facts={"2": CandidateDispatchFacts(file_overlap_lanes=("500",))},
        )
        counts = plan.counts_by_classification
        self.assertEqual(counts[PLAN_DISPATCHABLE], 1)
        self.assertEqual(counts[PLAN_STANDBY], 1)
        self.assertEqual(counts[PLAN_BLOCKED], 0)
        self.assertEqual(len(plan.dispatchable_candidates), 1)


class BucketSkipTest(unittest.TestCase):
    def test_unresolved_bucket_has_no_candidates(self):
        skip = BucketSkip(SKIP_VERSION_CLOSED, detail="version status is 'closed'", bucket_id="292")
        plan = build_dispatch_plan(BucketResolution.skipped(skip))
        self.assertFalse(plan.resolved)
        self.assertEqual(plan.candidates, ())
        self.assertIsNotNone(plan.bucket_skip)
        self.assertEqual(plan.bucket_skip["reason"], SKIP_VERSION_CLOSED)
        self.assertEqual(plan.bucket_id, "292")

    def test_skip_still_projects_queue(self):
        skip = BucketSkip(SKIP_VERSION_CLOSED, bucket_id="292")
        plan = build_dispatch_plan(
            BucketResolution.skipped(skip),
            active_lane_signals=(LaneSignal(issue="500", latest_gate="review_request"),),
        )
        self.assertEqual(plan.queue_state.review_waiting, ("500",))


class ModeTest(unittest.TestCase):
    def test_default_mode_is_dry_run(self):
        plan = build_dispatch_plan(BucketResolution.of(_bucket(_leaf("1"))))
        self.assertEqual(plan.mode, MODE_DRY_RUN)

    def test_execute_mode_is_identical_and_side_effect_free(self):
        resolution = BucketResolution.of(_bucket(_leaf("1")))
        dry = build_dispatch_plan(resolution, mode=MODE_DRY_RUN)
        execute = build_dispatch_plan(resolution, mode=MODE_EXECUTE)
        # Only the mode label differs; candidates / route are identical (no auto-dispatch).
        self.assertEqual(execute.mode, MODE_EXECUTE)
        self.assertEqual(
            [c.as_payload() for c in dry.candidates],
            [c.as_payload() for c in execute.candidates],
        )

    def test_unknown_mode_rejected(self):
        with self.assertRaises(LaneSetDispatchPlanError):
            build_dispatch_plan(
                BucketResolution.of(_bucket(_leaf("1"))), mode="ship-it"
            )


class JournalTest(unittest.TestCase):
    def test_journal_renders_queue_and_candidates(self):
        plan = build_dispatch_plan(
            BucketResolution.of(_bucket(_leaf("1"))),
            active_lane_signals=(LaneSignal(issue="500", latest_gate="review_request"),),
        )
        text = render_dispatch_plan_journal(plan)
        self.assertIn("## Lane-set dispatch plan", text)
        self.assertIn("review_waiting: 500", text)
        self.assertIn("dispatchable: 1", text)
        self.assertIn(RECOMMENDED_ROUTE, text)

    def test_journal_renders_bucket_skip(self):
        plan = build_dispatch_plan(
            BucketResolution.skipped(BucketSkip(SKIP_VERSION_CLOSED, bucket_id="292"))
        )
        text = render_dispatch_plan_journal(plan)
        self.assertIn("bucket_skip: version_closed", text)


if __name__ == "__main__":
    unittest.main()
