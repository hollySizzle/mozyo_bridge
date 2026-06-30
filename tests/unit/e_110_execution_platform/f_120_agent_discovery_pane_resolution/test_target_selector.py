"""Fail-closed semantic target selector policy (Redmine #12663).

Pure unit tests over
:func:`mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.target_selector.select_target`:
exactly one candidate selects; zero / many / cross-workspace-Claude fail closed
with a classified stage, never a silent pick. No tmux / git I/O.
"""

from __future__ import annotations

import sys
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    CONFIDENCE_WEAK,
    ROLE_SOURCE_INFERRED,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.target_selector import (
    SELECT_AMBIGUOUS,
    SELECT_CROSS_WORKSPACE_CLAUDE,
    SELECT_INVALID_ROLE,
    SELECT_NO_CANDIDATE,
    SELECT_RESOLVED,
    STAGE_BINDING,
    STAGE_PROJECT,
    STAGE_REPO,
    STAGE_ROLE,
    STAGE_SESSION,
    TargetSelectorQuery,
    candidate_binds_role,
    render_selection_diagnostics,
    select_target,
)

REPO = "/work/gk-3500-it-operations"
OTHER_REPO = "/work/other-project"
PROJECT = "giken-cloud-drive-management"


def _candidate(
    pane_id,
    *,
    role="codex",
    confidence=CONFIDENCE_STRONG,
    role_source=ROLE_SOURCE_PANE_OPTION,
    ambiguous=False,
    session="dept-root",
    repo_root=REPO,
    project_scope="",
):
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source=role_source,
        confidence=confidence,
        ambiguous=ambiguous,
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


def _query(**overrides):
    base = dict(role="codex", repo_root=REPO)
    base.update(overrides)
    return TargetSelectorQuery(**base)


class SelectTargetHappyPath(unittest.TestCase):
    def test_exactly_one_codex_in_repo_resolves(self):
        cands = [
            _candidate("%10", role="codex", repo_root=REPO),
            _candidate("%11", role="claude", repo_root=REPO),
            _candidate("%12", role="codex", repo_root=OTHER_REPO),
        ]
        sel = select_target(cands, _query())
        self.assertEqual(sel.status, SELECT_RESOLVED)
        self.assertTrue(sel.resolved)
        self.assertEqual(sel.pane_id, "%10")
        self.assertIs(sel.selected, cands[0])

    def test_optional_session_and_project_narrow_to_one(self):
        cands = [
            _candidate("%10", session="dept-root", project_scope=PROJECT),
            _candidate("%11", session="dept-root", project_scope="other-project"),
            _candidate("%12", session="aux", project_scope=PROJECT),
        ]
        sel = select_target(
            cands, _query(session="dept-root", project_scope=PROJECT)
        )
        self.assertEqual(sel.status, SELECT_RESOLVED)
        self.assertEqual(sel.pane_id, "%10")

    def test_repo_none_means_any_repo(self):
        cands = [_candidate("%10", repo_root=OTHER_REPO)]
        sel = select_target(cands, _query(repo_root=None))
        self.assertEqual(sel.status, SELECT_RESOLVED)
        self.assertEqual(sel.pane_id, "%10")


class SelectTargetFailClosed(unittest.TestCase):
    def test_no_role_match(self):
        sel = select_target([_candidate("%11", role="claude")], _query())
        self.assertEqual(sel.status, SELECT_NO_CANDIDATE)
        self.assertEqual(sel.narrowing_stage, STAGE_ROLE)
        self.assertIsNone(sel.pane_id)

    def test_role_present_but_not_strongly_bound(self):
        cands = [
            _candidate("%10", confidence=CONFIDENCE_WEAK, role_source=ROLE_SOURCE_INFERRED),
            _candidate("%11", ambiguous=True),
        ]
        sel = select_target(cands, _query())
        self.assertEqual(sel.status, SELECT_NO_CANDIDATE)
        self.assertEqual(sel.narrowing_stage, STAGE_BINDING)
        # role-matched set is preserved for the diagnostic
        self.assertEqual(len(sel.role_matches), 2)

    def test_role_bound_but_wrong_repo(self):
        sel = select_target([_candidate("%10", repo_root=OTHER_REPO)], _query())
        self.assertEqual(sel.status, SELECT_NO_CANDIDATE)
        self.assertEqual(sel.narrowing_stage, STAGE_REPO)

    def test_repo_match_but_wrong_session(self):
        sel = select_target(
            [_candidate("%10", session="aux")], _query(session="dept-root")
        )
        self.assertEqual(sel.status, SELECT_NO_CANDIDATE)
        self.assertEqual(sel.narrowing_stage, STAGE_SESSION)

    def test_session_match_but_wrong_project(self):
        sel = select_target(
            [_candidate("%10", project_scope="other")],
            _query(project_scope=PROJECT),
        )
        self.assertEqual(sel.status, SELECT_NO_CANDIDATE)
        self.assertEqual(sel.narrowing_stage, STAGE_PROJECT)

    def test_multiple_matches_are_ambiguous_with_session_hint(self):
        cands = [
            _candidate("%10", session="dept-root"),
            _candidate("%11", session="aux"),
        ]
        sel = select_target(cands, _query())
        self.assertEqual(sel.status, SELECT_AMBIGUOUS)
        self.assertEqual(len(sel.matches), 2)
        self.assertIn("--target-session", sel.detail)

    def test_invalid_role(self):
        sel = select_target([], TargetSelectorQuery(role="unknown"))
        self.assertEqual(sel.status, SELECT_INVALID_ROLE)


