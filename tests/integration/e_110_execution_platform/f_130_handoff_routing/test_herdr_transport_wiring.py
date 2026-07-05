"""Herdr live-wiring integration for ``orchestrate_handoff`` (Redmine #13253).

Drives the real ``mozyo-bridge handoff send`` CLI path end to end **through the
real** ``resolve_handoff_transport_binding`` (no patch of the resolver) with a
repo-local ``terminal_transport: herdr`` config, a trusted-env fake herdr binary,
and a faked ``subprocess.run`` — so the whole herdr path runs: config selection,
the target-pane identity projection (#13253 j#72373), the ``agent list`` re-bind,
the tmux-``%N`` → live-locator translation, and the send/capture over the port.
No live herdr binary and no tmux server.

The point of the fixtures here (j#72372 / j#72373): the translation identity must
come from the **target pane**, not the sender / current-repo context — so the
agent list carries both a sender row and the target-pane row, and delivery must
land only on the target pane's locator.
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

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)

SESSION = "agents"

# The sender's own herdr row (a different workspace/role than the target). If the
# translation wrongly bound to the sender, delivery would land on wS:pS.
SENDER_NAME = encode_assigned_name("ws-sender", "codex", "default")
SENDER_LOCATOR = "wS:pS"


def _target_pane(*, workspace_id, lane_id):
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


class HerdrRealResolverWiringTest(unittest.TestCase):
    def _run(
        self,
        *,
        target_pane,
        agent_rows,
        mode="standard",
        pane_active="1",
        extra_argv=None,
    ):
        """Drive `handoff send` over the real herdr resolver; return (result, sends, out, err).

        ``sends`` is the list of herdr port operations captured from the faked
        subprocess as ``(op, target_locator, *rest)`` — every send/read the shim
        performed on the port, so a test can assert which locator delivery hit.
        """
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        target_pane = dict(target_pane, pane_active=pane_active)
        sends: list = []

        def fake_run(argv, capture_output=None, text=None, timeout=None, **kw):
            # Only the herdr binary is ever spawned on this path.
            rest = list(argv[1:])
            # Exact argv: the real herdr `agent list` read carries no extra flags
            # (no `--json`; JSON is the default output). Pinning the whole argv
            # means a re-introduced `--json` (or any added flag) makes this fake
            # fall through to the AssertionError instead of silently matching.
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

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            herdr_bin = repo / "fake-herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(
                herdr_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )

            argv = [
                "handoff", "send", "--to", "claude",
                "--source", "asana", "--kind", "implementation_request",
                "--task-id", "T1", "--comment-id", "C1",
                "--target", "%2", "--mode", mode,
                # Redmine #13262 generalized the standard-rail turn-start
                # observation to the claude receiver. This wiring suite exercises
                # herdr locator translation, not tmux-rail turn-start semantics, so
                # it disables the observation with the supported `--landing-timeout
                # 0` (window 0 => observation disabled) to keep a successful send
                # resolving to `sent` without a tmux pane-advance capture (the herdr
                # port has no such capture; that interaction is #13261's concern).
                "--landing-timeout", "0", "--submit-delay", "0",
            ] + (extra_argv or [])
            args = build_parser().parse_args(argv)
            args.repo = str(repo)

            env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
            env["MOZYO_HERDR_BINARY"] = str(herdr_bin)
            env["MOZYO_REPO"] = str(repo)

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
                    return_value=[target_pane],
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
                except BaseException as exc:  # noqa: BLE001 - capture fail-closed outcome
                    result = exc
        return result, sends, out.getvalue(), err.getvalue()

    def test_target_pane_identity_drives_translation_not_sender(self) -> None:
        # Cross-lane fixture: the target pane's lane differs from any sender/current
        # context. The agent list carries BOTH the sender row and the target row;
        # delivery must land ONLY on the target pane's locator.
        pane = _target_pane(workspace_id="ws-target", lane_id="lane-x")
        target_name = encode_assigned_name("ws-target", "claude", "lane-x")
        target_locator = "wT:pT"
        # Real herdr `agent list` row shape: the transient locator rides on the
        # `pane_id` primary key (PoC #13175 E10 実測), pinning that the primary
        # path resolves against pane_id, not an alias.
        rows = [
            {"name": SENDER_NAME, "pane_id": SENDER_LOCATOR},
            {"name": target_name, "pane_id": target_locator},
        ]
        result, sends, out, err = self._run(target_pane=pane, agent_rows=rows)

        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "sent")
        # Delivery reached the port only at the TARGET pane's locator.
        self.assertTrue(sends, msg="no herdr port ops captured")
        targets = {op[1] for op in sends}
        self.assertEqual(targets, {target_locator})
        self.assertNotIn(SENDER_LOCATOR, targets)

    def test_sender_only_agent_list_fails_closed(self) -> None:
        # If only the sender's row exists (the target pane never registered its herdr
        # name), the target-name re-bind is not-found -> fail closed. Delivery must
        # NEVER fall back to the sender's locator.
        pane = _target_pane(workspace_id="ws-target", lane_id="lane-x")
        rows = [{"name": SENDER_NAME, "pane_id": SENDER_LOCATOR}]
        result, sends, out, err = self._run(target_pane=pane, agent_rows=rows)

        # Fail-closed: not a clean sent, and nothing delivered to the sender locator.
        self.assertNotEqual(result, 0)
        self.assertFalse(
            [
                op
                for op in sends
                if op[0] in ("send_text", "send_keys") and op[1] == SENDER_LOCATOR
            ],
            msg=f"a send leaked to the sender locator: {sends!r}",
        )

    def test_queue_enter_inactive_target_activates_and_sends_over_herdr(self) -> None:
        # finding-1 regression, now through the real resolver: a queue-enter inactive
        # admitted target activates (select-pane no-op) and delivers on the target
        # locator without a TransportBindingError.
        pane = _target_pane(workspace_id="ws-target", lane_id="lane-x")
        target_name = encode_assigned_name("ws-target", "claude", "lane-x")
        target_locator = "wT:pT"
        rows = [{"name": target_name, "pane_id": target_locator}]
        result, sends, out, err = self._run(
            target_pane=pane, agent_rows=rows, mode="queue-enter", pane_active="0"
        )

        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent")
        self.assertIn("- Activation:", out)
        # Every port op targeted the translated locator (the select-pane no-op never
        # reaches the port, so it is absent from `sends`).
        self.assertTrue([op for op in sends if op[0] == "send_text"])
        for op in sends:
            self.assertEqual(op[1], target_locator, msg=f"un-translated port op: {op!r}")


class TargetIdentityProjectionUnitTest(unittest.TestCase):
    """`_resolve_target_assigned_name` mints from the TARGET pane, fail-closed."""

    def test_name_is_minted_from_the_strong_target_pane_slot(self) -> None:
        from mozyo_bridge.application import handoff_transport_wiring as w

        # agent_role="claude" -> pane-option role -> strong / non-ambiguous.
        pane = _target_pane(workspace_id="ws-target", lane_id="lane-x")
        with patch.object(w, "_pane_info", return_value=pane):
            name = w._resolve_target_assigned_name("%2", receiver="claude")
        self.assertEqual(name, encode_assigned_name("ws-target", "claude", "lane-x"))

    def test_weak_process_only_role_fails_closed(self) -> None:
        # j#72380 minimal repro: no @mozyo_agent_role option and a non-agent window
        # name, so the role is only WEAKLY inferred from the `claude` process
        # basename. It must NOT mint an identity (could mis-send to another pane in
        # the same slot).
        from mozyo_bridge.application import handoff_transport_wiring as w
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
            TransportBindingError,
        )

        pane = dict(
            _target_pane(workspace_id="ws-target", lane_id="lane-x"),
            agent_role="",
            window_name="zsh",
            command="claude",
        )
        with patch.object(w, "_pane_info", return_value=pane):
            with self.assertRaises(TransportBindingError):
                w._resolve_target_assigned_name("%2", receiver="claude")

    def test_ambiguous_role_fails_closed(self) -> None:
        # An ambiguous projection must fail closed (binds_receiver requires
        # not ambiguous). Constructed by projecting an ambiguous PreflightTarget.
        from mozyo_bridge.application import handoff_transport_wiring as w
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
            PreflightTarget,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
            TransportBindingError,
        )

        ambiguous = PreflightTarget(
            pane_id="%2",
            role="claude",
            role_source="pane_option",
            confidence="strong",
            ambiguous=True,
            view_kind="agent",
            workspace_id="ws-target",
            lane_id="lane-x",
            window_name="claude",
            pane_option_role="claude",
        )
        with patch.object(w, "_pane_info", return_value={}), patch.object(
            w, "project_preflight_target", return_value=ambiguous
        ):
            with self.assertRaises(TransportBindingError):
                w._resolve_target_assigned_name("%2", receiver="claude")

    def test_cross_bound_role_fails_closed(self) -> None:
        # A strong `codex` pane targeted by --to claude does not bind the receiver.
        from mozyo_bridge.application import handoff_transport_wiring as w
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
            TransportBindingError,
        )

        pane = dict(
            _target_pane(workspace_id="ws-target", lane_id="lane-x"),
            agent_role="codex",
            window_name="codex",
            command="codex",
        )
        with patch.object(w, "_pane_info", return_value=pane):
            with self.assertRaises(TransportBindingError):
                w._resolve_target_assigned_name("%2", receiver="claude")

    def test_missing_workspace_fails_closed(self) -> None:
        from mozyo_bridge.application import handoff_transport_wiring as w
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
            TransportBindingError,
        )

        pane = _target_pane(workspace_id="", lane_id="lane-x")
        with patch.object(w, "_pane_info", return_value=pane):
            with self.assertRaises(TransportBindingError):
                w._resolve_target_assigned_name("%2", receiver="claude")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
