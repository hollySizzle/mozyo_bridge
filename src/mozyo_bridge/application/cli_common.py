"""Shared argparse helpers used across the split CLI parser family modules.

Extracted from ``application/cli.py`` (Redmine #12141) so family modules
(`cli_release`, `cli_docs_scaffold`, `cli_workspace`) can reuse the common
``--repo`` / ``--target`` option wiring without importing ``cli`` (which would
create a circular import). Behavior-preserving: the option definitions are
identical to the originals.
"""
from __future__ import annotations

import argparse


def add_repo_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Project root. Defaults to MOZYO_REPO or the nearest cwd parent with .git/.tmux.conf/pyproject.toml")


def add_scaffold_target_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Project root to scaffold. Defaults to the current working directory")
    parser.add_argument("--target", dest="repo", help="Project root to scaffold. Alias for --repo")
