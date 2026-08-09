"""Workspace-local read/write for the nested-workspace alias declaration (#15190).

The declaration lives at ``<workspace>/.mozyo-bridge/workspace-alias.json``,
next to the identity anchor but deliberately separate from it (see
:data:`...domain.workspace_alias.ALIAS_RELATIVE`).

Workspace-local storage — not the home registry — is the point. The acceptance
boundary for #15190 requires the declaration to survive **registry loss and
recovery**: after the home registry is moved aside and rebuilt from anchors, a
nested workspace must still fold into its parent instead of quietly becoming
launchable again. A row in ``registry.sqlite`` would be destroyed by exactly the
recovery procedure it has to outlive, and would also require a schema bump on
the identity store this rail is forbidden to hand-edit.

Filesystem safety
-----------------
This file gates whether a launch happens, and it sits at a path the repository
controls, so every operation treats the path itself as hostile. Four separate
review findings shaped the current contract; each is load-bearing.

- **Never follow a symlink** (j#102104 F1). ``Path.write_text`` follows them, so
  a ``workspace-alias.json`` symlinked out of the workspace made
  ``workspace alias disable`` overwrite an arbitrary external file. Operations
  are anchored to a directory fd opened ``O_NOFOLLOW`` and decisions are made
  from ``lstat``.
- **"Absent" is not "unreadable"** (j#102104 F2). ``is_file()`` is false for a
  directory, FIFO or dangling symlink, so a substituted declaration read back as
  "nothing declared" and the nested launch proceeded. Only a genuinely missing
  entry is ``no declaration``; anything else is a typed refusal.
- **A pinned dirfd is not the visible directory** (j#102140 F1). A dirfd survives
  a rename of its directory, so a write could land in a *detached* directory and
  report success while the workspace-visible path had nothing. Every mutation
  re-verifies that the root-visible ``.mozyo-bridge`` is still the same inode it
  pinned, and confirms the result by reading back through a **fresh** path-based
  open — the effective state, not the pinned one.
- **A failed verification must not leave damage** (j#102140 F2). ``os.replace``
  has already landed by the time a readback can fail, so the previous content is
  restored (or the new entry removed when there was none) before the typed error
  is raised. :class:`WorkspaceAliasStoreError` carries ``mutated`` so the CLI
  reports what actually happened instead of always claiming nothing was written.
- **Reads never raise, and never block** (j#102140 F3). Any ``stat``/``open``/
  ``read`` failure becomes a typed refusal, and the file is opened
  ``O_NONBLOCK`` and re-checked with ``fstat`` so a regular-file-to-FIFO swap
  between the ``lstat`` and the ``open`` cannot hang the reader forever.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    ALIAS_RELATIVE,
    ALIAS_SCHEMA_VERSION,
    REASON_DECLARATION_UNREADABLE,
    REASON_MULTIPLE_LINKS,
    REASON_NOT_REGULAR_FILE,
    REASON_PARENT_DRIFT,
    REASON_PARENT_UNSAFE,
    REASON_READBACK_FAILED,
    REASON_REMOVE_FAILED,
    REASON_WRITE_FAILED,
    AliasResolution,
    WorkspaceAliasDeclaration,
    parse_declaration,
    refused,
)

_ALIAS_PARENT = Path(ALIAS_RELATIVE).parent.as_posix()
_ALIAS_NAME = Path(ALIAS_RELATIVE).name

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
    if create:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_PARENT_UNSAFE, f"{parent}: {exc}"
            ) from exc
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
    try:
        dirfd = _open_parent(repo_root, create=False)
    except WorkspaceAliasStoreError:
        return True
    if dirfd is None:
        return False
    try:
        return _lstat_entry(dirfd) is not None
    except OSError:
        return True
    finally:
        os.close(dirfd)


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
        return os.read(fd, max(opened.st_size, 0) + 1)
    except OSError as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{ALIAS_RELATIVE}: {exc}")
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
    try:
        return _read_with_dirfd(dirfd)
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


def _restore(dirfd: int, backup: Optional[str], had_previous: bool) -> bool:
    """Undo a landed replace. True when the effective state was restored."""
    try:
        if had_previous and backup is not None:
            os.replace(backup, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        else:
            os.unlink(_ALIAS_NAME, dir_fd=dirfd)
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

        previous = _read_bytes(dirfd)
        previous_bytes = previous if isinstance(previous, bytes) else None
        had_previous = entry is not None

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

        try:
            _require_parent_visible(repo_root, anchor)
            os.replace(temp_name, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        except WorkspaceAliasStoreError:
            _discard(dirfd, temp_name)
            raise
        except OSError as exc:
            _discard(dirfd, temp_name)
            raise WorkspaceAliasStoreError(
                REASON_WRITE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        try:
            os.fsync(dirfd)
        except OSError:
            pass  # durability hint only; the rename already landed

        failure: Optional[Tuple[str, str]] = None
        try:
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
            effective = read_declaration(repo_root)
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
        anchor = _fd_identity(dirfd)
        try:
            _require_parent_visible(repo_root, anchor)
        except WorkspaceAliasStoreError as exc:
            raise WorkspaceAliasStoreError(REASON_REMOVE_FAILED, exc.detail) from exc
        try:
            os.unlink(_ALIAS_NAME, dir_fd=dirfd)
        except FileNotFoundError:
            outcome = CLEAR_ABSENT
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_REMOVE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        else:
            outcome = CLEAR_REMOVED
        # Confirm through a fresh path-based open that the effective state really
        # is "no declaration" — a drifted parent would have removed the entry
        # from a directory the workspace can no longer see.
        if read_declaration(repo_root) is not None:
            raise WorkspaceAliasStoreError(
                REASON_REMOVE_FAILED,
                f"a declaration is still effective at {alias_path(repo_root)} "
                f"after the removal",
            )
        return outcome
    finally:
        os.close(dirfd)


__all__ = (
    "CLEAR_ABSENT",
    "CLEAR_REMOVED",
    "WorkspaceAliasStoreError",
    "alias_path",
    "clear_declaration",
    "declaration_exists",
    "read_declaration",
    "write_declaration",
)
