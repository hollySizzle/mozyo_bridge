"""orchestrate_handoff wiring for role-profile redmine_project auto-resolution (Redmine #13477).

Task #13477 mid-review j#74505 finding_3: the field-resolver unit tests do not
pin the `orchestrate_handoff` (commands.py) wiring — that commands passes the
sender repo root to the resolver, resolves the role profile before any pane
send, auto-fills a coordinator `redmine_project` from the verified
workspace-local default, and fails closed (blocked / invalid_args, no pane text)
when a required default is missing / unverified.

Everything runs against a fake tmux rail + a temp repo — no real tmux, no
external send, no real `~/.mozyo_bridge`. The transport-isolation fixture pins
the tmux default rail so the committed herdr cutover config does not intercept.
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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from . import (  # noqa: E402,F401
    setUpModule,
    tearDownModule,
)

from mozyo_bridge.application.cli import build_parser

_DEFAULTS_YAML = (
    "schema_version: 1\n"
    "redmine:\n"
    "  default_project:\n"
    "    identifier: {identifier}\n"
    "    name: Example\n"
    "    url: https://redmine.giken.or.jp/projects/{identifier}\n"
    "    parent_label: parent\n"
    "  verification:\n"
    "    verified: {verified}\n"
    '    verification_date: "2026-07-10"\n'
    '    verified_by: "tester"\n'
    "outputs:\n"
    "  - kind: redmine_markdown\n"
    "    target: .mozyo-bridge/redmine-defaults.md\n"
)

_IDENTIFIER = "giken-3800-mozyo-bridge"


class RoleProfileDefaultResolutionWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _write_defaults(self, *, verified: str = "true") -> None:
        (self.repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        (self.repo / ".mozyo-bridge" / "project-defaults.yaml").write_text(
            _DEFAULTS_YAML.format(identifier=_IDENTIFIER, verified=verified),
            encoding="utf-8",
        )

    def _run(self, argv, *, allow_exit: bool = False):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""

        def fake_capture(_target: str, _lines: int) -> str:
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:1] == ("select-pane",):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.repo_root_from_args",
                return_value=self.repo,
            ), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value="agents"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), stderr.getvalue(), pane_text

    def _outcome_from_stdout(self, stdout: str) -> dict:
        lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome found in stdout: {stdout!r}")
        return json.loads(lines[-1])

    def _coordinator_argv(self) -> list[str]:
        return [
            "handoff", "send", "--to", "claude",
            "--source", "redmine", "--issue", "13477", "--journal", "74480",
            "--kind", "implementation_request",
            "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
            "--role-profile", "coordinator",
        ]

    def test_verified_default_autofills_redmine_project_and_sends(self) -> None:
        # Path (a): coordinator profile, explicit project omitted, verified
        # workspace default -> the contract carries the resolved identifier and
        # redmine_project is not reported unresolved; the send proceeds to sent.
        self._write_defaults(verified="true")
        result, sent, stdout, _stderr, _pane_text = self._run(self._coordinator_argv())

        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("coordinator", outcome["role_profile"]["role_profile"])
        self.assertNotIn(
            "redmine_project", outcome["role_profile"]["unresolved_placeholders"]
        )
        # The resolved contract (record_format=both) prints the substituted
        # identifier into the durable delivery record on stdout.
        self.assertIn(_IDENTIFIER, stdout)
        # Enter was pressed (send happened) — the fail-closed path is distinct.
        self.assertTrue(any(c == ("send-keys", "-t", "%2", "Enter") for c in sent))

    def test_missing_default_fails_closed_without_send(self) -> None:
        # Path (b): coordinator profile, explicit project omitted, NO workspace
        # default -> blocked / invalid_args, and no pane text is typed (the
        # fail-closed fires before any send).
        result, sent, stdout, stderr, pane_text = self._run(
            self._coordinator_argv(), allow_exit=True
        )

        self.assertIsInstance(result, SystemExit)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        self.assertEqual("", pane_text)
        self.assertIn("redmine_project", stderr)

    def test_unverified_default_fails_closed_without_send(self) -> None:
        # Path (b'): an unverified default is a suggestion only -> fail closed,
        # no send, exactly like a missing default.
        self._write_defaults(verified="false")
        result, sent, stdout, stderr, pane_text = self._run(
            self._coordinator_argv(), allow_exit=True
        )

        self.assertIsInstance(result, SystemExit)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        self.assertEqual("", pane_text)

    def test_role_without_redmine_project_placeholder_sends_without_default(self) -> None:
        # implementation_worker has no redmine_project placeholder, so a missing
        # workspace default must NOT block the send (the gate is placeholder-scoped).
        argv = [
            "handoff", "send", "--to", "claude",
            "--source", "redmine", "--issue", "13477", "--journal", "74480",
            "--kind", "implementation_request",
            "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
            "--role-profile", "implementation_worker",
            "--profile-field", "lane=issue_13477",
            "--profile-field", "gateway_callback_target=w16:p4",
        ]
        result, sent, stdout, _stderr, _pane_text = self._run(argv)

        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(
            "implementation_worker", outcome["role_profile"]["role_profile"]
        )


if __name__ == "__main__":
    unittest.main()
