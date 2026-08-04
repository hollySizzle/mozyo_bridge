"""Pure canonical plan for a shared-home Herdr offline rollout (Redmine #14838).

The plan is deliberately incapable of actuation.  It turns one already-captured,
path-redacted host snapshot into a deterministic stop / migrate / restore proposal and
its digest.  Live reads belong to the terminal adapter; migration, process control and
installation belong to later, separately-approved phases.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL,
    DEFAULT_SUPERVISOR_SERVICE_LABEL,
)


OFFLINE_ROLLOUT_PLAN_SCHEMA_VERSION = 1

PLAN_READY = "planned"
PLAN_REFUSED = "refused"

REASON_INVALID_CAPTURE = "invalid_capture"
REASON_DUPLICATE_WORKSPACE = "duplicate_workspace"
REASON_DUPLICATE_ASSIGNED_NAME = "duplicate_assigned_name"
REASON_UNREGISTERED_AGENT_WORKSPACE = "unregistered_agent_workspace"
REASON_UNMANAGED_AGENT_PRESENT = "unmanaged_agent_present"
REASON_TOP_IDENTITY_UNRESOLVED = "top_identity_unresolved"
REASON_WIP_UNREADABLE = "wip_unreadable"
REASON_STORE_SET_INVALID = "store_set_invalid"
REASON_STORE_UNREADABLE = "store_unreadable"
REASON_SUPERVISOR_SET_INVALID = "supervisor_set_invalid"

STORE_ATTESTATION = "attestation"
STORE_LANE_LIFECYCLE = "lane_lifecycle"
STORE_STARTUP_TRANSACTION = "startup_transaction"
STORE_NAMES = frozenset(
    {STORE_ATTESTATION, STORE_LANE_LIFECYCLE, STORE_STARTUP_TRANSACTION}
)
STORE_TARGET_VERSIONS = {
    STORE_ATTESTATION: 3,
    STORE_LANE_LIFECYCLE: 10,
    STORE_STARTUP_TRANSACTION: 2,
}

STORE_ABSENT = "absent"
STORE_RECOGNIZED = "recognized"
_ADMISSIBLE_STORE_STATES = frozenset({STORE_ABSENT, STORE_RECOGNIZED})

SCOPE_TARGET_PROJECT = "target_project"
SCOPE_UNRELATED_PROJECT = "unrelated_project"
_SCOPES = frozenset({SCOPE_TARGET_PROJECT, SCOPE_UNRELATED_PROJECT})

_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ORIGIN_HEAD_REF = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")
OWNED_SUPERVISOR_LABELS = frozenset(
    {
        DEFAULT_SUPERVISOR_SERVICE_LABEL,
        DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL,
    }
)


def _exact_nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _canonical_bytes(value: Mapping) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _valid_origin_head_ref(value: object) -> bool:
    if not isinstance(value, str) or not _ORIGIN_HEAD_REF.fullmatch(value):
        return False
    branch = value.removeprefix("refs/heads/")
    parts = branch.split("/")
    return (
        ".." not in branch
        and "//" not in branch
        and all(
            part
            and not part.startswith(".")
            and not part.endswith((".", ".lock"))
            for part in parts
        )
    )


@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace_id: str
    project_name: str
    scope: str
    assigned_names: tuple[str, ...] = ()
    wip_readable: bool = False
    dirty: bool = False
    untracked: bool = False
    wip_digest: str = ""

    def to_record(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "project_name": self.project_name,
            "scope": self.scope,
            "assigned_names": list(sorted(self.assigned_names)),
            "wip": {
                "readable": self.wip_readable,
                "dirty": self.dirty,
                "untracked": self.untracked,
                "digest": self.wip_digest,
            },
        }


@dataclass(frozen=True)
class AgentSnapshot:
    assigned_name: str
    workspace_id: str
    lane_id: str
    provider: str
    runtime_state: str

    def to_record(self) -> dict:
        return {
            "assigned_name": self.assigned_name,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "provider": self.provider,
            "runtime_state": self.runtime_state,
        }


@dataclass(frozen=True)
class TopIdentitySnapshot:
    workspace_id: str
    lane_id: str
    provider: str
    assigned_name: str

    def to_record(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
        }


@dataclass(frozen=True)
class StoreSnapshot:
    name: str
    state: str
    version: Optional[int]
    upgrade_required: bool = False
    content_digest: str = ""
    migration_plan_digest: str = ""

    def to_record(self) -> dict:
        return {
            "state": self.state,
            "version": self.version,
            "target_version": STORE_TARGET_VERSIONS[self.name],
            "upgrade_required": self.upgrade_required,
            "content_digest": self.content_digest,
            "migration_plan_digest": self.migration_plan_digest,
        }


@dataclass(frozen=True)
class SupervisorAgentSnapshot:
    label: str
    installed: bool
    loaded: bool
    pid: Optional[int]
    home_pin: str
    executable_matches: bool
    credential_readiness: str

    def to_record(self) -> dict:
        return {
            "label": self.label,
            "installed": self.installed,
            "loaded": self.loaded,
            "pid": self.pid,
            "home_pin": self.home_pin,
            "executable_matches": self.executable_matches,
            "credential_readiness": self.credential_readiness,
        }


@dataclass(frozen=True)
class OfflineRolloutCapture:
    current_workspace_id: str
    current_project_name: str
    candidate_version: str
    candidate_source_sha: str
    candidate_source_ref: str
    candidate_workflow_run_id: str
    candidate_wheel_sha256: str
    candidate_sdist_sha256: str
    workspaces: tuple[WorkspaceSnapshot, ...]
    agents: tuple[AgentSnapshot, ...]
    unmanaged_assigned_names: tuple[str, ...]
    top_identity: TopIdentitySnapshot
    stores: tuple[StoreSnapshot, ...]
    supervisors: tuple[SupervisorAgentSnapshot, ...] = ()


@dataclass(frozen=True)
class OfflineRolloutPlanResult:
    state: str
    reason: str = ""
    detail: str = ""
    plan_digest: str = ""
    canonical_plan_bytes: bytes = field(default=b"", repr=False)
    notes: Sequence[str] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.state == PLAN_READY

    @property
    def plan(self) -> Optional[dict]:
        """Decode a fresh public copy from the immutable digest authority."""
        if not self.canonical_plan_bytes:
            return None
        value = json.loads(self.canonical_plan_bytes.decode("utf-8"))
        if not isinstance(value, dict):  # construction below makes this unreachable
            raise ValueError("canonical_plan_not_mapping")
        return value

    def as_payload(self) -> dict:
        plan = self.plan
        return {
            "ok": self.ok,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "plan_digest": self.plan_digest,
            "plan": plan,
            "notes": list(self.notes),
        }


def refused(reason: str, detail: str) -> OfflineRolloutPlanResult:
    """Return one typed, non-actuating refusal."""
    return OfflineRolloutPlanResult(state=PLAN_REFUSED, reason=reason, detail=detail)


def _validate_capture(capture: OfflineRolloutCapture) -> Optional[OfflineRolloutPlanResult]:
    identity_tokens = (
        capture.current_workspace_id,
        capture.current_project_name,
        capture.candidate_version,
        capture.top_identity.workspace_id,
        capture.top_identity.lane_id,
        capture.top_identity.provider,
        capture.top_identity.assigned_name,
    )
    if not all(_exact_nonempty(value) for value in identity_tokens):
        return refused(REASON_INVALID_CAPTURE, "identity_or_candidate_token_invalid")
    if not isinstance(capture.candidate_source_sha, str) or (
        capture.candidate_source_sha
        and not _SHA40.fullmatch(capture.candidate_source_sha)
    ):
        return refused(REASON_INVALID_CAPTURE, "candidate_source_sha_invalid")
    source_ref = capture.candidate_source_ref
    if not isinstance(source_ref, str) or (
        source_ref and not _valid_origin_head_ref(source_ref)
    ):
        return refused(REASON_INVALID_CAPTURE, "candidate_source_ref_invalid")
    run_id = capture.candidate_workflow_run_id
    if not isinstance(run_id, str) or (
        run_id
        and (
            not run_id.isascii()
            or not run_id.isdecimal()
            or int(run_id) < 1
            or str(int(run_id)) != run_id
        )
    ):
        return refused(REASON_INVALID_CAPTURE, "candidate_workflow_run_id_invalid")
    for name, digest in (
        ("wheel", capture.candidate_wheel_sha256),
        ("sdist", capture.candidate_sdist_sha256),
    ):
        if not isinstance(digest, str) or (
            digest and not _SHA256.fullmatch(digest)
        ):
            return refused(REASON_INVALID_CAPTURE, f"candidate_{name}_sha256_invalid")

    workspace_ids = [workspace.workspace_id for workspace in capture.workspaces]
    if len(workspace_ids) != len(set(workspace_ids)):
        return refused(REASON_DUPLICATE_WORKSPACE, "workspace_id_not_unique")
    if capture.current_workspace_id not in set(workspace_ids):
        return refused(REASON_INVALID_CAPTURE, "current_workspace_not_registered")
    for workspace in capture.workspaces:
        if not all(
            (
                _exact_nonempty(workspace.workspace_id),
                _exact_nonempty(workspace.project_name),
                workspace.scope in _SCOPES,
            )
        ):
            return refused(REASON_INVALID_CAPTURE, "workspace_record_invalid")
        if not workspace.wip_readable:
            return refused(REASON_WIP_UNREADABLE, workspace.workspace_id)
        if workspace.wip_digest and not re.fullmatch(r"[0-9a-f]{64}", workspace.wip_digest):
            return refused(REASON_INVALID_CAPTURE, "wip_digest_invalid")

    if capture.unmanaged_assigned_names:
        return refused(
            REASON_UNMANAGED_AGENT_PRESENT,
            "live_inventory_contains_unmanaged_rows",
        )
    names = [agent.assigned_name for agent in capture.agents]
    if len(names) != len(set(names)):
        return refused(REASON_DUPLICATE_ASSIGNED_NAME, "assigned_name_not_unique")
    registered = set(workspace_ids)
    for agent in capture.agents:
        if not all(
            _exact_nonempty(value)
            for value in (
                agent.assigned_name,
                agent.workspace_id,
                agent.lane_id,
                agent.provider,
                agent.runtime_state,
            )
        ):
            return refused(REASON_INVALID_CAPTURE, "agent_record_invalid")
        if agent.workspace_id not in registered:
            return refused(REASON_UNREGISTERED_AGENT_WORKSPACE, agent.workspace_id)
    if names.count(capture.top_identity.assigned_name) != 1:
        return refused(REASON_TOP_IDENTITY_UNRESOLVED, "top_not_exactly_once")
    top_agent = next(
        agent
        for agent in capture.agents
        if agent.assigned_name == capture.top_identity.assigned_name
    )
    if (
        top_agent.workspace_id,
        top_agent.lane_id,
        top_agent.provider,
    ) != (
        capture.top_identity.workspace_id,
        capture.top_identity.lane_id,
        capture.top_identity.provider,
    ):
        return refused(REASON_TOP_IDENTITY_UNRESOLVED, "top_slot_mismatch")

    stores = {store.name: store for store in capture.stores}
    if len(stores) != len(capture.stores) or set(stores) != STORE_NAMES:
        return refused(REASON_STORE_SET_INVALID, "three_store_set_required")
    for store in stores.values():
        if store.state not in _ADMISSIBLE_STORE_STATES:
            return refused(REASON_STORE_UNREADABLE, store.name)
        if store.state == STORE_RECOGNIZED and (
            not isinstance(store.version, int) or isinstance(store.version, bool)
        ):
            return refused(REASON_STORE_UNREADABLE, store.name)
        if store.state == STORE_ABSENT and store.version is not None:
            return refused(REASON_INVALID_CAPTURE, "absent_store_has_version")
        if store.state == STORE_RECOGNIZED and not _SHA256.fullmatch(
            store.content_digest
        ):
            return refused(REASON_STORE_UNREADABLE, f"{store.name}_content_digest")
        if store.state == STORE_ABSENT and (
            store.content_digest or store.migration_plan_digest
        ):
            return refused(REASON_INVALID_CAPTURE, "absent_store_has_digest")
        if store.migration_plan_digest and not _SHA256.fullmatch(
            store.migration_plan_digest
        ):
            return refused(REASON_STORE_UNREADABLE, f"{store.name}_migration_digest")
        if (
            store.name == STORE_STARTUP_TRANSACTION
            and store.state == STORE_RECOGNIZED
            and not store.migration_plan_digest
        ):
            return refused(REASON_STORE_UNREADABLE, "startup_migration_digest")
    labels = [supervisor.label for supervisor in capture.supervisors]
    if (
        len(labels) != len(set(labels))
        or not all(_exact_nonempty(label) for label in labels)
        or set(labels) != OWNED_SUPERVISOR_LABELS
    ):
        return refused(REASON_SUPERVISOR_SET_INVALID, "owned_supervisor_pair_required")
    return None


def build_offline_rollout_plan(
    capture: OfflineRolloutCapture,
) -> OfflineRolloutPlanResult:
    """Validate ``capture`` and return its deterministic, side-effect-free plan."""
    invalid = _validate_capture(capture)
    if invalid is not None:
        return invalid

    workspaces = sorted(capture.workspaces, key=lambda item: item.workspace_id)
    agents = sorted(capture.agents, key=lambda item: item.assigned_name)
    stores = {store.name: store for store in capture.stores}
    top_name = capture.top_identity.assigned_name
    non_top = [agent.assigned_name for agent in agents if agent.assigned_name != top_name]
    supervisor_labels = sorted(OWNED_SUPERVISOR_LABELS)
    artifact_pins = (
        capture.candidate_source_sha,
        capture.candidate_source_ref,
        capture.candidate_workflow_run_id,
        capture.candidate_wheel_sha256,
        capture.candidate_sdist_sha256,
    )

    body = {
        "schema_version": OFFLINE_ROLLOUT_PLAN_SCHEMA_VERSION,
        "candidate_artifact": {
            "distribution": "testpypi",
            "version": capture.candidate_version,
            "source_sha": capture.candidate_source_sha,
            "source_ref": capture.candidate_source_ref,
            "workflow_run_id": capture.candidate_workflow_run_id,
            "wheel_sha256": capture.candidate_wheel_sha256,
            "sdist_sha256": capture.candidate_sdist_sha256,
            "exact_pin_ready": all(artifact_pins),
        },
        "current_workspace_id": capture.current_workspace_id,
        "current_project_name": capture.current_project_name,
        "top_identity": capture.top_identity.to_record(),
        "workspaces": [workspace.to_record() for workspace in workspaces],
        "agents": [agent.to_record() for agent in agents],
        "stores": {name: stores[name].to_record() for name in sorted(stores)},
        "supervisors": [
            supervisor.to_record()
            for supervisor in sorted(capture.supervisors, key=lambda item: item.label)
        ],
        "stop_order": [*non_top, top_name],
        "restore_order": [top_name, *non_top],
        "schema_transitions": [
            {
                "store": name,
                "from_version": stores[name].version,
                "to_version": STORE_TARGET_VERSIONS[name],
            }
            for name in sorted(stores)
        ],
        "phase_order": [
            {
                "phase": "supervisor_stop",
                "supervisor_labels": supervisor_labels,
                "required_readback": "all_not_installed_and_not_loaded",
            },
            {"phase": "non_top_workspace_stop", "assigned_names": non_top},
            {"phase": "top_workspace_stop", "assigned_names": [top_name]},
            {"phase": "consumer_zero", "required_readback": "zero"},
            {"phase": "verified_backup", "stores": sorted(stores)},
            {"phase": "migrate_attestation", "target_version": 3},
            {"phase": "migrate_lane_lifecycle", "target_version": 10},
            {"phase": "migrate_startup_transaction", "target_version": 2},
            {"phase": "exact_runtime_install"},
            {
                "phase": "top_restore_action_bootstrap",
                "assigned_names": [top_name],
            },
            {"phase": "remaining_workspace_restore", "assigned_names": non_top},
            {
                "phase": "supervisor_pair_install",
                "supervisor_labels": supervisor_labels,
            },
            {
                "phase": "supervisor_pair_readback",
                "supervisor_labels": supervisor_labels,
            },
            {"phase": "final_verify"},
        ],
    }
    canonical_plan_bytes = _canonical_bytes(body)
    digest = hashlib.sha256(canonical_plan_bytes).hexdigest()
    return OfflineRolloutPlanResult(
        state=PLAN_READY,
        plan_digest=digest,
        canonical_plan_bytes=canonical_plan_bytes,
        notes=(
            "side_effect_zero",
            "execution_requires_exact_plan_owner_approval",
        ),
    )


__all__ = (
    "AgentSnapshot",
    "OfflineRolloutCapture",
    "OfflineRolloutPlanResult",
    "StoreSnapshot",
    "SupervisorAgentSnapshot",
    "TopIdentitySnapshot",
    "WorkspaceSnapshot",
    "build_offline_rollout_plan",
    "refused",
)
