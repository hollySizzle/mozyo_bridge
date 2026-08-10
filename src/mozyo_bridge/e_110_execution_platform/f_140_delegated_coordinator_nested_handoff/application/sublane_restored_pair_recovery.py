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
    APPROVAL_HEALTH_STATES,
    BLOCK_GENERATION_CONDITIONAL_CLOSE_UNAVAILABLE,
    BLOCK_PAIR_INCOMPLETE,
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
    gateway_approval_health: str = ""
    worker_approval_health: str = ""
    # Explicit owner-approved re-anchor of an exact ZERO-EFFECT transaction.  The target
    # generation is exactly the stored generation + 1; the old journal/revision are approval
    # pins and the dedicated CAS rewrites the durable header before any live effect.
    supersede: bool = False
    supersedes_generation: int = 0
    supersedes_journal: str = ""
    supersedes_revision: int = 0

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

    def transaction_is_progressed_replay(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> bool:
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
    """Diagnose a restored pair; refuse effects until close is generation-conditional."""

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

        # This precedes transaction inspection, approval reads, and replacement.
        # The missing Herdr primitive is a compiled technical fact, not a
        # caller-overridable policy flag.
        if BLOCK_GENERATION_CONDITIONAL_CLOSE_UNAVAILABLE in plan.blocked_reasons:
            return self._refused(
                request,
                plan,
                "preflight blocked: "
                + BLOCK_GENERATION_CONDITIONAL_CLOSE_UNAVAILABLE,
            )

        if (
            request.action_id != plan.action_id
            or request.action_generation != plan.action_generation
        ):
            return self._refused(
                request, plan, "the supplied action id/generation does not pin this exact pair"
            )
        if (
            request.supersede != plan.supersede_requested
            or (
                request.supersede
                and (
                    request.supersedes_generation != plan.supersedes_generation
                    or request.supersedes_journal != plan.supersedes_journal
                    or request.supersedes_revision != plan.supersedes_revision
                )
            )
            or (
                not request.supersede
                and (
                    request.supersedes_generation
                    or request.supersedes_journal
                    or request.supersedes_revision
                )
            )
        ):
            return self._refused(
                request,
                plan,
                "the supplied zero-effect supersede pins do not match this transaction",
            )
        progressed_replay = self._ops.transaction_is_progressed_replay(request, plan)
        if (
            request.gateway_approval_health not in APPROVAL_HEALTH_STATES
            or request.worker_approval_health not in APPROVAL_HEALTH_STATES
        ):
            return self._refused(
                request,
                plan,
                "the approval-time health classification for both slots is required",
            )
        if not progressed_replay and (
            request.gateway_approval_health != plan.gateway.approval_health
            or request.worker_approval_health != plan.worker.approval_health
        ):
            return self._refused(
                request,
                plan,
                "a slot's health changed after the owner approval preflight",
            )
        hard_blockers = tuple(
            reason
            for reason in plan.blocked_reasons
            if not (progressed_replay and reason == BLOCK_PAIR_INCOMPLETE)
        )
        if hard_blockers:
            return self._refused(
                request, plan, "preflight blocked: " + ", ".join(hard_blockers)
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
