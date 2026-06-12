"""Cross-workspace handoff gateway diagnostics (Redmine #11776).

The cross-session safety boundary is unchanged: `--to claude` across sessions
stays blocked and `session:codex` still resolves through the live tmux window.
These tests only pin the *diagnostics* that point the operator at the safe
Codex gateway route — the candidate pane discovery and the hint/diagnostic
strings — plus one integration check that the blocked `cross_session_claude`
path surfaces a concrete candidate pane. All hermetic: pure helpers use
synthetic pane dicts, and the integration patches tmux at the seams.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.agent_discovery import codex_gateway_candidates
from mozyo_bridge.domain.handoff import (
    cross_session_gateway_hint,
    target_unavailable_codex_diagnostic,
)


def _pane(pane_id, session, window_name, cwd="/repo", active="1"):
    return {
        "id": pane_id,
        "location": f"{session}:1.0",
        "command": "node",
        "cwd": cwd,
        "window_name": window_name,
        "pane_active": active,
    }


class CodexGatewayCandidateDiscoveryTest(unittest.TestCase):
    def test_returns_only_codex_panes_in_target_session(self) -> None:
        panes = [
            _pane("%9", "other", "claude"),
            _pane("%884", "other", "codex"),
            _pane("%5", "local", "codex"),  # different session
            _pane("%7", "other", "shell"),  # not an agent
        ]
        cands = codex_gateway_candidates("other", panes)
        self.assertEqual(["%884"], [c.pane_id for c in cands])
        self.assertEqual("codex", cands[0].agent_kind)
        self.assertEqual("other", cands[0].session)

    def test_empty_session_yields_no_candidates(self) -> None:
        self.assertEqual([], codex_gateway_candidates("", [_pane("%1", "x", "codex")]))

    def test_no_codex_pane_yields_empty(self) -> None:
        panes = [_pane("%9", "other", "claude")]
        self.assertEqual([], codex_gateway_candidates("other", panes))


class GatewayHintFormattingTest(unittest.TestCase):
    def test_hint_lists_candidate_and_copyable_command(self) -> None:
        cands = [
            {
                "pane_id": "%884",
                "window_name": "codex",
                "cwd": "/ws/cloud-drive",
                "repo_root": "/ws/cloud-drive",
            }
        ]
        hint = cross_session_gateway_hint("target-sess", cands)
        self.assertIn("target-sess", hint)
        self.assertIn("%884", hint)
        self.assertIn("repo_root=/ws/cloud-drive", hint)
        # A copyable explicit-pane gateway command with the candidate's root.
        self.assertIn("--to codex --target %884 --target-repo /ws/cloud-drive", hint)

    def test_hint_without_candidates_explains_missing_codex_window(self) -> None:
        hint = cross_session_gateway_hint("target-sess", [])
        self.assertIn("no Codex-classified pane", hint)
        self.assertIn("agent_kind=codex", hint)
        self.assertIn("target-sess", hint)

    def test_unresolved_repo_root_is_marked(self) -> None:
        cands = [{"pane_id": "%884", "window_name": "codex", "cwd": "/x", "repo_root": None}]
        hint = cross_session_gateway_hint("s", cands)
        self.assertIn("repo_root=<unresolved>", hint)
        self.assertIn("--target-repo <target_workspace_root>", hint)


class TargetUnavailableDiagnosticTest(unittest.TestCase):
    def test_distinguishes_window_name_from_classification(self) -> None:
        cands = [
            {
                "pane_id": "%884",
                "window_name": "codex-cloud",
                "cwd": "/ws",
                "repo_root": "/ws",
            }
        ]
        diag = target_unavailable_codex_diagnostic("sess", "codex", cands)
        self.assertIn("'sess:codex' did not resolve", diag)
        self.assertIn("window *name* exactly", diag)
        self.assertIn("agent_kind", diag)
        self.assertIn("%884", diag)
        self.assertIn("explicit pane id", diag)

    def test_no_candidate_points_at_starting_codex_window(self) -> None:
        diag = target_unavailable_codex_diagnostic("sess", "codex", [])
        self.assertIn("No pane in 'sess' is classified agent_kind=codex", diag)
        self.assertIn("mozyo", diag)


class CrossSessionClaudeHintIntegrationTest(unittest.TestCase):
    """The blocked cross_session_claude path surfaces a concrete gateway pane."""

    def _run_send(self, panes):
        from mozyo_bridge.application.cli import build_parser

        args = build_parser().parse_args(
            [
                "handoff", "send", "--to", "claude",
                "--source", "redmine", "--issue", "10332", "--journal", "49623",
                "--kind", "implementation_request",
                "--target", "%9", "--mode", "standard",
            ]
        )

        def fake_run_tmux(*a, check: bool = True):
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", return_value=""), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value="local",
            ), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                args.func(args)
        return out.getvalue(), err.getvalue()

    def test_blocked_claude_send_names_codex_gateway_pane(self) -> None:
        # Target %9 (claude) lives in session 'other'; a codex pane %884 also
        # lives there. The send is still blocked, but the message now points at
        # %884 as the gateway route.
        panes = [
            _pane("%9", "other", "claude"),
            _pane("%884", "other", "codex"),
        ]
        _stdout, stderr = self._run_send(panes)
        # Safety boundary intact.
        self.assertIn("cross-session handoff to Claude is not allowed", stderr)
        # Diagnostics added: concrete gateway candidate.
        self.assertIn("Gateway route", stderr)
        self.assertIn("%884", stderr)

    def test_blocked_claude_send_without_codex_still_blocks_and_guides(self) -> None:
        panes = [_pane("%9", "other", "claude")]
        _stdout, stderr = self._run_send(panes)
        self.assertIn("cross-session handoff to Claude is not allowed", stderr)
        self.assertIn("no Codex-classified pane", stderr)


if __name__ == "__main__":
    unittest.main()
