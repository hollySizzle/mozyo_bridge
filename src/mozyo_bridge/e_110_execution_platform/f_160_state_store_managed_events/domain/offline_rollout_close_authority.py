"""Private generation-bound close pins for the global offline rollout (#15227).

The public rollout plan and the top-level action stay schema v1.  Destructive
stop authority is a separate, closed-shape v2 object inside ``private_bindings``
so old action records remain readable while new execution can fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping


CLOSE_AUTHORITY_VERSION = 2
_AUTHORITY_FIELDS = frozenset({"version", "pins"})
_PIN_FIELDS = frozenset(
    {
        "workspace_id",
        "lane_id",
        "role",
        "assigned_name",
        "locator",
        "startup_action_id",
    }
)
_PLAN_IDENTITY_FIELDS = (
    "workspace_id",
    "lane_id",
    "provider",
    "assigned_name",
)
_STARTUP_ACTION_ID = re.compile(r"startup-(?:ir1-)?[0-9a-f]{64}")


class OfflineRolloutCloseAuthorityError(ValueError):
    """The private close authority is absent or malformed; effects must stop."""


def _token(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise OfflineRolloutCloseAuthorityError("close_authority_pin_invalid")
    return value


def _startup_action_id(value: object) -> str:
    token = _token(value)
    if _STARTUP_ACTION_ID.fullmatch(token) is None:
        raise OfflineRolloutCloseAuthorityError("close_authority_pin_invalid")
    return token


@dataclass(frozen=True)
class OfflineRolloutClosePin:
    workspace_id: str
    lane_id: str
    role: str
    assigned_name: str
    locator: str = field(repr=False)
    startup_action_id: str = field(repr=False)

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "startup_action_id": self.startup_action_id,
        }


@dataclass(frozen=True)
class OfflineRolloutCloseAuthority:
    pins: tuple[OfflineRolloutClosePin, ...]
    version: int = CLOSE_AUTHORITY_VERSION

    def as_payload(self) -> dict:
        return {
            "version": self.version,
            "pins": [pin.as_payload() for pin in self.pins],
        }

    def by_name(self) -> Mapping[str, OfflineRolloutClosePin]:
        return {pin.assigned_name: pin for pin in self.pins}


def _plan_identities(plan: Mapping[str, object]) -> Mapping[str, tuple[str, ...]]:
    raw_agents = plan.get("agents")
    if not isinstance(raw_agents, list):
        raise OfflineRolloutCloseAuthorityError("close_authority_plan_invalid")
    identities: dict[str, tuple[str, ...]] = {}
    for raw in raw_agents:
        if not isinstance(raw, Mapping):
            raise OfflineRolloutCloseAuthorityError("close_authority_plan_invalid")
        try:
            values = tuple(_token(raw.get(field)) for field in _PLAN_IDENTITY_FIELDS)
        except OfflineRolloutCloseAuthorityError as exc:
            raise OfflineRolloutCloseAuthorityError(
                "close_authority_plan_invalid"
            ) from exc
        name = values[-1]
        if name in identities:
            raise OfflineRolloutCloseAuthorityError("close_authority_plan_invalid")
        identities[name] = values
    return identities


def decode_close_authority(
    private_bindings: object,
    *,
    plan: Mapping[str, object],
) -> OfflineRolloutCloseAuthority:
    """Decode one exact v2 authority and join its pins to every live plan agent.

    This decoder deliberately does not migrate or backfill old actions.  Status
    reads continue through the top-level v1 action decoder; execution calls this
    stricter boundary before the first phase effect.
    """

    if not isinstance(private_bindings, Mapping):
        raise OfflineRolloutCloseAuthorityError("close_authority_missing")
    raw = private_bindings.get("close_authority")
    if raw is None:
        raise OfflineRolloutCloseAuthorityError("close_authority_missing")
    if not isinstance(raw, Mapping) or set(raw) != _AUTHORITY_FIELDS:
        raise OfflineRolloutCloseAuthorityError("close_authority_shape_invalid")
    version = raw.get("version")
    if type(version) is not int or version != CLOSE_AUTHORITY_VERSION:
        raise OfflineRolloutCloseAuthorityError(
            "close_authority_schema_unsupported"
        )
    raw_pins = raw.get("pins")
    if not isinstance(raw_pins, list):
        raise OfflineRolloutCloseAuthorityError("close_authority_pins_invalid")

    pins = []
    names: set[str] = set()
    locators: set[str] = set()
    for raw_pin in raw_pins:
        if not isinstance(raw_pin, Mapping) or set(raw_pin) != _PIN_FIELDS:
            raise OfflineRolloutCloseAuthorityError(
                "close_authority_pin_shape_invalid"
            )
        pin = OfflineRolloutClosePin(
            workspace_id=_token(raw_pin.get("workspace_id")),
            lane_id=_token(raw_pin.get("lane_id")),
            role=_token(raw_pin.get("role")),
            assigned_name=_token(raw_pin.get("assigned_name")),
            locator=_token(raw_pin.get("locator")),
            startup_action_id=_startup_action_id(
                raw_pin.get("startup_action_id")
            ),
        )
        if pin.assigned_name in names or pin.locator in locators:
            raise OfflineRolloutCloseAuthorityError(
                "close_authority_pin_duplicate"
            )
        names.add(pin.assigned_name)
        locators.add(pin.locator)
        pins.append(pin)

    planned = _plan_identities(plan)
    observed = {
        pin.assigned_name: (
            pin.workspace_id,
            pin.lane_id,
            pin.role,
            pin.assigned_name,
        )
        for pin in pins
    }
    if observed != planned:
        raise OfflineRolloutCloseAuthorityError("close_authority_plan_mismatch")
    if pins != sorted(pins, key=lambda pin: pin.assigned_name):
        raise OfflineRolloutCloseAuthorityError("close_authority_pins_invalid")
    return OfflineRolloutCloseAuthority(tuple(pins))


__all__ = (
    "CLOSE_AUTHORITY_VERSION",
    "OfflineRolloutCloseAuthority",
    "OfflineRolloutCloseAuthorityError",
    "OfflineRolloutClosePin",
    "decode_close_authority",
)
