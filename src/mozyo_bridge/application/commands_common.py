"""Shared argparse→path helpers used by the split command-handler family modules.

Extracted from ``application/commands.py`` (Redmine #12142) so family modules
(`commands_docs_scaffold`, `commands_workspace`) can resolve ``--repo`` /
``--target`` without importing ``commands`` (which re-exports the family
handlers and would otherwise create a circular import). Behavior-preserving:
the helper bodies are identical to the originals; ``commands`` re-imports them
so ``mozyo_bridge.application.commands.repo_root_from_args`` keeps working.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from mozyo_bridge.shared.paths import resolve_repo_root


def repo_root_from_args(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def scaffold_target_from_args(args: argparse.Namespace) -> Path:
    target = getattr(args, "repo", None)
    if target:
        return Path(target).expanduser().resolve()
    return Path.cwd().resolve()
