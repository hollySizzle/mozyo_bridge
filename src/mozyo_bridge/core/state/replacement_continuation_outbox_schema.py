"""Schema guard for the replacement-continuation send outbox (Redmine #14741).

The continuation send reservation is a native component of the consolidated
``state.sqlite``.  Keeping it in the same SQLite container as
``replacement_transactions`` lets the send rail validate the exact transaction and reserve
the exact continuation under one ``BEGIN IMMEDIATE`` lock.  A table without its component
metadata, an unknown component version, or a non-exact table shape fails closed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mozyo_bridge.core.state.state_store import (
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)


REPLACEMENT_CONTINUATION_OUTBOX_COMPONENT = "replacement_continuation_outbox"
REPLACEMENT_CONTINUATION_OUTBOX_SCHEMA_VERSION = 1
REPLACEMENT_CONTINUATION_OUTBOX_RECOVERY_POLICY = "operator_current_state"
TABLE = "replacement_continuation_outbox"

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    workspace_id      TEXT NOT NULL,
    action_id         TEXT NOT NULL,
    action_generation INTEGER NOT NULL,
    source            TEXT NOT NULL,
    issue_id          TEXT NOT NULL,
    journal_id        TEXT NOT NULL,
    expected_gate     TEXT NOT NULL,
    next_action       TEXT NOT NULL,
    state             TEXT NOT NULL,
    owner_token       TEXT NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    reserved_at       TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (
        workspace_id, action_id, action_generation, source, issue_id, journal_id,
        expected_gate, next_action
    )
)
"""

_COLUMN_DEFS = {
    "workspace_id": ("TEXT", 1, None, 1),
    "action_id": ("TEXT", 1, None, 2),
    "action_generation": ("INTEGER", 1, None, 3),
    "source": ("TEXT", 1, None, 4),
    "issue_id": ("TEXT", 1, None, 5),
    "journal_id": ("TEXT", 1, None, 6),
    "expected_gate": ("TEXT", 1, None, 7),
    "next_action": ("TEXT", 1, None, 8),
    "state": ("TEXT", 1, None, 0),
    "owner_token": ("TEXT", 1, None, 0),
    "detail": ("TEXT", 1, "''", 0),
    "reserved_at": ("TEXT", 1, None, 0),
    "updated_at": ("TEXT", 1, None, 0),
}


class ReplacementContinuationOutboxError(RuntimeError):
    """The continuation outbox is unavailable; no continuation may be sent."""


def replacement_continuation_outbox_path(home: Path | None = None) -> Path:
    """Return the consolidated state container used by this component."""

    return state_store_path(home)


def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _recorded_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT typeof(schema_version), schema_version FROM state_schema_components "
        "WHERE component=?",
        (REPLACEMENT_CONTINUATION_OUTBOX_COMPONENT,),
    ).fetchone()
    if row is None:
        return None
    storage_class, value = row
    if (
        storage_class != "integer"
        or not isinstance(value, int)
        or isinstance(value, bool)
    ):
        return -1
    return value


def _shape_matches(conn: sqlite3.Connection) -> bool:
    if not _table_present(conn, TABLE):
        return False
    actual = {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute(f"PRAGMA table_info({TABLE})")
    }
    return actual == _COLUMN_DEFS


def ensure_replacement_continuation_outbox_schema(path: Path) -> None:
    """Create a fresh component or validate its exact registered v1 shape."""

    try:
        conn = connect_state_container_rw(path)
    except (StateStoreError, sqlite3.DatabaseError) as exc:
        raise ReplacementContinuationOutboxError(
            f"continuation outbox container {path} is unavailable; fail closed"
        ) from exc
    conn.isolation_level = None
    locked = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked = True
        recorded = _recorded_version(conn)
        table_present = _table_present(conn, TABLE)
        if recorded is None:
            if table_present:
                raise ReplacementContinuationOutboxError(
                    "continuation outbox table exists without component metadata; "
                    "fail closed (no silent adoption)"
                )
            conn.execute(TABLE_SQL)
            conn.execute(
                "INSERT INTO state_schema_components "
                "(component, schema_version, owner, recovery_policy, migrated_from, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
                (
                    REPLACEMENT_CONTINUATION_OUTBOX_COMPONENT,
                    REPLACEMENT_CONTINUATION_OUTBOX_SCHEMA_VERSION,
                    "core/state/replacement_continuation_outbox.py",
                    REPLACEMENT_CONTINUATION_OUTBOX_RECOVERY_POLICY,
                ),
            )
        elif recorded != REPLACEMENT_CONTINUATION_OUTBOX_SCHEMA_VERSION:
            raise ReplacementContinuationOutboxError(
                f"continuation outbox component records unsupported version {recorded}; "
                "the store is left untouched"
            )
        elif not _shape_matches(conn):
            raise ReplacementContinuationOutboxError(
                "continuation outbox table does not match its exact registered v1 shape; "
                "fail closed (no silent repair)"
            )
        conn.execute("COMMIT")
        locked = False
    except sqlite3.DatabaseError as exc:
        if locked:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
        raise ReplacementContinuationOutboxError(
            f"continuation outbox schema initialization failed ({type(exc).__name__}); "
            "fail closed"
        ) from exc
    except ReplacementContinuationOutboxError:
        if locked:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
        raise
    finally:
        conn.close()


__all__ = (
    "REPLACEMENT_CONTINUATION_OUTBOX_COMPONENT",
    "REPLACEMENT_CONTINUATION_OUTBOX_SCHEMA_VERSION",
    "REPLACEMENT_CONTINUATION_OUTBOX_RECOVERY_POLICY",
    "ReplacementContinuationOutboxError",
    "TABLE",
    "ensure_replacement_continuation_outbox_schema",
    "replacement_continuation_outbox_path",
)
