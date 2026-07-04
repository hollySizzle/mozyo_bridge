from __future__ import annotations

import argparse
import contextlib
import io
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

# tmux-rail transport isolation (Redmine #13254): this fake-tmux module is a
# tmux send/capture-rail test, independent of the workspace terminal_transport
# backend. Import the package fixture so unittest pins resolve_handoff_transport_
# binding to the tmux default and the committed herdr cutover config does not
# drive these sends through the herdr shim.
from tests.integration.e_110_execution_platform.f_130_handoff_routing import (  # noqa: E402,F401
    setUpModule,
    tearDownModule,
)

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    clear_read,
    require_read,
    resolve_target,
)

class MessageContractTest(unittest.TestCase):
    def run_message_with_fake_tmux(
        self,
        argv: list[str],
        captures: list[str] | None = None,
        allow_exit: bool = False,
    ):
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

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.require_read"), \
            patch("mozyo_bridge.application.commands.clear_read"), \
            patch(
                "mozyo_bridge.application.commands_target_select.resolve_message_target",
                return_value="%2",
            ), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="codex"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep") as sleep, \
            contextlib.redirect_stdout(io.StringIO()):
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, pane_text, sleep

    def test_message_submits_enter_after_marker_by_default(self) -> None:
        result, sent, pane_text, _sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "hello body", "--submit-delay", "0"]
        )

        self.assertEqual(0, result)
        self.assertIn("[mozyo-bridge from:codex pane:%1 at:agents:0.0]", pane_text)
        self.assertIn("hello body", pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

    def test_message_no_submit_leaves_input_pending(self) -> None:
        result, sent, pane_text, _sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "pending body", "--no-submit"]
        )

        self.assertEqual(0, result)
        self.assertIn("pending body", pane_text)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

    def test_message_no_submit_help_frames_it_as_operator_debug_fallback(self) -> None:
        # Redmine #12207: the `message --no-submit` help must read as an explicit
        # operator/debug fallback, not as a neutral / standard option. A future
        # edit that softens it back to a plain "type but don't submit" gloss —
        # which is what let same-lane dispatch agents treat pending as a normal
        # path — must fail here.
        parser = build_parser()
        subparsers_action = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        # argparse hard-wraps help to the terminal width, so collapse runs of
        # whitespace before matching phrases that may straddle a wrap boundary.
        help_text = " ".join(
            subparsers_action.choices["message"].format_help().split()
        )

        self.assertIn("Operator/debug fallback", help_text)
        self.assertIn("NOT the standard handoff path", help_text)
        self.assertIn("#12207", help_text)

    def test_message_rolls_back_when_marker_is_not_observed(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            result, sent, _pane_text, _sleep = self.run_message_with_fake_tmux(
                [
                    "message",
                    "%2",
                    "lost body",
                    "--landing-timeout",
                    "0.01",
                    "--submit-delay",
                    "0",
                ],
                captures=["", "", ""],
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)

    def test_message_waits_submit_delay_after_marker(self) -> None:
        _result, _sent, _pane_text, sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "delayed body", "--submit-delay", "0.2"]
        )

        sleep.assert_called_once_with(0.2)

    def test_message_submit_defaults_to_true(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["message", "%2", "hi"])

        self.assertTrue(args.submit)
        self.assertEqual(0.2, args.submit_delay)
        self.assertEqual(8.0, args.landing_timeout)

    def test_landing_timeout_default_is_eight_seconds_for_tui_redraw(self) -> None:
        # Redmine #10756: the landing-timeout default was raised 5.0 -> 8.0 to
        # absorb Claude/Codex TUI redraw delay. The marker rail still returns
        # as soon as the marker is observed, so this does not add success-path
        # latency. read-lines and submit-delay defaults are intentionally
        # unchanged. Pin the default across the parser surfaces that share the
        # timing flags (message / notify-delivery / handoff send).
        parser = build_parser()

        message_args = parser.parse_args(["message", "%2", "hi"])
        self.assertEqual(8.0, message_args.landing_timeout)
        self.assertEqual(0.2, message_args.submit_delay)

        notify_args = parser.parse_args(["notify-codex", "--target", "%2"])
        self.assertEqual(8.0, notify_args.landing_timeout)
        self.assertEqual(0.2, notify_args.submit_delay)
        self.assertEqual(20, notify_args.read_lines)

        handoff_args = parser.parse_args(
            ["handoff", "send", "--to", "codex", "--source", "redmine", "--kind", "reply"]
        )
        self.assertEqual(8.0, handoff_args.landing_timeout)
        self.assertEqual(0.2, handoff_args.submit_delay)
        self.assertEqual(50, handoff_args.read_lines)

    def test_message_submits_enter_when_marker_wraps_in_capture(self) -> None:
        # Receiver TUI (codex / claude code) word-wraps long input at the
        # visible pane width, inserting a literal newline + continuation
        # indent inside the marker. capture-pane -J cannot rejoin these
        # (the wrap is TUI-emitted, not tmux-display-wrap), so a raw
        # `marker in capture` search misses it. The landing gate must
        # still observe the marker and proceed to Enter.
        wrapped_capture = (
            "› [mozyo-bridge from:codex pane:%1\n"
            "  at:agents:0.0] hello body\n"
        )
        result, sent, _pane_text, _sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "hello body", "--submit-delay", "0"],
            captures=[wrapped_capture],
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

    def test_message_still_rolls_back_when_capture_lacks_marker(self) -> None:
        # Safety lock: even after wrap-tolerant normalization, a capture
        # that does not actually contain the marker must trigger C-u
        # rollback and skip Enter. Fail-closed is non-negotiable.
        unrelated_capture = "› unrelated placeholder\n  with continuation indent\n"
        with contextlib.redirect_stderr(io.StringIO()):
            result, sent, _pane_text, _sleep = self.run_message_with_fake_tmux(
                [
                    "message",
                    "%2",
                    "lost body",
                    "--landing-timeout",
                    "0.01",
                    "--submit-delay",
                    "0",
                ],
                captures=[unrelated_capture, unrelated_capture, unrelated_capture],
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)


