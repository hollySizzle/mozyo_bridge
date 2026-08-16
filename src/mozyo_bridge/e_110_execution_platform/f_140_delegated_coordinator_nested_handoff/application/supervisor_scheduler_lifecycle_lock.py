"""Advisory lifecycle serialization for the owned OS scheduler artifacts.

The OS APIs used by the supervisor are path/name based: launchd consumes a plist path and systemd
reloads unit names from its search path.  Neither manager offers a compare-and-swap operation that
starts a service only when a caller supplied definition digest still matches.  This lock therefore
binds the bridge's own cooperating writers into one critical section spanning filesystem work,
manager reload/unload/load, manager attestation, and start/restart (Redmine #15192 j#103093).

This is deliberately an *advisory* boundary.  A process running as the same uid can ignore
``flock``, unlink the lock name, edit the scheduler files directly, or invoke the manager itself.
Such a non-cooperating same-uid writer is outside the guarantee; claiming otherwise would turn a
user-owned lock into a security boundary it cannot be.  Stable drift is still detected by the
backend's fresh reads and manager attestation, but swap-consume-restore by that actor is not
preventable without privilege/uid separation.

Callers supply an already pinned scheduler-directory fd.  Keeping path traversal in the backend
filesystem adapters preserves their distinct roots while this object owns the common lock-file
identity and lifetime rules.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from types import TracebackType
from typing import Optional, Type


LIFECYCLE_LOCK_NAME = ".mozyo-bridge-callback-supervisor.lifecycle.lock"
LIFECYCLE_LOCK_MODE = 0o600


class SchedulerLifecycleLockError(OSError):
    """The lifecycle lock could not establish a trustworthy serialization boundary."""


class SchedulerLifecycleLockBusy(SchedulerLifecycleLockError):
    """Another cooperating scheduler lifecycle operation currently owns the lock."""


class SchedulerLifecycleLockUnsafe(SchedulerLifecycleLockError):
    """The lock name is not one owned, private, stable regular file."""


class SchedulerLifecycleLock:
    """A held exclusive advisory lock; closing it releases the cooperating-writer fence."""

    def __init__(self, fd: int) -> None:
        self._fd: Optional[int] = fd

    @classmethod
    def acquire(
        cls, dir_fd: int, *, name: str = LIFECYCLE_LOCK_NAME
    ) -> "SchedulerLifecycleLock":
        """Open, authenticate, and non-blockingly lock ``name`` relative to ``dir_fd``.

        The name is never removed or replaced by bridge code.  A post-lock inode comparison closes
        the open-vs-lock replacement window for cooperating code and fails closed if an unsafe
        pre-existing entry is observed.
        """
        flags = os.O_RDWR | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            try:
                fd = os.open(
                    name, flags | os.O_CREAT | os.O_EXCL,
                    LIFECYCLE_LOCK_MODE, dir_fd=dir_fd,
                )
                # The process umask must not turn a newly-created coordination object into a
                # permanently unsafe entry. Existing entries are never repaired implicitly.
                try:
                    os.fchmod(fd, LIFECYCLE_LOCK_MODE)
                except OSError:
                    os.close(fd)
                    raise
            except FileExistsError:
                fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError as exc:
            raise SchedulerLifecycleLockUnsafe(
                exc.errno, "scheduler lifecycle lock entry is unavailable"
            ) from exc
        try:
            opened = os.fstat(fd)
            cls._require_safe(opened)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise SchedulerLifecycleLockBusy(
                        exc.errno, "scheduler lifecycle is already locked"
                    ) from exc
                raise SchedulerLifecycleLockError(
                    exc.errno, "scheduler lifecycle lock could not be acquired"
                ) from exc

            named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            cls._require_safe(named)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise SchedulerLifecycleLockUnsafe(
                    errno.EAGAIN, "scheduler lifecycle lock identity changed"
                )
            return cls(fd)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _require_safe(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != LIFECYCLE_LOCK_MODE
        ):
            raise SchedulerLifecycleLockUnsafe(
                errno.EPERM, "scheduler lifecycle lock identity is unsafe"
            )

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> "SchedulerLifecycleLock":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()


__all__ = (
    "LIFECYCLE_LOCK_NAME",
    "LIFECYCLE_LOCK_MODE",
    "SchedulerLifecycleLock",
    "SchedulerLifecycleLockError",
    "SchedulerLifecycleLockBusy",
    "SchedulerLifecycleLockUnsafe",
)
