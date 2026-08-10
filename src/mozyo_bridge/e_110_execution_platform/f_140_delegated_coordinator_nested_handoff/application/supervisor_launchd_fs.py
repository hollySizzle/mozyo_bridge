"""The owned-path filesystem seam for the macOS supervisor adapter (Redmine #15192).

This module — and only this module — reads and writes the files the launchd adapter owns. It is
named for what it does: it **mutates the host**. Its siblings do not, and previously said so while
one of them held the writer (review j#102590 r14f4).

**Every component of the path is checked, not just the last one** (review j#102590 r14f1). The
adapter used to establish identity with ``lstat`` on the plist and open it with ``O_NOFOLLOW``, which
sounds sufficient and is not: both apply to the **final** component only. Replacing an *ancestor* —
making ``~/Library/LaunchAgents`` a symlink to somewhere else — left every leaf check intact and
still put the write, the read, and the unlink in a directory this adapter does not own. Classifying
the leaf harder cannot fix that, because the leaf is reached through the ancestors.

So the trusted root is opened first and each component below it is opened **relative to the previous
one** with ``O_NOFOLLOW | O_DIRECTORY``. What comes back is a directory file descriptor, and every
later operation — ``mkdir``, ``open``, ``stat``, ``unlink``, ``rename`` — is performed relative to
*that descriptor* rather than re-walking a string. A descriptor refers to the directory it was opened
on; swapping the name afterwards cannot redirect it. A symlink anywhere along the chain is refused
before anything is touched.

The trusted root itself (``os_home``) is taken on faith, as it must be: it is the caller's own
``Path.home()`` or an explicit test root, and there is no deeper anchor to check it against.

**Writes never truncate in place** (review j#102590 r14f2). Opening with ``O_TRUNC`` destroys the
file before the descriptor can be examined, so a plist swapped for a hard link to someone else's file
between the check and the open had that file's contents overwritten — through a fence that had just
refused hard links. And a partial ``os.write`` left a truncated plist while reporting success. The
payload is therefore staged under a temporary name in the same pinned directory, written in full,
and moved into place with ``os.replace``. Renaming swaps the *name*: if the destination were a hard
link to a stranger's file, that file keeps its contents and only loses one of its names. A failure
anywhere before the rename leaves the existing plist exactly as it was.

**Ownership and content come from one descriptor** (review j#102590 r14f3). :func:`read_owned` opens
once, verifies the descriptor, and reads through it, returning the classification together with the
bytes it classified. Callers that publish plist contents use that pair, so what was judged and what
is shown cannot be two different files.
"""

from __future__ import annotations

import errno
import os
import plistlib
import stat
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_agent import (  # noqa: E501
    SUPERVISOR_AGENT,
    SupervisorAgent,
)

#: Identity of whatever occupies an agent's plist path. Only :data:`PLIST_OWNED` may be mutated.
PLIST_ABSENT = "absent"  # nothing there: a clean host, or one already torn down
PLIST_OWNED = "owned"  # a regular, singly-linked file whose ``Label`` is exactly this agent's
PLIST_FOREIGN = "foreign"  # parses, but the ``Label`` belongs to someone else
#: Present but not identifiable as ours: unparseable, non-mapping, no ``Label``, a symlink, a
#: directory or device, or a file reachable under more than one name. "I cannot tell whose this is"
#: covers all of them, and none of them authorizes a mutation.
PLIST_UNREADABLE = "unreadable"

#: Suffix for the staging file a write is assembled in before it is renamed into place. It lives in
#: the same pinned directory so the rename is within one filesystem and therefore atomic.
_STAGING_SUFFIX = ".mozyo-staging"


class OwnedPathError(OSError):
    """A component of the owned path is not what it must be (a symlink, missing, unopenable).

    An ``OSError`` subclass so callers that already treat filesystem trouble as a typed refusal need
    no new except clause; distinct so a path-integrity failure can be told from an ordinary one.
    """


def open_owned_dir(os_home: Optional[Path], relative: Path, *, create: bool = False) -> int:
    """Open ``relative``'s directory under ``os_home``, refusing a symlink at **any** component.

    Returns a directory file descriptor the caller must close. Every operation on the owned plist
    goes through it: a descriptor is bound to the directory it was opened on, so an ancestor renamed
    or relinked afterwards cannot redirect what follows.

    ``create`` makes missing components, one level at a time and always relative to the descriptor
    already verified — never ``mkdir(parents=True)``, which walks a string and follows whatever it
    finds. A component that exists but is a symlink raises :class:`OwnedPathError` rather than being
    created over or followed.
    """
    root = Path(os_home) if os_home is not None else Path.home()
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:  # the trusted root itself is unusable
        raise OwnedPathError(exc.errno, "owned path root unavailable") from exc
    # `relative` is the plist's path relative to the root; its parents are the chain to walk.
    # `_descend` always closes the descriptor it was handed, on success and on failure alike, so no
    # descriptor leaks and none is closed twice however the walk ends.
    for part in relative.parent.parts:
        fd = _descend(fd, part, create=create)
    return fd


