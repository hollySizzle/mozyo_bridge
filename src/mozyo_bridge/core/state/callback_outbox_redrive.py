"""Explicit dead-letter redrive over the callback outbox (Redmine #15707 c).

A dead-lettered callback row is terminal to every AUTOMATIC path — ``claim_pending`` claims
only ``pending``, inflight recovery never touches it, and the #13974 invariants forbid a
restart / backlog replay from resurrecting it. That is correct for a row whose generation went
stale, and wrong for the measured #15707 shape: bounded ``precondition_not_idle`` retries
against a coordinator mid-turn exhausted the budget and terminalized a legitimately
deliverable callback. :class:`CallbackRedriveStore` is the sanctioned OUT-OF-BAND repair: an
explicit, fingerprint-gated compare-and-swap that returns one observed dead-letter row to
``pending``.

It lives beside :mod:`mozyo_bridge.core.state.callback_outbox` (same bounded context, same
``workflow-runtime.sqlite``) as its redrive companion **object** (review j#108062
finding_redriveboundary): the store module owns the schema and the automatic state machine,
this object owns the operator-driven exception to it, and the application layer consumes it
through a port Protocol — never as naked functions. It deliberately reuses the outbox's
package-internal connection helpers rather than growing a second connection/migration
implementation.

The dry-run read is **strictly read-only** (review j#108062 finding_dryrunmigration): it never
migrates a store just by asking a question — an existing older-but-recognized store is read
as-is when possible and otherwise raises (fail-closed), exactly the
:meth:`...callback_outbox.CallbackOutbox.read_strict_readonly` doctrine (#13892 R5-F4). Only
the explicit ``--apply`` mutation goes through the normal migrating write path every other
outbox write uses.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional

from mozyo_bridge.core.state.callback_outbox import (
    CallbackOutbox,
    CallbackOutboxKey,
    CallbackOutboxRow,
    _select_rows,
    _utc_now,
)
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_DEFAULT_MAX_ATTEMPTS,
    CALLBACK_PENDING,
    WorkflowRuntimeStoreError,
)

#: Typed results of :meth:`CallbackRedriveStore.requeue_dead_letter`. A closed vocabulary so
#: the operator surface can map each disposition to a distinct exit code; every value except
#: :data:`REDRIVE_REQUEUED` is a zero-write.
REDRIVE_REQUEUED = "requeued"
REDRIVE_ABSENT = "absent"
REDRIVE_STATE_MISMATCH = "state_mismatch"
REDRIVE_FINGERPRINT_MISMATCH = "fingerprint_mismatch"

#: The detail a redriven row carries: the redrive is an explicit operator action, and the row's
#: history must say so (the prior dead-letter detail is replaced, not appended — the fingerprint
#: the operator quoted already bound the exact observed row).
REDRIVE_DETAIL = "redriven from dead-letter by explicit operator action"


def redrive_fingerprint(
    key: CallbackOutboxKey, *, state: str, attempts: int, updated_at: str
) -> str:
    """The observation token an explicit dead-letter redrive must quote back (pure).

    Binds the exact row *as observed* — its UNIQUE key plus the mutable fields every state
    transition touches (``state`` / ``attempts`` / ``updated_at``) — so a redrive races nothing:
    any concurrent transition (a recovery, another redrive, a late terminal mark) changes the
    fingerprint and the apply zero-writes with :data:`REDRIVE_FINGERPRINT_MISMATCH`. The
    monotonic ``attempts`` history (a redrive never resets it) is what keeps this ABA-proof
    even inside one wall-clock second. Domain-separated and truncated; NOT a secret, just a
    compare-and-swap token.
    """
    material = "\x1f".join(
        ("mozyo-bridge:callback-redrive:v1", *key.as_row(), str(state), str(int(attempts)), str(updated_at))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class CallbackRedriveStore:
    """The public store object for the explicit dead-letter redrive (#15707 c).

    Wraps a :class:`...callback_outbox.CallbackOutbox` and exposes exactly the two redrive
    operations the application-layer use case's port needs: the strictly read-only dry-run
    read and the fingerprint-gated compare-and-swap. Construction never touches the
    filesystem.
    """

    def __init__(self, outbox: CallbackOutbox) -> None:
        self._outbox = outbox

    def dead_letter_fingerprints(
        self, *, workspace_id: Optional[str] = None
    ) -> tuple["tuple[CallbackOutboxRow, str]", ...]:
        """Read the dead-letter backlog with each row's redrive fingerprint (STRICT read-only).

        The dry-run half of the explicit redrive: the operator reads this, decides which ONE
        row to redrive, and quotes its fingerprint back to :meth:`requeue_dead_letter`.
        ``workspace_id`` filters to that partition (``None`` = all — the CLI gates this behind
        its own workspace attestation).

        Never migrates (review j#108062 finding_dryrunmigration): a missing store or a
        recognized store without the callback table reads as provably empty; a store this
        method cannot read AS-IS (an older column set, an unreadable file) raises
        :class:`WorkflowRuntimeStoreError` instead of being silently upgraded or reported
        empty — asking a question must not write, and an unanswerable question must not look
        answered.
        """
        conn = self._outbox._connect_ro()
        if conn is None:
            return ()
        try:
            if not self._outbox._table_present(conn):
                return ()
            sql = (
                "SELECT source, issue, journal, normalized_gate, callback_route, workspace_id, "
                "state, attempts, updated_at FROM callback_outbox WHERE state=?"
            )
            params: list = [CALLBACK_DEAD_LETTER]
            if workspace_id is not None:
                sql += " AND workspace_id=?"
                params.append(str(workspace_id))
            try:
                raw = conn.execute(sql + " ORDER BY seq, rowid", tuple(params)).fetchall()
                rows = _select_rows(conn, [CALLBACK_DEAD_LETTER])
            except sqlite3.DatabaseError as exc:
                raise WorkflowRuntimeStoreError(
                    "the callback outbox cannot be read at its current schema "
                    f"({type(exc).__name__}); the redrive dry-run will not migrate it to "
                    "find out"
                ) from exc
        finally:
            conn.close()
        by_key = {r.key.as_row(): r for r in rows}
        out = []
        for source, issue, journal, gate, route, ws, state, attempts, updated_at in raw:
            key = CallbackOutboxKey(
                source=source, issue=issue, journal=journal,
                normalized_gate=gate, callback_route=route, workspace_id=ws,
            )
            row = by_key.get(key.as_row())
            if row is None:
                continue
            out.append(
                (
                    row,
                    redrive_fingerprint(
                        key, state=state, attempts=int(attempts), updated_at=str(updated_at)
                    ),
                )
            )
        return tuple(out)

    def requeue_dead_letter(
        self,
        key: CallbackOutboxKey,
        *,
        expect_fingerprint: str,
        now: Optional[str] = None,
    ) -> str:
        """Return ONE observed dead-letter row to ``pending`` (explicit redrive; #15707 c).

        The compare-and-swap: the row must still be :data:`CALLBACK_DEAD_LETTER`, and
        ``expect_fingerprint`` must equal the fingerprint of the row AS PERSISTED NOW
        (:func:`redrive_fingerprint` over state / attempts / updated_at), proving the caller
        observed this exact row and nothing raced it.

        On success the row returns to ``pending`` with ONE fresh default bounded budget:
        ``attempts`` is PRESERVED (the delivery history stays auditable, and its monotonic
        growth is what makes the fingerprint ABA-proof — a redriven row that dead-letters
        again can never re-match an old observation) and ``max_attempts`` becomes
        ``attempts + CALLBACK_DEFAULT_MAX_ATTEMPTS``, so exactly one more bounded-retry cycle
        is granted per explicit redrive (the cap semantics are kept, never removed). Delivery
        then re-runs the whole fenced pipeline (claim -> generation fences -> admission -> one
        send), so a stale row is still zero-send terminal — a redrive re-admits, it never
        bypasses. Every other outcome (:data:`REDRIVE_ABSENT` / :data:`REDRIVE_STATE_MISMATCH`
        / :data:`REDRIVE_FINGERPRINT_MISMATCH`) is a typed zero-write.
        """
        stamp = now or _utc_now()
        conn = self._outbox._connect_immediate()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, attempts, updated_at FROM callback_outbox WHERE source=? AND "
                "issue=? AND journal=? AND normalized_gate=? AND callback_route=? AND "
                "workspace_id=?",
                key.as_row(),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return REDRIVE_ABSENT
            state, attempts, updated_at = str(row[0]), int(row[1]), str(row[2])
            if state != CALLBACK_DEAD_LETTER:
                conn.execute("ROLLBACK")
                return REDRIVE_STATE_MISMATCH
            expected = redrive_fingerprint(
                key, state=state, attempts=attempts, updated_at=updated_at
            )
            if str(expect_fingerprint or "").strip() != expected:
                conn.execute("ROLLBACK")
                return REDRIVE_FINGERPRINT_MISMATCH
            conn.execute(
                "UPDATE callback_outbox SET state=?, max_attempts=?, send_attempted=0, "
                "claim_token='', detail=?, updated_at=? WHERE source=? AND issue=? AND "
                "journal=? AND normalized_gate=? AND callback_route=? AND workspace_id=?",
                (
                    CALLBACK_PENDING,
                    attempts + CALLBACK_DEFAULT_MAX_ATTEMPTS,
                    REDRIVE_DETAIL,
                    stamp,
                    *key.as_row(),
                ),
            )
            conn.execute("COMMIT")
            return REDRIVE_REQUEUED
        except sqlite3.DatabaseError as exc:
            self._outbox._rollback(conn)
            raise WorkflowRuntimeStoreError(
                f"callback dead-letter redrive failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = (
    "REDRIVE_ABSENT",
    "REDRIVE_DETAIL",
    "REDRIVE_FINGERPRINT_MISMATCH",
    "REDRIVE_REQUEUED",
    "REDRIVE_STATE_MISMATCH",
    "CallbackRedriveStore",
    "redrive_fingerprint",
)
