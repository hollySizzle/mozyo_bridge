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
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    RELEASE_STATES,
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
#: The observation belongs to the CURRENT generation, but that generation has not COMPLETED
#: (``requested`` / ``partial``). Only a completed generation establishes what it closed: a
#: partial run may still close more slots, so its observation is not yet a survivor proof.
#: Review j#94727 R5-F1 corrected this: ``advance_release`` walks ``requested -> partial ->
#: released`` without rewriting the observation, so an in-flight row's observation is this
#: generation's — not a stale one — and the reason must say so.
OBSERVATION_GENERATION_NOT_COMPLETED = "release_observation_generation_not_completed"
#: An observation is present on a row whose release axis is ``not_requested`` — a shape the reset
#: writers make unreachable, because they clear the observation with the rest of the release set
#: (review j#94707 R4-F1). Reaching it means that invariant was violated (an older build, a
#: hand-edited row, a writer added without its reset), so it is named as the violation it is
#: rather than folded into the in-flight case. Reserved for the CANONICAL ``not_requested`` token
#: only — a non-canonical value is :data:`OBSERVATION_RELEASE_STATE_UNKNOWN`, not this.
OBSERVATION_STALE_AFTER_RESET = "release_observation_stale_after_reset"
#: The row's ``process_release`` is not a canonical release-state token at all, so what the
#: observation means cannot be classified (review j#94738 R6-F1). ``process_release`` is
#: ``TEXT NOT NULL`` with no CHECK constraint and the row decoder passes the string through, so a
#: legacy / corrupted / hand-edited row can carry anything. This is OUTCOME-UNKNOWN, matching the
#: standing ruling for the same storage fact on the hibernate rail (``release_state_unknown``,
#: review j#86776 R5-F5, time-classification rulings j#87181 / j#87182 / j#87188 / j#87226): an
#: unknown state must never be folded into a DETERMINISTIC classification — neither the in-flight
#: case nor the reset-invariant violation, and above all not into a pass.
#:
#: Canonicality is checked BYTE-EXACT, not after trimming. ``"released "`` normalises to the
#: canonical token, and the pre-R7 gate accepted it as a proof — a non-canonical stored value
#: being ADMITTED, which is worse than being misclassified. Same discipline as the ``lane_kind``
#: vocabulary (review j#85852 F1): a closed vocabulary is compared as stored.
OBSERVATION_RELEASE_STATE_UNKNOWN = "release_observation_release_state_unknown"

#: The release states this gate has an explicit RULE for. Deliberately its own literal set and
#: **not** :data:`RELEASE_STATES` (review j#94750 R7-F1): the pre-R8 gate refused the states it
#: knew and let everything else fall through to the ``released`` pin check, so adding a fifth
#: member to the vocabulary — without touching this module — made that member return a survivor
#: proof. Measured: injecting ``future_settling_state`` yielded ``release_observation_ok``. A state
#: this gate cannot classify must fail closed as unknown, so growing the vocabulary is safe by
#: construction and the omission surfaces as a refusal instead of an admission.
_CLASSIFIED_RELEASE_STATES = frozenset(
    {RELEASE_NOT_REQUESTED, RELEASE_REQUESTED, RELEASE_PARTIAL, RELEASE_RELEASED}
)


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

    The completeness check lives here, not in the caller (review j#94707 R4-F1), and it branches
    over the release axis EXPLICITLY — one reason per state, because a consumer branching on the
    reason needs them apart (reviews j#94727 R5-F1, j#94738 R6-F1):

    - not a state this gate has a rule for — ``process_release`` is unconstrained ``TEXT`` and the
      decoder passes it through, so a legacy / corrupted / hand-edited row can hold anything, and
      a future vocabulary member this module has not been taught lands here too. OUTCOME-UNKNOWN:
      :data:`OBSERVATION_RELEASE_STATE_UNKNOWN`. Checked byte-exact, so a value that merely
      *normalises* to a canonical token is unknown too, never a pass. Decided FIRST, before the
      observation is decoded, so this diagnosis does not depend on another field's shape.
    - ``requested`` / ``partial`` — the observation IS this generation's, written by
      :func:`open_release_generation` in the same CAS that opened it, and left untouched by
      :meth:`...LaneLifecycleStore.record_release_outcome` as the generation advances. It is not
      yet a proof because the generation has not completed: a ``partial`` run may still close
      more slots. Reason: :data:`OBSERVATION_GENERATION_NOT_COMPLETED`.
    - ``not_requested`` with an observation still present — the reset writers make this
      unreachable, so it is an invariant VIOLATION, not an in-flight state. Reason:
      :data:`OBSERVATION_STALE_AFTER_RESET`.
    - ``released`` — the only state that may yield the observation, and only if the stored pins
      describe it exactly.

    Refusing the unreachable shapes here rather than trusting the writers is deliberate: "no
    caller can currently reach that shape" is precisely the caller-side assumption this issue has
    had to retract twice.

    A complete-empty observation is returned successfully — it is positive evidence that the
    driver observed no live slot. Whether that is sufficient is the caller's decision; this
    function only reports what the row can prove.
    """
    if record is None:
        return None, OBSERVATION_ABSENT
    # The STATE is classified before the observation is even decoded (review j#94750 R7-F2):
    # otherwise an unknown state on a row whose observation happens to be empty / malformed was
    # reported as ``absent`` / ``unreadable``, so the state-specific diagnosis this gate promises
    # depended on the shape of a different field. Byte-exact, deliberately NOT ``norm``-ed.
    release = record.process_release
    if release not in _CLASSIFIED_RELEASE_STATES:
        return None, OBSERVATION_RELEASE_STATE_UNKNOWN
    try:
        observation = decode_release_observation(record.release_observation)
    except ReleaseObservationError:
        return None, OBSERVATION_UNREADABLE
    if observation is None:
        return None, OBSERVATION_ABSENT
    if release in (RELEASE_REQUESTED, RELEASE_PARTIAL):
        return None, OBSERVATION_GENERATION_NOT_COMPLETED
    if release == RELEASE_NOT_REQUESTED:
        return None, OBSERVATION_STALE_AFTER_RESET
    if release != RELEASE_RELEASED:  # unreachable today; see _CLASSIFIED_RELEASE_STATES
        return None, OBSERVATION_RELEASE_STATE_UNKNOWN
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
    "OBSERVATION_GENERATION_NOT_COMPLETED",
    "OBSERVATION_STALE_AFTER_RESET",
    "OBSERVATION_RELEASE_STATE_UNKNOWN",
    "RELEASE_RELEASED",
    "open_release_generation",
    "verify_release_observation",
)
