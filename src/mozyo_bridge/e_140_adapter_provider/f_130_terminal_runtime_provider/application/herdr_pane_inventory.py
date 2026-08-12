"""Strict full-pane inventory used by terminal-bound offline authority (#15227)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    valid_target,
)


def pane_rows_complete(rows: object) -> bool:
    """Whether rows are one complete, unique locator+terminal pane snapshot."""
    if not isinstance(rows, (tuple, list)):
        return False
    pane_ids: set[str] = set()
    terminal_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        pane_id = row.get("pane_id")
        workspace_id = row.get("workspace_id")
        tab_id = row.get("tab_id")
        terminal_id = row.get("terminal_id")
        if (
            not valid_target(pane_id)
            or not valid_target(workspace_id)
            or not valid_target(tab_id)
            or _workspace_prefix(pane_id) != workspace_id
            or not tab_id.startswith(f"{workspace_id}:t")
            or tab_id == f"{workspace_id}:t"
            or type(terminal_id) is not str
            or not terminal_id
            or terminal_id.strip() != terminal_id
            or pane_id in pane_ids
            or terminal_id in terminal_ids
        ):
            return False
        pane_ids.add(pane_id)
        terminal_ids.add(terminal_id)
    return True


def agent_pane_rows_exact(agents: object, rows: object) -> bool:
    """Whether live agents and agent-bearing panes form one exact two-way join.

    Pane ``workspace_id`` is the physical Herdr workspace encoded by ``pane_id``;
    it is deliberately not compared with an agent's logical project workspace.
    Shell/root panes remain valid when their ``agent`` value is absent or empty.
    """
    if not pane_rows_complete(rows):
        return False
    try:
        agent_rows = tuple(agents)
        by_locator = {row["pane_id"]: row for row in rows}
        agents_by_locator = {agent.locator: agent for agent in agent_rows}
        return bool(
            len(agents_by_locator) == len(agent_rows)
            and all(
                agent.locator in by_locator
                and by_locator[agent.locator]["terminal_id"]
                == agent.terminal_id
                and by_locator[agent.locator].get("agent") == agent.role
                for agent in agent_rows
            )
            and all(
                row.get("agent") in (None, "")
                or (
                    row["pane_id"] in agents_by_locator
                    and row.get("agent")
                    == agents_by_locator[row["pane_id"]].role
                    and row["terminal_id"]
                    == agents_by_locator[row["pane_id"]].terminal_id
                )
                for row in rows
            )
        )
    except (AttributeError, KeyError, TypeError):
        return False


def strict_pane_rows(stdout: object) -> tuple[Mapping[str, object], ...]:
    """Parse only Herdr 0.8's canonical complete ``pane list`` envelope."""
    if not isinstance(stdout, str):
        raise ValueError("pane list did not return text")
    payload = json.loads(stdout)
    if not isinstance(payload, Mapping):
        raise ValueError("pane list did not return an object")
    result = payload.get("result")
    if not isinstance(result, Mapping) or result.get("type") != "pane_list":
        raise ValueError("pane list result type is not pane_list")
    rows = result.get("panes")
    if not isinstance(rows, list) or not pane_rows_complete(rows):
        raise ValueError("pane list contains an incomplete or duplicate identity")
    return tuple(rows)


__all__ = (
    "agent_pane_rows_exact",
    "pane_rows_complete",
    "strict_pane_rows",
)
