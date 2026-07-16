"""Public high-level hibernated legacy-lane retire migration (Redmine #13841).

The action-time verification + orchestration half of the metadata-only migration, kept
apart from the destructive ``retire --execute`` guarded close
(:mod:`...sublane_retire_actuation`) it must never be confused with: this path launches /
closes / resumes **no** process, removes no worktree, deletes no branch. It moves a
hibernated / released legacy lane directly to the #13689 terminal ``retired`` disposition
through the bounded :class:`...lane_retire_migration.LaneRetireMigrationStore` CAS.

The problem (Redmine #13841, live evidence #13756 j#79114–j#79115): a hibernated / released
**legacy** owner row — the coordinator hibernated the lane, its process release completed
durably, its issue is closed, its worktree clean + integrated — but whose
``worktree_identity`` is EMPTY can be retired by neither existing path (``retire --execute``
blocks forever on ``worktree_binding_unverified``; the #13809 backfill is active-row only),
so ``sublane retire --execute`` returns ``worktree_binding_unverified`` and no durable
retirement is ever recorded.

Action-time verification, every axis fail-closed (nothing is written unless ALL hold):

- **exact issue / lane** — the lane unit is keyed ``(workspace_id, lane_label)`` and the
  bounded CAS requires the row to own **this exact** issue (a different issue / lane / a
  non-``issue`` binding is refused zero-write).
- **head integrated** — the caller's ``--branch`` is a read-only ancestor of the
  ``--integration-branch``; an unknown / non-ancestor probe fails closed
  (:data:`MIGRATE_HEAD_NOT_INTEGRATED`). The clean-worktree / issue-closed / latest-review /
  callback-drain invariants are the command's ``may_retire`` preflight (which gates whether
  this path runs at all), so a dirty / open / unapproved lane never reaches here.
- **no live pair** — the live herdr inventory is read (read-only) and MUST show zero expected
  managed slots for the lane unit. A live pair is :data:`MIGRATE_LIVE_PAIR_PRESENT`
  (``active/live pair`` fails closed) — a durable ``released`` record is *not* liveness
  (``lane_lifecycle`` boundary), so the durable proof is paired with this live-zero read.
- **released legacy state** — the bounded CAS additionally requires ``hibernated`` +
  durable ``released`` + **empty** ``worktree_identity`` + settled replacement, guarded on
  the row's exact revision (a revision race loses :data:`MIGRATE_REVISION_RACE`).

A duplicate replay is idempotent: an already-``retired`` row owning this issue is a verified
no-op success (:data:`MIGRATE_ALREADY_RETIRED`), read before the live check so a completed
migration replays without depending on a live inventory.

Boundary (Redmine #13841): no process launch / close / resume, no worktree / branch removal,
no raw Herdr / tmux, no origin/main, no production / tag / publish. Synthetic regression only.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# -- migration verdict vocabulary --------------------------------------------

#: The lane was migrated: the bounded CAS moved the hibernated / released legacy row to the
#: #13689 terminal ``retired`` disposition. Metadata only — no process was touched.
MIGRATE_RETIRED = "retired"
#: A verified idempotent no-op: the row is already ``retired`` and owns this exact issue, so
#: a duplicate replay succeeds without a second write.
MIGRATE_ALREADY_RETIRED = "already_retired"
#: Fail-closed: the migration proved nothing and wrote nothing. Never exit 0.
MIGRATE_BLOCKED = "blocked"

#: Blocked reasons (migration-specific). The lane-resolution reasons are reused from the
#: guarded close (:mod:`...sublane_herdr_retire`) so an operator reads one vocabulary.
MIGRATE_LIVE_PAIR_PRESENT = "live_pair_present"
MIGRATE_HEAD_NOT_INTEGRATED = "head_not_integrated"
#: The caller's ``--worktree`` is not actually checked out on the caller's ``--branch``
#: (a mismatch, a detached HEAD, or an unresolvable checkout). The clean / integrated
#: evidence would then describe a branch other than the worktree's real head, so the
#: identity is refused zero-write (review j#79150 finding 1).
MIGRATE_WORKTREE_BRANCH_MISMATCH = "worktree_branch_mismatch"
MIGRATE_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
#: The bounded CAS refused: the row is not the exact hibernated / released / empty-worktree
#: legacy signature (a different issue / disposition / binding, or an already-#13754-bound
#: non-empty worktree).
MIGRATE_NOT_LEGACY_STATE = "not_hibernated_legacy_state"
#: The bounded CAS refused: the process release is not durably ``released`` (never requested,
#: or still in flight), or a receiver replacement is in flight.
MIGRATE_RELEASE_NOT_PROVEN = "release_not_proven"
#: The bounded CAS refused: the row is not present (no durable lifecycle owner row).
MIGRATE_LANE_NOT_DECLARED = "lane_not_declared"
#: The bounded CAS refused: a concurrent declare / transition moved the row.
MIGRATE_REVISION_RACE = "revision_race"
#: The bounded CAS raised a store error (surfaced, not swallowed).
MIGRATE_STORE_ERROR = "store_error"


@dataclass(frozen=True)
class HibernatedLegacyRetireVerdict:
    """The fail-closed verdict of the metadata-only hibernated legacy retire migration.

    ``ok`` (the command's exit-code authority) is true only for a real migration or a
    verified idempotent no-op — every other outcome is :data:`MIGRATE_BLOCKED` with the
    ``reason`` that could not be established, never a success.
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    expected_live: tuple[str, ...] = ()
    #: The shared-store schema migration this retire's write gate performed, if any (Redmine
    #: #13844 R3-F2): the typed audit record (from/to version, backup, peer-reader risk) so the
    #: migration is legible in JSON/text, not only the pre-migration stderr advisory.
    lifecycle_migration: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.state in (MIGRATE_RETIRED, MIGRATE_ALREADY_RETIRED)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "expected_live": list(self.expected_live),
            "lifecycle_migration": self.lifecycle_migration,
        }


