"""`mozyo cockpit` reads `project_group_presentation` for launch/append (#12302, #12330).

The cockpit launcher / append path resolves the Project-Group presentation
placement from `.mozyo-bridge/config.yaml`
`presentation.project_group_presentation`:

- `same_cockpit_column` (the default / a missing config) preserves the current
  column append/create behavior exactly;
- `project_group_tmux_window` now *faithfully executes* (#12330): the launcher
  places the sublane in the Project Group's own tmux window. With no managed
  windows discoverable (the minimal stub here) that is a `group_create`; the
  cross-window focus / append cases are covered in
  `test_cockpit_group_window.py`. It is not degraded and never spawns a fresh
  iTerm window — the operator switches tmux windows;
- `normal_window` still records the *desired* placement and visibly degrades to
  the shared cockpit column (relaunching a normal window is out of scope);
- an invalid placement config fails closed (reported under --json, fatal on a
  real run).

Hermetic: every tmux read/mutation and the repo-local config load are stubbed —
no live tmux, no file IO, no destructive operations.
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

from mozyo_bridge.domain.repo_local_config import (  # noqa: E402
    RepoLocalConfig,
    RepoLocalConfigError,
)


def _config_for(mode: str | None) -> RepoLocalConfig:
    """A repo-local config whose presentation placement mode is ``mode``."""
    if mode is None:
        return RepoLocalConfig.default()
    return RepoLocalConfig.from_record(
        {"presentation": {"project_group_presentation": mode}}
    )


class CockpitPresentationPlacementTest(unittest.TestCase):
    def _args(self, **over):
        base = dict(
            action=None, repo="/repoX", codex_ratio=70, cockpit_session=None,
            dry_run=True, json_output=False, no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(self, *, columns, ws_id="wsX", load_return=None, load_side_effect=None):
        from mozyo_bridge.application import commands
        from mozyo_bridge.application import repo_local_config_loader
        from mozyo_bridge.domain.cockpit_layout import (
            ADOPT_STATUS_NONE,
            AdoptAdvisory,
            DEFAULT_LANE,
            LaneIdentity,
        )

        canon = argparse.Namespace(name="sessX", workspace_id=ws_id)
        lane = LaneIdentity(DEFAULT_LANE, None)
        advisory = AdoptAdvisory(ws_id, DEFAULT_LANE, ADOPT_STATUS_NONE, (), None)
        load_kwargs = {}
        if load_side_effect is not None:
            load_kwargs["side_effect"] = load_side_effect
        else:
            load_kwargs["return_value"] = (
                load_return if load_return is not None else RepoLocalConfig.default()
            )
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_agent_launch_command", side_effect=lambda r, s, c, **_: f"{r}-cmd"), \
            patch.object(commands, "require_tmux"), \
            patch.object(commands, "_read_cockpit_columns", return_value=columns), \
            patch.object(commands, "_resolve_workspace_lane", return_value=lane), \
            patch.object(commands, "_cockpit_adopt_advisory", return_value=advisory), \
            patch.object(commands, "session_exists", return_value=False), \
            patch.object(commands, "run_tmux") as run_tmux, \
            patch.object(repo_local_config_loader, "load_repo_local_config", **load_kwargs), \
            patch.object(commands.os, "execvp", side_effect=RuntimeError("attach")) as execvp:
            yield run_tmux, execvp

    def _run(self, args, columns, *, ws_id="wsX", load_return=None, load_side_effect=None):
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(
            columns=columns, ws_id=ws_id,
            load_return=load_return, load_side_effect=load_side_effect,
        ) as (run_tmux, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
                try:
                    rc = cmd_cockpit(args)
                except (RuntimeError, SystemExit) as exc:
                    rc = exc
        return out.getvalue(), rc, run_tmux, execvp

    # --- same_cockpit_column is behavior-preserving (default + explicit) ---

    def test_default_config_is_behavior_preserving_append(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _rc, _r, _e = self._run(self._args(), cols)
        self.assertIn("action=append", out)
        # No degrade notice on the default placement.
        self.assertNotIn("presentation:", out)

    def test_explicit_same_cockpit_column_not_degraded(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _rc, _r, _e = self._run(
            self._args(dry_run=False, json_output=True), cols,
            load_return=_config_for("same_cockpit_column"),
        )
        payload = json.loads(out)
        self.assertEqual("append", payload["action"])
        self.assertEqual("cockpit_column", payload["presentation"]["desired_surface"])
        self.assertEqual("cockpit_column", payload["presentation"]["executed_surface"])
        self.assertFalse(payload["presentation"]["degraded"])
        self.assertIsNone(payload["presentation_blocked"])

    # --- project_group_tmux_window: faithful execution (#12330) ---

    def test_tmux_window_json_records_faithful_execution(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _rc, _r, _e = self._run(
            self._args(dry_run=False, json_output=True), cols,
            load_return=_config_for("project_group_tmux_window"),
        )
        payload = json.loads(out)
        # With no managed windows discoverable in this minimal stub the faithful
        # path creates the group's own window — not the shared column.
        self.assertEqual("group_create", payload["action"])
        pres = payload["presentation"]
        self.assertEqual("group_tmux_window", pres["desired_surface"])
        self.assertEqual("group_tmux_window", pres["executed_surface"])
        self.assertFalse(pres["degraded"])
        self.assertIsNone(pres["diagnostic"])
        self.assertEqual("sessX", payload["group_window"])

    def test_tmux_window_dry_run_prints_faithful_notice(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _rc, _r, _e = self._run(
            self._args(), cols,
            load_return=_config_for("project_group_tmux_window"),
        )
        self.assertIn("action=group_create", out)
        self.assertIn("Project Group window", out)
        # Faithful, not degraded: the #12302 degrade wording must be gone.
        self.assertNotIn("never guarantees a tmux window", out)

    def test_tmux_window_real_run_creates_group_window_no_attach(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        with self._patched(
            columns=cols, load_return=_config_for("project_group_tmux_window"),
        ) as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(returncode=0, stdout="%9", stderr="")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cmd_cockpit(self._args(dry_run=False))
        self.assertEqual(0, rc)
        execvp.assert_not_called()  # group window, no new iTerm window
        self.assertIn("created Project Group window", out.getvalue())

    def test_normal_window_degrades_to_column(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, _rc, _r, _e = self._run(
            self._args(dry_run=False, json_output=True), cols,
            load_return=_config_for("normal_window"),
        )
        payload = json.loads(out)
        self.assertEqual("normal_window", payload["presentation"]["desired_surface"])
        self.assertTrue(payload["presentation"]["degraded"])

    # --- invalid config fails closed ---

    def test_invalid_config_reported_under_json_without_aborting(self) -> None:
        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        out, rc, run_tmux, _e = self._run(
            self._args(dry_run=False, json_output=True), cols,
            load_side_effect=RepoLocalConfigError("bad placement: iterm_tab"),
        )
        payload = json.loads(out)
        self.assertIsNone(payload["presentation"])
        self.assertIn("iterm_tab", payload["presentation_blocked"])
        run_tmux.assert_not_called()  # read-only, no mutation

    def test_invalid_config_fails_closed_on_real_run(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        cols = [{"pane_id": "%1", "workspace_id": "wsA", "role": "codex"}]
        with self._patched(
            columns=cols,
            load_side_effect=RepoLocalConfigError("bad placement: iterm_tab"),
        ) as (run_tmux, execvp):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_cockpit(self._args(dry_run=False))
        run_tmux.assert_not_called()  # never mutates on a fail-closed config
        execvp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
