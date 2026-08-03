"""Replacement transaction — schema, registration, downgrade guard (Redmine #13806).

The *shape* concern of the atomic self-replacement transaction component, kept apart
from the CAS writes in :mod:`mozyo_bridge.core.state.replacement_transaction`: which
table it owns, how it registers in ``state_schema_components``, which versions this build
understands, and — the safety part — what it refuses to touch.

A **native component** of the consolidated home-scoped ``state.sqlite`` (the sibling
:mod:`...lane_lifecycle` precedent): it shares the container guard
(:func:`~...state_store.connect_state_container_rw`) and self-registers with no
``migrated_from`` (there is no legacy file). It is a NEW table, not another axis on the
issue-owned lifecycle row (Design Answer j#78384 §1), because the transaction is session /
workspace scoped and binds several participants + a continuation.

The container guard (``PRAGMA user_version``) is **not** a component guard. A store whose
``replacement_transaction`` component records a version this build does not know is left
completely untouched: no table create, no migration, no metadata re-stamp
(``managed-state-model.md`` ``### backup / downgrade / partial migration``). v1 is the
first shape. v2 preserves the row shape in a v2-only authority table, leaves the legacy
table name as a read-only compatibility view, and adds an exact-action effect fence. A
v1 writer that passed admission before migration therefore still cannot mutate afterward;
the component stamp separately makes a fresh v1 runtime fail closed. The migration is
explicit offline and backup-first, never a silent repair.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle_backup import (
    backup_state_container,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_PLANNED,
)
from mozyo_bridge.core.state.state_store import (
    STATE_CONTAINER_VERSION,
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)


REPLACEMENT_TRANSACTION_COMPONENT = "replacement_transaction"
#: v2 is the first behavioral write-protocol bump (Redmine #14741 R12-F2/R13-F2).
#: Besides the writer admission stamp it owns a DB-visible exact-action effect fence and
#: mutation triggers.  The trigger is what stops a v1 writer that passed its version check
#: before an offline migration and resumes after the v2 sender has armed an effect.
REPLACEMENT_TRANSACTION_SCHEMA_VERSION = 2
#: The component shapes this build can read and write. Anything else — a newer version from
#: a future build, or a foreign value — fails closed and the store is left untouched.
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2})
#: An owner-approved replacement plan cannot be rebuilt from events; loss requires an
#: explicit re-plan from the Redmine durable pointer (the lifecycle precedent).
REPLACEMENT_TRANSACTION_RECOVERY_POLICY = "operator_current_state"

_LEGACY_TABLE = "replacement_transactions"
_TABLE = "replacement_transactions_v2"
REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE = (
    "replacement_transaction_effect_fences"
)

_EFFECT_FENCE_TABLE_SQL = f"""
CREATE TABLE {REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE} (
    workspace_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_generation INTEGER NOT NULL,
    owner_token TEXT NOT NULL,
    armed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, action_id)
)
"""

_EFFECT_FENCE_COLUMNS = frozenset(
    {
        "workspace_id",
        "action_id",
        "action_generation",
        "owner_token",
        "armed_at",
    }
)
_EFFECT_FENCE_COLUMN_DEFS: dict[str, tuple[str, int, Optional[str], int]] = {
    "workspace_id": ("TEXT", 1, None, 1),
    "action_id": ("TEXT", 1, None, 2),
    "action_generation": ("INTEGER", 1, None, 0),
    "owner_token": ("TEXT", 1, None, 0),
    "armed_at": ("TEXT", 1, None, 0),
}

_EFFECT_TRIGGER_SQL_BY_NAME = {
    "replacement_transactions_effect_fence_insert": f"""
        CREATE TRIGGER replacement_transactions_effect_fence_insert
        BEFORE INSERT ON {_TABLE}
        WHEN EXISTS (
            SELECT 1 FROM {REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE}
            WHERE workspace_id = NEW.workspace_id AND action_id = NEW.action_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'replacement transaction effect fenced');
        END
    """,
    "replacement_transactions_effect_fence_update": f"""
        CREATE TRIGGER replacement_transactions_effect_fence_update
        BEFORE UPDATE ON {_TABLE}
        WHEN EXISTS (
            SELECT 1 FROM {REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE}
            WHERE workspace_id = OLD.workspace_id AND action_id = OLD.action_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'replacement transaction effect fenced');
        END
    """,
    "replacement_transactions_effect_fence_delete": f"""
        CREATE TRIGGER replacement_transactions_effect_fence_delete
        BEFORE DELETE ON {_TABLE}
        WHEN EXISTS (
            SELECT 1 FROM {REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE}
            WHERE workspace_id = OLD.workspace_id AND action_id = OLD.action_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'replacement transaction effect fenced');
        END
    """,
}

_TABLE_SQL = f"""
CREATE TABLE {_TABLE} (
    workspace_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_generation INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT '{PHASE_PLANNED}',
    revision INTEGER NOT NULL,
    decision_source TEXT NOT NULL DEFAULT '',
    decision_issue_id TEXT NOT NULL DEFAULT '',
    decision_journal TEXT NOT NULL DEFAULT '',
    continuation_source TEXT NOT NULL DEFAULT '',
    continuation_issue_id TEXT NOT NULL DEFAULT '',
    continuation_journal TEXT NOT NULL DEFAULT '',
    continuation_expected_gate TEXT NOT NULL DEFAULT '',
    continuation_next_action TEXT NOT NULL DEFAULT '',
    participants_manifest TEXT NOT NULL DEFAULT '',
    lease_holder TEXT NOT NULL DEFAULT '',
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, action_id)
)
"""

_COLUMNS = (
    "workspace_id, action_id, action_generation, phase, revision, "
    "decision_source, decision_issue_id, decision_journal, "
    "continuation_source, continuation_issue_id, continuation_journal, "
    "continuation_expected_gate, continuation_next_action, participants_manifest, "
    "lease_holder, lease_epoch, lease_expires_at, created_at, updated_at"
)

_COMPAT_VIEW_SQL = f"""
CREATE VIEW {_LEGACY_TABLE} AS
SELECT {_COLUMNS} FROM {_TABLE}
"""

#: The EXACT column-name signature per recorded version (the #13754 R6-F1 discipline): a
#: recognized store must match its version's signature EXACTLY (set equality — no unknown
#: extra columns, no missing columns) or it is a partial / incompatible authority shape and
#: fails closed (never silently re-created, migrated, or re-stamped).
_V1_COLUMNS = frozenset(
    {
        "workspace_id",
        "action_id",
        "action_generation",
        "phase",
        "revision",
        "decision_source",
        "decision_issue_id",
        "decision_journal",
        "continuation_source",
        "continuation_issue_id",
        "continuation_journal",
        "continuation_expected_gate",
        "continuation_next_action",
        "participants_manifest",
        "lease_holder",
        "lease_epoch",
        "lease_expires_at",
        "created_at",
        "updated_at",
    }
)
_ALLOWED_SHAPES_BY_VERSION: dict[int, tuple[frozenset, ...]] = {
    1: (_V1_COLUMNS,),
    2: (_V1_COLUMNS,),
}

#: The authority-affecting definition each column MUST carry: ``(type, notnull, default,
#: pk_order)`` as ``PRAGMA table_info`` reports it. A same-named but re-typed / nullable /
#: default-changed / PK-shifted column is NOT the current column — it fails closed rather
#: than being read as authoritative (the #13754 R6-F1 discipline).
_COLUMN_DEFS: dict[str, tuple[str, int, Optional[str], int]] = {
    "workspace_id": ("TEXT", 1, None, 1),
    "action_id": ("TEXT", 1, None, 2),
    "action_generation": ("INTEGER", 1, None, 0),
    "phase": ("TEXT", 1, f"'{PHASE_PLANNED}'", 0),
    "revision": ("INTEGER", 1, None, 0),
    "decision_source": ("TEXT", 1, "''", 0),
    "decision_issue_id": ("TEXT", 1, "''", 0),
    "decision_journal": ("TEXT", 1, "''", 0),
    "continuation_source": ("TEXT", 1, "''", 0),
    "continuation_issue_id": ("TEXT", 1, "''", 0),
    "continuation_journal": ("TEXT", 1, "''", 0),
    "continuation_expected_gate": ("TEXT", 1, "''", 0),
    "continuation_next_action": ("TEXT", 1, "''", 0),
    "participants_manifest": ("TEXT", 1, "''", 0),
    "lease_holder": ("TEXT", 1, "''", 0),
    "lease_epoch": ("INTEGER", 1, "0", 0),
    "lease_expires_at": ("TEXT", 1, "''", 0),
    "created_at": ("TEXT", 1, None, 0),
    "updated_at": ("TEXT", 1, None, 0),
}


class ReplacementTransactionError(RuntimeError):
    """The transaction store is unusable (unreadable / unsupported); fail closed."""


def replacement_transaction_path(home: Path | None = None) -> Path:
    """The consolidated single state DB this component lives in (state.sqlite)."""
    return state_store_path(home)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            REPLACEMENT_TRANSACTION_COMPONENT,
            REPLACEMENT_TRANSACTION_SCHEMA_VERSION,
            "core/state/replacement_transaction.py",
            REPLACEMENT_TRANSACTION_RECOVERY_POLICY,
            _utc_now(),
        ),
    )


#: Sentinel for a component row whose version is present but not an exact integer.
_VERSION_MALFORMED = -1


def _recorded_version(conn: sqlite3.Connection) -> Optional[int]:
    """This component's recorded ``state_schema_components`` version, or ``None``.

    The lifecycle component's three-outcome discipline (Redmine #13689 R5-F1 / #13754
    R4-F1): ``None`` (absent — a fresh install this build may create),
    :data:`_VERSION_MALFORMED` (present but unusable — NULL / REAL / TEXT / query failure;
    never coerced, so an ``int(2.5)`` cannot pass the recognized-version check), or the
    exact recorded integer.
    """
    try:
        row = conn.execute(
            "SELECT typeof(schema_version), schema_version "
            "FROM state_schema_components WHERE component = ?",
            (REPLACEMENT_TRANSACTION_COMPONENT,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return _VERSION_MALFORMED
    if row is None:
        return None
    storage_class, value = row
    if (
        value is None
        or storage_class != "integer"
        or not isinstance(value, int)
        or isinstance(value, bool)
    ):
        return _VERSION_MALFORMED
    return value


#: Read-only schema-classification outcomes (the #13681 R3-F1 read-side mirror).
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


def _schema_object(conn: sqlite3.Connection, name: str) -> Optional[tuple[str, str]]:
    row = conn.execute(
        "SELECT type, sql FROM sqlite_schema WHERE name=?", (name,)
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1] or "")


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _effect_fence_shape_matches(conn: sqlite3.Connection) -> bool:
    """Whether v2's table and exact owned trigger set are intact."""

    if not _table_present(conn, REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE):
        return False
    stored_table = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        (REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE,),
    ).fetchone()
    if (
        stored_table is None
        or _normalized_sql(stored_table[0])
        != _normalized_sql(_EFFECT_FENCE_TABLE_SQL)
    ):
        return False
    attached = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL "
        "AND type IN ('index', 'trigger') LIMIT 1",
        (REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE,),
    ).fetchone()
    if attached is not None:
        return False
    info = {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute(
            f"PRAGMA table_info({REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE})"
        )
    }
    if frozenset(info) != _EFFECT_FENCE_COLUMNS:
        return False
    if any(
        _EFFECT_FENCE_COLUMN_DEFS.get(name) != definition
        for name, definition in info.items()
    ):
        return False
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=?",
        (_TABLE,),
    ).fetchall()
    actual = {str(name): _normalized_sql(sql) for name, sql in rows}
    expected = {
        name: _normalized_sql(sql) for name, sql in _EFFECT_TRIGGER_SQL_BY_NAME.items()
    }
    return actual == expected


