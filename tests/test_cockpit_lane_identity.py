"""Cockpit lane identity semantics (Redmine #11820).

Pure lane derivation (`resolve_lane_identity`) and the lane id / label stamping
that rides on tmux pane options. Split characterization-first from
`tests/test_cockpit_append.py` (Redmine #12152) so the lane-identity surface is
its own maintenance unit; behaviour is unchanged and these tests are hermetic
(no live tmux).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.cockpit_layout import CockpitWorkspace


class PlannerLaneStampingTest(unittest.TestCase):
    """Lane id / label ride on tmux pane options (Redmine #11820)."""

    def test_lane_id_and_label_stamped_when_present(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import build_cockpit_plan

        ws = CockpitWorkspace(
            "wsX", "alpha", "/a", lane_id="lane-abc", lane_label="feature/x"
        )
        plan = build_cockpit_plan([ws])
        lane_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_lane_id" in c.argv
        ]
        label_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_lane_label" in c.argv
        ]
        # one lane id + one lane label per pane (codex + claude).
        self.assertEqual(2, len(lane_opts))
        self.assertEqual(2, len(label_opts))
        self.assertTrue(all("lane-abc" in c.argv for c in lane_opts))
        self.assertTrue(all("feature/x" in c.argv for c in label_opts))
        # workspace id is unchanged and still stamped (additive, not replaced).
        ws_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_workspace_id" in c.argv
        ]
        self.assertEqual(2, len(ws_opts))
        self.assertTrue(all("wsX" in c.argv for c in ws_opts))

    def test_default_lane_stamped_and_no_label_when_absent(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import build_cockpit_plan

        plan = build_cockpit_plan([CockpitWorkspace("wsX", "alpha", "/a")])
        lane_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_lane_id" in c.argv
        ]
        label_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_lane_label" in c.argv
        ]
        self.assertEqual(2, len(lane_opts))
        self.assertTrue(all("default" in c.argv for c in lane_opts))
        # no label option when there is no label.
        self.assertEqual([], label_opts)

    def test_append_plan_stamps_lane(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import build_cockpit_append_plan

        ws = CockpitWorkspace(
            "wsB", "sessB", "/repoB", lane_id="lane-xyz", lane_label="wt"
        )
        plan = build_cockpit_append_plan(ws, anchor_pane="%7", column_index=1)
        lane_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_lane_id" in c.argv
        ]
        self.assertEqual(2, len(lane_opts))
        self.assertTrue(all("lane-xyz" in c.argv for c in lane_opts))


class LaneIdentityTest(unittest.TestCase):
    """Pure lane derivation (Redmine #11820)."""

    def test_primary_checkout_is_default_lane(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import (
            DEFAULT_LANE,
            resolve_lane_identity,
        )

        # main worktree: git_dir == git_common_dir, path == canonical.
        lane = resolve_lane_identity(
            repo_root="/work/repo",
            canonical_path="/work/repo",
            git_dir="/work/repo/.git",
            git_common_dir="/work/repo/.git",
            branch="main",
        )
        self.assertEqual(DEFAULT_LANE, lane.lane_id)
        self.assertEqual("main", lane.lane_label)

    def test_non_git_workspace_is_default_lane(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import (
            DEFAULT_LANE,
            resolve_lane_identity,
        )

        lane = resolve_lane_identity(repo_root="/work/plain")
        self.assertEqual(DEFAULT_LANE, lane.lane_id)
        self.assertIsNone(lane.lane_label)

    def test_linked_worktree_is_distinct_lane(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import resolve_lane_identity

        lane = resolve_lane_identity(
            repo_root="/work/repo-feature",
            canonical_path="/work/repo",
            git_dir="/work/repo/.git/worktrees/repo-feature",
            git_common_dir="/work/repo/.git",
            branch="feature/x",
        )
        self.assertTrue(lane.lane_id.startswith("lane-"))
        self.assertEqual("feature/x", lane.lane_label)

    def test_relocated_clone_sharing_workspace_id_is_distinct_lane(self) -> None:
        # A clone copied the tracked workspace.json (same workspace_id) but lives
        # at a different path with its own .git (git_dir == git_common_dir).
        from mozyo_bridge.domain.cockpit_layout import resolve_lane_identity

        lane = resolve_lane_identity(
            repo_root="/work/repo-clone",
            canonical_path="/work/repo",
            git_dir="/work/repo-clone/.git",
            git_common_dir="/work/repo-clone/.git",
            branch="main",
        )
        self.assertTrue(lane.lane_id.startswith("lane-"))

    def test_lane_id_is_deterministic_and_carries_no_raw_path(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import resolve_lane_identity

        kwargs = dict(
            repo_root="/work/repo-feature",
            canonical_path="/work/repo",
            git_dir="/work/repo/.git/worktrees/repo-feature",
            git_common_dir="/work/repo/.git",
        )
        a = resolve_lane_identity(**kwargs)
        b = resolve_lane_identity(**kwargs)
        self.assertEqual(a.lane_id, b.lane_id)  # deterministic
        # privacy-safe: the durable lane id never embeds the absolute path.
        self.assertNotIn("/work", a.lane_id)
        self.assertNotIn("repo-feature", a.lane_id)

    def test_distinct_checkouts_get_distinct_lane_ids(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import resolve_lane_identity

        a = resolve_lane_identity(
            repo_root="/work/wt-a",
            canonical_path="/work/repo",
            git_dir="/work/repo/.git/worktrees/wt-a",
            git_common_dir="/work/repo/.git",
        )
        b = resolve_lane_identity(
            repo_root="/work/wt-b",
            canonical_path="/work/repo",
            git_dir="/work/repo/.git/worktrees/wt-b",
            git_common_dir="/work/repo/.git",
        )
        self.assertNotEqual(a.lane_id, b.lane_id)


if __name__ == "__main__":
    unittest.main()
