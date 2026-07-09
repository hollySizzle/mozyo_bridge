"""Regression: herdr-backend standard entrypoints must not fall to tmux selection (#13446).

Expresses the #13435 j#74176 -> j#74177 recurrence as an executable fixture: with
``terminal_transport.backend: herdr`` active and the workspace's agents live, the standard
entrypoints an agent reaches for first — ``handoff send --select`` / ``agents targets``
(tmux semantic selection) and ``workflow step`` (tmux ``%pane`` self-lane resolution) — used
to fall through onto the tmux rails and die with a tmux-shaped ``no_candidate:repo`` /
``self_lane_unresolved``. The preflight guard (#13446) makes each surface fail closed with
``herdr backend active`` + the standard ``sublane create --execute`` dispatch instead.

Hermetic: the herdr backend is simulated by patching the shared
``herdr_entrypoint_preflight.herdr_backend_active`` seam (the consumers import it at call
time), so no test depends on a repo-local config or a live herdr binary.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import commands_agents, commands_target_select
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
    herdr_entrypoint_preflight as pre,
)

MARKER = pre.HERDR_BACKEND_ACTIVE_MARKER


def _herdr_active():
    """Patch the shared backend-detection seam so every surface sees backend=herdr."""
    return patch.object(pre, "herdr_backend_active", return_value=True)


class WorkflowStepRecurrenceTest(unittest.TestCase):
    """`workflow step` must not die on tmux `%pane` / `self_lane_unresolved` under herdr."""

    def test_preflight_returns_herdr_specific_blocked_outcome(self):
        with _herdr_active():
            outcome = cli_workflow._herdr_step_preflight(argparse.Namespace(repo=None))
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.execution, "blocked")
        # herdr-specific reason, NOT the tmux `self_lane_unresolved`.
        self.assertEqual(outcome.reason, "herdr_self_lane_unresolved")
        self.assertEqual(outcome.next_owner, "operator")
        self.assertIn("sublane create --execute", outcome.next_action)
        # It looked at the herdr-native lane env first (Acceptance 2), not a bare %pane.
        self.assertIn("HERDR_PANE_ID=", outcome.detail)
        self.assertIn("MOZYO_WORKSPACE_ID=", outcome.detail)

    def test_dry_run_json_is_herdr_specific_and_returns_rc1(self):
        args = argparse.Namespace(
            repo=None, dry_run=True, as_json=True, session=None,
            issue=None, journal=None, callback=None, store_path=None,
        )
        out = io.StringIO()
        # `require_tmux` / `current_pane` are NOT patched: the preflight must fire before
        # them, so a herdr session with no TMUX_PANE never reaches the tmux rail.
        with _herdr_active(), patch("sys.stdout", out):
            rc = cli_workflow.cmd_workflow_step(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(payload["reason"], "herdr_self_lane_unresolved")
        self.assertIn("HERDR_PANE_ID=", payload["detail"])
        self.assertIn("sublane create --execute", payload["next_action"])

    def test_tmux_backend_preflight_is_a_noop(self):
        # Backend=tmux: the preflight returns None so the tmux path (and its output) is
        # unchanged — the guard is strictly additive under herdr.
        with patch.object(pre, "herdr_backend_active", return_value=False):
            self.assertIsNone(
                cli_workflow._herdr_step_preflight(argparse.Namespace(repo=None))
            )


class HandoffSelectRecurrenceTest(unittest.TestCase):
    """`handoff send --select` selection failure must name the herdr standard dispatch."""

    def test_no_candidate_selection_die_carries_marker_and_sublane(self):
        captured = {}

        def fake_die(msg, *a, **k):
            captured["msg"] = msg
            raise SystemExit(2)

        with _herdr_active(), patch.object(
            commands_target_select, "discover_all_candidates", return_value=[]
        ), patch.object(commands_target_select, "die", fake_die), patch(
            "sys.stderr", io.StringIO()
        ):
            with self.assertRaises(SystemExit):
                commands_target_select.select_semantic_target(
                    role="codex", repo=None, session=None, project=None, sender_cwd="."
                )
        self.assertIn("no message was sent", captured["msg"])
        self.assertIn(MARKER, captured["msg"])
        self.assertIn("sublane create --execute", captured["msg"])

    def test_tmux_backend_selection_die_is_unchanged(self):
        captured = {}

        def fake_die(msg, *a, **k):
            captured["msg"] = msg
            raise SystemExit(2)

        with patch.object(pre, "herdr_backend_active", return_value=False), patch.object(
            commands_target_select, "discover_all_candidates", return_value=[]
        ), patch.object(commands_target_select, "die", fake_die), patch(
            "sys.stderr", io.StringIO()
        ):
            with self.assertRaises(SystemExit):
                commands_target_select.select_semantic_target(
                    role="codex", repo=None, session=None, project=None, sender_cwd="."
                )
        # No herdr marker leaks into the tmux-backend diagnostic.
        self.assertNotIn(MARKER, captured["msg"])


class AgentsTargetsRecurrenceTest(unittest.TestCase):
    """`agents targets` must demote itself to a tmux-era primitive under herdr (read-only)."""

    def test_emits_herdr_demotion_note_to_stderr(self):
        err = io.StringIO()
        with _herdr_active(), patch("sys.stderr", err):
            commands_agents._emit_herdr_backend_note(argparse.Namespace(repo=None))
        text = err.getvalue()
        self.assertIn("note:", text)
        self.assertIn(MARKER, text)
        self.assertIn("sublane create --execute", text)

    def test_tmux_backend_emits_no_note(self):
        err = io.StringIO()
        with patch.object(pre, "herdr_backend_active", return_value=False), patch(
            "sys.stderr", err
        ):
            commands_agents._emit_herdr_backend_note(argparse.Namespace(repo=None))
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
