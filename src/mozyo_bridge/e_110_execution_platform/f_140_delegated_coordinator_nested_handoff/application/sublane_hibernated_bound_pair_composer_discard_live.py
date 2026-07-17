"""Live adapter for ``sublane prepare-bound-pair`` (Redmine #13933).

The only destructive effect is delegated to the existing exact-generation actuator and
guarded close adapter.  This module neither types nor sends input, and it never changes lane
disposition or declared pins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.core.state.lane_lifecycle import DecisionPointer, norm
from mozyo_bridge.core.state.replacement_preservation import (
    PreservationObservation,
    assess_preservation,
    identity_observation_for,
)
from mozyo_bridge.core.state.replacement_transaction import (
    CAS_ALREADY_DECLARED,
    ContinuationPointer,
    ParticipantPin,
    ReplacementTransactionKey,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_CLOSE_OWED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (
    PreparationDrive,
    PreparationObservation,
    PrepareBoundPairRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    ConvergeBoundPairRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live import (
    LiveBoundPairConvergenceOps,
    _BoundPairActuatorPort,
    _SnapshotRecoveryOps,
    _git,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (
    LiveSublaneQuarantineOps,
    QuarantineRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_composer_discard import (
    APPROVAL_GATE,
    PreparationExpectation,
    expectation_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import worktree_digest
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_HEALTHY,
    SLOT_PRESERVE_PENDING,
    SLOT_RECOVER,
    decide_slot_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (
    ACTUATION_RECOVERED,
    OLD_SLOT_ABSENT,
    OLD_SLOT_AMBIGUOUS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
    list_herdr_agent_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
)


def _convergence_request(request: PrepareBoundPairRequest) -> ConvergeBoundPairRequest:
    return ConvergeBoundPairRequest(
        issue=request.issue,
        journal=request.journal,
        lane=request.lane,
        worktree=request.worktree,
        branch=request.branch,
    )


@dataclass
class _ComposerDiscardActuatorPort(_BoundPairActuatorPort):
    prepare_request: PrepareBoundPairRequest
    approved_roles: tuple[str, ...]

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        observed = super().observe_old_slot(pin)
        # An absent locator while the immutable participant still says ``close_owed`` is
        # not proof that THIS transaction closed it.  Only the durable ``launch_owed``
        # transition can carry that proof, and that phase bypasses this probe entirely.
        if observed == OLD_SLOT_ABSENT and pin.phase == PARTICIPANT_CLOSE_OWED:
            return OLD_SLOT_AMBIGUOUS
        return observed

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        """Admit exactly the approved uncorrelated pending composer at the close edge.

        The normal convergence port deliberately treats pending input as preservation.  This
        sibling port re-runs every identity/lifecycle/worktree/composer fact and clears only
        that one reason for a participant named by the distinct discard approval.
        """
        try:
            rows = tuple(list_herdr_agent_rows(self.owner.env))
        except Exception:  # noqa: BLE001
            return PreservationObservation(detail="inventory_unreadable")
        self.live.snapshot_rows = rows
        record = self.owner._lifecycle(self.request)
        if record is None:
            return PreservationObservation(detail="lifecycle_unreadable")
        worktree, workspace, identity = self.owner._worktree(self.request)
        if worktree is None or not workspace or not identity:
            return PreservationObservation(detail="worktree_identity_unreadable")
        ok_branch, branch = _git(worktree, "branch", "--show-current")
        ok_status, status = _git(worktree, "status", "--porcelain=v1")
        observation, locator, assigned = self.live.observe_slot(
            role=pin.role,
            provider=pin.provider,
            workspace_id=self.live.workspace_id(),
            lane=self.request.lane,
            record=record,
        )
        disposition = decide_slot_recovery(observation)
        authority_ok = bool(
            pin.role in self.approved_roles
            and disposition == SLOT_PRESERVE_PENDING
            and norm(workspace) == norm(self.live.workspace_id())
            and self.owner._lifecycle_exact(self.request, record, identity)
            and identity_observation_for(
                pin,
                observed_lane_id=self.request.lane,
                observed_role=pin.role,
                observed_provider=pin.provider,
                observed_assigned_name=assigned,
                observed_locator=locator,
                observed_lane_revision=str(record.revision),
                observed_lane_generation=str(record.lane_generation),
            )
        )
        composer_ok = bool(
            authority_ok
            and self.owner._composer_discardable(
                self.prepare_request,
                role=pin.role,
                provider=pin.provider,
                assigned_name=pin.assigned_name,
                locator=pin.old_locator,
            )
        )
        branch_ok = bool(ok_branch and norm(branch) == norm(self.request.branch))
        clean = bool(ok_status and not status and branch_ok)
        return PreservationObservation(
            dirty_diff=not clean,
            running_process=not observation.not_productive,
            # This is the only override: the distinct structured approval authorizes
            # discarding this exact uncorrelated pending composer generation.
            pending_approval=not composer_ok,
            identity_matches=authority_ok,
            # The old slot may be unattested; exact owner approval + positive pair identity
            # is its close authority.  The replacement must be action-attested below.
            attestation_fresh=composer_ok,
            detail=(
                "approved_exact_uncorrelated_pending_composer"
                if composer_ok and clean
                else f"{disposition}; discard_or_lifecycle_authority_changed"
            ),
        )


@dataclass
class LiveBoundPairPreparationOps(LiveBoundPairConvergenceOps):
    """Hibernated-bound observation plus the separate composer-discard transaction."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=dict)

    def _composer_discardable(
        self,
        request: PrepareBoundPairRequest,
        *,
        role: str,
        provider: str,
        assigned_name: str,
        locator: str,
    ) -> bool:
        """Positive raw facts for one uncorrelated, non-productive pending composer."""
        try:
            rows = tuple(list_herdr_agent_rows(self.env))
        except Exception:  # noqa: BLE001
            return False
        exact = [
            row for row in rows
            if norm(row.get(AGENT_KEY_NAME)) == norm(assigned_name)
            and _agent_locator(row) == norm(locator)
        ]
        named = [row for row in rows if norm(row.get(AGENT_KEY_NAME)) == norm(assigned_name)]
        if len(exact) != 1 or len(named) != 1:
            return False
        revision = exact[0].get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            return False
        try:
            inspection = LiveSublaneQuarantineOps(
                repo_root=Path(request.worktree).expanduser(), env=self.env
            ).inspect(
                QuarantineRequest(
                    issue=request.issue,
                    lane=request.lane,
                    journal=request.journal,
                    role=provider,
                    assigned_name=assigned_name,
                    locator=locator,
                    action_generation="inspection-only",
                    approval_observed_at="1970-01-01T00:00:00+00:00",
                    approved_revision=revision,
                )
            )
        except Exception:  # noqa: BLE001
            return False
        signal = inspection.signal
        return bool(
            inspection.receiver_present is True
            and signal.inventory_readable
            and signal.generation_matches
            and signal.has_pending is True
            and signal.agent_state.strip().lower() not in ("busy", "working")
            and not signal.correlation_ambiguous
            and not signal.correlated_marker_ids
        )

    def observe(
        self, request: PrepareBoundPairRequest, *, action_id: str = ""
    ) -> PreparationObservation:
        base = super().observe(_convergence_request(request), action_id=action_id)
        discard: list[str] = []
        rejected_pending: list[str] = []
        for slot in base.slots:
            if slot.disposition != SLOT_PRESERVE_PENDING:
                continue
            if self._composer_discardable(
                request,
                role=slot.role,
                provider=slot.provider,
                assigned_name=slot.assigned_name,
                locator=slot.locator,
            ):
                discard.append(slot.role)
            else:
                rejected_pending.append(slot.role)
        detail = base.detail
        if rejected_pending:
            detail = "pending composer is correlated, productive, ambiguous, or unreadable: " + ",".join(
                sorted(rejected_pending)
            )
        return PreparationObservation(
            workspace_id=base.workspace_id,
            worktree_path=base.worktree_path,
            worktree_identity=base.worktree_identity,
            branch=base.branch,
            revision=base.revision,
            generation=base.generation,
            lifecycle_exact=base.lifecycle_exact,
            pins_empty=base.pins_empty,
            inventory_readable=base.inventory_readable,
            worktree_readable=base.worktree_readable,
            worktree_clean=base.worktree_clean,
            branch_matches=base.branch_matches,
            slots=base.slots,
            discard_roles=tuple(sorted(discard)),
            detail=detail,
        )

    def approval_fields(self, issue: str, journal: str) -> Sequence[Mapping[str, str]]:
        source = LiveRedmineJournalSource.from_environment(environ=self.env)
        entries = source.read_entries(issue)
        exact = [entry for entry in entries if norm(entry.journal_id) == norm(journal)]
        fields: list[Mapping[str, str]] = []
        for entry in exact:
            for channel, marker in marker_fields_in_note(entry.notes):
                if channel == MARKER_CHANNEL_WORKFLOW_EVENT and norm(marker.get("gate")) == APPROVAL_GATE:
                    fields.append(marker)
        return tuple(fields)

    @staticmethod
    def _observation_matches(
        request: PrepareBoundPairRequest,
        observation: PreparationObservation,
        expectation: PreparationExpectation,
    ) -> bool:
        return bool(
            observation.lifecycle_exact
            and observation.pins_empty
            and observation.inventory_readable
            and observation.worktree_readable
            and observation.worktree_clean
            and observation.branch_matches
            and observation.revision == expectation.revision
            and observation.generation == expectation.generation
            and worktree_digest(
                resolved_path=observation.worktree_path,
                identity=observation.worktree_identity,
                branch=observation.branch,
            ) == expectation.worktree_digest
            and len(observation.slots) == 2
            and {slot.role for slot in observation.slots} == {"gateway", "worker"}
            and all(slot.provider and slot.assigned_name for slot in observation.slots)
            and request.issue == expectation.issue
            and request.lane == expectation.lane
        )

    def _finish(self, key: ReplacementTransactionKey, expectation: PreparationExpectation, holder: str) -> bool:
        record = self.transaction_store.get(key)
        if record is None:
            return False
        if record.phase == PHASE_COMPLETED:
            return True
        if record.phase == PHASE_REPLACING_NONSELF:
            moved = self.transaction_store.transition_phase(
                key,
                expected_revision=record.revision,
                expected_action_generation=expectation.action_generation,
                target=PHASE_DRAINING_CONTINUATION,
                holder=holder,
            )
            if not moved.applied:
                return False
            record = self.transaction_store.get(key)
        if record is None or record.phase != PHASE_DRAINING_CONTINUATION:
            return False
        done = self.transaction_store.transition_phase(
            key,
            expected_revision=record.revision,
            expected_action_generation=expectation.action_generation,
            target=PHASE_COMPLETED,
            holder=holder,
        )
        if not done.applied:
            return False
        record = self.transaction_store.get(key)
        if record is not None and record.lease_holder == holder:
            self.transaction_store.release(
                key,
                expected_revision=record.revision,
                expected_action_generation=expectation.action_generation,
                holder=holder,
            )
        return True

    def drive(
        self,
        request: PrepareBoundPairRequest,
        expectation: PreparationExpectation,
        initial: PreparationObservation,
    ) -> PreparationDrive:
        try:
            key = ReplacementTransactionKey(initial.workspace_id, expectation.action_id)
            existing = self.transaction_store.get(key)
        except Exception as exc:  # noqa: BLE001
            return PreparationDrive(False, "transaction_conflict", type(exc).__name__)

        current = self.observe(request, action_id=expectation.action_id if existing else "")
        if not self._observation_matches(request, current, expectation):
            return PreparationDrive(False, "transaction_conflict", "approval-bound observation changed")
        decision = DecisionPointer(
            source="redmine", issue_id=request.issue, journal_id=request.journal
        )
        continuation = ContinuationPointer(
            source="redmine",
            issue_id=request.issue,
            journal_id=request.journal,
            expected_gate=APPROVAL_GATE,
            next_semantic_action="converge_bound_pair",
        )
        planned: tuple[ParticipantPin, ...] | None = None
        if existing is None:
            # Final full re-read before the first durable transaction write.  It must be
            # byte-equal to the pre-approval command observation and to the structured marker.
            fresh = self.observe(request)
            if fresh != initial or not self._observation_matches(request, fresh, expectation):
                return PreparationDrive(False, "transaction_conflict", "observation changed before transaction plan")
            try:
                fresh_expectation = expectation_for(
                    issue=request.issue,
                    lane=request.lane,
                    revision=fresh.revision,
                    generation=fresh.generation,
                    resolved_worktree=fresh.worktree_path,
                    worktree_identity=fresh.worktree_identity,
                    branch=fresh.branch,
                    slots=fresh.slots,
                    discard_roles=fresh.discard_roles,
                )
            except ValueError as exc:
                return PreparationDrive(False, "transaction_conflict", str(exc))
            if fresh_expectation != expectation:
                return PreparationDrive(False, "transaction_conflict", "approval marker does not equal fresh pair")
            selected = [slot for slot in fresh.slots if slot.role in expectation.discard_roles]
            if (
                len(selected) != len(expectation.discard_roles)
                or any(slot.disposition != SLOT_PRESERVE_PENDING or not slot.locator for slot in selected)
                or any(
                    slot.disposition == SLOT_PRESERVE_PENDING
                    and slot.role not in expectation.discard_roles
                    for slot in fresh.slots
                )
                or any(
                    slot.disposition not in (SLOT_PRESERVE_PENDING, SLOT_RECOVER, SLOT_HEALTHY)
                    for slot in fresh.slots
                )
            ):
                return PreparationDrive(False, "transaction_conflict", "discard role set is no longer exact")
            planned = tuple(
                ParticipantPin(
                    lane_id=request.lane,
                    role=slot.role,
                    provider=slot.provider,
                    assigned_name=slot.assigned_name,
                    old_locator=slot.locator,
                    is_self=False,
                    lane_revision=str(expectation.revision),
                    lane_generation=str(expectation.generation),
                )
                for slot in selected
            )
            try:
                result = self.transaction_store.plan_transaction(
                    key,
                    action_generation=expectation.action_generation,
                    decision=decision,
                    continuation=continuation,
                    participants=planned,
                )
                current_record = self.transaction_store.get(key)
            except Exception as exc:  # noqa: BLE001
                return PreparationDrive(False, "transaction_conflict", type(exc).__name__)
            if current_record is None or (not result.applied and result.reason != CAS_ALREADY_DECLARED):
                return PreparationDrive(False, "transaction_conflict", result.reason)
        else:
            current_record = existing

        if (
            current_record.action_generation != expectation.action_generation
            or current_record.decision != decision
            or current_record.continuation != continuation
            or not current_record.participants
            or {participant.role for participant in current_record.participants}
            != set(expectation.discard_roles)
            or any(
                participant.is_self
                or norm(participant.lane_id) != norm(request.lane)
                or participant.lane_revision != str(expectation.revision)
                or participant.lane_generation != str(expectation.generation)
                for participant in current_record.participants
            )
            or (
                planned is not None
                and {(p.identity, p.old_locator) for p in current_record.participants}
                != {(p.identity, p.old_locator) for p in planned}
            )
        ):
            return PreparationDrive(False, "transaction_conflict", "immutable transaction header mismatch")

        # On retry, a still-owed close must remain the exact approved uncorrelated pending
        # composer.  Later phases rely on the immutable close proof and may legitimately have
        # an absent or freshly launched slot.
        for participant in current_record.participants:
            if participant.phase != PARTICIPANT_CLOSE_OWED:
                continue
            slot = next(
                (slot for slot in current.slots if slot.role == participant.role), None
            )
            if (
                slot is None
                or slot.disposition != SLOT_PRESERVE_PENDING
                or participant.role not in current.discard_roles
                or not self._composer_discardable(
                    request,
                    role=participant.role,
                    provider=participant.provider,
                    assigned_name=participant.assigned_name,
                    locator=participant.old_locator,
                )
            ):
                return PreparationDrive(False, "transaction_conflict", "owed close lost discard authority")

        try:
            rows = tuple(list_herdr_agent_rows(self.env))
        except Exception as exc:  # noqa: BLE001
            return PreparationDrive(False, "inventory_unreadable", type(exc).__name__)
        live = _SnapshotRecoveryOps(
            repo_root=self.repo_root,
            request_issue=request.issue,
            request_lane=request.lane,
            request_journal=request.journal,
            env=self.env,
        )
        live.snapshot_rows = rows
        live.target_workspace_id = current.workspace_id
        port = _ComposerDiscardActuatorPort(
            self,
            _convergence_request(request),
            expectation,  # compatible identity fields used by inherited close/launch port
            live,
            request,
            expectation.discard_roles,
        )
        holder = f"prepare:{expectation.action_id}:g{expectation.action_generation}"
        result = ReplacementActuatorUseCase(
            self.transaction_store,
            port,
            preservation_policy=assess_preservation,
        ).drive_worker_recovery(
            key,
            holder=holder,
            expected_action_generation=expectation.action_generation,
        )
        if result.status != ACTUATION_RECOVERED:
            return PreparationDrive(
                False,
                result.status,
                result.detail or ",".join(result.preservation_reasons),
            )
        if not self._finish(key, expectation, holder):
            return PreparationDrive(False, "completion_stopped", "transaction completion CAS stopped")
        return PreparationDrive(True, ACTUATION_RECOVERED)


__all__ = ("LiveBoundPairPreparationOps",)
