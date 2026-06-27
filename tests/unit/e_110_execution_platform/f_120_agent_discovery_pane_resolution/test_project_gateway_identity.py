"""Live project-gateway lane identity + launch-or-adopt scenarios (Redmine #12708).

Builds on the #12668 ``resolve_project_gateway`` resolver to cover the live-lane
layer the GK3500 exploratory smoke (#12698) needed: a derived gateway
``target_kind`` (the visible Cloud-Drive-gateway vs GK3500-root distinction), a
:class:`GatewayLaneIdentity` route-registry record derived from project metadata
(no pane id), and the launch-or-adopt decision (found -> adopt / gateway_missing
-> launch / ambiguous|selector_gap -> blocked). The pieces are pure over the
``TargetCandidate`` list, so candidates are constructed directly; the CLU `adopt`
command is exercised with the discovery seam patched.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
    ProjectScope,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    CONFIDENCE_WEAK,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    VIEW_KIND_NORMAL_WINDOW,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    STATUS_FOUND,
    STATUS_GATEWAY_AMBIGUOUS,
    STATUS_GATEWAY_MISSING,
    STATUS_SELECTOR_GAP,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    ACTION_ADOPT,
    ACTION_BLOCKED,
    ACTION_LAUNCH,
    CALLBACK_TO_GRANDPARENT,
    LANE_KIND_PARENT,
    LAUNCH_POLICY_LAUNCH_OR_ADOPT,
    TARGET_KIND_PROJECT_GATEWAY,
    TARGET_KIND_UNKNOWN,
    TARGET_KIND_WORKER,
    TARGET_KIND_WORKSPACE_ROOT,
    GatewayLaneIdentity,
    classify_target_kind,
    gateway_lane_identity_from_scope,
    gateway_projection,
    resolve_launch_or_adopt,
)

REPO = "/work/gk-3500-it-operations"
PROJECT = "giken-cloud-drive-management"
PROJECT_PATH = "projects/giken-cloud-drive-management"
LABEL = "クラウドドライブ管理"
WORKSPACE = "gk-3500-it-operations"


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
    view_kind=VIEW_KIND_COCKPIT_PANE,
):
    """A :class:`TargetCandidate` with project-gateway-shaped defaults."""
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
        active=False,
        workspace_id="ws-gk3500",
        workspace_label=WORKSPACE,
        lane_id="default",
        lane_label=None,
        repo_short="gk-3500-it-operations",
        repo_root=repo_root,
        cwd=f"{repo_root}/{project_path}" if project_path else repo_root,
        host="local",
        view_kind=view_kind,
        branch="main",
        project_scope=project_scope,
        project_path=project_path,
        project_label=LABEL,
    )


def _scope():
    return ProjectScope(
        scope=PROJECT,
        path=PROJECT_PATH,
        label=LABEL,
        workdir=PROJECT_PATH,
        parent_workspace=WORKSPACE,
        source=f"{PROJECT_PATH}/project.yaml",
        fingerprint="sha256:abc",
    )


def _identity():
    return gateway_lane_identity_from_scope(_scope(), repo_root=REPO)


class ClassifyTargetKindTest(unittest.TestCase):
    def test_strong_codex_with_project_scope_is_project_gateway(self) -> None:
        self.assertEqual(
            classify_target_kind(_candidate("%gw")), TARGET_KIND_PROJECT_GATEWAY
        )

    def test_strong_codex_without_project_scope_is_workspace_root(self) -> None:
        # The GK3500 department-root / default Codex: a Codex with NO project
        # scope is NOT a project gateway (the smoke's core confusion).
        root = _candidate("%root", project_scope="", project_path="")
        self.assertEqual(classify_target_kind(root), TARGET_KIND_WORKSPACE_ROOT)

    def test_claude_is_worker_even_with_project_scope(self) -> None:
        self.assertEqual(
            classify_target_kind(_candidate("%w", role="claude")), TARGET_KIND_WORKER
        )

    def test_weak_codex_is_unknown(self) -> None:
        weak = _candidate("%weak", confidence=CONFIDENCE_WEAK)
        self.assertEqual(classify_target_kind(weak), TARGET_KIND_UNKNOWN)

    def test_ambiguous_codex_is_unknown(self) -> None:
        amb = _candidate("%amb", ambiguous=True)
        self.assertEqual(classify_target_kind(amb), TARGET_KIND_UNKNOWN)

    def test_gateway_projection_flags_only_the_gateway(self) -> None:
        gw = gateway_projection(_candidate("%gw"))
        self.assertTrue(gw["is_project_gateway"])
        self.assertEqual(gw["target_kind"], TARGET_KIND_PROJECT_GATEWAY)
        self.assertEqual(gw["project_scope"], PROJECT)
        self.assertEqual(gw["project_label"], LABEL)

        root = gateway_projection(_candidate("%root", project_scope="", project_path=""))
        self.assertFalse(root["is_project_gateway"])
        self.assertEqual(root["target_kind"], TARGET_KIND_WORKSPACE_ROOT)
        self.assertIsNone(root["project_scope"])


class GatewayLaneIdentityTest(unittest.TestCase):
    def test_derives_from_project_metadata_with_policy_defaults(self) -> None:
        identity = _identity()
        self.assertEqual(identity.project_scope, PROJECT)
        self.assertEqual(identity.project_label, LABEL)
        self.assertEqual(identity.project_path, PROJECT_PATH)
        self.assertEqual(identity.repo_root, REPO)
        self.assertEqual(identity.workspace, WORKSPACE)
        # The declarative route-registry contract from the issue's example.
        self.assertEqual(identity.role, "codex")
        self.assertEqual(identity.target_kind, TARGET_KIND_PROJECT_GATEWAY)
        self.assertEqual(identity.lane_kind, LANE_KIND_PARENT)
        self.assertEqual(identity.launch_policy, LAUNCH_POLICY_LAUNCH_OR_ADOPT)
        self.assertEqual(identity.callback_to, CALLBACK_TO_GRANDPARENT)

    def test_payload_carries_no_pane_id(self) -> None:
        # Prohibition: project metadata / lane identity never fixes a pane id.
        payload = _identity().as_payload()
        self.assertNotIn("pane_id", payload)
        self.assertNotIn("pane", json.dumps(payload))

    def test_as_route_matches_resolver_inputs(self) -> None:
        route = _identity().as_route(session="dept-root")
        self.assertEqual(route.repo_root, REPO)
        self.assertEqual(route.project_scope, PROJECT)
        self.assertEqual(route.role, "codex")
        self.assertEqual(route.session, "dept-root")
        self.assertEqual(route.target_kind, TARGET_KIND_PROJECT_GATEWAY)


class ResolveLaunchOrAdoptTest(unittest.TestCase):
    def test_found_yields_adopt(self) -> None:
        candidates = [_candidate("%gw"), _candidate("%worker", role="claude")]
        decision = resolve_launch_or_adopt(candidates, _identity())
        self.assertEqual(decision.action, ACTION_ADOPT)
        self.assertTrue(decision.ok)
        self.assertIsNotNone(decision.adopted)
        self.assertEqual(decision.adopted.pane_id, "%gw")
        self.assertEqual(decision.resolution.status, STATUS_FOUND)
        self.assertEqual(decision.launch_command, "")

    def test_missing_yields_launch_with_concrete_command(self) -> None:
        # Only the worker is up; no Codex gateway exists yet -> launch one.
        candidates = [_candidate("%worker", role="claude")]
        decision = resolve_launch_or_adopt(candidates, _identity())
        self.assertEqual(decision.action, ACTION_LAUNCH)
        self.assertTrue(decision.ok)
        self.assertIsNone(decision.adopted)
        self.assertEqual(decision.resolution.status, STATUS_GATEWAY_MISSING)
        # The launch command targets the project workdir (cwd is the authority),
        # carries no `--repo <git-root>` (review j#66626 blocker 1).
        runnable = decision.launch_command.split("#", 1)[0]
        self.assertIn(f"cd {REPO}/{PROJECT_PATH}", runnable)
        self.assertIn("mozyo-bridge cockpit", runnable)
        self.assertNotIn("--repo", runnable)

    def test_no_panes_at_all_yields_launch(self) -> None:
        decision = resolve_launch_or_adopt([], _identity())
        self.assertEqual(decision.action, ACTION_LAUNCH)

    def test_ambiguous_yields_blocked(self) -> None:
        # Two project-scoped Codex gateways match -> refuse to adopt or launch.
        candidates = [
            _candidate("%gw1", session="window-a"),
            _candidate("%gw2", session="window-b"),
        ]
        decision = resolve_launch_or_adopt(candidates, _identity())
        self.assertEqual(decision.action, ACTION_BLOCKED)
        self.assertFalse(decision.ok)
        self.assertEqual(decision.resolution.status, STATUS_GATEWAY_AMBIGUOUS)
        self.assertEqual(len(decision.resolution.matched), 2)

    def test_ambiguous_narrows_to_adopt_with_session(self) -> None:
        candidates = [
            _candidate("%gw1", session="window-a"),
            _candidate("%gw2", session="window-b"),
        ]
        decision = resolve_launch_or_adopt(
            candidates, _identity(), session="window-a"
        )
        self.assertEqual(decision.action, ACTION_ADOPT)
        self.assertEqual(decision.adopted.pane_id, "%gw1")

    def test_underspecified_route_yields_blocked_selector_gap(self) -> None:
        # An identity missing the project scope cannot resolve -> selector_gap.
        identity = GatewayLaneIdentity(
            project_scope="", project_label="", project_path="", repo_root=REPO
        )
        decision = resolve_launch_or_adopt([_candidate("%gw")], identity)
        self.assertEqual(decision.action, ACTION_BLOCKED)
        self.assertEqual(decision.resolution.status, STATUS_SELECTOR_GAP)

    def test_repo_root_mismatch_is_not_adopted(self) -> None:
        # A pane with the right project scope but a different repo root must not
        # be adopted -- repo_root stays the Git authority (#12658).
        candidates = [_candidate("%other", repo_root="/work/other-repo")]
        decision = resolve_launch_or_adopt(candidates, _identity())
        self.assertEqual(decision.action, ACTION_LAUNCH)  # no in-repo gateway
        self.assertIsNone(decision.adopted)


class ProjectGatewayAdoptCliTest(unittest.TestCase):
    def _run(self, candidates, *, project=PROJECT, as_json=False, scopes=()):
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (
            cli_project_gateway,
        )

        args = argparse.Namespace(
            repo=REPO, project=project, session=None, as_json=as_json
        )
        with patch.object(cli_project_gateway, "require_tmux"), \
            patch.object(
                cli_project_gateway, "_discover_candidates", return_value=candidates
            ), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity."
                "application.project_discovery.adopted_scopes_for_repo",
                return_value=tuple(scopes),
            ), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli_project_gateway.cmd_project_gateway_adopt(args)
        return rc, out.getvalue()

    def test_adopt_prints_resolved_gateway_and_handoff_next(self) -> None:
        rc, out = self._run([_candidate("%gw")], scopes=[_scope()])
        self.assertEqual(0, rc)
        self.assertIn("action: adopt", out)
        self.assertIn("pane_id=%gw", out)
        self.assertIn("project-gateway handoff", out)
        # Project metadata fed the identity (parent workspace surfaced).
        self.assertIn(WORKSPACE, out)

    def test_launch_prints_concrete_start_command(self) -> None:
        rc, out = self._run([_candidate("%w", role="claude")], scopes=[_scope()])
        self.assertEqual(0, rc)
        self.assertIn("action: launch", out)
        self.assertIn("start_project_gateway", out)
        self.assertIn(f"cd {REPO}/{PROJECT_PATH}", out)

    def test_blocked_ambiguous_lists_candidates_and_fails_closed(self) -> None:
        rc, out = self._run(
            [_candidate("%gw1", session="a"), _candidate("%gw2", session="b")],
            scopes=[_scope()],
        )
        self.assertEqual(1, rc)
        self.assertIn("action: blocked", out)
        self.assertIn("matched (ambiguous", out)
        self.assertIn("%gw1", out)
        self.assertIn("%gw2", out)

    def test_json_emits_decision_payload(self) -> None:
        rc, out = self._run([_candidate("%gw")], scopes=[_scope()], as_json=True)
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual(payload["action"], "adopt")
        self.assertEqual(payload["identity"]["lane_kind"], LANE_KIND_PARENT)
        self.assertEqual(payload["adopted"]["runtime"]["pane_id"], "%gw")

    def test_unadopted_project_falls_back_to_thin_identity(self) -> None:
        # The project is not in adopted metadata (runtime_identity off): the
        # command still resolves and fails closed honestly rather than inventing
        # an adoption. With no live gateway it lands on launch.
        rc, out = self._run([_candidate("%w", role="claude")], scopes=[])
        self.assertEqual(0, rc)
        self.assertIn("action: launch", out)

    def test_adopt_cockpit_lane_reports_green_path(self) -> None:
        # Redmine #12699: adopting a cockpit-visible lane is green-path evidence.
        rc, out = self._run([_candidate("%gw")], scopes=[_scope()])
        self.assertEqual(0, rc)
        self.assertIn("startup_evidence: cockpit_visible (green_path=True)", out)

    def test_adopt_normal_window_lane_warns_not_green_path(self) -> None:
        # Redmine #12699: a matching coordinator that is only a detached normal
        # window is a real lane but NOT cockpit-visible green-path evidence. The
        # command surfaces the warning, names the cockpit-visible startup, and
        # fails closed (rc 1) so the detached lane is not accepted as the route.
        rc, out = self._run(
            [_candidate("%norm", view_kind=VIEW_KIND_NORMAL_WINDOW)], scopes=[_scope()]
        )
        self.assertEqual(1, rc)
        self.assertIn("startup_evidence: detached_no_attach (green_path=False)", out)
        self.assertIn("warning:", out)
        self.assertIn("mozyo-bridge cockpit", out)

    def test_launch_names_cockpit_visible_and_warns_detached(self) -> None:
        rc, out = self._run([_candidate("%w", role="claude")], scopes=[_scope()])
        self.assertEqual(0, rc)
        self.assertIn("cockpit-visible Unit", out)
        self.assertIn("--no-attach", out)

    def test_json_includes_startup_evidence(self) -> None:
        rc, out = self._run([_candidate("%gw")], scopes=[_scope()], as_json=True)
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual(payload["startup_evidence"]["mode"], "cockpit_visible")
        self.assertTrue(payload["startup_evidence"]["is_green_path"])

    def test_json_normal_window_adopt_fails_closed(self) -> None:
        # Redmine #12699 review rev2: JSON and text must agree — adopting a
        # detached normal-window lane is not green-path, so rc 1 in JSON mode too.
        rc, out = self._run(
            [_candidate("%norm", view_kind=VIEW_KIND_NORMAL_WINDOW)],
            scopes=[_scope()],
            as_json=True,
        )
        self.assertEqual(1, rc)
        payload = json.loads(out)
        self.assertEqual(payload["action"], "adopt")
        self.assertEqual(payload["startup_evidence"]["mode"], "detached_no_attach")
        self.assertFalse(payload["startup_evidence"]["is_green_path"])


class ProjectGatewayRoutePlanCliTest(unittest.TestCase):
    """`project-gateway route-plan`: current-Unit relative delegation route (#12699)."""

    def _run(self, candidates, *, from_role="grandparent_coordinator", as_json=False):
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (
            cli_project_gateway,
        )

        args = argparse.Namespace(
            from_role=from_role, repo=REPO, project=PROJECT, session=None, as_json=as_json
        )
        with patch.object(cli_project_gateway, "require_tmux"), \
            patch.object(
                cli_project_gateway, "_discover_candidates", return_value=candidates
            ), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli_project_gateway.cmd_project_gateway_route_plan(args)
        return rc, out.getvalue()

    def test_grandparent_adopt_cockpit_lane_is_green(self) -> None:
        rc, out = self._run([_candidate("%gw")])
        self.assertEqual(0, rc)
        self.assertIn("grandparent(grandparent_coordinator) -> parent(project_gateway)", out)
        self.assertIn("startup_evidence: cockpit_visible (green_path=True)", out)

    def test_grandparent_missing_launch_is_cockpit_visible(self) -> None:
        rc, out = self._run([_candidate("%w", role="claude")])
        self.assertEqual(0, rc)
        self.assertIn("cockpit", out)
        self.assertIn("--no-attach", out)

    def test_child_worker_is_anchor_gated(self) -> None:
        rc, out = self._run(
            [_candidate("%w", role="claude")], from_role="delegated_coordinator"
        )
        self.assertEqual(0, rc)
        self.assertIn("grandchild(implementation_worker)", out)
        self.assertIn("anchor_required: True", out)
        self.assertIn("handoff send", out)

    def test_json_payload(self) -> None:
        rc, out = self._run([_candidate("%gw")], as_json=True)
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual(payload["step"]["target_binding"], "project_gateway")
        self.assertTrue(payload["green_path"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
