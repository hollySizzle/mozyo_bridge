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
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)


LANE_LIFECYCLE_COMPONENT = "lane_lifecycle"
#: v2 (Redmine #13689 R2-F1): splits the durable decision anchor's issue
#: (``decision_issue_id``) from the lane's owner binding (``issue_id``). A Redmine
#: journal is only addressable through its issue, so an anchor without one names
#: nothing — and an unbound lane legitimately has no binding.
LANE_LIFECYCLE_SCHEMA_VERSION = 2
#: The component shapes this build can read and write. ``1`` is migrated additively
#: to ``2``; anything else — a newer version from a future build, or a foreign value —
#: fails closed and the store is left untouched (R3-F1).
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2})
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
    "revision, release_action_id, release_pins, decision_source, "
    "decision_issue_id, decision_journal, created_at, updated_at"
)


class LaneLifecycleError(RuntimeError):
    """The lifecycle store is unusable (unreadable / unsupported); fail closed."""


def lane_lifecycle_path(home: Path | None = None) -> Path:
    """The consolidated single state DB this component lives in (state.sqlite)."""
    return state_store_path(home)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _recorded_version(conn: sqlite3.Connection) -> Optional[int]:
    """This component's recorded ``state_schema_components`` version, or ``None``.

    ``None`` means the component has never registered — a fresh install, which this
    build may create. A *present* version is the store's own statement of the shape
    its rows are in, and only this build's recognized versions may be written under.
    """
    try:
        row = conn.execute(
            "SELECT schema_version FROM state_schema_components WHERE component = ?",
            (LANE_LIFECYCLE_COMPONENT,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # A non-integer version is not a version we recognize; treat it as one so the
        # caller fails closed rather than assuming a fresh install and re-stamping it.
        return -1


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
            raise LaneLifecycleError(
                f"lane lifecycle component is at schema version {recorded}; this "
                f"build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. The "
                f"store is left untouched (downgrade-safe); use a newer build."
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
    "COLUMNS",
    "TABLE",
    "LaneLifecycleError",
    "ensure_lane_lifecycle_schema",
    "lane_lifecycle_path",
)
