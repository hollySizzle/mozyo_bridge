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
    project_herdr_sublanes,
)
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
