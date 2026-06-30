"""Semantic target selector readiness scenarios (Redmine #12663).

Parent #12656 / workflow-control lane. These classical (Detroit-school)
scenarios drive the **real** pure
:func:`~mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.target_selector.select_target`
policy over candidate panes shaped like the #12659 multi-column cockpit that
once mis-routed a handoff to a Claude pane instead of the Codex gateway. Nothing
is faked: each scenario asserts the durable, fail-closed behaviour the issue's
acceptance list enumerates, so the standard ``handoff send`` / ``message`` UX is
expressible by *semantic route identity* with no hand-copied ``%pane``.

Each test class maps to one acceptance bullet from #12663:

- exactly one candidate (role + session + repo) selects and sends
  (``ExactlyOneCandidateSelectsScenarioTest``);
- zero / multiple candidates fail closed with diagnostics, never a silent active
  pane pick (``ZeroOrManyFailsClosedScenarioTest``);
- the #12659 mistake — addressing a foreign workspace's Claude pane directly — is
  refused and routed toward the Codex gateway
  (``CrossWorkspaceClaudeRoutedToGatewayScenarioTest``);
- the standard route needs no ``%pane`` id and the ``--target-repo`` /
  ``--target-project`` identity gates are preserved as the downstream backstop
  (``StandardRouteNeedsNoPaneIdScenarioTest``).

Cross-cutting (it spans the ``f_120`` discovery/selection and ``f_130`` handoff
surfaces), so per the tests-placement policy it lives in ``tests/scenarios/``.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E402
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.target_selector import (  # noqa: E402
    SELECT_AMBIGUOUS,
    SELECT_CROSS_WORKSPACE_CLAUDE,
    SELECT_NO_CANDIDATE,
    SELECT_RESOLVED,
    TargetSelectorQuery,
    render_selection_diagnostics,
    select_target,
)

# The #12659 cockpit: a department-root coordinator session holding both a Codex
# gateway and a Claude implementer pane for the same project repo, plus an
# unrelated project's gateway in another session.
DEPT_SESSION = "gk-3500-it-operations"
OTHER_SESSION = "other-workspace"
REPO = "/work/gk-3500-it-operations"
OTHER_REPO = "/work/other-project"


def _pane(pane_id, *, role, session=DEPT_SESSION, repo_root=REPO, project_scope=""):
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
        active=(pane_id == "%active"),
        workspace_id="ws-gk3500",
        workspace_label=DEPT_SESSION,
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


def _cockpit():
    return [
        _pane("%10", role="codex"),                       # the intended gateway
        _pane("%active", role="claude"),                  # the active pane (mistake target)
        _pane("%30", role="codex", session=OTHER_SESSION, repo_root=OTHER_REPO),
    ]


class ExactlyOneCandidateSelectsScenarioTest(unittest.TestCase):
    def test_role_session_repo_resolves_the_gateway_not_the_active_pane(self):
        sel = select_target(
            _cockpit(),
            TargetSelectorQuery(
                role="codex", repo_root=REPO, session=DEPT_SESSION,
                sender_session=DEPT_SESSION,
            ),
        )
        self.assertEqual(sel.status, SELECT_RESOLVED)
        # The Codex gateway is chosen even though the Claude pane is the active
        # split — the #12659 false-route is structurally impossible.
        self.assertEqual(sel.pane_id, "%10")
        self.assertEqual(sel.selected.role, "codex")


class ZeroOrManyFailsClosedScenarioTest(unittest.TestCase):
    def test_zero_candidates_fails_closed_with_resolution_guidance(self):
        sel = select_target(
            [_pane("%active", role="claude")],
            TargetSelectorQuery(role="codex", repo_root=REPO),
        )
        self.assertEqual(sel.status, SELECT_NO_CANDIDATE)
        diag = render_selection_diagnostics(sel)
        self.assertIn("no_candidate", diag)

    def test_multiple_codex_in_repo_fails_closed_not_silent_pick(self):
        cockpit = _cockpit() + [_pane("%11", role="codex")]  # 2 codex in REPO
        sel = select_target(
            cockpit, TargetSelectorQuery(role="codex", repo_root=REPO)
        )
        self.assertEqual(sel.status, SELECT_AMBIGUOUS)
        self.assertEqual({c.pane_id for c in sel.matches}, {"%10", "%11"})
        self.assertIsNone(sel.pane_id)


class CrossWorkspaceClaudeRoutedToGatewayScenarioTest(unittest.TestCase):
    def test_foreign_claude_is_refused_and_points_at_codex_gateway(self):
        # Operator tries to reach the other workspace's Claude pane directly.
        foreign_claude = _pane(
            "%40", role="claude", session=OTHER_SESSION, repo_root=OTHER_REPO
        )
        sel = select_target(
            [foreign_claude],
            TargetSelectorQuery(
                role="claude", repo_root=OTHER_REPO, sender_session=DEPT_SESSION
            ),
        )
        self.assertEqual(sel.status, SELECT_CROSS_WORKSPACE_CLAUDE)
        self.assertIsNone(sel.pane_id)
        self.assertIn("Codex gateway", sel.detail)


class StandardRouteNeedsNoPaneIdScenarioTest(unittest.TestCase):
    def test_send_to_gateway_for_this_repo_uses_only_semantic_identity(self):
        # No `%pane` is supplied anywhere: role + repo (+ session) is the whole
        # route, and it resolves to one pane id the caller never had to copy.
        sel = select_target(
            _cockpit(),
            TargetSelectorQuery(
                role="codex", repo_root=REPO, sender_session=DEPT_SESSION
            ),
        )
        self.assertEqual(sel.status, SELECT_RESOLVED)
        self.assertTrue(sel.pane_id.startswith("%"))
        # The matched candidate still carries the repo root the downstream
        # `--target-repo` gate will re-validate (selector narrows, gate enforces).
        self.assertEqual(sel.selected.repo_root, REPO)


if __name__ == "__main__":
    unittest.main()
