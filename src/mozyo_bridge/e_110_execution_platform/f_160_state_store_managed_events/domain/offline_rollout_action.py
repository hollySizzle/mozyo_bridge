"""Pure action contract for the shared-home Herdr offline rollout (#14838).

The Phase-A plan is the authority.  This module derives the exact owner-approval
manifest from that plan and advances a replayable action one phase at a time.  It
contains no process, filesystem, package-manager, Redmine, or SQLite I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    canonical_marker_bodies,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
)


ACTION_SCHEMA_VERSION = 1

ACTION_PREPARED = "prepared"
ACTION_DELEGATED = "delegated"
ACTION_RUNNING = "running"
ACTION_BLOCKED = "blocked"
ACTION_COMPLETED = "completed"
ACTION_STATES = frozenset(
    {ACTION_PREPARED, ACTION_DELEGATED, ACTION_RUNNING, ACTION_BLOCKED, ACTION_COMPLETED}
)

OFFLINE_ROLLOUT_APPROVAL_GATE = "herdr_offline_rollout_owner_approval"
APPROVAL_VERSION = "1"
APPROVAL_SOURCE = "direct_owner"
APPROVAL_DECISION = "approved"
APPROVAL_EFFECT = "global_herdr_offline_rollout"
APPROVAL_FIELD_ORDER = (
    "gate",
    "version",
    "approval_source",
    "decision",
    "effect",
    "issue",
    "action_digest",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ACTION_ID = re.compile(r"offline_[0-9a-f]{32}")
_POINTER = re.compile(r"([1-9][0-9]*):([1-9][0-9]*)")
EXECUTION_PHASES = (
    "supervisor_stop",
    "non_top_workspace_stop",
    "top_workspace_stop",
    "consumer_zero",
    "verified_backup",
    "migrate_attestation",
    "migrate_lane_lifecycle",
    "migrate_startup_transaction",
    "exact_runtime_install",
    "top_restore_action_bootstrap",
    "remaining_workspace_restore",
    "supervisor_pair_install",
    "supervisor_pair_readback",
    "final_verify",
)


class OfflineRolloutActionError(ValueError):
    """The action or its authority is malformed; callers must fail closed."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def deterministic_action_id(plan_digest: object, approval_pointer: object) -> str:
    """One action identity per exact plan+approval, preventing duplicate delegate races."""
    digest = _token(plan_digest, "plan_digest")
    if not _SHA256.fullmatch(digest):
        raise OfflineRolloutActionError("plan_digest_invalid")
    pointer = _token(approval_pointer, "owner_approval")
    if _POINTER.fullmatch(pointer) is None:
        raise OfflineRolloutActionError("owner_approval_invalid")
    suffix = hashlib.sha256(
        f"herdr-offline-rollout-v1\n{digest}\n{pointer}".encode("ascii")
    ).hexdigest()[:32]
    return f"offline_{suffix}"


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OfflineRolloutActionError(f"{name}_invalid")
    return value


def parse_approval_pointer(value: object) -> tuple[str, str]:
    raw = _token(value, "owner_approval")
    matched = _POINTER.fullmatch(raw)
    if matched is None:
        raise OfflineRolloutActionError("owner_approval_invalid")
    return matched.group(1), matched.group(2)


