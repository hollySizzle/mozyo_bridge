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

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE
from mozyo_bridge.core.state.state_store import (
    BACKUPS_DIRNAME,
    STATE_CONTAINER_VERSION,
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)


LANE_LIFECYCLE_COMPONENT = "lane_lifecycle"
#: v2 split the durable decision anchor's issue (``decision_issue_id``) from the lane's
#: owner binding. v3 (Redmine #13763 j#78052) adds the receiver-replacement generation on
#: the same row/revision as disposition and release.
#: v4 (Redmine #13754, integration j#78705) adds ``worktree_identity`` — the lane's
#: canonical worktree binding, so ``sublane retire --execute`` proves the caller's
#: ``--worktree`` from a fail-closed authority (not the display-only ``lane_metadata``).
#: A v1/v2/v3 row lands with an empty binding — a known-unbound lane whose execute retire
#: fails closed until it is re-declared. (This is the collision fix: #13754's worktree
#: field takes the NEXT free version v4, so it never clashes with #13763's v3 shape.)
LANE_LIFECYCLE_SCHEMA_VERSION = 4
#: The component shapes this build can read and write. ``1``/``2``/``3`` are migrated
#: additively to ``4``; anything else — a newer version from a future build, or a foreign
#: value — fails closed and the store is left untouched (R3-F1).
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})
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
    worktree_identity TEXT NOT NULL DEFAULT '',
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
    "decision_issue_id, decision_journal, created_at, updated_at, worktree_identity"
)

#: The exact column set each recognized version's table must carry — the shape a
#: migration / current-verify classifies against (Redmine #13754 R5-F1, j#78778). A
#: recorded-current store whose table is missing a required column, or a recorded-older
#: store whose shape does not match its known predecessor, is a corrupt / partial /
#: incompatible authority shape: it fails closed and is **never** silently re-created or
#: default-repaired. v1 is the pre-#13689-R2 base; each later version ADDS its delta.
_V1_COLUMNS = frozenset(
    {
        "repo_workspace_id",
        "lane_id",
        "issue_id",
        "lane_disposition",
        "process_release",
        "revision",
        "release_action_id",
        "release_pins",
        "decision_source",
        "decision_journal",
        "created_at",
        "updated_at",
    }
)
_V2_ADDS = frozenset({"decision_issue_id"})  # R2-F1: split the decision anchor's issue
_V3_ADDS = frozenset(
    {"replacement_state", "replacement_action_id", "replacement_pins"}  # #13763
)
_V4_ADDS = frozenset({"worktree_identity"})  # #13754 worktree binding
#: The REQUIRED (floor) columns for each recorded version. v3 is the collision point where
#: two branches diverge — the staging replacement-v3 (adds replacement_*) and the
#: already-live #13754 worktree-v3 (adds worktree_identity) — so a recorded-v3 store may
#: legitimately carry EITHER extra group. Its guaranteed floor is therefore the v2 shape;
#: the forward migration adds whichever of {replacement_*, worktree_identity} is missing to
#: converge on the full v4 shape. v4 is that convergence: BOTH groups are required, so a v4
#: store missing any of them is corrupt and fails closed (R5-F1).
_REQUIRED_COLUMNS_BY_VERSION = {
    1: _V1_COLUMNS,
    2: _V1_COLUMNS | _V2_ADDS,
    3: _V1_COLUMNS | _V2_ADDS,  # v2 floor — the two v3 branches diverge above it
    4: _V1_COLUMNS | _V2_ADDS | _V3_ADDS | _V4_ADDS,
}


class LaneLifecycleError(RuntimeError):
    """The lifecycle store is unusable (unreadable / unsupported); fail closed."""


