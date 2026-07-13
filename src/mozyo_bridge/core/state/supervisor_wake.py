"""Durable local-wake queue for the workspace callback supervisor (Redmine #13683 Phase A, R1-F2).

The design (j#77065 / j#76624) makes the **primary** supervisor trigger a *local wake* a
mozyo-originated durable gate/handoff commit emits, with bounded reconciliation as loss recovery —
polling is the supplement, not the only control surface. This module is the durable substrate for
that primary path: a small home-scoped queue the **canonical gate writer** enqueues a
``(workspace_id, issue)`` wake into after it records a gate, and the **supervisor** drains as its
local-wake work list.

Design (mirrors the sibling home-scoped fence / lease stores):

- **coalesced** — the primary key is ``(workspace_id, issue)``, so re-emitting a wake for the same
  workspace+issue before the supervisor drains it collapses to one row (a burst of gate commits on
  one issue is one unit of work, not N). ``enqueued_at`` is refreshed to the latest emit.
- **atomic drain** — :meth:`drain` claims + deletes the pending rows under ``BEGIN IMMEDIATE`` and
  returns them, so a concurrent drainer never double-processes the same wake (the supervisor lease
  already fences per workspace, but the queue itself is single-consumer-safe).
- **best-effort producer** — the emit path treats a wake enqueue as best-effort: a wake loss is
  recovered by the supervisor's bounded reconciliation (the whole roster is re-read), so the gate
  writer never fails because the wake queue was unavailable (the caller wraps the enqueue).
- **fail-closed schema** — an unrecognized ``PRAGMA user_version`` fails closed rather than
  rewriting a newer table.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-scoped SQLite file holding pending supervisor local wakes.
SUPERVISOR_WAKE_FILENAME = "supervisor-wake.sqlite"

#: Schema version stamped into ``PRAGMA user_version``. Unrecognized -> fail closed.
SUPERVISOR_WAKE_SCHEMA_VERSION = 1

_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1})

_WAKE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_wake (
    workspace_id TEXT NOT NULL,
    issue        TEXT NOT NULL,
    enqueued_at  TEXT NOT NULL,
    PRIMARY KEY (workspace_id, issue)
)
"""


class SupervisorWakeError(RuntimeError):
    """The supervisor-wake DB could not be opened at a recognized schema (fail-closed)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def supervisor_wake_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``supervisor-wake.sqlite`` path under the mozyo-bridge home."""
    return (home or mozyo_bridge_home()) / SUPERVISOR_WAKE_FILENAME


@dataclass(frozen=True)
class WakeHint:
    """A pending local wake: which workspace's which issue a gate commit signalled."""

    workspace_id: str
    issue: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.workspace_id, self.issue)


class SupervisorWakeStore:
    """Coalesced, atomically-drained local-wake queue in the home-scoped wake DB.

    Construction never touches the filesystem; the DB is created lazily on the first enqueue.
    Reads / drains on an absent DB are empty (the normal pre-write state); an existing DB with an
    unrecognized container version fails closed.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else supervisor_wake_path(home)

    def _connect_rw(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            conn.execute(_WAKE_TABLE_SQL)
            conn.execute(f"PRAGMA user_version = {SUPERVISOR_WAKE_SCHEMA_VERSION}")
        elif version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise SupervisorWakeError(
                f"supervisor wake store {self.path} has unsupported schema version {version}; "
                f"this build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. Left untouched."
            )
        else:
            conn.execute(_WAKE_TABLE_SQL)  # self-heal a table lost under a valid version
        return conn

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            conn.close()
            raise SupervisorWakeError(
                f"supervisor wake store {self.path} is unreadable: {exc}"
            ) from exc
        if version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise SupervisorWakeError(
                f"supervisor wake store {self.path} has unsupported schema version {version}."
            )
        return conn

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass

    def enqueue(self, workspace_id: str, issue: str, *, now: Optional[str] = None) -> bool:
        """Coalesced-enqueue a local wake for ``(workspace_id, issue)``; returns whether it stuck.

        A blank workspace / issue is a no-op (returns False) — a wake with no durable anchor cannot
        be routed. Re-enqueuing the same pair before a drain updates ``enqueued_at`` and adds no
        row (the PRIMARY KEY coalesces).
        """
        ws = str(workspace_id or "").strip()
        iss = str(issue or "").strip()
        if not ws or not iss:
            return False
        stamp = now or _utc_now_iso()
        conn = self._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO supervisor_wake (workspace_id, issue, enqueued_at) VALUES (?, ?, ?) "
                "ON CONFLICT(workspace_id, issue) DO UPDATE SET enqueued_at=excluded.enqueued_at",
                (ws, iss, stamp),
            )
            conn.execute("COMMIT")
            return True
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise SupervisorWakeError(
                f"supervisor wake enqueue failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def drain(self, *, workspace_id: Optional[str] = None) -> tuple[WakeHint, ...]:
        """Atomically claim + delete the pending wakes and return them (single-consumer-safe).

        ``workspace_id`` (optional) drains only that workspace's wakes. Under ``BEGIN IMMEDIATE``
        the rows are read then deleted in one transaction, so a concurrent drainer sees an empty
        queue — the drained wakes are handed to exactly one supervisor pass. An absent DB drains
        empty.
        """
        if not self.path.exists():
            return ()
        conn = self._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if workspace_id is None:
                rows = conn.execute(
                    "SELECT workspace_id, issue FROM supervisor_wake ORDER BY enqueued_at, rowid"
                ).fetchall()
                conn.execute("DELETE FROM supervisor_wake")
            else:
                ws = str(workspace_id).strip()
                rows = conn.execute(
                    "SELECT workspace_id, issue FROM supervisor_wake WHERE workspace_id=? "
                    "ORDER BY enqueued_at, rowid",
                    (ws,),
                ).fetchall()
                conn.execute("DELETE FROM supervisor_wake WHERE workspace_id=?", (ws,))
            conn.execute("COMMIT")
            return tuple(WakeHint(workspace_id=r[0], issue=r[1]) for r in rows)
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise SupervisorWakeError(
                f"supervisor wake drain failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def pending(self) -> tuple[WakeHint, ...]:
        """Return the pending wakes without consuming them (read-only; empty if absent)."""
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='supervisor_wake'"
            ).fetchone()
            if has is None:
                return ()
            rows = conn.execute(
                "SELECT workspace_id, issue FROM supervisor_wake ORDER BY enqueued_at, rowid"
            ).fetchall()
        finally:
            conn.close()
        return tuple(WakeHint(workspace_id=r[0], issue=r[1]) for r in rows)


__all__ = (
    "SUPERVISOR_WAKE_FILENAME",
    "SUPERVISOR_WAKE_SCHEMA_VERSION",
    "SupervisorWakeError",
    "WakeHint",
    "SupervisorWakeStore",
    "supervisor_wake_path",
)
