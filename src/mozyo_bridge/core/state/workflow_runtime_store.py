"""Home-scoped workflow-runtime state store (Redmine #12671).

The spine roadmap US #12671
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` ``### 設計思想`` /
``### ロードマップUS`` step 2) splits durable memory from runtime state:

- **Redmine** is durable external memory / audit log / owner-visible source.
- the **mozyo DB** holds *runtime* workflow state — the folded workflow event log,
  pending delivery, and route identity — plus duplicate suppression. Live tmux / cockpit
  is liveness evidence only; a pane id is cache / evidence, never the routing authority.

This module is that mozyo-DB half: a home-scoped SQLite holding the workflow runtime
state the #12857 pure runtime
(:mod:`...f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime`) replays and
the #12671 command result (:mod:`...domain.workflow_next_action`) enriches. It persists:

- ``workflow_events`` — the durable lane event log (one row per ``event_id`` durable
  anchor; re-recording the same anchor overwrites, so the persisted log is idempotent the
  same way :func:`...domain.workflow_runtime.replay_events` is). ``seq`` (the insert order)
  preserves apply order so a re-read replays in the same order.
- ``workflow_route_identities`` — the #12553 stable route identity per managed lane, keyed
  by ``route_id`` and tagged with the lane's Redmine ``issue``. ``last_seen_pane_id`` is a
  **cache / evidence column only** (staleness detection / audit); it is never the routing
  authority and the read model projects the public-safe identity, not the pane id.
- ``workflow_runtime_meta`` — the small advisory scalar inputs (ready independent /
  overlapping work counts, remaining capacity, owner-or-release-gate flag) so ``workflow
  resume`` reproduces the same admission decision the events were last evaluated under.

Deliberate boundaries (kept small, like the sibling stores):

- the store persists **domain-agnostic rows** (dicts / scalars); it imports no
  ``f_140`` domain type, so the bounded-context boundary stays one-way (the ``f_140``
  application layer maps rows <-> :class:`LaneEvent` / route records, never this module).
- it is **not** a Redmine mirror, an approval / review / close authority, or a liveness
  store. There is no completion / approval / close column here; workflow completion stays
  Redmine-only and live route resolution stays the ledger's job at action time.
- it is a **net-new** runtime cache, intentionally a separate home-scoped file rather than
  a component of the consolidated ``state.sqlite`` migration registry (which migrates
  *legacy* per-kind files). Folding it into that single DB is a later consolidation step,
  exactly how every sibling store predated consolidation.

Conventions mirror the sibling home-scoped stores
(:mod:`mozyo_bridge.core.state.presentation_state`,
:mod:`mozyo_bridge.core.state.workspace_registry`): a ``*_FILENAME`` constant, a
``*_path(home=None)`` helper resolving through
:func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a ``PRAGMA user_version`` schema
guard that fails closed on an unrecognized version, frozen dataclasses with
``as_payload()``, and ISO-second UTC timestamps.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-scoped SQLite file holding workflow runtime state. A separate DB from
#: ``registry.sqlite`` (identity) / ``presentation.sqlite`` (desired display) /
#: ``state.sqlite`` (legacy consolidation target); this is the runtime cache for the
#: workflow event log + route identity + advisory inputs.
WORKFLOW_RUNTIME_STORE_FILENAME = "workflow-runtime.sqlite"

#: Schema version stamped into ``PRAGMA user_version``. Bump only with a migration; an
#: unrecognized version fails closed rather than dropping the runtime state.
WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION = 1

#: The recognized advisory meta keys (the scalar inputs to the admission decision).
META_READY_INDEPENDENT = "ready_independent_work"
META_READY_OVERLAP = "ready_overlapping_work"
META_CAPACITY = "capacity_remaining"
META_OWNER_OR_RELEASE_GATE = "owner_or_release_gate_active"

#: The columns a persisted lane event row carries (mirrors the #12857 LaneEvent facts).
EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "issue",
    "gate",
    "review_conclusion",
    "callback_state",
    "commit_bearing",
    "integration_recorded",
    "issue_open",
    "blocker_recorded",
)

#: The columns a persisted route identity row carries (issue-tagged #12553 identity;
#: ``last_seen_pane_id`` is a cache / evidence column, never routing authority).
ROUTE_COLUMNS: tuple[str, ...] = (
    "route_id",
    "issue",
    "workspace_id",
    "lane_id",
    "role",
    "pane_name",
    "last_seen_pane_id",
    "observed_at",
)

_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workflow_events (
    event_id TEXT PRIMARY KEY,
    issue TEXT NOT NULL,
    gate TEXT NOT NULL,
    review_conclusion TEXT NOT NULL,
    callback_state TEXT NOT NULL,
    commit_bearing INTEGER NOT NULL DEFAULT 0,
    integration_recorded INTEGER NOT NULL DEFAULT 0,
    issue_open INTEGER NOT NULL DEFAULT 1,
    blocker_recorded INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
)
"""

