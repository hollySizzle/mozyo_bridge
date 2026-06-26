"""Project-scope discovery IO layer (Redmine #12658).

The thin *filesystem + YAML* layer over the pure
:mod:`mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope`
policy. The domain owns meaning (parsing, cache shape, drift, cwd resolution) and
does no IO; this layer:

- bounded-scans a repository root for schema-marked ``project.yaml`` files,
- loads each with ``yaml.safe_load`` (never ``yaml.load``) and hands the parsed
  mapping plus the raw text to the domain parser,
- reads the optional generated discovery cache from the root ``projects.yaml``,
- reconciles discovered candidates against that cache and returns adopted scopes
  together with any fail-closed drift.

Everything here is read-only and fail-soft for *display* surfaces: a malformed or
unreadable ``project.yaml`` is skipped (it cannot be a trustworthy routing source)
rather than aborting an ``agents targets`` / cockpit listing. The generated cache
is only an acceleration aid — adoption always re-derives from the live sources, so
a stale or missing cache never changes which scopes are adopted; it only feeds
:func:`detect_cache_drift` so the runtime can surface disagreement.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

import yaml

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
    CacheDrift,
    ProjectCandidate,
    ProjectScope,
    adopt_scopes,
    detect_cache_drift,
    parse_project_document,
    repo_relative_path,
    resolve_project_scope_for_path,
)

#: The project-owned descriptor filename scanned for under the repository root.
PROJECT_FILE_NAME = "project.yaml"

#: The root index / generated-cache file (human policy + generated discovery
#: cache live together, visibly separated — design doc "Generated Root Cache").
ROOT_INDEX_FILE_NAME = "projects.yaml"

#: Bounded-scan depth (directories below the repo root). A monorepo project lives
#: a few levels down (``projects/<name>/project.yaml``); a deep unbounded walk of
#: a large repo is both slow and a way to pick up vendored/test fixtures, so the
#: scan stops past this depth.
DEFAULT_MAX_DEPTH = 4

#: Directory names never descended into during the scan (build output, VCS
#: internals, dependency trees, caches). Keeps discovery cheap and avoids adopting
#: a fixture ``project.yaml`` vendored inside a dependency.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "build",
        "dist",
        ".eggs",
        "site-packages",
    }
)


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def discover_project_candidates(
    repo_root: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> list[ProjectCandidate]:
    """Bounded-scan ``repo_root`` for schema-marked ``project.yaml`` descriptors.

    Walks at most ``max_depth`` directories below the root, skipping VCS / build /
    dependency directories. Each ``project.yaml`` is loaded with
    ``yaml.safe_load`` and parsed by the domain; a non-mapping document, a missing
    schema marker, a malformed YAML file, or an unreadable file yields no
    candidate (fail-soft — a bad descriptor is never a routing source). The repo
    root's own ``project.yaml`` (depth 0) is included so a single-project repo can
    describe itself.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return []
    candidates: list[ProjectCandidate] = []
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root_str)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
        # Prune unwanted / hidden directories in place so os.walk never descends.
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        if PROJECT_FILE_NAME not in filenames:
            continue
        source_path = Path(dirpath) / PROJECT_FILE_NAME
        raw_text = _read_text(source_path)
        if raw_text is None:
            continue
        try:
            document = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            continue
        rel_dir = repo_relative_path(str(Path(dirpath).resolve()), str(root.resolve()))
        rel_source = repo_relative_path(str(source_path.resolve()), str(root.resolve()))
        if rel_dir is None or rel_source is None:
            continue
        candidate = parse_project_document(
            document,
            path=rel_dir,
            source=rel_source,
            raw_text=raw_text,
        )
        if candidate is not None:
            candidates.append(candidate)
    return sorted(candidates, key=lambda c: c.path)


