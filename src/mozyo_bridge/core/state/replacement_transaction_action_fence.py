"""Per-action exclusion for replacement transaction effects (Redmine #14741 R12).

``state.sqlite`` is shared by every workspace and lane. Holding its global writer lock
while an external transport runs turns one slow send into an unrelated-lane outage. This
module supplies the narrower authority: one advisory lock file for one exact
``(state-store inode, workspace_id, action_id)``. Replacement-transaction writers and
the continuation transport take the same lock, so same-action state cannot cross the
effect while different actions remain free to use short SQLite transactions.

The lock file is coordination metadata, not durable action evidence. It is intentionally
kept after release: unlinking an advisory-lock inode while another process has it open can
split lock authority. The filename contains only state device/inode numbers and a SHA-256
digest of the normalized action key.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Iterator

from mozyo_bridge.core.state.replacement_transaction_model import ReplacementTransactionKey
from mozyo_bridge.core.state.replacement_transaction_schema import (
    ReplacementTransactionError,
    ensure_replacement_transaction_schema,
)


_LOCK_ROOT_PREFIX = "mozyo-bridge-"
_LOCK_DIRECTORY_NAME = "replacement-transaction-action-fences"
_THREAD_STATE = threading.local()


class ReplacementTransactionActionFenceError(RuntimeError):
    """The exact action fence could not be acquired safely."""


def _action_digest(key: ReplacementTransactionKey) -> str:
    workspace = key.workspace_id.encode("utf-8")
    action = key.action_id.encode("utf-8")
    return hashlib.sha256(
        len(workspace).to_bytes(8, "big")
        + workspace
        + len(action).to_bytes(8, "big")
        + action
    ).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise ReplacementTransactionActionFenceError(
            f"replacement action fence directory unavailable ({type(exc).__name__}); "
            "fail closed"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ReplacementTransactionActionFenceError(
            "replacement action fence directory is not private owner authority; fail closed"
        )


def _private_lock_directory() -> Path:
    try:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as exc:
        raise ReplacementTransactionActionFenceError(
            f"replacement action fence temp root unavailable ({type(exc).__name__}); "
            "fail closed"
        ) from exc
    owner_root = temp_root / f"{_LOCK_ROOT_PREFIX}{os.getuid()}"
    _ensure_private_directory(owner_root)
    lock_directory = owner_root / _LOCK_DIRECTORY_NAME
    _ensure_private_directory(lock_directory)
    return lock_directory


def _action_lock_coordinates(
    state_path: Path, key: ReplacementTransactionKey
) -> tuple[Path, tuple[int, int, str]]:
    # Migration to the v2 behavioral protocol MUST happen before acquiring the v2 fence.
    # A v1-only writer then sees an unsupported component version and stops before mutate.
    try:
        ensure_replacement_transaction_schema(Path(state_path))
        state_info = os.stat(state_path)
    except (OSError, ReplacementTransactionError) as exc:
        raise ReplacementTransactionActionFenceError(
            f"replacement state identity unavailable ({type(exc).__name__}); fail closed"
        ) from exc
    if not stat.S_ISREG(state_info.st_mode) or state_info.st_nlink != 1:
        raise ReplacementTransactionActionFenceError(
            "replacement state authority is not one regular linked file; fail closed"
        )
    digest = _action_digest(key)
    identity = (int(state_info.st_dev), int(state_info.st_ino), digest)
    path = _private_lock_directory() / (
        f"{identity[0]:x}-{identity[1]:x}-{digest}.lock"
    )
    return path, identity


def replacement_transaction_action_lock_path(
    state_path: Path, key: ReplacementTransactionKey
) -> Path:
    """Return the secret-free lock path for the canonical DB inode + exact action."""

    path, _identity = _action_lock_coordinates(state_path, key)
    return path


def _open_lock(path: Path) -> int:
    fd = -1
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ReplacementTransactionActionFenceError(
                "replacement action fence is not one private owner file; fail closed"
            )
        return fd
    except ReplacementTransactionActionFenceError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    except OSError as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ReplacementTransactionActionFenceError(
            f"replacement action fence open failed ({type(exc).__name__}); fail closed"
        ) from exc


@contextmanager
def replacement_transaction_action_fence(
    state_path: Path, key: ReplacementTransactionKey
) -> Iterator[None]:
    """Hold the exact action's cross-process effect fence until context exit."""

    path, identity = _action_lock_coordinates(state_path, key)
    held = getattr(_THREAD_STATE, "held", None)
    if held is None:
        held = set()
        _THREAD_STATE.held = held
    if identity in held:
        raise ReplacementTransactionActionFenceError(
            "replacement action fence re-entry would deadlock; fail closed"
        )

    fd = _open_lock(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                detail = "replacement action fence remained busy"
            else:
                detail = f"replacement action fence lock failed ({type(exc).__name__})"
            raise ReplacementTransactionActionFenceError(
                detail + "; fail closed"
            ) from exc
        held.add(identity)
        try:
            yield
        finally:
            held.remove(identity)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def replacement_transaction_action_fenced(method):
    """Decorate one ``ReplacementTransactionStore`` exact-key mutation."""

    @wraps(method)
    def guarded(self, key: ReplacementTransactionKey, *args, **kwargs):
        try:
            with replacement_transaction_action_fence(self.path, key):
                return method(self, key, *args, **kwargs)
        except ReplacementTransactionActionFenceError as exc:
            raise ReplacementTransactionError(
                "replacement transaction action fence unavailable; fail closed"
            ) from exc

    return guarded


__all__ = (
    "ReplacementTransactionActionFenceError",
    "replacement_transaction_action_fence",
    "replacement_transaction_action_fenced",
    "replacement_transaction_action_lock_path",
)
