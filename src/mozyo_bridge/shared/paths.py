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
# Markers that establish a repo / workspace root for identity inference. The
# walk returns the deepest ancestor bearing ANY marker, so adding workspace
# markers can only stop the walk earlier (at a more specific root) — it never
# overrides a deeper git / pyproject root.
REPO_ROOT_MARKERS = PROJECT_MARKERS + WORKSPACE_MARKERS
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mozyo-bridge"


def mozyo_bridge_home() -> Path:
    """Resolve the mozyo-bridge home root (``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``).

    Canonical definition of the home contract (Redmine #11429); the
    scaffold rules store and the workspace registry both resolve through
    this single helper so the env override behaves identically everywhere.
    """
    return Path(os.environ.get("MOZYO_BRIDGE_HOME", "~/.mozyo_bridge")).expanduser().resolve()


def find_repo_root(start: Path | None = None) -> Path:
    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if any((path / marker).exists() for marker in REPO_ROOT_MARKERS):
            return path
    return current


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
