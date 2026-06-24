from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.domain.pane_resolver import (
    ensure_agent_target,
    resolve_target,
)
import mozyo_bridge.domain.pane_resolver as pane_resolver
from mozyo_bridge.domain.handoff import (
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
    build_delivery_record,
    make_outcome,
    next_action_for,
    normalize_anchor,
    project_last_input,
)

class HandoffCliParserTest(unittest.TestCase):
    def test_handoff_send_requires_kind(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "asana",
                        "--task-id",
                        "T1",
                        "--comment-id",
                        "C1",
                    ]
                )

    def test_handoff_send_parses_full_args(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--mode",
                "standard",
            ]
        )

        self.assertEqual("handoff", args.command)
        self.assertEqual("send", args.handoff_command)
        self.assertEqual("claude", args.to)
        self.assertEqual("asana", args.source)
        self.assertEqual("implementation_request", args.kind)
        self.assertEqual("T1", args.task_id)
        self.assertEqual("C1", args.comment_id)
        self.assertEqual(MODE_STANDARD, args.mode)

    def test_handoff_reply_allows_omitted_kind(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "handoff",
                "reply",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "9020",
                "--journal",
                "46005",
            ]
        )

        self.assertEqual("handoff", args.command)
        self.assertEqual("reply", args.handoff_command)
        self.assertIsNone(args.kind)

    def test_reply_alias_shares_handoff_reply_func(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "reply",
                "--to",
                "claude",
                "--source",
                "asana",
                "--task-id",
                "T1",
                "--anchor-url",
                "https://example/x",
                "--mode",
                "pending",
            ]
        )

        self.assertEqual("reply", args.command)
        self.assertEqual("https://example/x", args.anchor_url)
        self.assertEqual(MODE_PENDING, args.mode)
        from mozyo_bridge.application.commands import cmd_handoff_reply

        self.assertIs(cmd_handoff_reply, args.func)

    def test_handoff_send_rejects_unknown_source(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "jira",
                        "--kind",
                        "reply",
                    ]
                )

    def test_handoff_send_rejects_unknown_kind(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "asana",
                        "--kind",
                        "ship_it",
                    ]
                )


