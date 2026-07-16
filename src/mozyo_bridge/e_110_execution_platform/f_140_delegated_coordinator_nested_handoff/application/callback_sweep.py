"""Callback sweep use case: derive the verdict, serialize the attempt, send at most once (#13889).

The sweep answers one question — *is this lane actually stalled?* — and, only if it is, performs one
recovery notification. Both halves are durable acts, so the order is the design:

    acquire attempt lease
      -> decision read      (anchored, ordered watermark; the verdict is DERIVED, never asserted)
      -> boundary re-read   (a gate landing here folds to a first-pass zero-send resolution)
      -> publish the record (durable; the notification will point at it)
      -> final live read    (immediately before the send)
      -> reserve the send fence -> send -> mark delivered
    release the lease

**Three authorities, deliberately separate** (reviews R5-F1 / R6-F1 / R9-F1):

- :class:`...callback_sweep_lease.CallbackSweepLease` serializes the **attempt**. It is the one that
  may be *held* across slow Redmine I/O, because its rows name their owner: a loser is passive, and
  release is owner-conditional. :class:`...dispatch_outbox_fence.DispatchOutboxFence` cannot play
  that role — its contract assumes an instantaneous reservation and reads a lingering ``reserved``
  row as crash residue, so holding one corrupts a live owner and blocks the anchor forever.
- :class:`...callback_publication_fence.CallbackPublicationFence` serializes the **record**, keyed
  by its exact identity. It has no TTL and is never reclaimed, because a reclaimable reservation is
  no reservation at all against a slow owner (R9-F1). See :mod:`.callback_recovery_record`.
- the outbox fence still serializes the **send**, reserved immediately around it, exactly the short
  way its contract supports.

**Acquiring is not owning.** The lease has a TTL, so an owner that is merely *slow* — a few Redmine
round-trips — can outlive it and be reclaimed while still running. Every durable act therefore
re-verifies ownership at the authority first (:meth:`CallbackSweepLease.owns`), and a lost lease
publishes nothing and sends nothing. But that check is not itself a guarantee: an owner can pass it
and *then* be reclaimed mid-write, so neither durable act is gated by the lease alone — each has its
own never-reclaimed fence, and the lease only decides who gets to try.

**Every guard here was, at some revision, present but ineffective.** That is what the shape encodes:

- a **re-read** only guards if the source can return new data. A frozen snapshot re-reads to the
  identical payload, so mutation requires a source that positively declares
  :func:`source_is_fresh` (R2-F1);
- a **fence** only guards if its key is real. The key is workspace-partitioned, so an unattested id
  reserves a different row and the same recovery sends twice (R2-F2);
- a **record** must precede the pointer send, or the notification is a silent re-poke the workflow
  contract prohibits outright (R2-F3);
- **all** publications must be serialized, not just the stall record — zero-send resolutions are the
  common case and were duplicating (R5-F2);
- the store itself must be **identity-pinned**: a deleted / recreated lease DB otherwise hands a
  second live owner the same anchor (R6-F2).

**Documented residual** (coordinator disposition j#80302, option (b)+(c)): Redmine offers no CAS, so
the window between the final live read and the transport call cannot be closed by the sender alone.
It is narrowed to read->send and accepted as a documented residual — this covers the whole #13883
evidence, which is seconds to minutes. True receiver-side exactly-once is Redmine **#13910** and is
explicitly NOT claimed here.

Everything degrades to **zero-send**: an unreadable or non-fresh source, an unattested workspace, an
unavailable or lost lease, an unwritable record, an unbootstrapped fence, or a lost reserve. A sweep
that cannot prove a stall does not act on one.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mozyo_bridge.core.state.callback_publication_fence import (
    PUBLICATION_PUBLISHED,
    CallbackPublicationFence,
    PublicationKey,
)
from mozyo_bridge.core.state.callback_sweep_lease import (
    CallbackSweepLease,
    CallbackSweepLeaseError,
    LeaseKey,
)
from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    DispatchOutboxFenceError,
    FenceKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
    SEND_RESERVED,
    SWEEP_RECOVERY_ACTION_ID,
    SWEEP_STATE_STALL_UNPROVABLE,
    _journal_int,
    render_sweep_record_note,
    sweep_record_journals,
    ZERO_SEND_FENCE_HELD,
    ZERO_SEND_FENCE_UNAVAILABLE,
    ZERO_SEND_PROGRESS_LANDED,
    ZERO_SEND_STALL_UNPROVABLE,
    RecoveryDecision,
    SweepWatermark,
    classify_sweep,
    decide_recovery,
    resolve_watermark,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    dispatch_generations,
    resolve_dispatch_entry_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    STATE_NO_PROGRESS_AFTER_HANDOFF,
    STATE_PROGRESS_WITHOUT_CALLBACK,
)

#: The sweep could not read the durable record at all -> no verdict, no mutation (fail-closed).
SWEEP_SOURCE_UNREADABLE = "source_unreadable"
#: No ``send_fn`` was supplied: the sweep classified only and reserved nothing.
SWEEP_READ_ONLY = "read_only"
#: The source cannot promise a FRESH read per call, so the pre-mutation re-read would be a no-op
#: and the TOCTOU window would stay open (review R2-F1). Classification is still fine; mutation is
#: refused.
ZERO_SEND_SOURCE_NOT_FRESH = "source_not_fresh"
#: No attested workspace identity, so the fence key would be partition-ambiguous (review R2-F2).
ZERO_SEND_WORKSPACE_UNATTESTED = "workspace_unattested"
#: The durable recovery record could not be written / resolved, so a send would be a silent
#: re-poke (review R2-F3).
ZERO_SEND_RECORD_FAILED = "recovery_record_failed"
#: Another sweep owns this attempt's lease. The loser stands down PASSIVELY (review R5-F1): it does
#: not touch the owner's state, so a live owner is never mistaken for a crash.
ZERO_SEND_ATTEMPT_HELD = "attempt_held"
#: The attempt lease store is unusable, so the attempt would not be serialized -> no mutation.
ZERO_SEND_LEASE_UNAVAILABLE = "attempt_lease_unavailable"
#: The attempt lease was LOST mid-attempt (TTL expired and another owner reclaimed it, or the store
#: was replaced). Publication and send both stop (review R6-F1/F2): an owner that no longer owns the
#: anchor must not write a durable record or send, because the new owner will.
ZERO_SEND_OWNERSHIP_LOST = "attempt_ownership_lost"
#: This exact record is already reserved / uncertain / published by someone. Never republished.
ZERO_SEND_PUBLICATION_HELD = "publication_held"
#: A record PUT of unknown fate. Never auto-retried; an operator reconciles against Redmine.
ZERO_SEND_PUBLICATION_UNCERTAIN = "publication_uncertain"


def source_is_fresh(source: object) -> bool:
    """True only when ``source`` promises a genuinely fresh durable read on EVERY call.

    Review R2-F1. :func:`sweep_once` re-reads before mutating, but a re-read is only a guard if the
    source can actually return newer data. A snapshot source (an already-fetched mapping) returns
    the SAME immutable payload on every call, so its "re-read" is a no-op and the decision->mutation
    window stays wide open — which is exactly the defect this issue exists to close. The property is
    therefore **opt-in and explicit** (``fresh_read = True``): a source that does not positively
    declare it is treated as not fresh, so a new source type can never silently inherit the right
    to actuate.
    """
    return bool(getattr(source, "fresh_read", False))


def read_watermark(
    source: object, issue: str, *, lane: str, lane_generation: object
) -> SweepWatermark:
    """One durable read: resolve the exact dispatch anchor and derive the anchored watermark.

    Called once for the decision and **again** for the pre-mutation re-check, so each call is a
    genuine fresh read of the durable record. Raises whatever the source raises; the caller maps an
    unreadable source to a fail-closed abstain.

    The read resolves TWO things about rounds (review F3). ``resolve_dispatch_entry_journal`` answers
    "where is round N's anchor" for the caller-fixed ``lane_generation`` — by construction it can
    never reveal that round N+1 has opened, so two reads always agree and an anchor-vs-anchor
    comparison is dead. ``dispatch_generations`` reads the newest round on the record WITHOUT fixing
    a generation, which is the authority that actually detects a supersede.
    """
    entries = list(source.read_entries(str(issue).strip()))
    dispatch = resolve_dispatch_entry_journal(
        entries, lane=lane, lane_generation=lane_generation
    )
    generations = dispatch_generations(entries, lane=lane)
    return resolve_watermark(
        entries,
        dispatch_journal=dispatch,
        lane=lane,
        lane_generation=lane_generation,
        latest_generation=generations[-1] if generations else 0,
    )


def sweep_once(
    *,
    workspace_id: str,
    lane_id: str,
    issue: str,
    lane_generation: object,
    source: object,
    fence: DispatchOutboxFence,
    target_assigned_name: str,
    lease: Optional[CallbackSweepLease] = None,
    send_fn: Optional[Callable[[str], object]] = None,
    record_fn_factory: Optional[Callable[[Callable[[], bool]], Callable[[dict, SweepWatermark], str]]] = None,
    callback: str = CALLBACK_ABSENT,
    stale_cli: bool = False,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Run one fenced callback sweep for a lane+generation. Returns the verdict + send outcome.

    ``send_fn(recovery_record_journal)`` performs the single recovery delivery and is invoked
    **only** after the re-read, the fence reserve, and the durable recovery record all clear — the
    journal id it receives is the record it must point at. ``record_fn(result, watermark) ->
    journal_id`` writes that durable record and returns its journal id (blank -> the send is
    cancelled). Omitting ``send_fn`` makes the sweep a read-only classification (nothing reserved,
    nothing written).

    Mutation is fail-closed on four independent preconditions (reviews R2-F1/F2/F3): a source that
    cannot promise a fresh read per call, a blank workspace identity, a missing durable writer, and
    a record that cannot be resolved all zero-send. Classification never requires any of them.

    The returned dict carries the classification (``state`` / ``is_stall`` / ``summary`` /
    ``recovery``), the watermark facts (``dispatch_journal`` / ``progress_journals`` /
    ``opaque_journals``), the durable record pointer (``recovery_record_journal``), and the mutation
    outcome (``sent`` / ``send_reason`` / ``send_detail``) — so the journal the coordinator records
    is replayable from this output alone, with no after-the-fact correction.
    """
    wsid, laneid, issue_s = (
        str(workspace_id).strip(),
        str(lane_id).strip(),
        str(issue).strip(),
    )

    # (1) Decision read: the anchored, ordered, DERIVED verdict.
    try:
        decided = read_watermark(
            source, issue_s, lane=laneid, lane_generation=lane_generation
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable durable record must not mutate
        return _unreadable(exc)

    result = classify_sweep(watermark=decided, callback=callback, stale_cli=stale_cli)
    result.update({"sent": False, "send_reason": "", "send_detail": ""})

    if send_fn is None:
        result["send_reason"] = SWEEP_READ_ONLY
        result["send_detail"] = "no send_fn supplied; classification only, nothing reserved"
        return result

    # --- Mutation preconditions, measured BEFORE any verdict is acted on -----------------------
    # R2-F1: a re-read over a frozen snapshot is not a re-read. Refuse to actuate on a source that
    # cannot see a gate landing after the decision.
    if not source_is_fresh(source):
        result["send_reason"] = ZERO_SEND_SOURCE_NOT_FRESH
        result["send_detail"] = (
            f"{type(source).__name__} does not declare fresh_read: its pre-mutation re-read would "
            f"return the same payload as the decision read, leaving the TOCTOU window open. "
            f"Classification only; use a live durable source to actuate"
        )
        return result
    # R2-F2: the fence key is partitioned by workspace, so a blank id reserves a DIFFERENT row and
    # the same recovery sends again. An unattested partition is not a fence.
    if not wsid:
        result["send_reason"] = ZERO_SEND_WORKSPACE_UNATTESTED
        result["send_detail"] = (
            "no attested workspace id: the at-most-once fence key is partitioned by workspace, so "
            "a blank id would reserve a separate row and permit a duplicate recovery send"
        )
        return result
    # R2-F3: without a durable writer the send would be a silent re-poke, which the workflow
    # contract prohibits outright.
    if record_fn_factory is None:
        # R8-F2: actuation takes ONLY the factory. A raw writer was previously accepted, which let
        # a caller reproduce the exact defect the factory exists to prevent (check before the
        # recorder, then a whole Redmine round-trip before the write). A public invariant cannot be
        # a caller convention -- the API has to make the unsafe shape unrepresentable.
        result["send_reason"] = ZERO_SEND_RECORD_FAILED
        result["send_detail"] = (
            "no record_fn_factory supplied: an actuating sweep must build its recorder around the "
            "live-grant predicate, so the ownership check lands immediately before the write "
            "(a raw grant-less writer is refused, and a silent re-poke is prohibited)"
        )
        return result

    # Take the attempt lease BEFORE any durable publication (review R5-F2). EVERY resolution is a
    # durable publication — including the zero-send ones, which are the common case — so they all
    # have to be serialized, not just the stall record. An earlier revision leased only the stall
    # path and let two sweeps publish duplicate `progress_without_callback` records.
    lease_store = lease or CallbackSweepLease()
    lease_key = LeaseKey(
        workspace_id=wsid, lane_id=laneid, issue=issue_s, anchor=decided.dispatch_journal
    )
    try:
        held = lease_store.acquire(lease_key)
    except CallbackSweepLeaseError as exc:
        result["send_reason"] = ZERO_SEND_LEASE_UNAVAILABLE
        result["send_detail"] = (
            f"the attempt lease is unavailable ({exc}); zero-send rather than run an "
            f"unserialized attempt that could publish a duplicate record"
        )
        return result
    if not held.owned:
        result["send_reason"] = ZERO_SEND_ATTEMPT_HELD
        result["send_detail"] = (
            f"another sweep owns this attempt ({held.detail}); standing down WITHOUT touching its "
            f"state — a live owner is never reclassified as a crash"
        )
        return result

    def _release() -> None:
        try:
            lease_store.release(lease_key, held.token)  # owner-conditional: only our own lease
        except CallbackSweepLeaseError:
            pass  # the lease expires on its own; a later sweep reclaims it

    def _still_owns() -> bool:
        """Re-verify ownership at the authority, immediately before a durable act (R6-F1).

        Acquiring is not enough. The TTL can lapse while this owner is merely SLOW — a few Redmine
        round-trips — and another sweep then reclaims the anchor. Without this check both would
        publish, which is the duplicate durable record the issue exists to remove. Fail-closed: an
        unreadable / replaced store is treated as ownership lost.
        """
        try:
            return lease_store.owns(lease_key, held.token, store_nonce=held.store_nonce)
        except CallbackSweepLeaseError:
            return False

    def _ownership_lost(where: str) -> dict[str, Any]:
        result["send_reason"] = ZERO_SEND_OWNERSHIP_LOST
        result["send_detail"] = (
            f"the attempt lease was lost before {where} (expired and reclaimed, or the store was "
            f"replaced); this sweep publishes nothing and sends nothing — the current owner acts"
        )
        result["resolution_recorded"] = False
        return result

    # R7-F1 / R8-F1: with the grant in hand, build the recorder around the live-grant predicate so
    # the ownership check lands immediately before the WRITE -- and requires a MARGIN, so the lease
    # cannot lapse while the write is still in flight.
    record_fn = record_fn_factory(_still_owns)

    if result["state"] != STATE_NO_PROGRESS_AFTER_HANDOFF:
        decision = decide_recovery(
            decided=decided, rechecked=decided, decided_state=result["state"]
        )
        result["send_reason"] = decision.reason
        result["send_detail"] = decision.detail
        # Acceptance 3: a first-pass resolution (progress_without_callback / stall_unprovable) is a
        # durable event too — the design must not depend on a later correction journal. Published
        # under the lease (re-verified at the authority), then released.
        if not _still_owns():
            return _ownership_lost("publishing the first-pass resolution")
        _record_resolution(result, record_fn, decided)
        _release()
        return result

    return _mutating_path(
        result=result,
        decided=decided,
        source=source,
        issue=issue_s,
        lane=laneid,
        lane_generation=lane_generation,
        workspace_id=wsid,
        fence=fence,
        lease_key=lease_key,
        lease_token=held.token,
        release=_release,
        still_owns=_still_owns,
        ownership_lost=_ownership_lost,
        target_assigned_name=target_assigned_name,
        send_fn=send_fn,
        record_fn=record_fn,
        now=now,
    )


def _mutating_path(
    *,
    result: dict[str, Any],
    decided: SweepWatermark,
    source: object,
    issue: str,
    lane: str,
    lane_generation: object,
    workspace_id: str,
    fence: DispatchOutboxFence,
    lease_key: object,
    lease_token: str,
    release: Callable[[], None],
    still_owns: Callable[[], bool],
    ownership_lost: Callable[[str], dict],
    target_assigned_name: str,
    send_fn: Callable[[str], object],
    record_fn: Callable[[dict, SweepWatermark], str],
    now: Optional[str],
) -> dict[str, Any]:
    """The stall path: publish the record under a live grant, then send exactly once.

    Order (the lease is already held by :func:`sweep_once`)::

        boundary re-read -> record (published only while the grant is live)
          -> final live read -> reserve the send fence -> send -> mark delivered

    - the **boundary re-read** is the last chance to notice the lane is not stalled; a gate landing
      here folds to a first-pass zero-send resolution (acceptance 2/3);
    - the **record** is durable and the notification points at it — a re-poke with no journal behind
      it is the silent re-poke the workflow contract prohibits. It is published only while this
      sweep still owns the attempt: the recorder itself re-checks the grant immediately before its
      write, because the check must sit after the recorder's own Redmine reads, not before them
      (review R7-F1);
    - the **final live read** sits immediately before the send. The record's journal id is a
      position, not a CAS against future writes, so it cannot substitute for reading late;
    - the **send fence** (:class:`...dispatch_outbox_fence.DispatchOutboxFence`) is reserved here
      and resolved immediately — short-lived, the only way its contract supports. It is NOT held
      across the attempt; that is the attempt lease's job, and ``DispatchOutboxFence.release()``
      does not exist (a reservation it cannot prove was never sent must not be droppable).

    Any abort releases the lease and leaves no durable side effect, so the next sweep retries
    cleanly. The read->transport window that remains is the documented residual of coordinator
    disposition j#80302 (b)+(c); true receiver-side exactly-once is Redmine #13910.
    """
    key = FenceKey(
        workspace_id=workspace_id,
        lane_id=lane,
        issue=issue,
        journal=decided.dispatch_journal,
        action_id=SWEEP_RECOVERY_ACTION_ID,
        target_assigned_name=str(target_assigned_name).strip(),
    )
    _release = release

    def _abort(folded: dict[str, Any], watermark: SweepWatermark) -> dict[str, Any]:
        """Stand down without having sent: record the resolution, THEN drop our own lease.

        Order matters (R5-F2): releasing first would put the zero-send resolution publication back
        outside the serialized region, which is how two sweeps came to publish duplicate
        `progress_without_callback` records. Ownership is re-verified first (R6-F1) — a lapsed
        lease means the current owner will publish, so this one must not.
        """
        if not still_owns():
            return ownership_lost("publishing the resolution")
        _record_resolution(folded, record_fn, watermark)
        _release()
        return folded

    # (2) Mutation-boundary re-read: the last chance to notice the lane is not stalled.
    try:
        rechecked = read_watermark(
            source, issue, lane=lane, lane_generation=lane_generation
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable re-check must not mutate
        _release()
        return _unreadable(exc)

    decision = decide_recovery(
        decided=decided, rechecked=rechecked, decided_state=result["state"]
    )
    if decision.zero_send:
        return _abort(_apply_zero_send(result, decision, rechecked), rechecked)

    # (3) Record the stall durably, inside the lease — after re-verifying we still hold it (R6-F1).
    if not still_owns():
        _release()
        return ownership_lost("recording the stall")
    try:
        record_journal = str(record_fn(dict(result), rechecked) or "").strip()
    except RecordOwnershipLostError:
        _release()
        return ownership_lost("the record write")
    except RecordPublicationHeldError as exc:
        # Another owner holds (or already completed) this exact record's publication. Never retry:
        # a lingering reservation may be an owner mid-PUT (R9-F1 / j#80383).
        _release()
        result["send_reason"] = ZERO_SEND_PUBLICATION_HELD
        result["send_detail"] = f"{exc}"
        result["resolution_recorded"] = False
        result["needs_reconcile"] = True
        return result
    except RecordPublicationUncertainError as exc:
        # A PUT of unknown fate. Safety over availability: stop, and let an operator reconcile.
        _release()
        result["send_reason"] = ZERO_SEND_PUBLICATION_UNCERTAIN
        result["send_detail"] = f"{exc}"
        result["resolution_recorded"] = False
        result["sweep_complete"] = False
        result["needs_reconcile"] = True
        return result
    except RecordSupersededError as exc:
        # R4-F1: the recorder's own pre-write read contradicted the verdict, so nothing was
        # written. Fold to the first-pass resolution — no false stall record, no correction.
        try:
            fresh = read_watermark(source, issue, lane=lane, lane_generation=lane_generation)
        except Exception:  # noqa: BLE001 - fall back to the boundary read for the fold
            fresh = rechecked
        folded = _apply_zero_send(
            result,
            RecoveryDecision(
                send=False,
                reason=(
                    ZERO_SEND_PROGRESS_LANDED if fresh.has_progress else ZERO_SEND_STALL_UNPROVABLE
                ),
                detail=f"{exc}; the recovery is refused",
            ),
            fresh,
        )
        return _abort(folded, fresh)
    except Exception as exc:  # noqa: BLE001 - an unwritable record must not become a silent poke
        record_journal, record_error = "", type(exc).__name__
    else:
        record_error = ""
    if not record_journal:
        _release()
        result["send_reason"] = ZERO_SEND_RECORD_FAILED
        result["send_detail"] = (
            f"the recovery classification could not be durably recorded "
            f"({record_error or 'unresolved'}); nothing was sent and the attempt lease was "
            f"released, so the next sweep retries this anchor cleanly"
        )
        result["resolution_recorded"] = False
        return result
    result["recovery_record_journal"] = record_journal
    result["resolution_recorded"] = True

    # (4) Final live read immediately before the send (R4-F2).
    try:
        verified = read_watermark(
            source, issue, lane=lane, lane_generation=lane_generation
        )
    except Exception as exc:  # noqa: BLE001 - an unverifiable position must not send
        # R4-F4: a durable record already landed. Keep its pointer and report the sweep INCOMPLETE
        # rather than returning a bare unreadable result that drops the mutation from view.
        _release()
        result["send_reason"] = SWEEP_SOURCE_UNREADABLE
        result["send_detail"] = (
            f"the post-record verification read failed ({type(exc).__name__}): a recovery record "
            f"j#{record_journal} IS durable but the send was not attempted, so this sweep is "
            f"incomplete and must be re-run"
        )
        result["sweep_complete"] = False
        return result
    if not verified.stall_provable:
        # ANY qualifying progress refuses the send — a lane that is demonstrably advancing must not
        # be poked, whenever the gate landed. The record's position does not change *whether* to
        # send; it changes what the durable log now means, which is worth stating exactly:
        #
        # - progress PRECEDING j#R: the stall record was already false when written, so the log now
        #   holds a wrong verdict that a reader must not trust (``record_stale_at_write``);
        # - progress AFTER j#R: the record was true as of j#R and the lane simply advanced next —
        #   an honest, ordered history that needs no correction.
        preceding = [
            (j, kind)
            for j, kind in verified.progress
            if _journal_int(j) < _journal_int(record_journal, default=-1)
        ]
        detail = (
            f"qualifying progress at j#{preceding[0][0]} PRECEDES this sweep's own record "
            f"j#{record_journal}: the stall verdict was already stale when it was written"
            if preceding
            else (
                f"qualifying progress landed after this sweep's record j#{record_journal}: the "
                f"record was true when written, but the lane is advancing now"
                if verified.has_progress
                else f"the record at j#{record_journal} is contradicted by unreadable post-anchor "
                f"journals; the stall is unprovable"
            )
        )
        folded = _apply_zero_send(
            result,
            RecoveryDecision(
                send=False,
                reason=(
                    ZERO_SEND_PROGRESS_LANDED
                    if verified.has_progress
                    else ZERO_SEND_STALL_UNPROVABLE
                ),
                detail=f"{detail}; the recovery is refused",
            ),
            verified,
        )
        folded["record_stale_at_write"] = bool(preceding)
        return _abort(folded, verified)

    # (5) Reserve the send on the shared fence -- SHORT-LIVED, the way its contract intends: it
    #     is taken here and resolved immediately, never held across I/O (R5-F1).
    try:
        reserve = fence.reserve(key, now=now)
    except DispatchOutboxFenceError as exc:
        _release()
        result["send_reason"] = ZERO_SEND_FENCE_UNAVAILABLE
        result["send_detail"] = (
            f"the idempotency authority is unavailable ({exc}); zero-send rather than risk a "
            f"duplicate replay"
        )
        return result
    if not reserve.won:
        _release()
        result["send_reason"] = ZERO_SEND_FENCE_HELD
        result["send_detail"] = (
            f"recovery for dispatch anchor {decided.dispatch_journal} is already "
            f"{reserve.current_state}; at most one recovery delivery per gate anchor "
            f"({reserve.detail})"
        )
        result["needs_reconcile"] = bool(reserve.needs_reconcile)
        return result

    if not still_owns():
        fence.mark_cancelled(key, detail="attempt lease lost before send", now=now)
        _release()
        return ownership_lost("the send")
    try:
        send_fn(record_journal)
    except Exception as exc:  # noqa: BLE001 - an ambiguous send is uncertain, never auto-retried
        fence.mark_uncertain(key, detail=f"send raised {type(exc).__name__}", now=now)
        _release()
        result["send_reason"] = "send_uncertain"
        result["send_detail"] = (
            f"the recovery send raised {type(exc).__name__}; the fence key is marked uncertain "
            f"for operator reconcile and is NOT auto-retried"
        )
        result["needs_reconcile"] = True
        return result

    fence.mark_delivered(key, detail="callback sweep recovery delivered", now=now)
    _release()
    result["sent"] = True
    result["send_reason"] = SEND_RESERVED
    result["send_detail"] = (
        f"the single recovery delivery for dispatch anchor {rechecked.dispatch_journal}, "
        f"pointing at recovery record j#{record_journal}"
    )
    return result


def _record_resolution(
    result: dict[str, Any],
    record_fn: Optional[Callable[[dict, SweepWatermark], str]],
    watermark: SweepWatermark,
) -> None:
    """Durably record a resolution and report FAIL-CLOSED when it did not land (review R3-F4).

    A resolution (``progress_without_callback`` picked up first-pass, an abstention, a superseded
    round) is itself the durable event the workflow contract requires — the rule names the *stall
    check and its classification*, not only the send, and acceptance 3 says to **record** the
    first-pass resolution, not merely to compute it.

    An earlier revision made this best-effort, which let the sweep return
    ``state='progress_without_callback'`` as a "first-pass resolution" while nothing whatsoever had
    been written. That is a claim the caller cannot distinguish from a real one, so the outcome now
    carries ``resolution_recorded`` explicitly: ``False`` means the verdict stands but is NOT
    durable, and the caller (CLI exit code, journal text) must surface it as incomplete rather than
    as a resolved sweep.
    """
    if record_fn is None:
        result["resolution_recorded"] = False
        result["record_reason"] = "no_recorder"
        return
    try:
        journal = str(record_fn(dict(result), watermark) or "").strip()
    except RecordOwnershipLostError:
        # R7-F1: the grant lapsed at the write. Nothing was published; the current owner will.
        result["resolution_recorded"] = False
        result["record_reason"] = "ownership_lost"
        return
    except RecordPublicationHeldError:
        result["resolution_recorded"] = False
        result["record_reason"] = "publication_held"
        result["needs_reconcile"] = True
        return
    except RecordPublicationUncertainError:
        result["resolution_recorded"] = False
        result["record_reason"] = "publication_uncertain"
        result["sweep_complete"] = False
        result["needs_reconcile"] = True
        return
    except Exception as exc:  # noqa: BLE001 - surfaced as incomplete, never swallowed as success
        result["resolution_recorded"] = False
        result["record_reason"] = type(exc).__name__
        return
    if not journal:
        result["resolution_recorded"] = False
        result["record_reason"] = "unresolved"
        return
    result["recovery_record_journal"] = journal
    result["resolution_recorded"] = True
    result["record_reason"] = ""


def _apply_zero_send(
    result: dict[str, Any], decision: RecoveryDecision, rechecked: SweepWatermark
) -> dict[str, Any]:
    """Fold a zero-send decision into the verdict, re-classifying a progress race first-pass.

    Acceptance 3: when the re-read proves a qualifying gate landed, the sweep records
    ``progress_without_callback`` **as its own verdict** — it does not record a stall and leave a
    later journal to correct it. The watermark facts are re-pointed at the fresh read so the output
    names the gate that actually landed.
    """
    result["send_reason"] = decision.reason
    result["send_detail"] = decision.detail
    if decision.reason == ZERO_SEND_PROGRESS_LANDED:
        result["state"] = STATE_PROGRESS_WITHOUT_CALLBACK
        result["is_stall"] = True  # still a stall class: the pointer is missing, not the work
        result["new_durable_progress"] = True
        result["summary"] = (
            "a qualifying durable gate landed after the dispatch anchor between the sweep's "
            "decision and its send — the work is advancing and only the coordinator pointer is "
            "missing; picked up first-pass, no correction journal needed"
        )
        result["recovery"] = [
            "pick up the advanced durable state directly from the named journal; do NOT "
            "re-dispatch or replay work the record already shows as advanced",
            "record the progress_without_callback resolution so the next coordinator sees it "
            "was handled",
        ]
        result["dispatch_journal"] = rechecked.dispatch_journal
        result["progress_journals"] = [
            {"journal": j, "kind": kind} for j, kind in rechecked.progress
        ]
    elif decision.reason == ZERO_SEND_STALL_UNPROVABLE:
        # The same honesty obligation: opaque activity landed in the window, so the recorded
        # verdict must be the abstention we actually took — not a stall we declined to act on.
        result["state"] = SWEEP_STATE_STALL_UNPROVABLE
        result["is_stall"] = False
        result["summary"] = (
            "journal(s) with no recognized structured marker landed after the dispatch anchor "
            "between the sweep's decision and its send — the lane may be advancing in prose, so "
            "the stall is unprovable and no recovery was sent"
        )
        result["recovery"] = [
            "read the named journal(s) directly to see whether the lane advanced",
            "record the lane's gates through the canonical marker-bearing writers so the sweep "
            "can classify them structurally instead of abstaining",
            "do NOT re-dispatch on this verdict — it is an abstention, not a stall",
        ]
        result["opaque_journals"] = list(rechecked.opaque)
    return result


def _unreadable(exc: BaseException) -> dict[str, Any]:
    """The fail-closed abstain for an unreadable durable record (no verdict, no mutation)."""
    return {
        "state": SWEEP_SOURCE_UNREADABLE,
        "is_stall": False,
        "dispatch_delivered": False,
        "new_durable_progress": False,
        "callback": CALLBACK_ABSENT,
        "stale_cli": False,
        "summary": (
            f"the durable record could not be read ({type(exc).__name__}); the sweep abstains "
            f"rather than classify a stall it cannot prove"
        ),
        "recovery": [
            "restore Redmine read access and re-run the sweep; the durable record is the only "
            "workflow truth (pane / status / doctor are corroborating only)",
        ],
        "invariants": [],
        "dispatch_journal": "",
        "progress_journals": [],
        "sent": False,
        "send_reason": SWEEP_SOURCE_UNREADABLE,
        "send_detail": f"{type(exc).__name__}: {exc}",
    }


class RecoverySendError(RuntimeError):
    """The one recovery notification did not positively succeed (-> the fence marks it uncertain)."""


from .callback_recovery_record import (  # noqa: E402  (re-exported: the public API is unchanged)
    RecordOwnershipLostError,
    RecordPublicationHeldError,
    RecordPublicationUncertainError,
    RecordSupersededError,
    build_recovery_recorder,
)


def build_recovery_sender(
    *,
    issue: str,
    target: str,
    runner: Optional[Callable[[list], "tuple[int, str]"]] = None,
    mozyo_bridge_bin: str = "mozyo-bridge",
) -> Callable[[str], object]:
    """Build the production ``send_fn``: ONE ``handoff send`` pointing at the recovery record.

    The composition seam that makes :func:`sweep_once` a real recovery path rather than a library
    fixture (review R1-F1). It mirrors :class:`...callback_send_port.HandoffCallbackSendPort` — the
    established sender for this family — including its **injectable ``runner``**: production spawns
    the CLI, tests inject a fake and exercise the whole fenced path with no external send. That
    injectability is why the wiring does not need the (still unauthorized) live dogfood.

    The anchor is the **recovery record journal** :func:`sweep_once` just wrote, not the original
    dispatch (review R2-F3): the receiver must land on the record that says *why* it was re-poked —
    the classification, what was missing, the retry target — rather than on the dispatch it already
    knows about. The notification stays a pointer; the journal is the truth. A non-zero exit raises
    :class:`RecoverySendError`, which :func:`sweep_once` turns into a fence ``uncertain`` — an
    ambiguous send is never auto-retried.
    """

    def _run(record_journal: str) -> object:
        anchor = str(record_journal or "").strip()
        if not anchor:
            raise RecoverySendError("no recovery record journal to point at; refusing to send")
        argv = [
            str(mozyo_bridge_bin), "handoff", "send",
            "--to", "codex",
            "--target", str(target),
            "--source", "redmine",
            "--issue", str(issue),
            "--journal", anchor,
            "--kind", "reply",
            "--mode", "standard",
            "--target-repo", "auto",
        ]
        run = runner if runner is not None else _default_recovery_runner
        rc, detail = run(argv)
        if int(rc) != 0:
            raise RecoverySendError(f"handoff send exited {rc}: {str(detail)[:200]}")
        return {"rc": rc, "anchor": anchor}

    return _run


def _default_recovery_runner(argv: list) -> "tuple[int, str]":
    """Spawn the sanctioned mozyo-bridge CLI for the one recovery send (fixed argv, no shell)."""
    import subprocess  # noqa: S404 - the sanctioned CLI boundary, mirroring HandoffCallbackSendPort

    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True
    )
    return proc.returncode, (proc.stderr or proc.stdout or "")


__all__ = (
    "SWEEP_SOURCE_UNREADABLE",
    "SWEEP_READ_ONLY",
    "ZERO_SEND_SOURCE_NOT_FRESH",
    "ZERO_SEND_WORKSPACE_UNATTESTED",
    "ZERO_SEND_RECORD_FAILED",
    "ZERO_SEND_ATTEMPT_HELD",
    "ZERO_SEND_LEASE_UNAVAILABLE",
    "ZERO_SEND_OWNERSHIP_LOST",
    "ZERO_SEND_PUBLICATION_HELD",
    "ZERO_SEND_PUBLICATION_UNCERTAIN",
    "RecordPublicationHeldError",
    "RecordPublicationUncertainError",
    "RecoverySendError",
    "RecordSupersededError",
    "RecordOwnershipLostError",
    "source_is_fresh",
    "read_watermark",
    "sweep_once",
    "build_recovery_sender",
    "build_recovery_recorder",
)
