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
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

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
from mozyo_bridge.shared.paths import infer_git_worktree_root

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


#: Seconds after which a still-running bounded scan emits one ``scan_slow``
#: progress event (Redmine #12985). High enough that the common fast scan never
#: emits it; low enough that an operator watching a large-root walk learns the
#: process is alive well before losing patience.
DEFAULT_SLOW_SCAN_NOTICE_SECONDS = 5.0

#: :attr:`ScanProgressEvent.kind` vocabulary. ``scan_start`` fires when a live
#: bounded scan begins for a root (a memoized lookup emits nothing), ``scan_slow``
#: fires at most once per scan after :data:`DEFAULT_SLOW_SCAN_NOTICE_SECONDS`
#: (or the ``slow_after`` the listener was installed with), and ``scan_done``
#: fires when the scan returns.
SCAN_PROGRESS_START = "scan_start"
SCAN_PROGRESS_SLOW = "scan_slow"
SCAN_PROGRESS_DONE = "scan_done"


@dataclass(frozen=True)
class ScanProgressEvent:
    """One observable step of a per-root bounded project-scope scan (#12985).

    A display/diagnostic value only — never a routing or adoption input.
    ``adopted_count`` is populated on ``scan_done`` (the number of scopes the
    finished scan adopted; ``0`` also covers the drift-fail-closed outcome).
    """

    kind: str
    repo_root: str
    elapsed_seconds: float
    adopted_count: Optional[int] = None


ScanProgressListener = Callable[[ScanProgressEvent], None]

_progress_listener: Optional[ScanProgressListener] = None
_slow_notice_seconds: float = DEFAULT_SLOW_SCAN_NOTICE_SECONDS


@contextmanager
def scan_progress(
    listener: ScanProgressListener,
    *,
    slow_after: float = DEFAULT_SLOW_SCAN_NOTICE_SECONDS,
) -> Iterator[None]:
    """Install ``listener`` for scan progress events inside the block (#12985).

    The injectable seam the presentation layer (``agents targets``) uses to
    surface the previously-silent live scan: discovery itself never writes to
    stderr for progress, and with no listener installed (the default, and every
    other caller of this module) nothing is emitted — so the cockpit / handoff
    shared paths and the memoized cache-hit path stay exactly as quiet as
    before. Listener exceptions are swallowed: progress display must never
    change discovery results. Not thread-safe by design — the CLI installs it
    around one discovery pass on the main thread (the ``scan_slow`` timer
    thread only *reads* the installed listener).
    """
    global _progress_listener, _slow_notice_seconds
    previous = (_progress_listener, _slow_notice_seconds)
    _progress_listener = listener
    _slow_notice_seconds = slow_after
    try:
        yield
    finally:
        _progress_listener, _slow_notice_seconds = previous


def _emit_scan_progress(event: ScanProgressEvent) -> None:
    listener = _progress_listener
    if listener is None:
        return
    try:
        listener(event)
    except Exception:  # noqa: BLE001 - progress display must never break discovery
        pass


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
    # Progress events (#12985) fire only on a real (non-memoized) scan: entering
    # this body means the lru cache missed, so a cache hit stays silent for free.
    # The scan_slow timer arms only when a listener is installed and always
    # cancels on the way out; it is a daemon thread so a hung walk can never keep
    # the process alive on its own.
    started = time.monotonic()
    _emit_scan_progress(ScanProgressEvent(SCAN_PROGRESS_START, repo_root, 0.0))
    slow_timer: Optional[threading.Timer] = None
    if _progress_listener is not None:
        slow_timer = threading.Timer(
            _slow_notice_seconds,
            lambda: _emit_scan_progress(
                ScanProgressEvent(
                    SCAN_PROGRESS_SLOW, repo_root, time.monotonic() - started
                )
            ),
        )
        slow_timer.daemon = True
        slow_timer.start()
    try:
        adopted, drift = resolve_project_scopes(repo_root, max_depth=max_depth)
    finally:
        if slow_timer is not None:
            slow_timer.cancel()
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
        result: tuple[ProjectScope, ...] = ()
    else:
        result = tuple(adopted)
    _emit_scan_progress(
        ScanProgressEvent(
            SCAN_PROGRESS_DONE,
            repo_root,
            time.monotonic() - started,
            adopted_count=len(result),
        )
    )
    return result


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


def resolve_workspace_root(cwd: Optional[str]) -> Optional[str]:
    """Resolve the workspace root for ``cwd``, preferring the Git worktree root.

    Project-scoped identity (Redmine #12658 j#66499): a monorepo project
    subdirectory may carry its own ``.mozyo-bridge/scaffold.json``, at which the
    marker-based ``infer_repo_root`` would stop — collapsing the workspace
    identity onto the project. The Git worktree root is the workspace, so it is
    preferred; the marker resolver is the fallback ONLY when no Git root is
    reachable above (a genuinely non-git scaffolded workspace, Redmine #11301).
    Returns ``None`` when neither resolves.
    """
    if not cwd:
        return None
    git_root = infer_git_worktree_root(cwd)
    if git_root is not None:
        return str(git_root)
    # No Git root above: fall back to the marker resolver so a non-git scaffolded
    # workspace still resolves to its scaffold root (behavior preserved).
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
        infer_repo_root,
    )

    return infer_repo_root(cwd)


def project_scope_for_cwd(
    cwd: Optional[str],
    repo_root: Optional[str] = None,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Optional[ProjectScope]:
    """Resolve the adopted project scope that ``cwd`` belongs to (or ``None``).

    Convenience wrapper over :func:`adopted_scopes_for_repo` +
    :func:`resolve_project_scope_for_path` for the cockpit / discovery / handoff
    call sites. ``repo_root`` is the workspace root the project path is taken
    relative to; when omitted (or when it is a nested project-local scaffold root)
    the Git-worktree-preferring :func:`resolve_workspace_root` is used so the
    project path stays repo-relative to the real Git root (Redmine #12658
    j#66499). A pane whose cwd is the repo root (or outside every adopted project)
    resolves to ``None`` so single-repo workspaces keep their existing display.
    """
    if not cwd:
        return None
    # Prefer the Git worktree root so a nested project-local scaffold marker does
    # not collapse the workspace onto the project. An explicitly-passed repo_root
    # is honored only when it is at or above the resolved Git root (i.e. not a
    # nested scaffold subdir); otherwise the Git root wins.
    git_pref = resolve_workspace_root(cwd)
    effective_root = git_pref or (
        str(Path(repo_root).expanduser().resolve()) if repo_root else None
    )
    if not effective_root:
        return None
    adopted = adopted_scopes_for_repo(effective_root, max_depth=max_depth)
    if not adopted:
        return None
    return resolve_project_scope_for_path(
        str(Path(cwd).expanduser().resolve()),
        repo_root=str(Path(effective_root).expanduser().resolve()),
        adopted=adopted,
    )


def clear_discovery_cache() -> None:
    """Drop the memoized per-repo discovery (tests / long-running daemons)."""
    _cached_adopted_scopes.cache_clear()
