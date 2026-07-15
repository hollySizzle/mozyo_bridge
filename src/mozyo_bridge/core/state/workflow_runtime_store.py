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
#:
#: - v1 (#12671): ``workflow_events`` / ``workflow_route_identities`` /
#:   ``workflow_runtime_meta``.
#: - v2 (#13520): adds the **callback outbox** (``callback_outbox`` + ``callback_cursor``)
#:   for the zero-wait callback delivery bounded context. The v1->v2 migration is additive
#:   and explicit (:meth:`WorkflowRuntimeStore._connect_rw`): it creates the new tables and
#:   preserves every existing event / route / meta row. A downgraded build that only knows
#:   an older version fails closed rather than dropping the newer state.
#: - v3 (#13520 review R2-F5): adds ``workspace_id`` to the callback outbox and widens the UNIQUE
#:   key to include it, so a shared home DB partitions callback rows / claims by workspace (a
#:   watcher never claims another workspace's rows). The v2->v3 migration recreates the callback
#:   table preserving existing rows (``workspace_id=''``).
WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION = 3

#: The recognized schema versions this build can read. A write always migrates up to the
#: current version; a read tolerates any recognized version (a v1 DB is still readable for
#: its legacy tables, and its callback reads simply return empty until the first callback
#: write migrates it). Anything else (a newer / foreign version) fails closed.
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2, 3})

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

# ---------------------------------------------------------------------------
# Callback outbox (schema v2, Redmine #13520). The zero-wait callback delivery
# bounded context: a handoff-worthy durable gate transition becomes a callback to
# fire exactly once (a coordinator new-turn trigger), idempotency-fenced so a
# watcher restart / duplicate herdr-or-Redmine event / concurrent claimer can never
# produce a duplicate delivery. Deliberately a **separate bounded context** from the
# dispatch outbox fence (:mod:`...dispatch_outbox_fence`): different DB / table / key,
# because worker send authority and callback delivery are distinct concerns
# (#13520 design answer j#75098 Q3). What is reused is the *pattern* — ``BEGIN
# IMMEDIATE`` reserve, a UNIQUE idempotency key, a closed state vocabulary, and a
# fail-closed migration — not the fence's store.
# ---------------------------------------------------------------------------

#: The closed callback-outbox state vocabulary (#13520 design answer j#75098 Q3).
CALLBACK_PENDING = "pending"  # classified + enqueued; awaiting a delivery claim
CALLBACK_INFLIGHT = "inflight"  # claimed by a processor; ``send_attempted`` tracks the send edge
CALLBACK_DELIVERED = "delivered"  # the one send was positively delivered
CALLBACK_UNCERTAIN = "uncertain"  # send outcome unknown (ACK-only / crash-after-send) -> no auto-retry
CALLBACK_DEAD_LETTER = "dead_letter"  # unclassified, or retries exhausted -> fresh-turn sweep + diagnostic
CALLBACK_ABSENT = "absent"  # sentinel: no row for the key (never persisted)

CALLBACK_STATES = frozenset(
    {
        CALLBACK_PENDING,
        CALLBACK_INFLIGHT,
        CALLBACK_DELIVERED,
        CALLBACK_UNCERTAIN,
        CALLBACK_DEAD_LETTER,
    }
)

#: The default bounded retry budget for a *deterministic not-sent* delivery failure. Only a
#: pre-injection / known-not-sent failure consumes an attempt; an ACK-only / uncertain
#: outcome never auto-retries (it goes straight to :data:`CALLBACK_UNCERTAIN`).
CALLBACK_DEFAULT_MAX_ATTEMPTS = 3

