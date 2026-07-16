"""Dispatch-anchored callback-sweep watermark + zero-send recovery decision (Redmine #13889).

The sweep's progress verdict used to be an operator/agent **assertion**: ``sublane
callback-recovery --progress`` took a hand-set boolean that an agent derived from a journal read
it had performed *earlier*. That read is the coordinator-local cutoff the issue names: a durable
gate landing between the agent's read and its verdict is invisible, so the sweep records
``no_progress_after_handoff`` for a lane that is not stopped, sends a recovery mutation, and a
later journal has to correct it to ``progress_without_callback`` after the fact (#13883 evidence
j#79995 -> j#79996 at 8s; j#80002 -> j#80005 -> a duplicate replay at j#80006).

This module makes the verdict **derived, anchored and ordered** instead:

- **anchored** — progress is measured strictly after the EXACT dispatch anchor (the owning
  journal of this lane+generation's ``implementation_request`` marker, resolved by
  :func:`...redmine_journal_source.resolve_dispatch_entry_journal`), never after "the newest
  journal the coordinator happened to have read";
- **ordered** — the before/after test is an integer compare on the **durable Redmine journal id**
  (a monotonic autoincrement PK), never a wall-clock timestamp. The 8-second gap in the evidence
  is exactly what a clock-based cutoff cannot resolve and an id compare resolves exactly;
- **fail-closed** — no dispatch anchor means no baseline, so there is no verdict and no mutation
  (mirrors the #13758 leg's blank-anchor branch). A ``0`` baseline is never fabricated.

Progress vocabulary (issue #13889 未確認事項 1 / j#80071). ``GATE_BEARING_KINDS`` is the
**callback-required** set — the states that must WAKE the coordinator. It deliberately does not
contain the worker-side gates that prove a lane is alive but owe no callback, so the evidence's
``Gate: review_finding_verdict`` (j#80002) is invisible to it and anchoring alone would NOT fix
that case. Following the precedent :data:`...redmine_journal_source.DISPATCH_KIND_IMPLEMENTATION_REQUEST`
set (#13758 R5-F3: a dispatch is not a callback gate, so it must not widen the gate vocabulary),
this module adds a **separate** :data:`PROGRESS_BEARING_KINDS` closed vocabulary rather than
widening the callback-required one. Progress is the union: any callback-required gate landing is
also progress, but progress never implies a callback is owed.

Why marker-based and not "any newer journal": the coordinator's own stall-check / recovery
journals are themselves newer entries, so counting entries would make the sweep read its own
recovery as the lane's progress. Author-based exclusion cannot separate them either — in the
observed workspace the coordinator and the worker post as the **same** Redmine user (#13889
evidence journals are all author id 5). A structured closed-vocabulary marker is the only
discriminator that holds, and it keeps the module inside the repo's standing rule: read the
machine token, never the prose.

Pure / read-only: every input is a fact the caller read from the durable record. No I/O, no tmux,
no self-authorized close.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    GATE_BEARING_KINDS,
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    STATE_NO_PROGRESS_AFTER_HANDOFF,
    classify_callback_stall,
)

# --- Progress vocabulary (SEPARATE from the callback-required gate vocabulary) ---------------
#: Worker-side durable gates that prove the lane advanced but owe the coordinator **no** callback.
#: Kept disjoint from :data:`GATE_BEARING_KINDS` on purpose — widening that set would turn each of
#: these into a coordinator wake, which is precisely the duplicate-notification failure #13889 is
#: about. ``review_finding_verdict`` is the evidence case (j#80002): a verdict is a durable gate the
#: worker MUST record before acting on a finding, so a lane that just recorded one is demonstrably
#: not stalled.
PROGRESS_KIND_START = "start"
PROGRESS_KIND_PROGRESS_LOG = "progress_log"
PROGRESS_KIND_REVIEW_FINDING_VERDICT = "review_finding_verdict"
PROGRESS_KIND_DESIGN_CONSULTATION = "design_consultation"

PROGRESS_BEARING_KINDS: frozenset[str] = frozenset(
    {
        PROGRESS_KIND_START,
        PROGRESS_KIND_PROGRESS_LOG,
        PROGRESS_KIND_REVIEW_FINDING_VERDICT,
        PROGRESS_KIND_DESIGN_CONSULTATION,
    }
)

#: Everything that counts as "the lane advanced after the dispatch". A callback-required gate is
#: also progress; the reverse does not hold.
QUALIFYING_PROGRESS_KINDS: frozenset[str] = frozenset(PROGRESS_BEARING_KINDS | GATE_BEARING_KINDS)

#: The sweep verdict for a lane whose dispatch has no durable structured anchor (a legacy
#: prose-only IR). Not a stall — an unanchorable baseline, so the sweep abstains rather than
#: guessing (fail-closed; #13758 R5-F3 takes the same branch).
SWEEP_STATE_ANCHOR_MISSING = "dispatch_anchor_missing"

#: The sweep verdict when post-anchor journals exist that carry no recognized marker: the lane may
#: have advanced in prose, so a stall cannot be PROVEN and none is asserted (review F2). Not a
#: stall and never a mutation — declaring one on an unreadable record is the stale replay itself.
SWEEP_STATE_STALL_UNPROVABLE = "stall_unprovable"

#: The fence ``action_id`` every sweep recovery delivery reserves under. Combined with the fence
#: key's ``journal`` (the dispatch anchor) this makes recovery **at most once per gate anchor**.
SWEEP_RECOVERY_ACTION_ID = "callback_sweep_recovery"

# --- Zero-send reasons (the closed vocabulary the mutation edge reports) ----------------------
SEND_RESERVED = "reserved"  # the single caller cleared to perform the one recovery delivery
ZERO_SEND_NOT_A_STALL = "not_a_stall"
ZERO_SEND_ANCHOR_MISSING = "dispatch_anchor_missing"
ZERO_SEND_PROGRESS_LANDED = "progress_landed_after_decision"
ZERO_SEND_STALL_UNPROVABLE = "stall_unprovable"
ZERO_SEND_DISPATCH_ROUND_CHANGED = "dispatch_round_changed"
ZERO_SEND_FENCE_HELD = "fence_held"
ZERO_SEND_FENCE_UNAVAILABLE = "fence_unavailable"


def _journal_int(value: object, default: int = -1) -> int:
    """A durable journal id as an int for ordered comparison, or ``default`` (pure)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _entry_progress_kinds(entry: object, *, lane: str, lane_generation: object) -> set[str]:
    """The qualifying progress kinds a journal entry's markers name FOR THIS ROUND (pure).

    Scoped to ``lane`` + ``lane_generation`` (review F3): a progress marker carries its own lane /
    generation, so round N's watermark cannot absorb round N+1's progress. The dispatch marker was
    already round-scoped this way; progress must match, or a superseded round looks alive because
    its successor is working. A gate kind (:data:`GATE_BEARING_KINDS`) may arrive on either channel
    and is accepted un-scoped **only** on the workflow-event channel — the handoff channel carries
    *pointers to* gates, not gate landings, so counting one would let the coordinator's own dispatch
    notification masquerade as the lane's progress.
    """
    lane_s = str(lane or "").strip()
    gen_s = str(lane_generation if lane_generation is not None else "").strip()
    kinds: set[str] = set()
    for channel, fields in marker_fields_in_note(str(getattr(entry, "notes", "") or "")):
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        named = (fields.get("gate") or fields.get("kind") or "").strip()
        if named not in QUALIFYING_PROGRESS_KINDS:
            continue
        marker_lane = str(fields.get("lane", "")).strip()
        marker_gen = str(fields.get("lane_generation", "")).strip()
        # A round-scoped marker must match this round exactly. An unscoped marker (a legacy gate
        # note that predates scoping) is accepted for compatibility — it cannot be attributed to a
        # different round because nothing on it names one.
        if marker_lane and marker_lane != lane_s:
            continue
        if marker_gen and marker_gen != gen_s:
            continue
        kinds.add(named)
    return kinds


