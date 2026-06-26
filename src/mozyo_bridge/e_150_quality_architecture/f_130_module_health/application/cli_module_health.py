"""CLI parser registration for the module-health family (Redmine #12321).

Registers the top-level ``health`` command with two subcommands:

- ``health report`` — read-only per-module LOC / approximate complexity /
  top-level symbol count for the runtime package, largest-first.
- ``health check`` — the oversized-module gate; exits non-zero when a new
  oversized module appears or an allowlisted module grows past its baseline.

Handlers live in :mod:`mozyo_bridge.application.commands_module_health`; this
module only wires the parser, matching the split used by the other CLI families
(see ``cli_state`` for the reference shape).
"""
from __future__ import annotations

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands_module_health import (
    cmd_health_check,
    cmd_health_report,
)


def _add_config_option(parser) -> None:
    parser.add_argument(
        "--config",
        help=(
            "Path to the module-health config/allowlist. Defaults to "
            "<repo>/module_health.yaml."
        ),
    )


def _add_json_option(parser) -> None:
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=False,
        help="Emit the result as JSON.",
    )


def register(sub) -> None:
    """Register the ``health`` command group onto ``sub``."""
    health = sub.add_parser(
        "health",
        help=(
            "Module-health visibility + oversized-module gate (Redmine #12321): "
            "report per-module LOC/complexity/symbol counts, or run the "
            "PyLint-`too-many-lines`-equivalent gate that fails new oversized "
            "modules and growth of allowlisted ones. Read-only measurement; no "
            "routing, approval, or close authority."
        ),
    )
    health_sub = health.add_subparsers(dest="health_command", required=True)

    report = health_sub.add_parser(
        "report",
        help=(
            "Read-only per-module LOC / approximate complexity / top-level "
            "symbol count for the runtime package, largest-first."
        ),
    )
    add_repo_option(report)
    _add_config_option(report)
    report.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Show only the N largest modules (default: all in scope).",
    )
    _add_json_option(report)
    report.set_defaults(func=cmd_health_report)

    check = health_sub.add_parser(
        "check",
        help=(
            "Run the oversized-module gate. Exits non-zero when a module over "
            "the threshold is missing from the allowlist (new oversized file) or "
            "an allowlisted module grew past its recorded baseline."
        ),
    )
    add_repo_option(check)
    _add_config_option(check)
    _add_json_option(check)
    check.set_defaults(func=cmd_health_check)
