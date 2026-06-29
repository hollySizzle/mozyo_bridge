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

    Selection mirrors the predecessor script: ``--staged`` queries
    cached changes only, ``--all-changed`` includes unstaged +
    untracked, neither flag is the unstaged-only default.
    """
    commands: list[list[str]] = []
    if staged:
        commands.append(["git", "diff", "--cached", "--name-only"])
    if all_changed:
        commands.append(["git", "diff", "--name-only"])
        commands.append(["git", "ls-files", "--others", "--exclude-standard"])
    if not commands:
        commands.append(["git", "diff", "--name-only"])

    paths: list[str] = []
    seen: set[str] = set()
    for command in commands:
        output = subprocess.check_output(command, cwd=repo_root, text=True)
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
    the merge target. The same skip/dedup filtering is applied.
    """
    output = subprocess.check_output(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
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
