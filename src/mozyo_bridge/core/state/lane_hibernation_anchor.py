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

**A row with no anchor has NO boundary, and that is reported rather than substituted.** An
earlier revision of this surface fell back to ``updated_at`` for a pre-v8 row, arguing that
``updated_at >= hibernated_at`` always holds and so the fallback could only be stricter. That
argument was wrong, and Redmine #14477 review j#94515 (verdict j#94520) reproduced the
consequence: the invariant holds only for rows that CARRY an anchor — i.e. exactly the rows
that never take the fallback — and says nothing about a row whose anchor is empty. Nothing
enforces it either: every CAS on this component accepts a caller-supplied ``now`` and no
writer validates it against the row's prior stamp, so a regressing stamp (a backdated
programmatic caller, an NTP step-back, a skewed host clock) leaves ``updated_at`` EARLIER
than the true hibernation. The measured result was a threshold below the real boundary, which
admitted a genuine pre-hibernate survivor and flipped the lane to ``active``.

So a legacy row now resolves to :data:`ANCHOR_UNAVAILABLE` and the caller must fail closed.
There is no safe substitute to reach for: ``created_at`` predates the hibernation by even
more, the release generation carries no timestamp, and enforcing monotonicity going forward
cannot retro-fit rows an older build already wrote. The honest options are "refuse" or
"guess", and a generation proof may not guess. The current wall clock is likewise never
consulted — it would admit any pane merely observed *recently*, the exact inverse of a
generation proof.

The operational cost is stated plainly: a lane hibernated by a pre-v8 build cannot resume
through the standard rail until it passes through a v8 hibernate transition. Old rows stay
fully READABLE (``get`` / ``records`` / the non-migrating read path are untouched); what is
withheld is only the freshness *proof*, which for those rows never actually existed.
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
#: authority, unmoved by any later metadata write. The ONLY value that proves freshness.
ANCHOR_HIBERNATE_TRANSITION = "hibernate_transition"
#: No boundary exists to compare against: there is no row, or the row carries no
#: hibernate-transition stamp (a pre-v8 row, or one hibernated by an older build). The
#: freshness half of the proof cannot run, so the caller MUST fail closed rather than treat an
#: absent threshold as "nothing to compare, therefore fresh". Deliberately NOT substituted
#: with ``updated_at`` — that substitution admitted a real survivor (review j#94515, verdict
#: j#94520); see this module's docstring for why no safe substitute exists.
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

    The immutable hibernate-transition stamp when the row carries one, else an EMPTY threshold
    under :data:`ANCHOR_UNAVAILABLE` — which the caller must read as "the freshness half cannot
    be proven", never as "no threshold to fail". ``updated_at`` is deliberately not consulted:
    it is not a boundary, and using it as one admitted a real pre-hibernate survivor
    (review j#94515, verdict j#94520).
    """
    if record is None:
        return "", ANCHOR_UNAVAILABLE
    anchor = norm(record.hibernated_at)
    if anchor:
        return anchor, ANCHOR_HIBERNATE_TRANSITION
    return "", ANCHOR_UNAVAILABLE


__all__ = (
    "HIBERNATION_ANCHOR_COLUMN",
    "ANCHOR_HIBERNATE_TRANSITION",
    "ANCHOR_UNAVAILABLE",
    "hibernation_anchor_on_transition",
    "resume_freshness_anchor",
)
