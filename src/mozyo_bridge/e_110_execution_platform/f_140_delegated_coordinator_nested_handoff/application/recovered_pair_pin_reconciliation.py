"""Use case for bounded recovered active-pair pin reconciliation (#14203 R19)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_pair_pin_reconciliation import (  # noqa: E501
    RecoveredPairPinReconciliationOutcome,
    RecoveredPairPinReconciliationPreflight,
    RecoveredPairPinReconciliationRequest,
)


@runtime_checkable
class RecoveredPairPinReconciliationOps(Protocol):
    def preflight(
        self, request: RecoveredPairPinReconciliationRequest
    ) -> RecoveredPairPinReconciliationPreflight: ...

    def reconcile(
        self, request: RecoveredPairPinReconciliationRequest
    ) -> tuple[bool, int | None, str]: ...


@dataclass
class RecoveredPairPinReconciliationUseCase:
    ops: RecoveredPairPinReconciliationOps

    def run(
        self,
        request: RecoveredPairPinReconciliationRequest,
        *,
        execute: bool,
    ) -> RecoveredPairPinReconciliationOutcome:
        issue = str(request.issue).strip()
        lane = str(request.lane).strip()
        if not request.complete:
            preflight = RecoveredPairPinReconciliationPreflight(
                ready=False, detail="reconciliation_identity_incomplete"
            )
            return RecoveredPairPinReconciliationOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                preflight=preflight,
                detail=preflight.detail,
            )

        preflight = self.ops.preflight(request)
        if not preflight.ready or not execute:
            return RecoveredPairPinReconciliationOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                preflight=preflight,
                detail=(
                    "preflight_only"
                    if preflight.ready
                    else preflight.detail
                ),
            )

        applied, revision, detail = self.ops.reconcile(request)
        return RecoveredPairPinReconciliationOutcome(
            executed=True,
            issue=issue,
            lane=lane,
            preflight=preflight,
            applied=applied,
            revision=revision,
            detail=detail,
        )


__all__ = (
    "RecoveredPairPinReconciliationOps",
    "RecoveredPairPinReconciliationUseCase",
)
