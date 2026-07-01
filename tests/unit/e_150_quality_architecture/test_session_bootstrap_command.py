"""Fake-port / pure specifications for the session bootstrap boundary (#12975).

These exercise the ``session_bootstrap_command`` use case and pure projection
directly with a synthetic :class:`SessionBootstrapOps` — no real tmux server.
They pin:

- the pure projection (``project_session_window_names`` line parse,
  ``marker_visible_in`` raw / word-wrap / char-wrap normalizations,
  ``pane_command_basename`` extraction),
- the ``SessionBootstrapUseCase`` flows: the tolerant ``[]`` on a failed
  ``list-windows`` read, the pane-startup poll + timeout ``die``, the
  ``wait_for_text`` poll + fail-closed ``False``, the ``C-u`` rollback, and the
  ``ensure_repo_session_windows`` window-model orchestration (create-missing,
  config-load ordering, per-agent target + ready-wait + subtle style).

The end-to-end behavior over the live ``commands.*`` seams stays pinned by the
bare ``mozyo`` / launch / managed-event characterization tests; this file pins
the boundary in isolation.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application.session_bootstrap_command import (
    SessionBootstrapUseCase,
    marker_visible_in,
    pane_command_basename,
    project_session_window_names,
)


def _result(returncode: int = 0, stdout: str = "") -> argparse.Namespace:
    return argparse.Namespace(returncode=returncode, stdout=stdout)


class _FakeSessionBootstrapOps:
    """A synthetic :class:`SessionBootstrapOps` recording collaborator calls.

    Timing is deterministic: ``monotonic`` returns a monotonically increasing
    counter so the poll loops in ``wait_for_*`` terminate without real sleeps.
    """

    def __init__(
        self,
        *,
        run_result: argparse.Namespace | None = None,
        pane_infos: list[dict] | None = None,
        is_agent_results: list[bool] | None = None,
        captures: list[str] | None = None,
        session_exists_results: list[bool] | None = None,
        windows: list[str] | None = None,
        find_results: dict[str, dict | None] | None = None,
        new_session_pane: str = "%claude",
        new_window_panes: dict[str, str] | None = None,
    ) -> None:
        self._run_result = run_result if run_result is not None else _result()
        self._pane_infos = list(pane_infos or [])
        self._is_agent_results = list(is_agent_results or [])
        self._captures = list(captures or [])
        self._session_exists_results = list(session_exists_results or [])
        self._windows = list(windows or [])
        self._find_results = find_results or {}
        self._new_session_pane = new_session_pane
        self._new_window_panes = new_window_panes or {}
        self._clock = 0.0
        self.calls: list[tuple] = []
        self.died: str | None = None

    # --- reads / timing ---
    def run_tmux(self, *args, **kwargs):
        self.calls.append(("run_tmux", args, kwargs))
        return self._run_result

    def monotonic(self) -> float:
        self._clock += 1.0
        return self._clock

    def sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", seconds))

    def pane_info(self, target: str) -> dict:
        self.calls.append(("pane_info", target))
        return self._pane_infos.pop(0) if self._pane_infos else {}

    def is_agent_process(self, command: str) -> bool:
        self.calls.append(("is_agent_process", command))
        return self._is_agent_results.pop(0) if self._is_agent_results else False

    def capture_pane(self, target: str, lines: int) -> str:
        self.calls.append(("capture_pane", target, lines))
        return self._captures.pop(0) if self._captures else ""

    def die(self, message: str) -> None:
        self.died = message
        raise SystemExit(message)

    def run_keys(self, target: str, keys: list[str]) -> None:
        self.calls.append(("run_keys", target, tuple(keys)))

    # --- ensure_repo_session_windows collaborators ---
    def require_tmux(self) -> None:
        self.calls.append(("require_tmux",))

    def session_exists(self, session: str) -> bool:
        self.calls.append(("session_exists", session))
        return (
            self._session_exists_results.pop(0)
            if self._session_exists_results
            else False
        )

    def load_tmux_conf_for(self, args) -> bool:
        self.calls.append(("load_tmux_conf_for", args.session))
        return True

    def new_agent_session_window(self, agent, session, cwd) -> str:
        self.calls.append(("new_agent_session_window", agent, session, cwd))
        return self._new_session_pane

    def new_agent_window(self, agent, session, cwd) -> str:
        self.calls.append(("new_agent_window", agent, session, cwd))
        return self._new_window_panes.get(agent, f"%{agent}")

    def find_agent_window(self, agent, session) -> dict | None:
        self.calls.append(("find_agent_window", agent, session))
        return self._find_results.get(agent)

    def ensure_agent_target(self, pane, expected_agent, force) -> None:
        self.calls.append(("ensure_agent_target", pane.get("id"), expected_agent, force))

    def apply_window_subtle_style(self, session, window) -> bool:
        self.calls.append(("apply_window_subtle_style", session, window))
        return True

    def list_session_windows(self, session) -> list[str]:
        self.calls.append(("list_session_windows", session))
        return list(self._windows)

    def wait_for_agent_terminal_pane(self, pane_id, agent, timeout) -> None:
        self.calls.append(("wait_for_agent_terminal_pane", pane_id, agent, timeout))


class ProjectSessionWindowNamesTests(unittest.TestCase):
    def test_trims_and_drops_blank_lines(self) -> None:
        stdout = "claude\n  codex  \n\n   \nzsh\n"
        self.assertEqual(
            ["claude", "codex", "zsh"], project_session_window_names(stdout)
        )

    def test_empty_input(self) -> None:
        self.assertEqual([], project_session_window_names(""))
        self.assertEqual([], project_session_window_names(None))  # type: ignore[arg-type]


class MarkerVisibleInTests(unittest.TestCase):
    def test_raw_substring(self) -> None:
        self.assertTrue(marker_visible_in("prefix [mark] suffix", "[mark]"))

    def test_word_boundary_wrap(self) -> None:
        # A whitespace-bearing marker wrapped at a space: `\n\s+` -> ` `.
        captured = "[mozyo-bridge from:claude\n    pane:%110 at:x]"
        self.assertTrue(
            marker_visible_in(captured, "[mozyo-bridge from:claude pane:%110 at:x]")
        )

    def test_character_wrap(self) -> None:
        # A whitespace-free marker wrapped at an arbitrary char: `\n\s+` -> ``.
        captured = "[mozyo:handoff:source=asana:task=1\n   :kind=x:to=claude]"
        self.assertTrue(
            marker_visible_in(captured, "[mozyo:handoff:source=asana:task=1:kind=x:to=claude]")
        )

    def test_absent_marker_fails_closed(self) -> None:
        self.assertFalse(marker_visible_in("nothing here", "[mark]"))


class PaneCommandBasenameTests(unittest.TestCase):
    def test_absolute_command(self) -> None:
        self.assertEqual("claude", pane_command_basename({"command": "/usr/bin/claude"}))

    def test_missing_command_reads_empty(self) -> None:
        self.assertEqual("", pane_command_basename({}))
        self.assertEqual("", pane_command_basename({"command": None}))


class ListSessionWindowsUseCaseTests(unittest.TestCase):
    def test_parses_names_on_success(self) -> None:
        ops = _FakeSessionBootstrapOps(run_result=_result(0, "claude\ncodex\n"))
        self.assertEqual(
            ["claude", "codex"],
            SessionBootstrapUseCase(ops).list_session_windows("s"),
        )

    def test_failed_read_degrades_to_empty(self) -> None:
        ops = _FakeSessionBootstrapOps(run_result=_result(1, "junk"))
        self.assertEqual([], SessionBootstrapUseCase(ops).list_session_windows("s"))


class WaitForAgentTerminalPaneUseCaseTests(unittest.TestCase):
    def test_returns_when_agent_process_present(self) -> None:
        ops = _FakeSessionBootstrapOps(
            pane_infos=[{"command": "/bin/zsh"}, {"command": "/usr/bin/claude"}],
            is_agent_results=[False, True],
        )
        SessionBootstrapUseCase(ops).wait_for_agent_terminal_pane("%1", "claude", 100.0)
        self.assertIsNone(ops.died)
        self.assertIn(("is_agent_process", "claude"), ops.calls)

    def test_times_out_and_dies(self) -> None:
        # timeout below the monotonic step (1.0) so the loop body never runs.
        ops = _FakeSessionBootstrapOps(is_agent_results=[])
        with self.assertRaises(SystemExit):
            SessionBootstrapUseCase(ops).wait_for_agent_terminal_pane(
                "%1", "codex", 0.5
            )
        self.assertEqual(
            "timed out waiting for codex pane startup: %1", ops.died
        )


class WaitForTextUseCaseTests(unittest.TestCase):
    def test_returns_true_on_marker(self) -> None:
        ops = _FakeSessionBootstrapOps(captures=["...[mark]..."])
        self.assertTrue(
            SessionBootstrapUseCase(ops).wait_for_text("%2", "[mark]", 200, 100.0)
        )

    def test_fails_closed_on_timeout(self) -> None:
        ops = _FakeSessionBootstrapOps(captures=[])
        self.assertFalse(
            SessionBootstrapUseCase(ops).wait_for_text("%2", "[mark]", 200, 0.5)
        )


class RollbackUnsubmittedInputUseCaseTests(unittest.TestCase):
    def test_issues_ctrl_u(self) -> None:
        ops = _FakeSessionBootstrapOps()
        SessionBootstrapUseCase(ops).rollback_unsubmitted_input("%3")
        self.assertIn(("run_keys", "%3", ("C-u",)), ops.calls)


class EnsureRepoSessionWindowsUseCaseTests(unittest.TestCase):
    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            session="repo",
            cwd="/repo",
            config=False,
            force=False,
            ready_timeout=0,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_creates_session_and_missing_windows(self) -> None:
        # session absent: create claude session window, then create the codex
        # window (only claude window exists), then target both.
        ops = _FakeSessionBootstrapOps(
            session_exists_results=[False],
            windows=["claude"],
            find_results={"claude": {"id": "%c"}, "codex": {"id": "%x"}},
            new_session_pane="%c",
            new_window_panes={"codex": "%x"},
        )
        created = SessionBootstrapUseCase(ops).ensure_repo_session_windows(self._args())
        self.assertEqual(["claude:%c", "codex:%x"], created)
        # both agents targeted; subtle style applied per agent.
        self.assertIn(("ensure_agent_target", "%c", "claude", False), ops.calls)
        self.assertIn(("ensure_agent_target", "%x", "codex", False), ops.calls)
        self.assertIn(("apply_window_subtle_style", "repo", "claude"), ops.calls)

    def test_existing_session_creates_no_windows(self) -> None:
        # session present with both windows: nothing new created.
        ops = _FakeSessionBootstrapOps(
            session_exists_results=[True, True],
            windows=["claude", "codex"],
            find_results={"claude": {"id": "%c"}, "codex": {"id": "%x"}},
        )
        created = SessionBootstrapUseCase(ops).ensure_repo_session_windows(self._args())
        self.assertEqual([], created)
        self.assertFalse([c for c in ops.calls if c[0] == "new_agent_session_window"])
        self.assertFalse([c for c in ops.calls if c[0] == "new_agent_window"])

    def test_config_loads_conf_for_existing_session(self) -> None:
        ops = _FakeSessionBootstrapOps(
            session_exists_results=[True, True],
            windows=["claude", "codex"],
            find_results={"claude": {"id": "%c"}, "codex": {"id": "%x"}},
        )
        SessionBootstrapUseCase(ops).ensure_repo_session_windows(
            self._args(config=True)
        )
        # config + existing session -> load conf once, before window creation.
        self.assertEqual(
            1, len([c for c in ops.calls if c[0] == "load_tmux_conf_for"])
        )

    def test_ready_timeout_waits_for_pane(self) -> None:
        ops = _FakeSessionBootstrapOps(
            session_exists_results=[True, True],
            windows=["claude", "codex"],
            find_results={"claude": {"id": "%c"}, "codex": {"id": "%x"}},
        )
        SessionBootstrapUseCase(ops).ensure_repo_session_windows(
            self._args(ready_timeout=5.0)
        )
        self.assertIn(
            ("wait_for_agent_terminal_pane", "%c", "claude", 5.0), ops.calls
        )


if __name__ == "__main__":
    unittest.main()
