"""herdr `sublane list` projection tests (Redmine #13331 option A).

Pins the pure fold: one :class:`SublaneLaneView` per lane workspace, the sender's own
workspace excluded, foreign / non-default-lane rows dropped, repo-root / lane-label /
issue recovered from the injected registry resolver.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    LANE_RECORD_MISSING_HINT,
    project_herdr_sublanes,
)
from mozyo_bridge.core.state.lane_metadata import LaneMetadataRecord
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SUBLANE_STATE_ACTIVE,
    SUBLANE_STATE_GATEWAY_ONLY,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)


def _row(ws, role, lane, locator):
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


class ProjectHerdrSublanesTest(unittest.TestCase):
    def test_folds_lane_workspaces_excludes_own_and_foreign(self) -> None:
        roots = {
            "wsA": "/work/mozyo_bridge_issue_101_alpha",
            "wsB": "/work/mozyo_bridge_issue_202_beta",
        }
        rows = [
            _row("wsA", "codex", "", "wL1:p2"),
            _row("wsA", "claude", "", "wL1:p3"),
            _row("wsB", "codex", "", "wL2:p2"),
            _row("wsB", "claude", "", "wL2:p3"),
            # the coordinator's own workspace (excluded) — also a codex+claude pair
            _row("wsMain", "codex", "", "w2:p3"),
            _row("wsMain", "claude", "", "w2:p2"),
            # a foreign non-mzb1 agent, and a non-default lane slot — both dropped
            {"name": "someones-shell", "pane_id": "wZ:p1"},
            _row("wsA", "codex", "lane-x", "wL1:p9"),
        ]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: roots.get(ws),
        )
        self.assertEqual([v.workspace_id for v in views], ["wsA", "wsB"])
        a, b = views
        self.assertEqual(a.lane_label, "mozyo_bridge_issue_101_alpha")
        self.assertEqual(a.issue, "101")
        self.assertEqual(a.repo_root, "/work/mozyo_bridge_issue_101_alpha")
        self.assertEqual(a.gateway_pane, "wL1:p2")
        self.assertEqual(a.worker_pane, "wL1:p3")
        self.assertEqual(a.lane_id, "default")
        self.assertEqual(a.state, SUBLANE_STATE_ACTIVE)
        self.assertEqual(b.issue, "202")

    def test_gateway_only_workspace_is_degraded_lane(self) -> None:
        rows = [_row("wsA", "codex", "", "wL1:p2")]
        views = project_herdr_sublanes(
            rows, exclude_workspace_id="wsMain", resolve_repo_root=lambda ws: None
        )
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].gateway_pane, "wL1:p2")
        self.assertIsNone(views[0].worker_pane)
        self.assertEqual(views[0].state, SUBLANE_STATE_GATEWAY_ONLY)
        # Unresolvable workspace falls back to the workspace id as the label (never guessed).
        self.assertEqual(views[0].lane_label, "wsA")
        self.assertIsNone(views[0].issue)

    def test_row_without_locator_is_dropped(self) -> None:
        rows = [
            {"name": encode_assigned_name("wsA", "codex", ""), "pane_id": ""},
            _row("wsA", "claude", "", "wL1:p3"),
        ]
        views = project_herdr_sublanes(
            rows, exclude_workspace_id="", resolve_repo_root=lambda ws: None
        )
        self.assertEqual(len(views), 1)
        self.assertIsNone(views[0].gateway_pane)
        self.assertEqual(views[0].worker_pane, "wL1:p3")

    # -- lane metadata record join (Redmine #13356 j#73386) --------------------

    def test_lane_record_resolves_wt_token_to_human_identity(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc123",
            issue_id="13356",
            lane_label="issue_13356_cockpit_aggregate",
            branch="issue_13356_cockpit_aggregate",
            worktree_path="/work/mozyo_bridge_issue_13356_cockpit_aggregate",
        )
        rows = [
            _row("wt_abc123", "codex", "", "wD:p2"),
            _row("wt_abc123", "claude", "", "wD:p3"),
        ]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="wsMain",
            # A wt_<hash> token is never registry-resolvable (#13331 j#73357).
            resolve_repo_root=lambda ws: None,
            resolve_lane_record={"wt_abc123": record}.get,
        )
        self.assertEqual(len(views), 1)
        view = views[0]
        self.assertEqual(view.lane_label, "issue_13356_cockpit_aggregate")
        self.assertEqual(view.issue, "13356")
        self.assertEqual(view.branch, "issue_13356_cockpit_aggregate")
        self.assertEqual(
            view.repo_root, "/work/mozyo_bridge_issue_13356_cockpit_aggregate"
        )
        self.assertEqual(view.stale_hints, ())

    def test_missing_lane_record_degrades_to_raw_token_with_hint(self) -> None:
        rows = [_row("wt_orphan", "codex", "", "wX:p2")]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="",
            resolve_repo_root=lambda ws: None,
            resolve_lane_record=lambda ws: None,
        )
        self.assertEqual(len(views), 1)
        # Fail-open degrade: the raw token stays the label, kept visible via the
        # machine-readable hint — never a guessed identity, never a crash.
        self.assertEqual(views[0].lane_label, "wt_orphan")
        self.assertIsNone(views[0].issue)
        self.assertIn(LANE_RECORD_MISSING_HINT, views[0].stale_hints)

    def test_record_issue_falls_back_to_label_convention(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_x",
            issue_id="",
            lane_label="issue_777_slug",
        )
        rows = [_row("wt_x", "claude", "", "wY:p3")]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="",
            resolve_repo_root=lambda ws: None,
            resolve_lane_record={"wt_x": record}.get,
        )
        self.assertEqual(views[0].issue, "777")


class HerdrLaneViewForWorktreeTest(unittest.TestCase):
    """The lane-record-joined single-lane read-back (Redmine #13356).

    Not wired into dispatch-worker here — the herdr dispatch drive is #13357's
    surface; this pins the seam it can adopt for a recorded identity check.
    """

    _MODULE = (
        "mozyo_bridge.e_110_execution_platform."
        "f_140_delegated_coordinator_nested_handoff.application."
        "sublane_herdr_projection"
    )

    def _resolve(self, *, segment, rows, records):
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            herdr_lane_view_for_worktree,
        )

        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_session_start.herdr_workspace_segment",
            return_value=segment,
        ), mock.patch(
            f"{self._MODULE}.list_herdr_agent_rows", return_value=rows
        ), mock.patch(
            "mozyo_bridge.core.state.lane_metadata.load_lane_records",
            return_value=records,
        ):
            return herdr_lane_view_for_worktree("/work/lane")

    def test_resolves_lane_with_metadata_join(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc",
            issue_id="13356",
            lane_label="issue_13356_cockpit_aggregate",
            branch="issue_13356_cockpit_aggregate",
        )
        view = self._resolve(
            segment="wt_abc",
            rows=[
                _row("wt_abc", "codex", "", "wD:p2"),
                _row("wt_abc", "claude", "", "wD:p3"),
            ],
            records={"wt_abc": record},
        )
        self.assertIsNotNone(view)
        self.assertEqual(view.lane_label, "issue_13356_cockpit_aggregate")
        self.assertEqual(view.issue, "13356")
        self.assertEqual(view.gateway_pane, "wD:p2")
        self.assertEqual(view.worker_pane, "wD:p3")
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)
        self.assertEqual(view.stale_hints, ())

    def test_missing_record_falls_back_to_worktree_basename(self) -> None:
        view = self._resolve(
            segment="wt_abc",
            rows=[_row("wt_abc", "claude", "", "wD:p3")],
            records={},
        )
        self.assertIsNotNone(view)
        self.assertEqual(view.lane_label, "lane")
        self.assertIn(LANE_RECORD_MISSING_HINT, view.stale_hints)

    def test_no_live_slot_resolves_none(self) -> None:
        view = self._resolve(segment="wt_abc", rows=[], records={})
        self.assertIsNone(view)

    def test_unresolvable_segment_resolves_none(self) -> None:
        view = self._resolve(segment="", rows=[], records={})
        self.assertIsNone(view)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
