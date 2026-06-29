"""Fake-port specification for the status session-read boundary (Redmine #12785).

Migrates the status present/missing agent-window logic off the ``commands.*``
monkeypatch seam. The ``status`` command's existence / window-enumeration /
pane-capture reads previously could only be exercised by patching
``mozyo_bridge.application.commands.session_exists`` /
``commands.list_session_windows`` / ``commands.run_tmux`` and scraping stdout
(see ``test_mozyo_bridge`` ``test_cmd_status_*``). Here the three reads are
injected through a fake :class:`StatusSessionPort`, so
:class:`ResolveSessionStatusUseCase` is unit-tested with no patch and no real
tmux. The broad ``commands.*`` status integration tests stay as end-to-end
characterization; deeper migration of those sites remains residual to #12638.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.commands_status import (  # noqa: E402
    ResolveSessionStatusUseCase,
    StatusQuery,
)
from mozyo_bridge.application.status_session_port import (  # noqa: E402
    LiveStatusSession,
    StatusSessionPort,
)


class _FakeStatusSession:
    """Fake :class:`StatusSessionPort`: the three reads are scripted.

    Counts ``capture_panes`` calls so the spec can pin the behavior-preserving
    invariant that ``list-panes`` runs only when agent windows are present.
    """

    def __init__(self, *, exists, windows=(), capture=(False, "")):
        self._exists = exists
        self._windows = list(windows)
        self._capture = capture
        self.capture_calls = 0

    def session_exists(self, session):
        return self._exists

    def list_windows(self, session):
        return list(self._windows)

    def capture_panes(self, session):
        self.capture_calls += 1
        return self._capture


class StatusSessionPortContractTest(unittest.TestCase):
    def test_fake_and_live_satisfy_port(self) -> None:
        self.assertIsInstance(_FakeStatusSession(exists=False), StatusSessionPort)
        self.assertIsInstance(LiveStatusSession(), StatusSessionPort)


class ResolveSessionStatusUseCaseTest(unittest.TestCase):
    def test_missing_session_reports_absent_without_window_read(self) -> None:
        fake = _FakeStatusSession(exists=False)
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertFalse(view.present)
        self.assertFalse(view.has_agent_windows)
        # A missing session never reaches pane capture.
        self.assertEqual(0, fake.capture_calls)

    def test_agent_windows_capture_panes_and_compute_missing(self) -> None:
        fake = _FakeStatusSession(
            exists=True,
            windows=["claude"],
            capture=(True, "0\tclaude\t%1\t1\tclaude\t/repo\n"),
        )
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertTrue(view.present)
        self.assertTrue(view.has_agent_windows)
        self.assertEqual(("claude",), view.agent_windows)
        self.assertTrue(view.panes_ok)
        self.assertEqual("0\tclaude\t%1\t1\tclaude\t/repo\n", view.panes_text)
        # codex has no window -> reported missing (sorted set of agent labels).
        self.assertEqual(("codex",), view.missing_agents)
        self.assertEqual(1, fake.capture_calls)

    def test_window_order_is_preserved_and_no_agent_missing(self) -> None:
        fake = _FakeStatusSession(
            exists=True,
            windows=["codex", "shell", "claude"],
            capture=(True, "rows"),
        )
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        # Non-agent windows are dropped; agent windows keep tmux window order.
        self.assertEqual(("codex", "claude"), view.agent_windows)
        self.assertEqual((), view.missing_agents)

    def test_present_session_without_agent_windows_skips_pane_capture(self) -> None:
        fake = _FakeStatusSession(exists=True, windows=["zsh"])
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertTrue(view.present)
        self.assertFalse(view.has_agent_windows)
        self.assertEqual((), view.agent_windows)
        self.assertEqual((), view.missing_agents)
        # Behavior-preserving: no list-panes read when there are no agent windows.
        self.assertEqual(0, fake.capture_calls)

    def test_failed_pane_capture_keeps_header_renderable(self) -> None:
        fake = _FakeStatusSession(
            exists=True, windows=["claude", "codex"], capture=(False, "")
        )
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertTrue(view.has_agent_windows)
        self.assertFalse(view.panes_ok)
        self.assertEqual("", view.panes_text)
        self.assertEqual((), view.missing_agents)
        self.assertEqual(1, fake.capture_calls)


if __name__ == "__main__":
    unittest.main()
