"""Pinned filesystem seam for the Linux systemd supervisor adapter (Redmine #15192).

The systemd user unit directory is a mutation boundary, not a display path. Every component below a
trusted root is opened ``O_NOFOLLOW | O_DIRECTORY`` and every leaf read, write and unlink is relative
to the resulting directory descriptor. A symlinked ancestor therefore cannot redirect an operation
outside the owned tree (review j#102843 finding r15f1).

Systemd identifies a user unit by its exact filename. At that name this adapter accepts only a
regular, singly-linked file as an owned artifact. Symlinks, hard links, directories, devices and
unopenable entries are ``unreadable`` and never authorize mutation. Reads and classification use
one descriptor; writes assemble a writer-private ``O_EXCL`` staging entry and replace the name only
after all bytes are durable. No caller receives bytes from an unidentified entry.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_systemd_unit import (  # noqa: E501
    CONFIG_DIR_RELATIVE,
    UNIT_DIR_RELATIVE,
    SUPERVISOR_UNIT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_scheduler_lifecycle_lock import (  # noqa: E501
    SchedulerLifecycleLock,
)

UNIT_ABSENT = "absent"
UNIT_OWNED = "owned"
UNIT_UNREADABLE = "unreadable"

_STAGING_SUFFIX = ".mozyo-staging"
_STAGING_RANDOM_BYTES = 16
_STAGING_CREATE_ATTEMPTS = 8


class OwnedUnitPathError(OSError):
    """The unit directory or an entry cannot be used through the pinned ownership boundary."""


class UnsafeUnitArtifactError(OwnedUnitPathError):
    """A named unit entry exists but is not a regular, singly-linked file."""


@dataclass(frozen=True)
class OwnedUnitSnapshot:
    """One pinned read of both owned unit names; unidentified entries carry no bytes."""

    service_state: str
    service_payload: bytes
    timer_state: str
    timer_payload: bytes


def open_unit_dir(os_home: Optional[Path] = None, *, create: bool = False) -> int:
    """Open the owned systemd user unit directory and return a caller-owned directory fd.

    An explicit ``os_home`` and the fallback ``Path.home()`` are trusted roots. An absolute raw
    ``XDG_CONFIG_HOME`` is anchored at ``/`` and walked component by component, preserving its exact
    spelling (including whitespace) while still refusing symlink traversal.
    """
    root, relative = _root_and_relative(os_home)
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise OwnedUnitPathError(exc.errno, "systemd unit root unavailable") from exc
    for part in relative.parts:
        fd = _descend(fd, part, create=create)
    return fd


def _root_and_relative(os_home: Optional[Path]) -> tuple[Path, Path]:
    if os_home is not None:
        return Path(os_home), CONFIG_DIR_RELATIVE / UNIT_DIR_RELATIVE
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    if os.path.isabs(xdg):
        absolute = Path(xdg)
        # Linux is the only supported caller. ``anchor`` is therefore ``/`` (or ``//`` where that
        # spelling is meaningful); every remaining component is still walked no-follow.
        return Path(absolute.anchor), Path(*absolute.parts[1:]) / UNIT_DIR_RELATIVE
    return Path.home(), CONFIG_DIR_RELATIVE / UNIT_DIR_RELATIVE


def _descend(parent_fd: int, name: str, *, create: bool) -> int:
    """Open one directory component no-follow and always close ``parent_fd``."""
    try:
        try:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
        except FileNotFoundError:
            if not create:
                raise OwnedUnitPathError(errno.ENOENT, "systemd unit path component missing")
            try:
                os.mkdir(name, 0o755, dir_fd=parent_fd)
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
            except OSError as exc:
                raise OwnedUnitPathError(
                    exc.errno, "systemd unit path component could not be created safely"
                ) from exc
        except OSError as exc:
            raise OwnedUnitPathError(
                exc.errno, "systemd unit path component is not a directory"
            ) from exc
    finally:
        os.close(parent_fd)
    return child


def read_units(os_home: Optional[Path] = None) -> OwnedUnitSnapshot:
    """Read both owned names through one pinned directory, without creating anything."""
    try:
        dir_fd = open_unit_dir(os_home, create=False)
    except OwnedUnitPathError as exc:
        state = UNIT_ABSENT if exc.errno == errno.ENOENT else UNIT_UNREADABLE
        return OwnedUnitSnapshot(state, b"", state, b"")
    try:
        service_state, service_payload = _read_entry(dir_fd, SUPERVISOR_UNIT.service_unit)
        timer_state, timer_payload = _read_entry(dir_fd, SUPERVISOR_UNIT.timer_unit)
        return OwnedUnitSnapshot(
            service_state, service_payload, timer_state, timer_payload
        )
    finally:
        os.close(dir_fd)


def _read_entry(dir_fd: int, name: str) -> tuple[str, bytes]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return UNIT_ABSENT, b""
    except OSError:
        return UNIT_UNREADABLE, b""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return UNIT_UNREADABLE, b""
        return UNIT_OWNED, _read_all(fd)
    except OSError:
        return UNIT_UNREADABLE, b""
    finally:
        os.close(fd)


def write_units(
    service_payload: bytes, timer_payload: bytes, os_home: Optional[Path] = None
) -> None:
    """Write the service then timer through one pinned directory.

    This is intentionally not an atomic *pair* transaction: the public contract permits a partial
    install to be repaired by rerunning ``install``. It does guarantee that each individual file is
    complete-or-unchanged, and that no unidentified existing artifact is touched.
    """
    dir_fd = open_unit_dir(os_home, create=True)
    try:
        states = {
            SUPERVISOR_UNIT.service_unit: _read_entry(dir_fd, SUPERVISOR_UNIT.service_unit)[0],
            SUPERVISOR_UNIT.timer_unit: _read_entry(dir_fd, SUPERVISOR_UNIT.timer_unit)[0],
        }
        if any(state == UNIT_UNREADABLE for state in states.values()):
            raise UnsafeUnitArtifactError(
                errno.EPERM, "systemd unit entry is not an owned regular file"
            )
        _write_entry(dir_fd, SUPERVISOR_UNIT.service_unit, service_payload)
        _write_entry(dir_fd, SUPERVISOR_UNIT.timer_unit, timer_payload)
    finally:
        os.close(dir_fd)


def _write_entry(dir_fd: int, target_name: str, payload: bytes) -> None:
    # Revalidate at the action point. Rename replaces a name rather than truncating the inode, but
    # an unidentified current occupant still refuses by contract.
    state, _payload = _read_entry(dir_fd, target_name)
    if state == UNIT_UNREADABLE:
        raise UnsafeUnitArtifactError(
            errno.EPERM, "systemd unit entry changed to an unidentified artifact"
        )
    staging, fd = _create_staging(dir_fd, target_name)
    try:
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(staging, target_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        _discard(staging, dir_fd)
        raise


def unlink_units(os_home: Optional[Path] = None) -> bool:
    """Remove safe owned unit entries through one pinned directory; return whether any existed."""
    try:
        dir_fd = open_unit_dir(os_home, create=False)
    except OwnedUnitPathError as exc:
        if exc.errno == errno.ENOENT:
            return False
        raise
    try:
        states = {
            SUPERVISOR_UNIT.timer_unit: _read_entry(dir_fd, SUPERVISOR_UNIT.timer_unit)[0],
            SUPERVISOR_UNIT.service_unit: _read_entry(dir_fd, SUPERVISOR_UNIT.service_unit)[0],
        }
        if any(state == UNIT_UNREADABLE for state in states.values()):
            raise UnsafeUnitArtifactError(
                errno.EPERM, "systemd unit entry changed to an unidentified artifact"
            )
        removed = False
        for name, state in states.items():
            if state == UNIT_OWNED:
                # Re-open immediately before unlink so a late symlink/hardlink/device swap cannot
                # be removed under the earlier classification.
                action_state, _payload = _read_entry(dir_fd, name)
                if action_state != UNIT_OWNED:
                    raise UnsafeUnitArtifactError(
                        errno.EPERM, "systemd unit identity changed before unlink"
                    )
                os.unlink(name, dir_fd=dir_fd)
                removed = True
        return removed
    finally:
        os.close(dir_fd)


def acquire_lifecycle_lock(os_home: Optional[Path] = None) -> SchedulerLifecycleLock:
    """Serialize cooperating mutating verbs in the pinned user-unit directory."""
    dir_fd = open_unit_dir(os_home, create=True)
    try:
        return SchedulerLifecycleLock.acquire(dir_fd)
    finally:
        os.close(dir_fd)


def _create_staging(dir_fd: int, target_name: str) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    for _attempt in range(_STAGING_CREATE_ATTEMPTS):
        staging = (
            f".{target_name}{_STAGING_SUFFIX}-"
            f"{secrets.token_hex(_STAGING_RANDOM_BYTES)}"
        )
        try:
            fd = os.open(staging, flags, 0o644, dir_fd=dir_fd)
        except FileExistsError:
            continue
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            return staging, fd
        os.close(fd)
        _discard(staging, dir_fd)
        raise UnsafeUnitArtifactError(errno.EPERM, "systemd staging entry is unsafe")
    raise OwnedUnitPathError(errno.EEXIST, "unable to allocate systemd staging entry")


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(fd, payload[written:])
        if count <= 0:
            raise OSError(errno.EIO, "short write to systemd unit staging entry")
        written += count


def _discard(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


__all__ = (
    "UNIT_ABSENT",
    "UNIT_OWNED",
    "UNIT_UNREADABLE",
    "OwnedUnitPathError",
    "UnsafeUnitArtifactError",
    "OwnedUnitSnapshot",
    "open_unit_dir",
    "read_units",
    "write_units",
    "unlink_units",
    "acquire_lifecycle_lock",
)
