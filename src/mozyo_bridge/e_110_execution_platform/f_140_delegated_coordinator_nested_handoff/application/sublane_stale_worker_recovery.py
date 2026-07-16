"""Public stale standard-sublane worker recovery surface (Redmine #13806 tranche D).

The coordinator-facing entry the residual j#79435 found missing: ``herdr session-start`` only
*reports* a ``stale_named_slot`` read-only, and ``sublane quarantine`` handles only a pending
composer — neither recovers a standard-sublane worker whose process vanished after a turn,
leaving an Implementation Done / Review Request diff un-durable-ized. This use case is that
recovery, connected to the existing tranche A/B/C primitives (Implementation Request j#79485).

The default is a **read-only preflight** (:func:`...domain.stale_worker_recovery.decide_recovery`
over a live observation). ``--execute`` actuates ONLY with a positive owner approval (a durable
Redmine :class:`DecisionPointer` + the exact ``recover:<…>`` action id + the immutable approved
generation) AND an action-time re-verification that the target is still the exact stale worker.
Any productive-provider / tool-child, unknown, wrong-issue-lane, stale-generation, gateway /
foreign, or unreadable-worktree observation is a zero-close typed blocker.

The actuation is **atomic + resumable**: it plans (or resumes) a *non-self* replacement
transaction whose sole participant is the stale worker, drives it through the tranche B
actuator's :meth:`...ReplacementActuatorUseCase.drive_worker_recovery` (guarded close → same-slot
fresh launch → action-bound attestation, byte-preserving the worktree — never reset / stash /
recreate / delete), and only after the fresh receiver is attested redispatches the ORIGINAL
durable gate exactly once (``replacing_nonself -> draining_continuation -> completed``, reusing
the tranche C drain's "record attempted before the send, never blind-resend" discipline). It
never closes the lane gateway or any foreign slot, never touches the current coordinator, and
never promotes an ACK / queue-enter to task completion.

The live observation / actuation adapter is deliberately NOT shipped here (the tranche A/B/C
precedent — live process mutation is non-scope, j#79121 / j#79485 boundary): the ports are
injected and faked in tests, and the CLI wires a fail-closed staged seam until a follow-up
lands the live wiring.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

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
    CAS_GENERATION_MISMATCH,
    CAS_LEASE_NOT_HELD,
    ContinuationPointerError,
    DecisionPointerError,
    ParticipantPinError,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
    norm,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    DEFAULT_LEASE_TTL_SECONDS,
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (  # noqa: E501
    ExactGenerationActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_RECOVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.session_replacement_reconcile import (  # noqa: E501
    drain_state_for,
    may_attempt_drain,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    RECOVER_ACTIONABLE,
    RecoveryObservation,
    decide_recovery,
    stale_worker_recovery_action_id,
)

# -- recovery / redispatch status vocabulary (closed) ---------------------------

#: The ONLY gate kind a worker recovery may redispatch (Redmine #13806 R3-F1). The governed
#: same-lane worker-forward rail (``handoff send --kind implementation_request``) delivers an
#: implementation_request to the worker, so the continuation pointer's ``expected_gate`` must be
#: exactly this — a pointer naming any other gate is a zero-send typed blocker (the send kind,
#: the redispatch marker kind, and the pointer's gate kind are thereby all one closed token).
RECOVERY_REDISPATCH_GATE = "implementation_request"

#: Preflight only — no ``--execute`` was requested (read-only classification).
RECOVERY_PREFLIGHT = "preflight"
#: ``--execute`` refused before any actuation because the target is not actionable (a typed
#: preflight blocker) or the owner approval was incomplete — zero close.
RECOVERY_REFUSED = "refused"
#: The guarded actuation ran and every leg completed: the worker is replaced AND the original
#: gate was redispatched exactly once (the transaction is ``completed``).
RECOVERY_COMPLETED = "completed"
#: The actuation ran but a leg stopped fail-closed (lease lost / effect failed / attestation
#: pending / preservation / etc.); the durable transaction holds the replay fence — a re-run
#: resumes. The underlying actuation / redispatch status is carried in ``detail``.
RECOVERY_STOPPED = "stopped"

#: Redispatch leg status (a closed vocabulary), riding the tranche C drain discipline.
REDISPATCH_CONFIRMED = "confirmed"
REDISPATCH_UNCERTAIN = "uncertain"
REDISPATCH_SEND_FAILED = "send_failed"
REDISPATCH_LEASE_LOST = "lease_lost"
REDISPATCH_GENERATION_MISMATCH = "generation_mismatch"
REDISPATCH_NOT_FOUND = "not_found"
REDISPATCH_CONTINUATION_UNREADABLE = "continuation_unreadable"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RecoveryRequest:
    """One approved stale-worker recovery request (the exact target + the owner approval)."""

    issue: str
    lane: str
    role: str
    provider: str
    assigned_name: str
    locator: str
    #: The Redmine journal id of the positive owner approval (``--execute`` only).
    journal: str = ""
    #: The exact ``recover:<lane>:<role>:<provider>:<assigned_name>:<locator>`` action id the
    #: approval names — re-derived and matched, never trusted verbatim.
    action_id: str = ""
    #: The immutable approved generation counter (>= 1). The transaction's authority token.
    action_generation: int = 0
    #: The lane lifecycle ``(revision, generation)`` pinned at approval time (evidence).
    lane_revision: str = ""
    lane_generation: str = ""
    #: The durable gate the coordinator must find + the one semantic action to redispatch once.
    expected_gate: str = ""
    next_semantic_action: str = ""

    @property
    def holder(self) -> str:
        """The stable, action-bound lease identity for this recovery (resume-safe)."""
        return f"recover:{norm(self.action_id)}:g{int(self.action_generation)}"


@dataclass(frozen=True)
class RecoveryOutcome:
    """The typed outcome the coordinator renders / gates on."""

    issue: str
    lane: str
    role: str
    verdict: str
    status: str
    executed: bool = False
    recovery_status: str = ""
    redispatch_status: str = ""
    closed_old_worker: bool = False
    fresh_slot_attested: bool = False
    phase: str = ""
    revision: int = 0
    detail: str = ""
    observation: Optional[dict[str, bool]] = None

    @property
    def is_blocked(self) -> bool:
        # A read-only preflight is never "blocked" — it is a report. An --execute is blocked
        # unless every leg completed (worker replaced AND gate redispatched exactly once).
        if not self.executed:
            return False
        return self.status != RECOVERY_COMPLETED

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "role": self.role,
            "verdict": self.verdict,
            "status": self.status,
            "executed": self.executed,
            "recovery_status": self.recovery_status or None,
            "redispatch_status": self.redispatch_status or None,
            "closed_old_worker": self.closed_old_worker,
            "fresh_slot_attested": self.fresh_slot_attested,
            "phase": self.phase or None,
            "revision": self.revision,
            "is_blocked": self.is_blocked,
            "detail": self.detail,
            "observation": self.observation,
        }


@runtime_checkable
class StaleWorkerRecoveryOps(Protocol):
    """The injected observe + redispatch effects (faked in tests; live wiring is a follow-up)."""

    def observe_target(self, request: RecoveryRequest) -> RecoveryObservation:
        """Observe the live pinned worker — the positive facts :func:`decide_recovery` reads.

        Read-only: it resolves the exact ``(workspace, lane, issue, provider, assigned_name,
        locator)`` slot against the live inventory + attestation and returns a
        :class:`RecoveryObservation` whose every field defaults to the unsafe side, so an
        unreadable / ambiguous inventory classifies as ``identity_unknown`` (never launched
        blind).
        """
        ...

    def redispatch_gate(self, continuation: ContinuationPointer) -> str:
        """Redispatch the ORIGINAL durable gate to the fresh worker (high-level, once).

        Re-reads the source journal (``continuation``) to reconstruct the Implementation
        Request / gate and sends it ONCE under the same-lane gateway ownership. Returns
        :data:`DRAIN_SEND_OK` / an error token. Landing is NOT implied by the send — the
        caller confirms it via :meth:`gate_redispatched`. Never promotes an ACK / queue-enter
        to task completion.
        """
        ...

    def gate_redispatched(self, continuation: ContinuationPointer) -> bool:
        """Has the original gate already landed on the fresh worker's durable inbox / gate?

        The idempotency check that lets a resume distinguish confirmed from still-needed
        without a blind resend.
        """
        ...


class StaleWorkerRecoveryUseCase:
    """Read-only preflight + owner-approved atomic recovery of a stale sublane worker."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        actuation_port: ExactGenerationActuatorPort,
        ops: StaleWorkerRecoveryOps,
        *,
        workspace_id: str,
        clock=_utc_now,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._actuation_port = actuation_port
        self._ops = ops
        self._workspace_id = norm(workspace_id)
        self._clock = clock
        self._ttl = lease_ttl_seconds

    def run(self, request: RecoveryRequest, *, execute: bool) -> RecoveryOutcome:
        observation = self._ops.observe_target(request)
        verdict = decide_recovery(observation)
        if not execute:
            return self._outcome(
                request, verdict, status=RECOVERY_PREFLIGHT, observation=observation,
                detail="preflight only; --execute requires a positive owner approval",
            )
        # --execute: the target must be exactly the stale worker the approval names.
        if verdict != RECOVER_ACTIONABLE:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail=f"target not actionable ({verdict}); zero close",
            )
        return self._execute(request, verdict, observation)

    # -- execute -------------------------------------------------------------

    def _execute(
        self, request: RecoveryRequest, verdict: str, observation: RecoveryObservation
    ) -> RecoveryOutcome:
        # 1. Positive durable owner approval + exact action id + generation, before any write.
        try:
            decision = DecisionPointer(
                source="redmine",
                issue_id=norm(request.issue),
                journal_id=norm(request.journal),
            )
        except DecisionPointerError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="approval journal is not a complete Redmine pointer",
            )
        try:
            expected_action = stale_worker_recovery_action_id(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, locator=request.locator,
            )
        except ValueError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="recovery inputs do not identify one exact worker",
            )
        if norm(request.action_id) != expected_action:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="action id does not match the exact approved worker",
            )
        if not isinstance(request.action_generation, int) or isinstance(
            request.action_generation, bool
        ) or request.action_generation < 1:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="approved generation is not a positive exact integer",
            )
        # A DESTRUCTIVE worker recovery requires the exact lane lifecycle (revision,
        # generation) evidence the approval pinned (Redmine #13806 R1-F2 / j#79485 §2): the
        # ParticipantPin treats these as optional for the default companion / coordinator, but a
        # standard-sublane worker recovery must carry them so the durable manifest holds — and
        # each destructive effect / replay re-verifies against — the exact lifecycle generation.
        # A missing one is a typed zero-close blocker, never actuated on a bare boolean.
        if not norm(request.lane_revision) or not norm(request.lane_generation):
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail=(
                    "lane lifecycle revision / generation evidence is required for a "
                    "destructive worker recovery; zero close"
                ),
            )
        try:
            continuation = ContinuationPointer(
                source="redmine", issue_id=norm(request.issue),
                journal_id=norm(request.journal),
                expected_gate=norm(request.expected_gate),
                next_semantic_action=norm(request.next_semantic_action),
            )
        except ContinuationPointerError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="redispatch continuation pointer is incomplete",
            )
        # The redispatch delivers an implementation_request to the fresh worker (the only kind
        # the governed worker-forward rail sends), so the immutable continuation ``expected_gate``
        # must name exactly that (Redmine #13806 R3-F1). A pointer naming a different gate would
        # send one kind while the transaction header points at another — zero-send typed blocker,
        # never advanced to completed on a mismatched gate.
        if continuation.expected_gate != RECOVERY_REDISPATCH_GATE:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail=(
                    f"continuation gate {continuation.expected_gate!r} is not a redispatchable "
                    f"worker gate ({RECOVERY_REDISPATCH_GATE!r}); zero send"
                ),
            )
        try:
            worker = ParticipantPin(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, old_locator=request.locator,
                is_self=False, lane_revision=request.lane_revision,
                lane_generation=request.lane_generation,
            )
        except ParticipantPinError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, detail="approved worker pin is incomplete",
            )
        try:
            key = ReplacementTransactionKey(self._workspace_id, expected_action)
        except ValueError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, detail="workspace / action identity is incomplete",
            )
        gen = request.action_generation

        # 2. Plan (or idempotently resume) the non-self worker-recovery transaction.
        plan = self._store.plan_transaction(
            key, action_generation=gen, decision=decision, continuation=continuation,
            participants=[worker],
        )
        if not plan.applied and plan.reason != CAS_ALREADY_DECLARED:
            return self._outcome(
                request, verdict, status=RECOVERY_STOPPED, executed=True,
                observation=observation, detail=f"transaction plan refused ({plan.reason})",
            )
        current = self._store.get(key)
        if current is None:
            return self._outcome(
                request, verdict, status=RECOVERY_STOPPED, executed=True,
                observation=observation, detail="transaction row vanished after plan",
            )
        # A pre-existing row at this key must be THIS exact approved generation + decision +
        # continuation AND the same single pinned worker (identity + evidence) — otherwise a
        # different authority is already acting on this worker (authority conflict). Zero
        # actuation. (A fresh plan trivially matches.) The transaction key
        # ``recover:<lane>:<role>:<provider>:<name>:<locator>`` does not include the lane
        # ``(revision, generation)`` evidence, so an approval that differs ONLY in those pins is
        # explicitly refused here rather than silently resuming the stored worker's evidence.
        stored_worker = current.find_participant(worker.identity)
        if (
            current.action_generation != gen
            or current.decision != decision
            or current.continuation != continuation
            or len(current.participants) != 1
            or stored_worker is None
            or stored_worker.old_locator != worker.old_locator
            or stored_worker.lane_revision != worker.lane_revision
            or stored_worker.lane_generation != worker.lane_generation
        ):
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, phase=current.phase, revision=current.revision,
                detail="a different recovery authority is already in flight for this worker",
            )

        # 3. Drive the guarded close → launch → attest (tranche B actuator, byte-preserving).
        actuator = ReplacementActuatorUseCase(
            self._store, self._actuation_port, clock=self._clock,
            lease_ttl_seconds=self._ttl,
            preservation_policy=assess_worker_recovery_preservation,
        )
        recov = actuator.drive_worker_recovery(
            key, holder=request.holder, expected_action_generation=gen,
        )
        after = self._store.get(key)
        worker_pin = after.find_participant(worker.identity) if after else None
        if recov.status != ACTUATION_RECOVERED:
            # The recovery stopped fail-closed; the durable transaction holds the replay fence.
            return self._outcome(
                request, verdict, status=RECOVERY_STOPPED, executed=True,
                observation=observation, recovery_status=recov.status,
                closed_old_worker=self._closed_old_worker(worker_pin),
                phase=after.phase if after else "", revision=after.revision if after else 0,
                detail=f"worker recovery stopped ({recov.status}); re-run resumes",
            )

        # 4. Fresh receiver attested — redispatch the ORIGINAL gate exactly once.
        redis = self._redispatch(key, holder=request.holder, gen=gen)
        final = self._store.get(key)
        status = (
            RECOVERY_COMPLETED
            if redis == REDISPATCH_CONFIRMED
            else RECOVERY_STOPPED
        )
        return self._outcome(
            request, verdict, status=status, executed=True, observation=observation,
            recovery_status=recov.status, redispatch_status=redis,
            closed_old_worker=self._closed_old_worker(worker_pin),
            fresh_slot_attested=True,
            phase=final.phase if final else "", revision=final.revision if final else 0,
            detail=(
                "worker replaced and original gate redispatched exactly once"
                if redis == REDISPATCH_CONFIRMED
                else f"worker replaced; redispatch {redis} (no blind resend; re-run resumes)"
            ),
        )

    # -- redispatch leg (rides the tranche C drain discipline) ----------------

    def _redispatch(self, key, *, holder, gen) -> str:
        """Redispatch the original durable gate exactly once (``replacing_nonself -> completed``).

        Reuses the tranche C drain's "record ``attempted`` (the phase move into
        ``draining_continuation``) BEFORE the send, then confirm on the durable gate, and NEVER
        blind-resend after ``attempted``" discipline (:func:`drain_state_for` /
        :func:`may_attempt_drain`). The send is high-level (the whole Implementation Request /
        gate), under the same-lane gateway ownership; an ACK / queue-enter is not completion.
        """
        rec = self._store.get(key)
        if rec is None:
            return REDISPATCH_NOT_FOUND
        continuation = rec.continuation
        if continuation is None:
            return REDISPATCH_CONTINUATION_UNREADABLE
        # Idempotency FIRST: if the original gate has already landed (a prior send that we could
        # not confirm, or an out-of-band dispatch), advance to completion with ZERO send — even
        # from ``replacing_nonself`` (which ``drain_state_for`` treats as not-yet-attempted
        # regardless of the gate). This is what makes the redispatch exactly-once, never a
        # duplicate dispatch.
        if self._ops.gate_redispatched(continuation):
            return self._finalize_confirmed(key, holder=holder, gen=gen)
        # The gate is not confirmed; ride the drain state machine from the current phase.
        state = drain_state_for(rec.phase, gate_confirmed=False)
        if not may_attempt_drain(state):
            # attempted / uncertain and the gate is NOT confirmed — a send may be in flight.
            # Report uncertain; a later re-run re-checks the gate. Never blind-resend.
            return REDISPATCH_UNCERTAIN
        # not_attempted (phase replacing_nonself): record attempted (-> draining_continuation)
        # BEFORE the send, so a crash here resumes as uncertain rather than re-sending.
        attempt = self._store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=PHASE_DRAINING_CONTINUATION, holder=holder, now=self._clock(),
        )
        terminal = self._redispatch_terminal(attempt)
        if terminal is not None:
            return terminal
        # Re-authenticate the lease immediately before the send (a live-holder CAS re-read on a
        # fresh clock) — a lost lease yields ZERO send.
        fresh = self._store.get(key)
        effect_now = self._clock()
        if (
            fresh is None
            or fresh.action_generation != gen
            or fresh.lease_holder != holder
            or not fresh.lease_is_live(effect_now)
        ):
            return REDISPATCH_LEASE_LOST
        if self._ops.redispatch_gate(continuation) != DRAIN_SEND_OK:
            # Send failed; the state stays attempted/draining_continuation. A re-run re-checks
            # the gate and only completes if it confirms — never a blind resend.
            return REDISPATCH_SEND_FAILED
        if not self._ops.gate_redispatched(continuation):
            return REDISPATCH_UNCERTAIN
        return self._finalize_confirmed(key, holder=holder, gen=gen)

    def _finalize_confirmed(self, key, *, holder, gen) -> str:
        """Advance a gate-confirmed transaction to ``completed`` with ZERO send (idempotent).

        Reached only when the original gate has landed (a confirmed send, an out-of-band
        dispatch, or an already-``completed`` resume). Advances ``replacing_nonself ->
        draining_continuation -> completed`` as needed and releases the lease — never issues a
        send, so it can never duplicate the dispatch.
        """
        rec = self._store.get(key)
        if rec is None:
            return REDISPATCH_NOT_FOUND
        if rec.phase == PHASE_REPLACING_NONSELF:
            attempt = self._store.transition_phase(
                key, expected_revision=rec.revision, expected_action_generation=gen,
                target=PHASE_DRAINING_CONTINUATION, holder=holder, now=self._clock(),
            )
            terminal = self._redispatch_terminal(attempt)
            if terminal is not None:
                return terminal
            rec = self._store.get(key)
        if rec is not None and rec.phase == PHASE_DRAINING_CONTINUATION:
            done = self._store.transition_phase(
                key, expected_revision=rec.revision, expected_action_generation=gen,
                target=PHASE_COMPLETED, holder=holder, now=self._clock(),
            )
            terminal = self._redispatch_terminal(done)
            if terminal is not None:
                return terminal
        self._release(key, gen, holder)
        return REDISPATCH_CONFIRMED

    def _redispatch_terminal(self, outcome) -> Optional[str]:
        if outcome.applied:
            return None
        if outcome.reason == CAS_LEASE_NOT_HELD:
            return REDISPATCH_LEASE_LOST
        if outcome.reason == CAS_GENERATION_MISMATCH:
            return REDISPATCH_GENERATION_MISMATCH
        # A benign stale revision (a concurrent read moved the row) — a re-run re-reads; report
        # uncertain rather than assume the send state.
        return REDISPATCH_UNCERTAIN

    def _release(self, key, gen, holder) -> None:
        rec = self._store.get(key)
        if rec is None or rec.lease_holder != holder:
            return
        self._store.release(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            holder=holder, now=self._clock(),
        )

    @staticmethod
    def _closed_old_worker(worker_pin) -> bool:
        # The old exact worker was closed once the participant moved off close_owed.
        return worker_pin is not None and worker_pin.phase not in ("close_owed", "")

    # -- rendering -----------------------------------------------------------

    def _outcome(
        self,
        request: RecoveryRequest,
        verdict: str,
        *,
        status: str,
        executed: bool = False,
        observation: Optional[RecoveryObservation] = None,
        recovery_status: str = "",
        redispatch_status: str = "",
        closed_old_worker: bool = False,
        fresh_slot_attested: bool = False,
        phase: str = "",
        revision: int = 0,
        detail: str = "",
    ) -> RecoveryOutcome:
        return RecoveryOutcome(
            issue=norm(request.issue),
            lane=norm(request.lane),
            role=norm(request.role),
            verdict=verdict,
            status=status,
            executed=executed,
            recovery_status=recovery_status,
            redispatch_status=redispatch_status,
            closed_old_worker=closed_old_worker,
            fresh_slot_attested=fresh_slot_attested,
            phase=phase,
            revision=revision,
            detail=detail,
            observation=observation.as_payload() if observation is not None else None,
        )


