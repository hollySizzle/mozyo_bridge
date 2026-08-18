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
    cli_project_gateway,
    cli_project_gateway_child_intake,
    cli_project_gateway_consult,
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
    ROLE_IMPLEMENTER,
    ROLE_PROJECT_GATEWAY,
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


class AdoptAndRoutePlanFollowBindingTest(unittest.TestCase):
    """Review j#104586 finding_routeidentity: launch-or-adopt and route-plan match
    the SAME bound provider the inventory was fetched with — a live bound lane is
    adopted, never refused as role_mismatch with a duplicate launch advised."""

    def _adopt(self, *, provider, candidates):
        args = argparse.Namespace(
            repo=REPO, project=PROJECT, session=None, as_json=True
        )
        out = io.StringIO()
        with patch.object(cli_project_gateway, "require_tmux", lambda: None):
            with patch.object(
                cli_project_gateway, "_route_provider", return_value=provider
            ):
                with patch.object(
                    cli_project_gateway,
                    "_discover_candidates",
                    return_value=candidates,
                ):
                    with contextlib.redirect_stdout(out):
                        cli_project_gateway.cmd_project_gateway_adopt(args)
        return json.loads(out.getvalue())

    def _route_plan(self, *, provider, candidates, from_role):
        args = argparse.Namespace(
            from_role=from_role, repo=REPO, project=PROJECT, session=None,
            as_json=True,
        )
        out = io.StringIO()
        with patch.object(cli_project_gateway, "require_tmux", lambda: None):
            with patch.object(
                cli_project_gateway, "_route_provider", return_value=provider
            ):
                with patch.object(
                    cli_project_gateway,
                    "_discover_candidates",
                    return_value=candidates,
                ):
                    with contextlib.redirect_stdout(out):
                        cli_project_gateway.cmd_project_gateway_route_plan(args)
        return json.loads(out.getvalue())

    def test_claude_bound_adopt_adopts_the_live_claude_lane(self) -> None:
        payload = self._adopt(
            provider="claude", candidates=[_candidate("%gw", role="claude")]
        )
        self.assertEqual("adopt", payload["action"])

    def test_codex_bound_adopt_is_unchanged(self) -> None:
        payload = self._adopt(
            provider="codex", candidates=[_candidate("%gw", role="codex")]
        )
        self.assertEqual("adopt", payload["action"])

    def test_claude_bound_grandparent_route_plan_adopts_the_live_lane(self) -> None:
        payload = self._route_plan(
            provider="claude",
            candidates=[_candidate("%gw", role="claude")],
            from_role="grandparent_coordinator",
        )
        self.assertEqual("adopt", payload["launch_or_adopt"]["action"])
        self.assertEqual("claude", payload["step"]["target_role"])

    def test_claude_bound_parent_route_plan_adopts_the_live_child_lane(self) -> None:
        payload = self._route_plan(
            provider="claude",
            candidates=[_candidate("%child", role="claude")],
            from_role="project_gateway",
        )
        self.assertEqual("adopt", payload["launch_or_adopt"]["action"])
        self.assertEqual("claude", payload["step"]["target_role"])

    def test_worker_step_keeps_its_own_contract(self) -> None:
        # SELECT_NONE / implementation-worker leg: no coordinator override.
        payload = self._route_plan(
            provider="codex",
            candidates=[],
            from_role="delegated_coordinator",
        )
        self.assertEqual("claude", payload["step"]["target_role"])
        self.assertIsNone(payload["launch_or_adopt"])


