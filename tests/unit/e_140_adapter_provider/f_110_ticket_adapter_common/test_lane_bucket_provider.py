"""Lane bucket provider neutral-boundary tests (Redmine #12919).

Covers the core-owned vocabulary and pure decisions of the provider-neutral seam:
the closed skip-reason invariant, the Version-status -> skip mapping, the leaf rule,
the bucket-level umbrella reading, the cross-bucket execution-bucket judgment
(#12919 AC4), and the :class:`BucketResolution` exactly-one invariant. No provider /
network is involved — these are pure values and functions.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.lane_bucket_provider import (  # noqa: E402
    BUCKET_SKIP_REASONS,
    SKIP_VERSION_CLOSED,
    SKIP_VERSION_LOCKED,
    SOURCE_KIND_FIXED_VERSION,
    BucketResolution,
    BucketSkip,
    LaneBucket,
    LaneBucketError,
    LaneBucketIssue,
    decide_execution_bucket,
    distinct_parents,
    mark_leaves,
    version_status_skip_reason,
)


class BucketSkipInvariantTest(unittest.TestCase):
    def test_unknown_reason_rejected(self) -> None:
        with self.assertRaises(LaneBucketError):
            BucketSkip("totally_made_up")

    def test_known_reasons_accepted(self) -> None:
        for reason in BUCKET_SKIP_REASONS:
            self.assertEqual(BucketSkip(reason).reason, reason)


class VersionStatusSkipTest(unittest.TestCase):
    def test_closed_and_locked_map_to_skip(self) -> None:
        self.assertEqual(version_status_skip_reason("closed"), SKIP_VERSION_CLOSED)
        self.assertEqual(version_status_skip_reason("LOCKED"), SKIP_VERSION_LOCKED)

    def test_open_unknown_and_none_do_not_skip(self) -> None:
        self.assertIsNone(version_status_skip_reason("open"))
        self.assertIsNone(version_status_skip_reason("archived"))
        self.assertIsNone(version_status_skip_reason(None))


class MarkLeavesTest(unittest.TestCase):
    def test_open_issue_is_leaf_unless_an_open_issue_names_it_parent(self) -> None:
        issues = (
            LaneBucketIssue(issue_id="100", parent_id=None),  # open parent of 200 -> not leaf
            LaneBucketIssue(issue_id="200", parent_id="100"),  # open parent of 300 -> not leaf
            LaneBucketIssue(issue_id="300", parent_id="200"),  # work leaf
        )
        marked = {i.issue_id: i.is_leaf for i in mark_leaves(issues)}
        self.assertEqual(marked, {"100": False, "200": False, "300": True})

    def test_closed_issue_is_never_a_leaf_and_does_not_parent_others(self) -> None:
        issues = (
            LaneBucketIssue(issue_id="10", parent_id=None, is_closed=True),
            # parent (10) is closed, so 20 is still a leaf candidate
            LaneBucketIssue(issue_id="20", parent_id="10"),
        )
        marked = {i.issue_id: i.is_leaf for i in mark_leaves(issues)}
        self.assertFalse(marked["10"])
        self.assertTrue(marked["20"])


class LaneBucketReadModelTest(unittest.TestCase):
    def _bucket(self) -> LaneBucket:
        issues = mark_leaves(
            (
                LaneBucketIssue(issue_id="1", tracker="Task", parent_id="100"),
                LaneBucketIssue(issue_id="2", tracker="Test", parent_id="100"),
                LaneBucketIssue(
                    issue_id="3", tracker="Bug", parent_id="100", is_closed=True
                ),
            )
        )
        return LaneBucket(
            bucket_id="292",
            source_kind=SOURCE_KIND_FIXED_VERSION,
            name="bucket",
            status="open",
            issues=issues,
            parent_us="100",
        )

    def test_open_leaf_counts_and_tracker_breakdown(self) -> None:
        bucket = self._bucket()
        self.assertEqual(bucket.total_issues, 3)
        self.assertEqual(bucket.total_open, 2)
        self.assertEqual(
            {i.issue_id for i in bucket.open_leaf_issues}, {"1", "2"}
        )
        self.assertEqual(bucket.counts_by_tracker, {"Task": 1, "Test": 1})

    def test_as_dict_is_serializable_shape(self) -> None:
        payload = self._bucket().as_dict()
        self.assertEqual(payload["source_kind"], SOURCE_KIND_FIXED_VERSION)
        self.assertEqual(payload["parent_us"], "100")
        self.assertIn("open_leaf_issues", payload)


class BucketResolutionInvariantTest(unittest.TestCase):
    def test_must_carry_exactly_one(self) -> None:
        with self.assertRaises(LaneBucketError):
            BucketResolution()  # neither
        with self.assertRaises(LaneBucketError):
            BucketResolution(
                bucket=LaneBucket("1", SOURCE_KIND_FIXED_VERSION),
                skip=BucketSkip(SKIP_VERSION_CLOSED),
            )  # both

    def test_constructors(self) -> None:
        ok = BucketResolution.of(LaneBucket("1", SOURCE_KIND_FIXED_VERSION))
        self.assertTrue(ok.resolved)
        skipped = BucketResolution.skipped(BucketSkip(SKIP_VERSION_CLOSED))
        self.assertFalse(skipped.resolved)


class DecideExecutionBucketTest(unittest.TestCase):
    def test_umbrella_when_children_span_multiple_buckets(self) -> None:
        decision = decide_execution_bucket(
            "100",
            [("1", "292"), ("2", "303")],
            parent_bucket="276",
        )
        self.assertTrue(decision.is_umbrella)
        self.assertEqual(decision.child_buckets, ("292", "303"))
        # parent bucket is recorded but NOT authoritative for an umbrella
        self.assertEqual(decision.parent_bucket, "276")
        self.assertEqual(decision.execution_bucket_for("1"), "292")
        self.assertEqual(decision.execution_bucket_for("2"), "303")

    def test_single_shared_bucket_is_not_umbrella(self) -> None:
        decision = decide_execution_bucket(
            "100", [("1", "292"), ("2", "292")], parent_bucket="292"
        )
        self.assertFalse(decision.is_umbrella)
        self.assertEqual(decision.child_buckets, ("292",))

    def test_child_without_bucket_recorded_not_guessed(self) -> None:
        decision = decide_execution_bucket("100", [("1", "292"), ("2", None)])
        self.assertFalse(decision.is_umbrella)  # one distinct bucket only
        self.assertIsNone(decision.execution_bucket_for("2"))
        self.assertEqual(decision.child_buckets, ("292",))

    def test_no_children(self) -> None:
        decision = decide_execution_bucket("100", [])
        self.assertFalse(decision.is_umbrella)
        self.assertEqual(decision.child_buckets, ())


class DistinctParentsTest(unittest.TestCase):
    def test_sorted_deduped_parents(self) -> None:
        issues = (
            LaneBucketIssue(issue_id="1", parent_id="100"),
            LaneBucketIssue(issue_id="2", parent_id="100"),
            LaneBucketIssue(issue_id="3", parent_id="200"),
            LaneBucketIssue(issue_id="4", parent_id=None),
        )
        self.assertEqual(distinct_parents(issues), ("100", "200"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
