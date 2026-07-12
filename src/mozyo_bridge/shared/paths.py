from __future__ import annotations

import os
import unicodedata
from pathlib import Path


def normalize_path_unicode(text: str) -> str:
    """Fix a path string's Unicode normal form for identity use (Redmine #11625).

    The same directory can be spelled NFC (document-, Redmine-, or
    agent-supplied paths) or NFD (macOS readdir / shell completion), and the
    spellings differ in bytes for decomposable characters (dakuten katakana
    etc.). Every surface that hashes or compares paths as workspace identity
    must go through this single helper so the two spellings cannot diverge.

    NFD is the fixed form — deliberately matching macOS filesystem reality —
    so session names and anchors that were historically derived from real
    filesystem paths keep their values. Comparisons only need both sides in
    the same form; the hash in `domain.session_naming` is what makes the
    concrete form choice compatibility-relevant.
    """
    return unicodedata.normalize("NFD", text)


PROJECT_MARKERS = (".git", ".tmux.conf", "pyproject.toml")
# A scaffolded mozyo workspace is a first-class identity root even when it has
# no git / pyproject / tmux marker (Redmine #11301). Google-Drive-hosted,
# non-git workspaces created by `mozyo --repo <target>` otherwise leak their
# inferred repo root up to the home directory, which fail-closes the
# cross-workspace `--target-repo` gate. The scaffold manifest is the narrow
# marker; a bare `.mozyo-bridge/` directory would be too broad because tooling
# may create that directory without establishing workspace identity.
#
# The workspace-registry anchor (Redmine #11429, review #54760) is equally a
# workspace-identity root: `mozyo-bridge workspace register` writes it exactly
# once per workspace root, and without it a registered non-git workspace's
# subdirectories would re-derive a different session name instead of resolving
# the registered root. The anchor was renamed (Redmine #11920 / #11921) from
# `workspace.json` to `workspace-anchor.json`; both names mark a workspace root
# during the compatibility window so a registered workspace keeps resolving its
# root whether it carries the new or the legacy anchor.
WORKSPACE_MARKERS = (
    ".mozyo-bridge/scaffold.json",
    ".mozyo-bridge/workspace-anchor.json",
    ".mozyo-bridge/workspace.json",
)
# The repo-local config is equally an adoption-time root marker (Redmine
# #13379 review j#73711): a non-Git project adopted by hand-writing
# `.mozyo-bridge/config.yaml` alone (no scaffold manifest, no registry
# anchor) must resolve its root from a child cwd exactly like a scaffolded
# workspace — otherwise the bare-`mozyo` adoption gate refuses an adopted
# project and the repo-local backend selection never reads its config.
REPO_LOCAL_CONFIG_MARKER = ".mozyo-bridge/config.yaml"
# Markers that establish a repo / workspace root for identity inference. The
# walk returns the deepest ancestor bearing ANY marker, so adding workspace
# markers can only stop the walk earlier (at a more specific root) — it never
# overrides a deeper git / pyproject root.
REPO_ROOT_MARKERS = PROJECT_MARKERS + WORKSPACE_MARKERS + (REPO_LOCAL_CONFIG_MARKER,)
# Markers that establish mozyo ADOPTION of an already-resolved root (Redmine
# #13379). Identity inference (which ancestor is the root) and adoption (did
# this project opt into mozyo) are different questions: bare `mozyo` in an
# unadopted directory used to walk up to an incidental marker — the home
# directory's `.tmux.conf` in the observed trap — and silently start two real
# agents there. Only files written by an explicit adoption action count: the
# repo-local config (`scaffold apply` / hand-written) and the workspace
# anchors (`scaffold apply` manifest, `workspace register`). A bare
# `.mozyo-bridge/` directory is deliberately NOT a marker — repo-local stores
# (state.sqlite, ledgers) may create it as a side effect without adoption.
ADOPTION_MARKERS = (REPO_LOCAL_CONFIG_MARKER,) + WORKSPACE_MARKERS
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mozyo-bridge"


def mozyo_bridge_home() -> Path:
    """Resolve the mozyo-bridge home root (``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``).

    Canonical definition of the home contract (Redmine #11429); the
    scaffold rules store and the workspace registry both resolve through
    this single helper so the env override behaves identically everywhere.
    """
    return Path(os.environ.get("MOZYO_BRIDGE_HOME", "~/.mozyo_bridge")).expanduser().resolve()