def load_discovery_cache_entries(repo_root: str) -> list[dict]:
    """Read the generated ``discovery_cache.entries`` from the root ``projects.yaml``.

    Returns ``[]`` when the root index is absent, empty, malformed, or carries no
    generated cache block — the cache is optional and only feeds drift detection,
    so its absence is never an error.
    """
    index_path = Path(repo_root) / ROOT_INDEX_FILE_NAME
    raw_text = _read_text(index_path)
    if raw_text is None:
        return []
    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        return []
    if not isinstance(document, dict):
        return []
    cache = document.get("discovery_cache")
    if not isinstance(cache, dict):
        return []
    entries = cache.get("entries")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def resolve_project_scopes(
    repo_root: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> tuple[list[ProjectScope], list[CacheDrift]]:
    """Discover adopted project scopes for ``repo_root`` and any cache drift.

    Adoption always re-derives from the live ``project.yaml`` sources (the cache
    is never the authority). The generated cache, when present, is reconciled
    against the live candidates and any disagreement is returned as fail-closed
    :class:`CacheDrift` so the caller can surface it rather than trusting either
    side blindly.
    """
    candidates = discover_project_candidates(repo_root, max_depth=max_depth)
    adopted = adopt_scopes(candidates)
    cache_entries = load_discovery_cache_entries(repo_root)
    drift = detect_cache_drift(cache_entries, candidates) if cache_entries else []
    return adopted, drift


@lru_cache(maxsize=256)
def _cached_adopted_scopes(repo_root: str, max_depth: int) -> tuple[ProjectScope, ...]:
    adopted, drift = resolve_project_scopes(repo_root, max_depth=max_depth)
    if drift:
        # Fail closed on generated-cache drift (Redmine #12658 review j#66481
        # blocker 3): the design source requires a cache/source disagreement to be
        # SURFACED, never silently resolved to whichever value is convenient. The
        # runtime-facing project lookup therefore refuses to project ANY scope from
        # a repo whose `projects.yaml` discovery cache disagrees with its live
        # `project.yaml` sources, and emits a visible diagnostic (once per repo,
        # this fn is memoized). Adoption resumes once the operator regenerates the
        # cache. A repo with NO generated cache has no drift and is unaffected.
        detail = "; ".join(f"{d.kind}:{d.cache_key}" for d in drift[:5])
        print(
            f"warning: project discovery cache drift in {repo_root!r}; project "
            f"scope projection disabled until the generated `projects.yaml` "
            f"discovery_cache is regenerated ({detail})",
            file=sys.stderr,
        )
        return ()
    return tuple(adopted)


def adopted_scopes_for_repo(
    repo_root: Optional[str], *, max_depth: int = DEFAULT_MAX_DEPTH
) -> tuple[ProjectScope, ...]:
    """Memoized adopted-scope lookup for a repo root (read-only display hot path).

    ``agents targets`` / cockpit resolve the project scope for many panes sharing
    a handful of repo roots; this caches the per-root discovery so the bounded
    scan runs once per distinct root per process. Returns an empty tuple for a
    missing / unknown root, AND fails closed (empty) when the repo's generated
    discovery cache is in drift (Redmine #12658 review j#66481 blocker 3) so no
    surface silently projects a scope from a drifted cache.
    """
    if not repo_root:
        return ()
    return _cached_adopted_scopes(str(repo_root), max_depth)


def project_scope_for_cwd(
    cwd: Optional[str],
    repo_root: Optional[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Optional[ProjectScope]:
    """Resolve the adopted project scope that ``cwd`` belongs to (or ``None``).

    Convenience wrapper over :func:`adopted_scopes_for_repo` +
    :func:`resolve_project_scope_for_path` for the cockpit / discovery / handoff
    call sites: a pane whose cwd is the repo root (or outside every project)
    resolves to ``None`` so single-repo workspaces keep their existing display.
    """
    if not cwd or not repo_root:
        return None
    adopted = adopted_scopes_for_repo(repo_root, max_depth=max_depth)
    if not adopted:
        return None
    return resolve_project_scope_for_path(
        str(Path(cwd).expanduser().resolve()),
        repo_root=str(Path(repo_root).expanduser().resolve()),
        adopted=adopted,
    )


def clear_discovery_cache() -> None:
    """Drop the memoized per-repo discovery (tests / long-running daemons)."""
    _cached_adopted_scopes.cache_clear()
