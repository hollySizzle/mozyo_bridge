"""Private generation pins for scratch retirement replay (#15227 j#103467)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence


class ScratchRetirementPinError(ValueError):
    """A pin projection is malformed or not writable by this runtime."""


def _token(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ScratchRetirementPinError(f"scratch retirement pin {field} is not canonical")
    return value


@dataclass(frozen=True)
class ScratchRetirementPin:
    role: str
    assigned_name: str
    locator: str
    startup_action_id: str

    def __post_init__(self) -> None:
        for field in ("role", "assigned_name", "locator", "startup_action_id"):
            _token(getattr(self, field), field)


@dataclass(frozen=True)
class ScratchRetirementPinProjection:
    version: int
    pins: tuple[ScratchRetirementPin, ...]
    legacy_pairs: tuple[tuple[str, str], ...] = ()

    @property
    def current_authority(self) -> bool:
        return self.version == 2


def encode_scratch_retirement_pins(
    pins: Sequence[ScratchRetirementPin],
) -> str:
    wanted = tuple(pins)
    _validate_set(wanted)
    return json.dumps(
        {
            "v": 2,
            "pins": [
                {
                    "role": pin.role,
                    "assigned_name": pin.assigned_name,
                    "locator": pin.locator,
                    "startup_action_id": pin.startup_action_id,
                }
                for pin in wanted
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def decode_scratch_retirement_pin_projection(
    value: object,
) -> ScratchRetirementPinProjection:
    if type(value) is not str:
        raise ScratchRetirementPinError("scratch retirement pin projection is not text")
    try:
        raw = json.loads(value)
    except (TypeError, ValueError):
        return _decode_legacy(value)
    if not isinstance(raw, dict) or set(raw) != {"v", "pins"}:
        # The legacy writer used tab/newline text, never a JSON object.
        if isinstance(raw, (dict, list)):
            raise ScratchRetirementPinError("scratch retirement pin envelope is not exact")
        return _decode_legacy(value)
    if type(raw["v"]) is not int or raw["v"] != 2 or not isinstance(raw["pins"], list):
        raise ScratchRetirementPinError("scratch retirement pin version is unsupported")
    pins: list[ScratchRetirementPin] = []
    for item in raw["pins"]:
        if not isinstance(item, dict) or set(item) != {
            "role", "assigned_name", "locator", "startup_action_id"
        }:
            raise ScratchRetirementPinError("scratch retirement pin item is not exact")
        pins.append(ScratchRetirementPin(**item))
    _validate_set(pins)
    return ScratchRetirementPinProjection(2, tuple(pins))


def _decode_legacy(value: str) -> ScratchRetirementPinProjection:
    # Preserve old bytes only as typed diagnostic provenance. No current token is fabricated.
    if not value:
        return ScratchRetirementPinProjection(1, (), ())
    pairs: list[tuple[str, str]] = []
    for chunk in value.split("\n"):
        role, separator, locator = chunk.partition("\t")
        if (
            not separator
            or type(role) is not str
            or not role
            or role.strip() != role
            or type(locator) is not str
            or not locator
            or locator.strip() != locator
        ):
            raise ScratchRetirementPinError("legacy scratch retirement pins are malformed")
        pairs.append((role, locator))
    if len({role for role, _ in pairs}) != len(pairs) or len(
        {locator for _, locator in pairs}
    ) != len(pairs):
        raise ScratchRetirementPinError("legacy scratch retirement pin axis is duplicated")
    # Lossless provenance is carried separately; tokenless legacy slots are intentionally not
    # projected as current ScratchRetirementPin values.
    return ScratchRetirementPinProjection(1, (), tuple(pairs))


def _validate_set(pins: Sequence[ScratchRetirementPin]) -> None:
    wanted = tuple(pins)
    for axis in (
        tuple(pin.role for pin in wanted),
        tuple(pin.assigned_name for pin in wanted),
        tuple(pin.locator for pin in wanted),
    ):
        if len(set(axis)) != len(axis):
            raise ScratchRetirementPinError("scratch retirement pin axis is duplicated")


__all__ = (
    "ScratchRetirementPin",
    "ScratchRetirementPinError",
    "ScratchRetirementPinProjection",
    "decode_scratch_retirement_pin_projection",
    "encode_scratch_retirement_pins",
)