class CrossWorkspaceClaudeGuard(unittest.TestCase):
    def test_same_session_different_repo_claude_fails_closed(self):
        # The #12663 j#68819 finding 1 core case: a cockpit packs many repos into
        # ONE tmux session, so a same-session Claude in a different repo is still
        # a cross-workspace direct send and must be refused.
        cands = [_candidate("%20", role="claude", session="dept-root", repo_root=OTHER_REPO)]
        sel = select_target(
            cands,
            _query(role="claude", repo_root=OTHER_REPO, session="dept-root",
                   sender_repo_root=REPO),
        )
        self.assertEqual(sel.status, SELECT_CROSS_WORKSPACE_CLAUDE)
        self.assertIsNone(sel.pane_id)
        self.assertIs(sel.selected, cands[0])
        self.assertIn("Codex gateway", sel.detail)

    def test_missing_sender_repo_identity_fails_closed_for_claude(self):
        cands = [_candidate("%20", role="claude", repo_root=REPO)]
        sel = select_target(cands, _query(role="claude", sender_repo_root=None))
        self.assertEqual(sel.status, SELECT_CROSS_WORKSPACE_CLAUDE)

    def test_same_repo_claude_resolves_even_across_sessions(self):
        # Repo root is the workspace boundary, not the session: same repo in a
        # different session is the sender's own workspace -> allowed.
        cands = [_candidate("%20", role="claude", session="other-session", repo_root=REPO)]
        sel = select_target(
            cands, _query(role="claude", repo_root=REPO, sender_repo_root=REPO)
        )
        self.assertEqual(sel.status, SELECT_RESOLVED)
        self.assertEqual(sel.pane_id, "%20")

    def test_codex_cross_repo_is_allowed(self):
        # The cross-workspace guard is Claude-only; the Codex gateway IS the
        # cross-workspace route, so a foreign-repo Codex resolves.
        cands = [_candidate("%20", role="codex", session="other-ws", repo_root=OTHER_REPO)]
        sel = select_target(
            cands,
            _query(role="codex", repo_root=OTHER_REPO, sender_repo_root=REPO),
        )
        self.assertEqual(sel.status, SELECT_RESOLVED)


class NormalizationAndBinding(unittest.TestCase):
    def test_nfc_query_matches_nfd_candidate_repo(self):
        # Same directory spelled NFC vs NFD must compare equal through the
        # injected normaliser (Redmine #11625 path identity).
        repo_nfd = "/work/ガイケン"  # precomposed forms vary
        repo_nfc = unicodedata.normalize("NFC", repo_nfd)
        cands = [_candidate("%10", repo_root=repo_nfd)]
        sel = select_target(
            cands,
            _query(repo_root=repo_nfc),
            normalize=lambda p: unicodedata.normalize("NFD", p),
        )
        self.assertEqual(sel.status, SELECT_RESOLVED)

    def test_candidate_binds_role_predicate(self):
        strong = _candidate("%1", confidence=CONFIDENCE_STRONG, ambiguous=False)
        weak = _candidate("%2", confidence=CONFIDENCE_WEAK)
        amb = _candidate("%3", ambiguous=True)
        self.assertTrue(candidate_binds_role(strong, "codex"))
        self.assertFalse(candidate_binds_role(weak, "codex"))
        self.assertFalse(candidate_binds_role(amb, "codex"))
        self.assertFalse(candidate_binds_role(strong, "claude"))


class Diagnostics(unittest.TestCase):
    def test_ambiguous_diagnostic_lists_candidates(self):
        cands = [
            _candidate("%10", session="dept-root"),
            _candidate("%11", session="aux"),
        ]
        sel = select_target(cands, _query())
        text = render_selection_diagnostics(sel)
        self.assertIn("ambiguous", text)
        self.assertIn("%10", text)
        self.assertIn("%11", text)

    def test_no_candidate_diagnostic_shows_role_matches(self):
        sel = select_target([_candidate("%10", repo_root=OTHER_REPO)], _query())
        text = render_selection_diagnostics(sel)
        self.assertIn("no_candidate", text)
        # the wrong-repo pane is still shown so the operator sees what exists
        self.assertIn("%10", text)


if __name__ == "__main__":
    unittest.main()
