"""Audit doc impact across git-changed paths.

Combines ``git diff`` listings with the resolver so operators can see,
per changed path, which docs they should read before commit. The
``--check-generated`` flag chains the file_conventions drift check
since the impact gate is the natural place to wedge it in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .catalog import CatalogContext, resolve_audit_documents
from .generate import run_generate_check
from .overlay import OverlayInfo, load_effective_catalog


IGNORED_PATH_PARTS = frozenset({".git", "__pycache__"})
IGNORED_SUFFIXES = frozenset({".pyc"})

# ``--no-renames`` is load-bearing, not cosmetic. Rename detection is on by
# default and is user-configurable (``diff.renames``), so without it the same
# worktree yields different listings on different machines, and a detected
# rename collapses to the destination path only — the source path silently
# vanishes from a gate whose whole job is to surface every affected path.
# Disabling detection reports a rename as delete + add, which is both the
# complete set and independent of operator config (Redmine #13919).
CACHED_DIFF_COMMAND = ("git", "diff", "--cached", "--name-only", "--no-renames")
WORKTREE_DIFF_COMMAND = ("git", "diff", "--name-only", "--no-renames")
UNTRACKED_COMMAND = ("git", "ls-files", "--others", "--exclude-standard")


def _should_skip_path(path: str) -> bool:
    parts = set(Path(path).parts)
    if parts & IGNORED_PATH_PARTS:
        return True
    return Path(path).suffix in IGNORED_SUFFIXES


def _dedup_changed(lines: list[str], seen: set[str], paths: list[str]) -> None:
    """Append cleaned, de-duplicated, non-ignored ``lines`` onto ``paths``."""
    for line in lines:
        path = line.strip()
        if not path or path in seen or _should_skip_path(path):
            continue
        seen.add(path)
        paths.append(path)


def git_changed_paths(
    repo_root: Path,
    *,
    staged: bool = False,
    all_changed: bool = False,
) -> list[str]:
    """Return repo-relative changed paths from git.

    Selection: ``all_changed`` is the deduplicated union of cached +
    unstaged + untracked, ``staged`` queries cached changes only, and
    neither flag is the unstaged-only default. ``all_changed`` wins when
    both flags are set, since it is a superset of ``staged``.

    ``all_changed`` previously queried unstaged + untracked only, so a
    fully staged worktree resolved to zero paths and the gate passed
    silently while the documented contract (and ``--staged``) said
    otherwise (Redmine #13919). Every source is therefore queried here,
    and the ``staged`` scope is unchanged.

    Order is deterministic: sources are queried in a fixed order, git
    lists each source sorted by path, and duplicates keep their
    first-seen position. Any git failure propagates as
    :class:`subprocess.CalledProcessError` — an unreadable source must
    fail the gate, never degrade to a short listing that reads as
    "nothing changed".
    """
    if all_changed:
        commands = [CACHED_DIFF_COMMAND, WORKTREE_DIFF_COMMAND, UNTRACKED_COMMAND]
    elif staged:
        commands = [CACHED_DIFF_COMMAND]
    else:
        commands = [WORKTREE_DIFF_COMMAND]

    paths: list[str] = []
    seen: set[str] = set()
    for command in commands:
        output = subprocess.check_output(list(command), cwd=repo_root, text=True)
        _dedup_changed(output.splitlines(), seen, paths)
    return paths


def git_changed_paths_since(repo_root: Path, base: str) -> list[str]:
    """Return repo-relative paths changed on the current branch since ``base``.

    Uses the three-dot ``git diff <base>...HEAD`` form, so only the changes
    introduced since the merge-base with ``base`` are listed — the set a pull
    request adds, not unrelated commits that landed on ``base`` meanwhile. This
    is the CI counterpart to :func:`git_changed_paths` (which reads the working
    tree / index); both feed the same impact resolver, so the focused-test
    selection is identical whether the diff is derived locally or in CI against
    the merge target. That parity is why ``--no-renames`` is passed here too:
    it keeps rename listing identical to :func:`git_changed_paths` and
    independent of the ambient ``diff.renames`` config. The same skip/dedup
    filtering is applied.
    """
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...HEAD"],
        cwd=repo_root,
        text=True,
    )
    paths: list[str] = []
    _dedup_changed(output.splitlines(), set(), paths)
    return paths


def audit_doc_impact_detailed(
    context: CatalogContext,
    *,
    staged: bool = False,
    all_changed: bool = False,
    include_local: bool = True,
) -> tuple[list[dict[str, Any]], OverlayInfo]:
    """Resolve docs for git-changed paths; report whether the overlay applied.

    The effective catalog merges the git-ignored ``catalog.local.yaml``
    overlay when present (Redmine #11819) so local-only docs surface for
    changed paths the same way public docs do.
    """
    catalog, overlay_info = load_effective_catalog(
        context, include_local=include_local
    )
    paths = git_changed_paths(context.repo_root, staged=staged, all_changed=all_changed)
    results = [resolve_audit_documents(context, catalog, path) for path in paths]
    return results, overlay_info


def audit_doc_impact(
    context: CatalogContext,
    *,
    staged: bool = False,
    all_changed: bool = False,
    include_local: bool = True,
) -> list[dict[str, Any]]:
    """Resolve docs for every git-changed path. Returns one record per path."""
    results, _ = audit_doc_impact_detailed(
        context,
        staged=staged,
        all_changed=all_changed,
        include_local=include_local,
    )
    return results


def run_audit_impact_check_generated(
    context: CatalogContext,
    output: Path | str | None = None,
) -> tuple[bool, Path, str]:
    """Convenience pass-through; see ``run_generate_check``."""
    return run_generate_check(context, output)
