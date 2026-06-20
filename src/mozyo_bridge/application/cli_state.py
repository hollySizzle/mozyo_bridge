"""CLI parser registration for the home-scoped state store family (#12305).

Registers the top-level ``state`` command with three subcommands:

- ``state inspect`` — read-only per-component status (legacy files + the future
  single DB, side-by-side); reuses the #12273 doctor inspector.
- ``state migrate`` — dry-run plan by default; ``--write`` performs the
  backup-first, idempotent, non-destructive migration of the legacy per-kind
  SQLite files into the home-scoped ``state.sqlite``.
- ``state cleanup`` — the deliberately separate, destructive retirement of
  migrated legacy files; deletes nothing without ``--write --confirm-destroy``.

Handlers live in :mod:`mozyo_bridge.application.commands_state`; this module only
wires the parser, matching the split used by the other CLI families.
"""
from __future__ import annotations

from mozyo_bridge.application.commands_state import (
    cmd_state_cleanup,
    cmd_state_inspect,
    cmd_state_migrate,
)


def _add_home_option(parser) -> None:
    parser.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )


def _add_json_option(parser) -> None:
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=False,
        help="Emit the result as JSON.",
    )


def _add_component_option(parser) -> None:
    parser.add_argument(
        "--component",
        dest="components",
        action="append",
        metavar="NAME",
        help=(
            "Restrict to one component (registry / managed_events / inventory / "
            "otel); repeatable. Default: all components."
        ),
    )


def register(sub) -> None:
    """Register the ``state`` command group onto ``sub``."""
    state = sub.add_parser(
        "state",
        help=(
            "Home-scoped state store (Redmine #12305): inspect the legacy per-kind "
            "SQLite files and the consolidated `state.sqlite` side-by-side, or "
            "migrate the legacy files into it (backup-first, idempotent, "
            "non-destructive). Component status is a read-only projection — never "
            "liveness, routing, approval, or close authority."
        ),
    )
    state_sub = state.add_subparsers(dest="state_command", required=True)

    inspect = state_sub.add_parser(
        "inspect",
        help=(
            "Read-only per-component status of the legacy files and the single DB "
            "(creates nothing, writes nothing)."
        ),
    )
    _add_home_option(inspect)
    _add_json_option(inspect)
    inspect.set_defaults(func=cmd_state_inspect)

    migrate = state_sub.add_parser(
        "migrate",
        help=(
            "Migrate the legacy per-kind SQLite files into the home-scoped "
            "`state.sqlite`. Dry-run by default (writes nothing); pass --write to "
            "perform a backup-first, idempotent, non-destructive migration."
        ),
    )
    _add_home_option(migrate)
    _add_component_option(migrate)
    migrate.add_argument(
        "--write",
        dest="write",
        action="store_true",
        default=False,
        help=(
            "Perform the migration (default is a read-only dry-run plan). Backs up "
            "every legacy file it reads plus any existing single DB first; never "
            "deletes or mutates a legacy file."
        ),
    )
    _add_json_option(migrate)
    migrate.set_defaults(func=cmd_state_migrate)

    cleanup = state_sub.add_parser(
        "cleanup",
        help=(
            "Retire migrated legacy files (DESTRUCTIVE, separately gated). Prints "
            "the cleanup plan by default; deletes nothing without "
            "--write --confirm-destroy, and only for components recorded complete "
            "in the single DB."
        ),
    )
    _add_home_option(cleanup)
    _add_component_option(cleanup)
    cleanup.add_argument(
        "--write",
        dest="write",
        action="store_true",
        default=False,
        help="Required (with --confirm-destroy) to actually remove legacy files.",
    )
    cleanup.add_argument(
        "--confirm-destroy",
        dest="confirm_destroy",
        action="store_true",
        default=False,
        help=(
            "Explicit destructive gate. Required (with --write) to back up and "
            "delete migrated legacy files; without it cleanup is a read-only plan."
        ),
    )
    _add_json_option(cleanup)
    cleanup.set_defaults(func=cmd_state_cleanup)
