"""Specs for the ``status`` herdr backend block (#13355).

Pure-renderer / fake-port tests: the herdr block is exercised with synthetic
:class:`HerdrInventoryView` values only. The load-bearing contract is tmux
byte-invariance — with no herdr view (the ``backend: tmux`` case) the rendered
report is byte-identical to the pre-#13355 output.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.application.commands_status import (
    SessionStatusView,
    StatusCommandHandler,
    StatusCommandRequest,
    StatusHerdrBackendPort,
    LiveStatusHerdrBackend,
    render_herdr_status_block,
    render_status_report,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    project_observed_agents,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_TRANSPORT_ERROR,
)


def _session_view() -> SessionStatusView:
    return SessionStatusView(
        session="mozyo-demo",
        present=True,
        agent_windows=("claude", "codex"),
        has_agent_windows=True,
        panes_ok=True,
        panes_text="0\tclaude\t%1\t1\tclaude\t/repo\n",
        missing_agents=(),
    )


def _ok_view() -> HerdrInventoryView:
    name = encode_assigned_name("ws-a", "claude", "")
    return HerdrInventoryView(
        backend_selected=True,
        ok=True,
        workspace_segment="ws-a",
        agents=project_observed_agents(
            [{"name": name, "agent_status": "working", "pane_id": "%5"}]
        ),
    )


class RenderStatusReportHerdrTest(unittest.TestCase):
    def test_default_none_is_byte_invariant(self) -> None:
        # The tmux-backend case: the two-arg call and an explicit None third
        # arg must produce the identical pre-#13355 report with no herdr line.
        view = _session_view()
        report = render_status_report(view, None)
        self.assertEqual(report, render_status_report(view, None, None))
        self.assertNotIn("herdr", report)

    def test_herdr_view_appends_the_backend_block(self) -> None:
        report = render_status_report(_session_view(), None, _ok_view())

        self.assertIn("herdr: backend selected; 1 managed agent(s)", report)
        self.assertIn("WORKSPACE\tLANE\tROLE\tAGENT_STATUS\tLOCATOR\tNAME", report)
        self.assertIn("ws-a\tdefault\tclaude\tbusy\t%5\t", report)
        self.assertIn("runtime observation, not Redmine workflow", report)
        # The block sits before the trailing blank line.
        self.assertTrue(report.endswith("\n\n"))

    def test_unreadable_inventory_renders_fail_closed_line(self) -> None:
        view = HerdrInventoryView(
            backend_selected=True,
            ok=False,
            reason=REASON_TRANSPORT_ERROR,
            detail="herdr agent list timed out",
        )
        block = render_herdr_status_block(view)

        self.assertIn("unreadable (fail-closed, transport_error)", block)
        self.assertIn("herdr agent list timed out", block)
        self.assertIn("mozyo-bridge doctor", block)
        self.assertNotIn("WORKSPACE\t", block)

    def test_unmanaged_rows_are_counted_not_listed(self) -> None:
        view = HerdrInventoryView(
            backend_selected=True,
            ok=True,
            workspace_segment="ws-a",
            agents=project_observed_agents(
                [{"name": "foreign", "agent_status": "idle"}]
            ),
        )
        block = render_herdr_status_block(view)
        self.assertIn("0 managed agent(s)", block)
        self.assertIn("1 unmanaged row(s) not shown", block)
        self.assertNotIn("foreign", block)


class FakeSessions:
    def session_exists(self, session: str) -> bool:
        return True

    def list_windows(self, session: str):
        return ["claude", "codex"]

    def capture_panes(self, session: str):
        return True, "0\tclaude\t%1\t1\tclaude\t/repo\n"


class FakeHerdrPort:
    def __init__(self, view):
        self._view = view
        self.calls = 0

    def resolve(self):
        self.calls += 1
        return self._view


class StatusCommandHandlerHerdrTest(unittest.TestCase):
    def test_handler_without_herdr_port_renders_no_block(self) -> None:
        handler = StatusCommandHandler(sessions=FakeSessions())
        report = handler.handle(StatusCommandRequest(session="mozyo-demo"))
        self.assertNotIn("herdr", report.report_text)

    def test_handler_threads_the_herdr_view_into_the_report(self) -> None:
        port = FakeHerdrPort(_ok_view())
        handler = StatusCommandHandler(sessions=FakeSessions(), herdr=port)

        report = handler.handle(StatusCommandRequest(session="mozyo-demo"))

        self.assertEqual(1, port.calls)
        self.assertIn("herdr: backend selected", report.report_text)

    def test_none_view_from_port_renders_no_block(self) -> None:
        port = FakeHerdrPort(None)
        handler = StatusCommandHandler(sessions=FakeSessions(), herdr=port)
        report = handler.handle(StatusCommandRequest(session="mozyo-demo"))
        self.assertNotIn("herdr", report.report_text)

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        import argparse

        adapter = LiveStatusHerdrBackend(argparse.Namespace(repo="/repo"))
        self.assertIsInstance(adapter, StatusHerdrBackendPort)

    def test_live_adapter_resolves_none_for_a_non_herdr_repo(self) -> None:
        import argparse
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            adapter = LiveStatusHerdrBackend(argparse.Namespace(repo=str(repo)))
            self.assertIsNone(adapter.resolve())


if __name__ == "__main__":
    unittest.main()
