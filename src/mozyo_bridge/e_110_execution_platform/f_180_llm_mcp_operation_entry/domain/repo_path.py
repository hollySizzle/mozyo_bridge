"""The repo-relative path contract for tool arguments (pure, review j#102186).

``docs_resolve`` publishes its ``paths`` as *repo-relative*, but the schema
validator only checked "non-empty string" and the handler passed the value
straight through. Review finding_3 showed the two consequences:

- an **absolute** path reached the catalog resolver, which raised a ``ValueError``
  naming the server's own absolute repo root, and the handler put that exception
  text into the structured result — leaking a private host path to a caller that
  never knew it (j#102124 boundary: no private path in a tool result);
- ``../outside/private.py`` was echoed back as a normal resolution, so the tool
  answered questions about paths outside the repo it is scoped to.

So the contract is enforced here, before any resolver sees the value, and the
refusals are **fixed tokens** rather than exception text. The rule:

- reject an absolute path (POSIX ``/x`` or Windows-shaped ``C:\\x`` / ``\\\\host``);
- reject any path that escapes the repo root once ``.`` / ``..`` are folded;
- reject a path that is not usable text;
- otherwise return the normalized POSIX repo-relative form.

Normalization is **lexical**. It deliberately does not touch the filesystem: a
resolver that follows a symlink out of the repo is a different concern (this tool
only reads a catalog keyed by path), and calling ``resolve()`` here would both
require the path to exist and reintroduce absolute host paths into the code path
this module exists to keep them out of.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Sequence

# --- refusal vocabulary (closed) ------------------------------------------- #

PATH_NOT_TEXT = "not_text"
PATH_ABSOLUTE = "absolute"
PATH_ESCAPES_REPO = "escapes_repo"
PATH_EMPTY = "empty"

PATH_REFUSALS = frozenset(
    {PATH_NOT_TEXT, PATH_ABSOLUTE, PATH_ESCAPES_REPO, PATH_EMPTY}
)

#: Fixed, caller-facing explanations. Keyed by refusal token so the message can
#: never be an exception's text — the mechanism that leaked the server's repo root.
PATH_REFUSAL_REASONS = {
    PATH_NOT_TEXT: "must be a string",
    PATH_ABSOLUTE: "must be repo-relative, not absolute",
    PATH_ESCAPES_REPO: "must stay inside the repo; it resolves outside the repo root",
    PATH_EMPTY: "must not be empty",
}

#: A Windows drive-qualified or UNC path. Refused on every platform: this server
#: is scoped to one repo root, and a drive-absolute path is not relative to it.
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


@dataclass(frozen=True)
class RejectedPath:
    """One refused path, with the closed reason token.

    ``supplied`` echoes the caller's own input — never a server-side resolution of
    it. The caller already knows what it sent, so echoing it leaks nothing, while
    any *resolved* form would carry the repo root this module exists to withhold.
    """

    supplied: str
    reason: str

    @property
    def message(self) -> str:
        return PATH_REFUSAL_REASONS.get(self.reason, "is not an acceptable path")

    def as_payload(self) -> dict:
        return {
            "path": self.supplied,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass(frozen=True)
class NormalizedPaths:
    """The accepted repo-relative paths plus every refusal."""

    accepted: tuple
    rejected: tuple

    @property
    def ok(self) -> bool:
        return not self.rejected


def normalize_repo_relative(value: object) -> "str | RejectedPath":
    """Normalize one value to a repo-relative POSIX path, or refuse it."""
    if not isinstance(value, str):
        return RejectedPath(supplied=_echo(value), reason=PATH_NOT_TEXT)
    text = value.strip()
    if not text:
        return RejectedPath(supplied=text, reason=PATH_EMPTY)
    # Accept Windows-style separators as input, but judge the result as POSIX.
    unified = text.replace("\\", "/")
    if unified.startswith("/") or _WINDOWS_ABSOLUTE.match(text):
        return RejectedPath(supplied=text, reason=PATH_ABSOLUTE)
    normalized = posixpath.normpath(unified)
    if normalized == ".." or normalized.startswith("../"):
        return RejectedPath(supplied=text, reason=PATH_ESCAPES_REPO)
    if normalized in (".", ""):
        return RejectedPath(supplied=text, reason=PATH_EMPTY)
    return normalized


def normalize_repo_relative_paths(values: Sequence[object]) -> NormalizedPaths:
    """Normalize every path, collecting **all** refusals rather than the first.

    Reporting every bad path at once lets a caller repair the call in one round,
    matching how the schema validator reports argument violations.
    """
    accepted: list = []
    rejected: list = []
    for value in values:
        outcome = normalize_repo_relative(value)
        if isinstance(outcome, RejectedPath):
            rejected.append(outcome)
        else:
            accepted.append(outcome)
    return NormalizedPaths(accepted=tuple(accepted), rejected=tuple(rejected))


def _echo(value: object) -> str:
    """A short, safe echo of a non-string input (type only, never its content)."""
    return f"<{type(value).__name__}>"


__all__ = (
    "NormalizedPaths",
    "PATH_ABSOLUTE",
    "PATH_EMPTY",
    "PATH_ESCAPES_REPO",
    "PATH_NOT_TEXT",
    "PATH_REFUSALS",
    "PATH_REFUSAL_REASONS",
    "RejectedPath",
    "normalize_repo_relative",
    "normalize_repo_relative_paths",
)
