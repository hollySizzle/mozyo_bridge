"""Fake-port / use-case specifications for the init command boundary (#12926).

These exercise the ``init_command`` use case and pure policy directly with a
synthetic :class:`InitWorkspaceOps` — no real tmux server, no workspace
registry, no VS Code settings file. They pin:

- the request value object (:class:`InitRequest.from_args`, including the
  ``window_only`` / ``no_vscode_settings`` defaults),
- the exact fail-closed ``die`` message wording of each pure policy helper,
- the use-case walk: window-only success and its conflict refusal, the
  smart-adoption preflight refusals (label target, invalid pane id,
  unparseable location, unconfident root, conflict, meaningful-foreign
  session, expected-session collision), and the happy-path mutation order
  (registry -> vscode -> session rename -> window rename -> style -> markers).

The end-to-end behavior over the real tmux / registry / vscode helpers stays
pinned by the ``cmd_init`` characterization tests in
``tests/integration/.../test_mozyo_bridge.py``; this file pins the boundary in
isolation, which is the OOP-first carve's payoff — the policy is now
exercisable without patching the live side effects.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mozyo_bridge.application.init_command import (
    InitOutcome,
    InitRequest,
    InitUseCase,
    InitWorkspaceOps,
    LiveInitWorkspaceOps,
    _agent_window_conflict,
    _confident_workspace_root,
    _is_fallback_session_name,
    _write_vscode_session_name,
    agent_window_conflict_message,
    expected_session_exists_message,
    foreign_session_message,
    unconfident_root_message,
)
from mozyo_bridge.workspace_registry import SOURCE_HOME_REGISTRY


@dataclass
class _Expected:
    name: str = "mozyo-repo"
    workspace_id: str = "ws-anchor"
    source: str = "path_derivation"


@dataclass
class _RegRecord:
    canonical_session: str = "mozyo-repo"
    workspace_id: str = "ws-registered"
    project_name: str = "repo"


@dataclass
class _Registration:
    record: _RegRecord = field(default_factory=_RegRecord)
    outcome: str = "created"


@dataclass
class _FakeInitWorkspaceOps:
    """Synthetic :class:`InitWorkspaceOps`: every read is a fixed return.

    The mutation calls (``register`` / ``vscode`` / ``rename_session`` /
    ``rename_window`` / ``style`` / ``markers``) are recorded in ``calls`` in
    invocation order so a test can assert both *that* a mutation ran and the
    sequence it ran in. ``bind_agent_pane_markers`` appends a note like the live
    helper does so the note plumbing is exercised.
    """

    panes: list[dict] = field(default_factory=list)
    pane_id: str | None = "%5"
    target_is_tmux: bool = True
    location: str = "mozyo-repo:1.0"
    conflicts: list[str] = field(default_factory=list)
    root: Path | None = Path("/repo")
    expected: _Expected = field(default_factory=_Expected)
    fallback: bool = True
    session_present: bool = False
    registration: _Registration = field(default_factory=_Registration)
    vscode_result: tuple[Path, bool, str | None] = (
        Path("/repo/.vscode/settings.json"),
        True,
        None,
    )
    current_pane_id: str = "%9"
    calls: list[tuple] = field(default_factory=list)

    def require_tmux(self) -> None:
        self.calls.append(("require_tmux",))

    def current_pane(self) -> str:
        return self.current_pane_id

    def is_tmux_target(self, raw_target: str) -> bool:
        return self.target_is_tmux

    def resolve_pane_id(self, raw_target: str) -> str | None:
        return self.pane_id

    def pane_location(self, target: str) -> str:
        return self.location

    def pane_lines(self) -> list[dict]:
        return self.panes

    def agent_window_conflict(
        self, panes: list[dict], session: str, skip_window_index: str, agent: str
    ) -> list[str]:
        return self.conflicts

    def confident_root(self, cwd: str) -> Path | None:
        return self.root

    def canonical_session(self, root: Path) -> Any:
        return self.expected

    def is_fallback_session(self, name: str) -> bool:
        return self.fallback

    def session_exists(self, name: str) -> bool:
        return self.session_present

    def register_workspace(self, root: Path) -> Any:
        self.calls.append(("register", str(root)))
        return self.registration

    def write_vscode_session_name(
        self, root: Path, session_name: str
    ) -> tuple[Path, bool, str | None]:
        self.calls.append(("vscode", str(root), session_name))
        return self.vscode_result

    def rename_session(self, old: str, new: str) -> None:
        self.calls.append(("rename_session", old, new))

    def rename_window(self, window_target: str, name: str) -> None:
        self.calls.append(("rename_window", window_target, name))

    def apply_window_subtle_style(self, session: str, agent: str) -> None:
        self.calls.append(("style", session, agent))

    def bind_agent_pane_markers(
        self, target: str, agent: str, workspace_id: str | None, notes: list[str]
    ) -> None:
        self.calls.append(("markers", target, agent, workspace_id))
        notes.append(f"bound role marker @mozyo_agent_role={agent} on {target}")


def _request(**overrides: Any) -> InitRequest:
    base = dict(agent="claude", target_arg="%5", window_only=False, no_vscode_settings=False)
    base.update(overrides)
    return InitRequest(**base)  # type: ignore[arg-type]


def _kinds(ops: _FakeInitWorkspaceOps) -> list[str]:
    return [call[0] for call in ops.calls]


class InitRequestTest(unittest.TestCase):
    def test_from_args_reads_all_fields(self) -> None:
        args = argparse.Namespace(
            agent="codex", target="%7", window_only=True, no_vscode_settings=True
        )
        request = InitRequest.from_args(args)
        self.assertEqual(
            ("codex", "%7", True, True),
            (request.agent, request.target_arg, request.window_only, request.no_vscode_settings),
        )

    def test_from_args_defaults_missing_flags_to_false(self) -> None:
        args = argparse.Namespace(agent="claude", target=None)
        request = InitRequest.from_args(args)
        self.assertIsNone(request.target_arg)
        self.assertFalse(request.window_only)
        self.assertFalse(request.no_vscode_settings)


class InitOutcomeTest(unittest.TestCase):
    def test_refused_carries_message(self) -> None:
        outcome = InitOutcome.refused("nope")
        self.assertTrue(outcome.is_refused)
        self.assertEqual("nope", outcome.refused_message)
        self.assertIsNone(outcome.success_line)

    def test_completed_is_not_refused_and_freezes_notes(self) -> None:
        notes = ["a"]
        outcome = InitOutcome.completed("done", notes, warnings=["w"])
        notes.append("mutated-after")  # frozen snapshot must not see this
        self.assertFalse(outcome.is_refused)
        self.assertEqual("done", outcome.success_line)
        self.assertEqual(("a",), outcome.notes)
        self.assertEqual(("w",), outcome.warnings)


class InitPolicyMessageTest(unittest.TestCase):
    def test_conflict_message_none_when_no_conflict(self) -> None:
        self.assertIsNone(agent_window_conflict_message([], "s", "claude", "%5"))

    def test_conflict_message_dedupes_and_sorts(self) -> None:
        message = agent_window_conflict_message(
            ["s:2(%9)", "s:0(%1)", "s:0(%1)"], "s", "claude", "%5"
        )
        assert message is not None
        self.assertIn("s:0(%1), s:2(%9)", message)
        self.assertIn("already has a window named 'claude'", message)

    def test_unconfident_root_message_preserves_explicit_target_in_suffix(self) -> None:
        message = unconfident_root_message("%5", "/bare", "claude", "%5")
        self.assertIn("cannot confidently determine", message)
        self.assertIn("mozyo-bridge init claude %5 --window-only", message)

    def test_unconfident_root_message_drops_suffix_for_implicit_target(self) -> None:
        message = unconfident_root_message("%5", "", "claude", None)
        self.assertIn("(unknown)", message)
        self.assertIn("mozyo-bridge init claude --window-only", message)

    def test_foreign_session_message_names_current_and_expected(self) -> None:
        message = foreign_session_message("%5", "human-work", "mozyo-repo", "claude", "%5")
        self.assertIn("human-work", message)
        self.assertIn("mozyo-repo", message)
        self.assertIn("mozyo-bridge init claude %5 --window-only", message)

    def test_expected_exists_message_suggests_attach(self) -> None:
        message = expected_session_exists_message("%5", "____", "mozyo-repo", "claude", None)
        self.assertIn("already exists", message)
        self.assertIn("tmux attach -t mozyo-repo", message)


class InitUseCaseWindowOnlyTest(unittest.TestCase):
    def test_window_only_renames_window_and_styles_only(self) -> None:
        ops = _FakeInitWorkspaceOps(location="agents:3.0")
        outcome = InitUseCase(ops).run(_request(window_only=True, no_vscode_settings=True))
        self.assertFalse(outcome.is_refused)
        self.assertEqual(
            "initialized %5 as claude (renamed window agents:3 -> claude; window-only)",
            outcome.success_line,
        )
        self.assertEqual(
            [("require_tmux",), ("rename_window", "agents:3", "claude"), ("style", "agents", "claude")],
            ops.calls,
        )

    def test_window_only_refuses_on_same_session_conflict(self) -> None:
        ops = _FakeInitWorkspaceOps(location="agents:3.0", conflicts=["agents:0(%1)"])
        outcome = InitUseCase(ops).run(_request(window_only=True))
        self.assertTrue(outcome.is_refused)
        assert outcome.refused_message is not None
        self.assertIn("agents:0(%1)", outcome.refused_message)
        self.assertNotIn("rename_window", _kinds(ops))


class InitUseCaseTargetResolutionTest(unittest.TestCase):
    def test_label_target_is_refused(self) -> None:
        ops = _FakeInitWorkspaceOps(target_is_tmux=False)
        outcome = InitUseCase(ops).run(_request(target_arg="claude"))
        self.assertTrue(outcome.is_refused)
        assert outcome.refused_message is not None
        self.assertIn("not a label: claude", outcome.refused_message)

    def test_unresolvable_pane_id_is_refused(self) -> None:
        ops = _FakeInitWorkspaceOps(pane_id=None)
        outcome = InitUseCase(ops).run(_request(target_arg="%99"))
        self.assertTrue(outcome.is_refused)
        assert outcome.refused_message is not None
        self.assertIn("invalid tmux target: %99", outcome.refused_message)

    def test_unparseable_location_is_refused(self) -> None:
        ops = _FakeInitWorkspaceOps(location="nonsense")
        outcome = InitUseCase(ops).run(_request())
        self.assertTrue(outcome.is_refused)
        assert outcome.refused_message is not None
        self.assertIn("could not parse tmux location", outcome.refused_message)

    def test_implicit_target_uses_current_pane(self) -> None:
        ops = _FakeInitWorkspaceOps(current_pane_id="%9", target_is_tmux=False)
        # is_tmux_target(False) short-circuits, but the label echoed is the
        # current pane id, proving the fallback resolved it.
        outcome = InitUseCase(ops).run(_request(target_arg=None))
        assert outcome.refused_message is not None
        self.assertIn("not a label: %9", outcome.refused_message)


class InitUseCaseSmartPathTest(unittest.TestCase):
    def _panes(self) -> list[dict]:
        return [{"id": "%5", "location": "____:1.0", "cwd": "/repo"}]

    def test_full_adoption_registers_pins_renames_styles_and_marks(self) -> None:
        ops = _FakeInitWorkspaceOps(
            location="____:1.0",
            panes=self._panes(),
            expected=_Expected(name="mozyo-repo", source="path_derivation"),
            fallback=True,
            session_present=False,
        )
        outcome = InitUseCase(ops).run(_request())
        self.assertFalse(outcome.is_refused)
        self.assertEqual(
            "adopted %5 into session 'mozyo-repo' as claude", outcome.success_line
        )
        # Mutation order is the legacy order: register -> vscode -> session
        # rename -> window rename -> style -> markers.
        self.assertEqual(
            ["require_tmux", "register", "vscode", "rename_session", "rename_window", "style", "markers"],
            _kinds(ops),
        )
        # The registered workspace id (not the anchor id) is stamped on the pane.
        self.assertEqual(("markers", "%5", "claude", "ws-registered"), ops.calls[-1])
        self.assertIn(
            "registered workspace 'repo' (created; session 'mozyo-repo')", outcome.notes
        )
        self.assertIn(
            'pinned tmux-integrated.sessionName="mozyo-repo" in /repo/.vscode/settings.json',
            outcome.notes,
        )
        self.assertIn("renamed session '____' -> 'mozyo-repo'", outcome.notes)

    def test_already_registered_source_skips_registration(self) -> None:
        ops = _FakeInitWorkspaceOps(
            location="mozyo-repo:1.0",
            panes=[{"id": "%5", "location": "mozyo-repo:1.0", "cwd": "/repo"}],
            expected=_Expected(name="mozyo-repo", workspace_id="ws-anchor", source=SOURCE_HOME_REGISTRY),
        )
        outcome = InitUseCase(ops).run(_request(no_vscode_settings=True))
        self.assertFalse(outcome.is_refused)
        self.assertNotIn("register", _kinds(ops))
        self.assertNotIn("rename_session", _kinds(ops))  # already in expected session
        # The anchor workspace id is stamped when no registration happened.
        self.assertEqual(("markers", "%5", "claude", "ws-anchor"), ops.calls[-1])

    def test_no_vscode_settings_skips_the_settings_write(self) -> None:
        ops = _FakeInitWorkspaceOps(
            location="____:1.0", panes=self._panes(), expected=_Expected(source=SOURCE_HOME_REGISTRY)
        )
        InitUseCase(ops).run(_request(no_vscode_settings=True))
        self.assertNotIn("vscode", _kinds(ops))

    def test_vscode_warning_is_surfaced_and_blocks_the_pin_note(self) -> None:
        ops = _FakeInitWorkspaceOps(
            location="____:1.0",
            panes=self._panes(),
            expected=_Expected(source=SOURCE_HOME_REGISTRY),
            vscode_result=(Path("/repo/.vscode/settings.json"), False, "not plain JSON"),
        )
        outcome = InitUseCase(ops).run(_request())
        self.assertIn("not plain JSON", outcome.warnings)
        self.assertFalse(any("pinned" in note for note in outcome.notes))

    def test_unconfident_root_refuses_before_any_mutation(self) -> None:
        ops = _FakeInitWorkspaceOps(location="____:1.0", panes=self._panes(), root=None)
        outcome = InitUseCase(ops).run(_request())
        self.assertTrue(outcome.is_refused)
        assert outcome.refused_message is not None
        self.assertIn("workspace root", outcome.refused_message)
        self.assertEqual(["require_tmux"], _kinds(ops))

    def test_conflict_refuses_after_root_before_registration(self) -> None:
        ops = _FakeInitWorkspaceOps(
            location="____:1.0", panes=self._panes(), conflicts=["____:0(%1)"]
        )
        outcome = InitUseCase(ops).run(_request())
        self.assertTrue(outcome.is_refused)
        # No registry write on a detectable conflict (fail-closed ordering).
        self.assertNotIn("register", _kinds(ops))

    def test_meaningful_foreign_session_is_refused(self) -> None:
        ops = _FakeInitWorkspaceOps(
            location="human-work:1.0",
            panes=[{"id": "%5", "location": "human-work:1.0", "cwd": "/repo"}],
            expected=_Expected(name="mozyo-repo"),
            fallback=False,
        )
        outcome = InitUseCase(ops).run(_request())
        self.assertTrue(outcome.is_refused)
        assert outcome.refused_message is not None
        self.assertIn("meaningful name", outcome.refused_message)
        self.assertNotIn("rename_session", _kinds(ops))

    def test_expected_session_collision_is_refused(self) -> None:
        ops = _FakeInitWorkspaceOps(
            location="____:1.0",
            panes=self._panes(),
            expected=_Expected(name="mozyo-repo"),
            fallback=True,
            session_present=True,
        )
        outcome = InitUseCase(ops).run(_request())
        self.assertTrue(outcome.is_refused)
        assert outcome.refused_message is not None
        self.assertIn("already exists", outcome.refused_message)
        self.assertNotIn("rename_session", _kinds(ops))


class LiveInitWorkspaceOpsTest(unittest.TestCase):
    def test_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveInitWorkspaceOps(), InitWorkspaceOps)

    def test_fake_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(_FakeInitWorkspaceOps(), InitWorkspaceOps)


class WorkspaceConfigHelperTest(unittest.TestCase):
    """The workspace/session config helper tail moved into this boundary (#12979).

    These pin the pure fallback / window-conflict decisions and the workspace-root
    discovery / VS Code settings write directly in the module that now owns them,
    which is the OOP-first carve's payoff.
    """

    def test_is_fallback_session_name_only_matches_all_underscore(self) -> None:
        self.assertTrue(_is_fallback_session_name("___________"))
        self.assertTrue(_is_fallback_session_name("__"))
        self.assertFalse(_is_fallback_session_name(""))
        self.assertFalse(_is_fallback_session_name("mozyo-giken-3500-jgmlife"))
        self.assertFalse(_is_fallback_session_name("2026PBL_____"))

    def test_confident_root_returns_marked_root_and_fails_closed_otherwise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = (Path(tmp) / "ws").resolve()
            (workspace / ".git").mkdir(parents=True)
            nested = workspace / "sub" / "dir"
            nested.mkdir(parents=True)
            self.assertEqual(_confident_workspace_root(str(nested)), workspace)

            bare = (Path(tmp) / "bare").resolve()
            bare.mkdir()
            self.assertIsNone(_confident_workspace_root(str(bare)))
            self.assertIsNone(_confident_workspace_root(""))

    def test_agent_window_conflict_excludes_target_and_other_sessions(self) -> None:
        panes = [
            {"location": "sess:1.0", "window_name": "claude", "id": "%1"},
            {"location": "sess:2.0", "window_name": "claude", "id": "%2"},
            {"location": "sess:3.0", "window_name": "codex", "id": "%3"},
            {"location": "other:9.0", "window_name": "claude", "id": "%9"},
        ]
        # Window 1 is the target; only the other same-session `claude` window
        # (window 2) is a conflict — window 3 is a different agent, `other:9` a
        # different session.
        self.assertEqual(
            _agent_window_conflict(panes, "sess", "1", "claude"),
            ["sess:2(%2)"],
        )
        # Querying `codex` while skipping its own window (3) leaves no conflict.
        self.assertEqual(_agent_window_conflict(panes, "sess", "3", "codex"), [])

    def test_write_vscode_session_name_creates_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, written, warning = _write_vscode_session_name(root, "mozyo-repo")
            self.assertTrue(written)
            self.assertIsNone(warning)
            self.assertEqual(path, root / ".vscode" / "settings.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["tmux-integrated.sessionName"], "mozyo-repo")

    def test_write_vscode_session_name_warns_on_unparseable_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / ".vscode" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{ /* JSONC comment */ }", encoding="utf-8")
            path, written, warning = _write_vscode_session_name(root, "mozyo-repo")
            self.assertFalse(written)
            self.assertIsNotNone(warning)
            assert warning is not None
            self.assertIn("not plain JSON", warning)
            # Operator content is left untouched.
            self.assertEqual(
                settings.read_text(encoding="utf-8"), "{ /* JSONC comment */ }"
            )

    def test_commands_re_exports_the_legacy_names(self) -> None:
        from mozyo_bridge.application import commands

        self.assertIs(commands._confident_workspace_root, _confident_workspace_root)
        self.assertIs(commands._is_fallback_session_name, _is_fallback_session_name)
        self.assertIs(commands._agent_window_conflict, _agent_window_conflict)
        self.assertIs(commands._write_vscode_session_name, _write_vscode_session_name)


if __name__ == "__main__":
    unittest.main()
