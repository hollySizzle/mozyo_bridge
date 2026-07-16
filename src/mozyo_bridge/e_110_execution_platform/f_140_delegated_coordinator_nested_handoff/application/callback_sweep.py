"""Callback sweep use case: derived watermark -> re-read -> fence -> at-most-once recovery (#13889).

The composition the issue's acceptance describes, in one ordered path:

1. **read** the durable journal snapshot and resolve the EXACT dispatch anchor for this
   lane+generation, then derive the verdict from the anchored, ordered watermark (acceptance 1/2/4)
   — the sweep no longer accepts an agent's asserted ``--progress`` boolean;
2. **re-read** the snapshot immediately before any mutation and re-derive the watermark. A gate that
   landed in the decision->send window (the 8-second j#79995 -> j#79996 evidence window) turns the
   verdict into ``progress_without_callback`` and the mutation is refused (acceptance 2/3);
3. **fence** the surviving mutation on :class:`...dispatch_outbox_fence.DispatchOutboxFence`, keyed
   on the dispatch anchor, so recovery is delivered **at most once per gate anchor** even across
   crashes and concurrent sweeps (acceptance 5).

The two reads are the point: step 1 and step 2 are separate ``read_entries`` calls, so the fresh
read is real evidence and not a cached echo of the first. The fence — not the re-read — is the
at-most-once authority; the re-read only stops a *correct-but-stale* verdict from mutating. Both
guards are required: the re-read alone loses a concurrent sweep, and the fence alone would happily
deliver a recovery whose premise expired.

Every failure degrades to **zero-send**: an unreadable source, an unbootstrapped / replaced fence,
or a lost reserve all refuse the mutation. A sweep that cannot prove a stall does not act on one.
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
    send_fn: Optional[Callable[[], object]] = None,
    callback: str = CALLBACK_ABSENT,
    stale_cli: bool = False,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Run one fenced callback sweep for a lane+generation. Returns the verdict + send outcome.

    ``send_fn`` performs the single recovery delivery and is invoked **only** after the re-read and
    the fence reserve both clear. Its absence makes the sweep a read-only classification (the
    fence is not reserved), so a caller can preview the verdict without mutating.

    The returned dict carries the classification (``state`` / ``is_stall`` / ``summary`` /
    ``recovery``), the watermark facts (``dispatch_journal`` / ``progress_journals``), and the
    mutation outcome (``sent`` / ``send_reason`` / ``send_detail``) — so the journal the coordinator
    records is replayable from this output alone, with no after-the-fact correction.
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
    if result["state"] != STATE_NO_PROGRESS_AFTER_HANDOFF:
        decision = decide_recovery(
            decided=decided, rechecked=decided, decided_state=result["state"]
        )
        result["send_reason"] = decision.reason
        result["send_detail"] = decision.detail
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
        return _apply_zero_send(result, decision, rechecked)

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

    try:
        send_fn()
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
        f"the single recovery delivery for dispatch anchor {rechecked.dispatch_journal}"
    )
    return result


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
    journal: str,
    target: str,
    runner: Optional[Callable[[list], "tuple[int, str]"]] = None,
    mozyo_bridge_bin: str = "mozyo-bridge",
) -> Callable[[], object]:
    """Build the production ``send_fn``: ONE ``handoff send`` re-notification of the durable anchor.

    The composition seam that makes :func:`sweep_once` a real recovery path rather than a library
    fixture (review F1). It mirrors :class:`...callback_send_port.HandoffCallbackSendPort` — the
    established sender for this family — including its **injectable ``runner``**: production spawns
    the CLI, tests inject a fake and exercise the whole fenced path with no external send. That
    injectability is why the wiring does not need the (still unauthorized) live dogfood to be
    verified.

    The notification is only a pointer; the durable anchor named by ``issue`` / ``journal`` is the
    truth the receiver must read. A non-zero exit raises :class:`RecoverySendError`, which
    :func:`sweep_once` turns into a fence ``uncertain`` — an ambiguous send is never auto-retried.
    """
    argv = [
        str(mozyo_bridge_bin), "handoff", "send",
        "--to", "codex",
        "--target", str(target),
        "--source", "redmine",
        "--issue", str(issue),
        "--journal", str(journal),
        "--kind", "reply",
        "--mode", "standard",
        "--target-repo", "auto",
    ]

    def _run() -> object:
        run = runner if runner is not None else _default_recovery_runner
        rc, detail = run(list(argv))
        if int(rc) != 0:
            raise RecoverySendError(f"handoff send exited {rc}: {str(detail)[:200]}")
        return {"rc": rc}

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
    "RecoverySendError",
    "read_watermark",
    "sweep_once",
    "build_recovery_sender",
)
