"""Clock-independent survivor fence over the release observation (Redmine #14477 j#94582 A″).

``sublane resume`` must refuse a pane that **survived** hibernate's release. Three authorities
have been tried, and the first two failed for the same reason — they trusted a caller:

1. the **timestamp** boundary (``hibernated_at``, v8): defeated by a backdated CAS stamp, a
   regressed host clock, or a self-written ``observed_at`` (review j#94531 R2-F1);
2. the **released-locator** comparison against ``release_pins`` alone (v8 fence): defeated
   because ``request_release`` accepted any pin list, so a caller could record locators that
   were never live and the disjointness test passed vacuously (review j#94570 R3-F1);
3. **this** surface: the pins are DERIVED by the store from the release driver's single live
   enumeration, recorded as an immutable v9 observation, and both fields must agree exactly at
   the writer gate and again here (:mod:`...lane_release`, :mod:`...lane_release_observation`).

A survivor keeps its tmux pane-id, so its locator is inside the observation; a genuine relaunch
is assigned a new pane-id and is not. That comparison is independent of every clock, which is why
the timestamp is only a *liveness* conjunct now and never the generation proof (j#94582 item 3).

The verdicts below implement j#94582 item 4 exactly:

- observation ABSENT or UNREADABLE, or its pins disagreeing with it → **fail closed**. Absence of
  evidence is never freshness: the row cannot distinguish "nothing was live at hibernate" from
  "a survivor existed and nothing was recorded". That is a deliberate functional regression for
  a lane whose release generation was never completed or was written by an older build.
- **COMPLETE-EMPTY** observation → allowed. The driver looked and found no live slot; that is
  positive evidence, not missing evidence, and it is representable precisely because the v9
  envelope keeps it distinct from absent.
- non-empty observation intersecting the currently observed locators → **fail closed** as a
  survivor. A recycled tmux pane-id lands here too: a false refusal in the safe direction, named
  by its own token so an operator can tell the two apart (j#94582 item 4 / A.4).
- non-empty, disjoint, and exactly matching → allowed, provided every other fence is green
  (attestation, provider binding, multiplicity, declared pins). None of them is relaxed.

Still a trust-boundary authority, not a cryptographic one: a writer inside the boundary can
record a false observation, but only through one explicit auditable seam. Redmine #14756 replaces
that with an epoch bound into the startup attestation.
"""

from __future__ import annotations

from typing import Iterable, Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    RELEASE_RELEASED,
    LaneLifecycleRecord,
    is_canonical_release_state,
    norm,
)
from mozyo_bridge.core.state.lane_release import (
    OBSERVATION_ABSENT,
    OBSERVATION_OK,
    OBSERVATION_RELEASE_STATE_UNKNOWN,
    verify_release_observation,
)

#: The observed pair carries no locator the release generation closed, on evidence the row can
#: prove. Either a complete-empty observation or a disjoint non-empty one.
FENCE_OK = "released_locator_fence_ok"
#: An observed slot's locator IS one the release generation observed — the defining survivor
#: signature. A recycled pane-id surfaces here too (a false refusal in the safe direction), so an
#: operator seeing this should check pane-id reuse before concluding a survivor.
FENCE_LOCATOR_REUSED = "released_locator_reuse"
#: No usable release-generation evidence: the generation never completed, or the observation is
#: absent / unreadable / contradicted by the stored pins. Never a pass.
FENCE_EVIDENCE_ABSENT = "release_evidence_absent"
#: The driver observed ZERO live slots and recorded it. Positive evidence, distinct from absent.
FENCE_COMPLETE_EMPTY = "release_observation_complete_empty"


def released_locator_verdict(
    record: Optional[LaneLifecycleRecord],
    observed_locators: Iterable[str],
) -> tuple[bool, str]:
    """``(ok, reason)`` — may this observed pair be a genuine post-release generation?

    ``ok`` is True only on evidence the row can actually prove. Pure: no IO, no clock, no
    environment. The reason token is surfaced in the typed resume outcome.
    """
    if record is None:
        return False, FENCE_EVIDENCE_ABSENT
    if not is_canonical_release_state(record.process_release):
        # A stored value outside the vocabulary is OUTCOME-UNKNOWN and says so, rather than being
        # folded into "no evidence" (review j#94750 R7-F2). Byte-exact: the old ``norm``-ed check
        # let ``"released "`` past this precheck entirely, so the same non-canonical class got two
        # different operator details depending on spelling.
        return False, OBSERVATION_RELEASE_STATE_UNKNOWN
    if record.process_release != RELEASE_RELEASED:
        # A CANONICAL but non-released state keeps the generic reason it has always had: only a
        # durably COMPLETED generation proves what it closed, and a never-requested or in-flight
        # one is missing evidence, not evidence of absence.
        return False, FENCE_EVIDENCE_ABSENT
    observation, reason = verify_release_observation(record)
    if observation is None:
        # absent / unreadable / pins-disagree — all fail closed. The specific token is folded
        # into the caller's detail via this reason so the operator sees which one it was.
        return False, (
            FENCE_EVIDENCE_ABSENT if reason == OBSERVATION_ABSENT else reason
        )
    if reason != OBSERVATION_OK:  # defensive: a present observation must verify OK
        return False, reason
    observed = {norm(locator) for locator in observed_locators if norm(locator)}
    if observation.is_complete_empty:
        # The driver looked and found nothing live: positive evidence that no pane could have
        # survived this generation, whatever is live now.
        return True, FENCE_COMPLETE_EMPTY
    if not observed:
        # Nothing observed to compare; the both-slots-live fence owns that refusal, but this
        # surface must not report a pass it cannot justify.
        return False, FENCE_EVIDENCE_ABSENT
    if observed & observation.locators:
        return False, FENCE_LOCATOR_REUSED
    return True, FENCE_OK


__all__ = (
    "FENCE_OK",
    "FENCE_COMPLETE_EMPTY",
    "FENCE_LOCATOR_REUSED",
    "FENCE_EVIDENCE_ABSENT",
    "released_locator_verdict",
)
