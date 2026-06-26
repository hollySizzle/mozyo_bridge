"""Same-lane Codex→Claude dispatch submit-completion contract (Redmine #12207).

Locks the expected behavior of the same-lane `--to claude` dispatch hop a
target-lane Codex uses to route work to the Claude implementer in its own lane
(`vibes/docs/logics/coordinator-sublane-development-flow.md` ``## 標準フロー`` —
same-lane Claude へ submit 完結で渡す — and
``skills/mozyo-bridge-agent/references/workflow.md``
``## Same-Lane Claude Dispatch``).

The reproduction in #12207 j#60741 was a same-lane dispatch that reported
``blocked`` while the notification sat staged in the Claude pane until the
coordinator pressed Enter by hand. These tests pin the separation the fix
requires:

- a standard same-lane dispatch **submit-completes** — via the default
  ``queue-enter`` rail on an active split, or marker-observed ``--mode
  standard`` on an inactive (cockpit-grid) split — and never silently rests at
  a pending prompt;
- the inactive-split rejection steers recovery onto a *submit-completing*
  ``--mode standard`` rail, not ``--no-submit`` / ``--mode pending``;
- ``--mode pending`` stays an explicit operator/debug fallback that leaves the
  input unsubmitted.

The drive-the-CLI fake-tmux harness mirrors
``tests/test_handoff_orchestrator.py`` ``RelaxedQueueEnterRailTest`` so the
contract is exercised end-to-end without launching tmux. The non-goals of
#12207 (no active-split / receiver-binding / process-gate relaxation, no blind
Enter) are preserved: these tests assert behavior on the *existing* rails, they
do not introduce a new one.
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
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
)


class SameLaneClaudeDispatchSubmitDefaultTest(unittest.TestCase):
    def run_dispatch_with_fake_tmux(
        self,
        argv,
        *,
        pane=None,
        allow_exit: bool = False,
        current_session: str | None = "agents",
    ):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""

        def fake_capture(_target: str, _lines: int) -> str:
            return pane_text

        def fake_run_tmux(*tmux_args, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        default_pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        pane_value = pane if pane is not None else default_pane

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value=current_session), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), stderr.getvalue(), pane_text

    def _dispatch_argv(self, *, mode: str | None = None) -> list[str]:
        argv = [
            "handoff",
            "send",
            "--to",
            "claude",
            "--source",
            "redmine",
            "--issue",
            "12207",
            "--journal",
            "60739",
            "--kind",
            "implementation_request",
            "--target",
            "%2",
        ]
        if mode is not None:
            argv += ["--mode", mode]
        return argv

    def _outcome_from_stdout(self, stdout: str) -> dict:
        lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome found in stdout: {stdout!r}")
        return json.loads(lines[-1])

    def test_active_split_dispatch_submits_by_default_no_mode_flag(self) -> None:
        # The default rail (no --mode) submit-completes the same-lane dispatch:
        # Enter is pressed and the outcome is `sent`, never left pending.
        result, sent, stdout, _stderr, _pane = self.run_dispatch_with_fake_tmux(
            self._dispatch_argv()
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertNotEqual("pending_input", outcome["status"])

    def test_marker_observed_standard_dispatch_submits(self) -> None:
        # The strict-but-submitting fallback also reaches submit once the
        # landing marker is observed — `--mode standard` is not a "leave it
        # pending" mode.
        result, sent, stdout, _stderr, _pane = self.run_dispatch_with_fake_tmux(
            self._dispatch_argv(mode=MODE_STANDARD)
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])

    def test_inactive_split_recovery_steers_to_submit_completing_standard(self) -> None:
        # Cockpit-grid case: the Claude pane is an inactive split, so the
        # default queue-enter rail is fail-closed on the active-split gate and
        # types nothing. The emitted recovery must steer onto the
        # submit-completing `--mode standard` rail — NOT `--no-submit` /
        # `--mode pending`, which would leave the dispatch staged.
        result, sent, stdout, stderr, _pane = self.run_dispatch_with_fake_tmux(
            self._dispatch_argv(),
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "0",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        # Nothing was typed: the queue-enter active-split gate rejects before
        # any send-keys (no staged input).
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])

        combined = stdout + stderr
        self.assertIn("--mode standard", combined)
        # The recovery rail must be the submit-completing one; the pending
        # fallbacks must never be advertised as the dispatch recovery.
        self.assertNotIn("--no-submit", combined)
        self.assertNotIn("--mode pending", combined)
        # The recovery command re-pins the same durable anchor and pane.
        self.assertIn("--issue 12207", combined)
        self.assertIn("--journal 60739", combined)
        self.assertIn("--target %2", combined)

    def test_pending_mode_is_explicit_fallback_and_leaves_input_unsubmitted(
        self,
    ) -> None:
        # `--mode pending` stays the explicit operator/debug fallback: the
        # notification is typed but Enter is NOT pressed, and the outcome is
        # `pending_input`. This is the separation the standard path must avoid.
        result, sent, stdout, _stderr, pane_text = self.run_dispatch_with_fake_tmux(
            self._dispatch_argv(mode=MODE_PENDING)
        )

        self.assertEqual(0, result)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        # The body was typed (staged), but no Enter followed.
        self.assertIn("[mozyo:handoff:", pane_text)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("pending_input", outcome["status"])
        self.assertEqual(MODE_PENDING, outcome["mode"])


if __name__ == "__main__":
    unittest.main()