class BoundReceiverGateMessageTest(unittest.TestCase):
    """Review j#104586 finding_contracttext (behavioral half): the `--to` refusal
    names the provider the binding actually resolves, in both directions."""

    def _abort_message(self, module, cmd, args, *, provider):
        # `die` raises CommandAbort, a SystemExit subclass that carries the
        # operator message (#15149).
        with patch.object(module, "_route_provider", return_value=provider):
            with self.assertRaises(SystemExit) as caught:
                cmd(args)
        return str(caught.exception)

    def test_consult_names_the_claude_bound_gateway(self) -> None:
        message = self._abort_message(
            cli_project_gateway_consult,
            cli_project_gateway_consult.cmd_project_gateway_consult,
            argparse.Namespace(
                to="codex", target_repo=REPO, target_project=PROJECT,
                target=None, as_json=False,
            ),
            provider="claude",
        )
        self.assertIn("`--to claude`", message)
        self.assertIn("provider_binding", message)

    def test_handoff_names_the_codex_bound_gateway(self) -> None:
        message = self._abort_message(
            cli_project_gateway,
            cli_project_gateway.cmd_project_gateway_handoff,
            argparse.Namespace(
                to="claude", target_repo=REPO, target_project=PROJECT,
                target=None, gateway_session=None, as_json=False,
            ),
            provider="codex",
        )
        self.assertIn("`--to codex`", message)
        self.assertIn("provider_binding", message)

    def test_child_intake_names_the_claude_bound_child(self) -> None:
        message = self._abort_message(
            cli_project_gateway_child_intake,
            cli_project_gateway_child_intake.cmd_project_gateway_child_intake,
            argparse.Namespace(
                to="codex", target_repo=REPO, target_project=PROJECT,
                target=None, as_json=False,
            ),
            provider="claude",
        )
        self.assertIn("`--to claude`", message)
        self.assertIn("provider_binding", message)


class ChildIntakeIdentityFollowsBindingTest(unittest.TestCase):
    """Review j#104593 finding_childidentity: the child semantic identity follows
    the SAME bound provider the candidates were fetched with."""

    def _route(self, candidates, *, provider=None, caller="%parent"):
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.child_intake_route import (  # noqa: E501
            resolve_child_intake_route,
        )

        return resolve_child_intake_route(
            candidates,
            repo_root=REPO,
            project_scope=PROJECT,
            caller_pane=caller,
            provider=provider,
        )

    def test_claude_bound_distinct_child_is_resolved(self) -> None:
        route = self._route(
            [_candidate("%parent", role="claude"), _candidate("%child", role="claude")],
            provider="claude",
        )
        self.assertEqual("child_resolved", route.status)
        self.assertEqual("%child", route.selected.pane_id)

    def test_codex_bound_distinct_child_is_unchanged(self) -> None:
        route = self._route(
            [_candidate("%parent", role="codex"), _candidate("%child", role="codex")],
            provider="codex",
        )
        self.assertEqual("child_resolved", route.status)
        self.assertEqual("%child", route.selected.pane_id)

    def test_omitted_provider_keeps_the_historical_codex_contract(self) -> None:
        route = self._route(
            [_candidate("%parent", role="claude"), _candidate("%child", role="claude")],
        )
        self.assertNotEqual("child_resolved", route.status)

    def test_same_lane_guard_is_preserved_under_claude_binding(self) -> None:
        route = self._route(
            [_candidate("%parent", role="claude")], provider="claude"
        )
        self.assertEqual("same_lane", route.status)

    def test_cli_delivers_to_the_claude_bound_child(self) -> None:
        captured = {}

        def fake_orch(args, **kwargs):
            captured["target"] = args.target
            return 0

        args = argparse.Namespace(
            to="claude", target_repo=REPO, target_project=PROJECT, target=None,
            from_pane="%parent", work_shape=None, gateway_session=None,
            as_json=False,
        )
        with patch.object(
            cli_project_gateway_child_intake, "_route_provider",
            return_value="claude",
        ):
            with patch.object(
                cli_project_gateway_child_intake, "_discover_candidates",
                return_value=[
                    _candidate("%parent", role="claude"),
                    _candidate("%child", role="claude"),
                ],
            ):
                with patch.object(
                    cli_project_gateway_child_intake, "orchestrate_handoff",
                    side_effect=fake_orch,
                ):
                    rc = cli_project_gateway_child_intake.cmd_project_gateway_child_intake(args)
        self.assertEqual(0, rc)
        self.assertEqual("%child", captured["target"])


