"""Ordered completion of a managed Herdr session start.

The current-generation join used by configured project-column placement becomes
authoritative only after both the launch-generation row and the startup transaction
have completed.  Keeping these final steps in one leaf makes their order explicit:

1. finalize every launch generation;
2. settle the startup transaction;
3. complete container geometry;
4. and only then preview/apply configured shared-column placement.
"""

from __future__ import annotations

from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import evaluate_attestation
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    finalize_session_launch_generations,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_placement import (  # noqa: E501
    HerdrProjectColumnPlacement,
    PLACEMENT_APPLIED,
    PLACEMENT_DEFERRED,
    PLACEMENT_MATCHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E501
    COLUMN_APPLIED,
    COLUMN_DEFERRED,
    COLUMN_FAILED,
    COLUMN_MATCHED,
    COLUMN_NOT_APPLICABLE,
    COLUMN_PREPARED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    IdentityWorkspaceResolver,
    OwnSlot,
    ProjectColumnAuthority,
    StoreLaneFactsPort,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_LAUNCHED,
)


class _SessionAttestationPort:
    """Use the exact attestation reader already admitted by this session."""

    def __init__(self, reader) -> None:
        self._reader = reader

    def attested(self, pane) -> tuple[bool, str]:
        join = evaluate_attestation(
            self._reader(pane.assigned_name),
            live_locator=pane.locator,
            live_terminal_id=pane.terminal_id,
            expected_workspace_id=pane.workspace_id,
            expected_role=pane.role,
            expected_lane=pane.lane_id,
        )
        return join.ok, join.state


def finalize_session_launch_authority(
    result,
    *,
    store_home,
    transaction,
    workspace_id: str,
    attestation_read,
    attest_launcher: str,
    launch_plans,
    dry_run: bool,
    binary: str,
    runner,
    timeout: float,
    effect_fence=None,
) -> None:
    """Finalize v4/generation-v2 authority before any shared-pane geometry effect."""

    try:
        generation_inventory = tuple(_list_rows(binary, runner, timeout))
    except Exception:  # noqa: BLE001 - unreadable post-launch inventory leaves pending
        generation_inventory = ()

    finalize_session_launch_generations(
        store_home=Path(store_home),
        transaction=transaction,
        slots=result.slots,
        workspace_id=workspace_id,
        lane_id=result.lane_id,
        attestation_read=attestation_read,
        inventory_rows=generation_inventory,
        attest_launcher=attest_launcher,
        launch_plans=launch_plans,
        dry_run=dry_run,
        effect_fence=effect_fence,
    )
    if transaction is not None:
        transaction.settle(
            owed=result.owes_rollback,
            launched=bool(launch_plans),
        )


