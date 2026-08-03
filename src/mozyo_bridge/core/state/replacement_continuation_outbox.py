"""Durable exactly-once reservation for replacement continuation sends (#14741).

The outbox and ``replacement_transactions`` share ``state.sqlite``.  A reserve therefore
validates the exact generation, continuation marker, phase, holder, and live lease under the
same ``BEGIN IMMEDIATE`` lock that inserts the unique send right.  Only the returned owner token
may resolve or release that right.

``consume_reserved`` is deliberately stronger than a generic outbox executor: it keeps the exact
replacement action's advisory fence through the transport call while limiting global SQLite
writer locks to short validation/outcome transactions.  Only authority refusal before the call
or the rail's typed zero-injection outcome atomically deletes the reservation and reverts
``draining_continuation -> replacing_nonself`` while the holder still has a live lease.  No public
post-effect release surface exists.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mozyo_bridge.core.state.replacement_continuation_outbox_schema import (
    ReplacementContinuationOutboxError,
    TABLE,
    ensure_replacement_continuation_outbox_schema,
)
from mozyo_bridge.core.state.replacement_transaction_action_fence import (
    ReplacementTransactionActionFenceError,
    replacement_transaction_action_fence,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
    ReplacementTransactionKey,
    norm,
)
from mozyo_bridge.core.state.replacement_transaction_rows import _locked_row
from mozyo_bridge.core.state.replacement_transaction_schema import TABLE as TRANSACTION_TABLE


OUTBOX_RESERVED = "reserved"
OUTBOX_DELIVERED = "delivered"
OUTBOX_UNCERTAIN = "uncertain"
OUTBOX_CANCELLED = "cancelled"
OUTBOX_STATES = frozenset(
    {OUTBOX_RESERVED, OUTBOX_DELIVERED, OUTBOX_UNCERTAIN, OUTBOX_CANCELLED}
)

RESERVE_GRANTED = "granted"
RESERVE_HELD = "held"
RESERVE_COMPLETED = "completed"
RESERVE_NOT_FOUND = "not_found"
RESERVE_GENERATION_MISMATCH = "generation_mismatch"
RESERVE_LEASE_LOST = "lease_lost"
RESERVE_WRONG_PHASE = "wrong_phase"
RESERVE_POINTER_MISMATCH = "pointer_mismatch"
CONSUME_AUTHORITY_MOVED = "authority_moved"
CONSUME_ZERO_SEND = "zero_send"
CONSUME_DELIVERED = "delivered"
CONSUME_UNCERTAIN = "uncertain"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ContinuationSendKey:
    """The exact continuation effect identity, including its action generation."""

    workspace_id: str
    action_id: str
    action_generation: int
    source: str
    issue_id: str
    journal_id: str
    expected_gate: str
    next_action: str

    def __post_init__(self) -> None:
        for field in (
            "workspace_id",
            "action_id",
            "source",
            "issue_id",
            "journal_id",
            "expected_gate",
            "next_action",
        ):
            object.__setattr__(self, field, norm(getattr(self, field)))
        if not isinstance(self.action_generation, int) or isinstance(
            self.action_generation, bool
        ):
            raise ValueError("action_generation must be an exact integer")
        if self.action_generation < 1 or not all(
            (
                self.workspace_id,
                self.action_id,
                self.source,
                self.issue_id,
                self.journal_id,
                self.expected_gate,
                self.next_action,
            )
        ):
            raise ValueError("a continuation send key requires every exact identity axis")

    @classmethod
    def from_record(cls, record, *, action_generation: int) -> "ContinuationSendKey":
        return cls(
            workspace_id=record.workspace_id,
            action_id=record.action_id,
            action_generation=action_generation,
            source=record.continuation_source,
            issue_id=record.continuation_issue_id,
            journal_id=record.continuation_journal,
            expected_gate=record.continuation_expected_gate,
            next_action=record.continuation_next_action,
        )

    def as_row(self) -> tuple[object, ...]:
        return (
            self.workspace_id,
            self.action_id,
            self.action_generation,
            self.source,
            self.issue_id,
            self.journal_id,
            self.expected_gate,
            self.next_action,
        )


@dataclass(frozen=True)
class ContinuationReservation:
    disposition: str
    token: str = ""
    state: str = ""

    @property
    def granted(self) -> bool:
        return self.disposition == RESERVE_GRANTED


@dataclass(frozen=True)
class ContinuationConsumption:
    """The transport-coupled consumption of one already-durable reservation."""

    disposition: str
    state: str = ""


class ReplacementContinuationOutbox:
    """Native state-store component for one non-reclaimable continuation send right."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        ensure_replacement_continuation_outbox_schema(self.path)
        try:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.execute("PRAGMA busy_timeout = 2000")
            return conn
        except sqlite3.DatabaseError as exc:
            raise ReplacementContinuationOutboxError(
                f"continuation outbox {self.path} is unreadable; fail closed"
            ) from exc

    @staticmethod
    def _record_disposition(record, send_key: ContinuationSendKey, holder: str, now: str) -> str:
        if record is None:
            return RESERVE_NOT_FOUND
        if record.action_generation != send_key.action_generation:
            return RESERVE_GENERATION_MISMATCH
        if record.phase == PHASE_COMPLETED:
            return RESERVE_COMPLETED
        expected_pointer = (
            record.continuation_source,
            record.continuation_issue_id,
            record.continuation_journal,
            record.continuation_expected_gate,
            record.continuation_next_action,
        )
        if expected_pointer != send_key.as_row()[3:]:
            return RESERVE_POINTER_MISMATCH
        if record.phase != PHASE_DRAINING_CONTINUATION:
            return RESERVE_WRONG_PHASE
        if not norm(holder) or record.lease_holder != norm(holder) or not record.lease_is_live(now):
            return RESERVE_LEASE_LOST
        return RESERVE_GRANTED

    def reserve(
        self,
        send_key: ContinuationSendKey,
        *,
        holder: str,
        now: str | None = None,
    ) -> ContinuationReservation:
        """Atomically validate the transaction and reserve its one continuation send."""

        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            transaction_key = ReplacementTransactionKey(
                send_key.workspace_id, send_key.action_id
            )
            record = _locked_row(conn, transaction_key)
            disposition = self._record_disposition(record, send_key, holder, stamp)
            if disposition != RESERVE_GRANTED:
                conn.execute("ROLLBACK")
                return ContinuationReservation(disposition)
            row = conn.execute(
                f"SELECT state FROM {TABLE} WHERE workspace_id=? AND action_id=? AND "
                "action_generation=? AND source=? AND issue_id=? AND journal_id=? AND "
                "expected_gate=? AND next_action=?",
                send_key.as_row(),
            ).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                return ContinuationReservation(RESERVE_HELD, state=str(row[0]))
            token = secrets.token_hex(16)
            conn.execute(
                f"INSERT INTO {TABLE} (workspace_id, action_id, action_generation, source, "
                "issue_id, journal_id, expected_gate, next_action, state, owner_token, detail, "
                "reserved_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                (*send_key.as_row(), OUTBOX_RESERVED, token, stamp, stamp),
            )
            conn.execute("COMMIT")
            return ContinuationReservation(
                RESERVE_GRANTED, token=token, state=OUTBOX_RESERVED
            )
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ReplacementContinuationOutboxError(
                f"continuation send reserve failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    @staticmethod
    def _owned_reserved(conn, send_key: ContinuationSendKey, token: str):
        return conn.execute(
            f"SELECT state, owner_token FROM {TABLE} WHERE workspace_id=? AND action_id=? "
            "AND action_generation=? AND source=? AND issue_id=? AND journal_id=? AND "
            "expected_gate=? AND next_action=?",
            send_key.as_row(),
        ).fetchone()

    @staticmethod
    def _set_owned_state_locked(
        conn,
        send_key: ContinuationSendKey,
        token: str,
        *,
        state: str,
        detail: str,
        stamp: str,
    ) -> bool:
        cur = conn.execute(
            f"UPDATE {TABLE} SET state=?, detail=?, updated_at=? WHERE workspace_id=? "
            "AND action_id=? AND action_generation=? AND source=? AND issue_id=? AND "
            "journal_id=? AND expected_gate=? AND next_action=? AND owner_token=? AND state=?",
            (
                state,
                detail,
                stamp,
                *send_key.as_row(),
                norm(token),
                OUTBOX_RESERVED,
            ),
        )
        return cur.rowcount == 1

    @staticmethod
    def _release_owned_locked(
        conn,
        send_key: ContinuationSendKey,
        token: str,
        record,
        *,
        holder: str,
        stamp: str,
    ) -> bool:
        revision = record.revision + 1
        updated = conn.execute(
            f"UPDATE {TRANSACTION_TABLE} SET phase=?, revision=?, updated_at=? "
            "WHERE workspace_id=? AND action_id=? AND revision=? AND action_generation=? "
            "AND phase=? AND lease_holder=?",
            (
                PHASE_REPLACING_NONSELF,
                revision,
                stamp,
                send_key.workspace_id,
                send_key.action_id,
                record.revision,
                send_key.action_generation,
                PHASE_DRAINING_CONTINUATION,
                norm(holder),
            ),
        )
        deleted = conn.execute(
            f"DELETE FROM {TABLE} WHERE workspace_id=? AND action_id=? AND "
            "action_generation=? AND source=? AND issue_id=? AND journal_id=? AND "
            "expected_gate=? AND next_action=? AND owner_token=? AND state=?",
            (*send_key.as_row(), norm(token), OUTBOX_RESERVED),
        )
        return updated.rowcount == 1 and deleted.rowcount == 1

    def consume_reserved(
        self,
        send_key: ContinuationSendKey,
        token: str,
        *,
        holder: str,
        clock,
        authority_fn,
        send_fn,
        send_ok,
        send_zero,
    ) -> ContinuationConsumption:
        """Validate, authority-rejoin, and send while completion writers are excluded.

        The reservation was committed by :meth:`reserve` before this method starts.  Therefore
        a crash anywhere in this critical section leaves a durable ``reserved`` never-resend
        row.  The exact action's advisory fence excludes its replacement-transaction writers
        through the actual effect; the shared ``state.sqlite`` writer lock is held only for the
        short validation and outcome updates, so another action is not blocked by transport.
        """

        try:
            transaction_key = ReplacementTransactionKey(
                send_key.workspace_id, send_key.action_id
            )
            with replacement_transaction_action_fence(self.path, transaction_key):
                return self._consume_reserved_fenced(
                    send_key,
                    token,
                    transaction_key=transaction_key,
                    holder=holder,
                    clock=clock,
                    authority_fn=authority_fn,
                    send_fn=send_fn,
                    send_ok=send_ok,
                    send_zero=send_zero,
                )
        except ReplacementTransactionActionFenceError as exc:
            raise ReplacementContinuationOutboxError(
                "continuation action fence unavailable; fail closed"
            ) from exc

    @staticmethod
    def _clock_stamp(clock) -> str:
        try:
            stamp = clock()
        except (Exception, SystemExit):
            return ""
        return stamp if type(stamp) is str and stamp else ""

    def _consume_reserved_fenced(
        self,
        send_key: ContinuationSendKey,
        token: str,
        *,
        transaction_key: ReplacementTransactionKey,
        holder: str,
        clock,
        authority_fn,
        send_fn,
        send_ok,
        send_zero,
    ) -> ContinuationConsumption:
        """Consume with the exact action fence already held by this thread."""

        # Initial owner + transaction check.  This is deliberately short: authority observation
        # and transport happen after COMMIT under the per-action fence, not under the global DB
        # writer lock.
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._owned_reserved(conn, send_key, token)
            if row is None or str(row[0]) != OUTBOX_RESERVED or str(row[1]) != norm(token):
                conn.execute("ROLLBACK")
                return ContinuationConsumption(
                    RESERVE_HELD, state=str(row[0]) if row is not None else ""
                )
            stamp = self._clock_stamp(clock)
            if not stamp:
                conn.execute("ROLLBACK")
                return ContinuationConsumption(RESERVE_LEASE_LOST, state=OUTBOX_RESERVED)
            record = _locked_row(conn, transaction_key)
            disposition = self._record_disposition(record, send_key, holder, stamp)
            if disposition == RESERVE_COMPLETED:
                if not self._set_owned_state_locked(
                    conn,
                    send_key,
                    token,
                    state=OUTBOX_CANCELLED,
                    detail="transaction completed before reserved transport emission",
                    stamp=stamp,
                ):
                    conn.execute("ROLLBACK")
                    return ContinuationConsumption(RESERVE_HELD, state=OUTBOX_RESERVED)
                conn.execute("COMMIT")
                return ContinuationConsumption(RESERVE_COMPLETED, state=OUTBOX_CANCELLED)
            if disposition != RESERVE_GRANTED:
                conn.execute("ROLLBACK")
                return ContinuationConsumption(disposition, state=OUTBOX_RESERVED)
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ReplacementContinuationOutboxError(
                f"continuation initial validation failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

        try:
            authorized = bool(authority_fn())
        except (Exception, SystemExit):
            authorized = False

        # R11-F1: authority observation can consume the remainder of the lease.  Re-read the
        # clock and the locked row after it, immediately before any transport invocation.
        effect_stamp = self._clock_stamp(clock)
        if not effect_stamp:
            return ContinuationConsumption(RESERVE_LEASE_LOST, state=OUTBOX_RESERVED)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._owned_reserved(conn, send_key, token)
            if row is None or str(row[0]) != OUTBOX_RESERVED or str(row[1]) != norm(token):
                conn.execute("ROLLBACK")
                return ContinuationConsumption(
                    RESERVE_HELD, state=str(row[0]) if row is not None else ""
                )
            record = _locked_row(conn, transaction_key)
            disposition = self._record_disposition(
                record, send_key, holder, effect_stamp
            )
            if disposition != RESERVE_GRANTED:
                conn.execute("ROLLBACK")
                return ContinuationConsumption(disposition, state=OUTBOX_RESERVED)
            if not authorized:
                if not self._release_owned_locked(
                    conn,
                    send_key,
                    token,
                    record,
                    holder=holder,
                    stamp=effect_stamp,
                ):
                    conn.execute("ROLLBACK")
                    return ContinuationConsumption(RESERVE_HELD, state=OUTBOX_RESERVED)
                conn.execute("COMMIT")
                return ContinuationConsumption(CONSUME_AUTHORITY_MOVED)
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ReplacementContinuationOutboxError(
                f"continuation final validation failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

        try:
            sent = send_fn()
        except (Exception, SystemExit):
            sent = None

        if type(sent) is type(send_zero) and sent == send_zero:
            # A typed zero-send is revertible only while this holder still owns a live lease.
            # A stale holder has no transaction mutation authority even when the rail proved
            # that no effect occurred.
            zero_stamp = self._clock_stamp(clock)
            if not zero_stamp:
                return ContinuationConsumption(RESERVE_LEASE_LOST, state=OUTBOX_RESERVED)
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._owned_reserved(conn, send_key, token)
                record = _locked_row(conn, transaction_key)
                disposition = self._record_disposition(
                    record, send_key, holder, zero_stamp
                )
                if (
                    row is None
                    or str(row[0]) != OUTBOX_RESERVED
                    or str(row[1]) != norm(token)
                    or disposition != RESERVE_GRANTED
                ):
                    conn.execute("ROLLBACK")
                    return ContinuationConsumption(
                        disposition if disposition != RESERVE_GRANTED else RESERVE_HELD,
                        state=str(row[0]) if row is not None else OUTBOX_RESERVED,
                    )
                if not self._release_owned_locked(
                    conn,
                    send_key,
                    token,
                    record,
                    holder=holder,
                    stamp=zero_stamp,
                ):
                    conn.execute("ROLLBACK")
                    return ContinuationConsumption(RESERVE_HELD, state=OUTBOX_RESERVED)
                conn.execute("COMMIT")
                return ContinuationConsumption(CONSUME_ZERO_SEND)
            except sqlite3.DatabaseError as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise ReplacementContinuationOutboxError(
                    f"continuation zero-send release failed ({type(exc).__name__}); fail closed"
                ) from exc
            finally:
                conn.close()

        state = (
            OUTBOX_DELIVERED
            if type(sent) is type(send_ok) and sent == send_ok
            else OUTBOX_UNCERTAIN
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not self._set_owned_state_locked(
                conn,
                send_key,
                token,
                state=state,
                detail=(
                    "continuation transport reported a positive send"
                    if state == OUTBOX_DELIVERED
                    else "continuation transport outcome unknown"
                ),
                stamp=effect_stamp,
            ):
                conn.execute("ROLLBACK")
                return ContinuationConsumption(RESERVE_HELD, state=OUTBOX_RESERVED)
            conn.execute("COMMIT")
            return ContinuationConsumption(
                CONSUME_DELIVERED if state == OUTBOX_DELIVERED else CONSUME_UNCERTAIN,
                state=state,
            )
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ReplacementContinuationOutboxError(
                f"continuation outcome write failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def state_of(self, send_key: ContinuationSendKey) -> str:
        """Read one exact outbox state for diagnostics/tests."""

        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT state FROM {TABLE} WHERE workspace_id=? AND action_id=? AND "
                "action_generation=? AND source=? AND issue_id=? AND journal_id=? AND "
                "expected_gate=? AND next_action=?",
                send_key.as_row(),
            ).fetchone()
            return str(row[0]) if row is not None else ""
        finally:
            conn.close()


__all__ = (
    "OUTBOX_RESERVED",
    "OUTBOX_DELIVERED",
    "OUTBOX_UNCERTAIN",
    "OUTBOX_CANCELLED",
    "CONSUME_AUTHORITY_MOVED",
    "CONSUME_ZERO_SEND",
    "CONSUME_DELIVERED",
    "CONSUME_UNCERTAIN",
    "RESERVE_GRANTED",
    "RESERVE_HELD",
    "RESERVE_COMPLETED",
    "RESERVE_NOT_FOUND",
    "RESERVE_GENERATION_MISMATCH",
    "RESERVE_LEASE_LOST",
    "RESERVE_WRONG_PHASE",
    "RESERVE_POINTER_MISMATCH",
    "ContinuationSendKey",
    "ContinuationReservation",
    "ContinuationConsumption",
    "ReplacementContinuationOutbox",
    "ReplacementContinuationOutboxError",
)
