"""Semantic target selection wired into `handoff send` / `message` (Redmine #12663).

Integration tests over the shared resolver
(:func:`mozyo_bridge.application.commands_target_select.select_semantic_target`)
and its two CLI entry points: ``handoff send --select`` and
``message --select-role``. The selector resolves the target pane from role +
session + repo + optional project scope and fails closed (no send) on 0 / many /
cross-workspace-Claude, instead of requiring a hand-copied ``%pane``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
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

from mozyo_bridge.application import commands_target_select as cts
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.target_selector import (
    SELECT_RESOLVED,
)

REPO = "/work/gk-3500-it-operations"
OTHER_REPO = "/work/other-project"


def _candidate(pane_id, *, role="codex", session="dept-root", repo_root=REPO, project_scope=""):
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source=ROLE_SOURCE_PANE_OPTION,
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session=session,
        window_name="cockpit",
        window_index="0",
        pane_index="0",
        active=False,
        workspace_id="ws-gk3500",
        workspace_label="gk-3500-it-operations",
        lane_id="default",
        lane_label=None,
        repo_short=Path(repo_root).name,
        repo_root=repo_root,
        cwd=repo_root,
        host="local",
        view_kind=VIEW_KIND_COCKPIT_PANE,
        branch="main",
        project_scope=project_scope,
        project_path=project_scope,
        project_label=project_scope,
    )


class SelectSemanticTargetResolver(unittest.TestCase):
    """The shared resolver function, candidates injected (no live discovery)."""

    def test_resolves_unique_codex_in_explicit_repo(self):
        cands = [
            _candidate("%10", role="codex", repo_root=REPO),
            _candidate("%11", role="codex", repo_root=OTHER_REPO),
        ]
        selected = cts.select_semantic_target(
            role="codex",
            repo=REPO,
            session=None,
            project=None,
            sender_cwd="/somewhere",
            candidates=cands,
        )
        self.assertEqual(selected.pane_id, "%10")
        self.assertEqual(selected.selection.status, SELECT_RESOLVED)
        # The matched concrete root is handed back for the downstream gate.
        self.assertEqual(selected.repo_root, str(Path(REPO).resolve()))

    def test_fail_closed_prints_diagnostics_and_exits(self):
        cands = [_candidate("%10", role="codex", repo_root=OTHER_REPO)]
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
            cts.select_semantic_target(
                role="codex",
                repo=REPO,
                session=None,
                project=None,
                sender_cwd="/somewhere",
                candidates=cands,
            )
        out = stderr.getvalue()
        self.assertIn("no_candidate", out)
        self.assertIn("%10", out)

    def test_ambiguous_fails_closed(self):
        cands = [
            _candidate("%10", session="dept-root"),
            _candidate("%11", session="aux"),
        ]
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cts.select_semantic_target(
                role="codex",
                repo=REPO,
                session=None,
                project=None,
                sender_cwd="/x",
                candidates=cands,
            )

    def test_no_repo_identity_fails_closed(self):
        # Finding 2 (j#68819): no explicit --target-repo and an unresolvable
        # sender workspace must fail closed, not select the only visible pane.
        cands = [_candidate("%10", role="codex", repo_root=REPO)]
        with patch.object(cts, "resolve_sender_repo_root", return_value=None), \
            self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cts.select_semantic_target(
                role="codex",
                repo=None,
                session=None,
                project=None,
                sender_cwd="/unresolvable",
                candidates=cands,
            )

    def test_same_session_cross_repo_claude_fails_closed(self):
        # Finding 1 (j#68819): a Claude pane in another repo but the same cockpit
        # session must be refused even with an explicit --target-repo for it.
        cands = [_candidate("%20", role="claude", session="dept-root", repo_root=OTHER_REPO)]
        with patch.object(cts, "resolve_sender_repo_root", return_value=REPO), \
            self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cts.select_semantic_target(
                role="claude",
                repo=OTHER_REPO,
                session=None,
                project=None,
                sender_cwd="/sender/repo",
                candidates=cands,
            )

    def test_same_repo_claude_resolves(self):
        cands = [_candidate("%21", role="claude", session="dept-root", repo_root=REPO)]
        with patch.object(cts, "resolve_sender_repo_root", return_value=REPO):
            selected = cts.select_semantic_target(
                role="claude",
                repo=REPO,
                session=None,
                project=None,
                sender_cwd="/sender/repo",
                candidates=cands,
            )
        self.assertEqual(selected.pane_id, "%21")


class HandoffSendSelectWiring(unittest.TestCase):
    def _args(self, argv):
        return build_parser().parse_args(argv)

    def test_select_mutates_target_and_repo(self):
        from mozyo_bridge.application.commands_target_select import (
            apply_handoff_selection as _apply_semantic_target_selection,
        )

        args = self._args(
            [
                "handoff", "send", "--to", "codex", "--source", "redmine",
                "--issue", "12663", "--journal", "1", "--kind",
                "implementation_request", "--select", "--target-repo", REPO,
            ]
        )
        with patch.object(
            cts, "discover_all_candidates",
            return_value=[_candidate("%10", role="codex", repo_root=REPO)],
        ):
            _apply_semantic_target_selection(args)
        self.assertEqual(args.target, "%10")
        self.assertEqual(args.target_repo, str(Path(REPO).resolve()))

    def test_select_with_explicit_target_is_mutually_exclusive(self):
        from mozyo_bridge.application.commands_target_select import (
            apply_handoff_selection as _apply_semantic_target_selection,
        )

        args = self._args(
            [
                "handoff", "send", "--to", "codex", "--source", "redmine",
                "--issue", "12663", "--journal", "1", "--kind",
                "implementation_request", "--select", "--target", "%99",
            ]
        )
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            _apply_semantic_target_selection(args)

    def test_no_select_leaves_args_untouched(self):
        from mozyo_bridge.application.commands_target_select import (
            apply_handoff_selection as _apply_semantic_target_selection,
        )

        args = self._args(
            [
                "handoff", "send", "--to", "codex", "--source", "redmine",
                "--issue", "12663", "--journal", "1", "--kind",
                "implementation_request", "--target", "%5",
            ]
        )
        _apply_semantic_target_selection(args)
        self.assertEqual(args.target, "%5")


class MessageSelectWiring(unittest.TestCase):
    def _run_message(self, argv, candidates):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch.object(cts, "discover_all_candidates", return_value=candidates), \
            patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.require_read"), \
            patch("mozyo_bridge.application.commands.clear_read"), \
            patch("mozyo_bridge.application.commands.wait_for_text", return_value=True), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value="dept-root"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="root"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="dept-root:0.0"), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            contextlib.redirect_stdout(io.StringIO()):
            rc = args.func(args)
        return rc, sent

    def test_message_select_role_sends_to_resolved_pane(self):
        rc, sent = self._run_message(
            [
                "message", "--select-role", "codex", "--target-repo", REPO,
                "hello gateway",
            ],
            [_candidate("%10", role="codex", repo_root=REPO)],
        )
        self.assertEqual(rc, 0)
        # The header type and the Enter both target the resolved pane %10.
        targets = {a[2] for a in sent if a[:2] == ("send-keys", "-t")}
        self.assertIn("%10", targets)
        self.assertNotIn("%99", targets)

    def test_message_select_role_fail_closed_no_send(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run_message(
                [
                    "message", "--select-role", "codex", "--target-repo", REPO,
                    "hello",
                ],
                [_candidate("%10", role="codex", repo_root=OTHER_REPO)],
            )

    def test_message_target_and_select_role_mutually_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run_message(
                ["message", "--select-role", "codex", "%7", "text"],
                [_candidate("%10", role="codex", repo_root=REPO)],
            )

    def test_message_without_target_or_select_dies(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run_message(["message", "only-text"], [])


if __name__ == "__main__":
    unittest.main()
