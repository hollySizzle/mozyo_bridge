"""End-to-end main-lane implementation_request guard (Redmine #12441).

Implementation-shaped work defaults to a cockpit-visible sublane
(`vibes/docs/logics/coordinator-sublane-development-flow.md`); a direct
`handoff send --to claude --kind implementation_request` into the repo's
default/main-lane Claude is a process gap (#12438 j#63432/j#63434). The pure
predicate is pinned by the sibling unit test; here the orchestration integration
must fail closed *before any pane typing* for the main-lane case and reach typing
for the allowed cases.

Live tmux is patched at the same seams as `test_handoff_target_record_preflight`
(no tmux server required): the explicit `%pane` target resolves through the
numbered `pane_resolver`, and `run_tmux` / `capture_pane` are faked so an
admitted send observes its own landing marker and submits cleanly.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
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

from mozyo_bridge.application.cli import build_parser  # noqa: E402


class MainLaneGuardIntegrationTest(unittest.TestCase):
    """`handoff send` orchestration with tmux patched at the seams."""

    def _run(
        self,
        *,
        to="claude",
        kind="implementation_request",
        lane_id=None,
        window_name="claude",
        command="claude",
        mode=None,
        main_lane_exception=None,
        cockpit=True,
    ):
        from mozyo_bridge.application import commands

        pane = {
            "id": "%884",
            "location": "mysess:1.0",
            "command": command,
            "cwd": tempfile.gettempdir(),
            "window_name": window_name,
            "pane_active": "1",
            "workspace_id": "",
            "lane_id": "" if lane_id is None else lane_id,
        }
        # A cockpit pane carries the `@mozyo_agent_role` option (role_source =
        # pane_option -> cockpit_pane view); a plain repo Claude resolves its role
        # from the window name only (normal_window view).
        pane["agent_role"] = window_name if cockpit else ""

        argv = [
            "handoff", "send",
            "--to", to,
            "--source", "redmine", "--issue", "12441", "--journal", "63443",
            "--kind", kind,
            "--target", "%884",
            "--landing-timeout", "0.01", "--submit-delay", "0",
        ]
        if mode is not None:
            argv += ["--mode", mode]
        if main_lane_exception is not None:
            argv += ["--main-lane-exception", main_lane_exception]
        args = build_parser().parse_args(argv)

        self.typed: list[tuple[str, ...]] = []
        pane_text = ""

        def fake_capture(_target: str, _lines: int) -> str:
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", pane["id"], "-l"):
                pane_text += tmux_args[-1]
                self.typed.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value="mysess"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            with contextlib.suppress(SystemExit):
                args.func(args)
        outcome = None
        for line in out.getvalue().splitlines():
            if line.strip().startswith("{"):
                with contextlib.suppress(ValueError):
                    outcome = json.loads(line)
        return outcome, out.getvalue(), err.getvalue()

    def test_main_lane_claude_implementation_request_is_blocked(self) -> None:
        outcome, _out, err = self._run(lane_id=None)  # no lane -> main lane
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("main_lane_implementation_blocked", outcome["reason"])
        # Nothing was typed into the pane: it fails closed before the body lands.
        self.assertIsNone(outcome["notification_marker"])
        self.assertEqual([], self.typed)
        self.assertIn("default/main lane", err)

    def test_main_lane_block_holds_under_standard_mode(self) -> None:
        outcome, _out, _err = self._run(lane_id="default", mode="standard")
        self.assertEqual("main_lane_implementation_blocked", outcome["reason"])
        self.assertEqual([], self.typed)

    def test_sublane_claude_implementation_request_reaches_typing(self) -> None:
        # A non-default lane is a sublane dispatch: the guard admits it and the
        # send proceeds to typing.
        outcome, _out, _err = self._run(lane_id="lane-5ba25a56f773")
        self.assertNotEqual("main_lane_implementation_blocked", outcome["reason"])
        self.assertNotEqual([], self.typed)

    def test_main_lane_exception_admits_the_send(self) -> None:
        outcome, _out, _err = self._run(
            lane_id="default", main_lane_exception="#12441 j#99999"
        )
        self.assertNotEqual("main_lane_implementation_blocked", outcome["reason"])
        self.assertNotEqual([], self.typed)

    def test_non_implementation_main_lane_notification_is_allowed(self) -> None:
        outcome, _out, _err = self._run(lane_id="default", kind="design_consultation")
        self.assertNotEqual("main_lane_implementation_blocked", outcome["reason"])
        self.assertNotEqual([], self.typed)

    def test_codex_gateway_implementation_request_is_allowed(self) -> None:
        # `--to codex` to a default-lane Codex pane is the gateway path, unaffected.
        outcome, _out, _err = self._run(
            to="codex", lane_id="default", window_name="codex", command="codex"
        )
        self.assertNotEqual("main_lane_implementation_blocked", outcome["reason"])
        self.assertNotEqual([], self.typed)

    def test_normal_window_main_lane_claude_is_not_blocked(self) -> None:
        # A plain unmanaged-repo Claude window (no cockpit pane option) is the
        # generic same-session dispatch path and must keep working.
        outcome, _out, _err = self._run(cockpit=False, lane_id=None)
        self.assertNotEqual("main_lane_implementation_blocked", outcome["reason"])
        self.assertNotEqual([], self.typed)


if __name__ == "__main__":
    unittest.main()
