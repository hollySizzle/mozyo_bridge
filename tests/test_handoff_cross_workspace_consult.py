"""`handoff cross-workspace-consult` primitive (Redmine #11779).

The cross-workspace consult command is a boundary-preserving wrapper over
`handoff send`: it fixes the receiver to `codex` (the consult lands on the
target workspace's Codex gateway pane, never a foreign Claude pane), makes the
cross-workspace identity gate mandatory (`--target` + `--target-repo`
required), and defaults `--kind` to `design_consultation`. These tests pin the
wrapper surface and prove that every underlying safety gate is delegated to the
same orchestration and is neither hidden nor weakened. All hermetic: tmux is
patched at the seams and repo roots are synthetic temp dirs.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser


class ConsultParserSurfaceTest(unittest.TestCase):
    """The wrapper tightens the surface: no --to/--force, target gate required."""

    def _parse(self, argv):
        return build_parser().parse_args(["handoff", "cross-workspace-consult", *argv])

    def _base_argv(self, **overrides):
        argv = [
            "--source", "redmine", "--issue", "11779", "--journal", "58668",
            "--target", "%42", "--target-repo", "auto",
        ]
        return argv

    def test_minimal_valid_args_parse(self) -> None:
        ns = self._parse(self._base_argv())
        self.assertEqual("%42", ns.target)
        self.assertEqual("auto", ns.target_repo)
        # --to is not part of the surface; the handler fixes it to codex.
        self.assertFalse(hasattr(ns, "to"))

    def test_target_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse(["--source", "redmine", "--issue", "1", "--journal", "2",
                         "--target-repo", "auto"])

    def test_target_repo_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse(["--source", "redmine", "--issue", "1", "--journal", "2",
                         "--target", "%42"])

    def test_to_flag_is_rejected(self) -> None:
        # The consult primitive never lets the caller pick the receiver; the
        # gateway is always Codex. Passing --to is an unknown argument.
        with self.assertRaises(SystemExit):
            self._parse([*self._base_argv(), "--to", "claude"])

    def test_force_flag_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse([*self._base_argv(), "--force"])


class ConsultDelegationTest(unittest.TestCase):
    """The wrapper delegates to orchestrate_handoff; gates run unchanged."""

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
        target_repo="auto",
        kind=None,
        sender_session="mysess",
        pane_session="other",
        window_name="codex",
        command="codex",
        pane_active="1",
    ):
        from mozyo_bridge.application import commands

        pane = {
            "id": "%884",
            "location": f"{pane_session}:1.0",
            "command": command,
            "cwd": cwd,
            "window_name": window_name,
            "pane_active": pane_active,
        }
        argv = [
            "handoff", "cross-workspace-consult",
            "--source", "redmine", "--issue", "11779", "--journal", "58668",
            "--landing-timeout", "0.01", "--submit-delay", "0",
        ]
        if target is not None:
            argv += ["--target", target]
        if target_repo is not None:
            argv += ["--target-repo", target_repo]
        if kind is not None:
            argv += ["--kind", kind]
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

    def test_cross_session_codex_gateway_send_succeeds(self) -> None:
        # The headline route: cross-session consult through the target Codex
        # gateway, explicit pane + auto identity, admitted on queue-enter.
        repo = self._git_repo()
        outcome, _out, err = self._run(target="%884", cwd=repo)
        self.assertIsNotNone(outcome)
        self.assertEqual("sent", outcome["status"])
        # Receiver is forced to the Codex gateway even with no --to flag.
        self.assertEqual("codex", outcome["receiver"])
        # Kind defaults to design_consultation.
        self.assertEqual("design_consultation", outcome["kind"])
        self.assertIn("--target-repo auto resolved", err)

    def test_kind_override_is_honoured(self) -> None:
        repo = self._git_repo()
        outcome, _out, _err = self._run(target="%884", cwd=repo, kind="review_request")
        self.assertEqual("review_request", outcome["kind"])
        self.assertEqual("codex", outcome["receiver"])

    def test_explicit_target_repo_path_runs_identity_gate(self) -> None:
        repo = self._git_repo()
        outcome, _out, _err = self._run(target="%884", cwd=repo, target_repo=repo)
        self.assertEqual("sent", outcome["status"])

    def test_target_repo_mismatch_is_rejected(self) -> None:
        # Explicit --target-repo that does not match the pane cwd → blocked,
        # exactly as `handoff send`: the wrapper does not weaken the gate.
        repo = self._git_repo()
        other = self._git_repo()
        outcome, _out, err = self._run(target="%884", cwd=other, target_repo=repo)
        self.assertEqual("target_repo_mismatch", outcome["reason"])

    def test_auto_fails_closed_without_marker(self) -> None:
        outcome, _out, err = self._run(target="%884", cwd=self._bare_dir())
        self.assertEqual("target_repo_mismatch", outcome["reason"])
        self.assertIn("could not infer a workspace/repo root", err)

    def test_auto_requires_explicit_pane_target(self) -> None:
        # `--target-repo auto` with a location-form target is fail-closed in the
        # delegated orchestration — the wrapper inherits that, unweakened.
        repo = self._git_repo()
        outcome, _out, err = self._run(target="mysess:codex", cwd=repo)
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertIn("requires an explicit `%pane` target", err)

    def test_non_agent_target_is_rejected(self) -> None:
        # queue-enter binds strictly to the Codex agent process; a shell pane is
        # rejected and --force is not even on the surface to override it.
        repo = self._git_repo()
        outcome, _out, _err = self._run(
            target="%884", cwd=repo, command="zsh", window_name="codex"
        )
        self.assertEqual("target_not_agent", outcome["reason"])


if __name__ == "__main__":
    unittest.main()
