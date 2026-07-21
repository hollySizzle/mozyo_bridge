"""Pasteable-safe worktree-path redaction for sublane records (Redmine #13368).

Split out of :mod:`sublane_lifecycle` (module-health reduction, Redmine #14224 — the
new leaf-lane admission fence pushed the module past the 1000-line threshold and this
trio has no internal caller within that module, only external importers, so it is a
clean, behavior-preserving relocation). Every name / behavior is unchanged from its
prior home; :mod:`sublane_lifecycle` re-exports them so existing importers are
unaffected.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _portable_basename(text: str) -> str:
    """Final path component of ``text`` for either a POSIX or a Windows path (pure).

    Redmine #13368 review j#73538 (finding 1): a lane ``worktree_path`` may be a
    Windows-shaped host-local path (``C:\\Users\\<user>\\lane``). ``PurePosixPath``
    does not treat ``\\`` as a separator, so its ``.name`` returns the whole string
    and the private prefix survives redaction. Detect a Windows shape (a backslash
    separator or a leading drive designator) and use the Windows flavor for it;
    otherwise the POSIX flavor. Falls back to the raw string when no component can
    be derived (e.g. a bare drive), never an empty result.
    """
    if "\\" in text or _WINDOWS_DRIVE_RE.match(text):
        return PureWindowsPath(text).name or text
    return PurePosixPath(text).name or text


def portable_worktree_label(worktree_path: Optional[str]) -> str:
    """Pasteable-safe label for a lane worktree: its sibling basename (pure, #13368).

    Returns the worktree directory basename (no personal home / private-project
    absolute prefix), so it is safe to render into a Redmine journal / pasteable
    durable record. Handles both POSIX and Windows-shaped paths (:func:`_portable_basename`).
    Empty input renders as ``-`` (matching the existing ``or '-'`` field convention
    in the record renderers).
    """
    text = (worktree_path or "").strip()
    if not text:
        return "-"
    return _portable_basename(text)


def redact_worktree_paths(text: str, *worktree_paths: Optional[str]) -> str:
    """Redact known host-local worktree absolute paths in composed record text (pure).

    Replaces each supplied absolute worktree path with its portable sibling basename
    (:func:`portable_worktree_label`) wherever it appears in ``text`` — e.g. inside a
    replayable ``git worktree add <abs>`` / ``cockpit append --repo <abs>`` command
    line — so a pasteable text record carries no private path while the exact command
    (with the absolute path) is still preserved in the structured JSON outcome
    (#13368). Replacement is by the exact known string, never by guessing a home
    prefix, so it cannot mangle unrelated text.
    """
    out = text
    for raw in worktree_paths:
        candidate = (raw or "").strip()
        if candidate:
            out = out.replace(candidate, _portable_basename(candidate))
    return out


__all__ = (
    "portable_worktree_label",
    "redact_worktree_paths",
)