def complete_session_start(
    result,
    *,
    store_home,
    transaction,
    workspace_id: str,
    attestation_read,
    attest_launcher: str,
    launch_plans,
    dry_run: bool,
    project_column_coordinator: bool,
    coordinator_top_workspace_id: str,
    binary: str,
    runner,
    timeout: float,
    env,
    authority_already_completed: bool = False,
    effect_fence=None,
) -> None:
    """Complete authority when needed, then configured placement."""
    if not authority_already_completed:
        finalize_session_launch_authority(
            result,
            store_home=store_home,
            transaction=transaction,
            workspace_id=workspace_id,
            attestation_read=attestation_read,
            attest_launcher=attest_launcher,
            launch_plans=launch_plans,
            dry_run=dry_run,
            binary=binary,
            runner=runner,
            timeout=timeout,
            effect_fence=effect_fence,
        )

    # Configured reordering is intentionally a fresh-full-pair completion step.
    # Adopt, heal, dry-run, unhealthy startup, or an earlier geometry refusal keeps
    # the prior axis verdict and performs no additional Herdr command.
    fresh_slots = tuple(
        slot
        for slot in result.slots
        if getattr(slot, "outcome", "") == SLOT_LAUNCHED
    )
    if (
        dry_run
        or not project_column_coordinator
        or len(launch_plans) != 2
        or len(fresh_slots) != 2
        or result.owes_rollback
        or not all(getattr(slot, "healthy", False) for slot in result.slots)
        or result.column_outcome == COLUMN_NOT_APPLICABLE
        or (
            not result.column_ok
            and result.column_outcome != COLUMN_PREPARED
        )
        or not result.ratio_ok
    ):
        return

    prepared_only = result.column_outcome == COLUMN_PREPARED
    placement = HerdrProjectColumnPlacement(
        home=Path(store_home),
        target_workspace=result.herdr_workspace_id,
        top_workspace_id=coordinator_top_workspace_id,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
        authority=ProjectColumnAuthority(
            attestation=_SessionAttestationPort(attestation_read),
            lanes=StoreLaneFactsPort(Path(store_home)),
            workspaces=IdentityWorkspaceResolver(Path(store_home)),
        ),
        own_slots=tuple(
            OwnSlot(
                locator=getattr(slot, "locator", ""),
                assigned_name=getattr(slot, "assigned_name", ""),
                provider=getattr(slot, "provider", ""),
            )
            for slot in fresh_slots
        ),
        expected_own_key=(result.workspace_id, result.lane_id),
    ).converge()
    if placement.status == PLACEMENT_DEFERRED:
        if prepared_only:
            result.column_outcome = COLUMN_FAILED
            result.column_detail = (
                f"{placement.detail}; configured relative widths remain unverified"
            )
            return
        result.column_outcome = COLUMN_DEFERRED
        result.column_detail = placement.detail
        return
    if placement.status == PLACEMENT_APPLIED:
        result.column_outcome = COLUMN_APPLIED
        result.column_detail = placement.detail
        return
    if placement.status == PLACEMENT_MATCHED:
        # One project has no cross-project order/width to change, so retain the
        # existing not-applicable verdict.  For a populated shared tab, preserve
        # whether the preceding L-shape repair actually mutated geometry.
        if result.column_outcome not in {
            COLUMN_NOT_APPLICABLE,
            COLUMN_MATCHED,
            COLUMN_APPLIED,
        }:
            result.column_outcome = COLUMN_MATCHED
        if result.column_outcome != COLUMN_NOT_APPLICABLE:
            result.column_detail = placement.detail
        return
    result.column_outcome = COLUMN_FAILED
    result.column_detail = " ".join(
        part for part in (placement.detail, placement.recovery) if part
    )


def complete_session_start_container(
    result,
    *,
    geometry_finalizer,
    configured_placement,
    store_home,
    transaction,
    workspace_id: str,
    attestation_read,
    attest_launcher: str,
    launch_plans,
    dry_run: bool,
    config_split,
    config_order,
    pair_order,
    requested,
    config_ratio,
    initial_occupancy: int,
    project_column_coordinator: bool,
    coordinator_top_workspace_id: str,
    binary: str,
    runner,
    timeout: float,
    env,
    effect_fence=None,
) -> None:
    """Finalize launch authority, geometry, and configured placement in order."""

    finalize_session_launch_authority(
        result,
        store_home=store_home,
        transaction=transaction,
        workspace_id=workspace_id,
        attestation_read=attestation_read,
        attest_launcher=attest_launcher,
        launch_plans=launch_plans,
        dry_run=dry_run,
        binary=binary,
        runner=runner,
        timeout=timeout,
        effect_fence=effect_fence,
    )
    geometry_finalizer(
        result,
        config_split=config_split,
        config_order=config_order,
        pair_order=pair_order,
        requested=requested,
        config_ratio=config_ratio,
        launched=len(launch_plans),
        initial_occupancy=initial_occupancy,
        dry_run=dry_run,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
        project_coordinator=project_column_coordinator,
        store_home=store_home,
        top_workspace_id=coordinator_top_workspace_id,
        attestation_read=attestation_read,
    )
    configured_placement(
        result,
        store_home=store_home,
        transaction=transaction,
        workspace_id=workspace_id,
        attestation_read=attestation_read,
        attest_launcher=attest_launcher,
        launch_plans=launch_plans,
        dry_run=dry_run,
        project_column_coordinator=project_column_coordinator,
        coordinator_top_workspace_id=coordinator_top_workspace_id,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
        authority_already_completed=True,
        effect_fence=effect_fence,
    )


__all__ = ("complete_session_start",)
