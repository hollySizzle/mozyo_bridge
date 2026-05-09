from __future__ import annotations

import os
from pathlib import Path


PROJECT_MARKERS = (".git", ".tmux.conf", "pyproject.toml")
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mozyo-bridge"


def find_repo_root(start: Path | None = None) -> Path:
    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if any((path / marker).exists() for marker in PROJECT_MARKERS):
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
LABEL_OPTION = "@agent_name"
