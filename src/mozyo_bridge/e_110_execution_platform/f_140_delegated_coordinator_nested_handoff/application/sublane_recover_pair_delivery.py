"""``sublane recover-pair-delivery`` — the active-pair, new-action delivery surface (#13847).

Split out of the recover-pair use-case module so each surface's contract is separately
readable, and so the recovery module stays within the module-health line without an allowlist
entry (Redmine #14475). Behaviour is a verbatim move.

It shares the recovery's applied-effect / unresolved-fate contract (review j#88563 F2): a
fresh delivery is an effect, an already-delivered fence hit applies nothing, a retiring target
is a reserve-cancelled zero-send, and failed / uncertain carry an unresolved fate rather than
a claimed write.
"""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    BLOCK_IDENTITY_INCOMPLETE,
    HibernatedPairRecoveryOps,
    RecoverPairDeliveryRetryOutcome,
    RecoverPairDeliveryRetryRequest,
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_FAILED,
    REDISPATCH_TARGET_RETIRING,
    REDISPATCH_UNCERTAIN,
    _norm,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
    EFFECT_REDISPATCHED,
    FATE_REDISPATCH_UNRESOLVED,
)

@dataclass
class SublaneRecoverPairDeliveryUseCase:
    """Resume the original anchor on an already-active recovered pair under a new action."""

    ops: HibernatedPairRecoveryOps

    def run(
        self, request: RecoverPairDeliveryRetryRequest, *, execute: bool
    ) -> RecoverPairDeliveryRetryOutcome:
        issue = _norm(request.issue)
        lane = _norm(request.lane)
        workspace_id = _norm(self.ops.workspace_id())
        fields = (
            issue,
            lane,
            workspace_id,
            _norm(request.journal),
            _norm(request.implementation_request_journal),
            _norm(request.retry_of_action_id),
            _norm(request.prior_zero_send_journal),
        )
        try:
            action_id = request.action_id()
        except ValueError:
            action_id = ""
        if not all(fields) or not action_id:
            return RecoverPairDeliveryRetryOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                action_id=action_id,
                detail=BLOCK_IDENTITY_INCOMPLETE,
            )
        may_deliver, detail = self.ops.preflight_retry_redispatch_to_gateway(
            retry_of_action_id=_norm(request.retry_of_action_id),
            issue=issue,
            lane=lane,
            journal=_norm(request.implementation_request_journal),
            approval_journal=_norm(request.journal),
            prior_zero_send_journal=_norm(request.prior_zero_send_journal),
            workspace_id=workspace_id,
        )
        if not may_deliver:
            return RecoverPairDeliveryRetryOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                action_id=action_id,
                may_deliver=False,
                detail=detail or "fail-closed: recovery delivery preflight blocked",
            )
        if not execute:
            return RecoverPairDeliveryRetryOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                action_id=action_id,
                may_deliver=True,
                detail="preflight only (no --execute)",
            )
        redispatch = self.ops.retry_redispatch_to_gateway(
            action_id=action_id,
            retry_of_action_id=_norm(request.retry_of_action_id),
            issue=issue,
            lane=lane,
            journal=_norm(request.implementation_request_journal),
            approval_journal=_norm(request.journal),
            prior_zero_send_journal=_norm(request.prior_zero_send_journal),
            workspace_id=workspace_id,
        )
        effects = (
            (EFFECT_REDISPATCHED,) if redispatch == REDISPATCH_DELIVERED else ()
        )
        unresolved = (
            (FATE_REDISPATCH_UNRESOLVED,)
            if redispatch in (REDISPATCH_FAILED, REDISPATCH_UNCERTAIN)
            else ()
        )
        if redispatch == REDISPATCH_DELIVERED:
            detail = "original implementation_request delivered under a new recovery action"
        elif redispatch == REDISPATCH_ALREADY:
            # Review j#88563 F2: an already-delivered fence hit applies NOTHING; reporting it
            # as "delivered under a new action" claimed a send this run did not make.
            detail = (
                "idempotent replay: the fence already holds this delivery; nothing was sent "
                "under the new action"
            )
        elif redispatch == REDISPATCH_TARGET_RETIRING:
            detail = (
                "zero-send: the gateway is inside a retirement transaction, so the outbox "
                "reserve was cancelled"
            )
        elif unresolved:
            detail = (
                f"the redelivery's durable fate could not be established ({redispatch}); "
                "operator reconcile required"
            )
        else:
            detail = "fail-closed: recovery delivery blocked"
        return RecoverPairDeliveryRetryOutcome(
            executed=bool(effects),
            effects=effects,
            unresolved=unresolved,
            attempted=True,
            issue=issue,
            lane=lane,
            action_id=action_id,
            may_deliver=True,
            redispatch=redispatch,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# Text rendering + thin CLI handler.
# ---------------------------------------------------------------------------


__all__ = ("SublaneRecoverPairDeliveryUseCase",)
