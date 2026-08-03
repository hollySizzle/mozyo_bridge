"""Pure authority model for retiring one stale workspace registry row (#14877)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Mapping, Optional


RETIREMENT_PLAN_SCHEMA_VERSION = 1

STATE_PLANNED = "planned"
STATE_REFUSED = "refused"
STATE_RETIRED = "retired"
STATE_ALREADY_RETIRED = "already_retired"

REASON_TARGET_NOT_REGISTERED = "target_not_registered"
REASON_CURRENT_WORKSPACE_UNRESOLVED = "current_workspace_unresolved"
REASON_TARGET_IS_CURRENT = "target_is_current_workspace"
REASON_PATH_PRESENT = "workspace_path_present"
REASON_PATH_UNREADABLE = "workspace_path_unreadable"
REASON_REGISTRY_UNREADABLE = "registry_unreadable"
REASON_INVENTORY_UNREADABLE = "inventory_unreadable"
REASON_LIVE_AGENTS_PRESENT = "live_agents_present"
REASON_INVALID_OBSERVATION = "invalid_observation"
REASON_EXECUTE_DIGEST_REQUIRED = "execute_plan_digest_required"
REASON_PLAN_DIGEST_MISMATCH = "plan_digest_mismatch"
REASON_ACTION_TIME_DRIFT = "action_time_drift"
REASON_BACKUP_FAILED = "backup_failed"
REASON_RETIREMENT_FAILED = "retirement_failed"

PATH_MISSING = "missing"
PATH_PRESENT = "present"
PATH_UNREADABLE = "unreadable"

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical_bytes(value: Mapping) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _exact_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    )


def digest_workspace_record(payload: Mapping) -> str:
    """Digest the complete private registry record without exposing its path."""
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def exact_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


@dataclass(frozen=True)
class WorkspaceRetirementObservation:
    workspace_id: str
    project_name: str
    updated_at: str
    record_digest: str
    path_state: str


@dataclass(frozen=True)
class WorkspaceRetirementInventory:
    readable: bool
    projection_complete: bool
    live_agent_count: int
    target_agent_set_digest: str


@dataclass(frozen=True)
class WorkspaceRetirementPlanResult:
    state: str
    reason: str = ""
    detail: str = ""
    plan_digest: str = ""
    canonical_plan_bytes: bytes = b""
    backup_receipt: str = ""

    @property
    def ok(self) -> bool:
        return self.state in {
            STATE_PLANNED,
            STATE_RETIRED,
            STATE_ALREADY_RETIRED,
        }

    @property
    def plan(self) -> Optional[dict]:
        if not self.canonical_plan_bytes:
            return None
        return json.loads(self.canonical_plan_bytes.decode("utf-8"))

    def as_payload(self) -> dict:
        return {
            "ok": self.ok,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "plan_digest": self.plan_digest,
            "plan": self.plan,
            "backup_receipt": self.backup_receipt,
        }

    def retired(self, *, backup_receipt: str) -> "WorkspaceRetirementPlanResult":
        return replace(
            self,
            state=STATE_RETIRED,
            backup_receipt=backup_receipt,
        )

    def already_retired(
        self, *, backup_receipt: str
    ) -> "WorkspaceRetirementPlanResult":
        return replace(
            self,
            state=STATE_ALREADY_RETIRED,
            backup_receipt=backup_receipt,
        )


def refused(reason: str, detail: str) -> WorkspaceRetirementPlanResult:
    return WorkspaceRetirementPlanResult(
        state=STATE_REFUSED,
        reason=reason,
        detail=detail,
    )


def build_workspace_retirement_plan(
    *,
    observation: Optional[WorkspaceRetirementObservation],
    inventory: WorkspaceRetirementInventory,
    current_workspace_id: str,
    execute: bool = False,
    expected_plan_digest: str = "",
) -> WorkspaceRetirementPlanResult:
    """Build one path-redacted retirement plan or a typed zero-write refusal."""
    if not _exact_token(current_workspace_id):
        return refused(REASON_CURRENT_WORKSPACE_UNRESOLVED, "current_identity_invalid")
    if not isinstance(expected_plan_digest, str) or (
        expected_plan_digest and not exact_sha256(expected_plan_digest)
    ):
        return refused(REASON_INVALID_OBSERVATION, "expected_plan_digest_invalid")
    if execute and not expected_plan_digest:
        return refused(REASON_EXECUTE_DIGEST_REQUIRED, "exact_digest_not_declared")
    if observation is None:
        return refused(REASON_TARGET_NOT_REGISTERED, "workspace_id_not_found")
    if not (
        _exact_token(observation.workspace_id)
        and _exact_token(observation.project_name)
        and _exact_token(observation.updated_at)
        and exact_sha256(observation.record_digest)
        and observation.path_state in {PATH_MISSING, PATH_PRESENT, PATH_UNREADABLE}
    ):
        return refused(REASON_INVALID_OBSERVATION, "workspace_observation_invalid")
    if observation.workspace_id == current_workspace_id:
        return refused(REASON_TARGET_IS_CURRENT, "current_identity_must_be_preserved")
    if observation.path_state == PATH_PRESENT:
        return refused(REASON_PATH_PRESENT, "registered_path_still_exists")
    if observation.path_state != PATH_MISSING:
        return refused(REASON_PATH_UNREADABLE, "registered_path_not_measurable")
    if not (
        isinstance(inventory.readable, bool)
        and isinstance(inventory.projection_complete, bool)
        and isinstance(inventory.live_agent_count, int)
        and not isinstance(inventory.live_agent_count, bool)
        and inventory.live_agent_count >= 0
        and exact_sha256(inventory.target_agent_set_digest)
    ):
        return refused(REASON_INVALID_OBSERVATION, "inventory_observation_invalid")
    if not inventory.readable or not inventory.projection_complete:
        return refused(REASON_INVENTORY_UNREADABLE, "global_projection_not_lossless")
    if inventory.live_agent_count:
        return refused(REASON_LIVE_AGENTS_PRESENT, "target_has_live_agents")

    body = {
        "schema_version": RETIREMENT_PLAN_SCHEMA_VERSION,
        "action": "workspace_registry_retire",
        "current_workspace_id": current_workspace_id,
        "target": {
            "workspace_id": observation.workspace_id,
            "project_name": observation.project_name,
            "updated_at": observation.updated_at,
            "record_digest": observation.record_digest,
            "path_state": observation.path_state,
        },
        "liveness": {
            "authority": "herdr_global_inventory",
            "live_agent_count": inventory.live_agent_count,
            "target_agent_set_digest": inventory.target_agent_set_digest,
        },
        "effects": [
            "verified_sqlite_backup",
            "delete_exact_workspace_registry_row",
            "cascade_workspace_activity_row",
            "post_delete_readback",
        ],
    }
    canonical = _canonical_bytes(body)
    plan_digest = hashlib.sha256(canonical).hexdigest()
    if expected_plan_digest and expected_plan_digest != plan_digest:
        return refused(REASON_PLAN_DIGEST_MISMATCH, "fresh_plan_differs_from_approval")
    return WorkspaceRetirementPlanResult(
        state=STATE_PLANNED,
        plan_digest=plan_digest,
        canonical_plan_bytes=canonical,
    )


__all__ = (
    "PATH_MISSING",
    "PATH_PRESENT",
    "PATH_UNREADABLE",
    "STATE_ALREADY_RETIRED",
    "STATE_PLANNED",
    "STATE_REFUSED",
    "STATE_RETIRED",
    "WorkspaceRetirementInventory",
    "WorkspaceRetirementObservation",
    "WorkspaceRetirementPlanResult",
    "build_workspace_retirement_plan",
    "digest_workspace_record",
    "exact_sha256",
    "refused",
)
