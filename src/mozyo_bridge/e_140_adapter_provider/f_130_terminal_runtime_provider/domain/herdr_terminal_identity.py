"""Fail-closed joins for Herdr's server-owned terminal identity (#15227)."""

from typing import Mapping, Optional, Sequence

from .herdr_identity import (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
    AGENT_KEY_NAME,
    AGENT_KEY_TERMINAL_ID,
    _agent_locator,
    _norm,
)


_LOCATOR_KEYS = (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
)


def _canonical_snapshot(agents) -> tuple[Mapping[str, object], ...] | None:
    rows = tuple(agents)
    names: list[str] = []
    locators: list[str] = []
    terminals: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        terminal_id = terminal_identity_of_row(row)
        if terminal_id is None:
            return None
        raw_name = row.get(AGENT_KEY_NAME)
        if type(raw_name) is not str or not raw_name or raw_name.strip() != raw_name:
            return None
        supplied = [row.get(key) for key in _LOCATOR_KEYS if row.get(key) is not None]
        if not supplied or any(
            type(value) is not str or not value or value.strip() != value
            for value in supplied
        ):
            return None
        locator = _agent_locator(row)
        if not locator or any(value != locator for value in supplied):
            return None
        names.append(raw_name)
        locators.append(locator)
        terminals.append(terminal_id)
    if (
        len(names) != len(set(names))
        or len(locators) != len(set(locators))
        or len(terminals) != len(set(terminals))
    ):
        return None
    return rows


def terminal_identity_of_row(agent: Mapping[str, object]) -> Optional[str]:
    """Return one exact, nonblank Herdr terminal id, otherwise ``None``."""
    value = agent.get(AGENT_KEY_TERMINAL_ID)
    if type(value) is not str or not value or value.strip() != value:
        return None
    return value


def terminal_identity_snapshot_complete(agents) -> bool:
    """Whether every row has globally unique canonical name/locator/terminal axes."""
    return _canonical_snapshot(agents) is not None


def terminal_identity_of_locator(
    locator: object, agents: Sequence[Mapping[str, object]]
) -> Optional[str]:
    """Resolve a terminal only when exactly one row claims ``locator``."""
    if type(locator) is not str or not locator or locator.strip() != locator:
        return None
    wanted = locator
    rows = _canonical_snapshot(agents)
    if rows is None:
        return None
    matches = [
        row for row in rows if _agent_locator(row) == wanted
    ]
    return terminal_identity_of_row(matches[0]) if len(matches) == 1 else None


def terminal_identity_of_live_slot(
    assigned_name: object,
    locator: object,
    agents: Sequence[Mapping[str, object]],
) -> Optional[str]:
    """Return a globally unique terminal for one exact name+locator row."""
    if (
        type(assigned_name) is not str
        or not assigned_name
        or assigned_name.strip() != assigned_name
        or type(locator) is not str
        or not locator
        or locator.strip() != locator
    ):
        return None
    name = assigned_name
    pane = locator
    rows = _canonical_snapshot(agents)
    if rows is None:
        return None
    named = [
        row for row in rows
        if row.get(AGENT_KEY_NAME) == name
    ]
    located = [
        row for row in rows
        if _agent_locator(row) == pane
    ]
    if len(named) != 1 or len(located) != 1 or named[0] is not located[0]:
        return None
    terminal_id = terminal_identity_of_row(named[0])
    claims = [
        row for row in rows
        if terminal_identity_of_row(row) == terminal_id
    ]
    return terminal_id if terminal_id is not None and len(claims) == 1 else None


__all__ = (
    "terminal_identity_of_live_slot",
    "terminal_identity_of_locator",
    "terminal_identity_of_row",
    "terminal_identity_snapshot_complete",
)