class HandoffOrchestratorTest(unittest.TestCase):
    def run_handoff_with_fake_tmux(
        self,
        argv: list[str],
        captures: list[str] | None = None,
        allow_exit: bool = False,
        pane: dict[str, str] | None = None,
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
        }
        pane_value = pane if pane is not None else default_pane

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=None,
            ), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
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

    def test_standard_mode_sends_marker_body_and_enter(self) -> None:
        # Strict `--mode standard` happy path: marker observed → Enter pressed,
        # outcome `sent` / `ok` / mode=`standard`. v0.4 default is queue-enter,
        # so this test exercises the explicit strict fallback rail.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        expected_marker = "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]"
        self.assertIn(expected_marker, pane_text)
        self.assertIn("Asana task T1", pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("ok", outcome["reason"])
        self.assertEqual("claude", outcome["receiver"])
        self.assertEqual("%2", outcome["target"])
        self.assertEqual("asana", outcome["source"])
        self.assertEqual("implementation_request", outcome["kind"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])
        self.assertEqual(expected_marker, outcome["notification_marker"])
        self.assertEqual("receiver", outcome["next_action_owner"])

    def test_pending_mode_leaves_input_unsubmitted_and_emits_pending_outcome(self) -> None:
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "reply",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--target",
                "%2",
                "--force",
                "--mode",
                "pending",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
            },
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=reply:to=codex]",
            pane_text,
        )
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("pending_input", outcome["status"])
        self.assertEqual("reply", outcome["kind"])
        self.assertEqual("operator", outcome["next_action_owner"])

    def test_marker_timeout_rolls_back_and_emits_blocked_outcome(self) -> None:
        # Strict `--mode standard` fail-closed regression: marker miss must
        # roll back via `C-u` and emit `blocked` / `marker_timeout`. v0.4
        # default (queue-enter) deliberately does NOT roll back; that contract
        # is covered separately.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_result",
                "--task-id",
                "T1",
                "--anchor-url",
                "https://example/x",
                "--target",
                "%2",
                "--mode",
                "standard",
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

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])
        self.assertEqual("sender", outcome["next_action_owner"])

        # Asana task 1214779823377861: the rollback path must emit the
        # `--no-submit` fallback hint on stderr so agents do not jump to the
        # preset's `Notification fails` branch after a single transient
        # marker_timeout. Names the receiver and the per-preset cap so the
        # budget is unambiguous and not borrowed from the `handoff send`
        # retry pool.
        self.assertIn("hint: fallback path:", stderr)
        self.assertIn("mozyo-bridge read claude", stderr)
        self.assertIn("mozyo-bridge message claude", stderr)
        self.assertIn("--no-submit", stderr)
        self.assertIn("3", stderr)
        self.assertIn("separate budgets", stderr)
        self.assertIn("next-action verb", stderr)

    def test_invalid_anchor_emits_blocked_invalid_anchor_outcome(self) -> None:
        # Anchor normalization fires before rail-specific preflight, so this
        # test holds for both rails. Pinned to `--mode standard` so the v0.4
        # queue-enter force-rejection cannot eclipse the invalid_anchor exit.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--target",
                "%2",
                "--mode",
                "standard",
            ],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_anchor", outcome["reason"])
        self.assertIsNone(outcome["target"])
        # Source must survive anchor-normalization failure so task
        # 1214760547941073 can persist the outcome without re-deriving it.
        self.assertEqual("asana", outcome["source"])
        self.assertIn("asana anchor", stderr)

    def test_non_agent_pane_without_force_emits_target_not_agent(self) -> None:
        # Strict-rail agent gate (`ensure_agent_target`) must still reject a
        # non-agent foreground process when `--force` is absent. Pinned to
        # `--mode standard` because queue-enter's Layer B preflight rejects on
        # `target_not_agent` via a different code path (Step 12) and would
        # surface a different `Reason` ordering.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "zsh",
                "cwd": "/repo",
                "window_name": "claude",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual("%2", outcome["target"])
        self.assertIn("target pane does not look like an agent pane", stderr)

    def test_target_unavailable_emits_blocked_target_unavailable(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
            ]
        )

        # No agent window in the session, no explicit --target. resolve_target
        # should die; the orchestrator must emit a structured outcome first.
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value="my-project"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                args.func(args)

        out = stdout.getvalue()
        outcome_lines = [line for line in out.splitlines() if line.strip().startswith("{")]
        self.assertTrue(outcome_lines, f"no JSON outcome found in stdout: {out!r}")
        outcome = json.loads(outcome_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_unavailable", outcome["reason"])
        self.assertIsNone(outcome["target"])
        self.assertIn("no claude window found", stderr.getvalue())


class RelaxedQueueEnterRailTest(unittest.TestCase):
    """Coverage for the relaxed `queue-enter` rail.

    Implements the v0.2 contract section `## Relaxed Queue-Enter Rail` in
    ``vibes/docs/logics/tmux-send-safety-contract.md``. Strict `--mode standard`
    behavior must remain unchanged (covered by ``HandoffOrchestratorTest``);
    these tests focus on what the new rail adds and what it deliberately
    refuses to do.
    """

    def run_handoff_with_fake_tmux(
        self,
        argv,
        captures=None,
        allow_exit: bool = False,
        pane=None,
        current_session: str | None = "agents",
    ):
        # Mirror of HandoffOrchestratorTest.run_handoff_with_fake_tmux so this
        # class can drive the CLI end-to-end without launching tmux.
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

        # Step 10 (v0.3 deterministic preflight) reads the sender's tmux
        # session via `current_session_name`. Default to "agents" so the
        # default pane's location prefix (`agents:0.1`) matches; individual
        # tests can override by patching the same symbol inside the body.
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value=current_session), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
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

    # --- mode parsing / validation -------------------------------------------------

    def test_cli_accepts_mode_queue_enter(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--mode",
                "queue-enter",
            ]
        )

        self.assertEqual(MODE_QUEUE_ENTER, args.mode)

    def test_cli_default_mode_is_queue_enter_since_v0_4(self) -> None:
        # v0.4 contract pivot (Asana 1214824751741628) flipped the CLI default
        # for agent-pane handoff to queue-enter. Strict `--mode standard`
        # remains explicitly selectable; its regression coverage lives in
        # `test_strict_standard_still_rolls_back_on_marker_timeout` and
        # `test_strict_standard_admits_cross_receiver_process_unchanged`.
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
            ]
        )

        self.assertEqual(MODE_QUEUE_ENTER, args.mode)

    def test_cli_rejects_unknown_mode(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "asana",
                        "--kind",
                        "reply",
                        "--task-id",
                        "T1",
                        "--comment-id",
                        "C1",
                        "--mode",
                        "relaxed",
                    ]
                )

    # --- queue-enter behavior split ------------------------------------------------

    def test_queue_enter_observed_marker_emits_sent_ok_with_queue_enter_mode(self) -> None:
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        expected_marker = "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]"
        self.assertIn(expected_marker, pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        # Marker was observed (default capture returns pane_text), so no
        # rollback occurred.
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("ok", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual("receiver", outcome["next_action_owner"])

    def test_queue_enter_unobserved_marker_emits_sent_queue_enter_without_rollback(self) -> None:
        # Force capture to return empty so wait_for_text returns False.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            captures=["", "", ""],
        )

        self.assertEqual(0, result)
        # Body was typed.
        self.assertIn(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=review_request:to=claude]",
            pane_text,
        )
        # Enter WAS pressed (the rail's whole point).
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        # No rollback.
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("queue_enter", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        # Per the contract, next_action_owner stays receiver-owned even when
        # the marker was unobserved.
        self.assertEqual("receiver", outcome["next_action_owner"])
        self.assertIn("durable anchor", outcome["next_action"])

    def test_strict_standard_still_rolls_back_on_marker_timeout(self) -> None:
        # Regression: the v0.4 default flip to queue-enter must not weaken
        # strict `standard`. Strict is now an explicit fallback (`--mode
        # standard`); its fail-closed `C-u` rollback on marker_timeout stays
        # exactly as it was in v0.1.
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_result",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
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
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])

    # --- agent-target restriction --------------------------------------------------

    def test_queue_enter_rejects_force_flag(self) -> None:
        # Per contract, `--force` cannot bypass agent-gate under queue-enter.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--force",
            ],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        # No typing should have occurred.
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("--force", stderr)
        self.assertIn("queue-enter", stderr)

    def test_queue_enter_rejects_explicit_target_in_other_receiver_window(self) -> None:
        # Per Codex audit finding on task 1214782240686275 comment 1214783754107198:
        # `ensure_agent_target` only checks that the pane is running *some*
        # agent-looking process (claude/codex/node), not that the pane belongs
        # to the intended receiver. Under strict, marker_timeout rollback caps
        # the blast radius. Under queue-enter, marker miss does NOT roll back,
        # so an explicit `--target %X` in the wrong receiver's window would
        # silently press Enter into the wrong agent. The queue-enter preflight
        # must reject this mismatch before typing.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
            ],
            pane={
                "id": "%2",
                "location": "agents:1.0",
                "command": "node",
                "cwd": "/repo",
                # Pane is a real agent process, but lives in the codex window.
                # Strict (`ensure_agent_target`) would currently accept this;
                # queue-enter must not.
                "window_name": "codex",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        # No typing or Enter against the mismatched pane.
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        # Target id should still survive the outcome (typing was not done but
        # the pane resolved); helps audit which mismatched pane was rejected.
        self.assertEqual("%2", outcome["target"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("--target", stderr)
        self.assertIn("'codex'", stderr)
        self.assertIn("'claude'", stderr)

    def test_queue_enter_allows_explicit_target_in_matching_receiver_window(self) -> None:
        # Sanity check that the new preflight does not over-fire: an explicit
        # --target in the receiver's own window must still be accepted.
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--submit-delay",
                "0",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
        )

        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_queue_enter_blocks_non_agent_pane(self) -> None:
        # Non-agent process in a claude window: the standard agent-gate fires
        # and emits target_not_agent. Under queue-enter this stays blocked
        # (and `--force` is not even available to override).
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "zsh",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_allows_cockpit_pane_via_role_option(self) -> None:
        # Redmine #11822: a cockpit pane lives in window `cockpit` but carries
        # its role on `@mozyo_agent_role`. The role-aware receiver binding must
        # accept it under queue-enter WITHOUT `--force` (the prior window-name
        # gate forced `--mode standard --force`).
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff", "send", "--to", "claude", "--source", "asana",
                "--kind", "reply", "--task-id", "T1", "--comment-id", "C1",
                "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "cockpit",
                "pane_active": "1",
                "agent_role": "claude",
            },
        )
        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_queue_enter_rejects_cockpit_pane_with_mismatched_role_option(self) -> None:
        # Cockpit pane explicitly marked `codex` must not accept a `--to claude`
        # queue-enter send: role resolves to codex, fail-closed.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff", "send", "--to", "claude", "--source", "asana",
                "--kind", "implementation_request", "--task-id", "T1",
                "--comment-id", "C1", "--target", "%2", "--mode", "queue-enter",
            ],
            pane={
                "id": "%2",
                "location": "agents:1.0",
                "command": "node",
                "cwd": "/repo",
                "window_name": "cockpit",
                "pane_active": "1",
                "agent_role": "codex",
            },
            allow_exit=True,
        )
        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_allows_cockpit_pane_despite_layout_window_name(self) -> None:
        # Redmine #11822 audit regression (journal #57116): the live cockpit was
        # observed with a Claude-role pane (`@mozyo_agent_role=claude`) in a
        # window named `codex` (tmux layout / auto-naming). The explicit marker
        # is authoritative, so a `--to claude` queue-enter send must be ALLOWED
        # (no `--force`) — the layout window name is not a conflicting signal.
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff", "send", "--to", "claude", "--source", "asana",
                "--kind", "reply", "--task-id", "T1", "--comment-id", "C1",
                "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
            ],
            pane={
                "id": "%2",
                "location": "agents:1.0",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
                "agent_role": "claude",
            },
        )
        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    # --- v0.3 deterministic preflight (Step 10 / 11 / 12) -------------------------

    def _queue_enter_argv(self, *, kind: str = "implementation_request") -> list[str]:
        return [
            "handoff",
            "send",
            "--to",
            "claude",
            "--source",
            "asana",
            "--kind",
            kind,
            "--task-id",
            "T1",
            "--comment-id",
            "C1",
            "--target",
            "%2",
            "--mode",
            "queue-enter",
        ]

    def test_queue_enter_step10_rejects_foreign_session_target(self) -> None:
        # Step 10 (v0.3): same-session binding. An explicit `--target %X` whose
        # pane lives in a different tmux session than the sender must be
        # rejected before typing — under queue-enter marker miss does not roll
        # back, so cross-session delivery could otherwise land in the wrong
        # repo's agent.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
            pane={
                "id": "%2",
                "location": "other:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            current_session="agents",
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'agents'", stderr)
        self.assertIn("'other'", stderr)

    def test_queue_enter_step10_rejects_when_sender_outside_tmux(self) -> None:
        # Step 10 (v0.3): when invoked outside tmux, `current_session_name`
        # returns None. queue-enter must refuse rather than admit a comparison
        # against a missing sender session.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
            current_session=None,
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'<unset>'", stderr)

    # --- Step 10 constrained cross-session admission (Redmine #11301) ---------------

    def _cross_session_codex_argv(self, target_repo, extra=None):
        # `--to codex` is the cross-session gateway receiver; queue-enter
        # cross-session admission requires an explicit pane and --target-repo.
        argv = [
            "handoff",
            "send",
            "--to",
            "codex",
            "--source",
            "asana",
            "--kind",
            "reply",
            "--task-id",
            "T1",
            "--comment-id",
            "C1",
            "--target",
            "%2",
            "--mode",
            "queue-enter",
            "--submit-delay",
            "0",
        ]
        if target_repo is not None:
            argv += ["--target-repo", str(target_repo)]
        if extra:
            argv += extra
        return argv

    def test_queue_enter_cross_session_admitted_with_explicit_target_and_repo(
        self,
    ) -> None:
        # The constrained cross-session rail: an explicit pane in a foreign
        # session whose cwd resolves under the asserted scaffolded workspace
        # (`.mozyo-bridge/scaffold.json`) and passes the --target-repo gate is
        # admitted under queue-enter — no manual --mode standard fallback.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
                self._cross_session_codex_argv(workspace),
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "node",
                    "cwd": str(workspace / "src"),
                    "window_name": "codex",
                    "pane_active": "1",
                },
                current_session="agents",
            )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=reply:to=codex]",
            pane_text,
        )
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_cross_session_blocked_without_target_repo(self) -> None:
        # Cross-session without the identity gate stays fail-closed: even the
        # gateway receiver (codex) with an explicit pane must assert
        # --target-repo to leave the same-session rail.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._cross_session_codex_argv(None),
            pane={
                "id": "%2",
                "location": "other:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
            current_session="agents",
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("target_repo=unset", stderr)

    def test_queue_enter_cross_session_blocked_on_target_repo_mismatch(self) -> None:
        # Cross-session admission at Step 10 only opens the door; the
        # --target-repo gate still fails closed when the target pane's inferred
        # workspace root differs from the asserted one.
        with tempfile.TemporaryDirectory() as tmp_str:
            expected = Path(tmp_str) / "人形使い"
            (expected / ".mozyo-bridge").mkdir(parents=True)
            (expected / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            other = Path(tmp_str) / "other_repo"
            (other / "src").mkdir(parents=True)
            (other / "pyproject.toml").write_text("", encoding="utf-8")

            result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
                self._cross_session_codex_argv(expected),
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "node",
                    "cwd": str(other / "src"),
                    "window_name": "codex",
                    "pane_active": "1",
                },
                current_session="agents",
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_repo_mismatch", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_cross_session_non_agent_target_still_blocks(self) -> None:
        # The admitted cross-session rail does not bypass Step 12: a foreground
        # process that is not agent-compatible is still rejected before typing,
        # even with a passing --target-repo identity gate.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
                self._cross_session_codex_argv(workspace),
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "vim",
                    "cwd": str(workspace / "src"),
                    "window_name": "codex",
                    "pane_active": "1",
                },
                current_session="agents",
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_cross_session_to_claude_still_routes_through_gateway(
        self,
    ) -> None:
        # Cross-session admission must not let `--to claude` deliver directly
        # into a foreign workspace's Claude pane. Step 10 admits (explicit
        # target + --target-repo), but the cross-session Claude gate then fails
        # closed and points back to the codex-gateway path.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            argv = self._cross_session_codex_argv(workspace)
            argv[argv.index("codex")] = "claude"
            result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
                argv,
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "claude",
                    "cwd": str(workspace / "src"),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                current_session="agents",
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("cross_session_claude", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_same_session_still_admits_on_current_rail(self) -> None:
        # Regression: the constrained cross-session admission must not change
        # the same-session default. A same-session codex pane is still admitted
        # without requiring --target-repo.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            self._cross_session_codex_argv(None),
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
            current_session="agents",
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_step11_rejects_inactive_pane(self) -> None:
        # Step 11 (v0.3): the target pane must be the active split of its
        # window. An inactive split would still accept keystrokes typed via
        # `send-keys -t %X`, but the receiver agent is by construction not the
        # foreground process the operator is looking at; queue-enter rejects
        # before typing.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
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
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("pane_active", stderr)

    def test_queue_enter_step12_rejects_cross_receiver_literal_to_claude(self) -> None:
        # Step 12 (v0.3) strong identity: a literal `codex` process foregrounded
        # in a `claude` window cannot satisfy the per-receiver allowlist for
        # `claude`. Step 9 already enforces `window_name == receiver` for
        # explicit `--target`; this guards the case where the window itself was
        # renamed but the foreground process betrays a different receiver.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "codex",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'codex'", stderr)
        self.assertIn("claude agent", stderr)

    def test_queue_enter_step12_rejects_cross_receiver_literal_to_codex(self) -> None:
        # Step 12 strong identity, symmetric case: a literal `claude` process
        # in a `codex` window is rejected for receiver=`codex`. (Literal
        # `node` is weak identity for both receivers because both Claude
        # Code and the Codex CLI are Node-based, so `node` does NOT exhibit
        # cross-binding rejection here; this test exercises the *strong*
        # branch only.)
        argv = self._queue_enter_argv(kind="reply")
        argv[argv.index("claude")] = "codex"
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            argv,
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "claude",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'claude'", stderr)
        self.assertIn("codex agent", stderr)

    def test_queue_enter_step12_admits_node_for_codex_weak_identity(self) -> None:
        # Both Claude Code and the Codex CLI surface as `node` in tmux
        # (Node-based runtimes). Step 12 admits `node` for receiver=`codex`
        # under the weak-identity branch — Step 9 (`window_name == receiver`)
        # plus Layer A operator discipline carry cross-binding protection
        # here. This test pins admission to keep real codex panes deliverable
        # under queue-enter.
        argv = self._queue_enter_argv(kind="reply") + ["--submit-delay", "0"]
        argv[argv.index("claude")] = "codex"
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            argv,
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=reply:to=codex]",
            pane_text,
        )
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_step12_admits_versioned_native_binary_weak_identity(self) -> None:
        # Step 12 weak identity (Open Question 8 in the contract): a versioned
        # native binary basename (e.g. `1.0.32-arm64`) is receiver-agnostic by
        # design. queue-enter admits it because the pane is at least running
        # *some* versioned native agent binary, and Step 9 + Layer A operator
        # discipline carry cross-binding protection in this branch. The
        # contract explicitly concedes the weakness; do not pretend Step 12
        # confirms receiver identity here.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv() + ["--submit-delay", "0"],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "1.0.32-arm64",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]",
            pane_text,
        )
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_strict_standard_admits_cross_receiver_process_unchanged(self) -> None:
        # Regression: neither the v0.3 per-receiver process gate nor the v0.4
        # default flip to queue-enter must bleed into strict `--mode standard`.
        # Strict's behavior (admit any agent-looking process, rely on
        # marker_timeout + C-u rollback) stays as-is. This test pins that
        # boundary by sending strict to a pane whose foreground process is
        # `claude` while --to=codex; strict admits typing and rolls back on
        # marker miss, as it already did pre-v0.3.
        argv = [
            "handoff",
            "send",
            "--to",
            "codex",
            "--source",
            "asana",
            "--kind",
            "review_result",
            "--task-id",
            "T1",
            "--comment-id",
            "C1",
            "--target",
            "%2",
            "--mode",
            "standard",
            "--landing-timeout",
            "0.01",
            "--submit-delay",
            "0",
        ]
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            argv,
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "claude",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
            captures=["", "", ""],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        # Strict still types and then rolls back; the new v0.3 gates must not
        # have fired here.
        self.assertTrue(any(call[:4] == ("send-keys", "-t", "%2", "-l") for call in sent))
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])

    # --- projection / wording ------------------------------------------------------

    def test_project_last_input_for_queue_enter_matches_strict_sent_ok(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome_strict = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker="[m]",
        )
        outcome_queue_enter = make_outcome(
            status="sent",
            reason="queue_enter",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="reply",
            notification_marker="[m]",
        )

        proj_strict = project_last_input(
            outcome_strict, submitted_at="2026-05-13T17:30:00Z"
        )
        proj_queue = project_last_input(
            outcome_queue_enter, submitted_at="2026-05-13T17:30:00Z"
        )

        # Per contract, queue-enter projection MUST equal strict sent/ok
        # projection. Returning ack_status="unobserved" or submitted_at=None
        # would violate the upstream inspector contract derive rule.
        self.assertEqual(proj_strict, proj_queue)
        assert proj_queue is not None
        self.assertEqual("submitted", proj_queue.ack_status)
        self.assertEqual("2026-05-13T17:30:00Z", proj_queue.submitted_at)
        self.assertIsNone(proj_queue.acknowledged_at)

    def test_project_last_input_for_queue_enter_mode_with_ok_reason_also_matches(self) -> None:
        # Marker-observed queue-enter (status=sent, reason=ok, mode=queue-enter)
        # must also project identically to strict sent/ok.
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="reply",
            notification_marker="[m]",
        )
        projection = project_last_input(outcome, submitted_at="2026-05-13T17:30:00Z")
        assert projection is not None
        self.assertEqual("submitted", projection.ack_status)
        self.assertEqual("2026-05-13T17:30:00Z", projection.submitted_at)

    def test_make_outcome_for_queue_enter_keeps_receiver_owned_next_action(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="queue_enter",
            receiver="codex",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="implementation_request",
            notification_marker="[m]",
        )

        owner, action = next_action_for(outcome.status, outcome.reason, outcome.receiver)

        self.assertEqual("receiver", owner)
        self.assertEqual("receiver", outcome.next_action_owner)
        self.assertIn("durable anchor", action)
        self.assertIn("durable anchor", outcome.next_action)

    def test_delivery_record_for_queue_enter_unobserved_includes_operator_note(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="queue_enter",
            receiver="codex",
            target="%111",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="review_request",
            notification_marker="[mozyo:handoff:source=asana:task=T1:comment=C1:kind=review_request:to=codex]",
        )

        record = build_delivery_record(outcome)

        self.assertIn(
            "Delivery result — sent (queue-enter, marker unobserved)", record
        )
        self.assertIn("Mode: `queue-enter`", record)
        self.assertIn("Status: `sent` (reason: `queue_enter`)", record)
        # Receiver-side primary contract is identical to strict sent.
        self.assertIn("Next action owner: `receiver`", record)
        self.assertIn("Receiver-side contract", record)
        self.assertIn("durable anchor", record)
        # Operator note is the only place the queue-enter fallback is surfaced.
        self.assertIn("Operator note", record)
        self.assertIn("--mode standard", record)
        self.assertIn("not observed before Enter", record)

    def test_delivery_record_for_queue_enter_observed_marks_rail_but_no_operator_note(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="implementation_request",
            notification_marker="[m]",
        )

        record = build_delivery_record(outcome)

        self.assertIn(
            "Delivery result — sent (queue-enter, marker observed)", record
        )
        self.assertIn("Mode: `queue-enter`", record)
        self.assertIn("Next action owner: `receiver`", record)
        # No operator escalation note when the marker was actually observed.
        self.assertNotIn("Operator note", record)


if __name__ == "__main__":
    unittest.main()
