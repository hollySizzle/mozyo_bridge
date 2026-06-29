"""Fake-port specification for the status session-read + command-handler
boundary (Redmine #12825 / #12785).

#12785 migrated the status present/missing agent-window logic off the
``commands.*`` monkeypatch seam: the existence / window-enumeration /
pane-capture reads, once only exercisable by patching
``mozyo_bridge.application.commands.session_exists`` /
``commands.list_session_windows`` / ``commands.run_tmux`` and scraping stdout
(see ``test_mozyo_bridge`` ``test_cmd_status_*``), are injected through a fake
:class:`StatusSessionPort` so :class:`ResolveSessionStatusUseCase` is unit
tested with no patch and no real tmux.

#12825 extends that to the command handler. :class:`StatusCommandHandler`
composes the session-read use case, the cockpit-membership projection (over a
fake :class:`StatusCockpitMembershipPort`), and the pure
:func:`render_status_report` and returns a typed :class:`StatusReport`, so the
rendering / cockpit-projection behavior the broad ``test_cmd_status_*``
integration tests assert on by scraping stdout is now driven by fakes and
asserted on a returned string. The integration tests stay as end-to-end
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
    SessionStatusView,
    StatusCockpitMembershipPort,
    StatusCommandHandler,
    StatusCommandRequest,
    StatusQuery,
    StatusReport,
    render_status_report,
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


class _FakeMembership:
    """Minimal stand-in for ``WorkspaceMembership`` (only the rendered fields)."""

    def __init__(
        self,
        *,
        member,
        label="my_project",
        window="1",
        codex_pane="%2",
        claude_pane="%1",
        geometry_status="ok",
    ):
        self.member = member
        self.label = label
        self.window = window
        self.codex_pane = codex_pane
        self.claude_pane = claude_pane
        self.geometry_status = geometry_status


class _FakeMembershipPort:
    """Fake :class:`StatusCockpitMembershipPort`: a scripted projection result."""

    def __init__(self, membership):
        self._membership = membership

    def resolve(self):
        return self._membership


class RenderStatusReportTest(unittest.TestCase):
    """The pure renderer reproduces the procedural ``cmd_status`` stdout block."""

    def test_present_with_agent_table_and_missing_note(self) -> None:
        view = SessionStatusView(
            session="my_project",
            present=True,
            agent_windows=("claude",),
            has_agent_windows=True,
            panes_ok=True,
            panes_text="0\tclaude\t%1\t1\tclaude\t/repo\n",
            missing_agents=("codex",),
        )
        text = render_status_report(view, None)
        self.assertTrue(text.startswith("session: my_project\n"))
        self.assertIn("WINDOW\tNAME\tTARGET\tACTIVE\tPROCESS\tCWD\n", text)
        # panes_text is emitted raw (no doubled newline from the old end="" print).
        self.assertIn("0\tclaude\t%1\t1\tclaude\t/repo\n", text)
        self.assertNotIn("/repo\n\n  codex window missing", text)
        self.assertIn("  codex window missing; run `mozyo`", text)
        # No cockpit projection -> only the trailing blank line closes the block.
        self.assertNotIn("cockpit:", text)
        self.assertTrue(text.endswith("\n"))

    def test_present_without_agent_windows_emits_hint(self) -> None:
        view = SessionStatusView(session="agents", present=True, has_agent_windows=False)
        text = render_status_report(view, None)
        self.assertIn("no agent windows in this session", text)
        self.assertIn("mozyo-bridge init claude|codex", text)
        self.assertNotIn("WINDOW\tNAME", text)

    def test_missing_session(self) -> None:
        view = SessionStatusView(session="ghost", present=False)
        self.assertEqual("session: ghost (missing)\n\n", render_status_report(view, None))

    def test_cockpit_member_line(self) -> None:
        view = SessionStatusView(session="my_project", present=True, has_agent_windows=False)
        text = render_status_report(view, _FakeMembership(member=True))
        self.assertIn("cockpit: workspace 'my_project' IS loaded in cockpit", text)
        self.assertIn("codex=%2 claude=%1, geometry=ok", text)
        self.assertIn("display/liveness projection, not Redmine", text)

    def test_cockpit_non_member_line(self) -> None:
        view = SessionStatusView(session="my_project", present=True, has_agent_windows=False)
        text = render_status_report(view, _FakeMembership(member=False))
        self.assertIn("is NOT loaded in cockpit", text)
        self.assertIn("not cockpit membership", text)


class StatusCommandHandlerTest(unittest.TestCase):
    """The handler turns a typed request into a typed report over fakes."""

    def test_handle_composes_session_and_membership_into_report(self) -> None:
        sessions = _FakeStatusSession(
            exists=True,
            windows=["claude", "codex"],
            capture=(True, "0\tclaude\t%1\t1\tclaude\t/repo\n"),
        )
        handler = StatusCommandHandler(
            sessions=sessions,
            membership=_FakeMembershipPort(_FakeMembership(member=True)),
        )
        report = handler.handle(StatusCommandRequest(session="my_project"))
        self.assertIsInstance(report, StatusReport)
        self.assertIn("session: my_project\n", report.report_text)
        self.assertIn("0\tclaude\t%1\t1\tclaude\t/repo\n", report.report_text)
        self.assertIn("IS loaded in cockpit", report.report_text)
        # No agent is missing (both windows present) -> no missing note.
        self.assertNotIn("window missing", report.report_text)

    def test_handle_without_membership_port_omits_cockpit_lines(self) -> None:
        sessions = _FakeStatusSession(exists=True, windows=["zsh"])
        report = StatusCommandHandler(sessions=sessions).handle(
            StatusCommandRequest(session="agents")
        )
        self.assertIn("no agent windows in this session", report.report_text)
        self.assertNotIn("cockpit:", report.report_text)

    def test_handle_missing_session_with_absent_membership(self) -> None:
        sessions = _FakeStatusSession(exists=False)
        report = StatusCommandHandler(
            sessions=sessions,
            membership=_FakeMembershipPort(_FakeMembership(member=False)),
        ).handle(StatusCommandRequest(session="ghost"))
        self.assertIn("session: ghost (missing)\n", report.report_text)
        self.assertIn("is NOT loaded in cockpit", report.report_text)
        # A missing session never triggers a pane capture.
        self.assertEqual(0, sessions.capture_calls)


class StatusCockpitMembershipPortContractTest(unittest.TestCase):
    def test_fake_membership_port_satisfies_protocol(self) -> None:
        self.assertIsInstance(_FakeMembershipPort(None), StatusCockpitMembershipPort)


if __name__ == "__main__":
    unittest.main()
