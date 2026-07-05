"""Herdr-native live-wiring integration for ``orchestrate_handoff`` (Redmine #13261).

Drives the real ``mozyo-bridge handoff send`` CLI path end to end **through the
real** ``resolve_handoff_transport_binding`` (no patch of the resolver) with a
repo-local ``terminal_transport: herdr`` config, a trusted-env fake herdr binary,
a registered workspace anchor, launch-time sender-identity env, and a faked
``subprocess.run`` — so the whole herdr-native path runs: config selection, sender
identity attestation (env vs anchor), receiver-label resolution against the live
``agent list`` inventory scoped to the sender's workspace, the ``%N`` → live-locator
translation, and the send/capture over the port. No live herdr binary and no tmux
server.

#13261 supersedes the #13253 target-pane projection: the target is resolved from the
**inventory + sender workspace scope**, not a tmux pane's user-options. So the agent
list carries a same-workspace target agent (and a sender row), and delivery must land
only on the resolved target agent's locator.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)

SESSION = "agents"


def _target_pane(*, workspace_id, lane_id):
    # The tmux pane orchestrate_handoff resolves for its (non-herdr) send bookkeeping;
    # under #13261 it is NOT the herdr target authority.
    return {
        "id": "%2",
        "location": f"{SESSION}:0.1",
        "command": "claude",
        "cwd": "/repo",
        "window_name": "claude",
        "pane_active": "1",
        "agent_role": "claude",
        "workspace_id": workspace_id,
        "lane_id": lane_id,
    }


def _outcome_from(stdout: str):
    outcome = None
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                outcome = json.loads(line)
            except json.JSONDecodeError:
                pass
    return outcome


class HerdrNativeResolverWiringTest(unittest.TestCase):
    def _run(self, *, agent_rows_fn, sender_role="codex", mode="standard", pane_active="1"):
        """Drive `handoff send` over the real herdr-native resolver.

        ``agent_rows_fn(workspace_id)`` returns the ``agent list`` rows given the
        workspace id minted by ``register_workspace``. Returns
        ``(result, sends, workspace_id, out, err)``.
        """
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        sends: list = []

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            register_workspace(repo, home=home)
            workspace_id = read_anchor(repo)["workspace_id"]
            agent_rows = agent_rows_fn(workspace_id)

            def fake_run(argv, capture_output=None, text=None, timeout=None, **kw):
                rest = list(argv[1:])
                if rest == ["agent", "list"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps({"agents": agent_rows}), stderr=""
                    )
                if rest[:2] == ["pane", "send-text"]:
                    sends.append(("send_text", rest[2], rest[3] if len(rest) > 3 else ""))
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                if rest[:2] == ["pane", "send-keys"]:
                    sends.append(("send_keys", rest[2], rest[3] if len(rest) > 3 else ""))
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                if rest[:2] == ["agent", "read"]:
                    sends.append(("read", rest[2]))
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                raise AssertionError(f"unexpected subprocess call: {argv!r}")

            herdr_bin = repo / "fake-herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(
                herdr_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )

            pane = dict(
                _target_pane(workspace_id=workspace_id, lane_id="lane-x"),
                pane_active=pane_active,
            )

            argv = [
                "handoff", "send", "--to", "claude",
                "--source", "asana", "--kind", "implementation_request",
                "--task-id", "T1", "--comment-id", "C1",
                "--target", "%2", "--mode", mode,
                "--landing-timeout", "0.01", "--submit-delay", "0",
            ]
            args = build_parser().parse_args(argv)
            args.repo = str(repo)

            env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
            env["MOZYO_HERDR_BINARY"] = str(herdr_bin)
            env["MOZYO_REPO"] = str(repo)
            env["MOZYO_BRIDGE_HOME"] = str(home)
            env["MOZYO_WORKSPACE_ID"] = workspace_id
            env["MOZYO_AGENT_ROLE"] = sender_role
            env["MOZYO_LANE_ID"] = "lane-1"

            with patch("subprocess.run", fake_run), \
                patch.object(commands, "require_tmux"), \
                patch.object(commands, "wait_for_text", return_value=True), \
                patch("mozyo_bridge.application.commands.time.sleep"), \
                patch.object(commands, "current_session_name", return_value=SESSION), \
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name",
                    return_value=SESSION,
                ), \
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"
                ), \
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.resolve_target",
                    lambda target: "%2",
                ), \
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines",
                    return_value=[pane],
                ), \
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.infer_repo_root",
                    lambda cwd: "/repo",
                ), \
                patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    result = args.func(args)
                except BaseException as exc:  # noqa: BLE001
                    result = exc
        return result, sends, workspace_id, out.getvalue(), err.getvalue()

    def test_inventory_resolves_target_scoped_to_sender_workspace(self) -> None:
        sender_locator = "wS:pS"
        target_locator = "wT:pT"

        def rows(ws):
            return [
                {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": sender_locator},
                {"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": target_locator},
            ]

        result, sends, ws, out, err = self._run(agent_rows_fn=rows)
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "sent")
        self.assertTrue(sends, msg="no herdr port ops captured")
        targets = {op[1] for op in sends}
        self.assertEqual(targets, {target_locator})
        self.assertNotIn(sender_locator, targets)

    def test_no_target_agent_in_workspace_fails_closed(self) -> None:
        # Only the sender (codex) is present; --to claude has no live agent -> fail
        # closed. Nothing delivered to the sender locator.
        sender_locator = "wS:pS"

        def rows(ws):
            return [{"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": sender_locator}]

        result, sends, ws, out, err = self._run(agent_rows_fn=rows)
        self.assertNotEqual(result, 0)
        leaked = [
            op for op in sends
            if op[0] in ("send_text", "send_keys") and op[1] == sender_locator
        ]
        self.assertFalse(leaked, msg=f"a send leaked to the sender locator: {sends!r}")

    def test_queue_enter_inactive_target_activates_and_sends(self) -> None:
        target_locator = "wT:pT"

        def rows(ws):
            return [{"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": target_locator}]

        result, sends, ws, out, err = self._run(
            agent_rows_fn=rows, mode="queue-enter", pane_active="0"
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent")
        self.assertTrue([op for op in sends if op[0] == "send_text"])
        for op in sends:
            self.assertEqual(op[1], target_locator, msg=f"un-translated port op: {op!r}")


class TmuxBackendUntouchedTest(unittest.TestCase):
    """backend=tmux (and absent config) resolve to None — the shim installs nothing."""

    def _binding_for(self, config_text: str):
        from mozyo_bridge.application.handoff_transport_wiring import (
            resolve_handoff_transport_binding,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".mozyo-bridge").mkdir()
            if config_text is not None:
                (repo / ".mozyo-bridge" / "config.yaml").write_text(
                    config_text, encoding="utf-8"
                )

            class _Args:
                pass

            args = _Args()
            args.repo = str(repo)
            args.to = "claude"
            return resolve_handoff_transport_binding(args)

    def test_explicit_tmux_backend_returns_none(self) -> None:
        self.assertIsNone(
            self._binding_for("version: 1\nterminal_transport:\n  backend: tmux\n")
        )

    def test_absent_config_returns_none(self) -> None:
        self.assertIsNone(self._binding_for(None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
