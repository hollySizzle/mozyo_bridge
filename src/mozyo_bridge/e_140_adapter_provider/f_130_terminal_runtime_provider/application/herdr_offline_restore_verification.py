"""Terminal-bound verification for agents restored by offline rollout (#15227)."""

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_launch_generation import verified_generation_token
from mozyo_bridge.core.state.herdr_inventory_identity import terminal_inventory_complete
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
)


def verify_restored_names(*, view, names, home, exact_roster=False) -> tuple[bool, str]:
    if (
        not terminal_inventory_complete(view)
        or view.unmanaged_agents
    ):
        return False, ""
    if exact_roster and {agent.name for agent in view.managed_agents} != set(names):
        return False, ""
    live = {agent.name: agent for agent in view.managed_agents}
    name_counts = {
        name: sum(1 for agent in view.managed_agents if agent.name == name)
        for name in live
    }
    terminal_counts = {
        agent.terminal_id: sum(
            1 for candidate in view.agents
            if candidate.terminal_id == agent.terminal_id
        )
        for agent in view.agents if agent.terminal_id
    }
    locator_counts = {
        agent.locator: sum(
            1 for candidate in view.agents if candidate.locator == agent.locator
        )
        for agent in view.agents if agent.locator
    }
    store = HerdrIdentityAttestationStore(home=home)
    for name in names:
        agent = live.get(name)
        record = store.read(name)
        if (
            agent is None
            or name_counts.get(name) != 1
            or locator_counts.get(agent.locator) != 1
        ):
            return False, name
        terminal_id = (
            agent.terminal_id if terminal_counts.get(agent.terminal_id) == 1 else None
        )
        joined = evaluate_attestation(
            record,
            live_locator=agent.locator,
            live_terminal_id=terminal_id,
            expected_workspace_id=agent.workspace_id,
            expected_role=agent.role,
            expected_lane=agent.lane_id,
        )
        generation = verified_generation_token(
            home, assigned_name=name, workspace_id=agent.workspace_id,
            role=agent.role, lane_id=agent.lane_id, locator=agent.locator,
            live_terminal_id=terminal_id, norm=_norm, norm_lane=_norm_lane,
        )
        if not joined.ok or not generation:
            return False, name
    return True, ""


__all__ = ("verify_restored_names",)
