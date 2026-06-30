"""Redmine fixed_version lane bucket provider tests (Redmine #12919).

Drives the concrete provider over in-memory Redmine snapshots (the production
``RedmineFixedVersionLaneBucketProvider`` itself is the test double — no mock library,
matching the f_120 convention). Covers: structured bucket result (id / name / source
kind / issues / parent US / status / dates), open-leaf enumeration, the runtime
:class:`LaneBucketProvider` protocol conformance, the umbrella vs. per-child execution
bucket judgment, and every fail-closed skip reason (missing fixed_version, closed
issue, closed / locked Version, unknown bucket, issue absent from snapshot).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.lane_bucket_provider import (  # noqa: E402
    SKIP_AMBIGUOUS_SOURCE,
    SKIP_BUCKET_NOT_FOUND,
    SKIP_ISSUE_CLOSED,
    SKIP_NO_FIXED_VERSION,
    SKIP_VERSION_CLOSED,
    SKIP_VERSION_LOCKED,
    SOURCE_KIND_FIXED_VERSION,
    LaneBucketProvider,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.fixed_version_lane_bucket_provider import (  # noqa: E402
    RedmineFixedVersionLaneBucketProvider,
    RedmineVersionState,
)


def _issue(
    issue_id: int,
    *,
    tracker: str = "Task",
    status: str = "新規",
    closed: bool = False,
    parent: int | None = None,
    fixed_version: int | None = 292,
) -> dict:
    payload: dict = {
        "id": issue_id,
        "tracker": {"name": tracker},
        "status": {"name": status, "is_closed": closed},
    }
    if parent is not None:
        payload["parent"] = {"id": parent}
    if fixed_version is not None:
        payload["fixed_version"] = {"id": fixed_version, "name": "ワークフロー枠"}
    return payload


_VERSIONS = {
    "versions": [
        {
            "id": 292,
            "name": "ワークフロー管制基盤整備枠",
            "status": "open",
            "due_date": "2027-10-08",
            "created_on": "2026-06-01",
        },
        {"id": 999, "name": "閉鎖枠", "status": "closed"},
        {"id": 998, "name": "施錠枠", "status": "locked"},
    ]
}


class ProviderConformanceTest(unittest.TestCase):
    def test_is_lane_bucket_provider(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider()
        self.assertIsInstance(provider, LaneBucketProvider)
        self.assertEqual(provider.source_kind, SOURCE_KIND_FIXED_VERSION)


class ResolveBucketTest(unittest.TestCase):
    def _provider(self) -> RedmineFixedVersionLaneBucketProvider:
        issues = {
            "issues": [
                _issue(12919, tracker="開発", status="着手中", parent=12670),
                _issue(12920, parent=12919),
                _issue(12921, parent=12919, closed=True, status="終了"),
            ]
        }
        return RedmineFixedVersionLaneBucketProvider(
            issues_payload=issues, versions_payload=_VERSIONS
        )

    def test_structured_result_fields(self) -> None:
        resolution = self._provider().resolve_bucket("292")
        self.assertTrue(resolution.resolved)
        bucket = resolution.bucket
        assert bucket is not None
        self.assertEqual(bucket.bucket_id, "292")
        self.assertEqual(bucket.source_kind, SOURCE_KIND_FIXED_VERSION)
        self.assertEqual(bucket.name, "ワークフロー管制基盤整備枠")
        self.assertEqual(bucket.status, "open")
        self.assertEqual(bucket.due_date, "2027-10-08")
        self.assertEqual(bucket.start_date, "2026-06-01")
        self.assertEqual(bucket.total_issues, 3)
        self.assertEqual(bucket.total_open, 2)

    def test_open_leaf_enumeration(self) -> None:
        bucket = self._provider().resolve_bucket("292").bucket
        assert bucket is not None
        # 12919 is an open parent of 12920 -> not a leaf; 12920 is the work leaf;
        # 12921 is closed -> not a leaf.
        self.assertEqual({i.issue_id for i in bucket.open_leaf_issues}, {"12920"})

    def test_bucket_level_umbrella_when_two_parents(self) -> None:
        # bucket holds children of two distinct parents -> umbrella, no single parent US
        bucket = self._provider().resolve_bucket("292").bucket
        assert bucket is not None
        self.assertTrue(bucket.is_umbrella)
        self.assertIsNone(bucket.parent_us)

    def test_single_parent_us_recorded(self) -> None:
        issues = {"issues": [_issue(1, parent=100), _issue(2, parent=100)]}
        bucket = RedmineFixedVersionLaneBucketProvider(
            issues_payload=issues, versions_payload=_VERSIONS
        ).resolve_bucket("292").bucket
        assert bucket is not None
        self.assertFalse(bucket.is_umbrella)
        self.assertEqual(bucket.parent_us, "100")

    def test_version_state_derived_without_versions_payload(self) -> None:
        # No versions snapshot: name / status / dates come from the embedded fixed_version.
        issues = {
            "issues": [
                {
                    "id": 1,
                    "status": {"name": "新規", "is_closed": False},
                    "fixed_version": {
                        "id": 292,
                        "name": "枠",
                        "status": "open",
                        "effective_date": "2027-10-08",
                    },
                }
            ]
        }
        bucket = RedmineFixedVersionLaneBucketProvider(issues_payload=issues).resolve_bucket(
            "292"
        ).bucket
        assert bucket is not None
        self.assertEqual(bucket.name, "枠")
        self.assertEqual(bucket.due_date, "2027-10-08")

    def test_bare_list_issues_payload(self) -> None:
        bucket = RedmineFixedVersionLaneBucketProvider(
            issues_payload=[_issue(1)]
        ).resolve_bucket("292").bucket
        assert bucket is not None
        self.assertEqual(bucket.total_open, 1)


class FailClosedSkipTest(unittest.TestCase):
    def test_closed_version(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider(
            issues_payload=[_issue(1, fixed_version=999)], versions_payload=_VERSIONS
        )
        resolution = provider.resolve_bucket("999")
        self.assertFalse(resolution.resolved)
        assert resolution.skip is not None
        self.assertEqual(resolution.skip.reason, SKIP_VERSION_CLOSED)

    def test_locked_version(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider(
            issues_payload=[_issue(1, fixed_version=998)], versions_payload=_VERSIONS
        )
        self.assertEqual(
            provider.resolve_bucket("998").skip.reason, SKIP_VERSION_LOCKED
        )

    def test_unknown_bucket(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider(
            issues_payload=[_issue(1)], versions_payload=_VERSIONS
        )
        self.assertEqual(
            provider.resolve_bucket("404040").skip.reason, SKIP_BUCKET_NOT_FOUND
        )

    def test_empty_bucket_id(self) -> None:
        self.assertEqual(
            RedmineFixedVersionLaneBucketProvider().resolve_bucket("  ").skip.reason,
            SKIP_AMBIGUOUS_SOURCE,
        )


class ResolveIssueBucketTest(unittest.TestCase):
    def test_issue_with_no_fixed_version(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider(
            issues_payload=[_issue(1, fixed_version=None)], versions_payload=_VERSIONS
        )
        self.assertEqual(
            provider.resolve_issue_bucket("1").skip.reason, SKIP_NO_FIXED_VERSION
        )

    def test_closed_issue(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider(
            issues_payload=[_issue(1, closed=True, status="終了")],
            versions_payload=_VERSIONS,
        )
        self.assertEqual(
            provider.resolve_issue_bucket("1").skip.reason, SKIP_ISSUE_CLOSED
        )

    def test_issue_absent_from_snapshot(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider(issues_payload=[_issue(1)])
        skip = provider.resolve_issue_bucket("777").skip
        assert skip is not None
        self.assertEqual(skip.reason, SKIP_AMBIGUOUS_SOURCE)

    def test_open_issue_resolves_to_its_bucket(self) -> None:
        provider = RedmineFixedVersionLaneBucketProvider(
            issues_payload=[_issue(1, fixed_version=292)], versions_payload=_VERSIONS
        )
        resolution = provider.resolve_issue_bucket("1")
        self.assertTrue(resolution.resolved)
        self.assertEqual(resolution.bucket.bucket_id, "292")


class ResolveExecutionBucketTest(unittest.TestCase):
    def test_umbrella_parent_children_span_buckets(self) -> None:
        issues = {
            "issues": [
                _issue(100, fixed_version=276),  # parent US, own bucket
                _issue(1, parent=100, fixed_version=292),
                _issue(2, parent=100, fixed_version=303),
            ]
        }
        decision = RedmineFixedVersionLaneBucketProvider(
            issues_payload=issues
        ).resolve_execution_bucket("100")
        self.assertTrue(decision.is_umbrella)
        self.assertEqual(decision.child_buckets, ("292", "303"))
        self.assertEqual(decision.parent_bucket, "276")  # recorded, not authoritative
        self.assertEqual(decision.execution_bucket_for("1"), "292")
        self.assertEqual(decision.execution_bucket_for("2"), "303")

    def test_single_bucket_parent_not_umbrella(self) -> None:
        issues = {
            "issues": [
                _issue(100, fixed_version=292),
                _issue(1, parent=100, fixed_version=292),
                _issue(2, parent=100, fixed_version=292),
            ]
        }
        decision = RedmineFixedVersionLaneBucketProvider(
            issues_payload=issues
        ).resolve_execution_bucket("100")
        self.assertFalse(decision.is_umbrella)
        self.assertEqual(decision.child_buckets, ("292",))


class RedmineVersionStateTest(unittest.TestCase):
    def test_from_mapping_lowercases_status_and_maps_dates(self) -> None:
        state = RedmineVersionState.from_mapping(
            {"id": 292, "name": "x", "status": "OPEN", "effective_date": "2027-10-08"}
        )
        assert state is not None
        self.assertEqual(state.status, "open")
        self.assertEqual(state.due_date, "2027-10-08")

    def test_from_mapping_without_id_is_none(self) -> None:
        self.assertIsNone(RedmineVersionState.from_mapping({"name": "x"}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
