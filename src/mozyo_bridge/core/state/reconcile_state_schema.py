"""Reconcile-state — schema, registration, downgrade guard (Redmine #13758).

The *shape* concern of the event-driven reconcile-state component, kept apart from the CAS
writes in :mod:`mozyo_bridge.core.state.reconcile_state`: which table it owns, how it
registers in ``state_schema_components``, which versions this build understands, and — the
safety part — what it refuses to touch.

A **native component** of the consolidated home-scoped ``state.sqlite`` (the sibling
:mod:`...replacement_transaction` / :mod:`...lane_lifecycle` precedent): it shares the
container guard (:func:`~...state_store.connect_state_container_rw`) and self-registers
with no ``migrated_from`` (there is no legacy file). It is a NEW table because the reconcile
row is a per-dispatch self-heal-ladder bookkeeping unit, not another axis on the
issue-owned lifecycle row.

The container guard (``PRAGMA user_version``) is **not** a component guard. A store whose
``reconcile_state`` component records a version this build does not know is left completely
untouched: no table create, no migration, no metadata re-stamp
(``managed-state-model.md`` ``### backup / downgrade / partial migration``). v1 is the
first version; the additive-migration scaffolding mirrors the lifecycle component so a
future v2 lands backup-first with the same exact-shape classifier, never a silent repair.

Recovery policy is ``rebuildable_cache``: unlike the ``operator_current_state`` lifecycle /
replacement components, a lost reconcile row degrades to a fresh cycle (counter 0, expected
fields re-derived from Redmine) — safe by construction. The downgrade guard is still
fail-closed: an *unreadable / unknown-newer* component is refused, never silently rebuilt,
so a build that does not understand the shape cannot start moving derived state under a
newer semantics.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.state_store import (
    BACKUPS_DIRNAME,
    STATE_CONTAINER_VERSION,
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)


RECONCILE_STATE_COMPONENT = "reconcile_state"
#: v1 is the first shape (Redmine #13758). Bump only with an additive migration; a newer /
#: unknown version is reported unsupported and left untouched (downgrade-safe).
RECONCILE_STATE_SCHEMA_VERSION = 1
#: The component shapes this build can read and write. Anything else — a newer version from
#: a future build, or a foreign value — fails closed and the store is left untouched.
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1})
#: Derived self-heal bookkeeping: loss degrades to a fresh cycle, re-derivable from Redmine
#: + the lane registry + the outbox. Never authoritative (Redmine is workflow truth).
RECONCILE_STATE_RECOVERY_POLICY = "rebuildable_cache"

_TABLE = "reconcile_state_records"
_DEFAULT_PHASE = "turn_active"

_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    workspace_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    dispatch_anchor TEXT NOT NULL,
    lane_generation INTEGER NOT NULL DEFAULT 0,
    issue_id TEXT NOT NULL DEFAULT '',
    latest_journal_id TEXT NOT NULL DEFAULT '',
    expected_gate TEXT NOT NULL DEFAULT '',
    expected_next_owner TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT '{_DEFAULT_PHASE}',
    reconcile_failure_count INTEGER NOT NULL DEFAULT 0,
    deadline TEXT NOT NULL DEFAULT '',
    last_disposition TEXT NOT NULL DEFAULT '',
    escalated INTEGER NOT NULL DEFAULT 0,
    callback_outbox_state TEXT NOT NULL DEFAULT '',
    last_observed_runtime TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, lane_id, dispatch_anchor)
)
"""

_COLUMNS = (
    "workspace_id, lane_id, dispatch_anchor, lane_generation, issue_id, "
    "latest_journal_id, expected_gate, expected_next_owner, phase, "
    "reconcile_failure_count, deadline, last_disposition, escalated, "
    "callback_outbox_state, last_observed_runtime, revision, created_at, updated_at"
)

#: The EXACT column-name signature per recorded version (the #13754 R6-F1 discipline): a
#: recognized store must match its version's signature EXACTLY (set equality — no unknown
#: extra columns, no missing columns) or it is a partial / incompatible authority shape and
#: fails closed (never silently re-created, migrated, or re-stamped).
_V1_COLUMNS = frozenset(
    {
        "workspace_id",
        "lane_id",
        "dispatch_anchor",
        "lane_generation",
        "issue_id",
        "latest_journal_id",
        "expected_gate",
        "expected_next_owner",
        "phase",
        "reconcile_failure_count",
        "deadline",
        "last_disposition",
        "escalated",
        "callback_outbox_state",
        "last_observed_runtime",
        "revision",
        "created_at",
        "updated_at",
    }
)
_ALLOWED_SHAPES_BY_VERSION: dict[int, tuple[frozenset, ...]] = {
    1: (_V1_COLUMNS,),
}

