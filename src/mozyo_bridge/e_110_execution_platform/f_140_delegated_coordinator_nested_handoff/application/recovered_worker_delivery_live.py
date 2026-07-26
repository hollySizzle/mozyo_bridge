"""Live authority join for recovered managed-pair worker delivery (#14203 R18)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Tuple

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovered_worker_delivery import (  # noqa: E501
    RecoveredWorkerDeliveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_FAILED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
    LiveHibernatedPairRecoveryOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_worker_delivery import (  # noqa: E501
    recovered_worker_forward_attempt_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    KIND_IMPLEMENTATION_REQUEST,
    RecoveryAnchorDeliveryRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
    decode_assigned_name,
    encode_assigned_name,
)


class LiveRecoveredWorkerDeliveryOps(LiveHibernatedPairRecoveryOps):
    """Own the recovery-only worker authority join; reuse the fenced send core."""

    def _target_delivery_ready(
        self,
        *,
        assigned_name: str,
        target_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        workspace_id: str,
        kind: str,
    ) -> bool:
        locator, revision = self._gateway_live_target(assigned_name)
        decoded = decode_assigned_name(assigned_name)
        if not locator or not revision or not decoded.ok or decoded.identity is None:
            return False
        try:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
                LiveRecoveryAnchorDeliveryService,
            )

            result = LiveRecoveryAnchorDeliveryService(
                repo_root=self.repo_root,
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
                attestation_home=self.attestation_home,
            ).preflight(
                RecoveryAnchorDeliveryRequest(
                    issue=_norm(issue),
                    journal=_norm(journal),
                    kind=_norm(kind),
                    workspace_id=_norm(workspace_id),
                    lane_id=_norm_lane(lane),
                    provider=_norm(decoded.identity.role),
                    target_assigned_name=_norm(assigned_name),
                    target_locator=locator,
                    target_revision=revision,
                    target_action_id=_norm(target_action_id),
                )
            )
        except (Exception, SystemExit):
            return False
        return bool(result.may_deliver)

    def _worker_delivery_context(
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
    ) -> Tuple[str, str]:
        """Join lifecycle authority and work authority without equating them."""

        if not all(
            _norm(value)
            for value in (
                retry_of_action_id,
                target_action_id,
                issue,
                lane,
                journal,
                lifecycle_decision_journal,
                approval_journal,
                prior_zero_send_journal,
                workspace_id,
            )
        ):
            return "", "worker_retry_authority_incomplete"
        if _norm(approval_journal) != _norm(self.request_journal):
            return "", "worker_retry_authority_context_mismatch"
        try:
            rec = LaneLifecycleStore(home=self.lifecycle_home).get(
                LaneLifecycleKey(_norm(workspace_id), _norm_lane(lane))
            )
        except (LaneLifecycleError, OSError, ValueError):
            return "", "lifecycle_unreadable"
        if not (
            rec is not None
            and rec.lane_disposition == DISPOSITION_ACTIVE
            and _norm(rec.issue_id) == _norm(issue)
            and _norm(rec.decision_issue_id) == _norm(issue)
            and _norm(rec.decision_journal)
            == _norm(lifecycle_decision_journal)
            and int(getattr(rec, "lane_generation", 0) or 0) > 0
        ):
            return "", "lifecycle_decision_moved"
        pair = read_declared_pin_pair(rec)
        if not pair.ok or pair.gateway is None or pair.worker is None:
            return "", "declared_pair_unresolved"
        gateway_name = encode_assigned_name(
            _norm(workspace_id),
            _norm(pair.gateway.provider),
            _norm_lane(lane),
        )
        worker_name = encode_assigned_name(
            _norm(workspace_id),
            _norm(pair.worker.provider),
            _norm_lane(lane),
        )
        if (
            _norm(getattr(pair.gateway, "assigned_name", "")) != gateway_name
            or _norm(getattr(pair.worker, "assigned_name", "")) != worker_name
        ):
            return "", "declared_pair_identity_mismatch"
        try:
            expected_retry = recovered_worker_forward_attempt_id(
                issue=issue,
                lane=lane,
                workspace_id=workspace_id,
                lane_generation=rec.lane_generation,
                lifecycle_decision_journal=lifecycle_decision_journal,
                anchor_journal=journal,
                target_action_id=target_action_id,
                target_assigned_name=worker_name,
            )
        except ValueError:
            return "", "worker_retry_identity_incomplete"
        if _norm(retry_of_action_id) != expected_retry:
            return "", "worker_retry_identity_mismatch"
        if not self._retry_authority_is_exact(
            retry_of_action_id=retry_of_action_id,
            issue=issue,
            lane=lane,
            journal=journal,
            approval_journal=approval_journal,
            prior_zero_send_journal=prior_zero_send_journal,
            workspace_id=workspace_id,
            target_assigned_name=worker_name,
        ):
            return "", "worker_retry_authority_unverified"
        if not self._target_delivery_ready(
            assigned_name=gateway_name,
            target_action_id=target_action_id,
            issue=issue,
            lane=lane,
            journal=journal,
            workspace_id=workspace_id,
            kind="reply",
        ):
            return "", "recovered_gateway_not_current"
        if not self._target_delivery_ready(
            assigned_name=worker_name,
            target_action_id=target_action_id,
            issue=issue,
            lane=lane,
            journal=journal,
            workspace_id=workspace_id,
            kind=KIND_IMPLEMENTATION_REQUEST,
        ):
            return "", "recovered_worker_not_current"
        return worker_name, "ready"

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
    ) -> Tuple[bool, str]:
        worker, detail = self._worker_delivery_context(
            retry_of_action_id=retry_of_action_id,
            target_action_id=target_action_id,
            issue=issue,
            lane=lane,
            journal=journal,
            lifecycle_decision_journal=lifecycle_decision_journal,
            approval_journal=approval_journal,
            prior_zero_send_journal=prior_zero_send_journal,
            workspace_id=workspace_id,
        )
        return bool(worker), detail

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
    ) -> str:
        if not _norm(action_id):
            return REDISPATCH_FAILED

        def current_context() -> Tuple[str, str]:
            return self._worker_delivery_context(
                retry_of_action_id=retry_of_action_id,
                target_action_id=target_action_id,
                issue=issue,
                lane=lane,
                journal=journal,
                lifecycle_decision_journal=lifecycle_decision_journal,
                approval_journal=approval_journal,
                prior_zero_send_journal=prior_zero_send_journal,
                workspace_id=workspace_id,
            )

        worker_name, _detail = current_context()
        if not worker_name:
            return REDISPATCH_FAILED

        def action_time_authority() -> bool:
            fresh_name, fresh_detail = current_context()
            return fresh_detail == "ready" and fresh_name == worker_name

        return self._redispatch_with_action(
            action_id=action_id,
            target_action_id=target_action_id,
            gateway_assigned_name=worker_name,
            issue=issue,
            lane=lane,
            journal=journal,
            workspace_id=workspace_id,
            pre_send_authority=action_time_authority,
        )


def build_live_recovered_worker_delivery_use_case(
    *,
    repo_root: Path,
    env: Mapping[str, str],
    issue: str,
    lane: str,
    journal: str,
) -> RecoveredWorkerDeliveryUseCase:
    return RecoveredWorkerDeliveryUseCase(
        ops=LiveRecoveredWorkerDeliveryOps(
            repo_root=repo_root,
            request_issue=issue,
            request_lane=lane,
            request_journal=journal,
            env=dict(env),
        )
    )


__all__ = (
    "LiveRecoveredWorkerDeliveryOps",
    "build_live_recovered_worker_delivery_use_case",
)
