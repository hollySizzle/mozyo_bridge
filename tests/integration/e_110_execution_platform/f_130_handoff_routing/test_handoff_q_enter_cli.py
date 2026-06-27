"""End-to-end tests for `handoff q-enter` (Redmine #12705).

Exercises the LLM-facing front door through the real CLI parser + the existing
``orchestrate_handoff`` rail with a faked tmux, and pins the boundaries the issue
requires kept: the front door owns the anchor decision (fail-closed before any pane
is touched), a consultation callback rides the #12703 ticketless no-anchor rail, an
anchored worker dispatch keeps its Redmine anchor requirement, and the transport
outcome stays separated from the front-door (workflow) result.
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


class QEnterParserTest(unittest.TestCase):
    def test_intent_is_required(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["handoff", "q-enter", "--to", "codex"])

    def test_consultation_callback_carries_no_source_requirement(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff", "q-enter",
                "--intent", "consultation_callback",
                "--to", "codex", "--target", "%2",
                "--classification", "no_dispatch",
                "--dispatch-decision", "hand_back_to_caller",
                "--workflow-next-owner", "caller",
                "--callback-reason", "no_dispatch_decided",
                "--read-contract", "grandparent_coordinator",
            ]
        )
        self.assertEqual("q-enter", args.handoff_command)
        self.assertEqual("consultation_callback", args.intent)
        self.assertIsNone(args.source)


class QEnterFrontDoorTest(unittest.TestCase):
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

    def _json_blocks(self, stdout: str) -> list[dict]:
        return [
            json.loads(ln)
            for ln in stdout.splitlines()
            if ln.strip().startswith("{")
        ]

    def _front_door(self, stdout: str) -> dict:
        for block in self._json_blocks(stdout):
            if block.get("q_enter"):
                return block
        self.fail(f"no q_enter front-door envelope in stdout: {stdout!r}")

    def _transport(self, stdout: str) -> dict:
        for block in self._json_blocks(stdout):
            if "status" in block and not block.get("q_enter"):
                return block
        self.fail(f"no transport outcome in stdout: {stdout!r}")

    def test_consultation_callback_rides_ticketless_rail_without_anchor(self) -> None:
        result, sent, stdout, pane_text = self._run(
            [
                "handoff", "q-enter",
                "--intent", "consultation_callback",
                "--to", "codex", "--target", "%2",
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

        # Front-door (workflow) result: no anchor required, ticketless rail.
        front = self._front_door(stdout)
        self.assertTrue(front["dispatched"])
        self.assertFalse(front["anchor_required"])
        self.assertEqual("ticketless_callback", front["resolved_rail"])
        self.assertTrue(front["delivery_id"].startswith("qe-"))

        # Transport outcome: rode the standard rail with no fabricated anchor, and
        # carried the structured workflow result distinctly.
        transport = self._transport(stdout)
        self.assertEqual("sent", transport["status"])
        self.assertEqual("ticketless", transport["source"])
        self.assertIsNone(transport["anchor"].get("issue"))
        self.assertEqual(
            "no_dispatch", transport["ticketless_callback"]["classification"]
        )
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_worker_dispatch_without_anchor_fails_closed_without_touching_pane(self) -> None:
        result, sent, stdout, _pane = self._run(
            [
                "handoff", "q-enter",
                "--intent", "worker_dispatch",
                "--to", "claude", "--target", "%2",
                "--source", "redmine", "--issue", "12705",
                "--kind", "implementation_request",
                "--mode", "standard",
            ]
        )
        # Fail-closed front-door decision: nonzero, NO tmux send at all.
        self.assertEqual(1, result)
        self.assertEqual([], sent)
        front = self._front_door(stdout)
        self.assertTrue(front["blocked"])
        self.assertFalse(front["dispatched"])
        self.assertEqual("anchor_required", front["blocked_reason"])
        # The next action points the LLM at the no-anchor rail.
        self.assertIn("consultation_callback", front["guidance"])

    def test_worker_dispatch_with_anchor_dispatches_over_anchored_send(self) -> None:
        result, sent, stdout, pane_text = self._run(
            [
                "handoff", "q-enter",
                "--intent", "worker_dispatch",
                "--to", "claude", "--target", "%2",
                "--source", "redmine", "--issue", "12705", "--journal", "67162",
                "--kind", "implementation_request",
                "--mode", "standard",
                "--submit-delay", "0",
            ]
        )
        self.assertEqual(0, result)
        front = self._front_door(stdout)
        self.assertTrue(front["dispatched"])
        self.assertTrue(front["anchor_required"])
        self.assertEqual("anchored_send", front["resolved_rail"])

        transport = self._transport(stdout)
        self.assertEqual("sent", transport["status"])
        self.assertEqual("redmine", transport["source"])
        self.assertEqual("12705", transport["anchor"]["issue"])
        self.assertEqual("implementation_request", transport["kind"])
        # The anchored marker actually landed in the pane.
        self.assertIn("source=redmine", pane_text)

    def test_dispatched_record_carries_residue_and_delivery_id(self) -> None:
        _result, _sent, stdout, _pane = self._run(
            [
                "handoff", "q-enter",
                "--intent", "consultation_callback",
                "--to", "codex", "--target", "%2",
                "--classification", "no_dispatch",
                "--dispatch-decision", "hand_back_to_caller",
                "--workflow-next-owner", "caller",
                "--callback-reason", "no_dispatch_decided",
                "--read-contract", "grandparent_coordinator",
                "--mode", "standard",
                "--submit-delay", "0",
            ]
        )
        front = self._front_door(stdout)
        # The transport delivery record (text, in stdout) carries the front-door
        # `- Submit:` telemetry with the same delivery id and the residue class.
        self.assertIn("Submit (q-enter front door)", stdout)
        self.assertIn(front["delivery_id"], stdout)
        self.assertIn("Composer residue:", stdout)
        self.assertIn("cleared", stdout)


if __name__ == "__main__":
    unittest.main()
