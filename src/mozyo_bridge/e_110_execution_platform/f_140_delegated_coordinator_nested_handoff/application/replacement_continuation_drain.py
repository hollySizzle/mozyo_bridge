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
    DRAIN_SEND_ZERO,
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
#: The rail PROVED it transmitted nothing and the attempt was reverted, so a re-run may send
#: (Redmine #14661 j#92656 F4). Operationally it shares the authority-moved revert path — both
#: are "reverted, re-sendable" — but they are different facts: one says the lane authority moved
#: under us, the other says the authority held and the rail declined to transmit. Reporting a
#: zero-send as ``authority_moved`` produced outcomes asserting ``launch_authority_reason=ok``
#: and ``resume_status=authority_moved`` at once, which cannot both be true.
CONTINUATION_ZERO_SEND_REVERTED = "zero_send_reverted"
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
    if fresh is None:
        return CONTINUATION_NOT_FOUND
    if fresh.action_generation != gen:
        return CONTINUATION_GENERATION_MISMATCH
    # Another same-action drive may confirm the ledger and complete the transaction after
    # this drive's attempted CAS but before its send.  Completion is the stronger durable
    # fact and proves this invocation owes ZERO transport; checking only generation/holder/
    # lease let a still-held completed row pass through and duplicate the delivery
    # (Redmine #14741 R7-F1).
    if fresh.phase == PHASE_COMPLETED:
        return CONTINUATION_CONFIRMED
    if fresh.phase != PHASE_DRAINING_CONTINUATION:
        return CONTINUATION_RELEASE_REFUSED
    effect_now = clock()
    if fresh.lease_holder != holder or not fresh.lease_is_live(effect_now):
        return CONTINUATION_LEASE_LOST
    # Re-join the exact live lane authority as the LAST external observation, AFTER the
    # attempted CAS + lease re-auth and IMMEDIATELY before the transport (#13806 R3-F1 /
    # j#82760). On a move the send provably has NOT happened — un-record the attempt.
    if not authority_fn():
        return _un_record_attempt(store, clock, key, holder=holder, gen=gen)
    # The authority observation above is external and may take long enough for the exact
    # continuation to land (and for another same-action drive to complete the transaction).
    # Re-join both durable facts AFTER that observation.  Keep the confirmation result local
    # until the transaction row has also been validated: ``confirmed`` is evidence that the
    # effect landed, not permission to complete a future generation or an unrelated phase
    # (Redmine #14741 R9-F2).
    confirmation_landed = confirmed_fn()
    fenced = _transport_fence(
        store, clock, key, holder=holder, gen=gen,
    )
    if fenced is not None:
        return fenced
    if confirmation_landed:
        return finalize_confirmed(store, clock, key, holder=holder, gen=gen)
    # Confirmation and the first transaction fence are themselves observations.  The lane
    # authority may move while either is being read, so re-join it again afterwards.  A move
    # still proves ZERO send and takes the same typed revert as the earlier authority fence
    # (Redmine #14741 R9-F1).
    if not authority_fn():
        return _un_record_attempt(store, clock, key, holder=holder, gen=gen)
    # The final transaction read closes completion / generation / phase / lease changes that
    # happened inside the last lane-authority observation.  After this point the shared
    # driver performs no other external observation before invoking the transport.
    fenced = _transport_fence(
        store, clock, key, holder=holder, gen=gen,
    )
    if fenced is not None:
        return fenced
    sent = send_fn()
    if sent == DRAIN_SEND_ZERO:
        # A PROVEN zero-send: the rail established that nothing was transmitted (its own typed
        # disposition, not an inference). That is the same fact the authority-moved branch above
        # acts on, so it takes the same revert — un-record the attempt so a re-run may send.
        # Collapsing it into the failure branch below leaves the attempt recorded forever and a
        # post-close transaction can then never resume its anchor (Redmine #14661 j#92601 F5).
        return _un_record_attempt(
            store, clock, key, holder=holder, gen=gen,
            reverted=CONTINUATION_ZERO_SEND_REVERTED,
        )
    if sent != DRAIN_SEND_OK:
        # Send outcome UNCERTAIN; the state stays attempted. A re-run re-checks the confirmation
        # and only completes if it confirms — never a blind resend.
        return CONTINUATION_SEND_FAILED
    if not confirmed_fn():
        # A concurrent same-generation drive may complete while the lossy confirmation read
        # returns false.  The stored terminal fact is stronger than that observation and must
        # be projected by the shared driver so every call site reports the same monotonic
        # outcome, not only wrappers which happen to add a final projection (#14741 R8-F2).
        completed = store.get(key)
        if (
            completed is not None
            and completed.action_generation == gen
            and completed.phase == PHASE_COMPLETED
        ):
            return CONTINUATION_CONFIRMED
        return CONTINUATION_UNCERTAIN
    return finalize_confirmed(store, clock, key, holder=holder, gen=gen)


