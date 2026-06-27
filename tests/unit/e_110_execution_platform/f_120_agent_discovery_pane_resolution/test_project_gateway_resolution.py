"""Semantic project-gateway resolution scenarios (Redmine #12668).

Scenario coverage for ``resolve_project_gateway`` -- the department-root ->
project-gateway route from
``vibes/docs/logics/ticketless-project-gateway-runtime-ux.md``. The resolver is
pure over the :class:`TargetCandidate` list the existing discovery pipeline
emits, so these tests construct candidates directly (and one case drives the full
``discover_agents`` -> ``fold_agents_by_pane`` -> ``build_target_candidates``
pipeline) and pin the fail-closed classifications: missing / found / ambiguous,
plus the identity-only guards (separate window/session is normal, repo-root stays
the Git authority, weak/ambiguous role never auto-targets).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    CONFIDENCE_WEAK,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    TargetCandidate,
    build_target_candidates,
    discover_agents,
    fold_agents_by_pane,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    REASON_PROJECT_SCOPE_MISMATCH,
    REASON_REPO_ROOT_MISMATCH,
    REASON_ROLE_MISMATCH,
    REASON_SESSION_MISMATCH,
    REASON_WEAK_OR_AMBIGUOUS_ROLE,
    STATUS_FOUND,
    STATUS_GATEWAY_AMBIGUOUS,
    STATUS_GATEWAY_MISSING,
    STATUS_SELECTOR_GAP,
    ProjectGatewayRoute,
    resolve_project_gateway,
    start_project_gateway_command,
)

REPO = "/work/gk-3500-it-operations"
PROJECT = "giken-cloud-drive-management"
PROJECT_PATH = "projects/giken-cloud-drive-management"


def _candidate(
    pane_id,
    *,
    role="codex",
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
    session="dept-root",
    repo_root=REPO,
    project_scope=PROJECT,
    project_path=PROJECT_PATH,
    active=False,
):
    """Build a :class:`TargetCandidate` with project-gateway-shaped defaults."""
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source=ROLE_SOURCE_PANE_OPTION,
        confidence=confidence,
        ambiguous=ambiguous,
        session=session,
        window_name="cockpit",
        window_index="0",
        pane_index="0",
        active=active,
        workspace_id="ws-gk3500",
        workspace_label="gk-3500-it-operations",
        lane_id="default",
        lane_label=None,
        repo_short="gk-3500-it-operations",
        repo_root=repo_root,
        cwd=f"{repo_root}/{project_path}" if project_path else repo_root,
        host="local",
        view_kind=VIEW_KIND_COCKPIT_PANE,
        branch="main",
        project_scope=project_scope,
        project_path=project_path,
        project_label="クラウドドライブ管理",
    )


def _route(**overrides):
    base = dict(repo_root=REPO, project_scope=PROJECT, role="codex")
    base.update(overrides)
    return ProjectGatewayRoute(**base)


class ResolveProjectGatewayScenarios(unittest.TestCase):
    def test_found_exactly_one_gateway(self):
        # A project-scoped Codex gateway and the project's Claude worker coexist;
        # the route resolves the single Codex gateway, not the worker.
        candidates = [
            _candidate("%gw", role="codex"),
            _candidate("%worker", role="claude"),
        ]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_FOUND)
        self.assertTrue(res.ok)
        self.assertIsNotNone(res.selected)
        self.assertEqual(res.selected.pane_id, "%gw")
        self.assertEqual(len(res.matched), 1)
        # The Claude worker is recorded as a role_mismatch near miss.
        reasons = {nm.candidate.pane_id: nm.reason for nm in res.near_misses}
        self.assertEqual(reasons.get("%worker"), REASON_ROLE_MISMATCH)

    def test_found_across_separate_session_is_normal_path(self):
        # The gateway lives in a different session/window than the root unit.
        # Separate window/session is the normal path: with no --session filter the
        # route still resolves it by repo_root + project_scope + role.
        candidates = [_candidate("%gw", session="project-gateway-window")]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_FOUND)
        self.assertEqual(res.selected.pane_id, "%gw")

    def test_missing_when_no_candidate_matches(self):
        # Only the project's Claude worker is up; no Codex gateway exists yet.
        candidates = [_candidate("%worker", role="claude")]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_GATEWAY_MISSING)
        self.assertIsNone(res.selected)
        # A concrete start_project_gateway action is namable for the missing case.
        cmd = start_project_gateway_command(res.route, project_path=PROJECT_PATH)
        self.assertIn("mozyo-bridge cockpit", cmd)
        self.assertIn(PROJECT_PATH, cmd)
        self.assertIn(REPO, cmd)

    def test_missing_when_no_panes_at_all(self):
        res = resolve_project_gateway([], _route())
        self.assertEqual(res.status, STATUS_GATEWAY_MISSING)
        self.assertEqual(res.matched, ())

    def test_ambiguous_when_multiple_gateways_match(self):
        candidates = [
            _candidate("%gw1", session="win-a"),
            _candidate("%gw2", session="win-b"),
        ]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_GATEWAY_AMBIGUOUS)
        self.assertIsNone(res.selected)
        self.assertEqual({c.pane_id for c in res.matched}, {"%gw1", "%gw2"})

    def test_ambiguous_resolved_by_session_narrowing(self):
        # Two gateways match repo+scope+role; narrowing by session selects one.
        candidates = [
            _candidate("%gw1", session="win-a"),
            _candidate("%gw2", session="win-b"),
        ]
        res = resolve_project_gateway(candidates, _route(session="win-b"))
        self.assertEqual(res.status, STATUS_FOUND)
        self.assertEqual(res.selected.pane_id, "%gw2")
        reasons = {nm.candidate.pane_id: nm.reason for nm in res.near_misses}
        self.assertEqual(reasons.get("%gw1"), REASON_SESSION_MISMATCH)

    def test_right_project_scope_wrong_repo_fails_closed(self):
        # Project scope is layered UNDER the Git authority: a same-named adopted
        # project in a different repo is never selected (Redmine #12658).
        candidates = [_candidate("%gw", repo_root="/work/other-repo")]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_GATEWAY_MISSING)
        reasons = {nm.candidate.pane_id: nm.reason for nm in res.near_misses}
        self.assertEqual(reasons.get("%gw"), REASON_REPO_ROOT_MISMATCH)

    def test_right_repo_wrong_project_workdir_fails_closed(self):
        # Correct repo root, but the Codex pane is not inside the expected project
        # scope (e.g. department-root coordinator at the repo root).
        candidates = [_candidate("%root-codex", project_scope="", project_path="")]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_GATEWAY_MISSING)
        reasons = {nm.candidate.pane_id: nm.reason for nm in res.near_misses}
        self.assertEqual(reasons.get("%root-codex"), REASON_PROJECT_SCOPE_MISMATCH)

    def test_weak_role_never_auto_targets(self):
        # A process-inferred (weak) Codex pane in the right repo+scope is refused.
        candidates = [_candidate("%maybe", confidence=CONFIDENCE_WEAK)]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_GATEWAY_MISSING)
        reasons = {nm.candidate.pane_id: nm.reason for nm in res.near_misses}
        self.assertEqual(reasons.get("%maybe"), REASON_WEAK_OR_AMBIGUOUS_ROLE)

    def test_ambiguous_role_never_auto_targets(self):
        candidates = [_candidate("%conflict", ambiguous=True)]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_GATEWAY_MISSING)

    def test_active_pane_is_not_authority(self):
        # Two matching gateways, one active. Active-ness must NOT break the tie:
        # the resolver stays ambiguous rather than silently picking the active one.
        candidates = [
            _candidate("%gw1", session="win-a", active=True),
            _candidate("%gw2", session="win-b", active=False),
        ]
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_GATEWAY_AMBIGUOUS)

    def test_selector_gap_on_underspecified_route(self):
        res = resolve_project_gateway(
            [_candidate("%gw")],
            ProjectGatewayRoute(repo_root="", project_scope=PROJECT, role="codex"),
        )
        self.assertEqual(res.status, STATUS_SELECTOR_GAP)
        self.assertIn("repo_root", res.detail)

    def test_resolution_payload_is_json_shaped(self):
        res = resolve_project_gateway([_candidate("%gw")], _route())
        payload = res.as_payload()
        self.assertEqual(payload["status"], STATUS_FOUND)
        self.assertEqual(payload["selected"]["runtime"]["pane_id"], "%gw")
        self.assertEqual(payload["route"]["project_scope"], PROJECT)


class ResolveFromDiscoveryPipeline(unittest.TestCase):
    """End-to-end-ish: drive the real discovery pipeline, then resolve."""

    def _pane(self, pane_id, location, *, agent_role, project_scope, project_path,
              repo_root_stamp, cwd):
        return {
            "id": pane_id,
            "location": location,
            "window_name": "cockpit",
            "command": "node",
            "cwd": cwd,
            "pane_active": "0",
            "agent_role": agent_role,
            "lane_id": "",
            "lane_label": "",
            "lane_kind": "",
            "delegation_parent": "",
            "project_scope": project_scope,
            "project_path": project_path,
            "project_label": "クラウドドライブ管理",
            "repo_root_stamp": repo_root_stamp,
            "workspace_id": "ws-gk3500",
        }

    def test_discovery_then_resolve_found(self):
        cwd = f"{REPO}/{PROJECT_PATH}"
        panes = [
            # department root Codex (repo root, no project scope)
            self._pane("%root", "dept-root:0.0", agent_role="codex",
                       project_scope="", project_path="",
                       repo_root_stamp=REPO, cwd=REPO),
            # project gateway Codex (separate window, project scope stamped)
            self._pane("%gw", "gateway:1.0", agent_role="codex",
                       project_scope=PROJECT, project_path=PROJECT_PATH,
                       repo_root_stamp=REPO, cwd=cwd),
            # project implementation worker Claude
            self._pane("%worker", "gateway:1.1", agent_role="claude",
                       project_scope=PROJECT, project_path=PROJECT_PATH,
                       repo_root_stamp=REPO, cwd=cwd),
        ]
        records = fold_agents_by_pane(discover_agents(panes))
        candidates = build_target_candidates(records)
        res = resolve_project_gateway(candidates, _route())
        self.assertEqual(res.status, STATUS_FOUND)
        self.assertEqual(res.selected.pane_id, "%gw")
        self.assertEqual(res.selected.role_source, ROLE_SOURCE_PANE_OPTION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
