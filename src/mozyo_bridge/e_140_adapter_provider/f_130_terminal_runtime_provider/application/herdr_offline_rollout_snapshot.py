"""Read-only live snapshot adapter for ``herdr offline-rollout plan`` (#14838).

The adapter deliberately performs every authority read twice.  A plan is emitted only
when the registry, global Herdr inventory, worktree fingerprints, three store schemas and
supervisor pair are byte-equivalent before/after collection.  This is a bounded snapshot
barrier, not a process lock; any visible drift refuses rather than fabricating atomicity.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping, Optional

from mozyo_bridge.core.state.herdr_identity_attestation import (
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    STORE_ABSENT as ATTESTATION_ABSENT,
    STORE_RECOGNIZED as ATTESTATION_RECOGNIZED,
    probe_store_schema,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    LIFECYCLE_SCHEMA_ABSENT,
    LIFECYCLE_SCHEMA_RECOGNIZED,
    probe_lane_lifecycle_schema,
)
from mozyo_bridge.core.state.startup_action_capability import (
    STARTUP_TRANSACTION_FENCE_SUPPORTED_VERSIONS,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    STORE_ABSENT as STARTUP_ABSENT,
    STORE_PRESENT as STARTUP_PRESENT,
    StartupTransactionError,
    StartupTransactionFence,
)
from mozyo_bridge.core.state.workspace_registry import (
    REGISTRY_HEALTH_OK,
    inspect_registry_health,
    list_workspaces,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    read_live_worktree_fingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd import (  # noqa: E501
    service_status_pair,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_plan import (  # noqa: E501
    AgentSnapshot,
    OfflineRolloutCapture,
    OfflineRolloutPlanResult,
    SCOPE_TARGET_PROJECT,
    SCOPE_UNRELATED_PROJECT,
    STORE_ABSENT,
    STORE_ATTESTATION,
    STORE_LANE_LIFECYCLE,
    STORE_RECOGNIZED,
    STORE_STARTUP_TRANSACTION,
    StoreSnapshot,
    SupervisorAgentSnapshot,
    TopIdentitySnapshot,
    WorkspaceSnapshot,
    refused,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    read_herdr_inventory,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)


REASON_REGISTRY_UNREADABLE = "workspace_registry_unreadable"
REASON_INVENTORY_UNREADABLE = "inventory_unreadable"
REASON_TOP_IDENTITY_UNRESOLVED = "top_identity_unresolved"
REASON_SNAPSHOT_DRIFT = "snapshot_drift"


def _registry_snapshot(home: Path, health_reader, workspace_reader):
    health = health_reader(home)
    if health.get("status") != REGISTRY_HEALTH_OK:
        return None
    records = tuple(workspace_reader(home=home))
    token = tuple(
        sorted(
            (
                record.workspace_id,
                record.canonical_path,
                record.project_name,
                record.updated_at,
            )
            for record in records
        )
    )
    return records, token


def _inventory_snapshot(repo_root: Path, env: Mapping[str, str], inventory_reader):
    view = inventory_reader(repo_root, env=env)
    if not view.backend_selected or not view.ok:
        return None
    token = tuple(
        sorted(
            (
                agent.name,
                agent.managed,
                agent.workspace_id,
                agent.lane_id,
                agent.role,
                agent.runtime_state,
                agent.raw_status,
                agent.locator,
                agent.decode_reason,
            )
            for agent in view.agents
        )
    )
    return view, token


def _worktree_snapshots(records, reader, timeout: float):
    captured = {}
    for record in records:
        fingerprint = reader(Path(record.canonical_path), timeout)
        captured[record.workspace_id] = fingerprint
    token = tuple(
        sorted(
            (
                workspace_id,
                fingerprint.readable,
                fingerprint.dirty,
                fingerprint.untracked,
                fingerprint.digest,
                fingerprint.mutation_in_flight,
                fingerprint.pending_composer,
            )
            for workspace_id, fingerprint in captured.items()
        )
    )
    return captured, token


def _startup_store_snapshot(home: Path) -> StoreSnapshot:
    """Use the fence's one verified read funnel; never create or migrate the store."""
    fence = StartupTransactionFence(home=home)
    try:
        shape = fence.store_shape()
    except StartupTransactionError:
        return StoreSnapshot(STORE_STARTUP_TRANSACTION, "unreadable", None)
    if shape.state == STARTUP_ABSENT:
        return StoreSnapshot(STORE_STARTUP_TRANSACTION, STORE_ABSENT, None)
    if shape.state != STARTUP_PRESENT:
        return StoreSnapshot(STORE_STARTUP_TRANSACTION, "damaged", None)
    try:
        # `_connection("ro")` is the fence's single exact verifier: supported version,
        # tables/columns, v2 manifest shape and seal/DB identity are checked in one open
        # snapshot.  A raw second connection would weaken that authority check.
        with fence._connection("ro") as conn:  # noqa: SLF001 - authority-owned verifier
            version = conn.execute("PRAGMA user_version").fetchone()[0]
    except (StartupTransactionError, TypeError, ValueError):
        return StoreSnapshot(STORE_STARTUP_TRANSACTION, "unreadable", None)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in STARTUP_TRANSACTION_FENCE_SUPPORTED_VERSIONS
    ):
        return StoreSnapshot(STORE_STARTUP_TRANSACTION, "unsupported", None)
    return StoreSnapshot(STORE_STARTUP_TRANSACTION, STORE_RECOGNIZED, version)


