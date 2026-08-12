"""Private immutable restore-container anchors for one offline rollout (#15227)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .offline_rollout_restore_intent import OfflineRolloutRestoreIntent


RESTORE_CONTAINER_INTENT_VERSION = 1
_TOP_FIELDS = frozenset({"version", "groups"})
_GROUP_FIELDS = frozenset(
    {
        "expected_startup_action_id",
        "logical_workspace_id",
        "lane_id",
        "workspace_id",
        "tab_id",
        "pane_locator",
        "terminal_id",
    }
)
_WORKSPACE = re.compile(r"^w[A-Za-z0-9]+$")
_PANE = re.compile(r"^(w[A-Za-z0-9]+):p[A-Za-z0-9]+$")
_TAB = re.compile(r"^(w[A-Za-z0-9]+):t[A-Za-z0-9]+$")
_ACTION = re.compile(r"^startup-[0-9a-f]{64}$")


class OfflineRolloutContainerIntentError(ValueError):
    """The sealed passive container roster is absent or malformed."""


def _token(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OfflineRolloutContainerIntentError("restore_container_intent_invalid")
    return value


@dataclass(frozen=True, repr=False)
class OfflineRolloutRestoreContainer:
    expected_startup_action_id: str = field(repr=False)
    logical_workspace_id: str
    lane_id: str
    workspace_id: str
    tab_id: str
    pane_locator: str
    terminal_id: str = field(repr=False)

    def as_payload(self) -> dict:
        return {
            "expected_startup_action_id": self.expected_startup_action_id,
            "logical_workspace_id": self.logical_workspace_id,
            "lane_id": self.lane_id,
            "workspace_id": self.workspace_id,
            "tab_id": self.tab_id,
            "pane_locator": self.pane_locator,
            "terminal_id": self.terminal_id,
        }


@dataclass(frozen=True, repr=False)
class OfflineRolloutContainerIntent:
    groups: tuple[OfflineRolloutRestoreContainer, ...]
    version: int = RESTORE_CONTAINER_INTENT_VERSION

    def as_payload(self) -> dict:
        return {
            "version": self.version,
            "groups": [group.as_payload() for group in self.groups],
        }

    def for_action(self, action_id: str) -> OfflineRolloutRestoreContainer:
        matches = [
            group
            for group in self.groups
            if group.expected_startup_action_id == action_id
        ]
        if len(matches) != 1:
            raise OfflineRolloutContainerIntentError(
                "restore_container_intent_invalid"
            )
        return matches[0]


def build_container_intent(
    rows: Iterable[Mapping[str, object]],
    *,
    restore_intent: OfflineRolloutRestoreIntent,
) -> OfflineRolloutContainerIntent:
    payload = {
        "restore_container_intent": {
            "version": RESTORE_CONTAINER_INTENT_VERSION,
            "groups": [dict(row) for row in rows],
        }
    }
    return decode_container_intent(payload, restore_intent=restore_intent)


def decode_container_intent(
    private_bindings: object,
    *,
    restore_intent: OfflineRolloutRestoreIntent,
) -> OfflineRolloutContainerIntent:
    if not isinstance(private_bindings, Mapping):
        raise OfflineRolloutContainerIntentError("restore_container_intent_invalid")
    raw = private_bindings.get("restore_container_intent")
    if not isinstance(raw, Mapping) or set(raw) != _TOP_FIELDS:
        raise OfflineRolloutContainerIntentError("restore_container_intent_invalid")
    if type(raw.get("version")) is not int or raw.get(
        "version"
    ) != RESTORE_CONTAINER_INTENT_VERSION or not isinstance(
        raw.get("groups"), list
    ):
        raise OfflineRolloutContainerIntentError("restore_container_intent_invalid")
    if len(raw["groups"]) != len(restore_intent.groups):
        raise OfflineRolloutContainerIntentError("restore_container_intent_invalid")

    groups = []
    for value, expected in zip(raw["groups"], restore_intent.groups):
        if not isinstance(value, Mapping) or set(value) != _GROUP_FIELDS:
            raise OfflineRolloutContainerIntentError(
                "restore_container_intent_invalid"
            )
        group = OfflineRolloutRestoreContainer(
            expected_startup_action_id=_token(
                value.get("expected_startup_action_id")
            ),
            logical_workspace_id=_token(value.get("logical_workspace_id")),
            lane_id=_token(value.get("lane_id")),
            workspace_id=_token(value.get("workspace_id")),
            tab_id=_token(value.get("tab_id")),
            pane_locator=_token(value.get("pane_locator")),
            terminal_id=_token(value.get("terminal_id")),
        )
        if (
            _ACTION.fullmatch(group.expected_startup_action_id) is None
            or group.expected_startup_action_id
            != expected.expected_startup_action_id
            or group.logical_workspace_id != expected.workspace_id
            or group.lane_id != expected.lane_id
            or _WORKSPACE.fullmatch(group.workspace_id) is None
            or _TAB.fullmatch(group.tab_id) is None
            or _PANE.fullmatch(group.pane_locator) is None
            or group.tab_id.split(":", 1)[0] != group.workspace_id
            or group.pane_locator.split(":", 1)[0] != group.workspace_id
        ):
            raise OfflineRolloutContainerIntentError(
                "restore_container_intent_invalid"
            )
        groups.append(group)
    return OfflineRolloutContainerIntent(tuple(groups))


def require_container_pane_join(container_intent, pane_intent) -> None:
    """Require every sealed anchor to be the exact same sealed passive pane."""

    passive = {
        (pane.locator, pane.workspace_id, pane.tab_id, pane.terminal_id)
        for pane in pane_intent.panes
    }
    anchors = {
        (
            group.pane_locator,
            group.workspace_id,
            group.tab_id,
            group.terminal_id,
        )
        for group in container_intent.groups
    }
    if not anchors <= passive:
        raise OfflineRolloutContainerIntentError(
            "restore_container_intent_invalid"
        )


__all__ = (
    "OfflineRolloutContainerIntent",
    "OfflineRolloutContainerIntentError",
    "OfflineRolloutRestoreContainer",
    "RESTORE_CONTAINER_INTENT_VERSION",
    "build_container_intent",
    "decode_container_intent",
    "require_container_pane_join",
)
