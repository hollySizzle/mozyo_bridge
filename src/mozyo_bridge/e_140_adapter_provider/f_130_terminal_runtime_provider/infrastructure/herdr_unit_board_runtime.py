"""Live Herdr adapter for the public-safe Unit board read model.

The adapter reads the live Herdr inventory, restores mozyo's durable logical
agent identities, joins only reviewable workspace/role/lane display metadata,
and can refresh Herdr's display-only pane metadata.  It never writes agent
input, Redmine, workflow state, or a mozyo state database.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ContextManager, Optional

from mozyo_bridge.core.state.lane_metadata import LaneMetadataRecord, LaneMetadataStore
from mozyo_bridge.core.state.workspace_registry import WorkspaceRecord, load_workspace_by_id
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (
    load_parsed_role_bindings,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    ParsedRoleBindings,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_INVALID,
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    AgentObservation,
    SOURCE_RELOAD_REQUIRED,
    UnitBoardSnapshot,
    build_unit_board,
    lane_work_label,
    metadata_for_unit,
    safe_text,
    unavailable_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    SCHEME_PREFIX,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    HerdrCliAgentLister,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    agent_row_runtime_state,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    Runner,
    resolve_herdr_binary,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


METADATA_SOURCE = "mozyo.unit-board"
METADATA_TIMEOUT_SECONDS = 10.0
METADATA_SYNC_LOCK_FILENAME = ".herdr-unit-board-metadata.lock"
MAX_METADATA_RECONCILE_PASSES = 3
METADATA_TOKEN_KEYS = (
    "mozyo_identity",
    "mozyo_project",
    "mozyo_responsibility",
    "mozyo_role",
    "mozyo_unit",
    "mozyo_work",
)

WorkspaceLoader = Callable[[str], Optional[WorkspaceRecord]]
RoleLoader = Callable[[Path], ParsedRoleBindings]
LaneRecordsLoader = Callable[[], Mapping[str, LaneMetadataRecord]]
PaneRowsLoader = Callable[[], Sequence[Mapping[str, object]]]
SyncLockFactory = Callable[[], ContextManager[None]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_workspace_loader(workspace_id: str) -> Optional[WorkspaceRecord]:
    return load_workspace_by_id(workspace_id)


def _default_lane_records_loader() -> Mapping[str, LaneMetadataRecord]:
    return LaneMetadataStore().load_all(include_retired=False)


class MetadataSyncLockError(RuntimeError):
    """The cross-process metadata writer lock could not be honored."""

    def __init__(self, phase: str) -> None:
        super().__init__(f"metadata sync lock {phase} failed")
        self.phase = phase


@contextmanager
def unit_board_metadata_lock(home: Optional[Path] = None):
    """Serialize every Unit-board inventory-to-metadata transaction.

    Herdr starts plugin hooks asynchronously.  A process-local mutex therefore
    cannot prevent two hooks from observing different pane generations and then
    applying their reports in the opposite order.  This operator-home advisory
    lock covers the complete observe -> report -> re-observe transaction across
    processes.  A crashed holder is released by the kernel.

    The lock file is an empty, private synchronization artifact; it contains no
    identity, path, credential, workflow, or presentation state.
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - supported plugin OSes are POSIX
        raise MetadataSyncLockError("acquire") from exc

    fd: Optional[int] = None
    try:
        base = Path(home) if home is not None else mozyo_bridge_home()
        base.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(base / METADATA_SYNC_LOCK_FILENAME, flags, 0o600)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
        ):
            raise OSError("unsafe metadata lock file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except (OSError, RuntimeError, ValueError) as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise MetadataSyncLockError("acquire") from exc

    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        release_error: Optional[OSError] = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:  # pragma: no cover - kernel/filesystem fault
            release_error = exc
        try:
            os.close(fd)
        except OSError as exc:  # pragma: no cover - kernel/filesystem fault
            release_error = release_error or exc
        if release_error is not None and not body_failed:
            raise MetadataSyncLockError("release") from release_error


@dataclass(frozen=True)
class MetadataSyncFailure:
    unit_id: str
    provider: str
    reason: str

    def as_payload(self) -> dict[str, str]:
        return {
            "unit_id": safe_text(self.unit_id),
            "provider": safe_text(self.provider),
            "reason": safe_text(self.reason),
        }


@dataclass(frozen=True)
class MetadataSyncReport:
    source_state: str
    attempted: int
    updated: int
    failures: tuple[MetadataSyncFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.source_state == "live" and not self.failures and self.updated == self.attempted

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "source_state": self.source_state,
            "attempted": self.attempted,
            "updated": self.updated,
            "failures": [failure.as_payload() for failure in self.failures],
        }


@dataclass(frozen=True)
class _ObservedBoard:
    snapshot: UnitBoardSnapshot
    unmanaged_panes: tuple[str, ...]
    metadata_authority: tuple[tuple[str, ...], ...]


class HerdrUnitBoardRuntime:
    """Read/sync adapter with injectable IO seams for deterministic tests."""

    def __init__(
        self,
        binary: str,
        *,
        lister: Optional[HerdrCliAgentLister] = None,
        runner: Optional[Runner] = None,
        workspace_loader: WorkspaceLoader = _default_workspace_loader,
        role_loader: RoleLoader = load_parsed_role_bindings,
        lane_records_loader: LaneRecordsLoader = _default_lane_records_loader,
        pane_rows_loader: Optional[PaneRowsLoader] = None,
        sync_lock_factory: SyncLockFactory = unit_board_metadata_lock,
    ) -> None:
        if not isinstance(binary, str) or not binary:
            raise ValueError("Herdr Unit board binary must be a non-empty string")
        self._binary = binary
        self._runner: Runner = runner if runner is not None else subprocess.run
        self._lister = lister or HerdrCliAgentLister(binary, runner=self._runner)
        self._workspace_loader = workspace_loader
        self._role_loader = role_loader
        self._lane_records_loader = lane_records_loader
        self._pane_rows_loader = pane_rows_loader or self._list_live_pane_rows
        self._sync_lock_factory = sync_lock_factory

    def _list_live_pane_rows(self) -> Sequence[Mapping[str, object]]:
        """Read Herdr's complete pane inventory, including metadata tokens."""
        try:
            completed = self._runner(
                [self._binary, "pane", "list"],
                capture_output=True,
                text=True,
                timeout=METADATA_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TerminalTransportError("Herdr pane inventory is unavailable") from exc
        if completed.returncode != 0 or not isinstance(completed.stdout, str):
            raise TerminalTransportError("Herdr pane inventory is unavailable")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise TerminalTransportError("Herdr pane inventory is invalid") from exc
        result = payload.get("result") if isinstance(payload, Mapping) else None
        rows = result.get("panes") if isinstance(result, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("type") != "pane_list"
            or not isinstance(rows, list)
            or any(not isinstance(row, Mapping) for row in rows)
        ):
            raise TerminalTransportError("Herdr pane inventory is invalid")
        return tuple(rows)

    @staticmethod
    def _metadata_owner_panes(
        rows: Sequence[Mapping[str, object]],
    ) -> frozenset[str]:
        """Return panes carrying this plugin's namespaced metadata tokens."""
        pane_ids: set[str] = set()
        owners: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("pane inventory contains a non-object row")
            pane_id = row.get("pane_id")
            tokens = row.get("tokens", {})
            if (
                not valid_target(pane_id)
                or pane_id in pane_ids
                or not isinstance(tokens, Mapping)
                or any(not isinstance(key, str) for key in tokens)
            ):
                raise ValueError("pane inventory contains an invalid identity")
            pane_ids.add(pane_id)
            if any(key in tokens for key in METADATA_TOKEN_KEYS):
                owners.add(pane_id)
        return frozenset(owners)

    @staticmethod
    def _role_display(
        parsed: ParsedRoleBindings, lane_id: str, project_label: str
    ) -> tuple[str, str, str]:
        if not parsed.ok:
            return "unknown", project_label, AUTHORITY_INVALID
        matches = [binding for binding in parsed.bindings if binding.lane_id == lane_id]
        if not matches:
            return "unknown", project_label, AUTHORITY_MISSING
        if len(matches) != 1:
            return "unknown", project_label, AUTHORITY_INVALID
        binding = matches[0]
        return (
            safe_text(binding.role),
            safe_text(binding.project_scope, fallback=project_label),
            AUTHORITY_RESOLVED,
        )

    @staticmethod
    def _lane_display(
        records: Sequence[LaneMetadataRecord], workspace_id: str, lane_id: str
    ) -> str:
        if lane_id == "default":
            return lane_work_label(lane_id)
        matches = [
            record
            for record in records
            if record.repo_workspace_id == workspace_id and record.lane_id == lane_id
        ]
        if len(matches) != 1:
            return lane_work_label(lane_id)
        record = matches[0]
        return lane_work_label(lane_id, record.issue_id, record.lane_label)

    def _observe(self) -> _ObservedBoard:
        observed_at = _utc_now()
        try:
            rows = self._lister.list_agent_rows()
            metadata_owner_panes = self._metadata_owner_panes(
                self._pane_rows_loader()
            )
        except (TerminalTransportError, OSError, ValueError):
            return _ObservedBoard(
                snapshot=unavailable_snapshot(
                    "unavailable",
                    observed_at=observed_at,
                    detail="live Herdr agent inventory is unavailable",
                ),
                unmanaged_panes=(),
                metadata_authority=(),
            )
        try:
            lane_records = tuple(self._lane_records_loader().values())
        except (OSError, ValueError, TypeError):
            lane_records = ()

        observations: list[AgentObservation] = []
        unmanaged = 0
        unmanaged_panes: set[str] = set()
        managed_panes: set[str] = set()
        observed_panes: set[str] = set()
        metadata_authority: list[tuple[str, ...]] = []
        display_by_unit: dict[tuple[str, str], tuple[str, str, str, str]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                unmanaged += 1
                continue
            name = row.get("name")
            pane_id = row.get("pane_id")
            if valid_target(pane_id):
                if pane_id in observed_panes:
                    return _ObservedBoard(
                        snapshot=unavailable_snapshot(
                            SOURCE_RELOAD_REQUIRED,
                            observed_at=observed_at,
                            detail=(
                                "live Herdr inventory assigns one pane to multiple "
                                "agent rows; reload required"
                            ),
                        ),
                        unmanaged_panes=(),
                        metadata_authority=(),
                    )
                observed_panes.add(pane_id)
            decoded = decode_assigned_name(name)
            if not decoded.ok or decoded.identity is None:
                if isinstance(name, str) and name.startswith(f"{SCHEME_PREFIX}_"):
                    return _ObservedBoard(
                        snapshot=unavailable_snapshot(
                            SOURCE_RELOAD_REQUIRED,
                            observed_at=observed_at,
                            detail="managed Herdr identity is malformed; reload required",
                        ),
                        unmanaged_panes=(),
                        metadata_authority=(),
                    )
                unmanaged += 1
                if valid_target(pane_id):
                    unmanaged_panes.add(pane_id)
                continue
            identity = decoded.identity
            if not valid_target(pane_id):
                return _ObservedBoard(
                    snapshot=unavailable_snapshot(
                        SOURCE_RELOAD_REQUIRED,
                        observed_at=observed_at,
                        detail="managed Herdr agent has no valid live pane locator; reload required",
                    ),
                    unmanaged_panes=(),
                    metadata_authority=(),
                )
            managed_panes.add(pane_id)
            unit_key = (identity.workspace_id, identity.lane_id)
            display = display_by_unit.get(unit_key)
            if display is None:
                record = self._workspace_loader(identity.workspace_id)
                if record is None:
                    display = (
                        "unknown-project",
                        "unknown",
                        "unknown-project",
                        AUTHORITY_MISSING,
                    )
                else:
                    project_label = safe_text(
                        record.project_name, fallback="unknown-project"
                    )
                    try:
                        parsed = self._role_loader(Path(record.canonical_path))
                    except (OSError, ValueError, TypeError):
                        parsed = ParsedRoleBindings.invalid(
                            "workflow role authority could not be read"
                        )
                    role, responsibility, authority = self._role_display(
                        parsed, identity.lane_id, project_label
                    )
                    display = (project_label, role, responsibility, authority)
                display_by_unit[unit_key] = display
            project_label, role, responsibility, authority = display
            work_label = self._lane_display(
                lane_records, identity.workspace_id, identity.lane_id
            )
            metadata_authority.append(
                (
                    pane_id,
                    "managed",
                    identity.workspace_id,
                    identity.lane_id,
                    identity.role,
                    project_label,
                    role,
                    responsibility,
                    work_label,
                    authority,
                )
            )
            observations.append(
                AgentObservation(
                    workspace_id=identity.workspace_id,
                    lane_id=identity.lane_id,
                    provider=identity.role,
                    pane_id=pane_id,
                    runtime_state=agent_row_runtime_state(row),
                    interactive_ready=bool(row.get("interactive_ready", False)),
                    project_label=project_label,
                    workflow_role=role,
                    responsibility=responsibility,
                    work_label=work_label,
                    authority_state=authority,
                )
            )
        clear_panes = unmanaged_panes | (metadata_owner_panes - managed_panes)
        metadata_authority.extend(
            (pane_id, "clear") for pane_id in sorted(clear_panes)
        )
        return _ObservedBoard(
            snapshot=build_unit_board(
                observations,
                observed_at=observed_at,
                unmanaged_agents=unmanaged,
            ),
            unmanaged_panes=tuple(sorted(clear_panes - managed_panes)),
            metadata_authority=tuple(sorted(metadata_authority)),
        )

    def snapshot(self) -> UnitBoardSnapshot:
        return self._observe().snapshot

    @staticmethod
    def _inventory_failure(observed: _ObservedBoard) -> MetadataSyncReport:
        snapshot = observed.snapshot
        return MetadataSyncReport(
            source_state=snapshot.source_state,
            attempted=0,
            updated=0,
            failures=(
                MetadataSyncFailure(
                    unit_id="board",
                    provider="unknown",
                    reason=snapshot.detail or "inventory unavailable",
                ),
            ),
        )

    def _apply_metadata(self, observed: _ObservedBoard) -> MetadataSyncReport:
        snapshot = observed.snapshot
        if not snapshot.ok:
            return self._inventory_failure(observed)
        attempted = 0
        updated = 0
        failures: list[MetadataSyncFailure] = []
        for unit in snapshot.units:
            tokens, title = metadata_for_unit(unit)
            for agent in unit.agents:
                attempted += 1
                argv = [
                    self._binary,
                    "pane",
                    "report-metadata",
                    agent.pane_id,
                    "--source",
                    METADATA_SOURCE,
                    "--agent",
                    agent.provider,
                    "--title",
                    title,
                    "--display-agent",
                    safe_text(f"{agent.provider} · {unit.workflow_role}"),
                ]
                for key, value in sorted(tokens.items()):
                    argv.extend(("--token", f"{key}={value}"))
                try:
                    completed = self._runner(
                        argv,
                        capture_output=True,
                        text=True,
                        timeout=METADATA_TIMEOUT_SECONDS,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    completed = None
                if completed is not None and completed.returncode == 0:
                    updated += 1
                else:
                    failures.append(
                        MetadataSyncFailure(
                            unit_id=unit.unit_id,
                            provider=agent.provider,
                            reason="metadata_update_failed",
                        )
                    )
        for pane_id in observed.unmanaged_panes:
            attempted += 1
            argv = [
                self._binary,
                "pane",
                "report-metadata",
                pane_id,
                "--source",
                METADATA_SOURCE,
                "--clear-title",
                "--clear-display-agent",
            ]
            for key in METADATA_TOKEN_KEYS:
                argv.extend(("--clear-token", key))
            try:
                completed = self._runner(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=METADATA_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode == 0:
                updated += 1
            else:
                failures.append(
                    MetadataSyncFailure(
                        unit_id="unmanaged",
                        provider="unknown",
                        reason="metadata_clear_failed",
                    )
                )
        return MetadataSyncReport(
            source_state=snapshot.source_state,
            attempted=attempted,
            updated=updated,
            failures=tuple(failures),
        )

    def _sync_metadata_locked(self) -> MetadataSyncReport:
        """Reconcile a stable inventory while one cross-process writer owns it."""
        observed = self._observe()
        attempted = 0
        updated = 0
        failures: list[MetadataSyncFailure] = []
        for _ in range(MAX_METADATA_RECONCILE_PASSES):
            if not observed.snapshot.ok:
                inventory_failure = self._inventory_failure(observed)
                return MetadataSyncReport(
                    source_state=inventory_failure.source_state,
                    attempted=attempted,
                    updated=updated,
                    failures=tuple(failures) + inventory_failure.failures,
                )
            # Re-read immediately before mutation.  The cross-process lock orders
            # plugin writers, but it cannot stop Herdr from releasing one agent and
            # starting another on the same pane.  If that happened since the first
            # observation, do not clear or overwrite either generation; retry from
            # the newer authority instead.  The post-write observation below still
            # closes the unavoidable race after this check.
            preflight = self._observe()
            if not preflight.snapshot.ok:
                inventory_failure = self._inventory_failure(preflight)
                return MetadataSyncReport(
                    source_state=inventory_failure.source_state,
                    attempted=attempted,
                    updated=updated,
                    failures=tuple(failures) + inventory_failure.failures,
                )
            if preflight.metadata_authority != observed.metadata_authority:
                observed = preflight
                continue
            applied = self._apply_metadata(observed)
            attempted += applied.attempted
            updated += applied.updated
            failures.extend(applied.failures)
            if applied.failures:
                return MetadataSyncReport(
                    source_state=applied.source_state,
                    attempted=attempted,
                    updated=updated,
                    failures=tuple(failures),
                )

            verified = self._observe()
            if not verified.snapshot.ok:
                inventory_failure = self._inventory_failure(verified)
                return MetadataSyncReport(
                    source_state=inventory_failure.source_state,
                    attempted=attempted,
                    updated=updated,
                    failures=tuple(failures) + inventory_failure.failures,
                )
            if verified.metadata_authority == observed.metadata_authority:
                return MetadataSyncReport(
                    source_state=verified.snapshot.source_state,
                    attempted=attempted,
                    updated=updated,
                    failures=tuple(failures),
                )
            observed = verified

        return MetadataSyncReport(
            source_state=SOURCE_RELOAD_REQUIRED,
            attempted=attempted,
            updated=updated,
            failures=tuple(failures)
            + (
                MetadataSyncFailure(
                    unit_id="board",
                    provider="unknown",
                    reason="inventory_changed_during_sync",
                ),
            ),
        )

    def sync_metadata(self) -> MetadataSyncReport:
        """Synchronize metadata without out-of-order cross-process writers.

        No Herdr ``--seq`` is sent.  The advisory lock gives reports one total
        order, so wall-clock rollback or equal timestamps cannot make Herdr
        silently ignore a successful-looking command.  The inventory is checked
        again before the lock is released; a concurrent agent transition is
        reconciled up to a small fixed bound and otherwise fails visibly.
        """
        report: Optional[MetadataSyncReport] = None
        try:
            with self._sync_lock_factory():
                report = self._sync_metadata_locked()
        except MetadataSyncLockError as exc:
            return MetadataSyncReport(
                source_state="unavailable",
                attempted=report.attempted if report is not None else 0,
                updated=report.updated if report is not None else 0,
                failures=(
                    *((report.failures if report is not None else ())),
                    MetadataSyncFailure(
                        unit_id="board",
                        provider="unknown",
                        reason=f"metadata_sync_lock_{exc.phase}_failed",
                    ),
                ),
            )
        assert report is not None
        return report


def resolve_unit_board_binary(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the host-injected Herdr binary before the normal trusted fallback."""
    source = dict(os.environ if env is None else env)
    injected = str(source.get("HERDR_BIN_PATH") or "").strip()
    if injected:
        source["MOZYO_HERDR_BINARY"] = injected
    return resolve_herdr_binary(source).path


__all__ = (
    "HerdrUnitBoardRuntime",
    "METADATA_SOURCE",
    "METADATA_SYNC_LOCK_FILENAME",
    "METADATA_TOKEN_KEYS",
    "MetadataSyncLockError",
    "MetadataSyncFailure",
    "MetadataSyncReport",
    "resolve_unit_board_binary",
    "unit_board_metadata_lock",
)
