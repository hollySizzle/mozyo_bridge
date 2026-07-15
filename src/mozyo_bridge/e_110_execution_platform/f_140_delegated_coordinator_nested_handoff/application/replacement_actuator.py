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

from mozyo_bridge.core.state.replacement_preservation import assess_preservation
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
    PHASE_PLANNED,
    PHASE_REPLACING_NONSELF,
    PHASE_SELF_CLOSE_ARMED,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionRecord,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (  # noqa: E501
    ExactGenerationActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_AMBIGUOUS,
    ACTUATION_ARMED,
    ACTUATION_ATTESTATION_MISMATCH,
    ACTUATION_EFFECT_FAILED,
    ACTUATION_GENERATION_MISMATCH,
    ACTUATION_IN_PROGRESS,
    ACTUATION_LEASE_LOST,
    ACTUATION_NOT_FOUND,
    ACTUATION_PRESERVATION_BLOCKED,
    ATTEST_MISMATCH,
    CLOSE_DONE,
    LAUNCH_DONE,
    attestation_completes,
    bounded_recovery_available,
    is_zero_actuation_observation,
    new_close_required,
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


class ReplacementActuatorUseCase:
    """Drive a replacement transaction's non-self participants and arm it (tranche B)."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        port: ExactGenerationActuatorPort,
        *,
        clock: Callable[[], str] = _utc_now,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._port = port
        self._clock = clock
        self._ttl = lease_ttl_seconds

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
        pending = sorted(
            (
                p
                for p in rec.participants
                if not p.is_self and p.phase != PARTICIPANT_REPLACED
            ),
            key=lambda p: p.identity,
        )
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
        if pin.phase == PARTICIPANT_CLOSE_OWED:
            return self._step_close_owed(key, rec, pin, holder, gen, now)
        if pin.phase == PARTICIPANT_LAUNCH_OWED:
            return self._step_launch_owed(key, rec, pin, holder, gen, now)
        return self._step_verify_owed(key, rec, pin, holder, gen, now)

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
            verdict = assess_preservation(self._port.observe_preservation(pin))
            if verdict.blocked:
                return ActuationResult(
                    status=ACTUATION_PRESERVATION_BLOCKED, phase=rec.phase,
                    revision=rec.revision, stopped_on=pin.identity,
                    preservation_reasons=verdict.reasons, detail=verdict.detail,
                )
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
        # A launch of an already-closed slot is bounded recovery — no preservation gate. The
        # launch is bound to the replacement action id (j#78384 §4).
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
        cas = self._store.transition_participant(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            identity=pin.identity, target=PARTICIPANT_REPLACED, holder=holder, now=now,
        )
        return self._cas_terminal(cas, rec, pin)

    # -- CAS helpers ---------------------------------------------------------

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
