"""Home-global reader/writer gate for conforming Herdr session starts (#15227)."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import weakref
from contextlib import contextmanager
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import normalize_path_unicode


HERDR_SESSION_START_GATE_DIRECTORY = ".mozyo-bridge-session-start-gates"
HERDR_SESSION_START_GATE_SUFFIX = ".lock"
HERDR_SESSION_START_GATE_ANCHOR = Path("/tmp")
_MINT_TOKEN = object()
_ACTIVE: "weakref.WeakSet[SessionStartGateLease]" = weakref.WeakSet()


class SessionStartGateError(RuntimeError):
    """The home-global start gate is busy, unsafe, or unusable."""


@dataclass(frozen=True, eq=False, repr=False)
class SessionStartGateLease:
    home: Path
    home_identity: tuple[int, int] = field(repr=False)
    exclusive: bool
    minter_pid: int
    common_anchor: Path = field(repr=False)
    common_anchor_identity: tuple[int, int] = field(repr=False)
    gate_root: Path = field(repr=False)
    lock_paths: tuple[Path, Path] = field(repr=False)
    _dir_fd: int = field(repr=False)
    _fds: tuple[int, int] = field(repr=False)
    _mint_token: InitVar[object] = None

    def __post_init__(self, _mint_token: object) -> None:
        if _mint_token is not _MINT_TOKEN:
            raise SessionStartGateError("session_start_gate_lease_not_minted")


def _canonical_home(home: Path, *, create: bool = False) -> Path:
    """Resolve one owned, non-world-writable home; optionally bootstrap it."""

    path = Path(home).expanduser().absolute()
    if create:
        try:
            missing = []
            cursor = path
            while not cursor.exists():
                missing.append(cursor)
                if cursor == cursor.parent:
                    break
                cursor = cursor.parent
            for directory in reversed(missing):
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
        except OSError as exc:
            raise SessionStartGateError(
                "session_start_gate_home_unavailable"
            ) from exc
    try:
        named = path.lstat()
        resolved = path.resolve(strict=True)
        opened = resolved.stat()
    except OSError as exc:
        raise SessionStartGateError("session_start_gate_home_unavailable") from exc
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        or stat.S_IMODE(opened.st_mode) & 0o002
        or not _entry_is_stable(resolved)
        or (
            hasattr(os, "geteuid")
            and (named.st_uid != os.geteuid() or opened.st_uid != os.geteuid())
        )
    ):
        raise SessionStartGateError("session_start_gate_home_unsafe")
    return resolved


def _entry_is_stable(path: Path) -> bool:
    """Whether another UID cannot rename this directory entry from its parent."""

    parent = path.parent
    if parent == path:
        return False
    try:
        entry = path.lstat()
        holder = parent.stat()
    except OSError:
        return False
    holder_writable = stat.S_IMODE(holder.st_mode) & 0o022
    sticky_holder = bool(holder.st_mode & stat.S_ISVTX) and holder.st_uid in {
        0,
        os.geteuid(),
    }
    return bool(
        stat.S_ISDIR(entry.st_mode)
        and entry.st_uid == os.geteuid()
        and holder.st_uid in {0, os.geteuid()}
        and (not holder_writable or sticky_holder)
    )


def _unmapped_overflow_uid() -> Optional[int]:
    """The kernel overflow uid, IF no subject in this user namespace can hold it.

    A directory owner is a *subject* only when some process can run with that uid. Inside
    a user namespace, a uid that is not covered by ``/proc/self/uid_map`` cannot be held by
    any process in the namespace — files owned by an unmapped uid are reported as the kernel
    overflow uid and are unwritable-by-owner for every local subject. In the init namespace
    the identity map covers every uid, so this returns ``None`` there and the strict owner
    rule stays in force on real hosts: the relaxation can never apply outside a sandbox.

    ``None`` on any read/parse failure — unknown must stay strict, never permissive.
    """
    try:
        overflow = int(Path("/proc/sys/kernel/overflowuid").read_text().strip())
        mapped: list[tuple[int, int]] = []
        for line in Path("/proc/self/uid_map").read_text().splitlines():
            inside, _outside, count = (int(tok) for tok in line.split())
            mapped.append((inside, count))
    except (OSError, ValueError):
        return None
    if any(inside <= overflow < inside + count for inside, count in mapped):
        return None
    return overflow


def _common_anchor_matches(path: Path, identity: tuple[int, int]) -> bool:
    """Validate the fixed cross-alias namespace without creating it.

    The anchor's parent must not be swappable underneath us, or two processes could resolve
    different anchors and hold different locks. Swapping a directory entry requires write
    permission on the parent, so the parent is trusted when its owner is a trusted subject
    (root / us) — or when its owner is **not a subject at all**: under the official test
    runner's sandbox ``/`` is a namespace-private tmpfs owned by an unmapped uid (shown as
    the overflow uid), which no process in the namespace can hold, so with group/other write
    bits clear nobody can rename ``/tmp`` there. That case is accepted only when
    :func:`_unmapped_overflow_uid` proves the owner is unmappable; on a real host the init
    namespace maps every uid and the strict owner rule applies unchanged (#15227 R-correction:
    the gate must hold under the canonical runner, and weakening the host rule to get there
    would be the invented-premise failure mode all over again).
    """

    try:
        opened = path.stat()
        named = path.lstat()
        parent = path.parent.stat()
    except OSError:
        return False
    owners = {0, os.geteuid()}
    mode = stat.S_IMODE(opened.st_mode)
    writable = bool(mode & 0o022)
    parent_writable = bool(stat.S_IMODE(parent.st_mode) & 0o022)
    parent_sticky = bool(parent.st_mode & stat.S_ISVTX)
    parent_owner_trusted = parent.st_uid in owners or (
        not parent_writable and parent.st_uid == _unmapped_overflow_uid()
    )
    return bool(
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(named.st_mode)
        and (opened.st_dev, opened.st_ino) == identity
        and (named.st_dev, named.st_ino) == identity
        and opened.st_uid == named.st_uid
        and opened.st_uid in owners
        and parent_owner_trusted
        and (not writable or bool(opened.st_mode & stat.S_ISVTX))
        and (
            not parent_writable
            or (parent_sticky and parent.st_uid in owners)
        )
    )


def _common_gate_anchor() -> tuple[Path, tuple[int, int]]:
    try:
        anchor = HERDR_SESSION_START_GATE_ANCHOR.resolve(strict=True)
        observed = anchor.stat()
    except OSError as exc:
        raise SessionStartGateError(
            "session_start_gate_anchor_unavailable"
        ) from exc
    identity = (observed.st_dev, observed.st_ino)
    if not _common_anchor_matches(anchor, identity):
        raise SessionStartGateError("session_start_gate_anchor_unsafe")
    return anchor, identity


def _gate_root_matches(fd: int, path: Path) -> bool:
    try:
        opened = os.fstat(fd)
        named = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(named.st_mode)
        and opened.st_nlink > 0
        and opened.st_nlink == named.st_nlink
        and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        and stat.S_IMODE(opened.st_mode) == 0o700
        and stat.S_IMODE(named.st_mode) == 0o700
        and (
            not hasattr(os, "geteuid")
            or (opened.st_uid == named.st_uid == os.geteuid())
        )
    )


def _home_matches(path: Path, identity: tuple[int, int]) -> bool:
    try:
        named = path.lstat()
        resolved = path.resolve(strict=True)
        observed = resolved.stat()
    except OSError:
        return False
    return bool(
        not stat.S_ISLNK(named.st_mode)
        and stat.S_ISDIR(named.st_mode)
        and stat.S_ISDIR(observed.st_mode)
        and (named.st_dev, named.st_ino) == (observed.st_dev, observed.st_ino)
        and (observed.st_dev, observed.st_ino) == identity
        and observed.st_uid == os.geteuid()
        and not stat.S_IMODE(observed.st_mode) & 0o002
    )


def _artifact_matches(fd: int, dir_fd: int, name: str) -> bool:
    try:
        opened = os.fstat(fd)
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(named.st_mode)
        and opened.st_nlink == 1
        and named.st_nlink == 1
        and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        and stat.S_IMODE(opened.st_mode) == 0o600
        and stat.S_IMODE(named.st_mode) == 0o600
        and (
            not hasattr(os, "geteuid")
            or (opened.st_uid == named.st_uid == os.geteuid())
        )
    )


def _gate_lock_names(
    home: Path, identity: tuple[int, int]
) -> tuple[str, str]:
    """Return path- and inode-scoped keys in one deterministic order."""

    path_key = normalize_path_unicode(str(home).casefold())
    path_digest = hashlib.sha256(path_key.encode("utf-8")).hexdigest()
    inode_digest = hashlib.sha256(
        f"{identity[0]}:{identity[1]}".encode("ascii")
    ).hexdigest()
    names = sorted(
        (
            f"path-{path_digest}{HERDR_SESSION_START_GATE_SUFFIX}",
            f"inode-{inode_digest}{HERDR_SESSION_START_GATE_SUFFIX}",
        ),
        key=lambda value: value.encode("ascii"),
    )
    return names[0], names[1]


def _open_gate_root() -> tuple[Path, tuple[int, int], int]:
    anchor, anchor_identity = _common_gate_anchor()
    directory_name = f"{HERDR_SESSION_START_GATE_DIRECTORY}-{os.geteuid()}"
    directory = anchor / directory_name
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    anchor_fd = -1
    directory_fd = -1
    try:
        anchor_fd = os.open(anchor, flags)
        observed = os.fstat(anchor_fd)
        named = anchor.lstat()
        if (
            not stat.S_ISDIR(observed.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (observed.st_dev, observed.st_ino)
            != (named.st_dev, named.st_ino)
            or (observed.st_dev, observed.st_ino) != anchor_identity
            or not _common_anchor_matches(anchor, anchor_identity)
        ):
            raise SessionStartGateError("session_start_gate_anchor_unsafe")
        try:
            os.mkdir(
                directory_name,
                0o700,
                dir_fd=anchor_fd,
            )
        except FileExistsError:
            pass
        directory_fd = os.open(
            directory_name,
            flags,
            dir_fd=anchor_fd,
        )
        if not _gate_root_matches(directory_fd, directory):
            raise SessionStartGateError("session_start_gate_directory_unsafe")
        return directory, anchor_identity, directory_fd
    except SessionStartGateError:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise
    except OSError as exc:
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise SessionStartGateError("session_start_gate_unavailable") from exc
    finally:
        if anchor_fd >= 0:
            try:
                os.close(anchor_fd)
            except OSError:
                pass


def acquire_session_start_gate(
    home: Path, *, exclusive: bool
) -> SessionStartGateLease:
    """Take SH/EX without waiting and mint an opaque same-process lease."""

    root = _canonical_home(home, create=True)
    observed_home = root.stat()
    home_identity = (observed_home.st_dev, observed_home.st_ino)
    directory_fd = -1
    fds: list[int] = []
    directory, anchor_identity, directory_fd = _open_gate_root()
    names = _gate_lock_names(root, home_identity)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        for name in names:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
            fds.append(fd)
            if not _artifact_matches(fd, directory_fd, name):
                raise SessionStartGateError(
                    "session_start_gate_artifact_unsafe"
                )
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        for fd in fds:
            os.utime(fd, None)
        os.utime(directory_fd, None)
        if (
            not _home_matches(root, home_identity)
            or not _common_anchor_matches(directory.parent, anchor_identity)
            or not _gate_root_matches(directory_fd, directory)
            or any(
                not _artifact_matches(fd, directory_fd, name)
                for fd, name in zip(fds, names)
            )
        ):
            raise SessionStartGateError("session_start_gate_artifact_unsafe")
    except BlockingIOError as exc:
        _close_gate_fds(fds, directory_fd)
        raise SessionStartGateError("session_start_gate_busy") from exc
    except SessionStartGateError:
        _close_gate_fds(fds, directory_fd)
        raise
    except OSError as exc:
        _close_gate_fds(fds, directory_fd)
        raise SessionStartGateError("session_start_gate_unavailable") from exc
    lease = SessionStartGateLease(
        home=root,
        home_identity=home_identity,
        exclusive=bool(exclusive),
        minter_pid=os.getpid(),
        common_anchor=directory.parent,
        common_anchor_identity=anchor_identity,
        gate_root=directory,
        lock_paths=tuple(directory / name for name in names),
        _dir_fd=directory_fd,
        _fds=(fds[0], fds[1]),
        _mint_token=_MINT_TOKEN,
    )
    _ACTIVE.add(lease)
    return lease


def _close_gate_fds(fds: list[int], directory_fd: int) -> None:
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError:
            pass
    if directory_fd >= 0:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _lease_integrity(lease: SessionStartGateLease) -> bool:
    return bool(
        _home_matches(lease.home, lease.home_identity)
        and _common_anchor_matches(
            lease.common_anchor, lease.common_anchor_identity
        )
        and _gate_root_matches(lease._dir_fd, lease.gate_root)
        and len(lease.lock_paths) == len(lease._fds) == 2
        and all(
            _artifact_matches(fd, lease._dir_fd, path.name)
            for fd, path in zip(lease._fds, lease.lock_paths)
        )
    )


def require_session_start_gate(
    lease: object, *, home: Path, exclusive: bool
) -> SessionStartGateLease:
    root = _canonical_home(home)
    observed = root.stat()
    identity = (observed.st_dev, observed.st_ino)
    if (
        not isinstance(lease, SessionStartGateLease)
        or lease not in _ACTIVE
        or lease.minter_pid != os.getpid()
        or identity != lease.home_identity
        or not _home_matches(root, lease.home_identity)
        or (exclusive and not lease.exclusive)
        or not _lease_integrity(lease)
    ):
        raise SessionStartGateError("session_start_gate_lease_invalid")
    return lease


def release_session_start_gate(lease: object) -> None:
    if (
        not isinstance(lease, SessionStartGateLease)
        or lease not in _ACTIVE
        or lease.minter_pid != os.getpid()
    ):
        raise SessionStartGateError("session_start_gate_lease_invalid")
    failed = not _lease_integrity(lease)
    for fd in lease._fds:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            failed = True
    for fd in lease._fds:
        try:
            os.close(fd)
        except OSError:
            failed = True
    try:
        os.close(lease._dir_fd)
    except OSError:
        failed = True
    finally:
        _ACTIVE.discard(lease)
    if failed:
        raise SessionStartGateError("session_start_gate_release_unverified")


@contextmanager
def session_start_gate(home: Path, *, exclusive: bool):
    lease = acquire_session_start_gate(home, exclusive=exclusive)
    try:
        yield lease
    finally:
        release_session_start_gate(lease)


__all__ = (
    "HERDR_SESSION_START_GATE_DIRECTORY",
    "HERDR_SESSION_START_GATE_SUFFIX",
    "SessionStartGateError",
    "SessionStartGateLease",
    "acquire_session_start_gate",
    "release_session_start_gate",
    "require_session_start_gate",
    "session_start_gate",
)
