"""Lane lifecycle — schema, component registration, downgrade guard (Redmine #13689).

The component's *shape* concern, kept apart from the CAS writes in
:mod:`mozyo_bridge.core.state.lane_lifecycle`: which table it owns, how it registers
itself in ``state_schema_components``, which versions this build understands, and —
the part that carries the safety (R3-F1) — what it refuses to touch.

The container guard (``PRAGMA user_version``) is **not** a component guard. A store
whose ``lane_lifecycle`` component records a version this build does not know is left
completely untouched: no table create, no migration, no metadata re-stamp. Its rows
are lifecycle *authority*, and re-stamping them down to a shape we understand is
exactly how an old build starts moving state whose newer semantics it does not agree
to (``managed-state-model.md`` ``### backup / downgrade / partial migration``: an
older CLI seeing a newer container *or component* schema reports unsupported and
leaves the DB untouched).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE
from mozyo_bridge.core.state.state_store import (
    STATE_CONTAINER_VERSION,
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)


LANE_LIFECYCLE_COMPONENT = "lane_lifecycle"
#: v3 (Redmine #13763 j#78052): adds the receiver-replacement generation on the
#: same row/revision as disposition and release. v2 split the durable decision
#: anchor's issue (``decision_issue_id``) from the lane's owner binding.
LANE_LIFECYCLE_SCHEMA_VERSION = 3
#: The component shapes this build can read and write. ``1`` and ``2`` are migrated
#: additively to ``3``; anything else — a newer version from a future build, or a foreign value —
#: fails closed and the store is left untouched (R3-F1).
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2, 3})
#: A coordinator decision that cannot be rebuilt from events; loss requires an
#: explicit re-declare from the Redmine durable pointer.
LANE_LIFECYCLE_RECOVERY_POLICY = "operator_current_state"

_TABLE = "lane_lifecycle_records"
_OWNER_INDEX = "idx_lane_lifecycle_active_owner"

_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    repo_workspace_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    issue_id TEXT NOT NULL DEFAULT '',
    lane_disposition TEXT NOT NULL,
    process_release TEXT NOT NULL,
    revision INTEGER NOT NULL,
    release_action_id TEXT NOT NULL DEFAULT '',
    release_pins TEXT NOT NULL DEFAULT '',
    replacement_state TEXT NOT NULL DEFAULT 'not_requested',
    replacement_action_id TEXT NOT NULL DEFAULT '',
    replacement_pins TEXT NOT NULL DEFAULT '',
    decision_source TEXT NOT NULL DEFAULT '',
    decision_issue_id TEXT NOT NULL DEFAULT '',
    decision_journal TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_workspace_id, lane_id)
)
"""

