"""Terminal-bound verification for agents restored by offline rollout (#15227)."""

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
)
from mozyo_bridge.core.state.herdr_inventory_identity import terminal_inventory_complete
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)


def verify_restored_names(
    *,
    view,
    names,
    home,
    exact_roster=False,
    expected_identities=None,
    expected_tokens=None,
) -> tuple[bool, str]:
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
        try:
            record = store.read(name)
        except Exception:  # noqa: BLE001 - unreadable authority is never a restored slot
            return False, name
        identity = (expected_identities or {}).get(name)
        if (
            agent is None
            or name_counts.get(name) != 1
            or locator_counts.get(agent.locator) != 1
            or (
                identity is not None
                and (
                    agent.workspace_id,
                    agent.lane_id,
                    agent.role,
                    agent.name,
                )
                != (
                    identity.workspace_id,
                    identity.lane_id,
                    identity.provider,
                    identity.assigned_name,
                )
            )
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
        generation = verified_terminal_generation_token(
            home,
            assigned_name=name,
            workspace_id=agent.workspace_id,
            role=agent.role,
            lane_id=agent.lane_id,
            locator=agent.locator,
            terminal_id=terminal_id or "",
        )
        expected = (expected_tokens or {}).get(name)
        if (
            type(getattr(record, "schema_version", None)) is not int
            or getattr(record, "schema_version")
            != HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
            or not joined.ok
            or not generation
            or (expected is not None and generation != expected)
        ):
            return False, name
    return True, ""


__all__ = ("verify_restored_names",)
