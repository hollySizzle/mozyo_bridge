"""Workspace-scoped single-flight fence for the shared sublane tab (Redmine #14567).

``shared_tab`` topology (Redmine #14567, Design Answer j#91144 Decision 3) puts every
non-default lane of a project into ONE shared herdr tab, which each lane idempotently
adopts. Without a fence, "exactly one" does not hold on a clean slate:

- **concurrent clean-slate lane launches**: two lanes starting at once each read "no tab
  carries the shared label" and each create one — two shared tabs. The live agent
  inventory cannot close this window either, because a tab that has been created but
  whose first ``agent start`` has not landed yet contributes **no** ``agent list`` row;
  an inventory-only resolver is blind to exactly the tab the peer just made.

This module is the cross-process single-flight that create needs: an exclusive, blocking
advisory lock the ``tab list -> resolve -> create`` runs under, so only one process is in
that critical section. A concurrent launch waits, then re-reads the labels **under the
lock** (double-checked) and adopts the tab the first process created.

Scope: **one lock per mozyo workspace**, not one per home. The shared tab is a per-project
container (it lives in that project's sublane host workspace), so serialising every
project's lane creation behind a single home-global lock would be a broader mutual
exclusion than the invariant needs. It also keeps the promise made in the owner
clarification: the critical section covers the tab create/adopt only — never an
``agent start``, never a split — so nothing about lane *launch* is globally serialised.

It reuses the ``fcntl.flock`` protocol of
:func:`...coordinator_placement_fence.coordinator_shared_create_lock` (a holder's crash
releases the lock at the OS level, so no stale lock wedges a launch) on its own lock file,
so it never contends with the coordinator fence or the attestation-store lock a launch
already holds.

The lock file is an operator-private (0600) advisory artifact under the mozyo-bridge home;
it holds no state (it is only ``flock``-ed, never read/written).
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

#: Filename prefix of the per-workspace shared-tab advisory lock. The mozyo workspace id
#: is appended, so two projects never contend and one project's concurrent lane launches
#: always do.
SUBLANE_TAB_CREATE_LOCK_PREFIX = "sublane-shared-tab-create-"

#: Filename suffix of the lock file.
SUBLANE_TAB_CREATE_LOCK_SUFFIX = ".lock"

#: The shape a workspace id must have to be spliced into a filename. Deliberately a strict
#: allowlist rather than an escape/sanitise step: a value that is not this shape is not a
#: workspace id this build minted, and quietly rewriting it into "some safe filename" would
#: let two different ids collapse onto one lock (mutual exclusion silently wrong) or let a
#: separator escape the home directory. Real ids are the 32-hex ``workspace_id``.
_SAFE_WORKSPACE_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


class SublaneTabCreateLockUnavailable(RuntimeError):
    """The single-flight fence cannot be honored, so no tab may be created.

    Raised rather than proceeding unlocked — a silent no-op would advertise a
    single-flight guarantee that is not there (mirrors
    :class:`...coordinator_placement_fence.CoordinatorSharedCreateLockUnavailable`).

    Used for the **acquisition** phase (before any herdr command runs), so a caller that
    catches only this base type can still treat it as a zero-actuation failure. The
    **release** phase raises the :class:`SublaneTabCreateReleaseError` subtype so the caller
    can tell the two phases apart.
    """


class SublaneTabCreateReleaseError(SublaneTabCreateLockUnavailable):
    """The lock could not be RELEASED after the guarded body already succeeded.

    A subtype of :class:`SublaneTabCreateLockUnavailable` (so one ``except`` still catches
    every fence failure), but distinct so the caller can report the truth: the release runs
    AFTER the body, so on the clean-slate path the shared ``tab create`` has already
    happened — the "no tab was created" message that fits an acquisition failure is FALSE
    here. A re-run adopts the (possibly labelled, slot-less) tab idempotently.
    """


def sublane_tab_create_lock_path(
    workspace_id: str, *, home: Optional[Path] = None
) -> Path:
    """Absolute path of the ``workspace_id`` shared-tab advisory lock file under ``home``.

    Raises :class:`SublaneTabCreateLockUnavailable` for a workspace id that is not
    :data:`_SAFE_WORKSPACE_ID`-shaped, so a blank / separator-bearing / oversized id can
    never address a file outside the home or share a lock with a different workspace.
    """
    if not isinstance(workspace_id, str) or not _SAFE_WORKSPACE_ID.match(workspace_id):
        raise SublaneTabCreateLockUnavailable(
            f"workspace id {workspace_id!r} is not a lock-nameable identity "
            "(expected 1-64 chars of [A-Za-z0-9_-]); refusing to derive a shared-tab "
            "lock file from it"
        )
    base = home or mozyo_bridge_home()
    filename = (
        f"{SUBLANE_TAB_CREATE_LOCK_PREFIX}{workspace_id}{SUBLANE_TAB_CREATE_LOCK_SUFFIX}"
    )
    return Path(base) / filename


@contextmanager
def sublane_tab_create_lock(workspace_id: str, *, home: Optional[Path] = None):
    """Hold ``workspace_id``'s shared-tab advisory lock (exclusive, blocking).

    Serialises the shared tab's ``tab list -> resolve -> create`` so concurrent
    clean-slate lane launches in the SAME mozyo workspace converge to ONE tab: the first
    process creates it under the lock; the rest wait, then re-read the labels under the
    lock and adopt it. Blocking (not fail-closed) because the critical section is short
    (one ``tab list`` + at most one ``tab create``) and a normal concurrent lane launch
    should wait, not error.

    Every acquisition failure — ``fcntl`` unavailable, an unnameable workspace id, the lock
    file being unmakeable / unopenable (permission, a directory in its place), or a
    ``flock`` error — is raised as :class:`SublaneTabCreateLockUnavailable` rather than a
    raw ``OSError``, so the caller converts exactly one type into its typed error boundary.
    This holds for the WHOLE lock lifecycle:

    - **acquisition** (``mkdir`` / ``open`` / ``flock LOCK_EX``): a failure closes any
      opened fd quietly (a secondary close error must not mask the acquire error) and
      raises :class:`SublaneTabCreateLockUnavailable`;
    - **release** (``flock LOCK_UN`` and ``os.close``): BOTH are always attempted and the
      fd is always closed, so no fd leaks. A release failure is surfaced as
      :class:`SublaneTabCreateReleaseError` **only when the guarded body succeeded** — a
      body exception is the real fault and is propagated UNCHANGED, never overwritten by a
      secondary unlock/close error.
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - POSIX-only platforms in practice
        raise SublaneTabCreateLockUnavailable(
            "advisory file locking (fcntl.flock) is unavailable on this platform, so the "
            "shared sublane tab single-flight fence cannot be honored; refusing to create "
            "the shared tab unlocked"
        ) from exc

    path = sublane_tab_create_lock_path(workspace_id, home=home)
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        _close_fd_quietly(fd)
        raise SublaneTabCreateLockUnavailable(
            f"could not acquire the shared sublane tab single-flight lock at {path}: {exc}"
        ) from exc

    body_failed = False
    try:
        yield
    except BaseException:
        # The body's own fail-closed error (or any exception) is the real fault; a
        # secondary release error below must not overwrite it.
        body_failed = True
        raise
    finally:
        release_error = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as unlock_exc:  # pragma: no cover - unlock rarely errors
            release_error = unlock_exc
        try:
            os.close(fd)
        except OSError as close_exc:  # pragma: no cover - close rarely errors
            release_error = release_error or close_exc
        if release_error is not None and not body_failed:
            raise SublaneTabCreateReleaseError(
                "could not release the shared sublane tab single-flight lock "
                f"({release_error}); fail closed"
            ) from release_error


def _close_fd_quietly(fd: Optional[int]) -> None:
    """Close ``fd`` swallowing any error, so a cleanup close never masks a real one."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


__all__ = (
    "SUBLANE_TAB_CREATE_LOCK_PREFIX",
    "SUBLANE_TAB_CREATE_LOCK_SUFFIX",
    "SublaneTabCreateLockUnavailable",
    "SublaneTabCreateReleaseError",
    "sublane_tab_create_lock",
    "sublane_tab_create_lock_path",
)
