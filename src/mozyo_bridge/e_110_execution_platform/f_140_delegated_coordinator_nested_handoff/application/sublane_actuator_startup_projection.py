"""Typed projection of embedded herdr session-start health (Redmine #13948)."""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    SublaneStartupObservation,
    SublaneStartupRoleHealth,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (
    SessionStartResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (
    COMPENSATION_ROLLBACK_OWED,
)


def project_sublane_startup(
    result: SessionStartResult,
) -> SublaneStartupObservation:
    """Project a raw backend result without locators or observed screen content."""
    roles = tuple(
        SublaneStartupRoleHealth(
            provider=slot.provider,
            disposition=slot.disposition,
            health=slot.health,
            compensation=slot.compensation,
            blocker_id=slot.blocker_id,
            detail=slot.health_detail,
        )
        for slot in result.slots
    )
    return SublaneStartupObservation(
        ok=result.ok,
        action_id=result.action_id,
        roles=roles,
        rollback_owed=any(
            slot.compensation == COMPENSATION_ROLLBACK_OWED for slot in result.slots
        ),
    )


__all__ = ("project_sublane_startup",)