def verify_plan(plan: object, expected_digest: object) -> Mapping[str, object]:
    """Require one canonical Phase-A plan and its exact SHA-256 authority."""
    digest = _token(expected_digest, "plan_digest")
    if not _SHA256.fullmatch(digest):
        raise OfflineRolloutActionError("plan_digest_invalid")
    if not isinstance(plan, Mapping):
        raise OfflineRolloutActionError("plan_invalid")
    if canonical_digest(plan) != digest:
        raise OfflineRolloutActionError("plan_digest_mismatch")
    if plan.get("schema_version") != 1:
        raise OfflineRolloutActionError("plan_schema_unsupported")
    phases = plan.get("phase_order")
    if not isinstance(phases, list) or not phases:
        raise OfflineRolloutActionError("plan_phase_order_invalid")
    names: list[str] = []
    for phase in phases:
        if not isinstance(phase, Mapping):
            raise OfflineRolloutActionError("plan_phase_invalid")
        name = _token(phase.get("phase"), "phase")
        names.append(name)
    if tuple(names) != EXECUTION_PHASES:
        raise OfflineRolloutActionError("plan_phase_order_unsupported")
    artifact = plan.get("candidate_artifact")
    if not isinstance(artifact, Mapping) or artifact.get("exact_pin_ready") is not True:
        raise OfflineRolloutActionError("artifact_pin_incomplete")
    if artifact.get("distribution") != "testpypi":
        raise OfflineRolloutActionError("artifact_distribution_unsupported")
    stores = plan.get("stores")
    targets = {"attestation": 3, "lane_lifecycle": 10, "startup_transaction": 2}
    if not isinstance(stores, Mapping) or set(stores) != set(targets):
        raise OfflineRolloutActionError("plan_store_set_invalid")
    for name, target in targets.items():
        record = stores[name]
        if (
            not isinstance(record, Mapping)
            or record.get("state") != "recognized"
            or record.get("target_version") != target
            or not isinstance(record.get("content_digest"), str)
            or not _SHA256.fullmatch(record["content_digest"])
        ):
            raise OfflineRolloutActionError(f"plan_{name}_not_execution_ready")
    startup_digest = stores["startup_transaction"].get("migration_plan_digest", "")
    if not isinstance(startup_digest, str) or not _SHA256.fullmatch(startup_digest):
        raise OfflineRolloutActionError("plan_startup_migration_digest_invalid")
    expected_transitions = [
        {
            "store": name,
            "from_version": stores[name].get("version"),
            "to_version": targets[name],
        }
        for name in sorted(targets)
    ]
    if plan.get("schema_transitions") != expected_transitions:
        raise OfflineRolloutActionError("plan_schema_transitions_invalid")
    return plan


def approval_manifest(plan: Mapping[str, object], plan_digest: str) -> dict:
    """The exact high-blast facts a direct owner approval must enumerate."""
    verified = verify_plan(plan, plan_digest)
    workspaces = verified.get("workspaces")
    agents = verified.get("agents")
    transitions = verified.get("schema_transitions")
    artifact = verified.get("candidate_artifact")
    if not isinstance(workspaces, list) or not isinstance(agents, list):
        raise OfflineRolloutActionError("plan_target_set_invalid")
    if not isinstance(transitions, list) or not isinstance(artifact, Mapping):
        raise OfflineRolloutActionError("plan_migration_set_invalid")

    workspace_ids: list[str] = []
    unrelated: list[str] = []
    workspace_projects: list[dict] = []
    for row in workspaces:
        if not isinstance(row, Mapping):
            raise OfflineRolloutActionError("plan_workspace_invalid")
        workspace_id = _token(row.get("workspace_id"), "workspace_id")
        workspace_ids.append(workspace_id)
        project_name = _token(row.get("project_name"), "project_name")
        scope = _token(row.get("scope"), "workspace_scope")
        if scope not in ("target_project", "unrelated_project"):
            raise OfflineRolloutActionError("workspace_scope_invalid")
        workspace_projects.append(
            {
                "workspace_id": workspace_id,
                "project_name": project_name,
                "scope": scope,
            }
        )
        if row.get("scope") == "unrelated_project":
            unrelated.append(workspace_id)
    assigned_names = [
        _token(row.get("assigned_name"), "assigned_name")
        for row in agents
        if isinstance(row, Mapping)
    ]
    if len(assigned_names) != len(agents):
        raise OfflineRolloutActionError("plan_agent_invalid")
    return {
        "plan_digest": plan_digest,
        "workspace_ids": sorted(workspace_ids),
        "workspace_projects": sorted(
            workspace_projects, key=lambda row: row["workspace_id"]
        ),
        "assigned_names": sorted(assigned_names),
        "unrelated_workspace_ids": sorted(unrelated),
        "schema_transitions": transitions,
        "candidate_artifact": dict(artifact),
        "global_stop": True,
        "forward_only": True,
        "no_old_runtime_rollback": True,
    }


