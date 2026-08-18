"""Guarded use case for rebinding a restored active pair's lifecycle pins (#15656).

A herdr server restart restores an active lane's gateway+worker pair —
the SAME agent sessions, process-restored onto NEW pane locators — while the
lifecycle row's ``declared_slots`` keep pinning the old locators. The rail
CAS-replaces ONLY that stale pair snapshot from live restart evidence, so the
worker-dispatch admission's ``binds_same_generation`` join accepts the live
pair again. It never closes, launches, sends, chmods, or touches a worktree,
and it never changes ``lane_generation``: the restored processes are the same
agent-session incarnation, so every existing dispatch-marker anchor stays
valid.

Default is a read-only preflight; ``execute=True`` performs the CAS write only
when the preflight passed and the ops adapter's OWN action-time re-observation
still passes (the reconciliation-rail discipline: the plan a caller saw is
display, the write re-derives its own evidence). Every refusal is zero-write
with a typed reason.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_PREFLIGHT,
    STATUS_REFUSED,
    RestoredPairRebindOutcome,
    RestoredPairRebindPlan,
    RestoredPairRebindRequest,
)


@runtime_checkable
class RestoredPairRebindOps(Protocol):
    def observe(self, request: RestoredPairRebindRequest) -> RestoredPairRebindPlan:
        ...

    def rebind(
        self, request: RestoredPairRebindRequest
    ) -> tuple[bool, Optional[int], str]:
        """Re-observe and CAS-write. Returns ``(applied, revision, detail)``."""
        ...


class SublaneRestoredPairRebindUseCase:
    """Preflight-first, all-or-nothing, zero-write-on-refusal rebind driver."""

    def __init__(self, ops: RestoredPairRebindOps) -> None:
        self._ops = ops

    def run(
        self, request: RestoredPairRebindRequest, *, execute: bool = False
    ) -> RestoredPairRebindOutcome:
        plan = self._ops.observe(request)
        if not execute:
            return RestoredPairRebindOutcome(
                issue=plan.issue,
                lane=plan.lane,
                status=STATUS_PREFLIGHT,
                executed=False,
                plan=plan,
                revision=plan.revision or None,
                detail=(
                    "rebind_ready"
                    if plan.may_rebind
                    else "preflight blocked: " + ", ".join(plan.blocked_reasons)
                ),
                journal=request.journal,
            )
        if not plan.may_rebind:
            # Zero-write: an execute request against a blocked plan refuses
            # without ever reaching the store.
            return RestoredPairRebindOutcome(
                issue=plan.issue,
                lane=plan.lane,
                status=STATUS_BLOCKED,
                executed=True,
                plan=plan,
                revision=plan.revision or None,
                detail="preflight blocked: " + ", ".join(plan.blocked_reasons),
                journal=request.journal,
            )
        applied, revision, detail = self._ops.rebind(request)
        return RestoredPairRebindOutcome(
            issue=plan.issue,
            lane=plan.lane,
            status=STATUS_COMPLETED if applied else STATUS_REFUSED,
            executed=True,
            plan=plan,
            applied=applied,
            revision=revision,
            detail=detail,
            journal=request.journal,
        )


__all__ = (
    "RestoredPairRebindOps",
    "SublaneRestoredPairRebindUseCase",
)
