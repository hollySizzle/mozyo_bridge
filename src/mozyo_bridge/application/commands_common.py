"""Shared argparse→path helpers used by the split command-handler family modules.

Extracted from ``application/commands.py`` (Redmine #12142) so family modules
(`commands_docs_scaffold`, `commands_workspace`, `commands_tmux_ui`) can resolve
``--repo`` / ``--target`` / ``--config-path`` without importing ``commands``
(which re-exports the family handlers and would otherwise create a circular
import). Behavior-preserving: the helper bodies are identical to the originals;
``commands`` re-imports them so
``mozyo_bridge.application.commands.repo_root_from_args`` keeps working.

``config_path_from_args`` was relocated here from ``commands.py`` for the
#12749 tmux-config / tmux-ui OOP-first family split so both the relocated
``cmd_config`` handler (``commands_tmux_ui``) and the residual auto-startup
loader (``load_tmux_conf_for`` in ``commands``) share one source without a
circular import.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from mozyo_bridge.shared.paths import default_tmux_conf, resolve_repo_root


def repo_root_from_args(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def config_path_from_args(args: argparse.Namespace) -> str:
    return str(Path(getattr(args, "config_path", None) or default_tmux_conf(repo_root_from_args(args))).expanduser())


def scaffold_target_from_args(args: argparse.Namespace) -> Path:
    target = getattr(args, "repo", None)
    if target:
        return Path(target).expanduser().resolve()
    return Path.cwd().resolve()
