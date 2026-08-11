"""Offline backup/rebuild of the terminal-unbound launch-generation v1 cache."""

import os
import sqlite3
import stat

from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    remove_attestation_store_artifacts,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_STORE_HEALTHY,
    herdr_launch_generation_path,
    launch_generation_artifacts_secure,
    launch_generation_store_lock,
    probe_launch_generation_store,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from .herdr_offline_rollout_snapshot import (
    _launch_generation_v1_artifact_valid,
    _sqlite_content_digest,
)


_REBUILD_AUTHORITY = "launch-generation.rebuild-authority"


def _artifact_matches_plan(path, planned) -> bool:
    if planned.get("state") != "recognized" or not launch_generation_artifacts_secure(path):
        return False
    version = planned.get("version")
    if type(version) is not int or version not in (1, 2):
        return False
    schema_ok = (
        _launch_generation_v1_artifact_valid(path)
        if version == 1
        else version == 2 and probe_launch_generation_store(path)[0] == GENERATION_STORE_HEALTHY
    )
    return bool(schema_ok and _sqlite_content_digest(path) == planned.get("content_digest"))


def _planned_shape_exact(planned) -> bool:
    if not isinstance(planned, dict):
        return False
    state = planned.get("state")
    version = planned.get("version")
    return (
        state == "absent" and version is None
    ) or (
        state == "recognized"
        and type(version) is int
        and version in (1, 2)
    )


