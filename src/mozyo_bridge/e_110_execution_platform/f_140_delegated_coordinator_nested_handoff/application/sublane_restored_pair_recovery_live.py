"""Live composition for ``sublane recover-restored-pair`` (Redmine #15227)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.core.state.replacement_participant_authority import (
    participant_authority_matches,
    participant_with_stored_evidence,
    stored_evidence_is_foreign,
)
from mozyo_bridge.core.state.replacement_preservation import (
    assess_worker_recovery_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import (
    CAS_ALREADY_DECLARED,
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_owner_approval_live import (  # noqa: E501
    verify_live_recovery_owner_approval,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_completion import (  # noqa: E501
    build_update_evidence_completion,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner_composition import (  # noqa: E501
    plan_participants_with_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery import (  # noqa: E501
    PairReplacementResult,
    RestoredPairRecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_observation import (  # noqa: E501
    LiveRestoredPairObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E501
    RecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
    LiveRecoveryActuatorPort,
    LiveStaleWorkerRecoveryOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_owner_approval import (  # noqa: E501
    RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT,
    RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_recovery import (  # noqa: E501
    RestoredPairPlan,
    RestoredSlot,
    restored_pair_approval_operation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_RECOVERED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

_CONTINUATION_ACTION = "pair_relaunch_no_dispatch"


@dataclass
class _PairActuatorPort:
    ports: Mapping[tuple[str, str, str, str], LiveRecoveryActuatorPort]

    def _port(self, pin: ParticipantPin) -> LiveRecoveryActuatorPort:
        port = self.ports.get(pin.identity)
        if port is None:
            raise RuntimeError("replacement participant is not in the approved pair")
        return port

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        return self._port(pin).observe_old_slot(pin)

    def observe_preservation(self, pin: ParticipantPin):
        return self._port(pin).observe_preservation(pin)

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        return self._port(pin).close_exact_generation(pin)

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        return self._port(pin).launch_action_bound(action_id, pin)

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        return self._port(pin).verify_attestation(action_id, pin)


@dataclass
class LiveRestoredPairRecoveryOps:
    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS
    lifecycle_home: Optional[Path] = None
    attestation_home: Optional[Path] = None
    transaction_store: Optional[ReplacementTransactionStore] = None
    journal_reader: Optional[object] = None
    journal_reader_fresh: bool = False
    issuer_resolver: Optional[object] = None

    def _store(self) -> ReplacementTransactionStore:
        return self.transaction_store or ReplacementTransactionStore()

    def observe(self, request: RestoredPairRecoveryRequest) -> RestoredPairPlan:
        return LiveRestoredPairObservation(
            repo_root=self.repo_root,
            env=self.env,
            lifecycle_home=self.lifecycle_home,
            attestation_home=self.attestation_home,
        ).observe(request)

    def transaction_exists(self, action_id: str) -> bool:
        try:
            workspace_id = repo_scope_workspace_id(self.repo_root)
            key = ReplacementTransactionKey(workspace_id, action_id)
            return self._store().get(key) is not None
        except Exception:  # noqa: BLE001
            return False

    def approval_verified(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> bool:
        return verify_live_recovery_owner_approval(
            repo_root=self.repo_root,
            journal_reader=self.journal_reader,
            journal_reader_fresh=self.journal_reader_fresh,
            journal=request.journal,
            anchor_issue=request.issue,
            gate=RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
            effect=RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT,
            issue=plan.issue,
            lane=plan.lane,
            operation=restored_pair_approval_operation(plan),
            issuer_resolver=self.issuer_resolver,
        )

    @staticmethod
    def _recovery_request(
        request: RestoredPairRecoveryRequest,
        plan: RestoredPairPlan,
        slot: RestoredSlot,
    ) -> RecoveryRequest:
        return RecoveryRequest(
            issue=plan.issue,
            lane=plan.lane,
            role=slot.provider,
            provider=slot.provider,
            assigned_name=slot.assigned_name,
            locator=slot.locator,
            journal=request.journal,
            action_id=request.action_id,
            action_generation=request.action_generation,
            worker_revision=slot.revision,
            lane_revision=plan.lane_revision,
            lane_generation=plan.lane_generation,
        )

    def replace_pair(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> PairReplacementResult:
        store = self._store()
        try:
            key = ReplacementTransactionKey(plan.workspace_id, request.action_id)
            decision = DecisionPointer("redmine", plan.issue, request.journal)
            continuation = ContinuationPointer(
                "redmine",
                plan.issue,
                request.journal,
                RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
                _CONTINUATION_ACTION,
            )
            base = [
                ParticipantPin(
                    lane_id=plan.lane,
                    role=slot.provider,
                    provider=slot.provider,
                    assigned_name=slot.assigned_name,
                    old_locator=slot.locator,
                    lane_revision=plan.lane_revision,
                    lane_generation=plan.lane_generation,
                )
                for slot in plan.slots
            ]
        except Exception:  # noqa: BLE001
            return PairReplacementResult(False, detail="approved pair pin is incomplete")

        existing = store.get(key)
        if existing is None:
            planning = plan_participants_with_evidence(
                base,
                home=store.path.parent,
                workspace_id=plan.workspace_id,
                lane_id=plan.lane,
            )
            if planning.refused:
                return PairReplacementResult(
                    False, detail=f"update evidence planning refused ({planning.refusal})"
                )
            participants = list(planning.participants)
            declared = store.plan_transaction(
                key,
                action_generation=request.action_generation,
                decision=decision,
                continuation=continuation,
                participants=participants,
            )
            if not declared.applied and declared.reason != CAS_ALREADY_DECLARED:
                return PairReplacementResult(
                    False, revision=declared.revision,
                    detail=f"transaction plan refused ({declared.reason})",
                )
        else:
            participants = []
            for pin in base:
                stored = existing.find_participant(pin.identity)
                if stored_evidence_is_foreign(stored, workspace_id=plan.workspace_id):
                    return PairReplacementResult(False, detail="stored update evidence is foreign")
                participants.append(participant_with_stored_evidence(pin, stored))

        current = store.get(key)
        if current is None:
            return PairReplacementResult(False, detail="transaction row vanished after plan")
        if (
            current.action_generation != request.action_generation
            or current.decision != decision
            or current.continuation != continuation
            or len(current.participants) != len(participants)
            or any(
                not participant_authority_matches(
                    current.find_participant(pin.identity), pin
                )
                for pin in participants
            )
        ):
            return PairReplacementResult(
                False,
                phase=current.phase,
                revision=current.revision,
                detail="a different replacement authority already owns this action id",
            )

        requests = {
            pin.identity: self._recovery_request(
                request,
                plan,
                plan.gateway if pin.assigned_name == plan.gateway.assigned_name else plan.worker,
            )
            for pin in participants
        }
        role_ops = {
            identity: LiveStaleWorkerRecoveryOps(
                repo_root=self.repo_root,
                request=slot_request,
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
                lifecycle_home=self.lifecycle_home,
                attestation_home=self.attestation_home,
            )
            for identity, slot_request in requests.items()
        }
        ports = {
            identity: LiveRecoveryActuatorPort(
                repo_root=self.repo_root,
                request=slot_request,
                store=store,
                key=key,
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
                lifecycle_home=self.lifecycle_home,
                attestation_home=self.attestation_home,
            )
            for identity, slot_request in requests.items()
        }
        port = _PairActuatorPort(ports)
        actuator = ReplacementActuatorUseCase(
            store,
            port,
            preservation_policy=assess_worker_recovery_preservation,
            launch_authority=lambda pin: (
                role_ops[pin.identity].resume_lane_authority(requests[pin.identity])
                and role_ops[pin.identity].lane_free_of_live_process(requests[pin.identity])
            ),
            store_admission=lambda action_key, pin: role_ops[
                pin.identity
            ].replacement_store_admission(action_key, pin),
            evidence_completion=build_update_evidence_completion(store.path.parent),
        )
        result = actuator.drive_worker_recovery(
            key,
            holder=request.holder,
            expected_action_generation=request.action_generation,
        )
        current = store.get(key)
        if result.status != ACTUATION_RECOVERED or current is None:
            return PairReplacementResult(
                False,
                phase=current.phase if current else result.phase,
                revision=current.revision if current else result.revision,
                detail=(
                    f"pair replacement stopped ({result.status}"
                    + (f": {result.detail}" if result.detail else "")
                    + "); re-run the same pinned action to resume"
                ),
            )

        for source, target in (
            (PHASE_REPLACING_NONSELF, PHASE_DRAINING_CONTINUATION),
            (PHASE_DRAINING_CONTINUATION, PHASE_COMPLETED),
        ):
            current = store.get(key)
            if current is None:
                return PairReplacementResult(False, detail="transaction row vanished")
            if current.phase != source:
                continue
            moved = store.transition_phase(
                key,
                expected_revision=current.revision,
                expected_action_generation=request.action_generation,
                target=target,
                holder=request.holder,
            )
            if not moved.applied:
                return PairReplacementResult(
                    False,
                    phase=current.phase,
                    revision=moved.revision,
                    detail=f"transaction completion refused ({moved.reason})",
                )

        current = store.get(key)
        if current is None or current.phase != PHASE_COMPLETED:
            return PairReplacementResult(
                False,
                phase=current.phase if current else "",
                revision=current.revision if current else 0,
                detail="pair attested but transaction did not reach completed",
            )
        if current.lease_holder:
            released = store.release(
                key,
                expected_revision=current.revision,
                expected_action_generation=request.action_generation,
                holder=request.holder,
            )
            if not released.applied:
                return PairReplacementResult(
                    False,
                    phase=current.phase,
                    revision=released.revision,
                    detail=f"pair completed but lease release refused ({released.reason})",
                )
            current = store.get(key) or current
        return PairReplacementResult(
            True,
            phase=current.phase,
            revision=current.revision,
            detail=(
                "exact gateway/worker generations were replaced and action-bound startup "
                "attestations passed; worktree and branch were preserved"
            ),
        )


def build_live_restored_pair_recovery_ops(repo_root: Path) -> LiveRestoredPairRecoveryOps:
    journal_reader = None
    fresh = False
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        source = LiveRedmineJournalSource.from_environment()
        journal_reader = source.read_entries
        fresh = True
    except Exception:  # noqa: BLE001 - no durable reader means execute refuses approval
        pass
    return LiveRestoredPairRecoveryOps(
        repo_root=repo_root,
        env=dict(os.environ),
        journal_reader=journal_reader,
        journal_reader_fresh=fresh,
    )


__all__ = ("LiveRestoredPairRecoveryOps", "build_live_restored_pair_recovery_ops")
