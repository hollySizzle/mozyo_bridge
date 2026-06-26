"""mozyo cockpit append/focus planners and identity reader (Redmine #11803).

`mozyo cockpit` adds the current workspace to the shared `mozyo-cockpit`:
create on first use, append a column when the workspace is new, focus when it
is already present — never a duplicate column and never a second iTerm window
for an existing cockpit. These tests pin the append/focus planners, the
fair-share column geometry, the rightmost-anchor selection, and the
identity-option reader, all with tmux mocked (hermetic).

The create/append/focus decision (`cmd_cockpit`) is characterized in
`tests/test_cockpit_decision.py` and the lane-identity surface in
`tests/test_cockpit_lane_identity.py` (split characterization-first, Redmine
#12152).
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    CockpitWorkspace,
    build_cockpit_append_plan,
    build_cockpit_focus_plan,
)


class AppendPlannerTest(unittest.TestCase):
    def _plan(self):
        return build_cockpit_append_plan(
            CockpitWorkspace("wsB", "sessB", "/repoB"),
            anchor_pane="%7",
            column_index=1,
            launch=lambda role, ws: f"{role}-cmd",
        )

    def test_appends_one_full_height_column_from_anchor(self) -> None:
        plan = self._plan()
        first = plan.commands[0]
        # Full-height horizontal split (`-h -f`) so the new column does not
        # carve up the anchor's cell (Redmine #11807), sized to the new column's
        # fair share so widths re-equalize (Redmine #11854). column_index=1 ->
        # 2 columns total -> 50% each.
        self.assertEqual(
            ("split-window", "-h", "-f", "-l", "50%", "-t", "%7"), first.argv[:7]
        )
        self.assertEqual("@col1_codex", first.captures)
        self.assertEqual(1, plan.columns)
        # then a vertical split for the new column's claude pane.
        self.assertTrue(
            any(c.argv[:2] == ("split-window", "-v") for c in plan.commands)
        )

    def test_project_scope_is_stamped_and_repo_root_stays_distinct(self) -> None:
        # Redmine #12658: a column summoned from inside an adopted monorepo project
        # stamps `@mozyo_project_scope` / `@mozyo_project_path` / `@mozyo_project_label`
        # on its panes, and the Git repo_root stays separate from the repo-relative
        # project path in the plan JSON.
        plan = build_cockpit_append_plan(
            CockpitWorkspace(
                "gk-3500-it-operations",
                "gk-3500-it-operations",
                "/ws/gk-3500-it-operations",
                project_scope="giken-cloud-drive-management",
                project_path="projects/giken-cloud-drive-management",
                project_label="クラウドドライブ管理",
            ),
            anchor_pane="%7",
            column_index=1,
        )
        scope_cmds = [
            c for c in plan.commands
            if "@mozyo_project_scope" in c.argv
        ]
        self.assertTrue(scope_cmds)
        self.assertIn("giken-cloud-drive-management", scope_cmds[0].argv)
        path_cmds = [c for c in plan.commands if "@mozyo_project_path" in c.argv]
        self.assertIn("projects/giken-cloud-drive-management", path_cmds[0].argv)
        label_cmds = [c for c in plan.commands if "@mozyo_project_label" in c.argv]
        self.assertIn("クラウドドライブ管理", label_cmds[0].argv)
        # repo_root (Git) and project_path (repo-relative) are kept distinct.
        pane0 = plan.as_dict()["panes"][0]
        self.assertEqual(pane0["repo_root"], "/ws/gk-3500-it-operations")
        self.assertEqual(pane0["project_path"], "projects/giken-cloud-drive-management")
        self.assertNotEqual(pane0["repo_root"], pane0["project_path"])

    def test_single_repo_column_stamps_no_project_options(self) -> None:
        # No project scope -> no project stamp commands, so a single-repo column is
        # byte-identical to pre-#12658 (display compatibility).
        plan = self._plan()
        self.assertFalse(
            any("@mozyo_project_scope" in c.argv for c in plan.commands)
        )
        self.assertIsNone(plan.as_dict()["panes"][0]["project_scope"])

    def test_new_column_sized_to_fair_share_re_equalizes_widths(self) -> None:
        # Redmine #11854: a bare `split-window -h -f` grabs ~50% of the whole
        # window on every append, so the newest lane balloons and existing lanes
        # starve. Sizing the full-height split to 1/N of the window instead keeps
        # all N columns equal. column_index = number of existing columns, so the
        # new total is column_index + 1.
        for existing, expected_pct in [(1, "50%"), (2, "33%"), (3, "25%"), (4, "20%")]:
            plan = build_cockpit_append_plan(
                CockpitWorkspace("wsN", "sessN", "/repoN"),
                anchor_pane="%7",
                column_index=existing,
            )
            split = plan.commands[0]
            self.assertEqual("split-window", split.argv[0])
            self.assertIn("-f", split.argv)  # full-height: a true new column
            # the split carries an explicit `-l <pct>%` fair share.
            self.assertIn("-l", split.argv)
            pct = split.argv[split.argv.index("-l") + 1]
            self.assertEqual(
                expected_pct, pct,
                f"{existing} existing columns -> new column should take "
                f"{expected_pct} of the window, got {pct}",
            )

    def test_append_does_not_flatten_existing_columns(self) -> None:
        # Regression for Redmine #11807: `select-layout even-horizontal` would
        # split every existing workspace's Codex/Claude pair into left/right,
        # so the append plan must NOT emit it.
        plan = self._plan()
        ops = [c.argv[0] for c in plan.commands]
        self.assertNotIn("select-layout", ops)
        # the new column is the only thing created; existing panes (only the
        # anchor codex id is referenced) are never re-split or re-laid-out.
        referenced = {tok for c in plan.commands for tok in c.argv if tok.startswith("%")}
        self.assertEqual({"%7"}, referenced)  # only the anchor, nothing else existing

    def test_records_machine_readable_identity_options(self) -> None:
        plan = self._plan()
        ws_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_workspace_id" in c.argv
        ]
        role_opts = [
            c for c in plan.commands
            if c.argv[0] == "set-option" and "@mozyo_agent_role" in c.argv
        ]
        # one workspace_id + one role option per pane (codex + claude).
        self.assertEqual(2, len(ws_opts))
        self.assertEqual(2, len(role_opts))
        self.assertTrue(all("wsB" in c.argv for c in ws_opts))

    def test_anchor_required(self) -> None:
        with self.assertRaises(ValueError):
            build_cockpit_append_plan(
                CockpitWorkspace("w", "s", "/r"), anchor_pane="", column_index=1
            )


class EvenColumnShareTest(unittest.TestCase):
    """Fair-share column width for append re-equalization (Redmine #11854)."""

    def test_share_is_one_over_n_percent(self) -> None:
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import even_column_share

        self.assertEqual(50, even_column_share(2))
        self.assertEqual(33, even_column_share(3))
        self.assertEqual(25, even_column_share(4))
        self.assertEqual(20, even_column_share(5))
        self.assertEqual(17, even_column_share(6))

    def test_degenerate_counts_clamp_to_a_splittable_percentage(self) -> None:
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import even_column_share

        # 0 / 1 columns can't yield a 0% or 100% split tmux would reject; clamp
        # to a sane 2-column (50%) floor and keep 1..99.
        self.assertEqual(50, even_column_share(1))
        self.assertEqual(50, even_column_share(0))
        self.assertGreaterEqual(even_column_share(200), 1)
        self.assertLessEqual(even_column_share(200), 99)


class FocusPlannerTest(unittest.TestCase):
    def test_focus_selects_pane_without_creating(self) -> None:
        plan = build_cockpit_focus_plan("%9")
        self.assertEqual((), plan.panes)
        ops = [c.argv[0] for c in plan.commands]
        self.assertEqual(["select-window", "select-pane"], ops)
        self.assertIn("%9", plan.commands[-1].argv)
        # no pane is created.
        self.assertFalse(any(c.captures for c in plan.commands))


class ReadCockpitColumnsTest(unittest.TestCase):
    def test_parses_pane_identity_options(self) -> None:
        from mozyo_bridge.application import commands

        # Mixed feed: newer panes carry lane id + geometry (#11820, #11849),
        # pre-#11820 panes carry only 3 fields. Both parse; missing fields read
        # as "" / 0 without IndexError.
        out = (
            "%1\twsA\tcodex\twkt-1\t41\t39\n"
            "%2\twsA\tclaude\twkt-1\t41\t13\n"
            "%3\twsB\tcodex\n"
        )
        with patch.object(
            commands, "run_tmux",
            return_value=argparse.Namespace(returncode=0, stdout=out, stderr=""),
        ):
            cols = commands._read_cockpit_columns("mozyo-cockpit")
        self.assertEqual(3, len(cols))
        self.assertEqual(
            {
                "pane_id": "%1", "workspace_id": "wsA", "role": "codex",
                "lane_id": "wkt-1", "pane_left": 41, "pane_width": 39,
            },
            cols[0],
        )
        # Legacy 3-field pane -> lane_id "" and geometry defaults to 0.
        self.assertEqual("", cols[2]["lane_id"])
        self.assertEqual(0, cols[2]["pane_left"])
        self.assertEqual(0, cols[2]["pane_width"])

    def test_missing_session_returns_none(self) -> None:
        from mozyo_bridge.application import commands

        with patch.object(
            commands, "run_tmux",
            return_value=argparse.Namespace(returncode=1, stdout="", stderr="no session"),
        ):
            self.assertIsNone(commands._read_cockpit_columns("mozyo-cockpit"))


class RightmostAnchorTest(unittest.TestCase):
    def test_picks_max_pane_left_not_list_order(self) -> None:
        # Redmine #11849: list order != layout order. The rightmost column
        # (largest pane_left) must be the anchor even when it is listed first.
        from mozyo_bridge.application.commands import _rightmost_codex_anchor

        codex = [
            {"pane_id": "%rightmost", "pane_left": 80, "pane_width": 40},
            {"pane_id": "%left", "pane_left": 0, "pane_width": 40},
            {"pane_id": "%middle", "pane_left": 40, "pane_width": 40},
        ]
        self.assertEqual("%rightmost", _rightmost_codex_anchor(codex))

    def test_tie_breaks_deterministically_on_right_edge_then_id(self) -> None:
        from mozyo_bridge.application.commands import _rightmost_codex_anchor

        # equal pane_left -> wider (further right edge) wins.
        codex = [
            {"pane_id": "%a", "pane_left": 40, "pane_width": 10},
            {"pane_id": "%b", "pane_left": 40, "pane_width": 30},
        ]
        self.assertEqual("%b", _rightmost_codex_anchor(codex))

    def test_missing_geometry_falls_back_to_stable_id_order(self) -> None:
        from mozyo_bridge.application.commands import _rightmost_codex_anchor

        # all geometry absent (pre-#11849 panes) -> deterministic by pane id.
        codex = [
            {"pane_id": "%1"}, {"pane_id": "%3"}, {"pane_id": "%2"},
        ]
        self.assertEqual("%3", _rightmost_codex_anchor(codex))

    def test_empty_is_none(self) -> None:
        from mozyo_bridge.application.commands import _rightmost_codex_anchor

        self.assertIsNone(_rightmost_codex_anchor([]))


if __name__ == "__main__":
    unittest.main()
