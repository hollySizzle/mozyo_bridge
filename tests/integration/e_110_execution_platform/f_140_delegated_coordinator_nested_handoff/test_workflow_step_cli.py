"""`workflow step` CLI integration tests (Redmine #12755).

Covers the standard-entrypoint behavior the design fixes
(``vibes/docs/logics/workflow-step-command-design.md``):

- the family registers as the standard ``workflow step`` entrypoint;
- ``--dry-run`` reports the resolved outcome without dispatching a primitive;
- ``--json`` emits exactly one structured outcome envelope;
- a fail-closed lane (anchor-required / blocked) returns rc 1 with the next owner;
- an executable forward leg dispatches the internal primitive (the AI never types
  ``project-gateway consult`` / a ``%pane`` / a rail), and the dispatch reaches the
  gated ``orchestrate_handoff``.
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

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (
    cli_project_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    TargetCandidate,
    VIEW_KIND_COCKPIT_PANE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow,
)

REPO = "/work/repo"
PROJECT = "cloud-drive"


def _cand(pane_id, *, role="codex", project_scope="", lane_kind=""):
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source="pane_option",
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session="gw",
        window_name="w",
        window_index="0",
        pane_index="0",
        active=False,
        workspace_id="ws",
        workspace_label="ws",
        lane_id="lane",
        lane_label="lane",
        repo_short="repo",
        repo_root=REPO,
        cwd=REPO,
        host="host",
        view_kind=VIEW_KIND_COCKPIT_PANE,
        branch=None,
        lane_kind=lane_kind,
        delegation_parent="",
        project_scope=project_scope,
        project_path="",
        project_label="",
    )


def _args(**overrides):
    base = dict(dry_run=False, as_json=False, session=None, issue=None, journal=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def _run(args, candidates, *, self_pane="%self"):
    out = io.StringIO()
    with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
        cli_workflow, "current_pane", lambda: self_pane
    ), patch.object(
        cli_workflow, "_discover_candidates", return_value=candidates
    ), contextlib.redirect_stdout(out):
        rc = cli_workflow.cmd_workflow_step(args)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_workflow_step_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "step", "--dry-run", "--json"])
        self.assertEqual(ns.func.__name__, "cmd_workflow_step")
        self.assertTrue(ns.dry_run)
        self.assertTrue(ns.as_json)


class DryRunTest(unittest.TestCase):
    def test_dry_run_json_envelope_is_single_object(self):
        rc, text = _run(
            _args(dry_run=True, as_json=True),
            [_cand("%self"), _cand("%gw", project_scope=PROJECT)],
        )
        payload = json.loads(text)  # must parse as exactly one JSON object
        self.assertEqual(rc, 0)
        self.assertEqual(payload["execution"], "dry_run")
        self.assertEqual(payload["reason"], "consultation_ready")
        self.assertEqual(payload["next_owner"], "parent")
        self.assertEqual(payload["primitive"], "project_gateway_consult")

    def test_dry_run_does_not_dispatch_primitive(self):
        # If a primitive were dispatched, build_parser/orchestrate would be hit.
        with patch.object(cli_workflow, "_execute_primitive") as exec_mock:
            rc, _ = _run(
                _args(dry_run=True),
                [_cand("%self"), _cand("%gw", project_scope=PROJECT)],
            )
        exec_mock.assert_not_called()
        self.assertEqual(rc, 0)


class FailClosedTest(unittest.TestCase):
    def test_child_anchor_required_rc1(self):
        rc, text = _run(
            _args(),
            [_cand("%self", project_scope=PROJECT, lane_kind="delegated_coordinator")],
        )
        self.assertEqual(rc, 1)
        self.assertIn("anchor_required", text)
        self.assertIn("next_owner: child", text)

    def test_unsafe_self_lane_rc1(self):
        rc, text = _run(_args(), [_cand("%other", project_scope=PROJECT)])
        self.assertEqual(rc, 1)
        self.assertIn("self_lane_unresolved", text)


class ExecuteForwardLegTest(unittest.TestCase):
    """The grandparent forward leg dispatches `project-gateway consult` internally."""

    def test_consult_is_dispatched_and_reaches_orchestrate(self):
        gateway = _cand("%gw", project_scope=PROJECT)
        captured: dict[str, object] = {}

        def fake_orchestrate(args, **kwargs):
            captured["target"] = getattr(args, "target", None)
            captured["to"] = getattr(args, "to", None)
            captured["target_repo"] = getattr(args, "target_repo", None)
            captured["target_project"] = getattr(args, "target_project", None)
            captured["ticketless_consultation"] = kwargs.get("ticketless_consultation")
            return 0

        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", return_value=[_cand("%self"), gateway]
        ), patch.object(
            cli_project_gateway, "require_tmux", lambda: None
        ), patch.object(
            cli_project_gateway, "_discover_candidates", return_value=[gateway]
        ), patch.object(
            cli_project_gateway, "orchestrate_handoff", side_effect=fake_orchestrate
        ), contextlib.redirect_stdout(out):
            rc = cli_workflow.cmd_workflow_step(_args())

        self.assertEqual(rc, 0)
        # The pane was resolved by the primitive, not typed by the caller.
        self.assertEqual(captured["target"], "%gw")
        self.assertEqual(captured["to"], "codex")
        self.assertEqual(captured["target_repo"], REPO)
        self.assertEqual(captured["target_project"], PROJECT)
        self.assertTrue(captured["ticketless_consultation"])
        self.assertIn("execution: executed", out.getvalue())

    def test_execute_json_is_single_envelope(self):
        gateway = _cand("%gw", project_scope=PROJECT)
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", return_value=[_cand("%self"), gateway]
        ), patch.object(
            cli_project_gateway, "require_tmux", lambda: None
        ), patch.object(
            cli_project_gateway, "_discover_candidates", return_value=[gateway]
        ), patch.object(
            cli_project_gateway, "orchestrate_handoff", side_effect=lambda a, **k: 0
        ), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli_workflow.cmd_workflow_step(_args(as_json=True))

        payload = json.loads(out.getvalue())  # exactly one JSON object
        self.assertEqual(rc, 0)
        self.assertEqual(payload["execution"], "executed")
        self.assertEqual(payload["primitive_rc"], 0)
        self.assertIn("primitive_output", payload)


if __name__ == "__main__":
    unittest.main()
