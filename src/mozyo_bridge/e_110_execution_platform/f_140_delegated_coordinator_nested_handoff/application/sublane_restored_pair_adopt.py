"""Guarded use case for declaring a restored pair's ABSENT lifecycle pins (#15811).

The pin-absent sibling of :mod:`.sublane_restored_pair_rebind`. A lane created through the
normal create path carries an owner row with an EMPTY ``declared_slots`` snapshot; when a
herdr server generation change then restores that lane's gateway+worker pair, no existing
rail can recover it — the rebind rail has no exact old pair to replace, the create adopt
refuses the restore-stale attestation, and the drain / rehydrate rails cannot read a
current-generation proof (Redmine #15811, measured 2026-08-20).

This rail declares the pair for the first time from the SAME server-owned restore evidence
the #15769 rebind rail requires, and re-attests the launch-generation / participant /
attestation records so the unchanged read-side verifiers pass again. It never closes,
launches, sends, chmods, or touches a worktree, and it never changes ``lane_generation``:
the restored processes are the same agent-session incarnation.

Default is a read-only preflight; ``execute=True`` performs the writes only when the
preflight passed AND the ops adapter's OWN action-time re-observation still passes (the
reconciliation-rail discipline: the plan a caller saw is display, the write re-derives its
own evidence). Every refusal is zero-write with a typed reason.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_adopt import (  # noqa: E501
    RestoredPairAdoptOutcome,
    RestoredPairAdoptPlan,
    RestoredPairAdoptRequest,
    RestoredPairAdoptWriteResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_PREFLIGHT,
    STATUS_REFUSED,
)


@runtime_checkable
class RestoredPairAdoptOps(Protocol):
    def observe(self, request: RestoredPairAdoptRequest) -> RestoredPairAdoptPlan:
        ...

    def adopt(
        self, request: RestoredPairAdoptRequest
    ) -> RestoredPairAdoptWriteResult:
        """Re-observe, re-attest, and declare, returning the ACTION-TIME observation.

        The result carries the plan the write actually derived — not the preflight one —
        so the outcome can never report a locator / lineage the write did not act on
        (review j#109452 ``finding_actiontimeoutcome``).
        """
        ...


class SublaneRestoredPairAdoptUseCase:
    """Preflight-first, all-or-nothing, zero-write-on-refusal declaration driver."""

    def __init__(self, ops: RestoredPairAdoptOps) -> None:
        self._ops = ops

    def run(
        self, request: RestoredPairAdoptRequest, *, execute: bool = False
    ) -> RestoredPairAdoptOutcome:
        plan = self._ops.observe(request)
        if not execute:
            return RestoredPairAdoptOutcome(
                issue=plan.issue,
                lane=plan.lane,
                status=STATUS_PREFLIGHT,
                executed=False,
                plan=plan,
                revision=plan.revision or None,
                detail=(
                    "adopt_ready"
                    if plan.may_adopt
                    else "preflight blocked: " + ", ".join(plan.blocked_reasons)
                ),
                journal=request.journal,
            )
        if not plan.may_adopt:
            # Zero-write: an execute request against a blocked plan refuses without ever
            # reaching a store.
            return RestoredPairAdoptOutcome(
                issue=plan.issue,
                lane=plan.lane,
                status=STATUS_BLOCKED,
                executed=True,
                plan=plan,
                revision=plan.revision or None,
                detail="preflight blocked: " + ", ".join(plan.blocked_reasons),
                journal=request.journal,
            )
        # From here the PREFLIGHT plan is discarded: the write re-derived its own evidence,
        # and reporting anything else would name a locator / lineage the rail did not act on
        # (review j#109452 finding_actiontimeoutcome).
        result = self._ops.adopt(request)
        return RestoredPairAdoptOutcome(
            issue=result.plan.issue,
            lane=result.plan.lane,
            status=STATUS_COMPLETED if result.applied else STATUS_REFUSED,
            executed=True,
            plan=result.plan,
            applied=result.applied,
            revision=result.revision,
            detail=result.detail,
            journal=request.journal,
        )


__all__ = (
    "RestoredPairAdoptOps",
    "SublaneRestoredPairAdoptUseCase",
)
