"""Open-leaf Version enumeration tests (Redmine #12651).

Covers: REST issues.json parsing (nested tracker/status/parent, missing-id
drop), the leaf rule (an open issue with an open child in-set is a non-leaf),
closed-issue exclusion, per-tracker counts, and the Mapping source adapter over
both the ``{"issues": [...]}`` shape and a bare list. Pure; no network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.redmine_version_enumeration import (
    MappingRedmineVersionIssueSource,
    RedmineVersionIssueSource,
    VersionIssue,
    enumerate_from_source,
    enumerate_open_leaf_issues,
)

# A US (#900) with an open Task (#901), a closed Bug (#902), and an open Test
# (#903) as children — the Task/Test leaves are exactly what the MCP US-only
# surface cannot enumerate.
_SNAPSHOT = {
    "issues": [
        {"id": 900, "tracker": {"name": "UserStory"}, "status": {"name": "着手中", "is_closed": False}, "parent": {"id": 800}},
        {"id": 901, "tracker": {"name": "Task"}, "status": {"name": "未着手", "is_closed": False}, "parent": {"id": 900}},
        {"id": 902, "tracker": {"name": "Bug"}, "status": {"name": "終了", "is_closed": True}, "parent": {"id": 900}},
        {"id": 903, "tracker": {"name": "Test"}, "status": {"name": "未着手", "is_closed": False}, "parent": {"id": 900}},
    ]
}


class ParseTest(unittest.TestCase):
    def test_from_mapping_reads_nested_rest_shape(self) -> None:
        issue = VersionIssue.from_mapping(_SNAPSHOT["issues"][1])
        assert issue is not None
        self.assertEqual(issue.issue_id, "901")
        self.assertEqual(issue.tracker, "Task")
        self.assertFalse(issue.is_closed)
        self.assertEqual(issue.parent_id, "900")

    def test_from_mapping_without_id_is_dropped(self) -> None:
        self.assertIsNone(VersionIssue.from_mapping({"tracker": {"name": "Task"}}))

    def test_from_mapping_non_mapping_is_dropped(self) -> None:
        self.assertIsNone(VersionIssue.from_mapping("not-a-mapping"))  # type: ignore[arg-type]


class EnumerationTest(unittest.TestCase):
    def setUp(self) -> None:
        source = MappingRedmineVersionIssueSource(_SNAPSHOT)
        self.result = enumerate_from_source(source, "248")

    def test_open_leaves_are_the_open_task_and_test(self) -> None:
        leaf_ids = {i.issue_id for i in self.result.open_leaf_issues}
        self.assertEqual(leaf_ids, {"901", "903"})

    def test_parent_with_open_child_is_a_nonleaf(self) -> None:
        nonleaf_ids = {i.issue_id for i in self.result.open_nonleaf_issues}
        self.assertEqual(nonleaf_ids, {"900"})

    def test_closed_issue_excluded(self) -> None:
        all_ids = {i.issue_id for i in self.result.open_leaf_issues} | {
            i.issue_id for i in self.result.open_nonleaf_issues
        }
        self.assertNotIn("902", all_ids)

    def test_counts_and_totals(self) -> None:
        self.assertEqual(self.result.total_issues, 4)
        self.assertEqual(self.result.total_open, 3)
        self.assertEqual(dict(self.result.counts_by_tracker), {"Task": 1, "Test": 1})

    def test_as_dict_round_trip_shape(self) -> None:
        payload = self.result.as_dict()
        self.assertEqual(payload["open_leaf_count"], 2)
        self.assertEqual(payload["version_id"], "248")


class LeafRuleEdgeTest(unittest.TestCase):
    def test_all_independent_open_issues_are_leaves(self) -> None:
        issues = [
            VersionIssue("1", "Task", "open", False, None),
            VersionIssue("2", "Bug", "open", False, None),
        ]
        result = enumerate_open_leaf_issues(issues, "v")
        self.assertEqual(len(result.open_leaf_issues), 2)
        self.assertEqual(len(result.open_nonleaf_issues), 0)

    def test_parent_whose_only_child_is_closed_is_a_leaf(self) -> None:
        issues = [
            VersionIssue("10", "UserStory", "open", False, None),
            VersionIssue("11", "Task", "closed", True, "10"),
        ]
        result = enumerate_open_leaf_issues(issues, "v")
        self.assertEqual({i.issue_id for i in result.open_leaf_issues}, {"10"})


class MappingSourceTest(unittest.TestCase):
    def test_source_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(
            MappingRedmineVersionIssueSource(_SNAPSHOT), RedmineVersionIssueSource
        )

    def test_bare_list_payload_supported(self) -> None:
        source = MappingRedmineVersionIssueSource(_SNAPSHOT["issues"])
        self.assertEqual(len(source.read_version_issues("x")), 4)

    def test_malformed_payload_yields_no_issues(self) -> None:
        self.assertEqual(
            MappingRedmineVersionIssueSource("garbage").read_version_issues("x"), []
        )
        self.assertEqual(
            MappingRedmineVersionIssueSource({"no_issues_key": 1}).read_version_issues("x"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
