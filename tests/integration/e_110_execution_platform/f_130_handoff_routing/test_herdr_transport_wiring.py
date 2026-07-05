"""Pure-herdr end-to-end handoff wiring for ``orchestrate_handoff`` (Redmine #13261).

Increment 2 proves a full ``mozyo-bridge handoff send`` with **no tmux available**:
the target is resolved herdr-natively at the orchestrate entry (launch-time sender
identity + live ``agent list`` inventory), the marker lands via a fake herdr
``agent read``, and the outcome is ``sent`` — all without patching the tmux pane
resolver or ``wait_for_text``. The only tmux touchpoints (``require_tmux``, the
tmux-session gates, ``pane_info``, the duplicate-pane snapshot) are gated off under
the herdr backend, so simulating tmux absence (``TMUX`` / ``TMUX_PANE`` unset,
``run_tmux`` never reached for the send since the shim maps to the herdr port)
exercises the real herdr path.

Also covers the fail-closed branches (un-attested sender env, no live target agent)
and confirms the tmux backend resolves to no binding (byte-identical tmux path).
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


class _FakeHerdr:
    """A fake herdr CLI keyed on argv; echoes the last send-text on ``agent read``."""

    def __init__(self, agent_rows):
        self.agent_rows = agent_rows
        self.sends: list = []
        self._last_body_by_target: dict = {}
        self._enter_sent: set = set()

    def run(self, argv, capture_output=None, text=None, timeout=None, **kw):
        rest = list(argv[1:])
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.agent_rows}), stderr=""
            )
        if rest[:2] == ["pane", "send-text"]:
            target, body = rest[2], rest[3] if len(rest) > 3 else ""
            self.sends.append(("send_text", target, body))
            self._last_body_by_target[target] = body
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if rest[:2] == ["pane", "send-keys"]:
            keys = rest[3] if len(rest) > 3 else ""
            self.sends.append(("send_keys", rest[2], keys))
            # Model the agent *starting its turn* on Enter: subsequent pane reads
            # advance past the pre-Enter baseline so the #13262 standard-rail
            # turn-start observation (now generalized to claude standard) confirms
            # via the real herdr read path — answering the "herdr backend × claude
            # standard turn-start" interaction with a live-shaped test.
            # The herdr shim maps the rail's tmux ``Enter`` to the lowercase herdr
            # key token ``enter`` (``_HerdrTmuxShim._ENTER_KEYS``), so match that.
            if keys.lower() == "enter":
                self._enter_sent.add(rest[2])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if rest[:2] == ["agent", "read"]:
            target = rest[2]
            self.sends.append(("read", target))
            body = self._last_body_by_target.get(target, "")
            # Before Enter: echo the injected marker+body (so the landing gate and
            # the pre-Enter turn-start baseline observe the composer). After Enter:
            # append new output so the post-Enter capture differs from the baseline
            # (submit_activity_observed -> confirmed).
            if target in self._enter_sent:
                body = f"{body}\n[agent] working on the turn"
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
        raise AssertionError(f"unexpected subprocess call: {argv!r}")


def _outcome_from(stdout: str):
    outcome = None
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                outcome = json.loads(line)
            except json.JSONDecodeError:
                pass
    return outcome


class PureHerdrEndToEndTest(unittest.TestCase):
    def _run(
        self, *, agent_rows_fn, set_sender_env=True, mode="standard", tmux_pane=None
    ):
        from mozyo_bridge.application import commands  # noqa: F401 (import side effects)
        from mozyo_bridge.application.cli import build_parser

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
            herdr = _FakeHerdr(agent_rows_fn(workspace_id))

            herdr_bin = repo / "fake-herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(
                herdr_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )

            argv = [
                "handoff", "send", "--to", "claude",
                "--source", "asana", "--kind", "implementation_request",
                "--task-id", "T1", "--comment-id", "C1",
                "--mode", mode,
                "--landing-timeout", "0.05", "--submit-delay", "0",
            ]
            args = build_parser().parse_args(argv)
            args.repo = str(repo)

            # Simulate a pure herdr session: no tmux server. TMUX_PANE is unset by
            # default; a test may set it (``tmux_pane``) to prove the send makes ZERO
            # tmux calls even when a stale TMUX_PANE is present (the fake herdr runner
            # raises on any non-herdr argv, so a tmux `list-panes` would blow up).
            env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
            env["MOZYO_HERDR_BINARY"] = str(herdr_bin)
            env["MOZYO_REPO"] = str(repo)
            env["MOZYO_BRIDGE_HOME"] = str(home)
            if tmux_pane is not None:
                env["TMUX_PANE"] = tmux_pane
            if set_sender_env:
                env["MOZYO_WORKSPACE_ID"] = workspace_id
                env["MOZYO_AGENT_ROLE"] = "codex"
                env["MOZYO_LANE_ID"] = "lane-1"

            with patch("subprocess.run", herdr.run), \
                patch("mozyo_bridge.application.commands.time.sleep"), \
                patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    result = args.func(args)
                except BaseException as exc:  # noqa: BLE001
                    result = exc
            return result, herdr, workspace_id, out.getvalue(), err.getvalue()

    def test_send_resolves_target_and_marker_lands_no_tmux(self) -> None:
        target_locator = "wT:pT"

        def rows(ws):
            # Same-lane worker send (sender + target both lane-1): the gateway-route
            # gate allows it, so the send completes.
            return [
                {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
                {"name": encode_assigned_name(ws, "claude", "lane-1"), "pane_id": target_locator},
            ]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows)
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        # Delivery + read all hit the herdr-resolved target locator (never the sender).
        touched = {op[1] for op in herdr.sends}
        self.assertEqual(touched, {target_locator})
        # A real herdr read observed the marker (wait_for_text was not patched).
        self.assertTrue([op for op in herdr.sends if op[0] == "read"])
        self.assertTrue([op for op in herdr.sends if op[0] == "send_text"])

    def test_gateway_gate_blocks_cross_lane_worker_via_env_no_tmux(self) -> None:
        # Redmine #13261 increment 4: a governed implementation_request `--to claude`
        # to a worker in a DIFFERENT lane than the env-derived sender must fail closed
        # on the gateway-route gate — resolved from the env sender lane (lane-1) vs the
        # target agent's lane (lane-x), with NO tmux call (TMUX_PANE is set, so a
        # `current_pane_lane_unit()` fallback would spawn `tmux list-panes` and the
        # fake herdr runner would raise).
        def rows(ws):
            return [
                {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
                {"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": "wT:pT"},
            ]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows, tmux_pane="%99")
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "blocked")
        self.assertEqual(outcome.get("reason"), "gateway_route_blocked")
        # Fail-closed before any send.
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]
        )

    def test_gateway_gate_same_lane_worker_passes_with_tmux_pane_set(self) -> None:
        # Same-lane worker send with a stale TMUX_PANE present: the gate resolves the
        # sender lane from env (lane-1 == target lane-1) and allows the send — proving
        # the herdr path never reads tmux even when TMUX_PANE is set.
        target_locator = "wT:pT"

        def rows(ws):
            return [
                {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
                {"name": encode_assigned_name(ws, "claude", "lane-1"), "pane_id": target_locator},
            ]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows, tmux_pane="%99")
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        self.assertEqual(_outcome_from(out).get("status"), "sent", msg=out)

    def test_missing_sender_env_fails_closed(self) -> None:
        def rows(ws):
            return [{"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": "wT:pT"}]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows, set_sender_env=False)
        self.assertNotEqual(result, 0)
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "blocked")
        self.assertEqual(outcome.get("reason"), "target_unavailable")
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]
        )

    def test_no_target_agent_fails_closed(self) -> None:
        def rows(ws):
            # Only the sender (codex) exists; --to claude has no live agent.
            return [{"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"}]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows)
        self.assertNotEqual(result, 0)
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked")
        self.assertEqual(outcome.get("reason"), "target_unavailable")
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]
        )


class TmuxBackendUntouchedTest(unittest.TestCase):
    """backend=tmux (and absent config) resolve to None — the shim installs nothing."""

    def _binding_for(self, config_text):
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

    def test_herdr_backend_selected_predicate(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
            herdr_backend_selected,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )

            class _Args:
                pass

            args = _Args()
            args.repo = str(repo)
            self.assertTrue(herdr_backend_selected(args))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
