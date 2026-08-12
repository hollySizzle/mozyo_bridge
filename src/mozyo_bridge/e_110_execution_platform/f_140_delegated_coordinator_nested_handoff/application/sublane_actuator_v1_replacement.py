"""Legacy replacement-store diagnostic fence for a managed sublane heal.

v1-v3 side bindings are readable rollback diagnostics only. Current replacement launches
delegate to the v4 managed-launch preflight and never reserve a legacy side row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from mozyo_bridge.core.state.startup_transaction_fence import (
        StartupTransactionFence,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
        SessionStartResult,
    )

from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    selected_attestation_store_is_v1,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    AttestationStoreLockBusy,
    AttestationStoreLockUnavailable,
    attestation_store_lock,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_startup_projection import (  # noqa: E501
    project_sublane_startup,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    SublaneHealError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding import (  # noqa: E501
    V1_BINDING_MAINTENANCE_BUSY,
    V1_BINDING_STORE_UNUSABLE,
)


@dataclass(frozen=True)
class V1ReplacementRequest:
    """Exact immutable authority and live observation for one replacement drive."""

    action_id: str
    assigned_name: str
    old_locator: str
    target_provider: str | None
    workspace_id: str
    lane_id: str
    managed_pair: tuple[str, ...]
    rows: Sequence[Mapping[str, object]]
    existing: Mapping[str, tuple[str, str]]
    launch: Callable[["str", "StartupTransactionFence"], "SessionStartResult"]
    target_only: bool = False


@dataclass(frozen=True)
class V1ReplacementDriver:
    """Typed application service for the v1 side-binding startup transaction."""

    home: Path

    def run(self, request: V1ReplacementRequest) -> bool:
        """Return whether v1 handled launch; false delegates to the general path."""
        if not (request.action_id or "").strip():
            return False
        try:
            # Pin the store generation through reserve, launch/self-attestation, receipt,
            # and side bind. An exclusive maintenance action cannot overtake this drive.
            with attestation_store_lock(
                self.home, exclusive=False, blocking=False
            ):
                # v1-v3 side rows are diagnostic-only under the terminal-bound v4
                # contract. Delegate to the general path, whose preflight refuses the
                # legacy store before registry/startup/generation/Herdr writes.
                selected_attestation_store_is_v1(self.home)
                return False
        except AttestationStoreLockBusy as exc:
            raise SublaneHealError(
                "lane heal fenced (replacement_binding_maintenance_busy): "
                "the attestation store is under maintenance",
                reason=V1_BINDING_MAINTENANCE_BUSY,
            ) from exc
        except AttestationStoreLockUnavailable as exc:
            raise SublaneHealError(
                "lane heal fenced (replacement_binding_store_unusable): "
                "the attestation-store generation lock is unavailable",
                reason=V1_BINDING_STORE_UNUSABLE,
            ) from exc
        except OSError as exc:
            raise SublaneHealError(
                "lane heal fenced (replacement_binding_store_unusable): "
                "the attestation-store generation lock could not be opened",
                reason=V1_BINDING_STORE_UNUSABLE,
            ) from exc


def require_replacement_target_healthy(result, action_id, target_provider, assigned_name) -> None:
    """Fence the exact v4 replacement participant, independent of sibling health."""
    if not action_id:
        return
    launched = [slot for slot in result.slots if slot.provider == target_provider
                and slot.assigned_name == assigned_name]
    if len(launched) != 1 or launched[0].outcome != "launched" or not launched[0].healthy:
        raise SublaneHealError(
            "lane heal fenced (replacement_binding_launch_unhealthy): the fresh "
            "replacement participant did not reach bounded startup health",
            reason="replacement_binding_launch_unhealthy",
            startup=project_sublane_startup(result),
        )


__all__ = ("V1ReplacementDriver", "V1ReplacementRequest", "require_replacement_target_healthy")
