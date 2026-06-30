"""Fake-port / pure-policy specifications for the doctor tmux pane-health
boundary (#12881).

These exercise the ``doctor_tmux`` verdict authority and tmux read port directly
with a synthetic read-view — without a real tmux server, without a real pane
topology, without touching ``TMUX_PANE`` or the ``.claude/skills`` checkout
probe. They are the tmux-server-topology -> fake-port / fake-policy migration for
the ``tmux`` section, and they pin the legacy ``doctor_tmux_section`` dict shape,
statuses, warnings, and ``next_action`` wording.
"""

from __future__ import annotations

import argparse
import types
import unittest
from typing import Any
from unittest import mock

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_tmux import (
    LiveTmuxPaneHealthReads,
    TmuxPaneHealthReads,
    TmuxSectionUseCase,
    evaluate_tmux_section,
)


def _pane(
    pane_id: str,
    *,
    session: str = "main",
    window: str = "0",
    pane_index: str = "0",
    window_name: str = "",
    command: str = "",
    cwd: str = "",
    active: str = "1",
) -> dict[str, str]:
    return {
        "id": pane_id,
        "location": f"{session}:{window}.{pane_index}",
        "window_name": window_name,
        "command": command,
        "cwd": cwd,
        "pane_active": active,
    }


def _connected_view(
    panes: list[dict[str, str]],
    *,
    tmux_pane: str = "%1",
    repo_root: str = "/repo",
    project_skills_dir_exists: bool = True,
    panes_total: int | None = None,
) -> dict[str, Any]:
    return {
        "tmux_pane": tmux_pane,
        "tmux_installed": True,
        "tmux_server_connected": True,
        "panes_total": len(panes) if panes_total is None else panes_total,
        "panes": panes,
        "repo_root": repo_root,
        "project_skills_dir_exists": project_skills_dir_exists,
    }


def _healthy_panes() -> list[dict[str, str]]:
    """A current pane plus an ok claude window and an ok codex window."""
    return [
        _pane("%1", session="main", window="0", window_name="shell", command="zsh"),
        _pane(
            "%2",
            session="main",
            window="1",
            window_name="claude",
            command="/usr/bin/claude",
            cwd="/repo",
        ),
        _pane(
            "%3",
            session="main",
            window="2",
            window_name="codex",
            command="codex",
            cwd="/repo",
        ),
    ]