#: The authority-affecting definition each column MUST carry: ``(type, notnull, default,
#: pk_order)`` as ``PRAGMA table_info`` reports it. A same-named but re-typed / nullable /
#: default-changed / PK-shifted column is NOT the current column — it fails closed rather
#: than being read as authoritative (the #13754 R6-F1 discipline).
_COLUMN_DEFS: dict[str, tuple[str, int, Optional[str], int]] = {
    "workspace_id": ("TEXT", 1, None, 1),
    "lane_id": ("TEXT", 1, None, 2),
    "dispatch_anchor": ("TEXT", 1, None, 3),
    "lane_generation": ("INTEGER", 1, "0", 0),
    "issue_id": ("TEXT", 1, "''", 0),
    "latest_journal_id": ("TEXT", 1, "''", 0),
    "expected_gate": ("TEXT", 1, "''", 0),
    "expected_next_owner": ("TEXT", 1, "''", 0),
    "phase": ("TEXT", 1, f"'{_DEFAULT_PHASE}'", 0),
    "reconcile_failure_count": ("INTEGER", 1, "0", 0),
    "deadline": ("TEXT", 1, "''", 0),
    "last_disposition": ("TEXT", 1, "''", 0),
    "escalated": ("INTEGER", 1, "0", 0),
    "callback_outbox_state": ("TEXT", 1, "''", 0),
    "last_observed_runtime": ("TEXT", 1, "''", 0),
    "revision": ("INTEGER", 1, None, 0),
    "created_at": ("TEXT", 1, None, 0),
    "updated_at": ("TEXT", 1, None, 0),
}


class ReconcileStateError(RuntimeError):
    """The reconcile-state store is unusable (unreadable / unsupported); fail closed."""


def reconcile_state_path(home: Path | None = None) -> Path:
    """The consolidated single state DB this component lives in (state.sqlite)."""
    return state_store_path(home)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup_stamp(now: str) -> str:
    parsed = datetime.fromisoformat(now)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _rollback_quietly(conn: sqlite3.Connection) -> None:
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
            RECONCILE_STATE_COMPONENT,
            RECONCILE_STATE_SCHEMA_VERSION,
            "core/state/reconcile_state.py",
            RECONCILE_STATE_RECOVERY_POLICY,
            _utc_now(),
        ),
    )


def backup_state_container(path: Path) -> Optional[Path]:
    """Copy an existing ``state.sqlite`` into ``backups/state-<ts>/`` before a write."""
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
            f"backup near {base} failed ({exc}); migration aborted (nothing was written)"
        ) from exc
    return backup_dir


#: Sentinel for a component row whose version is present but not an exact integer.
_VERSION_MALFORMED = -1


