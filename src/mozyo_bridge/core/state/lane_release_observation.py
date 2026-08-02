"""The release generation's immutable observed-slot snapshot (Redmine #14477 disposition j#94582).

Resume must refuse a pane that **survived** hibernate's release. Two earlier authorities failed
that job, each for the same reason:

- the **timestamp** boundary (``hibernated_at``, v8) — defeated by a backdated CAS stamp, a
  regressed host clock, or a self-written ``observed_at`` (review j#94531 R2-F1);
- the **released-locator** comparison against ``release_pins`` (v8 fence) — defeated because
  ``LaneLifecycleStore.request_release`` accepted *any* pin list a caller handed it, so a caller
  could record locators that were never live and a survivor passed the disjointness test
  (review j#94570 R3-F1).

Both failures share one shape: **the authority was caller-supplied**. So this surface removes the
seam rather than adding another comparison. The release driver enumerates the lane's live slots
from the inventory ONCE, wraps that enumeration in a :class:`ReleaseObservation`, and the store
derives the release pins from that single snapshot — there is no second caller-supplied value to
disagree with it, and the raw ``pins=`` API is refused outright (j#94582 items 1, 3, 6).

**Absent and complete-empty are different facts, and the encoding keeps them different**
(j#94582 item 2). An empty column means *no observation was ever recorded* (a pre-v9 row, or a
legacy write) and resume fails closed on it. A recorded observation carrying zero slots means
*the driver looked and found nothing live* — positive evidence that the lane had no processes at
hibernate, which resume may accept. Collapsing those two into "empty" is exactly the
"absence of evidence read as evidence" mistake the #14477 review chain kept catching, so the
envelope is explicit: ``''`` is absent, ``{"v":1,"slots":[]}`` is complete-empty.

The snapshot is **write-once per release generation** (j#94582 item 5): only opening a new
generation replaces it, and no metadata / decision / revision / outcome writer may touch it.

This is still a trust-boundary authority, not a cryptographic one: a writer inside the boundary
can record a false observation. What changed is that doing so now requires an explicit,
auditable claim through the one seam that exists, instead of being accepted implicitly anywhere
``pins=`` was passed. The epoch-bound replacement is Redmine #14756.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    ReleasePin,
    ReleasePinError,
    norm,
)

#: Envelope version. A row written by a future build with a different version is unreadable
#: here rather than silently reinterpreted.
RELEASE_OBSERVATION_VERSION = 1


class ReleaseObservationError(ValueError):
    """The observation is unusable (malformed, or an unusable slot set); fail closed."""


@dataclass(frozen=True)
class ReleaseObservation:
    """Exactly the live managed slots the release driver enumerated, at one instant.

    ``slots`` is the enumeration itself. An EMPTY tuple is meaningful and distinct from an
    absent observation: it records that the driver looked at the live inventory and found no
    managed slot for this lane.
    """

    slots: tuple[ReleasePin, ...] = ()

    @property
    def is_complete_empty(self) -> bool:
        """The driver observed zero live slots (positive evidence, not missing evidence)."""
        return not self.slots

    @property
    def locators(self) -> frozenset[str]:
        return frozenset(norm(pin.locator) for pin in self.slots)


def build_release_observation(pins: Iterable[ReleasePin]) -> ReleaseObservation:
    """Wrap a driver enumeration, refusing a set that could not describe a real observation.

    A slot with no locator names nothing, and a duplicated locator means the enumeration is not
    a snapshot of distinct live panes (j#94582 lists both as adversarial edges). Either is a
    programming / corruption error at the writer, refused here rather than stored.
    """
    slots = tuple(pins)
    seen: set[str] = set()
    for pin in slots:
        locator = norm(pin.locator)
        if not locator:
            raise ReleaseObservationError(
                "a release observation slot must carry the locator it observed"
            )
        if locator in seen:
            raise ReleaseObservationError(
                "a release observation cannot repeat a locator; it is a snapshot of "
                "distinct live slots"
            )
        seen.add(locator)
    return ReleaseObservation(slots=slots)


def encode_release_observation(observation: ReleaseObservation) -> str:
    """Serialise for the v9 column. A complete-empty observation is a PRESENT envelope."""
    return json.dumps(
        {
            "v": RELEASE_OBSERVATION_VERSION,
            "slots": [
                {
                    "role": pin.role,
                    "assigned_name": pin.assigned_name,
                    "locator": pin.locator,
                }
                for pin in observation.slots
            ],
        },
        sort_keys=True,
    )


def decode_release_observation(raw: str) -> Optional[ReleaseObservation]:
    """``None`` when ABSENT (no observation recorded); an observation when present.

    Raises :class:`ReleaseObservationError` when present-but-unreadable. A corrupt envelope must
    never decode to "no slots", because that is the one value a caller could exploit to turn
    missing evidence into the complete-empty positive evidence resume accepts.
    """
    if not norm(raw):
        return None
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReleaseObservationError(
            f"release observation is not readable JSON: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise ReleaseObservationError("release observation must be an object")
    if loaded.get("v") != RELEASE_OBSERVATION_VERSION:
        raise ReleaseObservationError(
            f"release observation version {loaded.get('v')!r} is not readable by this build"
        )
    raw_slots = loaded.get("slots")
    if not isinstance(raw_slots, list):
        raise ReleaseObservationError("release observation slots must be a list")
    pins: list[ReleasePin] = []
    for item in raw_slots:
        if not isinstance(item, dict):
            raise ReleaseObservationError(f"release observation slot is not an object: {item!r}")
        try:
            pins.append(
                ReleasePin(
                    role=norm(item.get("role")),
                    assigned_name=norm(item.get("assigned_name")),
                    locator=norm(item.get("locator")),
                )
            )
        except ReleasePinError as exc:
            raise ReleaseObservationError(f"release observation slot unusable: {exc}") from exc
    try:
        return build_release_observation(pins)
    except ReleaseObservationError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise ReleaseObservationError(f"release observation unusable: {exc}") from exc


def observation_matches_pins(
    observation: ReleaseObservation, pins: Iterable[ReleasePin]
) -> bool:
    """Do the stored release pins describe EXACTLY this observation?

    Exact, not "covers": a missing pin leaves a slot unproven and an extra pin claims a close
    the observation never saw. Both are refused at the writer gate and again at the read gate
    (j#94582 item 3), so a row whose two fields disagree can never be used as proof.
    """
    stored = tuple(pins)
    if len(stored) != len(observation.slots):
        return False
    def key(pin: ReleasePin) -> tuple[str, str, str]:
        return (norm(pin.role), norm(pin.assigned_name), norm(pin.locator))
    return sorted(key(p) for p in stored) == sorted(key(p) for p in observation.slots)


__all__ = (
    "RELEASE_OBSERVATION_VERSION",
    "ReleaseObservation",
    "ReleaseObservationError",
    "build_release_observation",
    "encode_release_observation",
    "decode_release_observation",
    "observation_matches_pins",
)