class FakeTmuxPaneHealthReads:
    """In-memory fake of the ``TmuxPaneHealthReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateTmuxSectionEarlyReturnTest(unittest.TestCase):
    def test_tmux_not_installed_is_missing(self) -> None:
        section = evaluate_tmux_section(
            {"tmux_pane": "", "tmux_installed": False}
        )
        self.assertEqual(
            {
                "status": "missing",
                "next_action": [
                    "install tmux to use mozyo-bridge pane notifications"
                ],
                "tmux_pane": "",
                "detail": "tmux not installed",
            },
            section,
        )

    def test_no_tmux_server_is_skipped(self) -> None:
        section = evaluate_tmux_section(
            {
                "tmux_pane": "%9",
                "tmux_installed": True,
                "tmux_server_connected": False,
            }
        )
        self.assertEqual("skipped", section["status"])
        self.assertEqual(
            "not connected to a tmux server "
            "(run `mozyo` to start the repo session)",
            section["detail"],
        )
        self.assertEqual([], section["next_action"])
        # The probed pane id is still surfaced for the report.
        self.assertEqual("%9", section["tmux_pane"])
        # No pane-topology keys leak into the early-return dict.
        self.assertNotIn("agent_windows", section)


class EvaluateTmuxSectionVerdictTest(unittest.TestCase):
    def test_all_agents_ok_is_ok(self) -> None:
        section = evaluate_tmux_section(_connected_view(_healthy_panes()))
        self.assertEqual("ok", section["status"])
        self.assertEqual([], section["next_action"])
        self.assertEqual([], section["warnings"])
        self.assertEqual("main", section["current_session"])
        self.assertEqual(3, section["panes_total"])
        self.assertEqual(
            {"status": "ok", "session": "main", "window": "1", "id": "%2",
             "process": "claude", "cwd": "/repo"},
            section["agent_windows"]["claude"],
        )
        self.assertEqual("ok", section["agent_windows"]["codex"]["status"])

    def test_missing_agent_window_is_warning(self) -> None:
        # Only a claude window in the session; codex is missing.
        panes = [
            _pane("%1", window="0", window_name="shell", command="zsh"),
            _pane(
                "%2",
                window="1",
                window_name="claude",
                command="claude",
                cwd="/repo",
            ),
        ]
        section = evaluate_tmux_section(_connected_view(panes))
        self.assertEqual("warning", section["status"])
        self.assertEqual(
            {"status": "missing", "session": "main"},
            section["agent_windows"]["codex"],
        )
        self.assertEqual(
            [
                "run `mozyo` from the repo, or `mozyo-bridge init codex` from the "
                "pane you want to be `codex`"
            ],
            section["next_action"],
        )

    def test_duplicate_windows_is_warning(self) -> None:
        panes = [
            _pane("%1", window="0", window_name="shell", command="zsh"),
            _pane("%2", window="1", window_name="claude", command="claude",
                  cwd="/repo"),
            _pane("%4", window="3", window_name="claude", command="claude",
                  cwd="/repo"),
            _pane("%3", window="2", window_name="codex", command="codex",
                  cwd="/repo"),
        ]
        section = evaluate_tmux_section(_connected_view(panes))
        self.assertEqual("warning", section["status"])
        self.assertEqual(
            {
                "status": "duplicate",
                "session": "main",
                "windows": ["1", "3"],
            },
            section["agent_windows"]["claude"],
        )
        self.assertEqual(
            [
                "resolve duplicate `claude` windows in session 'main'; "
                "tmux tolerates duplicates but the resolver does not"
            ],
            section["next_action"],
        )

    def test_not_agent_process_is_warning(self) -> None:
        panes = [
            _pane("%1", window="0", window_name="shell", command="zsh"),
            _pane("%2", window="1", window_name="claude", command="bash",
                  cwd="/repo"),
            _pane("%3", window="2", window_name="codex", command="codex",
                  cwd="/repo"),
        ]
        section = evaluate_tmux_section(_connected_view(panes))
        self.assertEqual("warning", section["status"])
        self.assertEqual(
            "not-agent-process", section["agent_windows"]["claude"]["status"]
        )
        self.assertEqual("bash", section["agent_windows"]["claude"]["process"])
        self.assertEqual(
            [
                "`claude` window in session 'main' is running `bash`; start the "
                "agent CLI or `mozyo-bridge init claude` on the pane that is"
            ],
            section["next_action"],
        )

    def test_unscoped_when_current_pane_absent(self) -> None:
        # TMUX_PANE points at a pane that is not in the snapshot -> the resolver
        # cannot scope to a session, so every agent window is `unscoped` and the
        # section stays `ok` (cross-session panes are legitimate).
        section = evaluate_tmux_section(
            _connected_view(_healthy_panes(), tmux_pane="%999")
        )
        self.assertEqual("ok", section["status"])
        self.assertEqual("", section["current_session"])
        self.assertEqual(
            {"status": "unscoped"}, section["agent_windows"]["claude"]
        )
        self.assertEqual(
            {"status": "unscoped"}, section["agent_windows"]["codex"]
        )
        self.assertEqual([], section["next_action"])

    def test_unscoped_when_tmux_pane_unset(self) -> None:
        section = evaluate_tmux_section(
            _connected_view(_healthy_panes(), tmux_pane="")
        )
        self.assertEqual("ok", section["status"])
        self.assertEqual("", section["current_session"])
        self.assertEqual("unscoped", section["agent_windows"]["codex"]["status"])

    def test_cross_session_agent_windows_are_ignored(self) -> None:
        # An ok claude/codex pair lives in `main`; a broken claude in `other`
        # session must not taint the current-session verdict.
        panes = _healthy_panes() + [
            _pane("%8", session="other", window="1", window_name="claude",
                  command="bash", cwd="/elsewhere"),
        ]
        section = evaluate_tmux_section(_connected_view(panes))
        self.assertEqual("ok", section["status"])
        self.assertEqual([], section["next_action"])


class EvaluateTmuxSectionClaudeCwdWarningTest(unittest.TestCase):
    def _claude_cwd_panes(self, cwd: str) -> list[dict[str, str]]:
        return [
            _pane("%1", window="0", window_name="shell", command="zsh"),
            _pane("%2", window="1", window_name="claude", command="claude",
                  cwd=cwd),
            _pane("%3", window="2", window_name="codex", command="codex",
                  cwd="/repo"),
        ]

    def test_claude_cwd_outside_repo_raises_warning(self) -> None:
        section = evaluate_tmux_section(
            _connected_view(
                self._claude_cwd_panes("/somewhere/else"),
                repo_root="/repo",
                project_skills_dir_exists=True,
            )
        )
        self.assertEqual("warning", section["status"])
        self.assertEqual(
            [
                {
                    "kind": "claude_pane_cwd_outside_repo",
                    "cwd": "/somewhere/else",
                    "repo": "/repo",
                }
            ],
            section["warnings"],
        )
        # The claude window itself is still `ok` (the warning is advisory).
        self.assertEqual("ok", section["agent_windows"]["claude"]["status"])

    def test_claude_cwd_under_repo_no_warning(self) -> None:
        section = evaluate_tmux_section(
            _connected_view(
                self._claude_cwd_panes("/repo/subdir"),
                repo_root="/repo",
                project_skills_dir_exists=True,
            )
        )
        self.assertEqual("ok", section["status"])
        self.assertEqual([], section["warnings"])

    def test_no_warning_when_project_has_no_skills_dir(self) -> None:
        # The warning is gated on a project `.claude/skills` directory existing;
        # without it an outside-repo cwd is not flagged.
        section = evaluate_tmux_section(
            _connected_view(
                self._claude_cwd_panes("/somewhere/else"),
                repo_root="/repo",
                project_skills_dir_exists=False,
            )
        )
        self.assertEqual("ok", section["status"])
        self.assertEqual([], section["warnings"])


class TmuxSectionUseCaseTest(unittest.TestCase):
    def test_use_case_returns_legacy_section_dict(self) -> None:
        reads = FakeTmuxPaneHealthReads(_connected_view(_healthy_panes()))
        section = TmuxSectionUseCase(reads).execute()
        self.assertEqual("ok", section["status"])
        self.assertEqual(
            ["status", "next_action", "tmux_pane", "panes_total",
             "agent_windows", "warnings", "current_session"],
            list(section.keys()),
        )
        self.assertEqual(1, reads.calls)

    def test_use_case_propagates_missing(self) -> None:
        reads = FakeTmuxPaneHealthReads(
            {"tmux_pane": "", "tmux_installed": False}
        )
        section = TmuxSectionUseCase(reads).execute()
        self.assertEqual("missing", section["status"])
        self.assertEqual(1, reads.calls)

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(
            LiveTmuxPaneHealthReads(argparse.Namespace(repo=".")),
            TmuxPaneHealthReads,
        )


class LiveTmuxPaneHealthReadsTest(unittest.TestCase):
    """The live adapter reproduces the legacy collector's external reads and
    its short-circuits (no ``pane_lines`` call before the server is proven). It
    resolves ``subprocess`` / ``run_tmux`` / ``pane_lines`` through the
    ``doctor`` module at call time, so the reads are patched there."""

    def _patch_tmux_available(self, available: bool):
        return mock.patch(
            "mozyo_bridge.application.doctor.subprocess.run",
            return_value=types.SimpleNamespace(returncode=0 if available else 1),
        )

    def test_not_installed_short_circuits_before_tmux_calls(self) -> None:
        run_tmux = mock.Mock()
        pane_lines = mock.Mock()
        with self._patch_tmux_available(False), \
            mock.patch.object(doctor, "run_tmux", run_tmux), \
            mock.patch.object(doctor, "pane_lines", pane_lines), \
            mock.patch.dict("os.environ", {"TMUX_PANE": "%5"}, clear=False):
            view = LiveTmuxPaneHealthReads(
                argparse.Namespace(repo=".")
            ).describe()
        self.assertEqual(
            {"tmux_pane": "%5", "tmux_installed": False}, view
        )
        run_tmux.assert_not_called()
        pane_lines.assert_not_called()

    def test_no_server_short_circuits_before_pane_lines(self) -> None:
        run_tmux = mock.Mock(
            return_value=types.SimpleNamespace(returncode=1, stdout="")
        )
        pane_lines = mock.Mock()
        with self._patch_tmux_available(True), \
            mock.patch.object(doctor, "run_tmux", run_tmux), \
            mock.patch.object(doctor, "pane_lines", pane_lines), \
            mock.patch.dict("os.environ", {"TMUX_PANE": "%5"}, clear=False):
            view = LiveTmuxPaneHealthReads(
                argparse.Namespace(repo=".")
            ).describe()
        self.assertEqual(
            {
                "tmux_pane": "%5",
                "tmux_installed": True,
                "tmux_server_connected": False,
            },
            view,
        )
        # The connection probe ran, but pane_lines must not (it would `die`).
        run_tmux.assert_called_once()
        pane_lines.assert_not_called()

    def test_connected_reads_panes_and_resolves_repo(self) -> None:
        panes = _healthy_panes()
        run_tmux = mock.Mock(
            return_value=types.SimpleNamespace(
                returncode=0, stdout="%1\n%2\n%3\n"
            )
        )
        pane_lines = mock.Mock(return_value=panes)
        with self._patch_tmux_available(True), \
            mock.patch.object(doctor, "run_tmux", run_tmux), \
            mock.patch.object(doctor, "pane_lines", pane_lines), \
            mock.patch.dict("os.environ", {"TMUX_PANE": "%1"}, clear=False):
            view = LiveTmuxPaneHealthReads(
                argparse.Namespace(repo="/tmp")
            ).describe()
        self.assertTrue(view["tmux_installed"])
        self.assertTrue(view["tmux_server_connected"])
        self.assertEqual(3, view["panes_total"])
        self.assertIs(panes, view["panes"])
        self.assertEqual("%1", view["tmux_pane"])
        # repo resolved + skills probe computed without raising.
        self.assertTrue(view["repo_root"].endswith("/tmp"))
        self.assertIn("project_skills_dir_exists", view)
        pane_lines.assert_called_once()


if __name__ == "__main__":
    unittest.main()
