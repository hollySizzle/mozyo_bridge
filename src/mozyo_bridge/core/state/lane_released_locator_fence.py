"""Clock-independent survivor fence from the release generation (Redmine #14477 disposition A).

``sublane resume`` must refuse a pane that **survived** hibernate's release. Until this surface
that proof was purely a timestamp comparison: the pane's startup self-attestation had to be
observed after the lane's hibernate boundary. Review j#94531 R2-F1 showed a timestamp can never
carry that proof, because three independent vectors defeat it and changing *which* clock is
trusted only closes the first:

1. the hibernate CAS stores a caller-supplied ``now``, and no writer on the lifecycle component
   validates it against the row's prior stamp — a backdated stamp admits a real survivor;
2. a regressed host wall clock (NTP step-back, skew) produces the same ordering even from a
   trusted clock, because the pane's attestation was recorded before the clock moved;
3. ``observed_at`` is written by the attesting process itself, so a misconfigured or hostile
   pane can claim a later time.

The fence here uses a fact that no clock can rewrite: **hibernate's release recorded the exact
locators it closed** (``release_pins`` on the lifecycle row, authority-grade). A survivor keeps
its tmux pane-id, so its locator is *in that set*; a genuine relaunch is assigned a new pane-id,
so its locator is *not*. Comparing locators therefore refuses all three vectors at once
(coordinator disposition j#94544 A.1).

**Absent, unreadable or incomplete evidence is a REFUSAL, not a pass** (j#94544 A.2). The row
cannot distinguish "no process existed at hibernate" from "a survivor existed and no release
evidence was recorded", so absence of evidence is never read as freshness. This is a deliberate
functional regression for a lane hibernated without a completed release generation.

**Locator reuse is accepted as a false refusal in the safe direction** (j#94544 A.4). tmux may
recycle a pane-id after enough churn, so a genuine relaunch can land on a locator the previous
generation used. That yields a refusal, never an admission, and the typed reason names it so an
operator can tell it apart from a real survivor.

Completeness is deliberately **not** keyed on the pins' ``role`` field. That vocabulary is not
consistent across callers (some record lane roles like ``gateway`` / ``worker``, others record
provider names), so a role-based rule would silently mis-evaluate. The rule is instead on
distinct locators: the released set must cover at least as many distinct locators as there are
observed live slots, or the uncovered slot is unproven.

This fence does not replace the timestamp comparison and does not relax any existing gate. It is
one more conjunct: resume requires the released-locator inequality AND the existing attestation /
provider / generation / declared-pin fences (j#94544 A.3, A.6). The correct long-term proof — an
authority-grade lane epoch bound into the startup attestation, with resume requiring a strictly
newer epoch — is Redmine #14756 (disposition B); this surface is the immediate fence, not that
design.
"""

from __future__ import annotations

from typing import Iterable, Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    RELEASE_RELEASED,
    LaneLifecycleRecord,
    ReleasePinError,
    decode_release_pins,
    norm,
)

#: The observed pair carries no locator the release generation closed, and the evidence was
#: complete enough to say so.
FENCE_OK = "released_locator_fence_ok"
#: An observed slot's locator IS one the release generation closed — the defining survivor
#: signature. Also the token a pane-id-reuse false refusal surfaces under (j#94544 A.4), so an
#: operator seeing it should check whether the pane-id was recycled before assuming a survivor.
FENCE_LOCATOR_REUSED = "released_locator_reuse"
#: No release generation evidence exists: no row, or the release never durably completed. Not a
#: pass — the row cannot tell "no process existed" from "a survivor was never recorded".
FENCE_EVIDENCE_ABSENT = "release_evidence_absent"
#: The stored pin set could not be decoded. Fail closed rather than treat a shorter/failed
#: decode as "nothing was released" (the ``decode_release_pins`` R1-F4 discipline).
FENCE_EVIDENCE_UNREADABLE = "release_evidence_unreadable"
#: Evidence exists but does not cover every observed live slot (or a pin carries no locator), so
#: at least one observed slot is unproven.
FENCE_EVIDENCE_INCOMPLETE = "release_evidence_incomplete"


def released_locator_verdict(
    record: Optional[LaneLifecycleRecord],
    observed_locators: Iterable[str],
) -> tuple[bool, str]:
    """``(ok, reason)`` — may this observed pair be a genuine post-release generation?

    ``ok`` is True only when a COMPLETE release generation is on record and none of the observed
    locators appears in it. Every other outcome is a refusal with a typed reason from this
    module's vocabulary. Pure: no IO, no clock, no environment.
    """
    if record is None:
        return False, FENCE_EVIDENCE_ABSENT
    if norm(record.process_release) != RELEASE_RELEASED:
        # Only a durably COMPLETED release proves which locators were closed. A never-requested
        # or in-flight generation is missing evidence, not evidence of absence.
        return False, FENCE_EVIDENCE_ABSENT
    try:
        pins = decode_release_pins(record.release_pins)
    except ReleasePinError:
        return False, FENCE_EVIDENCE_UNREADABLE
    if not pins:
        return False, FENCE_EVIDENCE_ABSENT
    released = {norm(pin.locator) for pin in pins}
    if "" in released:
        # A pin without a locator proves nothing about the slot it names.
        return False, FENCE_EVIDENCE_INCOMPLETE
    observed = {norm(locator) for locator in observed_locators if norm(locator)}
    if not observed:
        return False, FENCE_EVIDENCE_INCOMPLETE
    if observed & released:
        return False, FENCE_LOCATOR_REUSED
    if len(released) < len(observed):
        return False, FENCE_EVIDENCE_INCOMPLETE
    return True, FENCE_OK


__all__ = (
    "FENCE_OK",
    "FENCE_LOCATOR_REUSED",
    "FENCE_EVIDENCE_ABSENT",
    "FENCE_EVIDENCE_UNREADABLE",
    "FENCE_EVIDENCE_INCOMPLETE",
    "released_locator_verdict",
)