def _schema_signature_matches(conn: sqlite3.Connection, recorded: int) -> bool:
    """Does the live table EXACTLY match one of ``recorded``'s allowed signatures?

    The #13754 R6-F1 discipline with no mutation: the table exists; its column-NAME set
    equals one of :data:`_ALLOWED_SHAPES_BY_VERSION` for ``recorded`` exactly (no unknown
    extra column, no missing column); and every present column's authority-affecting
    definition (type / NOT NULL / default / PK order) matches :data:`_COLUMN_DEFS`.
    """
    authority_table = _LEGACY_TABLE if recorded == 1 else _TABLE
    if not _table_present(conn, authority_table):
        return False
    info = {
        row[1]: (row[2], row[3], row[4], row[5])  # name -> (type, notnull, dflt, pk)
        for row in conn.execute(f"PRAGMA table_info({authority_table})")
    }
    names = frozenset(info)
    if names not in _ALLOWED_SHAPES_BY_VERSION.get(recorded, ()):
        return False
    for name, definition in info.items():
        if _COLUMN_DEFS.get(name) != definition:
            return False
    if recorded == 1:
        # A genuine v1 store is the legacy-named table and predates the v2 authority
        # table/view split, guard table, and owned triggers. Merely stamping v2 down to 1
        # is a foreign/partial shape, not a migration source.
        return (
            _schema_object(conn, _TABLE) is None
            and not _table_present(conn, REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE)
            and not conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=? LIMIT 1",
                (_LEGACY_TABLE,),
            ).fetchone()
        )
    if recorded == 2:
        compat = _schema_object(conn, _LEGACY_TABLE)
        return (
            compat is not None
            and compat[0] == "view"
            and _normalized_sql(compat[1]) == _normalized_sql(_COMPAT_VIEW_SQL)
            and not conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=? LIMIT 1",
                (_LEGACY_TABLE,),
            ).fetchone()
            and _effect_fence_shape_matches(conn)
        )
    return False