def lane_lifecycle_path(home: Path | None = None) -> Path:
    """The consolidated single state DB this component lives in (state.sqlite)."""
    return state_store_path(home)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup_stamp(now: str) -> str:
    """Compact filesystem-safe stamp (``20260621T130000Z``) for a backup dir."""
    parsed = datetime.fromisoformat(now)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Best-effort ``ROLLBACK`` so a failed migration leaves the store byte-unchanged."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        pass


def _stamp_component_version(conn: sqlite3.Connection) -> None:
    """Register / re-stamp this component at the current schema version (native form)."""
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


def backup_state_container(path: Path) -> Optional[Path]:
    """Copy an existing ``state.sqlite`` into ``backups/state-<ts>/`` before a write.

    A **component** write migration (an additive ``ALTER`` on authoritative rows) must
    honor ``managed-state-model.md`` (``### backup / downgrade / partial migration``)
    like the container's legacy import does: copy the DB under home before the first
    write; a copy failure raises :class:`StateStoreError` so the caller fails closed with
    the DB byte-unchanged. Returns the backup dir, or ``None`` when there is nothing to
    preserve yet (a fresh store has no prior authority).

    The backup directory **never overwrites an existing snapshot** (Redmine #13754
    R4-F1): the second-precision stamp can collide, so a taken directory gets a numeric
    suffix (``…-1``, ``…-2``) rather than a clobbering ``copy2`` over a prior backup.
    Migration is serialized upstream, so this is defense in depth — a pre-migration
    snapshot is preserved even if two backups ever share a second.
    """
    if not path.exists():
        return None
    base = path.parent / BACKUPS_DIRNAME / f"state-{_backup_stamp(_utc_now())}"
    try:
        backup_dir = base
        suffix = 1
        while backup_dir.exists():
            backup_dir = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        backup_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(path, backup_dir / path.name)
    except OSError as exc:
        raise StateStoreError(
            f"backup near {base} failed ({exc}); migration aborted "
            f"(nothing was written)"
        ) from exc
    return backup_dir

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
    # Serialize the whole migration under one exclusive write lock (Redmine #13754 R4-F1):
    # ``BEGIN IMMEDIATE`` takes the reserved lock BEFORE the version is read, so a
    # concurrent first-use caller cannot read the same pre-migration version, back up, and
    # overwrite the only pre-migration snapshot with a post-migration copy. The lock is
    # cross-process (SQLite file locks + the container guard's ``busy_timeout``); a second
    # migrator blocks, re-reads the now-current version, and does nothing. The container
    # guard's connection is default-isolation; switch it to autocommit so an explicit
    # ``BEGIN IMMEDIATE`` (not Python's implicit deferred transaction) governs the section.
    conn.isolation_level = None
    locked = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked = True
        # Read the recorded component version UNDER the lock and BEFORE any DDL/DML: the
        # downgrade refusal below must leave the store byte-equivalent, and this read is
        # now authoritative (no concurrent migrator can move it out from under us).
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
        # Classify the recorded version AGAINST the actual table shape before any DDL/DML
        # (Redmine #13754 R5-F1): the migration must never silently re-create a missing
        # table or default-repair a missing authority column on a store that *claims* to be
        # complete. ``CREATE TABLE IF NOT EXISTS`` + idempotent ``ALTER`` would do exactly
        # that. Instead: a recognized recorded version must match its known shape, or the
        # store is a corrupt / partial / incompatible authority and fails closed
        # (``managed-state-model.md``: partial migration is never a success, an authoritative
        # component is never silently overwritten, repair is an explicit path).
        table_exists = _table_present(conn, _TABLE)
        current_columns = (
            {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
            if table_exists
            else set()
        )
        if recorded is None:
            # A component this build never registered. Only a genuinely fresh store (no
            # table) is a create; a table *without* its metadata row is a partial / unknown
            # state — fail closed exactly like the read-side ``readonly_component_status``.
            if table_exists:
                raise LaneLifecycleError(
                    "lane lifecycle table exists without a component metadata row "
                    "(partial / unknown state); fail closed (no silent adoption)."
                )
            conn.execute(_TABLE_SQL)
            conn.execute(_OWNER_INDEX_SQL)
            _stamp_component_version(conn)
        elif recorded == LANE_LIFECYCLE_SCHEMA_VERSION:
            # A store that records the CURRENT version. It is a no-op only when its shape
            # actually IS current; a missing table or a missing required column is a corrupt
            # current store — fail closed, never re-create / default-repair / re-stamp it.
            required = _REQUIRED_COLUMNS_BY_VERSION[LANE_LIFECYCLE_SCHEMA_VERSION]
            if not table_exists or not required.issubset(current_columns):
                raise LaneLifecycleError(
                    f"lane lifecycle records v{LANE_LIFECYCLE_SCHEMA_VERSION} but its table "
                    "is missing / lacks a required column (corrupt / partial current "
                    "shape); fail closed (no silent repair). Restore from a backup."
                )
            # Intact current: nothing to write. Do NOT re-run DDL or re-stamp the metadata.
        else:
            # A recognized OLDER version (1/2/3). Migrate forward ONLY when its table matches
            # that version's known predecessor shape; a shape mismatch (missing an older
            # required column, or no table) is a corrupt / incompatible store — fail closed
            # rather than migrate an unknown shape. This preserves BOTH v3 shapes: the
            # staging replacement-v3 and the (already-live) #13754 worktree-v3 each carry
            # their version's required columns and migrate additively to v4.
            required = _REQUIRED_COLUMNS_BY_VERSION[recorded]
            if not table_exists or not required.issubset(current_columns):
                raise LaneLifecycleError(
                    f"lane lifecycle records v{recorded} but its table is missing / lacks a "
                    f"column that version requires (corrupt / incompatible predecessor "
                    f"shape); fail closed (no silent repair)."
                )
            # Backup-first (Redmine #13754 R3-F1): copy the DB under home BEFORE the first
            # ``ALTER``; a backup failure aborts with nothing written. Under the lock the DB
            # on disk is the committed pre-``ALTER`` state and no other writer can change it
            # during the copy, so the snapshot is a faithful, unique pre-migration copy.
            try:
                backup_state_container(path)
            except StateStoreError as exc:
                raise LaneLifecycleError(
                    f"lane lifecycle migration to v{LANE_LIFECYCLE_SCHEMA_VERSION} "
                    f"aborted: {exc}. The store is left untouched (backup-first)."
                ) from exc
            # One atomic transaction (Redmine #13754 R4-F2): the additive ``ALTER``s that
            # bring the older shape up to v4 and the version re-stamp run inside this one
            # ``BEGIN IMMEDIATE`` block, so a failure part-way rolls the whole migration back
            # (schema + recorded version stay at the predecessor) and the backup remains the
            # recovery point. Each ``ALTER`` only ADDS a column the older version legitimately
            # lacks — an existing row lands with the column's empty default (a known-unbound
            # / known-incomplete field), never a silently-guessed authority value.
            if "decision_issue_id" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN decision_issue_id TEXT NOT NULL DEFAULT ''"
                )
            if "replacement_state" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN replacement_state TEXT NOT NULL DEFAULT 'not_requested'"
                )
            if "replacement_action_id" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN replacement_action_id TEXT NOT NULL DEFAULT ''"
                )
            if "replacement_pins" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN replacement_pins TEXT NOT NULL DEFAULT ''"
                )
            if "worktree_identity" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN worktree_identity TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(_OWNER_INDEX_SQL)
            _stamp_component_version(conn)
        conn.execute("COMMIT")
        locked = False
    except sqlite3.DatabaseError as exc:
        if locked:
            _rollback_quietly(conn)
        raise LaneLifecycleError(
            f"lane lifecycle schema init failed ({type(exc).__name__}); fail closed"
        ) from exc
    except LaneLifecycleError:
        if locked:
            _rollback_quietly(conn)
        raise
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