_CALLBACK_OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS callback_outbox (
    source              TEXT NOT NULL,
    issue               TEXT NOT NULL,
    journal             TEXT NOT NULL,
    normalized_gate     TEXT NOT NULL,
    callback_route      TEXT NOT NULL,
    state               TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    send_attempted      INTEGER NOT NULL DEFAULT 0,
    claim_token         TEXT NOT NULL DEFAULT '',
    claimed_at          TEXT NOT NULL DEFAULT '',
    notification_kind   TEXT NOT NULL DEFAULT '',
    notification_summary TEXT NOT NULL DEFAULT '',
    gate_mismatch       INTEGER NOT NULL DEFAULT 0,
    detail              TEXT NOT NULL DEFAULT '',
    payload             TEXT NOT NULL DEFAULT '',
    workspace_id        TEXT NOT NULL DEFAULT '',
    target_lane         TEXT NOT NULL DEFAULT '',
    target_receiver     TEXT NOT NULL DEFAULT '',
    target_generation   TEXT NOT NULL DEFAULT '',
    seq                 INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(workspace_id, source, issue, journal, normalized_gate, callback_route)
)
"""


def _migrate_callback_outbox_workspace(conn: sqlite3.Connection) -> None:
    """v2 -> v3: add ``workspace_id`` to the callback outbox + widen the UNIQUE key (#13520 R2-F5).

    A callback outbox created before the review-R2-F5 fix keys rows on
    ``(source, issue, journal, normalized_gate, callback_route)`` with NO workspace authority, so a
    shared home DB lets one workspace's watcher claim / collide with another's rows. Widening the
    UNIQUE key to include ``workspace_id`` requires recreating the table (SQLite cannot alter a
    table-level UNIQUE). Data-preserving: existing rows copy across with ``workspace_id=''`` (they
    were unique on the old sub-key, so they stay unique under the widened key). Idempotent — a no-op
    once ``workspace_id`` is present.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(callback_outbox)").fetchall()]
    if not cols or "workspace_id" in cols:
        return  # table absent (created fresh by the caller) or already migrated
    conn.execute("ALTER TABLE callback_outbox RENAME TO _callback_outbox_pre_v3")
    conn.execute(_CALLBACK_OUTBOX_TABLE_SQL)  # new table: workspace_id + widened UNIQUE
    shared = ", ".join(cols)  # every old column exists in the new table (a superset)
    conn.execute(
        f"INSERT INTO callback_outbox ({shared}) SELECT {shared} FROM _callback_outbox_pre_v3"
    )
    conn.execute("DROP TABLE _callback_outbox_pre_v3")

#: The callback-outbox ownership columns (Redmine #13520 review F2). A ``claim_token`` +
#: ``claimed_at`` lease fences a claim so a concurrent processor cannot reclaim an actively
#: worked row and double-send. Added defensively to a callback table that predates them.
_CALLBACK_OWNERSHIP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("claim_token", "TEXT NOT NULL DEFAULT ''"),
    ("claimed_at", "TEXT NOT NULL DEFAULT ''"),
)


def _ensure_callback_ownership_columns(conn: sqlite3.Connection) -> None:
    """Add the F2 ownership columns to a callback table that lacks them (idempotent).

    A callback outbox created before the #13520 review-F2 fix has no ``claim_token`` /
    ``claimed_at``. ``ALTER TABLE ADD COLUMN`` is additive and preserves rows; it is a no-op
    once present. Guarded by ``PRAGMA table_info`` so it never errors on an already-migrated DB.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(callback_outbox)").fetchall()}
    for name, decl in _CALLBACK_OWNERSHIP_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE callback_outbox ADD COLUMN {name} {decl}")


#: The callback-outbox durable target-tuple columns (Redmine #13683 review R4-F2). The row holds
#: the intended target ``lane`` / ``receiver`` (binding-resolved provider) and a ``generation`` /
#: correlation seam, so the background_service delivery authority binds the re-resolved live target
#: to the row's durable expectation (a wrong lane / receiver / unknown generation fails closed).
#: ``target_generation`` is the seam #13684's correlated review-result routing populates; #13683
#: fixes the field so the dependency does not invert (j#77069). Added defensively (additive).
_CALLBACK_TARGET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("target_lane", "TEXT NOT NULL DEFAULT ''"),
    ("target_receiver", "TEXT NOT NULL DEFAULT ''"),
    ("target_generation", "TEXT NOT NULL DEFAULT ''"),
)


def _ensure_callback_target_columns(conn: sqlite3.Connection) -> None:
    """Add the R4-F2 durable target-tuple columns to a callback table that lacks them (idempotent).

    ``ALTER TABLE ADD COLUMN`` is additive and preserves rows; a no-op once present. An older build
    that does not read these columns is unaffected (they simply default ``''``), so the change is
    backward / forward compatible without a container version bump (same posture as the ownership
    columns).
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(callback_outbox)").fetchall()}
    for name, decl in _CALLBACK_TARGET_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE callback_outbox ADD COLUMN {name} {decl}")