def _create_v2_effect_fence(conn: sqlite3.Connection) -> None:
    conn.execute(_EFFECT_FENCE_TABLE_SQL)
    for sql in _EFFECT_TRIGGER_SQL_BY_NAME.values():
        conn.execute(sql)


def readonly_component_status(conn: sqlite3.Connection) -> str:
    """Classify this component for a NON-CREATING read (the #13681 R3-F1 mirror).

    A read-only mirror of the write-side downgrade guard: it never writes, and returns
    :data:`READONLY_COMPONENT_ABSENT` (a recognized container whose component is completely
    absent), :data:`READONLY_COMPONENT_RECOGNIZED` (a recognized container + a recorded
    version this build understands + the table present), or
    :data:`READONLY_COMPONENT_UNSUPPORTED` (a newer / unknown container version, an unknown
    / newer / malformed component version, a metadata row without its table, a table
    without metadata, a **live shape that does not exactly match the recorded version's
    signature**, or a query failure).

    The read-side must agree with the write-side downgrade guard (Redmine #13806 R1-F3): a
    recognized recorded version whose live table shape is NOT one of that version's exact
    signatures — an extra / re-typed / missing column — is a partial / foreign authority
    shape and is :data:`READONLY_COMPONENT_UNSUPPORTED`, never ``recognized``. Otherwise a
    read-only projection could read authority rows from a shape the write path rejects,
    degrading the fail-closed read into a fail-open "no transactions" absence.
    """
    try:
        container_version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    if container_version != STATE_CONTAINER_VERSION:
        return READONLY_COMPONENT_UNSUPPORTED
    try:
        has_meta = _table_present(conn, "state_schema_components")
        has_component_objects = any(
            _schema_object(conn, name) is not None
            for name in (
                _LEGACY_TABLE,
                _TABLE,
                REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE,
            )
        )
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    if not has_meta:
        return (
            READONLY_COMPONENT_ABSENT
            if not has_component_objects
            else READONLY_COMPONENT_UNSUPPORTED
        )
    recorded = _recorded_version(conn)
    if recorded is None:
        return (
            READONLY_COMPONENT_ABSENT
            if not has_component_objects
            else READONLY_COMPONENT_UNSUPPORTED
        )
    if recorded not in _RECOGNIZED_SCHEMA_VERSIONS:
        return READONLY_COMPONENT_UNSUPPORTED
    try:
        # The write path rejects a shape that is not an exact signature; the read path must
        # too (R1-F3), or a foreign / partial authority table reads as "recognized".
        if not _schema_signature_matches(conn, recorded):
            return READONLY_COMPONENT_UNSUPPORTED
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    return READONLY_COMPONENT_RECOGNIZED


