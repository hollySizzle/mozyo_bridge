"""Live adapters for ``sublane converge-bound-pair`` (Redmine #13933).

All process mutation stays behind reviewed high-level adapters: the existing exact-generation
replacement transaction/actuator, the quarantine close boundary, and the Herdr lane healer.
There is no raw Herdr, tmux, SQLite, resume, dispatch or worktree mutation here.
"""

from __future__ import annotations

import os
import subprocess
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
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DISPOSITION_HIBERNATED,
    RELEASE_RELEASED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    norm,
    replacement_settled,
)
from mozyo_bridge.core.state.lane_pin_repair import LanePinRepairStore
from mozyo_bridge.core.state.lane_pin_role import PIN_ROLE_GATEWAY, PIN_ROLE_WORKER
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
    ReplacementTransactionStore,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (
    ExactGenerationActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    BoundPairObservation,
    ConvergeBoundPairRequest,
    PinRepairResult,
    ReplacementDrive,
    transaction_plan_observation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (
    LiveHibernatedPairRecoveryOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    SublaneLauncherIncompatibleError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (
    SublaneHealError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
    is_git_worktree_root,
    list_herdr_agent_rows,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    APPROVAL_GATE,
    FAULT_IDENTITY_ABSENT,
    FAULT_IDENTITY_MISMATCH,
    FAULT_ISSUE_MISMATCH,
    FAULT_NOT_HIBERNATED,
    FAULT_NOT_ISSUE_BOUND,
    FAULT_NOT_RELEASED,
    FAULT_PINS_NOT_EMPTY,
    FAULT_PROJECT_SCOPED,
    FAULT_REPLACEMENT_UNSETTLED,
    ApprovalExpectation,
    BoundSlot,
    decide_transaction_plan,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_HEALTHY,
    SLOT_RECOVER,
    decide_slot_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (
    ACTUATION_EFFECT_FAILED,
    ACTUATION_RECOVERED,
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    ATTEST_PENDING,
    CLOSE_DONE,
    CLOSE_ERROR,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_ABSENT,
    OLD_SLOT_AMBIGUOUS,
    OLD_SLOT_PRESENT,
    OLD_SLOT_RECYCLED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    decode_assigned_name,
    lane_runtime_identity,
)


def _action_bound_after_identity_join(
    record,
    *,
    action_id: str,
    live_locator: str,
    workspace_id: str,
    role: str,
    lane: str,
    assigned_name: str,
    old_locator: str,
) -> bool:
    """Accept native-v2 directly, or the exact v1 side binding after identity join."""
    direct_action = norm(getattr(record, "replacement_action_id", ""))
    if direct_action:
        return direct_action == norm(action_id)
    return replacement_action_is_bound(
        record,
        action_id=norm(action_id),
        live_locator=live_locator,
        expected_workspace_id=workspace_id,
        expected_role=role,
        expected_lane=lane,
        expected_assigned_name=assigned_name,
        expected_old_locator=old_locator,
    )


def _launch_detail(result, port) -> str:
    """Surface a fenced launch's typed reason in the public drive detail (Redmine #13933 R11).

    The generic actuator records a hardcoded ``detail="launch"`` for any ``effect_failed`` at
    the launch leg (:meth:`ReplacementActuatorUseCase._step_launch_owed`).  The port stashed
    the stable, path/credential-free reason token the heal fence raised, so the public outcome
    carries ``launch:<reason>`` (j#81429 #2) instead of a bare swallowed exception.  Every
    other status keeps its own detail unchanged.
    """
    detail = result.detail or ",".join(result.preservation_reasons)
    reason = norm(getattr(port, "launch_failure_reason", ""))
    if result.status == ACTUATION_EFFECT_FAILED and norm(result.detail) == "launch" and reason:
        return f"launch:{reason}"
    return detail


def _git(worktree: Path, *args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ("git", "-C", str(worktree), *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return result.returncode == 0, result.stdout.strip()


class _SnapshotRecoveryOps(LiveHibernatedPairRecoveryOps):
    """Reuse the reviewed slot classifier against one cardinality-stable inventory snapshot."""

    snapshot_rows: Sequence[Mapping[str, object]] = ()
    target_workspace_id: str = ""

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return self.snapshot_rows

    def workspace_id(self) -> str:
        # ``repo_root`` is the coordinator checkout, while a bound lane can live in a
        # different issue worktree.  Attestation must join to the request-derived workspace.
        return self.target_workspace_id


@dataclass
class _BoundPairActuatorPort(ExactGenerationActuatorPort):
    owner: "LiveBoundPairConvergenceOps"
    request: ConvergeBoundPairRequest
    expectation: ApprovalExpectation
    live: _SnapshotRecoveryOps

    def _rows(self):
        try:
            return True, tuple(list_herdr_agent_rows(self.owner.env))
        except Exception:  # noqa: BLE001
            return False, ()

    def _matches(self, pin: ParticipantPin):
        readable, rows = self._rows()
        matches = [
            row for row in rows
            if norm(row.get(AGENT_KEY_NAME)) == norm(pin.assigned_name)
        ]
        exact = [row for row in matches if _agent_locator(row) == norm(pin.old_locator)]
        return readable, matches, exact

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        readable, matches, exact = self._matches(pin)
        if not readable:
            return OLD_SLOT_AMBIGUOUS
        if len(exact) == 1 and len(matches) == 1:
            return OLD_SLOT_PRESENT
        if exact or len(matches) > 1:
            return OLD_SLOT_AMBIGUOUS
        return OLD_SLOT_RECYCLED if matches else OLD_SLOT_ABSENT

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        # Re-run the positive-fact slot classifier at the close boundary; only a still-
        # recoverable exact generation clears the identity/running/pending fence.
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
        worktree_ok, status = _git(worktree, "status", "--porcelain=v1")
        observation, locator, assigned = self.live.observe_slot(
            role=pin.role,
            provider=pin.provider,
            workspace_id=self.live.workspace_id(),
            lane=self.request.lane,
            record=record,
        )
        disposition = decide_slot_recovery(observation)
        safe_identity = bool(
            disposition == SLOT_RECOVER
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
        branch_safe = bool(ok_branch and norm(branch) == norm(self.request.branch))
        return PreservationObservation(
            dirty_diff=not worktree_ok or bool(status) or not branch_safe,
            running_process=not observation.not_productive,
            pending_approval=not observation.no_pending_composer,
            identity_matches=safe_identity,
            # The OLD slot is intentionally stale/unattested.  The exact owner approval plus
            # the positive bad-generation classifier is its close authority; action-bound
            # attestation is required of the NEW slot in verify_attestation below.
            attestation_fresh=True,
            detail=(
                disposition
                if safe_identity and branch_safe and worktree_ok and not status
                else f"{disposition}; lifecycle_or_branch_authority_changed"
            ),
        )

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        ok = self.live.close_bad_slot(
            role=pin.role,
            provider=pin.provider,
            assigned_name=pin.assigned_name,
            locator=pin.old_locator,
            action_id=self.expectation.action_id,
        )
        return CLOSE_DONE if ok else CLOSE_ERROR

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        # ``heal_lane_column`` is the high-level idempotent launcher.  It is driven once per
        # participant; ``target_provider`` scopes its same-tab postcondition to THIS owed slot
        # so an approved partial pair (a still-owed / legitimately absent sibling) converges
        # without tripping the full-pair postcondition, while a LIVE split still fails closed
        # (Redmine #13933 R11 j#81429 #3).  A fenced launch records its stable, typed reason
        # (no path / credential) so the public outcome surfaces WHY (j#81429 #2), instead of a
        # bare swallowed exception under ``effect_failed / launch``.
        try:
            HerdrSublaneActuatorOps(
                repo_root=self.owner.repo_root,
                lane_label=norm(self.request.lane),
                issue=norm(self.request.issue),
                journal=norm(self.request.journal),
                env=self.owner.env,
                replacement_action_id=norm(action_id),
                replacement_assigned_name=norm(pin.assigned_name),
                replacement_old_locator=norm(pin.old_locator),
            ).heal_lane_column(self.request.worktree, target_provider=norm(pin.provider))
        except SublaneHealError as exc:
            self.launch_failure_reason = norm(exc.reason) or "launch_error"
            # A nested unhealthy replacement launch carries the locator-free startup
            # observation of the SAME startup action; stash it so the public outcome can
            # surface its typed health + explicit rollback pointer (Redmine #13948 R3).
            self.launch_startup_health = exc.startup
            return LAUNCH_ERROR
        except SublaneLauncherIncompatibleError as exc:
            self.launch_failure_reason = norm(exc.reason) or "launcher_incompatible"
            self.launch_startup_health = None
            return LAUNCH_ERROR
        except Exception:  # noqa: BLE001 - any other relaunch failure is a fixed launch error
            self.launch_failure_reason = "launch_error"
            self.launch_startup_health = None
            return LAUNCH_ERROR
        self.launch_failure_reason = ""
        self.launch_startup_health = None
        return LAUNCH_DONE

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        readable, rows = self._rows()
        if not readable:
            return ATTEST_PENDING
        matches = [
            row for row in rows
            if norm(row.get(AGENT_KEY_NAME)) == norm(pin.assigned_name)
        ]
        if len(matches) != 1:
            return ATTEST_PENDING
        locator = _agent_locator(matches[0])
        if not locator or locator == norm(pin.old_locator):
            return ATTEST_PENDING
        try:
            record = HerdrIdentityAttestationStore().read(norm(pin.assigned_name))
        except Exception:  # noqa: BLE001
            return ATTEST_PENDING
        join = evaluate_attestation(
            record,
            live_locator=locator,
            expected_workspace_id=self.live.workspace_id(),
            expected_role=pin.provider,
            expected_lane=self.request.lane,
        )
        if not join.ok:
            return ATTEST_PENDING
        return (
            ATTEST_BOUND
            if _action_bound_after_identity_join(
                record,
                action_id=norm(action_id),
                live_locator=locator,
                workspace_id=self.live.workspace_id(),
                role=pin.provider,
                lane=self.request.lane,
                assigned_name=pin.assigned_name,
                old_locator=pin.old_locator,
            )
            else ATTEST_MISMATCH
        )


@dataclass
class LiveBoundPairConvergenceOps:
    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    lifecycle_store: LaneLifecycleStore = field(default_factory=LaneLifecycleStore)
    transaction_store: ReplacementTransactionStore = field(default_factory=ReplacementTransactionStore)
    pin_store: LanePinRepairStore = field(default_factory=LanePinRepairStore)

    def _worktree(self, request: ConvergeBoundPairRequest) -> tuple[Path | None, str, str]:
        """Resolve the target lane root and the identity that root derives.

        The identity is a fact about ``request.worktree`` alone.  It deliberately does not
        consult ``self.repo_root``: that is only the caller's ``--repo`` / cwd here, and
        keying the token family off ``resolved == repo_root`` made the same lane resolve
        ``wt_`` from one directory and ``dl_`` from another, so its own row stopped matching
        itself when the command ran from the lane worktree (#13846 j#81024 / #13933 j#81043).
        """
        try:
            resolved = Path(request.worktree).expanduser().resolve(strict=True)
            workspace = herdr_workspace_segment(resolved)
            identity = lane_runtime_identity(
                str(resolved), request.lane, git_worktree=is_git_worktree_root(resolved)
            )
        except (OSError, ValueError):
            return None, "", ""
        return resolved, workspace, identity

    def _lifecycle(self, request: ConvergeBoundPairRequest):
        _worktree, workspace, _identity = self._worktree(request)
        if not workspace:
            return None
        try:
            return self.lifecycle_store.get(LaneLifecycleKey(workspace, request.lane))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _bound_signature_faults(
        request: ConvergeBoundPairRequest, record, identity: str
    ) -> tuple[str, ...]:
        """Name every bound-signature axis this row fails (Redmine #13933 j#81046 Decision 2).

        The conjunction below is the same one :meth:`_lifecycle_exact` asserts; evaluating it
        axis-by-axis is what lets a blocked rail say WHICH premise broke instead of only that
        one did.  Axis names only -- no observed row value is returned, so a caller may put
        the whole tuple in a public payload.  ``pins`` is not here: each rail states its own
        pin requirement (this one wants none, the retire rails want exact), so the pin axis is
        folded in by the caller.
        """
        faults: list[str] = []
        if record.lane_disposition != DISPOSITION_HIBERNATED:
            faults.append(FAULT_NOT_HIBERNATED)
        if norm(record.binding_kind) != BINDING_KIND_ISSUE:
            faults.append(FAULT_NOT_ISSUE_BOUND)
        if norm(record.issue_id) != norm(request.issue):
            faults.append(FAULT_ISSUE_MISMATCH)
        if record.project_scope:
            faults.append(FAULT_PROJECT_SCOPED)
        if not norm(record.worktree_identity):
            faults.append(FAULT_IDENTITY_ABSENT)
        elif norm(record.worktree_identity) != norm(identity):
            faults.append(FAULT_IDENTITY_MISMATCH)
        if record.process_release != RELEASE_RELEASED:
            faults.append(FAULT_NOT_RELEASED)
        if not replacement_settled(record.replacement_state):
            faults.append(FAULT_REPLACEMENT_UNSETTLED)
        return tuple(faults)

    @classmethod
    def _lifecycle_exact(cls, request: ConvergeBoundPairRequest, record, identity: str) -> bool:
        # Delegated, not restated: a second copy of the conjunction could drift from the
        # typed axes above and let a rail report a fault set that contradicts its own verdict.
        return not cls._bound_signature_faults(request, record, identity)

    def _transaction(self, workspace: str, action_id: str):
        if not action_id:
            return None
        try:
            return self.transaction_store.get(ReplacementTransactionKey(workspace, action_id))
        except Exception:  # noqa: BLE001
            return None

    def observe(self, request: ConvergeBoundPairRequest, *, action_id: str = "") -> BoundPairObservation:
        worktree, workspace, identity = self._worktree(request)
        if worktree is None or not workspace or not identity:
            return BoundPairObservation(detail="worktree/workspace identity unresolved")
        ok_branch, branch = _git(worktree, "branch", "--show-current")
        ok_status, status = _git(worktree, "status", "--porcelain=v1")
        try:
            record = self.lifecycle_store.get(LaneLifecycleKey(workspace, request.lane))
        except Exception as exc:  # noqa: BLE001
            return BoundPairObservation(
                workspace_id=workspace,
                worktree_path=str(worktree),
                worktree_identity=identity,
                branch=branch,
                detail=f"lifecycle unreadable ({type(exc).__name__})",
            )
        if record is None:
            return BoundPairObservation(
                workspace_id=workspace, worktree_path=str(worktree), worktree_identity=identity,
                branch=branch, worktree_readable=ok_status, worktree_clean=ok_status and not status,
                branch_matches=ok_branch and branch == request.branch, detail="lifecycle absent",
            )
        faults = self._bound_signature_faults(request, record, identity)
        exact = not faults
        try:
            rows = tuple(list_herdr_agent_rows(self.env))
            inventory_readable = True
        except Exception as exc:  # noqa: BLE001
            return BoundPairObservation(
                workspace_id=workspace, worktree_path=str(worktree), worktree_identity=identity,
                branch=branch, revision=record.revision, generation=record.lane_generation,
                lifecycle_exact=exact, pins_empty=not bool(record.declared_slots),
                pins_known=True, bound_faults=faults,
                worktree_readable=ok_status, worktree_clean=ok_status and not status,
                branch_matches=ok_branch and branch == request.branch,
                detail=f"inventory unreadable ({type(exc).__name__})",
            )
        try:
            gateway_provider = resolve_gateway_provider(str(self.repo_root))
            worker_provider = resolve_worker_provider(str(self.repo_root))
        except Exception as exc:  # noqa: BLE001 - unresolved provider identity is zero-effect
            return BoundPairObservation(
                workspace_id=workspace, worktree_path=str(worktree), worktree_identity=identity,
                branch=branch, revision=record.revision, generation=record.lane_generation,
                lifecycle_exact=exact, pins_empty=not bool(record.declared_slots),
                pins_known=True, bound_faults=faults,
                inventory_readable=False,
                worktree_readable=ok_status, worktree_clean=ok_status and not status,
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
        live.snapshot_rows = rows
        live.target_workspace_id = workspace
        transaction = self._transaction(workspace, action_id)
        slots: list[BoundSlot] = []
        for role, provider in ((PIN_ROLE_GATEWAY, gateway_provider), (PIN_ROLE_WORKER, worker_provider)):
            observation, locator, assigned = live.observe_slot(
                role=role, provider=provider, workspace_id=workspace,
                lane=request.lane, record=record,
            )
            disposition = decide_slot_recovery(observation)
            proof = False
            if not locator and transaction is not None:
                participant = transaction.find_participant((request.lane, role, provider, assigned))
                if participant is not None and participant.phase != PARTICIPANT_CLOSE_OWED:
                    locator = participant.old_locator
                    disposition = SLOT_RECOVER
                    proof = True
            slots.append(
                BoundSlot(
                    role=role,
                    provider=provider,
                    assigned_name=assigned,
                    locator=locator,
                    disposition=disposition,
                    close_proven=proof,
                )
            )
        pins_exact = False
        if record.declared_slots and all(slot.disposition == SLOT_HEALTHY for slot in slots):
            wanted = {(slot.role, slot.provider, slot.assigned_name, slot.locator) for slot in slots}
            got = {(pin.role, pin.provider, pin.assigned_name, pin.locator) for pin in record.declared_slots}
            pins_exact = wanted == got
        return BoundPairObservation(
            workspace_id=workspace,
            worktree_path=str(worktree),
            worktree_identity=identity,
            branch=branch,
            revision=record.revision,
            generation=record.lane_generation,
            lifecycle_exact=exact,
            pins_empty=not bool(record.declared_slots),
            pins_exact=pins_exact,
            pins_known=True,
            bound_faults=faults,
            inventory_readable=inventory_readable,
            worktree_readable=ok_status,
            worktree_clean=ok_status and not status,
            branch_matches=ok_branch and branch == request.branch,
            slots=tuple(slots),
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

    def drive_replacement(
        self,
        request: ConvergeBoundPairRequest,
        expectation: ApprovalExpectation,
        initial_observation: BoundPairObservation,
    ) -> ReplacementDrive:
        try:
            key = ReplacementTransactionKey(
                initial_observation.workspace_id, expectation.action_id
            )
            existing = self.transaction_store.get(key)
        except Exception as exc:  # noqa: BLE001
            return ReplacementDrive(False, "transaction_conflict", type(exc).__name__)

        # F2: this is the final read-only boundary before any transaction plan write.  Feed the
        # complete observation back through the pure decision and require stability with the
        # caller's approval-bound snapshot.  An existing immutable transaction may have a
        # progressed pair digest; it is resumed without calling plan_transaction again.
        observation = self.observe(request, action_id=expectation.action_id)
        admission = decide_transaction_plan(
            expectation,
            transaction_plan_observation(request, initial_observation),
            transaction_plan_observation(request, observation),
            transaction_exists=existing is not None,
        )
        if not admission.allowed:
            return ReplacementDrive(False, "transaction_conflict", admission.reason)

        recover = [slot for slot in observation.slots if slot.disposition == SLOT_RECOVER]
        decision = DecisionPointer(source="redmine", issue_id=request.issue, journal_id=request.journal)
        continuation = ContinuationPointer(
            source="redmine", issue_id=request.issue, journal_id=request.journal,
            expected_gate=APPROVAL_GATE, next_semantic_action="repair_pins",
        )
        try:
            planned_participants: tuple[ParticipantPin, ...] | None = None
            if existing is None:
                if not recover:
                    return ReplacementDrive(
                        False, "transaction_conflict", "no bad generation and no transaction proof"
                    )
                participants = [
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
                    for slot in recover
                ]
                planned_participants = tuple(participants)
                plan = self.transaction_store.plan_transaction(
                    key,
                    action_generation=expectation.action_generation,
                    decision=decision,
                    continuation=continuation,
                    participants=participants,
                )
                current = self.transaction_store.get(key)
                if current is None or (not plan.applied and plan.reason != CAS_ALREADY_DECLARED):
                    return ReplacementDrive(False, "transaction_conflict", plan.reason)
            else:
                current = existing
        except Exception as exc:  # noqa: BLE001
            return ReplacementDrive(False, "transaction_conflict", type(exc).__name__)
        if (
            current.action_generation != expectation.action_generation
            or current.decision != decision
            or current.continuation != continuation
            or not current.participants
            or any(
                participant.is_self
                or norm(participant.lane_id) != norm(request.lane)
                or participant.lane_revision != str(expectation.revision)
                or participant.lane_generation != str(expectation.generation)
                for participant in current.participants
            )
            or (
                planned_participants is not None
                and {
                    (participant.identity, participant.old_locator)
                    for participant in current.participants
                }
                != {
                    (participant.identity, participant.old_locator)
                    for participant in planned_participants
                }
            )
        ):
            return ReplacementDrive(False, "transaction_conflict", "immutable header mismatch")
        try:
            rows = tuple(list_herdr_agent_rows(self.env))
        except Exception as exc:  # noqa: BLE001
            return ReplacementDrive(False, "inventory_unreadable", type(exc).__name__)
        live = _SnapshotRecoveryOps(
            repo_root=self.repo_root,
            request_issue=request.issue,
            request_lane=request.lane,
            request_journal=request.journal,
            env=self.env,
        )
        live.snapshot_rows = rows
        live.target_workspace_id = observation.workspace_id
        port = _BoundPairActuatorPort(self, request, expectation, live)
        holder = f"converge:{expectation.action_id}:g{expectation.action_generation}"
        result = ReplacementActuatorUseCase(
            self.transaction_store,
            port,
            preservation_policy=assess_preservation,
        ).drive_worker_recovery(
            key,
            holder=holder,
            expected_action_generation=expectation.action_generation,
        )
        return ReplacementDrive(
            result.status == ACTUATION_RECOVERED,
            result.status,
            _launch_detail(result, port),
        )

    def final_pins(
        self, request: ConvergeBoundPairRequest, *, action_id: str
    ) -> tuple[BoundPairObservation, tuple[ProcessGenerationPin, ...]]:
        observation = self.observe(request, action_id=action_id)
        transaction = self._transaction(observation.workspace_id, action_id)
        pins: list[ProcessGenerationPin] = []
        for slot in observation.slots:
            if slot.disposition != SLOT_HEALTHY or not slot.locator:
                return observation, ()
            try:
                attestation = HerdrIdentityAttestationStore().read(slot.assigned_name)
            except Exception:  # noqa: BLE001
                return observation, ()
            join = evaluate_attestation(
                attestation,
                live_locator=slot.locator,
                expected_workspace_id=observation.workspace_id,
                expected_role=slot.provider,
                expected_lane=request.lane,
            )
            if not join.ok:
                return observation, ()
            participant = next(
                (
                    item
                    for item in (transaction.participants if transaction else ())
                    if item.assigned_name == slot.assigned_name
                ),
                None,
            )
            if participant is not None and not _action_bound_after_identity_join(
                attestation,
                action_id=norm(action_id),
                live_locator=slot.locator,
                workspace_id=observation.workspace_id,
                role=slot.provider,
                lane=request.lane,
                assigned_name=slot.assigned_name,
                old_locator=participant.old_locator,
            ):
                return observation, ()
            pins.append(
                ProcessGenerationPin(
                    role=slot.role,
                    provider=slot.provider,
                    assigned_name=slot.assigned_name,
                    locator=slot.locator,
                    attested_at=norm(attestation.observed_at),
                )
            )
        return observation, tuple(pins)

    def repair_pins(
        self,
        request: ConvergeBoundPairRequest,
        expectation: ApprovalExpectation,
        observation: BoundPairObservation,
        pins: Sequence[ProcessGenerationPin],
    ) -> PinRepairResult:
        try:
            result = self.pin_store.repair_hibernated_bound_pins(
                LaneLifecycleKey(observation.workspace_id, request.lane),
                expected_revision=expectation.revision,
                expected_generation=expectation.generation,
                issue_id=request.issue,
                worktree_identity=observation.worktree_identity,
                declared_slots=pins,
                decision=DecisionPointer(
                    source="redmine", issue_id=request.issue, journal_id=request.journal,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return PinRepairResult(False, type(exc).__name__)
        return PinRepairResult(result.applied, result.reason, repaired=result.applied)

    def finish_replacement(self, expectation: ApprovalExpectation) -> bool:
        # CAS pin repair is the effect this transaction exists to guard.  Only after it applies
        # do we complete the no-send continuation; replay never closes/launches again.
        try:
            matches = [
                record for record in self.transaction_store.records()
                if record.action_id == expectation.action_id
            ]
            if len(matches) != 1:
                return False
            record = matches[0]
            key = ReplacementTransactionKey(record.workspace_id, expectation.action_id)
        except Exception:  # noqa: BLE001
            return False
        if record is None:
            return False
        if record.phase == PHASE_COMPLETED:
            return True
        holder = f"converge:{expectation.action_id}:g{expectation.action_generation}"
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


__all__ = ("LiveBoundPairConvergenceOps",)
