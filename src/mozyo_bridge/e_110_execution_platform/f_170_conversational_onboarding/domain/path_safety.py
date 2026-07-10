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

``root_kind`` is resolved by Git **worktree ancestry** (``infer_git_worktree_root``),
not just a ``.git`` entry directly under the root: a cwd nested inside a Git
worktree — or a linked-worktree ``.git`` *file* — is ``git`` (Redmine #13508 F2).

Sync/cloud detection combines three signals (Redmine #13508 F1): the injected
platform sync roots (path prefix), known provider directory names, and — when a
:class:`MountProbe` is supplied — **mount metadata**. Mount metadata that is
``unavailable`` or ``conflicting`` fails closed to ``ambiguous`` (never
``normal``). The probe is a Port: the domain consumes a closed
:class:`MountFacts` value and never runs an ambient OS command itself.

The only ambient inputs are ``home``, the platform sync roots, and the injected
mount probe; all are injectable so the matrix (home / normal / sync / mount-
metadata / symlink-ambiguity / Git-ancestry / non-Git) is testable against
``tempfile`` fixtures and fakes without depending on the runner's real host. The
module performs read-only filesystem ``exists``/``resolve`` probes only; it
mutates nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from mozyo_bridge.shared.paths import (
    REPO_LOCAL_CONFIG_MARKER,
    WORKSPACE_MARKERS,
    infer_git_worktree_root,
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
    "MOUNT_LOCAL",
    "MOUNT_SYNC_CLOUD",
    "MOUNT_NETWORK",
    "MOUNT_UNAVAILABLE",
    "MOUNT_CONFLICTING",
    "MountFacts",
    "MountProbe",
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

# --- mount metadata (Redmine #13508 F1) --------------------------------------

#: Closed mount-classification vocabulary the domain consumes. ``sync_cloud`` and
#: ``network`` are treated as sync/cloud risk; ``local`` is a normal on-disk
#: mount; ``unavailable`` (the probe could not read metadata) and ``conflicting``
#: (the metadata disagrees with itself / the path signals) both fail closed to
#: ``ambiguous`` — never ``normal``.
MOUNT_LOCAL = "local"
MOUNT_SYNC_CLOUD = "sync_cloud"
MOUNT_NETWORK = "network"
MOUNT_UNAVAILABLE = "unavailable"
MOUNT_CONFLICTING = "conflicting"

_MOUNT_SYNC_STATES: frozenset[str] = frozenset({MOUNT_SYNC_CLOUD, MOUNT_NETWORK})
# The full closed vocabulary. A ``MountFacts`` whose state is outside this set is
# invalid and is treated as ``unavailable`` (fail closed, never ``normal``).
_ALL_MOUNT_STATES: frozenset[str] = frozenset(
    {MOUNT_LOCAL, MOUNT_SYNC_CLOUD, MOUNT_NETWORK, MOUNT_UNAVAILABLE, MOUNT_CONFLICTING}
)


@dataclass(frozen=True)
class MountFacts:
    """A closed, already-probed mount classification for a canonical root.

    Produced by an application-layer :class:`MountProbe` adapter (which may read
    ``statfs`` / mount tables / provider markers) and consumed by the pure
    classifier. ``detail`` is a short, non-secret evidence string for rendering.
    """

    state: str
    source: str = ""
    detail: str = ""


@runtime_checkable
class MountProbe(Protocol):
    """Port: classify a canonical path's mount into closed :class:`MountFacts`.

    The domain never runs an ambient OS command; an application adapter
    implements this and the classifier only consumes its closed result.
    """

    def classify_mount(self, path: Path) -> MountFacts:  # pragma: no cover - protocol
        ...


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


def _resolve_mount_facts(
    canonical: Path,
    *,
    mount_facts: MountFacts | None,
    mount_probe: MountProbe | None,
) -> MountFacts:
    """Resolve the mount facts for ``canonical``, always fail-closed.

    Precedence: an explicit pre-probed ``mount_facts`` (the pure boundary — the
    application adapter probes and hands the domain a closed fact) wins; else a
    ``mount_probe`` is called *inside a guard* that converts any exception into a
    credential-free ``MOUNT_UNAVAILABLE`` (F5 — an adapter failure never escapes
    the hard gate); else, with neither supplied, mount metadata is *unavailable*
    (F3 — a missing probe is not evidence of a local mount). Any result whose
    ``state`` is outside the closed vocabulary is likewise ``MOUNT_UNAVAILABLE``
    (F3 — an unknown state never falls through to ``normal``).
    """
    facts = mount_facts
    if facts is None and mount_probe is not None:
        try:
            facts = mount_probe.classify_mount(canonical)
        except Exception as exc:  # noqa: BLE001 - never leak past the hard gate
            return MountFacts(
                state=MOUNT_UNAVAILABLE,
                source="probe_error",
                detail=f"mount probe raised {type(exc).__name__}",
            )
    if facts is None:
        return MountFacts(
            state=MOUNT_UNAVAILABLE,
            source="absent",
            detail="no mount facts / probe supplied",
        )
    if not isinstance(facts, MountFacts) or facts.state not in _ALL_MOUNT_STATES:
        return MountFacts(
            state=MOUNT_UNAVAILABLE,
            source="invalid",
            detail=f"mount facts state {getattr(facts, 'state', facts)!r} is not recognised",
        )
    return facts


def _classify_risk(
    root: Path,
    *,
    home: Path,
    sync_roots: Sequence[Path],
    mount_facts: MountFacts,
) -> tuple[str, tuple[str, ...]]:
    """Classify ``path_risk`` for an already-canonical ``root``.

    ``mount_facts`` is always a valid, resolved :class:`MountFacts` (see
    :func:`_resolve_mount_facts`). Precedence, chosen so no branch can reach
    ``normal`` without positive local-mount evidence:

    1. ``home`` — hard block regardless of anything else.
    2. ``conflicting`` mount metadata — the identity actively disagrees with
       itself / the path signals, so it is ``ambiguous`` (hard block) *before*
       any positive path signal can weaken it to a mere caution (F4).
    3. path-based sync signal (sync-root prefix / provider name) — an
       authoritative positive sync signal → ``sync_or_cloud``. This survives
       ``unavailable`` (a missing probe does not contradict a name/prefix hit).
    4. ``unavailable`` mount metadata (with no positive path signal) — the
       sync-vs-local question is undeterminable → ``ambiguous`` (F3).
    5. ``sync_cloud`` / ``network`` mount → ``sync_or_cloud``.
    6. ``local`` mount → ``normal`` (the only path to ``normal`` — it requires
       positive local-mount evidence).
    7. defensive fallthrough → ``ambiguous`` (never ``normal``).
    """
    if root == home:
        return PATH_RISK_HOME, (f"root is the home directory {root}",)

    if mount_facts.state == MOUNT_CONFLICTING:
        return (
            PATH_RISK_AMBIGUOUS,
            (
                f"mount metadata is conflicting ({mount_facts.detail or 'no detail'}); "
                "the root identity is not unique, so failing closed to ambiguous",
            ),
        )

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

    if mount_facts.state == MOUNT_UNAVAILABLE:
        return (
            PATH_RISK_AMBIGUOUS,
            (
                f"mount metadata is unavailable ({mount_facts.detail or 'no detail'}); "
                "cannot determine sync/cloud vs local, so failing closed to "
                "ambiguous (never normal)",
            ),
        )

    if mount_facts.state in _MOUNT_SYNC_STATES:
        return (
            PATH_RISK_SYNC_OR_CLOUD,
            (f"mount metadata classifies the root as {mount_facts.state}",),
        )

    if mount_facts.state == MOUNT_LOCAL:
        return PATH_RISK_NORMAL, ()

    # Defensive: any state not handled above fails closed, never normal.
    return (
        PATH_RISK_AMBIGUOUS,
        (f"unhandled mount state {mount_facts.state!r}; failing closed to ambiguous",),
    )


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
    mount_facts: MountFacts | None = None,
    mount_probe: MountProbe | None = None,
) -> PathSafety:
    """Classify ``raw_root`` into a closed :class:`PathSafety`.

    ``home`` / ``sync_roots`` and the mount inputs are injectable so the
    classification is deterministic. Supply mount metadata one of two ways:

    - ``mount_facts`` — a pre-probed closed :class:`MountFacts` (the **pure
      boundary**: the application adapter runs the OS probe and hands the domain
      a validated fact); or
    - ``mount_probe`` — a :class:`MountProbe` the classifier calls inside a guard
      that converts any exception into ``MOUNT_UNAVAILABLE`` (a convenience whose
      failures never escape the hard gate).

    **Mount metadata is required to reach ``normal``.** With neither supplied —
    or with an unknown/invalid state, or a probe error — the mount is treated as
    ``unavailable`` and a plain path fails closed to ``ambiguous`` rather than
    ``normal`` (F3/F5). ``normal`` is reachable only on a positive ``local``
    mount fact.

    Steps:

    1. Resolve to a canonical, symlink-free directory identity. If that fails,
       return ``path_risk=ambiguous`` immediately (fail closed — never
       ``normal`` on doubt).
    2. Classify ``root_kind`` from Git worktree **ancestry** of the canonical
       root (nested cwd + linked-worktree ``.git`` file both count).
    3. Resolve mount facts fail-closed, then classify ``path_risk`` (home →
       conflicting → path sync signal → unavailable → mount sync → local-normal).
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

    # ``root_kind`` follows Git worktree ancestry, not just a ``.git`` entry at
    # the root: a cwd nested inside a Git worktree is git-managed, and a linked
    # worktree carries a ``.git`` *file* (Redmine #13508 F2).
    root_kind = (
        ROOT_KIND_GIT
        if infer_git_worktree_root(canonical) is not None
        else ROOT_KIND_NON_GIT
    )
    resolved_mount = _resolve_mount_facts(
        canonical, mount_facts=mount_facts, mount_probe=mount_probe
    )
    path_risk, risk_notes = _classify_risk(
        canonical,
        home=home,
        sync_roots=resolved_sync_roots,
        mount_facts=resolved_mount,
    )
    adoption_marker = _classify_adoption(canonical)

    return PathSafety(
        root=canonical,
        root_kind=root_kind,
        path_risk=path_risk,
        adoption_marker=adoption_marker,
        notes=risk_notes,
    )