def ensure_replacement_transaction_schema(path: Path) -> None:
    """Create / validate the container and this component's table.

    Uses the shared container guard, then registers this component with no
    ``migrated_from`` (native-component form). A newer component schema fails closed and
    the DB is left untouched (the lifecycle component's R3-F1 contract): the rows it would
    write are replacement **authority**, and re-stamping them down to a shape this build
    understands is exactly how an old build starts moving state whose newer semantics it
    does not agree to.
    """
    try:
        conn = connect_state_container_rw(path)
    except StateStoreError as exc:
        raise ReplacementTransactionError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise ReplacementTransactionError(
            f"replacement transaction store {path} is unreadable "
            f"({type(exc).__name__}); fail closed"
        ) from exc
    # Serialize the whole migration under one exclusive write lock (the #13754 R4-F1
    # discipline): ``BEGIN IMMEDIATE`` takes the reserved lock BEFORE the version is read,
    # so a concurrent first-use caller cannot read the same pre-migration version, back up,
    # and overwrite the only pre-migration snapshot with a post-migration copy.
    conn.isolation_level = None
    locked = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked = True
        recorded = _recorded_version(conn)
        if recorded is not None and recorded not in _RECOGNIZED_SCHEMA_VERSIONS:
            detail = (
                "a present-but-malformed value (not an exact integer)"
                if recorded == _VERSION_MALFORMED
                else f"version {recorded}"
            )
            raise ReplacementTransactionError(
                f"replacement transaction component records {detail}; this build "
                f"understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. The store is left "
                f"untouched (downgrade-safe); use a newer build."
            )
        component_objects_exist = any(
            _schema_object(conn, name) is not None
            for name in (
                _LEGACY_TABLE,
                _TABLE,
                REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE,
            )
        )
        if recorded is None:
            # A component this build never registered. Only a genuinely fresh store (no
            # table) is a create; a table WITHOUT its metadata row is a partial / unknown
            # state — fail closed (no silent adoption), exactly like the lifecycle guard.
            if component_objects_exist:
                raise ReplacementTransactionError(
                    "replacement transaction table exists without a component metadata "
                    "row (partial / unknown state); fail closed (no silent adoption)."
                )
            conn.execute(_TABLE_SQL)
            conn.execute(_COMPAT_VIEW_SQL)
            _create_v2_effect_fence(conn)
            _stamp_component_version(conn)
        elif not _schema_signature_matches(conn, recorded):
            raise ReplacementTransactionError(
                f"replacement transaction records v{recorded} but its live table shape "
                f"does not match a known v{recorded} signature (corrupt / partial / "
                f"incompatible authority shape); fail closed (no silent repair). "
                f"Restore from a backup."
            )
        elif recorded == REPLACEMENT_TRANSACTION_SCHEMA_VERSION:
            # Intact current: the signature already matches. Do NOT re-run DDL or re-stamp.
            pass
        else:
            # Normal reads/writes never upgrade a shared home. The old process set must be
            # quiesced first by the offline rollout rail, which then calls the explicit
            # migrator below. This refusal is intentionally zero-write.
            raise ReplacementTransactionError(
                f"replacement transaction component records v{recorded}; explicit offline "
                f"migration to v{REPLACEMENT_TRANSACTION_SCHEMA_VERSION} is required "
                "before this runtime may read or mutate it. The store is untouched."
            )
        conn.execute("COMMIT")
        locked = False
    except sqlite3.DatabaseError as exc:
        if locked:
            _rollback_quietly(conn)
        raise ReplacementTransactionError(
            f"replacement transaction schema init failed ({type(exc).__name__}); "
            f"fail closed"
        ) from exc
    except ReplacementTransactionError:
        if locked:
            _rollback_quietly(conn)
        raise
    finally:
        conn.close()


