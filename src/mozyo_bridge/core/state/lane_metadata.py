"""Lane metadata record — lane↔human-label display join (Redmine #13356 / #13377).

Under the herdr backend a sublane is a ``(repo_workspace_id, lane_id)`` slot
inside the shared **project** herdr workspace (Redmine #13377 Opt3, design
j#73613): its slots are ``mzb1_<project-ws>_<role>_<lane>``. A *legacy*
pre-#13377 lane (#13331 j#73357) was instead its own herdr workspace keyed on
the deterministic path-hash token (``wt_<hash>``). Either way the live
inventory carries only machine identity, so every display surface —
``sublane list``, ``sublane dispatch-worker`` lane resolution, the cockpit web
UI — needs this record to show the lane's label / issue (#13331 j#73363
residual; the #13356 design answer j#73386 Q2 fixes the join here).

This module is that join: a **host-local display-metadata record** keyed on the
per-worktree path token, carrying the human lane identity (``lane_label`` /
``issue_id`` / ``branch`` / ``worktree_path``) and — since v2 (#13377) — the
``lane_id`` a projection joins a live ``(workspace_id, lane_id)`` unit on. It is:

- **display metadata, never routing authority** (j#73386): the token→label join
  names a row for a human; route resolution stays with the live ``agent list``
  inventory + the backend-neutral resolver. No reader may promote a record to a
  delivery endpoint, liveness fact, or workflow truth.
- **a native component of the consolidated ``state.sqlite``** (j#73386 Q2:
  "route-identity ledger へ混ぜず、同じ consolidated state.sqlite 配下の別
  component / table"). It shares the container guard with the #12305 migrator
  (:func:`~mozyo_bridge.core.state.state_store.connect_state_container_rw`) and
  records itself in ``state_schema_components`` — with no ``migrated_from``
  (there is no legacy file; this is a post-consolidation native table).
- **recovery policy: ``operator_current_state``** (the vocabulary of
  ``vibes/docs/logics/managed-state-model.md`` ``### recovery policy
  vocabulary``): the token→label mapping cannot be rebuilt from events (the
  token is a one-way path hash), so loss requires explicit re-declare (a future
  backfill / re-register). Meanwhile every reader **fails open**: a missing /
  unreadable record degrades the display to the raw ``wt_<hash>`` token (the
  caller labels it ``lane_record_missing``), never an abort.
- **upsert + tombstone, not append-only.** ``sublane create`` (herdr path)
  upserts the record at the command boundary; ``sublane retire --execute``
  marks it ``retired`` with ``retired_at`` — the tombstone is kept (short-term
  residue diagnosis + label resolution for late readers; cleanup is a separate
  future preflight / TTL per j#73386).

Privacy boundary (``vibes/docs/rules/public-private-boundary.md``):
``worktree_path`` is a host-local absolute path and stays **local/private
state only** — a caller must never copy it into a Redmine journal / any
pasteable durable record (j#73386: "worktree_path # local/private state only;
Redmine noteへ出さない"). Local surfaces (``sublane list`` JSON, the local
cockpit web UI) may show it, exactly like the live projections already show
checkout roots.

Conventions mirror the sibling home-scoped stores: a ``*_path(home=None)``
helper through :func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, frozen
dataclass with ``as_payload()``, ISO-second UTC timestamps, and best-effort
command-boundary write wrappers that never raise into the caller.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.state_store import (
    STATE_CONTAINER_VERSION,
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)

#: The ``state_schema_components`` identity of this native component.
LANE_METADATA_COMPONENT = "lane_metadata"
#: v2 (Redmine #13377): adds ``lane_id`` — under the shared project workspace
#: model a lane is a ``(repo_workspace_id, lane_id)`` slot of the project
#: workspace, so readers join on that unit; ``lane_workspace_token`` stays the
#: stable per-worktree primary key (and the legacy pre-#13377 workspace segment).
LANE_METADATA_SCHEMA_VERSION = 2

#: Recovery policy (managed-state-model.md ``### recovery policy vocabulary``):
#: a desired current state that cannot be rebuilt from events (the token is a
#: one-way path hash); loss requires explicit re-declare. Readers fail open.
LANE_METADATA_RECOVERY_POLICY = "operator_current_state"

#: Record status tokens. ``active`` — the lane workspace was created and not yet
#: retired; ``retired`` — a tombstone kept for late label resolution / residue
#: diagnosis (cleanup is a separate, future concern).
LANE_STATUS_ACTIVE = "active"
LANE_STATUS_RETIRED = "retired"

#: The single table this component owns (its namespace in the container).
_TABLE = "lane_metadata_records"

_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    lane_workspace_token TEXT PRIMARY KEY,
    repo_workspace_id TEXT,
    issue_id TEXT,
    lane_label TEXT,
    branch TEXT,
    worktree_path TEXT,
    source_backend TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT,
    lane_id TEXT
)
"""

