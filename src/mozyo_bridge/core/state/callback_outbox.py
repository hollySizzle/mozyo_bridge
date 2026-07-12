"""Callback outbox access over the workflow-runtime DB (Redmine #13520 / US #13518).

The zero-wait callback delivery bounded context (design answer j#75098 Q3). A handoff-worthy
durable gate transition becomes a callback to fire **exactly once** (a coordinator new-turn
trigger), idempotency-fenced so a watcher restart / duplicate herdr-or-Redmine event /
concurrent claimer can never produce a duplicate delivery.

This is a **separate bounded context** from the dispatch outbox fence
(:mod:`mozyo_bridge.core.state.dispatch_outbox_fence`) — different table and key, because
worker *send authority* and callback *delivery* are distinct concerns. What is reused is the
*pattern*: a ``BEGIN IMMEDIATE`` reserve-before-act, a UNIQUE idempotency key, and a closed
state vocabulary. Per Q3 it lives in the **same** ``workflow-runtime.sqlite`` file (schema
v2), not a new DB — so the schema authority (version, table SQL, migration) stays in
:mod:`mozyo_bridge.core.state.workflow_runtime_store` and this module drives it through the
public :meth:`WorkflowRuntimeStore.ensure_schema` before opening its own manual-transaction
connection. The two never share a table's identity beyond the file they co-inhabit.

State machine (closed vocabulary):

- ``pending`` — classified + enqueued; awaiting a delivery claim.
- ``inflight`` — claimed by a processor; ``send_attempted`` marks the send edge so a crash is
  recoverable (pre-send -> retry, post-send -> uncertain).
- ``delivered`` — the one send was positively delivered.
- ``uncertain`` — the send outcome is unknown (ACK-only / crash-after-send); **never**
  auto-retried (a duplicate delivery is the failure to avoid).
- ``dead_letter`` — unclassified, or the bounded retries were exhausted; surfaced once by a
  fresh-turn sweep for an LLM / operator to read the source journal.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

#: Default lease (seconds) after which a still-``inflight`` row is treated as abandoned and
#: eligible for recovery (#13520 review F2). A live processor claims and sends in well under a
#: second, so a concurrent processor never reclaims a fresh active claim; only a genuinely
#: crashed / hung claim older than the lease is recovered.
CALLBACK_CLAIM_LEASE_SECONDS = 300

from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_ABSENT,
    CALLBACK_DEAD_LETTER,
    CALLBACK_DEFAULT_MAX_ATTEMPTS,
    CALLBACK_DELIVERED,
    CALLBACK_INFLIGHT,
    CALLBACK_PENDING,
    CALLBACK_STATES,
    CALLBACK_UNCERTAIN,
    _RECOGNIZED_SCHEMA_VERSIONS,
    WorkflowRuntimeStore,
    WorkflowRuntimeStoreError,
    workflow_runtime_store_path,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_cutoff(stale_seconds: int) -> str:
    """ISO-second UTC timestamp ``stale_seconds`` in the past (the recovery lease cutoff).

    Compared lexicographically against ``claimed_at`` (both ISO-8601 UTC-second, which sorts
    chronologically): a row whose ``claimed_at`` is < this cutoff has an expired lease.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat(
        timespec="seconds"
    )


@dataclass(frozen=True)
class CallbackOutboxKey:
    """The UNIQUE callback-outbox idempotency key (#13520 design answer j#75098 Q3).

    ``(source, issue, journal, normalized_gate, callback_route)`` — the same handoff-worthy
    durable gate on the same journal for the same callback route is one delivery, so a
    watcher restart / duplicate herdr-or-Redmine event enqueues no new row. ``normalized_gate``
    is the gate **adopted from the exact source journal's structured marker** (never the
    notification's claimed kind); ``callback_route`` is the route the callback is delivered to.
    """

    source: str
    issue: str
    journal: str
    normalized_gate: str
    callback_route: str
    #: The workspace this callback belongs to (#13520 review R2-F5). Part of the UNIQUE key so a
    #: shared home DB partitions rows by workspace — one workspace's watcher never claims / collides
    #: with another's. Default ``""`` is the un-partitioned (legacy / single-workspace) bucket.
    workspace_id: str = ""

    def as_row(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.source,
            self.issue,
            self.journal,
            self.normalized_gate,
            self.callback_route,
            self.workspace_id,
        )