def _entry_is_classified(entry: object) -> bool:
    """True when the entry carries ANY recognized structured marker (pure).

    The discriminator behind :data:`SWEEP_STATE_STALL_UNPROVABLE`: an entry the reader recognizes
    (a gate, a progress note, a dispatch, a handoff pointer) is *understood*, whether or not it
    counts as progress. An entry with no recognized token is **opaque** — it could be a worker's
    prose gate or coordinator noise, and nothing on it says which.
    """
    return bool(marker_fields_in_note(str(getattr(entry, "notes", "") or "")))


def progress_entries_after(
    entries: Iterable[object],
    *,
    after_journal: object,
    lane: str = "",
    lane_generation: object = "",
) -> tuple[tuple[str, str], ...]:
    """The ``(journal_id, kind)`` pairs strictly after the anchor that prove progress (pure, sorted).

    The **ordered durable journal id** compare at the heart of #13889 acceptance 1/2: an entry
    qualifies when its OWN journal id (the durable anchor authority — never the marker's
    self-reported ``journal=`` field) is numerically greater than ``after_journal`` and it carries a
    :data:`QUALIFYING_PROGRESS_KINDS` marker for this ``lane`` / ``lane_generation``. Sorted by
    journal id so the result is replay-stable. An unparseable / blank anchor yields ``()`` — the
    caller must treat that as *unanchored*, not as *no progress* (:func:`resolve_watermark` enforces
    the distinction).
    """
    anchor = _journal_int(after_journal)
    if anchor < 0:
        return ()
    found: dict[int, str] = {}
    for entry in entries or ():
        jid = _journal_int(getattr(entry, "journal_id", ""))
        if jid <= anchor:
            continue
        kinds = _entry_progress_kinds(entry, lane=lane, lane_generation=lane_generation)
        if not kinds:
            continue
        found[jid] = sorted(kinds)[0]
    return tuple((str(j), found[j]) for j in sorted(found))