def _directory_fsync(path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _file_fsync(path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_rebuild_authority(*, backup_root, path, content_digest):
    metadata = path.lstat()
    marker = backup_root / _REBUILD_AUTHORITY
    staging = marker.with_name(marker.name + ".staging")
    payload = (
        f"{content_digest}\n{metadata.st_dev}\n{metadata.st_ino}\n"
        f"{metadata.st_ctime_ns}\n{metadata.st_size}\n"
    ).encode("ascii")
    pinned = metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns, metadata.st_size
    try:
        existing = _read_rebuild_authority(
            backup_root=backup_root, content_digest=content_digest
        )
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing != pinned:
            raise OSError("rebuild-authority pin changed")
        _directory_fsync(backup_root)
        return pinned
    staging.unlink(missing_ok=True)
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short rebuild-authority write")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(staging, marker)
    except FileExistsError:
        pass
    finally:
        staging.unlink(missing_ok=True)
    _directory_fsync(backup_root)
    if _read_rebuild_authority(
        backup_root=backup_root, content_digest=content_digest
    ) != pinned:
        raise OSError("rebuild-authority readback failed")
    return pinned


def _read_rebuild_authority(*, backup_root, content_digest):
    marker = backup_root / _REBUILD_AUTHORITY
    metadata = marker.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise OSError("unsafe rebuild authority")
    fields = marker.read_text(encoding="ascii").splitlines()
    if len(fields) != 5 or fields[0] != content_digest:
        raise OSError("mismatched rebuild authority")
    return tuple(int(value) for value in fields[1:])


def _backup_launch_generation_locked(*, home, backup_root, planned) -> PhaseExecutionResult:
    path = herdr_launch_generation_path(home)
    backup = backup_root / "launch-generation.sqlite3"
    staging = backup.with_name(backup.name + ".staging")
    try:
        if (
            not launch_generation_artifacts_secure(path)
            or not launch_generation_artifacts_secure(backup)
        ):
            return PhaseExecutionResult(False, reason="launch_generation_backup_unsafe")
        if planned["state"] == "recognized" and not _artifact_matches_plan(path, planned):
            return PhaseExecutionResult(False, reason="launch_generation_plan_drift")
        if backup.exists():
            backup_metadata = backup.lstat()
            if (
                not stat.S_ISREG(backup_metadata.st_mode)
                or backup_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(backup_metadata.st_mode) != 0o600
            ):
                return PhaseExecutionResult(False, reason="launch_generation_backup_unsafe")
        if path.exists():
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                return PhaseExecutionResult(False, reason="launch_generation_backup_unsafe")
        if path.is_file() and not backup.exists():
            remove_attestation_store_artifacts(staging)
            with (
                sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as src,
                sqlite3.connect(staging) as dst,
            ):
                src.backup(dst)
            staging.chmod(0o600)
            if not _artifact_matches_plan(staging, planned):
                remove_attestation_store_artifacts(staging)
                return PhaseExecutionResult(
                    False, reason="launch_generation_backup_readback_failed"
                )
            os.replace(staging, backup)
            _directory_fsync(backup_root)
            if not _artifact_matches_plan(backup, planned):
                return PhaseExecutionResult(
                    False, reason="launch_generation_backup_readback_failed"
                )
        digest = _sqlite_content_digest(backup)
    except (OSError, sqlite3.DatabaseError):
        remove_attestation_store_artifacts(staging)
        return PhaseExecutionResult(False, reason="launch_generation_backup_failed")
    if planned["state"] == "recognized" and (
        not path.is_file() or not backup.is_file()
        or not _artifact_matches_plan(backup, planned)
        or digest != planned["content_digest"]
    ):
        return PhaseExecutionResult(False, reason="launch_generation_backup_readback_failed")
    if backup.is_file():
        _file_fsync(backup)
        _directory_fsync(backup_root)
    return PhaseExecutionResult(True, receipt={
        "launch_generation_backup": backup.is_file(),
        "launch_generation_content_digest": digest,
    })


def backup_launch_generation(*, home, backup_root, planned, observe) -> PhaseExecutionResult:
    """Pin the source generation from fresh observation through atomic backup publish."""
    if not _planned_shape_exact(planned):
        return PhaseExecutionResult(False, reason="launch_generation_plan_drift")
    try:
        with launch_generation_store_lock(home, exclusive=True, blocking=False):
            if observe() != planned:
                return PhaseExecutionResult(False, reason="launch_generation_plan_drift")
            return _backup_launch_generation_locked(
                home=home, backup_root=backup_root, planned=planned
            )
    except Exception as exc:  # noqa: BLE001 - lock/IO errors remain typed
        return PhaseExecutionResult(
            False, reason="launch_generation_backup_failed", detail=type(exc).__name__
        )


def rebuild_launch_generation(
    *, home, backup_root, planned, observe, backup_receipt, replaying
) -> PhaseExecutionResult:
    path = herdr_launch_generation_path(home)
    if not _planned_shape_exact(planned):
        return PhaseExecutionResult(False, reason="launch_generation_plan_drift")
    try:
        with launch_generation_store_lock(home, exclusive=True, blocking=False):
            observed = observe()
            if planned["state"] == "absent":
                ok, outcome = observed == planned, "already_absent"
            elif planned["version"] == 2:
                ok, outcome = observed == planned, "already_current"
            else:
                backup = backup_root / "launch-generation.sqlite3"
                try:
                    metadata = backup.lstat()
                    artifact_ok = (
                        stat.S_ISREG(metadata.st_mode)
                        and metadata.st_uid == os.geteuid()
                        and stat.S_IMODE(metadata.st_mode) == 0o600
                        and _sqlite_content_digest(backup)
                        == planned.get("content_digest")
                        and _artifact_matches_plan(backup, planned)
                    )
                except OSError:
                    artifact_ok = False
                backup_ok = (
                    artifact_ok
                    and backup_receipt.get("launch_generation_backup") is True
                    and backup_receipt.get("launch_generation_content_digest")
                    == planned.get("content_digest")
                )
                if not backup_ok:
                    return PhaseExecutionResult(
                        False, reason="launch_generation_backup_unverified"
                    )
                digest = planned["content_digest"]
                if replaying:
                    try:
                        pinned = _read_rebuild_authority(
                            backup_root=backup_root, content_digest=digest
                        )
                        _directory_fsync(backup_root)
                    except FileNotFoundError:
                        if observed != planned:
                            return PhaseExecutionResult(
                                False, reason="launch_generation_plan_drift"
                            )
                        pinned = _publish_rebuild_authority(
                            backup_root=backup_root, path=path,
                            content_digest=digest,
                        )
                    try:
                        metadata = path.lstat()
                    except FileNotFoundError:
                        metadata = None
                    source = None if metadata is None else (
                        metadata.st_dev, metadata.st_ino,
                        metadata.st_ctime_ns, metadata.st_size,
                    )
                    if source is not None and source != pinned:
                        return PhaseExecutionResult(
                            False, reason="launch_generation_plan_drift"
                        )
                elif observed == planned:
                    pinned = _publish_rebuild_authority(
                        backup_root=backup_root, path=path, content_digest=digest
                    )
                else:
                    return PhaseExecutionResult(False, reason="launch_generation_plan_drift")
                remove_attestation_store_artifacts(path)
                _directory_fsync(path.parent)
                ok = observe()["state"] == "absent"
                outcome = (
                    "rebuild_replay_verified" if replaying
                    else "rebuilt_for_v2_restore"
                )
    except Exception as exc:  # noqa: BLE001 - maintenance errors remain typed
        return PhaseExecutionResult(
            False, reason="launch_generation_rebuild_failed",
            detail=type(exc).__name__,
        )
    return PhaseExecutionResult(
        ok, reason="" if ok else "launch_generation_rebuild_unverified",
        receipt={"outcome": outcome} if ok else {},
    )


__all__ = ("backup_launch_generation", "rebuild_launch_generation")
