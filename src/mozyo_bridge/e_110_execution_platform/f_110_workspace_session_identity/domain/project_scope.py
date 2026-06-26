"""Project-scoped workspace identity discovery (Redmine #12658, #12656).

A monorepo's Git repository root is the *workspace* (department / umbrella
identity); a self-describing project directory inside it — e.g.
``projects/<project-name>/`` — is a *project scope*. Project scope
is a routing / presentation layer **under** the workspace, never a replacement
for it: Git branches, commits, worktrees and the cross-workspace ``--target-repo``
gate stay anchored to the real repository root (see
``vibes/docs/logics/project-scoped-workspace-identity.md``).

This module is **pure**: it parses already-loaded ``project.yaml`` documents,
grades discovery candidates, derives the generated root discovery cache, detects
cache/source drift (fail-closed), and resolves which adopted project scope a cwd
belongs to. All filesystem and YAML I/O is injected by the application layer, so
the policy here is fully unit-testable without a repository on disk.

Two distinct outputs (design doc "Discovery Philosophy"):

- **discovered candidates** — every ``project.yaml`` carrying the
  :data:`PROJECT_SCHEMA_PREFIX` schema marker, whether or not it opts into
  runtime identity.
- **adopted project scopes** — candidates that explicitly set
  ``runtime_identity.enabled: true``. Only an adopted scope is used for cockpit /
  handoff routing surfaces; scanning avoids manual root-registry drift, but
  adoption stays explicit because pane routing is higher risk than an IDE hint.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping, Optional, Sequence

# A `project.yaml` is recognized as a mozyo project descriptor under either of two
# schema markers, so the existing GK monorepo router metadata is discoverable
# without a forced rewrite (Redmine #12658 j#66473):
#
#   A) native shape — top-level `schema: mozyo.project/v<n>` + top-level
#      `redmine_project` (the shape this US documents going forward);
#   B) GK router shape — top-level `schema_version: <int>` + a nested
#      `project:` mapping carrying `redmine_project` / `path` / `status`.
#
# A bare file named `project.yaml` carrying NEITHER marker (an unrelated tool's
# config) is ignored so discovery never adopts a directory it does not own
# (design doc "Runtime Identity Marker": a file's mere existence must not make a
# directory routable). Recognizing a shape is *discovery* only — ADOPTION still
# requires an explicit `runtime_identity.enabled: true` opt-in in BOTH shapes, so
# an existing `schema_version: 1` project is never silently treated as routable.
PROJECT_SCHEMA_PREFIX = "mozyo.project/"

# Generator provenance recorded in the root discovery cache so the generated
# block is visibly separated from human-owned routing policy.
DISCOVERY_CACHE_GENERATED_BY = "mozyo-bridge project discovery"

# Drift kinds surfaced by :func:`detect_cache_drift`. The runtime must surface
# drift instead of silently choosing the cache or the source value.
DRIFT_FINGERPRINT = "fingerprint_mismatch"  # cache entry exists but the source project.yaml changed
DRIFT_MISSING_SOURCE = "missing_source"  # cache entry points at a project that no longer discovers
DRIFT_UNCACHED_SOURCE = "uncached_source"  # adopted project has no cache entry
DRIFT_FIELD = "field_mismatch"  # cache scope/label/path disagrees with the source


def repo_relative_path(path: str, repo_root: str) -> Optional[str]:
    """Return ``path`` expressed relative to ``repo_root`` (POSIX, no leading ``./``).

    Pure string math over already-resolved absolute paths — the caller resolves
    symlinks / ``..`` before calling. Returns ``None`` when ``path`` is not at or
    below ``repo_root`` (so a project path can never leak an absolute private
    directory above the repo root). The repo root itself maps to ``"."``.
    """
    if not path or not repo_root:
        return None
    root = PurePosixPath(repo_root)
    target = PurePosixPath(path)
    try:
        rel = target.relative_to(root)
    except ValueError:
        return None
    text = rel.as_posix()
    return text or "."


def project_fingerprint(raw_text: str) -> str:
    """Stable content fingerprint of a ``project.yaml`` source (``sha256:<hex>``).

    Hashes the raw bytes so any change to the project-owned metadata changes the
    fingerprint; the generated cache records it so the runtime can detect that a
    cache entry no longer matches its source (drift) without re-parsing.
    """
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def cache_key(scope: str, path: str) -> str:
    """Deterministic, machine-portable cache key (``project:<scope>@<repo-rel-path>``).

    Stable across machines for the same repository layout (it carries only the
    project identifier and the repository-relative path, never an absolute private
    path) and changes only when the project identity or its path changes.
    """
    return f"project:{scope}@{path}"


@dataclass(frozen=True)
class ProjectCandidate:
    """One discovered ``project.yaml`` (schema-marked), adopted or not.

    ``path`` and ``source`` are repository-relative POSIX strings so no absolute
    private path is carried. ``runtime_identity_enabled`` grades adoption: only an
    enabled candidate becomes a routable :class:`ProjectScope`. ``fingerprint`` is
    the source content hash used for cache drift detection.
    """

    scope: str
    path: str
    source: str
    label: str
    runtime_identity_enabled: bool
    kind: str
    parent_workspace: Optional[str]
    workdir: str
    fingerprint: str

    @property
    def project_workdir(self) -> str:
        """Repository-relative working directory (``path`` joined with the marker workdir)."""
        rel = (self.workdir or ".").strip()
        if rel in ("", "."):
            return self.path
        joined = (PurePosixPath(self.path) / rel).as_posix()
        return joined

    def as_scope(self) -> "ProjectScope":
        return ProjectScope(
            scope=self.scope,
            path=self.path,
            label=self.label,
            workdir=self.project_workdir,
            parent_workspace=self.parent_workspace,
            source=self.source,
            fingerprint=self.fingerprint,
        )

    def cache_entry(self) -> dict:
        """The generator-owned discovery-cache entry for this candidate (design doc shape)."""
        return {
            "cache_key": cache_key(self.scope, self.path),
            "source": self.source,
            "path": self.path,
            "redmine_project": self.scope,
            "display_label": self.label,
            "runtime_identity_enabled": self.runtime_identity_enabled,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ProjectScope:
    """An adopted, routable project-scoped identity (design doc "Core Model").

    Carried alongside — never replacing — the workspace identity. ``path`` /
    ``workdir`` / ``source`` are repository-relative POSIX strings; ``label`` is
    the human-facing display name (round-trips arbitrary Unicode, e.g. Japanese).
    A :class:`ProjectScope` is a projection / routing scope: it carries no Git
    authority of its own.
    """

    scope: str
    path: str
    label: str
    workdir: str
    parent_workspace: Optional[str]
    source: str
    fingerprint: str

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "path": self.path,
            "label": self.label,
            "workdir": self.workdir,
            "parent_workspace": self.parent_workspace,
            "source": self.source,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class CacheDrift:
    """One detected disagreement between the generated cache and a live source."""

    kind: str
    cache_key: str
    detail: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "cache_key": self.cache_key, "detail": self.detail}


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def parse_project_document(
    document: Optional[Mapping[str, object]],
    *,
    path: str,
    source: str,
    raw_text: str,
) -> Optional[ProjectCandidate]:
    """Parse one loaded ``project.yaml`` into a :class:`ProjectCandidate`.

    Returns ``None`` when the document is not a mozyo project descriptor — it is
    not a mapping, or it carries neither recognized schema marker (native
    :data:`PROJECT_SCHEMA_PREFIX` ``schema`` nor a GK ``schema_version`` +
    nested ``project`` mapping). A descriptor without ``redmine_project`` is also
    rejected (the project identity is mandatory). ``path`` / ``source`` are
    repository-relative POSIX strings supplied by the caller; ``raw_text`` is the
    untouched file content used to compute the drift fingerprint.

    Adoption is explicit in BOTH shapes: ``runtime_identity.enabled`` must be
    truthy for the candidate to later become a routable scope. The runtime block
    is read from its natural location for the shape (nested under ``project`` for
    the GK shape, top-level otherwise), falling back to a top-level
    ``runtime_identity``. The display label prefers the runtime-identity block,
    then the descriptor's ``display_label``, then the project id, so a marked
    project is always labelable.
    """
    if not isinstance(document, Mapping):
        return None

    schema = str(document.get("schema") or "").strip()
    schema_version = document.get("schema_version")
    project_block = document.get("project")
    if schema.startswith(PROJECT_SCHEMA_PREFIX):
        # Native shape: identity + metadata live at the top level.
        meta: Mapping[str, object] = document
    elif schema_version is not None and isinstance(project_block, Mapping):
        # GK router shape: identity + metadata live under the `project` mapping.
        meta = project_block
    else:
        return None

    scope = str(meta.get("redmine_project") or "").strip()
    if not scope:
        return None

    # runtime_identity opt-in: prefer the descriptor-local block (top-level for
    # the native shape, nested under `project` for GK), fall back to a top-level
    # block. Absent / disabled -> discovered but never adopted.
    runtime = meta.get("runtime_identity")
    if not isinstance(runtime, Mapping):
        top_runtime = document.get("runtime_identity")
        runtime = top_runtime if isinstance(top_runtime, Mapping) else {}
    runtime_map: Mapping[str, object] = runtime
    enabled = _as_bool(runtime_map.get("enabled", False))
    kind = str(runtime_map.get("kind") or "project_scope").strip() or "project_scope"
    parent_workspace = (
        str(runtime_map.get("parent_workspace") or "").strip() or None
    )
    workdir = str(runtime_map.get("workdir") or ".").strip() or "."

    label = (
        str(runtime_map.get("display_label") or "").strip()
        or str(meta.get("display_label") or "").strip()
        or scope
    )

    return ProjectCandidate(
        scope=scope,
        path=path,
        source=source,
        label=label,
        runtime_identity_enabled=enabled,
        kind=kind,
        parent_workspace=parent_workspace,
        workdir=workdir,
        fingerprint=project_fingerprint(raw_text),
    )


def adopt_scopes(candidates: Sequence[ProjectCandidate]) -> list[ProjectScope]:
    """Project the explicitly-adopted candidates into routable scopes (sorted by path).

    Only ``runtime_identity_enabled`` candidates are adopted (design doc: scan is
    advisory, adoption is explicit). Sorting by repository-relative path keeps the
    output deterministic across scan orders.
    """
    adopted = [c.as_scope() for c in candidates if c.runtime_identity_enabled]
    return sorted(adopted, key=lambda s: s.path)


def build_discovery_cache(
    candidates: Sequence[ProjectCandidate],
    *,
    generated_at: str,
    generated_by: str = DISCOVERY_CACHE_GENERATED_BY,
) -> dict:
    """Build the generated root discovery cache block (design doc "Generated Root Cache").

    A write-back cache / review aid keyed by the stable repository-relative cache
    key. It records what the scanner derived from project-owned metadata; it is
    NOT a second authority for project-owned fields. ``generated_at`` is supplied
    by the caller (the domain stays clock-free). Entries are sorted by cache key
    for a stable, reviewable diff.
    """
    entries = sorted(
        (c.cache_entry() for c in candidates), key=lambda e: e["cache_key"]
    )
    return {
        "generated_by": generated_by,
        "generated_at": generated_at,
        "entries": entries,
    }


def detect_cache_drift(
    cache_entries: Sequence[Mapping[str, object]],
    candidates: Sequence[ProjectCandidate],
) -> list[CacheDrift]:
    """Compare a generated cache against freshly-discovered candidates (fail-closed).

    The cache is an acceleration aid and is NEVER allowed to silently override the
    local ``project.yaml``: when the cache and the live source disagree the
    runtime must surface drift rather than choosing whichever value is convenient.
    This returns every disagreement; an empty list means the cache faithfully
    reflects the current sources.

    Detected drift:

    - :data:`DRIFT_FINGERPRINT` — same project, changed source content.
    - :data:`DRIFT_FIELD` — cache scope / path / label disagrees with the source.
    - :data:`DRIFT_MISSING_SOURCE` — a cached project no longer discovers.
    - :data:`DRIFT_UNCACHED_SOURCE` — a discovered project has no cache entry.
    """
    by_key_candidate = {cache_key(c.scope, c.path): c for c in candidates}
    by_key_cache: dict[str, Mapping[str, object]] = {}
    drifts: list[CacheDrift] = []

    for entry in cache_entries:
        key = str(entry.get("cache_key") or "").strip()
        if not key:
            continue
        by_key_cache[key] = entry
        candidate = by_key_candidate.get(key)
        if candidate is None:
            drifts.append(
                CacheDrift(
                    DRIFT_MISSING_SOURCE,
                    key,
                    "cached project no longer discovers from a source project.yaml",
                )
            )
            continue
        cached_fp = str(entry.get("fingerprint") or "").strip()
        if cached_fp != candidate.fingerprint:
            drifts.append(
                CacheDrift(
                    DRIFT_FINGERPRINT,
                    key,
                    f"cache fingerprint {cached_fp or '<none>'} != source {candidate.fingerprint}",
                )
            )
        for field, cached_value, source_value in (
            ("redmine_project", entry.get("redmine_project"), candidate.scope),
            ("path", entry.get("path"), candidate.path),
            ("display_label", entry.get("display_label"), candidate.label),
        ):
            if str(cached_value or "") != str(source_value or ""):
                drifts.append(
                    CacheDrift(
                        DRIFT_FIELD,
                        key,
                        f"{field}: cache {cached_value!r} != source {source_value!r}",
                    )
                )

    for key, candidate in by_key_candidate.items():
        if key not in by_key_cache:
            drifts.append(
                CacheDrift(
                    DRIFT_UNCACHED_SOURCE,
                    key,
                    "discovered project has no generated cache entry",
                )
            )

    return sorted(drifts, key=lambda d: (d.cache_key, d.kind))


def resolve_project_scope_for_path(
    candidate_path: str,
    *,
    repo_root: str,
    adopted: Sequence[ProjectScope],
) -> Optional[ProjectScope]:
    """Resolve which adopted project scope a repo path belongs to, or ``None``.

    ``candidate_path`` is an absolute path (typically a pane cwd). It is matched
    against each adopted scope's repository-relative ``path``; the **deepest**
    (longest path) containing scope wins, so a nested project inside another
    project resolves to the inner one. A path at the workspace root (outside every
    project) resolves to ``None`` — the workspace has no project scope, preserving
    single-repo display compatibility.
    """
    rel = repo_relative_path(candidate_path, repo_root)
    if rel is None:
        return None
    rel_path = PurePosixPath(rel)
    best: Optional[ProjectScope] = None
    best_len = -1
    for scope in adopted:
        scope_path = PurePosixPath(scope.path)
        if rel_path == scope_path or scope_path in rel_path.parents:
            depth = len(scope_path.parts)
            if depth > best_len:
                best = scope
                best_len = depth
    return best


def path_under_project(candidate_path: str, *, repo_root: str, scope: ProjectScope) -> bool:
    """True when ``candidate_path`` is at or below the adopted ``scope``'s path.

    The cwd-side half of the handoff project-scope gate (design doc "Pane And
    Target Projection"): a target that claims a project scope but whose cwd is not
    under the expected project path must fail closed even when the Git repo gate
    passes.
    """
    rel = repo_relative_path(candidate_path, repo_root)
    if rel is None:
        return False
    rel_path = PurePosixPath(rel)
    scope_path = PurePosixPath(scope.path)
    return rel_path == scope_path or scope_path in rel_path.parents
