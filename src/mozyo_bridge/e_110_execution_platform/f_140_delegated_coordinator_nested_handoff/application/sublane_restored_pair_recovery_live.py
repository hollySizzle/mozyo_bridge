"""Live composition for ``sublane recover-restored-pair`` (Redmine #15227)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

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
    PARTICIPANT_CLOSE_OWED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
    transaction_has_zero_actuation_effect,
)
from mozyo_bridge.core.state.replacement_transaction_reapproval import (
    reapprove_zero_effect_transaction,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.worker_recovery_phase_verdict import (  # noqa: E501
    VERDICT_ALREADY_RECOVERED,
    VERDICT_DRIVABLE,
    worker_recovery_phase_verdict,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_owner_approval import (  # noqa: E501
    RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT,
    RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_recovery import (  # noqa: E501
    APPROVAL_DEGRADED,
    APPROVAL_HEALTH_STATES,
    RestoredPairPlan,
    RestoredSlot,
    restored_pair_approval_operation,
    restored_pair_authority_fields,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_RECOVERED,
    ATTEST_BOUND,
    LAUNCH_DONE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

_CONTINUATION_ACTION = "pair_relaunch_no_dispatch"


def _slot_identity(plan: RestoredPairPlan, slot: RestoredSlot):
    return (plan.lane, slot.provider, slot.provider, slot.assigned_name)


def _fresh_pair_close_requalified(
    approved: RestoredPairPlan,
    fresh: RestoredPairPlan,
    *,
    pending_identities: set[tuple[str, str, str, str]],
    all_participants_close_owed: bool,
    approval_health_by_identity: Mapping[
        tuple[str, str, str, str], str
    ],
) -> bool:
    """Re-join restored-pair authority immediately before a live close.

    A new close is allowed only while the immutable approval pins and every still-close-owed
    participant remain current, uniquely observed, settled, and readable.  Once another
    participant has progressed, its fresh replacement is deliberately outside this close
    qualification: replay must not compare a new action-bound slot with the old-generation
    pin it has already replaced.
    """

    if not pending_identities:
        return False
    if (
        fresh.issue != approved.issue
        or fresh.lane != approved.lane
        or fresh.workspace_id != approved.workspace_id
        or fresh.action_generation != approved.action_generation
        or restored_pair_authority_fields(fresh)
        != restored_pair_authority_fields(approved)
        or not fresh.lifecycle_current
        or not fresh.worktree_authority_current
    ):
        return False

    fresh_slots = {
        _slot_identity(fresh, slot): slot
        for slot in fresh.slots
    }
    for identity in pending_identities:
        approval_health = approval_health_by_identity.get(identity, "")
        slot = fresh_slots.get(identity)
        if (
            approval_health not in APPROVAL_HEALTH_STATES
            or slot is None
            or not slot.complete
            or not slot.runtime_settled
            or not slot.attestation_readable
            or (
                all_participants_close_owed
                and slot.approval_health != approval_health
            )
            or (approval_health == APPROVAL_DEGRADED and slot.healthy)
        ):
            return False

    # Before the first close, every slot must retain its approval-time health classification
    # and the entire pair must still be recovery-eligible.  This re-applies healthy/default/
    # composer-loss and every aggregate blocker at the destructive edge.  During a progressed
    # replay, only remaining close-owed members are checked above; already-replaced members
    # must not strand convergence.  A pending slot that was approved degraded may never be
    # closed after it heals, even during that partial convergence.
    return fresh.may_recover if all_participants_close_owed else True


@dataclass
class _PairCloseBoundary:
    store: ReplacementTransactionStore
    key: ReplacementTransactionKey
    request: RestoredPairRecoveryRequest
    approved_plan: RestoredPairPlan
    observe: Callable[[], RestoredPairPlan]

    def __call__(self, pin: ParticipantPin) -> bool:
        try:
            current = self.store.get(self.key)
            if (
                current is None
                or current.action_generation != self.request.action_generation
            ):
                return False
            pending = {
                candidate.identity
                for candidate in current.participants
                if candidate.phase == PARTICIPANT_CLOSE_OWED
            }
            if pin.identity not in pending:
                return False
            fresh = self.observe()
            if not isinstance(fresh, RestoredPairPlan):
                return False
            return _fresh_pair_close_requalified(
                self.approved_plan,
                fresh,
                pending_identities=pending,
                all_participants_close_owed=(
                    len(pending) == len(current.participants)
                ),
                approval_health_by_identity={
                    _slot_identity(self.approved_plan, self.approved_plan.gateway): (
                        self.request.gateway_approval_health
                    ),
                    _slot_identity(self.approved_plan, self.approved_plan.worker): (
                        self.request.worker_approval_health
                    ),
                },
            )
        except Exception:  # noqa: BLE001 - an unreadable action-time join is zero close
            return False


@dataclass
class _PairActuatorPort:
    ports: Mapping[tuple[str, str, str, str], LiveRecoveryActuatorPort]
    _adopted_launches: set[tuple[str, tuple[str, str, str, str]]] = field(
        default_factory=set, init=False, repr=False
    )

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
        key = (action_id, pin.identity)
        if key in self._adopted_launches:
            # The launch effect already completed before the durable launch_owed ->
            # verify_owed CAS.  The action-bound live attestation was re-joined by
            # admit_action_bound_live(), so this is an idempotent adopt, not a second
            # Herdr launch.  verify_attestation() is still called again by the actuator
            # at verify_owed, closing the race between this read and durable completion.
            return LAUNCH_DONE
        return self._port(pin).launch_action_bound(action_id, pin)

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        return self._port(pin).verify_attestation(action_id, pin)

    def admit_action_bound_live(self, action_id: str, pin: ParticipantPin) -> bool:
        """Adopt only a fresh live slot already bound to this exact replacement.

        This is the launch-effect/CAS crash-window fence.  A normal, foreign, stale,
        ambiguous, or unreadable slot cannot return ATTEST_BOUND through the canonical
        live identity join and therefore remains blocked by the lane-free probe.
        """

        if self._port(pin).verify_attestation(action_id, pin) != ATTEST_BOUND:
            return False
        self._adopted_launches.add((action_id, pin.identity))
        return True


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
        plan = LiveRestoredPairObservation(
            repo_root=self.repo_root,
            env=self.env,
            lifecycle_home=self.lifecycle_home,
            attestation_home=self.attestation_home,
        ).observe(request)
        requested_generation = request.action_generation
        if not isinstance(requested_generation, int) or isinstance(
            requested_generation, bool
        ) or requested_generation < 1:
            requested_generation = 1
        plan = replace(plan, action_generation=requested_generation)
        if not request.supersede:
            return plan
        return self._observe_supersede_plan(request, plan)

    @staticmethod
    def _participants(plan: RestoredPairPlan) -> list[ParticipantPin]:
        return [
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

    def _observe_supersede_plan(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> RestoredPairPlan:
        """Derive the exact next-generation pins, or reconstruct an applied rerun."""

        blocked = replace(plan, supersede_requested=True)
        try:
            key = ReplacementTransactionKey(plan.workspace_id, plan.action_id)
            existing = self._store().get(key)
            if existing is None or not self._existing_participants_match(
                existing, self._participants(plan), workspace_id=plan.workspace_id
            ):
                return blocked

            # CAS-success/process-crash replay: the durable header already names the new
            # journal/generation. Reconstruct the old provenance from the exact approved
            # request; the approval digest makes those values tamper-evident.
            if request.journal:
                decision = DecisionPointer("redmine", plan.issue, request.journal)
                continuation = ContinuationPointer(
                    "redmine",
                    plan.issue,
                    request.journal,
                    RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
                    _CONTINUATION_ACTION,
                )
                if (
                    existing.action_generation == request.action_generation
                    and existing.decision == decision
                    and existing.continuation == continuation
                ):
                    return replace(
                        plan,
                        action_generation=request.action_generation,
                        supersede_requested=True,
                        supersedes_generation=request.supersedes_generation,
                        supersedes_journal=request.supersedes_journal,
                        supersedes_revision=request.supersedes_revision,
                    )

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if (
                not transaction_has_zero_actuation_effect(existing)
                or existing.lease_is_live(now)
                or existing.decision.source != "redmine"
                or existing.decision.issue_id != plan.issue
                or existing.continuation.source != "redmine"
                or existing.continuation.issue_id != plan.issue
                or existing.continuation.journal_id
                != existing.decision.journal_id
                or existing.continuation.expected_gate
                != RESTORED_PAIR_RECOVERY_APPROVAL_GATE
                or existing.continuation.next_semantic_action != _CONTINUATION_ACTION
            ):
                return blocked
            return replace(
                plan,
                action_generation=existing.action_generation + 1,
                supersede_requested=True,
                supersedes_generation=existing.action_generation,
                supersedes_journal=existing.decision.journal_id,
                supersedes_revision=existing.revision,
            )
        except Exception:  # noqa: BLE001 - unreadable supersede authority fails closed
            return blocked

    @staticmethod
    def _transaction_authority(
        request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ):
        key = ReplacementTransactionKey(plan.workspace_id, request.action_id)
        decision = DecisionPointer("redmine", plan.issue, request.journal)
        continuation = ContinuationPointer(
            "redmine",
            plan.issue,
            request.journal,
            RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
            _CONTINUATION_ACTION,
        )
        participants = LiveRestoredPairRecoveryOps._participants(plan)
        return key, decision, continuation, participants

    @staticmethod
    def _existing_participants_match(
        existing, participants, *, workspace_id: str
    ) -> bool:
        if len(existing.participants) != len(participants):
            return False
        for pin in participants:
            stored = existing.find_participant(pin.identity)
            if stored_evidence_is_foreign(stored, workspace_id=workspace_id):
                return False
            planned = participant_with_stored_evidence(pin, stored)
            if not participant_authority_matches(stored, planned):
                return False
        return True

    def transaction_is_progressed_replay(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> bool:
        try:
            key, decision, continuation, participants = self._transaction_authority(
                request, plan
            )
            existing = self._store().get(key)
            if (
                existing is None
                or existing.action_generation != request.action_generation
                or existing.decision != decision
                or existing.continuation != continuation
                or not self._existing_participants_match(
                    existing, participants, workspace_id=plan.workspace_id
                )
            ):
                return False
            verdict = worker_recovery_phase_verdict(existing)
            if verdict == VERDICT_ALREADY_RECOVERED:
                return True
            return bool(
                verdict == VERDICT_DRIVABLE
                and existing.phase == PHASE_REPLACING_NONSELF
                and any(
                    pin.phase != PARTICIPANT_CLOSE_OWED
                    for pin in existing.participants
                )
            )
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
            operation=restored_pair_approval_operation(
                plan,
                gateway_approval_health=request.gateway_approval_health,
                worker_approval_health=request.worker_approval_health,
            ),
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
            key, decision, continuation, base = self._transaction_authority(
                request, plan
            )
        except Exception:  # noqa: BLE001
            return PairReplacementResult(False, detail="approved pair pin is incomplete")

        existing = store.get(key)
        if existing is None:
            if request.supersede:
                return PairReplacementResult(
                    False,
                    detail="zero-effect supersede requires an existing exact transaction",
                )
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
        authority_diverged = (
            current.action_generation != request.action_generation
            or current.decision != decision
            or current.continuation != continuation
            or not self._existing_participants_match(
                current, participants, workspace_id=plan.workspace_id
            )
        )
        if authority_diverged and request.supersede:
            pending = {pin.identity for pin in participants}
            try:
                fresh = self.observe(request)
                fresh_exact = _fresh_pair_close_requalified(
                    plan,
                    fresh,
                    pending_identities=pending,
                    all_participants_close_owed=True,
                    approval_health_by_identity={
                        _slot_identity(plan, plan.gateway): (
                            request.gateway_approval_health
                        ),
                        _slot_identity(plan, plan.worker): (
                            request.worker_approval_health
                        ),
                    },
                )
            except Exception:  # noqa: BLE001 - supersede observation fails closed
                fresh_exact = False
            if not fresh_exact:
                return PairReplacementResult(
                    False,
                    phase=current.phase,
                    revision=current.revision,
                    detail="zero-effect supersede fresh pair requalification failed",
                )
            reanchored = reapprove_zero_effect_transaction(
                store,
                key,
                expected_revision=request.supersedes_revision,
                expected_action_generation=request.supersedes_generation,
                expected_journal=request.supersedes_journal,
                new_action_generation=request.action_generation,
                decision=decision,
                continuation=continuation,
            )
            if not reanchored.applied:
                return PairReplacementResult(
                    False,
                    phase=current.phase,
                    revision=reanchored.revision,
                    detail=f"zero-effect supersede refused ({reanchored.reason})",
                )
            current = store.get(key)
            authority_diverged = bool(
                current is None
                or current.action_generation != request.action_generation
                or current.decision != decision
                or current.continuation != continuation
                or not self._existing_participants_match(
                    current, participants, workspace_id=plan.workspace_id
                )
            )
        if authority_diverged:
            return PairReplacementResult(
                False,
                phase=current.phase if current else "",
                revision=current.revision if current else 0,
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
        close_boundary = _PairCloseBoundary(
            store=store,
            key=key,
            request=request,
            approved_plan=plan,
            observe=lambda: self.observe(request),
        )
        actuator = ReplacementActuatorUseCase(
            store,
            port,
            preservation_policy=assess_worker_recovery_preservation,
            close_authority=close_boundary,
            launch_authority=lambda pin: (
                role_ops[pin.identity].resume_lane_authority(requests[pin.identity])
                and (
                    role_ops[pin.identity].lane_free_of_live_process(
                        requests[pin.identity]
                    )
                    or port.admit_action_bound_live(request.action_id, pin)
                )
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
