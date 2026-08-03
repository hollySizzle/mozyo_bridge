"""Per-action exclusion for replacement transaction effects (Redmine #14741 R12).

``state.sqlite`` is shared by every workspace and lane. Holding its global writer lock
while an external transport runs turns one slow send into an unrelated-lane outage. This
module supplies the narrower authority: one advisory lock file for one exact
``(canonical state-store pathname, workspace_id, action_id)``. Replacement-transaction writers and
the continuation transport take the same lock, so same-action state cannot cross the
effect while different actions remain free to use short SQLite transactions.

The lock file is coordination metadata, not durable action evidence. It is intentionally
kept after release: unlinking an advisory-lock inode while another process has it open can
split lock authority. The authority directory is adjacent to the canonical DB pathname,
owner-private, and remains stable if ``state.sqlite`` is atomically replaced. State-file
symlinks and hardlinks are rejected; parent-directory aliases converge through resolution.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import sqlite3
import stat
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


_LOCK_DIRECTORY_SUFFIX = ".replacement-action-fences"
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


def _canonical_state_path(state_path: Path) -> Path:
    """Resolve a stable pathname authority without following a final file symlink."""

    supplied = Path(state_path)
    try:
        supplied_info = supplied.lstat()
    except FileNotFoundError:
        supplied_info = None
    except OSError as exc:
        raise ReplacementTransactionActionFenceError(
            f"replacement state path unavailable ({type(exc).__name__}); "
            "fail closed"
        ) from exc
    if supplied_info is not None and stat.S_ISLNK(supplied_info.st_mode):
        raise ReplacementTransactionActionFenceError(
            "replacement state file symlink is not one pathname authority; fail closed"
        )
    try:
        parent = supplied.parent.resolve(strict=True)
    except OSError as exc:
        raise ReplacementTransactionActionFenceError(
            f"replacement state parent unavailable ({type(exc).__name__}); fail closed"
        ) from exc
    return parent / supplied.name


def validate_replacement_state_path(
    state_path: Path, *, require_exists: bool = True
) -> Path:
    """Return one canonical regular-file authority or fail closed.

    The check is intentionally reusable at every use boundary.  A caller that validated a
    regular pathname before taking the action lock must not later let SQLite follow a
    symlink or hardlink installed at that pathname (R14-F2).
    """

    canonical = _canonical_state_path(state_path)
    try:
        info = canonical.lstat()
    except FileNotFoundError:
        if not require_exists:
            return canonical
        raise ReplacementTransactionActionFenceError(
            "replacement state authority disappeared; fail closed"
        ) from None
    except OSError as exc:
        raise ReplacementTransactionActionFenceError(
            f"replacement state authority unavailable ({type(exc).__name__}); fail closed"
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
    ):
        raise ReplacementTransactionActionFenceError(
            "replacement state authority is not one regular linked file; fail closed"
        )
    return canonical


def connect_validated_replacement_state(state_path: Path, ensure_schema):
    """Open SQLite only while the final pathname remains one regular authority."""

    conn = None
    try:
        path = validate_replacement_state_path(state_path, require_exists=False)
        ensure_schema(path)
        validate_replacement_state_path(path)
        conn = sqlite3.connect(path, isolation_level=None)
        validate_replacement_state_path(path)
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        raise


def _action_lock_coordinates(
    state_path: Path, key: ReplacementTransactionKey
) -> tuple[Path, tuple[str, str]]:
    canonical = validate_replacement_state_path(state_path, require_exists=False)
    try:
        ensure_replacement_transaction_schema(canonical)
        canonical = validate_replacement_state_path(canonical)
    except (ReplacementTransactionActionFenceError, ReplacementTransactionError) as exc:
        raise ReplacementTransactionActionFenceError(
            f"replacement state identity unavailable ({type(exc).__name__}); fail closed"
        ) from exc
    digest = _action_digest(key)
    lock_directory = canonical.parent / f".{canonical.name}{_LOCK_DIRECTORY_SUFFIX}"
    _ensure_private_directory(lock_directory)
    identity = (str(canonical), digest)
    path = lock_directory / f"{digest}.lock"
    return path, identity


def replacement_transaction_action_lock_path(
    state_path: Path, key: ReplacementTransactionKey
) -> Path:
    """Return the lock path for the canonical DB pathname + exact action."""

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
        current = validate_replacement_state_path(state_path)
        if str(current) != identity[0]:
            raise ReplacementTransactionActionFenceError(
                "replacement state canonical authority changed after lock selection; "
                "fail closed"
            )
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
                validate_replacement_state_path(self.path)
                return method(self, key, *args, **kwargs)
        except ReplacementTransactionActionFenceError as exc:
            raise ReplacementTransactionError(
                "replacement transaction action fence unavailable; fail closed"
            ) from exc

    return guarded


__all__ = (
    "ReplacementTransactionActionFenceError",
    "connect_validated_replacement_state",
    "replacement_transaction_action_fence",
    "replacement_transaction_action_fenced",
    "replacement_transaction_action_lock_path",
    "validate_replacement_state_path",
)
