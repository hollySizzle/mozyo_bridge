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
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.agent_discovery import codex_gateway_candidates
from mozyo_bridge.domain.handoff import (
    cross_session_gateway_hint,
    is_explicit_pane_target,
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

    def test_diagnostics_tmux_failure_does_not_break_claude_block(self) -> None:
        # Regression for the Redmine #11778 CI failure: the best-effort gateway
        # hint calls pane_lines(), which raises SystemExit (via die()) when no
        # tmux server is reachable (e.g. CI). That must NOT pre-empt the
        # cross_session_claude boundary message — diagnostics catch SystemExit.
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        args = build_parser().parse_args(
            [
                "handoff", "send", "--to", "claude",
                "--source", "redmine", "--issue", "10332", "--journal", "49623",
                "--kind", "implementation_request",
                "--target", "%9", "--mode", "standard",
            ]
        )

        def boom(*_a, **_k):
            raise SystemExit("error: tmux list-panes failed: tmux missing")

        with patch.object(commands, "require_tmux"), \
            patch.object(commands, "capture_pane", return_value=""), \
            patch.object(commands, "run_tmux", return_value=argparse.Namespace(returncode=0, stdout="", stderr="")), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch.object(commands, "current_session_name", return_value="local"), \
            patch.object(
                commands,
                "pane_info",
                return_value=_pane("%9", "other", "claude"),
            ), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", side_effect=boom), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                args.func(args)
        # The terminal output is the boundary message, not the tmux error.
        self.assertIn("cross-session handoff to Claude is not allowed", err.getvalue())
        json_lines = [l for l in out.getvalue().splitlines() if l.strip().startswith("{")]
        self.assertTrue(json_lines)
        self.assertEqual("cross_session_claude", json.loads(json_lines[-1])["reason"])


class ExplicitPaneTargetPredicateTest(unittest.TestCase):
    def test_pane_id_is_explicit(self) -> None:
        self.assertTrue(is_explicit_pane_target("%884"))

    def test_label_location_and_empty_are_not_explicit(self) -> None:
        self.assertFalse(is_explicit_pane_target("codex"))
        self.assertFalse(is_explicit_pane_target("sess:codex"))
        self.assertFalse(is_explicit_pane_target(""))
        self.assertFalse(is_explicit_pane_target(None))


class AutoTargetRepoTest(unittest.TestCase):
    """`--target-repo auto` (Redmine #11778): explicit-pane-only identity helper."""

    def _git_repo(self) -> str:
        ctx = tempfile.TemporaryDirectory()
        tmp = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        repo = (Path(tmp) / "ws").resolve()
        (repo / ".git").mkdir(parents=True)
        return str(repo)

    def _bare_dir(self) -> str:
        ctx = tempfile.TemporaryDirectory()
        tmp = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        d = (Path(tmp) / "no-marker").resolve()
        d.mkdir(parents=True)
        return str(d)

    def _run(
        self,
        *,
        target,
        cwd,
        receiver="codex",
        sender_session="mysess",
        pane_session="mysess",
        window_name=None,
        command=None,
        pane_active="1",
        target_repo="auto",
    ):
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        window_name = window_name or receiver
        command = command or receiver
        pane = {
            "id": "%884",
            "location": f"{pane_session}:1.0",
            "command": command,
            "cwd": cwd,
            "window_name": window_name,
            "pane_active": pane_active,
        }
        argv = [
            "handoff", "send", "--to", receiver,
            "--source", "redmine", "--issue", "11775", "--journal", "56743",
            "--kind", "review_request",
            "--landing-timeout", "0.01", "--submit-delay", "0",
        ]
        if target is not None:
            argv += ["--target", target]
        if target_repo is not None:
            argv += ["--target-repo", target_repo]
        args = build_parser().parse_args(argv)

        def fake_run_tmux(*a, check: bool = True):
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch.object(commands, "require_tmux"), \
            patch.object(commands, "capture_pane", return_value=""), \
            patch.object(commands, "run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch.object(commands, "current_session_name", return_value=sender_session), \
            patch.object(commands, "pane_info", return_value=pane), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                args.func(args)
            except SystemExit:
                pass
        outcome = None
        for line in out.getvalue().splitlines():
            if line.strip().startswith("{"):
                try:
                    outcome = json.loads(line)
                except ValueError:
                    pass
        return outcome, out.getvalue(), err.getvalue()

    def test_auto_resolves_root_from_explicit_pane_and_sends(self) -> None:
        repo = self._git_repo()
        outcome, _out, err = self._run(target="%884", cwd=repo)
        # Auto resolved the identity gate; the send was not rejected by auto
        # nor by the repo-mismatch gate.
        self.assertIn("--target-repo auto resolved", err)
        self.assertIn("repo_root=", err)
        self.assertIsNotNone(outcome)
        self.assertNotIn(outcome["reason"], {"invalid_args", "target_repo_mismatch"})
        self.assertEqual("sent", outcome["status"])

    def test_auto_resolves_for_cross_session_codex_gateway(self) -> None:
        # The headline #11775 win: cross-session `--to codex` + explicit pane +
        # auto is admitted without a hand-passed --target-repo.
        repo = self._git_repo()
        outcome, _out, err = self._run(
            target="%884", cwd=repo, pane_session="other", sender_session="mysess"
        )
        self.assertIn("--target-repo auto resolved", err)
        self.assertEqual("sent", outcome["status"])

    def test_auto_rejects_non_explicit_location_target(self) -> None:
        repo = self._git_repo()
        outcome, _out, err = self._run(target="mysess:codex", cwd=repo)
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertIn("requires an explicit `%pane` target", err)

    def test_auto_rejects_implicit_receiver_target(self) -> None:
        repo = self._git_repo()
        outcome, _out, err = self._run(target=None, cwd=repo)
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertIn("requires an explicit `%pane` target", err)

    def test_auto_fails_closed_when_cwd_has_no_marker(self) -> None:
        outcome, _out, err = self._run(target="%884", cwd=self._bare_dir())
        self.assertEqual("target_repo_mismatch", outcome["reason"])
        self.assertIn("could not infer a workspace/repo root", err)

    def test_auto_does_not_bypass_cross_session_claude_block(self) -> None:
        # Even with a resolvable pane root, cross-session `--to claude` stays
        # blocked — auto only resolves identity, it never opens the direct route.
        repo = self._git_repo()
        outcome, _out, err = self._run(
            target="%883",
            cwd=repo,
            receiver="claude",
            command="claude",
            window_name="claude",
            pane_session="other",
            sender_session="mysess",
        )
        self.assertEqual("cross_session_claude", outcome["reason"])
        self.assertIn("cross-session handoff to Claude is not allowed", err)


class InactiveQueueEnterPaneFallbackTest(unittest.TestCase):
    """Redmine #12071 / #12162: an inactive queue-enter pane surfaces the
    `--mode standard` fallback so the operator does not have to know the
    active-split constraint is queue-enter-only. #12162 upgraded the prose hint
    to a concrete, copy-pasteable `handoff send … --target %pane --target-repo
    auto --mode standard` recovery command built from the resolved pane and
    anchor. queue-enter stays the default rail — `--mode standard` reads as a
    fallback, never a requirement (`tmux-send-safety-contract.md`).
    """

    def _git_repo(self) -> str:
        ctx = tempfile.TemporaryDirectory()
        tmp = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        repo = (Path(tmp) / "ws").resolve()
        (repo / ".git").mkdir(parents=True)
        return str(repo)

    def _run(self, *, target, cwd, target_repo, pane_active="0"):
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        pane = {
            "id": "%884",
            "location": "mysess:1.0",
            "command": "codex",
            "cwd": cwd,
            "window_name": "codex",
            "pane_active": pane_active,
        }
        argv = [
            "handoff", "send", "--to", "codex",
            "--source", "redmine", "--issue", "12071", "--journal", "59628",
            "--kind", "review_request",
            "--landing-timeout", "0.01", "--submit-delay", "0",
        ]
        if target is not None:
            argv += ["--target", target]
        if target_repo is not None:
            argv += ["--target-repo", target_repo]
        args = build_parser().parse_args(argv)

        def fake_run_tmux(*a, check: bool = True):
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch.object(commands, "require_tmux"), \
            patch.object(commands, "capture_pane", return_value=""), \
            patch.object(commands, "run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch.object(commands, "current_session_name", return_value="mysess"), \
            patch.object(commands, "pane_info", return_value=pane), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                args.func(args)
            except SystemExit:
                pass
        outcome = None
        for line in out.getvalue().splitlines():
            if line.strip().startswith("{"):
                try:
                    outcome = json.loads(line)
                except ValueError:
                    pass
        return outcome, err.getvalue()

    def test_inactive_pane_with_repo_identity_names_standard_fallback(self) -> None:
        # Explicit pane + `--target-repo auto` (resolves to a concrete root): the
        # safest retry is the strict `--mode standard` rail, which does not need
        # the active split. #12162: the hint is now the concrete recovery command
        # carrying the resolved pane id and the durable anchor.
        outcome, err = self._run(
            target="%884", cwd=self._git_repo(), target_repo="auto"
        )
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertIn("active split of its window", err)
        self.assertIn("pane_active=", err)
        self.assertIn(
            "mozyo-bridge handoff send --to codex --source redmine "
            "--kind review_request --issue 12071 --journal 59628 "
            "--target %884 --target-repo auto --mode standard",
            err,
        )

    def test_inactive_pane_without_repo_identity_hints_pin_and_standard(self) -> None:
        # No `--target-repo` on the original send: the recovery command is still
        # built from the resolved pane id (#12162), so the operator gets the same
        # concrete `--target %884 --target-repo auto --mode standard` retry — the
        # resolved pane is always an explicit `%pane`, so `auto` can pin it.
        outcome, err = self._run(
            target="%884", cwd=self._git_repo(), target_repo=None
        )
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertIn("active split of its window", err)
        self.assertIn(
            "mozyo-bridge handoff send --to codex --source redmine "
            "--kind review_request --issue 12071 --journal 59628 "
            "--target %884 --target-repo auto --mode standard",
            err,
        )

    def test_active_pane_is_not_blocked_by_the_active_split_gate(self) -> None:
        # Control: the same explicit pane, active, is not rejected by Step 11 —
        # the fallback hint only fires on the inactive-pane block.
        outcome, _err = self._run(
            target="%884", cwd=self._git_repo(), target_repo="auto", pane_active="1"
        )
        self.assertEqual("sent", outcome["status"])


if __name__ == "__main__":
    unittest.main()
