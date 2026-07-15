"""Fresh-coordinator claim + continuation drain (Redmine #13806 tranche C).

After the process-external self-close executor has replaced the current coordinator (the
self participant is ``replaced`` and the transaction is armed at ``self_close_armed`` with
the lease released), a **fresh, action-attested** coordinator claims the transaction and
drains its continuation — the last leg of the "1 action generation = 1 durable replacement
transaction" flow (j#78384 §2 step 7, §3).

Only a fresh locator/revision carrying an action-bound startup attestation may claim
(:meth:`ContinuationDrainPort.verify_fresh_attestation`). The drain rides the EXISTING
transaction phases (``fresh_coordinator_claimed -> draining_continuation -> completed``) —
no second ledger (j#79121 scope 4) — with the ``not_attempted -> attempted -> confirmed |
uncertain`` state machine of :mod:`...domain.session_replacement_reconcile`. The DB holds
only the Redmine continuation pointer + the closed semantic token; the fresh coordinator
re-reads the source journal (via the port) to reconstruct the semantic workflow / outbox,
and NEVER blind-resends after an attempt — a resume at ``draining_continuation`` re-reads
the durable gate before deciding confirmed-vs-still-needed. All sends are high-level
workflow / outbox; no low-level read/message/type/keys, no raw transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol, runtime_checkable

from mozyo_bridge.core.state.replacement_transaction import ReplacementTransactionStore
from mozyo_bridge.core.state.replacement_transaction_model import (
    CAS_GENERATION_MISMATCH,
    CAS_LEASE_NOT_HELD,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_FRESH_COORDINATOR_CLAIMED,
    PHASE_SELF_CLOSE_ARMED,
    ContinuationPointer,
    ReplacementTransactionKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.session_replacement_reconcile import (  # noqa: E501
    DRAIN_ATTEMPTED,
    DRAIN_CONFIRMED,
    DRAIN_UNCERTAIN,
    drain_state_for,
    may_attempt_drain,
)

#: Default lease TTL the drain claims for (seconds).
DEFAULT_DRAIN_LEASE_TTL_SECONDS = 300

#: Drain-send outcomes the port reports (the send is high-level; landing is confirmed
#: separately by the durable gate, never assumed from the send).
DRAIN_SEND_OK = "sent"
DRAIN_SEND_ERROR = "error"

#: Terminal / yield statuses for a drain run (a closed vocabulary).
DRAIN_COMPLETED = "completed"
DRAIN_UNCERTAIN_STATUS = "uncertain"
DRAIN_NOT_READY = "not_ready"
DRAIN_ATTESTATION_FAILED = "attestation_failed"
DRAIN_LEASE_LOST = "lease_lost"
DRAIN_GENERATION_MISMATCH = "generation_mismatch"
DRAIN_NOT_FOUND = "not_found"
DRAIN_SEND_FAILED = "send_failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@runtime_checkable
class ContinuationDrainPort(Protocol):
    """The injected fresh-coordinator effects (faked in tests; live drain is non-scope)."""

    def verify_fresh_attestation(self, action_id: str, holder: str) -> bool:
        """Does ``holder`` carry an action-bound startup attestation for ``action_id``?

        Only an action-attested fresh locator/revision may claim (j#78384 §3). A normal
        name/role/lane identity is not proof of THIS replacement.
        """
        ...

    def drain_send(self, continuation: ContinuationPointer) -> str:
        """Issue the continuation's semantic action via the high-level workflow / outbox.

        Re-reads the source journal (``continuation``) to reconstruct the workflow and sends
        ONCE. Returns :data:`DRAIN_SEND_OK` / :data:`DRAIN_SEND_ERROR`. Landing is NOT implied
        by a successful send — the caller confirms it via :meth:`drain_gate_confirmed`.
        """
        ...

    def drain_gate_confirmed(self, continuation: ContinuationPointer) -> bool:
        """Has the continuation's semantic action landed on the durable gate / outbox?

        The idempotency check that lets a resume distinguish confirmed from still-needed
        without a blind resend.
        """
        ...


@dataclass(frozen=True)
class DrainResult:
    """The outcome of one fresh-coordinator drain run."""

    status: str
    phase: str = ""
    revision: int = 0
    drain_state: str = ""
    detail: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "revision": self.revision,
            "drain_state": self.drain_state,
            "detail": self.detail,
        }


class FreshCoordinatorDrainUseCase:
    """Claim as the fresh coordinator and drain the continuation (tranche C)."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        port: ContinuationDrainPort,
        *,
        clock: Callable[[], str] = _utc_now,
        lease_ttl_seconds: int = DEFAULT_DRAIN_LEASE_TTL_SECONDS,
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
    ) -> DrainResult:
        """Claim (action-attested) and drain, or stop fail-closed.

        ``holder`` is the fresh coordinator's action-bound identity token. Requires the self
        participant already ``replaced`` (the executor's job) so the transaction can enter
        ``fresh_coordinator_claimed``.
        """
        rec = self._store.get(key)
        if rec is None:
            return DrainResult(status=DRAIN_NOT_FOUND)
        if rec.action_generation != expected_action_generation:
            return DrainResult(
                status=DRAIN_GENERATION_MISMATCH, phase=rec.phase, revision=rec.revision
            )
        # Only an action-attested fresh identity may claim (j#78384 §3).
        if not self._port.verify_fresh_attestation(rec.action_id, holder):
            return DrainResult(
                status=DRAIN_ATTESTATION_FAILED, phase=rec.phase, revision=rec.revision,
                detail="fresh coordinator attestation does not bind the action",
            )
        gen = expected_action_generation
        # Already completed? Idempotent success (a re-run drains nothing).
        if rec.phase == PHASE_COMPLETED:
            return DrainResult(
                status=DRAIN_COMPLETED, phase=rec.phase, revision=rec.revision,
                drain_state=DRAIN_CONFIRMED,
            )
        now = self._clock()
        claim = self._store.claim(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            holder=holder, lease_expires_at=self._expiry(now), now=now,
        )
        if not claim.applied:
            status = (
                DRAIN_GENERATION_MISMATCH
                if claim.reason == CAS_GENERATION_MISMATCH
                else DRAIN_LEASE_LOST
            )
            return DrainResult(
                status=status, phase=rec.phase, revision=claim.revision,
                detail=claim.reason,
            )
        # Advance self_close_armed -> fresh_coordinator_claimed when the self is replaced.
        rec = self._store.get(key)
        if rec.phase == PHASE_SELF_CLOSE_ARMED:
            advance = self._store.transition_phase(
                key, expected_revision=rec.revision, expected_action_generation=gen,
                target=PHASE_FRESH_COORDINATOR_CLAIMED, holder=holder,
                now=self._clock(),
            )
            terminal = self._phase_terminal(advance, rec)
            if terminal is not None:
                return terminal
        return self._drain(key, holder, gen)

    # -- drain --------------------------------------------------------------

    def _drain(self, key, holder, gen) -> DrainResult:
        rec = self._store.get(key)
        if rec is None:
            return DrainResult(status=DRAIN_NOT_FOUND)
        continuation = rec.continuation
        if continuation is None:
            # The stored continuation pointer is unreadable — never drain blind.
            return DrainResult(
                status=DRAIN_NOT_READY, phase=rec.phase, revision=rec.revision,
                detail="continuation pointer unreadable",
            )
        # Resolve the durable drain state from the phase + a gate observation (idempotency).
        gate_confirmed = self._port.drain_gate_confirmed(continuation)
        state = drain_state_for(rec.phase, gate_confirmed=gate_confirmed)

        if state == DRAIN_CONFIRMED:
            # Confirmed on the gate; ensure the phase reflects completion (idempotent).
            if rec.phase == PHASE_DRAINING_CONTINUATION:
                done = self._store.transition_phase(
                    key, expected_revision=rec.revision,
                    expected_action_generation=gen, target=PHASE_COMPLETED,
                    holder=holder, now=self._clock(),
                )
                terminal = self._phase_terminal(done, rec)
                if terminal is not None:
                    return terminal
                rec = self._store.get(key)
            return DrainResult(
                status=DRAIN_COMPLETED, phase=rec.phase, revision=rec.revision,
                drain_state=DRAIN_CONFIRMED,
            )

        if not may_attempt_drain(state):
            # attempted / uncertain and the gate is NOT confirmed: a send may already be in
            # flight — do NOT blind-resend. Report uncertain; a later re-run re-checks the gate.
            return DrainResult(
                status=DRAIN_UNCERTAIN_STATUS, phase=rec.phase, revision=rec.revision,
                drain_state=DRAIN_UNCERTAIN,
                detail="drain attempted; gate not yet confirmed (no blind resend)",
            )

        # not_attempted: record `attempted` (phase -> draining_continuation) BEFORE the send
        # (j#78384 §2 "effect 前に次の owed state を CAS 記録"), then send, then confirm.
        attempt = self._store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=PHASE_DRAINING_CONTINUATION, holder=holder, now=self._clock(),
        )
        terminal = self._phase_terminal(attempt, rec)
        if terminal is not None:
            return terminal
        # Re-authenticate the lease right before the send (a live-holder CAS re-read), so a
        # lost lease yields zero send.
        fresh = self._store.get(key)
        effect_now = self._clock()
        if (
            fresh is None
            or fresh.action_generation != gen
            or fresh.lease_holder != holder
            or not fresh.lease_is_live(effect_now)
        ):
            return DrainResult(
                status=DRAIN_LEASE_LOST, phase=fresh.phase if fresh else "",
                revision=fresh.revision if fresh else 0,
                drain_state=DRAIN_ATTEMPTED, detail="lease lost before send",
            )
        if self._port.drain_send(continuation) != DRAIN_SEND_OK:
            # The send failed; the state stays `attempted`/draining_continuation. A re-run
            # re-checks the gate and only resends if the gate is still unconfirmed AND a fresh
            # not_attempted (it is not) — so this is `uncertain`, never a blind resend.
            return DrainResult(
                status=DRAIN_SEND_FAILED, phase=fresh.phase, revision=fresh.revision,
                drain_state=DRAIN_ATTEMPTED, detail="drain send failed",
            )
        # Confirm the send landed on the durable gate, then complete.
        if not self._port.drain_gate_confirmed(continuation):
            return DrainResult(
                status=DRAIN_UNCERTAIN_STATUS, phase=fresh.phase, revision=fresh.revision,
                drain_state=DRAIN_UNCERTAIN,
                detail="sent but not yet confirmed on the gate",
            )
        rec = self._store.get(key)
        done = self._store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=PHASE_COMPLETED, holder=holder, now=self._clock(),
        )
        terminal = self._phase_terminal(done, rec)
        if terminal is not None:
            return terminal
        rec = self._store.get(key)
        return DrainResult(
            status=DRAIN_COMPLETED, phase=rec.phase, revision=rec.revision,
            drain_state=DRAIN_CONFIRMED,
        )

    def _phase_terminal(self, outcome, rec) -> Optional[DrainResult]:
        """Map a phase-transition CAS outcome to a terminal DrainResult, or ``None`` to go on."""
        if outcome.applied:
            return None
        if outcome.reason == CAS_LEASE_NOT_HELD:
            return DrainResult(
                status=DRAIN_LEASE_LOST, revision=outcome.revision, detail=outcome.reason
            )
        if outcome.reason == CAS_GENERATION_MISMATCH:
            return DrainResult(
                status=DRAIN_GENERATION_MISMATCH, revision=outcome.revision,
                detail=outcome.reason,
            )
        # A forbidden transition here means the cross-axis prerequisite is unmet (e.g. the
        # self is not yet replaced for -> fresh_coordinator_claimed): not ready.
        return DrainResult(
            status=DRAIN_NOT_READY, revision=outcome.revision, detail=outcome.reason
        )

    def _expiry(self, now: str) -> str:
        base = datetime.fromisoformat(now)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        return (base + timedelta(seconds=self._ttl)).isoformat(timespec="seconds")


__all__ = (
    "ContinuationDrainPort",
    "DrainResult",
    "FreshCoordinatorDrainUseCase",
    "DEFAULT_DRAIN_LEASE_TTL_SECONDS",
    "DRAIN_SEND_OK",
    "DRAIN_SEND_ERROR",
    "DRAIN_COMPLETED",
    "DRAIN_UNCERTAIN_STATUS",
    "DRAIN_NOT_READY",
    "DRAIN_ATTESTATION_FAILED",
    "DRAIN_LEASE_LOST",
    "DRAIN_GENERATION_MISMATCH",
    "DRAIN_NOT_FOUND",
    "DRAIN_SEND_FAILED",
)
