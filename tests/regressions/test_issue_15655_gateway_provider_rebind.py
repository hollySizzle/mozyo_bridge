"""Redmine #15655 — the lane gateway provider must not follow a coordinator rebind.

Rebinding the coordinator role onto the implementation profile
(``agents.roles.coordinator: implementation`` in ``.mozyo-bridge/config.yaml``,
the #15631 provider claude rebind) moved :func:`resolve_gateway_provider` too,
because it resolved the gateway through ``ROLE_COORDINATOR``. Both sublane pair
slots then resolved to ``claude`` and pair creation failed closed with
``duplicate requested slot for provider 'claude'``.

The fix resolves the lane gateway through the ``project_gateway`` role of the
#12670 lane vocabulary (default: coordination profile = ``codex``), which the
config's closed role vocabulary already carries. These tests pin:

1. a coordinator->claude rebind (the incident binding) leaves the gateway on
   ``codex`` and distinct from the worker provider (no duplicate slot);
2. rebinding ``project_gateway`` itself DOES move the gateway (the role stays
   config-driven, not a re-hardcoded literal);
3. ``resolve_worker_provider`` / ``resolve_role_provider`` semantics are
   unchanged by the fix.

Bindings are injected through the ``RoleProviderBinding`` override API, so no
test depends on the operator's real ``.mozyo-bridge/config.yaml``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E402,E501
    resolve_gateway_provider,
    resolve_role_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E402,E501
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    ROLE_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
    RoleProviderBinding,
)

#: The incident binding (#15655): coordinator rebound onto the implementation
#: provider (claude), everything else default — exactly what the
#: ``agents.roles.coordinator: implementation`` config resolves to.
COORDINATOR_REBOUND = RoleProviderBinding.default().with_overrides(
    {ROLE_COORDINATOR: PROVIDER_CLAUDE}
)


class GatewayProviderIgnoresCoordinatorRebind(unittest.TestCase):
    """A coordinator rebind must not move the lane gateway onto the worker slot."""

    def test_coordinator_claude_rebind_keeps_gateway_on_codex(self) -> None:
        self.assertEqual(
            resolve_gateway_provider(binding=COORDINATOR_REBOUND), PROVIDER_CODEX
        )

    def test_gateway_and_worker_slots_stay_distinct_under_the_incident_binding(
        self,
    ) -> None:
        # The #15655 failure mode: both sublane pair slots resolving to the same
        # provider ("duplicate requested slot for provider 'claude'").
        gateway = resolve_gateway_provider(binding=COORDINATOR_REBOUND)
        worker = resolve_worker_provider(binding=COORDINATOR_REBOUND)
        self.assertEqual(worker, PROVIDER_CLAUDE)
        self.assertNotEqual(gateway, worker)

    def test_coordinator_role_itself_still_follows_the_rebind(self) -> None:
        # The fix narrows what the GATEWAY keys on; the coordinator role's own
        # resolution is untouched.
        self.assertEqual(
            resolve_role_provider(ROLE_COORDINATOR, binding=COORDINATOR_REBOUND),
            PROVIDER_CLAUDE,
        )


class GatewayProviderFollowsProjectGatewayRebind(unittest.TestCase):
    """The gateway stays config-driven through the ``project_gateway`` role."""

    def test_project_gateway_rebind_moves_the_gateway(self) -> None:
        rebound = RoleProviderBinding.default().with_overrides(
            {ROLE_PROJECT_GATEWAY: "grok-gw"}
        )
        self.assertEqual(resolve_gateway_provider(binding=rebound), "grok-gw")

    def test_project_gateway_rebind_does_not_move_the_worker(self) -> None:
        rebound = RoleProviderBinding.default().with_overrides(
            {ROLE_PROJECT_GATEWAY: "grok-gw"}
        )
        self.assertEqual(resolve_worker_provider(binding=rebound), PROVIDER_CLAUDE)

    def test_default_binding_is_byte_identical(self) -> None:
        default = RoleProviderBinding.default()
        self.assertEqual(resolve_gateway_provider(binding=default), PROVIDER_CODEX)
        self.assertEqual(resolve_worker_provider(binding=default), PROVIDER_CLAUDE)


class CoordinatorAuthorityStaysOnCoordinatorRole(unittest.TestCase):
    """Review j#107777 finding_1: splitting the gateway off the coordinator role must
    not detach the coordinator AUTHORITY surfaces (coordinator pseudo-target / callback
    transport / coordinator pane resolution / sender preflight) from the coordinator
    rebind — ``resolve_coordinator_provider`` resolves ``coordinator`` directly, never
    through the ``project_gateway``-keyed gateway resolver."""

    def test_coordinator_rebind_moves_coordinator_but_not_gateway(self) -> None:
        # Under coordinator=claude / project_gateway=codex (the incident binding), BOTH
        # must hold at once: the lane gateway stays codex AND the coordinator authority
        # follows the rebind to claude.
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            main_lane_guard_gate,
            workflow_provider_resolution,
        )

        with mock.patch.object(
            workflow_provider_resolution,
            "load_workflow_binding",
            return_value=(COORDINATOR_REBOUND, ()),
        ):
            self.assertEqual(
                resolve_gateway_provider(binding=COORDINATOR_REBOUND), PROVIDER_CODEX
            )
            self.assertEqual(
                main_lane_guard_gate.resolve_coordinator_provider(), PROVIDER_CLAUDE
            )


if __name__ == "__main__":
    unittest.main()