def _un_record_attempt(
    store, clock, key, *, holder: str, gen: int,
    reverted: str = CONTINUATION_AUTHORITY_MOVED,
) -> str:
    """Un-record a PROVEN zero-send attempt, handling the release CAS outcome (j#82768).

    Each attempt re-reads the CURRENT row and classifies it into a typed disposition; a
    stale-revision refusal re-reads and retries under the bounded cap. Any unexpected
    concurrent phase / refusal reason reports :data:`CONTINUATION_RELEASE_REFUSED` (j#82782
    F1) — a distinct zero-send CAS-recovery failure, never the send-in-flight ``uncertain``
    and never a false re-sendable ``authority_moved``.

    ``reverted`` is the token to report once the revert succeeds: the CAS recovery is identical
    for both callers, but the FACT differs (the authority moved vs the rail proved a zero send),
    so the outcome must not claim the wrong one (#14661 j#92656 F4).
    """
    for _ in range(_UN_RECORD_RETRY_CAP):
        rec = store.get(key)
        if rec is None:
            return CONTINUATION_NOT_FOUND
        if rec.action_generation != gen:
            return CONTINUATION_GENERATION_MISMATCH
        if rec.phase == PHASE_REPLACING_NONSELF:
            return reverted  # re-sendable (reverted / never attempted)
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
            return reverted  # reverted -> re-sendable (the caller's own fact)
        if out.reason == CAS_GENERATION_MISMATCH:
            return CONTINUATION_GENERATION_MISMATCH
        if out.reason == CAS_LEASE_NOT_HELD:
            return CONTINUATION_LEASE_LOST
        if out.reason not in (CAS_STALE_REVISION, CAS_NOT_FOUND):
            return CONTINUATION_RELEASE_REFUSED
        # CAS_STALE_REVISION / CAS_NOT_FOUND: a concurrent write moved the row — re-read + retry.
    return CONTINUATION_RELEASE_REFUSED  # cap exhausted (a lease-held revert converges quickly)


def _transport_fence(store, clock, key, *, holder: str, gen: int):
    """Return a typed stop for a non-sendable row, or ``None`` when transport may proceed."""

    rec = store.get(key)
    if rec is None:
        return CONTINUATION_NOT_FOUND
    if rec.action_generation != gen:
        return CONTINUATION_GENERATION_MISMATCH
    if rec.phase == PHASE_COMPLETED:
        return CONTINUATION_CONFIRMED
    if rec.phase != PHASE_DRAINING_CONTINUATION:
        return CONTINUATION_RELEASE_REFUSED
    now = clock()
    if rec.lease_holder != holder or not rec.lease_is_live(now):
        return CONTINUATION_LEASE_LOST
    return None


def finalize_confirmed(store, clock, key, *, holder: str, gen: int) -> str:
    """Advance a confirmed transaction to ``completed`` with ZERO send (idempotent).

    Reached only when the continuation's durable effect has landed. Advances
    ``replacing_nonself -> draining_continuation -> completed`` as needed and releases the
    lease — never issues a send, so it can never duplicate the dispatch.
    """
    # A confirmation can race with generation supersede, lease movement, or another phase
    # writer.  Re-read on every transition and accept only this generation's two legal
    # continuation phases (plus its already-completed terminal).  The bounded retry handles a
    # benign stale revision without ever synthesising ``confirmed`` from an unverified row.
    for _ in range(_UN_RECORD_RETRY_CAP):
        rec = store.get(key)
        status = _confirmed_record_status(rec, clock, holder=holder, gen=gen)
        if status is not None:
            if status != CONTINUATION_CONFIRMED:
                return status
            release_lease(store, clock, key, gen=gen, holder=holder)
            final = store.get(key)
            if final is None:
                return CONTINUATION_NOT_FOUND
            if final.action_generation != gen:
                return CONTINUATION_GENERATION_MISMATCH
            if final.phase != PHASE_COMPLETED:
                return CONTINUATION_RELEASE_REFUSED
            return CONTINUATION_CONFIRMED
        target = (
            PHASE_DRAINING_CONTINUATION
            if rec.phase == PHASE_REPLACING_NONSELF
            else PHASE_COMPLETED
        )
        outcome = store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=target, holder=holder, now=clock(),
        )
        if outcome.applied or outcome.reason == CAS_STALE_REVISION:
            continue
        if outcome.reason == CAS_NOT_FOUND:
            return CONTINUATION_NOT_FOUND
        if outcome.reason == CAS_GENERATION_MISMATCH:
            return CONTINUATION_GENERATION_MISMATCH
        if outcome.reason == CAS_LEASE_NOT_HELD:
            return CONTINUATION_LEASE_LOST
        return CONTINUATION_RELEASE_REFUSED
    return CONTINUATION_RELEASE_REFUSED


def _confirmed_record_status(rec, clock, *, holder: str, gen: int):
    """Classify one fresh row for confirmed finalization; ``None`` means legal progress."""

    if rec is None:
        return CONTINUATION_NOT_FOUND
    if rec.action_generation != gen:
        return CONTINUATION_GENERATION_MISMATCH
    if rec.phase == PHASE_COMPLETED:
        return CONTINUATION_CONFIRMED
    if rec.phase not in (PHASE_REPLACING_NONSELF, PHASE_DRAINING_CONTINUATION):
        return CONTINUATION_RELEASE_REFUSED
    now = clock()
    if rec.lease_holder != holder or not rec.lease_is_live(now):
        return CONTINUATION_LEASE_LOST
    return None


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
    "CONTINUATION_ZERO_SEND_REVERTED",
    "CONTINUATION_RELEASE_REFUSED",
    "drive_continuation_once",
    "finalize_confirmed",
    "release_lease",
)
