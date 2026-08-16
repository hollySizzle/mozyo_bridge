"""Exclusive advisory lock used by the startup transaction fence (#13948)."""

from __future__ import annotations

import errno
import fcntl
import os

from mozyo_bridge.core.state.startup_action_capability import (
    StartupTransactionBusy,
    StartupTransactionError,
)


def _close_os_fd_quietly(fd) -> None:
    """Close an fd during acquire cleanup without masking the primary failure."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class FenceLock:
    """Exclusive, non-blocking lock held across an external compensation effect."""

    def __init__(
        self,
        fence,
        *,
        error_type=StartupTransactionError,
        busy_type=StartupTransactionBusy,
    ) -> None:
        self._fence = fence
        self._error_type = error_type
        self._busy_type = busy_type
        self._nested = False

    def __enter__(self) -> "FenceLock":
        fence = self._fence
        if fence._lock_depth > 0:
            fence._lock_depth += 1
            self._nested = True
            return self
        fd = None
        try:
            lock = fence.lock_path
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            contention = (
                fd is not None
                and getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN)
            )
            _close_os_fd_quietly(fd)
            if contention:
                raise self._busy_type(
                    "another startup transaction holds this authority; refusing to wait "
                    "or steal it — nothing was started or closed"
                ) from exc
            raise self._error_type(
                f"could not take the startup transaction lock ({exc}); fail closed"
            ) from exc
        fence._lock_fd = fd
        fence._lock_depth = 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        fence = self._fence
        if self._nested:
            fence._lock_depth -= 1
            return
        if fence._lock_fd is None:
            return
        fd = fence._lock_fd
        fence._lock_fd = None
        fence._lock_depth = 0
        release_error = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as unlock_exc:
            release_error = unlock_exc
        try:
            os.close(fd)
        except OSError as close_exc:
            release_error = release_error or close_exc
        if release_error is not None and exc_type is None:
            raise self._error_type(
                f"could not release the startup transaction lock ({release_error}); "
                "fail closed"
            ) from release_error


__all__ = ("FenceLock",)
