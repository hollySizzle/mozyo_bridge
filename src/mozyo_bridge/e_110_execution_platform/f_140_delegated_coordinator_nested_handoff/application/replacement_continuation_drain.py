"""Shared exactly-once continuation-drain leg over a replacement transaction (Redmine #13806 / #14203).

The ONE authority for driving a recovered non-self replacement transaction's continuation
exactly once (``replacing_nonself -> draining_continuation -> completed``), extracted from the
stale-worker recovery (#13806 tranche D) so the gateway refresh (#14203) reuses the identical
CAS discipline instead of re-enumerating it (a second enumeration of the same rule is where
drift starts). The discipline, unchanged from its origin:

- **idempotency first**: a continuation whose durable effect already landed advances to
  ``completed`` with ZERO send — even from ``replacing_nonself`` — so the drive can never
  duplicate the dispatch;
- **record attempted BEFORE the send** (``-> draining_continuation``), so a crash resumes as
  uncertain rather than re-sending;
- **lease re-auth + action-time authority re-join immediately before the transport**; on an
  authority move the send provably has NOT happened, so the attempt is UN-recorded (a typed
  CAS-outcome-aware revert, j#82768 / j#82782 F1) rather than left mistaken for send-in-flight;
- **never blind-resend** past ``attempted``: an unconfirmed effect reports uncertain and a
  later re-run re-checks the durable confirmation.

Callers inject the three effect probes as callables — ``authority_fn`` (the action-time lane
authority re-join), ``send_fn`` (the one high-level send / resume invocation, returning
:data:`...fresh_coordinator_drain.DRAIN_SEND_OK` on success), and ``confirmed_fn`` (the fresh
durable confirmation read) — so this leaf holds only the transaction-CAS machinery and stays
pure of transports.
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import (
    CAS_GENERATION_MISMATCH,
    CAS_LEASE_NOT_HELD,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.session_replacement_reconcile import (  # noqa: E501
    drain_state_for,
    may_attempt_drain,
)

# -- continuation-drive status vocabulary (closed; the #13806 REDISPATCH_* literals) ------------

CONTINUATION_CONFIRMED = "confirmed"
CONTINUATION_UNCERTAIN = "uncertain"
CONTINUATION_SEND_FAILED = "send_failed"
CONTINUATION_LEASE_LOST = "lease_lost"
CONTINUATION_GENERATION_MISMATCH = "generation_mismatch"
CONTINUATION_NOT_FOUND = "not_found"
CONTINUATION_UNREADABLE = "continuation_unreadable"
#: The live lane authority moved between the launch and the send — a fail-closed ZERO send
#: re-joined action-time immediately before the transport (#13806 R3-F1 / j#82731). The phase
#: is reverted so a later re-run re-attempts exactly once; never a blind send into a lane the
#: approval no longer governs.
CONTINUATION_AUTHORITY_MOVED = "authority_moved"
#: A PROVEN zero-send attempt's revert CAS could not complete (#13806 R3 j#82782 F1) — a
#: concrete zero-send CAS-recovery failure, DISTINCT from the send-in-flight ``uncertain``.
CONTINUATION_RELEASE_REFUSED = "release_refused"

#: Bounded re-read + retry cap for un-recording a proven zero-send attempt whose revert CAS
#: was refused by a concurrent write (#13806 j#82768). A lease-held revert converges in one or
#: two iterations once the racing write settles; hitting the cap reports
#: :data:`CONTINUATION_RELEASE_REFUSED` (never a false ``authority_moved`` / ``uncertain``).
_UN_RECORD_RETRY_CAP = 8


def drive_continuation_once(
    store,
    clock,
    key,
    *,
    holder: str,
    gen: int,
    authority_fn,
    send_fn,
    confirmed_fn,
) -> str:
    """Drive the recovered transaction's continuation exactly once. (the #13806 discipline)

    ``authority_fn() -> bool`` re-joins the exact live lane authority as the LAST external
    observation immediately before the transport; ``send_fn() -> str`` performs the one
    high-level effect (``DRAIN_SEND_OK`` on success); ``confirmed_fn() -> bool`` freshly reads
    the durable confirmation. Returns a closed ``CONTINUATION_*`` token.
    """
    rec = store.get(key)
    if rec is None:
        return CONTINUATION_NOT_FOUND
    if rec.continuation is None:
        return CONTINUATION_UNREADABLE
    # Idempotency FIRST: if the continuation's durable effect has already landed (a prior send
    # that could not be confirmed, or an out-of-band dispatch), advance to completion with ZERO
    # send — even from ``replacing_nonself``. This is what makes the drive exactly-once.
    if confirmed_fn():
        return finalize_confirmed(store, clock, key, holder=holder, gen=gen)
    state = drain_state_for(rec.phase, gate_confirmed=False)
    if not may_attempt_drain(state):
        # attempted / uncertain and the effect is NOT confirmed — a send may be in flight.
        # Report uncertain; a later re-run re-checks. Never blind-resend.
        return CONTINUATION_UNCERTAIN
    # not_attempted (phase replacing_nonself): record attempted (-> draining_continuation)
    # BEFORE the send, so a crash here resumes as uncertain rather than re-sending.
    attempt = store.transition_phase(
        key, expected_revision=rec.revision, expected_action_generation=gen,
        target=PHASE_DRAINING_CONTINUATION, holder=holder, now=clock(),
    )
    terminal = _terminal(attempt)
    if terminal is not None:
        return terminal
    # Re-authenticate the lease immediately before the send (a live-holder CAS re-read on a
    # fresh clock) — a lost lease yields ZERO send.
    fresh = store.get(key)
    effect_now = clock()
    if (
        fresh is None
        or fresh.action_generation != gen
        or fresh.lease_holder != holder
        or not fresh.lease_is_live(effect_now)
    ):
        return CONTINUATION_LEASE_LOST
    # Re-join the exact live lane authority as the LAST external observation, AFTER the
    # attempted CAS + lease re-auth and IMMEDIATELY before the transport (#13806 R3-F1 /
    # j#82760). On a move the send provably has NOT happened — un-record the attempt.
    if not authority_fn():
        return _un_record_attempt(store, clock, key, holder=holder, gen=gen)
    if send_fn() != DRAIN_SEND_OK:
        # Send failed; the state stays attempted. A re-run re-checks the confirmation and only
        # completes if it confirms — never a blind resend.
        return CONTINUATION_SEND_FAILED
    if not confirmed_fn():
        return CONTINUATION_UNCERTAIN
    return finalize_confirmed(store, clock, key, holder=holder, gen=gen)


def _un_record_attempt(store, clock, key, *, holder: str, gen: int) -> str:
    """Un-record a PROVEN zero-send attempt, handling the release CAS outcome (j#82768).

    Each attempt re-reads the CURRENT row and classifies it into a typed disposition; a
    stale-revision refusal re-reads and retries under the bounded cap. Any unexpected
    concurrent phase / refusal reason reports :data:`CONTINUATION_RELEASE_REFUSED` (j#82782
    F1) — a distinct zero-send CAS-recovery failure, never the send-in-flight ``uncertain``
    and never a false re-sendable ``authority_moved``.
    """
    for _ in range(_UN_RECORD_RETRY_CAP):
        rec = store.get(key)
        if rec is None:
            return CONTINUATION_NOT_FOUND
        if rec.action_generation != gen:
            return CONTINUATION_GENERATION_MISMATCH
        if rec.phase == PHASE_REPLACING_NONSELF:
            return CONTINUATION_AUTHORITY_MOVED  # re-sendable (reverted / never attempted)
        if rec.phase == PHASE_COMPLETED:
            return CONTINUATION_CONFIRMED  # a concurrent holder dispatched + drained
        if rec.phase != PHASE_DRAINING_CONTINUATION:
            # An unexpected concurrent phase (a self flow / mid-transition); never claim
            # re-sendable — a distinct zero-send recovery failure, not send-in-flight uncertain.
            return CONTINUATION_RELEASE_REFUSED
        now = clock()
        if rec.lease_holder != holder or not rec.lease_is_live(now):
            return CONTINUATION_LEASE_LOST
        out = store.release_drain_attempt(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            holder=holder, now=now,
        )
        if out.applied:
            return CONTINUATION_AUTHORITY_MOVED  # reverted -> re-sendable
        if out.reason == CAS_GENERATION_MISMATCH:
            return CONTINUATION_GENERATION_MISMATCH
        if out.reason == CAS_LEASE_NOT_HELD:
            return CONTINUATION_LEASE_LOST
        if out.reason not in (CAS_STALE_REVISION, CAS_NOT_FOUND):
            return CONTINUATION_RELEASE_REFUSED
        # CAS_STALE_REVISION / CAS_NOT_FOUND: a concurrent write moved the row — re-read + retry.
    return CONTINUATION_RELEASE_REFUSED  # cap exhausted (a lease-held revert converges quickly)


def finalize_confirmed(store, clock, key, *, holder: str, gen: int) -> str:
    """Advance a confirmed transaction to ``completed`` with ZERO send (idempotent).

    Reached only when the continuation's durable effect has landed. Advances
    ``replacing_nonself -> draining_continuation -> completed`` as needed and releases the
    lease — never issues a send, so it can never duplicate the dispatch.
    """
    rec = store.get(key)
    if rec is None:
        return CONTINUATION_NOT_FOUND
    if rec.phase == PHASE_REPLACING_NONSELF:
        attempt = store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=PHASE_DRAINING_CONTINUATION, holder=holder, now=clock(),
        )
        terminal = _terminal(attempt)
        if terminal is not None:
            return terminal
        rec = store.get(key)
    if rec is not None and rec.phase == PHASE_DRAINING_CONTINUATION:
        done = store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=PHASE_COMPLETED, holder=holder, now=clock(),
        )
        terminal = _terminal(done)
        if terminal is not None:
            return terminal
    release_lease(store, clock, key, gen=gen, holder=holder)
    return CONTINUATION_CONFIRMED


def _terminal(outcome):
    if outcome.applied:
        return None
    if outcome.reason == CAS_LEASE_NOT_HELD:
        return CONTINUATION_LEASE_LOST
    if outcome.reason == CAS_GENERATION_MISMATCH:
        return CONTINUATION_GENERATION_MISMATCH
    # A benign stale revision (a concurrent read moved the row) — a re-run re-reads; report
    # uncertain rather than assume the send state.
    return CONTINUATION_UNCERTAIN


def release_lease(store, clock, key, *, gen: int, holder: str) -> None:
    rec = store.get(key)
    if rec is None or rec.lease_holder != holder:
        return
    store.release(
        key, expected_revision=rec.revision, expected_action_generation=gen,
        holder=holder, now=clock(),
    )


__all__ = (
    "CONTINUATION_CONFIRMED",
    "CONTINUATION_UNCERTAIN",
    "CONTINUATION_SEND_FAILED",
    "CONTINUATION_LEASE_LOST",
    "CONTINUATION_GENERATION_MISMATCH",
    "CONTINUATION_NOT_FOUND",
    "CONTINUATION_UNREADABLE",
    "CONTINUATION_AUTHORITY_MOVED",
    "CONTINUATION_RELEASE_REFUSED",
    "drive_continuation_once",
    "finalize_confirmed",
    "release_lease",
)
