"""Desired-state managed event log PoC (Redmine #11700).

Append-only record of what mozyo *intended* — which session / window /
pane it created or adopted, via which command — the one source-of-truth
layer that ``managed-state-model.md`` (#11695) identified as currently
missing. Strict boundaries from that design and the #11698 invariant:

- **desired state only.** This log never records, and is never consulted
  for, observed liveness, handoff target resolution, or preflight. Those
  stay live-tmux-authoritative; this PoC adds no read path into any of
  them.
- **append-only.** State is the fold of events; there is no UPDATE. The
  latest estimate for fast display remains the inventory projection.
- **identity key is ``pane_id``** (#11628); ``session`` is an attribute.
- **``socket`` column exists from v1** as the multi-tmux-server extension
  point (today fixed to ``default``); a future ``(socket, pane_id)``
  composite key migrates without a schema break.
- **``repo_root`` is NFD-normalized at write** via the shared helper
  (#11625), so the comparison form is fixed once at the boundary.
- **SQLite single-writer**, same posture as the other home caches
  (#56088). No Postgres / queue / pipeline.

Loss is best-effort: losing ``managed-events.sqlite`` loses intent
history only — identity (registry/anchor) and liveness (tmux) do not
depend on it (#11695 loss-recovery), so this PoC has no recovery path by
design.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mozyo_bridge.shared.paths import mozyo_bridge_home, normalize_path_unicode

MANAGED_EVENTS_FILENAME = "managed-events.sqlite"
MANAGED_EVENTS_SCHEMA_VERSION = 1
DEFAULT_SOCKET = "default"

# Event kinds the desired-state log records. All describe mozyo *intent*
# or a recorded-time observation point — never current liveness.
KIND_CREATED = "created"
KIND_ADOPTED = "adopted"
KIND_RENAMED = "renamed"
KIND_MARKED = "marked"
KIND_OBSERVED = "observed"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS managed_events (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    command TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    socket TEXT NOT NULL DEFAULT 'default',
    pane_id TEXT,
    mozyo_session TEXT,
    workspace_id TEXT,
    repo_root TEXT,
    intent_json TEXT NOT NULL
)
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_managed_events_pane "
    "ON managed_events(pane_id, recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_managed_events_ws "
    "ON managed_events(workspace_id, recorded_at)",
)

_COLUMNS = (
    "recorded_at, command, event_kind, socket, pane_id, mozyo_session, "
    "workspace_id, repo_root, intent_json"
)


def managed_events_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / MANAGED_EVENTS_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ManagedEvent:
    """One desired-state record. ``intent`` holds the minimal config."""

    command: str
    event_kind: str
    pane_id: str | None = None
    mozyo_session: str | None = None
    workspace_id: str | None = None
    repo_root: str | None = None
    socket: str = DEFAULT_SOCKET
    intent: dict = field(default_factory=dict)
    recorded_at: str | None = None

    def as_payload(self) -> dict:
        return {
            "recorded_at": self.recorded_at,
            "command": self.command,
            "event_kind": self.event_kind,
            "socket": self.socket,
            "pane_id": self.pane_id,
            "mozyo_session": self.mozyo_session,
            "workspace_id": self.workspace_id,
            "repo_root": self.repo_root,
            "intent": self.intent,
        }


class ManagedEventStoreError(RuntimeError):
    """User-actionable store error (schema mismatch, corruption)."""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 2000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(_TABLE_SQL)
        for sql in _INDEX_SQL:
            conn.execute(sql)
        conn.execute(f"PRAGMA user_version = {MANAGED_EVENTS_SCHEMA_VERSION}")
        conn.commit()
    elif version != MANAGED_EVENTS_SCHEMA_VERSION:
        conn.close()
        raise ManagedEventStoreError(
            f"managed event log {path} has schema version {version}; this "
            f"mozyo-bridge supports {MANAGED_EVENTS_SCHEMA_VERSION}."
        )
    return conn


class ManagedEventLog:
    """Append-only desired-state log. Single-writer by construction."""

    def __init__(self, path: Path | None = None, *, home: Path | None = None):
        self.path = path or managed_events_path(home)

    def append(self, event: ManagedEvent) -> ManagedEvent:
        """Append one desired-state event. NFD-normalizes repo_root here."""
        recorded_at = event.recorded_at or _utc_now()
        repo_root = (
            normalize_path_unicode(event.repo_root)
            if event.repo_root is not None
            else None
        )
        conn = _connect(self.path)
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO managed_events ({_COLUMNS}) "
                    f"VALUES ({', '.join('?' * 9)})",
                    (
                        recorded_at,
                        event.command,
                        event.event_kind,
                        event.socket or DEFAULT_SOCKET,
                        event.pane_id,
                        event.mozyo_session,
                        event.workspace_id,
                        repo_root,
                        json.dumps(event.intent, ensure_ascii=False, sort_keys=True),
                    ),
                )
        finally:
            conn.close()
        return ManagedEvent(
            command=event.command,
            event_kind=event.event_kind,
            pane_id=event.pane_id,
            mozyo_session=event.mozyo_session,
            workspace_id=event.workspace_id,
            repo_root=repo_root,
            socket=event.socket or DEFAULT_SOCKET,
            intent=event.intent,
            recorded_at=recorded_at,
        )

    def _read(self, sql: str, params: tuple = ()) -> list[ManagedEvent]:
        if not self.path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != MANAGED_EVENTS_SCHEMA_VERSION:
                    return []
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return []
        return [self._row_to_event(row) for row in rows]

    def recent(self, *, limit: int = 50) -> list[ManagedEvent]:
        return self._read(
            f"SELECT {_COLUMNS} FROM managed_events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def events_for_pane(self, pane_id: str) -> list[ManagedEvent]:
        return self._read(
            f"SELECT {_COLUMNS} FROM managed_events "
            "WHERE pane_id = ? ORDER BY id",
            (pane_id,),
        )

    @staticmethod
    def _row_to_event(row: tuple) -> ManagedEvent:
        try:
            intent = json.loads(row[8])
        except ValueError:
            intent = {}
        return ManagedEvent(
            recorded_at=row[0],
            command=row[1],
            event_kind=row[2],
            socket=row[3],
            pane_id=row[4],
            mozyo_session=row[5],
            workspace_id=row[6],
            repo_root=row[7],
            intent=intent if isinstance(intent, dict) else {},
        )


def record_managed_event(
    *,
    command: str,
    event_kind: str,
    pane_id: str | None = None,
    mozyo_session: str | None = None,
    workspace_id: str | None = None,
    repo_root: str | None = None,
    intent: dict | None = None,
    home: Path | None = None,
) -> ManagedEvent | None:
    """Best-effort command-boundary append. Never raises into the caller.

    The PoC append surface a real mozyo command boundary would call. It is
    best-effort by design: a desired-state log failure must not break the
    command that triggered it (session creation, adoption, ...), exactly
    like telemetry. Returns the appended event, or ``None`` on any failure.
    """
    try:
        return ManagedEventLog(home=home).append(
            ManagedEvent(
                command=command,
                event_kind=event_kind,
                pane_id=pane_id,
                mozyo_session=mozyo_session,
                workspace_id=workspace_id,
                repo_root=repo_root,
                intent=intent or {},
            )
        )
    except (ManagedEventStoreError, sqlite3.DatabaseError, OSError):
        return None
