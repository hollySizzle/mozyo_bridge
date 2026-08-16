"""Pre-destructive private authority capture for an offline rollout (#15227)."""

from __future__ import annotations

import shutil
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
    merge_legacy_recovery_agent_bindings,
)
from .herdr_offline_rollout_runner import capture_provider_launch_bindings


def _fail(reason: str, detail: str = "") -> PhaseExecutionResult:
    return PhaseExecutionResult(False, reason=reason, detail=detail[:1000])


def capture_private_bindings(port, *, plan):
    from mozyo_bridge.core.state.workspace_registry import list_workspaces
    from mozyo_bridge.core.state.lane_epoch_adoption import legacy_adoption_refusal
    from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer
    from mozyo_bridge.core.state.lane_lifecycle_readonly import LaneLifecycleReader
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
        bind_lane_worktree,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
        read_live_worktree_fingerprint,
    )
    from .herdr_observability import read_herdr_inventory

    if port.repo_root is None:
        return _fail("repo_root_required")
    try:
        records = tuple(list_workspaces(home=port.home))
        lifecycle_rows = LaneLifecycleReader(home=port.home).records()
        inventory = read_herdr_inventory(port.repo_root, env=port.env)
        pane_inventory = port._pane_inventory()
    except Exception as exc:  # noqa: BLE001
        return _fail("private_binding_capture_failed", type(exc).__name__)
    from .herdr_offline_inventory_identity import private_agent_bindings

    wanted_workspaces = {row["workspace_id"] for row in plan.get("workspaces", ())}
    by_workspace = {record.workspace_id: record for record in records}
    if set(by_workspace) != wanted_workspaces:
        return _fail("workspace_set_drift")
    captured_agents = private_agent_bindings(inventory, plan)
    if not captured_agents.ok:
        return captured_agents
    from .herdr_offline_rollout_close import capture_close_authority

    close_authority = capture_close_authority(view=inventory, plan=plan, home=port.home)
    if not close_authority.ok:
        return close_authority
    try:
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_pane_intent import (  # noqa: E501
            build_pane_intent,
        )

        pane_intent = build_pane_intent(pane_inventory, agents=inventory.agents)
    except Exception as exc:  # noqa: BLE001
        return _fail("passive_pane_intent_capture_failed", type(exc).__name__)
    merged = merge_legacy_recovery_agent_bindings(
        plan=plan, agents=list(captured_agents.receipt["agents"])
    )
    if not merged.ok:
        return merged
    agents = list(merged.receipt["agents"])
    try:
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_restore_intent import (  # noqa: E501
            build_restore_intent,
        )
        from .herdr_startup_transaction import new_action_nonce
        from .herdr_offline_rollout_legacy_absence import (
            capture_legacy_absence_authority,
        )
        from .herdr_offline_rollout_restore import capture_restore_container_intent

        restore_intent = build_restore_intent(plan, nonce_factory=new_action_nonce)
        legacy_absence = capture_legacy_absence_authority(
            home=port.home,
            plan=plan,
            restore_intent=restore_intent,
            view=inventory,
            pane_rows=pane_inventory,
        )
        if not legacy_absence.ok:
            return legacy_absence
        sealed_restore_bindings = {
            "close_authority": close_authority.receipt["close_authority"],
            "legacy_absence_authority": legacy_absence.receipt[
                "legacy_absence_authority"
            ],
            "restore_intent": restore_intent.as_payload(),
            "passive_pane_intent": pane_intent.as_payload(),
        }
        restore_container_intent = capture_restore_container_intent(
            home=port.home,
            plan=plan,
            private_bindings=sealed_restore_bindings,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("restore_intent_capture_failed", type(exc).__name__)
    recovery_paths = {}
    for recovery in plan.get("legacy_recoveries", ()):
        matches = [
            row
            for row in lifecycle_rows
            if row.issue_id == recovery["issue_id"]
            and row.repo_workspace_id == recovery["workspace_id"]
            and row.lane_id == recovery["lane_id"]
            and row.lane_generation == recovery["lane_generation"]
        ]
        if len(matches) != 1:
            return _fail("legacy_recovery_row_drift", recovery["issue_id"])
        row = matches[0]
        decision = DecisionPointer(
            "redmine", recovery["issue_id"], recovery["journal_id"]
        )
        refusal = legacy_adoption_refusal(
            row,
            expected_revision=recovery["expected_revision"],
            issue_id=recovery["issue_id"],
            decision=decision,
        )
        registry = by_workspace.get(recovery["workspace_id"])
        bound = (
            bind_lane_worktree(
                Path(registry.canonical_path),
                lifecycle_rows,
                workspace=recovery["workspace_id"],
                lane=recovery["lane_id"],
                generation=recovery["lane_generation"],
            )
            if registry is not None and refusal is None
            else None
        )
        if bound is None or row.worktree_identity != recovery["worktree"]["identity"]:
            return _fail("legacy_recovery_worktree_drift", recovery["issue_id"])
        worktree, _branch = bound
        fingerprint = read_live_worktree_fingerprint(worktree, 30.0)
        expected_wip = recovery["worktree"]["wip"]
        if (
            not fingerprint.readable
            or fingerprint.digest != expected_wip["digest"]
            or fingerprint.dirty != expected_wip["dirty"]
            or fingerprint.untracked != expected_wip["untracked"]
        ):
            return _fail("legacy_recovery_wip_drift", recovery["issue_id"])
        recovery_paths[f"legacy:{recovery['issue_id']}"] = str(worktree.resolve())
    target_cli = shutil.which("mozyo-bridge", path=port.env.get("PATH"))
    pipx = shutil.which("pipx", path=port.env.get("PATH"))
    if not target_cli or not pipx:
        return _fail("runtime_installer_unavailable")
    try:
        from ..infrastructure.herdr_transport import resolve_herdr_binary

        herdr_binary = resolve_herdr_binary(port.env).path
    except Exception as exc:  # noqa: BLE001
        return _fail("herdr_binary_unavailable", type(exc).__name__)
    try:
        provider_executable_bindings = capture_provider_launch_bindings(
            agents=agents, env=port.env
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("agent_provider_binary_unavailable", type(exc).__name__)
    return PhaseExecutionResult(
        True,
        receipt={
            "workspace_paths": {
                workspace_id: by_workspace[workspace_id].canonical_path
                for workspace_id in sorted(wanted_workspaces)
            },
            "legacy_recovery_worktree_paths": recovery_paths,
            "agents": sorted(agents, key=lambda row: row["assigned_name"]),
            "close_authority": close_authority.receipt["close_authority"],
            "legacy_absence_authority": legacy_absence.receipt[
                "legacy_absence_authority"
            ],
            "restore_intent": restore_intent.as_payload(),
            "passive_pane_intent": pane_intent.as_payload(),
            "restore_container_intent": restore_container_intent.as_payload(),
            "target_cli": str(Path(target_cli).absolute()),
            "pipx": str(Path(pipx).resolve()),
            "herdr_binary": str(Path(herdr_binary).resolve()),
            "provider_executable_bindings": provider_executable_bindings,
        },
    )


__all__ = ("capture_private_bindings",)
