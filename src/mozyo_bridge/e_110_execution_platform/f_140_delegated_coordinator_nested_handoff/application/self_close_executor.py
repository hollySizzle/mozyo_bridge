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

It performs NO second close primitive and NO fresh-coordinator claim: after the self is
``replaced`` and the lease released, a fresh action-attested coordinator claims and drains
(:mod:`...fresh_coordinator_drain`). A self-close-then-crash or a missing fresh coordinator
is recovered by re-running from the durable owed state; unknown / ambiguous / recycled /
newer-authority observations are zero additional close (all enforced by the reused tranche B
steps).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mozyo_bridge.core.state.replacement_transaction import ReplacementTransactionStore
from mozyo_bridge.core.state.replacement_transaction_model import (
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionRecord,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    ActuationResult,
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_ARMED,
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
        actuator: ReplacementActuatorUseCase,
        seal_port: SelfCloseSealPort,
    ) -> None:
        self._store = store
        self._actuator = actuator
        self._seal_port = seal_port

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
        # Action-time seal re-verify (pure decision over live observations). Zero effect on
        # any failing seal — the executor never closes without every seal (j#78384 §2/§3).
        observation = self._seal_port.observe_self_close_seals(rec, self_pin)
        verdict = decide_self_close(observation)
        if verdict != SELF_CLOSE_MAY_PROCEED:
            return SelfCloseResult(
                status=SELF_CLOSE_BLOCKED, phase=rec.phase, revision=rec.revision,
                blocked_reason=verdict,
            )
        # Every seal held — drive the self participant to replaced via the reused tranche B
        # actuator (exact old-generation close + action-bound fresh launch + attestation
        # verify, with the R1/R2/R3 lease fences). The actuator releases the lease when done.
        outcome: ActuationResult = self._actuator.drive_self_participant(
            key, holder=holder, expected_action_generation=expected_action_generation
        )
        after = self._store.get(key)
        if outcome.status == ACTUATION_ARMED:
            return SelfCloseResult(
                status=SELF_CLOSE_REPLACED,
                phase=after.phase if after else "",
                revision=after.revision if after else 0,
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
