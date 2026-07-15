"""Process-external self-close executor (Redmine #13806 tranche C).

The victim coordinator arms its self-close and yields (tranche B stops at
``self_close_armed``); THIS runs *outside* the victim process (never a synchronous
self-kill, j#78384 §2 mandatory safety / Verdict j#78406) to actually replace the current
coordinator. It re-verifies the action-time seals — transaction phase, exact action
generation, the old coordinator's pinned identity, turn-ended + idle, no pending composer,
the preservation seal, and the continuation seal (:func:`decide_self_close`) — and only
then drives the self participant ``close_owed -> launch_owed -> verify_owed -> replaced``
by reusing the tranche B actuator's :meth:`...ReplacementActuatorUseCase.drive_self_participant`
(so the exact same evidence-gated close/launch, action-bound attestation, pre-effect lease
re-authentication, and CAS discipline apply — the old generation is closed, the fresh
coordinator launched + attested).

The self-specific seals are re-observed **at the destructive-close boundary**, not once
early (Redmine #13806 R1-F1): a :class:`_SelfSealAwareActuatorPort` re-runs
:func:`decide_self_close` inside the self participant's ``observe_preservation`` — the last
observation the tranche B close path makes right before the close — so a seal that regresses
(a pending composer appearing, a turn resuming) folds into a fail-closed preservation block
with zero close. This reuses the tranche B close path and its preservation gate; there is no
second close primitive.

It performs NO fresh-coordinator claim: after the self is ``replaced`` and the lease
released, a fresh action-attested coordinator claims and drains
(:mod:`...fresh_coordinator_drain`). A self-close-then-crash or a missing fresh coordinator
is recovered by re-running from the durable owed state; unknown / ambiguous / recycled /
newer-authority observations are zero additional close (all enforced by the reused tranche B
steps).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable

from mozyo_bridge.core.state.replacement_preservation import PreservationObservation
from mozyo_bridge.core.state.replacement_transaction import ReplacementTransactionStore
from mozyo_bridge.core.state.replacement_transaction_model import (
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionRecord,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    DEFAULT_LEASE_TTL_SECONDS,
    ActuationResult,
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (  # noqa: E501
    ExactGenerationActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_ARMED,
    ACTUATION_PRESERVATION_BLOCKED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.session_replacement_reconcile import (  # noqa: E501
    SELF_CLOSE_MAY_PROCEED,
    SelfCloseObservation,
    decide_self_close,
)

#: Statuses for a self-close executor run (a closed vocabulary).
SELF_CLOSE_REPLACED = "self_replaced"
SELF_CLOSE_BLOCKED = "blocked"
SELF_CLOSE_INVALID_TOPOLOGY = "invalid_topology"
SELF_CLOSE_GENERATION_MISMATCH = "generation_mismatch"
SELF_CLOSE_NOT_FOUND = "not_found"
#: The self-drive stopped fail-closed (lease lost / effect failed / attestation, etc.); the
#: underlying actuation status is carried in ``detail``.
SELF_CLOSE_ACTUATION_STOPPED = "actuation_stopped"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@runtime_checkable
class SelfCloseSealPort(Protocol):
    """The action-time seal observation the process-external executor needs (faked)."""

    def observe_self_close_seals(
        self, record: ReplacementTransactionRecord, self_pin: ParticipantPin
    ) -> SelfCloseObservation:
        """Gather the live seals for the self-close (phase / generation / old-coordinator
        identity / turn-ended / idle / no-pending-composer / preservation / continuation).
        """
        ...


class _SelfSealAwareActuatorPort:
    """Wraps an :class:`ExactGenerationActuatorPort` to re-verify the self seals at close.

    Every method delegates to the wrapped ``inner`` port EXCEPT the self participant's
    :meth:`observe_preservation` — the last observation the tranche B ``_step_close_owed``
    makes immediately before a new close. For the self participant it re-reads the durable
    row and re-runs :func:`decide_self_close`; a regressed seal returns a fail-closed
    :class:`PreservationObservation` (no positive evidence → blocked), so the tranche B
    preservation gate yields zero close and the participant stays ``close_owed`` (R1-F1). The
    failing seal is captured in :attr:`last_seal_block` for the durable record. Non-self
    participants (there are none in a self-drive, but defensively) are unaffected.
    """

    def __init__(
        self,
        inner: ExactGenerationActuatorPort,
        seal_port: SelfCloseSealPort,
        store: ReplacementTransactionStore,
        key: ReplacementTransactionKey,
        self_identity: tuple,
    ) -> None:
        self._inner = inner
        self._seal_port = seal_port
        self._store = store
        self._key = key
        self._self_identity = self_identity
        self.last_seal_block = ""

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        return self._inner.observe_old_slot(pin)

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        if pin.identity == self._self_identity:
            record = self._store.get(self._key)
            self_pin = (
                record.find_participant(self._self_identity)
                if record is not None
                else None
            )
            if record is None or self_pin is None:
                self.last_seal_block = "record_or_participant_missing"
                return PreservationObservation()  # fail closed
            verdict = decide_self_close(
                self._seal_port.observe_self_close_seals(record, self_pin)
            )
            if verdict != SELF_CLOSE_MAY_PROCEED:
                # A self seal regressed between the executor's initial check and this close
                # boundary — block the close fail-closed.
                self.last_seal_block = verdict
                return PreservationObservation()
        return self._inner.observe_preservation(pin)

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        return self._inner.close_exact_generation(pin)

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        return self._inner.launch_action_bound(action_id, pin)

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        return self._inner.verify_attestation(action_id, pin)


@dataclass(frozen=True)
class SelfCloseResult:
    """The outcome of one self-close executor run."""

    status: str
    phase: str = ""
    revision: int = 0
    blocked_reason: str = ""
    detail: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "revision": self.revision,
            "blocked_reason": self.blocked_reason,
            "detail": self.detail,
        }


class SelfCloseExecutorUseCase:
    """Re-verify seals, then replace the self coordinator via the tranche B actuator."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        actuation_port: ExactGenerationActuatorPort,
        seal_port: SelfCloseSealPort,
        *,
        clock: Callable[[], str] = _utc_now,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._actuation_port = actuation_port
        self._seal_port = seal_port
        self._clock = clock
        self._ttl = lease_ttl_seconds

    def run(
        self,
        key: ReplacementTransactionKey,
        *,
        holder: str,
        expected_action_generation: int,
    ) -> SelfCloseResult:
        """Verify the seals and replace the self coordinator, or stop fail-closed.

        ``holder`` is the executor's (process-external) lease identity — NOT the fresh
        coordinator's. On success the self participant is ``replaced``, the lease is released,
        and the transaction is ready for the fresh-coordinator claim.
        """
        rec = self._store.get(key)
        if rec is None:
            return SelfCloseResult(status=SELF_CLOSE_NOT_FOUND)
        if rec.action_generation != expected_action_generation:
            return SelfCloseResult(
                status=SELF_CLOSE_GENERATION_MISMATCH, phase=rec.phase,
                revision=rec.revision,
            )
        self_pins = [p for p in rec.participants if p.is_self]
        if len(self_pins) != 1:
            return SelfCloseResult(
                status=SELF_CLOSE_INVALID_TOPOLOGY, phase=rec.phase,
                revision=rec.revision,
                detail="exactly one self participant required",
            )
        self_pin = self_pins[0]
        # Fast-fail: the action-time seal re-verify (pure decision over live observations)
        # before any claim / effect. Zero effect on any failing seal (j#78384 §2/§3).
        verdict = decide_self_close(
            self._seal_port.observe_self_close_seals(rec, self_pin)
        )
        if verdict != SELF_CLOSE_MAY_PROCEED:
            return SelfCloseResult(
                status=SELF_CLOSE_BLOCKED, phase=rec.phase, revision=rec.revision,
                blocked_reason=verdict,
            )
        # Drive the self participant via the reused tranche B actuator, but through a
        # self-seal-aware port so the seals are RE-verified at the destructive-close boundary
        # (R1-F1) — a seal regressing after the fast-fail above still blocks the close.
        wrapped = _SelfSealAwareActuatorPort(
            self._actuation_port, self._seal_port, self._store, key, self_pin.identity
        )
        actuator = ReplacementActuatorUseCase(
            self._store, wrapped, clock=self._clock, lease_ttl_seconds=self._ttl
        )
        outcome: ActuationResult = actuator.drive_self_participant(
            key, holder=holder, expected_action_generation=expected_action_generation
        )
        after = self._store.get(key)
        if outcome.status == ACTUATION_ARMED:
            return SelfCloseResult(
                status=SELF_CLOSE_REPLACED,
                phase=after.phase if after else "",
                revision=after.revision if after else 0,
            )
        if (
            outcome.status == ACTUATION_PRESERVATION_BLOCKED
            and wrapped.last_seal_block
        ):
            # A self seal regressed at the close boundary — surfaced as a blocked self-close.
            return SelfCloseResult(
                status=SELF_CLOSE_BLOCKED,
                phase=after.phase if after else "",
                revision=after.revision if after else 0,
                blocked_reason=wrapped.last_seal_block,
            )
        return SelfCloseResult(
            status=SELF_CLOSE_ACTUATION_STOPPED,
            phase=after.phase if after else "",
            revision=after.revision if after else 0,
            detail=outcome.status,
        )


__all__ = (
    "SelfCloseSealPort",
    "SelfCloseResult",
    "SelfCloseExecutorUseCase",
    "SELF_CLOSE_REPLACED",
    "SELF_CLOSE_BLOCKED",
    "SELF_CLOSE_INVALID_TOPOLOGY",
    "SELF_CLOSE_GENERATION_MISMATCH",
    "SELF_CLOSE_NOT_FOUND",
    "SELF_CLOSE_ACTUATION_STOPPED",
)
