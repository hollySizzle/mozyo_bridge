"""Filesystem lock primitive for workspace alias declaration access (#15190)."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    ALIAS_RELATIVE,
    REASON_LOCK_FAILED,
)


LOCK_NAME = f".{Path(ALIAS_RELATIVE).name}.lock"


class WorkspaceAliasLockError(Exception):
    """A fixed typed refusal from lock open, validation, or acquisition."""

    reason = REASON_LOCK_FAILED

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.reason}: {detail}")
        self.detail = detail


def require_lock_visible(dirfd: int, fd: int) -> None:
    """Require ``fd`` to be the safe lock entry still visible in ``dirfd``."""
    try:
        opened = os.fstat(fd)
    except OSError as exc:
        raise WorkspaceAliasLockError(
            f"could not stat opened {LOCK_NAME}: {exc}"
        ) from exc
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise WorkspaceAliasLockError(
            f"{LOCK_NAME} is not a single-linked regular file "
            f"(mode {stat.filemode(opened.st_mode)}, links {opened.st_nlink})"
        )
    try:
        visible = os.stat(LOCK_NAME, dir_fd=dirfd, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceAliasLockError(
            f"{LOCK_NAME} is not safely visible after open ({exc})"
        ) from exc
    if not stat.S_ISREG(visible.st_mode) or visible.st_nlink != 1:
        raise WorkspaceAliasLockError(
            f"visible {LOCK_NAME} is not a single-linked regular file "
            f"(mode {stat.filemode(visible.st_mode)}, links {visible.st_nlink})"
        )
    if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
        raise WorkspaceAliasLockError(
            f"{LOCK_NAME} was replaced while it was being opened or locked"
        )


def open_lock_fd(dirfd: int, *, create: bool, writable: bool) -> Optional[int]:
    """Open and validate the coordination lock without following or blocking."""
    flags = (
        (os.O_RDWR if writable else os.O_RDONLY)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(LOCK_NAME, flags, 0o644, dir_fd=dirfd)
    except FileNotFoundError:
        if not create:
            return None
        raise WorkspaceAliasLockError(
            f"{LOCK_NAME} disappeared while being created"
        ) from None
    except OSError as exc:
        raise WorkspaceAliasLockError(f"could not open {LOCK_NAME}: {exc}") from exc
    try:
        require_lock_visible(dirfd, fd)
    except WorkspaceAliasLockError:
        os.close(fd)
        raise
    return fd


@contextmanager
def mutation_lock(dirfd: int) -> Iterator[int]:
    """Hold one exclusive lock across the caller's complete mutation."""
    fd = open_lock_fd(dirfd, create=True, writable=True)
    assert fd is not None
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        except OSError as exc:
            raise WorkspaceAliasLockError(f"could not lock {LOCK_NAME}: {exc}") from exc
        # A post-flock check prevents separate lock generations after a rename.
        require_lock_visible(dirfd, fd)
        yield fd
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


__all__ = (
    "LOCK_NAME",
    "WorkspaceAliasLockError",
    "mutation_lock",
    "open_lock_fd",
    "require_lock_visible",
)
