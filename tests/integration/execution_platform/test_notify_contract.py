from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.domain.notification import (
    build_prompt,
    landing_marker,
    validate_notify_gate,
)
import mozyo_bridge.domain.pane_resolver as pane_resolver
from mozyo_bridge.infrastructure.queue_reader import find_handoff_task

class NotificationTest(unittest.TestCase):
    def assert_exits_cleanly(self, callback) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                callback()

    def test_validate_notify_gate_requires_issue(self) -> None:
        args = argparse.Namespace(issue=None, journal="1", task_id=None)

        self.assert_exits_cleanly(lambda: validate_notify_gate(args))

    def test_validate_notify_gate_requires_journal_or_task(self) -> None:
        args = argparse.Namespace(issue="9020", journal=None, task_id=None)

        self.assert_exits_cleanly(lambda: validate_notify_gate(args))

    def test_build_prompt_uses_redmine_gate(self) -> None:
        args = argparse.Namespace(issue="9020", journal="46005", type="review_request", commit="abc123", prompt=None)

        prompt = build_prompt(args, "codex", None)

        self.assertIn("[mozyo:notify:issue=9020:journal=46005:type=review_request]", prompt)
        self.assertIn("Redmine #9020 journal #46005", prompt)
        self.assertIn("Stop-hook handoff waiting is disabled", prompt)
        self.assertEqual("[mozyo:notify:issue=9020:journal=46005:type=review_request]", landing_marker(args, None))

    def test_prompt_override_keeps_machine_landing_marker(self) -> None:
        args = argparse.Namespace(prompt="custom operator prompt", issue="9020", journal="1", type="review_request")

        self.assertEqual(
            "[mozyo:notify:issue=9020:journal=1:type=review_request] custom operator prompt",
            build_prompt(args, "codex", None),
        )
        self.assertEqual("[mozyo:notify:issue=9020:journal=1:type=review_request]", landing_marker(args, None))

    def test_build_prompt_uses_handoff_task(self) -> None:
        args = argparse.Namespace(prompt=None)
        task = {"id": "task-1", "issue_id": 9020, "commit": "abc123", "type": "review_request"}

        prompt = build_prompt(args, "codex", task)

        self.assertIn("[mozyo:notify:task=task-1:issue=9020]", prompt)
        self.assertIn("handoff task task-1 is ready for codex", prompt)
        self.assertIn("issue=#9020", prompt)
        self.assertEqual("[mozyo:notify:task=task-1:issue=9020]", landing_marker(args, task))

    def test_journal_takes_precedence_over_legacy_task(self) -> None:
        args = argparse.Namespace(
            prompt=None,
            issue="9020",
            journal="46005",
            type="review_request",
            commit="abc123",
        )
        task = {"id": "task-1", "issue_id": 9020, "commit": "abc123", "type": "review_request"}

        prompt = build_prompt(args, "codex", task)

        self.assertIn("Redmine #9020 journal #46005", prompt)
        self.assertNotIn("handoff task", prompt)
        self.assertEqual("[mozyo:notify:issue=9020:journal=46005:type=review_request]", landing_marker(args, task))


