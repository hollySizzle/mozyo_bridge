"""mozyo cockpit create/append/focus decision (Redmine #11803).

The `cmd_cockpit` orchestration: deciding create vs append vs focus, failing
closed on a stale cockpit, cleaning up partially-created panes, surfacing the
adopt advisory, and lane-aware duplicate detection (#11820). Split
characterization-first from `tests/test_cockpit_append.py` (Redmine #12152) so
the decision/execution surface is its own maintenance unit; behaviour is
unchanged and every test stubs tmux (hermetic, no live tmux, no destructive
operations).
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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))


class CockpitDecisionTest(unittest.TestCase):
    def _args(self, **over):
        base = dict(
            action=None, repo="/repoX", codex_ratio=70, cockpit_session=None,
            dry_run=True, json_output=False, no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(
        self, *, columns, ws_id="wsX", session_present=False, lane=None, advisory=None
    ):
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.cockpit_layout import (
            ADOPT_STATUS_NONE,
            AdoptAdvisory,
            DEFAULT_LANE,
            LaneIdentity,
        )

        canon = argparse.Namespace(name="sessX", workspace_id=ws_id)
        lane = lane if lane is not None else LaneIdentity(DEFAULT_LANE, None)
        # Stub the adopt detector so the create/append/focus tests stay hermetic
        # (#11897): the real one would read the live session inventory via tmux.
        advisory = advisory if advisory is not None else AdoptAdvisory(
            ws_id, DEFAULT_LANE, ADOPT_STATUS_NONE, (), None
        )
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_agent_launch_command", side_effect=lambda r, s, c, **_: f"{r}-cmd"), \
            patch.object(commands, "require_tmux"), \
            patch.object(commands, "_read_cockpit_columns", return_value=columns), \
            patch.object(commands, "_resolve_workspace_lane", return_value=lane), \
            patch.object(commands, "_cockpit_adopt_advisory", return_value=advisory), \
            patch.object(commands, "session_exists", return_value=session_present), \
            patch.object(commands, "run_tmux") as run_tmux, \
            patch.object(commands.os, "execvp", side_effect=RuntimeError("attach")) as execvp:
            yield run_tmux, execvp

    def _run(
        self, args, columns, ws_id="wsX", session_present=False, lane=None, advisory=None
    ):
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(
            columns=columns,
            ws_id=ws_id,
            session_present=session_present,
            lane=lane,
            advisory=advisory,
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

    # --- Adopt advisory rides the normal flow as a non-mutating notice (#11897) ---

    def _candidate_advisory(self):
        from mozyo_bridge.domain.cockpit_layout import detect_adopt_candidates
        from mozyo_bridge.domain.cockpit_layout import NormalSessionObservation

        return detect_adopt_candidates(
            workspace_id="wsX",
            lane_id="default",
            observations=[
                NormalSessionObservation("mozyo-ws", "wsX", "default", "codex", "%2"),
                NormalSessionObservation("mozyo-ws", "wsX", "default", "claude", "%3"),
            ],
        )

    def test_advisory_surfaced_in_dry_run_append(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _r, _e = self._run(
            self._args(), columns=cols, advisory=self._candidate_advisory()
        )
        self.assertIn("action=append", out)
        self.assertIn("adopt candidate", out)  # advisory printed under the plan

    def test_advisory_in_json_payload(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _r, _e = self._run(
            self._args(dry_run=False, json_output=True),
            columns=cols,
            advisory=self._candidate_advisory(),
        )
        payload = json.loads(out)
        self.assertEqual("append", payload["action"])
        self.assertTrue(payload["adopt_advisory"]["adoptable"])
        self.assertEqual("candidate", payload["adopt_advisory"]["status"])

    def test_no_advisory_on_focus(self) -> None:
        # The workspace is already a cockpit column (focus): adopt does not fire
        # even when the detector would surface a candidate (focus priority,
        # j#57823), so no advisory line is printed.
        cols = [{"pane_id": "%5", "workspace_id": "wsX", "role": "codex"}]
        out, _r, _e = self._run(
            self._args(), columns=cols, ws_id="wsX", advisory=self._candidate_advisory()
        )
        self.assertIn("action=focus", out)
        self.assertNotIn("adopt candidate", out)

    def test_advisory_surfaced_after_real_append(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        with self._patched(columns=cols, advisory=self._candidate_advisory()) as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(returncode=0, stdout="%9", stderr="")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cmd_cockpit(self._args(dry_run=False))
        self.assertIn("appended", out.getvalue())
        self.assertIn("adopt candidate", out.getvalue())

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


if __name__ == "__main__":
    unittest.main()