def approval_action_digest(manifest: Mapping[str, object], issue: object) -> str:
    """Bind the complete enumerated blast manifest to one Redmine issue."""
    issue_s = _token(issue, "approval_issue")
    if (
        not issue_s.isascii()
        or not issue_s.isdecimal()
        or int(issue_s) < 1
        or str(int(issue_s)) != issue_s
    ):
        raise OfflineRolloutActionError("approval_issue_invalid")
    return canonical_digest(
        {
            "gate": OFFLINE_ROLLOUT_APPROVAL_GATE,
            "version": APPROVAL_VERSION,
            "issue": issue_s,
            "approval_manifest": dict(manifest),
        }
    )


def approval_fields(
    manifest: Mapping[str, object], issue: object
) -> dict[str, str]:
    issue_s = _token(issue, "approval_issue")
    return {
        "gate": OFFLINE_ROLLOUT_APPROVAL_GATE,
        "version": APPROVAL_VERSION,
        "approval_source": APPROVAL_SOURCE,
        "decision": APPROVAL_DECISION,
        "effect": APPROVAL_EFFECT,
        "issue": issue_s,
        "action_digest": approval_action_digest(manifest, issue_s),
    }


def render_approval_note(manifest: Mapping[str, object], issue: object) -> str:
    """Render the canonical structured marker the owner records after inspecting manifest."""
    fields = approval_fields(manifest, issue)
    body = ":".join(f"{key}={fields[key]}" for key in APPROVAL_FIELD_ORDER)
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


def parse_approval_note(note: object) -> Mapping[str, object]:
    """Parse exactly one canonical, quote-aware workflow-event approval marker."""
    if not isinstance(note, str):
        raise OfflineRolloutActionError("approval_note_invalid")
    parsed: list[dict[str, str]] = []
    for _channel, body in canonical_marker_bodies(
        note, channels=frozenset({MARKER_CHANNEL_WORKFLOW_EVENT})
    ):
        components = body.split(":")
        if not any(
            component.strip() == f"gate={OFFLINE_ROLLOUT_APPROVAL_GATE}"
            for component in components
        ):
            continue
        fields: dict[str, str] = {}
        order: list[str] = []
        for component in components:
            key, separator, value = component.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not key or not value or key in fields:
                raise OfflineRolloutActionError("approval_marker_malformed")
            fields[key] = value
            order.append(key)
        if tuple(order) != APPROVAL_FIELD_ORDER:
            raise OfflineRolloutActionError("approval_marker_noncanonical")
        parsed.append(fields)
    if len(parsed) != 1:
        raise OfflineRolloutActionError("approval_marker_not_unique")
    return parsed[0]


def approval_matches(
    note: object, plan: Mapping[str, object], plan_digest: str, issue: object
) -> bool:
    try:
        observed = parse_approval_note(note)
        expected = approval_fields(approval_manifest(plan, plan_digest), issue)
    except OfflineRolloutActionError:
        return False
    return canonical_bytes(observed) == canonical_bytes(expected)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_action(
    *,
    action_id: str,
    plan: Mapping[str, object],
    plan_digest: str,
    approval_pointer: str,
    private_bindings: Mapping[str, object],
    now: Optional[str] = None,
) -> dict:
    """Create one action payload. Private bindings never enter public status."""
    if not _ACTION_ID.fullmatch(_token(action_id, "action_id")):
        raise OfflineRolloutActionError("action_id_invalid")
    verify_plan(plan, plan_digest)
    issue, journal = parse_approval_pointer(approval_pointer)
    if not isinstance(private_bindings, Mapping):
        raise OfflineRolloutActionError("private_bindings_invalid")
    stamp = now or utc_now()
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action_id": action_id,
        "plan_digest": plan_digest,
        "plan": dict(plan),
        "approval": {"issue": issue, "journal": journal},
        "private_bindings": dict(private_bindings),
        "state": ACTION_PREPARED,
        "active_phase": "",
        "completed_phases": [],
        "phase_receipts": {},
        "attempts": 0,
        "last_reason": "",
        "last_detail": "",
        "created_at": stamp,
        "updated_at": stamp,
    }


