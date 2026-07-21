"""Callback supervisor authoritative workspace partition — pure/isolated units (Redmine #13968).

The host-global roster fold, the durable authoritative-workspace resolver, and the
latest-generation dispatch-anchor candidate fence, each verified in isolation (a single subject,
no wired collaborators). The multi-collaborator supervisor E2E lives in the integration twin
(`tests/integration/.../test_supervisor_workspace_partition.py`) per the tests-placement policy.

- :class:`EnumerateActiveLanesPartitionTest` — the roster fold partitions host-global views by
  the lane's durable ``workspace_id`` (review R1 original fix);
- :class:`AuthoritativeWorkspaceResolveTest` — the durable owner authority selects THE unique
  authoritative workspace per issue; zero / ambiguous owners are omitted (review F1);
- :class:`CandidateFenceTest` — general callback candidates older than the current dispatch anchor
  are dropped, and an unresolvable anchor fails closed (review F2).
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    glance_snapshot_source as gss,
    sublane_herdr_projection as proj,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
    enumerate_active_lanes,
    enumerate_active_lanes_for_workspace,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    authoritative_workspace_by_issue,
    fence_candidates_to_anchor,
    make_send_edge_fence,
    partition_authoritative,
)

WS_A = "wsAuthoritative"
WS_B = "wsForeign"
ISSUE = "13811"


@dataclass
class _View:
    """The minimal ``SublaneLaneView`` shape the roster fold reads (issue + workspace_id)."""

    issue: str
    lane_label: str
    lane_id: str
    workspace_id: str
    state: str = "active"


@dataclass
class _Cand:
    """The minimal candidate shape the anchor fence reads (its journal id)."""

    journal: str


@dataclass
class _Row:
    """The minimal outbox-row shape the send-edge fence reads (journal + route)."""

    journal: str
    callback_route: str = "coordinator"


class EnumerateActiveLanesPartitionTest(unittest.TestCase):
    """Review R1 (pure): the host-global inventory partitions by durable workspace_id."""

    def _host_global_views(self):
        # The SAME issue id is live under TWO distinct workspaces (its authoritative one and a
        # foreign lane that also carries it) — the reproduction where an active issue was visible
        # in more than one registry workspace's roster.
        return [
            _View(ISSUE, "issue_13811_a", "issue_13811_a", WS_A),
            _View("13933", "issue_13933_a", "issue_13933_a", WS_A),
            _View(ISSUE, "issue_13811_foreign", "issue_13811_foreign", WS_B),
        ]

    def _patched(self):
        return patch.multiple(
            proj,
            repo_backend_is_herdr=lambda repo_root: True,
            herdr_sublane_views=lambda repo_root, **_kw: self._host_global_views(),
        )

    def test_partition_returns_only_the_requested_workspace_lanes(self) -> None:
        with self._patched(), patch.object(gss, "_lifecycle_disposition_by_unit", return_value={}):
            roster_a, err_a = enumerate_active_lanes_for_workspace(Path("."), workspace_id=WS_A)
            roster_b, err_b = enumerate_active_lanes_for_workspace(Path("."), workspace_id=WS_B)
            roster_foreign, err_f = enumerate_active_lanes_for_workspace(
                Path("."), workspace_id="wsNobody"
            )
        self.assertIsNone(err_a)
        self.assertIsNone(err_b)
        self.assertIsNone(err_f)
        self.assertEqual(roster_a, ((ISSUE, "issue_13811_a"), ("13933", "issue_13933_a")))
        self.assertEqual(roster_b, ((ISSUE, "issue_13811_foreign"),))
        self.assertEqual(roster_foreign, ())

    def test_blank_workspace_id_matches_nothing(self) -> None:
        with self._patched(), patch.object(gss, "_lifecycle_disposition_by_unit", return_value={}):
            roster, err = enumerate_active_lanes_for_workspace(Path("."), workspace_id="")
        self.assertIsNone(err)
        self.assertEqual(roster, ())

    def test_host_global_enumeration_unchanged(self) -> None:
        with self._patched(), patch.object(gss, "_lifecycle_disposition_by_unit", return_value={}):
            roster, err = enumerate_active_lanes(Path("."))
        self.assertIsNone(err)
        self.assertEqual(
            set(roster),
            {(ISSUE, "issue_13811_a"), ("13933", "issue_13933_a"), (ISSUE, "issue_13811_foreign")},
        )


class AuthoritativeWorkspaceResolveTest(unittest.TestCase):
    """Review F1 (pure): the durable owner authority selects one authoritative workspace / issue."""

    def test_unique_owner_maps_to_its_workspace(self) -> None:
        m = authoritative_workspace_by_issue([(WS_A, ISSUE), (WS_A, "13933")])
        self.assertEqual(m, {ISSUE: WS_A, "13933": WS_A})

    def test_issue_owned_by_two_workspaces_is_omitted(self) -> None:
        # The exact ambiguity the outbox's workspace-partitioned key would otherwise double-deliver:
        # no unique authoritative workspace -> supervised nowhere (fail-closed).
        m = authoritative_workspace_by_issue([(WS_A, ISSUE), (WS_B, ISSUE)])
        self.assertNotIn(ISSUE, m)

    def test_blank_pairs_ignored(self) -> None:
        m = authoritative_workspace_by_issue([("", ISSUE), (WS_A, ""), (WS_A, ISSUE)])
        self.assertEqual(m, {ISSUE: WS_A})

    def test_partition_keeps_only_this_workspaces_issues(self) -> None:
        authoritative = {ISSUE: WS_A, "13933": WS_B}
        kept, dropped = partition_authoritative((ISSUE, "13933", "99999"), authoritative, WS_A)
        # #13811 owned here (kept); #13933 owned elsewhere + #99999 unowned (dropped).
        self.assertEqual(kept, (ISSUE,))
        self.assertEqual(dropped, ("13933", "99999"))


class CandidateFenceTest(unittest.TestCase):
    """Review F2 (pure): general callback candidates fenced to the current dispatch anchor."""

    def test_older_than_anchor_dropped_newer_kept(self) -> None:
        cands = [_Cand("100"), _Cand("205"), _Cand("206"), _Cand("300")]
        kept, dropped = fence_candidates_to_anchor(cands, "206")
        # Anchor 206: journals >= 206 are the current generation; < 206 are historical.
        self.assertEqual([c.journal for c in kept], ["206", "300"])
        self.assertEqual([c.journal for c in dropped], ["100", "205"])

    def test_none_anchor_drops_all(self) -> None:
        cands = [_Cand("100"), _Cand("300")]
        kept, dropped = fence_candidates_to_anchor(cands, None)
        self.assertEqual(kept, ())
        self.assertEqual(len(dropped), 2)

    def test_blank_anchor_drops_all(self) -> None:
        kept, dropped = fence_candidates_to_anchor([_Cand("300")], "")
        self.assertEqual(kept, ())
        self.assertEqual(len(dropped), 1)

    def test_non_numeric_candidate_journal_dropped(self) -> None:
        kept, dropped = fence_candidates_to_anchor([_Cand("abc"), _Cand("300")], "206")
        self.assertEqual([c.journal for c in kept], ["300"])
        self.assertEqual([c.journal for c in dropped], ["abc"])


class SendEdgeFenceTest(unittest.TestCase):
    """Review R2-F1 (pure): the per-row send-edge fence for pre-existing / recovered backlog rows."""

    def test_historical_coordinator_row_fenced(self) -> None:
        fence = make_send_edge_fence("206", "coordinator")
        blocked, reason = fence(_Row("100", "coordinator"))
        self.assertTrue(blocked)
        self.assertIn("superseded", reason)

    def test_current_coordinator_row_allowed(self) -> None:
        fence = make_send_edge_fence("206", "coordinator")
        blocked, _ = fence(_Row("300", "coordinator"))
        self.assertFalse(blocked)

    def test_unresolvable_anchor_fences_coordinator_row(self) -> None:
        fence = make_send_edge_fence(None, "coordinator")
        blocked, reason = fence(_Row("300", "coordinator"))
        self.assertTrue(blocked)
        self.assertIn("unresolvable", reason)

    def test_review_return_row_is_exempt(self) -> None:
        # review_return rows carry their OWN generation fence (#13684); the general anchor never
        # fences them, even a historical journal.
        fence = make_send_edge_fence("206", "coordinator")
        blocked, _ = fence(_Row("100", "review_return:issue_13811_lane"))
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
