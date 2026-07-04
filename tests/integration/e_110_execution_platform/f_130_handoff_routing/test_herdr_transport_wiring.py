"""Herdr live-wiring smoke for ``orchestrate_handoff`` (Redmine #13253).

Drives the real ``mozyo-bridge handoff send`` CLI path end to end with the
runtime transport binding resolved to the **herdr** backend over an in-memory
fake port (no live herdr binary, no tmux server). It proves the single injection
point (the ``_bind_runtime_transport`` decorator) swaps the send/capture
primitives so the unchanged send choreography lands on the herdr port:

- the internal pre-type snapshot maps to ``read_pane`` (visible source);
- the marker+body type maps to ``send_text`` (the composer injection);
- the submit maps to ``send_keys("enter")``.

The tmux-default byte-for-byte behaviour is covered by the rest of the handoff
suite (every existing test runs with the default binding, which installs
nothing); this file pins only the herdr branch of the wiring.
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

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
    resolve_runtime_transport_binding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    SOURCE_VISIBLE,
    PaneReadResult,
    TerminalTransportConfig,
    TransportResult,
)

SESSION = "mozyo-cockpit"


def _pane(pane_id, location, **kw):
    base = {
        "id": pane_id,
        "location": location,
        "command": kw.get("command", "node"),
        "cwd": kw.get("cwd", "/repo"),
        "window_name": kw.get("window_name", "cockpit"),
        "pane_active": kw.get("pane_active", "1"),
        "agent_role": kw.get("agent_role", ""),
        "workspace_id": kw.get("workspace_id", ""),
        "lane_id": kw.get("lane_id", ""),
        "lane_label": kw.get("lane_label", ""),
    }
    return base


SENDER_CODEX = _pane(
    "%900",
    f"{SESSION}:0.9",
    agent_role="codex",
    workspace_id="ws-it-donyu",
    lane_id="lane-main",
    command="codex",
    cwd="/ws/it-donyu",
)
LOCAL_CLAUDE = _pane(
    "%901",
    f"{SESSION}:0.1",
    agent_role="claude",
    workspace_id="ws-it-donyu",
    lane_id="lane-main",
    command="claude",
    cwd="/ws/it-donyu/app",
)

REPO_ROOTS = {"/ws/it-donyu": "/ws/it-donyu", "/ws/it-donyu/app": "/ws/it-donyu"}


class FakePort:
    """An in-memory herdr :class:`TerminalTransportPort` recording every call."""

    backend = BACKEND_HERDR

    def __init__(self):
        self.calls: list = []

    def send_text(self, target, text):
        self.calls.append(("send_text", target, text))
        return TransportResult.success()

    def send_keys(self, target, keys):
        self.calls.append(("send_keys", target, keys))
        return TransportResult.success()

    def read_pane(self, target, *, source=SOURCE_VISIBLE, lines=None):
        self.calls.append(("read_pane", target, source, lines))
        return PaneReadResult.success("")


def _outcome_from(stdout: str):
    outcome = None
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                outcome = json.loads(line)
            except json.JSONDecodeError:
                pass
    return outcome


class HerdrWiringSmokeTest(unittest.TestCase):
    def test_standard_send_lands_on_herdr_port(self) -> None:
        from mozyo_bridge.application import commands
        from mozyo_bridge.application import handoff_transport_wiring
        from mozyo_bridge.application.cli import build_parser

        port = FakePort()

        def _tmux_run_tmux(*a, check=True):  # unused by the herdr branch
            raise AssertionError("tmux run_tmux must not be called under herdr")

        def _tmux_capture_pane(_t, _l):
            raise AssertionError("tmux capture_pane must not be called under herdr")

        binding = resolve_runtime_transport_binding(
            TerminalTransportConfig(backend=BACKEND_HERDR),
            tmux_run_tmux=_tmux_run_tmux,
            tmux_capture_pane=_tmux_capture_pane,
            port=port,
        )

        argv = [
            "handoff", "send", "--to", "claude",
            "--source", "redmine", "--issue", "13253", "--journal", "72356",
            "--kind", "implementation_request",
            "--mode", "standard",
            "--landing-timeout", "0.01", "--submit-delay", "0",
            "--summary", "herdr smoke",
        ]
        args = build_parser().parse_args(argv)

        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        env["TMUX_PANE"] = "%900"

        with patch.object(commands, "require_tmux"), \
            patch.object(
                handoff_transport_wiring,
                "resolve_handoff_transport_binding",
                return_value=binding,
            ), \
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
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines",
                return_value=[SENDER_CODEX, LOCAL_CLAUDE],
            ), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.infer_repo_root",
                lambda cwd: REPO_ROOTS.get(cwd),
            ), \
            patch.dict(os.environ, env, clear=True), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()):
            with contextlib.suppress(SystemExit):
                args.func(args)

        outcome = _outcome_from(out.getvalue())
        self.assertIsNotNone(outcome, msg=out.getvalue())
        self.assertEqual(outcome.get("status"), "sent")

        kinds = [c[0] for c in port.calls]
        # Preflight read -> composer inject -> submit, all on the herdr port.
        self.assertIn("read_pane", kinds)
        self.assertIn("send_text", kinds)
        self.assertIn("send_keys", kinds)

        send_text_calls = [c for c in port.calls if c[0] == "send_text"]
        self.assertEqual(len(send_text_calls), 1)
        _, target, text = send_text_calls[0]
        self.assertEqual(target, "%901")
        self.assertIn("herdr smoke", text)

        enter_calls = [c for c in port.calls if c[0] == "send_keys" and c[2] == "enter"]
        self.assertEqual(len(enter_calls), 1)
        self.assertEqual(enter_calls[0][1], "%901")

        # The read maps to the visible source (the tmux capture equivalent).
        read_calls = [c for c in port.calls if c[0] == "read_pane"]
        self.assertTrue(read_calls)
        self.assertEqual(read_calls[0][2], SOURCE_VISIBLE)


class StatefulFakePort:
    """A herdr port that echoes the injected composer, so the marker lands.

    Models a well-behaved receiver: ``send_text`` appends to the composer,
    ``read_pane`` returns it (so the landing-marker wait observes the marker),
    and ``send_keys enter`` submits (clears the composer).
    """

    backend = BACKEND_HERDR

    def __init__(self):
        self.calls: list = []
        self.composer = ""

    def send_text(self, target, text):
        self.calls.append(("send_text", target, text))
        self.composer += text
        return TransportResult.success()

    def send_keys(self, target, keys):
        self.calls.append(("send_keys", target, keys))
        if keys == "enter":
            self.composer = ""
        return TransportResult.success()

    def read_pane(self, target, *, source=SOURCE_VISIBLE, lines=None):
        self.calls.append(("read_pane", target, source, lines))
        return PaneReadResult.success(self.composer)


class HerdrInactiveTargetActivationTest(unittest.TestCase):
    """The finding-1 regression: default queue-enter + inactive admitted target.

    The target-activation tail (#12597) resolves ``run_tmux("select-pane", …)``
    through ``commands`` at call time. Under the herdr binding the whole
    ``commands.run_tmux`` is swapped for the shim, so before the finding-1 fix the
    activation raised ``TransportBindingError`` and the send crashed. With the fix
    ``select-pane`` is a target-validated no-op, so the admitted inactive split is
    activated and delivered to over the herdr port without error.
    """

    def test_queue_enter_inactive_admitted_target_activates_and_sends_over_herdr(
        self,
    ) -> None:
        from mozyo_bridge.application import commands  # noqa: F401
        from mozyo_bridge.application import handoff_transport_wiring
        from mozyo_bridge.application.cli import build_parser

        port = StatefulFakePort()

        def _tmux_unused(*a, **k):
            raise AssertionError("tmux primitive must not be called under herdr")

        binding = resolve_runtime_transport_binding(
            TerminalTransportConfig(backend=BACKEND_HERDR),
            tmux_run_tmux=_tmux_unused,
            tmux_capture_pane=_tmux_unused,
            port=port,
        )

        argv = [
            "handoff", "send", "--to", "claude",
            "--source", "asana", "--kind", "implementation_request",
            "--task-id", "T1", "--comment-id", "C1",
            "--target", "%2", "--mode", "queue-enter",
        ]
        args = build_parser().parse_args(argv)

        inactive_pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "0",
            "workspace_id": "ws-1",
        }

        raised: list = []

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch.object(
                handoff_transport_wiring,
                "resolve_handoff_transport_binding",
                return_value=binding,
            ), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value="agents",
            ), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"
            ), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines",
                return_value=[inactive_pane],
            ), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()):
            try:
                result = args.func(args)
            except SystemExit as exc:
                result = exc
            except Exception as exc:  # pragma: no cover - the bug we are fixing
                raised.append(exc)
                result = None

        # The activation select-pane must NOT have raised (finding-1 fix).
        self.assertEqual(raised, [], msg=f"send raised under herdr: {raised!r}")
        self.assertEqual(result, 0)

        outcome = _outcome_from(out.getvalue())
        self.assertIsNotNone(outcome, msg=out.getvalue())
        self.assertEqual(outcome.get("status"), "sent")

        # Delivery reached the herdr port: composer inject + submit.
        self.assertTrue([c for c in port.calls if c[0] == "send_text"])
        self.assertTrue(
            [c for c in port.calls if c[0] == "send_keys" and c[2] == "enter"]
        )
        # The durable record still documents the #12597 activation fact.
        self.assertIn("- Activation:", out.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
