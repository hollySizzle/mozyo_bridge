"""Private completeness gate for offline rollout inventory authority."""

from collections.abc import Mapping
from mozyo_bridge.core.state.herdr_inventory_identity import (
    terminal_inventory_complete,
)


def private_agent_bindings(inventory, plan):
    """Capture restore identities only; destructive pins live in close-authority v2."""
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
        PhaseExecutionResult,
    )
    if not terminal_inventory_complete(inventory):
        return PhaseExecutionResult(False, reason="inventory_unreadable")
    planned = {
        row["assigned_name"]: row
        for row in plan.get("agents", ()) if isinstance(row, Mapping)
    }
    wanted = set(planned)
    if (
        len(planned) != len(plan.get("agents", ()))
        or {agent.name for agent in inventory.managed_agents} != wanted
        or inventory.unmanaged_agents
    ):
        return PhaseExecutionResult(False, reason="agent_set_drift")
    agents = []
    for agent in inventory.managed_agents:
        expected = planned[agent.name]
        if (
            not agent.locator
            or agent.workspace_id != expected.get("workspace_id")
            or agent.lane_id != expected.get("lane_id")
            or agent.role != expected.get("provider")
        ):
            return PhaseExecutionResult(False, reason="agent_set_drift")
        agents.append({
            "assigned_name": agent.name, "workspace_id": agent.workspace_id,
            "lane_id": agent.lane_id, "provider": agent.role,
        })
    return PhaseExecutionResult(True, receipt={"agents": agents})


def private_inventory_current(view, bindings) -> bool:
    """Compatibility identity-roster check; this never authorizes a close."""
    if not terminal_inventory_complete(view) or view.unmanaged_agents:
        return False
    if {agent.name for agent in view.managed_agents} != set(bindings):
        return False
    for agent in view.managed_agents:
        binding = bindings.get(agent.name)
        if binding is None or any((
            agent.workspace_id != binding["workspace_id"],
            agent.lane_id != binding["lane_id"],
            agent.role != binding["provider"],
        )):
            return False
    return True


__all__ = (
    "private_agent_bindings", "private_inventory_current",
    "terminal_inventory_complete",
)