def validate_action(action: object) -> Mapping[str, object]:
    if not isinstance(action, Mapping):
        raise OfflineRolloutActionError("action_invalid")
    if action.get("schema_version") != ACTION_SCHEMA_VERSION:
        raise OfflineRolloutActionError("action_schema_unsupported")
    if not _ACTION_ID.fullmatch(_token(action.get("action_id"), "action_id")):
        raise OfflineRolloutActionError("action_id_invalid")
    plan = action.get("plan")
    digest = action.get("plan_digest")
    verify_plan(plan, digest)
    approval = action.get("approval")
    if not isinstance(approval, Mapping):
        raise OfflineRolloutActionError("approval_invalid")
    parse_approval_pointer(f"{approval.get('issue', '')}:{approval.get('journal', '')}")
    if action.get("state") not in ACTION_STATES:
        raise OfflineRolloutActionError("action_state_invalid")
    if not isinstance(action.get("private_bindings"), Mapping):
        raise OfflineRolloutActionError("private_bindings_invalid")
    completed = action.get("completed_phases")
    receipts = action.get("phase_receipts")
    if not isinstance(completed, list) or not isinstance(receipts, Mapping):
        raise OfflineRolloutActionError("action_progress_invalid")
    phase_names = [phase["phase"] for phase in plan["phase_order"]]
    if completed != phase_names[: len(completed)]:
        raise OfflineRolloutActionError("action_phase_prefix_invalid")
    if set(receipts) != set(completed):
        raise OfflineRolloutActionError("action_receipts_invalid")
    if not all(isinstance(receipts[name], Mapping) for name in completed):
        raise OfflineRolloutActionError("action_receipts_invalid")
    is_terminal = len(completed) == len(phase_names)
    if (action.get("state") == ACTION_COMPLETED) != is_terminal:
        raise OfflineRolloutActionError("action_terminal_state_invalid")
    active_phase = action.get("active_phase")
    if not isinstance(active_phase, str):
        raise OfflineRolloutActionError("action_active_phase_invalid")
    expected_active = "" if is_terminal else phase_names[len(completed)]
    if active_phase not in ("", expected_active):
        raise OfflineRolloutActionError("action_active_phase_invalid")
    if is_terminal and active_phase:
        raise OfflineRolloutActionError("action_active_phase_invalid")
    attempts = action.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise OfflineRolloutActionError("action_attempts_invalid")
    for field in ("last_reason", "last_detail", "created_at", "updated_at"):
        if not isinstance(action.get(field), str):
            raise OfflineRolloutActionError(f"action_{field}_invalid")
    return action


def _copy(action: Mapping[str, object]) -> dict:
    return json.loads(canonical_bytes(action).decode("ascii"))


def mark_delegated(action: Mapping[str, object], *, now: Optional[str] = None) -> dict:
    validate_action(action)
    updated = _copy(action)
    updated.update(state=ACTION_DELEGATED, updated_at=now or utc_now())
    return updated


def next_phase(action: Mapping[str, object]) -> Optional[Mapping[str, object]]:
    validate_action(action)
    completed = action["completed_phases"]
    phases = action["plan"]["phase_order"]
    return None if len(completed) == len(phases) else phases[len(completed)]


def mark_running(action: Mapping[str, object], *, now: Optional[str] = None) -> dict:
    validate_action(action)
    if action["state"] == ACTION_COMPLETED:
        return _copy(action)
    updated = _copy(action)
    updated["state"] = ACTION_RUNNING
    updated["attempts"] = int(updated.get("attempts", 0)) + 1
    updated["last_reason"] = ""
    updated["last_detail"] = ""
    updated["updated_at"] = now or utc_now()
    return updated


