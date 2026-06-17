"""CLI parser registration for the runtime-config / instruction-alias family.

Split out of ``application/cli.py`` (Redmine #12153). Behavior-preserving;
the handlers themselves live in ``application/commands.py``. Block text is
moved verbatim from ``build_parser()`` so help / choices / defaults / dest /
``func`` bindings (including the deprecation metadata used by ``main()``) are
unchanged.

``_add_runtime_config_check_parser`` / ``_add_runtime_config_install_parser``
are re-exported from ``application/cli.py`` to preserve the pre-split
module-level import surface (Redmine #12138 scope guard).
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.commands import (
    cmd_instruction_doctor,
    cmd_instruction_install,
)
from mozyo_bridge.application.instruction_doctor import (
    KNOWN_PROFILES,
    PROFILE_REDMINE_CODEX,
)


def _add_runtime_config_check_parser(
    subparsers: argparse._SubParsersAction,
    *,
    name: str,
    deprecated_alias: str | None = None,
    canonical_command: str | None = None,
) -> argparse.ArgumentParser:
    """Add the read-only runtime-config check parser under `name`.

    Used twice: as the canonical `runtime-config check`, and as the deprecated
    `instruction doctor` alias. The alias path records deprecation metadata so
    `main()` can warn before dispatch.
    """
    parser = subparsers.add_parser(
        name,
        help=(
            "Profile-aware, read-only check that a Redmine/Codex workspace "
            "carries the repo-root runtime config the bootstrap docs require "
            "(`<repo>/.codex/config.toml`, optional `<repo>/.mcp.json`). Does "
            "not call the network, autogenerate, or write home config."
            + ("" if deprecated_alias is None else " [deprecated alias]")
        ),
    )
    parser.add_argument(
        "--target",
        dest="target",
        help="Project root to check. Defaults to MOZYO_REPO or the current "
        "working directory.",
    )
    parser.add_argument("--repo", dest="target", help="Alias for --target.")
    parser.add_argument(
        "--profile",
        choices=list(KNOWN_PROFILES),
        default=PROFILE_REDMINE_CODEX,
        help="Config profile to check. Only `redmine-codex` is defined today; "
        "other presets are intentionally not failed by this command.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    parser.set_defaults(
        func=cmd_instruction_doctor,
        deprecated_alias=deprecated_alias,
        canonical_command=canonical_command,
    )
    return parser


def _add_runtime_config_install_parser(
    subparsers: argparse._SubParsersAction,
    *,
    name: str,
    deprecated_alias: str | None = None,
    canonical_command: str | None = None,
) -> argparse.ArgumentParser:
    """Add the write-capable runtime-config install parser under `name`.

    Used twice: as the canonical `runtime-config install`, and as the
    deprecated `instruction install` alias.
    """
    parser = subparsers.add_parser(
        name,
        help=(
            "Project the verified Redmine default project from "
            "`<repo>/.mozyo-bridge/workspace-defaults.yaml` into the repo-root "
            "`<repo>/.codex/config.toml` so `runtime-config check` turns green. "
            "Source of truth stays workspace-defaults; only the repo-root config "
            "is written (never home config), no credentials are generated, and "
            "the default is a dry-run (pass `--write` to apply)."
            + ("" if deprecated_alias is None else " [deprecated alias]")
        ),
    )
    parser.add_argument(
        "--target",
        dest="target",
        help="Project root to install into. Defaults to MOZYO_REPO or the "
        "current working directory.",
    )
    parser.add_argument("--repo", dest="target", help="Alias for --target.")
    parser.add_argument(
        "--profile",
        choices=list(KNOWN_PROFILES),
        default=PROFILE_REDMINE_CODEX,
        help="Config profile to install. Only `redmine-codex` is defined today.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply the change to `<repo>/.codex/config.toml` (default: dry-run).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "When the managed [redmine] / [mcp_servers.redmine_epic_grid] tables "
            "already exist but disagree with workspace-defaults, regenerate them "
            "(other tables are preserved). Without --force a conflict fails."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    parser.set_defaults(
        func=cmd_instruction_install,
        deprecated_alias=deprecated_alias,
        canonical_command=canonical_command,
    )
    return parser


def register(sub) -> None:
    """Register the `runtime-config` group and deprecated `instruction` alias."""
    # `runtime-config` is the canonical repo-local LLM runtime config group
    # (renamed from `instruction` in Redmine #11051 to free the word
    # "instruction" for the `doctor instruction` runbook). `check` is read-only,
    # `install` is write-capable (dry-run by default). The legacy `instruction`
    # group below is a deprecated alias that warns and is a removal candidate
    # next minor.
    runtime_config = sub.add_parser(
        "runtime-config",
        help=(
            "Repo-local LLM runtime config commands: `check` (read-only) and "
            "`install` (write-capable, dry-run by default)"
        ),
    )
    runtime_config_sub = runtime_config.add_subparsers(
        dest="runtime_config_command", required=True
    )
    _add_runtime_config_check_parser(runtime_config_sub, name="check")
    _add_runtime_config_install_parser(runtime_config_sub, name="install")

    instruction = sub.add_parser(
        "instruction",
        help=(
            "Deprecated alias for `runtime-config` (write-capable). Use "
            "`runtime-config check` / `runtime-config install`; the old names "
            "still run but warn and are a removal candidate next minor."
        ),
    )
    instruction_sub = instruction.add_subparsers(
        dest="instruction_command", required=True
    )
    _add_runtime_config_check_parser(
        instruction_sub,
        name="doctor",
        deprecated_alias="mozyo-bridge instruction doctor",
        canonical_command="mozyo-bridge runtime-config check",
    )
    _add_runtime_config_install_parser(
        instruction_sub,
        name="install",
        deprecated_alias="mozyo-bridge instruction install",
        canonical_command="mozyo-bridge runtime-config install",
    )
