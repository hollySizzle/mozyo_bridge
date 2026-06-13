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

    def test_appends_one_column_split_from_anchor(self) -> None:
        plan = self._plan()
        first = plan.commands[0]
        self.assertEqual(("split-window", "-h", "-t", "%7"), first.argv[:4])
        self.assertEqual("@col1_codex", first.captures)
        self.assertEqual(1, plan.columns)
        # widths re-equalized, then a vertical split for claude.
        ops = [c.argv[0] for c in plan.commands]
        self.assertIn("select-layout", ops)
        self.assertTrue(
            any(c.argv[:2] == ("split-window", "-v") for c in plan.commands)
        )

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

        out = "%1\twsA\tcodex\n%2\twsA\tclaude\n%3\twsB\tcodex\n"
        with patch.object(
            commands, "run_tmux",
            return_value=argparse.Namespace(returncode=0, stdout=out, stderr=""),
        ):
            cols = commands._read_cockpit_columns("mozyo-cockpit")
        self.assertEqual(3, len(cols))
        self.assertEqual({"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}, cols[0])

    def test_missing_session_returns_none(self) -> None:
        from mozyo_bridge.application import commands

        with patch.object(
            commands, "run_tmux",
            return_value=argparse.Namespace(returncode=1, stdout="", stderr="no session"),
        ):
            self.assertIsNone(commands._read_cockpit_columns("mozyo-cockpit"))


class CockpitDecisionTest(unittest.TestCase):
    def _args(self, **over):
        base = dict(
            action=None, repo="/repoX", codex_ratio=70, cockpit_session=None,
            dry_run=True, json_output=False, no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(self, *, columns, ws_id="wsX", session_present=False):
        from mozyo_bridge.application import commands

        canon = argparse.Namespace(name="sessX", workspace_id=ws_id)
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_agent_launch_command", side_effect=lambda r, s, c: f"{r}-cmd"), \
            patch.object(commands, "require_tmux"), \
            patch.object(commands, "_read_cockpit_columns", return_value=columns), \
            patch.object(commands, "session_exists", return_value=session_present), \
            patch.object(commands, "run_tmux") as run_tmux, \
            patch.object(commands.os, "execvp", side_effect=RuntimeError("attach")) as execvp:
            yield run_tmux, execvp

    def _run(self, args, columns, ws_id="wsX", session_present=False):
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(
            columns=columns, ws_id=ws_id, session_present=session_present
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
        # appends by splitting the existing codex pane.
        self.assertIn("tmux split-window -h -t %1", out)

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


if __name__ == "__main__":
    unittest.main()
