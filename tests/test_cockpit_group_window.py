"""Faithful per-Project-Group tmux window execution (Redmine #12330).

`project_group_tmux_window` is the #12290 desired presentation that #12302 only
recorded-and-degraded. #12330 makes `mozyo cockpit` faithfully *execute* it: a
Project Group is laid out in its OWN tmux window in the cockpit session, with the
same workspace+lane duplicate gate and pane-identity stamping the shared cockpit
uses, plus rollback on a failed build. The window is a display surface only — its
NAME is never identity; Unit identity stays on the pane options (read session-wide
via `list-panes -a`), and a window's group is located by the mozyo-written
`@mozyo_group_id` window marker, never the name.

These tests pin:
- the pure domain builders (`build_group_window_create_plan` /
  `build_group_window_focus_plan` / `sanitize_group_window_name`) and the faithful
  `resolve_group_window_placement(execute_group_window=True)` decision;
- the cross-window duplicate / append / create routing
  (`_cockpit_group_window_action`) with multi-window discovery stubbed;
- multi-window discovery (`_read_managed_cockpit_windows`);
- the rollback boundary (a failed group-window build kills only the captured
  panes, so tmux drops the empty window — no orphan);
- the reset multi-window destruction warning.

Hermetic: every tmux read/mutation is mocked — no live tmux, no file IO.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.cockpit_layout import (  # noqa: E402
    GROUP_WINDOW_OPTION,
    CockpitWorkspace,
    build_group_window_create_plan,
    build_group_window_focus_plan,
    sanitize_group_window_name,
)
from mozyo_bridge.domain.presentation_grouping import (  # noqa: E402
    GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
    GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    GroupPlacement,
    GroupWindowDecision,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
    STATUS_CONFIGURED,
    resolve_group_window_placement,
)


# --- Pure domain: window-name sanitizer ---------------------------------------


class SanitizeGroupWindowNameTest(unittest.TestCase):
    def test_plain_name_is_preserved(self) -> None:
        self.assertEqual("Alpha", sanitize_group_window_name("Alpha"))

    def test_strips_target_separator_characters(self) -> None:
        # `:` / `.` / quotes / `$` could break a `session:window` target.
        self.assertEqual("ab", sanitize_group_window_name("a:b"))
        self.assertEqual("ab", sanitize_group_window_name('a"b'))
        self.assertEqual("ab", sanitize_group_window_name("a.b"))

    def test_collapses_whitespace(self) -> None:
        self.assertEqual("a b", sanitize_group_window_name("  a\t\n  b  "))

    def test_empty_falls_back_to_group(self) -> None:
        self.assertEqual("group", sanitize_group_window_name(""))
        self.assertEqual("group", sanitize_group_window_name(None))
        self.assertEqual("group", sanitize_group_window_name("  :. "))


# --- Pure domain: group-window plan builders ----------------------------------


class GroupWindowCreatePlanTest(unittest.TestCase):
    def _plan(self, *, group_id="alpha"):
        return build_group_window_create_plan(
            CockpitWorkspace("wsA", "repoA", "/repoA", lane_id="default"),
            group_id=group_id,
            window_name="Alpha",
            launch=lambda role, ws: f"{role}-cmd",
        )

    def test_first_command_creates_a_new_window(self) -> None:
        plan = self._plan()
        first = plan.commands[0]
        self.assertEqual("new-window", first.argv[0])
        self.assertIn("-n", first.argv)
        self.assertIn("Alpha", first.argv)
        self.assertEqual("@grp_codex", first.captures)
        self.assertEqual("codex-cmd", first.argv[-1])
        self.assertEqual("Alpha", plan.window)

    def test_claude_is_a_vertical_split_of_codex(self) -> None:
        plan = self._plan()
        split = plan.commands[1]
        self.assertEqual(("split-window", "-v", "-t", "@grp_codex"), split.argv[:4])
        self.assertEqual("@grp_claude", split.captures)

    def test_stamps_identical_pane_identity_options(self) -> None:
        plan = self._plan()
        argvs = [c.argv for c in plan.commands]
        # Both panes carry workspace / role / lane options — the same set the
        # shared cockpit stamps, so duplicate detection / target resolution read
        # them regardless of the holding window.
        self.assertIn(
            ("set-option", "-p", "-t", "@grp_codex", "@mozyo_workspace_id", "wsA"),
            argvs,
        )
        self.assertIn(
            ("set-option", "-p", "-t", "@grp_codex", "@mozyo_agent_role", "codex"),
            argvs,
        )
        self.assertIn(
            ("set-option", "-p", "-t", "@grp_claude", "@mozyo_agent_role", "claude"),
            argvs,
        )

    def test_group_id_window_hint_is_stamped_when_present(self) -> None:
        plan = self._plan(group_id="alpha")
        argvs = [c.argv for c in plan.commands]
        self.assertIn(
            ("set-option", "-w", "-t", "@grp_codex", GROUP_WINDOW_OPTION, "alpha"),
            argvs,
        )

    def test_no_group_hint_for_ungrouped_unit(self) -> None:
        plan = self._plan(group_id=None)
        for cmd in plan.commands:
            self.assertNotIn(GROUP_WINDOW_OPTION, cmd.argv)

    def test_empty_window_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_group_window_create_plan(
                CockpitWorkspace("wsA", "repoA", "/repoA"),
                group_id="alpha",
                window_name="",
            )


class GroupWindowFocusPlanTest(unittest.TestCase):
    def test_focus_selects_the_window_of_the_pane_then_the_pane(self) -> None:
        plan = build_group_window_focus_plan("%42")
        self.assertEqual(
            [("select-window", "-t", "%42"), ("select-pane", "-t", "%42")],
            [c.argv for c in plan.commands],
        )
        self.assertEqual((), plan.panes)

    def test_empty_pane_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_group_window_focus_plan("")


# --- Pure domain: faithful placement decision ---------------------------------


class FaithfulPlacementDecisionTest(unittest.TestCase):
    def _placement(self, *, group_id="alpha", label="Alpha"):
        return GroupPlacement(
            status=STATUS_CONFIGURED, group_id=group_id, label=label,
        )

    def test_tmux_window_faithful_when_enabled(self) -> None:
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
            self._placement(),
            execute_group_window=True,
        )
        self.assertEqual(
            GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW, decision.desired_surface
        )
        self.assertEqual(
            GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW, decision.executed_surface
        )
        self.assertFalse(decision.degraded)
        self.assertIsNone(decision.diagnostic)
        self.assertEqual("Alpha", decision.desired_window_name)

    def test_tmux_window_still_degrades_when_not_enabled(self) -> None:
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_TMUX_WINDOW, self._placement()
        )
        self.assertEqual(
            GROUP_WINDOW_SURFACE_COCKPIT_COLUMN, decision.executed_surface
        )
        self.assertTrue(decision.degraded)

    def test_same_column_unchanged_under_enable(self) -> None:
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
            self._placement(),
            execute_group_window=True,
        )
        self.assertEqual(
            GROUP_WINDOW_SURFACE_COCKPIT_COLUMN, decision.executed_surface
        )
        self.assertFalse(decision.degraded)

    def test_normal_window_still_degrades_under_enable(self) -> None:
        # Faithful execution covers the tmux window only; normal_window is not
        # relaunched, so it stays a visible degrade even with the flag on.
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
            self._placement(),
            execute_group_window=True,
        )
        self.assertEqual(
            GROUP_WINDOW_SURFACE_COCKPIT_COLUMN, decision.executed_surface
        )
        self.assertTrue(decision.degraded)


# --- Application: faithful routing decision ------------------------------------


def _faithful_decision(group_id="alpha", window="Alpha"):
    return GroupWindowDecision(
        presentation_mode=PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
        desired_surface=GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
        executed_surface=GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
        group_id=group_id,
        label=window,
        desired_window_name=window,
        degraded=False,
    )


class GroupWindowActionTest(unittest.TestCase):
    def _action(self, managed, *, group_id="alpha", ws_id="wsA", lane="default"):
        from mozyo_bridge.application import commands

        ws = CockpitWorkspace(ws_id, "repoA", "/repoA", lane_id=lane)
        with patch.object(
            commands, "_read_managed_cockpit_windows", return_value=managed
        ):
            return commands._cockpit_group_window_action(
                ws,
                "mozyo-cockpit",
                decision=_faithful_decision(group_id=group_id),
                codex_ratio=70,
                launch=lambda role, w: f"{role}-cmd",
            )

    def test_cross_window_focus_when_unit_already_placed_anywhere(self) -> None:
        from mozyo_bridge.application.commands import GROUP_ACTION_FOCUS

        managed = [
            {
                "window": "Alpha",
                "group_id": "alpha",
                "columns": [
                    {"pane_id": "%5", "workspace_id": "wsA", "role": "codex",
                     "lane_id": "default", "pane_left": 0, "pane_width": 80},
                ],
            }
        ]
        action, plan, blocked, window = self._action(managed)
        self.assertEqual(GROUP_ACTION_FOCUS, action)
        self.assertIsNone(blocked)
        self.assertEqual("Alpha", window)
        # Focuses the exact existing pane, in whatever window holds it.
        self.assertEqual(("select-window", "-t", "%5"), plan.commands[0].argv)

    def test_append_into_existing_group_window_by_marker(self) -> None:
        from mozyo_bridge.application.commands import GROUP_ACTION_APPEND

        # A DIFFERENT unit already occupies the alpha group window; the new unit
        # appends a column beside its rightmost codex pane.
        managed = [
            {
                "window": "Alpha",
                "group_id": "alpha",
                "columns": [
                    {"pane_id": "%9", "workspace_id": "wsZ", "role": "codex",
                     "lane_id": "default", "pane_left": 0, "pane_width": 80},
                ],
            }
        ]
        action, plan, blocked, window = self._action(managed)
        self.assertEqual(GROUP_ACTION_APPEND, action)
        self.assertIsNone(blocked)
        self.assertEqual("Alpha", window)
        # Anchored on the existing group-window codex pane.
        self.assertEqual("split-window", plan.commands[0].argv[0])
        self.assertIn("%9", plan.commands[0].argv)

    def test_create_new_group_window_when_no_matching_window(self) -> None:
        from mozyo_bridge.application.commands import GROUP_ACTION_CREATE

        # Only the cockpit home window (no group marker) exists -> create the
        # group's own window.
        managed = [
            {
                "window": "cockpit",
                "group_id": "",
                "columns": [
                    {"pane_id": "%1", "workspace_id": "wsZ", "role": "codex",
                     "lane_id": "default", "pane_left": 0, "pane_width": 80},
                ],
            }
        ]
        action, plan, blocked, window = self._action(managed)
        self.assertEqual(GROUP_ACTION_CREATE, action)
        self.assertIsNone(blocked)
        self.assertEqual("Alpha", window)
        self.assertEqual("new-window", plan.commands[0].argv[0])

    def test_ungrouped_unit_never_shares_a_window(self) -> None:
        from mozyo_bridge.application.commands import GROUP_ACTION_CREATE

        # group_id empty: even a window with an empty group marker must not match;
        # an ungrouped unit always gets its own fresh window.
        managed = [
            {
                "window": "cockpit",
                "group_id": "",
                "columns": [
                    {"pane_id": "%1", "workspace_id": "wsZ", "role": "codex",
                     "lane_id": "default", "pane_left": 0, "pane_width": 80},
                ],
            }
        ]
        action, _plan, _blocked, _window = self._action(managed, group_id=None)
        self.assertEqual(GROUP_ACTION_CREATE, action)

    def test_different_lane_is_not_a_duplicate(self) -> None:
        from mozyo_bridge.application.commands import GROUP_ACTION_APPEND

        # Same workspace id, DIFFERENT lane -> not a focus; it appends as its own
        # column in the group window (worktree / clone semantics, #11820).
        managed = [
            {
                "window": "Alpha",
                "group_id": "alpha",
                "columns": [
                    {"pane_id": "%9", "workspace_id": "wsA", "role": "codex",
                     "lane_id": "worktree-x", "pane_left": 0, "pane_width": 80},
                ],
            }
        ]
        action, _plan, _blocked, _window = self._action(managed, lane="default")
        self.assertEqual(GROUP_ACTION_APPEND, action)


# --- Application: multi-window discovery ---------------------------------------


class ReadManagedWindowsTest(unittest.TestCase):
    def test_reads_each_window_group_marker_and_managed_panes(self) -> None:
        from mozyo_bridge.application import commands

        list_windows = argparse.Namespace(
            returncode=0,
            stdout="cockpit\t\nAlpha\talpha\nstray\t\n",
            stderr="",
        )

        def fake_columns(session, window):
            return {
                "cockpit": [
                    {"pane_id": "%1", "workspace_id": "wsZ", "role": "codex",
                     "lane_id": "default"},
                ],
                "Alpha": [
                    {"pane_id": "%5", "workspace_id": "wsA", "role": "codex",
                     "lane_id": "default"},
                ],
                "stray": [
                    {"pane_id": "%9", "workspace_id": "", "role": "", "lane_id": ""},
                ],
            }[window]

        with patch.object(commands, "run_tmux", return_value=list_windows), \
            patch.object(commands, "_read_cockpit_columns", side_effect=fake_columns):
            managed = commands._read_managed_cockpit_windows("mozyo-cockpit")

        windows = {m["window"]: m for m in managed}
        # `stray` carries no managed pane -> omitted.
        self.assertEqual({"cockpit", "Alpha"}, set(windows))
        self.assertEqual("alpha", windows["Alpha"]["group_id"])
        self.assertEqual("", windows["cockpit"]["group_id"])

    def test_unreadable_window_list_degrades_to_empty(self) -> None:
        from mozyo_bridge.application import commands

        with patch.object(
            commands, "run_tmux",
            return_value=argparse.Namespace(returncode=1, stdout="", stderr="x"),
        ):
            self.assertEqual([], commands._read_managed_cockpit_windows("s"))


# --- Application: rollback boundary --------------------------------------------


class GroupWindowRollbackTest(unittest.TestCase):
    def test_failed_build_kills_only_captured_panes(self) -> None:
        """A mid-create failure kills the captured codex pane; tmux then drops the
        empty window, so no orphan window survives (acceptance #12330)."""
        from mozyo_bridge.application import commands

        plan = build_group_window_create_plan(
            CockpitWorkspace("wsA", "repoA", "/repoA"),
            group_id="alpha",
            window_name="Alpha",
            launch=lambda role, ws: f"{role}-cmd",
        )

        calls = []

        def fake_run(*argv, check=True):
            calls.append(argv)
            if argv[0] == "new-window":
                return argparse.Namespace(returncode=0, stdout="%100", stderr="")
            if argv[0] == "split-window":
                # The claude split fails mid-build.
                return argparse.Namespace(returncode=1, stdout="", stderr="boom")
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch.object(commands, "die", side_effect=SystemExit(2)):
            with self.assertRaises(SystemExit):
                commands.execute_cockpit_plan(
                    plan, fake_run, cleanup_captured=True
                )

        # The captured codex pane is killed; killing the window's only pane drops
        # the window, so there is no orphan group window.
        self.assertIn(("kill-pane", "-t", "%100"), calls)


# --- Application: reset multi-window destruction warning -----------------------


class ResetMultiWindowWarningTest(unittest.TestCase):
    def test_extra_windows_listed_beyond_cockpit(self) -> None:
        from mozyo_bridge.application import commands

        target = argparse.Namespace(windows=("cockpit", "Alpha", "Beta"))
        self.assertEqual(["Alpha", "Beta"], commands._cockpit_extra_windows(target))

    def test_no_extra_windows_when_only_cockpit(self) -> None:
        from mozyo_bridge.application import commands

        target = argparse.Namespace(windows=("cockpit",))
        self.assertEqual([], commands._cockpit_extra_windows(target))


if __name__ == "__main__":
    unittest.main()
