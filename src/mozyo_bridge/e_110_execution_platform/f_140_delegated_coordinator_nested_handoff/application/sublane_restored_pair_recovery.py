"""Guarded use case for replacing a post-reboot damaged active sublane pair (#15227)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_owner_approval import (  # noqa: E501
    RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT,
    RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
    render_recovery_owner_approval_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_recovery import (  # noqa: E501
    BLOCK_PAIR_HEALTHY,
    BLOCK_PAIR_INCOMPLETE,
    BLOCK_SLOT_BUSY,
    STATUS_COMPLETED,
    STATUS_PREFLIGHT,
    STATUS_REFUSED,
    STATUS_STOPPED,
    RestoredPairOutcome,
    RestoredPairPlan,
    restored_pair_approval_operation,
)


@dataclass(frozen=True)
class RestoredPairRecoveryRequest:
    issue: str
    lane: str
    journal: str = ""
    action_id: str = ""
    action_generation: int = 0
    allow_pending_composer_loss: bool = False
    gateway_assigned_name: str = ""
    gateway_locator: str = ""
    gateway_revision: str = ""
    worker_assigned_name: str = ""
    worker_locator: str = ""
    worker_revision: str = ""

    @property
    def holder(self) -> str:
        return f"restored-pair:{self.action_id}:g{self.action_generation}"


@dataclass(frozen=True)
class PairReplacementResult:
    completed: bool
    phase: str = ""
    revision: int = 0
    detail: str = ""


@runtime_checkable
class RestoredPairRecoveryOps(Protocol):
    def observe(self, request: RestoredPairRecoveryRequest) -> RestoredPairPlan:
        ...

    def transaction_exists(self, action_id: str) -> bool:
        ...

    def approval_verified(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> bool:
        ...

    def replace_pair(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> PairReplacementResult:
        ...


class SublaneRestoredPairRecoveryUseCase:
    """Read-only by default; execute only an exact direct-owner-approved pair action."""

    def __init__(self, ops: RestoredPairRecoveryOps) -> None:
        self._ops = ops

    def run(
        self, request: RestoredPairRecoveryRequest, *, execute: bool = False
    ) -> RestoredPairOutcome:
        plan = self._ops.observe(request)
        marker = ""
        if plan.may_recover:
            marker = render_recovery_owner_approval_marker(
                gate=RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
                effect=RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT,
                issue=plan.issue,
                lane=plan.lane,
                operation=restored_pair_approval_operation(plan),
            )
        if not execute:
            return RestoredPairOutcome(
                issue=plan.issue,
                lane=plan.lane,
                status=STATUS_PREFLIGHT,
                executed=False,
                plan=plan,
                detail=(
                    "exact pair is approval-ready"
                    if plan.may_recover
                    else "preflight blocked: " + ", ".join(plan.blocked_reasons)
                ),
                required_approval_marker=marker,
            )

        existing = bool(request.action_id) and self._ops.transaction_exists(request.action_id)
        replay_only = {BLOCK_PAIR_INCOMPLETE, BLOCK_PAIR_HEALTHY, BLOCK_SLOT_BUSY}
        hard_blockers = tuple(
            reason
            for reason in plan.blocked_reasons
            if not (existing and reason in replay_only)
        )
        if hard_blockers:
            return self._refused(request, plan, "preflight blocked: " + ", ".join(hard_blockers))
        if request.action_id != plan.action_id or request.action_generation != plan.action_generation:
            return self._refused(
                request, plan, "the supplied action id/generation does not pin this exact pair"
            )
        if not request.journal:
            return self._refused(request, plan, "an exact owner-approval journal is required")
        if not self._ops.approval_verified(request, plan):
            return self._refused(
                request, plan, "the exact structured direct-owner approval was not verified"
            )

        result = self._ops.replace_pair(request, plan)
        return RestoredPairOutcome(
            issue=plan.issue,
            lane=plan.lane,
            status=STATUS_COMPLETED if result.completed else STATUS_STOPPED,
            executed=True,
            plan=plan,
            detail=result.detail,
            phase=result.phase,
            revision=result.revision,
            conversation_resume_guaranteed=False,
        )

    @staticmethod
    def _refused(
        request: RestoredPairRecoveryRequest, plan: RestoredPairPlan, detail: str
    ) -> RestoredPairOutcome:
        return RestoredPairOutcome(
            issue=plan.issue,
            lane=plan.lane,
            status=STATUS_REFUSED,
            executed=True,
            plan=plan,
            detail=detail,
            conversation_resume_guaranteed=False,
        )


__all__ = (
    "PairReplacementResult",
    "RestoredPairRecoveryOps",
    "RestoredPairRecoveryRequest",
    "SublaneRestoredPairRecoveryUseCase",
)