_COLUMNS = (
    "lane_workspace_token, repo_workspace_id, issue_id, lane_label, branch, "
    "worktree_path, source_backend, status, created_at, updated_at, retired_at, "
    "lane_id"
)


def lane_metadata_path(home: Path | None = None) -> Path:
    """The consolidated single state DB this component lives in (``state.sqlite``)."""
    return state_store_path(home)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class LaneMetadataRecord:
    """One lane workspace's display-metadata record (display join, not authority).

    ``lane_workspace_token`` is the stable per-worktree primary key (the
    ``wt_<hash>`` path token — the mzb1 ``workspace`` segment of a *legacy*
    pre-#13377 per-lane workspace, and purely a metadata key for a shared-model
    lane). ``repo_workspace_id`` is the main checkout's registry workspace id.
    ``lane_id`` (Redmine #13377, shared project workspace model) is the mzb1
    ``lane`` segment the lane's slots carry — readers join a live
    ``(workspace_id, lane_id)`` unit on ``(repo_workspace_id, lane_id)``; an
    empty ``lane_id`` marks a legacy record whose live rows are keyed on the
    token itself (default lane of a ``wt_<hash>`` workspace). ``worktree_path``
    is host-local private state; never copy it into a durable Redmine record.
    """

    lane_workspace_token: str
    repo_workspace_id: str = ""
    issue_id: str = ""
    lane_label: str = ""
    branch: str = ""
    worktree_path: str = ""
    source_backend: str = "herdr"
    status: str = LANE_STATUS_ACTIVE
    created_at: str = ""
    updated_at: str = ""
    retired_at: Optional[str] = None
    lane_id: str = ""

    @property
    def retired(self) -> bool:
        return self.status == LANE_STATUS_RETIRED

    def as_payload(self) -> dict:
        return {
            "lane_workspace_token": self.lane_workspace_token,
            "repo_workspace_id": self.repo_workspace_id,
            "issue_id": self.issue_id,
            "lane_label": self.lane_label,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "source_backend": self.source_backend,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "retired_at": self.retired_at,
            "lane_id": self.lane_id,
        }


