"""Callback sweep use case: fresh read -> re-read -> fence -> durable record -> one send (#13889).

The composition the issue's acceptance describes, in one ordered path:

1. **read** the durable record live and resolve the EXACT dispatch anchor for this lane+generation,
   then derive the verdict from the anchored, ordered watermark (acceptance 1/2/4) — the sweep never
   accepts an agent's asserted ``--progress`` boolean;
2. **re-read** immediately before any mutation and re-derive the watermark. A gate landing in the
   decision->send window (the 8-second j#79995 -> j#79996 evidence window) turns the verdict into
   ``progress_without_callback`` and the mutation is refused (acceptance 2/3);
3. **fence** the surviving mutation on :class:`...dispatch_outbox_fence.DispatchOutboxFence`, keyed
   on the dispatch anchor, so recovery is delivered **at most once per gate anchor** even across
   crashes and concurrent sweeps (acceptance 5);
4. **record** the classification durably, then point the one notification at THAT journal — a
   re-poke with no journal behind it is invisible to the next coordinator and is prohibited.

Each guard covers a failure the others cannot, so all four are required:

- the **re-read** stops a correct-but-stale verdict from mutating, but only if the source can
  actually return newer data. A frozen snapshot re-read is a no-op that merely *looks* like a guard
  (review R2-F1), so mutation requires a source that positively declares :func:`source_is_fresh`;
- the **fence** is the at-most-once authority, but only if its key is real: the key is partitioned
  by workspace, so an unattested (blank) id reserves a different row and the same recovery sends
  again (review R2-F2). An unmeasured partition is not a fence;
- the **record** makes the action auditable, and it is written *after* the reserve and *before* the
  send — a send whose reason never landed durably is the prohibited silent re-poke (review R2-F3).

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

    # (2) Pre-mutation re-read: close the TOCTOU window the evidence lands in.
    try:
        rechecked = read_watermark(
            source, issue_s, lane=laneid, lane_generation=lane_generation
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable re-check must not mutate
        return _unreadable(exc)

    decision = decide_recovery(
        decided=decided, rechecked=rechecked, decided_state=result["state"]
    )
    if decision.zero_send:
        folded = _apply_zero_send(result, decision, rechecked)
        _record_resolution(folded, record_fn, rechecked)
        return folded

    # (3) The one fenced mutation, keyed on the dispatch anchor -> at most once per gate anchor.
    key = FenceKey(
        workspace_id=wsid,
        lane_id=laneid,
        issue=issue_s,
        journal=rechecked.dispatch_journal,
        action_id=SWEEP_RECOVERY_ACTION_ID,
        target_assigned_name=str(target_assigned_name).strip(),
    )
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
            f"recovery for dispatch anchor {rechecked.dispatch_journal} is already "
            f"{reserve.current_state}; at most one recovery delivery per gate anchor "
            f"({reserve.detail})"
        )
        result["needs_reconcile"] = bool(reserve.needs_reconcile)
        return result

    # (4) Record the stall classification DURABLY, before the pointer send (review R2-F3). The
    #     notification then points at THIS journal, not at the original dispatch: the next
    #     coordinator reads why the recovery happened, not merely that something was re-poked.
    #     A record that cannot be written / resolved cancels the reservation — a send whose reason
    #     is not on the record is the prohibited silent re-poke.
    try:
        record_journal = str(record_fn(dict(result), rechecked) or "").strip()
    except Exception as exc:  # noqa: BLE001 - an unwritable record must not become a silent poke
        record_journal = ""
        record_error: object = type(exc).__name__
    else:
        record_error = ""
    if not record_journal:
        fence.mark_cancelled(
            key, detail=f"recovery record not durable ({record_error or 'unresolved'})", now=now
        )
        result["send_reason"] = ZERO_SEND_RECORD_FAILED
        result["send_detail"] = (
            f"the recovery classification could not be durably recorded "
            f"({record_error or 'unresolved'}); the send is cancelled rather than performed as a "
            f"silent re-poke, and the fence key is released for a later attempt"
        )
        return result
    result["recovery_record_journal"] = record_journal

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
    """Durably record a zero-send resolution, best-effort (acceptance 3 / review R2-F3).

    A resolution (``progress_without_callback`` picked up first-pass, an abstention, a superseded
    round) is a durable event the next coordinator must see — the whole point of acceptance 3 is
    that no later correction journal is needed. Unlike the send path this is best-effort: nothing
    was mutated, so a failed write degrades the audit trail rather than risking a duplicate.
    """
    if record_fn is None:
        return
    try:
        journal = str(record_fn(dict(result), watermark) or "").strip()
    except Exception as exc:  # noqa: BLE001 - a failed resolution record never fails the sweep
        result["recovery_record_reason"] = type(exc).__name__
        return
    if journal:
        result["recovery_record_journal"] = journal


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
        existing = sweep_record_journals(source.read_entries(str(issue)), **keys)
        if len(existing) == 1:
            return existing[0]  # already recorded this resolution: recover, write nothing
        if len(existing) >= 2:
            return ""  # ambiguous: fail closed rather than pick one
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
    "source_is_fresh",
    "read_watermark",
    "sweep_once",
    "build_recovery_sender",
    "build_recovery_recorder",
)
