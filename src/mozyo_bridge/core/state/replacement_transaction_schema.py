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
first version; the additive-migration scaffolding mirrors the lifecycle component so a
future v2 lands backup-first with the same exact-shape classifier, never a silent repair.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_PLANNED,
)
from mozyo_bridge.core.state.state_store import (
    BACKUPS_DIRNAME,
    STATE_CONTAINER_VERSION,
    StateStoreError,
    connect_state_container_rw,
    state_store_path,
)


REPLACEMENT_TRANSACTION_COMPONENT = "replacement_transaction"
#: v1 is the first shape (Redmine #13806 tranche A). Bump only with an additive migration;
#: a newer / unknown version is reported unsupported and left untouched (downgrade-safe).
REPLACEMENT_TRANSACTION_SCHEMA_VERSION = 1
#: The component shapes this build can read and write. Anything else — a newer version from
#: a future build, or a foreign value — fails closed and the store is left untouched.
_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1})
#: An owner-approved replacement plan cannot be rebuilt from events; loss requires an
#: explicit re-plan from the Redmine durable pointer (the lifecycle precedent).
REPLACEMENT_TRANSACTION_RECOVERY_POLICY = "operator_current_state"

_TABLE = "replacement_transactions"

_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
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
            REPLACEMENT_TRANSACTION_COMPONENT,
            REPLACEMENT_TRANSACTION_SCHEMA_VERSION,
            "core/state/replacement_transaction.py",
            REPLACEMENT_TRANSACTION_RECOVERY_POLICY,
            _utc_now(),
        ),
    )


def backup_state_container(path: Path) -> Optional[Path]:
    """Copy an existing ``state.sqlite`` into ``backups/state-<ts>/`` before a write.

    The lifecycle component's ``backup_state_container`` contract (Redmine #13754 R3-F1 /
    R4-F1): copy the DB under home before the first migration write; a copy failure raises
    :class:`StateStoreError` so the caller fails closed with the DB byte-unchanged. Returns
    the backup dir, or ``None`` when there is nothing to preserve yet. Never overwrites an
    existing snapshot — a taken directory gets a numeric suffix.
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
            f"backup near {base} failed ({exc}); migration aborted (nothing was written)"
        ) from exc
    return backup_dir


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


def _schema_signature_matches(conn: sqlite3.Connection, recorded: int) -> bool:
    """Does the live table EXACTLY match one of ``recorded``'s allowed signatures?

    The #13754 R6-F1 discipline with no mutation: the table exists; its column-NAME set
    equals one of :data:`_ALLOWED_SHAPES_BY_VERSION` for ``recorded`` exactly (no unknown
    extra column, no missing column); and every present column's authority-affecting
    definition (type / NOT NULL / default / PK order) matches :data:`_COLUMN_DEFS`.
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
    return True


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
        has_table = _table_present(conn, _TABLE)
    except sqlite3.DatabaseError:
        return READONLY_COMPONENT_UNSUPPORTED
    if not has_meta:
        return (
            READONLY_COMPONENT_ABSENT
            if not has_table
            else READONLY_COMPONENT_UNSUPPORTED
        )
    recorded = _recorded_version(conn)
    if recorded is None:
        return (
            READONLY_COMPONENT_ABSENT
            if not has_table
            else READONLY_COMPONENT_UNSUPPORTED
        )
    if recorded not in _RECOGNIZED_SCHEMA_VERSIONS:
        return READONLY_COMPONENT_UNSUPPORTED
    if not has_table:
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
        table_exists = _table_present(conn, _TABLE)
        if recorded is None:
            # A component this build never registered. Only a genuinely fresh store (no
            # table) is a create; a table WITHOUT its metadata row is a partial / unknown
            # state — fail closed (no silent adoption), exactly like the lifecycle guard.
            if table_exists:
                raise ReplacementTransactionError(
                    "replacement transaction table exists without a component metadata "
                    "row (partial / unknown state); fail closed (no silent adoption)."
                )
            conn.execute(_TABLE_SQL)
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
            # No older recognized version exists yet (v1 is the first). This branch is the
            # additive-migration scaffolding a future v2 fills — backup-first, then add only
            # the columns the older version legitimately lacks. Unreachable at v1, but kept
            # so the migration story is fail-closed by construction, never open-coded later.
            try:
                backup_state_container(path)
            except StateStoreError as exc:
                raise ReplacementTransactionError(
                    f"replacement transaction migration to "
                    f"v{REPLACEMENT_TRANSACTION_SCHEMA_VERSION} aborted: {exc}. The store "
                    f"is left untouched (backup-first)."
                ) from exc
            _stamp_component_version(conn)
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


TABLE = _TABLE
COLUMNS = _COLUMNS


__all__ = (
    "REPLACEMENT_TRANSACTION_COMPONENT",
    "REPLACEMENT_TRANSACTION_RECOVERY_POLICY",
    "REPLACEMENT_TRANSACTION_SCHEMA_VERSION",
    "READONLY_COMPONENT_ABSENT",
    "READONLY_COMPONENT_RECOGNIZED",
    "READONLY_COMPONENT_UNSUPPORTED",
    "COLUMNS",
    "TABLE",
    "ReplacementTransactionError",
    "ensure_replacement_transaction_schema",
    "readonly_component_status",
    "replacement_transaction_path",
)
