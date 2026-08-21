"""The at-use fence for the diagnostic sink — a path decision is not a write permit (#15840).

Design record: ``vibes/docs/logics/exception-diagnostic-sink-boundary.md``.

:func:`...diagnostic_sink_location.resolve_diagnostic_sink_root` decides whether a path *may*
hold diagnostics. Review j#109685 ``finding_staleadmissionrace`` showed why that decision cannot
also be the write permit: ``Path.resolve()`` is non-strict, so a candidate under an ancestor that
does not exist yet canonicalizes happily — and once the caller holds the admitted root, replacing
that ancestor with a symlink moves it inside a forbidden root. Reproduced:

    admission        -> admissible=True   root=<tmp>/state/mozyo-bridge/diagnostics
    (ancestor `state` is then replaced with a symlink to the guarded home)
    re-resolution    -> <tmp>/guarded/mozyo-bridge/diagnostics

The earlier symlink regression only covered links that already existed when the decision was
taken. A path string checked at time T says nothing about the filesystem at time T+1.

This module is the other half: **the check happens on the filesystem objects actually being
opened, at the moment they are opened.** Instead of trusting a resolved string, it walks the
components one at a time with ``openat``-style ``dir_fd`` opens and ``O_NOFOLLOW``, so a symlink
anywhere on the path is a refusal rather than a redirection. There is no string to substitute
behind us: each component is opened relative to a descriptor we already hold.

**Named residual.** ``O_NOFOLLOW`` refuses to *traverse* a symlink; it does not stop a directory
from being renamed out from under an already-open descriptor. Once a descriptor is held, however,
it refers to that inode — a later rename does not redirect writes through it. The window this
does NOT close is between the caller receiving the descriptor and using it for something other
than a ``dir_fd``-relative open. Callers must keep using the descriptor, never re-derive the path
as a string.

Platform: ADR-0012 fixes the supported platforms as macOS and Linux, both of which provide
``dir_fd`` support. Where ``os.supports_dir_fd`` does not advertise it, this refuses rather than
silently falling back to a path-based open (which is precisely the unsafe form).
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: The platform does not offer descriptor-relative opens, so the walk cannot be performed safely.
#: Refusing beats falling back to a path-based open — the fallback IS the vulnerability.
FENCE_DIR_FD_UNSUPPORTED = "dir_fd_unsupported"
#: The root is not an absolute path, so there is no anchor to start the walk from.
FENCE_ROOT_NOT_ABSOLUTE = "root_not_absolute"
#: A component on the way to the sink is a symlink. Following it is exactly the substitution this
#: fence exists to refuse, so the walk stops instead.
FENCE_SYMLINK_COMPONENT = "symlink_component"
#: A component exists but is not a directory.
FENCE_NOT_A_DIRECTORY = "not_a_directory"
#: A component is missing and the caller did not ask for it to be created.
FENCE_MISSING_COMPONENT = "missing_component"
#: The open failed for an OS reason that is not one of the above (permission, ENOSPC, ...).
FENCE_OPEN_FAILED = "open_failed"


@dataclass(frozen=True)
class FenceResult:
    """An open directory descriptor for the sink, or the reason there is none.

    ``dir_fd`` is set only when ``ok``. **The caller owns it and must close it**, and must keep
    addressing the sink through it (``os.open(name, ..., dir_fd=result.dir_fd)``) rather than
    re-deriving a path string — re-deriving reintroduces the very substitution window the walk
    just closed.

    A refusal means the diagnostic is dropped. That is the intended direction: losing a
    diagnostic is strictly less bad than writing raw exception text into a guarded home or a
    committable checkout.
    """

    ok: bool
    dir_fd: Optional[int] = None
    reason: str = ""
    detail: str = ""

    def as_payload(self) -> dict:
        """Diagnostic shape for the fence itself — carries no path (#15840 決定 3)."""
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail}


def _refused(reason: str, detail: str) -> FenceResult:
    return FenceResult(ok=False, reason=reason, detail=detail)