@dataclass(frozen=True)
class CallbackOutboxRow:
    """A persisted callback-outbox row (the delivery fact + its closed state)."""

    source: str
    issue: str
    journal: str
    normalized_gate: str
    callback_route: str
    state: str
    attempts: int
    max_attempts: int
    send_attempted: bool
    notification_kind: str
    notification_summary: str
    gate_mismatch: bool
    detail: str
    payload: str
    claim_token: str = ""
    workspace_id: str = ""

    @property
    def key(self) -> CallbackOutboxKey:
        return CallbackOutboxKey(
            source=self.source,
            issue=self.issue,
            journal=self.journal,
            normalized_gate=self.normalized_gate,
            callback_route=self.callback_route,
            workspace_id=self.workspace_id,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "issue": self.issue,
            "journal": self.journal,
            "normalized_gate": self.normalized_gate,
            "callback_route": self.callback_route,
            "state": self.state,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "send_attempted": self.send_attempted,
            "notification_kind": self.notification_kind,
            "notification_summary": self.notification_summary,
            "gate_mismatch": self.gate_mismatch,
            "detail": self.detail,
            "payload": self.payload,
            "claim_token": self.claim_token,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True)
class CallbackEnqueueResult:
    """The outcome of a :meth:`CallbackOutbox.enqueue` attempt.

    ``inserted`` is True only when this call wrote a fresh row (idempotency winner). For an
    existing key it is False and ``current_state`` echoes the persisted state — a duplicate
    herdr / Redmine event enqueues nothing new.
    """

    inserted: bool
    current_state: str


_SELECT = (
    "SELECT source, issue, journal, normalized_gate, callback_route, state, "
    "attempts, max_attempts, send_attempted, notification_kind, "
    "notification_summary, gate_mismatch, detail, payload, claim_token, workspace_id "
    "FROM callback_outbox"
)


def _row(r: tuple) -> CallbackOutboxRow:
    return CallbackOutboxRow(
        source=r[0],
        issue=r[1],
        journal=r[2],
        normalized_gate=r[3],
        callback_route=r[4],
        state=r[5],
        attempts=int(r[6]),
        max_attempts=int(r[7]),
        send_attempted=bool(r[8]),
        notification_kind=r[9],
        notification_summary=r[10],
        gate_mismatch=bool(r[11]),
        detail=r[12],
        payload=r[13],
        claim_token=r[14] if len(r) > 14 else "",
        workspace_id=r[15] if len(r) > 15 else "",
    )


