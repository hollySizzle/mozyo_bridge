"""Lossless Git topology observation for workspace-alias authority (#15190).

The shared workspace-registry helper deliberately collapses every Git failure
to ``None`` for backwards compatibility.  Workspace aliasing cannot use that
tolerant contract: ``None`` used to mean both "positively not a repository" and
"the probe failed", which could authorize a cross-repository alias when Git was
missing, timed out, or returned malformed output.

This module keeps those outcomes distinct.  It never exposes Git stderr; callers
receive only the closed state vocabulary below and, for a successful Git probe,
resolved directory paths.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


GIT_PROBE_GIT = "git"
GIT_PROBE_NON_GIT = "non_git"
GIT_PROBE_UNAVAILABLE = "unavailable"

_NOT_REPOSITORY_PATTERNS = (
    re.compile(
        r"\Afatal: not a git repository \(or any of the parent directories\): "
        r"\.git\n?\Z"
    ),
    re.compile(
        r"\Afatal: not a git repository \(or any parent up to mount point "
        r"[^\r\n]+\)\nStopping at filesystem boundary "
        r"\(GIT_DISCOVERY_ACROSS_FILESYSTEM not set\)\.\n?\Z"
    ),
)


@dataclass(frozen=True)
class WorkspaceGitProbe:
    """One root's Git discovery result without raw subprocess diagnostics."""

    state: str
    git_dir: str = ""
    common_dir: str = ""


def _unavailable() -> WorkspaceGitProbe:
    return WorkspaceGitProbe(state=GIT_PROBE_UNAVAILABLE)


def _resolved_directory(raw: Path | str) -> Path | None:
    try:
        path = Path(raw).expanduser().resolve(strict=True)
        metadata = path.stat()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return path if stat.S_ISDIR(metadata.st_mode) else None


def _git_executable() -> str | None:
    raw = shutil.which("git")
    if not raw:
        return None
    try:
        path = Path(raw).resolve(strict=True)
        metadata = path.stat()
    except (OSError, RuntimeError):
        return None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        return None
    return str(path)


def _probe_environment() -> dict[str, str]:
    # Git discovery must describe ``root``, not ambient GIT_DIR / GIT_WORK_TREE
    # overrides.  Locale is pinned so the one positive non-repository outcome
    # can be recognized without treating arbitrary non-zero exits as non-Git.
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environ["LC_ALL"] = "C"
    environ["LANG"] = "C"
    return environ


def _canonical_non_repository(stderr: str) -> bool:
    return any(
        pattern.fullmatch(stderr) is not None
        for pattern in _NOT_REPOSITORY_PATTERNS
    )


def _git_discovery_markers_absent(root: Path) -> bool:
    """Return true only when every visible discovery-level ``.git`` is absent.

    An invalid, unreadable, or racy marker must not turn a malformed repository
    into a positive non-Git observation.  Any lstat failure other than ENOENT is
    therefore the same fail-closed result as a visible marker.
    """

    for directory in (root, *root.parents):
        try:
            os.lstat(directory / ".git")
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return True


def _resolved_git_directory(root: Path, raw: str) -> str | None:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        return None
    return str(resolved) if stat.S_ISDIR(metadata.st_mode) else None


def probe_workspace_git(root: Path | str) -> WorkspaceGitProbe:
    """Classify ``root`` as Git, positively non-Git, or unavailable.

    ``non_git`` requires three conjuncts: Git actually executed, returned its
    locale-pinned canonical non-repository result, and no ``.git`` discovery
    marker was visible from the root to the filesystem root.  Every other
    failure is ``unavailable`` and can never authorize an alias.
    """

    resolved_root = _resolved_directory(root)
    executable = _git_executable()
    if resolved_root is None or executable is None:
        return _unavailable()

    try:
        result = subprocess.run(
            [
                executable,
                "-C",
                str(resolved_root),
                "rev-parse",
                "--git-dir",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=_probe_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return _unavailable()

    returncode = getattr(result, "returncode", None)
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    if (
        type(returncode) is not int
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
    ):
        return _unavailable()

    if returncode != 0:
        if (
            returncode == 128
            and not stdout
            and _canonical_non_repository(stderr)
            and _git_discovery_markers_absent(resolved_root)
        ):
            return WorkspaceGitProbe(state=GIT_PROBE_NON_GIT)
        return _unavailable()

    if stderr:
        return _unavailable()
    lines = stdout.splitlines()
    if len(lines) != 2 or any(not line or line != line.strip() for line in lines):
        return _unavailable()
    git_dir = _resolved_git_directory(resolved_root, lines[0])
    common_dir = _resolved_git_directory(resolved_root, lines[1])
    if git_dir is None or common_dir is None:
        return _unavailable()
    return WorkspaceGitProbe(
        state=GIT_PROBE_GIT,
        git_dir=git_dir,
        common_dir=common_dir,
    )


__all__ = (
    "GIT_PROBE_GIT",
    "GIT_PROBE_NON_GIT",
    "GIT_PROBE_UNAVAILABLE",
    "WorkspaceGitProbe",
    "probe_workspace_git",
)