class DeclareRouteFollowsBindingTest(unittest.TestCase):
    """Review j#104593 finding_declareidentity: the declaration's semantic identity
    and candidate discovery use the SAME gateway provider the use case resolved."""

    def _resolve_route(self, *, gateway_provider, candidates):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_project_gateway_declare,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            workflow_provider_resolution,
        )

        ops = cli_project_gateway_declare.LiveProjectGatewayDeclareOps(REPO)
        # The provider is PASSED IN (the use case's single snapshot, #15414
        # finding_providersnapshot); the route must not re-read the binding.
        with patch.object(
            workflow_provider_resolution, "resolve_gateway_provider"
        ) as gateway_read:
            with patch.object(
                workflow_provider_resolution, "resolve_worker_provider"
            ) as worker_read:
                with patch.object(
                    cli_project_gateway, "_discover_candidates",
                    return_value=candidates,
                ) as discovered:
                    result = ops.resolve_route(PROJECT, gateway_provider)
        gateway_read.assert_not_called()
        worker_read.assert_not_called()
        return result, discovered

    def test_all_claude_gateway_resolves_the_observed_route(self) -> None:
        (_repo, _path, observed), discovered = self._resolve_route(
            gateway_provider="claude",
            candidates=[_candidate("%gw", role="claude")],
        )
        self.assertIsNotNone(observed)
        self.assertEqual("%gw", observed.locator)
        self.assertEqual(
            "claude", discovered.call_args.kwargs.get("provider")
        )

    def test_codex_bound_gateway_is_unchanged(self) -> None:
        (_repo, _path, observed), _discovered = self._resolve_route(
            gateway_provider="codex",
            candidates=[_candidate("%gw", role="codex")],
        )
        self.assertIsNotNone(observed)
        self.assertEqual("%gw", observed.locator)

    def test_a_mismatched_live_provider_stays_owner_unbound(self) -> None:
        (_repo, _path, observed), _discovered = self._resolve_route(
            gateway_provider="claude",
            candidates=[_candidate("%gw", role="codex")],
        )
        self.assertIsNone(observed)

    def test_provider_pair_derives_from_one_binding_snapshot(self) -> None:
        # Review j#104607 finding_providerpairsnapshot: the (gateway, worker)
        # pair must come from ONE binding load — per-role loads could hand the
        # declaration a hybrid pair that never existed in any single binding
        # state. The drifting loader returns a different binding on every load,
        # so any second read is visible in both the count and the pair value.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_project_gateway_declare,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            workflow_binding_source,
        )

        # The gateway slot resolves via the ``project_gateway`` lane role, not
        # the coordinator role (Redmine #15655), so the drift is expressed on
        # the roles the pair actually reads.
        all_claude = RoleProviderBinding.default().with_overrides(
            {ROLE_PROJECT_GATEWAY: "claude", ROLE_IMPLEMENTER: "claude"}
        )
        all_codex = RoleProviderBinding.default().with_overrides(
            {ROLE_PROJECT_GATEWAY: "codex", ROLE_IMPLEMENTER: "codex"}
        )
        loads: list = []

        def drifting_load(root):
            loads.append(root)
            return (all_claude if len(loads) == 1 else all_codex), ()

        ops = cli_project_gateway_declare.LiveProjectGatewayDeclareOps(REPO)
        with patch.object(
            workflow_binding_source, "load_workflow_binding",
            side_effect=drifting_load,
        ):
            pair = ops.providers()
        self.assertEqual(1, len(loads))
        self.assertEqual(("claude", "claude"), pair)


class HelpContractTextTest(unittest.TestCase):
    """Review j#104586 finding_contracttext (authority-text half): the CLI help no
    longer advertises the codex-fixed contract; it names the binding-derived
    receiver (Close condition 4)."""

    def _help_text(self, *argv):
        from mozyo_bridge.application.cli import build_parser

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                build_parser().parse_args([*argv, "--help"])
        return out.getvalue()

    def test_family_listing_help_is_binding_derived(self) -> None:
        # The per-command `help=` strings render in the PARENT subcommand listing.
        text = self._help_text("project-gateway")
        self.assertNotIn("Codex unit", text)
        self.assertNotIn("--to codex (", text)
        self.assertIn("provider_binding", text)

    def test_child_intake_to_option_help_is_binding_derived(self) -> None:
        text = self._help_text("project-gateway", "child-intake")
        self.assertNotIn("Codex unit", text)
        self.assertIn("provider_binding", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
