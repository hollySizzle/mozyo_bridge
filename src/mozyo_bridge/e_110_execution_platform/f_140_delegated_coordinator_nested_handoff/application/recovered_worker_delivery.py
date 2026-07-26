"""Use case for the worker leg of an already-recovered managed pair (#14203 R18)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Tuple, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_worker_delivery import (  # noqa: E501
    RecoveredWorkerDeliveryOutcome,
    RecoveredWorkerDeliveryRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)

BLOCK_IDENTITY_INCOMPLETE = "identity_or_decision_incomplete"


@runtime_checkable
class RecoveredWorkerDeliveryOps(Protocol):
    """The narrow live boundary owned by the recovered worker-delivery leg."""

    def workspace_id(self) -> str: ...

    def preflight_retry_redispatch_to_worker(
        self,
        *,
        retry_of_action_id: str,
        target_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        lifecycle_decision_journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
    ) -> Tuple[bool, str]: ...

    def retry_redispatch_to_worker(
        self,
        *,
        action_id: str,
        retry_of_action_id: str,
        target_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        lifecycle_decision_journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
    ) -> str: ...


@dataclass
class RecoveredWorkerDeliveryUseCase:
    """Deliver an unchanged work anchor to the exact recovered worker once."""

    ops: RecoveredWorkerDeliveryOps

    def run(
        self,
        request: RecoveredWorkerDeliveryRequest,
        *,
        execute: bool,
    ) -> RecoveredWorkerDeliveryOutcome:
        issue = _norm(request.issue)
        lane = _norm(request.lane)
        workspace_id = _norm(self.ops.workspace_id())
        fields = (
            issue,
            lane,
            workspace_id,
            _norm(request.journal),
            _norm(request.implementation_request_journal),
            _norm(request.lifecycle_decision_journal),
            _norm(request.target_action_id),
            _norm(request.retry_of_action_id),
            _norm(request.prior_zero_send_journal),
        )
        try:
            action_id = request.action_id()
        except ValueError:
            action_id = ""
        if not all(fields) or not action_id:
            return RecoveredWorkerDeliveryOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                action_id=action_id,
                detail=BLOCK_IDENTITY_INCOMPLETE,
            )
        may_deliver, detail = self.ops.preflight_retry_redispatch_to_worker(
            retry_of_action_id=_norm(request.retry_of_action_id),
            target_action_id=_norm(request.target_action_id),
            issue=issue,
            lane=lane,
            journal=_norm(request.implementation_request_journal),
            lifecycle_decision_journal=_norm(
                request.lifecycle_decision_journal
            ),
            approval_journal=_norm(request.journal),
            prior_zero_send_journal=_norm(request.prior_zero_send_journal),
            workspace_id=workspace_id,
        )
        if not may_deliver:
            return RecoveredWorkerDeliveryOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                action_id=action_id,
                may_deliver=False,
                detail=(
                    detail
                    or "fail-closed: worker recovery delivery preflight blocked"
                ),
            )
        if not execute:
            return RecoveredWorkerDeliveryOutcome(
                executed=False,
                issue=issue,
                lane=lane,
                action_id=action_id,
                may_deliver=True,
                detail="preflight only (no --execute)",
            )
        redispatch = self.ops.retry_redispatch_to_worker(
            action_id=action_id,
            retry_of_action_id=_norm(request.retry_of_action_id),
            target_action_id=_norm(request.target_action_id),
            issue=issue,
            lane=lane,
            journal=_norm(request.implementation_request_journal),
            lifecycle_decision_journal=_norm(
                request.lifecycle_decision_journal
            ),
            approval_journal=_norm(request.journal),
            prior_zero_send_journal=_norm(request.prior_zero_send_journal),
            workspace_id=workspace_id,
        )
        return RecoveredWorkerDeliveryOutcome(
            executed=True,
            issue=issue,
            lane=lane,
            action_id=action_id,
            may_deliver=True,
            redispatch=redispatch,
            detail=(
                "unchanged implementation_request delivered to recovered worker"
                if redispatch in (REDISPATCH_DELIVERED, REDISPATCH_ALREADY)
                else "fail-closed: worker recovery delivery blocked"
            ),
        )


__all__ = (
    "BLOCK_IDENTITY_INCOMPLETE",
    "RecoveredWorkerDeliveryOps",
    "RecoveredWorkerDeliveryUseCase",
)
