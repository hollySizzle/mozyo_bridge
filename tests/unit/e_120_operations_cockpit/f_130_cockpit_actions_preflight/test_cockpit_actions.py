"""Pane-centric cockpit action + preflight-bridge tests (Redmine #12323 split).

Focused on :mod:`mozyo_bridge.application.cockpit_actions`: the side-effecting
``reveal_in_finder`` / ``jump_to_unit`` actions and their action-time live
preflight (``_resolve_record`` / ``_pick_attached_client``) — structured argv,
stale-safe failure, attached-client selection with control-mode demotion. Split
out of ``test_cockpit_ui`` (#12323) so the action / preflight responsibility is
tested independently of the HTTP server, the served-API payload contract, and the
HTML page. The grouped (Unit-identity) action preflight has its own focused file
(``test_cockpit_grouped_action``). Everything runs on temp homes with patched
inventory / subprocess — no real tmux mutations.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_actions import (
    CockpitActionError,
    jump_to_unit,
    reveal_in_finder,
)

# reveal_in_finder calls subprocess.run / sys.platform inside cockpit_actions, so
# patch those names on that module.
COCKPIT_ACTIONS = "mozyo_bridge.application.cockpit_actions"


def pane(pane_id: str, session: str, agent: str, cwd: str = "") -> dict:
    return {
        "id": pane_id,
        "location": f"{session}:1.0",
        "command": agent,
        "cwd": cwd,
        "window_name": agent,
        "pane_active": "1",
    }


class CockpitActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def _panes(self) -> list[dict]:
        return [pane("%1", "mozyo-demo", "claude", cwd=str(self.repo))]

    def test_reveal_runs_structured_open_on_repo_root(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, capture_output, text, check):
            calls.append(argv)
            return type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()

        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.try_pane_lines",
            return_value=self._panes(),
        ), patch(
            f"{COCKPIT_ACTIONS}.subprocess.run",
            side_effect=fake_run,
        ), patch(
            f"{COCKPIT_ACTIONS}.sys.platform", "darwin"
        ):
            result = reveal_in_finder("%1", home=self.home)
        # Structured argv: the path rides as one argument, never through a
        # shell string — spaces / Japanese segments cannot inject.
        self.assertEqual([["open", str(self.repo.resolve())]], calls)
        self.assertEqual("reveal", result["action"])

    def test_jump_switches_most_recent_regular_client(self) -> None:
        tmux_calls: list[tuple] = []

        def fake_run_tmux(*args, check: bool = True):
            tmux_calls.append(args)
            if args[0] == "list-clients":
                return type(
                    "R",
                    (),
                    {
                        "returncode": 0,
                        # control-mode client is newer but demoted; the
                        # regular client wins (jump v1 contract).
                        "stdout": (
                            "200\t1\t/dev/ttys-cc\n"
                            "100\t0\t/dev/ttys-old\n"
                            "150\t0\t/dev/ttys-new\n"
                        ),
                        "stderr": "",
                    },
                )()
            return type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()

        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.try_pane_lines",
            return_value=self._panes(),
        ), patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.run_tmux",
            side_effect=fake_run_tmux,
        ):
            result = jump_to_unit("%1", home=self.home)
        self.assertEqual("/dev/ttys-new", result["client"])
        self.assertEqual("mozyo-demo:1", result["target"])
        switch = [c for c in tmux_calls if c[0] == "switch-client"]
        self.assertEqual(
            [("switch-client", "-c", "/dev/ttys-new", "-t", "mozyo-demo:1")],
            switch,
        )

    def test_jump_without_attached_client_fails_safely(self) -> None:
        def fake_run_tmux(*args, check: bool = True):
            return type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()

        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.try_pane_lines",
            return_value=self._panes(),
        ), patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.run_tmux",
            side_effect=fake_run_tmux,
        ):
            with self.assertRaises(CockpitActionError) as ctx:
                jump_to_unit("%1", home=self.home)
        self.assertIn("no attached tmux client", str(ctx.exception))

    def test_reveal_refuses_missing_directory(self) -> None:
        panes = [pane("%1", "mozyo-demo", "claude", cwd="/no/such/dir")]
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            with self.assertRaises(CockpitActionError):
                reveal_in_finder("%1", home=self.home)


if __name__ == "__main__":
    unittest.main()