def _store_snapshots(home: Path) -> tuple[StoreSnapshot, ...]:
    attestation = probe_store_schema(herdr_identity_attestation_path(home))
    lifecycle = probe_lane_lifecycle_schema(home=home)
    return (
        StoreSnapshot(
            STORE_ATTESTATION,
            (
                STORE_ABSENT
                if attestation.state == ATTESTATION_ABSENT
                else (
                    STORE_RECOGNIZED
                    if attestation.state == ATTESTATION_RECOGNIZED
                    else attestation.state
                )
            ),
            attestation.version,
            attestation.upgrade_required,
        ),
        StoreSnapshot(
            STORE_LANE_LIFECYCLE,
            (
                STORE_ABSENT
                if lifecycle.state == LIFECYCLE_SCHEMA_ABSENT
                else (
                    STORE_RECOGNIZED
                    if lifecycle.state == LIFECYCLE_SCHEMA_RECOGNIZED
                    else lifecycle.state
                )
            ),
            lifecycle.version,
            lifecycle.upgrade_required,
        ),
        _startup_store_snapshot(home),
    )


def _supervisor_snapshots(home: Path, reader) -> tuple[SupervisorAgentSnapshot, ...]:
    pair = reader(mozyo_home=home)
    snapshots = []
    for row in pair.get("agents", ()):  # public projection is already secret-safe
        pid = row.get("pid")
        snapshots.append(
            SupervisorAgentSnapshot(
                label=str(row.get("label") or ""),
                installed=bool(row.get("installed")),
                loaded=bool(row.get("loaded")),
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                home_pin=str(row.get("home_pin") or ""),
                executable_matches=bool(row.get("executable_matches")),
                credential_readiness=str(row.get("credential_readiness") or ""),
            )
        )
    return tuple(snapshots)


