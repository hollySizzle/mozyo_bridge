"""Hibernated UNBOUND live-zero terminal retire (Redmine #14716).

This is the metadata-only rail for a closed issue whose lane is durably
``hibernated`` and ``released``, records no canonical worktree binding, and has no
live managed process.  It does not restore or remove a checkout and never changes a
Git ref.  The write is the existing hibernated-legacy CAS, additionally fenced by a
fresh Redmine issue snapshot, the exact lane generation/revision, branch metadata,
head integration, an exclusive launch lock, and a positive live-zero measurement.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_patch_equivalent_integration import (  # noqa: E501
        PatchEquivalentResolution,
    )


HIBERNATED_UNBOUND_RETIRE_RETIRED = "retired"
HIBERNATED_UNBOUND_RETIRE_ALREADY_RETIRED = "already_retired"
HIBERNATED_UNBOUND_RETIRE_BLOCKED = "blocked"

HIBERNATED_UNBOUND_RETIRE_FENCE_NOT_DECLARED = "generation_fence_not_declared"
HIBERNATED_UNBOUND_RETIRE_NOT_HERDR_BACKEND = "not_herdr_backend"
HIBERNATED_UNBOUND_RETIRE_WORKSPACE_UNRESOLVED = "workspace_unresolved"
HIBERNATED_UNBOUND_RETIRE_BRANCH_NOT_LANE_BOUND = "branch_not_lane_bound"
HIBERNATED_UNBOUND_RETIRE_WORKTREE_CONFLICT = "worktree_conflict"
HIBERNATED_UNBOUND_RETIRE_HEAD_NOT_INTEGRATED = "head_not_integrated"
HIBERNATED_UNBOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED = (
    "patch_equivalence_unverified"
)
HIBERNATED_UNBOUND_RETIRE_REDMINE_UNREADABLE = "redmine_unreadable"
HIBERNATED_UNBOUND_RETIRE_ISSUE_NOT_CLOSED = "issue_not_closed"
HIBERNATED_UNBOUND_RETIRE_DECISION_JOURNAL_NOT_FOUND = (
    "decision_journal_not_found"
)
HIBERNATED_UNBOUND_RETIRE_LAUNCH_IN_FLIGHT = "launch_in_flight"
HIBERNATED_UNBOUND_RETIRE_EXCLUSION_UNAVAILABLE = "exclusion_unavailable"
HIBERNATED_UNBOUND_RETIRE_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
HIBERNATED_UNBOUND_RETIRE_LANE_NOT_DECLARED = "lane_not_declared"
HIBERNATED_UNBOUND_RETIRE_NOT_HIBERNATED_UNBOUND_STATE = (
    "not_hibernated_unbound_state"
)
HIBERNATED_UNBOUND_RETIRE_RELEASE_UNPROVEN = "release_unproven"
HIBERNATED_UNBOUND_RETIRE_REPLACEMENT_IN_FLIGHT = "replacement_in_flight"
HIBERNATED_UNBOUND_RETIRE_GENERATION_RACE = "generation_race"
HIBERNATED_UNBOUND_RETIRE_STORE_ERROR = "store_error"


@dataclass(frozen=True)
class HibernatedUnboundLiveZeroRetireVerdict:
    """Typed result of one hibernated-unbound terminalization attempt."""

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    expected_live: tuple[str, ...] = ()
    foreign_names: tuple[str, ...] = ()
    lifecycle_migration: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.state in (
            HIBERNATED_UNBOUND_RETIRE_RETIRED,
            HIBERNATED_UNBOUND_RETIRE_ALREADY_RETIRED,
        )

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "expected_live": list(self.expected_live),
            "foreign_names": list(self.foreign_names),
            "lifecycle_migration": self.lifecycle_migration,
        }


def _blocked(
    reason: str,
    *,
    detail: str = "",
    workspace_id: str = "",
    lane_id: str = "",
    expected_live: tuple[str, ...] = (),
    foreign_names: tuple[str, ...] = (),
    lifecycle_migration: Optional[dict] = None,
) -> HibernatedUnboundLiveZeroRetireVerdict:
    return HibernatedUnboundLiveZeroRetireVerdict(
        state=HIBERNATED_UNBOUND_RETIRE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_live=expected_live,
        foreign_names=foreign_names,
        lifecycle_migration=lifecycle_migration,
    )


def _metadata_worktree_matches_if_supplied(
    args: argparse.Namespace, *, workspace_id: str, lane_label: str
) -> tuple[bool, str]:
    """Use an optional worktree only as a refusal check, never as authority."""
    supplied = (getattr(args, "worktree", "") or "").strip()
    if not supplied:
        return True, ""
    from mozyo_bridge.core.state.lane_metadata import (
        lane_records_by_unit,
        load_lane_records,
    )

    try:
        record = lane_records_by_unit(load_lane_records()).get(
            (workspace_id, lane_label)
        )
    except Exception:  # noqa: BLE001 - an unreadable refusal source never permits
        return False, "lane metadata is unreadable; optional --worktree cannot be checked"
    if record is None or not (record.worktree_path or "").strip():
        return False, (
            "lane metadata has no recorded worktree path; the supplied --worktree cannot "
            "be shown to describe this lane"
        )
    try:
        supplied_path = Path(supplied).expanduser().resolve(strict=False)
        recorded_path = Path(record.worktree_path).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return False, "the supplied or recorded worktree path cannot be normalized"
    if supplied_path != recorded_path:
        return False, (
            "the supplied --worktree does not match this lane's recorded metadata path; "
            "it is refused and is never used as terminalization authority"
        )
    return True, ""


def _issue_record(payload: Mapping[str, object]) -> Optional[Mapping[str, object]]:
    issue = payload.get("issue")
    return issue if isinstance(issue, Mapping) else None


def _journal_rows(
    payload: Mapping[str, object], issue_record: Mapping[str, object]
) -> tuple[Mapping[str, object], ...]:
    # Keep the same precedence as LiveRedmineJournalSource: MCP/export wrappers
    # expose journals at the top level, while the REST response nests them.
    raw = payload.get("journals")
    if not isinstance(raw, list):
        raw = issue_record.get("journals")
    if not isinstance(raw, list):
        return ()
    return tuple(row for row in raw if isinstance(row, Mapping))


def _fresh_closed_decision_snapshot(
    issue: str, journal: str
) -> tuple[bool, str, str]:
    """Verify closed status and journal existence from one fresh issue-detail GET."""
    if not issue or not journal:
        return (
            False,
            HIBERNATED_UNBOUND_RETIRE_DECISION_JOURNAL_NOT_FOUND,
            "--issue and --journal must name the exact durable retirement decision",
        )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
        LiveRedmineJournalSource,
    )

    try:
        source = LiveRedmineJournalSource.from_environment(environ=os.environ)
        payload = source.transport(
            base_url=source.base_url,
            api_key=source.api_key,
            issue_id=issue,
            since=None,
        )
    except Exception as exc:  # noqa: BLE001 - any unreadable live authority fails closed
        return (
            False,
            HIBERNATED_UNBOUND_RETIRE_REDMINE_UNREADABLE,
            f"the exact Redmine issue snapshot is unreadable ({type(exc).__name__})",
        )
    if not isinstance(payload, Mapping):
        return (
            False,
            HIBERNATED_UNBOUND_RETIRE_REDMINE_UNREADABLE,
            "the Redmine response is not an issue-detail object",
        )
    record = _issue_record(payload)
    if record is None or str(record.get("id", "")) != issue:
        return (
            False,
            HIBERNATED_UNBOUND_RETIRE_REDMINE_UNREADABLE,
            "the Redmine response does not identify the exact requested issue",
        )
    status = record.get("status")
    if not isinstance(status, Mapping) or status.get("is_closed") is not True:
        return (
            False,
            HIBERNATED_UNBOUND_RETIRE_ISSUE_NOT_CLOSED,
            "the tracker does not report the exact issue closed in the fresh snapshot",
        )
    if not any(
        str(row.get("id", "")) == journal
        for row in _journal_rows(payload, record)
    ):
        return (
            False,
            HIBERNATED_UNBOUND_RETIRE_DECISION_JOURNAL_NOT_FOUND,
            "the exact retirement decision journal is absent from the fresh issue snapshot",
        )
    return True, "", ""


def run_hibernated_unbound_live_zero_retire(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    head_integrated: Optional[bool],
    patch_equivalent: Optional["PatchEquivalentResolution"] = None,
):
    """Terminalize one released hibernated UNBOUND row, or fail closed."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_backend_is_herdr,
        repo_scope_workspace_id,
    )

    lane_label = (getattr(args, "lane_label", "") or "").strip()
    if not repo_backend_is_herdr(repo_root):
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_NOT_HERDR_BACKEND,
            detail=(
                "the hibernated-unbound terminal retire is a Herdr lifecycle "
                "operation and cannot actuate for this repository backend"
            ),
            lane_id=lane_label,
        )
    issue = (getattr(args, "issue", "") or "").strip()
    journal = (getattr(args, "journal", "") or "").strip()
    try:
        expected_generation = int(
            getattr(args, "expect_lane_generation", 0) or 0
        )
        expected_revision = int(getattr(args, "expect_lane_revision", 0) or 0)
    except (TypeError, ValueError):
        expected_generation = expected_revision = 0
    if expected_generation < 1 or expected_revision < 1:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_FENCE_NOT_DECLARED,
            detail=(
                "--expect-lane-generation and --expect-lane-revision must both name "
                "the positive values reported by `sublane reboot-audit`"
            ),
            lane_id=lane_label,
        )

    workspace_id = repo_scope_workspace_id(repo_root)
    if not workspace_id:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_WORKSPACE_UNRESOLVED,
            detail="the canonical --repo does not resolve a durable workspace identity",
            lane_id=lane_label,
        )

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
        verify_branch_binds_to_lane,
    )

    branch_ok, branch_detail = verify_branch_binds_to_lane(
        args, workspace_id=workspace_id, lane_label=lane_label
    )
    if not branch_ok:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_BRANCH_NOT_LANE_BOUND,
            detail=branch_detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    worktree_ok, worktree_detail = _metadata_worktree_matches_if_supplied(
        args, workspace_id=workspace_id, lane_label=lane_label
    )
    if not worktree_ok:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_WORKTREE_CONFLICT,
            detail=worktree_detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    if head_integrated is not True:
        if patch_equivalent is None:
            return _blocked(
                HIBERNATED_UNBOUND_RETIRE_HEAD_NOT_INTEGRATED,
                detail=(
                    "the lane branch is not a proven ancestor of the integration branch"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
        if not patch_equivalent.admissible:
            return _blocked(
                HIBERNATED_UNBOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED,
                detail=(
                    "the non-ancestor integration disposition did not verify at action "
                    f"time ({patch_equivalent.reason}): {patch_equivalent.detail}"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )

    snapshot_ok, snapshot_reason, snapshot_detail = _fresh_closed_decision_snapshot(
        issue, journal
    )
    if not snapshot_ok:
        return _blocked(
            snapshot_reason,
            detail=snapshot_detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    from mozyo_bridge.core.state.lane_lifecycle_model import (
        DecisionPointer,
        DecisionPointerError,
    )

    try:
        decision = DecisionPointer(
            source="redmine", issue_id=issue, journal_id=journal
        )
    except DecisionPointerError as exc:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_DECISION_JOURNAL_NOT_FOUND,
            detail=f"the durable retirement decision pointer is invalid ({exc})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
        AttestationStoreLockBusy,
        AttestationStoreLockUnavailable,
        attestation_store_lock,
    )
    from mozyo_bridge.shared.paths import mozyo_bridge_home

    try:
        with attestation_store_lock(
            mozyo_bridge_home(), exclusive=True, blocking=False
        ):
            return _terminalize_under_exclusion(
                args,
                repo_root,
                workspace_id=workspace_id,
                lane_label=lane_label,
                issue=issue,
                decision=decision,
                expected_generation=expected_generation,
                expected_revision=expected_revision,
            )
    except AttestationStoreLockBusy as exc:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_LAUNCH_IN_FLIGHT,
            detail=f"the launch exclusion lock is busy ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    except AttestationStoreLockUnavailable as exc:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_EXCLUSION_UNAVAILABLE,
            detail=f"the launch exclusion lock is unavailable ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )


def _terminalize_under_exclusion(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    workspace_id: str,
    lane_label: str,
    issue: str,
    decision: "DecisionPointer",
    expected_generation: int,
    expected_revision: int,
) -> HibernatedUnboundLiveZeroRetireVerdict:
    """Re-read local authority, measure liveness, and CAS while lock is held."""
    from mozyo_bridge.core.state.lane_lifecycle import (
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )
    from mozyo_bridge.core.state.lane_lifecycle_model import (
        BINDING_KIND_ISSUE,
        CAS_FORBIDDEN_TRANSITION,
        CAS_NOT_FOUND,
        CAS_STALE_REVISION,
        DISPOSITION_HIBERNATED,
        DISPOSITION_RETIRED,
        RELEASE_RELEASED,
        replacement_settled,
        stored_binding_kind_is,
    )

    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
        record = LaneLifecycleStore().get(key)
    except (LaneLifecycleError, OSError, ValueError) as exc:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_LIFECYCLE_UNREADABLE,
            detail=f"the lifecycle row cannot be read ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_LANE_NOT_DECLARED,
            detail="the exact workspace/lane has no durable lifecycle row",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    replay = (
        record.lane_disposition == DISPOSITION_RETIRED
        and record.issue_id == issue
    )
    if not replay and (
        record.lane_generation != expected_generation
        or record.revision != expected_revision
    ):
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_GENERATION_RACE,
            detail="the lane generation or revision moved after the audit snapshot",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if not replay and (
        record.lane_disposition != DISPOSITION_HIBERNATED
        or not stored_binding_kind_is(record.binding_kind, BINDING_KIND_ISSUE)
        or record.issue_id != issue
        or bool(record.project_scope)
        or bool(record.worktree_identity)
    ):
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_NOT_HIBERNATED_UNBOUND_STATE,
            detail=(
                "the row is not the exact hibernated / issue-bound / unbound signature "
                "selected by the audit"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if not replay and record.process_release != RELEASE_RELEASED:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_RELEASE_UNPROVEN,
            detail="the hibernated row has no exact durable released witness",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if not replay and not replacement_settled(record.replacement_state):
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_REPLACEMENT_IN_FLIGHT,
            detail="a receiver replacement generation is still unsettled",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_live_zero_measurement import (  # noqa: E501
        measure_live_zero,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_INVENTORY_UNREADABLE,
        REASON_PROVIDER_UNRESOLVED,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
        is_lane_workspace_token,
    )

    legacy_token = ""
    worktree = (getattr(args, "worktree", "") or "").strip()
    if worktree:
        try:
            candidate = derive_lane_workspace_token(
                str(Path(worktree).expanduser().resolve(strict=False))
            )
            legacy_token = candidate if is_lane_workspace_token(candidate) else ""
        except (OSError, ValueError):
            legacy_token = ""
    try:
        measurement = measure_live_zero(
            repo_root,
            workspace_id=workspace_id,
            lane_label=lane_label,
            legacy_workspace_id=legacy_token,
            env=os.environ,
        )
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=f"live inventory is unreadable ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    except WorkflowProviderUnresolved as exc:
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            detail=f"workflow providers are unresolved ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if not measurement.proven:
        return _blocked(
            measurement.reason,
            detail=measurement.detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
            expected_live=measurement.expected_live,
            foreign_names=measurement.foreign_names,
        )
    if replay:
        return HibernatedUnboundLiveZeroRetireVerdict(
            state=HIBERNATED_UNBOUND_RETIRE_ALREADY_RETIRED,
            detail=(
                "the exact issue-bound lane is already retired and its managed unit "
                "still measures live-zero; replay performs no write"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    from mozyo_bridge.core.state.lane_retire_migration import (
        LaneRetireMigrationStore,
    )
    from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointerError

    store = LaneRetireMigrationStore()
    try:
        outcome = store.retire_released_hibernated_legacy(
            key,
            expected_revision=expected_revision,
            expected_generation=expected_generation,
            issue_id=issue,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, OSError, ValueError) as exc:
        return _blocked(
            HIBERNATED_UNBOUND_RETIRE_STORE_ERROR,
            detail=f"the bounded lifecycle write failed ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    migration_payload = _migration_payload(
        getattr(store, "last_write_preparation", None)
    )
    if outcome.applied:
        return HibernatedUnboundLiveZeroRetireVerdict(
            state=HIBERNATED_UNBOUND_RETIRE_RETIRED,
            detail=(
                "the released hibernated unbound row moved to retired; no process, "
                "checkout, branch, or commit was changed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=migration_payload,
        )
    reason_map = {
        CAS_NOT_FOUND: HIBERNATED_UNBOUND_RETIRE_LANE_NOT_DECLARED,
        CAS_STALE_REVISION: HIBERNATED_UNBOUND_RETIRE_GENERATION_RACE,
        CAS_FORBIDDEN_TRANSITION: HIBERNATED_UNBOUND_RETIRE_RELEASE_UNPROVEN,
    }
    return _blocked(
        reason_map.get(
            outcome.reason,
            HIBERNATED_UNBOUND_RETIRE_NOT_HIBERNATED_UNBOUND_STATE,
        ),
        detail=f"the bounded hibernated-unbound CAS refused ({outcome.reason})",
        workspace_id=workspace_id,
        lane_id=lane_label,
        lifecycle_migration=migration_payload,
    )


def _migration_payload(migration) -> Optional[dict]:
    if migration is None:
        return None
    try:
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            lifecycle_migration_payload,
        )

        return lifecycle_migration_payload(migration)
    except Exception:  # noqa: BLE001 - audit rendering cannot change the verdict
        return None


def format_hibernated_unbound_retire_text(
    verdict: HibernatedUnboundLiveZeroRetireVerdict,
) -> str:
    lines = [
        f"hibernated_unbound_live_zero_retire: {verdict.state}",
        f"  workspace: {verdict.workspace_id or '-'}",
        f"  lane: {verdict.lane_id or '-'}",
    ]
    if verdict.reason:
        lines.append(f"  reason: {verdict.reason}")
    if verdict.detail:
        lines.append(f"  detail: {verdict.detail}")
    if verdict.expected_live:
        lines.append(f"  expected_live: {', '.join(verdict.expected_live)}")
    if verdict.foreign_names:
        lines.append(f"  foreign_names: {', '.join(verdict.foreign_names)}")
    return "\n".join(lines)


__all__ = (
    "HIBERNATED_UNBOUND_RETIRE_ALREADY_RETIRED",
    "HIBERNATED_UNBOUND_RETIRE_BLOCKED",
    "HIBERNATED_UNBOUND_RETIRE_BRANCH_NOT_LANE_BOUND",
    "HIBERNATED_UNBOUND_RETIRE_DECISION_JOURNAL_NOT_FOUND",
    "HIBERNATED_UNBOUND_RETIRE_EXCLUSION_UNAVAILABLE",
    "HIBERNATED_UNBOUND_RETIRE_FENCE_NOT_DECLARED",
    "HIBERNATED_UNBOUND_RETIRE_GENERATION_RACE",
    "HIBERNATED_UNBOUND_RETIRE_HEAD_NOT_INTEGRATED",
    "HIBERNATED_UNBOUND_RETIRE_ISSUE_NOT_CLOSED",
    "HIBERNATED_UNBOUND_RETIRE_LANE_NOT_DECLARED",
    "HIBERNATED_UNBOUND_RETIRE_LAUNCH_IN_FLIGHT",
    "HIBERNATED_UNBOUND_RETIRE_LIFECYCLE_UNREADABLE",
    "HIBERNATED_UNBOUND_RETIRE_NOT_HIBERNATED_UNBOUND_STATE",
    "HIBERNATED_UNBOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED",
    "HIBERNATED_UNBOUND_RETIRE_REDMINE_UNREADABLE",
    "HIBERNATED_UNBOUND_RETIRE_RELEASE_UNPROVEN",
    "HIBERNATED_UNBOUND_RETIRE_REPLACEMENT_IN_FLIGHT",
    "HIBERNATED_UNBOUND_RETIRE_RETIRED",
    "HIBERNATED_UNBOUND_RETIRE_STORE_ERROR",
    "HIBERNATED_UNBOUND_RETIRE_WORKSPACE_UNRESOLVED",
    "HIBERNATED_UNBOUND_RETIRE_WORKTREE_CONFLICT",
    "HibernatedUnboundLiveZeroRetireVerdict",
    "format_hibernated_unbound_retire_text",
    "run_hibernated_unbound_live_zero_retire",
)
