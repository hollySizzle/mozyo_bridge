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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    BINDING_KIND_PROJECT_GATEWAY,
    DISPOSITION_ACTIVE,
)
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
#: v5 (Redmine #13810, Design Answer j#78386) adds the binding/generation/declaration
#: triple: ``binding_kind`` (issue | project_gateway), full ``project_scope``,
#: ``lane_generation`` (positive monotonic incarnation), and the versioned
#: ``declared_slots`` :class:`ProcessGenerationPin` snapshot. A pre-v5 row migrates with
#: ``binding_kind='issue'`` / empty scope / generation 1 / empty slots — its ownership is
#: never re-derived as project scope from the lane id (j#78386 §6). j#78386's original
#: "v4" number is deliberately NOT reused: staging is already v4 (worktree binding), so
#: this tranche takes the next free version v5 (j#78860 item 2).
#: v6 (Redmine #13842 review j#79363 R6) adds ``reconcile_phase`` — the collision-proof
#: provenance the hibernated live-contradiction reconcile writes (``'reconciled'``) so a
#: retired row can be told apart from an ordinary #13809 / #13810-bound retired row when the
#: reconcile resumes its own owed pane close after a crash. It lives ON the authoritative row
#: (co-located, recovered by the component's own ``operator_current_state`` re-declare) rather
#: than a separate losable cache: a load-bearing owed-state marker must not be a rebuildable
#: cache (R6). A pre-v6 row migrates with an empty phase (an ordinary / non-reconcile row).
#: v7 (Redmine #13647 Tranche 1b) adds ``lane_kind`` — the resolved delegation-geometry
#: kind (``coordinator`` | ``delegated_coordinator`` | ``implementation``) the lane was
#: created with, stored generation-bound so a heal resolves the SAME lane-role placement
#: OFFLINE (never re-read from Redmine, never from the display cache). It is the launch
#: path's *heal authority* for lane-role pane placement (the fresh-launch authority stays
#: the caller-supplied ``LaneLaunchContext``); ``lane_kind`` is immutable within a
#: generation — a governance change re-binds it on a NEW generation, never an in-place
#: overwrite. A pre-v7 row migrates with an empty ``lane_kind`` (no durable kind fact → the
#: launch path falls back to ``lane_class`` geometry, the issue's close condition), never a
#: guessed value.
LANE_LIFECYCLE_SCHEMA_VERSION = 7
#: The component shapes this build can read and write. ``1``–``6`` are migrated
#: additively to ``7``; anything else — a newer version from a future build, or a foreign
#: value — fails closed and the store is left untouched (R3-F1).
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7})
#: A coordinator decision that cannot be rebuilt from events; loss requires an
#: explicit re-declare from the Redmine durable pointer.
LANE_LIFECYCLE_RECOVERY_POLICY = "operator_current_state"

_TABLE = "lane_lifecycle_records"
_OWNER_INDEX = "idx_lane_lifecycle_active_owner"
_PROJECT_OWNER_INDEX = "idx_lane_lifecycle_active_project_owner"

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
    binding_kind TEXT NOT NULL DEFAULT 'issue',
    project_scope TEXT NOT NULL DEFAULT '',
    lane_generation INTEGER NOT NULL DEFAULT 1,
    declared_slots TEXT NOT NULL DEFAULT '',
    reconcile_phase TEXT NOT NULL DEFAULT '',
    lane_kind TEXT NOT NULL DEFAULT '',
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

#: At most one ACTIVE project-gateway owner per (workspace, project_scope) — v5
#: (Redmine #13810). The storage-engine twin of the issue owner index: a
#: project-gateway lane owns a full ``project_scope``, not an issue, so double
#: project ownership is made unrepresentable on the *scope*, not the (empty) issue.
#: Scoped to ``binding_kind='project_gateway'`` so an issue lane — which always has an
#: empty ``project_scope`` — is never caught by it, and to a non-empty scope so a
#: not-yet-scoped row is exempt (mirroring the issue index's ``issue_id <> ''``).
_PROJECT_OWNER_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_PROJECT_OWNER_INDEX}
ON {_TABLE} (repo_workspace_id, project_scope)
WHERE lane_disposition = '{DISPOSITION_ACTIVE}'
  AND binding_kind = '{BINDING_KIND_PROJECT_GATEWAY}' AND project_scope <> ''
