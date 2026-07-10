"""Regression: herdr-backend standard entrypoints must not fall to tmux selection (#13446).

Expresses the #13435 j#74176 -> j#74177 recurrence as an executable fixture: with
``terminal_transport.backend: herdr`` active and the workspace's agents live, the standard
entrypoints an agent reaches for first — ``handoff send --select`` / ``agents targets``
(tmux semantic selection) and ``workflow step`` (tmux ``%pane`` self-lane resolution) — used
to fall through onto the tmux rails and die with a tmux-shaped ``no_candidate:repo`` /
``self_lane_unresolved``.

``handoff send --select`` / ``agents targets`` still fail closed with ``herdr backend
active`` + the standard ``sublane create --execute`` dispatch (the #13446 preflight guard).
``workflow step`` is now resolved **herdr-natively** (Redmine #13489): it no longer dead-ends
on ``herdr_self_lane_unresolved`` but classifies the lane role from the launch-time sender
identity and returns a role-appropriate outcome — the recurrence guard here is that it never
touches the tmux ``%pane`` rail (``require_tmux`` / ``current_pane``) under the herdr backend.

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
    herdr_workflow_step,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_NO_OP,
    OWNER_GRANDCHILD,
    PRIMITIVE_NONE,
    STATE_GRANDCHILD_REDMINE_WORK,
    WorkflowStepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    REASON_HERDR_WORKER_STEP_READY,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
    herdr_entrypoint_preflight as pre,
)

MARKER = pre.HERDR_BACKEND_ACTIVE_MARKER


def _herdr_active():
    """Patch the shared backend-detection seam so every surface sees backend=herdr."""
    return patch.object(pre, "herdr_backend_active", return_value=True)


def _worker_outcome():
    """A representative herdr-native worker-lane outcome the adapter would resolve."""
    return WorkflowStepOutcome(
        state=STATE_GRANDCHILD_REDMINE_WORK,
        next_action="read the worker anchor and implement",
        execution=EXECUTION_NO_OP,
        reason=REASON_HERDR_WORKER_STEP_READY,
        next_owner=OWNER_GRANDCHILD,
        primitive=PRIMITIVE_NONE,
    )


def _boom(*_a, **_k):
    raise AssertionError("tmux rail reached under the herdr backend")


class WorkflowStepRecurrenceTest(unittest.TestCase):
    """`workflow step` must resolve herdr-natively, never touch the tmux `%pane` rail."""

    def test_preflight_delegates_to_herdr_native_resolution(self):
        # Under herdr the preflight now returns the herdr-native resolution (no dead-end).
        with _herdr_active(), patch.object(
            herdr_workflow_step, "resolve_herdr_step_outcome", return_value=_worker_outcome()
        ):
            outcome = cli_workflow._herdr_step_preflight(argparse.Namespace(repo=None))
        self.assertIsNotNone(outcome)
        # herdr-native reason, NOT the tmux `self_lane_unresolved` nor the #13446 dead-end.
        self.assertEqual(outcome.reason, REASON_HERDR_WORKER_STEP_READY)
        self.assertNotEqual(outcome.reason, "self_lane_unresolved")
        self.assertNotEqual(outcome.reason, "herdr_self_lane_unresolved")

    def test_dry_run_never_touches_the_tmux_rail(self):
        args = argparse.Namespace(
            repo=None, dry_run=True, as_json=True, session=None,
            issue=None, journal=None, callback=None, store_path=None,
        )
        out = io.StringIO()
        # `require_tmux` / `current_pane` blow up if reached: the herdr resolution must fire
        # before them, so a herdr session with no TMUX_PANE never reaches the tmux rail.
        with _herdr_active(), patch.object(
            herdr_workflow_step, "resolve_herdr_step_outcome", return_value=_worker_outcome()
        ), patch.object(cli_workflow, "require_tmux", _boom), patch.object(
            cli_workflow, "current_pane", _boom
        ), patch("sys.stdout", out):
            rc = cli_workflow.cmd_workflow_step(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 0)  # a worker no_op is a forward step
        self.assertEqual(payload["reason"], REASON_HERDR_WORKER_STEP_READY)

    def test_tmux_backend_preflight_is_a_noop(self):
        # Backend=tmux: the preflight returns None so the tmux path (and its output) is
        # unchanged — the herdr resolution is strictly gated on the herdr backend.
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
