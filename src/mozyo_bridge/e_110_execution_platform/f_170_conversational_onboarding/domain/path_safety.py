"""Pure path-safety classifier for onboarding preflight (Redmine #13508).

Classifies a filesystem root — the canonical cwd a human ran ``mozyo`` in —
into the safety facts the onboarding orchestrator gates on *before any LLM is
started*. It is deliberately separated from the orchestrator so it can be
audited independently (the task-level exception carved out under #13498): this
module makes no decision about intent, preset, plan, or mutation, and never
starts a model. It only answers three closed questions about a path:

- ``root_kind``    — is this a Git worktree or a non-Git directory?
- ``path_risk``    — is it a normal root, the home directory (hard block),
  a sync/cloud folder (caution), or identity-ambiguous (hard block)?
- ``adoption_marker`` — which mozyo adoption evidence does the root carry?

The classifier resolves the raw path to a canonical, symlink-free identity and
**fails closed to ``ambiguous`` when that identity cannot be uniquely pinned**
(dangling symlink, symlink loop, unreadable path). It never falls back to
``normal`` on doubt — a path whose identity we cannot resolve must stop the
flow, not proceed as if safe.

The only ambient inputs are ``home`` and the platform sync roots; both are
injectable so the matrix (home / normal / sync / symlink-ambiguity / Git /
non-Git) is testable against ``tempfile`` fixtures without depending on the
runner's real home directory. The module performs read-only filesystem
``exists``/``resolve`` probes only; it mutates nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from mozyo_bridge.shared.paths import (
    REPO_LOCAL_CONFIG_MARKER,
    WORKSPACE_MARKERS,
)

__all__ = (
    "ROOT_KIND_GIT",
    "ROOT_KIND_NON_GIT",
    "PATH_RISK_NORMAL",
    "PATH_RISK_HOME",
    "PATH_RISK_SYNC_OR_CLOUD",
    "PATH_RISK_AMBIGUOUS",
    "ADOPTION_ABSENT",
    "ADOPTION_CONFIG",
    "ADOPTION_SCAFFOLD",
    "ADOPTION_WORKSPACE_ANCHOR",
    "ADOPTION_ONBOARDING_RECEIPT",
    "ONBOARDING_RECEIPT_MARKER",
    "PathSafety",
    "platform_sync_roots",
    "classify_path_safety",
)

# --- closed classification vocabularies --------------------------------------

ROOT_KIND_GIT = "git"
ROOT_KIND_NON_GIT = "non_git"

PATH_RISK_NORMAL = "normal"
PATH_RISK_HOME = "home"
PATH_RISK_SYNC_OR_CLOUD = "sync_or_cloud"
PATH_RISK_AMBIGUOUS = "ambiguous"

ADOPTION_ABSENT = "absent"
ADOPTION_CONFIG = "config"
ADOPTION_SCAFFOLD = "scaffold"
ADOPTION_WORKSPACE_ANCHOR = "workspace_anchor"
ADOPTION_ONBOARDING_RECEIPT = "onboarding_receipt"

# The onboarding receipt is the net-new adoption marker this feature introduces
# (the credential-free record of an in-progress / complete adoption). It is the
# most specific adoption evidence, so it is checked before the pre-existing
# config / scaffold / workspace-anchor markers.
ONBOARDING_RECEIPT_MARKER = ".mozyo-bridge/onboarding-receipt.json"

# Ordered adoption-marker probe: most-specific evidence first. Each entry is a
# (repo-relative marker path, classification) pair; the first present marker
# wins. The workspace anchors (new + legacy name) both classify as
# ``workspace_anchor`` — matching ``shared.paths.WORKSPACE_MARKERS``.
_SCAFFOLD_MARKER = ".mozyo-bridge/scaffold.json"
_ADOPTION_MARKER_PROBES: tuple[tuple[str, str], ...] = (
    (ONBOARDING_RECEIPT_MARKER, ADOPTION_ONBOARDING_RECEIPT),
    (REPO_LOCAL_CONFIG_MARKER, ADOPTION_CONFIG),
    (_SCAFFOLD_MARKER, ADOPTION_SCAFFOLD),
) + tuple(
    (marker, ADOPTION_WORKSPACE_ANCHOR)
    for marker in WORKSPACE_MARKERS
    if marker != _SCAFFOLD_MARKER
)

# Directory names that, when they appear as a path component, mark a sync/cloud
# provider folder regardless of platform-specific mount roots. Name-based
# detection is a *supplement* to the platform sync roots below, not a
# replacement (the spec requires more than known provider names).
_SYNC_PROVIDER_COMPONENTS: frozenset[str] = frozenset(
    {
        "CloudStorage",  # macOS File Provider root (Google Drive, Dropbox, OneDrive, Box)
        "Mobile Documents",  # macOS iCloud Drive backing dir (com~apple~CloudDocs)
        "Google Drive",
        "GoogleDrive",
        "My Drive",
        "Dropbox",
        "OneDrive",
        "Box",
        "Box Sync",
        "iCloud Drive",
        "iCloudDrive",
    }
)


@dataclass(frozen=True)
class PathSafety:
    """The closed safety classification of a canonical onboarding root.

    ``root`` is the canonical (symlink-free) path when identity resolved, or the
    best-effort expanded raw path when ``path_risk`` is ``ambiguous`` (identity
    could not be pinned). ``notes`` carries short, non-secret evidence strings
    for user-facing rendering (never a transcript or a path secret beyond the
    root itself).
    """

    root: Path
    root_kind: str
    path_risk: str
    adoption_marker: str
    notes: tuple[str, ...] = ()

    @property
    def is_hard_block(self) -> bool:
        """True when the path itself is a model-pre-launch hard block.

        Home and ambiguous identity are hard blocks the model cannot clear
        (per the spec's ``hard_block`` list). ``sync_or_cloud`` is a caution,
        not a block, so it is deliberately excluded here.
        """
        return self.path_risk in (PATH_RISK_HOME, PATH_RISK_AMBIGUOUS)

    @property
    def requires_caution_ack(self) -> bool:
        """True when a human caution acknowledgement is required to proceed."""
        return self.path_risk == PATH_RISK_SYNC_OR_CLOUD


def platform_sync_roots(home: Path) -> tuple[Path, ...]:
    """Default sync/cloud mount roots for ``home``, deterministic and injectable.

    Returns the platform-specific directories under which a path is treated as
    living in a synced / cloud-backed folder. Kept injectable (callers pass an
    explicit ``home``) so classification does not read the ambient environment
    and stays reproducible in tests. Non-existent roots are still returned —
    prefix matching is a pure path operation and does not require the root to
    exist on the current host.
    """
    return (
        home / "Library" / "CloudStorage",
        home / "Library" / "Mobile Documents",
        home / "Google Drive",
        home / "GoogleDrive",
        home / "Dropbox",
        home / "OneDrive",
        home / "Box",
        home / "Box Sync",
    )


def _canonical_root(raw: str | Path) -> Path | None:
    """Resolve ``raw`` to a canonical, symlink-free directory identity.

    Returns ``None`` when identity cannot be uniquely pinned — a dangling
    symlink, a symlink loop, an unreadable path, or a target that is not a
    directory. ``None`` is the classifier's ``ambiguous`` signal; the caller
    never treats an unresolved path as ``normal``.
    """
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_dir():
        return None
    return resolved


def _within(root: Path, ancestor: Path) -> bool:
    """True when ``root`` is ``ancestor`` or lives under it (pure path check)."""
    try:
        root.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _classify_risk(
    root: Path, *, home: Path, sync_roots: Sequence[Path]
) -> tuple[str, tuple[str, ...]]:
    """Classify ``path_risk`` for an already-canonical ``root``.

    Precedence is home → sync/cloud → normal. Home is checked first because it
    is a hard block regardless of whether home itself happens to sit under a
    sync root. ``ambiguous`` is not decided here — it is signalled upstream by
    an unresolved canonical root.
    """
    if root == home:
        return PATH_RISK_HOME, (f"root is the home directory {root}",)

    for sync_root in sync_roots:
        if _within(root, sync_root):
            return (
                PATH_RISK_SYNC_OR_CLOUD,
                (f"root is under sync/cloud mount {sync_root}",),
            )

    components = set(Path(root).parts)
    hit = components & _SYNC_PROVIDER_COMPONENTS
    if hit:
        return (
            PATH_RISK_SYNC_OR_CLOUD,
            (f"root path contains sync/cloud provider folder {sorted(hit)[0]!r}",),
        )

    return PATH_RISK_NORMAL, ()


def _classify_adoption(root: Path) -> str:
    """Return the most-specific adoption marker present under ``root``."""
    for marker, classification in _ADOPTION_MARKER_PROBES:
        if (root / marker).exists():
            return classification
    return ADOPTION_ABSENT


def classify_path_safety(
    raw_root: str | Path,
    *,
    home: Path,
    sync_roots: Sequence[Path] | None = None,
) -> PathSafety:
    """Classify ``raw_root`` into a closed :class:`PathSafety`.

    ``home`` and ``sync_roots`` are injectable (``sync_roots`` defaults to
    :func:`platform_sync_roots` for ``home``) so the classification is a pure
    function of its inputs. The steps:

    1. Resolve to a canonical, symlink-free directory identity. If that fails,
       return ``path_risk=ambiguous`` immediately (fail closed — never
       ``normal`` on doubt). ``root_kind`` / ``adoption_marker`` cannot be
       trusted for an unresolved identity, so they are reported as
       ``non_git`` / ``absent`` placeholders alongside the ambiguous verdict.
    2. Classify ``root_kind`` from a ``.git`` entry at the canonical root.
    3. Classify ``path_risk`` (home → sync/cloud → normal).
    4. Classify ``adoption_marker`` from the ordered marker probe.
    """
    # Resolve ``home`` to the same canonical form as the root so the home
    # comparison is symlink-invariant (e.g. macOS ``/var`` -> ``/private/var``).
    # ``resolve()`` is non-strict, so a not-yet-existing home still normalises
    # its existing prefix.
    home = Path(home).expanduser().resolve()
    canonical = _canonical_root(raw_root)
    if canonical is None:
        best_effort = Path(raw_root).expanduser()
        return PathSafety(
            root=best_effort,
            root_kind=ROOT_KIND_NON_GIT,
            path_risk=PATH_RISK_AMBIGUOUS,
            adoption_marker=ADOPTION_ABSENT,
            notes=(
                "canonical cwd / symlink / mount identity could not be uniquely "
                f"resolved for {best_effort}; refusing to classify as a safe root",
            ),
        )

    if sync_roots is None:
        sync_roots = platform_sync_roots(home)
    # Normalise sync roots to the same canonical form as the root so prefix
    # matching is symlink-invariant.
    resolved_sync_roots = tuple(Path(s).expanduser().resolve() for s in sync_roots)

    root_kind = ROOT_KIND_GIT if (canonical / ".git").exists() else ROOT_KIND_NON_GIT
    path_risk, risk_notes = _classify_risk(
        canonical, home=home, sync_roots=resolved_sync_roots
    )
    adoption_marker = _classify_adoption(canonical)

    return PathSafety(
        root=canonical,
        root_kind=root_kind,
        path_risk=path_risk,
        adoption_marker=adoption_marker,
        notes=risk_notes,
    )