_CALLBACK_CURSOR_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS callback_cursor (
    source     TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

#: The columns a persisted callback outbox row carries.
CALLBACK_COLUMNS: tuple[str, ...] = (
    "source",
    "issue",
    "journal",
    "normalized_gate",
    "callback_route",
    "state",
    "attempts",
    "max_attempts",
    "send_attempted",
    "notification_kind",
    "notification_summary",
    "gate_mismatch",
    "detail",
    "payload",
)


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
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            # Fresh file: create the full v2 schema (all tables) and stamp the version.
            conn.execute(_EVENTS_TABLE_SQL)
            conn.execute(_ROUTE_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(_CALLBACK_OUTBOX_TABLE_SQL)
            conn.execute(_CALLBACK_CURSOR_TABLE_SQL)
            conn.execute(
                f"PRAGMA user_version = {WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION}"
            )
            conn.commit()
        elif version in (1, 2):
            # v1 -> v3 (#13520): a v1 DB has no callback tables — create them fresh (the current
            # SQL already carries workspace_id + the widened UNIQUE). A v2 DB has the callback
            # table without workspace_id — recreate it preserving rows (workspace migration). Both
            # leave event / route / meta rows untouched (data preservation) and re-stamp the
            # version. `CREATE TABLE IF NOT EXISTS` is a no-op for a table already present.
            conn.execute(_CALLBACK_OUTBOX_TABLE_SQL)
            conn.execute(_CALLBACK_CURSOR_TABLE_SQL)
            _ensure_callback_ownership_columns(conn)
            _ensure_callback_target_columns(conn)
            _migrate_callback_outbox_workspace(conn)
            conn.execute(
                f"PRAGMA user_version = {WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION}"
            )
            conn.commit()
        elif version == WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION:
            # Already current; the tables exist. A defensive IF-NOT-EXISTS keeps a DB that
            # somehow lost a table self-healing without touching data; the F2 ownership columns
            # and the R2-F5 workspace_id migration are idempotently applied if this DB predates
            # them (the version guard alone cannot distinguish a partially-migrated file).
            conn.execute(_CALLBACK_OUTBOX_TABLE_SQL)
            conn.execute(_CALLBACK_CURSOR_TABLE_SQL)
            _ensure_callback_ownership_columns(conn)
            _ensure_callback_target_columns(conn)
            _migrate_callback_outbox_workspace(conn)
            conn.commit()
        else:
            conn.close()
            raise WorkflowRuntimeStoreError(
                f"workflow runtime store {self.path} has unsupported schema version "
                f"{version}; this build understands "
                f"{sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. The DB is left untouched "
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
        if version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise WorkflowRuntimeStoreError(
                f"workflow runtime store {self.path} has unsupported schema version "
                f"{version}; this build understands "
                f"{sorted(_RECOGNIZED_SCHEMA_VERSIONS)}."
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
        """Return the persisted route identities in **recorded order**; empty if absent.

        Ordered by ``(recorded_at, rowid)`` so the most-recently-written route for an issue
        sorts last — the deterministic last-write-wins order route selection relies on
        (an upsert advances ``recorded_at``, so a re-persisted route is treated as the most
        recent). Not ``route_id`` order, which is arbitrary and would silently pick the
        wrong same-issue route (review j#68908 finding 1).
        """
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            rows = conn.execute(
                "SELECT route_id, issue, workspace_id, lane_id, role, pane_name, "
                "last_seen_pane_id, observed_at FROM workflow_route_identities "
                "ORDER BY recorded_at, rowid"
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

    #: Reserved ``workflow_runtime_meta`` key prefix for durable review-generation leases (#13518
    #: review R3-F2). Kept out of the advisory meta vocabulary so the review-generation lease and
    #: the runtime scalars never collide.
    _GENERATION_LEASE_PREFIX = "genlease:"

    def acquire_generation_lease(self, key: str, holder: str, *, now: Optional[str] = None) -> bool:
        """Atomically CAS-acquire a durable single-consumer review-generation lease (#13518 R3-F2).

        ``BEGIN IMMEDIATE`` serializes concurrent acquirers over the shared
        ``workflow-runtime.sqlite``: the lease under ``key`` is granted iff it is unheld OR already
        held by the same ``holder`` (idempotent re-acquire); a DIFFERENT holder is refused. This is
        the durable review-decision-commit fence — at most one consumer ever commits a given review
        generation's approval — complementing the callback-transport outbox fence. Returns True iff
        this ``holder`` now holds the lease.
        """
        h = str(holder or "").strip()
        if not h:
            return False
        self.ensure_schema()
        stamp = now or _utc_now()
        row_key = f"{self._GENERATION_LEASE_PREFIX}{key}"
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "SELECT value FROM workflow_runtime_meta WHERE key=?", (row_key,)
            ).fetchone()
            if cur is None:
                conn.execute(
                    "INSERT INTO workflow_runtime_meta (key, value, updated_at) VALUES (?, ?, ?)",
                    (row_key, h, stamp),
                )
                conn.execute("COMMIT")
                return True
            conn.execute("ROLLBACK")
            return str(cur[0]) == h
        finally:
            conn.close()

    def generation_lease_holder(self, key: str) -> Optional[str]:
        """Return the current durable review-generation lease holder for ``key`` (or ``None``)."""
        row_key = f"{self._GENERATION_LEASE_PREFIX}{key}"
        conn = self._connect_ro()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT value FROM workflow_runtime_meta WHERE key=?", (row_key,)
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row is not None else None

    def read_meta(self) -> dict[str, str]:
        """Return the persisted advisory meta as ``{key: value}``; empty if absent.

        The reserved review-generation lease rows (``genlease:`` prefix, #13518 R3-F2) are excluded
        so they never leak into the advisory runtime meta vocabulary.
        """
        conn = self._connect_ro()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT key, value FROM workflow_runtime_meta"
            ).fetchall()
        finally:
            conn.close()
        return {
            r[0]: r[1] for r in rows if not str(r[0]).startswith(self._GENERATION_LEASE_PREFIX)
        }

    def ensure_schema(self) -> None:
        """Create / migrate the container to the current schema version (idempotent).

        Public so the sibling callback-outbox bounded context
        (:mod:`mozyo_bridge.core.state.callback_outbox`), which shares this same
        ``workflow-runtime.sqlite`` file, can drive the v1->v2 migration through the one
        schema authority before opening its own manual-transaction connection.
        """
        self._connect_rw().close()


__all__ = (
    "WORKFLOW_RUNTIME_STORE_FILENAME",
    "WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION",
    "META_READY_INDEPENDENT",
    "META_READY_OVERLAP",
    "META_CAPACITY",
    "META_OWNER_OR_RELEASE_GATE",
    "EVENT_COLUMNS",
    "ROUTE_COLUMNS",
    "CALLBACK_COLUMNS",
    "CALLBACK_PENDING",
    "CALLBACK_INFLIGHT",
    "CALLBACK_DELIVERED",
    "CALLBACK_UNCERTAIN",
    "CALLBACK_DEAD_LETTER",
    "CALLBACK_ABSENT",
    "CALLBACK_STATES",
    "CALLBACK_DEFAULT_MAX_ATTEMPTS",
    "WorkflowRuntimeStoreError",
    "workflow_runtime_store_path",
    "WorkflowEventRow",
    "WorkflowRouteRow",
    "WorkflowRuntimeStore",
)
