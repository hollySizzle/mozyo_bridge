"""Backup-first recovery points for the state container (Redmine #14756 j#96956 F5).

Carved out of ``lane_lifecycle_schema`` because taking a *verified* recovery point stopped
being one statement. It was ``shutil.copy2``; review j#96956 measured that a main-file copy
is not a recovery point at all under WAL journalling, and the replacement is a staged
logical snapshot with a readback and an atomic publish. That is a unit of its own, and
leaving it inline pushed the schema module past its 1000-line gate — the split follows the
seam the feature actually has rather than an allowlist entry (#13948 j#80989).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.state_store import BACKUPS_DIRNAME, StateStoreError


def _utc_now() -> str:
    """Local, not imported from ``lane_lifecycle_rows``: that module imports the schema,
    which imports this one, and the cycle only exists to share three lines."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup_stamp(stamp: str) -> str:
    """Filesystem-safe second-precision stamp for a backup directory name."""
    return stamp.replace(":", "").replace("-", "").replace("+", "Z")


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
    staging = path.parent / BACKUPS_DIRNAME / f".staging-{_backup_stamp(_utc_now())}"
    try:
        backup_dir = base
        suffix = 1
        while backup_dir.exists():
            backup_dir = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        stage = staging
        suffix = 1
        while stage.exists():
            stage = staging.with_name(f"{staging.name}-{suffix}")
            suffix += 1
        stage.mkdir(parents=True, exist_ok=False)
        staged = stage / path.name
        _snapshot_state_container(path, staged)
        _readback_state_container(path, staged)
        # Publish only after the snapshot has been read back. `rename` within one directory
        # is atomic, so a reader of `backups/` never observes a half-written snapshot: it
        # sees the staging name or the final one, never a partial under the final name.
        stage.rename(backup_dir)
    except StateStoreError:
        _discard_staging(staging)
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        _discard_staging(staging)
        raise StateStoreError(
            f"backup near {base} failed ({exc}); migration aborted "
            f"(nothing was written)"
        ) from exc
    return backup_dir


def _snapshot_state_container(source: Path, target: Path) -> None:
    """Write a transaction-consistent logical snapshot of ``source`` to ``target``.

    ``shutil.copy2`` of the main database file is NOT a recovery point (Redmine #14756
    review j#96956 F5). Under WAL journalling, committed pages live in ``-wal`` until a
    checkpoint folds them back, so copying only ``state.sqlite`` silently drops every
    committed authority row written since the last checkpoint — and
    ``wal_autocheckpoint=0`` makes that window unbounded. A "backup-first" migration whose
    backup can be missing the rows it exists to preserve is not backup-first.

    ``Connection.backup()`` reads through SQLite itself, so the snapshot includes the WAL
    contents and is consistent as of one transaction boundary.

    A failure here is NEVER retried as a raw copy. If SQLite cannot back a database up,
    the honest conclusion is that no verified recovery point exists — falling back to the
    very copy that loses WAL pages would manufacture one, which is worse than refusing.
    """
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)


def _content_digest(conn: sqlite3.Connection) -> str:
    """A deterministic digest of the whole logical database — schema AND every cell.

    Row COUNTS and ``user_version`` were the entire readback before Redmine #14756 review
    j#96971 R11-F6, which measured the gap directly: a snapshot with the same schema, same
    version and the same number of rows, but one cell rewritten, was published as a verified
    backup. A recovery point that can differ from the store in content is not one.

    ``typeof()`` is folded in beside each value so the digest cannot be satisfied by a
    different storage class holding an equal-looking value — the same laundering the
    ``lane_epoch`` column had to be moved off INTEGER affinity to avoid. Rows are ordered by
    their full tuple rather than by ``rowid`` so the digest is a property of the CONTENT and
    not of physical layout, which a logical snapshot legitimately changes.
    """
    import hashlib

    digest = hashlib.sha256()
    schema = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    digest.update(repr(schema).encode())
    tables = [
        name
        for _type, name, _sql in schema
        if _type == "table"
    ]
    for table in tables:
        columns = [
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        if not columns:
            continue
        projection = ", ".join(
            f"typeof({column}), {column}" for column in columns
        )
        rows = conn.execute(
            f"SELECT {projection} FROM {table} ORDER BY {projection}"
        ).fetchall()
        digest.update(table.encode())
        digest.update(repr(rows).encode())
    digest.update(str(conn.execute("PRAGMA user_version").fetchone()[0]).encode())
    return digest.hexdigest()


def _readback_state_container(source: Path, staged: Path) -> None:
    """Fail closed unless the staged snapshot reproduces the source EXACTLY, content included.

    Compares one digest covering schema identity, every cell with its storage class, and
    ``user_version`` — so a mismatch anywhere refuses the publish, leaving no backup rather
    than a plausible-looking wrong one.
    """
    with sqlite3.connect(f"file:{staged}?mode=ro", uri=True) as snap:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
            try:
                snapshot_digest = _content_digest(snap)
            except sqlite3.DatabaseError as exc:
                raise StateStoreError(
                    f"backup readback failed: the snapshot is unreadable ({exc}); "
                    f"migration aborted (nothing was written)"
                ) from exc
            if snapshot_digest != _content_digest(src):
                raise StateStoreError(
                    "backup readback failed: the snapshot's schema, rows or recorded "
                    "version differ from the store; migration aborted (nothing was written)"
                )


def _discard_staging(staging: Path) -> None:
    """Remove every staging attempt so a partial snapshot is never left behind."""
    parent = staging.parent
    if not parent.exists():
        return
    for candidate in parent.glob(f"{staging.name}*"):
        shutil.rmtree(candidate, ignore_errors=True)
