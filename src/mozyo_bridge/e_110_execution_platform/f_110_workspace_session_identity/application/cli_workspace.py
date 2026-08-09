"""CLI parser registration for the workspace / workspace-defaults families.

Split out of ``application/cli.py`` (Redmine #12141). Behavior-preserving.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.commands_workspace import (  # noqa: E501
    cmd_workspace_alias,
)
from mozyo_bridge.application.commands import (
    cmd_workspace_defaults,
    cmd_workspace_inspect,
    cmd_workspace_list,
    cmd_workspace_register,
    cmd_workspace_retire,
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
            "anchor when the home registry was lost."
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
        "--move",
        action="store_true",
        help=(
            "Allow relocating an already-registered workspace's canonical path "
            "to this checkout even when the previously registered checkout still "
            "exists (Redmine #13152). Without it, registration refuses the move so "
            "a clone/copy cannot hijack the identity; a linked git worktree is "
            "always refused regardless of this flag."
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

    workspace_alias = workspace_sub.add_parser(
        "alias",
        help=(
            "Declare how a NESTED workspace's explicit launch root resolves "
            "(Redmine #15190). When one Git repository holds both a canonical "
            "repo-root workspace and a nested application-root workspace, "
            "`herdr session-start --repo <nested>` would otherwise plan a second "
            "default Codex/Claude pair for the same repository. Declare the "
            "nested root as an alias of its canonical parent, or as "
            "launch-disabled, and session-start adopts the canonical root or "
            "fails closed with a fixed typed reason. Read-only surfaces "
            "(`workspace inspect`, `docs resolve`, `scaffold status`) keep "
            "addressing the nested root as itself, so it stays usable as a "
            "code/docs working root."
        ),
    )
    alias_sub = workspace_alias.add_subparsers(dest="alias_command", required=True)

    alias_show = alias_sub.add_parser(
        "show",
        help=(
            "Show the declaration this workspace carries and how a launch root "
            "would resolve, including a typed refusal reason. Read-only. Exits 0 "
            "when a launch would proceed (no declaration, or a verified alias) "
            "and 1 when it would not (launch-disabled, or a declaration that "
            "fails verification) — so the exit code answers the same question "
            "session-start asks."
        ),
    )
    add_repo_option(alias_show)
    alias_show.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the declaration and resolution as JSON.",
    )
    alias_show.set_defaults(func=cmd_workspace_alias, alias_command="show")

    alias_set = alias_sub.add_parser(
        "set",
        help=(
            "Declare this workspace an alias of a canonical parent workspace. "
            "Verified before anything is written: the target must exist, be a "
            "strict ancestor, live in the same repository, carry a durable "
            "workspace identity, and declare no alias of its own. Any failure "
            "writes nothing."
        ),
    )
    add_repo_option(alias_set)
    alias_set.add_argument(
        "--to",
        required=True,
        help=(
            "Canonical parent workspace root this nested root folds into. Its "
            "current workspace id is recorded as the verification binding, so a "
            "later re-registration cannot silently re-point the alias."
        ),
    )
    alias_set.add_argument(
        "--reason", default="",
        help="Operator note recorded in the declaration for later readback.",
    )
    alias_set.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the written declaration and its readback as JSON.",
    )
    alias_set.set_defaults(func=cmd_workspace_alias, alias_command="set")

    alias_disable = alias_sub.add_parser(
        "disable",
        help=(
            "Declare this workspace launch-disabled: session-start fails closed "
            "here with a fixed typed reason and launches nothing. Use when there "
            "is no canonical parent to fold into."
        ),
    )
    add_repo_option(alias_disable)
    alias_disable.add_argument(
        "--reason", default="",
        help="Operator note recorded in the declaration for later readback.",
    )
    alias_disable.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the written declaration as JSON.",
    )
    alias_disable.set_defaults(func=cmd_workspace_alias, alias_command="disable")

    alias_clear = alias_sub.add_parser(
        "clear",
        help=(
            "Remove this workspace's declaration, restoring independent-workspace "
            "launch behavior. Removes only the declaration file — never the "
            "identity anchor, the registry row, or tracked workspace content."
        ),
    )
    add_repo_option(alias_clear)
    alias_clear.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the outcome as JSON.",
    )
    alias_clear.set_defaults(func=cmd_workspace_alias, alias_command="clear")

    workspace_retire = workspace_sub.add_parser(
        "retire",
        help=(
            "Plan or execute removal of one stale missing-path registry row. "
            "Dry-run is the default. Execution requires the exact fresh plan "
            "digest, re-reads global Herdr inventory, creates a verified SQLite "
            "backup, and deletes only the fenced row."
        ),
    )
    add_repo_option(workspace_retire)
    workspace_retire.add_argument(
        "--workspace-id",
        required=True,
        help="Exact registered workspace id to inspect or retire.",
    )
    workspace_retire.add_argument(
        "--expect-plan-digest",
        default="",
        help=(
            "Exact SHA-256 digest from an approved fresh dry-run. Required with "
            "--execute and revalidated immediately before the registry write."
        ),
    )
    workspace_retire.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Create the verified private backup and delete the exact row. "
            "Without this flag the command is strictly read-only."
        ),
    )
    workspace_retire.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the path-redacted plan or retirement result as JSON.",
    )
    workspace_retire.set_defaults(func=cmd_workspace_retire)

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
