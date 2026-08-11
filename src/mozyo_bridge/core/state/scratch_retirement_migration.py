"""Backup-first v1→v2 preparation for the scratch retirement authority (#15227)."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import sys
from pathlib import Path

from mozyo_bridge.core.state.scratch_retirement_attempt_codec import (
    ScratchRetirementAttemptCodecError,
    decode_attempt_detail,
    decode_close_pairs,
    encode_attempt_detail,
    encode_close_pairs,
)
from mozyo_bridge.core.state.scratch_retirement_pin import (
    ScratchRetirementPinError,
    decode_scratch_retirement_pin_projection,
)


class ScratchRetirementMigrationError(RuntimeError):
    pass


def migrate_scratch_retirement_v1_locked(path: Path, seal_path: Path) -> int:
    """Return the current version, migrating exact v1 under the caller's exclusive lock."""
    version = _version(path)
    if version == 2:
        return 2
    if version != 1:
        raise ScratchRetirementMigrationError("scratch retirement schema is unsupported")
    _regular_owned(path)
    _regular_owned(seal_path)
    for suffix in ("-wal", "-shm", "-journal"):
        artifact = path.with_name(path.name + suffix)
        if os.path.lexists(artifact):
            _regular_owned(artifact)
    nonce = _canonical_text(seal_path)
    if _store_nonce(path) != nonce:
        raise ScratchRetirementMigrationError("scratch retirement seal does not match")
    wanted = _logical_digest(path)
    backup = path.with_name(path.name + ".v1.backup")
    backup_seal = backup.with_name(backup.name + ".seal")
    _publish_backup(path, seal_path, backup, backup_seal, wanted, nonce)
    # Legacy writers inherited the process umask. The verified backup exists before the
    # migration normalizes the now-token-bearing v2 authority to private owner-only mode.
    os.chmod(path, 0o600)
    os.chmod(seal_path, 0o600)
    _fsync_file(path)
    _fsync_file(seal_path)
    _fsync_dir(path.parent)
    # No authority byte is changed until the complete, durable v1 backup was read back.
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != 1:
            raise ScratchRetirementMigrationError("scratch retirement schema drifted")
        conn.execute("PRAGMA user_version = 2")
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise
    finally:
        conn.close()
    _fsync_file(path)
    _fsync_dir(path.parent)
    return 2


def _publish_backup(
    source: Path,
    seal: Path,
    backup: Path,
    backup_seal: Path,
    wanted: str,
    nonce: str,
) -> None:
    control = backup.with_name(backup.name + ".migration")
    for legacy in (
        backup.with_name(backup.name + ".staging"),
        backup_seal.with_name(backup_seal.name + ".staging"),
    ):
        if os.path.lexists(legacy):
            raise ScratchRetirementMigrationError(
                "scratch retirement unproven flat staging artifact exists"
            )
    _reject_backup_sidecars(backup)
    final_main = os.path.lexists(backup)
    final_seal = os.path.lexists(backup_seal)
    if final_main and final_seal and not os.path.lexists(control):
        _verify_backup(backup, backup_seal, wanted, nonce)
        return
    if (final_main or final_seal) and not os.path.lexists(control):
        raise ScratchRetirementMigrationError(
            "scratch retirement partial backup lacks durable staging provenance"
        )
    staging_root, artifact_pins = _staging_root(
        control, backup, backup_seal, wanted, nonce
    )
    staging = staging_root / "store.sqlite3"
    staging_seal = staging_root / "store.seal"
    if not final_main:
        _repair_staging_database(source, staging, wanted, artifact_pins["store.sqlite3"])
    if not final_seal:
        _repair_staging_seal(staging_seal, nonce, artifact_pins["store.seal"])
    if not final_main:
        _publish_pinned_link(staging, artifact_pins["store.sqlite3"], backup)
    if not final_seal:
        _publish_pinned_link(staging_seal, artifact_pins["store.seal"], backup_seal)
    _verify_staging_locations(staging_root, backup, backup_seal, artifact_pins)
    _verify_backup(backup, backup_seal, wanted, nonce)
    # Retain the verified control + marker inode as durable migration provenance. Trying
    # to unlink the staged links and directory before stamping v2 created unreplayable crash
    # windows. Normal runtime recognizes only the exact marker + two pinned hardlink shape
    # beside the complete backup pair; any partial/foreign shape remains damaged.