def _descend(parent_fd: int, name: str, *, create: bool) -> int:
    """Open ``name`` under ``parent_fd`` as a directory, no-follow. Always closes ``parent_fd``."""
    try:
        try:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
        except FileNotFoundError:
            if not create:
                raise OwnedPathError(errno.ENOENT, "owned path component missing")
            os.mkdir(name, 0o755, dir_fd=parent_fd)
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
        except OSError as exc:
            # ELOOP: the component is a symlink. ENOTDIR: it is a file. Either way the owned path
            # does not exist as an owned path, and nothing below it may be touched.
            raise OwnedPathError(exc.errno, "owned path component is not a directory") from exc
    finally:
        os.close(parent_fd)
    return child


def classify_fd(fd: int, payload: bytes, *, label: str) -> str:
    """Classify an already-opened plist descriptor plus the bytes read **from that descriptor**.

    Identity is the plist's own ``Label``: a path is a location, and a location says nothing about
    who wrote what is there. The descriptor is checked as well as the content — a regular file
    reachable under exactly one name — because a mutation through a hard link reaches a file this
    adapter never accounted for.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return PLIST_UNREADABLE
    try:
        parsed = plistlib.loads(payload)
    except (ValueError, plistlib.InvalidFileException):
        return PLIST_UNREADABLE
    if not isinstance(parsed, dict):
        return PLIST_UNREADABLE
    found = parsed.get("Label")
    if not isinstance(found, str) or not found:
        return PLIST_UNREADABLE
    return PLIST_OWNED if found == label else PLIST_FOREIGN


def read_owned(
    os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT
) -> tuple[str, bytes]:
    """``(state, payload)`` for ``agent``'s plist — classified and read through ONE descriptor.

    Returning both is the point (review j#102590 r14f3). Classifying by path and then re-opening it
    to read judged one file and published another: a plist swapped between the two calls was
    reported ``owned`` while a stranger's arguments went out in the projection. Callers that show
    plist contents must use the bytes returned here, not re-read the path.

    ``payload`` is empty whenever ``state`` is not :data:`PLIST_OWNED`, so a caller cannot
    accidentally publish something unidentified.
    """
    name = agent.plist_relative.name
    try:
        dir_fd = open_owned_dir(os_home, agent.plist_relative)
    except OwnedPathError as exc:
        # A directory that does not exist cannot contain a plist, so this really is absence — the
        # ordinary state of a host that has never installed. Anything else (a symlinked component, a
        # file where a directory belongs, a directory we may not open) established nothing about
        # what is there, which is not the same as establishing that nothing is.
        return (PLIST_ABSENT if exc.errno == errno.ENOENT else PLIST_UNREADABLE), b""
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            return PLIST_ABSENT, b""
        except OSError:
            # ELOOP (a symlink), EACCES, ENOTDIR — present in some form we cannot identify.
            return PLIST_UNREADABLE, b""
        try:
            payload = _read_all(fd)
            state = classify_fd(fd, payload, label=agent.label)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    return (state, payload) if state == PLIST_OWNED else (state, b"")


def classify(os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT) -> str:
    """The classification alone, for callers that do not need the bytes."""
    return read_owned(os_home, agent=agent)[0]


def write_owned(
    payload: bytes, os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT
) -> None:
    """Put ``payload`` at ``agent``'s plist path: staged in full, then renamed into place.

    Never truncates the destination (review j#102590 r14f2). The bytes are written to a temporary
    name in the same pinned directory, checked complete, and moved over the target with
    ``os.replace``. Two things follow, and both were broken before:

    - a hard link at the destination loses a *name*, not its contents, so a swap cannot make this
      write reach into a file the adapter never owned;
    - a short write or an ``ENOSPC`` fails before the rename, leaving the previous plist intact
      instead of replacing it with a truncated one.

    Raises ``OSError`` — including :class:`OwnedPathError` for a symlinked ancestor — which callers
    turn into a typed refusal.
    """
    name = agent.plist_relative.name
    staging = f"{name}{_STAGING_SUFFIX}"
    dir_fd = open_owned_dir(os_home, agent.plist_relative, create=True)
    try:
        # O_EXCL: never write into a staging file someone else left or planted.
        try:
            os.unlink(staging, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        fd = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd
        )
        try:
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            # Anything short of a completed rename leaves the previous plist untouched; clear the
            # half-written staging file so a later run does not have to reason about it.
            _discard(staging, dir_fd)
            raise
    finally:
        os.close(dir_fd)


def unlink_owned(os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT) -> None:
    """Remove ``agent``'s plist through the pinned directory. Raises ``OSError`` on failure."""
    dir_fd = open_owned_dir(os_home, agent.plist_relative)
    try:
        os.unlink(agent.plist_relative.name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def ensure_log_dir(os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT) -> None:
    """Create the owned log directory, one no-follow component at a time. Raises ``OSError``."""
    os.close(open_owned_dir(os_home, agent.log_relative, create=True))


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte or raise. ``os.write`` may write fewer bytes than it was given."""
    written = 0
    while written < len(payload):
        count = os.write(fd, payload[written:])
        if count <= 0:
            raise OSError(errno.EIO, "short write to the owned plist")
        written += count


def _discard(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


__all__ = (
    "OwnedPathError",
    "PLIST_ABSENT",
    "PLIST_OWNED",
    "PLIST_FOREIGN",
    "PLIST_UNREADABLE",
    "open_owned_dir",
    "classify_fd",
    "read_owned",
    "classify",
    "write_owned",
    "unlink_owned",
    "ensure_log_dir",
)