def capture_offline_rollout_snapshot(
    *,
    repo_root: Path,
    home: Path,
    candidate_version: str,
    candidate_source_sha: str = "",
    env: Optional[Mapping[str, str]] = None,
    timeout: float = 10.0,
    inventory_reader: Callable = read_herdr_inventory,
    registry_health_reader: Callable = inspect_registry_health,
    workspace_reader: Callable = list_workspaces,
    worktree_reader: Callable = read_live_worktree_fingerprint,
    store_reader: Callable = _store_snapshots,
    supervisor_reader: Callable = service_status_pair,
) -> OfflineRolloutCapture | OfflineRolloutPlanResult:
    """Capture one stable, path-redacted host view or return a typed refusal."""
    source_env = dict(os.environ if env is None else env)

    registry_before = _registry_snapshot(home, registry_health_reader, workspace_reader)
    if registry_before is None:
        return refused(REASON_REGISTRY_UNREADABLE, "registry_not_healthy")
    records, registry_token = registry_before

    inventory_before = _inventory_snapshot(repo_root, source_env, inventory_reader)
    if inventory_before is None:
        return refused(REASON_INVENTORY_UNREADABLE, "global_inventory_not_readable")
    view, inventory_token = inventory_before

    top_workspace = source_env.get("MOZYO_WORKSPACE_ID", "")
    top_provider = source_env.get("MOZYO_AGENT_ROLE", "")
    top_lane = source_env.get("MOZYO_LANE_ID", "")
    try:
        top_name = encode_assigned_name(top_workspace, top_provider, top_lane)
    except (TypeError, ValueError):
        return refused(REASON_TOP_IDENTITY_UNRESOLVED, "identity_env_invalid")

    current = next(
        (record for record in records if record.workspace_id == view.workspace_segment),
        None,
    )
    if current is None:
        return refused(REASON_REGISTRY_UNREADABLE, "current_workspace_not_registered")

    wip, wip_token = _worktree_snapshots(records, worktree_reader, timeout)
    stores = tuple(store_reader(home))
    supervisors = _supervisor_snapshots(home, supervisor_reader)

    # Re-read every mutable input.  A plan represents neither the first nor the second
    # instant unless both agree exactly.
    registry_after = _registry_snapshot(home, registry_health_reader, workspace_reader)
    inventory_after = _inventory_snapshot(repo_root, source_env, inventory_reader)
    if registry_after is None or inventory_after is None:
        return refused(REASON_SNAPSHOT_DRIFT, "authority_became_unreadable")
    wip_after, wip_after_token = _worktree_snapshots(
        registry_after[0], worktree_reader, timeout
    )
    stores_after = tuple(store_reader(home))
    supervisors_after = _supervisor_snapshots(home, supervisor_reader)
    if (
        registry_token != registry_after[1]
        or inventory_token != inventory_after[1]
        or wip_token != wip_after_token
        or stores != stores_after
        or supervisors != supervisors_after
    ):
        return refused(REASON_SNAPSHOT_DRIFT, "snapshot_changed_during_capture")
    del wip_after  # proof-only second read; the first stable view is the plan input.

    agents = tuple(
        AgentSnapshot(
            assigned_name=agent.name,
            workspace_id=agent.workspace_id,
            lane_id=agent.lane_id,
            provider=agent.role,
            runtime_state=agent.runtime_state,
            raw_status=agent.raw_status,
        )
        for agent in view.managed_agents
    )
    assigned_by_workspace = {}
    for agent in agents:
        assigned_by_workspace.setdefault(agent.workspace_id, []).append(agent.assigned_name)

    workspaces = tuple(
        WorkspaceSnapshot(
            workspace_id=record.workspace_id,
            project_name=record.project_name,
            scope=(
                SCOPE_TARGET_PROJECT
                if record.project_name == current.project_name
                else SCOPE_UNRELATED_PROJECT
            ),
            assigned_names=tuple(sorted(assigned_by_workspace.get(record.workspace_id, ()))),
            wip_readable=wip[record.workspace_id].readable,
            dirty=wip[record.workspace_id].dirty,
            untracked=wip[record.workspace_id].untracked,
            wip_digest=wip[record.workspace_id].digest,
        )
        for record in records
    )
    unmanaged = tuple(sorted(agent.name for agent in view.unmanaged_agents))
    return OfflineRolloutCapture(
        current_workspace_id=view.workspace_segment,
        current_project_name=current.project_name,
        candidate_version=candidate_version,
        candidate_source_sha=candidate_source_sha,
        workspaces=workspaces,
        agents=agents,
        unmanaged_assigned_names=unmanaged,
        top_identity=TopIdentitySnapshot(
            workspace_id=top_workspace,
            lane_id=top_lane,
            provider=top_provider,
            assigned_name=top_name,
        ),
        stores=stores,
        supervisors=supervisors,
    )


__all__ = ("capture_offline_rollout_snapshot",)
