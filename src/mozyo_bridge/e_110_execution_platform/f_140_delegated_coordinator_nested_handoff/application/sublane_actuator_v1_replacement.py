"""Action-bound v1 replacement drive for a managed sublane heal.

The herdr actuator owns general lane placement and lifecycle behavior.  This module owns
the narrower compatibility transaction required while the selected identity-attestation
store is still v1: pin the store generation, reserve-before-launch, bind the exact startup
receipt, and project any nested startup rollback debt into the execution-platform error
vocabulary.  Keeping that transaction here prevents the general actuator from becoming a
second implementation of the v1 binding state machine.
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
    V1ReplacementBindingFailure,
    launch_or_resume_v1_replacement,
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
                if not selected_attestation_store_is_v1(self.home):
                    return False
                launch_or_resume_v1_replacement(
                    home=self.home,
                    action_id=request.action_id,
                    assigned_name=request.assigned_name,
                    old_locator=request.old_locator,
                    target_provider=request.target_provider,
                    workspace_id=request.workspace_id,
                    lane_id=request.lane_id,
                    managed_pair=request.managed_pair,
                    rows=request.rows,
                    existing=request.existing,
                    launch=request.launch,
                    target_only=request.target_only,
                )
                return True
        except V1ReplacementBindingFailure as exc:
            startup = (
                project_sublane_startup(exc.startup_result)
                if exc.startup_result is not None else None
            )
            raise SublaneHealError(
                f"lane heal fenced ({exc.reason}): {exc.detail}",
                reason=exc.reason,
                startup=startup,
            ) from exc
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


__all__ = ("V1ReplacementDriver", "V1ReplacementRequest")
