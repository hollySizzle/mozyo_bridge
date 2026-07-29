"""The lane's EXACT current work anchor, joined to lane + generation (Redmine #14586).

A lane has two durable pointers into Redmine, and they are **not** the same pointer:

- the **lifecycle state decision** (``lane_lifecycle`` ``decision_*``) — the record that put the lane
  in its current *state*. A hibernate, a resume, a replacement and a retire each write one. It
  answers "why is this lane in the state it is in";
- the **current work anchor** — the record that delegated the *work this lane is doing now*. It
  answers "what am I working on".

They coincide often enough to look like one field and diverge exactly when it matters: a resume
decision is a lifecycle record about the lane, not an instruction to do something. Re-reading one as
the other is a category error, and this module exists so that neither the resume path nor
``workflow step`` has to make that substitution.

The defect this replaces (#14577 j#90416 F2) is the other way of guessing: with no exact binding to
join on, ``workflow step`` re-derived the work anchor as *the latest gate-bearing marker anywhere in
the issue's history*. On a fresh lane of an issue that had already been through review rounds, the
latest gate-bearing marker is a previous round's callback — so the lane's own dispatch (j#90409) lost
to R6's callback journal. "Latest thing on the issue" is not a binding; it is a heuristic that gets
worse the longer an issue lives.

The exact binding is the canonical **dispatch marker**
(``[mozyo:workflow-event:kind=implementation_request:lane=<lane>:lane_generation=<n>]``), because it
is the only durable record that states *which lane* and *which incarnation of that lane* the work was
delegated to. Resolution joins three facts and fails closed on every disagreement:

1. the lane's own ``(lane, lane_generation)`` binding from the durable lifecycle authority;
2. the **canonical** dispatch markers on the live Redmine record — canonical in the
   :mod:`...domain.canonical_note_scan` sense, so a journal quoting a dispatch marker (a callback
   record echoing the landing marker it observed, say) is not a dispatch (#14585);
3. the newest round the record has opened for this lane, which is what detects a supersede.

Every non-resolution is a distinct, named status rather than a shared "no": which way the join failed
is what an operator needs, and collapsing them is how "your issue's dispatches belong to another
lane" gets reported as "no record found".

**Resolution is stricter than verification, deliberately.** The recovery rail already has
:func:`...domain.recovered_worker_delivery.is_exact_implementation_request_anchor`, which *verifies*
an anchor a caller already named, and which therefore accepts both canonical historical shapes (a
workflow-event marker whose ``gate`` **or** ``kind`` says ``implementation_request``, and a handoff
marker). This module *resolves* an anchor nobody named, from the whole issue, via the same
:func:`...redmine_journal_source.dispatch_entry_journals` authority the callback sweep and the
recovery admission use — which recognizes only the canonical producer's shape
(``kind=implementation_request`` with ``lane`` / ``lane_generation`` on the workflow-event channel).
The asymmetry is the right way round: checking a named record can afford to read legacy shapes,
because the caller has already committed to which record it means; searching for one cannot, because
every extra accepted shape widens what a search may silently land on. The operational consequence is
worth stating plainly rather than discovering: **an implementation request recorded without the
canonical dispatch marker leaves the lane with no resolvable work anchor**, and the fix is for the
coordinator to record one (top level, one line), not for this resolver to guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    dispatch_entry_journals,
    dispatch_generations,
    dispatch_lanes,
)

#: Exactly one canonical dispatch marker names this lane + generation: its OWNING entry journal is
#: the work anchor.
WORK_ANCHOR_RESOLVED = "resolved"
#: The caller has no ``(lane, positive generation)`` binding to join on — an unreadable / absent /
#: non-active lifecycle row, or a generation of zero. Nothing is guessed from the lane id alone.
WORK_ANCHOR_UNBOUND = "unbound"
#: This lane + generation has no canonical dispatch marker on the record. A legacy prose-only
#: implementation request, or a dispatch that was only ever quoted, both land here — never guessed
#: at from the issue's other gate journals.
WORK_ANCHOR_MISSING = "missing"
#: The record carries canonical dispatch markers, but every one of them names a DIFFERENT lane.
WORK_ANCHOR_FOREIGN = "foreign_lane"
#: Two or more distinct entries claim to be this lane + generation's dispatch (duplicate / forged).
WORK_ANCHOR_AMBIGUOUS = "ambiguous"
#: The record has opened a NEWER round for this lane than the one the caller is bound to. Whatever
#: this generation's anchor says, the work it delegated has been superseded.
WORK_ANCHOR_STALE_GENERATION = "stale_generation"


@dataclass(frozen=True)
class LaneWorkAnchor:
    """The outcome of the work-anchor join (a pure value object).

    ``journal`` is non-empty **only** on :data:`WORK_ANCHOR_RESOLVED` — every other status is a
    zero-send, and carrying a "best guess" journal alongside a failure status is how a caller ends
    up using one. ``latest_generation`` (0 when the record shows none) and ``foreign_lanes`` carry
    the facts the failing statuses were derived from, so the refusal is replayable from the result
    without a second read.
    """

    status: str
    journal: str = ""
    lane: str = ""
    lane_generation: int = 0
    latest_generation: int = 0
    foreign_lanes: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.status == WORK_ANCHOR_RESOLVED and bool(self.journal)


def _positive_generation(value: object) -> int:
    """``value`` as a positive int, else 0 (pure; a non-numeric / non-positive binding is no binding)."""
    try:
        generation = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return generation if generation > 0 else 0


def resolve_lane_work_anchor(
    entries, *, lane: str, lane_generation: object
) -> LaneWorkAnchor:
    """Join the lane's ``(lane, generation)`` binding against the record's dispatch markers (pure).

    Returns the single named status the join produced. The order of the checks is deliberate:

    - **binding first.** Without a lane and a positive generation there is nothing to join, and a
      lane id on its own would match any round.
    - **supersede before absence.** A newer round on the record invalidates this one even when this
      round's own anchor is still perfectly resolvable, so the stale verdict must not be reachable
      only via "nothing found". This is what keeps a lane that was re-dispatched from continuing to
      act on the round it happened to be holding.
    - **ambiguity before absence**, so a duplicate is never reported as a clean miss.
    - **foreign before missing**, so a cross-lane read is named as one.
    """
    lane_s = str(lane or "").strip()
    generation = _positive_generation(lane_generation)
    if not lane_s or not generation:
        return LaneWorkAnchor(
            status=WORK_ANCHOR_UNBOUND, lane=lane_s, lane_generation=generation
        )

    rows = list(entries or ())
    generations = dispatch_generations(rows, lane=lane_s)
    latest = generations[-1] if generations else 0
    base = dict(lane=lane_s, lane_generation=generation, latest_generation=latest)

    if latest > generation:
        return LaneWorkAnchor(status=WORK_ANCHOR_STALE_GENERATION, **base)

    journals = dispatch_entry_journals(rows, lane=lane_s, lane_generation=generation)
    if len(journals) >= 2:
        return LaneWorkAnchor(status=WORK_ANCHOR_AMBIGUOUS, **base)
    if not journals:
        lanes = dispatch_lanes(rows)
        if lanes and lane_s not in lanes:
            return LaneWorkAnchor(
                status=WORK_ANCHOR_FOREIGN, foreign_lanes=lanes, **base
            )
        return LaneWorkAnchor(status=WORK_ANCHOR_MISSING, **base)
    return LaneWorkAnchor(status=WORK_ANCHOR_RESOLVED, journal=journals[0], **base)


__all__ = (
    "WORK_ANCHOR_RESOLVED",
    "WORK_ANCHOR_UNBOUND",
    "WORK_ANCHOR_MISSING",
    "WORK_ANCHOR_FOREIGN",
    "WORK_ANCHOR_AMBIGUOUS",
    "WORK_ANCHOR_STALE_GENERATION",
    "LaneWorkAnchor",
    "resolve_lane_work_anchor",
)
