"""Exact-generation actuator use case (Redmine #13806 tranche B).

Drives the tranche A replacement transaction
(:mod:`mozyo_bridge.core.state.replacement_transaction`) forward by *actuating the non-self
participants* and arming the transaction up to ``self_close_armed`` — the tranche B
boundary. It composes the tranche A CAS store (the durable owed state / lease / immutable
generation) with an injected :class:`...application.replacement_actuator_ops.ExactGenerationActuatorPort`
(the live close / launch / attestation effects, faked in tests), and makes every decision
from the pure :mod:`...domain.replacement_actuation` vocabulary.

The driver is **resumable / partial-replay safe** by construction: it re-reads the durable
transaction at the top of every step and acts only on the remaining owed work, so a crash
anywhere is recovered by re-running against the same durable row. Each participant walks
``close_owed -> launch_owed -> verify_owed -> replaced`` with an evidence-gated effect at
each step, recorded to the durable owed state by a tranche A CAS *before* the actuator
trusts it (j#78384 §2 "effect 前に次の owed state を CAS 記録する").

What tranche B deliberately does NOT do (j#79121 non-scope): it never closes / kills the
self (current coordinator) participant (no in-victim synchronous kill), never claims the
fresh coordinator, never drains the continuation, and performs no live process mutation of
its own (all live effects are behind the injected port; no live adapter ships here). It
arms at ``self_close_armed`` and yields; the self-close executor + fresh-coordinator claim +
continuation drain are tranche C.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from mozyo_bridge.core.state.replacement_preservation import (
    PreservationObservation,
    PreservationVerdict,
    assess_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import ReplacementTransactionStore
from mozyo_bridge.core.state.replacement_transaction_model import (
    CAS_GENERATION_MISMATCH,
    CAS_LEASE_NOT_HELD,
    CAS_STALE_REVISION,
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANT_LAUNCH_OWED,
    PARTICIPANT_REPLACED,
    PARTICIPANT_VERIFY_OWED,
    PHASE_AWAITING_SELF_TURN_END,
    PHASE_CLAIMED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_PLANNED,
    PHASE_REPLACING_NONSELF,
    PHASE_SELF_CLOSE_ARMED,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionRecord,
)
from mozyo_bridge.core.state.launch_identity_receipt import (
    CONSUME_ABSENT,
    CONSUME_FOREIGN,
    CONSUME_OK,
    CONSUME_REPLAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_completion import (  # noqa: E501
    COMPLETION_CAUSE_MISMATCH,
    COMPLETION_FOREIGN_WORKSPACE,
    COMPLETION_INCOMPLETE,
    COMPLETION_UNAVAILABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (  # noqa: E501
    ExactGenerationActuatorPort,
    UpdateEvidenceCompletionPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_AMBIGUOUS,
    ACTUATION_ARMED,
    ACTUATION_ATTESTATION_MISMATCH,
    ACTUATION_EFFECT_FAILED,
    ACTUATION_GENERATION_MISMATCH,
    ACTUATION_IN_PROGRESS,
    ACTUATION_INVALID_TOPOLOGY,
    ACTUATION_LEASE_LOST,
    ACTUATION_NOT_FOUND,
    ACTUATION_PRESERVATION_BLOCKED,
    ACTUATION_RECOVERED,
    ATTEST_MISMATCH,
    CLOSE_DONE,
    LAUNCH_DONE,
    attestation_completes,
    bounded_recovery_available,
    is_self_replacement_topology,
    is_worker_recovery_topology,
    is_zero_actuation_observation,
    new_close_required,
    nonself_actuation_order,
    zero_actuation_status,
)

#: Default lease TTL the actuator claims for (seconds). Generous relative to a synthetic
#: drive; the real live cadence is the caller's (and each step re-checks live ownership).
DEFAULT_LEASE_TTL_SECONDS = 300


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ActuationResult:
    """The terminal (or yield) outcome of one actuator run.

    ``status`` is a closed :mod:`...domain.replacement_actuation` token. ``stopped_on`` is
    the participant identity the run stopped on (``None`` for a whole-transaction outcome
    like ``armed`` / ``not_found`` / ``lease_lost`` at the transaction level).
    ``preservation_reasons`` is populated only for ``preservation_blocked``. The full
    participant state is always re-readable from the durable transaction — this result is a
    pointer to *why the run stopped*, not a copy of the state.
    """

    status: str
    phase: str = ""
    revision: int = 0
    stopped_on: Optional[tuple[str, str, str, str]] = None
    detail: str = ""
    preservation_reasons: tuple[str, ...] = ()

    @property
    def armed(self) -> bool:
        return self.status == ACTUATION_ARMED

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "revision": self.revision,
            "stopped_on": list(self.stopped_on) if self.stopped_on else None,
            "detail": self.detail,
            "preservation_reasons": list(self.preservation_reasons),
        }


#: Every answer the actuator will act on, as exact closed tokens. An injected port is not
#: the actuator's code: whatever it returns is INPUT, and input does not get rendered into a
#: public detail (audit j#97136 F1). A newline, an ANSI escape, a workflow marker or an
#: object with an opinionated ``__format__`` would otherwise reach the surface verbatim.
COMPLETION_SUCCESS_OUTCOMES = (CONSUME_OK, CONSUME_REPLAY)
#: The typed refusals this build knows how to name.
COMPLETION_KNOWN_FAILURES = (
    CONSUME_ABSENT,
    CONSUME_FOREIGN,
    COMPLETION_CAUSE_MISMATCH,
    COMPLETION_FOREIGN_WORKSPACE,
    COMPLETION_INCOMPLETE,
    COMPLETION_UNAVAILABLE,
)
#: Anything else -- an unknown token, a non-``str``, or a port that raised.
COMPLETION_UNKNOWN = "evidence_completion_unknown_outcome"
#: Closed, fixed text per outcome. Built from the token constants themselves, so the detail
#: can only ever be one of these literals.
COMPLETION_FAILURE_DETAIL = {
    token: f"update evidence not discharged ({token})"
    for token in COMPLETION_KNOWN_FAILURES + (COMPLETION_UNKNOWN,)
}


def _completion_outcome(port, key, pin, replacement_action_id: str) -> str:
    """Call the completion port and reduce its answer to a token this build knows.

    ``type(outcome) is str`` and membership in the closed set, in that order: a ``str``
    subclass decides for itself what ``==`` means, and an arbitrary object should never be
    compared at all.

    ``except Exception``, NOT ``BaseException``. A port FAILING is an ``Exception``; a
    ``KeyboardInterrupt``, ``SystemExit`` or ``GeneratorExit`` is the process or the
    interpreter unwinding, and swallowing those turns control flow into a typed refusal
    (audit j#97142 R1 measured it: a ``GeneratorExit`` that must propagate came back as
    ``evidence_completion_unknown_outcome``). Catching the narrower class also removes the
    need for a re-raise clause -- none of them are ``Exception`` to begin with.
    """
    try:
        outcome = port(key, pin, replacement_action_id=replacement_action_id)
    except Exception:  # noqa: BLE001 - an unusable port leaves the participant owed
        return COMPLETION_UNKNOWN
    if type(outcome) is not str:
        return COMPLETION_UNKNOWN
    if outcome in COMPLETION_SUCCESS_OUTCOMES or outcome in COMPLETION_KNOWN_FAILURES:
        return outcome
    return COMPLETION_UNKNOWN


def _phase_token(value: object) -> str:
    """A phase only when it is already plain exact text; otherwise no phase at all."""
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


#: The phases a worker recovery may be driven from.
_RECOVERY_DRIVABLE_PHASES = (PHASE_PLANNED, PHASE_CLAIMED, PHASE_REPLACING_NONSELF)
#: The phases that MEAN the redispatch leg already ran.
_RECOVERY_PROGRESSED_PHASES = (PHASE_DRAINING_CONTINUATION, PHASE_COMPLETED)


def _worker_recovery_phase_refusal(rec):
    """The refusal a row's phase earns before anything is claimed, or ``None``. (pure)

    Two things this used to get wrong, both measured (j#97190 F2, j#97201):

    * every phase outside the drivable three was reported as ``recovered`` -- including a
      SELF-replacement phase and one this build does not know -- for a participant that was
      never launched or attested;
    * ``draining_continuation`` / ``completed`` were taken at face value. They mean the
      redispatch leg already ran, which is only true if every non-self participant really is
      ``replaced``; a completed row whose worker is still ``close_owed`` is a contradiction,
      not an idempotent success.
    """
    phase = _phase_token(getattr(rec, "phase", ""))
    if phase in _RECOVERY_PROGRESSED_PHASES:
        participants = tuple(getattr(rec, "participants", ()) or ())
        if participants and all(
            _phase_token(getattr(p, "phase", "")) == PARTICIPANT_REPLACED
            for p in participants
        ):
            return None
        return ActuationResult(
            status=ACTUATION_INVALID_TOPOLOGY, phase=rec.phase, revision=rec.revision,
            detail="a progressed phase whose participants are not replaced",
        )
    if phase in _RECOVERY_DRIVABLE_PHASES:
        return None
    return ActuationResult(
        status=ACTUATION_INVALID_TOPOLOGY, phase=rec.phase, revision=rec.revision,
        detail="phase is not part of the worker-recovery flow",
    )


def _pin_carries_evidence(pin) -> bool:
    """Does this participant carry an update-evidence triplet at all? (pure)

    Presence, not well-formedness: a pin whose triplet is malformed must reach the
    completion port and be refused there, not be read as "legacy" and skipped.
    """
    return any(
        getattr(pin, name, "") not in (None, "")
        for name in (
            "evidence_workspace_id",
            "evidence_startup_action_id",
            "evidence_cause",
        )
    )


class ReplacementActuatorUseCase:
    """Drive a replacement transaction's non-self participants and arm it (tranche B)."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        port: ExactGenerationActuatorPort,
        *,
        clock: Callable[[], str] = _utc_now,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        preservation_policy: Callable[
            [PreservationObservation], PreservationVerdict
        ] = assess_preservation,
        launch_authority: Optional[Callable[[ParticipantPin], bool]] = None,
        store_admission: Optional[
            Callable[[ReplacementTransactionKey, ParticipantPin], Optional[str]]
        ] = None,
        evidence_completion: Optional[UpdateEvidenceCompletionPort] = None,
    ) -> None:
        self._store = store
        self._port = port
        self._clock = clock
        self._ttl = lease_ttl_seconds
        # The close fence applied before a NEW close. Defaults to the self-replacement fence
        # (:func:`assess_preservation`); a coordinator-alive worker recovery injects
        # :func:`assess_worker_recovery_preservation`, which byte-preserves (does not block on)
        # a dirty / unrecorded worktree while still refusing to close a live-working or
        # wrong-identity slot (Redmine #13806 tranche D, j#79485 §4).
        self._preservation_policy = preservation_policy
        # An OPTIONAL action-time authority probe re-joined by the launch step IMMEDIATELY before
        # the (bounded-recovery) ``launch_action_bound`` effect (Redmine #13806 R3-F1, Review
        # j#82731). ``None`` (the self-replacement default) leaves the launch unchanged. A
        # post-close worker recovery injects one that re-verifies the exact live lane authority
        # (lifecycle / worktree token / branch) + a lane free of any foreign live process, so the
        # relaunch is fenced action-time and not by a stale admission-time snapshot. Returns
        # ``True`` to permit the launch; ``False`` fails closed with zero launch (stay launch_owed).
        self._launch_authority = launch_authority
        # An OPTIONAL pre-effect admission probe evaluated BEFORE any owed step is dispatched
        # (Redmine #14756 j#96848). It answers "may this action produce effects at all?" — as
        # opposed to ``launch_authority``, which answers "is the lane still exact?" immediately
        # before one launch. The distinction is the whole point of this seam: the attestation
        # store's shape is knowable before anything is touched, and evaluating it inside the
        # LAUNCH step meant the refusal arrived only after ``_step_close_owed`` had already
        # closed the old slot and CAS'd the participant to ``launch_owed`` — irreversible
        # effects ahead of a typed "we refuse to act" (measured; the contract claimed
        # zero-close and was not). Returning a reason token refuses with zero close, zero CAS
        # and zero launch; ``None`` permits. Replays reach it too, because every owed step —
        # first run or crash replay — dispatches through one place.
        #
        # It is called with ``(key, pin)`` because the transaction already pins both halves of
        # the question — ``key.workspace_id`` and ``pin.lane_id`` — so no construction site has
        # to re-derive them from whatever it happens to have in scope. That matters: the six
        # sites are NOT alike (three hold an ops handle and a request, three do not), and a
        # seam that made each one resolve the lane itself would have been six chances to
        # resolve it differently. The injected callable therefore supplies only what genuinely
        # varies per site — which store home is selected.
        self._store_admission = store_admission
        # OPTIONAL, and a separate port from the actuator's five effects on purpose: the
        # self-replacement executor must never consume anything, so "no port" has to be a
        # state this use case can refuse on rather than a method every implementer is
        # forced to grow. Bound by each construction site to the SAME explicit home the
        # planner reads, so a plan and its discharge cannot address different stores.
        self._evidence_completion = evidence_completion

    def run(
        self,
        key: ReplacementTransactionKey,
        *,
        holder: str,
        expected_action_generation: int,
    ) -> ActuationResult:
        """Actuate ``key`` as ``holder`` up to ``self_close_armed``, or stop fail-closed.

        Acquires (or resumes) the lease, then drives the fixed DAG: ``planned -> claimed ->
        replacing_nonself`` (replacing every non-self participant in turn) ``->
        awaiting_self_turn_end -> self_close_armed``. Returns ``armed`` when the transaction
        is armed with every non-self participant ``replaced``; otherwise a closed fail-closed
        status naming why it stopped. The self participant is never actuated here.
        """
        rec = self._store.get(key)
        if rec is None:
            return ActuationResult(status=ACTUATION_NOT_FOUND)
        if rec.action_generation != expected_action_generation:
            return ActuationResult(
                status=ACTUATION_GENERATION_MISMATCH,
                phase=rec.phase,
                revision=rec.revision,
            )
        if not is_self_replacement_topology(rec.participants):
            # An atomic *self* replacement carries exactly one self participant (R1-F2).
            # Refuse a zero- / many-self plan BEFORE claiming or any effect, so the
            # destructive non-self replacement never runs for a non-self-replacement plan.
            return ActuationResult(
                status=ACTUATION_INVALID_TOPOLOGY, phase=rec.phase, revision=rec.revision,
                detail="a self-replacement transaction requires exactly one self participant",
            )
        now = self._clock()
        claim = self._store.claim(
            key,
            expected_revision=rec.revision,
            expected_action_generation=expected_action_generation,
            holder=holder,
            lease_expires_at=self._expiry(now),
            now=now,
        )
        if not claim.applied:
            # A live foreign holder (lease_conflict) or a superseded generation.
            status = (
                ACTUATION_GENERATION_MISMATCH
                if claim.reason == CAS_GENERATION_MISMATCH
                else ACTUATION_LEASE_LOST
            )
            return ActuationResult(
                status=status, phase=rec.phase, revision=claim.revision,
                detail=claim.reason,
            )
        return self._drive(key, holder, expected_action_generation)

    def drive_self_participant(
        self,
        key: ReplacementTransactionKey,
        *,
        holder: str,
        expected_action_generation: int,
    ) -> ActuationResult:
        """Drive ONLY the self participant's owed progression during ``self_close_armed``.

        The tranche C entry (Redmine #13806): tranche B's :meth:`run` arms the transaction
        at ``self_close_armed`` and never touches the self coordinator. Once a
        process-external executor is ready to replace the current coordinator, THIS drives
        the self participant ``close_owed -> launch_owed -> verify_owed -> replaced`` — the
        old self generation is closed and the fresh coordinator launched + attested — reusing
        the exact same evidence-gated steps, pre-effect lease re-authentication, and CAS
        discipline as the non-self path (so the R1/R2/R3 lease fences apply identically).

        It refuses unless the transaction is at ``self_close_armed`` (the only phase the self
        participant may be actuated in), and it releases the lease when the self reaches
        ``replaced`` so a **fresh, action-attested** coordinator can then claim it and drain
        the continuation (j#78384 §2 step 7 — the executor never claims the fresh coordinator
        itself). Returns ``armed`` when the self is already / now ``replaced`` (the transaction
        is ready for the fresh-coordinator claim), or a fail-closed status.
        """
        rec = self._store.get(key)
        if rec is None:
            return ActuationResult(status=ACTUATION_NOT_FOUND)
        if rec.action_generation != expected_action_generation:
            return ActuationResult(
                status=ACTUATION_GENERATION_MISMATCH, phase=rec.phase,
                revision=rec.revision,
            )
        if rec.phase != PHASE_SELF_CLOSE_ARMED:
            # The self participant is actuated only in its armed window; refuse otherwise.
            return ActuationResult(
                status=ACTUATION_EFFECT_FAILED, phase=rec.phase, revision=rec.revision,
                detail="self participant may only be driven at self_close_armed",
            )
        self_pins = [p for p in rec.participants if p.is_self]
        if len(self_pins) != 1:
            return ActuationResult(
                status=ACTUATION_INVALID_TOPOLOGY, phase=rec.phase, revision=rec.revision,
                detail="exactly one self participant required",
            )
        self_identity = self_pins[0].identity
        now = self._clock()
        claim = self._store.claim(
            key, expected_revision=rec.revision,
            expected_action_generation=expected_action_generation, holder=holder,
            lease_expires_at=self._expiry(now), now=now,
        )
        if not claim.applied:
            status = (
                ACTUATION_GENERATION_MISMATCH
                if claim.reason == CAS_GENERATION_MISMATCH
                else ACTUATION_LEASE_LOST
            )
            return ActuationResult(
                status=status, phase=rec.phase, revision=claim.revision,
                detail=claim.reason,
            )
        gen = expected_action_generation
        max_iterations = 8
        for _ in range(max_iterations):
            now = self._clock()
            rec = self._store.get(key)
            if rec is None:
                return ActuationResult(status=ACTUATION_NOT_FOUND)
            if rec.action_generation != gen:
                return ActuationResult(
                    status=ACTUATION_GENERATION_MISMATCH, phase=rec.phase,
                    revision=rec.revision,
                )
            if rec.lease_holder != holder or not rec.lease_is_live(now):
                return ActuationResult(
                    status=ACTUATION_LEASE_LOST, phase=rec.phase, revision=rec.revision,
                    detail="lease not live",
                )
            if rec.phase != PHASE_SELF_CLOSE_ARMED:
                return ActuationResult(
                    status=ACTUATION_EFFECT_FAILED, phase=rec.phase,
                    revision=rec.revision, detail="phase moved off self_close_armed",
                )
            self_pin = rec.find_participant(self_identity)
            if self_pin is not None and self_pin.phase == PARTICIPANT_REPLACED:
                # The self is replaced; hand the lease back so a fresh coordinator may claim.
                self._store.release(
                    key, expected_revision=rec.revision,
                    expected_action_generation=gen, holder=holder, now=now,
                )
                return ActuationResult(
                    status=ACTUATION_ARMED, phase=rec.phase, revision=rec.revision,
                    stopped_on=self_identity,
                )
            terminal = self._actuate_participant(key, rec, self_pin, holder, gen, now)
            if terminal is not None:
                return terminal
        return ActuationResult(
            status=ACTUATION_EFFECT_FAILED, detail="self-drive iteration cap exceeded"
        )

    def drive_worker_recovery(
        self,
        key: ReplacementTransactionKey,
        *,
        holder: str,
        expected_action_generation: int,
    ) -> ActuationResult:
        """Drive a coordinator-alive **worker recovery** to ``recovered`` (Redmine #13806 tranche D).

        The public entry for recovering one or more stale standard-sublane workers *while the
        current coordinator keeps running*: the transaction carries only NON-self participants
        (no self coordinator to replace — :func:`is_worker_recovery_topology`). It reuses the
        exact same per-participant owed progression as the self-replacement's non-self path
        (``close_owed -> launch_owed -> verify_owed -> replaced``), with the identical
        evidence gates, pre-effect lease re-authentication (R1/R2/R3), and CAS discipline — the
        old exact worker generation is closed, a fresh slot launched, and its startup
        attestation confirmed to bind THIS replacement action.

        Unlike :meth:`run` it drives ``planned -> claimed -> replacing_nonself`` and then
        **stops** once every worker is ``replaced`` (it does NOT advance to
        ``awaiting_self_turn_end`` — there is no self-close leg). It returns
        :data:`ACTUATION_RECOVERED` holding the lease, so the recovery use case's next leg can
        redispatch the original durable gate exactly once (``replacing_nonself ->
        draining_continuation -> completed``) under the same holder. A resume at
        ``draining_continuation`` / ``completed`` (the redispatch leg already advanced) also
        returns ``recovered`` without re-actuating any worker. Refuses a self-bearing manifest
        (that is a self-replacement) or an empty one with :data:`ACTUATION_INVALID_TOPOLOGY`,
        zero effect.
        """
        rec = self._store.get(key)
        if rec is None:
            return ActuationResult(status=ACTUATION_NOT_FOUND)
        if rec.action_generation != expected_action_generation:
            return ActuationResult(
                status=ACTUATION_GENERATION_MISMATCH, phase=rec.phase,
                revision=rec.revision,
            )
        if not is_worker_recovery_topology(rec.participants):
            # A worker recovery carries zero self participants and >=1 non-self. A self-bearing
            # manifest is a self-replacement (driven by run / drive_self_participant); refuse it
            # here BEFORE any claim or effect so the coordinator-alive path never closes a self.
            return ActuationResult(
                status=ACTUATION_INVALID_TOPOLOGY, phase=rec.phase, revision=rec.revision,
                detail=(
                    "a worker recovery requires zero self participants and at least one "
                    "non-self participant"
                ),
            )
        phase_refusal = _worker_recovery_phase_refusal(rec)
        if phase_refusal is not None:
            # BEFORE the clock and the claim (audit j#97201): a claim is a durable write --
            # it moves the row's revision and takes its lease -- so a phase this flow does
            # not own, or a progressed phase whose participants contradict it, has to be
            # refused here rather than after the row has already been altered. The
            # vanished-gateway executor had this gate; the other five call sites of this
            # shared method did not, which is why it belongs on this side of the boundary.
            return phase_refusal
        now = self._clock()
        claim = self._store.claim(
            key,
            expected_revision=rec.revision,
            expected_action_generation=expected_action_generation,
            holder=holder,
            lease_expires_at=self._expiry(now),
            now=now,
        )
        if not claim.applied:
            status = (
                ACTUATION_GENERATION_MISMATCH
                if claim.reason == CAS_GENERATION_MISMATCH
                else ACTUATION_LEASE_LOST
            )
            return ActuationResult(
                status=status, phase=rec.phase, revision=claim.revision,
                detail=claim.reason,
            )
        return self._drive_recovery(key, holder, expected_action_generation)

    def _drive_recovery(self, key, holder, gen) -> ActuationResult:
        rec0 = self._store.get(key)
        max_iterations = 16 + 8 * len(rec0.participants if rec0 else ())
        for _ in range(max_iterations):
            now = self._clock()
            rec = self._store.get(key)
            if rec is None:
                return ActuationResult(status=ACTUATION_NOT_FOUND)
            if rec.action_generation != gen:
                return ActuationResult(
                    status=ACTUATION_GENERATION_MISMATCH, phase=rec.phase,
                    revision=rec.revision,
                )
            if rec.lease_holder != holder or not rec.lease_is_live(now):
                return ActuationResult(
                    status=ACTUATION_LEASE_LOST, phase=rec.phase, revision=rec.revision,
                    detail="lease not live",
                )
            phase = rec.phase
            if phase == PHASE_PLANNED:
                terminal = self._advance_phase(key, rec, PHASE_CLAIMED, holder, gen, now)
            elif phase == PHASE_CLAIMED:
                terminal = self._advance_phase(
                    key, rec, PHASE_REPLACING_NONSELF, holder, gen, now
                )
            elif phase == PHASE_REPLACING_NONSELF:
                pending = [
                    p
                    for p in nonself_actuation_order(rec.participants)
                    if p.phase != PARTICIPANT_REPLACED
                ]
                if not pending:
                    # Every stale worker is replaced. Stop at replacing_nonself holding the
                    # lease — the redispatch leg (the recovery use case) advances from here.
                    return ActuationResult(
                        status=ACTUATION_RECOVERED, phase=rec.phase, revision=rec.revision,
                    )
                terminal = self._actuate_participant(
                    key, rec, pending[0], holder, gen, now
                )
            elif phase in (PHASE_DRAINING_CONTINUATION, PHASE_COMPLETED):
                # The redispatch leg already advanced past replacing_nonself -- the workers
                # are replaced. Report recovered (idempotent).
                return ActuationResult(
                    status=ACTUATION_RECOVERED, phase=rec.phase, revision=rec.revision,
                )
            else:
                # NAMED phases only (audit j#97190 F2). A bare `else` reported every other
                # phase as recovered, so a row sitting in a SELF-replacement phase, or in one
                # this build does not know, answered "recovered" for a gateway that was never
                # launched or attested -- measured: `self_close_armed` and an unknown phase
                # both returned recovered with zero launches and the participant still
                # close_owed. A phase this worker-recovery flow does not own is a refusal
                # before any claim or effect, not a success.
                return ActuationResult(
                    status=ACTUATION_INVALID_TOPOLOGY, phase=rec.phase,
                    revision=rec.revision,
                    detail="phase is not part of the worker-recovery flow",
                )
            if terminal is not None:
                return terminal
        return ActuationResult(
            status=ACTUATION_EFFECT_FAILED, detail="recovery iteration cap exceeded"
        )

    # -- driver --------------------------------------------------------------

    def _drive(self, key, holder, gen) -> ActuationResult:
        # A runaway backstop far above any real drive (a few steps per participant plus the
        # phase transitions). Hitting it is a logic bug, reported fail-closed, never looped.
        rec0 = self._store.get(key)
        max_iterations = 16 + 8 * len(rec0.participants if rec0 else ())
        for _ in range(max_iterations):
            now = self._clock()
            rec = self._store.get(key)
            if rec is None:
                return ActuationResult(status=ACTUATION_NOT_FOUND)
            if rec.action_generation != gen:
                return ActuationResult(
                    status=ACTUATION_GENERATION_MISMATCH, phase=rec.phase,
                    revision=rec.revision,
                )
            if rec.lease_holder != holder or not rec.lease_is_live(now):
                return ActuationResult(
                    status=ACTUATION_LEASE_LOST, phase=rec.phase, revision=rec.revision,
                    detail="lease not live",
                )
            phase = rec.phase
            if phase == PHASE_PLANNED:
                terminal = self._advance_phase(key, rec, PHASE_CLAIMED, holder, gen, now)
            elif phase == PHASE_CLAIMED:
                terminal = self._advance_phase(
                    key, rec, PHASE_REPLACING_NONSELF, holder, gen, now
                )
            elif phase == PHASE_REPLACING_NONSELF:
                terminal = self._replacing_nonself_step(key, rec, holder, gen, now)
            elif phase == PHASE_AWAITING_SELF_TURN_END:
                terminal = self._advance_phase(
                    key, rec, PHASE_SELF_CLOSE_ARMED, holder, gen, now
                )
            else:
                # self_close_armed (the tranche B boundary) or anything a tranche C run has
                # already advanced past — the non-self replacement is complete; yield armed.
                return ActuationResult(
                    status=ACTUATION_ARMED, phase=rec.phase, revision=rec.revision,
                )
            if terminal is not None:
                return terminal
        return ActuationResult(
            status=ACTUATION_EFFECT_FAILED, detail="iteration cap exceeded"
        )

    def _replacing_nonself_step(
        self, key, rec: ReplacementTransactionRecord, holder, gen, now
    ) -> Optional[ActuationResult]:
        # The design's fixed order (sublane participants, then the default companion), not a
        # lexical sort (R1-F3). Filter out the already-replaced ones and take the next owed.
        pending = [
            p
            for p in nonself_actuation_order(rec.participants)
            if p.phase != PARTICIPANT_REPLACED
        ]
        if not pending:
            # Every non-self participant is replaced; leave replacing_nonself. The
            # awaiting_self_turn_end prerequisite (all non-self replaced) is satisfied.
            return self._advance_phase(
                key, rec, PHASE_AWAITING_SELF_TURN_END, holder, gen, now
            )
        return self._actuate_participant(key, rec, pending[0], holder, gen, now)

    def _actuate_participant(
        self, key, rec, pin: ParticipantPin, holder, gen, now
    ) -> Optional[ActuationResult]:
        """One owed step for one non-self participant (``None`` => continue the drive)."""
        if pin.phase not in (
            PARTICIPANT_CLOSE_OWED,
            PARTICIPANT_LAUNCH_OWED,
            PARTICIPANT_VERIFY_OWED,
        ):
            # Only close_owed / launch_owed / verify_owed are actionable; replaced is
            # filtered out upstream. Any other value is a corrupt owed state — fail closed.
            return ActuationResult(
                status=ACTUATION_EFFECT_FAILED, phase=rec.phase, revision=rec.revision,
                stopped_on=pin.identity, detail=f"unactionable owed phase {pin.phase!r}",
            )
        blocked = self._admission_refusal(key, rec)
        if blocked is not None:
            # Redmine #14756 j#96848: BEFORE the close / participant CAS / launch, not between
            # them. A refusal here leaves the transaction exactly where it was, so the pair's
            # WIP and the operator's next rail both survive it.
            identity, refusal = blocked
            return ActuationResult(
                status=ACTUATION_PRESERVATION_BLOCKED, phase=rec.phase,
                revision=rec.revision, stopped_on=identity, detail=refusal,
            )
        if pin.phase == PARTICIPANT_CLOSE_OWED:
            return self._step_close_owed(key, rec, pin, holder, gen, now)
        if pin.phase == PARTICIPANT_LAUNCH_OWED:
            return self._step_launch_owed(key, rec, pin, holder, gen, now)
        return self._step_verify_owed(key, rec, pin, holder, gen, now)

    def _admission_refusal(self, key, rec) -> Optional[tuple[str, str]]:
        """``(identity, reason)`` if any UNFINISHED participant is inadmissible, else ``None``.

        Evaluated over every participant that still owes work — not only the one about to be
        actuated. j#96848 asks for zero close, zero CAS and zero launch for the OUTER action,
        and a per-participant gate would not give that: it would let the first participant be
        closed and relaunched before discovering that the second one can never come back,
        which is the same half-destroyed pair the fence exists to prevent, reached one step
        later. Already-``replaced`` participants are skipped because they have no remaining
        effect to withhold; refusing on their behalf would strand a transaction that is past
        them without protecting anything.
        """
        if self._store_admission is None:
            return None
        for candidate in rec.participants:
            if candidate.phase == PARTICIPANT_REPLACED:
                continue
            refusal = self._store_admission(key, candidate)
            if refusal:
                return (candidate.identity, refusal)
        return None

    def _step_close_owed(self, key, rec, pin, holder, gen, now) -> Optional[ActuationResult]:
        observation = self._port.observe_old_slot(pin)
        if is_zero_actuation_observation(observation):
            # A recycled / ambiguous inventory: never close, never adopt (j#78384 §4).
            return ActuationResult(
                status=zero_actuation_status(observation), phase=rec.phase,
                revision=rec.revision, stopped_on=pin.identity,
            )
        if new_close_required(observation):
            # A genuinely new close — re-evaluate the preservation fence first (j#78384 §3).
            # The fence is the injected policy: the self-replacement fence by default, or the
            # worker-recovery fence (byte-preserving a dirty worktree) for tranche D.
            verdict = self._preservation_policy(self._port.observe_preservation(pin))
            if verdict.blocked:
                return ActuationResult(
                    status=ACTUATION_PRESERVATION_BLOCKED, phase=rec.phase,
                    revision=rec.revision, stopped_on=pin.identity,
                    preservation_reasons=verdict.reasons, detail=verdict.detail,
                )
            # Re-authenticate immediately before the destructive close (R1-F1): the external
            # observations above may have taken time during which the lease could expire or
            # be reclaimed. On failure, ZERO close — never actuate without live authority.
            fresh = self._reauth_before_effect(
                key, holder, gen, pin.identity, PARTICIPANT_CLOSE_OWED
            )
            if isinstance(fresh, ActuationResult):
                return fresh
            rec = fresh
            if self._port.close_exact_generation(pin) != CLOSE_DONE:
                return ActuationResult(
                    status=ACTUATION_EFFECT_FAILED, phase=rec.phase,
                    revision=rec.revision, stopped_on=pin.identity, detail="close",
                )
        elif not bounded_recovery_available(observation):  # defensive; unreachable
            return ActuationResult(
                status=ACTUATION_EFFECT_FAILED, phase=rec.phase, revision=rec.revision,
                stopped_on=pin.identity, detail=f"unhandled observation {observation!r}",
            )
        # Either the exact old generation was just closed, or it is already absent with no
        # recycle (bounded recovery). Record that the close is done and the launch is owed.
        cas = self._store.transition_participant(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            identity=pin.identity, target=PARTICIPANT_LAUNCH_OWED, holder=holder, now=now,
        )
        return self._cas_terminal(cas, rec, pin)

    def _step_launch_owed(self, key, rec, pin, holder, gen, now) -> Optional[ActuationResult]:
        # A launch of an already-closed slot is bounded recovery — no preservation gate. But
        # it IS a live effect, so re-authenticate the lease immediately before it (R1-F1);
        # on failure, ZERO launch. The launch is bound to the replacement action id (§4).
        fresh = self._reauth_before_effect(
            key, holder, gen, pin.identity, PARTICIPANT_LAUNCH_OWED
        )
        if isinstance(fresh, ActuationResult):
            return fresh
        rec = fresh
        # R3-F1 (Review j#82731) — an OPTIONAL action-time authority probe re-joined IMMEDIATELY
        # before the launch effect (a post-close worker recovery injects one; self-replacement
        # leaves it None). The lease re-auth above proves WE still hold the transaction; this
        # proves the LANE authority (lifecycle / worktree token / branch) is still exact and the
        # lane is free of any foreign live process, right now — not at a stale admission snapshot.
        # A blocked probe is a fail-closed ZERO launch that stays launch_owed (a later re-run
        # re-joins and resumes once authority is restored).
        if self._launch_authority is not None and not self._launch_authority(pin):
            return ActuationResult(
                status=ACTUATION_PRESERVATION_BLOCKED, phase=rec.phase, revision=rec.revision,
                stopped_on=pin.identity, detail="launch_authority_moved",
            )
        if self._port.launch_action_bound(rec.action_id, pin) != LAUNCH_DONE:
            # Stay launch_owed (retryable): a later re-run relaunches, never re-closes.
            return ActuationResult(
                status=ACTUATION_EFFECT_FAILED, phase=rec.phase, revision=rec.revision,
                stopped_on=pin.identity, detail="launch",
            )
        cas = self._store.transition_participant(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            identity=pin.identity, target=PARTICIPANT_VERIFY_OWED, holder=holder, now=now,
        )
        return self._cas_terminal(cas, rec, pin)

    def _step_verify_owed(self, key, rec, pin, holder, gen, now) -> Optional[ActuationResult]:
        verdict = self._port.verify_attestation(rec.action_id, pin)
        if verdict == ATTEST_MISMATCH:
            # Attested but not to THIS replacement action — zero completion (j#78384 §4).
            return ActuationResult(
                status=ACTUATION_ATTESTATION_MISMATCH, phase=rec.phase,
                revision=rec.revision, stopped_on=pin.identity,
            )
        if not attestation_completes(verdict):
            # Still booting (pending): yield and let a later re-run retry from verify_owed.
            return ActuationResult(
                status=ACTUATION_IN_PROGRESS, phase=rec.phase, revision=rec.revision,
                stopped_on=pin.identity, detail="attestation pending",
            )
        # The relaunch is now VERIFIED, so this is the one moment the update evidence that
        # armed it may be discharged: earlier and a crash loses the evidence while the launch
        # never happened (j#96966 C15); later and the transaction is complete with the
        # evidence still live for the next recovery to re-arm from (j#97131). A legacy /
        # generic participant carries no evidence and takes the pre-#14741 path untouched --
        # no port call, no re-authentication, same effect order.
        if _pin_carries_evidence(pin):
            fresh = self._reauth_before_effect(
                key, holder, gen, pin.identity, PARTICIPANT_VERIFY_OWED
            )
            if isinstance(fresh, ActuationResult):
                return fresh
            if self._evidence_completion is None:
                # An evidenceful participant reaching an actuator with no completion port is
                # a composition error, and it fails closed: staying `verify_owed` is
                # replayable, discharging nothing is not recoverable from.
                return ActuationResult(
                    status=ACTUATION_EFFECT_FAILED, phase=fresh.phase,
                    revision=fresh.revision, stopped_on=pin.identity,
                    detail="no update-evidence completion port is wired",
                )
            outcome = _completion_outcome(
                self._evidence_completion, key, pin, rec.action_id
            )
            if outcome not in (CONSUME_OK, CONSUME_REPLAY):
                # `replay` is this SAME replacement action having already consumed it, which
                # is exactly what a crash between the consume and the CAS below looks like.
                # Everything else -- absent, foreign consumer, a refusal token, an unknown
                # answer -- stays owed with zero launch and zero CAS.
                return ActuationResult(
                    status=ACTUATION_EFFECT_FAILED, phase=fresh.phase,
                    revision=fresh.revision, stopped_on=pin.identity,
                    detail=COMPLETION_FAILURE_DETAIL[outcome],
                )
            rec, now = fresh, self._clock()
        cas = self._store.transition_participant(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            identity=pin.identity, target=PARTICIPANT_REPLACED, holder=holder, now=now,
        )
        return self._cas_terminal(cas, rec, pin)

    # -- CAS helpers ---------------------------------------------------------

    def _reauth_before_effect(
        self, key, holder, gen, pin_identity, expected_phase
    ) -> "ReplacementTransactionRecord | ActuationResult":
        """Re-authenticate authority immediately before a destructive effect (R1-F1 / R2-F1).

        Re-reads the row on a *fresh* clock and renews the lease — a tranche A CAS that
        succeeds ONLY for the current live holder at the matching immutable generation, and
        which also refreshes the TTL so the effect runs inside a fresh window. Then re-reads
        once more and re-verifies **full authority** on the fresh row, not just the
        participant phase (R2-F1): a foreign claim slipping in between the renew and this read
        bumps the row's ``revision`` off the value the renew established and moves the holder,
        which a phase-only check would miss. Fail-closed unless the fresh row still has
        ``action_generation == gen``, ``revision == renew.revision`` (no intervening write),
        ``lease_holder == holder``, ``lease_is_live(now)``, and the participant at
        ``expected_phase``.

        Returns the renewed record to actuate against, or a fail-closed
        :class:`ActuationResult` (``lease_lost`` / ``generation_mismatch`` / ``not_found`` /
        ``effect_failed``) — in which case the caller performs ZERO effect.
        """
        now = self._clock()
        current = self._store.get(key)
        if current is None:
            return ActuationResult(status=ACTUATION_NOT_FOUND, stopped_on=pin_identity)
        if current.action_generation != gen:
            return ActuationResult(
                status=ACTUATION_GENERATION_MISMATCH, phase=current.phase,
                revision=current.revision, stopped_on=pin_identity,
            )
        renew = self._store.renew(
            key, expected_revision=current.revision, expected_action_generation=gen,
            holder=holder, lease_expires_at=self._expiry(now), now=now,
        )
        if not renew.applied:
            status = (
                ACTUATION_GENERATION_MISMATCH
                if renew.reason == CAS_GENERATION_MISMATCH
                else ACTUATION_LEASE_LOST
            )
            return ActuationResult(
                status=status, revision=renew.revision, stopped_on=pin_identity,
                detail=renew.reason,
            )
        fresh = self._store.get(key)
        if fresh is None:
            return ActuationResult(status=ACTUATION_NOT_FOUND, stopped_on=pin_identity)
        if fresh.action_generation != gen:
            return ActuationResult(
                status=ACTUATION_GENERATION_MISMATCH, phase=fresh.phase,
                revision=fresh.revision, stopped_on=pin_identity,
                detail="generation moved after renew",
            )
        # Liveness is evaluated at a FRESH clock reading taken here — not the pre-renew
        # ``now`` (R3-F1). The renew set the expiry to ``now + ttl``, so ``lease_is_live(now)``
        # is tautologically true and could never detect a lease that expired while the fresh
        # read above was in flight. ``effect_now`` is the moment we re-authorize the effect.
        effect_now = self._clock()
        if (
            fresh.revision != renew.revision
            or fresh.lease_holder != holder
            or not fresh.lease_is_live(effect_now)
        ):
            # A write (a foreign claim) landed between the renew and this read: the revision
            # is off the one renew established and/or the holder / liveness moved. Zero effect.
            return ActuationResult(
                status=ACTUATION_LEASE_LOST, phase=fresh.phase, revision=fresh.revision,
                stopped_on=pin_identity, detail="lease authority moved after renew",
            )
        pin = fresh.find_participant(pin_identity)
        if pin is None or pin.phase != expected_phase:
            return ActuationResult(
                status=ACTUATION_EFFECT_FAILED, phase=fresh.phase,
                revision=fresh.revision, stopped_on=pin_identity,
                detail="participant owed phase moved before effect",
            )
        return fresh

    def _advance_phase(
        self, key, rec, target, holder, gen, now
    ) -> Optional[ActuationResult]:
        cas = self._store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=target, holder=holder, now=now,
        )
        return self._cas_terminal(cas, rec, None)

    def _cas_terminal(self, cas, rec, pin) -> Optional[ActuationResult]:
        """Map a tranche A CAS outcome to a terminal result, or ``None`` to continue.

        ``applied`` (progress) and a benign ``stale_revision`` (a fresh re-read next
        iteration) continue the drive; a lost lease or a superseded generation are terminal
        fail-closed stops. A ``forbidden_transition`` here is a driver invariant break (the
        driver only ever requests legal edges) — reported fail-closed rather than looped.
        """
        if cas.applied:
            return None
        identity = pin.identity if pin is not None else None
        if cas.reason == CAS_LEASE_NOT_HELD:
            return ActuationResult(
                status=ACTUATION_LEASE_LOST, revision=cas.revision,
                stopped_on=identity, detail=cas.reason,
            )
        if cas.reason == CAS_GENERATION_MISMATCH:
            return ActuationResult(
                status=ACTUATION_GENERATION_MISMATCH, revision=cas.revision,
                stopped_on=identity, detail=cas.reason,
            )
        if cas.reason == CAS_STALE_REVISION:
            # A concurrent write moved the row; re-read on the next iteration and retry.
            return None
        return ActuationResult(
            status=ACTUATION_EFFECT_FAILED, revision=cas.revision,
            stopped_on=identity, detail=cas.reason,
        )

    def _expiry(self, now: str) -> str:
        base = datetime.fromisoformat(now)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        return (base + timedelta(seconds=self._ttl)).isoformat(timespec="seconds")


__all__ = (
    "ActuationResult",
    "ReplacementActuatorUseCase",
    "DEFAULT_LEASE_TTL_SECONDS",
)