_ROUTE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workflow_route_identities (
    route_id TEXT PRIMARY KEY,
    issue TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    role TEXT NOT NULL,
    pane_name TEXT NOT NULL,
    last_seen_pane_id TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workflow_runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class WorkflowRuntimeStoreError(RuntimeError):
    """The workflow-runtime DB could not be opened at the expected schema.

    Raised for a structural / version problem the store must not paper over — an
    unrecognized ``user_version`` most importantly. Runtime workflow state is rebuilt
    from Redmine durable memory, but the store still fails closed (never silently drops
    tables) so a downgraded build cannot quietly discard a newer schema's state.
    """


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (sibling-store convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workflow_runtime_store_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``workflow-runtime.sqlite`` path under the mozyo-bridge home.

    ``home`` overrides the root (tests pass a temp dir); otherwise the shared
    :func:`mozyo_bridge.shared.paths.mozyo_bridge_home` resolves
    ``MOZYO_BRIDGE_HOME`` / ``~/.mozyo_bridge``.
    """
    return (home or mozyo_bridge_home()) / WORKFLOW_RUNTIME_STORE_FILENAME


def _as_bool(value: object) -> bool:
    """Coerce a persisted scalar to a bool (``0``/``1`` int columns or truthy strings)."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


@dataclass(frozen=True)
class WorkflowEventRow:
    """A persisted lane event row (the durable facts; ``seq`` is the apply order)."""

    event_id: str
    issue: str
    gate: str
    review_conclusion: str
    callback_state: str
    commit_bearing: bool
    integration_recorded: bool
    issue_open: bool
    blocker_recorded: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "issue": self.issue,
            "gate": self.gate,
            "review_conclusion": self.review_conclusion,
            "callback_state": self.callback_state,
            "commit_bearing": self.commit_bearing,
            "integration_recorded": self.integration_recorded,
            "issue_open": self.issue_open,
            "blocker_recorded": self.blocker_recorded,
        }


@dataclass(frozen=True)
class WorkflowRouteRow:
    """A persisted route identity row (issue-tagged; pane id is cache / evidence only)."""

    route_id: str
    issue: str
    workspace_id: str
    lane_id: str
    role: str
    pane_name: str
    last_seen_pane_id: str
    observed_at: str

    def as_record(self) -> dict[str, str]:
        """Full record (cache included) for the ledger's ``from_record`` round-trip."""
        return {
            "route_id": self.route_id,
            "issue": self.issue,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "pane_name": self.pane_name,
            "last_seen_pane_id": self.last_seen_pane_id,
            "observed_at": self.observed_at,
        }


class WorkflowRuntimeStore:
    """Read/write access to the home-scoped workflow-runtime DB.

    Construction never touches the filesystem; the DB is created lazily on the first
    write. Reads on an absent DB return empty results (the normal pre-write state); an
    existing DB with an unrecognized container version fails closed.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else workflow_runtime_store_path(home)

    # -- connections -------------------------------------------------------

    def _connect_rw(self) -> sqlite3.Connection:
        """Open a read-write connection, creating / validating the container.

        ``PRAGMA user_version`` is the migration guard (mirrors the sibling stores).
        Version ``0`` is a fresh file — create the tables and stamp the version. A newer,
        unrecognized version fails closed via :class:`WorkflowRuntimeStoreError` rather
        than being rewritten, so a downgraded build never destroys newer state.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute(_EVENTS_TABLE_SQL)
            conn.execute(_ROUTE_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(
                f"PRAGMA user_version = {WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION}"
            )
            conn.commit()
        elif version != WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION:
            conn.close()
            raise WorkflowRuntimeStoreError(
                f"workflow runtime store {self.path} has unsupported schema version "
                f"{version}; this build understands "
                f"{WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION}. The DB is left untouched "
                f"(downgrade-safe); migrate with a newer build or move it aside."
            )
        return conn

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        """Open a read-only connection if the DB exists; ``None`` when absent.

        A *missing* file is the normal pre-write state (returns ``None``). An existing
        file with an unrecognized container version fails closed.
        """
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
        if version != WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION:
            conn.close()
            raise WorkflowRuntimeStoreError(
                f"workflow runtime store {self.path} has unsupported schema version "
                f"{version}; this build understands "
                f"{WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION}."
            )
        return conn

    # -- events ------------------------------------------------------------

    def append_events(
        self, events: Iterable[Mapping[str, object]], *, now: Optional[str] = None
    ) -> int:
        """Persist (upsert) lane event rows; return the number written.

        Each event is keyed by ``event_id`` and upserted: re-recording the same durable
        anchor overwrites in place (idempotent, like the replay's duplicate suppression)
        and keeps its original ``seq`` so apply order is stable. A new ``event_id`` is
        appended after the current max ``seq``. The optional boolean facts default to the
        #12857 LaneEvent defaults when a key is omitted.
        """
        stamp = now or _utc_now()
        rows = list(events)
        if not rows:
            return 0
        conn = self._connect_rw()
        try:
            next_seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 FROM workflow_events"
                ).fetchone()[0]
            )
            written = 0
            for row in rows:
                event_id = str(row.get("event_id", "")).strip()
                issue = str(row.get("issue", "")).strip()
                if not event_id or not issue:
                    raise WorkflowRuntimeStoreError(
                        "workflow event requires a non-empty event_id and issue; "
                        f"got event_id={event_id!r} issue={issue!r}"
                    )
                existing = conn.execute(
                    "SELECT seq FROM workflow_events WHERE event_id = ?", (event_id,)
                ).fetchone()
                seq = int(existing[0]) if existing is not None else next_seq
                if existing is None:
                    next_seq += 1
                conn.execute(
                    "INSERT INTO workflow_events "
                    "(event_id, issue, gate, review_conclusion, callback_state, "
                    "commit_bearing, integration_recorded, issue_open, blocker_recorded, "
                    "seq, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(event_id) DO UPDATE SET "
                    "issue = excluded.issue, gate = excluded.gate, "
                    "review_conclusion = excluded.review_conclusion, "
                    "callback_state = excluded.callback_state, "
                    "commit_bearing = excluded.commit_bearing, "
                    "integration_recorded = excluded.integration_recorded, "
                    "issue_open = excluded.issue_open, "
                    "blocker_recorded = excluded.blocker_recorded, "
                    "recorded_at = excluded.recorded_at",
                    (
                        event_id,
                        issue,
                        str(row.get("gate", "none")),
                        str(row.get("review_conclusion", "pending")),
                        str(row.get("callback_state", "none")),
                        1 if _as_bool(row.get("commit_bearing", False)) else 0,
                        1 if _as_bool(row.get("integration_recorded", False)) else 0,
                        1 if _as_bool(row.get("issue_open", True)) else 0,
                        1 if _as_bool(row.get("blocker_recorded", False)) else 0,
                        seq,
                        stamp,
                    ),
                )
                written += 1
            conn.commit()
            return written
        finally:
            conn.close()

    def read_events(self) -> tuple[WorkflowEventRow, ...]:
        """Return the persisted lane events in apply (``seq``) order; empty if absent."""
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            rows = conn.execute(
                "SELECT event_id, issue, gate, review_conclusion, callback_state, "
                "commit_bearing, integration_recorded, issue_open, blocker_recorded "
                "FROM workflow_events ORDER BY seq, rowid"
            ).fetchall()
        finally:
            conn.close()
        return tuple(
            WorkflowEventRow(
                event_id=r[0],
                issue=r[1],
                gate=r[2],
                review_conclusion=r[3],
                callback_state=r[4],
                commit_bearing=bool(r[5]),
                integration_recorded=bool(r[6]),
                issue_open=bool(r[7]),
                blocker_recorded=bool(r[8]),
            )
            for r in rows
        )

    # -- route identities --------------------------------------------------

    def put_route_identities(
        self, records: Iterable[Mapping[str, object]], *, now: Optional[str] = None
    ) -> int:
        """Upsert issue-tagged route identity rows; return the number written.

        Each record carries the #12553 stable identity plus the lane's Redmine ``issue``.
        ``last_seen_pane_id`` is persisted as a cache / evidence column only. A record
        missing a stable field (route_id / issue / workspace_id / role / pane_name) fails
        closed — an identity that could only be matched by pane id is never persisted.
        """
        stamp = now or _utc_now()
        rows = list(records)
        if not rows:
            return 0
        conn = self._connect_rw()
        try:
            written = 0
            for rec in rows:
                values = {key: str(rec.get(key, "")).strip() for key in ROUTE_COLUMNS}
                values["lane_id"] = values["lane_id"] or "default"
                missing = [
                    key
                    for key in ("route_id", "issue", "workspace_id", "role", "pane_name")
                    if not values[key]
                ]
                if missing:
                    raise WorkflowRuntimeStoreError(
                        "workflow route identity requires non-empty stable fields "
                        f"(missing: {', '.join(missing)}); a pane id is never the route "
                        "authority"
                    )
                conn.execute(
                    "INSERT INTO workflow_route_identities "
                    "(route_id, issue, workspace_id, lane_id, role, pane_name, "
                    "last_seen_pane_id, observed_at, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(route_id) DO UPDATE SET "
                    "issue = excluded.issue, workspace_id = excluded.workspace_id, "
                    "lane_id = excluded.lane_id, role = excluded.role, "
                    "pane_name = excluded.pane_name, "
                    "last_seen_pane_id = excluded.last_seen_pane_id, "
                    "observed_at = excluded.observed_at, "
                    "recorded_at = excluded.recorded_at",
                    (
                        values["route_id"],
                        values["issue"],
                        values["workspace_id"],
                        values["lane_id"],
                        values["role"],
                        values["pane_name"],
                        values["last_seen_pane_id"],
                        values["observed_at"],
                        stamp,
                    ),
                )
                written += 1
            conn.commit()
            return written
        finally:
            conn.close()

    def read_route_identities(self) -> tuple[WorkflowRouteRow, ...]:
        """Return the persisted route identities (route_id order); empty if absent."""
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            rows = conn.execute(
                "SELECT route_id, issue, workspace_id, lane_id, role, pane_name, "
                "last_seen_pane_id, observed_at FROM workflow_route_identities "
                "ORDER BY route_id"
            ).fetchall()
        finally:
            conn.close()
        return tuple(
            WorkflowRouteRow(
                route_id=r[0],
                issue=r[1],
                workspace_id=r[2],
                lane_id=r[3],
                role=r[4],
                pane_name=r[5],
                last_seen_pane_id=r[6],
                observed_at=r[7],
            )
            for r in rows
        )

    # -- advisory meta -----------------------------------------------------

    def set_meta(
        self, values: Mapping[str, object], *, now: Optional[str] = None
    ) -> int:
        """Upsert advisory scalar inputs (``key`` -> ``value``); return the count.

        Values are stored verbatim as strings; the reader coerces them back. Only the
        recognized meta keys are meaningful, but the store does not gatekeep the key set
        (an unknown key is simply persisted and ignored by the use case).
        """
        stamp = now or _utc_now()
        items = list(values.items())
        if not items:
            return 0
        conn = self._connect_rw()
        try:
            for key, value in items:
                conn.execute(
                    "INSERT INTO workflow_runtime_meta (key, value, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, updated_at = excluded.updated_at",
                    (str(key), str(value), stamp),
                )
            conn.commit()
            return len(items)
        finally:
            conn.close()

    def read_meta(self) -> dict[str, str]:
        """Return the persisted advisory meta as ``{key: value}``; empty if absent."""
        conn = self._connect_ro()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT key, value FROM workflow_runtime_meta"
            ).fetchall()
        finally:
            conn.close()
        return {r[0]: r[1] for r in rows}


__all__ = (
    "WORKFLOW_RUNTIME_STORE_FILENAME",
    "WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION",
    "META_READY_INDEPENDENT",
    "META_READY_OVERLAP",
    "META_CAPACITY",
    "META_OWNER_OR_RELEASE_GATE",
    "EVENT_COLUMNS",
    "ROUTE_COLUMNS",
    "WorkflowRuntimeStoreError",
    "workflow_runtime_store_path",
    "WorkflowEventRow",
    "WorkflowRouteRow",
    "WorkflowRuntimeStore",
)
