"""Herdr adapter registered into the core project-gateway inventory port.

The project-gateway resolver belongs to the execution core, while decoding live
Herdr rows, resolving Herdr inventory, and constructing a send capability belong
to this adapter layer.  Package composition imports this module once and registers
the implementation; the core never imports the adapter in either module or local
scope.
"""

from __future__ import annotations

from pathlib import Path

from mozyo_bridge.core.state.herdr_launch_generation import (
    verified_generation_token,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.project_gateway_backend_inventory import (
    HerdrTargetObservation,
    register_project_gateway_backend_support,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
    AGENT_KEY_NAME,
    _norm,
    _norm_lane,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_LIVE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportConfig,
    TerminalTransportError,
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    resolve_agent_lister,
)


class HerdrProjectGatewayBackendSupport:
    """Concrete Herdr operations behind the core-owned inventory port."""

    agent_name_key = AGENT_KEY_NAME
    locator_keys = (
        AGENT_KEY_LOCATOR,
        AGENT_KEY_LOCATOR_ALIAS,
        AGENT_KEY_LOCATOR_ALIAS_2,
    )

    @staticmethod
    def normalize(value: object) -> str:
        return _norm(value)

    @staticmethod
    def normalize_lane(value: object) -> str:
        return _norm_lane(value)

    @staticmethod
    def decode_assigned_name(value: object):
        return decode_assigned_name(value)

    @staticmethod
    def slot_is_live(row: object) -> bool:
        return classify_named_slot(row) == SLOT_LIVE

    @staticmethod
    def valid_target(value: object) -> bool:
        return valid_target(value)

    @staticmethod
    def workspace_segment(repo_root: Path) -> str:
        return herdr_workspace_segment(repo_root)

    @staticmethod
    def list_agent_rows(config: TerminalTransportConfig):
        lister = resolve_agent_lister(config)
        if lister is None:
            raise TerminalTransportError(
                "Herdr backend selected but no agent lister resolved"
            )
        return lister.list_agent_rows()

    @staticmethod
    def generation_token(
        *,
        assigned_name: str,
        workspace_id: str,
        provider: str,
        lane_id: str,
        locator: str,
    ) -> str:
        return verified_generation_token(
            None,
            assigned_name=assigned_name,
            workspace_id=workspace_id,
            role=provider,
            lane_id=lane_id,
            locator=locator,
            norm=_norm,
            norm_lane=_norm_lane,
        )

    @staticmethod
    def build_project_gateway_capability(observation: HerdrTargetObservation):
        # Lazy import keeps package composition from pulling the send rail into
        # process bootstrap before it is needed.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
            PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
            ResolvedHerdrTargetCapability,
        )

        return ResolvedHerdrTargetCapability(
            workspace_id=observation.workspace_id,
            lane_id=observation.lane_id,
            provider=observation.provider,
            assigned_name=observation.assigned_name,
            locator=observation.locator,
            purpose=PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
            generation_token=observation.generation_token,
            project_scope=observation.project_scope,
            target_repo_root=observation.target_repo_root,
            target_cwd=observation.target_cwd,
            project_path=observation.project_path,
            project_scope_root_fallback=observation.project_scope_root_fallback,
        )


PROJECT_GATEWAY_BACKEND_SUPPORT = HerdrProjectGatewayBackendSupport()
register_project_gateway_backend_support(PROJECT_GATEWAY_BACKEND_SUPPORT)


__all__ = (
    "HerdrProjectGatewayBackendSupport",
    "PROJECT_GATEWAY_BACKEND_SUPPORT",
)