"""

_COLUMNS = (
    "repo_workspace_id, lane_id, issue_id, lane_disposition, process_release, "
    "revision, release_action_id, release_pins, replacement_state, "
    "replacement_action_id, replacement_pins, decision_source, "
    "decision_issue_id, decision_journal, created_at, updated_at, worktree_identity, "
    "binding_kind, project_scope, lane_generation, declared_slots, reconcile_phase, "
    "lane_kind"
)

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
_V5_ADDS = frozenset(
    {"binding_kind", "project_scope", "lane_generation", "declared_slots"}  # #13810
)
_V6_ADDS = frozenset({"reconcile_phase"})  # #13842 reconcile owed-close provenance
_V7_ADDS = frozenset({"lane_kind"})  # #13647 generation-bound lane-role heal authority

#: The EXACT allowed column-name signatures per recorded version (Redmine #13754 R6-F1,
#: j#78803). A recognized store must match one of its version's signatures EXACTLY (set
#: equality — no unknown extra columns, no missing columns), or it is a partial /
#: incompatible authority shape and fails closed (never silently re-created, migrated, or
#: re-stamped). v3 is the collision point where TWO branches legitimately exist — the
#: staging replacement-v3 and the already-live #13754 worktree-v3 — so v3 allows exactly
#: those two shapes and NOTHING ELSE (a pure-v2 shape recorded v3 is NOT a known v3).
_SHAPE_V1 = _V1_COLUMNS
_SHAPE_V2 = _V1_COLUMNS | _V2_ADDS
_SHAPE_V3_REPLACEMENT = _V1_COLUMNS | _V2_ADDS | _V3_ADDS
_SHAPE_V3_WORKTREE = _V1_COLUMNS | _V2_ADDS | _V4_ADDS
_SHAPE_V4 = _V1_COLUMNS | _V2_ADDS | _V3_ADDS | _V4_ADDS
_SHAPE_V5 = _V1_COLUMNS | _V2_ADDS | _V3_ADDS | _V4_ADDS | _V5_ADDS
_SHAPE_V6 = _V1_COLUMNS | _V2_ADDS | _V3_ADDS | _V4_ADDS | _V5_ADDS | _V6_ADDS
_SHAPE_V7 = _SHAPE_V6 | _V7_ADDS
_ALLOWED_SHAPES_BY_VERSION: dict[int, tuple[frozenset, ...]] = {
    1: (_SHAPE_V1,),
    2: (_SHAPE_V2,),
    3: (_SHAPE_V3_REPLACEMENT, _SHAPE_V3_WORKTREE),
    4: (_SHAPE_V4,),
    5: (_SHAPE_V5,),
    6: (_SHAPE_V6,),
    7: (_SHAPE_V7,),
}

#: The authority-affecting definition each column MUST carry: ``(type, notnull, default,
#: pk_order)`` as ``PRAGMA table_info`` reports it. A same-named but re-typed / nullable /
#: default-changed / PK-shifted column is NOT the current column — it fails closed rather
#: than being read as authoritative (R6-F1). ``pk_order`` is the 1-based composite-PK
#: position (0 for a non-PK column); ``default`` is the SQL literal ``table_info`` returns.
_COLUMN_DEFS: dict[str, tuple[str, int, Optional[str], int]] = {
    "repo_workspace_id": ("TEXT", 1, None, 1),
    "lane_id": ("TEXT", 1, None, 2),
    "issue_id": ("TEXT", 1, "''", 0),
    "lane_disposition": ("TEXT", 1, None, 0),
    "process_release": ("TEXT", 1, None, 0),
    "revision": ("INTEGER", 1, None, 0),
    "release_action_id": ("TEXT", 1, "''", 0),
    "release_pins": ("TEXT", 1, "''", 0),
    "replacement_state": ("TEXT", 1, "'not_requested'", 0),
    "replacement_action_id": ("TEXT", 1, "''", 0),
    "replacement_pins": ("TEXT", 1, "''", 0),
    "decision_source": ("TEXT", 1, "''", 0),
    "decision_issue_id": ("TEXT", 1, "''", 0),
    "decision_journal": ("TEXT", 1, "''", 0),
    "created_at": ("TEXT", 1, None, 0),
    "updated_at": ("TEXT", 1, None, 0),
    "worktree_identity": ("TEXT", 1, "''", 0),
    "binding_kind": ("TEXT", 1, "'issue'", 0),
    "project_scope": ("TEXT", 1, "''", 0),
    "lane_generation": ("INTEGER", 1, "1", 0),
    "declared_slots": ("TEXT", 1, "''", 0),
    "reconcile_phase": ("TEXT", 1, "''", 0),
    "lane_kind": ("TEXT", 1, "''", 0),
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


#: The active-owner index's authority-defining shape (Redmine #13754 R7-F1, j#78814): the
#: key columns in order, and the partial predicate that scopes uniqueness to ACTIVE,
#: issue-bound rows. A same-named index that is non-unique, keyed on other columns, scoped
#: by a different predicate, or attached to another table does NOT enforce exactly-one
#: active owner — it is not this constraint and fails closed.
_OWNER_INDEX_KEY_COLUMNS = ("repo_workspace_id", "issue_id")
_OWNER_INDEX_PREDICATE = "lane_disposition = 'active' AND issue_id <> ''"

#: The v5 project-gateway owner index's authority-defining shape (Redmine #13810), the
#: twin of :data:`_OWNER_INDEX_PREDICATE`. A same-named index that is non-unique, keyed on
#: other columns, or scoped by a different predicate does NOT enforce exactly-one active
#: project owner and is a corrupt authority shape.
_PROJECT_OWNER_INDEX_KEY_COLUMNS = ("repo_workspace_id", "project_scope")
_PROJECT_OWNER_INDEX_PREDICATE = (
    "lane_disposition = 'active' AND binding_kind = 'project_gateway' "
    "AND project_scope <> ''"
)


def _verify_partial_owner_index(
    conn: sqlite3.Connection,
    *,
    name: str,
    key_columns: tuple[str, ...],
    predicate: str,
) -> bool:
    """Does ``name`` exist on this table as a UNIQUE PARTIAL index with EXACTLY this shape?

    The shared verifier behind both the issue owner index (Redmine #13754 R7-F1) and the
    v5 project-gateway owner index (Redmine #13810): the exactly-one-active-owner invariant
    is enforced by the storage engine only when the index is (1) attached to
    ``lane_lifecycle_records``, (2) UNIQUE, (3) PARTIAL, (4) keyed on ``key_columns`` in
    that order, and (5) scoped by ``predicate``. A same-named index missing any of these
    does not enforce the constraint and is a corrupt authority shape.
    """
    row = conn.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()
    if row is None or row[0] != _TABLE:
        return False  # absent, or attached to a foreign table
    listing = {
        r[1]: (r[2], r[4])  # name -> (unique, partial)
        for r in conn.execute(f"PRAGMA index_list({_TABLE})")
    }
    if listing.get(name) != (1, 1):  # must be UNIQUE and PARTIAL
        return False
    live_key_columns = tuple(
        r[2] for r in conn.execute(f"PRAGMA index_info({name})")
    )
    if live_key_columns != key_columns:  # wrong / reordered key columns
        return False
    # The partial predicate lives only in the stored CREATE text; collapse whitespace
    # first (SQLite preserves the original newlines), then extract after WHERE and compare
    # so formatting does not matter but semantics do.
    sql = " ".join((row[1] or "").split())
    marker = " where "
    where_at = sql.lower().find(marker)
    if where_at == -1:
        return False
    live_predicate = sql[where_at + len(marker) :].strip()
    return live_predicate == predicate


def _verify_owner_index(conn: sqlite3.Connection) -> bool:
    """Does the active-owner unique partial index exist with its EXACT authority shape?

    Not a name check (Redmine #13754 R7-F1): the exactly-one-active-owner invariant is
    enforced by the storage engine only when the index is (1) attached to
    ``lane_lifecycle_records``, (2) UNIQUE, (3) PARTIAL, (4) keyed on
    ``(repo_workspace_id, issue_id)`` in that order, and (5) scoped by the predicate
    ``lane_disposition = 'active' AND issue_id <> ''``. A same-named index missing any of
    these does not enforce the constraint and is a corrupt authority shape.
    """
    return _verify_partial_owner_index(
        conn,
        name=_OWNER_INDEX,
        key_columns=_OWNER_INDEX_KEY_COLUMNS,
        predicate=_OWNER_INDEX_PREDICATE,
    )


def _verify_project_owner_index(conn: sqlite3.Connection) -> bool:
    """Does the v5 project-gateway active-owner unique partial index hold (Redmine #13810)?

    The exact-shape twin of :func:`_verify_owner_index` for the project scope. Only a v5
    store carries it (its predicate names ``binding_kind`` / ``project_scope``, columns
    that exist only from v5), so :func:`_schema_signature_matches` requires it only when
    the recorded version is v5.
    """
    return _verify_partial_owner_index(
        conn,
        name=_PROJECT_OWNER_INDEX,
        key_columns=_PROJECT_OWNER_INDEX_KEY_COLUMNS,
        predicate=_PROJECT_OWNER_INDEX_PREDICATE,
    )


def _schema_signature_matches(conn: sqlite3.Connection, recorded: int) -> bool:
    """Does the live table EXACTLY match one of ``recorded``'s allowed signatures?

    Redmine #13754 R6-F1 (j#78803): the classifier must accept only a *known* shape, not a
    minimum column floor. This verifies, with no mutation:

    - the table exists;
    - its column-NAME set equals one of :data:`_ALLOWED_SHAPES_BY_VERSION` for ``recorded``
      exactly (no unknown extra column, no missing column — a pure-v2 shape recorded v3 is
      rejected, an extra column on a v4 store is rejected);
    - every present column's authority-affecting definition (type / NOT NULL / default /
      PK order) matches :data:`_COLUMN_DEFS` (a same-named but re-typed / nullable column
      is not the current column);
    - the active-owner index enforces its EXACT constraint (:func:`_verify_owner_index`):
      a same-named index that is non-unique / wrong-keyed / wrong-predicate / on a foreign
      table does not enforce exactly-one-active-owner and is a corrupt shape (R7-F1);
    - and, on a v5 store, the project-gateway active-owner index enforces its EXACT
      constraint too (:func:`_verify_project_owner_index`, Redmine #13810). It is required
      only for v5 because its predicate names columns (``binding_kind`` / ``project_scope``)
      that exist only from v5.
    """
    if not _table_present(conn, _TABLE):
        return False
    info = {
        row[1]: (row[2], row[3], row[4], row[5])  # name -> (type, notnull, dflt, pk)
        for row in conn.execute(f"PRAGMA table_info({_TABLE})")
    }
    names = frozenset(info)
    if names not in _ALLOWED_SHAPES_BY_VERSION.get(recorded, ()):
        return False
    for name, definition in info.items():
        if _COLUMN_DEFS.get(name) != definition:
            return False
    if not _verify_owner_index(conn):
        return False
    if recorded >= 5 and not _verify_project_owner_index(conn):
        return False
    return True


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


# -- read-compatible / write-migrating split (Redmine #13844) ----------------
#
# The bug: parallel repo lanes each run a source CLI of a different schema generation,
# but the lifecycle *authority* is home-scoped and shared. A newer-schema source CLI
# that forward-migrates the shared store on a mere READ (status / handoff / review /
# callback / drain routing) fails-closes every concurrent older-schema reader — the
# older reader can no longer read the authority, so ``standard`` handoff stops with
# ``gateway_route_blocked`` (#13813 j#79382). The fix separates *reading* the authority
# (never migrates; a newer build reads an older KNOWN additive shape by padding the
# columns it lacks with their in-memory migration defaults) from *mutating* it (the only
# path that runs the explicit backup-first migration below).


def readonly_compatible_select(conn: sqlite3.Connection) -> Optional[str]:
    """A NON-MUTATING full-width ``SELECT`` column list for a recognized, KNOWN-shape store.

    The read half of the read-compatible / write-migrating split (Redmine #13844). Given a
    store whose recorded component version this build recognizes AND whose live table shape
    EXACTLY matches one of that version's known signatures (:func:`_schema_signature_matches`),
    returns a ``SELECT`` column expression that yields the full current (v6) column vocabulary
    in :data:`COLUMNS` order — projecting the columns the store's older shape lacks as their
    **migration-default literal** (``'' AS reconcile_phase`` for a v5 store, and so on). The
    caller then decodes each row exactly as a native current row, WITHOUT altering the DB
    bytes, the schema version, or taking a backup: a newer reader interprets a missing additive
    field with the same default a forward migration's ``ALTER … DEFAULT`` would have written,
    but writes nothing.

    Returns ``None`` — fail closed — when the store's live shape is NOT a known signature for
    its recorded version (a partial / corrupt / unknown shape), or when its version is
    unrecognized. It never guesses a compatibility: the shape/capability table
    (:data:`_ALLOWED_SHAPES_BY_VERSION` / :data:`_COLUMN_DEFS`) is the only authority
    (Redmine #13844 design item 4). A missing NON-additive column (one with no default) also
    fails closed rather than fabricate a required authority value.

    This is a pure read: only ``PRAGMA``/``sqlite_master`` reads run, no DDL, no DML.
    """
    recorded = _recorded_version(conn)
    if recorded is None or recorded not in _RECOGNIZED_SCHEMA_VERSIONS:
        return None
    if not _schema_signature_matches(conn, recorded):
        return None
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
    parts: list[str] = []
    for column in (name.strip() for name in _COLUMNS.split(",")):
        if column in present:
            parts.append(column)
            continue
        # The column is absent from this older, known signature. It is therefore an
        # additive column (a non-additive column with no default is caught above by the
        # exact-signature match), so its migration default is a non-NULL literal.
        default = _COLUMN_DEFS.get(column, (None, 0, None, 0))[2]
        if default is None:
            return None
        parts.append(f"{default} AS {column}")
    return ", ".join(parts)


#: A read reached a store whose schema is NEWER than anything this build understands — a
#: newer container version, or a newer component version. It is NOT downgraded or misread
#: (Redmine #13844 item 5): the caller routes to a current, compatible high-level facade
#: (the up-to-date source CLI), never a raw DB downgrade.
READER_UPGRADE_REQUIRED = "reader_upgrade_required"


def reader_upgrade_required(conn: sqlite3.Connection) -> bool:
    """Read-only: is this store NEWER than this build can read (needs an upgraded reader)?

    ``True`` when the container ``PRAGMA user_version`` exceeds this build's
    :data:`STATE_CONTAINER_VERSION`, or the recorded component version is a genuine integer
    greater than the newest version this build recognizes. This is the specific, actionable
    sub-case of :data:`READONLY_COMPONENT_UNSUPPORTED` (Redmine #13844 item 5): the store is
    fine, THIS reader is stale, so the caller routes to the current compatible facade rather
    than downgrading. A malformed / partial / older-corrupt store is NOT this case (it is
    ``False`` here) — it fails closed as unsupported without claiming "just upgrade".
    """
    try:
        container = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        return False
    if isinstance(container, int) and container > STATE_CONTAINER_VERSION:
        return True
    recorded = _recorded_version(conn)
    return (
        isinstance(recorded, int)
        and recorded != _VERSION_MALFORMED
        and recorded > max(_RECOGNIZED_SCHEMA_VERSIONS)
    )


#: Typed outcomes of the schema-ensuring WRITE gate (Redmine #13844 item 3): so a mutating
#: caller can tell "already current" from "migrated an older store" (and where its backup
#: went) rather than inferring it from a bare ``None`` return.
SCHEMA_CREATED = "created"
SCHEMA_INTACT = "intact"
SCHEMA_MIGRATED = "migrated"


@dataclass(frozen=True)
class LifecycleSchemaOutcome:
    """What :func:`ensure_lane_lifecycle_schema` actually did (Redmine #13844 item 3).

    - :data:`SCHEMA_CREATED` — a fresh store: the table + owner indexes were created and the
      component registered at the current version (``from_version`` is ``None``).
    - :data:`SCHEMA_INTACT` — the store already matched the current version; nothing was
      written (``from_version == to_version``).
    - :data:`SCHEMA_MIGRATED` — a recognized OLDER store was migrated forward backup-first;
      ``from_version`` is where it started and ``backup_dir`` is the pre-migration snapshot.

    A downgrade / corrupt / unknown store never yields an outcome — it raises
    :class:`LaneLifecycleError` and leaves the store untouched.
    """

    action: str
    from_version: Optional[int]
    to_version: int = LANE_LIFECYCLE_SCHEMA_VERSION
    backup_dir: Optional[str] = None


def ensure_lane_lifecycle_schema(path: Path) -> LifecycleSchemaOutcome:
    """Create / validate the container, this component's table + owner index (WRITE gate).

    This is the *write-migrating* half of the read-compatible / write-migrating split
    (Redmine #13844): the ONLY path that may forward-migrate the shared authority store, and
    only for a genuinely mutating use case (a CAS write needs the current schema). Reads take
    the non-migrating :func:`readonly_compatible_select` path instead, so a status / handoff /
    review / callback / drain lookup by a newer-schema source CLI never migrates the shared
    store out from under a concurrent older-schema reader. Returns a
    :class:`LifecycleSchemaOutcome` (created / intact / migrated) so a caller can log the
    schema-changing event and its backup; a downgrade / corrupt / unknown store raises.

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
    outcome: LifecycleSchemaOutcome
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
        # Classify the recorded version against the EXACT live table signature before any
        # DDL/DML (Redmine #13754 R5-F1 / R6-F1): the migration must never silently
        # re-create a missing table, default-repair a missing / re-typed authority column,
        # or adopt an unknown-shaped store as a known one. ``CREATE TABLE IF NOT EXISTS`` +
        # idempotent ``ALTER`` + a subset check would do exactly that. Instead a recognized
        # recorded version must match one of its version's ALLOWED signatures exactly
        # (:func:`_schema_signature_matches`) — otherwise it is a corrupt / partial /
        # incompatible authority shape and fails closed (``managed-state-model.md``: a
        # partial migration is never a success, an authoritative component is never silently
        # overwritten, repair is an explicit path).
        table_exists = _table_present(conn, _TABLE)
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
            conn.execute(_PROJECT_OWNER_INDEX_SQL)
            _stamp_component_version(conn)
            outcome = LifecycleSchemaOutcome(action=SCHEMA_CREATED, from_version=None)
        elif not _schema_signature_matches(conn, recorded):
            # A recorded version whose live shape is NOT one of that version's known
            # signatures: a missing table, a missing / extra / re-typed column, or a missing
            # active-owner index. Never re-create / default-repair / re-stamp it.
            raise LaneLifecycleError(
                f"lane lifecycle records v{recorded} but its live table shape does not match "
                f"a known v{recorded} signature (corrupt / partial / incompatible authority "
                f"shape); fail closed (no silent repair). Restore from a backup."
            )
        elif recorded == LANE_LIFECYCLE_SCHEMA_VERSION:
            # Intact current: the signature already matches. Do NOT re-run DDL or re-stamp.
            outcome = LifecycleSchemaOutcome(
                action=SCHEMA_INTACT, from_version=recorded
            )
        else:
            # A recognized OLDER version whose shape matches one of its KNOWN predecessor
            # signatures (v1, v2, or either v3 branch). Migrate forward: back up first, then
            # add only the columns that version legitimately lacks to converge on the current
            # version.
            current_columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")
            }
            # Backup-first (Redmine #13754 R3-F1): copy the DB under home BEFORE the first
            # ``ALTER``; a backup failure aborts with nothing written. Under the lock the DB
            # on disk is the committed pre-``ALTER`` state and no other writer can change it
            # during the copy, so the snapshot is a faithful, unique pre-migration copy.
            try:
                backup_dir = backup_state_container(path)
            except StateStoreError as exc:
                raise LaneLifecycleError(
                    f"lane lifecycle migration to v{LANE_LIFECYCLE_SCHEMA_VERSION} "
                    f"aborted: {exc}. The store is left untouched (backup-first)."
                ) from exc
            # One atomic transaction (Redmine #13754 R4-F2): the additive ``ALTER``s that
            # bring the older shape up to the current version and the version re-stamp run inside this one
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
            # v5 (Redmine #13810): the binding / generation / declaration columns. Each
            # ADD lands the existing rows on the migration default — ``binding_kind='issue'``
            # (every pre-v5 lane was an issue lane, j#78386 §6), empty ``project_scope`` (an
            # issue lane owns no scope, and a legacy empty-issue row is NOT re-derived as a
            # project lane), ``lane_generation=1`` (the first incarnation), empty
            # ``declared_slots`` (no snapshot was captured then) — never a guessed authority
            # value.
            if "binding_kind" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN binding_kind TEXT NOT NULL DEFAULT 'issue'"
                )
            if "project_scope" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN project_scope TEXT NOT NULL DEFAULT ''"
                )
            if "lane_generation" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN lane_generation INTEGER NOT NULL DEFAULT 1"
                )
            if "declared_slots" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN declared_slots TEXT NOT NULL DEFAULT ''"
                )
            # v6 (Redmine #13842): the reconcile owed-close provenance. A pre-v6 row lands with
            # an empty phase — an ordinary / non-reconcile row (never a guessed 'reconciled').
            if "reconcile_phase" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN reconcile_phase TEXT NOT NULL DEFAULT ''"
                )
            # v7 (Redmine #13647): the generation-bound lane-role heal authority. A pre-v7 row
            # lands with an empty ``lane_kind`` — no durable kind fact, so a heal falls back to
            # ``lane_class`` geometry (the issue's close condition), never a guessed kind.
            if "lane_kind" not in current_columns:
                conn.execute(
                    f"ALTER TABLE {_TABLE} "
                    "ADD COLUMN lane_kind TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(_OWNER_INDEX_SQL)
            conn.execute(_PROJECT_OWNER_INDEX_SQL)
            _stamp_component_version(conn)
            outcome = LifecycleSchemaOutcome(
                action=SCHEMA_MIGRATED,
                from_version=recorded,
                backup_dir=str(backup_dir) if backup_dir is not None else None,
            )
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
    return outcome


TABLE = _TABLE
COLUMNS = _COLUMNS


__all__ = (
    "LANE_LIFECYCLE_COMPONENT",
    "LANE_LIFECYCLE_RECOVERY_POLICY",
    "LANE_LIFECYCLE_SCHEMA_VERSION",
    "READONLY_COMPONENT_ABSENT",
    "READONLY_COMPONENT_RECOGNIZED",
    "READONLY_COMPONENT_UNSUPPORTED",
    "READER_UPGRADE_REQUIRED",
    "SCHEMA_CREATED",
    "SCHEMA_INTACT",
    "SCHEMA_MIGRATED",
    "COLUMNS",
    "TABLE",
    "LaneLifecycleError",
    "LifecycleSchemaOutcome",
    "ensure_lane_lifecycle_schema",
    "lane_lifecycle_path",
    "readonly_compatible_select",
    "readonly_component_status",
    "reader_upgrade_required",
)