def _staging_root(
    control: Path, backup: Path, backup_seal: Path, wanted: str, nonce: str
) -> tuple[Path, dict[str, tuple[int, int]]]:
    if not os.path.lexists(control):
        name = f".{backup.name}.staging-{secrets.token_hex(16)}"
        root = backup.parent / name
        os.mkdir(root, 0o700)
        artifact_pins = {}
        for artifact_name in ("store.sqlite3", "store.seal"):
            artifact = root / artifact_name
            fd = os.open(artifact, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            info = os.fstat(fd)
            os.close(fd)
            artifact_pins[artifact_name] = (info.st_dev, info.st_ino)
        marker = root / "authority.json"
        payload = _staging_payload(name, wanted, nonce, artifact_pins)
        _write_private(marker, payload)
        _fsync_dir(root)
        try:
            os.link(marker, control)
        except BaseException as exc:
            # Without the durable control hardlink there is no crash-surviving proof that
            # this process still owns every pathname below ``root``.  A same-UID writer
            # can replace one between validation and unlink, so retain the exact 0700
            # directory as recovery evidence instead of deleting by pathname.
            raise ScratchRetirementMigrationError(
                "scratch retirement control publish failed; unproven private staging "
                "was retained for explicit recovery"
            ) from exc
        _fsync_dir(control.parent)
    _regular_owned(control, exact_mode=True)
    try:
        payload = json.loads(control.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ScratchRetirementMigrationError(
            "scratch retirement staging authority is unreadable"
        ) from exc
    if (
        type(payload) is not dict
        or set(payload) != {
            "v", "staging_name", "source_digest", "seal_nonce", "artifacts",
            "artifact_pins",
        }
        or type(payload.get("v")) is not int
        or payload["v"] != 1
        or type(payload.get("staging_name")) is not str
        or not payload["staging_name"].startswith(f".{backup.name}.staging-")
        or Path(payload["staging_name"]).name != payload["staging_name"]
        or payload.get("source_digest") != wanted
        or payload.get("seal_nonce") != nonce
        or payload.get("artifacts") != ["authority.json", "store.sqlite3", "store.seal"]
        or not _artifact_pins_payload(payload.get("artifact_pins"))
    ):
        raise ScratchRetirementMigrationError(
            "scratch retirement staging authority does not match the locked source"
        )
    root = control.parent / payload["staging_name"]
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ScratchRetirementMigrationError("scratch retirement staging directory is unsafe")
    marker = root / "authority.json"
    _regular_owned(marker, exact_mode=True)
    if marker.stat().st_ino != control.stat().st_ino or marker.stat().st_dev != control.stat().st_dev:
        raise ScratchRetirementMigrationError("scratch retirement staging authority inode drifted")
    artifact_pins = {
        name: tuple(values) for name, values in payload["artifact_pins"].items()
    }
    if marker.read_bytes() != _staging_payload(
        root.name, wanted, nonce, artifact_pins
    ):
        raise ScratchRetirementMigrationError("scratch retirement staging marker drifted")
    if any(child.name not in {"authority.json", "store.sqlite3", "store.seal"} for child in root.iterdir()):
        raise ScratchRetirementMigrationError("scratch retirement staging contains unknown artifacts")
    _verify_staging_locations(root, backup, backup_seal, artifact_pins)
    return root, artifact_pins


def _staging_payload(
    name: str, wanted: str, nonce: str, artifact_pins: dict[str, tuple[int, int]]
) -> bytes:
    return json.dumps(
        {"v": 1, "staging_name": name, "source_digest": wanted,
         "seal_nonce": nonce,
         "artifacts": ["authority.json", "store.sqlite3", "store.seal"],
         "artifact_pins": {key: list(value) for key, value in artifact_pins.items()}},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _repair_staging_database(
    source: Path, staging: Path, wanted: str, pin: tuple[int, int]
) -> None:
    _require_pinned_artifact(staging, pin)
    try:
        valid = _logical_digest(staging) == wanted
    except (ScratchRetirementMigrationError, sqlite3.DatabaseError):
        valid = False
    if not valid:
        _truncate_pinned(staging, pin)
        _copy_sqlite(source, staging, pin)
    _require_pinned_artifact(staging, pin)


def _repair_staging_seal(staging: Path, nonce: str, pin: tuple[int, int]) -> None:
    _require_pinned_artifact(staging, pin)
    try:
        valid = _canonical_text(staging) == nonce
    except (ScratchRetirementMigrationError, OSError, UnicodeError):
        valid = False
    if not valid:
        _truncate_pinned(staging, pin)
        _write_seal(staging, nonce, pin)
    _require_pinned_artifact(staging, pin)


def _verify_backup(backup: Path, seal: Path, wanted: str, nonce: str) -> None:
    _reject_backup_sidecars(backup)
    _regular_owned(backup, exact_mode=True)
    _regular_owned(seal, exact_mode=True)
    if _logical_digest(backup) != wanted or _canonical_text(seal) != nonce:
        raise ScratchRetirementMigrationError("scratch retirement backup does not match")
    _fsync_file(backup)
    _fsync_file(seal)
    _fsync_dir(backup.parent)


def _artifact_pins_payload(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == {"store.sqlite3", "store.seal"}
        and all(
            type(parts) is list
            and len(parts) == 2
            and all(type(part) is int and part >= 0 for part in parts)
            for parts in value.values()
        )
    )


def _require_pinned_artifact(path: Path, pin: tuple[int, int]) -> None:
    _regular_owned(path, exact_mode=True)
    info = path.lstat()
    if (info.st_dev, info.st_ino) != pin:
        raise ScratchRetirementMigrationError(
            "scratch retirement staging artifact inode drifted"
        )


def _verify_staging_locations(
    root: Path, backup: Path, backup_seal: Path,
    pins: dict[str, tuple[int, int]],
) -> None:
    for name, final in (
        ("store.sqlite3", backup), ("store.seal", backup_seal)
    ):
        staging = root / name
        _require_pinned_artifact(staging, pins[name])
        if os.path.lexists(final):
            _require_pinned_artifact(final, pins[name])


def _publish_pinned_link(source: Path, pin: tuple[int, int], target: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(source, flags)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != pin:
            raise ScratchRetirementMigrationError(
                "scratch retirement staging artifact changed before publish"
            )
        if not _link_open_inode(fd, target):
            # Some non-Linux platforms expose no fd-to-link primitive.  The private
            # 0700 staging directory is still under the durable control marker, so a
            # path hardlink is safe only as a no-clobber publication attempt.  Recheck
            # both names afterwards; any same-UID replacement is retained as damaged
            # evidence rather than unlinked or stamped into the v2 authority.
            _require_pinned_artifact(source, pin)
            os.link(source, target, follow_symlinks=False)
    finally:
        os.close(fd)
    _require_pinned_artifact(source, pin)
    _require_pinned_artifact(target, pin)


def _link_open_inode(fd: int, target: Path) -> bool:
    """Hardlink the exact opened inode when the platform exposes a safe primitive.

    Linux ``linkat(AT_EMPTY_PATH)`` binds the destination to ``fd`` itself.  Going via
    ``/proc/self/fd`` looks equivalent but crosses a procfs/bind-mount boundary under
    common sandbox runners and can fail with ``EXDEV`` even when staging and backup are
    on the same filesystem.  Other platforms retain the fd pseudo-filesystem attempt;
    callers use a checked, fail-closed private-path fallback when neither works.
    """
    unavailable = {
        errno.EACCES,
        errno.EINVAL,
        errno.ENOENT,
        errno.ENOSYS,
        errno.EPERM,
        errno.EXDEV,
    }
    if hasattr(errno, "ENOTSUP"):
        unavailable.add(errno.ENOTSUP)
    if hasattr(errno, "EOPNOTSUPP"):
        unavailable.add(errno.EOPNOTSUPP)

    if sys.platform.startswith("linux"):
        linkat = getattr(ctypes.CDLL(None, use_errno=True), "linkat", None)
        if linkat is not None:
            linkat.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
            )
            linkat.restype = ctypes.c_int
            if linkat(
                fd,
                ctypes.c_char_p(b""),
                -100,  # AT_FDCWD
                ctypes.c_char_p(os.fsencode(os.path.abspath(target))),
                0x1000,  # AT_EMPTY_PATH
            ) == 0:
                return True
            error = ctypes.get_errno()
            if error not in unavailable:
                raise OSError(error, os.strerror(error), target)

    for fd_root in (Path("/proc/self/fd"), Path("/dev/fd")):
        if not fd_root.is_dir():
            continue
        try:
            os.link(fd_root / str(fd), target, follow_symlinks=True)
        except OSError as exc:
            if exc.errno not in unavailable:
                raise
            continue
        return True
    return False


def _truncate_pinned(path: Path, pin: tuple[int, int]) -> None:
    flags = os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != pin:
            raise ScratchRetirementMigrationError(
                "scratch retirement staging artifact changed before rewrite"
            )
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)


def _reject_backup_sidecars(backup: Path) -> None:
    if any(os.path.lexists(backup.with_name(backup.name + suffix))
           for suffix in ("-wal", "-shm", "-journal")):
        raise ScratchRetirementMigrationError("scratch retirement backup has a sidecar")


def _copy_sqlite(source: Path, staging: Path, pin: tuple[int, int]) -> None:
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(staging, flags)
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != pin:
        os.close(fd)
        raise ScratchRetirementMigrationError(
            "scratch retirement staging database changed before copy"
        )
    fd_root = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
    if not fd_root.is_dir():
        os.close(fd)
        raise ScratchRetirementMigrationError(
            "this platform cannot write a pinned retirement backup inode"
        )
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(f"file:{fd_root / str(fd)}?mode=rw", uri=True)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
        os.close(fd)
    _fsync_file(staging)
    _require_pinned_artifact(staging, pin)


def _write_seal(path: Path, nonce: str, pin: tuple[int, int]) -> None:
    flags = os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != pin:
            raise ScratchRetirementMigrationError(
                "scratch retirement staging seal changed before write"
            )
        view = memoryview(nonce.encode("utf-8"))
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short scratch retirement backup seal write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short scratch retirement staging marker write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _version(path: Path) -> int:
    _regular_owned(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _logical_digest(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != 1:
            raise ScratchRetirementMigrationError("scratch retirement backup is not v1")
        _validate_v1(conn)
        tables = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        if tables != ("scratch_retirement", "store_meta"):
            raise ScratchRetirementMigrationError("scratch retirement v1 table shape is unknown")
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return hashlib.sha256(f"v={version}\n{dump}".encode("utf-8")).hexdigest()


_RETIRE_COLUMNS = (
    (0, "workspace_id", "TEXT", 1, None, 0),
    (1, "lane_id", "TEXT", 1, None, 0),
    (2, "slot_digest", "TEXT", 1, None, 0),
    (3, "attempt_id", "TEXT", 1, None, 0),
    (4, "revision", "INTEGER", 1, None, 0),
    (5, "state", "TEXT", 1, None, 0),
    (6, "pinned_json", "TEXT", 1, "''", 0),
    (7, "closed_json", "TEXT", 1, "''", 0),
    (8, "detail", "TEXT", 1, "''", 0),
    (9, "reserved_at", "TEXT", 1, None, 0),
    (10, "updated_at", "TEXT", 1, None, 0),
)
_META_COLUMNS = ((0, "key", "TEXT", 0, None, 1), (1, "value", "TEXT", 1, None, 0))


def _validate_v1(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ScratchRetirementMigrationError("scratch retirement v1 integrity check failed")
    if tuple(conn.execute("PRAGMA table_info(scratch_retirement)")) != _RETIRE_COLUMNS or tuple(
        conn.execute("PRAGMA table_info(store_meta)")
    ) != _META_COLUMNS:
        raise ScratchRetirementMigrationError("scratch retirement v1 column shape is unknown")
    objects = tuple(
        conn.execute(
            "SELECT type,name,tbl_name FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' OR name LIKE 'sqlite_autoindex_%' "
            "ORDER BY type,name"
        )
    )
    if objects != (
        ("index", "sqlite_autoindex_scratch_retirement_1", "scratch_retirement"),
        ("index", "sqlite_autoindex_store_meta_1", "store_meta"),
        ("table", "scratch_retirement", "scratch_retirement"),
        ("table", "store_meta", "store_meta"),
    ):
        raise ScratchRetirementMigrationError("scratch retirement v1 object set is unknown")
    index = tuple(conn.execute("PRAGMA index_list(scratch_retirement)"))
    if len(index) != 1 or tuple(index[0][2:]) != (1, "u", 0) or tuple(
        row[2] for row in conn.execute(f"PRAGMA index_info({index[0][1]})")
    ) != ("workspace_id", "lane_id", "slot_digest"):
        raise ScratchRetirementMigrationError("scratch retirement v1 key shape is unknown")
    meta = tuple(conn.execute("SELECT key, value, typeof(value) FROM store_meta"))
    if len(meta) != 1 or meta[0][0] != "store_nonce" or meta[0][2] != "text":
        raise ScratchRetirementMigrationError("scratch retirement v1 metadata is unknown")
    rows = conn.execute(
        "SELECT workspace_id,lane_id,slot_digest,attempt_id,revision,state,pinned_json,"
        "closed_json,detail,reserved_at,updated_at,typeof(revision),typeof(pinned_json),"
        "typeof(closed_json),typeof(detail) FROM scratch_retirement"
    )
    try:
        for row in rows:
            if (
                any(type(row[i]) is not str or not row[i] or row[i].strip() != row[i] for i in (0, 1, 2, 3, 9, 10))
                or type(row[4]) is not int
                or row[4] < 1
                or row[11] != "integer"
                or row[12:] != ("text", "text", "text")
                or row[5] not in ("pending", "completed")
            ):
                raise ScratchRetirementMigrationError("scratch retirement v1 row is invalid")
            projection = decode_scratch_retirement_pin_projection(row[6])
            if projection.version != 1:
                raise ScratchRetirementMigrationError("scratch retirement v1 pin is not legacy")
            canonical_pins = "\n".join(
                f"{role}\t{locator}" for role, locator in projection.legacy_pairs
            )
            close_pairs = decode_close_pairs(row[7])
            approval, detail = decode_attempt_detail(row[8])
            if (
                row[6] != canonical_pins
                or row[7] != encode_close_pairs(close_pairs)
                or row[8] != encode_attempt_detail(
                    approval_evidence=approval, detail=detail
                )
            ):
                raise ScratchRetirementMigrationError(
                    "scratch retirement v1 row codec is noncanonical"
                )
    except (ScratchRetirementPinError, ScratchRetirementAttemptCodecError) as exc:
        raise ScratchRetirementMigrationError("scratch retirement v1 row codec failed") from exc


def _store_nonce(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM store_meta WHERE key='store_nonce'").fetchone()
    finally:
        conn.close()
    return str(row[0]) if row is not None else ""


def _canonical_text(path: Path) -> str:
    value = path.read_text(encoding="utf-8")
    if not value or value.strip() != value:
        raise ScratchRetirementMigrationError("scratch retirement seal is not canonical")
    return value


def _regular_owned(path: Path, *, exact_mode: bool = False) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ScratchRetirementMigrationError("scratch retirement artifact is unsafe")
    if exact_mode and stat.S_IMODE(info.st_mode) != 0o600:
        raise ScratchRetirementMigrationError("scratch retirement backup mode is unsafe")


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = ("ScratchRetirementMigrationError", "migrate_scratch_retirement_v1_locked")