def opaque_entries_after(
    entries: Iterable[object], *, after_journal: object
) -> tuple[str, ...]:
    """The journal ids strictly after the anchor that carry NO recognized marker (pure, sorted).

    Review F2. The real #13883 records are **prose-only**: neither j#79995 (``## Gate: review —
    changes_requested``) nor j#80002 (``## Gate: review_finding_verdict``) carries a marker. A
    marker-only reader sees no progress there and, if it then declares a stall, performs exactly the
    stale replay this issue exists to remove — the re-read cannot save it, because the re-read is
    equally blind, and the fence only stops the *second* such send.

    So the sweep must distinguish two very different situations it previously conflated:

    - **nothing happened** — no post-anchor entries at all. A stall is *provable*.
    - **something happened that I cannot read** — post-anchor entries with no recognized marker. The
      lane may be advancing in prose. A stall is *unprovable*, and asserting one is a guess.

    Declaring a stall requires proving silence, not merely failing to recognize speech. These ids
    are that proof obligation: non-empty means the sweep must abstain
    (:data:`SWEEP_STATE_STALL_UNPROVABLE`) rather than mutate on an unreadable record. As producers
    are wired onto the canonical marker writers this set drains to empty and the sweep regains full
    precision — without ever guessing at prose.
    """
    anchor = _journal_int(after_journal)
    if anchor < 0:
        return ()
    found: list[int] = []
    for entry in entries or ():
        jid = _journal_int(getattr(entry, "journal_id", ""))
        if jid <= anchor:
            continue
        if not _entry_is_classified(entry):
            found.append(jid)
    return tuple(str(j) for j in sorted(found))


@dataclass(frozen=True)
class SweepWatermark:
    """The dispatch-anchored progress watermark for one lane+generation's sweep (pure value).

    ``dispatch_journal`` is the exact anchor (blank -> unanchored -> no verdict). ``progress`` is
    the ordered ``(journal_id, kind)`` list of qualifying gates strictly after it. ``opaque`` is the
    post-anchor entries carrying no recognized marker — non-empty means a stall is unprovable
    (review F2). ``lane_generation`` is the round this watermark describes and ``latest_generation``
    is the newest round the record shows for the lane; a newer one means this watermark is about a
    superseded round (review F3).
    """

    dispatch_journal: str
    progress: tuple[tuple[str, str], ...] = ()
    opaque: tuple[str, ...] = ()
    lane_generation: int = 0
    latest_generation: int = 0

    @property
    def anchored(self) -> bool:
        return bool(self.dispatch_journal)

    @property
    def has_progress(self) -> bool:
        return bool(self.progress)

    @property
    def stall_provable(self) -> bool:
        """True only when the record proves silence: no progress AND nothing unreadable after it."""
        return not self.progress and not self.opaque

    @property
    def superseded(self) -> bool:
        """True when a newer dispatch round has opened since the round this watermark describes."""
        return bool(self.latest_generation and self.latest_generation > self.lane_generation)

    @property
    def latest_progress_journal(self) -> str:
        return self.progress[-1][0] if self.progress else ""

    @property
    def progress_journals(self) -> tuple[str, ...]:
        return tuple(j for j, _kind in self.progress)