def mark_phase_started(
    action: Mapping[str, object], phase: str, *, now: Optional[str] = None
) -> dict:
    """Persist intent before the phase's first external effect.

    Re-entering the same active phase is an exact replay.  A different active phase is
    never normalised away: it means the durable prefix and the requested effect disagree.
    """
    expected = next_phase(action)
    if expected is None or expected["phase"] != phase:
        raise OfflineRolloutActionError("action_phase_out_of_order")
    active = action.get("active_phase", "")
    if active not in ("", phase):
        raise OfflineRolloutActionError("action_active_phase_conflict")
    updated = _copy(action)
    updated["active_phase"] = phase
    updated["state"] = ACTION_RUNNING
    updated["updated_at"] = now or utc_now()
    return updated


def mark_phase_completed(
    action: Mapping[str, object],
    phase: str,
    receipt: Mapping[str, object],
    *,
    now: Optional[str] = None,
) -> dict:
    expected = next_phase(action)
    if expected is None or expected["phase"] != phase:
        raise OfflineRolloutActionError("action_phase_out_of_order")
    if not isinstance(receipt, Mapping):
        raise OfflineRolloutActionError("phase_receipt_invalid")
    if action.get("active_phase") != phase:
        raise OfflineRolloutActionError("action_phase_not_started")
    updated = _copy(action)
    updated["completed_phases"].append(phase)
    updated["phase_receipts"][phase] = dict(receipt)
    updated["active_phase"] = ""
    updated["state"] = (
        ACTION_COMPLETED
        if len(updated["completed_phases"]) == len(updated["plan"]["phase_order"])
        else ACTION_RUNNING
    )
    updated["updated_at"] = now or utc_now()
    return updated


def mark_blocked(
    action: Mapping[str, object], reason: str, detail: str = "", *, now: Optional[str] = None
) -> dict:
    validate_action(action)
    updated = _copy(action)
    updated["state"] = ACTION_BLOCKED
    updated["last_reason"] = _token(reason, "reason")
    updated["last_detail"] = str(detail or "")[:1000]
    updated["updated_at"] = now or utc_now()
    return updated


def public_status(action: Mapping[str, object]) -> dict:
    """Path/locator/WIP-byte-free status projection."""
    validate_action(action)
    phase = next_phase(action)
    return {
        "completed": action["state"] == ACTION_COMPLETED,
        "action_id": action["action_id"],
        "plan_digest": action["plan_digest"],
        "state": action["state"],
        "completed_phases": list(action["completed_phases"]),
        "next_phase": phase["phase"] if phase is not None else "",
        "active_phase": action["active_phase"],
        "attempts": action["attempts"],
        "last_reason": action["last_reason"],
        "private_detail_recorded": bool(action["last_detail"]),
        "created_at": action["created_at"],
        "updated_at": action["updated_at"],
    }


__all__ = (
    "ACTION_BLOCKED",
    "ACTION_COMPLETED",
    "ACTION_DELEGATED",
    "ACTION_PREPARED",
    "ACTION_RUNNING",
    "APPROVAL_DECISION",
    "APPROVAL_EFFECT",
    "APPROVAL_FIELD_ORDER",
    "APPROVAL_SOURCE",
    "APPROVAL_VERSION",
    "EXECUTION_PHASES",
    "OFFLINE_ROLLOUT_APPROVAL_GATE",
    "OfflineRolloutActionError",
    "approval_action_digest",
    "approval_fields",
    "approval_manifest",
    "approval_matches",
    "canonical_bytes",
    "canonical_digest",
    "deterministic_action_id",
    "mark_blocked",
    "mark_delegated",
    "mark_phase_completed",
    "mark_phase_started",
    "mark_running",
    "new_action",
    "next_phase",
    "parse_approval_pointer",
    "public_status",
    "render_approval_note",
    "validate_action",
    "verify_plan",
)
