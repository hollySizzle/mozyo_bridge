"""CLI parser registration for the workspace / workspace-defaults families.

Split out of ``application/cli.py`` (Redmine #12141). Behavior-preserving.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands import (
    cmd_workspace_defaults,
    cmd_workspace_inspect,
    cmd_workspace_list,
    cmd_workspace_register,
)


def register(sub) -> None:
    """Register the `workspace` and `workspace-defaults` subcommands onto ``sub``."""
    workspace = sub.add_parser(
        "workspace",
        help=(
            "Home-registry-first workspace identity (Redmine #11429). The "
            "home registry (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/"
            "registry.sqlite`) is the source of truth for workspace id, "
            "paths, readable name, and canonical tmux session name; the "
            "workspace-local anchor (`<repo>/.mozyo-bridge/workspace.json`) "
            "restores the same identity if the home registry is lost. Live "
            "tmux state is never stored here."
        ),
    )
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)

    workspace_register = workspace_sub.add_parser(
        "register",
        help=(
            "Register (or refresh) the workspace in the home registry and "
            "write its local anchor. Idempotent: keeps the existing workspace "
            "id and canonical session name; the session name is derived from "
            "the path only on first registration. Restores identity from the "
            "anchor when the home registry was lost. This is the only "
            "registry write surface."
        ),
    )
    add_repo_option(workspace_register)
    workspace_register.add_argument(
        "--name",
        help=(
            "Readable project name to record (may be non-ASCII). Defaults to "
            "the previously registered name, else the directory basename."
        ),
    )
    workspace_register.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the registration outcome and workspace record as JSON.",
    )
    workspace_register.set_defaults(func=cmd_workspace_register)

    workspace_list = workspace_sub.add_parser(
        "list",
        help="List registered workspaces from the home registry. Read-only.",
    )
    workspace_list.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the registry rows as JSON.",
    )
    workspace_list.set_defaults(func=cmd_workspace_list)

    workspace_inspect = workspace_sub.add_parser(
        "inspect",
        help=(
            "Show how this workspace's identity resolves: registry row, "
            "local anchor, path-derived fallback, and the effective session "
            "name with its source. Read-only."
        ),
    )
    add_repo_option(workspace_inspect)
    workspace_inspect.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit all identity layers and the effective resolution as JSON.",
    )
    workspace_inspect.set_defaults(func=cmd_workspace_inspect)

    workspace_defaults = sub.add_parser(
        "workspace-defaults",
        help=(
            "Render or drift-check the workspace-local Redmine default-"
            "project snippet (Redmine #10689). Single source is "
            "`<repo>/.mozyo-bridge/workspace-defaults.yaml`; default "
            "output is `.mozyo-bridge/redmine-defaults.md`. Distributed "
            "mozyo_bridge code does not carry project-specific values; "
            "the workspace YAML does. Pass `--check` to verify drift; "
            "default action regenerates the output(s)."
        ),
    )
    add_repo_option(workspace_defaults)
    workspace_defaults.add_argument(
        "--check",
        action="store_true",
        help=(
            "Re-render in memory and compare against the committed "
            "output(s). Exit 1 on drift; writes nothing."
        ),
    )
    workspace_defaults.set_defaults(func=cmd_workspace_defaults)
