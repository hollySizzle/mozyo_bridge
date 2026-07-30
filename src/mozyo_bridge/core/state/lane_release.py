"""Opening a release generation from an observation, and verifying it (Redmine #14477 j#94582).

Moved out of :mod:`mozyo_bridge.core.state.lane_lifecycle` so the release axis is its own
cohesive unit — the pattern :mod:`...lane_replacement` (its own axis on the same row) and
:mod:`...lane_lifecycle_rows` already establish, and what keeps each module under the
module-health threshold. :meth:`LaneLifecycleStore.request_release` is now a thin delegator, so
every existing call site keeps working; only its keyword changed from ``pins`` to ``observation``.

**Why the keyword changed.** ``request_release`` used to accept any pin list a caller handed it,
and resume later compared those locators against the live pair to refuse survivors. Review
j#94570 R3-F1 showed the hole: a caller can record locators that were never live, so the
comparison passes vacuously and a true pre-hibernate survivor is admitted. That is the same
defect shape as the timestamp authority before it (j#94531 R2-F1) — **the authority was
caller-supplied**. Adding a second field for the caller to cross-check against itself would keep
the shape, so the raw seam is closed instead (j#94582 items 1, 3, 6):

- the release **driver** enumerates the lane's live slots from the inventory once and wraps that
  enumeration in a :class:`ReleaseObservation`;
- this surface derives the generation's pins from **that snapshot only** — there is no second
  caller value to disagree with it;
- both fields are written in one CAS and verified to match exactly at the writer gate, and again
  at the read gate before resume may use them as proof;
- a legacy ``pins=`` call is refused outright, not silently accepted for compatibility.

A **complete-empty** observation is a first-class, recordable fact: the driver looked and found
no live slot. It opens a generation with zero pins, which is why the empty case bypasses
:func:`validate_release_pins` (that helper exists to reject an *ambiguous* pin set and refuses
empty by design). Distinguishing it from an ABSENT observation is the whole point of the v9
envelope — see :mod:`...lane_release_observation`.

This is a trust-boundary authority, not a cryptographic one: a writer inside the boundary can
still record a false observation. The difference is that it must now do so explicitly through one
auditable seam. The epoch-bound replacement is Redmine #14756.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    CasOutcome,
    LaneLifecycleKey,
    LaneLifecycleRecord,
    ReleasePin,
    ReleasePinError,
    decode_release_pins,
    encode_release_pins,
    norm,
    release_transition_allowed,
    replacement_settled,
    validate_release_pins,
)
from mozyo_bridge.core.state.lane_lifecycle_rows import _locked_row, _rollback, _utc_now
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    TABLE as _TABLE,
    LaneLifecycleError,
)
from mozyo_bridge.core.state.lane_release_observation import (
    ReleaseObservation,
    ReleaseObservationError,
    decode_release_observation,
    encode_release_observation,
    observation_matches_pins,
)

#: Read-gate outcomes for :func:`verify_release_observation`.
OBSERVATION_OK = "release_observation_ok"
#: No observation was ever recorded (a pre-v9 row, or a generation opened by an older build).
#: NOT the same as a recorded complete-empty observation.
OBSERVATION_ABSENT = "release_observation_absent"
#: Present but unreadable (malformed envelope / unusable slot). Fail closed rather than let a
#: corrupt value decode to "no slots", which is the one value that would masquerade as the
#: complete-empty positive evidence.
OBSERVATION_UNREADABLE = "release_observation_unreadable"
#: The stored release pins do not describe exactly the recorded observation (missing, extra, or
#: different). A row whose two fields disagree is never usable as proof.
OBSERVATION_PIN_MISMATCH = "release_observation_pin_mismatch"
#: The row carries an observation but its release generation is not COMPLETED, so the observation
#: is not this generation's authority (review j#94707 R4-F1). The reset writers clear the whole
#: release set, so a live row should never be in this shape; the read gate refuses it anyway
#: rather than depending on every caller to check ``process_release`` first.
OBSERVATION_NOT_CURRENT_GENERATION = "release_observation_not_current_generation"


def open_release_generation(
    store,
    key: LaneLifecycleKey,
    *,
    expected_revision: int,
    action_id: str,
    observation: Optional[ReleaseObservation] = None,
    pins: Optional[Iterable[ReleasePin]] = None,
    now: Optional[str] = None,
) -> CasOutcome:
    """Open a release generation whose pins are DERIVED from ``observation``.

    Only a lane that has already left ``active`` may open one: a lane still holding its work is
    never a release target. The derived pins are the only slots this generation may ever close,
    and the actuator must still re-verify each one against the live inventory before closing it.

    ``pins`` exists solely to refuse the legacy call shape loudly (j#94582 item 6). It is never
    read; passing it raises rather than being honoured for compatibility.

    ``observation`` is a keyword with a ``None`` default *so that the legacy shape reaches this
    body*. Review j#94707 R4-F2 measured that a required parameter made the literal legacy call —
    ``pins=`` with no ``observation`` — fail as ``TypeError`` at argument binding, never reaching
    the typed refusal the seam exists to give. The refusal is a domain fact about the authority
    contract, so it must be a :class:`ReleaseObservationError` carrying that reason, not an
    arity error; the missing-argument case is refused just as loudly below.
    """
    if pins is not None:
        raise ReleaseObservationError(
            "request_release no longer accepts caller-supplied pins: a release generation is "
            "derived from the driver's ReleaseObservation so the recorded locators cannot be "
            "values that were never live (Redmine #14477 j#94570 R3-F1 / j#94582 item 6)"
        )
    if observation is None or not isinstance(observation, ReleaseObservation):
        raise ReleaseObservationError(
            "a release generation requires a ReleaseObservation from the release driver"
        )
    action = norm(action_id)
    if not action:
        raise ValueError("a release generation requires a non-empty action id")
    # A complete-empty observation legitimately opens a generation with zero pins;
    # ``validate_release_pins`` refuses empty by design (it guards AMBIGUOUS sets), so the
    # empty case skips it. A non-empty set still goes through the R1-F4 duplicate guard.
    pinned = validate_release_pins(observation.slots) if observation.slots else ()
    encoded_pins = encode_release_pins(pinned) if pinned else ""
    encoded_observation = encode_release_observation(observation)
    # Writer gate (j#94582 item 3): what we are about to store must decode back to EXACTLY the
    # observation. A mismatch here is a programming error, refused before it becomes durable.
    if not observation_matches_pins(observation, decode_release_pins(encoded_pins)):
        raise ReleaseObservationError(
            "derived release pins do not match the observation; refusing to store a row whose "
            "two authority fields disagree"
        )
    stamp = now or _utc_now()
    conn = store._connect_write(key)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _locked_row(conn, key)
        if current is None:
            conn.execute("ROLLBACK")
            return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
        if current.revision != expected_revision:
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False, reason=CAS_STALE_REVISION, revision=current.revision
            )
        if current.lane_disposition == DISPOSITION_ACTIVE:
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False, reason=CAS_UNEXPECTED_STATE, revision=current.revision
            )
        if not replacement_settled(current.replacement_state):
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False, reason=CAS_FORBIDDEN_TRANSITION, revision=current.revision
            )
        if not release_transition_allowed(current.process_release, RELEASE_REQUESTED):
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False, reason=CAS_FORBIDDEN_TRANSITION, revision=current.revision
            )
        revision = current.revision + 1
        conn.execute(
            f"UPDATE {_TABLE} SET process_release = ?, release_action_id = ?, "
            "release_pins = ?, release_observation = ?, revision = ?, updated_at = ? "
            "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
            (
                RELEASE_REQUESTED,
                action,
                encoded_pins,
                encoded_observation,
                revision,
                stamp,
                key.repo_workspace_id,
                key.lane_id,
                current.revision,
            ),
        )
        conn.execute("COMMIT")
        return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
    except sqlite3.DatabaseError as exc:
        _rollback(conn)
        raise LaneLifecycleError(
            f"lane release request failed ({type(exc).__name__}); fail closed"
        ) from exc
    finally:
        conn.close()


def verify_release_observation(
    record: Optional[LaneLifecycleRecord],
) -> tuple[Optional[ReleaseObservation], str]:
    """``(observation, reason)`` — the READ gate over a completed release generation.

    Returns the observation only when it belongs to a COMPLETED release generation, is present,
    readable, AND exactly described by the stored release pins (j#94582 items 3, 4). Every other
    outcome returns ``None`` with a typed reason, and the caller must fail closed on it: an ABSENT
    observation is missing evidence, not evidence of absence.

    The generation check lives here, not in the caller (review j#94707 R4-F1). Every writer that
    resets the release axis clears the observation with it, so a row that has left ``released``
    carries no observation at all — but "no caller can currently reach that shape" is precisely
    the kind of caller-side assumption this issue has had to retract twice, so the component
    refuses a non-current observation itself.

    A complete-empty observation is returned successfully — it is positive evidence that the
    driver observed no live slot. Whether that is sufficient is the caller's decision; this
    function only reports what the row can prove.
    """
    if record is None:
        return None, OBSERVATION_ABSENT
    try:
        observation = decode_release_observation(record.release_observation)
    except ReleaseObservationError:
        return None, OBSERVATION_UNREADABLE
    if observation is None:
        return None, OBSERVATION_ABSENT
    if norm(record.process_release) != RELEASE_RELEASED:
        # Present but not this generation's proof: an in-flight generation has not established
        # what it closed, and a reset row should not be carrying one at all.
        return None, OBSERVATION_NOT_CURRENT_GENERATION
    try:
        stored_pins = decode_release_pins(record.release_pins)
    except ReleasePinError:
        return None, OBSERVATION_UNREADABLE
    if not observation_matches_pins(observation, stored_pins):
        return None, OBSERVATION_PIN_MISMATCH
    return observation, OBSERVATION_OK


__all__ = (
    "OBSERVATION_OK",
    "OBSERVATION_ABSENT",
    "OBSERVATION_UNREADABLE",
    "OBSERVATION_PIN_MISMATCH",
    "OBSERVATION_NOT_CURRENT_GENERATION",
    "RELEASE_RELEASED",
    "open_release_generation",
    "verify_release_observation",
)
