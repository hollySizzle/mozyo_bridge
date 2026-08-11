"""CLI surface for the semantic project-gateway route (Redmine #12668 / #12751).

Pins ``project-gateway resolve`` (read-only text + JSON), ``handoff``,
``consult``, and ``child-intake`` fail-closed behavior with discovery mocked, so
the command layer's classification + exit codes + no-deliver-on-fail-closed
contract are covered without touching tmux. After the Redmine #12751
modularization each subcommand family lives in its own module, so the resolve /
consult / child-intake tests patch + call their own module directly while the
registrar (`cli_project_gateway`) still owns `handoff` and assembles the tree
(`RegistrationTest`). The pure resolver scenarios live in
``test_project_gateway_resolution``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (
    cli_project_gateway,
    cli_project_gateway_child_intake,
    cli_project_gateway_consult,
    cli_project_gateway_resolve,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    VIEW_KIND_NORMAL_WINDOW,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.project_gateway_backend_inventory import (
    LiveProjectGatewayInventoryOps,
    ProjectGatewayBackendInventoryUseCase,
    ProjectGatewayInventoryError,
    ProjectGatewayInventoryRequest,
    ProjectPathAuthority,
    SELECT_CHILD_INTAKE,
    SELECT_CHILD_ROUTE,
    SELECT_GATEWAY,
    prepare_project_gateway_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    ParsedRoleBindings,
    WorkflowRoleBinding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)

REPO = "/work/gk-3500-it-operations"
PROJECT = "giken-cloud-drive-management"


def _candidate(
    pane_id,
    *,
    role="codex",
    repo_root=REPO,
    project_scope=PROJECT,
    session="gw",
    view_kind=VIEW_KIND_COCKPIT_PANE,
):
    return TargetCandidate(
        pane_id=pane_id, role=role, role_source=ROLE_SOURCE_PANE_OPTION,
        confidence=CONFIDENCE_STRONG, ambiguous=False, session=session,
        window_name="cockpit", window_index="0", pane_index="0", active=False,
        workspace_id="ws", workspace_label="gk", lane_id="default", lane_label=None,
        repo_short="gk-3500-it-operations", repo_root=repo_root,
        cwd=f"{repo_root}/projects/{project_scope}", host="local",
        view_kind=view_kind, branch="main",
        project_scope=project_scope, project_path=f"projects/{project_scope}",
        project_label="label",
    )


def _resolve_args(**overrides):
    base = dict(repo=REPO, project=PROJECT, role="codex", session=None, as_json=False)
    base.update(overrides)
    return argparse.Namespace(**base)


class _HerdrInventory(list):
    """Small backend-tagged sequence used to pin the no-tmux CLI branch."""

    backend = "herdr"


class _InventoryOps:
    """Deterministic no-runtime fake for the shared backend inventory."""

    def __init__(self, *, backend="herdr", rows=(), generation="generation-1"):
        self.backend_value = backend
        self.rows = list(rows)
        self.generation = generation
        self.workspace = "workspace-1"
        self.target_root = ""
        self.target_root_unavailable = False
        self.project_path_value = "projects/giken-cloud-drive-management"
        self.project_path_fallback = False
        self.foreign_roots = set()
        self.herdr_reads = 0
        self.tmux_reads = 0
        self.parsed = ParsedRoleBindings.valid(
            [
                WorkflowRoleBinding(
                    role="coordinator",
                    project_scope=PROJECT,
                    lane_id="default",
                )
            ]
        )
        self.binding = RoleProviderBinding.default()

    def backend(self, repo_root):
        return self.backend_value

    def tmux_candidates(self):
        self.tmux_reads += 1
        return [_candidate("%tmux")]

    def parsed_role_bindings(self, repo_root):
        return self.parsed

    def provider_binding(self, repo_root):
        return self.binding

    def workspace_id(self, repo_root):
        if str(repo_root) in self.foreign_roots:
            return "foreign-workspace"
        return self.workspace

    def herdr_rows(self, repo_root):
        self.herdr_reads += 1
        return self.rows

    def generation_token(self, **kwargs):
        return self.generation

    def project_path(self, repo_root, project_scope):
        if self.project_path_fallback:
            return ProjectPathAuthority(
                self.project_path_value,
                fallback_root_scope=True,
            )
        return self.project_path_value

    def target_repo_root(self, cwd, fallback):
        if self.target_root_unavailable:
            return ""
        return self.target_root or str(fallback)


class BackendInventoryTest(unittest.TestCase):
    def _request(self, **overrides):
        values = dict(
            repo_root=REPO,
            project_scope=PROJECT,
            provider="codex",
            selector=SELECT_GATEWAY,
        )
        values.update(overrides)
        return ProjectGatewayInventoryRequest(**values)

    @staticmethod
    def _row(workspace, lane="default", *, locator="w1:p1", role="codex"):
        return {
            "name": encode_assigned_name(workspace, role, lane),
            "pane_id": locator,
            "terminal_id": f"terminal-{locator}",
            "revision": 7,
            "agent": role,
            "agent_status": "idle",
            "cwd": f"{REPO}/projects/{PROJECT}",
        }

    def test_tmux_branch_is_unchanged_and_never_reads_herdr(self):
        ops = _InventoryOps(backend="tmux")
        inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(inventory.backend, "tmux")
        self.assertEqual(inventory[0].pane_id, "%tmux")
        self.assertEqual(ops.tmux_reads, 1)
        self.assertEqual(ops.herdr_reads, 0)

    def test_default_coordinator_binding_projects_truthful_herdr_candidate(self):
        ops = _InventoryOps()
        ops.rows = [self._row(ops.workspace)]
        inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(len(inventory), 1)
        candidate = inventory[0]
        self.assertEqual(candidate.pane_id, encode_assigned_name(ops.workspace, "codex", "default"))
        payload = candidate.to_dict()
        self.assertEqual(payload["runtime"]["provider"], "herdr")
        self.assertEqual(payload["runtime"]["pane_id"], "w1:p1")
        self.assertEqual(payload["runtime"]["assigned_name"], candidate.pane_id)
        observation = inventory.observations[0]
        self.assertEqual(observation.generation_token, "generation-1")
        self.assertEqual(observation.terminal_id, "terminal-w1:p1")
        self.assertIn("terminal-w1:p1", observation.process_generation)

    def test_herdr_session_selector_fails_before_inventory_read(self):
        ops = _InventoryOps()
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(
                self._request(session="tmux-only-selector")
            )
        self.assertEqual(caught.exception.reason, "herdr_session_selector_unsupported")
        self.assertEqual(ops.herdr_reads, 0)

    def test_duplicate_assigned_name_is_typed_failure(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        ops.rows = [row, dict(row)]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_assigned_name_ambiguous")

    def test_duplicate_locator_across_durable_names_is_typed_failure(self):
        ops = _InventoryOps()
        ops.rows = [
            self._row(ops.workspace, locator="w1:p1"),
            self._row(ops.workspace, "child-lane", locator="w1:p1"),
        ]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(
                self._request(selector=SELECT_CHILD_INTAKE)
            )
        self.assertEqual(caught.exception.reason, "herdr_locator_ambiguous")

    def test_unselected_malformed_alias_cannot_hide_duplicate_locator(self):
        ops = _InventoryOps()
        alias = self._row(ops.workspace, "other-lane", locator="w1:p1")
        alias["pane"] = []
        ops.rows = [self._row(ops.workspace, locator="w1:p1"), alias]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_locator_ambiguous")

    def test_conflicting_locator_aliases_are_typed_failure(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row["pane"] = "w9:p9"
        ops.rows = [row]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_locator_evidence_invalid")

    def test_non_text_locator_evidence_is_typed_failure(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row["pane_id"] = 123
        ops.rows = [row]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_locator_evidence_invalid")

    def test_unverified_generation_is_typed_failure(self):
        ops = _InventoryOps(generation="")
        ops.rows = [self._row(ops.workspace)]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_generation_unverified")

    def test_missing_terminal_identity_is_typed_failure_before_generation_read(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row.pop("terminal_id")
        ops.rows = [row]
        ops.generation_token = Mock(return_value="generation-1")
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(
            caught.exception.reason, "herdr_terminal_identity_unavailable"
        )
        ops.generation_token.assert_not_called()

    def test_missing_process_generation_is_typed_failure(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row.pop("revision")
        ops.rows = [row]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(
            caught.exception.reason, "herdr_process_generation_unavailable"
        )

    def test_unreadable_generation_authority_is_typed_failure(self):
        ops = _InventoryOps()
        ops.rows = [self._row(ops.workspace)]
        ops.generation_token = Mock(side_effect=OSError("unreadable"))
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_generation_unavailable")

    def test_detected_live_provider_must_match_durable_name(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row["agent"] = "claude"
        ops.rows = [row]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_live_provider_mismatch")

    def test_missing_target_cwd_is_typed_failure(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row["cwd"] = ""
        ops.rows = [row]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_target_cwd_unavailable")

    def test_unestablished_target_repo_is_typed_failure(self):
        ops = _InventoryOps()
        ops.rows = [self._row(ops.workspace)]
        ops.target_root_unavailable = True
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_target_repo_unavailable")

    def test_project_scope_must_resolve_to_one_adopted_path(self):
        ops = _InventoryOps()
        ops.rows = [self._row(ops.workspace)]
        ops.project_path_value = ""
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "project_scope_path_unavailable")

    def test_repo_level_durable_scope_requires_exact_repo_root_cwd(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row["cwd"] = REPO
        ops.rows = [row]
        ops.project_path_value = "."
        ops.project_path_fallback = True
        inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(
            self._request()
        )
        self.assertEqual(inventory[0].project_path, ".")

        row["cwd"] = f"{REPO}/projects/another-project"
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(
            caught.exception.reason, "herdr_target_project_scope_mismatch"
        )

    def test_adopted_repo_root_scope_allows_a_target_subdirectory(self):
        ops = _InventoryOps()
        row = self._row(ops.workspace)
        row["cwd"] = f"{REPO}/projects/{PROJECT}"
        ops.rows = [row]
        ops.project_path_value = "."

        inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(
            self._request()
        )

        self.assertEqual(inventory[0].project_path, ".")
        self.assertFalse(inventory.observations[0].project_scope_root_fallback)

    def test_child_target_cwd_must_be_inside_requested_project_path(self):
        ops = _InventoryOps()
        child = self._row(ops.workspace, "child-lane")
        child["cwd"] = f"{REPO}/projects/another-project"
        ops.rows = [child]
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(
                self._request(selector=SELECT_CHILD_ROUTE)
            )
        self.assertEqual(
            caught.exception.reason, "herdr_target_project_scope_mismatch"
        )

    def test_target_cwd_in_foreign_workspace_is_typed_failure(self):
        ops = _InventoryOps()
        ops.rows = [self._row(ops.workspace)]
        ops.target_root = "/foreign/repo"
        ops.foreign_roots.add(ops.target_root)
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        self.assertEqual(caught.exception.reason, "herdr_target_workspace_mismatch")

    def test_child_selectors_exclude_default_parent_and_worker(self):
        ops = _InventoryOps()
        ops.rows = [
            self._row(ops.workspace),
            self._row(ops.workspace, "lane-child", locator="w1:p2"),
            self._row(
                ops.workspace,
                "lane-child",
                locator="w1:p3",
                role="claude",
            ),
        ]
        route_inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(
            self._request(selector=SELECT_CHILD_ROUTE)
        )
        self.assertEqual([c.lane_id for c in route_inventory], ["lane-child"])
        intake_inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(
            self._request(selector=SELECT_CHILD_INTAKE)
        )
        self.assertEqual(
            {c.lane_id for c in intake_inventory}, {"default", "lane-child"}
        )
        self.assertEqual(
            intake_inventory.gateway_assigned_name,
            encode_assigned_name(ops.workspace, "codex", "default"),
        )

    def test_child_intake_honors_distinct_parent_and_child_provider_bindings(self):
        ops = _InventoryOps()
        ops.parsed = ParsedRoleBindings.valid(
            [
                WorkflowRoleBinding(
                    role="project_gateway",
                    project_scope=PROJECT,
                    lane_id="gateway-lane",
                )
            ]
        )
        ops.binding = ops.binding.with_overrides({"project_gateway": "grok"})
        ops.rows = [
            self._row(
                ops.workspace,
                "gateway-lane",
                locator="w1:p1",
                role="grok",
            ),
            self._row(ops.workspace, "child-lane", locator="w1:p2"),
        ]
        inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(
            self._request(selector=SELECT_CHILD_INTAKE)
        )
        self.assertEqual({candidate.role for candidate in inventory}, {"codex", "grok"})
        self.assertEqual(
            inventory.gateway_assigned_name,
            encode_assigned_name(ops.workspace, "grok", "gateway-lane"),
        )

    def test_required_herdr_backend_drift_refuses_before_tmux_read(self):
        ops = _InventoryOps(backend="tmux")
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            ProjectGatewayBackendInventoryUseCase(ops).discover(
                self._request(required_backend="herdr")
            )
        self.assertEqual(caught.exception.reason, "backend_changed")
        self.assertEqual(ops.tmux_reads, 0)

    def test_delivery_revalidation_carries_generation_bound_capability(self):
        ops = _InventoryOps()
        ops.rows = [self._row(ops.workspace)]
        inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution."
            "application.project_gateway_backend_inventory.discover_project_gateway_inventory",
            return_value=inventory,
        ):
            prepared = prepare_project_gateway_delivery(inventory, inventory[0])
        self.assertEqual(prepared.target, inventory[0].pane_id)
        self.assertEqual(prepared.target_lane, "default")
        self.assertEqual(prepared.capability.generation_token, "generation-1")
        self.assertEqual(prepared.capability.terminal_id, "terminal-w1:p1")
        self.assertEqual(
            prepared.capability.process_generation,
            inventory.observations[0].process_generation,
        )
        self.assertEqual(prepared.capability.purpose, "project_gateway")

    def test_delivery_revalidation_refuses_generation_drift(self):
        ops = _InventoryOps()
        ops.rows = [self._row(ops.workspace)]
        inventory = ProjectGatewayBackendInventoryUseCase(ops).discover(self._request())
        drift_ops = _InventoryOps(generation="generation-2")
        drift_ops.rows = [self._row(drift_ops.workspace)]
        fresh = ProjectGatewayBackendInventoryUseCase(drift_ops).discover(self._request())
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution."
            "application.project_gateway_backend_inventory.discover_project_gateway_inventory",
            return_value=fresh,
        ):
            with self.assertRaises(ProjectGatewayInventoryError) as caught:
                prepare_project_gateway_delivery(inventory, inventory[0])
        self.assertEqual(caught.exception.reason, "herdr_inventory_generation_changed")


class LiveProjectPathAuthorityTest(unittest.TestCase):
    def test_repo_root_fallback_requires_no_adopted_project_descriptors(self):
        ops = LiveProjectGatewayInventoryOps()
        resolver = (
            "mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity."
            "application.project_discovery.resolve_project_scopes"
        )
        with patch(resolver, return_value=([], [])):
            authority = ops.project_path(Path(REPO), PROJECT)
            self.assertEqual(authority.path, ".")
            self.assertTrue(authority.fallback_root_scope)

        other = SimpleNamespace(scope="another-project", path="projects/another")
        with patch(resolver, return_value=([other], [])):
            with self.assertRaises(ValueError):
                ops.project_path(Path(REPO), PROJECT)

        exact = SimpleNamespace(scope=PROJECT, path=f"projects/{PROJECT}")
        with patch(resolver, return_value=([other, exact], [])):
            authority = ops.project_path(Path(REPO), PROJECT)
            self.assertEqual(authority.path, f"projects/{PROJECT}")
            self.assertFalse(authority.fallback_root_scope)

        adopted_root = SimpleNamespace(scope=PROJECT, path=".")
        with patch(resolver, return_value=([adopted_root], [])):
            authority = ops.project_path(Path(REPO), PROJECT)
            self.assertEqual(authority.path, ".")
            self.assertFalse(authority.fallback_root_scope)


@patch.object(cli_project_gateway_resolve, "require_tmux", lambda: None)
class ResolveCliTest(unittest.TestCase):
    # Redmine #12751: the read-only resolve handler + the shared resolution core
    # moved to `cli_project_gateway_resolve`; patch/call that module directly.
    def _run(self, args, candidates):
        out = io.StringIO()
        with patch.object(cli_project_gateway_resolve, "_discover_candidates", return_value=candidates):
            with contextlib.redirect_stdout(out):
                rc = cli_project_gateway_resolve.cmd_project_gateway_resolve(args)
        return rc, out.getvalue()

    def test_found_text(self):
        rc, text = self._run(_resolve_args(), [_candidate("%gw"), _candidate("%w", role="claude")])
        self.assertEqual(rc, 0)
        self.assertIn("status: found", text)
        self.assertIn("pane_id=%gw", text)
        self.assertIn("project-gateway handoff", text)

    def test_found_json(self):
        rc, text = self._run(_resolve_args(as_json=True), [_candidate("%gw")])
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        self.assertEqual(payload["status"], "found")
        self.assertEqual(payload["selected"]["runtime"]["pane_id"], "%gw")

    def test_missing_names_start_action(self):
        rc, text = self._run(_resolve_args(), [_candidate("%w", role="claude")])
        self.assertEqual(rc, 1)
        self.assertIn("status: gateway_missing", text)
        self.assertIn("start_project_gateway", text)
        self.assertIn("mozyo-bridge cockpit", text)
        # Start action is cwd-authority; must not misdirect via --repo (j#66626 b1).
        self.assertNotIn("cockpit --repo", text)

    def test_missing_names_cockpit_visible_startup(self):
        # Redmine #12699: the start action is a cockpit-visible Unit, and the
        # launch command warns against the detached / preview escape hatch.
        rc, text = self._run(_resolve_args(), [_candidate("%w", role="claude")])
        self.assertEqual(rc, 1)
        self.assertIn("cockpit-visible Unit", text)
        self.assertIn("--no-attach", text)

    def test_claude_worker_is_not_resolved_as_gateway(self):
        # Only a Claude worker is up; the gateway role is fixed to codex, so this
        # is gateway_missing, never a resolved Claude target (j#66626 blocker 2).
        rc, text = self._run(_resolve_args(), [_candidate("%w", role="claude")])
        self.assertEqual(rc, 1)
        self.assertIn("role=codex", text)
        self.assertNotIn("status: found", text)

    def test_ambiguous_lists_candidates(self):
        rc, text = self._run(
            _resolve_args(),
            [_candidate("%gw1", session="a"), _candidate("%gw2", session="b")],
        )
        self.assertEqual(rc, 1)
        self.assertIn("status: gateway_target_ambiguous", text)
        self.assertIn("%gw1", text)
        self.assertIn("%gw2", text)
        self.assertIn("--session", text)

    def test_herdr_inventory_never_requires_tmux(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway_resolve, "require_tmux") as require:
            with patch.object(
                cli_project_gateway_resolve,
                "_discover_candidates",
                return_value=_HerdrInventory([_candidate("mzb1_gateway")]),
            ):
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_resolve.cmd_project_gateway_resolve(
                        _resolve_args()
                    )
        self.assertEqual(rc, 0)
        require.assert_not_called()
        self.assertIn("status: found", out.getvalue())


@patch.object(cli_project_gateway, "require_tmux", lambda: None)
class AdoptAndRoutePlanBackendTest(unittest.TestCase):
    """The three read-only reproductions use the shared backend inventory."""

    @staticmethod
    def _adopt_args():
        return argparse.Namespace(
            repo=REPO,
            project=PROJECT,
            session=None,
            as_json=True,
        )

    @staticmethod
    def _route_args(from_role):
        return argparse.Namespace(
            from_role=from_role,
            repo=REPO,
            project=PROJECT,
            session=None,
            as_json=True,
        )

    def test_adopt_herdr_inventory_never_requires_tmux(self):
        inventory = _HerdrInventory(
            [_candidate("mzb1_gateway", view_kind=VIEW_KIND_NORMAL_WINDOW)]
        )
        out = io.StringIO()
        with patch.object(cli_project_gateway, "require_tmux") as require, patch.object(
            cli_project_gateway,
            "_discover_candidates",
            return_value=inventory,
        ) as discover, contextlib.redirect_stdout(out):
            rc = cli_project_gateway.cmd_project_gateway_adopt(self._adopt_args())

        # A truthful Herdr target is not a tmux cockpit pane, so the historical
        # startup-evidence contract remains non-green even though identity resolves.
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["action"], "adopt")
        require.assert_not_called()
        self.assertEqual(discover.call_args.kwargs["selector"], SELECT_GATEWAY)

    def test_route_plan_grandparent_uses_gateway_inventory_without_tmux(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway, "require_tmux") as require, patch.object(
            cli_project_gateway,
            "_discover_candidates",
            return_value=_HerdrInventory(
                [_candidate("mzb1_gateway", view_kind=VIEW_KIND_NORMAL_WINDOW)]
            ),
        ) as discover, contextlib.redirect_stdout(out):
            rc = cli_project_gateway.cmd_project_gateway_route_plan(
                self._route_args("grandparent_coordinator")
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out.getvalue())["launch_or_adopt"]["action"], "adopt"
        )
        require.assert_not_called()
        self.assertEqual(discover.call_args.kwargs["selector"], SELECT_GATEWAY)

    def test_route_plan_project_gateway_uses_child_inventory_without_tmux(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway, "require_tmux") as require, patch.object(
            cli_project_gateway,
            "_discover_candidates",
            return_value=_HerdrInventory(
                [_candidate("mzb1_child", view_kind=VIEW_KIND_NORMAL_WINDOW)]
            ),
        ) as discover, contextlib.redirect_stdout(out):
            rc = cli_project_gateway.cmd_project_gateway_route_plan(
                self._route_args("project_gateway")
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out.getvalue())["step"]["target_binding"],
            "delegated_coordinator",
        )
        require.assert_not_called()
        self.assertEqual(discover.call_args.kwargs["selector"], SELECT_CHILD_ROUTE)


@patch.object(cli_project_gateway, "require_tmux", lambda: None)
class HandoffCliTest(unittest.TestCase):
    def _handoff_args(self, **overrides):
        base = dict(
            to="codex", target_repo=REPO, target_project=PROJECT, target=None,
            gateway_session=None, as_json=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_refuses_when_missing_does_not_deliver(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude")]):
            with patch.object(cli_project_gateway, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args())
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIn("gateway_missing", out.getvalue())

    def test_found_injects_pane_and_delegates(self):
        captured = {}

        def fake_orch(args):
            captured["target"] = args.target
            return 0

        with patch.object(cli_project_gateway, "_discover_candidates",
                          return_value=[_candidate("%gw")]):
            with patch.object(cli_project_gateway, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args())
        self.assertEqual(rc, 0)
        self.assertEqual(captured["target"], "%gw")

    def test_action_time_inventory_drift_is_zero_send(self):
        inventory = _HerdrInventory([_candidate("mzb1_gateway")])
        error = ProjectGatewayInventoryError(
            "herdr_inventory_generation_changed",
            "generation changed",
            backend="herdr",
        )
        with patch.object(
            cli_project_gateway, "_discover_candidates", return_value=inventory
        ), patch.object(
            cli_project_gateway,
            "prepare_project_gateway_delivery",
            side_effect=error,
        ), patch.object(cli_project_gateway, "orchestrate_handoff") as orchestrate:
            rc = cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args())
        self.assertEqual(rc, 1)
        orchestrate.assert_not_called()

    def test_found_auto_injects_grandparent_transition_role(self):
        # Redmine #12706: project-gateway handoff IS the grandparent ->
        # project-gateway transition, so a `found` resolution auto-injects the
        # grandparent_coordinator boundary onto the standard payload (the operator
        # never types it). The receiver gateway then owns the project-domain /
        # no_dispatch decision the grandparent must not pre-empt.
        captured = {}

        def fake_orch(args):
            captured["transition_role"] = getattr(args, "transition_role", None)
            return 0

        with patch.object(cli_project_gateway, "_discover_candidates",
                          return_value=[_candidate("%gw")]):
            with patch.object(cli_project_gateway, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args())
        self.assertEqual(rc, 0)
        self.assertEqual(captured["transition_role"], "grandparent_coordinator")

    def test_found_auto_injects_workflow_contract(self):
        # Redmine #12700: the same grandparent -> project-gateway transition also
        # auto-injects the workflow-contract reference bundle (keyed by the same
        # grandparent role token) on a `found` resolution, so the receiver gateway
        # knows the required workflow contract docs as a normal-operation contract.
        captured = {}

        def fake_orch(args):
            captured["workflow_contract"] = getattr(args, "workflow_contract", None)
            return 0

        with patch.object(cli_project_gateway, "_discover_candidates",
                          return_value=[_candidate("%gw")]):
            with patch.object(cli_project_gateway, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args())
        self.assertEqual(rc, 0)
        self.assertEqual(captured["workflow_contract"], "grandparent_coordinator")

    def test_fail_closed_does_not_inject_transition_role(self):
        # A non-found resolution does not deliver, so no boundary is injected; the
        # args carry no transition_role for a route that never reached the gateway.
        args = self._handoff_args()
        out = io.StringIO()
        with patch.object(cli_project_gateway, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude")]):
            with patch.object(cli_project_gateway, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway.cmd_project_gateway_handoff(args)
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIsNone(getattr(args, "transition_role", None))
        self.assertIsNone(getattr(args, "workflow_contract", None))

    def test_rejects_explicit_target(self):
        with patch.object(cli_project_gateway, "_discover_candidates", return_value=[]):
            with self.assertRaises(SystemExit):
                cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args(target="%99"))

    def test_rejects_auto_target_repo(self):
        with patch.object(cli_project_gateway, "_discover_candidates", return_value=[]):
            with self.assertRaises(SystemExit):
                cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args(target_repo="auto"))

    def test_rejects_direct_claude_send(self):
        # The gateway is a Codex unit; --to claude must fail closed before any
        # resolution/delivery so the project Claude worker is never direct-sent
        # (review j#66626 blocker 2).
        with patch.object(cli_project_gateway, "_discover_candidates") as disc:
            with patch.object(cli_project_gateway, "orchestrate_handoff") as orch:
                with self.assertRaises(SystemExit):
                    cli_project_gateway.cmd_project_gateway_handoff(self._handoff_args(to="claude"))
        disc.assert_not_called()
        orch.assert_not_called()


@patch.object(cli_project_gateway_consult, "require_tmux", lambda: None)
class ConsultCliTest(unittest.TestCase):
    """`project-gateway consult` — the forward no-anchor consultation (#12740).

    Redmine #12751: the consult handler moved to its own bounded module
    `cli_project_gateway_consult` with this independent test target; patch/call that
    module directly. The fail-closed paths render through the shared pure renderer
    over the already-computed resolution (no second discovery scan).
    """

    def _consult_args(self, **overrides):
        base = dict(
            to="codex", target_repo=REPO, target_project=PROJECT, target=None,
            gateway_session=None, as_json=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_refuses_when_missing_does_not_deliver(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway_consult, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude")]):
            with patch.object(cli_project_gateway_consult, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args())
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIn("gateway_missing", out.getvalue())

    def test_ambiguous_fails_closed_no_delivery(self):
        # Two distinct gateway lanes -> ambiguous; refuse to auto-select and do not
        # deliver (the same fail-closed contract as `resolve` / `handoff`).
        out = io.StringIO()
        with patch.object(cli_project_gateway_consult, "_discover_candidates",
                          return_value=[_candidate("%gw1", session="a"), _candidate("%gw2", session="b")]):
            with patch.object(cli_project_gateway_consult, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args())
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIn("status: gateway_target_ambiguous", out.getvalue())

    def test_fail_closed_json_emits_resolution_payload(self):
        # --json on a fail-closed resolution emits the structured GatewayResolution
        # payload (the shared renderer over the already-computed resolution).
        out = io.StringIO()
        with patch.object(cli_project_gateway_consult, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude")]):
            with patch.object(cli_project_gateway_consult, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_consult.cmd_project_gateway_consult(
                        self._consult_args(as_json=True)
                    )
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "gateway_missing")

    def test_found_delivers_no_anchor_consultation(self):
        captured = {}

        def fake_orch(args, **kwargs):
            captured["target"] = args.target
            captured["ticketless"] = kwargs.get("ticketless")
            captured["ticketless_consultation"] = kwargs.get("ticketless_consultation")
            captured["default_kind"] = kwargs.get("default_kind")
            captured["consultation_kind"] = getattr(args, "consultation_kind", None)
            captured["callback_to_role"] = getattr(args, "callback_to_role", None)
            captured["callback_methods"] = getattr(args, "callback_methods", None)
            captured["read_contract"] = getattr(args, "read_contract", None)
            captured["transition_role"] = getattr(args, "transition_role", None)
            captured["workflow_contract"] = getattr(args, "workflow_contract", None)
            # The forward rail must never fabricate a Redmine anchor.
            captured["source"] = getattr(args, "source", None)
            captured["issue"] = getattr(args, "issue", None)
            captured["journal"] = getattr(args, "journal", None)
            return 0

        with patch.object(cli_project_gateway_consult, "_discover_candidates",
                          return_value=[_candidate("%gw")]):
            with patch.object(cli_project_gateway_consult, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args())
        self.assertEqual(rc, 0)
        self.assertEqual(captured["target"], "%gw")
        self.assertTrue(captured["ticketless"])
        self.assertTrue(captured["ticketless_consultation"])
        self.assertEqual(captured["default_kind"], "design_consultation")
        # The forward consultation payload is injected programmatically.
        self.assertEqual(captured["consultation_kind"], "project_domain_consultation")
        self.assertEqual(captured["callback_to_role"], "grandparent_coordinator")
        self.assertEqual(
            captured["callback_methods"],
            ["ticketless_callback", "q_enter_consultation_callback"],
        )
        self.assertEqual(captured["read_contract"], "project_gateway")
        # The same transition boundary + workflow contract auto-inject as handoff.
        self.assertEqual(captured["transition_role"], "grandparent_coordinator")
        self.assertEqual(captured["workflow_contract"], "grandparent_coordinator")
        # No Redmine anchor was fabricated on the forward leg.
        self.assertIsNone(captured["source"])
        self.assertIsNone(captured["issue"])
        self.assertIsNone(captured["journal"])

    def test_found_injects_resolved_pane_not_caller(self):
        # Among several candidates the single matching gateway pane is injected,
        # never some other discovered lane.
        captured = {}

        def fake_orch(args, **kwargs):
            captured["target"] = args.target
            return 0

        with patch.object(cli_project_gateway_consult, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude"), _candidate("%gw")]):
            with patch.object(cli_project_gateway_consult, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args())
        self.assertEqual(rc, 0)
        self.assertEqual(captured["target"], "%gw")

    def test_action_time_inventory_drift_is_zero_send(self):
        args = self._consult_args()
        error = ProjectGatewayInventoryError(
            "herdr_inventory_generation_changed",
            "generation changed",
            backend="herdr",
        )
        with patch.object(
            cli_project_gateway_consult,
            "_discover_candidates",
            return_value=_HerdrInventory([_candidate("mzb1_gateway")]),
        ), patch.object(
            cli_project_gateway_consult,
            "prepare_project_gateway_delivery",
            side_effect=error,
        ), patch.object(
            cli_project_gateway_consult, "orchestrate_handoff"
        ) as orchestrate:
            rc = cli_project_gateway_consult.cmd_project_gateway_consult(args)
        self.assertEqual(rc, 1)
        orchestrate.assert_not_called()
        self.assertIsNone(getattr(args, "consultation_kind", None))

    def test_fail_closed_does_not_inject_consultation_payload(self):
        args = self._consult_args()
        out = io.StringIO()
        with patch.object(cli_project_gateway_consult, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude")]):
            with patch.object(cli_project_gateway_consult, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_consult.cmd_project_gateway_consult(args)
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIsNone(getattr(args, "consultation_kind", None))
        self.assertIsNone(getattr(args, "transition_role", None))
        self.assertIsNone(getattr(args, "workflow_contract", None))
        self.assertIsNone(getattr(args, "read_contract", None))

    def test_rejects_explicit_target_pane_authority(self):
        with patch.object(cli_project_gateway_consult, "_discover_candidates") as disc:
            with self.assertRaises(SystemExit):
                cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args(target="%99"))
        # The pane-authority refusal is a pre-resolution validation gate.
        disc.assert_not_called()

    def test_rejects_auto_target_repo(self):
        with patch.object(cli_project_gateway_consult, "_discover_candidates") as disc:
            with self.assertRaises(SystemExit):
                cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args(target_repo="auto"))
        disc.assert_not_called()

    def test_rejects_missing_target_project(self):
        with patch.object(cli_project_gateway_consult, "_discover_candidates") as disc:
            with self.assertRaises(SystemExit):
                cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args(target_project=None))
        disc.assert_not_called()

    def test_rejects_direct_claude_send(self):
        with patch.object(cli_project_gateway_consult, "_discover_candidates") as disc:
            with patch.object(cli_project_gateway_consult, "orchestrate_handoff") as orch:
                with self.assertRaises(SystemExit):
                    cli_project_gateway_consult.cmd_project_gateway_consult(self._consult_args(to="claude"))
        disc.assert_not_called()
        orch.assert_not_called()


@patch.object(cli_project_gateway_child_intake, "require_tmux", lambda: None)
class ChildIntakeCliTest(unittest.TestCase):
    """`project-gateway child-intake` — the forward no-anchor work-intake (#12748)."""

    def _intake_args(self, **overrides):
        base = dict(
            to="codex", target_repo=REPO, target_project=PROJECT, target=None,
            from_pane="%parent", work_shape=None, gateway_session=None, as_json=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_resolved_delivers_no_anchor_work_intake(self):
        captured = {}

        def fake_orch(args, **kwargs):
            captured["target"] = args.target
            captured["ticketless"] = kwargs.get("ticketless")
            captured["ticketless_work_intake"] = kwargs.get("ticketless_work_intake")
            captured["default_kind"] = kwargs.get("default_kind")
            captured["work_shape"] = getattr(args, "work_shape", None)
            captured["callback_to_role"] = getattr(args, "callback_to_role", None)
            captured["callback_methods"] = getattr(args, "callback_methods", None)
            captured["read_contract"] = getattr(args, "read_contract", None)
            # This leg injects NO transition_role / workflow_contract boundary.
            captured["transition_role"] = getattr(args, "transition_role", None)
            captured["workflow_contract"] = getattr(args, "workflow_contract", None)
            # The forward rail must never fabricate a Redmine anchor.
            captured["source"] = getattr(args, "source", None)
            captured["issue"] = getattr(args, "issue", None)
            captured["journal"] = getattr(args, "journal", None)
            return 0

        with patch.object(cli_project_gateway_child_intake, "_discover_candidates",
                          return_value=[_candidate("%parent"), _candidate("%child")]):
            with patch.object(cli_project_gateway_child_intake, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway_child_intake.cmd_project_gateway_child_intake(self._intake_args())
        self.assertEqual(rc, 0)
        # The resolved CHILD pane is injected, never the caller's own (%parent).
        self.assertEqual(captured["target"], "%child")
        self.assertTrue(captured["ticketless"])
        self.assertTrue(captured["ticketless_work_intake"])
        self.assertEqual(captured["default_kind"], "design_consultation")
        self.assertEqual(captured["work_shape"], "domain_design_work_intake")
        # The child returns to the parent gateway and acts under its own contract.
        self.assertEqual(captured["callback_to_role"], "project_gateway")
        self.assertEqual(captured["read_contract"], "delegated_coordinator")
        self.assertEqual(
            captured["callback_methods"],
            ["ticketless_callback", "q_enter_consultation_callback"],
        )
        # No #12706 boundary on this leg (the envelope carries the contract).
        self.assertIsNone(captured["transition_role"])
        self.assertIsNone(captured["workflow_contract"])
        # No Redmine anchor was fabricated on the forward leg.
        self.assertIsNone(captured["source"])
        self.assertIsNone(captured["issue"])
        self.assertIsNone(captured["journal"])

    def test_action_time_inventory_drift_is_zero_send(self):
        args = self._intake_args()
        error = ProjectGatewayInventoryError(
            "herdr_inventory_generation_changed",
            "generation changed",
            backend="herdr",
        )
        with patch.object(
            cli_project_gateway_child_intake,
            "_discover_candidates",
            return_value=_HerdrInventory(
                [_candidate("%parent"), _candidate("mzb1_child")]
            ),
        ), patch.object(
            cli_project_gateway_child_intake,
            "prepare_project_gateway_delivery",
            side_effect=error,
        ), patch.object(
            cli_project_gateway_child_intake, "orchestrate_handoff"
        ) as orchestrate:
            rc = cli_project_gateway_child_intake.cmd_project_gateway_child_intake(args)
        self.assertEqual(rc, 1)
        orchestrate.assert_not_called()
        self.assertIsNone(getattr(args, "work_shape", None))
        self.assertIsNone(getattr(args, "read_contract", None))

    def test_same_lane_fails_closed_no_delivery(self):
        # The only coordinator lane is the caller itself -> same_lane; do not adopt
        # the parent as its own child, and do not deliver.
        out = io.StringIO()
        args = self._intake_args()
        with patch.object(cli_project_gateway_child_intake, "_discover_candidates",
                          return_value=[_candidate("%parent")]):
            with patch.object(cli_project_gateway_child_intake, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_child_intake.cmd_project_gateway_child_intake(args)
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIn("status: same_lane", out.getvalue())
        # No work-intake payload was injected on the fail-closed path.
        self.assertIsNone(getattr(args, "read_contract", None))

    def test_missing_child_fails_closed(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway_child_intake, "_discover_candidates",
                          return_value=[_candidate("%w", role="claude")]):
            with patch.object(cli_project_gateway_child_intake, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_child_intake.cmd_project_gateway_child_intake(self._intake_args())
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIn("status: child_missing", out.getvalue())

    def test_ambiguous_child_fails_closed(self):
        out = io.StringIO()
        with patch.object(cli_project_gateway_child_intake, "_discover_candidates",
                          return_value=[_candidate("%parent"), _candidate("%c1"), _candidate("%c2")]):
            with patch.object(cli_project_gateway_child_intake, "orchestrate_handoff") as orch:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_child_intake.cmd_project_gateway_child_intake(self._intake_args())
        self.assertEqual(rc, 1)
        orch.assert_not_called()
        self.assertIn("status: child_ambiguous", out.getvalue())

    def test_requires_from_pane_self_fence(self):
        with patch.object(cli_project_gateway_child_intake, "_discover_candidates") as disc:
            with self.assertRaises(SystemExit):
                cli_project_gateway_child_intake.cmd_project_gateway_child_intake(
                    self._intake_args(from_pane=None)
                )
        disc.assert_not_called()

    def test_rejects_explicit_target_pane_authority(self):
        with patch.object(cli_project_gateway_child_intake, "_discover_candidates", return_value=[]):
            with self.assertRaises(SystemExit):
                cli_project_gateway_child_intake.cmd_project_gateway_child_intake(
                    self._intake_args(target="%99")
                )

    def test_rejects_direct_claude_send(self):
        with patch.object(cli_project_gateway_child_intake, "_discover_candidates") as disc:
            with patch.object(cli_project_gateway_child_intake, "orchestrate_handoff") as orch:
                with self.assertRaises(SystemExit):
                    cli_project_gateway_child_intake.cmd_project_gateway_child_intake(
                        self._intake_args(to="claude")
                    )
        disc.assert_not_called()
        orch.assert_not_called()

    def test_explicit_work_shape_is_forwarded(self):
        captured = {}

        def fake_orch(args, **kwargs):
            captured["work_shape"] = getattr(args, "work_shape", None)
            return 0

        with patch.object(cli_project_gateway_child_intake, "_discover_candidates",
                          return_value=[_candidate("%parent"), _candidate("%child")]):
            with patch.object(cli_project_gateway_child_intake, "orchestrate_handoff", side_effect=fake_orch):
                rc = cli_project_gateway_child_intake.cmd_project_gateway_child_intake(
                    self._intake_args(work_shape="implementation_work_intake")
                )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["work_shape"], "implementation_work_intake")


class RegistrationTest(unittest.TestCase):
    """The `project-gateway` subcommand tree is assembled in one place (#12751).

    Pins that the registrar wires every subcommand family (including the extracted
    `resolve` / `consult` / `child-intake` modules) and that their handler bindings
    + read-only flags survive parsing, so the modularization preserves the parser
    help / validation surface.
    """

    def _gateway_subparser_names(self):
        parser = argparse.ArgumentParser(prog="mozyo-bridge")
        sub = parser.add_subparsers(dest="command")
        cli_project_gateway.register(sub)
        # `sub` is the `_SubParsersAction`; its `.choices` hold the registered
        # parsers. The single `project-gateway` parser holds the family subparsers.
        gateway_parser = sub.choices["project-gateway"]
        family = next(
            a for a in gateway_parser._actions  # noqa: SLF001 - argparse exposes choices here
            if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
        )
        return parser, family.choices

    def test_all_subcommands_registered_in_one_place(self):
        _, names = self._gateway_subparser_names()
        self.assertEqual(
            set(names),
            {"resolve", "adopt", "route-plan", "handoff", "consult", "child-intake"},
        )

    def test_subcommands_bind_their_extracted_handlers(self):
        _, names = self._gateway_subparser_names()
        # The extracted modules own their handlers; the registrar wires them.
        self.assertIs(
            names["resolve"].get_default("func"),
            cli_project_gateway_resolve.cmd_project_gateway_resolve,
        )
        self.assertIs(
            names["consult"].get_default("func"),
            cli_project_gateway_consult.cmd_project_gateway_consult,
        )
        self.assertIs(
            names["child-intake"].get_default("func"),
            cli_project_gateway_child_intake.cmd_project_gateway_child_intake,
        )

    def test_resolve_parses_read_only_flags(self):
        parser, _ = self._gateway_subparser_names()
        ns = parser.parse_args(
            ["project-gateway", "resolve", "--repo", REPO, "--project", PROJECT, "--json"]
        )
        self.assertTrue(ns.as_json)
        self.assertIs(ns.func, cli_project_gateway_resolve.cmd_project_gateway_resolve)

    def test_consult_parses_semantic_route_flags(self):
        parser, _ = self._gateway_subparser_names()
        ns = parser.parse_args(
            ["project-gateway", "consult", "--to", "codex",
             "--target-repo", REPO, "--target-project", PROJECT]
        )
        self.assertEqual(ns.to, "codex")
        self.assertEqual(ns.target_project, PROJECT)
        self.assertIs(ns.func, cli_project_gateway_consult.cmd_project_gateway_consult)


class PublicImportCompatTest(unittest.TestCase):
    """Pre-#12751 public import surface is preserved (Review Gate j#68486 finding 1).

    The resolve / consult handler bodies moved to sibling modules, but the
    registrar was previously the import / patch seam for the
    `cmd_project_gateway_resolve` / `cmd_project_gateway_consult` handler symbols,
    so `from ...cli_project_gateway import cmd_project_gateway_*` must keep
    resolving to the same handler objects.
    """

    def test_registrar_reexports_moved_handlers(self):
        self.assertIs(
            cli_project_gateway.cmd_project_gateway_resolve,
            cli_project_gateway_resolve.cmd_project_gateway_resolve,
        )
        self.assertIs(
            cli_project_gateway.cmd_project_gateway_consult,
            cli_project_gateway_consult.cmd_project_gateway_consult,
        )

    def test_registrar_keeps_in_place_handlers(self):
        # The handlers that stayed in the registrar remain importable from it.
        for name in (
            "cmd_project_gateway_adopt",
            "cmd_project_gateway_handoff",
            "cmd_project_gateway_route_plan",
            "register",
        ):
            self.assertTrue(hasattr(cli_project_gateway, name), name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
