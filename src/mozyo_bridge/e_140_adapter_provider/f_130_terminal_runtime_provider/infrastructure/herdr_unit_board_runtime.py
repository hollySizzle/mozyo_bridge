"""Live Herdr adapter for the public-safe Unit board read model.

The adapter reads the live Herdr inventory, restores mozyo's durable logical
agent identities, joins only reviewable workspace/role/lane display metadata,
and can refresh Herdr's display-only pane metadata.  It never writes agent
input, Redmine, workflow state, or a mozyo state database.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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


METADATA_SOURCE = "mozyo.unit-board"
METADATA_TIMEOUT_SECONDS = 10.0

WorkspaceLoader = Callable[[str], Optional[WorkspaceRecord]]
RoleLoader = Callable[[Path], ParsedRoleBindings]
LaneRecordsLoader = Callable[[], Mapping[str, LaneMetadataRecord]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_workspace_loader(workspace_id: str) -> Optional[WorkspaceRecord]:
    return load_workspace_by_id(workspace_id)


def _default_lane_records_loader() -> Mapping[str, LaneMetadataRecord]:
    return LaneMetadataStore().load_all(include_retired=False)


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
    ) -> None:
        if not isinstance(binary, str) or not binary:
            raise ValueError("Herdr Unit board binary must be a non-empty string")
        self._binary = binary
        self._runner: Runner = runner if runner is not None else subprocess.run
        self._lister = lister or HerdrCliAgentLister(binary, runner=self._runner)
        self._workspace_loader = workspace_loader
        self._role_loader = role_loader
        self._lane_records_loader = lane_records_loader

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

    def snapshot(self) -> UnitBoardSnapshot:
        observed_at = _utc_now()
        try:
            rows = self._lister.list_agent_rows()
        except (TerminalTransportError, OSError, ValueError):
            return unavailable_snapshot(
                "unavailable",
                observed_at=observed_at,
                detail="live Herdr agent inventory is unavailable",
            )
        try:
            lane_records = tuple(self._lane_records_loader().values())
        except (OSError, ValueError, TypeError):
            lane_records = ()

        observations: list[AgentObservation] = []
        unmanaged = 0
        for row in rows:
            if not isinstance(row, Mapping):
                unmanaged += 1
                continue
            name = row.get("name")
            decoded = decode_assigned_name(name)
            if not decoded.ok or decoded.identity is None:
                if isinstance(name, str) and name.startswith(f"{SCHEME_PREFIX}_"):
                    return unavailable_snapshot(
                        SOURCE_RELOAD_REQUIRED,
                        observed_at=observed_at,
                        detail="managed Herdr identity is malformed; reload required",
                    )
                unmanaged += 1
                continue
            identity = decoded.identity
            pane_id = row.get("pane_id")
            if not isinstance(pane_id, str) or not pane_id.strip():
                return unavailable_snapshot(
                    SOURCE_RELOAD_REQUIRED,
                    observed_at=observed_at,
                    detail="managed Herdr agent has no live pane locator; reload required",
                )
            record = self._workspace_loader(identity.workspace_id)
            if record is None:
                project_label = "unknown-project"
                role, responsibility, authority = (
                    "unknown",
                    "unknown-project",
                    AUTHORITY_MISSING,
                )
            else:
                project_label = safe_text(record.project_name, fallback="unknown-project")
                try:
                    parsed = self._role_loader(Path(record.canonical_path))
                except (OSError, ValueError, TypeError):
                    parsed = ParsedRoleBindings.invalid(
                        "workflow role authority could not be read"
                    )
                role, responsibility, authority = self._role_display(
                    parsed, identity.lane_id, project_label
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
                    work_label=self._lane_display(
                        lane_records, identity.workspace_id, identity.lane_id
                    ),
                    authority_state=authority,
                )
            )
        return build_unit_board(
            observations,
            observed_at=observed_at,
            unmanaged_agents=unmanaged,
        )

    def sync_metadata(self) -> MetadataSyncReport:
        snapshot = self.snapshot()
        if not snapshot.ok:
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
                argv.append(agent.pane_id)
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
        return MetadataSyncReport(
            source_state=snapshot.source_state,
            attempted=attempted,
            updated=updated,
            failures=tuple(failures),
        )


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
    "MetadataSyncFailure",
    "MetadataSyncReport",
    "resolve_unit_board_binary",
)
