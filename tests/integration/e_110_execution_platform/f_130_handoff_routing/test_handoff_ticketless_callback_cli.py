"""End-to-end tests for `handoff ticketless-callback` (Redmine #12703).

Exercises the standard ticketless no-anchor callback rail through the real CLI
parser + ``orchestrate_handoff`` with a faked tmux, and pins the two boundaries
the issue requires kept: the anchored ``handoff reply`` rail still requires a
Redmine anchor, and an actual worker dispatch is not expressible on the ticketless
rail.
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


class TicketlessCallbackCliParserTest(unittest.TestCase):
    def test_parses_full_ticketless_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff", "ticketless-callback",
                "--to", "codex",
                "--target", "%0",
                "--classification", "no_dispatch",
                "--dispatch-decision", "hand_back_to_caller",
                "--workflow-next-owner", "caller",
                "--callback-reason", "no_dispatch_decided",
                "--read-contract", "grandparent_coordinator",
                "--mode", "standard",
            ]
        )
        self.assertEqual("handoff", args.command)
        self.assertEqual("ticketless-callback", args.handoff_command)
        self.assertEqual("no_dispatch", args.classification)
        self.assertEqual("hand_back_to_caller", args.dispatch_decision)
        self.assertEqual("caller", args.workflow_next_owner)
        self.assertEqual("no_dispatch_decided", args.callback_reason)
        self.assertEqual("grandparent_coordinator", args.read_contract)
        # The rail carries NO anchor flags at all.
        self.assertFalse(hasattr(args, "issue"))
        self.assertFalse(hasattr(args, "source"))

    def test_structured_fields_are_required(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["handoff", "ticketless-callback", "--to", "codex"]
                )

    def test_worker_dispatch_is_not_an_offered_choice(self) -> None:
        # The boundary: an actual worker dispatch token is rejected at the parser
        # (it is not in the choice set), so the rail cannot express it.
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "handoff", "ticketless-callback",
                        "--to", "codex",
                        "--classification", "consultation_result",
                        "--dispatch-decision", "dispatch_redmine_anchored_worker",
                        "--workflow-next-owner", "worker",
                        "--callback-reason", "consultation_classified",
                        "--read-contract", "project_gateway",
                    ]
                )


class TicketlessCallbackOrchestrationTest(unittest.TestCase):
    def _run(self, argv: list[str], allow_exit: bool = False):
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
                if tmux_args[-1] == "Enter":
                    # Redmine #13166: model a well-behaved codex receiver that
                    # starts a turn on Enter so the codex standard-rail turn-start
                    # observation confirms and the send resolves to sent/ok.
                    pane_text += "\n<codex-turn-started>"
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        pane_value = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "codex",
        }

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value=None), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()):
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), pane_text

    def _outcome_json(self, stdout: str) -> dict:
        lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome in stdout: {stdout!r}")
        return json.loads(lines[-1])

    def test_no_dispatch_callback_sends_over_standard_rail_without_anchor(self) -> None:
        result, sent, stdout, pane_text = self._run(
            [
                "handoff", "ticketless-callback",
                "--to", "codex",
                "--target", "%2",
                "--classification", "no_dispatch",
                "--dispatch-decision", "hand_back_to_caller",
                "--workflow-next-owner", "caller",
                "--callback-reason", "no_dispatch_decided",
                "--read-contract", "grandparent_coordinator",
                "--mode", "standard",
                "--submit-delay", "0",
            ]
        )
        self.assertEqual(0, result)
        marker = (
            "[mozyo:handoff:source=ticketless:classification=no_dispatch:"
            "dispatch=hand_back_to_caller:kind=reply:to=codex]"
        )
        self.assertIn(marker, pane_text)
        # Pane body must NOT tell the receiver to read a (nonexistent) ticket.
        self.assertNotIn("read it from the source-of-truth system", pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

        outcome = self._outcome_json(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("ok", outcome["reason"])
        self.assertEqual("ticketless", outcome["source"])
        self.assertEqual("reply", outcome["kind"])
        self.assertIsNone(outcome["anchor"].get("issue"))
        self.assertIsNone(outcome["anchor"].get("journal"))
        # The structured workflow result is carried distinctly from the transport.
        self.assertEqual(
            outcome["ticketless_callback"],
            {
                "classification": "no_dispatch",
                "dispatch_decision": "hand_back_to_caller",
                "next_action_owner": "caller",
                "callback_reason": "no_dispatch_decided",
                "read_contract": "grandparent_coordinator",
                "redmine_anchor_required": False,
            },
        )

    def test_anchor_required_callback_marks_next_phase_anchor_required(self) -> None:
        _result, _sent, stdout, _pane = self._run(
            [
                "handoff", "ticketless-callback",
                "--to", "codex",
                "--target", "%2",
                "--classification", "anchor_required",
                "--dispatch-decision", "anchor_required_before_worker_dispatch",
                "--workflow-next-owner", "caller",
                "--callback-reason", "anchor_required_for_worker_dispatch",
                "--read-contract", "project_gateway",
                "--mode", "standard",
                "--submit-delay", "0",
            ]
        )
        outcome = self._outcome_json(stdout)
        self.assertTrue(outcome["ticketless_callback"]["redmine_anchor_required"])

    def test_anchored_reply_still_requires_redmine_anchor(self) -> None:
        # Boundary preserved: the Redmine-governed reply rail is unchanged.
        result, sent, stdout, _pane = self._run(
            [
                "handoff", "reply",
                "--to", "codex",
                "--source", "redmine",
                "--target", "%2",
                "--mode", "standard",
            ],
            allow_exit=True,
        )
        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_json(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_anchor", outcome["reason"])


if __name__ == "__main__":
    unittest.main()
