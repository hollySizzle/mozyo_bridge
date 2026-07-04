"""CLI/integration regression for the #12918 gateway-route enforcement gate.

Pins the wired ``mozyo-bridge handoff send`` behavior end to end: the gate added
to ``orchestrate_handoff`` keys on the lane Unit (``@mozyo_lane_id``) of the sender
pane (resolved from the live inventory via ``TMUX_PANE``) and the resolved target,
so the recorded failure mode — a coordinator dispatching an
``implementation_request`` / ``review_result`` *directly* to a sublane Claude
worker in a different lane (#12670 j#68733) — fails closed before any text is
typed, while a same-lane gateway -> worker dispatch and the Codex-gateway route are
untouched. An explicit ``--allow-direct-worker`` durable exception releases the
block and is recorded distinctly.

All hermetic: ``pane_lines`` / ``current_session_name`` / the typing seams are
patched and the sender pane is the patched ``TMUX_PANE``. No live tmux is required.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
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
from . import (  # noqa: E402,F401
    setUpModule,
    tearDownModule,
)

SESSION = "mozyo-cockpit"


def _pane(
    pane_id,
    location,
    *,
    agent_role="",
    workspace_id="",
    lane_id="",
    window_name="cockpit",
    command="node",
    cwd="/repo",
    pane_active="1",
):
    return {
        "id": pane_id,
        "location": location,
        "command": command,
        "cwd": cwd,
        "window_name": window_name,
        "pane_active": pane_active,
        "agent_role": agent_role,
        "workspace_id": workspace_id,
        "lane_id": lane_id,
        "lane_label": "",
    }


# The coordinator pane: the command sender (TMUX_PANE). It lives in the cockpit
# main/coordinator lane, distinct from any sublane.
COORDINATOR = _pane(
    "%500",
    f"{SESSION}:0.0",
    agent_role="codex",
    workspace_id="ws-a",
    lane_id="lane-coordinator",
    command="codex",
    cwd="/ws/a",
)
# A sublane Claude worker in a DIFFERENT lane than the coordinator: the pane a
# direct coordinator-to-worker governed delivery must NOT reach.
SUBLANE_WORKER = _pane(
    "%600",
    f"{SESSION}:0.6",
    agent_role="claude",
    workspace_id="ws-a",
    lane_id="lane-sub-12642",
    command="claude",
    cwd="/ws/a/sub",
)
# A sublane Codex gateway in that same sublane: the governed receiver for the lane.
SUBLANE_GATEWAY = _pane(
    "%601",
    f"{SESSION}:0.7",
    agent_role="codex",
    workspace_id="ws-a",
    lane_id="lane-sub-12642",
    command="codex",
    cwd="/ws/a/sub",
)


class GatewayRouteEnforcementCliTest(unittest.TestCase):
    def _run_send(
        self,
        *,
        panes,
        target,
        receiver="claude",
        kind="implementation_request",
        sender_pane_id="%500",
        mode="pending",
        allow_direct_worker=False,
    ):
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        argv = [
            "handoff", "send", "--to", receiver,
            "--source", "redmine", "--issue", "12918", "--journal", "69368",
            "--kind", kind,
            "--target", target,
            "--mode", mode,
            "--landing-timeout", "0.01", "--submit-delay", "0",
            "--summary", "gateway-route fixture",
        ]
        if allow_direct_worker:
            argv.append("--allow-direct-worker")
        args = build_parser().parse_args(argv)

        sent: list[tuple] = []

        def fake_run_tmux(*a, check: bool = True):
            if a[:1] == ("send-keys",):
                sent.append(a)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        if sender_pane_id is not None:
            env["TMUX_PANE"] = sender_pane_id

        pr = "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver"
        with patch.object(commands, "require_tmux"), \
            patch.object(commands, "capture_pane", return_value=""), \
            patch.object(commands, "run_tmux", side_effect=fake_run_tmux), \
            patch.object(commands, "wait_for_text", return_value=True), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch.object(commands, "current_session_name", return_value=SESSION), \
            patch(f"{pr}.current_session_name", return_value=SESSION), \
            patch(f"{pr}.validate_target"), \
            patch(f"{pr}.pane_lines", return_value=panes), \
            patch.dict(os.environ, env, clear=True), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            with contextlib.suppress(SystemExit):
                args.func(args)
        return self._outcome_from(out.getvalue()), out.getvalue(), err.getvalue(), sent

    @staticmethod
    def _outcome_from(stdout: str):
        outcome = None
        for line in stdout.splitlines():
            if line.strip().startswith("{"):
                with contextlib.suppress(ValueError):
                    outcome = json.loads(line)
        return outcome

    # --- the recorded failure mode fails closed ----------------------------

    def test_coordinator_direct_to_sublane_worker_blocks_before_typing(self) -> None:
        outcome, _out, err, sent = self._run_send(
            panes=[COORDINATOR, SUBLANE_WORKER, SUBLANE_GATEWAY],
            target="%600",
        )
        self.assertIsNotNone(outcome)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("gateway_route_blocked", outcome["reason"])
        self.assertEqual("claude", outcome["receiver"])
        # Fails closed BEFORE any text is typed at the worker pane.
        self.assertEqual([], sent)
        # The operator is pointed at the governed gateway route and the exception.
        self.assertIn("Codex gateway", err)
        self.assertIn("--allow-direct-worker", err)

    def test_review_result_direct_to_sublane_worker_also_blocks(self) -> None:
        outcome, _out, _err, sent = self._run_send(
            panes=[COORDINATOR, SUBLANE_WORKER, SUBLANE_GATEWAY],
            target="%600",
            kind="review_result",
        )
        self.assertEqual("gateway_route_blocked", outcome["reason"])
        self.assertEqual([], sent)

    # --- the governed / legitimate routes are untouched --------------------

    def test_governed_kind_to_sublane_codex_gateway_is_allowed(self) -> None:
        # coordinator -> sublane Codex gateway: the governed route head proceeds.
        outcome, _out, _err, _sent = self._run_send(
            panes=[COORDINATOR, SUBLANE_WORKER, SUBLANE_GATEWAY],
            target="%601",
            receiver="codex",
        )
        self.assertIsNotNone(outcome)
        self.assertNotEqual("gateway_route_blocked", outcome.get("reason"))
        self.assertEqual("pending_input", outcome["status"])

    def test_same_lane_gateway_to_worker_is_allowed(self) -> None:
        # The sender IS the sublane gateway (%601, lane-sub-12642) handing off to
        # its own same-lane worker (%600): not a bypass, proceeds.
        outcome, _out, _err, _sent = self._run_send(
            panes=[COORDINATOR, SUBLANE_WORKER, SUBLANE_GATEWAY],
            target="%600",
            sender_pane_id="%601",
        )
        self.assertIsNotNone(outcome)
        self.assertNotEqual("gateway_route_blocked", outcome.get("reason"))
        self.assertEqual("pending_input", outcome["status"])

    def test_sender_outside_inventory_is_not_blocked(self) -> None:
        # The sender pane is not in the live inventory (run from an unmanaged pane):
        # the gate cannot prove a cross-lane bypass and stays out of the way.
        outcome, _out, _err, _sent = self._run_send(
            panes=[SUBLANE_WORKER, SUBLANE_GATEWAY],
            target="%600",
            sender_pane_id="%999",
        )
        self.assertNotEqual("gateway_route_blocked", outcome.get("reason"))
        self.assertEqual("pending_input", outcome["status"])

    # --- explicit durable exception releases the block ---------------------

    def test_allow_direct_worker_admits_send_and_records_exception(self) -> None:
        outcome, _out, err, sent = self._run_send(
            panes=[COORDINATOR, SUBLANE_WORKER, SUBLANE_GATEWAY],
            target="%600",
            allow_direct_worker=True,
        )
        self.assertNotEqual("gateway_route_blocked", outcome.get("reason"))
        self.assertEqual("pending_input", outcome["status"])
        # The bypass is admitted but recorded distinctly from the normal route.
        self.assertIn("explicit durable exception", err)
        # And it actually proceeded to type the body at the worker pane.
        self.assertTrue(sent)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