def resolve_watermark(
    entries: Iterable[object],
    *,
    dispatch_journal: object,
    lane: str = "",
    lane_generation: object = "",
    latest_generation: object = 0,
) -> SweepWatermark:
    """Resolve the dispatch-anchored watermark from a journal snapshot (pure).

    ``dispatch_journal`` is the exact anchor from
    :func:`...redmine_journal_source.resolve_dispatch_entry_journal`. A blank / unparseable anchor
    yields an **unanchored** watermark (``anchored=False``): the sweep then has no baseline and must
    abstain — it never falls back to a fabricated ``0``, which would make every journal on the issue
    look like post-dispatch progress. ``latest_generation`` is the newest dispatch round on the
    record (:func:`...redmine_journal_source.dispatch_generations`), which lets the caller detect
    that it is reasoning about a superseded round.
    """
    anchor = str(dispatch_journal or "").strip()
    gen = _journal_int(lane_generation, default=0)
    if _journal_int(anchor) < 0:
        return SweepWatermark(
            dispatch_journal="",
            lane_generation=gen,
            latest_generation=_journal_int(latest_generation, default=0),
        )
    return SweepWatermark(
        dispatch_journal=anchor,
        progress=progress_entries_after(
            entries, after_journal=anchor, lane=lane, lane_generation=lane_generation
        ),
        opaque=opaque_entries_after(entries, after_journal=anchor),
        lane_generation=gen,
        latest_generation=_journal_int(latest_generation, default=0),
    )


def classify_sweep(
    *,
    watermark: SweepWatermark,
    callback: str = CALLBACK_ABSENT,
    stale_cli: bool = False,
) -> dict[str, Any]:
    """Classify the sweep from the **derived** watermark instead of an asserted boolean (pure).

    This is the #13889 acceptance 4 first-pass verdict: ``new_durable_progress`` is *computed* from
    the anchored, ordered watermark at decision time, so a gate that landed 8 seconds before the
    sweep is seen on the FIRST pass and classified ``progress_without_callback`` — there is no
    after-the-fact correction journal in the design.

    An unanchored watermark returns :data:`SWEEP_STATE_ANCHOR_MISSING` (``is_stall=False``): with no
    durable dispatch identity there is nothing to measure progress against, so the sweep abstains.
    The returned dict extends :func:`...sublane_callback.classify_callback_stall`'s shape with the
    watermark facts, so the verdict is auditable from the output alone.
    """
    if not watermark.anchored:
        return {
            "state": SWEEP_STATE_ANCHOR_MISSING,
            "is_stall": False,
            "dispatch_delivered": False,
            "new_durable_progress": False,
            "callback": callback,
            "stale_cli": bool(stale_cli),
            "summary": (
                "no durable structured dispatch marker for this lane+generation — the sweep has "
                "no anchor to measure progress against, so it abstains (fail-closed) rather than "
                "baselining on a fabricated 0"
            ),
            "recovery": [
                "record the Implementation Request through the canonical writer so it carries the "
                "`[mozyo:workflow-event:kind=implementation_request:lane=...:lane_generation=...]` "
                "marker; the sweep anchors on that entry's own journal id",
                "a legacy prose-only IR is never parse-guessed — re-record it structurally",
            ],
            "invariants": [],
            "dispatch_journal": "",
            "progress_journals": [],
        }

    if not watermark.has_progress and watermark.opaque:
        # Review F2: post-anchor entries exist that carry no recognized marker (the real #13883
        # j#79995 / j#80002 shape). "I found no progress marker" is not "nothing happened" — the
        # lane may be advancing in prose. Declaring a stall here IS the stale replay this issue
        # removes, so abstain instead of guessing.
        return {
            "state": SWEEP_STATE_STALL_UNPROVABLE,
            "is_stall": False,
            "dispatch_delivered": True,
            "new_durable_progress": False,
            "callback": callback,
            "stale_cli": bool(stale_cli),
            "summary": (
                f"{len(watermark.opaque)} journal(s) landed after the dispatch anchor carrying no "
                f"recognized structured marker (j#"
                f"{', j#'.join(watermark.opaque)}) — the sweep cannot tell lane progress from "
                f"unrelated noise, so a stall is UNPROVABLE and no recovery is sent"
            ),
            "recovery": [
                "read the named journal(s) directly to see whether the lane advanced; the durable "
                "record is the truth, the sweep only reports what it can prove",
                "record the lane's gates through the canonical marker-bearing writers "
                "(`workflow callbacks --emit-gate` / `--emit-progress`) so the sweep can classify "
                "them structurally instead of abstaining",
                "do NOT re-dispatch on this verdict — it is an abstention, not a stall",
            ],
            "invariants": [],
            "dispatch_journal": watermark.dispatch_journal,
            "progress_journals": [],
            "opaque_journals": list(watermark.opaque),
        }

    result = classify_callback_stall(
        dispatch_delivered=True,  # an exact dispatch anchor IS the delivered dispatch journal
        new_durable_progress=watermark.has_progress,
        callback=callback,
        stale_cli=stale_cli,
    )
    result["dispatch_journal"] = watermark.dispatch_journal
    result["progress_journals"] = [
        {"journal": j, "kind": kind} for j, kind in watermark.progress
    ]
    result["opaque_journals"] = list(watermark.opaque)
    return result


