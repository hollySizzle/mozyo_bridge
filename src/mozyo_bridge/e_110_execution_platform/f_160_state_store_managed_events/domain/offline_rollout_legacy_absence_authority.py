"""Private non-destructive absence pins for legacy offline restore (#15227).

Legacy recovery groups are intentionally absent from the public plan's live agent
roster, and therefore from destructive ``close_authority`` v2.  This closed v1
object proves which completed historical generation must remain absent while that
group is restored; it never grants a close operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from .offline_rollout_restore_intent import (
    OfflineRolloutRestoreIntent,
    decode_restore_intent,
)


LEGACY_ABSENCE_AUTHORITY_VERSION = 1
_AUTHORITY_FIELDS = frozenset({"version", "pins"})
_PIN_FIELDS = frozenset(
    {
        "workspace_id",
        "lane_id",
        "provider",
        "assigned_name",
        "old_locator",
        "startup_action_id",
    }
)
_STARTUP_ACTION_ID = re.compile(r"startup-(?:ir1-)?[0-9a-f]{64}")


class OfflineRolloutLegacyAbsenceAuthorityError(ValueError):
    """The sealed legacy-absence authority is absent or malformed."""


def _token(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_pin_invalid"
        )
    return value


def _startup_action_id(value: object) -> str:
    token = _token(value)
    if _STARTUP_ACTION_ID.fullmatch(token) is None:
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_pin_invalid"
        )
    return token


@dataclass(frozen=True)
class OfflineRolloutLegacyAbsencePin:
    workspace_id: str
    lane_id: str
    provider: str
    assigned_name: str
    old_locator: str = field(repr=False)
    startup_action_id: str = field(repr=False)

    @property
    def role(self) -> str:
        """Close-pin-compatible read seam; this authority never licenses close."""

        return self.provider

    @property
    def locator(self) -> str:
        """Historical locator alias used only by shared absence verification."""

        return self.old_locator

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "old_locator": self.old_locator,
            "startup_action_id": self.startup_action_id,
        }


@dataclass(frozen=True)
class OfflineRolloutLegacyAbsenceAuthority:
    pins: tuple[OfflineRolloutLegacyAbsencePin, ...]
    version: int = LEGACY_ABSENCE_AUTHORITY_VERSION

    def as_payload(self) -> dict:
        return {
            "version": self.version,
            "pins": [pin.as_payload() for pin in self.pins],
        }

    def by_name(self) -> Mapping[str, OfflineRolloutLegacyAbsencePin]:
        return {pin.assigned_name: pin for pin in self.pins}


def _plan_agent_names(plan: Mapping[str, object]) -> set[str]:
    raw_agents = plan.get("agents")
    if not isinstance(raw_agents, list):
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_plan_invalid"
        )
    names = []
    for row in raw_agents:
        if not isinstance(row, Mapping):
            raise OfflineRolloutLegacyAbsenceAuthorityError(
                "legacy_absence_authority_plan_invalid"
            )
        names.append(_token(row.get("assigned_name")))
    if len(names) != len(set(names)):
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_plan_invalid"
        )
    return set(names)


def _expected_identities(
    plan: Mapping[str, object], restore_intent: OfflineRolloutRestoreIntent
) -> Mapping[str, tuple[str, str, str, str]]:
    live_names = _plan_agent_names(plan)
    return {
        agent.assigned_name: (
            agent.workspace_id,
            agent.lane_id,
            agent.provider,
            agent.assigned_name,
        )
        for group in restore_intent.groups
        for agent in group.agents
        if agent.assigned_name not in live_names
    }


def decode_legacy_absence_authority(
    private_bindings: object,
    *,
    plan: Mapping[str, object],
    restore_intent: OfflineRolloutRestoreIntent | None = None,
) -> OfflineRolloutLegacyAbsenceAuthority:
    """Decode pins for exactly ``restore identities - plan.agents``."""

    if not isinstance(private_bindings, Mapping):
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_missing"
        )
    raw = private_bindings.get("legacy_absence_authority")
    if raw is None:
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_missing"
        )
    if not isinstance(raw, Mapping) or set(raw) != _AUTHORITY_FIELDS:
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_shape_invalid"
        )
    if (
        type(raw.get("version")) is not int
        or raw.get("version") != LEGACY_ABSENCE_AUTHORITY_VERSION
    ):
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_schema_unsupported"
        )
    raw_pins = raw.get("pins")
    if not isinstance(raw_pins, list):
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_pins_invalid"
        )

    pins = []
    names: set[str] = set()
    locators: set[str] = set()
    for value in raw_pins:
        if not isinstance(value, Mapping) or set(value) != _PIN_FIELDS:
            raise OfflineRolloutLegacyAbsenceAuthorityError(
                "legacy_absence_authority_pin_shape_invalid"
            )
        pin = OfflineRolloutLegacyAbsencePin(
            workspace_id=_token(value.get("workspace_id")),
            lane_id=_token(value.get("lane_id")),
            provider=_token(value.get("provider")),
            assigned_name=_token(value.get("assigned_name")),
            old_locator=_token(value.get("old_locator")),
            startup_action_id=_startup_action_id(
                value.get("startup_action_id")
            ),
        )
        if pin.assigned_name in names or pin.old_locator in locators:
            raise OfflineRolloutLegacyAbsenceAuthorityError(
                "legacy_absence_authority_pin_duplicate"
            )
        names.add(pin.assigned_name)
        locators.add(pin.old_locator)
        pins.append(pin)

    intent = restore_intent or decode_restore_intent(
        private_bindings, plan=plan
    )
    expected = _expected_identities(plan, intent)
    observed = {
        pin.assigned_name: (
            pin.workspace_id,
            pin.lane_id,
            pin.provider,
            pin.assigned_name,
        )
        for pin in pins
    }
    if observed != expected:
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_plan_mismatch"
        )
    if pins != sorted(pins, key=lambda pin: pin.assigned_name):
        raise OfflineRolloutLegacyAbsenceAuthorityError(
            "legacy_absence_authority_pins_invalid"
        )
    if pins:
        stores = plan.get("stores")
        generation = (
            stores.get("launch_generation")
            if isinstance(stores, Mapping)
            else None
        )
        if not (
            isinstance(generation, Mapping)
            and generation.get("state") == "recognized"
            and type(generation.get("version")) is int
            and generation.get("version") == 2
            and type(generation.get("target_version")) is int
            and generation.get("target_version") == 2
            and generation.get("upgrade_required") is False
        ):
            raise OfflineRolloutLegacyAbsenceAuthorityError(
                "legacy_absence_authority_generation_plan_invalid"
            )
    by_name = {pin.assigned_name: pin for pin in pins}
    for group in intent.groups:
        group_pins = [
            by_name[name] for name in group.assigned_names if name in by_name
        ]
        if group_pins and len(
            {pin.startup_action_id for pin in group_pins}
        ) != 1:
            raise OfflineRolloutLegacyAbsenceAuthorityError(
                "legacy_absence_authority_group_mismatch"
            )
    return OfflineRolloutLegacyAbsenceAuthority(tuple(pins))


__all__ = (
    "LEGACY_ABSENCE_AUTHORITY_VERSION",
    "OfflineRolloutLegacyAbsenceAuthority",
    "OfflineRolloutLegacyAbsenceAuthorityError",
    "OfflineRolloutLegacyAbsencePin",
    "decode_legacy_absence_authority",
)
