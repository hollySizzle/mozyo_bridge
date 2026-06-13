"""mozyo cockpit append/focus UX (Redmine #11803).

`mozyo cockpit` adds the current workspace to the shared `mozyo-cockpit`:
create on first use, append a column when the workspace is new, focus when it
is already present — never a duplicate column and never a second iTerm window
for an existing cockpit. These tests pin the append/focus planners, the
identity-option reader, and the create/append/focus decision, all with tmux
mocked (hermetic).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.cockpit_layout import (
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
        from mozyo_bridge.domain.cockpit_layout import even_column_share

        self.assertEqual(50, even_column_share(2))
        self.assertEqual(33, even_column_share(3))
        self.assertEqual(25, even_column_share(4))
        self.assertEqual(20, even_column_share(5))
        self.assertEqual(17, even_column_share(6))

    def test_degenerate_counts_clamp_to_a_splittable_percentage(self) -> None:
        from mozyo_bridge.domain.cockpit_layout import even_column_share

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


class CockpitDecisionTest(unittest.TestCase):
    def _args(self, **over):
        base = dict(
            action=None, repo="/repoX", codex_ratio=70, cockpit_session=None,
            dry_run=True, json_output=False, no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(self, *, columns, ws_id="wsX", session_present=False, lane=None):
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.cockpit_layout import DEFAULT_LANE, LaneIdentity

        canon = argparse.Namespace(name="sessX", workspace_id=ws_id)
        lane = lane if lane is not None else LaneIdentity(DEFAULT_LANE, None)
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_agent_launch_command", side_effect=lambda r, s, c: f"{r}-cmd"), \
            patch.object(commands, "require_tmux"), \
            patch.object(commands, "_read_cockpit_columns", return_value=columns), \
            patch.object(commands, "_resolve_workspace_lane", return_value=lane), \
            patch.object(commands, "session_exists", return_value=session_present), \
            patch.object(commands, "run_tmux") as run_tmux, \
            patch.object(commands.os, "execvp", side_effect=RuntimeError("attach")) as execvp:
            yield run_tmux, execvp

    def _run(self, args, columns, ws_id="wsX", session_present=False, lane=None):
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(
            columns=columns, ws_id=ws_id, session_present=session_present, lane=lane
        ) as (run_tmux, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    cmd_cockpit(args)
                except RuntimeError:
                    pass
        return out.getvalue(), run_tmux, execvp

    def test_dry_run_create_when_no_cockpit(self) -> None:
        out, run_tmux, execvp = self._run(self._args(), columns=None)
        self.assertIn("action=create", out)
        self.assertIn("tmux new-session", out)
        run_tmux.assert_not_called()
        execvp.assert_not_called()

    def test_dry_run_append_when_workspace_absent(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _r, _e = self._run(self._args(), columns=cols)
        self.assertIn("action=append", out)
        # appends a full-height column split from the existing codex pane, sized
        # to the new column's fair share (1 existing -> 2 total -> 50%, #11854).
        self.assertIn("tmux split-window -h -f -l 50% -t %1", out)

    def test_append_anchors_on_geometry_rightmost_not_list_order(self) -> None:
        # Redmine #11849: the rightmost column (%right, pane_left 40) is listed
        # FIRST; append must still split from it, not from the last-listed pane.
        cols = [
            {"pane_id": "%right", "workspace_id": "wsB", "role": "codex",
             "lane_id": "default", "pane_left": 40, "pane_width": 40},
            {"pane_id": "%left", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default", "pane_left": 0, "pane_width": 40},
        ]
        out, _r, _e = self._run(self._args(), columns=cols, ws_id="wsNew")
        self.assertIn("action=append", out)
        # 2 existing columns -> 3 total -> the new column takes 33% (#11854).
        self.assertIn("tmux split-window -h -f -l 33% -t %right", out)
        self.assertNotIn("-t %left", out)

    def test_dry_run_focus_when_workspace_present(self) -> None:
        cols = [{"pane_id": "%5", "workspace_id": "wsX", "role": "codex"}]
        out, _r, _e = self._run(self._args(), columns=cols, ws_id="wsX")
        self.assertIn("action=focus", out)
        self.assertIn("tmux select-pane -t %5", out)

    def test_json_emits_action_and_workspace(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _r, _e = self._run(
            self._args(dry_run=False, json_output=True), columns=cols
        )
        payload = json.loads(out)
        self.assertEqual("append", payload["action"])
        self.assertEqual("wsX", payload["workspace_id"])

    def test_create_executes_and_control_mode_attaches(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(columns=None) as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(returncode=0, stdout="%1", stderr="")
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "attach"):
                    cmd_cockpit(self._args(dry_run=False))
        self.assertTrue(run_tmux.called)
        self.assertEqual(
            ["tmux", "-CC", "attach", "-t", "mozyo-cockpit"],
            list(execvp.call_args.args[1]),
        )

    def test_append_executes_without_new_attach(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        with self._patched(columns=cols) as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(returncode=0, stdout="%9", stderr="")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cmd_cockpit(self._args(dry_run=False))
        self.assertEqual(0, rc)
        self.assertTrue(run_tmux.called)
        execvp.assert_not_called()  # no new iTerm window for an existing cockpit
        self.assertIn("appended", out.getvalue())

    def test_dry_run_is_non_mutating_and_does_not_abort_on_stale_cockpit(self) -> None:
        # Redmine #11803 review (Major 1): a stale cockpit (panes without mozyo
        # identity options) must not make --dry-run mutate tmux or abort.
        cols = [{"pane_id": "%1", "workspace_id": "", "role": ""}]
        out, run_tmux, execvp = self._run(self._args(), columns=cols)
        self.assertIn("action=append", out)
        self.assertIn("blocked", out)
        run_tmux.assert_not_called()  # read-only / non-mutating
        execvp.assert_not_called()

    def test_json_reports_blocked_append_without_aborting(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "", "role": ""}]
        out, _r, _e = self._run(
            self._args(dry_run=False, json_output=True), columns=cols
        )
        payload = json.loads(out)
        self.assertEqual("append", payload["action"])
        self.assertIsNotNone(payload["blocked"])

    def test_real_append_on_stale_cockpit_fails_closed(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        cols = [{"pane_id": "%1", "workspace_id": "", "role": ""}]
        with self._patched(columns=cols) as (run_tmux, execvp):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_cockpit(self._args(dry_run=False))
        run_tmux.assert_not_called()  # nothing mutated
        execvp.assert_not_called()

    def test_append_partial_failure_cleans_up_created_pane(self) -> None:
        # Redmine #11803 review (Major 2): a mid-append failure must not orphan
        # the pane already created in the shared cockpit.
        from mozyo_bridge.application.commands import cmd_cockpit

        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        calls = []

        def fake(*argv, check=True):
            calls.append(argv)
            if argv[:2] == ("split-window", "-h"):
                return argparse.Namespace(returncode=0, stdout="%9", stderr="")
            if argv[:1] == ("kill-pane",):
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            return argparse.Namespace(returncode=1, stdout="", stderr="boom")

        with self._patched(columns=cols) as (run_tmux, execvp):
            run_tmux.side_effect = fake
            with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_cockpit(self._args(dry_run=False))
        # the created codex pane %9 was cleaned up; the session was NOT killed.
        self.assertTrue(any(c[:1] == ("kill-pane",) and "%9" in c for c in calls))
        self.assertFalse(any(c[:1] == ("kill-session",) for c in calls))
        execvp.assert_not_called()

    def test_existing_session_without_cockpit_window_blocks_dry_run(self) -> None:
        # Redmine #11803 re-review (Major): session present but no cockpit
        # window must NOT be treated as a plain create.
        out, run_tmux, execvp = self._run(
            self._args(), columns=None, session_present=True
        )
        self.assertIn("blocked", out)
        self.assertIn("already exists but has no cockpit window", out)
        run_tmux.assert_not_called()
        execvp.assert_not_called()

    def test_real_run_blocked_when_session_present_without_window(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(columns=None, session_present=True) as (run_tmux, execvp):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_cockpit(self._args(dry_run=False))
        # Never new-session and never kill-session a pre-existing session.
        for call in run_tmux.call_args_list:
            self.assertNotIn(call.args[:1], [("new-session",), ("kill-session",)])
        execvp.assert_not_called()

    def test_create_failure_does_not_kill_preexisting_session(self) -> None:
        # new-session fails (e.g. a race) -> nothing was captured -> nothing is
        # killed; a pre-existing session must never be blanket-killed.
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(columns=None, session_present=False) as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(
                returncode=1, stdout="", stderr="duplicate session: mozyo-cockpit"
            )
            with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_cockpit(self._args(dry_run=False))
        kills = [c for c in run_tmux.call_args_list if c.args[:1] == ("kill-session",)]
        self.assertEqual([], kills)
        execvp.assert_not_called()

    def test_create_partial_failure_kills_only_created_panes(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        calls = []

        def fake(*argv, check=True):
            calls.append(argv)
            if argv[:1] == ("new-session",):
                return argparse.Namespace(returncode=0, stdout="%1", stderr="")
            if argv[:1] == ("kill-pane",):
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            return argparse.Namespace(returncode=1, stdout="", stderr="boom")

        with self._patched(columns=None, session_present=False) as (run_tmux, execvp):
            run_tmux.side_effect = fake
            with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_cockpit(self._args(dry_run=False))
        # the created pane %1 was killed; no blanket kill-session.
        self.assertTrue(any(c[:1] == ("kill-pane",) and "%1" in c for c in calls))
        self.assertFalse(any(c[:1] == ("kill-session",) for c in calls))

    def test_focus_executes_without_attach(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        cols = [{"pane_id": "%5", "workspace_id": "wsX", "role": "codex"}]
        with self._patched(columns=cols, ws_id="wsX") as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(returncode=0, stdout="", stderr="")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cmd_cockpit(self._args(dry_run=False))
        self.assertEqual(0, rc)
        execvp.assert_not_called()
        self.assertIn("already in cockpit", out.getvalue())

    # --- Lane-aware duplicate detection (Redmine #11820). ---
    def _lane(self, lane_id, label=None):
        from mozyo_bridge.domain.cockpit_layout import LaneIdentity

        return LaneIdentity(lane_id, label)

    def test_same_workspace_same_lane_focuses(self) -> None:
        cols = [
            {"pane_id": "%5", "workspace_id": "wsX", "role": "codex", "lane_id": "lane-abc"}
        ]
        out, _r, _e = self._run(
            self._args(), columns=cols, ws_id="wsX", lane=self._lane("lane-abc", "feat")
        )
        self.assertIn("action=focus", out)
        self.assertIn("tmux select-pane -t %5", out)
        self.assertIn("lane=lane-abc", out)

    def test_same_workspace_different_lane_appends_a_new_column(self) -> None:
        # Same workspace_id but a different checkout lane (e.g. a git worktree)
        # must NOT focus the existing column — it appends beside it.
        cols = [
            {"pane_id": "%5", "workspace_id": "wsX", "role": "codex", "lane_id": "default"}
        ]
        out, _r, _e = self._run(
            self._args(), columns=cols, ws_id="wsX", lane=self._lane("lane-abc", "feat")
        )
        self.assertIn("action=append", out)
        self.assertIn("tmux split-window -h -f -l 50% -t %5", out)

    def test_legacy_pane_without_lane_is_focusable_by_default_lane(self) -> None:
        # Backward compat: a pane stamped before #11820 carries no lane id; a
        # primary (default-lane) checkout of the same workspace still focuses it
        # instead of appending a duplicate column.
        cols = [
            {"pane_id": "%5", "workspace_id": "wsX", "role": "codex", "lane_id": ""}
        ]
        out, _r, _e = self._run(
            self._args(), columns=cols, ws_id="wsX", lane=self._lane("default", None)
        )
        self.assertIn("action=focus", out)

    def test_json_exposes_lane_fields(self) -> None:
        cols = [
            {"pane_id": "%5", "workspace_id": "wsX", "role": "codex", "lane_id": "lane-abc"}
        ]
        out, _r, _e = self._run(
            self._args(dry_run=False, json_output=True),
            columns=cols,
            ws_id="wsX",
            lane=self._lane("lane-abc", "feature/x"),
        )
        payload = json.loads(out)
        self.assertEqual("focus", payload["action"])
        self.assertEqual("lane-abc", payload["lane_id"])
        self.assertEqual("feature/x", payload["lane_label"])


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
