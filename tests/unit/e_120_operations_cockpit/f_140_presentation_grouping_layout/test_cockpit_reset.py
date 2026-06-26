"""Cockpit reset / rebuild — safe teardown of a stale/broken cockpit (#11814).

The #11807 append-flatten regression could leave a cockpit that did not self-heal;
the operator fell back to a manual `tmux kill-session`. This US gives that teardown
a first-class, fail-closed UX. These tests pin the pure identity grader
(:func:`assess_cockpit_reset`), the kill-plan builder, the executor, and the
`mozyo cockpit reset` / `rebuild` command — every fail-closed gate and the
confirm-only mutation — all hermetic (no live tmux). The load-bearing regression
is that a same-named session that is NOT mozyo-identified is never killed.
Synthetic, neutral identifiers only.
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

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    COCKPIT_RESET_ABSENT,
    COCKPIT_RESET_FOREIGN,
    COCKPIT_RESET_MANAGED,
    COCKPIT_RESET_UNMANAGED,
    CockpitWorkspace,
    assess_cockpit_reset,
    build_cockpit_reset_plan,
)


def _col(pane_id, *, workspace_id="wsX", role="codex", lane_id="default"):
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "role": role,
        "lane_id": lane_id,
        "pane_left": 0,
        "pane_width": 80,
    }


def _managed_columns():
    return [
        _col("%2", role="codex"),
        _col("%3", role="claude"),
    ]


class AssessCockpitResetTest(unittest.TestCase):
    def test_absent_when_no_session(self) -> None:
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=False, columns=None
        )
        self.assertEqual(COCKPIT_RESET_ABSENT, target.status)
        self.assertTrue(target.absent)
        self.assertFalse(target.resettable)
        self.assertFalse(target.mozyo_identified)

    def test_foreign_when_session_present_but_no_cockpit_window(self) -> None:
        # A same-named session with no `cockpit` window: ownership unconfirmable,
        # so it must NOT be claimed for kill (fail-closed).
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=True, columns=None,
            windows=("shell",),
        )
        self.assertEqual(COCKPIT_RESET_FOREIGN, target.status)
        self.assertFalse(target.resettable)
        self.assertFalse(target.mozyo_identified)
        self.assertIn("by name alone", target.blocked_reason)

    def test_unmanaged_when_cockpit_window_has_no_marker(self) -> None:
        # A `cockpit` window whose panes carry no `@mozyo_workspace_id` marker is
        # not a mozyo cockpit — never kill it (the core regression guard).
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=True,
            columns=[_col("%9", workspace_id="")],
        )
        self.assertEqual(COCKPIT_RESET_UNMANAGED, target.status)
        self.assertFalse(target.resettable)
        self.assertFalse(target.mozyo_identified)
        self.assertEqual(1, len(target.unmanaged_panes))
        self.assertEqual(0, len(target.managed_panes))

    def test_managed_is_resettable_when_detached(self) -> None:
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=True,
            columns=_managed_columns(), attached_clients=(), windows=("cockpit",),
        )
        self.assertEqual(COCKPIT_RESET_MANAGED, target.status)
        self.assertTrue(target.mozyo_identified)
        self.assertTrue(target.resettable)
        self.assertIsNone(target.blocked_reason)
        self.assertEqual(2, len(target.managed_panes))

    def test_managed_fails_closed_when_client_state_unknown(self) -> None:
        # The client read failed (could not be queried). An *unknown* client
        # state must fail closed for a destructive teardown — never treated as
        # "no client attached" (#11814 review j#57928).
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=True,
            columns=_managed_columns(), attached_clients=(),
            attached_clients_known=False,
        )
        self.assertEqual(COCKPIT_RESET_MANAGED, target.status)
        self.assertTrue(target.mozyo_identified)  # identity confirmed...
        self.assertFalse(target.resettable)  # ...but unknown client state blocks
        self.assertIn("unknown", target.blocked_reason)

    def test_managed_fails_closed_when_client_attached(self) -> None:
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=True,
            columns=_managed_columns(), attached_clients=("/dev/ttys003",),
        )
        self.assertEqual(COCKPIT_RESET_MANAGED, target.status)
        self.assertTrue(target.mozyo_identified)  # identity confirmed...
        self.assertFalse(target.resettable)  # ...but a live client blocks it
        self.assertIn("attached client", target.blocked_reason)

    def test_mixed_panes_are_managed_but_stray_pane_reported(self) -> None:
        # A managed cockpit with one stray (unmarked) pane stays resettable and
        # surfaces the stray in the preview inventory.
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=True,
            columns=[_col("%2"), _col("%9", workspace_id="")],
        )
        self.assertEqual(COCKPIT_RESET_MANAGED, target.status)
        self.assertTrue(target.resettable)
        self.assertEqual(1, len(target.managed_panes))
        self.assertEqual(1, len(target.unmanaged_panes))

    def test_as_dict_is_json_safe(self) -> None:
        target = assess_cockpit_reset(
            session="mozyo-cockpit", session_present=True,
            columns=_managed_columns(), windows=("cockpit",),
        )
        payload = target.as_dict()
        json.dumps(payload)  # must not raise
        self.assertTrue(payload["mozyo_identified"])
        self.assertEqual(2, len(payload["managed_panes"]))


class BuildResetPlanTest(unittest.TestCase):
    def test_single_kill_session_command(self) -> None:
        plan = build_cockpit_reset_plan("mozyo-cockpit")
        self.assertEqual(1, len(plan.commands))
        self.assertEqual(
            ("kill-session", "-t", "mozyo-cockpit"), plan.commands[0].argv
        )

    def test_rejects_empty_session(self) -> None:
        with self.assertRaises(ValueError):
            build_cockpit_reset_plan("")

    def test_as_dict_is_json_safe(self) -> None:
        json.dumps(build_cockpit_reset_plan("mozyo-cockpit").as_dict())


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRun:
    """A run_tmux-style callable recording argv, optionally failing on a predicate.

    A `kill-session` (and `new-session` on rebuild) returns a `%pane` id so the
    create executor's capture step is satisfied.
    """

    def __init__(self, fail_when=None):
        self.calls = []
        self.fail_when = fail_when or (lambda argv: False)

    def __call__(self, *argv, check=False):
        self.calls.append(tuple(argv))
        rc = 1 if self.fail_when(argv) else 0
        stdout = "%100\n" if ("-P" in argv and "-F" in argv) else ""
        return FakeResult(returncode=rc, stdout=stdout, stderr="boom" if rc else "")


class ExecuteResetPlanTest(unittest.TestCase):
    def _execute(self, run):
        from mozyo_bridge.application.commands import execute_cockpit_reset_plan

        return execute_cockpit_reset_plan(build_cockpit_reset_plan("mozyo-cockpit"), run)

    def test_runs_kill_session(self) -> None:
        run = RecordingRun()
        self._execute(run)
        self.assertIn(("kill-session", "-t", "mozyo-cockpit"), run.calls)

    def test_fails_fast_on_nonzero(self) -> None:
        run = RecordingRun(fail_when=lambda argv: argv[0] == "kill-session")
        with self.assertRaises(SystemExit):
            self._execute(run)


class SessionAttachedClientsResultTest(unittest.TestCase):
    """`_session_attached_clients_result` separates "no client" from "unreadable"."""

    def _call(self, run):
        from mozyo_bridge.application import commands

        with patch.object(commands, "run_tmux", side_effect=run):
            return commands._session_attached_clients_result("mozyo-cockpit")

    def test_success_empty_is_known(self) -> None:
        clients, known = self._call(lambda *a, **k: FakeResult(0, stdout=""))
        self.assertEqual((), clients)
        self.assertTrue(known)

    def test_success_with_clients_is_known(self) -> None:
        clients, known = self._call(
            lambda *a, **k: FakeResult(0, stdout="/dev/ttys003\n/dev/ttys004\n")
        )
        self.assertEqual(("/dev/ttys003", "/dev/ttys004"), clients)
        self.assertTrue(known)

    def test_nonzero_is_unknown(self) -> None:
        clients, known = self._call(lambda *a, **k: FakeResult(1, stderr="boom"))
        self.assertEqual((), clients)
        self.assertFalse(known)

    def test_exception_is_unknown(self) -> None:
        def boom(*a, **k):
            raise RuntimeError("no server")

        clients, known = self._call(boom)
        self.assertEqual((), clients)
        self.assertFalse(known)


class CockpitResetCommandTest(unittest.TestCase):
    """`mozyo cockpit reset` / `rebuild` — preview vs confirm-gated teardown (#11814)."""

    def _args(self, action="reset", **over):
        base = dict(
            action=action, repo="/workspace/project-alpha", codex_ratio=70,
            cockpit_session=None, dry_run=False, json_output=False,
            no_attach=False, confirm=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(self, *, columns, attached=(), clients_known=True,
                 windows=("cockpit",), session_present=True, run=None):
        from mozyo_bridge.application import commands
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import DEFAULT_LANE, LaneIdentity

        canon = argparse.Namespace(name="mozyo-ws", workspace_id="wsX")
        run = run or RecordingRun()
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_resolve_workspace_lane",
                         return_value=LaneIdentity(DEFAULT_LANE, None)), \
            patch.object(commands, "require_tmux") as require_tmux, \
            patch.object(commands, "_read_cockpit_columns", return_value=columns), \
            patch.object(commands, "_cockpit_session_present",
                         return_value=session_present), \
            patch.object(commands, "_session_attached_clients_result",
                         return_value=(attached, clients_known)), \
            patch.object(commands, "list_session_windows", return_value=list(windows)), \
            patch.object(commands, "_agent_launch_command", return_value="launch"), \
            patch.object(commands, "run_tmux", side_effect=run), \
            patch.object(commands.os, "execvp",
                         side_effect=RuntimeError("attach")) as execvp:
            yield require_tmux, run, execvp

    def _run(self, args, **patched):
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(**patched) as (require_tmux, run, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    rc = cmd_cockpit(args)
                except SystemExit as exc:
                    rc = exc.code
                except RuntimeError:
                    # The patched os.execvp raises instead of replacing the
                    # process — the rebuild reached its attach step.
                    rc = "attached"
        return rc, out.getvalue(), require_tmux, run, execvp

    # --- the load-bearing regression: a non-mozyo session is never killed ----

    def test_confirm_does_not_kill_foreign_session(self) -> None:
        # Same name, but no `cockpit` window -> fail-closed, no kill-session.
        rc, out, _rt, run, _ev = self._run(
            self._args(confirm=True), columns=None, session_present=True,
            windows=("shell",),
        )
        self.assertEqual(2, rc)  # die()
        self.assertNotIn("kill-session", [c[0] for c in run.calls if c])

    def test_confirm_does_not_kill_unmanaged_cockpit(self) -> None:
        # A `cockpit` window with no mozyo marker -> never killed.
        rc, out, _rt, run, _ev = self._run(
            self._args(confirm=True),
            columns=[_col("%9", workspace_id="")],
        )
        self.assertEqual(2, rc)
        self.assertNotIn("kill-session", [c[0] for c in run.calls if c])

    def test_confirm_fails_closed_on_attached_client(self) -> None:
        rc, out, _rt, run, _ev = self._run(
            self._args(confirm=True), columns=_managed_columns(),
            attached=("/dev/ttys003",),
        )
        self.assertEqual(2, rc)
        self.assertNotIn("kill-session", [c[0] for c in run.calls if c])

    def test_confirm_fails_closed_when_client_state_unreadable(self) -> None:
        # tmux list-clients could not be read -> unknown client state -> the
        # destructive confirm path must NOT kill-session (#11814 review j#57928).
        rc, out, _rt, run, _ev = self._run(
            self._args(confirm=True), columns=_managed_columns(),
            attached=(), clients_known=False,
        )
        self.assertEqual(2, rc)  # die()
        self.assertNotIn("kill-session", [c[0] for c in run.calls if c])

    def test_rebuild_confirm_fails_closed_when_client_state_unreadable(self) -> None:
        rc, out, _rt, run, execvp = self._run(
            self._args(action="rebuild", confirm=True),
            columns=_managed_columns(), attached=(), clients_known=False,
        )
        self.assertEqual(2, rc)
        self.assertNotIn("kill-session", [c[0] for c in run.calls if c])
        self.assertNotIn("new-session", [c[0] for c in run.calls if c])
        execvp.assert_not_called()

    # --- confirmed teardown of a proven-managed cockpit ----------------------

    def test_confirm_kills_managed_cockpit(self) -> None:
        run = RecordingRun()
        rc, out, require_tmux, _run, execvp = self._run(
            self._args(confirm=True), columns=_managed_columns(), run=run,
        )
        self.assertEqual(0, rc)
        self.assertIn(("kill-session", "-t", "mozyo-cockpit"), run.calls)
        self.assertIn("killed", out)
        require_tmux.assert_called_once()
        execvp.assert_not_called()  # reset does not attach

    def test_reset_confirm_on_absent_is_noop(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, _ev = self._run(
            self._args(confirm=True), columns=None, session_present=False, run=run,
        )
        self.assertEqual(0, rc)
        self.assertIn("nothing to do", out)
        self.assertEqual([], run.calls)

    # --- preview / json are non-mutating -------------------------------------

    def test_bare_reset_is_preview_only(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, _ev = self._run(
            self._args(), columns=_managed_columns(), run=run,
        )
        self.assertEqual(0, rc)
        self.assertIn("preview", out)
        self.assertIn("reset plan", out)
        self.assertEqual([], run.calls)  # no mutation

    def test_dry_run_outranks_confirm(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, _ev = self._run(
            self._args(confirm=True, dry_run=True), columns=_managed_columns(),
            run=run,
        )
        self.assertEqual(0, rc)
        self.assertEqual([], run.calls)

    def test_json_preview_reports_target_and_plan(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, _ev = self._run(
            self._args(json_output=True), columns=_managed_columns(), run=run,
        )
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual("cockpit reset", payload["command"])
        self.assertFalse(payload["executes"])
        self.assertTrue(payload["target"]["mozyo_identified"])
        self.assertIsNotNone(payload["reset_plan"])
        self.assertEqual([], run.calls)

    def test_json_preview_blocks_foreign(self) -> None:
        rc, out, _rt, _run, _ev = self._run(
            self._args(json_output=True), columns=None, session_present=True,
        )
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertIsNotNone(payload["blocked"])
        self.assertIsNone(payload["reset_plan"])

    # --- rebuild ------------------------------------------------------------

    def test_rebuild_confirm_kills_then_creates_and_attaches(self) -> None:
        run = RecordingRun()
        rc, out, require_tmux, _run, execvp = self._run(
            self._args(action="rebuild", confirm=True),
            columns=_managed_columns(), run=run,
        )
        # execvp(attach) is patched to raise -> the harness surfaces "attached".
        self.assertEqual("attached", rc)
        kills = [c for c in run.calls if c and c[0] == "kill-session"]
        creates = [c for c in run.calls if c and c[0] == "new-session"]
        self.assertTrue(kills, "rebuild must kill the stale cockpit first")
        self.assertTrue(creates, "rebuild must recreate a fresh cockpit")
        # kill happens before create.
        self.assertLess(run.calls.index(kills[0]), run.calls.index(creates[0]))
        execvp.assert_called_once()

    def test_rebuild_no_attach_skips_execvp(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, execvp = self._run(
            self._args(action="rebuild", confirm=True, no_attach=True),
            columns=_managed_columns(), run=run,
        )
        self.assertEqual(0, rc)
        self.assertIn("rebuilt", out)
        self.assertIn("attach: tmux -CC attach -t mozyo-cockpit", out)
        execvp.assert_not_called()

    def test_rebuild_on_absent_creates_without_kill(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, execvp = self._run(
            self._args(action="rebuild", confirm=True, no_attach=True),
            columns=None, session_present=False, run=run,
        )
        self.assertEqual(0, rc)
        self.assertNotIn("kill-session", [c[0] for c in run.calls if c])
        self.assertIn("new-session", [c[0] for c in run.calls if c])

    def test_rebuild_fails_closed_on_foreign_without_killing(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, execvp = self._run(
            self._args(action="rebuild", confirm=True),
            columns=None, session_present=True, windows=("shell",), run=run,
        )
        self.assertEqual(2, rc)
        self.assertNotIn("kill-session", [c[0] for c in run.calls if c])
        self.assertNotIn("new-session", [c[0] for c in run.calls if c])
        execvp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
