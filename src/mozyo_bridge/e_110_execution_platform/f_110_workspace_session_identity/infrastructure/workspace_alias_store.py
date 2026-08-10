"""Workspace-local nested-workspace alias declaration store (#15190).

The declaration survives home-registry recovery and stays separate from the
identity anchor. Repository-controlled paths are hostile: operations pin a
nofollow directory fd, distinguish absence from unreadability, verify visible
inode identity, and roll back failed mutations. Reads are bounded/nonblocking
and hold ``LOCK_SH | LOCK_NB`` through parsing so a writer's temporary absence
is a typed refusal. Full rationale: ``vibes/docs/logics/workspace-registry.md``.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure import (  # noqa: E501
    workspace_alias_lock,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    ALIAS_RELATIVE,
    ALIAS_SCHEMA_VERSION,
    MAX_DECLARATION_BYTES,
    REASON_CONCURRENT_CHANGE,
    REASON_DECLARATION_MUTATION_IN_PROGRESS,
    REASON_DECLARATION_UNREADABLE,
    REASON_DURABILITY_FAILED,
    REASON_LOCK_FAILED,
    REASON_MULTIPLE_LINKS,
    REASON_NOT_REGULAR_FILE,
    REASON_PARENT_DRIFT,
    REASON_PARENT_UNSAFE,
    REASON_READBACK_FAILED,
    REASON_REMOVE_FAILED,
    REASON_SNAPSHOT_FAILED,
    REASON_TOO_LARGE,
    REASON_WRITE_FAILED,
    AliasResolution,
    WorkspaceAliasDeclaration,
    parse_declaration,
    refused,
)

_ALIAS_PARENT = Path(ALIAS_RELATIVE).parent.as_posix()
_ALIAS_NAME = Path(ALIAS_RELATIVE).name

# Compatibility seams used by existing focused tests and callers in this module.
_LOCK_NAME = workspace_alias_lock.LOCK_NAME
_open_lock_fd = workspace_alias_lock.open_lock_fd
_require_lock_visible = workspace_alias_lock.require_lock_visible
WorkspaceAliasLockError = workspace_alias_lock.WorkspaceAliasLockError

#: Read granularity for the bounded declaration read.
_READ_CHUNK = 8192

#: Outcomes of :func:`clear_declaration`.
CLEAR_REMOVED = "removed"
CLEAR_ABSENT = "absent"


class WorkspaceAliasStoreError(Exception):
    """A mutation refused, carrying a fixed typed reason.

    ``mutated`` is ``False`` when the workspace-visible state is unchanged — the
    normal case, including every refusal that happens after a rollback. It is
    ``True`` only when the rollback itself failed, which the CLI must say out
    loud rather than reporting "nothing was written".
    """

    def __init__(self, reason: str, detail: str, *, mutated: bool = False) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.mutated = mutated


def alias_path(repo_root: Path | str) -> Path:
    """The declaration path for ``repo_root`` (also the write target)."""
    return Path(repo_root) / ALIAS_RELATIVE


def _parent_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / _ALIAS_PARENT


def _fsync_repo_root(repo_root: Path | str) -> None:
    """Persist a NEWLY created ``.mozyo-bridge`` entry in its own parent.

    Syncing the declaration and the ``.mozyo-bridge`` dirfd is not enough when
    that directory itself was just created: the entry naming it lives in the
    repo root, and an unsynced directory creation can be lost on power loss —
    taking the declaration with it (review j#102710 r6f4).
    """
    root = Path(repo_root)
    try:
        fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_DURABILITY_FAILED, f"{root} could not be opened to sync ({exc})"
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_DURABILITY_FAILED,
            f"{root} could not be synced after creating {_ALIAS_PARENT} ({exc})",
        ) from exc
    finally:
        os.close(fd)


def _open_parent(repo_root: Path | str, *, create: bool) -> Optional[int]:
    """A dirfd for ``<repo_root>/.mozyo-bridge``, opened ``O_NOFOLLOW``.

    ``None`` when the directory does not exist and ``create`` is false. Raises
    :class:`WorkspaceAliasStoreError` when the path exists but is not a real
    directory — including when it is a *symlink* to one, which ``O_NOFOLLOW``
    rejects with ``ELOOP``. That refusal is the point: a symlinked
    ``.mozyo-bridge`` would let a repo redirect every declaration write out of
    the workspace.
    """
    parent = _parent_path(repo_root)
    created = False
    if create:
        try:
            created = not parent.exists()
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_PARENT_UNSAFE, f"{parent}: {exc}"
            ) from exc
    if created:
        _fsync_repo_root(repo_root)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(parent, flags)
    except FileNotFoundError:
        if create:
            raise WorkspaceAliasStoreError(
                REASON_PARENT_UNSAFE, f"{parent}: disappeared before it could be used"
            ) from None
        return None
    except NotADirectoryError as exc:
        raise WorkspaceAliasStoreError(
            REASON_PARENT_UNSAFE, f"{parent}: not a directory"
        ) from exc
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_PARENT_UNSAFE,
            f"{parent}: not a usable directory ({exc}); a symlinked "
            f"{_ALIAS_PARENT} is refused",
        ) from exc


def _fd_identity(dirfd: int) -> Tuple[int, int]:
    """``(st_dev, st_ino)`` of the pinned directory."""
    info = os.fstat(dirfd)
    return (info.st_dev, info.st_ino)


def _require_parent_visible(repo_root: Path | str, anchor: Tuple[int, int]) -> None:
    """Assert the root-visible ``.mozyo-bridge`` is still the pinned directory.

    A dirfd keeps working after its directory is renamed or replaced, so without
    this the writer can operate on a directory no longer reachable from the
    workspace root and still report success (j#102140 Finding 1). Checked before
    the replace and again after it, so a drift is caught whichever side of the
    rename it happens on.
    """
    parent = _parent_path(repo_root)
    try:
        info = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_PARENT_DRIFT,
            f"{parent} is no longer readable from the workspace root ({exc})",
        ) from exc
    if (info.st_dev, info.st_ino) != anchor:
        raise WorkspaceAliasStoreError(
            REASON_PARENT_DRIFT,
            f"{parent} was replaced while this operation was in flight; the "
            f"directory it pinned is no longer the one reachable from the "
            f"workspace root, so the result would not be the effective state",
        )


def _lstat_entry(dirfd: int) -> Optional[os.stat_result]:
    """``lstat`` of the declaration entry, or ``None`` when absent.

    Raises ``OSError`` for anything other than absence; callers turn that into a
    typed refusal (reads) or a typed error (writes). Swallowing it here is what
    let a ``PermissionError`` escape ``read_declaration`` raw (j#102140 F3).
    """
    try:
        return os.stat(_ALIAS_NAME, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def declaration_exists(repo_root: Path | str) -> bool:
    """Whether ``repo_root`` carries a declaration entry of any kind.

    Used for the alias-cycle check, which must fail closed on a target whose
    declaration is *present but broken* just as firmly as on a valid one. So this
    reports any existing directory entry — regular file, symlink, directory —
    rather than only a readable declaration, and an unreadable parent counts as
    "present" so the caller refuses instead of aliasing into it.
    """
    # Reuse the coordinated public reader. A refusal is deliberately "present":
    # alias-cycle detection must fail closed on an active mutation, unsafe lock,
    # unreadable declaration, or malformed declaration rather than treating any
    # of those as proof that the target declares nothing.
    return read_declaration(repo_root) is not None


def _read_bytes(dirfd: int) -> Optional[bytes] | AliasResolution:
    """Raw declaration bytes, ``None`` when absent, or a typed refusal.

    The file is opened ``O_NONBLOCK`` and re-checked with ``fstat``: the ``lstat``
    above only describes the entry at that instant, and a regular file swapped
    for a FIFO before the ``open`` would otherwise block the reader forever
    (j#102140 Finding 3). ``fstat`` describes the object actually opened, so the
    check cannot be raced.
    """
    try:
        entry = _lstat_entry(dirfd)
    except OSError as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{ALIAS_RELATIVE}: {exc}")
    if entry is None:
        return None
    if not stat.S_ISREG(entry.st_mode):
        return refused(
            REASON_NOT_REGULAR_FILE,
            f"{ALIAS_RELATIVE} exists but is not a regular file "
            f"(mode {stat.filemode(entry.st_mode)}); refusing to treat it as an "
            f"absent declaration",
        )
    try:
        fd = os.open(
            _ALIAS_NAME,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dirfd,
        )
    except OSError as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{ALIAS_RELATIVE}: {exc}")
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return refused(
                REASON_NOT_REGULAR_FILE,
                f"{ALIAS_RELATIVE} changed type while it was being opened "
                f"(now {stat.filemode(opened.st_mode)}); refusing to read it",
            )
        # Size is checked BEFORE any allocation, and the read is still bounded
        # afterwards (review j#102230 Finding 2). `st_size` is not trustworthy on
        # its own: the file may grow between the fstat and the read, and a sparse
        # file can declare a huge size while occupying nothing — the previous
        # `os.read(fd, st_size + 1)` turned that into one unbounded allocation,
        # which a 512 MiB sparse declaration escalated into a raw `MemoryError`
        # from what is supposed to be a fail-closed reader.
        if opened.st_size > MAX_DECLARATION_BYTES:
            return refused(
                REASON_TOO_LARGE,
                f"{ALIAS_RELATIVE} is {opened.st_size} bytes; the maximum is "
                f"{MAX_DECLARATION_BYTES}",
            )
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_DECLARATION_BYTES:
            chunk = os.read(fd, min(_READ_CHUNK, MAX_DECLARATION_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_DECLARATION_BYTES:
            return refused(
                REASON_TOO_LARGE,
                f"{ALIAS_RELATIVE} exceeds the maximum of "
                f"{MAX_DECLARATION_BYTES} bytes while it was being read",
            )
        return b"".join(chunks)
    except (OSError, MemoryError) as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{ALIAS_RELATIVE}: {exc!r}")
    finally:
        os.close(fd)


def _read_with_dirfd(dirfd: int) -> Optional[WorkspaceAliasDeclaration] | AliasResolution:
    body = _read_bytes(dirfd)
    if body is None or isinstance(body, AliasResolution):
        return body
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return refused(
            REASON_DECLARATION_UNREADABLE,
            f"{ALIAS_RELATIVE}: invalid JSON ({exc})",
        )
    return parse_declaration(raw)


def read_declaration(
    repo_root: Path | str,
) -> Optional[WorkspaceAliasDeclaration] | AliasResolution:
    """Read ``repo_root``'s declaration. Never raises.

    Returns ``None`` only when the declaration is genuinely **absent** (no
    ``.mozyo-bridge`` directory, or no entry in it), a parsed
    :class:`WorkspaceAliasDeclaration`, or an :class:`AliasResolution` refusal
    when the entry exists but is not a readable, parseable regular file.
    """
    try:
        dirfd = _open_parent(repo_root, create=False)
    except WorkspaceAliasStoreError as exc:
        return refused(exc.reason, exc.detail)
    except OSError as exc:  # pragma: no cover - defensive
        return refused(REASON_DECLARATION_UNREADABLE, f"{_parent_path(repo_root)}: {exc}")
    if dirfd is None:
        return None
    lockfd: Optional[int] = None
    locked = False
    try:
        try:
            lockfd = _open_lock_fd(dirfd, create=False, writable=False)
        except WorkspaceAliasLockError as exc:
            return refused(exc.reason, exc.detail)
        if lockfd is None:
            # Do not create a lock file from a read-only operation. A present
            # entry without its coordination lock cannot be proved stable, so
            # it fails closed instead.
            try:
                entry = _lstat_entry(dirfd)
            except OSError as exc:
                return refused(
                    REASON_DECLARATION_UNREADABLE, f"{ALIAS_RELATIVE}: {exc}"
                )
            if entry is None:
                # Recheck the lock after observing absence. If a supported
                # writer created it in between, join the normal shared-lock
                # path instead of exposing its pre-install interval as None.
                # A second ENOENT is the linearization point: no supported
                # mutation had begun, and this read remains side-effect free.
                try:
                    lockfd = _open_lock_fd(dirfd, create=False, writable=False)
                except WorkspaceAliasLockError as exc:
                    return refused(exc.reason, exc.detail)
                if lockfd is None:
                    return None
            else:
                return refused(
                    REASON_LOCK_FAILED,
                    f"{ALIAS_RELATIVE} exists but {_LOCK_NAME} is absent; refusing "
                    "an uncoordinated read",
                )
        try:
            fcntl.flock(lockfd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return refused(
                    REASON_DECLARATION_MUTATION_IN_PROGRESS,
                    f"{_LOCK_NAME} is exclusively locked by a declaration mutation",
                )
            return refused(
                REASON_LOCK_FAILED, f"could not share-lock {_LOCK_NAME}: {exc}"
            )
        try:
            _require_lock_visible(dirfd, lockfd)
        except WorkspaceAliasLockError as exc:
            return refused(exc.reason, exc.detail)
        result = _read_with_dirfd(dirfd)
        try:
            _require_lock_visible(dirfd, lockfd)
        except WorkspaceAliasLockError as exc:
            return refused(exc.reason, exc.detail)
        return result
    finally:
        if lockfd is not None:
            if locked:
                try:
                    fcntl.flock(lockfd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lockfd)
        os.close(dirfd)


def _read_effective_with_mutation_lock(
    repo_root: Path | str,
    expected_lock_fd: int,
) -> Optional[WorkspaceAliasDeclaration] | AliasResolution:
    """Fresh path-based readback for a caller already holding ``LOCK_EX``.

    ``read_declaration`` deliberately opens another fd and attempts ``LOCK_SH``.
    Calling it while this process holds the writer lock would self-conflict and
    make every mutation fail. Writers use this helper only for their effective
    post-mutation check; the exclusive lock remains held for the entire call,
    while the parent directory is reopened from ``repo_root`` so parent drift is
    still observed rather than hidden by the writer's pinned dirfd.
    """
    try:
        dirfd = _open_parent(repo_root, create=False)
    except WorkspaceAliasStoreError as exc:
        return refused(exc.reason, exc.detail)
    except OSError as exc:  # pragma: no cover - defensive
        return refused(
            REASON_DECLARATION_UNREADABLE, f"{_parent_path(repo_root)}: {exc}"
        )
    if dirfd is None:
        return refused(
            REASON_PARENT_DRIFT,
            f"{_parent_path(repo_root)} disappeared while its mutation lock "
            "remained held; refusing to treat parent drift as an absent declaration",
        )
    try:
        try:
            _require_lock_visible(dirfd, expected_lock_fd)
        except WorkspaceAliasLockError as exc:
            return refused(exc.reason, exc.detail)
        result = _read_with_dirfd(dirfd)
        try:
            _require_lock_visible(dirfd, expected_lock_fd)
        except WorkspaceAliasLockError as exc:
            return refused(exc.reason, exc.detail)
        return result
    finally:
        os.close(dirfd)


def _write_temp(dirfd: int, body: bytes) -> str:
    name = f".{_ALIAS_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
            dir_fd=dirfd,
        )
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_WRITE_FAILED, f"could not create a temporary file: {exc}"
        ) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _discard(dirfd, name)
        raise WorkspaceAliasStoreError(
            REASON_WRITE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
        ) from exc
    return name


def _discard(dirfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dirfd)
    except OSError:
        pass


@contextmanager
def _mutation_lock(dirfd: int):
    """Compatibility wrapper preserving ``WorkspaceAliasStoreError``."""
    try:
        with workspace_alias_lock.mutation_lock(dirfd) as fd:
            yield fd
    except WorkspaceAliasLockError as exc:
        raise WorkspaceAliasStoreError(REASON_LOCK_FAILED, exc.detail) from exc


def _fence(dirfd: int):
    """A **content-bound** identity of the current declaration.

    Metadata alone is not an identity (review j#102641 Finding 2): an in-place
    update that keeps the inode, writes the same number of bytes and restores
    the mtime is invisible to a ``(dev, ino, mtime_ns, size)`` comparison, so a
    failed rollback would overwrite content this operation never read. The
    digest of the bytes closes that: two different declarations cannot share it.

    ``None`` when the declaration is absent. Raises ``OSError`` only from the
    ``lstat``; an unreadable-but-present entry yields ``(metadata, None)``, which
    can never compare equal to a readable one.
    """
    entry = _lstat_entry(dirfd)
    if entry is None:
        return None
    meta = (entry.st_dev, entry.st_ino, entry.st_mtime_ns, entry.st_size)
    body = _read_bytes(dirfd)
    digest = (
        hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else None
    )
    return (meta, digest)


def _require_fence(dirfd: int, expected) -> None:
    """Assert the declaration is byte-for-byte the one this operation read."""
    try:
        current = _fence(dirfd)
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_CONCURRENT_CHANGE, f"{ALIAS_RELATIVE}: {exc}"
        ) from exc
    if current != expected:
        raise WorkspaceAliasStoreError(
            REASON_CONCURRENT_CHANGE,
            f"{ALIAS_RELATIVE} changed between this operation's snapshot and its "
            f"write; refusing to overwrite a declaration it never read",
        )


def _fsync_dir(dirfd: int) -> None:
    """Make the directory entry durable, or refuse.

    Review j#102259 Finding 2: this used to swallow the error. An unsynced
    rename / unlink can be lost on power loss, so a mutation whose directory
    entry is not durable is not a completed declaration and must not be reported
    as one.
    """
    try:
        os.fsync(dirfd)
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_DURABILITY_FAILED,
            f"{_ALIAS_PARENT} could not be synced ({exc}); the change is not "
            f"durable",
        ) from exc


def _take_ownership(dirfd: int, fence) -> Optional[str]:
    """Atomically claim the current declaration, or refuse.

    ``_require_fence`` followed by ``os.replace`` / ``os.unlink`` is check-then-act:
    a writer that does not take the lock can land between the two syscalls, and
    the mutation then destroys an update this operation never read (review
    j#102710 r6f2). ``rename`` IS atomic, so the entry is moved aside FIRST —
    after which this operation owns that exact inode and can verify it at
    leisure. A mismatch is put back, leaving zero mutation.

    Returns the private name now holding the previous declaration, or ``None``
    when there was nothing to claim.
    """
    if fence is None:
        return None
    owned = f".{_ALIAS_NAME}.{uuid.uuid4().hex}.owned"
    try:
        os.rename(_ALIAS_NAME, owned, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except FileNotFoundError:
        raise WorkspaceAliasStoreError(
            REASON_CONCURRENT_CHANGE,
            f"{ALIAS_RELATIVE} disappeared before this operation could claim it",
        ) from None
    except OSError as exc:
        raise WorkspaceAliasStoreError(
            REASON_WRITE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
        ) from exc
    try:
        body = _read_owned(dirfd, owned)
    except OSError as exc:
        _put_back(dirfd, owned)
        raise WorkspaceAliasStoreError(
            REASON_CONCURRENT_CHANGE, f"{ALIAS_RELATIVE}: {exc}"
        ) from exc
    digest = hashlib.sha256(body).hexdigest() if body is not None else None
    if digest != fence[1]:
        _put_back(dirfd, owned)
        raise WorkspaceAliasStoreError(
            REASON_CONCURRENT_CHANGE,
            f"{ALIAS_RELATIVE} was replaced by another writer just before this "
            f"operation claimed it; the update it never read has been left in "
            f"place",
        )
    return owned


def _read_owned(dirfd: int, name: str) -> Optional[bytes]:
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dirfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DECLARATION_BYTES:
            return None
        return os.read(fd, MAX_DECLARATION_BYTES + 1)
    finally:
        os.close(fd)


def _put_back(dirfd: int, owned: str) -> None:
    """Restore a claimed entry. Best effort: the caller is already refusing."""
    try:
        os.rename(owned, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    except OSError:
        pass


def _restore(dirfd: int, backup: Optional[str], had_previous: bool) -> bool:
    """Undo a landed replace. True when the effective state was restored.

    ``had_previous`` without a ``backup`` is NOT restorable and must not report
    success (review j#102230 Finding 1): unlinking then would remove the new
    entry *and* leave the previous declaration destroyed, while telling the
    caller nothing was lost. The write path refuses before the replace when it
    cannot snapshot, so this is the belt to that braces.
    """
    if had_previous and backup is None:
        return False
    try:
        if had_previous and backup is not None:
            os.replace(backup, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        else:
            os.unlink(_ALIAS_NAME, dir_fd=dirfd)
        # A rollback that is not durable is not a rollback: after power loss the
        # workspace could come back holding the failed operation's declaration
        # (review j#102259 Finding 2, evidence C).
        os.fsync(dirfd)
        return True
    except OSError:
        return False


def write_declaration(
    repo_root: Path | str, declaration: WorkspaceAliasDeclaration
) -> Path:
    """Atomically write ``declaration``, preserving ``created_at``.

    Fails closed with :class:`WorkspaceAliasStoreError` when the destination is
    not a plain, single-linked regular file, when the parent is not a real
    directory or drifts mid-flight, or when the value read back — through a
    **fresh, path-based** open, so it is the effective state and not the pinned
    one — does not match what was intended. On every one of those the previous
    declaration is left exactly as it was.

    Idempotent: re-declaring the same alias keeps the original ``created_at`` so
    the durable record shows when the routing decision was first made.
    """
    dirfd = _open_parent(repo_root, create=True)
    assert dirfd is not None  # create=True either opens or raises
    backup: Optional[str] = None
    try:
      with _mutation_lock(dirfd) as mutation_lock_fd:
        anchor = _fd_identity(dirfd)
        _require_parent_visible(repo_root, anchor)

        try:
            entry = _lstat_entry(dirfd)
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_WRITE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        if entry is not None:
            if not stat.S_ISREG(entry.st_mode):
                raise WorkspaceAliasStoreError(
                    REASON_NOT_REGULAR_FILE,
                    f"{ALIAS_RELATIVE} exists but is not a regular file "
                    f"(mode {stat.filemode(entry.st_mode)}); refusing to write "
                    f"through it. Inspect and remove it by hand.",
                )
            if entry.st_nlink != 1:
                raise WorkspaceAliasStoreError(
                    REASON_MULTIPLE_LINKS,
                    f"{ALIAS_RELATIVE} has {entry.st_nlink} hard links; refusing "
                    f"to write a declaration whose inode is shared.",
                )

        # An existing entry that cannot be captured is refused HERE, before the
        # replace (review j#102230 Finding 1). Proceeding would put the write in
        # a state where a later verification failure has nothing to restore: the
        # rollback would unlink the new entry and the previous declaration would
        # be gone, reported as an unchanged no-op.
        had_previous = entry is not None
        previous_bytes: Optional[bytes] = None
        if had_previous:
            previous = _read_bytes(dirfd)
            if isinstance(previous, AliasResolution):
                raise WorkspaceAliasStoreError(
                    REASON_SNAPSHOT_FAILED,
                    f"the existing {ALIAS_RELATIVE} could not be captured "
                    f"({previous.reason}: {previous.detail}), so it could not be "
                    f"restored if this write failed verification",
                )
            if previous is None:
                # It existed at the lstat and was gone by the snapshot read, so
                # the true previous state is "absent". Recording that (rather
                # than leaving had_previous set with no backup) is what lets a
                # rollback restore it by removing the new entry; otherwise a
                # failed write would leave a declaration the operator never
                # successfully set active in the workspace.
                had_previous = False
            previous_bytes = previous

        # The content-bound fence of what this operation actually read. Captured
        # AFTER the snapshot and re-checked before the replace, so a declaration
        # that landed during the read is not mistaken for the one snapshotted.
        try:
            fence = _fence(dirfd)
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_CONCURRENT_CHANGE, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        if had_previous and fence is None:
            had_previous = False
            previous_bytes = None
        if had_previous and previous_bytes is not None:
            if fence is None or fence[1] != hashlib.sha256(previous_bytes).hexdigest():
                raise WorkspaceAliasStoreError(
                    REASON_CONCURRENT_CHANGE,
                    f"{ALIAS_RELATIVE} changed while it was being snapshotted; "
                    f"refusing to proceed with bytes that are no longer current",
                )

        created_at = declaration.created_at
        if not created_at:
            existing = _read_with_dirfd(dirfd)
            if isinstance(existing, WorkspaceAliasDeclaration) and existing.created_at:
                created_at = existing.created_at

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = dict(declaration.as_payload())
        payload["schema_version"] = ALIAS_SCHEMA_VERSION
        payload["created_at"] = created_at or now
        payload["updated_at"] = now
        body = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        # The previous content is staged BEFORE the replace, so a post-replace
        # verification failure can put it back (j#102140 Finding 2).
        if previous_bytes is not None:
            backup = _write_temp(dirfd, previous_bytes)
        temp_name = _write_temp(dirfd, body)

        owned: Optional[str] = None
        try:
            _require_parent_visible(repo_root, anchor)
            # Claim the current entry atomically, then install. When there is
            # nothing to claim, `os.link` creates only if absent, so a
            # declaration that appeared meanwhile is not clobbered.
            owned = _take_ownership(dirfd, fence)
            if fence is None:
                try:
                    os.link(temp_name, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
                except FileExistsError:
                    raise WorkspaceAliasStoreError(
                        REASON_CONCURRENT_CHANGE,
                        f"{ALIAS_RELATIVE} was created by another writer while "
                        f"this operation was preparing to create it",
                    ) from None
                _discard(dirfd, temp_name)
            else:
                os.replace(temp_name, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        except WorkspaceAliasStoreError:
            _discard(dirfd, temp_name)
            if owned is not None:
                _put_back(dirfd, owned)
            raise
        except OSError as exc:
            _discard(dirfd, temp_name)
            raise WorkspaceAliasStoreError(
                REASON_WRITE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        failure: Optional[Tuple[str, str]] = None
        try:
            # The rename is not a completed declaration until its directory
            # entry is durable (review j#102259 Finding 2).
            _fsync_dir(dirfd)
            _require_parent_visible(repo_root, anchor)
        except WorkspaceAliasStoreError as exc:
            failure = (exc.reason, exc.detail)
        if failure is None:
            readback = _read_with_dirfd(dirfd)
            if isinstance(readback, AliasResolution):
                failure = (
                    REASON_READBACK_FAILED,
                    f"{ALIAS_RELATIVE} did not read back as a valid declaration "
                    f"({readback.reason}: {readback.detail})",
                )
            elif readback is None or readback.as_payload() != payload:
                failure = (
                    REASON_READBACK_FAILED,
                    f"{ALIAS_RELATIVE} read back differently than it was written",
                )
        if failure is None:
            # The effective state, read through a fresh path-based open. This is
            # what catches a parent that drifted: the pinned dirfd would happily
            # read back the write it just made into a detached directory.
            effective = _read_effective_with_mutation_lock(
                repo_root, mutation_lock_fd
            )
            if isinstance(effective, AliasResolution) or effective is None or (
                effective.as_payload() != payload
            ):
                failure = (
                    REASON_READBACK_FAILED,
                    f"{ALIAS_RELATIVE} is not the effective declaration at "
                    f"{alias_path(repo_root)} after the write",
                )
        if failure is not None:
            restored = _restore(dirfd, backup, had_previous)
            raise WorkspaceAliasStoreError(
                failure[0], failure[1], mutated=not restored
            )

        return alias_path(repo_root)
    finally:
        if backup is not None:
            _discard(dirfd, backup)
        os.close(dirfd)


def clear_declaration(repo_root: Path | str) -> str:
    """Remove ``repo_root``'s declaration.

    Returns :data:`CLEAR_REMOVED` or :data:`CLEAR_ABSENT`, and raises
    :class:`WorkspaceAliasStoreError` when an entry exists but could not be
    removed, or when the removal did not take effect at the workspace-visible
    path. A present-but-unremovable declaration must never be reported as
    "nothing was declared" — that would tell the operator the workspace launches
    independently again when it does not.

    Removes only this entry: unlinking a symlink drops the link, never its
    target, and the identity anchor, registry row and tracked scaffold / catalog
    / skills content are untouched.
    """
    try:
        dirfd = _open_parent(repo_root, create=False)
    except WorkspaceAliasStoreError as exc:
        raise WorkspaceAliasStoreError(REASON_REMOVE_FAILED, exc.detail) from exc
    if dirfd is None:
        return CLEAR_ABSENT
    try:
      with _mutation_lock(dirfd) as mutation_lock_fd:
        anchor = _fd_identity(dirfd)
        try:
            _require_parent_visible(repo_root, anchor)
        except WorkspaceAliasStoreError as exc:
            raise WorkspaceAliasStoreError(REASON_REMOVE_FAILED, exc.detail) from exc
        # Capture the entry before removing it, so a failed post-removal
        # verification can put it back (review j#102230 Finding 1). Only a
        # regular file is capturable; a symlink / directory is not, and removing
        # one is reported honestly as a mutation if it cannot be confirmed.
        try:
            entry = _lstat_entry(dirfd)
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_REMOVE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        snapshot: Optional[bytes] = None
        if entry is not None and stat.S_ISREG(entry.st_mode):
            captured = _read_bytes(dirfd)
            if isinstance(captured, bytes):
                snapshot = captured

        # The removal must be fenced exactly like a write (review j#102641
        # Finding 2): without this, a declaration that landed between the
        # snapshot and the unlink was deleted and reported as a clean removal,
        # destroying an update this operation never read.
        try:
            fence = _fence(dirfd)
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_REMOVE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        if snapshot is not None and (
            fence is None or fence[1] != hashlib.sha256(snapshot).hexdigest()
        ):
            raise WorkspaceAliasStoreError(
                REASON_CONCURRENT_CHANGE,
                f"{ALIAS_RELATIVE} changed while it was being read; refusing to "
                f"remove a declaration this operation never read",
            )

        try:
            if snapshot is not None:
                # A regular declaration is claimed atomically first, so an
                # external replacement between the check and the removal is
                # detected and left in place rather than deleted (j#102710 r6f2).
                claimed = _take_ownership(dirfd, fence)
                if claimed is None:
                    raise FileNotFoundError
                os.unlink(claimed, dir_fd=dirfd)
            else:
                # Non-regular entries (symlink / directory / FIFO) have no
                # content to claim: unlinking a symlink drops the link, and a
                # directory correctly fails below.
                os.unlink(_ALIAS_NAME, dir_fd=dirfd)
        except FileNotFoundError:
            outcome = CLEAR_ABSENT
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_REMOVE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        else:
            outcome = CLEAR_REMOVED

        # The removal is not complete until its directory entry is durable, and
        # then only if the effective state really is "no declaration" — a
        # drifted parent would have removed the entry from a directory the
        # workspace can no longer see (review j#102259 Finding 2 evidence B).
        durability: Optional[str] = None
        if outcome == CLEAR_REMOVED:
            try:
                _fsync_dir(dirfd)
            except WorkspaceAliasStoreError as exc:
                durability = exc.detail
        effective = _read_effective_with_mutation_lock(repo_root, mutation_lock_fd)
        if effective is None and durability is None:
            return outcome

        # The removal is not confirmed. Once the unlink landed, this IS a
        # mutation, so put the entry back and only report an unchanged workspace
        # when that actually succeeded.
        restored = False
        if outcome == CLEAR_REMOVED and snapshot is not None:
            try:
                recovery = _write_temp(dirfd, snapshot)
                os.replace(recovery, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
                # A restore that is not durable is not a restore.
                os.fsync(dirfd)
                restored = True
            except (WorkspaceAliasStoreError, OSError):
                restored = False
        detail = durability or (
            f"the removal of {alias_path(repo_root)} could not be confirmed "
            f"({getattr(effective, 'reason', 'a declaration is still effective')})"
        )
        raise WorkspaceAliasStoreError(
            REASON_DURABILITY_FAILED if durability else REASON_REMOVE_FAILED,
            detail,
            mutated=outcome == CLEAR_REMOVED and not restored,
        )
    finally:
        os.close(dirfd)


__all__ = (
    "CLEAR_ABSENT",
    "WorkspaceAliasDeclaration",
    "CLEAR_REMOVED",
    "WorkspaceAliasStoreError",
    "alias_path",
    "clear_declaration",
    "declaration_exists",
    "read_declaration",
    "write_declaration",
)
