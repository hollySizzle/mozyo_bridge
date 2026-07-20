"""Operator-home single-flight fence for the shared coordinators space (Redmine #14139).

``shared_space`` placement (Redmine #14139) creates ONE stable shared coordinators
herdr workspace and every project idempotently adopts it. Two failure modes break
that "exactly one" without a fence (R5 full-surface review j#83516):

- **concurrent clean-slate launches**: managed-launch admission holds the
  attestation store lock only **shared** (many launches run at once), so two
  projects that both start on a clean slate each read "no shared workspace" and each
  create one — two shared spaces.
- (the partial-failure husk is handled separately, by adopting a labelled but
  slot-less workspace in the resolver — see
  ``herdr_lane_topology._shared_coordinator_target``.)

This module is the cross-process single-flight the create needs: a home-scoped
**exclusive, blocking** advisory lock the shared-space list→resolve→create runs
under, so only one process is ever in that critical section. A concurrent launch
waits, then re-reads the labels under the lock and adopts the workspace the first
process created (double-checked). It reuses the exact ``fcntl.flock`` protocol of
:func:`...herdr_identity_attestation_schema.attestation_store_lock` — a holder's
crash releases the lock at the OS level, so no stale lock wedges a launch — but on a
**separate lock file** so it never contends with the attestation-store lock the
launch already holds shared (acquiring the same lock exclusive under our own shared
hold would deadlock).

The lock file is an operator-private (0600) advisory artifact under the mozyo-bridge
home; it is NOT the operator placement config and holds no state (it is only
``flock``-ed, never read/written), so it stays inside the "no operator-home config
write" boundary.
"""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-relative advisory lock file serialising the shared coordinators space
#: create. Distinct from the attestation-store lock so the two never contend.
COORDINATOR_SHARED_CREATE_LOCK_FILENAME = "coordinator-shared-create.lock"


class CoordinatorSharedCreateLockUnavailable(RuntimeError):
    """``fcntl.flock`` is unavailable, so the single-flight fence cannot be honored.

    Raised rather than proceeding unlocked — a silent no-op would advertise a
    single-flight guarantee that is not there (mirrors
    :class:`...herdr_identity_attestation_schema.AttestationStoreLockUnavailable`).
    """


def coordinator_shared_create_lock_path(home: Optional[Path] = None) -> Path:
    """Absolute path of the shared-create advisory lock file under ``home``."""
    base = (home or mozyo_bridge_home())
    return Path(base) / COORDINATOR_SHARED_CREATE_LOCK_FILENAME


@contextmanager
def coordinator_shared_create_lock(home: Path):
    """Hold the home's shared-coordinators-create advisory lock (exclusive, blocking).

    Serialises the shared-space list→resolve→create so concurrent clean-slate
    launches converge to ONE workspace: the first process creates it under the lock;
    the rest wait, then re-read the labels under the lock and adopt it. Blocking (not
    fail-closed) because the critical section is short (one ``workspace list`` + one
    ``workspace create``) and a normal concurrent ``mozyo`` launch should wait, not
    error. A holder's crash releases the lock at the OS level.

    Every acquisition failure — ``fcntl`` unavailable, the home lock file being
    unmakeable / unopenable (permission, a directory in its place), or a ``flock``
    error — is raised as :class:`CoordinatorSharedCreateLockUnavailable` rather than
    a raw ``OSError``, so the single caller can convert exactly one type into the
    session-start typed error boundary (R6 review j#83569 F2). Exceptions raised
    inside the guarded body are propagated unchanged (they are the launch's own
    fail-closed errors, not the fence's).
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - POSIX-only platforms in practice
        raise CoordinatorSharedCreateLockUnavailable(
            "advisory file locking (fcntl.flock) is unavailable on this platform, so "
            "the shared coordinators single-flight fence cannot be honored; refusing to "
            "create the shared space unlocked"
        ) from exc

    path = coordinator_shared_create_lock_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise CoordinatorSharedCreateLockUnavailable(
            f"could not open the shared coordinators single-flight lock at {path}: {exc}"
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:  # pragma: no cover - blocking flock rarely errors here
            raise CoordinatorSharedCreateLockUnavailable(
                f"could not acquire the shared coordinators single-flight lock: {exc}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = (
    "COORDINATOR_SHARED_CREATE_LOCK_FILENAME",
    "CoordinatorSharedCreateLockUnavailable",
    "coordinator_shared_create_lock",
    "coordinator_shared_create_lock_path",
)