class MessageGateGuidanceTest(unittest.TestCase):
    """Regression coverage for Asana task 1214779823377861.

    The CLI must emit a structured stderr trailer after `mozyo-bridge message`
    read-marker / marker-observation gate failures so agents see the literal
    retry path and the per-preset `--no-submit` retry budget. Without this
    trailer, agents have been observed conflating the `--no-submit` budget
    with the `handoff send` retry pool and jumping straight to the preset's
    `Notification fails` branch after a single transient failure (see Asana
    task 1214774670696760 comment 1214778979254677 for the failure example).
    """

    def _run_message_with_gate_failure(
        self,
        argv: list[str],
        *,
        require_read_side_effect=None,
        suppress_marker_in_capture: bool = False,
    ):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""

        def fake_capture(_target: str, _lines: int) -> str:
            # When the test exercises the wait_for_text rollback path, the
            # capture must not echo the typed marker even though
            # `fake_run_tmux` accumulates it into `pane_text`. Otherwise the
            # gate would observe the marker (because the marker is in
            # `pane_text`) and the rollback branch never fires.
            if suppress_marker_in_capture:
                return ""
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        require_read_patch = (
            patch(
                "mozyo_bridge.application.commands.require_read",
                side_effect=require_read_side_effect,
            )
            if require_read_side_effect is not None
            else patch("mozyo_bridge.application.commands.require_read")
        )

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            require_read_patch, \
            patch("mozyo_bridge.application.commands.clear_read"), \
            patch(
                "mozyo_bridge.application.commands_target_select.resolve_message_target",
                return_value="%2",
            ), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="codex"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                result = exc

        return result, sent, stderr.getvalue()

    def test_no_submit_read_marker_failure_emits_retry_path_and_budget(self) -> None:
        # require_read dies with the literal next-action verb ("read target
        # again before interacting"). The CLI must augment stderr with an
        # explicit retry path and the per-preset --no-submit budget so the
        # agent does not need to pattern-match from memory (failure mode #1 in
        # the task body).
        result, _sent, stderr = self._run_message_with_gate_failure(
            ["message", "%2", "pending body", "--no-submit"],
            require_read_side_effect=SystemExit(2),
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn("hint: retry path:", stderr)
        self.assertIn("mozyo-bridge read %2", stderr)
        self.assertIn("--no-submit retry budget", stderr)
        # The base preset cap is 3; if this assertion ever fails, double-check
        # NO_SUBMIT_RETRY_BUDGET in domain/handoff.py and update the preset
        # `Notification fails` branch in lockstep.
        self.assertIn("3", stderr)
        self.assertIn(
            "handoff send",
            stderr,
            "stderr must name the `handoff send` pool to prevent budget conflation (failure mode #2 in task 1214779823377861)",
        )

    def test_no_submit_read_marker_failure_with_attempt_reports_remaining(
        self,
    ) -> None:
        # --attempt N parameterizes the budget reporting so the agent knows
        # exactly how many --no-submit retries remain. Operator-tracked
        # because the CLI is stateless across invocations.
        result, _sent, stderr = self._run_message_with_gate_failure(
            [
                "message",
                "%2",
                "pending body",
                "--no-submit",
                "--attempt",
                "2",
            ],
            require_read_side_effect=SystemExit(2),
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn("attempt 2/3 just failed", stderr)
        self.assertIn("1/3 attempts remaining", stderr)

    def test_submit_marker_timeout_still_rolls_back_and_emits_guidance(
        self,
    ) -> None:
        # Safety-gate regression: the existing fail-closed contract (no Enter
        # when marker is not observed; C-u rollback) must remain intact, and
        # the new guidance trailer must fire alongside it. This is the test
        # for failure mode #3 in the task body ("Notification fails" used as
        # escape hatch after transient failure).
        result, sent, stderr = self._run_message_with_gate_failure(
            [
                "message",
                "%2",
                "lost body",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            suppress_marker_in_capture=True,
        )

        self.assertIsInstance(result, SystemExit)
        # Fail-closed contract: Enter not pressed, C-u issued.
        self.assertFalse(
            any(call == ("send-keys", "-t", "%2", "Enter") for call in sent)
        )
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)
        # New trailer present.
        self.assertIn("hint: retry path:", stderr)
        self.assertIn("mozyo-bridge read %2", stderr)
        # In default (submit) mode no --no-submit budget trailer is emitted —
        # `--no-submit` was not requested. The retry-path line is enough; the
        # budget line is gated on --no-submit so we do not over-promise a
        # budget that does not apply here.
        self.assertNotIn("--no-submit retry budget:", stderr)

    def test_no_submit_message_happy_path_emits_no_gate_guidance(self) -> None:
        # Anti-regression: the trailer must NOT fire when require_read
        # succeeds. The happy path must remain silent on stderr.
        parser = build_parser()
        args = parser.parse_args(["message", "%2", "ok body", "--no-submit"])
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.require_read"), \
            patch("mozyo_bridge.application.commands.clear_read"), \
            patch(
                "mozyo_bridge.application.commands_target_select.resolve_message_target",
                return_value="%2",
            ), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="codex"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", return_value=""), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = args.func(args)

        self.assertEqual(0, result)
        self.assertNotIn("hint: retry path:", stderr.getvalue())
        self.assertNotIn("--no-submit retry budget", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