def render_progress_marker(kind: str, *, lane: str, lane_generation: object) -> str:
    """Render the structured progress marker a worker-side durable gate embeds (pure).

    The **producer** inverse of :func:`progress_entries_after`, mirroring
    :func:`...redmine_journal_source.render_dispatch_marker`: a worker recording e.g. a
    ``review_finding_verdict`` gate embeds this token so the sweep can DISCOVER the gate
    structurally. Without it the gate is prose and the sweep abstains
    (:data:`SWEEP_STATE_STALL_UNPROVABLE`) — never parse-guessed.

    ``lane`` / ``lane_generation`` are **required** and scope the marker to its round (review F3):
    the dispatch marker is round-scoped, so progress must be too, or round N's watermark absorbs
    round N+1's progress and a superseded round looks alive because its successor is working.
    ``kind`` must be a :data:`PROGRESS_BEARING_KINDS` member; a callback-required gate uses
    ``render_gate_note`` instead (it owes a callback, which this token deliberately does not
    signal).
    """
    kind_s = str(kind).strip()
    if kind_s not in PROGRESS_BEARING_KINDS:
        raise ValueError(
            f"render_progress_marker kind must be one of {sorted(PROGRESS_BEARING_KINDS)}, "
            f"got {kind!r} (a callback-required gate uses render_gate_note)"
        )
    lane_s = str(lane or "").strip()
    gen_s = str(lane_generation if lane_generation is not None else "").strip()
    if not (lane_s and gen_s):
        raise ValueError(
            "render_progress_marker requires a lane and lane_generation: an unscoped progress "
            "marker cannot be attributed to a dispatch round (Redmine #13889 review F3)"
        )
    return (
        f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:"
        f"kind={kind_s}:lane={lane_s}:lane_generation={gen_s}]"
    )


def render_progress_note(
    kind: str, *, lane: str, lane_generation: object, body: str = ""
) -> str:
    """A canonical progress-gate note: prose ``body`` + the round-scoped progress marker (pure)."""
    marker = render_progress_marker(kind, lane=lane, lane_generation=lane_generation)
    body_s = str(body or "").rstrip()
    return f"{body_s}\n\n{marker}" if body_s else marker


@dataclass(frozen=True)
class RecoveryDecision:
    """Whether the sweep may perform its one recovery mutation, and why not when it may not."""

    send: bool
    reason: str
    detail: str = ""

    @property
    def zero_send(self) -> bool:
        return not self.send