def _blocked(
    reason: str,
    *,
    detail: str = "",
    workspace_id: str = "",
    lane_id: str = "",
    expected_live: tuple[str, ...] = (),
    lifecycle_migration: Optional[dict] = None,
) -> HibernatedLegacyRetireVerdict:
    return HibernatedLegacyRetireVerdict(
        state=MIGRATE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_live=expected_live,
        lifecycle_migration=lifecycle_migration,
    )


def run_hibernated_legacy_retire_migration(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    head_integrated: Optional[bool],
    worktree_branch: Optional[str],
):
    """Metadata-only migrate a hibernated / released legacy lane to ``retired`` (Redmine #13841).

    Returns a :class:`HibernatedLegacyRetireVerdict`, or ``None`` when the repo is not on the
    herdr backend (the migration is a herdr lane-lifecycle operation, like the guarded close).

    ``head_integrated`` is the command's read-only ancestry probe result (``--branch`` reachable
    from ``--integration-branch``); ``None`` / ``False`` fails closed. ``worktree_branch`` is the
    ``--worktree``'s ACTUAL checked-out branch (``git rev-parse --abbrev-ref HEAD``, ``None`` when
    unresolvable / detached): it must equal ``--branch``, so the clean + integrated evidence
    describes the worktree's real head and not an unrelated branch name (review j#79150 finding 1).

    The command runs this only when its ``may_retire`` preflight already passed (issue closed,
    worktree clean, latest review admissible, callbacks drained, target identity known), so
    those axes are established upstream. This adds the axes the preflight cannot: the worktree ↔
    branch identity, head integration, a live-inventory zero read, and the released-legacy-state
    CAS. Every success — including an idempotent already-retired replay — is action-time verified
    against the live inventory before it is reported (review j#79150 finding 2).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
        repo_backend_is_herdr,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_INVENTORY_UNREADABLE,
        REASON_NO_WORKTREE_ANCHOR,
        REASON_PROVIDER_NOT_LAUNCHABLE,
        REASON_PROVIDER_UNRESOLVED,
        REASON_WORKSPACE_UNRESOLVED,
        expected_live_slots,
        plan_herdr_retire_close,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
    )

    if not repo_backend_is_herdr(repo_root):
        return None
    worktree = getattr(args, "worktree", None)
    lane_label = (getattr(args, "lane_label", "") or "").strip()
    issue = (getattr(args, "issue", "") or "").strip()
    journal = (getattr(args, "journal", "") or "").strip()
    if not worktree:
        return _blocked(
            REASON_NO_WORKTREE_ANCHOR,
            detail=(
                "the migration needs the lane's --worktree anchor to resolve the lane "
                "unit; without it no lane identity can be established"
            ),
            lane_id=lane_label,
        )
    # Resolve the lane unit from the --worktree anchor, exactly as the guarded close does:
    # the worktree inherits the project workspace identity (#13377), and a legacy pre-#13377
    # lane keeps its path-derived ``wt_`` token. The migration never derives the metadata
    # worktree token (there is nothing to attest against — an empty binding is the defining
    # legacy signature; the CAS requires it empty), only the unit the live check scopes to.
    try:
        resolved_worktree = Path(worktree).expanduser().resolve()
        workspace_id = herdr_workspace_segment(resolved_worktree)
    except (OSError, ValueError) as exc:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=f"--worktree does not resolve ({type(exc).__name__})",
            lane_id=lane_label,
        )
    try:
        collapsed_to_root = resolved_worktree == repo_root.expanduser().resolve()
    except OSError:
        collapsed_to_root = False
    legacy_token = (
        "" if collapsed_to_root else derive_lane_workspace_token(str(resolved_worktree))
    )
    if not workspace_id and not legacy_token:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=(
                "the --worktree root carries no herdr workspace anchor and no lane "
                "token; the lane unit cannot be identified (point --repo / --worktree "
                "at the lane's own checkout)"
            ),
            lane_id=lane_label,
        )
    # Worktree ↔ branch identity (review j#79150 finding 1): the clean probe measures the
    # --worktree while the integration probe measures --branch, so unless the --worktree is
    # ACTUALLY checked out on --branch the two describe different heads — an unrelated branch's
    # clean / integrated evidence could then license the retire. Require the worktree's real
    # branch (git rev-parse --abbrev-ref HEAD) to equal --branch; a mismatch, a detached HEAD
    # ("HEAD"), an unresolvable checkout (None), or an empty --branch fails closed zero-write.
    want_branch = (getattr(args, "branch", "") or "").strip()
    actual_branch = (worktree_branch or "").strip()
    if (
        not want_branch
        or not actual_branch
        or actual_branch == "HEAD"
        or actual_branch != want_branch
    ):
        return _blocked(
            MIGRATE_WORKTREE_BRANCH_MISMATCH,
            detail=(
                f"the --worktree is not checked out on --branch {want_branch or '<none>'} "
                f"(actual head: {actual_branch or '<unresolved/detached>'}); its clean + "
                "integrated evidence cannot be attributed to the lane's branch, so the "
                "migration fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Head-integration is an action-time invariant (Redmine #13841): the retire preflight runs
    # with merge_on_retire=False, so it does NOT check integration — this probe does. Unknown
    # (probe could not answer) or non-ancestor fails closed.
    if head_integrated is not True:
        return _blocked(
            MIGRATE_HEAD_NOT_INTEGRATED,
            detail=(
                "--branch is not a verified ancestor of --integration-branch "
                "(unintegrated or the ancestry probe could not answer); the lane's head "
                "must be integrated before a legacy retire migration"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # The lane unit the migration targets. Read the durable row here, but do NOT decide the
    # idempotent already-retired success yet: that is gated on the live-inventory zero read
    # below (review j#79150 finding 2), so a persisted ``retired`` never reports success while
    # a pair was relaunched under it. The row read is needed for both that idempotency check
    # and the CAS; the success decision is deferred until after the live-zero read.
    from mozyo_bridge.core.state.lane_lifecycle import (
        DISPOSITION_RETIRED,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )

    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
    except ValueError:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=(
                "the lane unit cannot be keyed (empty workspace / lane); its identity "
                "cannot be established before a migration"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        record = LaneLifecycleStore().get(key)
    except (LaneLifecycleError, OSError) as exc:
        return _blocked(
            MIGRATE_LIFECYCLE_UNREADABLE,
            detail=(
                f"the lifecycle store is unreadable ({type(exc).__name__}); the lane's "
                "state cannot be verified, so the migration fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None:
        return _blocked(
            MIGRATE_LANE_NOT_DECLARED,
            detail=(
                "the lane unit has no durable lifecycle owner row; there is no legacy "
                "state to migrate"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # No live pair may remain (Redmine #13841 ``active/live pair`` fail-closed). A durable
    # ``released`` record is NOT liveness (``lane_lifecycle`` boundary), so the released-state
    # CAS below is paired with this live-inventory zero read. An unreadable inventory is NOT an
    # empty one — folding it to "nothing live" is the #13682 R1-F1 / #13754 anti-pattern. This
    # runs BEFORE the idempotent already-retired success (review j#79150 finding 2): a persisted
    # ``retired`` disposition does not prove the pair is currently gone, so even a duplicate
    # replay is only a success once the live inventory is readable AND measures zero live pair.
    try:
        rows = list_herdr_agent_rows(os.environ)
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=f"live herdr inventory unreadable ({exc}); liveness cannot be measured",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        managed_roles = (
            resolve_gateway_provider(str(repo_root)),
            resolve_worker_provider(str(repo_root)),
        )
    except WorkflowProviderUnresolved as exc:
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            detail=f"workflow provider binding unresolved ({exc})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E501
        BUILTIN_AGENT_PROVIDER_SNAPSHOT,
    )

    if not all(BUILTIN_AGENT_PROVIDER_SNAPSHOT.is_launchable(p) for p in managed_roles):
        return _blocked(
            REASON_PROVIDER_NOT_LAUNCHABLE,
            detail=(
                "the binding assigns a provider that is not mechanically launchable; the "
                "lane unit's managed pair cannot be measured"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    plan = plan_herdr_retire_close(
        rows,
        workspace_id=workspace_id,
        lane_id=lane_label,
        legacy_workspace_id=legacy_token,
        managed_roles=managed_roles,
    )
    live = expected_live_slots(rows, plan, managed_roles=managed_roles)
    if live:
        return _blocked(
            MIGRATE_LIVE_PAIR_PRESENT,
            detail=(
                "expected managed slot(s) are still live "
                f"({', '.join(live)}); the lane still has a live pair and is not an "
                "already-released legacy row — re-hibernate / release it first"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            expected_live=live,
        )
    if record.lane_disposition == DISPOSITION_RETIRED and (
        record.issue_id or ""
    ).strip() == issue:
        # Idempotent duplicate replay: the row already reached the terminal disposition and
        # owns this exact issue. A verified no-op success — reported only AFTER the live-zero
        # read above confirmed no pair is currently live (review j#79150 finding 2), so a
        # persisted ``retired`` never reports success while a pair was relaunched under it.
        return HibernatedLegacyRetireVerdict(
            state=MIGRATE_ALREADY_RETIRED,
            detail=(
                "the lane is already durably retired and no expected managed slot is live; "
                "migration is an idempotent no-op"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # The released-legacy-state CAS: hibernated + durable released + empty worktree binding +
    # this exact issue + settled replacement, guarded on the row's exact revision. Every other
    # shape is refused zero-write.
    from mozyo_bridge.core.state.lane_lifecycle import (
        CAS_FORBIDDEN_TRANSITION,
        CAS_NOT_FOUND,
        CAS_STALE_REVISION,
        DecisionPointer,
        DecisionPointerError,
    )
    from mozyo_bridge.core.state.lane_retire_migration import LaneRetireMigrationStore

    try:
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
    except DecisionPointerError:
        return _blocked(
            MIGRATE_LIFECYCLE_UNREADABLE,
            detail=(
                "no re-readable Redmine decision anchor (--issue / --journal) to record "
                "the retirement with; the migration fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Redmine #13844 R3-F2: retain the store so the migration record its write gate produced is
    # surfaced in the structured verdict (JSON/text), not only the pre-migration stderr advisory.
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        lifecycle_migration_payload,
    )

    retire_store = LaneRetireMigrationStore()
    try:
        outcome = retire_store.retire_released_hibernated_legacy(
            key,
            expected_revision=record.revision,
            issue_id=issue,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, ValueError, OSError) as exc:
        return _blocked(
            MIGRATE_STORE_ERROR,
            detail=f"the retire migration CAS raised ({type(exc).__name__}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=lifecycle_migration_payload(
                retire_store.last_write_preparation
            ),
        )
    migration = lifecycle_migration_payload(retire_store.last_write_preparation)
    if outcome.applied:
        return HibernatedLegacyRetireVerdict(
            state=MIGRATE_RETIRED,
            detail=(
                "hibernated / released legacy lane migrated directly to retired "
                "(metadata only; no process launched / closed / resumed)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=migration,
        )
    # Map the CAS refusal to a diagnostic reason (each is a distinct fail-closed shape).
    reason_map = {
        CAS_NOT_FOUND: MIGRATE_LANE_NOT_DECLARED,
        CAS_STALE_REVISION: MIGRATE_REVISION_RACE,
        CAS_FORBIDDEN_TRANSITION: MIGRATE_RELEASE_NOT_PROVEN,
    }
    reason = reason_map.get(outcome.reason, MIGRATE_NOT_LEGACY_STATE)
    return _blocked(
        reason,
        detail=(
            f"the retire migration CAS refused ({outcome.reason}); the row is not the "
            "exact hibernated / released / empty-worktree legacy signature"
        ),
        workspace_id=workspace_id,
        lane_id=lane_label,
        lifecycle_migration=migration,
    )


def format_migration_text(result: HibernatedLegacyRetireVerdict) -> str:
    """Render the migration verdict (Redmine #13841), leading with the verdict."""
    unit = result.workspace_id or "<unresolved>"
    if result.lane_id:
        unit = f"{unit} lane={result.lane_id}"
    header = f"  hibernated legacy retire migration: {result.state}"
    if result.reason:
        header += f" ({result.reason})"
    lines = [f"{header} workspace={unit}"]
    if result.detail:
        lines.append(f"    {result.detail}")
    if not result.ok:
        lines.append("    -> fail-closed: lane NOT retired; nothing was written")
    if result.expected_live:
        lines.append(
            "    live expected managed slots: " + ", ".join(result.expected_live)
        )
    if result.lifecycle_migration:
        mig = result.lifecycle_migration
        lines.append(
            "    - shared lifecycle store forward-migrated "
            f"v{mig['from_version']} -> v{mig['to_version']} "
            f"(peer lanes at read-fail-closed risk: {mig['peer_active_lanes'] or 'none'})"
        )
    return "\n".join(lines)


__all__ = (
    "MIGRATE_RETIRED",
    "MIGRATE_ALREADY_RETIRED",
    "MIGRATE_BLOCKED",
    "MIGRATE_LIVE_PAIR_PRESENT",
    "MIGRATE_HEAD_NOT_INTEGRATED",
    "MIGRATE_WORKTREE_BRANCH_MISMATCH",
    "MIGRATE_LIFECYCLE_UNREADABLE",
    "MIGRATE_NOT_LEGACY_STATE",
    "MIGRATE_RELEASE_NOT_PROVEN",
    "MIGRATE_LANE_NOT_DECLARED",
    "MIGRATE_REVISION_RACE",
    "MIGRATE_STORE_ERROR",
    "HibernatedLegacyRetireVerdict",
    "run_hibernated_legacy_retire_migration",
    "format_migration_text",
)