def open_sink_directory(root: Path, *, create: bool = False) -> FenceResult:
    """Open ``root`` as a directory descriptor without ever traversing a symlink (#15840).

    ``root`` is the path :func:`...diagnostic_sink_location.resolve_diagnostic_sink_root` already
    admitted. Admission is advisory: it says the location is *allowed*, not that the filesystem
    still matches what was inspected. This performs the second, non-optional half.

    The walk starts at ``/`` and opens one component at a time with ``O_NOFOLLOW | O_DIRECTORY``
    relative to the descriptor for its parent. Consequences that matter:

    - a symlink **anywhere** on the path is :data:`FENCE_SYMLINK_COMPONENT`, whether it was
      planted before or after admission — the review's counter-example is refused by
      construction rather than by a second string comparison;
    - nothing is resolved from a string after the walk begins, so there is no name left to
      substitute;
    - ``create=True`` makes missing components with mode ``0o700``, still one at a time and still
      relative to the parent descriptor. A component that appears between the failed open and the
      ``mkdir`` is re-opened with the same ``O_NOFOLLOW``, so a planted symlink loses that race
      too.

    Every intermediate descriptor is closed; on success exactly one (the sink directory's) is
    returned to the caller, who owns it.
    """
    if os.open not in os.supports_dir_fd:
        return _refused(
            FENCE_DIR_FD_UNSUPPORTED,
            "this platform does not support descriptor-relative opens, so the sink path cannot "
            "be walked without exposing a symlink-substitution window. Falling back to a "
            "path-based open would be the unsafe form, so the diagnostic is dropped instead",
        )
    if not Path(root).is_absolute():
        return _refused(
            FENCE_ROOT_NOT_ABSOLUTE,
            "the sink root is relative; there is no anchor to begin a component walk from",
        )

    parts = Path(root).parts
    anchor, components = parts[0], parts[1:]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(anchor, flags)
    except OSError as exc:
        return _refused(
            FENCE_OPEN_FAILED,
            f"the filesystem anchor could not be opened ({exc.__class__.__name__})",
        )

    # Every descriptor except the one handed back is closed, on every exit path. A refusal is
    # the COMMON case for this fence (it runs on a diagnostic path), so leaking one fd per
    # refusal would exhaust the process's descriptors exactly when things are already going
    # wrong. Found while pinning the review's counter-example, not by the counter-example.
    try:
        for name in components:
            nxt = _open_component(current, name, flags, create=create)
            if isinstance(nxt, FenceResult):
                return nxt
            os.close(current)
            current = nxt
    except BaseException:
        os.close(current)
        raise
    else:
        handed_back = current
        current = None
        return FenceResult(ok=True, dir_fd=handed_back)
    finally:
        if current is not None:
            os.close(current)


def _open_component(parent_fd: int, name: str, flags: int, *, create: bool):
    """Open one component below ``parent_fd``. Returns an ``int`` fd or a typed refusal."""
    opened = _try_open(parent_fd, name, flags)
    if not isinstance(opened, _Missing):
        return opened
    if not create:
        return _refused(
            FENCE_MISSING_COMPONENT,
            "a component on the way to the sink does not exist and creation was not requested",
        )
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        # Someone created it between the failed open and here. The re-open below applies the
        # same O_NOFOLLOW check, so a symlink planted in that gap loses the race too.
        pass
    except OSError as exc:
        return _refused(
            FENCE_OPEN_FAILED,
            f"a missing component could not be created ({exc.__class__.__name__})",
        )
    opened = _try_open(parent_fd, name, flags)
    if isinstance(opened, _Missing):
        return _refused(
            FENCE_OPEN_FAILED,
            "a component vanished immediately after being created; nothing is written",
        )
    return opened


def _is_symlink(parent_fd: int, name: str) -> bool:
    """Is ``name`` below ``parent_fd`` a symlink? ``lstat`` does not follow, so this is safe.

    Only ever used to pick the right typed reason after an open has ALREADY been refused —
    never to decide whether to proceed.
    """
    try:
        return os.path.islink(os.path.join("/proc/self/fd", str(parent_fd), name)) or bool(
            os.lstat(name, dir_fd=parent_fd).st_mode & 0o120000 == 0o120000
        )
    except OSError:
        return False


class _Missing:
    """Sentinel: the component does not exist (ENOENT), which is not itself a refusal."""


def _try_open(parent_fd: int, name: str, flags: int):
    """``os.open`` below ``parent_fd``, mapping the errnos that carry meaning here."""
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            # POSIX says O_NOFOLLOW raises ELOOP on a symlink, but Linux returns ENOTDIR when
            # O_DIRECTORY is set as well -- measured on this platform. Both errnos therefore
            # have to be disambiguated by asking the filesystem directly, or a symlink
            # substitution would be reported as the unrelated "not a directory". The refusal is
            # identical either way; only the typed reason differs, and a wrong reason sends the
            # next reader looking for the wrong problem.
            if _is_symlink(parent_fd, name):
                return _refused(
                    FENCE_SYMLINK_COMPONENT,
                    "a component on the way to the sink is a symbolic link. Following it is "
                    "the substitution this fence exists to refuse, so nothing is written",
                )
            return _refused(
                FENCE_NOT_A_DIRECTORY,
                "a component on the way to the sink exists but is not a directory",
            )
        if exc.errno == errno.ENOENT:
            return _Missing()
        return _refused(
            FENCE_OPEN_FAILED,
            f"a component could not be opened ({exc.__class__.__name__})",
        )


__all__ = (
    "FENCE_DIR_FD_UNSUPPORTED",
    "FENCE_MISSING_COMPONENT",
    "FENCE_NOT_A_DIRECTORY",
    "FENCE_OPEN_FAILED",
    "FENCE_ROOT_NOT_ABSOLUTE",
    "FENCE_SYMLINK_COMPONENT",
    "FenceResult",
    "open_sink_directory",
)
