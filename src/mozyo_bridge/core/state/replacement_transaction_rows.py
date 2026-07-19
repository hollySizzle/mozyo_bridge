"""Replacement transaction store row plumbing (Redmine #13806 tranche A).

The pure SQL-row / CAS helper functions the :mod:`...replacement_transaction` store composes
its guarded writes from — extracted so the store module holds only the store class + its
public reads (module-health boundary). No I/O of its own beyond reading an already-locked row
inside the caller's ``BEGIN IMMEDIATE`` transaction; every function is a pure decode / guard.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_PLANNED,
    ContinuationPointer,
    DecisionPointer,
    ReplacementTransactionKey,
    ReplacementTransactionRecord,
)
from mozyo_bridge.core.state.replacement_transaction_schema import (
    COLUMNS as _COLUMNS,
    TABLE as _TABLE,
)


def _record(row: Sequence[object]) -> ReplacementTransactionRecord:
    return ReplacementTransactionRecord(
        workspace_id=str(row[0]),
        action_id=str(row[1]),
        action_generation=int(row[2]),
        phase=str(row[3]),
        revision=int(row[4]),
        decision_source=str(row[5] or ""),
        decision_issue_id=str(row[6] or ""),
        decision_journal=str(row[7] or ""),
        continuation_source=str(row[8] or ""),
        continuation_issue_id=str(row[9] or ""),
        continuation_journal=str(row[10] or ""),
        continuation_expected_gate=str(row[11] or ""),
        continuation_next_action=str(row[12] or ""),
        participants_manifest=str(row[13] or ""),
        lease_holder=str(row[14] or ""),
        lease_epoch=int(row[15]),
        lease_expires_at=str(row[16] or ""),
        created_at=str(row[17]),
        updated_at=str(row[18]),
    )


def _locked_row(
    conn: sqlite3.Connection, key: ReplacementTransactionKey
) -> Optional[ReplacementTransactionRecord]:
    """Read the row inside the already-open ``BEGIN IMMEDIATE`` write lock."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM {_TABLE} WHERE workspace_id = ? AND action_id = ?",
        key.as_row(),
    ).fetchone()
    return _record(row) if row is not None else None


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        pass


def _require_exact_generation(value: object) -> int:
    """Validate an effect's ``expected_action_generation`` as a positive exact int.

    An authority token is never compared by Python numeric equality (Redmine #13806 R2-F2):
    ``7 == 7.0`` and ``1 == True`` fold, so a ``float`` / ``bool`` / string generation token
    would slip through a bare ``!=`` fence and be accepted as an approved exact generation.
    This rejects any non-``int`` (``bool`` is an ``int`` subclass and is not a generation) or
    non-positive value BEFORE any DB read/write, mirroring ``plan_transaction``'s
    ``action_generation`` validation and the manifest / component-version bool-float
    fail-closed discipline. Raising (not a zero-write outcome) is deliberate: a malformed
    token is a caller type error, exactly as ``plan_transaction`` treats a malformed
    ``action_generation`` — distinct from a well-formed but *stale* generation, which is the
    :data:`CAS_GENERATION_MISMATCH` zero-write path.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            "expected_action_generation must be an exact integer, "
            f"got {type(value).__name__}"
        )
    if value < 1:
        raise ValueError("expected_action_generation is a positive counter (>= 1)")
    return value


def _is_pristine_replan(
    existing: ReplacementTransactionRecord,
    *,
    action_generation: int,
    decision: DecisionPointer,
    continuation: ContinuationPointer,
    participants_manifest: str,
) -> bool:
    """Is ``existing`` a *genuinely untouched* exact-header duplicate? (the idempotency test)

    Idempotent no-op requires BOTH an exact header/manifest match AND a pristine row
    (Redmine #13806 R1-F5): the row is still at :data:`PHASE_PLANNED`, at its initial
    revision 1, and holds no lease (``lease_holder`` empty, ``lease_epoch`` 0). The manifest
    match already asserts every participant is ``close_owed`` (a fresh plan's manifest is
    all-``close_owed``); the pristine gate additionally excludes a row that has been
    **claimed or phase-advanced** without moving a participant. Reporting an in-flight,
    lease-held authority row as an ``applied`` re-plan would let a caller mistake a claimed
    transaction for a fresh, untouched one — so anything short of pristine is
    :data:`CAS_ALREADY_DECLARED`, never a silent success.
    """
    header_matches = (
        existing.action_generation == action_generation
        and existing.decision_source == decision.source
        and existing.decision_issue_id == decision.issue_id
        and existing.decision_journal == decision.journal_id
        and existing.continuation_source == continuation.source
        and existing.continuation_issue_id == continuation.issue_id
        and existing.continuation_journal == continuation.journal_id
        and existing.continuation_expected_gate == continuation.expected_gate
        and existing.continuation_next_action == continuation.next_semantic_action
        and existing.participants_manifest == participants_manifest
    )
    pristine = (
        existing.phase == PHASE_PLANNED
        and existing.revision == 1
        and existing.lease_holder == ""
        and existing.lease_epoch == 0
    )
    return header_matches and pristine


__all__ = (
    "_record",
    "_locked_row",
    "_rollback",
    "_require_exact_generation",
    "_is_pristine_replan",
)
