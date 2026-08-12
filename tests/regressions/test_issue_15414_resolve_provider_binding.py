"""Redmine #15414 — the project-gateway CLI family follows provider_binding.

The Herdr inventory always verified the requested receiver against the scope's
``provider_binding`` (``provider_binding_mismatch``), but every CLI sibling
REQUESTED a hard-coded ``codex``. A workspace whose coordinator role is bound to
``claude`` (#13157 config; the #15255 j#104520 consolidation) therefore failed
closed on a constant: requested (codex) != bound (claude), permanently.

The fix derives the requested receiver from the SAME durable authority the gate
verifies (:func:`resolve_scope_route_provider` /
``ProjectGatewayBackendInventoryUseCase._scope_route_providers``). These tests pin:

1. the helper resolves the BOUND provider (claude-bound coordinator -> ``claude``,
   default binding -> ``codex``; the child route follows the coordinator role);
2. the non-Herdr (tmux) backend keeps the historical ``codex`` default and never
   reads the Herdr scope-binding machinery;
3. the missing / ambiguous scope binding refusals stay typed and fail-closed;
4. the inventory's ``provider_binding_mismatch`` gate is RETAINED for an
   explicitly mismatched requested provider — the fix moves the request onto the
   binding, it does not delete the verification;
5. the read-only ``resolve`` command routes on the resolved provider (a
   claude-bound scope resolves a claude gateway candidate as ``found``).
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (  # noqa: E402,E501
    cli_project_gateway_resolve,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.project_gateway_backend_inventory import (  # noqa: E402,E501
    ProjectGatewayBackendInventoryUseCase,
    ProjectGatewayInventoryError,
    ProjectGatewayInventoryRequest,
    SELECT_CHILD_INTAKE,
    SELECT_CHILD_ROUTE,
    SELECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.project_gateway_route_binding import (  # noqa: E402,E501
    resolve_scope_route_provider,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E402,E501
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    TargetCandidate,
    VIEW_KIND_COCKPIT_PANE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E402,E501
    ParsedRoleBindings,
    WorkflowRoleBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E402,E501
    ROLE_COORDINATOR,
    RoleProviderBinding,
)

PROJECT = "giken-3800-mozyo-bridge"
REPO = "/repo/root"


class _BindingOps:
    """Durable-config-only fake: exactly the surface the helper reads."""

    def __init__(self, *, backend="herdr", bound_provider=None, bindings=None):
        self.backend_value = backend
        self.parsed_reads = 0
        if bindings is None:
            bindings = [
                WorkflowRoleBinding(
                    role="coordinator", project_scope=PROJECT, lane_id="default"
                )
            ]
        self.parsed = ParsedRoleBindings.valid(bindings)
        binding = RoleProviderBinding.default()
        if bound_provider is not None:
            binding = binding.with_overrides({ROLE_COORDINATOR: bound_provider})
        self.binding = binding

    def backend(self, repo_root):
        return self.backend_value

    def parsed_role_bindings(self, repo_root):
        self.parsed_reads += 1
        return self.parsed

    def provider_binding(self, repo_root):
        return self.binding


def _resolve(ops, *, selector=SELECT_GATEWAY, project=PROJECT):
    return resolve_scope_route_provider(
        repo_root=REPO, project_scope=project, selector=selector, ops=ops
    )


class RouteProviderFollowsBindingTest(unittest.TestCase):
    def test_claude_bound_coordinator_resolves_claude(self) -> None:
        self.assertEqual("claude", _resolve(_BindingOps(bound_provider="claude")))

    def test_default_binding_resolves_the_historical_codex(self) -> None:
        self.assertEqual("codex", _resolve(_BindingOps()))

    def test_child_route_follows_the_coordinator_role(self) -> None:
        ops = _BindingOps(bound_provider="claude")
        self.assertEqual("claude", _resolve(ops, selector=SELECT_CHILD_ROUTE))
        self.assertEqual("claude", _resolve(ops, selector=SELECT_CHILD_INTAKE))

    def test_tmux_backend_keeps_codex_and_reads_no_scope_binding(self) -> None:
        # The scope-binding machinery is a Herdr route contract; the tmux route
        # never had the binding gate, so it keeps the historical constant and
        # must not start failing on a workspace without role-binding files.
        ops = _BindingOps(backend="tmux", bound_provider="claude")
        self.assertEqual("codex", _resolve(ops))
        self.assertEqual(0, ops.parsed_reads)

    def test_missing_scope_binding_is_a_typed_refusal(self) -> None:
        ops = _BindingOps(bindings=[])
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            _resolve(ops)
        self.assertEqual("project_scope_binding_missing", caught.exception.reason)

    def test_ambiguous_scope_binding_is_a_typed_refusal(self) -> None:
        ops = _BindingOps(
            bindings=[
                WorkflowRoleBinding(
                    role="coordinator", project_scope=PROJECT, lane_id="default"
                ),
                WorkflowRoleBinding(
                    role="project_gateway", project_scope=PROJECT, lane_id="lane-2"
                ),
            ]
        )
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            _resolve(ops)
        self.assertEqual("project_scope_binding_ambiguous", caught.exception.reason)


class MismatchGateRetainedTest(unittest.TestCase):
    """The fix moves the REQUEST onto the binding; the verifying gate stays."""

    def _discover(self, ops, *, provider):
        return ProjectGatewayBackendInventoryUseCase(ops).discover(
            ProjectGatewayInventoryRequest(
                repo_root=REPO,
                project_scope=PROJECT,
                provider=provider,
                selector=SELECT_GATEWAY,
            )
        )

    def test_an_explicitly_mismatched_provider_still_fails_closed(self) -> None:
        ops = _BindingOps(bound_provider="claude")
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            self._discover(ops, provider="codex")
        self.assertEqual("provider_binding_mismatch", caught.exception.reason)

    def test_the_bound_provider_passes_the_gate(self) -> None:
        # Passing the BOUND provider crosses the binding gate; this minimal fake
        # then fails at the next stage (workspace identity), proving the refusal
        # above is the gate itself and not an earlier stage.
        ops = _BindingOps(bound_provider="claude")
        with self.assertRaises(ProjectGatewayInventoryError) as caught:
            self._discover(ops, provider="claude")
        self.assertEqual("workspace_identity_unavailable", caught.exception.reason)


def _candidate(pane_id, *, role="claude"):
    return TargetCandidate(
        pane_id=pane_id, role=role, role_source=ROLE_SOURCE_PANE_OPTION,
        confidence=CONFIDENCE_STRONG, ambiguous=False, session="gw",
        window_name="cockpit", window_index="0", pane_index="0", active=False,
        workspace_id="ws", workspace_label="gk", lane_id="default", lane_label=None,
        repo_short="root", repo_root=REPO,
        cwd=f"{REPO}/projects/{PROJECT}", host="local",
        view_kind=VIEW_KIND_COCKPIT_PANE, branch="main",
        project_scope=PROJECT, project_path=f"projects/{PROJECT}",
        project_label="label",
    )


class ResolveCliFollowsBindingTest(unittest.TestCase):
    """The read-only resolve command routes on the binding-resolved provider."""

    def _run(self, *, provider, candidates):
        args = argparse.Namespace(
            repo=REPO, project=PROJECT, session=None, as_json=True
        )
        out = io.StringIO()
        with patch.object(
            cli_project_gateway_resolve, "_route_provider", return_value=provider
        ):
            with patch.object(
                cli_project_gateway_resolve,
                "_discover_candidates",
                return_value=candidates,
            ) as discovered:
                with contextlib.redirect_stdout(out):
                    rc = cli_project_gateway_resolve.cmd_project_gateway_resolve(args)
        return rc, json.loads(out.getvalue()), discovered

    def test_claude_bound_scope_resolves_a_claude_gateway_as_found(self) -> None:
        rc, payload, discovered = self._run(
            provider="claude",
            candidates=[_candidate("%gw", role="claude")],
        )
        self.assertEqual(0, rc)
        self.assertEqual("found", payload["status"])
        self.assertEqual("claude", payload["route"]["role"])
        self.assertEqual(
            "claude", discovered.call_args.kwargs.get("provider")
        )

    def test_a_worker_style_candidate_of_the_other_provider_is_not_found(self) -> None:
        rc, payload, _discovered = self._run(
            provider="claude",
            candidates=[_candidate("%w", role="codex")],
        )
        self.assertEqual(1, rc)
        self.assertNotEqual("found", payload["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
