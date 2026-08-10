"""Dedicated zero-effect owner-reapproval CAS for replacement transactions (#15227).

This is deliberately separate from ``ReplacementTransactionStore.supersede_transaction``.
That older rail corrects participant lifecycle evidence while keeping decision pointers fixed.
This rail keeps the participant manifest byte-for-byte fixed and changes only the owner journal
plus the exact next action generation, before any close / launch / send has occurred.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from mozyo_bridge.core.state.replacement_transaction_action_fence import (
    replacement_transaction_action_fenced,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    CAS_ACTION_MISMATCH,
    CAS_APPLIED,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    PHASE_PLANNED,
    CasOutcome,
    ContinuationPointer,
    DecisionPointer,
    ReplacementTransactionKey,
    transaction_has_zero_actuation_effect,
)
from mozyo_bridge.core.state.replacement_transaction_rows import (
    _locked_row,
    _require_exact_generation,
    _rollback,
)
from mozyo_bridge.core.state.replacement_transaction_schema import (
    TABLE as _TABLE,
    ReplacementTransactionError,
    _utc_now,
)


class _TransactionStore(Protocol):
    path: Path

    def _connect(self) -> sqlite3.Connection:
        ...


def _pointer_scope_matches(
    old_decision: DecisionPointer,
    old_continuation: ContinuationPointer,
    new_decision: DecisionPointer,
    new_continuation: ContinuationPointer,
) -> bool:
    """Only the shared journal may change; every semantic pointer field stays exact."""

    return bool(
        old_decision.source == new_decision.source
        and old_decision.issue_id == new_decision.issue_id
        and old_continuation.source == new_continuation.source
        and old_continuation.issue_id == new_continuation.issue_id
        and old_continuation.expected_gate == new_continuation.expected_gate
        and old_continuation.next_semantic_action
        == new_continuation.next_semantic_action
        and new_decision.journal_id == new_continuation.journal_id
        and new_decision.journal_id != old_decision.journal_id
    )


@replacement_transaction_action_fenced
def reapprove_zero_effect_transaction(
    store: _TransactionStore,
    key: ReplacementTransactionKey,
    *,
    expected_revision: int,
    expected_action_generation: int,
    expected_journal: str,
    new_action_generation: int,
    decision: DecisionPointer,
    continuation: ContinuationPointer,
    now: str | None = None,
) -> CasOutcome:
    """Atomically re-anchor an exact zero-effect row to one fresh owner approval.

    The participant manifest is not accepted as input and is never rewritten.  The locked row
    must still match the owner-approved old revision/generation/journal, have no actuation effect
    and no live lease, and the new generation must be exactly old + 1.  A CAS success invalidates
    every old-generation executor before this caller reaches a live effect.
    """

    old_generation = _require_exact_generation(expected_action_generation)
    next_generation = _require_exact_generation(new_action_generation)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise ValueError("expected_revision must be an exact integer")
    if expected_revision < 1:
        raise ValueError("expected_revision is a positive counter (>= 1)")
    old_journal = str(expected_journal or "").strip()
    if not old_journal:
        raise ValueError("expected_journal is required")

    stamp = now or _utc_now()
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _locked_row(conn, key)
        if existing is None:
            conn.execute("ROLLBACK")
            return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
        if existing.revision != expected_revision:
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False,
                reason=CAS_STALE_REVISION,
                revision=existing.revision,
            )
        if (
            existing.action_generation != old_generation
            or next_generation != old_generation + 1
        ):
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False,
                reason=CAS_GENERATION_MISMATCH,
                revision=existing.revision,
            )
        old_decision = existing.decision
        old_continuation = existing.continuation
        if (
            old_decision is None
            or old_continuation is None
            or old_decision.journal_id != old_journal
            or old_continuation.journal_id != old_journal
            or not _pointer_scope_matches(
                old_decision,
                old_continuation,
                decision,
                continuation,
            )
        ):
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False,
                reason=CAS_ACTION_MISMATCH,
                revision=existing.revision,
            )
        try:
            zero_effect = transaction_has_zero_actuation_effect(existing)
            lease_is_live = existing.lease_is_live(stamp)
        except Exception:  # malformed manifest / lease timestamp is never reapproval proof
            zero_effect = False
            lease_is_live = True
        if not zero_effect or lease_is_live:
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False,
                reason=CAS_UNEXPECTED_STATE,
                revision=existing.revision,
            )

        revision = existing.revision + 1
        changed = conn.execute(
            f"UPDATE {_TABLE} SET action_generation = ?, phase = ?, revision = ?, "
            "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
            "continuation_source = ?, continuation_issue_id = ?, continuation_journal = ?, "
            "continuation_expected_gate = ?, continuation_next_action = ?, "
            "lease_holder = '', lease_epoch = 0, lease_expires_at = '', "
            "created_at = ?, updated_at = ? "
            "WHERE workspace_id = ? AND action_id = ? AND revision = ?",
            (
                next_generation,
                PHASE_PLANNED,
                revision,
                decision.source,
                decision.issue_id,
                decision.journal_id,
                continuation.source,
                continuation.issue_id,
                continuation.journal_id,
                continuation.expected_gate,
                continuation.next_semantic_action,
                stamp,
                stamp,
                key.workspace_id,
                key.action_id,
                existing.revision,
            ),
        )
        if changed.rowcount != 1:
            conn.execute("ROLLBACK")
            return CasOutcome(
                applied=False,
                reason=CAS_STALE_REVISION,
                revision=existing.revision,
            )
        conn.execute("COMMIT")
        return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
    except sqlite3.DatabaseError as exc:
        _rollback(conn)
        raise ReplacementTransactionError(
            "replacement transaction owner reapproval failed "
            f"({type(exc).__name__}); fail closed"
        ) from exc
    finally:
        conn.close()


__all__ = ("reapprove_zero_effect_transaction",)
