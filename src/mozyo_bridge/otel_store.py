"""OTel event store: SQLite sink for agent telemetry (Redmine #11639 / #11672).

Phase 1 of the unit-state-detection US. Claude Code / Codex CLI / Gemini CLI
emit OpenTelemetry directly; a small self-built OTLP/HTTP receiver
(``application/otel_receiver.py``) normalizes the payloads and this module
persists them. Design constraints from the owner decision (#11639 journal
#56088):

- **Best-effort, never the source of truth.** OTLP is push-based: events
  sent while the receiver is down are lost, and that is accepted. The
  store answers "is this unit actively emitting?"; liveness is the tmux
  layer's job (``agents list`` / session inventory) and workflow state is
  Redmine's. Consumers must degrade to the tmux layer when the store is
  silent — silence here distinguishes nothing between "waiting for input"
  and "dead".
- **SQLite single-writer.** The receiver process is the only writer (the
  HTTP server is single-threaded by construction). No Postgres, no message
  queue, no generic pipeline — a dozen agents in a two-person org do not
  need them.
- **No prompt bodies, ever.** Only usage numbers, event kinds, and minimal
  identity metadata are stored. Attribute filtering is allowlist-based and
  a deny list (prompt / content / message / ...) wins over the allowlist,
  so a prompt-content opt-in path cannot be created by accident. Log
  record bodies are never persisted.

The store lives at ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite``
— a durable cache in the same sense as ``inventory.sqlite``: losing it
loses history, not identity, and the next events rebuild it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from mozyo_bridge.shared.paths import mozyo_bridge_home

OTEL_STORE_FILENAME = "otel-events.sqlite"
OTEL_STORE_SCHEMA_VERSION = 1

# Default retention. Activity / idle judgement needs minutes of history;
# a week is ample for debugging without growing the cache unboundedly.
DEFAULT_RETENTION_DAYS = 7

# Attribute keys that may be persisted (exact match, case-insensitive).
# Identity / usage / event-kind metadata only. Anything not listed is
# dropped; anything matching a deny token is dropped even if listed.
ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "session.id",
        "event.name",
        "event.timestamp",
        "model",
        "app.version",
        "terminal.type",
        "cwd",
        "workspace.dir",
        "tool_name",
        "tool.name",
        "decision",
        "source",
        "success",
        "duration_ms",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "total_tokens",
        "token.type",
        "type",
        "language",
        "service.name",
        "service.version",
        "process.pid",
        "host.arch",
        "os.type",
    }
)

# Deny tokens checked as substrings of the lower-cased key. Deny beats
# allow: even a future allowlist mistake cannot persist prompt-shaped or
# credential-shaped payloads.
DENIED_KEY_TOKENS = (
    "prompt",
    "content",
    "message",
    "text",
    "body",
    "input_value",
    "completion",
    "api_key",
    "apikey",
    "token_value",
    "authorization",
    "secret",
    "password",
    "email",
    "user.account",
)

_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS otel_events (
    id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    event_time TEXT,
    signal TEXT NOT NULL,
    event_name TEXT NOT NULL,
    service_name TEXT,
    session_id TEXT,
    pid TEXT,
    cwd TEXT,
    attrs_json TEXT NOT NULL
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS otel_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_otel_events_received "
    "ON otel_events(received_at)",
    "CREATE INDEX IF NOT EXISTS idx_otel_events_source "
    "ON otel_events(service_name, session_id, received_at)",
)

_EVENT_COLUMNS = (
    "received_at, event_time, signal, event_name, service_name, "
    "session_id, pid, cwd, attrs_json"
)


def otel_store_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / OTEL_STORE_FILENAME


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def filter_attributes(attrs: dict) -> dict:
    """Reduce raw attributes to the persistable allowlisted subset.

    Deny tokens win over the allowlist; values must be scalars (nested
    structures are dropped — they are where free-form content hides).
    """
    out: dict = {}
    for key, value in attrs.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if any(token in lowered for token in DENIED_KEY_TOKENS):
            continue
        if lowered not in ALLOWED_ATTRIBUTE_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[lowered] = value
    return out


@dataclass(frozen=True)
class OtelEvent:
    """One normalized telemetry event as persisted in the store."""

    signal: str
    event_name: str
    event_time: str | None = None
    service_name: str | None = None
    session_id: str | None = None
    pid: str | None = None
    cwd: str | None = None
    attrs: dict = field(default_factory=dict)
    received_at: str | None = None

    def as_payload(self) -> dict:
        return {
            "received_at": self.received_at,
            "event_time": self.event_time,
            "signal": self.signal,
            "event_name": self.event_name,
            "service_name": self.service_name,
            "session_id": self.session_id,
            "pid": self.pid,
            "cwd": self.cwd,
            "attrs": self.attrs,
        }


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 2000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(_EVENTS_TABLE_SQL)
        conn.execute(_META_TABLE_SQL)
        for sql in _INDEX_SQL:
            conn.execute(sql)
        conn.execute(f"PRAGMA user_version = {OTEL_STORE_SCHEMA_VERSION}")
        conn.commit()
    elif version != OTEL_STORE_SCHEMA_VERSION:
        conn.close()
        raise OtelStoreError(
            f"otel event store {path} has schema version {version}; this "
            f"mozyo-bridge supports {OTEL_STORE_SCHEMA_VERSION}. The store "
            "is a regenerable cache: move it aside and restart the receiver."
        )
    return conn


class OtelStoreError(RuntimeError):
    """User-actionable store error (schema mismatch, corrupt file)."""


class OtelEventStore:
    """Single-writer event sink plus read API.

    The receiver owns the only long-lived writing instance; CLI reads use
    short-lived read-only connections so they never block the writer.
    """

    def __init__(self, path: Path | None = None, *, home: Path | None = None):
        self.path = path or otel_store_path(home)
        self._conn: sqlite3.Connection | None = None

    # -- write side (receiver only) ---------------------------------------

    def _writer(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self._conn = _connect(self.path)
            except sqlite3.DatabaseError as exc:
                raise OtelStoreError(
                    f"otel event store {self.path} is unreadable ({exc}). "
                    "It is a regenerable cache: move the corrupt file aside "
                    "and restart the receiver."
                ) from exc
        return self._conn

    def insert_events(self, events: Iterable[OtelEvent]) -> int:
        rows = []
        now = utc_now_iso()
        for event in events:
            rows.append(
                (
                    event.received_at or now,
                    event.event_time,
                    event.signal,
                    event.event_name,
                    event.service_name,
                    event.session_id,
                    event.pid,
                    event.cwd,
                    json.dumps(event.attrs, ensure_ascii=False, sort_keys=True),
                )
            )
        if not rows:
            return 0
        conn = self._writer()
        with conn:
            conn.executemany(
                f"INSERT INTO otel_events ({_EVENT_COLUMNS}) "
                f"VALUES ({', '.join('?' * 9)})",
                rows,
            )
            conn.execute(
                "INSERT INTO otel_meta (key, value) VALUES ('last_write', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (now,),
            )
        return len(rows)

    def prune(self, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """Delete events older than the retention window. Returns count."""
        conn = self._writer()
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(
            timespec="seconds"
        )
        with conn:
            cur = conn.execute(
                "DELETE FROM otel_events WHERE received_at < ?", (cutoff_iso,)
            )
        return cur.rowcount

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- read side (CLI / activity model) ----------------------------------

    def _read_rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != OTEL_STORE_SCHEMA_VERSION:
                    return []
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return []

    def recent_events(self, *, limit: int = 50) -> list[OtelEvent]:
        rows = self._read_rows(
            f"SELECT {_EVENT_COLUMNS} FROM otel_events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_event(row) for row in rows]

    def latest_per_source(self) -> list[OtelEvent]:
        """Most recent event per (service_name, session_id) source."""
        rows = self._read_rows(
            f"SELECT {_EVENT_COLUMNS} FROM otel_events WHERE id IN ("
            "  SELECT MAX(id) FROM otel_events "
            "  GROUP BY service_name, session_id"
            ") ORDER BY received_at DESC"
        )
        return [self._row_to_event(row) for row in rows]

    def counts(self) -> dict:
        rows = self._read_rows(
            "SELECT signal, COUNT(*) FROM otel_events GROUP BY signal"
        )
        meta = self._read_rows(
            "SELECT value FROM otel_meta WHERE key = 'last_write'"
        )
        return {
            "events_by_signal": {signal: count for signal, count in rows},
            "total": sum(count for _, count in rows),
            "last_write": meta[0][0] if meta else None,
        }

    @staticmethod
    def _row_to_event(row: tuple) -> OtelEvent:
        try:
            attrs = json.loads(row[8])
        except ValueError:
            attrs = {}
        return OtelEvent(
            received_at=row[0],
            event_time=row[1],
            signal=row[2],
            event_name=row[3],
            service_name=row[4],
            session_id=row[5],
            pid=row[6],
            cwd=row[7],
            attrs=attrs if isinstance(attrs, dict) else {},
        )
