"""Versioned process-generation pins for destructive lane close rails (#15227)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence


def _norm(value: object) -> str:
    return str(value or "").strip()


def _canonical_text(value: object) -> bool:
    return type(value) is str and bool(value) and value.strip() == value


class ReleasePinError(ValueError):
    """A release pin is unusable; it is never shortened or back-filled."""


@dataclass(frozen=True)
class ReleasePinsProjection:
    """Lossless read provenance: 0 absent, 1 legacy list, 2 current envelope."""

    version: int
    pins: tuple["ReleasePin", ...]

    @property
    def current_authority(self) -> bool:
        return self.version == 2


@dataclass(frozen=True)
class ReleasePin:
    """One stable slot plus its nonsecret immutable startup-generation token."""

    role: str
    assigned_name: str
    locator: str
    startup_action_id: str = ""

    def __post_init__(self) -> None:
        if not all(
            _canonical_text(getattr(self, name))
            for name in ("role", "assigned_name", "locator")
        ) or (
            self.startup_action_id != ""
            and not _canonical_text(self.startup_action_id)
        ):
            raise ReleasePinError(
                "a release pin requires canonical role / assigned_name / locator / token"
            )

    @property
    def stable_identity(self) -> tuple[str, str]:
        return (self.role, self.assigned_name)

    @property
    def current_generation_bound(self) -> bool:
        return bool(self.startup_action_id)

    def as_payload(self) -> dict[str, str]:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "startup_action_id": self.startup_action_id,
        }


def encode_release_pins(pins: Sequence[ReleasePin]) -> str:
    """Serialize exact v2 pins; tokenless legacy projections are read-only."""
    pinned = tuple(pins)
    _validate_axes(pinned)
    if any(not pin.current_generation_bound for pin in pinned):
        raise ReleasePinError(
            "release pin v2 requires startup_action_id for every slot; legacy pins are "
            "read-only and cannot authorize a destructive close"
        )
    return json.dumps(
        {"v": 2, "pins": [p.as_payload() for p in sorted(pinned, key=lambda p: p.role)]},
        ensure_ascii=False,
        sort_keys=True,
    )


def decode_release_pin_projection(raw: str) -> ReleasePinsProjection:
    """Decode pins without collapsing absent, legacy-empty and current-empty."""
    if raw == "":
        return ReleasePinsProjection(version=0, pins=())
    if type(raw) is not str or raw.strip() != raw:
        raise ReleasePinError("release pins absent form must be the exact empty string")
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReleasePinError("release pins are not readable JSON") from exc
    if isinstance(loaded, list):
        raw_pins, current = loaded, False
    elif (
        isinstance(loaded, dict)
        and set(loaded) == {"v", "pins"}
        and type(loaded.get("v")) is int
        and loaded.get("v") == 2
    ):
        raw_pins, current = loaded.get("pins"), True
    else:
        raise ReleasePinError("release pins must be a legacy list or exact v2 envelope")
    if not isinstance(raw_pins, list):
        raise ReleasePinError("release pins payload must be a list")
    pins: list[ReleasePin] = []
    for item in raw_pins:
        if not isinstance(item, dict):
            raise ReleasePinError("release pin is not an object")
        expected = (
            {"role", "assigned_name", "locator", "startup_action_id"}
            if current
            else {"role", "assigned_name", "locator"}
        )
        if set(item) != expected:
            raise ReleasePinError("release pin does not have the exact versioned shape")
        if current and not all(_canonical_text(item[key]) for key in expected):
            raise ReleasePinError("release pin v2 values must be canonical non-empty strings")
        pins.append(
            ReleasePin(
                role=_norm(item["role"]),
                assigned_name=_norm(item["assigned_name"]),
                locator=_norm(item["locator"]),
                startup_action_id=_norm(item["startup_action_id"]) if current else "",
            )
        )
    _validate_axes(pins)
    return ReleasePinsProjection(version=2 if current else 1, pins=tuple(pins))


def decode_release_pins(raw: str) -> tuple[ReleasePin, ...]:
    """Compatibility projection; authority callers use ``decode_release_pin_projection``."""
    return decode_release_pin_projection(raw).pins


def validate_release_pins(pins: Sequence[ReleasePin]) -> tuple[ReleasePin, ...]:
    """Require a nonempty release generation with unique stable identities."""
    pinned = tuple(pins)
    if not pinned:
        raise ReleasePinError("a release generation requires at least one pinned slot")
    _validate_axes(pinned)
    return pinned


def _validate_axes(pins: Sequence[ReleasePin]) -> None:
    pinned = tuple(pins)
    for field in ("role", "assigned_name", "locator"):
        values = tuple(getattr(pin, field) for pin in pinned)
        if len(set(values)) != len(values):
            raise ReleasePinError(f"duplicate release pin {field} axis")


__all__ = (
    "ReleasePin",
    "ReleasePinError",
    "ReleasePinsProjection",
    "decode_release_pin_projection",
    "decode_release_pins",
    "encode_release_pins",
    "validate_release_pins",
)
