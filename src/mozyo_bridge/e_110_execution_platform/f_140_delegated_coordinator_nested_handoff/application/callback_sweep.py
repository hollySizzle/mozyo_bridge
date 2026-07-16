"""Callback sweep use case: fresh read -> re-read -> fence -> durable record -> one send (#13889).

The composition the issue's acceptance describes, in one ordered path:

1. **read** the durable record live and resolve the EXACT dispatch anchor for this lane+generation,
   then derive the verdict from the anchored, ordered watermark (acceptance 1/2/4) — the sweep never
   accepts an agent's asserted ``--progress`` boolean;
2. **re-read** immediately before any mutation and re-derive the watermark. A gate landing in the
   decision->send window (the 8-second j#79995 -> j#79996 evidence window) turns the verdict into
   ``progress_without_callback`` and the mutation is refused (acceptance 2/3);
3. **record** the classification durably — before touching any authority (review R3-F3) — and
   point the one notification at THAT journal; a re-poke with no journal behind it is invisible to
   the next coordinator and is prohibited (review R2-F3);
4. **verify** against the record's own journal id, then **fence** the send so recovery is delivered
   **at most once per gate anchor** across crashes and concurrent sweeps (acceptance 5).

Each guard covers a failure the others cannot, and every one of them was, at some revision, present
but ineffective — which is the lesson the ordering encodes:

- the **re-read** stops a correct-but-stale verdict from mutating, but only if the source can
  actually return newer data. A frozen snapshot re-read is a no-op that merely *looks* like a guard
  (review R2-F1), so mutation requires a source that positively declares :func:`source_is_fresh`;
- the **fence** is the at-most-once authority, but only if its key is real: the key is partitioned
  by workspace, so an unattested id reserves a different row and the same recovery sends again
  (review R2-F2). An unmeasured partition is not a fence;
- the **record** is written BEFORE the reserve (review R3-F3). Reserving first and cancelling on a
  write failure looked like a clean rollback but was not: ``FENCE_CANCELLED`` is terminal to
  ``reserve``, so a transient failure blocked that anchor's recovery permanently. The record is
  idempotent, so writing it first costs nothing and leaves a failed attempt with no durable
  side-effect to undo;
- **no CAS exists** in Redmine, so the last window is closed by position rather than by locking:
  the record's journal id ``R`` is a serialization point, and the sweep requires that no qualifying
  gate PRECEDES ``R`` (review R3-F1). A gate before ``R`` proves the verdict was stale when written.

Every failure degrades to **zero-send**: an unreadable source, a non-fresh source, an unattested
workspace, an unwritable record, an unbootstrapped / replaced fence, or a lost reserve all refuse
the mutation. A sweep that cannot prove a stall does not act on one.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

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
    send_fn: Optional[Callable[[str], object]] = None,
    record_fn: Optional[Callable[[dict, SweepWatermark], str]] = None,
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
    if record_fn is None:
        result["send_reason"] = ZERO_SEND_RECORD_FAILED
        result["send_detail"] = (
            "no record_fn supplied: every stall check and re-notification must be recorded as a "
            "durable journal before the pointer send (a silent re-poke is prohibited)"
        )
        return result

    if result["state"] != STATE_NO_PROGRESS_AFTER_HANDOFF:
        decision = decide_recovery(
            decided=decided, rechecked=decided, decided_state=result["state"]
        )
        result["send_reason"] = decision.reason
        result["send_detail"] = decision.detail
        # Acceptance 3: a first-pass resolution (progress_without_callback / stall_unprovable) is a
        # durable event too — the design must not depend on a later correction journal.
        _record_resolution(result, record_fn, decided)
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
    target_assigned_name: str,
    send_fn: Callable[[str], object],
    record_fn: Callable[[dict, SweepWatermark], str],
    now: Optional[str],
) -> dict[str, Any]:
    """The stall path: ONE reservation owns the whole attempt — read, record, verify, send.

    Four review rounds of local re-ordering produced a design where the fence guarded only the
    *send*, and every other step raced. This is the structure that follows from making the
    reservation own the attempt end to end:

    ``reserve -> boundary read -> record -> final live read -> send -> mark_delivered``,
    with :meth:`DispatchOutboxFence.release` on any abort.

    Why each position (and what broke when it was elsewhere):

    - **reserve first.** The fence's ``BEGIN IMMEDIATE`` row is the only real serialization
      authority available — Redmine has none. Holding it across the *whole* attempt is what makes
      the record publication atomic: a concurrent sweep loses the reserve and never writes, so two
      sweeps cannot post duplicate records and strand the anchor in permanent ambiguity (R4-F3).
      An earlier revision moved the reserve after the record to fix a retry block (R3-F3), which
      bought retryability by giving up atomicity.
    - **release, not cancel, on abort.** That retry block is instead fixed by
      :meth:`DispatchOutboxFence.release`, which drops a still-reserved row: this attempt did not
      happen, so the next one starts clean. ``mark_cancelled`` is terminal and would block the
      anchor forever.
    - **final live read immediately before the send** (R4-F2). The record's journal id is only a
      *position*, not a CAS against future Redmine writes — an earlier revision claimed otherwise
      and a gate landing during the reserve was still replayed. The window cannot be closed to
      zero over a store with no CAS; it is narrowed to read->send and no wider claim is made.
    - **the recorder aborts rather than write a stale verdict** (R4-F1): its own pre-write read is
      an observation too, so a superseded stall raises :class:`RecordSupersededError` and folds to
      the first-pass resolution instead of writing a false record and correcting it.
    """
    key = FenceKey(
        workspace_id=workspace_id,
        lane_id=lane,
        issue=issue,
        journal=decided.dispatch_journal,
        action_id=SWEEP_RECOVERY_ACTION_ID,
        target_assigned_name=str(target_assigned_name).strip(),
    )

    # (1) Take ownership of the whole attempt before any durable work.
    try:
        reserve = fence.reserve(key, now=now)
    except DispatchOutboxFenceError as exc:
        result["send_reason"] = ZERO_SEND_FENCE_UNAVAILABLE
        result["send_detail"] = (
            f"the idempotency authority is unavailable ({exc}); zero-send rather than risk a "
            f"duplicate replay"
        )
        return result
    if not reserve.won:
        result["send_reason"] = ZERO_SEND_FENCE_HELD
        result["send_detail"] = (
            f"recovery for dispatch anchor {decided.dispatch_journal} is already "
            f"{reserve.current_state}; at most one recovery attempt per gate anchor "
            f"({reserve.detail})"
        )
        result["needs_reconcile"] = bool(reserve.needs_reconcile)
        return result

    def _abort(folded: dict[str, Any], watermark: SweepWatermark) -> dict[str, Any]:
        """Stand down without having sent: drop the reservation so a later sweep may retry."""
        try:
            fence.release(key)
        except DispatchOutboxFenceError:
            pass  # the row stays reserved; a re-entry surfaces it as uncertain for reconcile
        _record_resolution(folded, record_fn, watermark)
        return folded

    # (2) Mutation-boundary re-read: the last chance to notice the lane is not stalled.
    try:
        rechecked = read_watermark(
            source, issue, lane=lane, lane_generation=lane_generation
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable re-check must not mutate
        try:
            fence.release(key)
        except DispatchOutboxFenceError:
            pass
        return _unreadable(exc)

    decision = decide_recovery(
        decided=decided, rechecked=rechecked, decided_state=result["state"]
    )
    if decision.zero_send:
        return _abort(_apply_zero_send(result, decision, rechecked), rechecked)

    # (3) Record the stall durably, inside the reservation (R4-F3 atomicity).
    try:
        record_journal = str(record_fn(dict(result), rechecked) or "").strip()
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
        try:
            fence.release(key)
        except DispatchOutboxFenceError:
            pass
        result["send_reason"] = ZERO_SEND_RECORD_FAILED
        result["send_detail"] = (
            f"the recovery classification could not be durably recorded "
            f"({record_error or 'unresolved'}); nothing was sent and the reservation was released, "
            f"so the next sweep retries this anchor cleanly"
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
        try:
            fence.release(key)
        except DispatchOutboxFenceError:
            pass
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

    # (5) Send exactly once, pointing at the record. The reservation has been held since step (1).
    try:
        send_fn(record_journal)
    except Exception as exc:  # noqa: BLE001 - an ambiguous send is uncertain, never auto-retried
        fence.mark_uncertain(key, detail=f"send raised {type(exc).__name__}", now=now)
        result["send_reason"] = "send_uncertain"
        result["send_detail"] = (
            f"the recovery send raised {type(exc).__name__}; the fence key is marked uncertain "
            f"for operator reconcile and is NOT auto-retried"
        )
        result["needs_reconcile"] = True
        return result

    fence.mark_delivered(key, detail="callback sweep recovery delivered", now=now)
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


class RecordSupersededError(RuntimeError):
    """The recorder's own pre-write read contradicts the verdict, so it wrote nothing (R4-F1).

    Distinct from a write failure: nothing went wrong and nothing is broken — the lane simply is
    not stalled after all, discovered at the last durable read before the write. The caller folds
    it into the first-pass resolution, which is what acceptance 3 asks for: no false stall record
    to correct later.
    """


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


def build_recovery_recorder(
    *,
    source: object,
    issue: str,
    lane: str,
    lane_generation: object,
    post_note: Callable[[str, str], object],
) -> Callable[[dict, SweepWatermark], str]:
    """Build the production ``record_fn``: write the sweep record, then RESOLVE its journal id.

    Review R2-F3. Redmine's note write returns ``204 No Content`` with no journal id, so the writer
    cannot learn where its own record landed. This uses the same write -> re-read -> resolve-by-
    marker pattern :mod:`...reconcile_dispatch_writer` established for the IR anchor: the record
    carries an identifying marker, and its OWNING entry's journal id (the durable authority, never a
    self-reported field) is read back afterwards.

    Idempotent by pre-read: an already-recorded resolution is recovered rather than duplicated, so
    repeated sweeps at the same verdict do not spam the issue. Keyed by ``outcome`` as well as the
    dispatch anchor, so a legitimately changed verdict (``stall_unprovable`` -> a landed gate) is
    recorded once each. Returns ``""`` when the record cannot be resolved — :func:`sweep_once` then
    cancels the send rather than perform an unrecorded one.
    """

    def _record(result: dict, watermark: SweepWatermark) -> str:
        outcome = str(result.get("state", "") or "").strip()
        anchor = str(watermark.dispatch_journal or "").strip()
        if not (outcome and anchor):
            return ""
        keys = dict(
            lane=lane, lane_generation=lane_generation, dispatch_anchor=anchor, outcome=outcome
        )
        pre = list(source.read_entries(str(issue)))
        existing = sweep_record_journals(pre, **keys)
        if len(existing) == 1:
            return existing[0]  # already recorded this resolution: recover, write nothing
        if len(existing) >= 2:
            return ""  # ambiguous: fail closed rather than pick one
        # Review R4-F1: this pre-read is a durable OBSERVATION, not just an idempotency lookup.
        # Writing a stall record while it already shows a landed gate produces exactly the
        # false-verdict-then-correction pair acceptance 3 forbids, so the verdict is re-checked
        # against the very entries about to be written against, and a superseded stall aborts
        # BEFORE the write. The caller folds this into the first-pass resolution.
        if outcome == STATE_NO_PROGRESS_AFTER_HANDOFF:
            fresh = resolve_watermark(
                pre,
                dispatch_journal=anchor,
                lane=lane,
                lane_generation=lane_generation,
                latest_generation=(dispatch_generations(pre, lane=lane) or (0,))[-1],
            )
            if not fresh.stall_provable or fresh.superseded:
                raise RecordSupersededError(
                    f"the stall verdict no longer holds at write time (progress="
                    f"{[j for j, _ in fresh.progress]} opaque={list(fresh.opaque)} "
                    f"superseded={fresh.superseded}); no stall record was written"
                )
        post_note(str(issue), render_sweep_record_note(_record_body(result), **keys))
        written = sweep_record_journals(source.read_entries(str(issue)), **keys)
        return written[0] if len(written) == 1 else ""

    return _record


def _record_body(result: dict) -> str:
    """The human-readable sweep record: the classification, what was missing, the retry target."""
    lines = [
        "## Gate: progress_log — callback sweep record",
        "",
        f"- **state**: `{result.get('state', '')}`",
        f"- **is_stall**: {result.get('is_stall', False)}",
        f"- **dispatch_anchor**: j#{result.get('dispatch_journal', '') or '-'}",
        f"- **callback**: `{result.get('callback', '')}`",
        f"- **send_reason**: `{result.get('send_reason', '') or 'pending'}`",
        "",
        f"{result.get('summary', '')}",
    ]
    progress = result.get("progress_journals") or []
    if progress:
        lines += ["", "### 観測した durable progress"] + [
            f"- j#{p['journal']} `{p['kind']}`" for p in progress
        ]
    opaque = result.get("opaque_journals") or []
    if opaque:
        lines += [
            "",
            "### marker を持たない post-anchor journal (分類不能)",
            "- " + ", ".join(f"j#{j}" for j in opaque),
        ]
    steps = result.get("recovery") or []
    if steps:
        lines += ["", "### recovery"] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    lines += [
        "",
        "本 record は coordinator の sweep 記録であり、worker progress ではない "
        "(marker kind は `callback_sweep_record`)。",
    ]
    return "\n".join(lines)


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
    "RecoverySendError",
    "RecordSupersededError",
    "source_is_fresh",
    "read_watermark",
    "sweep_once",
    "build_recovery_sender",
    "build_recovery_recorder",
)