def migrate_replacement_transaction_schema_v2(path: Path) -> Optional[Path]:
    """Explicitly migrate one quiesced exact v1 store to the v2 effect protocol.

    This low-level primitive does not stop processes itself.  Its caller must first prove
    offline/quiescent authority.  It serializes admission, validates an exact v1 source,
    takes a backup before the first DB write, installs the DB-visible effect fence, stamps
    v2 last, and returns the recovery-point directory.
    """

    path = Path(path)
    if not path.exists():
        raise ReplacementTransactionError(
            "replacement transaction offline migration requires an existing v1 store; "
            "nothing was created"
        )
    try:
        conn = connect_state_container_rw(path)
    except StateStoreError as exc:
        raise ReplacementTransactionError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise ReplacementTransactionError(
            f"replacement transaction store {path} is unreadable "
            f"({type(exc).__name__}); fail closed"
        ) from exc
    conn.isolation_level = None
    locked = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked = True
        recorded = _recorded_version(conn)
        if recorded == REPLACEMENT_TRANSACTION_SCHEMA_VERSION:
            if not _schema_signature_matches(conn, recorded):
                raise ReplacementTransactionError(
                    "replacement transaction records v2 with a partial / foreign live "
                    "shape; offline migration cannot repair it"
                )
            conn.execute("ROLLBACK")
            locked = False
            return None
        if recorded != 1 or not _schema_signature_matches(conn, 1):
            raise ReplacementTransactionError(
                "replacement transaction offline migration accepts only an exact v1 "
                "component; the store is untouched"
            )
        try:
            backup_dir = backup_state_container(path)
        except StateStoreError as exc:
            raise ReplacementTransactionError(
                f"replacement transaction migration to v2 aborted: {exc}. The store is "
                "untouched (backup-first)."
            ) from exc
        conn.execute(f"ALTER TABLE {_LEGACY_TABLE} RENAME TO {_TABLE}")
        conn.execute(_COMPAT_VIEW_SQL)
        _create_v2_effect_fence(conn)
        _stamp_component_version(conn)
        if not _schema_signature_matches(conn, 2):
            raise ReplacementTransactionError(
                "replacement transaction v2 readback failed; migration rolled back"
            )
        conn.execute("COMMIT")
        locked = False
        return backup_dir
    except sqlite3.DatabaseError as exc:
        if locked:
            _rollback_quietly(conn)
        raise ReplacementTransactionError(
            f"replacement transaction offline migration failed "
            f"({type(exc).__name__}); fail closed"
        ) from exc
    except ReplacementTransactionError:
        if locked:
            _rollback_quietly(conn)
        raise
    finally:
        conn.close()


TABLE = _TABLE
READONLY_TABLE = _LEGACY_TABLE
COLUMNS = _COLUMNS


__all__ = (
    "REPLACEMENT_TRANSACTION_COMPONENT",
    "REPLACEMENT_TRANSACTION_RECOVERY_POLICY",
    "REPLACEMENT_TRANSACTION_SCHEMA_VERSION",
    "REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE",
    "READONLY_COMPONENT_ABSENT",
    "READONLY_COMPONENT_RECOGNIZED",
    "READONLY_COMPONENT_UNSUPPORTED",
    "COLUMNS",
    "READONLY_TABLE",
    "TABLE",
    "ReplacementTransactionError",
    "ensure_replacement_transaction_schema",
    "migrate_replacement_transaction_schema_v2",
    "readonly_component_status",
    "replacement_transaction_path",
)
