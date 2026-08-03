"""Per-action exclusion for replacement transaction effects (Redmine #14741 R11).

``state.sqlite`` is shared by every workspace and lane.  Holding its global writer lock while
an external transport runs therefore turns one slow send into an unrelated-lane outage.  This
module supplies the narrower authority: one advisory lock file for one exact
``(workspace_id, action_id)``.  Replacement-transaction writers and the continuation transport
take the same lock, so same-action state cannot cross the effect while different actions remain
free to use their own short SQLite transactions.

The lock file is coordination metadata, not durable action evidence.  It is intentionally kept
after release: unlinking an advisory-lock inode while another process has it open can split the
lock authority.  The filename contains only a SHA-256 digest of the normalized action key.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Iterator

from mozyo_bridge.core.state.replacement_transaction_model import ReplacementTransactionKey
from mozyo_bridge.core.state.replacement_transaction_schema import ReplacementTransactionError


_LOCK_DIRECTORY_SUFFIX = ".replacement-action-locks"
_THREAD_STATE = threading.local()


class ReplacementTransactionActionFenceError(RuntimeError):
    """The exact action fence could not be acquired safely."""


def replacement_transaction_action_lock_path(
    state_path: Path, key: ReplacementTransactionKey
) -> Path:
    """Return the secret-free, exact-key lock path beside ``state.sqlite``."""

    workspace = key.workspace_id.encode("utf-8")
    action = key.action_id.encode("utf-8")
    digest = hashlib.sha256(
        len(workspace).to_bytes(8, "big")
        + workspace
        + len(action).to_bytes(8, "big")
        + action
    ).hexdigest()
    directory = Path(state_path).with_name(Path(state_path).name + _LOCK_DIRECTORY_SUFFIX)
    return directory / f"{digest}.lock"


def _open_lock(path: Path) -> int:
    fd = -1
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReplacementTransactionActionFenceError(
                "replacement action fence is not one regular linked file; fail closed"
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
    """Hold the exact action's cross-process effect fence until the context exits.

    Blocking is deliberate and narrow: a same-action completion waits for its bounded transport
    to finish, while another action uses a different inode and proceeds.  Same-thread re-entry is
    rejected instead of self-deadlocking; current continuation transports are read-only with
    respect to their replacement action.
    """

    path = replacement_transaction_action_lock_path(state_path, key)
    identity = str(path)
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
