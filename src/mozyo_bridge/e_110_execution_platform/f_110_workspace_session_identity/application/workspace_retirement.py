"""Application service for one backup-first workspace registry retirement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_retirement import (
    REASON_ACTION_TIME_DRIFT,
    REASON_BACKUP_FAILED,
    REASON_INVALID_OBSERVATION,
    REASON_REGISTRY_UNREADABLE,
    REASON_RETIREMENT_FAILED,
    WorkspaceRetirementInventory,
    WorkspaceRetirementObservation,
    WorkspaceRetirementPlanResult,
    build_workspace_retirement_plan,
    exact_sha256,
    refused,
)


@dataclass(frozen=True)
class WorkspaceRetirementStoreOutcome:
    ok: bool
    reason: str = ""
    backup_receipt: str = ""


class WorkspaceRetirementAuthorityError(RuntimeError):
    """A source could not prove the requested retirement authority."""


class WorkspaceRetirementRegistryPort(Protocol):
    def observe(self, workspace_id: str) -> Optional[WorkspaceRetirementObservation]: ...

    def observe_retired(
        self, workspace_id: str, plan_digest: str
    ) -> Optional[WorkspaceRetirementObservation]: ...

    def retire(
        self,
        *,
        workspace_id: str,
        expected_record_digest: str,
        plan_digest: str,
    ) -> WorkspaceRetirementStoreOutcome: ...


class WorkspaceRetirementInventoryPort(Protocol):
    def observe(self, workspace_id: str) -> WorkspaceRetirementInventory: ...


class WorkspaceRetirementUseCase:
    """Coordinates the double-read fence and the single storage effect."""

    def __init__(
        self,
        *,
        registry: WorkspaceRetirementRegistryPort,
        inventory: WorkspaceRetirementInventoryPort,
    ) -> None:
        self._registry = registry
        self._inventory = inventory

    def run(
        self,
        *,
        workspace_id: str,
        current_workspace_id: str,
        execute: bool = False,
        expected_plan_digest: str = "",
    ) -> WorkspaceRetirementPlanResult:
        if not isinstance(expected_plan_digest, str) or (
            expected_plan_digest and not exact_sha256(expected_plan_digest)
        ):
            return refused(
                REASON_INVALID_OBSERVATION, "expected_plan_digest_invalid"
            )
        try:
            observation = self._registry.observe(workspace_id)
        except WorkspaceRetirementAuthorityError:
            return refused(REASON_REGISTRY_UNREADABLE, "registry_not_healthy")
        if observation is None and expected_plan_digest:
            try:
                retired = self._registry.observe_retired(
                    workspace_id, expected_plan_digest
                )
            except WorkspaceRetirementAuthorityError:
                return refused(REASON_REGISTRY_UNREADABLE, "backup_not_readable")
            if retired is not None:
                replay_inventory = self._inventory.observe(workspace_id)
                replay_plan = build_workspace_retirement_plan(
                    observation=retired,
                    inventory=replay_inventory,
                    current_workspace_id=current_workspace_id,
                    execute=execute,
                    expected_plan_digest=expected_plan_digest,
                )
                if replay_plan.ok:
                    return replay_plan.already_retired(
                        backup_receipt=expected_plan_digest
                    )
                return replay_plan

        inventory = self._inventory.observe(workspace_id)
        first = build_workspace_retirement_plan(
            observation=observation,
            inventory=inventory,
            current_workspace_id=current_workspace_id,
            execute=execute,
            expected_plan_digest=expected_plan_digest,
        )
        if not first.ok or not execute:
            return first

        try:
            second_observation = self._registry.observe(workspace_id)
        except WorkspaceRetirementAuthorityError:
            return refused(REASON_ACTION_TIME_DRIFT, "registry_became_unreadable")
        second_inventory = self._inventory.observe(workspace_id)
        second = build_workspace_retirement_plan(
            observation=second_observation,
            inventory=second_inventory,
            current_workspace_id=current_workspace_id,
            execute=True,
            expected_plan_digest=expected_plan_digest,
        )
        if not second.ok or second.plan_digest != first.plan_digest:
            return refused(REASON_ACTION_TIME_DRIFT, "preflight_changed_before_write")

        outcome = self._registry.retire(
            workspace_id=workspace_id,
            expected_record_digest=observation.record_digest,
            plan_digest=first.plan_digest,
        )
        if not outcome.ok:
            reason = (
                REASON_BACKUP_FAILED
                if outcome.reason == REASON_BACKUP_FAILED
                else REASON_RETIREMENT_FAILED
            )
            return refused(reason, outcome.reason or "registry_write_failed")
        return first.retired(backup_receipt=outcome.backup_receipt)


__all__ = (
    "WorkspaceRetirementInventoryPort",
    "WorkspaceRetirementAuthorityError",
    "WorkspaceRetirementRegistryPort",
    "WorkspaceRetirementStoreOutcome",
    "WorkspaceRetirementUseCase",
)