class LaneMetadataStore:
    """Upsert / tombstone / read access to the lane metadata component.

    Writes go through the shared state-container guard (fail-closed on an
    unsupported container); reads are read-only and **fail open** to empty — a
    missing / unreadable / foreign-version store yields no records so a display
    degrades to the raw token instead of aborting.
    """

    def __init__(self, path: Path | None = None, *, home: Path | None = None):
        self.path = path or lane_metadata_path(home)

    # -- writes (command boundary: sublane create / retire) -----------------

    def _connect_rw(self) -> sqlite3.Connection:
        conn = connect_state_container_rw(self.path)
        conn.execute(_TABLE_SQL)
        # In-place v1 -> v2 migration (Redmine #13377): an existing table created
        # before the ``lane_id`` column gains it additively (legacy rows read as
        # ``lane_id=""`` — the legacy marker). CREATE TABLE IF NOT EXISTS never
        # alters an existing table, so this is the single write-path migration.
        if "lane_id" not in self._table_columns(conn):
            conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN lane_id TEXT")
        return conn

    @staticmethod
    def _table_columns(conn: sqlite3.Connection) -> frozenset[str]:
        return frozenset(
            row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")
        )

    def _record_component(self, conn: sqlite3.Connection, now: str) -> None:
        """Register this native component in ``state_schema_components``.

        Owner-namespace only (managed-state-model.md ownership rules): this
        writes the ``lane_metadata`` row and nothing else. ``migrated_from`` is
        NULL — a native post-consolidation component has no legacy file.
        """
        conn.execute(
            "INSERT INTO state_schema_components "
            "(component, schema_version, owner, recovery_policy, migrated_from, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(component) DO UPDATE SET "
            "schema_version = excluded.schema_version, "
            "owner = excluded.owner, recovery_policy = excluded.recovery_policy, "
            "updated_at = excluded.updated_at",
            (
                LANE_METADATA_COMPONENT,
                LANE_METADATA_SCHEMA_VERSION,
                LANE_METADATA_COMPONENT,
                LANE_METADATA_RECOVERY_POLICY,
                now,
            ),
        )

    def upsert(self, record: LaneMetadataRecord) -> LaneMetadataRecord:
        """Insert or refresh one lane record, stamping the timestamps.

        A re-create of the same lane token revives a tombstone: the upsert
        resets ``status`` / ``retired_at`` from the incoming record (a lane
        recreated after retire is active again), while ``created_at`` keeps the
        first-seen stamp.
        """
        if not record.lane_workspace_token:
            raise ValueError("lane_workspace_token must not be empty")
        now = _utc_now()
        created = record.created_at or now
        stamped = replace(record, created_at=created, updated_at=now)
        conn = self._connect_rw()
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO {_TABLE} ({_COLUMNS}) "
                    f"VALUES ({', '.join('?' * 12)}) "
                    "ON CONFLICT(lane_workspace_token) DO UPDATE SET "
                    "repo_workspace_id = excluded.repo_workspace_id, "
                    "issue_id = excluded.issue_id, "
                    "lane_label = excluded.lane_label, "
                    "branch = excluded.branch, "
                    "worktree_path = excluded.worktree_path, "
                    "source_backend = excluded.source_backend, "
                    "status = excluded.status, "
                    "updated_at = excluded.updated_at, "
                    "retired_at = excluded.retired_at, "
                    "lane_id = excluded.lane_id",
                    (
                        stamped.lane_workspace_token,
                        stamped.repo_workspace_id,
                        stamped.issue_id,
                        stamped.lane_label,
                        stamped.branch,
                        stamped.worktree_path,
                        stamped.source_backend,
                        stamped.status,
                        stamped.created_at,
                        stamped.updated_at,
                        stamped.retired_at,
                        stamped.lane_id,
                    ),
                )
                self._record_component(conn, now)
        finally:
            conn.close()
        return stamped

    def mark_retired(self, lane_workspace_token: str) -> bool:
        """Tombstone one lane record (``status=retired`` + ``retired_at``).

        Returns True when a record was updated; False when no record exists for
        the token (nothing is invented — an unrecorded lane stays unrecorded).
        """
        if not lane_workspace_token:
            return False
        now = _utc_now()
        conn = self._connect_rw()
        try:
            with conn:
                cursor = conn.execute(
                    f"UPDATE {_TABLE} SET status = ?, retired_at = ?, updated_at = ? "
                    "WHERE lane_workspace_token = ?",
                    (LANE_STATUS_RETIRED, now, now, lane_workspace_token),
                )
                self._record_component(conn, now)
                return cursor.rowcount > 0
        finally:
            conn.close()

    # -- reads (fail-open display join) --------------------------------------

    def _read_rows(self) -> list[tuple]:
        if not self.path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != STATE_CONTAINER_VERSION:
                    return []
                # A v1 table (no write since the #13377 lane_id migration) is
                # still fully readable: select the legacy shape and default the
                # missing column, so a read-only consumer never loses the
                # display join just because no write has migrated the schema.
                columns = _COLUMNS
                if "lane_id" not in self._table_columns(conn):
                    columns = _COLUMNS.rsplit(", lane_id", 1)[0] + ", '' AS lane_id"
                return conn.execute(
                    f"SELECT {columns} FROM {_TABLE} ORDER BY lane_workspace_token"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            # Absent table / corrupt file: fail open to "no records" — the
            # display degrades to the raw token (lane_record_missing).
            return []

    def load_all(self, *, include_retired: bool = True) -> dict[str, LaneMetadataRecord]:
        """Every lane record keyed by token (fail-open: empty on any read failure).

        ``include_retired=True`` (the default) keeps tombstones visible so a
        late reader can still resolve a retired lane's label; a caller that only
        wants live lanes filters them out.
        """
        records: dict[str, LaneMetadataRecord] = {}
        for row in self._read_rows():
            record = LaneMetadataRecord(
                lane_workspace_token=row[0],
                repo_workspace_id=row[1] or "",
                issue_id=row[2] or "",
                lane_label=row[3] or "",
                branch=row[4] or "",
                worktree_path=row[5] or "",
                source_backend=row[6] or "",
                status=row[7] or LANE_STATUS_ACTIVE,
                created_at=row[8] or "",
                updated_at=row[9] or "",
                retired_at=row[10],
                lane_id=row[11] or "",
            )
            if not include_retired and record.retired:
                continue
            records[record.lane_workspace_token] = record
        return records

    def get(self, lane_workspace_token: str) -> Optional[LaneMetadataRecord]:
        return self.load_all().get(lane_workspace_token)


def record_lane_created(
    *,
    lane_workspace_token: str,
    repo_workspace_id: str = "",
    issue_id: str = "",
    lane_label: str = "",
    branch: str = "",
    worktree_path: str = "",
    source_backend: str = "herdr",
    lane_id: str = "",
    home: Path | None = None,
) -> Optional[LaneMetadataRecord]:
    """Best-effort upsert at the ``sublane create`` command boundary.

    Never raises into the caller: a metadata write failure must not break the
    lane actuation that triggered it (exactly like the sibling best-effort
    telemetry appends). Returns the stamped record, or ``None`` on any failure —
    the projection then degrades to the raw token (``lane_record_missing``).
    """
    try:
        return LaneMetadataStore(home=home).upsert(
            LaneMetadataRecord(
                lane_workspace_token=lane_workspace_token,
                repo_workspace_id=repo_workspace_id,
                issue_id=issue_id,
                lane_label=lane_label,
                branch=branch,
                worktree_path=worktree_path,
                source_backend=source_backend,
                status=LANE_STATUS_ACTIVE,
                retired_at=None,
                lane_id=lane_id,
            )
        )
    except (StateStoreError, sqlite3.DatabaseError, OSError, ValueError):
        return None


def record_lane_retired(
    lane_workspace_token: str, *, home: Path | None = None
) -> bool:
    """Best-effort tombstone at the ``sublane retire`` command boundary.

    Never raises into the caller; returns True only when an existing record was
    marked retired.
    """
    try:
        return LaneMetadataStore(home=home).mark_retired(lane_workspace_token)
    except (StateStoreError, sqlite3.DatabaseError, OSError):
        return False


def load_lane_records(
    *, include_retired: bool = True, home: Path | None = None
) -> dict[str, LaneMetadataRecord]:
    """Fail-open read for display joins: empty mapping on any failure."""
    try:
        return LaneMetadataStore(home=home).load_all(include_retired=include_retired)
    except (StateStoreError, sqlite3.DatabaseError, OSError):
        return {}


def lane_records_by_unit(
    records: dict[str, LaneMetadataRecord],
) -> dict[tuple[str, str], LaneMetadataRecord]:
    """Index token-keyed records by their live lane unit ``(repo_workspace_id, lane_id)``.

    Shared project workspace model (Redmine #13377): a lane's live rows carry
    ``(workspace_id=<project>, lane_id=<lane>)``, so display joins key on that
    unit. Only records that carry BOTH fields participate (a legacy record —
    empty ``lane_id`` — joins by its token instead; an unattributed record never
    fabricates a unit key). Pure over the supplied mapping.
    """
    by_unit: dict[tuple[str, str], LaneMetadataRecord] = {}
    for record in records.values():
        ws = (record.repo_workspace_id or "").strip()
        lane = (record.lane_id or "").strip()
        if ws and lane:
            by_unit.setdefault((ws, lane), record)
    return by_unit


__all__ = (
    "LANE_METADATA_COMPONENT",
    "LANE_METADATA_SCHEMA_VERSION",
    "LANE_METADATA_RECOVERY_POLICY",
    "LANE_STATUS_ACTIVE",
    "LANE_STATUS_RETIRED",
    "LaneMetadataRecord",
    "LaneMetadataStore",
    "lane_metadata_path",
    "lane_records_by_unit",
    "load_lane_records",
    "record_lane_created",
    "record_lane_retired",
)