class CallbackOutbox:
    """Read/write access to the callback outbox in the home-scoped workflow-runtime DB.

    Construction never touches the filesystem. The schema is created / migrated (v1->v2)
    through the one schema authority (:meth:`WorkflowRuntimeStore.ensure_schema`) on the first
    write; every write path drives an explicit ``BEGIN IMMEDIATE`` so an enqueue / claim / mark
    holds the write lock across its read-then-write and two concurrent callers of the same key
    serialize (the UNIQUE constraint is the backstop).
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else workflow_runtime_store_path(home)
        self._schema = WorkflowRuntimeStore(path=self.path)

    # -- connections -------------------------------------------------------

    def _connect_immediate(self) -> sqlite3.Connection:
        """Autocommit connection for the manual ``BEGIN IMMEDIATE`` paths; migrates first."""
        self._schema.ensure_schema()
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def _ensure_migrated_if_exists(self) -> None:
        """Migrate an EXISTING store up to the current schema before a read (never creates one).

        A read path selects the current column set (including ``workspace_id``, #13520 review R2-F5),
        so an older but recognized DB (e.g. a v2 callback table without ``workspace_id``) must be
        migrated first or the read would fail on the missing column. Guarded on ``exists()`` so a
        pure read never creates the store (a missing DB still reads as empty).
        """
        if self.path.exists():
            self._schema.ensure_schema()

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        """Read-only connection if the DB exists; ``None`` when absent. Unknown version fails."""
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            conn.close()
            raise WorkflowRuntimeStoreError(
                f"workflow runtime store {self.path} is unreadable: {exc}"
            ) from exc
        if version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise WorkflowRuntimeStoreError(
                f"workflow runtime store {self.path} has unsupported schema version "
                f"{version}; this build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}."
            )
        return conn

    @staticmethod
    def _table_present(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='callback_outbox'"
        ).fetchone()
        return row is not None

    # -- enqueue -----------------------------------------------------------

    def enqueue(
        self,
        key: CallbackOutboxKey,
        *,
        initial_state: str = CALLBACK_PENDING,
        notification_kind: str = "",
        notification_summary: str = "",
        gate_mismatch: bool = False,
        max_attempts: int = CALLBACK_DEFAULT_MAX_ATTEMPTS,
        detail: str = "",
        payload: str = "",
        cursor_source: Optional[str] = None,
        cursor: Optional[str] = None,
        now: Optional[str] = None,
    ) -> CallbackEnqueueResult:
        """Idempotently enqueue a callback row and (optionally) advance the source cursor.

        ``BEGIN IMMEDIATE`` + ``INSERT ... ON CONFLICT DO NOTHING`` on the UNIQUE key, so a
        duplicate herdr / Redmine event for the same key writes **no** new row and never resets
        a delivered / dead-lettered row. The optional cursor advance happens in the **same
        transaction** (ingest -> enqueue -> cursor advance atomic), so a crash cannot advance
        the cursor past an un-enqueued event. ``initial_state`` is :data:`CALLBACK_PENDING`
        for a classified gate or :data:`CALLBACK_DEAD_LETTER` for an unclassified one.
        """
        if initial_state not in CALLBACK_STATES:
            raise WorkflowRuntimeStoreError(
                f"callback initial_state must be one of {sorted(CALLBACK_STATES)}, "
                f"got {initial_state!r}"
            )
        for field, value in (
            ("source", key.source),
            ("issue", key.issue),
            ("journal", key.journal),
            ("normalized_gate", key.normalized_gate),
            ("callback_route", key.callback_route),
        ):
            if not str(value).strip():
                raise WorkflowRuntimeStoreError(
                    f"callback key requires a non-empty {field}; got {value!r}"
                )
        stamp = now or _utc_now()
        conn = self._connect_immediate()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT state FROM callback_outbox WHERE source=? AND issue=? AND journal=? "
                "AND normalized_gate=? AND callback_route=? AND workspace_id=?",
                key.as_row(),
            ).fetchone()
            if existing is None:
                next_seq = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(seq), -1) + 1 FROM callback_outbox"
                    ).fetchone()[0]
                )
                conn.execute(
                    "INSERT INTO callback_outbox (source, issue, journal, normalized_gate, "
                    "callback_route, workspace_id, state, attempts, max_attempts, send_attempted, "
                    "notification_kind, notification_summary, gate_mismatch, detail, payload, "
                    "seq, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(workspace_id, source, issue, journal, normalized_gate, callback_route) "
                    "DO NOTHING",
                    (
                        *key.as_row(),
                        initial_state,
                        int(max_attempts),
                        notification_kind,
                        notification_summary,
                        1 if gate_mismatch else 0,
                        detail,
                        payload,
                        next_seq,
                        stamp,
                        stamp,
                    ),
                )
                current = initial_state
                inserted = True
            else:
                current = str(existing[0])
                inserted = False
            if cursor_source is not None and cursor is not None:
                conn.execute(
                    "INSERT INTO callback_cursor (source, cursor, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, "
                    "updated_at=excluded.updated_at",
                    (str(cursor_source), str(cursor), stamp),
                )
            conn.execute("COMMIT")
            return CallbackEnqueueResult(inserted=inserted, current_state=current)
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise WorkflowRuntimeStoreError(
                f"callback enqueue failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- claim / recover ---------------------------------------------------

    def claim_pending(
        self, *, limit: int = 32, now: Optional[str] = None, workspace_id: Optional[str] = None
    ) -> tuple[CallbackOutboxRow, ...]:
        """Atomically claim up to ``limit`` pending rows, moving them to ``inflight``.

        ``BEGIN IMMEDIATE`` serializes concurrent processors: the first claimer flips the
        pending rows to :data:`CALLBACK_INFLIGHT` (``send_attempted=0``) and returns them; a
        second concurrent claimer sees no pending rows and returns empty — a single winner.
        Each claimed row is stamped with a fresh **claim token** + a ``claimed_at`` lease so a
        concurrent processor's :meth:`recover_inflight` cannot reclaim this active claim, and
        the owner's subsequent marks are token-conditional (#13520 review F2).

        ``workspace_id`` (#13520 review R2-F5) **partitions the claim**: when supplied, only rows in
        that workspace are claimed, so a watcher owning workspace A can never claim workspace B's
        rows on a shared home DB. ``None`` (the default) is the un-partitioned legacy behavior
        (single-workspace / test); the production watcher always pins its attested workspace.
        """
        stamp = now or _utc_now()
        conn = self._connect_immediate()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if workspace_id is None:
                rows = conn.execute(
                    _SELECT + " WHERE state=? ORDER BY seq LIMIT ?",
                    (CALLBACK_PENDING, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    _SELECT + " WHERE state=? AND workspace_id=? ORDER BY seq LIMIT ?",
                    (CALLBACK_PENDING, str(workspace_id), int(limit)),
                ).fetchall()
            claimed = []
            for r in rows:
                row = _row(r)
                token = secrets.token_hex(16)
                conn.execute(
                    "UPDATE callback_outbox SET state=?, send_attempted=0, claim_token=?, "
                    "claimed_at=?, updated_at=? WHERE source=? AND issue=? AND journal=? "
                    "AND normalized_gate=? AND callback_route=? AND workspace_id=?",
                    (CALLBACK_INFLIGHT, token, stamp, stamp, *row.key.as_row()),
                )
                claimed.append(
                    CallbackOutboxRow(
                        **{**row.as_payload(), "state": CALLBACK_INFLIGHT, "claim_token": token}  # type: ignore[arg-type]
                    )
                )
            conn.execute("COMMIT")
            return tuple(claimed)
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise WorkflowRuntimeStoreError(
                f"callback claim failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def recover_inflight(
        self, *, stale_seconds: int = CALLBACK_CLAIM_LEASE_SECONDS, now: Optional[str] = None
    ) -> tuple[CallbackOutboxRow, ...]:
        """Reconcile ``inflight`` rows whose claim **lease has expired** (crash recovery).

        Only a row whose ``claimed_at`` is older than ``stale_seconds`` is reclaimed — an
        actively-worked fresh claim (a concurrent processor between claim and its terminal
        mark) is left untouched, so a concurrent :meth:`recover_inflight` can never steal it and
        cause a double send (#13520 review F2). For a stale row, ``send_attempted`` disambiguates:
        ``0`` (pre-injection crash) -> reset to :data:`CALLBACK_PENDING` (a later claim retries;
        nothing was sent); ``1`` (crash after the send edge) -> :data:`CALLBACK_UNCERTAIN`, never
        auto-retried. The claim token is cleared on reclaim (a new owner will re-claim). A row
        with an empty ``claimed_at`` (a legacy pre-F2 row) is treated as stale.
        """
        stamp = now or _utc_now()
        cutoff = _utc_cutoff(stale_seconds)
        conn = self._connect_immediate()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                _SELECT + " WHERE state=? AND (claimed_at='' OR claimed_at <= ?) ORDER BY seq",
                (CALLBACK_INFLIGHT, cutoff),
            ).fetchall()
            recovered: list[CallbackOutboxRow] = []
            for r in rows:
                row = _row(r)
                new_state = CALLBACK_UNCERTAIN if row.send_attempted else CALLBACK_PENDING
                conn.execute(
                    "UPDATE callback_outbox SET state=?, claim_token='', detail=?, updated_at=? "
                    "WHERE source=? AND issue=? AND journal=? AND normalized_gate=? "
                    "AND callback_route=? AND workspace_id=?",
                    (
                        new_state,
                        "recovered stale inflight: "
                        + ("post-send uncertain" if row.send_attempted else "pre-send retry"),
                        stamp,
                        *row.key.as_row(),
                    ),
                )
                recovered.append(
                    CallbackOutboxRow(
                        **{**row.as_payload(), "state": new_state, "claim_token": ""}  # type: ignore[arg-type]
                    )
                )
            conn.execute("COMMIT")
            return tuple(recovered)
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise WorkflowRuntimeStoreError(
                f"callback recover failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- outcome marks -----------------------------------------------------

    def mark_sending(
        self,
        key: CallbackOutboxKey,
        *,
        claim_token: Optional[str] = None,
        now: Optional[str] = None,
    ) -> bool:
        """Checkpoint the send edge (``send_attempted=1``) right before injection.

        Token-conditional: returns ``False`` when the caller no longer owns the row (its claim
        was recovered + re-claimed elsewhere). The processor uses this as the **send gate** — it
        only injects when ``mark_sending`` matched, so a de-owned processor never sends. Also
        lets crash recovery tell a pre-send crash (retry) from a post-send crash (uncertain).
        """
        return self._update(
            key, "send_attempted=1", (), now=now or _utc_now(), claim_token=claim_token
        )

    def mark_delivered(
        self,
        key: CallbackOutboxKey,
        *,
        claim_token: Optional[str] = None,
        detail: str = "",
        now: Optional[str] = None,
    ) -> bool:
        """Record the claimed callback's one send as positively delivered (token-conditional)."""
        return self._update(
            key, "state=?, detail=?", (CALLBACK_DELIVERED, detail or "callback delivered"),
            now=now or _utc_now(), claim_token=claim_token,
        )

    def mark_uncertain(
        self,
        key: CallbackOutboxKey,
        *,
        claim_token: Optional[str] = None,
        detail: str = "",
        now: Optional[str] = None,
    ) -> bool:
        """Record the send outcome as unknown (ACK-only / crash-after-send); no auto-retry."""
        return self._update(
            key, "state=?, detail=?", (CALLBACK_UNCERTAIN, detail or "callback outcome uncertain"),
            now=now or _utc_now(), claim_token=claim_token,
        )

    def mark_dead_letter(
        self, key: CallbackOutboxKey, *, detail: str = "", now: Optional[str] = None
    ) -> bool:
        """Record the callback as dead-lettered (unclassified, or retries exhausted)."""
        return self._update(
            key, "state=?, detail=?", (CALLBACK_DEAD_LETTER, detail or "callback dead-lettered"),
            now=now or _utc_now(),
        )

    def mark_retry_or_dead(
        self,
        key: CallbackOutboxKey,
        *,
        claim_token: Optional[str] = None,
        detail: str = "",
        now: Optional[str] = None,
    ) -> str:
        """Record a **deterministic not-sent** failure: bump attempts, retry or dead-letter.

        If the incremented attempt count is still under ``max_attempts`` the row returns to
        :data:`CALLBACK_PENDING`; otherwise it becomes :data:`CALLBACK_DEAD_LETTER`. Returns the
        resulting state (or :data:`CALLBACK_ABSENT` if the key had no row).
        """
        stamp = now or _utc_now()
        token_clause = " AND claim_token=?" if claim_token is not None else ""
        token_params = (claim_token,) if claim_token is not None else ()
        conn = self._connect_immediate()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts, max_attempts FROM callback_outbox WHERE source=? AND "
                "issue=? AND journal=? AND normalized_gate=? AND callback_route=? AND "
                "workspace_id=?" + token_clause,
                (*key.as_row(), *token_params),
            ).fetchone()
            if row is None:
                # No row for the key, or the caller lost ownership (token mismatch) — no-op.
                conn.execute("ROLLBACK")
                return CALLBACK_ABSENT
            attempts = int(row[0]) + 1
            resulting = CALLBACK_PENDING if attempts < int(row[1]) else CALLBACK_DEAD_LETTER
            # A retry clears the claim token so a later claim re-owns it; a dead-letter clears it too.
            conn.execute(
                "UPDATE callback_outbox SET state=?, attempts=?, send_attempted=0, "
                "claim_token='', detail=?, updated_at=? WHERE source=? AND issue=? AND journal=? AND "
                "normalized_gate=? AND callback_route=? AND workspace_id=?" + token_clause,
                (
                    resulting,
                    attempts,
                    detail
                    or (
                        "retry after known-not-sent"
                        if resulting == CALLBACK_PENDING
                        else "retries exhausted; dead-lettered"
                    ),
                    stamp,
                    *key.as_row(),
                    *token_params,
                ),
            )
            conn.execute("COMMIT")
            return resulting
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise WorkflowRuntimeStoreError(
                f"callback retry/dead update failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def _update(
        self,
        key: CallbackOutboxKey,
        assignments: str,
        params: tuple,
        *,
        now: str,
        claim_token: Optional[str] = None,
    ) -> bool:
        """Token-conditional row update; returns whether a row matched.

        When ``claim_token`` is given, the update only applies to the row still owned by that
        token (``AND claim_token=?``) — a processor that lost ownership (its claim was recovered
        + re-claimed by another) gets ``rowcount == 0`` and does not transition the row, so a
        stale owner never corrupts the state or double-sends. ``None`` means unconditional
        (direct store use / single-processor tests).
        """
        token_clause = " AND claim_token=?" if claim_token is not None else ""
        token_params = (claim_token,) if claim_token is not None else ()
        conn = self._connect_immediate()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"UPDATE callback_outbox SET {assignments}, updated_at=? WHERE source=? "
                "AND issue=? AND journal=? AND normalized_gate=? AND callback_route=? "
                "AND workspace_id=?" + token_clause,
                (*params, now, *key.as_row(), *token_params),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise WorkflowRuntimeStoreError(
                f"callback update failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass

    # -- reads -------------------------------------------------------------

    def read(self, *, states: Optional[Iterable[str]] = None) -> tuple[CallbackOutboxRow, ...]:
        """Return persisted callback rows (optionally filtered by state) in ``seq`` order.

        A v1 DB with no callback table yet (never migrated) reads as empty rather than raising.
        """
        self._ensure_migrated_if_exists()
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            if not self._table_present(conn):
                return ()
            if states is not None:
                wanted = list(states)
                if not wanted:
                    return ()
                placeholders = ",".join("?" for _ in wanted)
                rows = conn.execute(
                    _SELECT + f" WHERE state IN ({placeholders}) ORDER BY seq, rowid",
                    tuple(wanted),
                ).fetchall()
            else:
                rows = conn.execute(_SELECT + " ORDER BY seq, rowid").fetchall()
        finally:
            conn.close()
        return tuple(_row(r) for r in rows)

    def state_of(self, key: CallbackOutboxKey) -> str:
        """Return the current persisted state for the key, or :data:`CALLBACK_ABSENT`.

        A read-only diagnostic used by the processor to report the **actual** durable state when
        a terminal mark no-ops (the owner lost the lease and another processor reconciled the
        row): the report must match the persisted state, not the intended one (#13520 review F2-R1).
        """
        self._ensure_migrated_if_exists()
        conn = self._connect_ro()
        if conn is None:
            return CALLBACK_ABSENT
        try:
            if not self._table_present(conn):
                return CALLBACK_ABSENT
            row = conn.execute(
                "SELECT state FROM callback_outbox WHERE source=? AND issue=? AND journal=? "
                "AND normalized_gate=? AND callback_route=? AND workspace_id=?",
                key.as_row(),
            ).fetchone()
            return str(row[0]) if row is not None else CALLBACK_ABSENT
        finally:
            conn.close()

    def read_cursor(self, source: str) -> Optional[str]:
        """Return the persisted cursor token for ``source``, or ``None`` if unset."""
        conn = self._connect_ro()
        if conn is None:
            return None
        try:
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='callback_cursor'"
            ).fetchone()
            if has is None:
                return None
            cur = conn.execute(
                "SELECT cursor FROM callback_cursor WHERE source=?", (str(source),)
            ).fetchone()
            return str(cur[0]) if cur is not None else None
        finally:
            conn.close()


__all__ = (
    "CallbackOutboxKey",
    "CallbackOutboxRow",
    "CallbackEnqueueResult",
    "CallbackOutbox",
)