#: At most one ACTIVE owner per (workspace, issue) — enforced by the storage
#: engine, so "original + recovery both own the issue" is unrepresentable rather
#: than merely detected afterwards. Scoped to the workspace (Design Answer D2): a
#: home-global unique would collide across unrelated projects. Rows with an empty
#: issue (a lane not bound to an issue yet) are exempt.
_OWNER_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_OWNER_INDEX}
ON {_TABLE} (repo_workspace_id, issue_id)
WHERE lane_disposition = '{DISPOSITION_ACTIVE}' AND issue_id <> ''
"""

_COLUMNS = (
    "repo_workspace_id, lane_id, issue_id, lane_disposition, process_release, "
    "revision, release_action_id, release_pins, replacement_state, "
    "replacement_action_id, replacement_pins, decision_source, "
    "decision_issue_id, decision_journal, created_at, updated_at"
)


class LaneLifecycleError(RuntimeError):
    """The lifecycle store is unusable (unreadable / unsupported); fail closed."""


def lane_lifecycle_path(home: Path | None = None) -> Path:
    """The consolidated single state DB this component lives in (state.sqlite)."""
    return state_store_path(home)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

#: Sentinel for a component row whose version is present but not an exact integer
#: (a REAL like ``2.5``, TEXT, BLOB, …). It is deliberately outside
#: :data:`_RECOGNIZED_SCHEMA_VERSIONS` so the caller fails closed on it (R4-F1).
_VERSION_MALFORMED = -1


def _recorded_version(conn: sqlite3.Connection) -> Optional[int]:
    """This component's recorded ``state_schema_components`` version, or ``None``.

    Three outcomes, kept distinct (R5-F1):

    - ``None`` — the component row is **absent**. Only this is a fresh install this
      build may create.
    - :data:`_VERSION_MALFORMED` — the row is **present but unusable**: a ``NULL``
      version, a REAL ``2.5``, TEXT, BLOB, or a version-query failure after the
      container was already initialized. A *present* row is the store's own
      statement about its rows; a broken one is not "never registered", it is an
      unknown state, and re-stamping it to v2 would let this build write authority
      rows it does not understand.

    A present-but-malformed value is never a coerced number (R4-F1): ``int(2.5)``
    would truncate a ``2.5`` REAL to ``2`` and pass the recognized-version check.
    Both the SQLite storage class (``typeof``) and the returned Python type must say
    integer, and the value must not be ``NULL``.
    """
    try:
        row = conn.execute(
            "SELECT typeof(schema_version), schema_version "
            "FROM state_schema_components WHERE component = ?",
            (LANE_LIFECYCLE_COMPONENT,),
        ).fetchone()
    except sqlite3.DatabaseError:
        # The container guard has already created `state_schema_components`; a query
        # failing *now* is a broken store, not a fresh one. Fail closed.
        return _VERSION_MALFORMED
    if row is None:
        return None  # genuinely never registered — a fresh install
    storage_class, value = row
    # A present row whose version is NULL / a non-integer REAL / TEXT / BLOB is a
    # malformed record, distinct from an absent row. `bool` is an `int` subclass and
    # is not a version.
    if (
        value is None
        or storage_class != "integer"
        or not isinstance(value, int)
        or isinstance(value, bool)
    ):
        return _VERSION_MALFORMED
    return value


#: Read-only schema-classification outcomes (Redmine #13681 R3-F1, j#77307).
READONLY_COMPONENT_ABSENT = "absent"
READONLY_COMPONENT_RECOGNIZED = "recognized"
READONLY_COMPONENT_UNSUPPORTED = "unsupported"


def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def readonly_component_status(conn: sqlite3.Connection) -> str:
    """Classify this component for a NON-CREATING read (Redmine #13681 R3-F1, j#77307).

    A read-only mirror of the write-side downgrade guard in
    :func:`ensure_lane_lifecycle_schema`: it never writes (no DDL, no metadata upsert),
    and returns one of

    - :data:`READONLY_COMPONENT_ABSENT` — a **recognized container** whose lifecycle
      component is completely absent (neither its metadata row nor its table exists). The
      caller reads no rows without creating anything.
    - :data:`READONLY_COMPONENT_RECOGNIZED` — a recognized container AND a recorded
      component ``schema_version`` this build understands
      (:data:`_RECOGNIZED_SCHEMA_VERSIONS`) AND the table is present. The caller may read.
    - :data:`READONLY_COMPONENT_UNSUPPORTED` — a **newer / unknown container**
      ``PRAGMA user_version`` (R4-F1, j#77322), an unknown / newer / malformed component
      version, a metadata row without its table (a partial / migrating store), a table
      without its metadata / a missing components registry, or a query failure.

    The container ``PRAGMA user_version`` is checked FIRST and must equal the exact
    :data:`STATE_CONTAINER_VERSION` — mirroring the write-side ``connect_state_container_rw``
    — so a store written by a newer build fails closed here too. An older build never reads
    authority rows whose newer container *or component* semantics it does not agree to
    (``managed-state-model.md`` ``### backup / downgrade / partial migration``).
    """
    try:
        container_version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    if container_version != STATE_CONTAINER_VERSION:
        # A newer / unknown container schema — fail closed exactly like the write-side
        # container guard's exact-version enforcement (never read a downgraded store).
        return READONLY_COMPONENT_UNSUPPORTED
    try:
        has_meta = _table_present(conn, "state_schema_components")
        has_table = _table_present(conn, _TABLE)
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    if not has_meta:
        # No components registry at all — only a genuinely fresh store (no table either).
        return (
            READONLY_COMPONENT_ABSENT
            if not has_table
            else READONLY_COMPONENT_UNSUPPORTED
        )
    recorded = _recorded_version(conn)
    if recorded is None:
        # The component was never registered.
        return (
            READONLY_COMPONENT_ABSENT
            if not has_table
            else READONLY_COMPONENT_UNSUPPORTED
        )
    if recorded not in _RECOGNIZED_SCHEMA_VERSIONS:
        # A newer / malformed component version -> unsupported (downgrade-safe).
        return READONLY_COMPONENT_UNSUPPORTED
    # A recognized version is only readable when its table is actually present.
    return (
        READONLY_COMPONENT_RECOGNIZED if has_table else READONLY_COMPONENT_UNSUPPORTED
    )


def ensure_lane_lifecycle_schema(path: Path) -> None:
    """Create / validate the container, this component's table + owner index.

    Uses the shared container guard (``PRAGMA user_version`` +
    ``state_schema_components``), then registers this component with no
    ``migrated_from`` — the native-component registration form.

    **A newer component schema fails closed and the DB is left untouched**
    (R3-F1). The container guard only checks the *container* version; a build
    that does not understand this component's own recorded version must not
    create tables, run migrations, or re-stamp the metadata under it, because the
    rows it would then write are lifecycle **authority** — updated with semantics
    the newer schema does not agree to. This is the contract in
    ``managed-state-model.md`` (``### backup / downgrade / partial migration``:
    an older CLI seeing a newer container *or component* schema reports
    unsupported and leaves the DB untouched), and the discipline the sibling
    :mod:`...workflow_runtime_store` already applies.
    """
    try:
        conn = connect_state_container_rw(path)
    except StateStoreError as exc:
        raise LaneLifecycleError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        # An unreadable / non-SQLite file: fail closed rather than surface a raw
        # driver error into a caller that would read it as "no lifecycle state".
        raise LaneLifecycleError(
            f"lane lifecycle store {path} is unreadable "
            f"({type(exc).__name__}); fail closed"
        ) from exc
    try:
        # Read the recorded component version BEFORE any DDL/DML: the refusal
        # below must leave the store byte-equivalent, and `CREATE TABLE IF NOT
        # EXISTS` / `ALTER` / the metadata upsert would each already be a write
        # under a schema we do not understand.
        recorded = _recorded_version(conn)
        if recorded is not None and recorded not in _RECOGNIZED_SCHEMA_VERSIONS:
            detail = (
                "a present-but-malformed value (not an exact integer)"
                if recorded == _VERSION_MALFORMED
                else f"version {recorded}"
            )
            raise LaneLifecycleError(
                f"lane lifecycle component records {detail}; this build understands "
                f"{sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. The store is left untouched "
                f"(downgrade-safe); use a newer build."
            )
        with conn:
            conn.execute(_TABLE_SQL)
            # v1 -> v2 (R2-F1): additive, mirroring the sibling native component's
            # ``lane_metadata`` v2 migration. A v1 row's anchor kept only the
            # journal, so its ``decision_issue_id`` lands empty — that row is a
            # known-incomplete anchor, not a silently-repaired one.
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")
            }
            if "decision_issue_id" not in columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN decision_issue_id TEXT NOT NULL DEFAULT ''"
                )
            # v2 -> v3 (Redmine #13763 j#78052): an additive third lifecycle
            # axis for owner-approved receiver replacement. These fields share
            # the row revision with disposition / release so their actuators
            # cannot race past each other.
            if "replacement_state" not in columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN replacement_state TEXT NOT NULL DEFAULT 'not_requested'"
                )
            if "replacement_action_id" not in columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN replacement_action_id TEXT NOT NULL DEFAULT ''"
                )
            if "replacement_pins" not in columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN replacement_pins TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(_OWNER_INDEX_SQL)
            conn.execute(
                "INSERT INTO state_schema_components "
                "(component, schema_version, owner, recovery_policy, "
                "migrated_from, updated_at) VALUES (?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(component) DO UPDATE SET "
                "schema_version = excluded.schema_version, "
                "owner = excluded.owner, "
                "recovery_policy = excluded.recovery_policy, "
                "updated_at = excluded.updated_at",
                (
                    LANE_LIFECYCLE_COMPONENT,
                    LANE_LIFECYCLE_SCHEMA_VERSION,
                    "core/state/lane_lifecycle.py",
                    LANE_LIFECYCLE_RECOVERY_POLICY,
                    _utc_now(),
                ),
            )
    except sqlite3.DatabaseError as exc:
        raise LaneLifecycleError(
            f"lane lifecycle schema init failed ({type(exc).__name__}); fail closed"
        ) from exc
    finally:
        conn.close()


TABLE = _TABLE
COLUMNS = _COLUMNS


__all__ = (
    "LANE_LIFECYCLE_COMPONENT",
    "LANE_LIFECYCLE_RECOVERY_POLICY",
    "LANE_LIFECYCLE_SCHEMA_VERSION",
    "READONLY_COMPONENT_ABSENT",
    "READONLY_COMPONENT_RECOGNIZED",
    "READONLY_COMPONENT_UNSUPPORTED",
    "COLUMNS",
    "TABLE",
    "LaneLifecycleError",
    "ensure_lane_lifecycle_schema",
    "lane_lifecycle_path",
    "readonly_component_status",
)
