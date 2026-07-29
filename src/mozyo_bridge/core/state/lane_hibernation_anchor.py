"""Immutable hibernate-transition freshness anchor (Redmine #14477).

``sublane resume`` proves that a relaunched pair is a GENUINE post-hibernate generation by
requiring each slot's #13637 startup self-attestation to be observed strictly AFTER the lane
hibernated (Redmine #13682). The locator alone cannot carry that proof: a pane that
**survived** hibernate's release keeps its tmux pane-id and therefore still matches its own
*pre-hibernate* attestation, so the freshness half needs a timestamp boundary. That gate is
only ever as good as the timestamp it compares against.

Before this surface the boundary was read from the row's generic ``updated_at`` — the column
EVERY lifecycle write moves. A metadata-only write therefore moved the freshness boundary
forward while no process changed at all, and the measured consequence (#14476
j#88614-j#88618) was that ``sublane repair-pins`` — which fills a hibernated bound row's
EMPTY declared-pin snapshot and bumps the revision / decision anchor, launching, closing,
resuming and sending nothing — pushed the boundary PAST the self-attestation of the exact
live pair it had just verified. ``recover-pair`` / ``resume`` then refused that pair
``stale_generation`` forever, and the only way forward was an action-specific glass-break.

The fix is a **responsibility split, not a relaxed gate**. ``hibernated_at`` (schema v8) is
written by exactly ONE event — the disposition CAS that moves a lane INTO ``hibernated`` —
and is never touched by a revision bump, a decision-anchor update, a declared-pin repair, a
release / replacement write, or a reconcile phase. Metadata may move ``updated_at`` as often
as it likes; the boundary the freshness proof compares against does not move with it. Every
existing fence stays exactly where it was: this module supplies a *threshold*, and decides
nothing about locators, providers, multiplicity, or attestation validity.

**Every fallback here points fail-closed.** For any row THIS build hibernated,
``updated_at >= hibernated_at`` always holds — both are stamped by the same write, and only
``updated_at`` advances afterwards — so falling back to ``updated_at`` yields a threshold
that is equal or LATER, i.e. a STRICTER gate, never a looser one. That is what makes the
pre-v8 compatibility disposition safe rather than a guess: a row hibernated by an older
build carries an empty anchor and keeps precisely its pre-#14477 (over-strict) boundary. The
current wall clock is never consulted — it would admit any pane merely observed *recently*,
which is the opposite of a generation proof — and an absent boundary is reported as absent
so the caller can refuse, never silently skipped.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    LaneLifecycleRecord,
    norm,
)

#: The v8 lifecycle column this module owns the semantics of. Named here so the schema, the
#: CAS writers and the resume reader all spell one authority the same way.
HIBERNATION_ANCHOR_COLUMN = "hibernated_at"

#: The boundary came from the immutable hibernate-transition stamp — the post-#14477
#: authority, unmoved by any later metadata write.
ANCHOR_HIBERNATE_TRANSITION = "hibernate_transition"
#: The row carries no hibernate-transition stamp (a pre-v8 row, or one hibernated by an
#: older build), so the pre-#14477 generic lifecycle ``updated_at`` is used instead. It is
#: equal-or-later than the true boundary, so the gate is stricter — never weaker — and this
#: token makes that compatibility disposition explicit in the typed outcome instead of
#: leaving the operator to infer which authority answered.
ANCHOR_LIFECYCLE_UPDATED_AT = "lifecycle_updated_at_pre_v8"
#: No boundary could be resolved at all (no row, or a row carrying neither stamp). The
#: freshness half of the proof cannot run, so the caller must fail closed rather than treat
#: an absent threshold as "nothing to compare, therefore fresh".
ANCHOR_UNAVAILABLE = "unavailable"


def hibernation_anchor_on_transition(current: str, *, target: str, stamp: str) -> str:
    """This row's ``hibernated_at`` after a disposition CAS to ``target``.

    - into ``hibernated``: ``stamp`` — the transition **is** the boundary event, and this is
      the only place the column is ever written;
    - into ``active`` (a resume rehydrate, a recovery-lane promotion, a new generation):
      ``""`` — an awake lane has no boundary in force. Clearing is the fail-closed
      direction, not merely tidier: a stale earlier boundary left on a row that some future
      writer moved back to ``hibernated`` **without** stamping would be a threshold far in
      the past (a looser gate), whereas an empty one falls back to ``updated_at``, which is
      that write's own stamp (a correct, and at worst stricter, gate);
    - any other target (``superseded`` / ``retired``): preserved byte-for-byte — a terminal
      row keeps the boundary it was hibernated at as an audit fact.
    """
    if target == DISPOSITION_HIBERNATED:
        return stamp
    if target == DISPOSITION_ACTIVE:
        return ""
    return current


def resume_freshness_anchor(
    record: Optional[LaneLifecycleRecord],
) -> tuple[str, str]:
    """``(threshold, authority)`` for the post-hibernate freshness proof of ``record``.

    The immutable hibernate-transition stamp when the row carries one, else the pre-#14477
    generic ``updated_at`` under the explicit :data:`ANCHOR_LIFECYCLE_UPDATED_AT`
    compatibility disposition (equal-or-later than the true boundary, so stricter), else
    :data:`ANCHOR_UNAVAILABLE` with an empty threshold — which the caller must read as "the
    freshness half cannot be proven", never as "no threshold to fail".
    """
    if record is None:
        return "", ANCHOR_UNAVAILABLE
    anchor = norm(record.hibernated_at)
    if anchor:
        return anchor, ANCHOR_HIBERNATE_TRANSITION
    legacy = norm(record.updated_at)
    if legacy:
        return legacy, ANCHOR_LIFECYCLE_UPDATED_AT
    return "", ANCHOR_UNAVAILABLE


__all__ = (
    "HIBERNATION_ANCHOR_COLUMN",
    "ANCHOR_HIBERNATE_TRANSITION",
    "ANCHOR_LIFECYCLE_UPDATED_AT",
    "ANCHOR_UNAVAILABLE",
    "hibernation_anchor_on_transition",
    "resume_freshness_anchor",
)
