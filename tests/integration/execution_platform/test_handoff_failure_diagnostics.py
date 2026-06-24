"""Failure-diagnostics contract for `handoff send` (Redmine #12193).

Pins the operator-facing contract of the three blocked delivery paths the US
names — inactive split, marker timeout, and target-repo mismatch — so the
transcript an operator sees can never silently contradict the durable journal:

- exit code is the shared ``die()`` value ``2`` (matched the #12193 j#61041
  dispatch record but was never asserted in the suite before);
- the failure is **never silent**: stdout carries the human delivery record
  (``Delivery result —``) *and* the single-line JSON outcome with the exact
  ``status`` / ``reason``, and stderr carries the ``error:`` line plus the
  mode-specific recovery hint;
- the record narrative and the JSON outcome agree on ``status`` / ``reason``;
- queue-enter safety is not weakened: the inactive-split and mismatch gates
  fire before any typing, and the marker-timeout rollback presses no Enter.

This module changes no behavior; it locks the existing contract. The deeper
"C-u rollback may not clear a TUI composer" observation from #12193 j#61041 was
carried to #12188, which reworded the marker_timeout diagnostics so they claim
only that a ``C-u`` rollback was issued and Enter was not pressed — never that
the receiver composer was verified cleared. This module pins that wording on
the stderr ``die()`` line (the ``rolled_back`` narrative stays aligned with the
block-severity ``tmux-send-safety-contract.md``).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

# Self-contained src bootstrap so isolated discovery (unittest discover
# scoped to this subpackage or a single file) imports mozyo_bridge without
# relying on a sibling test inserting src first (Redmine #12490 j#64426).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.domain.handoff import MODE_QUEUE_ENTER, MODE_STANDARD

DIE_EXIT_CODE = 2


class _HandoffFailureHarness(unittest.TestCase):
    """Shared fake-tmux drivers for the blocked handoff paths."""

    def _run_standard(self, argv, *, pane, captures=None, current_session=None):
        """Drive a strict ``--mode standard`` send (marker-timeout / mismatch).

        Mirrors the proven ``test_handoff_orchestrator`` harness: the marker is
        never pre-confirmed unless ``captures`` supplies it, and any raised
        ``SystemExit`` is returned so the caller can pin ``.code``.
        """
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""
        forced_captures = captures is not None
        capture_outputs = list(captures or [])

        def fake_capture(_target: str, _lines: int) -> str:
            if capture_outputs:
                return capture_outputs.pop(0)
            if forced_captures:
                return ""
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), patch(
            "mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture
        ), patch(
            "mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux
        ), patch(
            "mozyo_bridge.application.commands.time.sleep"
        ), patch(
            "mozyo_bridge.application.commands.current_session_name",
            return_value=current_session,
        ), patch(
            "mozyo_bridge.domain.pane_resolver.validate_target"
        ), patch(
            "mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]
        ), contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                result = exc

        return result, sent, stdout.getvalue(), stderr.getvalue(), pane_text

    def _run_queue_enter(self, argv, *, pane, current_session="agents"):
        """Drive a default ``queue-enter`` send into an inactive split.

        Mirrors ``test_notify_inactive_pane_fallback``'s proven patch set so the
        same-session active-split gate is reached, and returns any raised
        ``SystemExit`` for ``.code`` assertions.
        """
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), patch(
            "mozyo_bridge.application.commands.current_pane", return_value="%1"
        ), patch(
            "mozyo_bridge.application.commands.current_session_name",
            return_value=current_session,
        ), patch(
            "mozyo_bridge.application.commands.pane_window_name",
            return_value=pane.get("window_name"),
        ), patch(
            "mozyo_bridge.application.commands.pane_location",
            return_value="agents:0.0",
        ), patch(
            "mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux
        ), patch(
            "mozyo_bridge.domain.pane_resolver.validate_target"
        ), patch(
            "mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]
        ), contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                result = exc

        return result, sent, stdout.getvalue(), stderr.getvalue()

    def _json_outcome(self, stdout: str) -> dict:
        lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome on stdout: {stdout!r}")
        return json.loads(lines[-1])

    def _assert_blocked_not_silent(self, stdout, stderr, *, reason):
        """Common contract: blocked is visible on both streams and consistent."""
        self.assertIn("Delivery result —", stdout)  # human record present
        outcome = self._json_outcome(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual(reason, outcome["reason"])
        # Record narrative and JSON agree on (status, reason).
        self.assertIn(f"`blocked` (reason: `{reason}`)", stdout)
        # stderr carries the die() error line — the transcript is never empty.
        self.assertIn("error:", stderr)
        return outcome


class MarkerTimeoutDiagnosticsTest(_HandoffFailureHarness):
    def test_marker_timeout_pins_exit_stdout_stderr_and_no_enter(self) -> None:
        result, sent, stdout, stderr, _pane_text = self._run_standard(
            [
                "handoff", "send",
                "--to", "claude",
                "--source", "redmine",
                "--kind", "implementation_request",
                "--issue", "12193",
                "--journal", "61038",
                "--target", "%2",
                "--mode", "standard",
                "--landing-timeout", "0.01",
                "--submit-delay", "0",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            captures=["", "", ""],  # marker never observed → timeout
        )

        # Exit code: the shared die() value, recorded verbatim in j#61041.
        self.assertIsInstance(result, SystemExit)
        self.assertEqual(DIE_EXIT_CODE, result.code)

        outcome = self._assert_blocked_not_silent(stdout, stderr, reason="marker_timeout")
        self.assertEqual("sender", outcome["next_action_owner"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])

        # Fail-closed: C-u rollback sent, Enter never pressed.
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)
        self.assertFalse(
            any(call == ("send-keys", "-t", "%2", "Enter") for call in sent),
            msg=f"Enter pressed on marker_timeout: {sent!r}",
        )
        # #12188: the die() error line claims only that a C-u rollback was
        # issued and Enter was not pressed — never that the receiver composer
        # was verified cleared (a sender cannot confirm composer state from
        # tmux capture).
        self.assertIn("a C-u rollback was issued and Enter was not pressed", stderr)
        self.assertIn("receiver composer state was not verified", stderr)
        self.assertNotIn("input was cleared and Enter was not pressed", stderr)

        # The durable delivery record on stdout carries the same distinction.
        self.assertIn("C-u rollback was issued", stdout)
        self.assertIn("cannot verify", stdout)
        self.assertNotIn("input was cleared via C-u", stdout)

        # stderr trailer surfaces the bounded --no-submit fallback budget.
        self.assertIn("hint: fallback path:", stderr)
        self.assertIn("mozyo-bridge read claude", stderr)
        self.assertIn("--no-submit", stderr)
        self.assertIn("separate budgets", stderr)


class InactiveSplitDiagnosticsTest(_HandoffFailureHarness):
    def test_inactive_split_pins_exit_recovery_command_and_no_typing(self) -> None:
        result, sent, stdout, stderr = self._run_queue_enter(
            [
                "handoff", "send",
                "--to", "codex",
                "--source", "redmine",
                "--kind", "review_request",
                "--issue", "12193",
                "--journal", "61038",
                "--target", "%2",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "codex",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "0",  # inactive split
            },
        )

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(DIE_EXIT_CODE, result.code)

        outcome = self._assert_blocked_not_silent(stdout, stderr, reason="invalid_args")
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

        # Concrete strict-rail recovery on BOTH streams (#12162 contract).
        expected_recovery = (
            "mozyo-bridge handoff send --to codex --source redmine "
            "--kind review_request --issue 12193 --journal 61038 "
            "--target %2 --target-repo auto --mode standard"
        )
        self.assertIn("Fallback recovery", stdout)
        self.assertIn(expected_recovery, stdout)
        self.assertIn(expected_recovery, stderr)

        # queue-enter guard intact: nothing typed into the inactive split.
        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            msg=f"typed into an inactive split: {sent!r}",
        )


class TargetRepoMismatchDiagnosticsTest(_HandoffFailureHarness):
    def test_target_repo_mismatch_pins_exit_expected_observed_and_no_typing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            expected = Path(tmp_str) / "expected_repo"
            (expected / ".mozyo-bridge").mkdir(parents=True)
            (expected / ".mozyo-bridge" / "scaffold.json").write_text("{}", encoding="utf-8")
            other = Path(tmp_str) / "other_repo"
            (other / "src").mkdir(parents=True)
            (other / "pyproject.toml").write_text("", encoding="utf-8")

            result, sent, stdout, stderr, _pane_text = self._run_standard(
                [
                    "handoff", "send",
                    "--to", "claude",
                    "--source", "redmine",
                    "--kind", "implementation_request",
                    "--issue", "12193",
                    "--journal", "61038",
                    "--target", "%2",
                    "--target-repo", str(expected),
                    "--mode", "standard",
                    "--landing-timeout", "0.01",
                    "--submit-delay", "0",
                ],
                pane={
                    "id": "%2",
                    "location": "agents:0.1",
                    "command": "node",
                    "cwd": str(other / "src"),
                    "window_name": "claude",
                    "pane_active": "1",
                },
            )

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(DIE_EXIT_CODE, result.code)

        outcome = self._assert_blocked_not_silent(stdout, stderr, reason="target_repo_mismatch")
        self.assertEqual(MODE_STANDARD, outcome["mode"])
        # The gate fires before typing; the marker is null in the outcome.
        self.assertIsNone(outcome["notification_marker"])

        # Operator-actionable stderr: expected vs observed, no marker scrollback.
        self.assertIn("not in the expected repo", stderr)
        self.assertIn("expected=", stderr)
        self.assertIn("observed=", stderr)

        # Fail-closed before any keystroke.
        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            msg=f"typed before the repo-identity gate: {sent!r}",
        )


class UniformExitCodeTest(_HandoffFailureHarness):
    """All three blocked modes converge on the same non-zero exit code, so an
    operator (or a wrapper script) can treat any blocked handoff uniformly."""

    def test_blocked_modes_share_exit_code_two(self) -> None:
        marker_timeout, _s1, _o1, _e1, _p1 = self._run_standard(
            [
                "handoff", "send", "--to", "claude", "--source", "redmine",
                "--kind", "implementation_request", "--issue", "12193",
                "--journal", "61038", "--target", "%2", "--mode", "standard",
                "--landing-timeout", "0.01", "--submit-delay", "0",
            ],
            pane={
                "id": "%2", "location": "agents:0.1", "command": "node",
                "cwd": "/repo", "window_name": "claude", "pane_active": "1",
            },
            captures=["", "", ""],
        )
        inactive_split, _s2, _o2, _e2 = self._run_queue_enter(
            [
                "handoff", "send", "--to", "codex", "--source", "redmine",
                "--kind", "review_request", "--issue", "12193",
                "--journal", "61038", "--target", "%2",
            ],
            pane={
                "id": "%2", "location": "agents:0.1", "command": "codex",
                "cwd": "/repo", "window_name": "codex", "pane_active": "0",
            },
        )

        for result in (marker_timeout, inactive_split):
            self.assertIsInstance(result, SystemExit)
            self.assertEqual(DIE_EXIT_CODE, result.code)


if __name__ == "__main__":
    unittest.main()
