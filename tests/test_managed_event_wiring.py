"""Command-boundary wiring for the desired-state event log (Redmine #11727).

Classical-school: these tests exercise the real boundary functions
(`new_agent_session_window` / `new_agent_window`) with only the tmux
subprocess mocked, then observe the durable contract — that a `created`
event lands in the real managed-events store keyed on pane_id, that the
boundary survives an append failure, and that the runtime source-of-truth
surfaces are not rerouted through the event log. Internal call order is
not pinned.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.managed_events import KIND_CREATED, ManagedEventLog


def _ok_tmux(pane_id: str):
    """A fake run_tmux that returns success and the given pane id."""

    def fake(*args, check: bool = True):
        # new-session / new-window print the pane id (-P -F '#{pane_id}').
        if args and args[0] in ("new-session", "new-window"):
            return argparse.Namespace(returncode=0, stdout=f"{pane_id}\n", stderr="")
        # set-option (marker) and anything else: succeed quietly.
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    return fake


class CommandBoundaryAppendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run_boundary(self, func_name: str, *, pane_id: str, cwd: str):
        from mozyo_bridge.application import commands

        with patch.object(commands, "require_tmux"), patch.object(
            commands, "run_tmux", side_effect=_ok_tmux(pane_id)
        ):
            return getattr(commands, func_name)("claude", "mozyo-demo", cwd)

    def test_creating_a_session_appends_a_created_event(self) -> None:
        repo = Path(self._tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True)
        returned = self._run_boundary(
            "new_agent_session_window", pane_id="%1", cwd=str(repo)
        )
        self.assertEqual("%1", returned)
        # Observable contract: the desired-state log now carries the event.
        events = ManagedEventLog().events_for_pane("%1")
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(KIND_CREATED, event.event_kind)
        self.assertEqual("mozyo", event.command)
        self.assertEqual("%1", event.pane_id)
        # pane_id is the identity key; session is an attribute.
        self.assertEqual("mozyo-demo", event.mozyo_session)
        self.assertEqual("claude", event.intent["agent"])

    def test_creating_a_window_appends_too(self) -> None:
        repo = Path(self._tmp.name) / "repo2"
        (repo / ".git").mkdir(parents=True)
        self._run_boundary("new_agent_window", pane_id="%2", cwd=str(repo))
        events = ManagedEventLog().events_for_pane("%2")
        self.assertEqual(1, len(events))
        self.assertEqual("%2", events[0].pane_id)

    def test_repo_root_is_nfd_normalized_in_the_event(self) -> None:
        # An NFC repo path must be stored NFD (the shared #11625 form).
        nfc_name = unicodedata.normalize("NFC", "動画ドライブ")
        repo = Path(self._tmp.name) / nfc_name
        (repo / ".git").mkdir(parents=True)
        self._run_boundary(
            "new_agent_session_window", pane_id="%3", cwd=str(repo)
        )
        event = ManagedEventLog().events_for_pane("%3")[0]
        self.assertEqual(
            unicodedata.normalize("NFD", str(repo)), event.repo_root
        )
        self.assertEqual("default", event.socket)

    def test_append_failure_does_not_break_creation(self) -> None:
        # If the desired-state append blows up, the boundary must still
        # return the pane id (best-effort recording, never a hard failure).
        from mozyo_bridge.application import commands

        repo = Path(self._tmp.name) / "repo3"
        (repo / ".git").mkdir(parents=True)
        with patch.object(commands, "require_tmux"), patch.object(
            commands, "run_tmux", side_effect=_ok_tmux("%9")
        ), patch(
            "mozyo_bridge.managed_events.record_managed_event",
            side_effect=RuntimeError("store exploded"),
        ):
            returned = commands.new_agent_session_window(
                "claude", "mozyo-demo", str(repo)
            )
        self.assertEqual("%9", returned)
        # And nothing was recorded for that pane.
        self.assertEqual([], ManagedEventLog().events_for_pane("%9"))


class RuntimeBoundaryNotReroutedTest(unittest.TestCase):
    def test_resolver_and_handoff_surfaces_do_not_read_event_log(self) -> None:
        # #11726 invariant: liveness / handoff target resolve / preflight /
        # tmux discovery must not depend on the desired-state event log.
        # Pin that the runtime-authoritative modules import neither the
        # event log nor the marker module.
        forbidden = ("managed_events", "managed_marker")
        for module in (
            "src/mozyo_bridge/domain/pane_resolver.py",
            "src/mozyo_bridge/domain/agent_discovery.py",
            "src/mozyo_bridge/domain/handoff.py",
            "src/mozyo_bridge/session_inventory.py",
        ):
            text = (ROOT / module).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(
                    token, text, f"{module} references {token}"
                )

    def test_handoff_preflight_still_uses_live_pane_info(self) -> None:
        # A light regression: the resolver path still goes through
        # pane_lines (live tmux), not any event-log lookup.
        #
        # pane_info() -> resolve_target() first runs validate_target(), a
        # real-tmux pane-existence precheck. Stub it so the test is hermetic
        # (CI has no live "%5" pane); we are only removing the live-tmux call,
        # not substituting an event-log source — the assertion below still
        # proves pane_info reads the patched pane_lines() snapshot.
        from mozyo_bridge.domain import pane_resolver

        panes = [
            {
                "id": "%5",
                "location": "s:1.0",
                "command": "claude",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            }
        ]
        with patch(
            "mozyo_bridge.domain.pane_resolver.validate_target"
        ), patch(
            "mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes
        ):
            info = pane_resolver.pane_info("%5")
        self.assertEqual("%5", info["id"])


if __name__ == "__main__":
    unittest.main()
