"""End-to-end tests for the forward ticketless no-anchor consultation rail (#12740).

Exercises ``orchestrate_handoff(ticketless=True, ticketless_consultation=True)``
through a faked tmux — the delivery rail the new ``project-gateway consult`` command
delegates to — and pins the boundaries the issue requires:

- the consultation is delivered WITHOUT a Redmine anchor and without fabricating
  one (no issue/journal in the outcome, source ``ticketless``);
- the forward consultation payload is carried distinctly from the transport
  outcome and from the return-leg ``ticketless_callback`` field;
- an under-specified / unknown forward consultation fails closed (blocked /
  ``invalid_args``), so a worker dispatch cannot be smuggled onto this rail.
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

from mozyo_bridge.application import commands
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    CALLBACK_METHODS,
    CONSULTATION_PROJECT_DOMAIN,
)


class TicketlessConsultationOrchestrationTest(unittest.TestCase):
    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            to="codex",
            target="%2",
            mode="standard",
            submit_delay=0,
            record_format="both",
            # The forward consultation payload, as `project-gateway consult` injects it.
            consultation_kind=CONSULTATION_PROJECT_DOMAIN,
            callback_to_role=ROLE_GRANDPARENT_COORDINATOR,
            callback_methods=list(CALLBACK_METHODS),
            read_contract=ROLE_PROJECT_GATEWAY,
            transition_role=ROLE_GRANDPARENT_COORDINATOR,
            workflow_contract=ROLE_GRANDPARENT_COORDINATOR,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, args: argparse.Namespace, allow_exit: bool = False):
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
                result = commands.orchestrate_handoff(
                    args,
                    default_kind="design_consultation",
                    ticketless=True,
                    ticketless_consultation=True,
                )
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), pane_text

    def _outcome_json(self, stdout: str) -> dict:
        lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome in stdout: {stdout!r}")
        return json.loads(lines[-1])

    def test_forward_consultation_sends_over_standard_rail_without_anchor(self) -> None:
        result, sent, stdout, pane_text = self._run(self._args())
        self.assertEqual(0, result)
        marker = (
            "[mozyo:handoff:source=ticketless:"
            "consultation=project_domain_consultation:"
            "callback_to=grandparent_coordinator:kind=design_consultation:to=codex]"
        )
        self.assertIn(marker, pane_text)
        # Pane body names the forward consultation, not a ticket to read, and not
        # the return callback leg.
        self.assertIn("ticketless no-anchor consultation", pane_text)
        self.assertNotIn("read it from the source-of-truth system", pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

        outcome = self._outcome_json(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("ok", outcome["reason"])
        self.assertEqual("ticketless", outcome["source"])
        self.assertEqual("design_consultation", outcome["kind"])
        # No Redmine issue/journal anchor was fabricated.
        self.assertIsNone(outcome["anchor"].get("issue"))
        self.assertIsNone(outcome["anchor"].get("journal"))
        # The forward consultation payload is carried distinctly from the transport,
        # and the return-leg callback field stays empty.
        self.assertEqual(
            outcome["ticketless_consultation"],
            {
                "consultation_kind": "project_domain_consultation",
                "callback_to_role": "grandparent_coordinator",
                "callback_methods": [
                    "ticketless_callback",
                    "q_enter_consultation_callback",
                ],
                "read_contract": "project_gateway",
                "worker_dispatch_requires_anchor": True,
            },
        )
        self.assertIsNone(outcome["ticketless_callback"])

    def test_unknown_consultation_kind_fails_closed_no_send(self) -> None:
        # A bogus forward consultation (e.g. an attempt to smuggle a worker
        # dispatch class) fails closed before anything is typed.
        result, sent, stdout, _pane = self._run(
            self._args(consultation_kind="dispatch_worker"), allow_exit=True
        )
        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_json(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])


if __name__ == "__main__":
    unittest.main()