# -- CLI ------------------------------------------------------------------------

#: The verdict a fail-closed construction error surfaces (a missing repo / workspace identity),
#: so a broken invocation never silently reads as a clean preflight.
SEAM_UNAVAILABLE_VERDICT = "recovery_seam_error"


def format_recover_text(outcome: RecoveryOutcome) -> str:
    lines = [
        f"sublane recover-stale: {outcome.lane} / {outcome.role} (issue {outcome.issue})",
        f"  verdict: {outcome.verdict}  status: {outcome.status}",
        f"  executed: {outcome.executed}",
    ]
    if outcome.executed:
        lines.append(
            f"  recovery: {outcome.recovery_status or '-'}  "
            f"redispatch: {outcome.redispatch_status or '-'}  "
            f"closed_old: {outcome.closed_old_worker}"
        )
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    return "\n".join(lines)


def _run_live_recovery(
    args: argparse.Namespace, request: RecoveryRequest, *, execute: bool
) -> RecoveryOutcome:
    """Construct the LIVE use case (real inventory + actuation + redispatch) and run it.

    The live adapters are imported lazily to avoid an import cycle (they import this module for
    the request / ops types). A construction error — a repo / workspace identity that cannot be
    resolved — is a fail-closed typed outcome, never a fabricated preflight.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
        LiveRecoveryActuatorPort,
        LiveStaleWorkerRecoveryOps,
    )

    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    try:
        workspace_id = repo_scope_workspace_id(repo_root)
    except Exception:  # noqa: BLE001 - an unresolvable workspace identity fails closed
        workspace_id = ""
    if not norm(workspace_id):
        return RecoveryOutcome(
            issue=norm(request.issue), lane=norm(request.lane), role=norm(request.role),
            verdict=SEAM_UNAVAILABLE_VERDICT, status=RECOVERY_REFUSED, executed=execute,
            detail="could not resolve the repo workspace identity; zero process effect",
        )
    # The transaction key the use case will derive (best-effort; the use case re-derives and
    # refuses on incomplete inputs before the port is ever exercised).
    try:
        action_id = stale_worker_recovery_action_id(
            lane_id=request.lane, role=request.role, provider=request.provider,
            assigned_name=request.assigned_name, locator=request.locator,
        )
        key = ReplacementTransactionKey(workspace_id, action_id)
    except Exception:  # noqa: BLE001 - incomplete identity => the use case refuses downstream
        key = ReplacementTransactionKey(workspace_id, "recover:pending")
    store = ReplacementTransactionStore()
    actuation_port = LiveRecoveryActuatorPort(
        repo_root=repo_root, request=request, store=store, key=key,
    )
    ops = LiveStaleWorkerRecoveryOps(repo_root=repo_root, request=request)
    use_case = StaleWorkerRecoveryUseCase(
        store, actuation_port, ops, workspace_id=workspace_id,
    )
    return use_case.run(request, execute=execute)


def cmd_sublane_recover_stale(args: argparse.Namespace) -> int:
    request = RecoveryRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        role=getattr(args, "role", "") or "",
        provider=getattr(args, "provider", "") or "",
        assigned_name=getattr(args, "assigned_name", "") or "",
        locator=getattr(args, "locator", "") or "",
        journal=getattr(args, "journal", "") or "",
        action_id=getattr(args, "action_id", "") or "",
        action_generation=int(getattr(args, "action_generation", 0) or 0),
        lane_revision=getattr(args, "lane_revision", "") or "",
        lane_generation=getattr(args, "lane_generation", "") or "",
        expected_gate=getattr(args, "expected_gate", "") or "",
        next_semantic_action=getattr(args, "next_semantic_action", "") or "",
    )
    execute = bool(getattr(args, "execute", False))
    outcome = _run_live_recovery(args, request, execute=execute)
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_recover_text(outcome), file=sys.stdout)
    # A staged-seam refusal is a non-zero exit so a caller never mistakes it for a completed
    # recovery; a preflight (once wired) that merely reports a blocker is exit 0.
    return 1 if outcome.is_blocked or outcome.verdict == SEAM_UNAVAILABLE_VERDICT else 0


def register_sublane_recover_stale_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "recover-stale",
        help=(
            "Redmine #13806: recover the exact stale standard-sublane worker of a lane whose "
            "worker process vanished after a turn. Default is read-only preflight; --execute "
            "requires a positive generation-bound owner approval and closes only that worker "
            "(never the gateway / coordinator / a foreign slot), byte-preserving the worktree."
        ),
    )
    for flag, dest, help_text in (
        ("--issue", "issue", "Redmine issue id owning the lane"),
        ("--lane", "lane", "Exact lane id/label of the stale worker"),
        ("--role", "role", "Exact provider role of the worker"),
        ("--provider", "provider", "Exact provider of the worker"),
        ("--assigned-name", "assigned_name", "Exact managed assigned name"),
        ("--locator", "locator", "Exact stale (old) process locator"),
    ):
        parser.add_argument(flag, dest=dest, required=True, help=help_text)
    for flag, dest, help_text in (
        ("--journal", "journal", "Positive owner approval journal id (--execute)"),
        ("--action-id", "action_id", "Exact recover:<lane>:<role>:<provider>:<name>:<locator> id"),
        ("--lane-revision", "lane_revision", "Lane lifecycle revision pinned at approval"),
        ("--lane-generation", "lane_generation", "Lane lifecycle generation pinned at approval"),
        ("--expected-gate", "expected_gate", "The durable gate the fresh worker must resume"),
        (
            "--next-semantic-action",
            "next_semantic_action",
            "The single semantic action to redispatch exactly once",
        ),
    ):
        parser.add_argument(flag, dest=dest, default="", help=help_text)
    parser.add_argument(
        "--action-generation", dest="action_generation", type=int, default=0,
        help="Immutable approved generation counter (>= 1) (--execute)",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply owner-approved recovery; otherwise read-only preflight only",
    )
    from mozyo_bridge.application.cli_common import add_repo_option

    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_recover_stale)


__all__ = (
    "RECOVERY_PREFLIGHT",
    "RECOVERY_REFUSED",
    "RECOVERY_COMPLETED",
    "RECOVERY_STOPPED",
    "REDISPATCH_CONFIRMED",
    "REDISPATCH_UNCERTAIN",
    "REDISPATCH_SEND_FAILED",
    "REDISPATCH_LEASE_LOST",
    "REDISPATCH_GENERATION_MISMATCH",
    "REDISPATCH_NOT_FOUND",
    "REDISPATCH_CONTINUATION_UNREADABLE",
    "RecoveryRequest",
    "RecoveryOutcome",
    "StaleWorkerRecoveryOps",
    "StaleWorkerRecoveryUseCase",
    "LIVE_RECOVERY_SEAM_INSTALLED",
    "SEAM_UNAVAILABLE_VERDICT",
    "cmd_sublane_recover_stale",
    "format_recover_text",
    "register_sublane_recover_stale_parser",
)
