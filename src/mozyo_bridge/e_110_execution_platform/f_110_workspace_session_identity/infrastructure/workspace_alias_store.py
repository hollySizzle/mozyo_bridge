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

Filesystem safety (review j#102104 Findings 1 & 2)
--------------------------------------------------
The first implementation used ``Path.write_text`` / ``Path.is_file`` directly.
Both are wrong here, and the review measured both:

- ``write_text`` **follows symlinks**, so a ``workspace-alias.json`` symlinked out
  of the workspace made ``workspace alias disable`` overwrite an arbitrary file
  outside it — a repo-controlled path escaping the "writes only its own
  declaration" boundary;
- ``is_file()`` is false for a *directory* (or FIFO, or dangling symlink) at that
  path, so a substituted or damaged declaration read back as "nothing declared"
  and re-enabled the nested launch — the exact opposite of the fail-closed
  contract this rail exists to provide.

So every operation here is anchored to a **directory file descriptor** opened
``O_NOFOLLOW``, and every path component decision is made from ``lstat`` rather
than from a follow-through helper:

- "absent" is distinguished from "present but not a regular file"; only the
  former is ``no declaration``, the latter is a typed refusal;
- writes go to a private temp file in the same directory and land via
  ``os.replace`` on the same descriptor — atomic, never following a symlink at
  the destination — then are read back and compared before being reported;
- any failure at any step leaves the declaration unchanged (zero mutation) and
  raises :class:`WorkspaceAliasStoreError` with a fixed typed reason.

Reads still never raise: an unreadable or malformed declaration is *reported* as
a typed refusal so the caller fails closed with a nameable reason.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    ALIAS_RELATIVE,
    ALIAS_SCHEMA_VERSION,
    REASON_DECLARATION_UNREADABLE,
    REASON_MULTIPLE_LINKS,
    REASON_NOT_REGULAR_FILE,
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
    """A mutation refused, carrying a fixed typed reason. Zero mutation."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def alias_path(repo_root: Path | str) -> Path:
    """The declaration path for ``repo_root`` (also the write target)."""
    return Path(repo_root) / ALIAS_RELATIVE


def _open_parent(repo_root: Path | str, *, create: bool) -> Optional[int]:
    """A dirfd for ``<repo_root>/.mozyo-bridge``, opened ``O_NOFOLLOW``.

    ``None`` when the directory does not exist and ``create`` is false. Raises
    :class:`WorkspaceAliasStoreError` when the path exists but is not a real
    directory — including when it is a *symlink* to one, which ``O_NOFOLLOW``
    rejects with ``ELOOP``. That refusal is the point: a symlinked
    ``.mozyo-bridge`` would let a repo redirect every declaration write out of
    the workspace.
    """
    parent = Path(repo_root) / _ALIAS_PARENT
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


def _lstat_entry(dirfd: int) -> Optional[os.stat_result]:
    """``lstat`` of the declaration entry, or ``None`` when absent."""
    try:
        return os.stat(_ALIAS_NAME, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def declaration_exists(repo_root: Path | str) -> bool:
    """Whether ``repo_root`` carries a declaration entry of any kind.

    Used for the alias-cycle check, which must fail closed on a target whose
    declaration is *present but broken* just as firmly as on a valid one. So this
    reports any existing directory entry — regular file, symlink, directory —
    rather than only a readable declaration.
    """
    try:
        dirfd = _open_parent(repo_root, create=False)
    except WorkspaceAliasStoreError:
        # An unsafe parent is not evidence of "no declaration"; treat it as
        # present so the caller refuses instead of aliasing into it.
        return True
    if dirfd is None:
        return False
    try:
        return _lstat_entry(dirfd) is not None
    finally:
        os.close(dirfd)


def _read_with_dirfd(dirfd: int) -> Optional[WorkspaceAliasDeclaration] | AliasResolution:
    entry = _lstat_entry(dirfd)
    if entry is None:
        return None
    if not stat.S_ISREG(entry.st_mode):
        # Directory / symlink / FIFO / device. NOT "no declaration" (Finding 2).
        return refused(
            REASON_NOT_REGULAR_FILE,
            f"{ALIAS_RELATIVE} exists but is not a regular file "
            f"(mode {stat.filemode(entry.st_mode)}); refusing to treat it as an "
            f"absent declaration",
        )
    try:
        fd = os.open(
            _ALIAS_NAME,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=dirfd,
        )
    except OSError as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{ALIAS_RELATIVE}: {exc}")
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{ALIAS_RELATIVE}: {exc}")
    except ValueError as exc:
        return refused(
            REASON_DECLARATION_UNREADABLE,
            f"{ALIAS_RELATIVE}: invalid JSON ({exc})",
        )
    return parse_declaration(raw)



def read_declaration(
    repo_root: Path | str,
) -> Optional[WorkspaceAliasDeclaration] | AliasResolution:
    """Read ``repo_root``'s declaration.

    Returns ``None`` only when the declaration is genuinely **absent** (no
    ``.mozyo-bridge`` directory, or no entry in it), a parsed
    :class:`WorkspaceAliasDeclaration`, or an :class:`AliasResolution` refusal
    when the entry exists but is not a readable, parseable regular file.
    """
    try:
        dirfd = _open_parent(repo_root, create=False)
    except WorkspaceAliasStoreError as exc:
        return refused(exc.reason, exc.detail)
    if dirfd is None:
        return None
    try:
        return _read_with_dirfd(dirfd)
    finally:
        os.close(dirfd)


def write_declaration(
    repo_root: Path | str, declaration: WorkspaceAliasDeclaration
) -> Path:
    """Atomically write ``declaration``, preserving ``created_at``.

    Fails closed with :class:`WorkspaceAliasStoreError` — leaving any existing
    declaration untouched — when the destination is not a plain, single-linked
    regular file, when the parent is not a real directory, or when the value read
    back does not match what was intended.

    Idempotent: re-declaring the same alias keeps the original ``created_at`` so
    the durable record shows when the routing decision was first made.
    """
    dirfd = _open_parent(repo_root, create=True)
    assert dirfd is not None  # create=True either opens or raises
    try:
        entry = _lstat_entry(dirfd)
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

        temp_name = f".{_ALIAS_NAME}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(
                temp_name,
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
            os.replace(temp_name, _ALIAS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        except OSError as exc:
            try:
                os.unlink(temp_name, dir_fd=dirfd)
            except OSError:
                pass
            raise WorkspaceAliasStoreError(
                REASON_WRITE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        try:
            os.fsync(dirfd)
        except OSError:
            # Durability hint only; the rename already landed.
            pass

        readback = _read_with_dirfd(dirfd)
        if isinstance(readback, AliasResolution):
            raise WorkspaceAliasStoreError(
                REASON_READBACK_FAILED,
                f"{ALIAS_RELATIVE} did not read back as a valid declaration "
                f"({readback.reason}: {readback.detail})",
            )
        if readback is None or readback.as_payload() != payload:
            raise WorkspaceAliasStoreError(
                REASON_READBACK_FAILED,
                f"{ALIAS_RELATIVE} read back differently than it was written",
            )
        return alias_path(repo_root)
    finally:
        os.close(dirfd)


def clear_declaration(repo_root: Path | str) -> str:
    """Remove ``repo_root``'s declaration.

    Returns :data:`CLEAR_REMOVED` or :data:`CLEAR_ABSENT`, and raises
    :class:`WorkspaceAliasStoreError` when an entry exists but could not be
    removed. A present-but-unremovable declaration must never be reported as
    "nothing was declared" (review j#102104 Finding 2) — that would tell the
    operator the workspace launches independently again when it does not.

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
        try:
            os.unlink(_ALIAS_NAME, dir_fd=dirfd)
        except FileNotFoundError:
            return CLEAR_ABSENT
        except OSError as exc:
            raise WorkspaceAliasStoreError(
                REASON_REMOVE_FAILED, f"{ALIAS_RELATIVE}: {exc}"
            ) from exc
        return CLEAR_REMOVED
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
