"""Increment 2B acceptance: the delegated-route / sublane actuation keys on the
RoleProviderBinding, not a hard-coded ``claude`` / ``codex`` literal (Redmine #13569,
Coordinator Answer j#76969 corrections 2-4).

Rebinding the implementer (worker) / coordinator (gateway) provider moves the route
heads, the cross-boundary worker-direct guard, the managed-retire target, the route
re-resolution expected role, and the child-gateway landing — with no consumer source
literal — while the gateway-via invariant is never weakened (the guard keys on the
binding-resolved provider). An unbound role fails closed (zero-send) rather than
silently defaulting to a literal (correction 4).

Providers here are fake, explicit placeholders (strengthened-scanner rule).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E402
    WorkflowProviderUnresolved,
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E402
    ROLE_COORDINATOR,
    ROLE_IMPLEMENTER,
    RoleProviderBinding,
)

WORKER = "mistral-cli"
GATEWAY = "codex"
REBOUND = RoleProviderBinding.default().with_overrides({ROLE_IMPLEMENTER: WORKER})


class WorkflowProviderResolutionFailsClosed(unittest.TestCase):
    def test_default_binding_is_byte_identical(self) -> None:
        default = RoleProviderBinding.default()
        self.assertEqual(resolve_worker_provider(binding=default), "claude")
        self.assertEqual(resolve_gateway_provider(binding=default), "codex")

    def test_rebound_worker_provider_follows_binding(self) -> None:
        self.assertEqual(resolve_worker_provider(binding=REBOUND), WORKER)

    def test_unbound_role_raises_rather_than_defaulting(self) -> None:
        # A binding that binds no roles (an impossible-under-default custom binding) must
        # fail closed, never silently default to a literal (correction 4).
        empty = RoleProviderBinding(MappingProxyType({}))
        with self.assertRaises(WorkflowProviderUnresolved):
            resolve_worker_provider(binding=empty)


class WorkerDispatchArgvKeysOnBinding(unittest.TestCase):
    def test_dispatch_argv_uses_the_resolved_worker_provider(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (  # noqa: E501
            _worker_dispatch_argv,
        )

        argv = _worker_dispatch_argv(
            issue="1",
            journal="2",
            worker_pane="%9",
            lane_label="lane",
            gateway_callback_target=None,
            target_repo="auto",
            worker_provider=WORKER,
        )
        self.assertEqual(argv[argv.index("--to") + 1], WORKER)
        # default is byte-identical claude
        default_argv = _worker_dispatch_argv(
            issue="1",
            journal="2",
            worker_pane="%9",
            lane_label="lane",
            gateway_callback_target=None,
            target_repo="auto",
        )
        self.assertEqual(default_argv[default_argv.index("--to") + 1], "claude")


class DelegationRoutePlannerKeysOnBinding(unittest.TestCase):
    def _plan(self, request):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner import (  # noqa: E501
            _handoff_step,
        )

        return _handoff_step

    def test_same_lane_worker_step_head_follows_binding(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner import (  # noqa: E501
            DelegationRoutePlanError,
            _handoff_step,
        )

        # A same-lane (non-cross-boundary) worker step to the rebound worker provider is
        # allowed; a CROSS-boundary send to that same provider is refused — the guard keys
        # on the binding-resolved worker, not the literal claude.
        step = _handoff_step(
            kind="worker_handoff",
            to_role=WORKER,
            cross_boundary=False,
            description="same-lane",
            route_target="same_lane_worker",
            role_profile=None,
            realization="adopt",
            worker_provider=WORKER,
        )
        self.assertEqual(step.route_target, "same_lane_worker")
        with self.assertRaises(DelegationRoutePlanError):
            _handoff_step(
                kind="worker_handoff",
                to_role=WORKER,
                cross_boundary=True,
                description="cross",
                route_target="same_lane_worker",
                role_profile=None,
                realization="adopt",
                worker_provider=WORKER,
            )


class RouteLedgerExpectedRoleFollowsBinding(unittest.TestCase):
    def _identity(self, role):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (  # noqa: E501
            RouteIdentity,
        )

        return RouteIdentity(
            workspace_id="w",
            lane_id="lane",
            role=role,
            pane_name="label",
            route_id="r1",
            observed_at="t",
            last_seen_pane_id="%1",
        )

    def test_worker_target_expects_the_bound_worker_provider(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner import (  # noqa: E501
            TARGET_SAME_LANE_WORKER,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (  # noqa: E501
            DelegationRoutePlanError,
            enforce_route_target_guards,
            expected_roles_for,
        )

        rebound_map = expected_roles_for(gateway_provider=GATEWAY, worker_provider=WORKER)
        # A rebound worker pane re-resolves cleanly under the rebound expected map…
        enforce_route_target_guards(
            TARGET_SAME_LANE_WORKER,
            self._identity(WORKER),
            expected_roles=rebound_map,
        )
        # …but under the DEFAULT map (expects claude) the rebound pane is a role mismatch.
        with self.assertRaises(DelegationRoutePlanError):
            enforce_route_target_guards(
                TARGET_SAME_LANE_WORKER, self._identity(WORKER)
            )


class LaunchAdoptGatewayLandingFollowsBinding(unittest.TestCase):
    def test_landing_role_must_be_the_bound_gateway_provider(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt import (  # noqa: E501
            DelegationLaunchAdoptError,
            resolve_launch_adopt,
        )

        # Landing at the (rebound) gateway provider is permitted…
        decision = resolve_launch_adopt(
            mode="adopt_existing",
            candidates=(),
            target_repo_identity="repo",
            required_role="grok-gw",
            gateway_provider="grok-gw",
        )
        self.assertIsNotNone(decision)
        # …but landing directly at the worker provider is refused (gateway-via invariant).
        with self.assertRaises(DelegationLaunchAdoptError):
            resolve_launch_adopt(
                mode="adopt_existing",
                candidates=(),
                target_repo_identity="repo",
                required_role="claude",
                gateway_provider="codex",
            )


class RetireManagedRolesFollowBinding(unittest.TestCase):
    def test_retire_targets_the_bound_worker_slot(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            plan_herdr_retire_close,
        )

        # The mzb1 assigned-name scheme constrains the role segment charset (no hyphen),
        # so this herdr-name-based test uses a hyphen-free synthetic worker provider.
        worker = "mistralcli"
        rows = [
            {AGENT_KEY_NAME: "mzb1_projws_codex_lane1", "pane_id": "wZ:p2"},
            {AGENT_KEY_NAME: f"mzb1_projws_{worker}_lane1", "pane_id": "wZ:p3"},
        ]
        # Default managed pair (codex, claude) never matches the rebound worker slot…
        default_plan = plan_herdr_retire_close(
            rows, workspace_id="projws", lane_id="lane1"
        )
        default_roles = {role for role, _ in default_plan.close_targets}
        self.assertNotIn(worker, default_roles)
        # …the binding-resolved managed pair (codex, mistralcli) retires it.
        rebound_plan = plan_herdr_retire_close(
            rows,
            workspace_id="projws",
            lane_id="lane1",
            managed_roles=(GATEWAY, worker),
        )
        rebound_roles = {role for role, _ in rebound_plan.close_targets}
        self.assertIn(worker, rebound_roles)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
