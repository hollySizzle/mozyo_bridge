"""Main-lane implementation_request fail-closed guard (Redmine #12441).

Implementation-shaped work defaults to a cockpit-visible sublane
(`vibes/docs/logics/coordinator-sublane-development-flow.md`); a direct
`handoff send --to claude --kind implementation_request` into the repo's
default/main-lane Claude is a process gap (#12438 j#63432/j#63434). These tests
pin the guard:

- the pure predicate `main_lane_implementation_request_blocked` decides the four
  cases the dispatch requires (main-lane Claude impl blocked, sublane Claude impl
  allowed, Codex gateway allowed, non-implementation main-lane notification
  allowed), plus the `--main-lane-exception` escape hatch;
- the orchestration integration fails closed before any pane typing for the
  main-lane case and reaches typing for the allowed cases.

Live tmux is patched at the seams (the same harness shape as
`test_handoff_cross_workspace_consult`); no tmux server is required.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser  # noqa: E402
from mozyo_bridge.domain.handoff import (  # noqa: E402
    MAIN_LANE_ID,
    main_lane_implementation_request_blocked,
)


class MainLanePredicateTest(unittest.TestCase):
    def _blocked(self, **overrides) -> bool:
        kwargs = dict(
            receiver="claude",
            kind="implementation_request",
            target_lane_id="default",
            target_is_cockpit_pane=True,
            target_binds_claude=True,
            has_main_lane_exception=False,
        )
        kwargs.update(overrides)
        return main_lane_implementation_request_blocked(**kwargs)

    def test_cockpit_main_lane_claude_implementation_request_blocked(self) -> None:
        self.assertTrue(self._blocked())

    def test_empty_or_missing_lane_normalizes_to_main(self) -> None:
        for lane in (None, "", "  "):
            self.assertTrue(
                self._blocked(target_lane_id=lane),
                f"lane={lane!r} should normalize to {MAIN_LANE_ID}",
            )

    def test_sublane_claude_implementation_request_allowed(self) -> None:
        self.assertFalse(self._blocked(target_lane_id="lane-5ba25a56f773"))

    def test_normal_window_main_lane_not_blocked(self) -> None:
        # A plain unmanaged-repo Claude (normal_window) carries no sublane role,
        # so the cockpit/sublane guard does not apply.
        self.assertFalse(self._blocked(target_is_cockpit_pane=False))

    def test_pane_not_binding_claude_left_to_binding_gate(self) -> None:
        # A cockpit pane that does not strongly bind claude (e.g. marked codex)
        # is a role-mismatch for the binding gate, not a main-lane block.
        self.assertFalse(self._blocked(target_binds_claude=False))

    def test_codex_gateway_dispatch_allowed(self) -> None:
        self.assertFalse(self._blocked(receiver="codex"))

    def test_non_implementation_main_lane_notification_allowed(self) -> None:
        for kind in ("design_consultation", "custom", "reply", "review_request"):
            self.assertFalse(
                self._blocked(kind=kind),
                f"kind={kind} to main-lane Claude must not be blocked",
            )

    def test_explicit_exception_allows_main_lane(self) -> None:
        self.assertFalse(self._blocked(has_main_lane_exception=True))


class MainLaneGuardIntegrationTest(unittest.TestCase):
    """End-to-end `handoff send` integration with tmux patched at the seams."""

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
        }
        # A cockpit pane carries the `@mozyo_agent_role` option (role_source =
        # pane_option -> cockpit_pane view); a plain repo Claude resolves its role
        # from the window name only (normal_window view).
        if cockpit:
            pane["agent_role"] = window_name
        if lane_id is not None:
            pane["lane_id"] = lane_id

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

        self.typed = []

        def fake_run_tmux(*a, check: bool = True):
            flat = a[0] if (a and isinstance(a[0], (list, tuple))) else a
            if "send-keys" in flat and "-l" in flat:
                self.typed.append(flat)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch.object(commands, "require_tmux"), \
            patch.object(commands, "capture_pane", return_value=""), \
            patch.object(commands, "run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch.object(commands, "current_session_name", return_value="mysess"), \
            patch.object(commands, "pane_info", return_value=pane), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]), \
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
        # A non-default lane is a sublane dispatch: the guard admits it (it then
        # proceeds to typing; the mock has no observable marker so it does not
        # report the main-lane block).
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