def infer_git_worktree_root(start: str | Path | None) -> Path | None:
    """Walk up from ``start`` to the nearest Git worktree root, or ``None``.

    A Git worktree root carries a ``.git`` entry — a directory for the main
    worktree, a file for a linked worktree — so this stops at the first ancestor
    where ``.git`` exists. Unlike :func:`find_repo_root` / ``infer_repo_root``
    (which also stop at non-git workspace markers such as
    ``.mozyo-bridge/scaffold.json``), this resolver looks ONLY for the Git root.

    Project-scoped identity (Redmine #12658 j#66499) needs this because a monorepo
    project subdirectory may carry its own ``.mozyo-bridge/scaffold.json``: the
    marker-based resolver would stop at the project subdir and collapse the
    workspace identity onto the project. The Git worktree root is the workspace;
    the nested scaffold marker only matters when there is no Git root above (a
    genuinely non-git scaffolded workspace, Redmine #11301), which the callers
    handle by falling back to the marker resolver — both
    :func:`find_repo_root` (the shared identity/config-root resolver, Git-root-
    first since Redmine #13641) and
    :func:`project_discovery.resolve_workspace_root` (cockpit identity). Returns
    ``None`` on an unreadable path or when no ``.git`` is reachable.
    """
    if not start:
        return None
    try:
        current = Path(start).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if (path / ".git").exists():
            return path
    return None


def find_repo_root(start: Path | None = None) -> Path:
    """Resolve the repo / workspace identity root for ``start`` (or the cwd).

    Git-root-first (Redmine #13641): when a Git worktree root is reachable above
    ``start``, that Git root IS the workspace and wins — even when a nested
    ``.mozyo-bridge/scaffold.json`` (or another workspace marker) sits in a
    monorepo project subdirectory between ``start`` and the Git root. The plain
    marker walk stops at that nested marker and collapses the workspace identity
    onto the project subtree — the trap documented on
    :func:`infer_git_worktree_root` (Redmine #12658) and fixed for cockpit
    identity in :func:`project_discovery.resolve_workspace_root`. Before this the
    bare-``mozyo`` entrypoint resolved its config root through the marker walk, so
    a subtree with a bare ``scaffold.json`` shadowed the Git root's
    ``.mozyo-bridge/config.yaml``: the invocation read the (usually absent)
    subtree config and fell through to the default tmux backend instead of the
    Git root's ``terminal_transport.backend`` (Redmine #13641). Making the shared
    resolver Git-root-first aligns every entrypoint with the workspace invariant
    (``Workspace = Git repository / registry identity``,
    ``project-scoped-workspace-identity.md``).

    The marker walk is the fallback ONLY when no Git root is reachable, so it is
    behavior-preserving for a genuinely non-git scaffolded workspace (Redmine
    #11301), a registry-anchored non-git workspace (#11429), and a config-only
    adopted root (#13379): each still resolves to its marker root exactly as
    before. Explicit ``--repo`` / ``MOZYO_REPO`` never reach here (they short-
    circuit in :func:`resolve_repo_root`), so that override contract is unchanged.
    Note that a Git-root-first result is not by itself an adoption decision:
    :func:`workspace_adoption_marker` still gates whether the resolved root is
    launched, so an unadopted Git root does not start a real agent (#13379).
    """
    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    git_root = infer_git_worktree_root(current)
    if git_root is not None:
        return git_root
    for path in (current, *current.parents):
        if any((path / marker).exists() for marker in REPO_ROOT_MARKERS):
            return path
    return current


def workspace_adoption_marker(root: str | Path) -> str | None:
    """The adoption marker ``root`` carries, or ``None`` for an unadopted root.

    Checks :data:`ADOPTION_MARKERS` directly under the already-resolved
    ``root`` — no ancestor walk; adoption is a property of the resolved root
    itself, not of whatever ancestor made it resolvable. Returns the matching
    marker's repo-relative path so refusal text can name the evidence.
    """
    base = Path(root).expanduser()
    for marker in ADOPTION_MARKERS:
        if (base / marker).exists():
            return marker
    return None


def resolve_repo_root(repo: str | Path | None = None, start: Path | None = None) -> Path:
    if repo:
        return Path(repo).expanduser().resolve()
    env_repo = os.environ.get("MOZYO_REPO")
    if env_repo:
        return Path(env_repo).expanduser().resolve()
    return find_repo_root(start)


def default_queue_path(repo_root: Path | None = None) -> Path:
    return Path(repo_root or resolve_repo_root()) / ".agent_handoff" / "tasks.json"


def default_tmux_conf(repo_root: Path | None = None) -> Path:
    root_conf = Path(repo_root or resolve_repo_root()) / ".tmux.conf"
    if root_conf.exists():
        return root_conf
    return CONFIG_HOME / "tmux.conf"


REPO_ROOT = resolve_repo_root()
DEFAULT_QUEUE_PATH = default_queue_path(REPO_ROOT)
DEFAULT_TMUX_CONF = default_tmux_conf(REPO_ROOT)
READ_MARK_PREFIX = "/tmp/mozyo-bridge-read-"
