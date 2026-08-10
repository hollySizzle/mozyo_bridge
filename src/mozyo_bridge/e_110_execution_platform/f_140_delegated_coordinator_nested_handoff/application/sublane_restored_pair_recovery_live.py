"""Live composition for ``sublane recover-restored-pair`` (Redmine #15227)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.core.state.replacement_participant_authority import (
    participant_authority_matches,
    participant_with_stored_evidence,
    stored_evidence_is_foreign,
)
from mozyo_bridge.core.state.replacement_transaction import (
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_CLOSE_OWED,
    PHASE_REPLACING_NONSELF,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_owner_approval_live import (  # noqa: E501
    verify_live_recovery_owner_approval,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery import (  # noqa: E501
    PairReplacementResult,
    RestoredPairRecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_observation import (  # noqa: E501
    LiveRestoredPairObservation,
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
    RestoredPairPlan,
    restored_pair_approval_operation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

_CONTINUATION_ACTION = "pair_relaunch_no_dispatch"


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
        # Supersede inspection formerly opened the write-capable replacement
        # store and could initialize its schema.  While conditional close is
        # unavailable this public observation must remain side-effect free.
        return replace(plan, action_generation=requested_generation)

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

    def replace_pair(
        self, request: RestoredPairRecoveryRequest, plan: RestoredPairPlan
    ) -> PairReplacementResult:
        # Herdr 0.8 / protocol 19 has no mutation that consumes the observed
        # terminal generation.  Do not create or advance a replacement
        # transaction, close, relaunch, or send from this rail.  A separate
        # inventory read followed by ``pane close <pane_id>`` is not a CAS.
        return PairReplacementResult(
            False,
            detail=(
                "Herdr generation-conditional close is unavailable; "
                "zero transaction write, zero close, and zero relaunch"
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
