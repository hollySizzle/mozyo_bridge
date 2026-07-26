"""Bounded active-lane declared-pin reconciliation after pair recovery (#14203 R19).

This store is intentionally narrower than declaration backfill and hibernated pin
repair.  It replaces a non-empty stale pair snapshot only when the caller supplies
the exact old pair, the exact fresh pair, and every immutable lifecycle axis still
matches under one ``BEGIN IMMEDIATE`` transaction.  It never launches, closes, or
sends to a process and it never changes the lifecycle decision pointer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    RELEASE_NOT_REQUESTED,
    CasOutcome,
    DecisionPointer,
    LaneLifecycleKey,
    ProcessGenerationPin,
    encode_declared_slots,
    norm,
    replacement_settled,
    validate_declared_slots,
)
from mozyo_bridge.core.state.lane_lifecycle_rows import (
    _locked_row,
    _rollback,
    _utc_now,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    TABLE as _TABLE,
    LaneLifecycleError,
)
from mozyo_bridge.core.state.lane_pin_role import resolve_declared_pin_pair


def _pair_identity(
    slots: Sequence[ProcessGenerationPin],
) -> tuple[tuple[str, str], tuple[str, str]]:
    pair = resolve_declared_pin_pair(slots)
    if not pair.ok or pair.gateway is None or pair.worker is None:
        raise ValueError("declared slots must resolve to one gateway and one worker")
    return (
        (norm(pair.gateway.provider), norm(pair.gateway.assigned_name)),
        (norm(pair.worker.provider), norm(pair.worker.assigned_name)),
    )


class LaneRecoveredPairPinReconcileStore:
    """CAS-replace only the stale snapshot of one proven recovered active pair."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    @property
    def last_write_preparation(self):
        return self._lifecycle.last_write_preparation

    def reconcile(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        expected_generation: int,
        issue_id: str,
        worktree_identity: str,
        lifecycle_decision: DecisionPointer,
        expected_old_slots: Sequence[ProcessGenerationPin],
        recovered_slots: Sequence[ProcessGenerationPin],
        now: Optional[str] = None,
    ) -> CasOutcome:
        issue = norm(issue_id)
        worktree = norm(worktree_identity)
        if not issue or not worktree:
            raise ValueError("active recovered-pin reconciliation requires issue and worktree")
        if not lifecycle_decision.authorizes_binding(issue):
            raise ValueError("lifecycle decision does not authorize the bound issue")

        old_slots = validate_declared_slots(tuple(expected_old_slots))
        new_slots = validate_declared_slots(tuple(recovered_slots))
        if len(old_slots) != 2 or len(new_slots) != 2:
            raise ValueError("active recovered-pin reconciliation requires an exact pair")
        if _pair_identity(old_slots) != _pair_identity(new_slots):
            raise ValueError(
                "recovered slots must preserve the gateway/worker provider-bound identities"
            )
        encoded_old = encode_declared_slots(old_slots)
        encoded_new = encode_declared_slots(new_slots)

        stamp = now or _utc_now()
        conn = self._lifecycle._connect_write(key)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.lane_generation != expected_generation:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
            if not (
                current.lane_disposition == DISPOSITION_ACTIVE
                and norm(current.binding_kind) == BINDING_KIND_ISSUE
                and norm(current.issue_id) == issue
                and not current.project_scope
                and norm(current.worktree_identity) == worktree
                and norm(current.decision_source) == lifecycle_decision.source
                and norm(current.decision_issue_id) == lifecycle_decision.issue_id
                and norm(current.decision_journal) == lifecycle_decision.journal_id
            ):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if (
                current.process_release != RELEASE_NOT_REQUESTED
                or not replacement_settled(current.replacement_state)
            ):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if current.declared_slots == encoded_new:
                # The original expected revision, or the one revision bump this
                # exact reconciliation itself performed, is a byte-equal replay.
                if current.revision not in (
                    expected_revision,
                    expected_revision + 1,
                ):
                    conn.execute("ROLLBACK")
                    return CasOutcome(
                        applied=False,
                        reason=CAS_STALE_REVISION,
                        revision=current.revision,
                    )
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=True, reason=CAS_APPLIED, revision=current.revision
                )
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if encoded_old == encoded_new:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )
            if current.declared_slots != encoded_old:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )

            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET declared_slots = ?, revision = ?, updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    encoded_new,
                    revision,
                    stamp,
                    key.repo_workspace_id,
                    key.lane_id,
                    current.revision,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                "active recovered-pair pin reconciliation failed; fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneRecoveredPairPinReconcileStore",)
