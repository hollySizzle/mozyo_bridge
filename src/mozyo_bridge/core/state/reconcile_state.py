"""Reconcile-state CAS store (Redmine #13758).

The **store** half of the event-driven reconcile-state component: schema, registration,
and the guarded writes over the single ``reconcile_state_records`` row per
``(workspace_id, lane_id, dispatch_anchor)``. The typed key / record and the shared
``CasOutcome`` vocabulary are the pure :mod:`mozyo_bridge.core.state.reconcile_state_model`,
re-exported here so callers have one import surface; the schema guard is
:mod:`mozyo_bridge.core.state.reconcile_state_schema`.

A **native component** of the consolidated ``state.sqlite`` (the sibling
:mod:`...replacement_transaction` precedent): it uses the container guard only to create /
validate the container, then opens its own autocommit connection for the CAS (the guard's
default-isolation connection cannot drive ``BEGIN IMMEDIATE``). ``open_cycle`` is
INSERT-if-absent (a returning caller never resets the accumulated failure counter — that is
the whole point of the edge-based accumulator); ``advance`` is an exact-``expected_revision``
CAS. A stale, duplicate, or out-of-order caller updates nothing and is told why
(:class:`CasOutcome`) rather than clobbering a newer decision.

This store never sends and never reads Redmine; it only persists the derived self-heal
bookkeeping. The reconcile decision is the pure
:mod:`...domain.reconcile_state_machine` over an action-time Redmine re-read, actuated
through the **existing** callback outbox (no second outbox / ledger, j#79337).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.reconcile_state_model import (
    CAS_APPLIED,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    CasOutcome,
    ReconcileStateKey,
    ReconcileStateRecord,
    norm,
)
from mozyo_bridge.core.state.reconcile_state_schema import (
    COLUMNS as _COLUMNS,
    DEFAULT_PHASE,
    READONLY_COMPONENT_ABSENT,
    READONLY_COMPONENT_RECOGNIZED,
    RECONCILE_STATE_COMPONENT,
    RECONCILE_STATE_RECOVERY_POLICY,
    RECONCILE_STATE_SCHEMA_VERSION,
    TABLE as _TABLE,
    ReconcileStateError,
    _utc_now,
    ensure_reconcile_state_schema,
    readonly_component_status,
    reconcile_state_path,
)

# Re-export for a single import surface.
__all_reexport__ = (
    RECONCILE_STATE_COMPONENT,
    RECONCILE_STATE_RECOVERY_POLICY,
    RECONCILE_STATE_SCHEMA_VERSION,
    READONLY_COMPONENT_ABSENT,
    READONLY_COMPONENT_RECOGNIZED,
    ReconcileStateError,
    readonly_component_status,
)


# -- row plumbing ------------------------------------------------------------


def _record(row: Sequence[object]) -> ReconcileStateRecord:
    return ReconcileStateRecord(
        workspace_id=str(row[0]),
        lane_id=str(row[1]),
        dispatch_anchor=str(row[2]),
        lane_generation=int(row[3]),
        issue_id=str(row[4] or ""),
        latest_journal_id=str(row[5] or ""),
        expected_gate=str(row[6] or ""),
        expected_next_owner=str(row[7] or ""),
        phase=str(row[8]),
        reconcile_failure_count=int(row[9]),
        deadline=str(row[10] or ""),
        last_disposition=str(row[11] or ""),
        escalated=bool(row[12]),
        callback_outbox_state=str(row[13] or ""),
        last_observed_runtime=str(row[14] or ""),
        revision=int(row[15]),
        created_at=str(row[16]),
        updated_at=str(row[17]),
    )


def _locked_row(
    conn: sqlite3.Connection, key: ReconcileStateKey
) -> Optional[ReconcileStateRecord]:
    """Read the row inside the already-open ``BEGIN IMMEDIATE`` write lock."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM {_TABLE} "
        "WHERE workspace_id = ? AND lane_id = ? AND dispatch_anchor = ?",
        key.as_row(),
    ).fetchone()
    return _record(row) if row is not None else None


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        pass


def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
    """Validate a counter / revision / generation as an exact, non-negative int.

    An authority counter is never compared by Python numeric equality (the #13806 R2-F2
    discipline): ``1 == True`` and ``7 == 7.0`` fold, so a ``bool`` / ``float`` token would
    slip a bare ``!=`` fence. Rejects any non-``int`` (``bool`` is an ``int`` subclass and is
    not a counter) or below-minimum value BEFORE any DB access.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an exact integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


# -- store -------------------------------------------------------------------


class ReconcileStateStore:
    """CAS store for the event-driven reconcile bookkeeping (native ``state.sqlite``)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self.path = path if path is not None else reconcile_state_path(home)

    # -- schema / connections ------------------------------------------------

    def ensure_schema(self) -> None:
        ensure_reconcile_state_schema(self.path)

    def _connect(self) -> sqlite3.Connection:
        """An autocommit connection for the CAS (the container guard's is not)."""
        ensure_reconcile_state_schema(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    # -- reads ---------------------------------------------------------------

    def get(self, key: ReconcileStateKey) -> Optional[ReconcileStateRecord]:
        """The reconcile row, or ``None``. Raises when unreadable (fail closed)."""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} "
                "WHERE workspace_id = ? AND lane_id = ? AND dispatch_anchor = ?",
                key.as_row(),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ReconcileStateError(
                f"reconcile state read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return _record(row) if row is not None else None

    def records(self) -> tuple[ReconcileStateRecord, ...]:
        """Every row (the reconcile-projection source). Raises when unreadable."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} "
                "ORDER BY workspace_id, lane_id, dispatch_anchor"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ReconcileStateError(
                f"reconcile state read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return tuple(_record(row) for row in rows)

    # -- writes (CAS) --------------------------------------------------------

    def open_cycle(
        self,
        key: ReconcileStateKey,
        *,
        lane_generation: int = 0,
        issue_id: str = "",
        latest_journal_id: str = "",
        expected_gate: str = "",
        expected_next_owner: str = "",
        deadline: str = "",
        phase: str = "",
        last_observed_runtime: str = "",
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Create the reconcile row for a dispatch if absent; never reset a returning row.

        INSERT-if-absent at ``revision`` 1, ``reconcile_failure_count`` 0. A row that already
        exists for this exact ``(workspace_id, lane_id, dispatch_anchor)`` is returned as-is
        (``CAS_UNEXPECTED_STATE`` — not applied), so a duplicate wake for the same dispatch
        never resets the accumulated failure counter (acceptance §7). A new dispatch is a new
        anchor, hence a fresh row. Raises :class:`ReconcileStateError` when the key is invalid
        or the store is unreadable (fail closed).
        """
        if not key.valid:
            raise ReconcileStateError(
                "reconcile key requires non-empty workspace_id / lane_id / dispatch_anchor"
            )
        gen = _require_int(lane_generation, "lane_generation")
        stamp = now or _utc_now()
        opening_phase = norm(phase) or DEFAULT_PHASE
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _locked_row(conn, key)
            if existing is not None:
                conn.execute("COMMIT")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=existing.revision,
                )
            conn.execute(
                f"INSERT INTO {_TABLE} ("
                "workspace_id, lane_id, dispatch_anchor, lane_generation, issue_id, "
                "latest_journal_id, expected_gate, expected_next_owner, phase, "
                "reconcile_failure_count, deadline, last_disposition, escalated, "
                "callback_outbox_state, last_observed_runtime, revision, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '', 0, '', ?, 1, ?, ?)",
                (
                    *key.as_row(),
                    gen,
                    norm(issue_id),
                    norm(latest_journal_id),
                    norm(expected_gate),
                    norm(expected_next_owner),
                    opening_phase,
                    norm(deadline),
                    norm(last_observed_runtime),
                    stamp,
                    stamp,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=1)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReconcileStateError(
                f"reconcile open_cycle failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def advance(
        self,
        key: ReconcileStateKey,
        *,
        expected_revision: int,
        next_phase: str,
        next_failure_count: int,
        last_disposition: str = "",
        escalated: bool = False,
        callback_outbox_state: str = "",
        latest_journal_id: Optional[str] = None,
        last_observed_runtime: Optional[str] = None,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """CAS-advance the reconcile row's phase / counter under an exact expected revision.

        ``BEGIN IMMEDIATE`` + re-read the locked row + match ``expected_revision`` exactly
        (:data:`CAS_STALE_REVISION` on a duplicate / out-of-order caller,
        :data:`CAS_NOT_FOUND` when the row is gone), then bump ``revision`` by one. The new
        ``reconcile_failure_count`` is supplied by the caller (the edge-based counter is the
        pure state machine's; the store only persists it). Fields that are not passed
        (``latest_journal_id`` / ``last_observed_runtime`` ``None``) are left unchanged.
        """
        rev = _require_int(expected_revision, "expected_revision", minimum=1)
        count = _require_int(next_failure_count, "next_failure_count")
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("COMMIT")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND, revision=0)
            if current.revision != rev:
                conn.execute("COMMIT")
                return CasOutcome(
                    applied=False, reason=CAS_STALE_REVISION, revision=current.revision
                )
            new_journal = (
                current.latest_journal_id
                if latest_journal_id is None
                else norm(latest_journal_id)
            )
            new_runtime = (
                current.last_observed_runtime
                if last_observed_runtime is None
                else norm(last_observed_runtime)
            )
            conn.execute(
                f"UPDATE {_TABLE} SET phase = ?, reconcile_failure_count = ?, "
                "last_disposition = ?, escalated = ?, callback_outbox_state = ?, "
                "latest_journal_id = ?, last_observed_runtime = ?, "
                "revision = revision + 1, updated_at = ? "
                "WHERE workspace_id = ? AND lane_id = ? AND dispatch_anchor = ? "
                "AND revision = ?",
                (
                    norm(next_phase),
                    count,
                    norm(last_disposition),
                    1 if escalated else 0,
                    norm(callback_outbox_state),
                    new_journal,
                    new_runtime,
                    stamp,
                    *key.as_row(),
                    rev,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=rev + 1)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReconcileStateError(
                f"reconcile advance failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = (
    "ReconcileStateStore",
    "ReconcileStateKey",
    "ReconcileStateRecord",
    "ReconcileStateError",
    "CasOutcome",
    "CAS_APPLIED",
    "CAS_NOT_FOUND",
    "CAS_STALE_REVISION",
    "CAS_UNEXPECTED_STATE",
    "reconcile_state_path",
    "readonly_component_status",
    "RECONCILE_STATE_COMPONENT",
    "RECONCILE_STATE_SCHEMA_VERSION",
    "RECONCILE_STATE_RECOVERY_POLICY",
)