class NotifyContractTest(unittest.TestCase):
    def run_notify_with_fake_tmux(
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
                text = tmux_args[-1]
                pane_text += text
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        pane = {"id": "%2", "location": "agents:0.1", "command": "node", "cwd": "/repo", "window_name": "codex", "pane_active": "1"}

        # v0.4: the standard notify wrappers default to `--mode queue-enter`,
        # so the Layer B preflight runs. Patch `current_session_name` so the
        # Step 10 same-session binding can compare sender vs target without
        # invoking real tmux.
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value="agents"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="claude"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), pane_text

    def test_notify_by_journal_types_observed_text_then_submits(self) -> None:
        # `notify-codex` is a thin wrapper over the new handoff primitive
        # (Codex audit: `1214760803593547`). The marker and body shape come
        # from `mozyo_bridge.domain.handoff`; the legacy `[mozyo:notify:...]`
        # marker is reserved for the legacy queue subcommands. v0.4: the
        # standard notify wrappers default to `--mode queue-enter`, so the
        # Layer B preflight admits the send under the fake's codex pane.
        result, sent, stdout, pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--type",
                "review_request",
                "--target",
                "%2",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=review_request:to=codex]",
            pane_text,
        )
        self.assertIn("review request ready for codex", pane_text)
        self.assertIn("Redmine #9020 journal #46005", pane_text)
        outcome_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(outcome_lines)
        outcome = json.loads(outcome_lines[-1])
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("redmine", outcome["source"])
        self.assertEqual("review_request", outcome["kind"])
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_legacy_task_notification_uses_separate_contract(self) -> None:
        task = {"id": "legacy-task", "issue_id": 9596, "commit": "abc123", "type": "design_consultation_result"}
        with patch("mozyo_bridge.application.commands.find_handoff_task", return_value=task) as find_task:
            result, _sent, _stdout, pane_text = self.run_notify_with_fake_tmux(
                [
                    "notify-claude-legacy-task",
                    "--issue",
                    "9596",
                    "--task-id",
                    "legacy-task",
                    "--type",
                    "design_consultation_result",
                    "--target",
                    "%2",
                    "--force",
                    "--submit-delay",
                    "0",
                ]
            )

        self.assertEqual(0, result)
        find_task.assert_called_once()
        self.assertIn("[mozyo:notify:task=legacy-task:issue=9596]", pane_text)
        self.assertIn("handoff task legacy-task is ready for claude", pane_text)
        self.assertIn("legacy queue fallback", pane_text)

    def test_legacy_task_notification_does_not_emit_structured_outcome(self) -> None:
        # Regression rail for the handoff-primitive split (Asana 1214760806178471):
        # `notify-*-legacy-task` is the retired-queue cleanup wrapper and must
        # NOT route through `orchestrate_handoff`. It therefore must not emit
        # the structured JSON outcome line nor the markdown delivery record,
        # because callers of the legacy wrapper have no durable Asana / Redmine
        # anchor to anchor that record at. If a future refactor accidentally
        # unifies the legacy queue path with the standard primitive, callers
        # would start seeing structured-outcome bytes that name a stale queue
        # task as the anchor.
        task = {"id": "legacy-task", "issue_id": 9596, "commit": "abc123", "type": "design_consultation_result"}
        with patch("mozyo_bridge.application.commands.find_handoff_task", return_value=task):
            result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
                [
                    "notify-claude-legacy-task",
                    "--issue",
                    "9596",
                    "--task-id",
                    "legacy-task",
                    "--type",
                    "design_consultation_result",
                    "--target",
                    "%2",
                    "--force",
                    "--submit-delay",
                    "0",
                ]
            )

        self.assertEqual(0, result)
        outcome_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual([], outcome_lines, msg=f"legacy wrapper emitted structured outcome: {stdout!r}")
        self.assertNotIn("Delivery result —", stdout)
        self.assertNotIn("Durable anchor:", stdout)
        self.assertNotIn("`receiver`", stdout)

    def test_notify_submits_under_queue_enter_default_even_when_marker_missed(
        self,
    ) -> None:
        # v0.4 contract pivot (Asana 1214824751741628): the standard notify
        # wrappers default to `--mode queue-enter`, so marker miss must NOT
        # roll back — Enter is issued and the durable outcome is `sent` /
        # `queue_enter`. Strict-rail rollback on marker miss is still covered
        # by `RelaxedQueueEnterRailTest.test_strict_standard_still_rolls_back_on_marker_timeout`;
        # notify-* wrappers cannot opt into strict by design (no `--mode` flag
        # is exposed on them).
        result, sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--target",
                "%2",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            captures=["", "", ""],
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))
        outcome_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(outcome_lines)
        outcome = json.loads(outcome_lines[-1])
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("queue_enter", outcome["reason"])
        self.assertEqual("queue-enter", outcome["mode"])
        self.assertEqual("redmine", outcome["source"])

    def test_notify_submit_delay_default_is_classic_short_tui_delay(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["notify-codex", "--issue", "9020", "--journal", "1"])

        self.assertEqual(0.2, args.submit_delay)

    def test_standard_notify_wrapper_preserves_legacy_success_line(self) -> None:
        # Codex audit finding 1 on task 1214760547941073: the wrapper must
        # keep printing `notified <agent>: journal=... target=... read_lines=...`
        # so the in-repo smoke and external scripts that grep that line
        # continue to work after the handoff-primitive retrofit. v0.4: the
        # notify wrappers default to queue-enter; the legacy success line is
        # still printed when the Layer B preflight admits and Enter is sent.
        result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--type",
                "review_request",
                "--target",
                "%2",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn("notified codex: journal=46005 target=%2 read_lines=20", stdout)

    def test_standard_notify_wrapper_omits_success_line_on_failure(self) -> None:
        # The legacy success line is a courtesy that must only fire on real
        # success. v0.4 routes the standard notify wrappers through
        # `--mode queue-enter`, which rejects `--force` before any typing;
        # the wrapper must not have printed `notified codex: ...` before the
        # forced exit. Pre-v0.4 this test exercised the strict-rail
        # marker_timeout path; the success-line invariant is the same under
        # any wrapper failure, so we keep the regression on the new path.
        with contextlib.redirect_stderr(io.StringIO()):
            result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
                [
                    "notify-codex",
                    "--issue",
                    "9020",
                    "--journal",
                    "46005",
                    "--target",
                    "%2",
                    "--force",
                    "--landing-timeout",
                    "0.01",
                    "--submit-delay",
                    "0",
                ],
                captures=["", "", ""],
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertNotIn("notified codex:", stdout)

    def test_standard_notify_accepts_record_format_json(self) -> None:
        # Codex audit finding 2 on task 1214760547941073: the wrapper parser
        # must accept the same --record-format / --record-command knobs as
        # `handoff send/reply`, so callers using the compatibility wrapper
        # can still ask for json-only output.
        parser = build_parser()

        args = parser.parse_args(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "1",
                "--record-format",
                "json",
            ]
        )

        self.assertEqual("json", args.record_format)

    def test_standard_notify_record_format_json_suppresses_record(self) -> None:
        # End-to-end through the wrapper: --record-format json suppresses
        # the markdown block but keeps the JSON outcome and the legacy
        # success line. v0.4: notify wrappers default to queue-enter; the
        # fake fixture passes Layer B preflight so Enter is issued and the
        # legacy success line still fires.
        result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--type",
                "review_request",
                "--target",
                "%2",
                "--submit-delay",
                "0",
                "--record-format",
                "json",
            ]
        )

        self.assertEqual(0, result)
        self.assertNotIn("Delivery result —", stdout)
        self.assertIn("notified codex: journal=46005", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual(1, len(json_lines))

    def test_notify_review_wrapper_accepts_record_command(self) -> None:
        # --record-command flows through the review wrappers too (issue
        # required path). End-to-end: the record block shows the literal
        # command and the legacy success line still fires. v0.4: review
        # wrappers default to queue-enter; the fake fixture passes Layer B
        # preflight.
        result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex-review",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--target",
                "%2",
                "--submit-delay",
                "0",
                "--record-command",
                "mozyo-bridge notify-codex-review --issue 9020 --journal 46005",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn(
            "- Command: `mozyo-bridge notify-codex-review --issue 9020 --journal 46005`",
            stdout,
        )
        self.assertIn("notified codex: journal=46005", stdout)


if __name__ == "__main__":
    unittest.main()