def decide_recovery(
    *, decided: SweepWatermark, rechecked: SweepWatermark, decided_state: str
) -> RecoveryDecision:
    """The TOCTOU close-out: re-verify the watermark immediately before the mutation (pure).

    #13889 acceptance 2/3. ``decided`` is the watermark the verdict was formed from; ``rechecked``
    is a **fresh** read taken at the mutation edge. The mutation is refused (zero-send) when:

    - the verdict was not a ``no_progress_after_handoff`` stall (nothing to recover);
    - the re-read lost its anchor, or resolved a **different** dispatch anchor — a new dispatch
      round raced the sweep, so this verdict is about a round that no longer exists;
    - the re-read shows qualifying progress. This is the exact evidence window: the gate landed
      between the decision and the send, so the lane is not stalled and the recovery would be the
      duplicate replay. The caller records ``progress_without_callback`` instead.

    Only a verdict that still holds against the fresh read proceeds — and even then the fence, not
    this function, is the at-most-once authority.
    """
    if decided_state != STATE_NO_PROGRESS_AFTER_HANDOFF:
        return RecoveryDecision(
            send=False,
            reason=ZERO_SEND_NOT_A_STALL,
            detail=f"verdict {decided_state!r} owes no recovery mutation",
        )
    if not rechecked.anchored:
        return RecoveryDecision(
            send=False,
            reason=ZERO_SEND_ANCHOR_MISSING,
            detail="the re-read resolved no dispatch anchor; abstain rather than mutate",
        )
    # Review F3: comparing the two reads' anchors could never detect a new round — both reads
    # resolve the anchor for the SAME caller-fixed generation, so they always agree. The round
    # authority is the newest dispatch generation on the record, read WITHOUT fixing a generation.
    if rechecked.superseded or rechecked.dispatch_journal != decided.dispatch_journal:
        return RecoveryDecision(
            send=False,
            reason=ZERO_SEND_DISPATCH_ROUND_CHANGED,
            detail=(
                f"a newer dispatch round opened (generation {decided.lane_generation} -> "
                f"{rechecked.latest_generation}; anchor {decided.dispatch_journal} -> "
                f"{rechecked.dispatch_journal}) between the decision and the send; the verdict "
                f"describes a superseded round"
            ),
        )
    if rechecked.has_progress:
        return RecoveryDecision(
            send=False,
            reason=ZERO_SEND_PROGRESS_LANDED,
            detail=(
                f"qualifying progress landed at journal "
                f"{rechecked.latest_progress_journal} after the dispatch anchor "
                f"{rechecked.dispatch_journal}; the lane is not stalled — record "
                f"progress_without_callback, do NOT replay"
            ),
        )
    if not rechecked.stall_provable:
        # Opaque activity landed in the decision->send window: same obligation as at classify time
        # (review F2) — the sweep cannot prove the lane is silent, so it must not mutate.
        return RecoveryDecision(
            send=False,
            reason=ZERO_SEND_STALL_UNPROVABLE,
            detail=(
                f"journal(s) with no recognized marker landed after the anchor (j#"
                f"{', j#'.join(rechecked.opaque)}); the lane may be advancing in prose, so the "
                f"stall is unprovable — abstain rather than replay"
            ),
        )
    return RecoveryDecision(
        send=True,
        reason=SEND_RESERVED,
        detail="the stall verdict still holds against a fresh read; the fence gates the one send",
    )


__all__ = (
    "PROGRESS_KIND_START",
    "PROGRESS_KIND_PROGRESS_LOG",
    "PROGRESS_KIND_REVIEW_FINDING_VERDICT",
    "PROGRESS_KIND_DESIGN_CONSULTATION",
    "PROGRESS_BEARING_KINDS",
    "QUALIFYING_PROGRESS_KINDS",
    "SWEEP_STATE_ANCHOR_MISSING",
    "SWEEP_STATE_STALL_UNPROVABLE",
    "SWEEP_RECOVERY_ACTION_ID",
    "SEND_RESERVED",
    "ZERO_SEND_NOT_A_STALL",
    "ZERO_SEND_ANCHOR_MISSING",
    "ZERO_SEND_PROGRESS_LANDED",
    "ZERO_SEND_STALL_UNPROVABLE",
    "ZERO_SEND_DISPATCH_ROUND_CHANGED",
    "ZERO_SEND_FENCE_HELD",
    "ZERO_SEND_FENCE_UNAVAILABLE",
    "SweepWatermark",
    "RecoveryDecision",
    "render_progress_marker",
    "render_progress_note",
    "progress_entries_after",
    "opaque_entries_after",
    "resolve_watermark",
    "classify_sweep",
    "decide_recovery",
)
