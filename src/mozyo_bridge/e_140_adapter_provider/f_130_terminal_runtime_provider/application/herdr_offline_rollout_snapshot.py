"""Read-only live snapshot adapter for ``herdr offline-rollout plan`` (#14838).

The adapter deliberately performs every authority read twice.  A plan is emitted only
when the registry, global Herdr inventory, worktree fingerprints, four store schemas and
supervisor pair are byte-equivalent before/after collection.  This is a bounded snapshot
barrier, not a process lock; any visible drift refuses rather than fabricating atomicity.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
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
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_STORE_ABSENT,
    GENERATION_STORE_HEALTHY,
    herdr_launch_generation_path,
    launch_generation_artifacts_secure,
    probe_launch_generation_store,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    LIFECYCLE_SCHEMA_ABSENT,
    LIFECYCLE_SCHEMA_RECOGNIZED,
    LaneLifecycleReader,
    probe_lane_lifecycle_schema,
)
from mozyo_bridge.core.state.lane_epoch_adoption import legacy_adoption_refusal
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer
from mozyo_bridge.core.state.lane_lifecycle_schema import lane_lifecycle_path
from mozyo_bridge.core.state.startup_store_migration import (
    startup_store_migration_plan_digest,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
    bind_lane_worktree,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_service_backend import (  # noqa: E501
    service_status as read_supervisor_status,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_plan import (  # noqa: E501
    AgentSnapshot,
    LegacyRecoveryAgentSnapshot,
    LegacyRecoverySnapshot,
    OfflineRolloutCapture,
    OfflineRolloutPlanResult,
    SCOPE_TARGET_PROJECT,
    SCOPE_UNRELATED_PROJECT,
    STORE_ABSENT,
    STORE_ATTESTATION,
    STORE_LANE_LIFECYCLE,
    STORE_LAUNCH_GENERATION,
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
REASON_INVENTORY_PROJECTION_INCOMPLETE = "inventory_projection_incomplete"
REASON_TOP_IDENTITY_UNRESOLVED = "top_identity_unresolved"
REASON_SNAPSHOT_DRIFT = "snapshot_drift"
REASON_LEGACY_RECOVERY_UNAVAILABLE = "legacy_recovery_unavailable"


def _legacy_recovery_snapshots(
    home: Path,
    pointers,
    records_reader,
    registry_records,
    worktree_reader,
    timeout: float,
    worktree_binder,
):
    if not pointers:
        return ()
    parsed = []
    for raw in pointers:
        if not isinstance(raw, str) or raw.count(":") != 1:
            return refused(REASON_LEGACY_RECOVERY_UNAVAILABLE, "decision_pointer_invalid")
        issue, journal = raw.split(":", 1)
        if any(
            not token.isascii()
            or not token.isdecimal()
            or int(token) < 1
            or str(int(token)) != token
            for token in (issue, journal)
        ):
            return refused(REASON_LEGACY_RECOVERY_UNAVAILABLE, "decision_pointer_invalid")
        parsed.append((issue, journal))
    if len({issue for issue, _journal in parsed}) != len(parsed):
        return refused(REASON_LEGACY_RECOVERY_UNAVAILABLE, "decision_issue_duplicate")
    try:
        rows = tuple(records_reader(home))
    except Exception as exc:  # noqa: BLE001 - unreadable authority is a typed refusal
        return refused(REASON_LEGACY_RECOVERY_UNAVAILABLE, type(exc).__name__)
    snapshots = []
    for issue, journal in parsed:
        matches = [row for row in rows if row.issue_id == issue]
        if len(matches) != 1:
            return refused(
                REASON_LEGACY_RECOVERY_UNAVAILABLE,
                f"issue_{issue}_row_count_{len(matches)}",
            )
        row = matches[0]
        decision = DecisionPointer("redmine", issue, journal)
        refusal = legacy_adoption_refusal(
            row,
            expected_revision=row.revision,
            issue_id=issue,
            decision=decision,
        )
        if refusal is not None:
            return refused(
                REASON_LEGACY_RECOVERY_UNAVAILABLE,
                f"issue_{issue}_{refusal.reason}",
            )
        registry = next(
            (
                record
                for record in registry_records
                if record.workspace_id == row.repo_workspace_id
            ),
            None,
        )
        bound = (
            worktree_binder(
                Path(registry.canonical_path),
                rows,
                workspace=row.repo_workspace_id,
                lane=row.lane_id,
                generation=row.lane_generation,
            )
            if registry is not None
            else None
        )
        if bound is None:
            return refused(
                REASON_LEGACY_RECOVERY_UNAVAILABLE,
                f"issue_{issue}_worktree_unresolved",
            )
        worktree_path, _branch_ref = bound
        fingerprint = worktree_reader(worktree_path, timeout)
        if not fingerprint.readable or not fingerprint.digest:
            return refused(
                REASON_LEGACY_RECOVERY_UNAVAILABLE,
                f"issue_{issue}_wip_unreadable",
            )
        agents = tuple(
            LegacyRecoveryAgentSnapshot(
                assigned_name=encode_assigned_name(
                    row.repo_workspace_id, provider, row.lane_id
                ),
                provider=provider,
            )
            for provider in ("claude", "codex")
        )
        snapshots.append(
            LegacyRecoverySnapshot(
                issue_id=issue,
                journal_id=journal,
                workspace_id=row.repo_workspace_id,
                lane_id=row.lane_id,
                lane_generation=row.lane_generation,
                expected_revision=row.revision,
                worktree_identity=row.worktree_identity,
                wip_readable=fingerprint.readable,
                dirty=fingerprint.dirty,
                untracked=fingerprint.untracked,
                wip_digest=fingerprint.digest,
                agents=agents,
            )
        )
    return tuple(snapshots)


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
                agent.terminal_id,
                agent.decode_reason,
            )
            for agent in view.agents
        )
    )
    return view, (view.raw_row_count, view.invalid_row_count, token)


def _inventory_projection_complete(view) -> bool:
    raw_count = view.raw_row_count
    invalid_count = view.invalid_row_count
    return (
        isinstance(raw_count, int)
        and not isinstance(raw_count, bool)
        and isinstance(invalid_count, int)
        and not isinstance(invalid_count, bool)
        and raw_count >= 0
        and invalid_count == 0
        and raw_count == len(view.agents)
        and all(
            type(agent.terminal_id) is str
            and agent.terminal_id
            and agent.terminal_id.strip() == agent.terminal_id
            for agent in view.agents
        )
        and len({agent.terminal_id for agent in view.agents}) == len(view.agents)
    )


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
            content_digest = _connection_content_digest(conn)
            migration_digest = startup_store_migration_plan_digest(conn)
    except (StartupTransactionError, sqlite3.DatabaseError, TypeError, ValueError):
        return StoreSnapshot(STORE_STARTUP_TRANSACTION, "unreadable", None)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in STARTUP_TRANSACTION_FENCE_SUPPORTED_VERSIONS
    ):
        return StoreSnapshot(STORE_STARTUP_TRANSACTION, "unsupported", None)
    return StoreSnapshot(
        STORE_STARTUP_TRANSACTION,
        STORE_RECOGNIZED,
        version,
        content_digest=content_digest,
        migration_plan_digest=migration_digest,
    )


def _launch_generation_store_snapshot(home: Path) -> StoreSnapshot:
    """Recognize exact v1 predecessor or current v2 without mutating either."""
    path = herdr_launch_generation_path(home)
    state, _detail = probe_launch_generation_store(path)
    if state == GENERATION_STORE_ABSENT:
        return StoreSnapshot(STORE_LAUNCH_GENERATION, STORE_ABSENT, None)
    if state == GENERATION_STORE_HEALTHY:
        return StoreSnapshot(
            STORE_LAUNCH_GENERATION, STORE_RECOGNIZED, 2,
            content_digest=_sqlite_content_digest(path),
        )
    expected_v1 = (
        "assigned_name", "startup_action_id", "phase", "workspace_id", "role",
        "lane_id", "locator", "verdict", "observed_at", "reserved_at", "attested_at",
    )
    try:
        if not _launch_generation_v1_artifact_valid(path):
            return StoreSnapshot(STORE_LAUNCH_GENERATION, "unsupported", None)
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return StoreSnapshot(STORE_LAUNCH_GENERATION, "unsupported", None)
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            info = tuple(
                (row[1], str(row[2]).upper(), row[3], row[5])
                for row in conn.execute("PRAGMA table_info(herdr_launch_generations)")
            )
            tables = tuple(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ))
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return StoreSnapshot(STORE_LAUNCH_GENERATION, "unreadable", None)
    if (
        version != 1
        or tuple(row[0] for row in info) != expected_v1
        or any(row[1:] != ("TEXT", 1, 1 if index == 0 else 0)
               for index, row in enumerate(info))
        or tables != ("herdr_launch_generations",)
    ):
        return StoreSnapshot(STORE_LAUNCH_GENERATION, "unsupported", None)
    return StoreSnapshot(
        STORE_LAUNCH_GENERATION, STORE_RECOGNIZED, 1, upgrade_required=True,
        content_digest=_sqlite_content_digest(path),
    )


def _launch_generation_v1_artifact_valid(path: Path) -> bool:
    """Exact legacy v1 shape/security gate used by plan and destructive backup authority."""
    expected = (
        "assigned_name", "startup_action_id", "phase", "workspace_id", "role",
        "lane_id", "locator", "verdict", "observed_at", "reserved_at", "attested_at",
    )
    try:
        if not launch_generation_artifacts_secure(path):
            return False
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            info = tuple(
                (row[1], str(row[2]).upper(), row[3], row[5])
                for row in conn.execute("PRAGMA table_info(herdr_launch_generations)")
            )
            tables = tuple(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ))
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return False
    return bool(
        version == 1
        and tuple(row[0] for row in info) == expected
        and all(row[1:] == ("TEXT", 1, 1 if index == 0 else 0)
                for index, row in enumerate(info))
        and tables == ("herdr_launch_generations",)
    )


def _connection_content_digest(conn: sqlite3.Connection) -> str:
    """Hash one logical SQLite read snapshot, including schema and committed rows."""
    digest = hashlib.sha256()
    for statement in conn.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_content_digest(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")
            return _connection_content_digest(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError, UnicodeError):
        return ""


def _store_snapshots(home: Path) -> tuple[StoreSnapshot, ...]:
    attestation_path = herdr_identity_attestation_path(home)
    lifecycle_path = lane_lifecycle_path(home)
    attestation = probe_store_schema(attestation_path)
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
            content_digest=(
                _sqlite_content_digest(attestation_path)
                if attestation.state == ATTESTATION_RECOGNIZED
                else ""
            ),
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
            content_digest=(
                _sqlite_content_digest(lifecycle_path)
                if lifecycle.state == LIFECYCLE_SCHEMA_RECOGNIZED
                else ""
            ),
        ),
        _launch_generation_store_snapshot(home),
        _startup_store_snapshot(home),
    )


def _supervisor_snapshots(home: Path, reader) -> tuple[SupervisorAgentSnapshot, ...]:
    # The platform-resolving backend, not a host adapter: it normalizes whichever OS scheduler owns
    # this host into the same `agents` roster (#15192 retired the launchd-only `*_pair` verbs).
    projection = reader(mozyo_home=home)
    backend = str(projection.get("backend") or "")
    snapshots = []
    for row in projection.get("agents", ()):  # public projection is already secret-safe
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
                backend=backend,
                legacy_drain=(
                    str(row.get("legacy_drain") or "")
                    if backend == "launchd"
                    else "not_applicable"
                ),
            )
        )
    return tuple(snapshots)


def capture_offline_rollout_snapshot(
    *,
    repo_root: Path,
    home: Path,
    candidate_version: str,
    candidate_source_sha: str = "",
    candidate_source_ref: str = "",
    candidate_workflow_run_id: str = "",
    candidate_wheel_sha256: str = "",
    candidate_sdist_sha256: str = "",
    legacy_recovery_pointers: tuple[str, ...] = (),
    env: Optional[Mapping[str, str]] = None,
    timeout: float = 10.0,
    inventory_reader: Callable = read_herdr_inventory,
    registry_health_reader: Callable = inspect_registry_health,
    workspace_reader: Callable = list_workspaces,
    worktree_reader: Callable = read_live_worktree_fingerprint,
    store_reader: Callable = _store_snapshots,
    supervisor_reader: Callable = read_supervisor_status,
    lifecycle_records_reader: Optional[Callable] = None,
    lane_worktree_binder: Callable = bind_lane_worktree,
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
    if not _inventory_projection_complete(view):
        return refused(
            REASON_INVENTORY_PROJECTION_INCOMPLETE,
            "raw_projection_join_not_lossless",
        )

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

    if lifecycle_records_reader is None:
        lifecycle_records_reader = lambda selected_home: LaneLifecycleReader(
            home=selected_home
        ).records()
    recoveries = _legacy_recovery_snapshots(
        home,
        legacy_recovery_pointers,
        lifecycle_records_reader,
        records,
        worktree_reader,
        timeout,
        lane_worktree_binder,
    )
    if isinstance(recoveries, OfflineRolloutPlanResult):
        return recoveries

    wip, wip_token = _worktree_snapshots(records, worktree_reader, timeout)
    stores = tuple(store_reader(home))
    supervisors = _supervisor_snapshots(home, supervisor_reader)

    # Re-read every mutable input.  A plan represents neither the first nor the second
    # instant unless both agree exactly.
    registry_after = _registry_snapshot(home, registry_health_reader, workspace_reader)
    inventory_after = _inventory_snapshot(repo_root, source_env, inventory_reader)
    if registry_after is None or inventory_after is None:
        return refused(REASON_SNAPSHOT_DRIFT, "authority_became_unreadable")
    if not _inventory_projection_complete(inventory_after[0]):
        return refused(
            REASON_INVENTORY_PROJECTION_INCOMPLETE,
            "raw_projection_join_not_lossless",
        )
    wip_after, wip_after_token = _worktree_snapshots(
        registry_after[0], worktree_reader, timeout
    )
    stores_after = tuple(store_reader(home))
    supervisors_after = _supervisor_snapshots(home, supervisor_reader)
    recoveries_after = _legacy_recovery_snapshots(
        home,
        legacy_recovery_pointers,
        lifecycle_records_reader,
        registry_after[0],
        worktree_reader,
        timeout,
        lane_worktree_binder,
    )
    if isinstance(recoveries_after, OfflineRolloutPlanResult):
        return refused(REASON_SNAPSHOT_DRIFT, "legacy_recovery_became_unreadable")
    if (
        registry_token != registry_after[1]
        or inventory_token != inventory_after[1]
        or wip_token != wip_after_token
        or stores != stores_after
        or supervisors != supervisors_after
        or recoveries != recoveries_after
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
        candidate_source_ref=candidate_source_ref,
        candidate_workflow_run_id=candidate_workflow_run_id,
        candidate_wheel_sha256=candidate_wheel_sha256,
        candidate_sdist_sha256=candidate_sdist_sha256,
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
        legacy_recoveries=recoveries,
    )


__all__ = ("capture_offline_rollout_snapshot",)
