"""Live adapter for ``sublane prepare-bound-pair`` (Redmine #13933).

The only destructive effect is delegated to the existing exact-generation actuator and
guarded close adapter.  This module neither types nor sends input, and it never changes lane
disposition or declared pins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    replacement_action_is_bound,
)
from mozyo_bridge.core.state.lane_lifecycle import DecisionPointer, norm
from mozyo_bridge.core.state.replacement_preservation import (
    PreservationObservation,
    assess_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import (
    CAS_ALREADY_DECLARED,
    ContinuationPointer,
    ParticipantPin,
    ReplacementTransactionKey,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANT_LAUNCH_OWED,
    PARTICIPANT_REPLACED,
    PARTICIPANT_VERIFY_OWED,
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
    _launch_detail,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    BoundSlot,
    slot_digest,
    worktree_digest,
)
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
    ATTEST_BOUND,
    ATTEST_PENDING,
    CLOSE_ERROR,
    LAUNCH_ERROR,
    OLD_SLOT_ABSENT,
    OLD_SLOT_AMBIGUOUS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
    list_herdr_agent_rows,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (
    WorkflowProviderUnresolved,
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm_lane as norm_lane,
    decode_assigned_name,
)


def _action_closed_providers(slots: Sequence[BoundSlot]) -> tuple[str, ...]:
    """Provider roles whose live absence is proven by this action's own immutable close.

    ``close_proven`` is set only where the slot has no live row AND the transaction pinned by
    the approved action id carries that participant past ``close_owed``.
    """
    return tuple(slot.provider for slot in slots if slot.close_proven)


def _convergence_request(request: PrepareBoundPairRequest) -> ConvergeBoundPairRequest:
    return ConvergeBoundPairRequest(
        issue=request.issue,
        journal=request.journal,
        lane=request.lane,
        worktree=request.worktree,
        branch=request.branch,
    )


@dataclass
class _SnapshotQuarantineOps(LiveSublaneQuarantineOps):
    """Run the pending-composer classifier against one already-read inventory."""

    snapshot_rows: Sequence[Mapping[str, object]] = ()
    #: Provider roles whose live row is absent *because this immutable action closed it*
    #: (Redmine #13933 j#80934).  Only a caller holding that transaction proof may set this.
    action_closed_roles: tuple[str, ...] = ()

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return self.snapshot_rows

    def _pair_ok(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        workspace_id: str,
        lane: str,
    ) -> bool:
        """Admit the sibling absence this action itself caused, and nothing else.

        The inherited pair fence requires BOTH provider rows live and co-placed.  A
        ``prepare-bound-pair`` run that closed one role and then failed to relaunch destroys
        that premise with its own effect, so the still-owed role reads as
        ``generation_mismatch`` and the transaction can never be replayed (j#80934).  The
        absence is re-admitted only for a role whose close THIS transaction proves, and only
        while that role stays absent — a reappeared or foreign sibling falls back to the
        inherited fence, which is the authority for every live row.
        """
        if super()._pair_ok(rows, workspace_id=workspace_id, lane=lane):
            return True
        if not self.action_closed_roles:
            return False
        try:
            providers = set(self._providers())
        except WorkflowProviderUnresolved:
            return False
        closed = {norm(role) for role in self.action_closed_roles}
        # A proper subset: a wholly action-closed pair has no live composer left to classify.
        if not closed < providers:
            return False
        live: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decoded.ok or decoded.identity is None:
                continue
            identity = decoded.identity
            if (
                identity.workspace_id == workspace_id
                and norm_lane(identity.lane_id) == norm_lane(lane)
                and identity.role in providers
            ):
                live.append(identity.role)
        # Every role this action closed must still be gone (a reappeared sibling is a live row
        # the inherited fence owns), and every other role of the pair must still be live and
        # unique.  Identity / revision / cwd of the classified row remain the caller's fences.
        return (
            len(live) == len(set(live))
            and not (closed & set(live))
            and (providers - closed) <= set(live)
        )


@dataclass
class _ComposerDiscardActuatorPort(_BoundPairActuatorPort):
    prepare_request: PrepareBoundPairRequest
    approved_roles: tuple[str, ...]

    def _fresh_authority(
        self, *, require_attested_roles: Sequence[str] = ()
    ) -> PreparationObservation | None:
        """Revalidate the approval-bound full pair at one destructive-effect edge."""
        try:
            rows = tuple(list_herdr_agent_rows(self.owner.env))
            record = self.owner._lifecycle(self.request)
            worktree, workspace, identity = self.owner._worktree(self.request)
            if record is None or worktree is None or not workspace or not identity:
                return None
            ok_branch, branch = _git(worktree, "branch", "--show-current")
            ok_status, status = _git(worktree, "status", "--porcelain=v1")
            key = ReplacementTransactionKey(workspace, self.expectation.action_id)
            transaction = self.owner.transaction_store.get(key)
            if transaction is None:
                return None
            observation = self.owner._observation_from_snapshot(
                self.prepare_request,
                rows=rows,
                record=record,
                worktree=worktree,
                workspace=workspace,
                identity=identity,
                ok_branch=ok_branch,
                branch=branch,
                ok_status=ok_status,
                status=status,
                transaction=transaction,
            )
            progress = self.owner._progress_proven_roles(
                self.prepare_request,
                observation,
                self.expectation,
                transaction.participants,
                require_attested_roles=require_attested_roles,
            )
        except Exception:  # noqa: BLE001 - every unreadable edge is zero effect
            return None
        if not self.owner._progress_snapshot_matches(
            self.prepare_request,
            observation,
            self.expectation,
            transaction.participants,
            progress_proven_roles=progress,
        ):
            return None
        return observation

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
        fresh = self._fresh_authority()
        slot = next(
            (item for item in fresh.slots if item.role == pin.role), None
        ) if fresh is not None else None
        authority_ok = bool(
            fresh is not None
            and pin.role in self.approved_roles
            and slot is not None
            and self.owner._close_owed_slot_matches(slot, pin, fresh.discard_roles)
        )
        return PreservationObservation(
            dirty_diff=not authority_ok,
            running_process=not authority_ok,
            # This is the only override: the distinct structured approval authorizes
            # discarding this exact uncorrelated pending composer generation.
            pending_approval=not authority_ok,
            identity_matches=authority_ok,
            # The old slot may be unattested; exact owner approval + positive pair identity
            # is its close authority.  The replacement must be action-attested below.
            attestation_fresh=authority_ok,
            detail=(
                "approved_exact_uncorrelated_pending_composer"
                if authority_ok
                else "approval_bound_full_pair_or_composer_changed"
            ),
        )

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        if self._fresh_authority() is None:
            return CLOSE_ERROR
        return super().close_exact_generation(pin)

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        if self._fresh_authority() is None:
            return LAUNCH_ERROR
        return super().launch_action_bound(action_id, pin)

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        """Accept a bound attestation only under a final fresh full-pair fence."""
        verdict = super().verify_attestation(action_id, pin)
        if verdict != ATTEST_BOUND:
            return verdict
        # The inherited verifier proves the launched participant only.  Re-read the full
        # approval-bound pair as the last step before the generic actuator records
        # ``verify_owed -> replaced``.  An authority race remains retryable, never writable.
        return (
            ATTEST_BOUND
            if self._fresh_authority(require_attested_roles=(pin.role,)) is not None
            else ATTEST_PENDING
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
        rows: Sequence[Mapping[str, object]] | None = None,
        action_closed_roles: Sequence[str] = (),
    ) -> bool:
        """Positive raw facts for one uncorrelated, non-productive pending composer."""
        if rows is None:
            try:
                rows = tuple(list_herdr_agent_rows(self.env))
            except Exception:  # noqa: BLE001
                return False
        else:
            rows = tuple(rows)
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
            inspection = _SnapshotQuarantineOps(
                repo_root=Path(request.worktree).expanduser(),
                env=self.env,
                snapshot_rows=rows,
                action_closed_roles=tuple(action_closed_roles),
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

    @staticmethod
    def _close_owed_slot_matches(
        slot: BoundSlot,
        participant: ParticipantPin,
        discard_roles: Sequence[str],
    ) -> bool:
        return bool(
            participant.phase == PARTICIPANT_CLOSE_OWED
            and slot.role == participant.role
            and slot.provider == participant.provider
            and slot.assigned_name == participant.assigned_name
            and slot.locator == participant.old_locator
            and slot.disposition == SLOT_PRESERVE_PENDING
            and not slot.close_proven
            and participant.role in discard_roles
        )

    @classmethod
    def _progress_snapshot_matches(
        cls,
        request: PrepareBoundPairRequest,
        observation: PreparationObservation,
        expectation: PreparationExpectation,
        participants: Sequence[ParticipantPin],
        *,
        progress_proven_roles: Sequence[str] = (),
    ) -> bool:
        """Compare a retry to the approved pair while admitting proven participant progress."""
        if not cls._observation_matches(request, observation, expectation):
            return False
        pins = tuple(participants)
        by_role = {pin.role: pin for pin in pins}
        expected_roles = set(expectation.discard_roles)
        if (
            len(by_role) != len(pins)
            or set(by_role) != expected_roles
            or any(role not in expected_roles for role in observation.discard_roles)
            or any(
                pin.is_self
                or norm(pin.lane_id) != norm(request.lane)
                or pin.lane_revision != str(expectation.revision)
                or pin.lane_generation != str(expectation.generation)
                or pin.phase not in {
                    PARTICIPANT_CLOSE_OWED,
                    PARTICIPANT_LAUNCH_OWED,
                    PARTICIPANT_VERIFY_OWED,
                    PARTICIPANT_REPLACED,
                }
                for pin in pins
            )
        ):
            return False
        proven = set(progress_proven_roles)
        projected: list[BoundSlot] = []
        for slot in observation.slots:
            pin = by_role.get(slot.role)
            if pin is None:
                projected.append(slot)
                continue
            if pin.phase == PARTICIPANT_CLOSE_OWED:
                if not cls._close_owed_slot_matches(
                    slot, pin, observation.discard_roles
                ):
                    return False
            elif pin.role not in proven:
                return False
            projected.append(
                BoundSlot(
                    role=pin.role,
                    provider=pin.provider,
                    assigned_name=pin.assigned_name,
                    locator=pin.old_locator,
                    disposition=SLOT_PRESERVE_PENDING,
                )
            )
        return slot_digest(projected) == expectation.slot_digest

    @staticmethod
    def _action_bound_slot(
        request: PrepareBoundPairRequest,
        observation: PreparationObservation,
        expectation: PreparationExpectation,
        participant: ParticipantPin,
        *,
        require_attestation: bool = False,
    ) -> bool:
        slot = next(
            (item for item in observation.slots if item.role == participant.role), None
        )
        if slot is None:
            return False
        if (
            participant.phase == PARTICIPANT_LAUNCH_OWED
            and slot.close_proven
            and slot.provider == participant.provider
            and slot.assigned_name == participant.assigned_name
            and slot.locator == participant.old_locator
        ):
            return True
        if (
            slot.provider != participant.provider
            or slot.assigned_name != participant.assigned_name
            or not slot.locator
            or slot.locator == participant.old_locator
        ):
            return False
        if (
            participant.phase == PARTICIPANT_VERIFY_OWED
            and slot.disposition in (SLOT_RECOVER, SLOT_HEALTHY)
            and not require_attestation
        ):
            # The immutable phase proves launch completion.  The generic actuator owns the
            # action-bound attestation wait and cannot advance another participant until this
            # one reaches ``replaced``.
            return True
        try:
            record = HerdrIdentityAttestationStore().read(slot.assigned_name)
        except Exception:  # noqa: BLE001
            return False
        join = evaluate_attestation(
            record,
            live_locator=slot.locator,
            expected_workspace_id=observation.workspace_id,
            expected_role=slot.provider,
            expected_lane=request.lane,
        )
        direct_action = norm(getattr(record, "replacement_action_id", ""))
        return join.ok and (
            direct_action == norm(expectation.action_id)
            if direct_action
            else replacement_action_is_bound(
                record,
                action_id=norm(expectation.action_id),
                live_locator=slot.locator,
                expected_workspace_id=observation.workspace_id,
                expected_role=slot.provider, expected_lane=request.lane,
                expected_assigned_name=slot.assigned_name,
                expected_old_locator=participant.old_locator,
            )
        )

    def _progress_proven_roles(
        self,
        request: PrepareBoundPairRequest,
        observation: PreparationObservation,
        expectation: PreparationExpectation,
        participants: Sequence[ParticipantPin],
        *,
        require_attested_roles: Sequence[str] = (),
    ) -> tuple[str, ...]:
        exact_attestation = set(require_attested_roles)
        return tuple(
            pin.role
            for pin in participants
            if pin.phase != PARTICIPANT_CLOSE_OWED
            and self._action_bound_slot(
                request,
                observation,
                expectation,
                pin,
                require_attestation=pin.role in exact_attestation,
            )
        )

    def _observation_from_snapshot(
        self,
        request: PrepareBoundPairRequest,
        *,
        rows: Sequence[Mapping[str, object]],
        record,
        worktree: Path,
        workspace: str,
        identity: str,
        ok_branch: bool,
        branch: str,
        ok_status: bool,
        status: str,
        transaction,
    ) -> PreparationObservation:
        faults = self._bound_signature_faults(
            _convergence_request(request), record, identity
        )
        try:
            providers = (
                ("gateway", resolve_gateway_provider(str(self.repo_root))),
                ("worker", resolve_worker_provider(str(self.repo_root))),
            )
        except Exception as exc:  # noqa: BLE001
            return PreparationObservation(
                workspace_id=workspace,
                worktree_path=str(worktree),
                worktree_identity=identity,
                branch=branch,
                revision=record.revision,
                generation=record.lane_generation,
                lifecycle_exact=not faults,
                pins_empty=not bool(record.declared_slots),
                pins_known=True,
                bound_faults=faults,
                worktree_readable=ok_status,
                worktree_clean=ok_status and not status,
                branch_matches=ok_branch and branch == request.branch,
                detail=f"provider identity unreadable ({type(exc).__name__})",
            )
        live = _SnapshotRecoveryOps(
            repo_root=self.repo_root,
            request_issue=request.issue,
            request_lane=request.lane,
            request_journal=request.journal,
            env=self.env,
        )
        live.snapshot_rows = tuple(rows)
        live.target_workspace_id = workspace
        slots: list[BoundSlot] = []
        for role, provider in providers:
            slot_observation, locator, assigned = live.observe_slot(
                role=role,
                provider=provider,
                workspace_id=workspace,
                lane=request.lane,
                record=record,
            )
            disposition = decide_slot_recovery(slot_observation)
            close_proven = False
            if not locator and transaction is not None:
                participant = transaction.find_participant(
                    (request.lane, role, provider, assigned)
                )
                if participant is not None and participant.phase != PARTICIPANT_CLOSE_OWED:
                    locator = participant.old_locator
                    disposition = SLOT_RECOVER
                    close_proven = True
            slots.append(
                BoundSlot(
                    role=role,
                    provider=provider,
                    assigned_name=assigned,
                    locator=locator,
                    disposition=disposition,
                    close_proven=close_proven,
                )
            )
        # Discardability is decided only after the whole pair is known: a sibling this action
        # already closed must not read as a broken pair for the role still owed (j#80934).
        action_closed = _action_closed_providers(slots)
        discard = [
            slot.role
            for slot in slots
            if slot.disposition == SLOT_PRESERVE_PENDING
            and self._composer_discardable(
                request,
                role=slot.role,
                provider=slot.provider,
                assigned_name=slot.assigned_name,
                locator=slot.locator,
                rows=rows,
                action_closed_roles=action_closed,
            )
        ]
        return PreparationObservation(
            workspace_id=workspace,
            worktree_path=str(worktree),
            worktree_identity=identity,
            branch=branch,
            revision=record.revision,
            generation=record.lane_generation,
            lifecycle_exact=not faults,
            pins_empty=not bool(record.declared_slots),
            pins_known=True,
            bound_faults=faults,
            inventory_readable=True,
            worktree_readable=ok_status,
            worktree_clean=ok_status and not status,
            branch_matches=ok_branch and branch == request.branch,
            slots=tuple(slots),
            discard_roles=tuple(sorted(discard)),
        )

    def observe(
        self, request: PrepareBoundPairRequest, *, action_id: str = ""
    ) -> PreparationObservation:
        base = super().observe(_convergence_request(request), action_id=action_id)
        # With an action id the base projects the slots this action already closed; that proof
        # is what lets the role still owed keep its discard authority on a replay (j#80934).
        action_closed = _action_closed_providers(base.slots)
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
                action_closed_roles=action_closed,
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
            pins_known=base.pins_known,
            bound_faults=base.bound_faults,
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

    def _finish(
        self,
        key: ReplacementTransactionKey,
        expectation: PreparationExpectation,
        holder: str,
        authority: _ComposerDiscardActuatorPort,
    ) -> bool:
        record = self.transaction_store.get(key)
        if record is None:
            return False
        if record.phase == PHASE_COMPLETED:
            # No write remains.  The immutable completed row is the idempotent replay proof;
            # fresh external authority is required only at the still-owed transition edges.
            return True
        if record.phase == PHASE_REPLACING_NONSELF:
            if authority._fresh_authority() is None:
                return False
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
        # The first transition and this final completion CAS are distinct durable writes.
        # Re-read full pair/lifecycle/worktree authority at each edge so a race between them
        # leaves the transaction retryably draining rather than falsely completed.
        if authority._fresh_authority() is None:
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
        if existing is not None:
            progress = self._progress_proven_roles(
                request, current, expectation, existing.participants
            )
            if not self._progress_snapshot_matches(
                request,
                current,
                expectation,
                existing.participants,
                progress_proven_roles=progress,
            ):
                return PreparationDrive(
                    False,
                    "transaction_conflict",
                    "approval-bound full pair or immutable progress changed",
                )
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
        current_action_closed = _action_closed_providers(current.slots)
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
                    action_closed_roles=current_action_closed,
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
                _launch_detail(result, port),
                startup=getattr(port, "launch_startup_health", None),  # #13948 R3
            )
        if not self._finish(key, expectation, holder, port):
            return PreparationDrive(False, "completion_stopped", "transaction completion CAS stopped")
        return PreparationDrive(True, ACTUATION_RECOVERED)


__all__ = ("LiveBoundPairPreparationOps",)
