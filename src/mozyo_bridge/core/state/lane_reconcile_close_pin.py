"""Versioned owed-close authority for hibernated-live reconcile (#15227 j#103467)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Sequence

from .lane_lifecycle_model import ReleasePin, ReleasePinError, norm


RECONCILE_CLOSE_PIN_VERSION = 1


class ReconcileClosePinError(ValueError):
    """The dedicated reconcile close pin is absent, legacy, or malformed."""


@dataclass(frozen=True)
class ReconcileClosePin:
    """Exact pair generations durably owned by one reconcile retire CAS."""

    slots: tuple[ReleasePin, ...]

    def __post_init__(self) -> None:
        if len(self.slots) != 2:
            raise ReconcileClosePinError("reconcile close pin requires exactly two slots")
        identities: set[tuple[str, str]] = set()
        roles: set[str] = set()
        names: set[str] = set()
        locators: set[str] = set()
        for slot in self.slots:
            if not slot.current_generation_bound:
                raise ReconcileClosePinError(
                    "reconcile close pin requires startup_action_id for every slot"
                )
            if (
                slot.stable_identity in identities
                or slot.role in roles
                or slot.assigned_name in names
                or slot.locator in locators
            ):
                raise ReconcileClosePinError(
                    "reconcile close pin contains duplicate identity axis"
                )
            identities.add(slot.stable_identity)
            roles.add(slot.role)
            names.add(slot.assigned_name)
            locators.add(slot.locator)


def build_reconcile_close_pin(slots: Sequence[ReleasePin]) -> ReconcileClosePin:
    return ReconcileClosePin(tuple(slots))


def encode_reconcile_close_pin(pin: ReconcileClosePin) -> str:
    return json.dumps(
        {
            "v": RECONCILE_CLOSE_PIN_VERSION,
            "slots": [
                slot.as_payload() for slot in sorted(pin.slots, key=lambda item: item.role)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def decode_reconcile_close_pin(raw: object) -> Optional[ReconcileClosePin]:
    if type(raw) is not str or not raw:
        return None
    if raw.strip() != raw:
        raise ReconcileClosePinError("reconcile close pin has surrounding whitespace")
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReconcileClosePinError("reconcile close pin is not readable JSON") from exc
    if (
        not isinstance(loaded, dict)
        or set(loaded) != {"v", "slots"}
        or type(loaded.get("v")) is not int
        or loaded.get("v") != RECONCILE_CLOSE_PIN_VERSION
    ):
        raise ReconcileClosePinError("reconcile close pin version is unsupported")
    raw_slots = loaded.get("slots")
    if not isinstance(raw_slots, list):
        raise ReconcileClosePinError("reconcile close pin slots must be a list")
    slots = []
    for item in raw_slots:
        if not isinstance(item, dict):
            raise ReconcileClosePinError("reconcile close pin slot must be an object")
        if set(item) != {"role", "assigned_name", "locator", "startup_action_id"}:
            raise ReconcileClosePinError(
                "reconcile close pin slot does not have the exact versioned shape"
            )
        if not all(
            type(item[key]) is str and bool(item[key]) and item[key].strip() == item[key]
            for key in ("role", "assigned_name", "locator", "startup_action_id")
        ):
            raise ReconcileClosePinError(
                "reconcile close pin values must be canonical non-empty strings"
            )
        try:
            slots.append(
                ReleasePin(
                    role=norm(item.get("role")),
                    assigned_name=norm(item.get("assigned_name")),
                    locator=norm(item.get("locator")),
                    startup_action_id=norm(item.get("startup_action_id")),
                )
            )
        except ReleasePinError as exc:
            raise ReconcileClosePinError("reconcile close pin slot is unusable") from exc
    return build_reconcile_close_pin(slots)


__all__ = (
    "RECONCILE_CLOSE_PIN_VERSION",
    "ReconcileClosePin",
    "ReconcileClosePinError",
    "build_reconcile_close_pin",
    "decode_reconcile_close_pin",
    "encode_reconcile_close_pin",
)
