"""CLI surface for the semantic project-gateway route (Redmine #12668).

Pins ``project-gateway resolve`` (read-only text + JSON) and the
``project-gateway handoff`` fail-closed behavior with discovery mocked, so the
command layer's classification + exit codes + no-deliver-on-fail-closed contract
are covered without touching tmux. The pure resolver scenarios live in
``test_project_gateway_resolution``.
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

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (
    cli_project_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    TargetCandidate,
)

REPO = "/work/gk-3500-it-operations"
PROJECT = "giken-cloud-drive-management"


def _candidate(pane_id, *, role="codex", repo_root=REPO, project_scope=PROJECT, session="gw"):
    return TargetCandidate(
        pane_id=pane_id, role=role, role_source=ROLE_SOURCE_PANE_OPTION,
        confidence=CONFIDENCE_STRONG, ambiguous=False, session=session,
        window_name="cockpit", window_index="0", pane_index="0", active=False,
        workspace_id="ws", workspace_label="gk", lane_id="default", lane_label=None,
        repo_short="gk-3500-it-operations", repo_root=repo_root,
        cwd=f"{repo_root}/projects/{project_scope}", host="local",
        view_kind=VIEW_KIND_COCKPIT_PANE, branch="main",
        project_scope=project_scope, project_path=f"projects/{project_scope}",
        project_label="label",
    )


def _resolve_args(**overrides):
    base = dict(repo=REPO, project=PROJECT, role="codex", session=None, as_json=False)
    base.update(overrides)
    return argparse.Namespace(**base)


@patch.object(cli_project_gateway, "require_tmux", lambda: None)
class ResolveCliTest(unittest.TestCase):
    def _run(self, args, candidates):
        out = io.StringIO()
        with patch.object(cli_project_gateway, "_discover_candidates", return_value=candidates):
            with contextlib.redirect_stdout(out):
                rc = cli_project_gateway.cmd_project_gateway_resolve(args)
        return rc, out.getvalue()

    def test_found_text(self):
        rc, text = self._run(_resolve_args(), [_candidate("%gw"), _candidate("%w", role="claude")])
        self.assertEqual(rc, 0)
        self.assertIn("status: found", text)
        self.assertIn("pane_id=%gw", text)
        self.assertIn("project-gateway handoff", text)

    def test_found_json(self):
        rc, text = self._run(_resolve_args(as_json=True), [_candidate("%gw")])
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        self.assertEqual(payload["status"], "found")
        self.assertEqual(payload["selected"]["runtime"]["pane_id"], "%gw")

    def test_missing_names_start_action(self):
        rc, text = self._run(_resolve_args(), [_candidate("%w", role="claude")])
        self.assertEqual(rc, 1)
        self.assertIn("status: gateway_missing", text)
        self.assertIn("start_project_gateway", text)
        self.assertIn("mozyo-bridge cockpit", text)

    def test_ambiguous_lists_candidates(self):
        rc, text = self._run(
            _resolve_args(),
            [_candidate("%gw1", session="a"), _candidate("%gw2", session="b")],
        )
        self.assertEqual(rc, 1)
        self.assertIn("status: gateway_target_ambiguous", text)
        self.assertIn("%gw1", text)
        self.assertIn("%gw2", text)
        self.assertIn("--session", text)


@patch.object(cli_project_gateway, "require_tmux", lambda: None)
class HandoffCliTest(unittest.TestCase):
    def _handoff_args(self, **overrides):
        base = dict(
            to="codex", target_repo=REPO, target_project=PROJECT, target=None,
            gateway_session=None, as_json=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_refuses_when_missing_does_not_deliver(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude")]):
            with patch.object(cli_project_gateway, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args())
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIn("gateway_missing", out.getvalue())

    def test_found_injects_pane_and_delegates(self):
        captured = {}

        def fake_orch(args):
            captured["target"] = args.target
            return 0

        with patch.object(cli_project_gateway, "_discover_candidates",
                          return_value=[_candidate("%gw")]):
            with patch.object(cli_project_gateway, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args())
        self.assertEqual(rc, 0)
        self.assertEqual(captured["target"], "%gw")

    def test_rejects_explicit_target(self):
        with patch.object(cli_project_gateway, "_discover_candidates", return_value=[]):
            with self.assertRaises(SystemExit):
                cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args(target="%99"))

    def test_rejects_auto_target_repo(self):
        with patch.object(cli_project_gateway, "_discover_candidates", return_value=[]):
            with self.assertRaises(SystemExit):
                cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args(target_repo="auto"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