def _recorded_version(conn: sqlite3.Connection) -> Optional[int]:
    """This component's recorded ``state_schema_components`` version, or ``None``.

    The lifecycle component's three-outcome discipline: ``None`` (absent — a fresh install
    this build may create), :data:`_VERSION_MALFORMED` (present but unusable — NULL / REAL /
    TEXT / query failure; never coerced, so an ``int(2.5)`` cannot pass the recognized-version
    check), or the exact recorded integer.
    """
    try:
        row = conn.execute(
            "SELECT typeof(schema_version), schema_version "
            "FROM state_schema_components WHERE component = ?",
            (RECONCILE_STATE_COMPONENT,),
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


def _schema_signature_matches(conn: sqlite3.Connection, recorded: int) -> bool:
    """Does the live table EXACTLY match one of ``recorded``'s allowed signatures?"""
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
    return True


def readonly_component_status(conn: sqlite3.Connection) -> str:
    """Classify this component for a NON-CREATING read (the #13681 R3-F1 mirror).

    Returns :data:`READONLY_COMPONENT_ABSENT` (recognized container, component absent),
    :data:`READONLY_COMPONENT_RECOGNIZED` (recognized container + recorded version this build
    understands + table present + exact signature), or :data:`READONLY_COMPONENT_UNSUPPORTED`
    (newer / unknown container or component version, metadata without its table, table without
    metadata, a live shape that does not exactly match the recorded signature, or a query
    failure). The read-side agrees with the write-side downgrade guard, so a foreign / partial
    authority shape never reads as ``recognized``.
    """
    try:
        container_version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    if container_version != STATE_CONTAINER_VERSION:
        return READONLY_COMPONENT_UNSUPPORTED
    try:
        has_meta = _table_present(conn, "state_schema_components")
        has_table = _table_present(conn, _TABLE)
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    if not has_meta:
        return (
            READONLY_COMPONENT_ABSENT if not has_table else READONLY_COMPONENT_UNSUPPORTED
        )
    recorded = _recorded_version(conn)
    if recorded is None:
        return (
            READONLY_COMPONENT_ABSENT if not has_table else READONLY_COMPONENT_UNSUPPORTED
        )
    if recorded not in _RECOGNIZED_SCHEMA_VERSIONS:
        return READONLY_COMPONENT_UNSUPPORTED
    if not has_table:
        return READONLY_COMPONENT_UNSUPPORTED
    try:
        if not _schema_signature_matches(conn, recorded):
            return READONLY_COMPONENT_UNSUPPORTED
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    return READONLY_COMPONENT_RECOGNIZED


def ensure_reconcile_state_schema(path: Path) -> None:
    """Create / validate the container and this component's table.

    Uses the shared container guard, then registers this component with no ``migrated_from``
    (native-component form). A newer component schema fails closed and the DB is left
    untouched (the lifecycle component's R3-F1 contract).
    """
    try:
        conn = connect_state_container_rw(path)
    except StateStoreError as exc:
        raise ReconcileStateError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise ReconcileStateError(
            f"reconcile state store {path} is unreadable ({type(exc).__name__}); fail closed"
        ) from exc
    # Serialize the whole migration under one exclusive write lock (the #13754 R4-F1
    # discipline): ``BEGIN IMMEDIATE`` takes the reserved lock BEFORE the version is read.
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
            raise ReconcileStateError(
                f"reconcile state component records {detail}; this build understands "
                f"{sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. The store is left untouched "
                f"(downgrade-safe); use a newer build."
            )
        table_exists = _table_present(conn, _TABLE)
        if recorded is None:
            if table_exists:
                raise ReconcileStateError(
                    "reconcile state table exists without a component metadata row "
                    "(partial / unknown state); fail closed (no silent adoption)."
                )
            conn.execute(_TABLE_SQL)
            _stamp_component_version(conn)
        elif not _schema_signature_matches(conn, recorded):
            raise ReconcileStateError(
                f"reconcile state records v{recorded} but its live table shape does not "
                f"match a known v{recorded} signature (corrupt / partial / incompatible "
                f"authority shape); fail closed (no silent repair). Restore from a backup."
            )
        elif recorded == RECONCILE_STATE_SCHEMA_VERSION:
            # Intact current: the signature already matches. Do NOT re-run DDL or re-stamp.
            pass
        else:
            # No older recognized version exists yet (v1 is the first). This branch is the
            # additive-migration scaffolding a future v2 fills — backup-first, then add only
            # the columns the older version legitimately lacks. Unreachable at v1.
            try:
                backup_state_container(path)
            except StateStoreError as exc:
                raise ReconcileStateError(
                    f"reconcile state migration to v{RECONCILE_STATE_SCHEMA_VERSION} "
                    f"aborted: {exc}. The store is left untouched (backup-first)."
                ) from exc
            _stamp_component_version(conn)
        conn.execute("COMMIT")
        locked = False
    except sqlite3.DatabaseError as exc:
        if locked:
            _rollback_quietly(conn)
        raise ReconcileStateError(
            f"reconcile state schema init failed ({type(exc).__name__}); fail closed"
        ) from exc
    except ReconcileStateError:
        if locked:
            _rollback_quietly(conn)
        raise
    finally:
        conn.close()


TABLE = _TABLE
COLUMNS = _COLUMNS
DEFAULT_PHASE = _DEFAULT_PHASE


__all__ = (
    "RECONCILE_STATE_COMPONENT",
    "RECONCILE_STATE_RECOVERY_POLICY",
    "RECONCILE_STATE_SCHEMA_VERSION",
    "READONLY_COMPONENT_ABSENT",
    "READONLY_COMPONENT_RECOGNIZED",
    "READONLY_COMPONENT_UNSUPPORTED",
    "COLUMNS",
    "TABLE",
    "DEFAULT_PHASE",
    "ReconcileStateError",
    "ensure_reconcile_state_schema",
    "readonly_component_status",
    "reconcile_state_path",
)
