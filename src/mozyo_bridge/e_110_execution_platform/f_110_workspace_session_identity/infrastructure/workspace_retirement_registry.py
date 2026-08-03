"""SQLite backup/delete adapter for workspace retirement (#14877)."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.workspace_registry import (
    REGISTRY_HEALTH_OK,
    REGISTRY_SCHEMA_VERSION,
    WorkspaceRecord,
    inspect_registry_health,
    registry_path,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.workspace_retirement import (
    WorkspaceRetirementAuthorityError,
    WorkspaceRetirementStoreOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_retirement import (
    PATH_MISSING,
    PATH_PRESENT,
    PATH_UNREADABLE,
    REASON_BACKUP_FAILED,
    WorkspaceRetirementObservation,
    digest_workspace_record,
    exact_sha256,
)


_SELECT_RECORD = """
SELECT w.workspace_id, w.canonical_path, w.display_path, w.project_name,
       w.canonical_session, w.preset, w.preset_version, w.created_at,
       w.updated_at, a.last_seen
FROM workspaces w
LEFT JOIN workspace_activity a ON a.workspace_id = w.workspace_id
WHERE w.workspace_id = ?
"""


def _row_to_record(row: tuple) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=row[0],
        canonical_path=row[1],
        display_path=row[2],
        project_name=row[3],
        canonical_session=row[4],
        preset=row[5],
        preset_version=row[6],
        created_at=row[7],
        updated_at=row[8],
        last_seen=row[9],
    )


def _record_digest(record: WorkspaceRecord) -> str:
    return digest_workspace_record(record.as_payload())


def _path_state(raw: str) -> str:
    try:
        Path(raw).stat()
        return PATH_PRESENT
    except FileNotFoundError:
        return PATH_MISSING
    except OSError:
        return PATH_UNREADABLE


def _observation(record: WorkspaceRecord) -> WorkspaceRetirementObservation:
    return WorkspaceRetirementObservation(
        workspace_id=record.workspace_id,
        project_name=record.project_name,
        updated_at=record.updated_at,
        record_digest=_record_digest(record),
        path_state=_path_state(record.canonical_path),
    )


class SQLiteWorkspaceRetirementRegistry:
    """Keeps private backup paths out of the application/public result."""

    def __init__(self, *, home: Optional[Path] = None) -> None:
        self._home = home

    @property
    def _registry_path(self) -> Path:
        return registry_path(self._home)

    @property
    def _backup_root(self) -> Path:
        return self._registry_path.parent / "workspace-registry-backups"

    def _backup_path(self, plan_digest: str) -> Path:
        if not exact_sha256(plan_digest):
            raise WorkspaceRetirementAuthorityError("plan_digest_invalid")
        return self._backup_root / f"{plan_digest}.sqlite"

    def _healthy(self) -> bool:
        try:
            return (
                inspect_registry_health(self._home).get("status")
                == REGISTRY_HEALTH_OK
            )
        except OSError:
            return False

    def _read_registry_record(self, workspace_id: str) -> Optional[WorkspaceRecord]:
        try:
            conn = sqlite3.connect(
                f"file:{self._registry_path}?mode=ro", uri=True
            )
            try:
                if (
                    conn.execute("PRAGMA user_version").fetchone()[0]
                    != REGISTRY_SCHEMA_VERSION
                ):
                    raise WorkspaceRetirementAuthorityError(
                        "registry_schema_invalid"
                    )
                row = conn.execute(_SELECT_RECORD, (workspace_id,)).fetchone()
            finally:
                conn.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise WorkspaceRetirementAuthorityError("registry_not_readable") from exc
        return _row_to_record(row) if row is not None else None

    def _read_registry_presence(self, workspace_id: str) -> tuple[bool, bool]:
        """Read row/activity presence in one strict, read-only snapshot."""
        try:
            conn = sqlite3.connect(
                f"file:{self._registry_path}?mode=ro", uri=True
            )
            try:
                conn.execute("BEGIN")
                if (
                    conn.execute("PRAGMA user_version").fetchone()[0]
                    != REGISTRY_SCHEMA_VERSION
                ):
                    raise WorkspaceRetirementAuthorityError(
                        "registry_schema_invalid"
                    )
                row_present = (
                    conn.execute(_SELECT_RECORD, (workspace_id,)).fetchone()
                    is not None
                )
                activity_present = (
                    conn.execute(
                        "SELECT 1 FROM workspace_activity WHERE workspace_id = ?",
                        (workspace_id,),
                    ).fetchone()
                    is not None
                )
                conn.rollback()
            finally:
                conn.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise WorkspaceRetirementAuthorityError("registry_not_readable") from exc
        return row_present, activity_present

    def observe(self, workspace_id: str) -> Optional[WorkspaceRetirementObservation]:
        if not self._healthy():
            raise WorkspaceRetirementAuthorityError("registry_not_healthy")
        record = self._read_registry_record(workspace_id)
        return _observation(record) if record is not None else None

    def _read_backup_record(
        self, workspace_id: str, plan_digest: str
    ) -> Optional[WorkspaceRecord]:
        path = self._backup_path(plan_digest)
        try:
            backup_stat = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceRetirementAuthorityError("backup_not_readable") from exc
        if (
            not stat.S_ISREG(backup_stat.st_mode)
            or stat.S_IMODE(backup_stat.st_mode) != 0o600
            or backup_stat.st_nlink != 1
        ):
            raise WorkspaceRetirementAuthorityError("backup_permissions_invalid")
        try:
            registry_stat = self._registry_path.lstat()
        except OSError as exc:
            raise WorkspaceRetirementAuthorityError("registry_not_readable") from exc
        if (
            backup_stat.st_dev == registry_stat.st_dev
            and backup_stat.st_ino == registry_stat.st_ino
        ):
            raise WorkspaceRetirementAuthorityError("backup_not_independent")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                if (
                    conn.execute("PRAGMA user_version").fetchone()[0]
                    != REGISTRY_SCHEMA_VERSION
                ):
                    raise WorkspaceRetirementAuthorityError("backup_schema_invalid")
                row = conn.execute(_SELECT_RECORD, (workspace_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise WorkspaceRetirementAuthorityError("backup_not_readable") from exc
        if row is None:
            raise WorkspaceRetirementAuthorityError("backup_target_missing")
        return _row_to_record(row)

    def observe_retired(
        self, workspace_id: str, plan_digest: str
    ) -> Optional[WorkspaceRetirementObservation]:
        if not self._healthy():
            raise WorkspaceRetirementAuthorityError("registry_not_healthy")
        self._require_clean_replay_state(workspace_id)
        record = self._read_backup_record(workspace_id, plan_digest)
        self._require_clean_replay_state(workspace_id)
        return _observation(record) if record is not None else None

    def _require_clean_replay_state(self, workspace_id: str) -> None:
        row_present, activity_present = self._read_registry_presence(workspace_id)
        if row_present:
            raise WorkspaceRetirementAuthorityError("replay_target_present")
        if activity_present:
            raise WorkspaceRetirementAuthorityError("replay_activity_present")

    def _ensure_backup(
        self,
        *,
        workspace_id: str,
        expected_record_digest: str,
        plan_digest: str,
    ) -> bool:
        final_path = self._backup_path(plan_digest)
        if final_path.exists() or final_path.is_symlink():
            try:
                existing = self._read_backup_record(workspace_id, plan_digest)
            except WorkspaceRetirementAuthorityError:
                return False
            return (
                existing is not None
                and _record_digest(existing) == expected_record_digest
            )

        root = self._backup_root
        try:
            if root.is_symlink():
                return False
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode):
                return False
            os.chmod(root, 0o700)
            if stat.S_IMODE(root.lstat().st_mode) != 0o700:
                return False
            file_descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{plan_digest}.",
                suffix=".pending.sqlite",
                dir=root,
            )
            os.close(file_descriptor)
        except OSError:
            return False
        temp_path = Path(temp_name)
        try:
            source = None
            destination = None
            try:
                source = sqlite3.connect(
                    f"file:{self._registry_path}?mode=ro", uri=True
                )
                destination = sqlite3.connect(temp_path)
                source.backup(destination)
            finally:
                if destination is not None:
                    destination.close()
                if source is not None:
                    source.close()
            os.chmod(temp_path, 0o600)
            conn = sqlite3.connect(f"file:{temp_path}?mode=ro", uri=True)
            try:
                if (
                    conn.execute("PRAGMA user_version").fetchone()[0]
                    != REGISTRY_SCHEMA_VERSION
                ):
                    return False
                row = conn.execute(_SELECT_RECORD, (workspace_id,)).fetchone()
            finally:
                conn.close()
            if row is None or _record_digest(_row_to_record(row)) != expected_record_digest:
                return False
            try:
                os.link(temp_path, final_path)
            except FileExistsError:
                try:
                    existing = self._read_backup_record(workspace_id, plan_digest)
                except WorkspaceRetirementAuthorityError:
                    return False
                return (
                    existing is not None
                    and _record_digest(existing) == expected_record_digest
                )
            temp_path.unlink()
            existing = self._read_backup_record(workspace_id, plan_digest)
            return (
                existing is not None
                and _record_digest(existing) == expected_record_digest
            )
        except (OSError, sqlite3.DatabaseError, WorkspaceRetirementAuthorityError):
            return False
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def retire(
        self,
        *,
        workspace_id: str,
        expected_record_digest: str,
        plan_digest: str,
    ) -> WorkspaceRetirementStoreOutcome:
        if not exact_sha256(plan_digest) or not exact_sha256(expected_record_digest):
            return WorkspaceRetirementStoreOutcome(False, "digest_invalid")
        if not self._healthy():
            return WorkspaceRetirementStoreOutcome(False, "registry_unreadable")
        try:
            current = self._read_registry_record(workspace_id)
        except WorkspaceRetirementAuthorityError:
            return WorkspaceRetirementStoreOutcome(False, "registry_unreadable")
        if current is None:
            try:
                retired_observation = self.observe_retired(
                    workspace_id, plan_digest
                )
            except WorkspaceRetirementAuthorityError:
                retired_observation = None
            if (
                retired_observation is not None
                and retired_observation.record_digest == expected_record_digest
            ):
                return WorkspaceRetirementStoreOutcome(
                    True, backup_receipt=plan_digest
                )
            return WorkspaceRetirementStoreOutcome(
                False, "retirement_replay_not_proven"
            )
        if _record_digest(current) != expected_record_digest:
            return WorkspaceRetirementStoreOutcome(False, "record_drift")
        if _path_state(current.canonical_path) != PATH_MISSING:
            return WorkspaceRetirementStoreOutcome(False, "workspace_path_reappeared")
        if not self._ensure_backup(
            workspace_id=workspace_id,
            expected_record_digest=expected_record_digest,
            plan_digest=plan_digest,
        ):
            return WorkspaceRetirementStoreOutcome(False, REASON_BACKUP_FAILED)

        try:
            conn = sqlite3.connect(self._registry_path)
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                conn.execute("BEGIN IMMEDIATE")
                if (
                    conn.execute("PRAGMA user_version").fetchone()[0]
                    != REGISTRY_SCHEMA_VERSION
                ):
                    conn.rollback()
                    return WorkspaceRetirementStoreOutcome(
                        False, "registry_schema_drift"
                    )
                row = conn.execute(_SELECT_RECORD, (workspace_id,)).fetchone()
                if row is None:
                    if conn.execute(
                        "SELECT 1 FROM workspace_activity WHERE workspace_id = ?",
                        (workspace_id,),
                    ).fetchone() is not None:
                        conn.rollback()
                        return WorkspaceRetirementStoreOutcome(
                            False, "activity_orphan_present"
                        )
                    conn.rollback()
                    return WorkspaceRetirementStoreOutcome(
                        True, backup_receipt=plan_digest
                    )
                if _record_digest(_row_to_record(row)) != expected_record_digest:
                    conn.rollback()
                    return WorkspaceRetirementStoreOutcome(False, "record_drift")
                if _path_state(_row_to_record(row).canonical_path) != PATH_MISSING:
                    conn.rollback()
                    return WorkspaceRetirementStoreOutcome(
                        False, "workspace_path_reappeared"
                    )
                conn.execute(
                    "DELETE FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                )
                if conn.execute(_SELECT_RECORD, (workspace_id,)).fetchone() is not None:
                    conn.rollback()
                    return WorkspaceRetirementStoreOutcome(False, "delete_readback_failed")
                if conn.execute(
                    "SELECT 1 FROM workspace_activity WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone() is not None:
                    conn.rollback()
                    return WorkspaceRetirementStoreOutcome(
                        False, "activity_cascade_failed"
                    )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return WorkspaceRetirementStoreOutcome(False, "registry_write_failed")
        return WorkspaceRetirementStoreOutcome(
            True,
            backup_receipt=plan_digest,
        )


__all__ = ("SQLiteWorkspaceRetirementRegistry",)
