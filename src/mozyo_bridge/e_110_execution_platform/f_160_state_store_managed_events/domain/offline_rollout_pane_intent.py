"""Private immutable pane residue allowed across an offline rollout (#15227)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping


PASSIVE_PANE_INTENT_VERSION = 1
_TOP_FIELDS = frozenset({"version", "panes"})
_PANE_FIELDS = frozenset({"locator", "workspace_id", "tab_id", "terminal_id"})
_WORKSPACE = re.compile(r"^w[A-Za-z0-9]+$")
_PANE = re.compile(r"^(w[A-Za-z0-9]+):p[A-Za-z0-9]+$")
_TAB = re.compile(r"^(w[A-Za-z0-9]+):t[A-Za-z0-9]+$")


class OfflineRolloutPaneIntentError(ValueError):
    """The sealed passive-pane roster is absent or malformed."""


def _token(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
    return value


@dataclass(frozen=True)
class OfflineRolloutPassivePane:
    locator: str
    workspace_id: str
    tab_id: str
    terminal_id: str = field(repr=False)

    def as_payload(self) -> dict:
        return {
            "locator": self.locator,
            "workspace_id": self.workspace_id,
            "tab_id": self.tab_id,
            "terminal_id": self.terminal_id,
        }


@dataclass(frozen=True)
class OfflineRolloutPaneIntent:
    panes: tuple[OfflineRolloutPassivePane, ...]
    version: int = PASSIVE_PANE_INTENT_VERSION

    def as_payload(self) -> dict:
        return {"version": self.version, "panes": [row.as_payload() for row in self.panes]}


def build_pane_intent(pane_rows, *, agents) -> OfflineRolloutPaneIntent:
    observed_agents = tuple(agents)
    agent_identities = {
        getattr(agent, "locator", ""): (
            getattr(agent, "terminal_id", ""),
            getattr(agent, "role", ""),
        )
        for agent in observed_agents
    }
    agent_terminals = {
        getattr(agent, "terminal_id", "") for agent in observed_agents
    }
    if (
        len(agent_identities) != len(observed_agents)
        or len(agent_terminals) != len(observed_agents)
        or any(
            not locator or not terminal or not role
            for locator, (terminal, role) in agent_identities.items()
        )
    ):
        raise OfflineRolloutPaneIntentError("passive_pane_inventory_mismatch")
    rows = tuple(pane_rows)
    if any(
        not isinstance(row, Mapping)
        or _PANE.fullmatch(str(row.get("pane_id") or "")) is None
        or _WORKSPACE.fullmatch(str(row.get("workspace_id") or "")) is None
        or _TAB.fullmatch(str(row.get("tab_id") or "")) is None
        or _token(row.get("terminal_id")) != row.get("terminal_id")
        or row["pane_id"].split(":", 1)[0] != row["workspace_id"]
        or row["tab_id"].split(":", 1)[0] != row["workspace_id"]
        for row in rows
    ):
        raise OfflineRolloutPaneIntentError("passive_pane_inventory_mismatch")
    by_locator = {
        row.get("pane_id"): row for row in rows if isinstance(row, Mapping)
    }
    if (
        len(by_locator) != len(rows)
        or len({row["terminal_id"] for row in rows}) != len(rows)
        or any(
            locator not in by_locator
            or by_locator[locator].get("terminal_id") != identity[0]
            or by_locator[locator].get("agent") != identity[1]
            for locator, identity in agent_identities.items()
        )
        or any(
            row.get("pane_id") not in agent_identities
            and row.get("agent") not in (None, "")
            for row in rows
        )
    ):
        raise OfflineRolloutPaneIntentError("passive_pane_inventory_mismatch")
    return decode_pane_intent(
        {
            "passive_pane_intent": {
                "version": PASSIVE_PANE_INTENT_VERSION,
                "panes": sorted(
                    [
                        {
                            "locator": row.get("pane_id"),
                            "workspace_id": row.get("workspace_id"),
                            "tab_id": row.get("tab_id"),
                            "terminal_id": row.get("terminal_id"),
                        }
                        for row in rows
                        if row.get("pane_id") not in agent_identities
                    ],
                    key=lambda row: (row["locator"], row["terminal_id"]),
                ),
            }
        }
    )


def decode_pane_intent(private_bindings: object) -> OfflineRolloutPaneIntent:
    if not isinstance(private_bindings, Mapping):
        raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
    raw = private_bindings.get("passive_pane_intent")
    if not isinstance(raw, Mapping) or set(raw) != _TOP_FIELDS:
        raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
    if type(raw.get("version")) is not int or raw.get(
        "version"
    ) != PASSIVE_PANE_INTENT_VERSION or not isinstance(
        raw.get("panes"), list
    ):
        raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
    panes = []
    identities: set[tuple[str, str]] = set()
    for row in raw["panes"]:
        if not isinstance(row, Mapping) or set(row) != _PANE_FIELDS:
            raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
        pane = OfflineRolloutPassivePane(
            locator=_token(row.get("locator")),
            workspace_id=_token(row.get("workspace_id")),
            tab_id=_token(row.get("tab_id")),
            terminal_id=_token(row.get("terminal_id")),
        )
        if (
            _WORKSPACE.fullmatch(pane.workspace_id) is None
            or _PANE.fullmatch(pane.locator) is None
            or _TAB.fullmatch(pane.tab_id) is None
            or pane.locator.split(":", 1)[0] != pane.workspace_id
            or pane.tab_id.split(":", 1)[0] != pane.workspace_id
            or (pane.locator, pane.terminal_id) in identities
        ):
            raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
        identities.add((pane.locator, pane.terminal_id))
        panes.append(pane)
    ordered = tuple(sorted(panes, key=lambda row: (row.locator, row.terminal_id)))
    if tuple(panes) != ordered:
        raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
    if len({row.locator for row in ordered}) != len(ordered) or len(
        {row.terminal_id for row in ordered}
    ) != len(ordered):
        raise OfflineRolloutPaneIntentError("passive_pane_intent_invalid")
    return OfflineRolloutPaneIntent(ordered)


__all__ = (
    "OfflineRolloutPaneIntent",
    "OfflineRolloutPaneIntentError",
    "OfflineRolloutPassivePane",
    "PASSIVE_PANE_INTENT_VERSION",
    "build_pane_intent",
    "decode_pane_intent",
)
