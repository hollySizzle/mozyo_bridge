"""CLI parser registration for the ``redmine-version`` family (Redmine #12651).

Registers the top-level ``redmine-version`` command with two subcommands:

- ``redmine-version list-open-leaf`` — enumerate the open *leaf* issues of a
  Version from a flat ``GET /issues.json?fixed_version_id=<id>`` snapshot.
- ``redmine-version preflight`` — fail-closed rename / close / lock / delete
  preflight that prints an allow/blocked decision plus the concrete REST /
  operator-UI step to perform out-of-band.

Both are advisory and read-only: they consume operator-exported JSON snapshots
and render a decision; neither performs a Redmine write or touches the network.
Handlers live in
:mod:`mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.application.commands_redmine_version`;
this module only wires the parser, matching the split used by the other CLI
families (see ``cli_module_health`` for the reference shape).
"""
from __future__ import annotations

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.application.commands_redmine_version import (
    cmd_redmine_version_list_open_leaf,
    cmd_redmine_version_preflight,
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
    """Register the ``redmine-version`` command group onto ``sub``."""
    family = sub.add_parser(
        "redmine-version",
        help=(
            "Redmine Version metadata operations (Redmine #12651): enumerate the "
            "open leaf issues of a Version, and run a fail-closed rename / close / "
            "lock / delete preflight. Advisory / read-only over operator-exported "
            "snapshots; performs no Redmine write and no network call."
        ),
    )
    family_sub = family.add_subparsers(dest="redmine_version_command", required=True)

    leaves = family_sub.add_parser(
        "list-open-leaf",
        help=(
            "Enumerate the open leaf issues of a Version from a flat "
            "issues.json snapshot (the read model the MCP US-only surface "
            "cannot produce)."
        ),
    )
    leaves.add_argument(
        "--version-id",
        required=True,
        metavar="ID",
        help="Redmine Version id the snapshot was exported for.",
    )
    leaves.add_argument(
        "--issues-json",
        required=True,
        metavar="PATH",
        help=(
            "Path to a GET /issues.json?fixed_version_id=<id> export "
            '(``{"issues": [...]}`` or a bare list of issue mappings).'
        ),
    )
    _add_json_option(leaves)
    leaves.set_defaults(func=cmd_redmine_version_list_open_leaf)

    preflight = family_sub.add_parser(
        "preflight",
        help=(
            "Fail-closed rename/close/lock/delete preflight. Prints an "
            "allow/blocked decision with the required confirmation token and the "
            "concrete REST / operator-UI step. Executes nothing."
        ),
    )
    preflight.add_argument(
        "--version-id", required=True, metavar="ID", help="Redmine Version id."
    )
    preflight.add_argument(
        "--op",
        required=True,
        choices=("rename", "close", "lock", "delete"),
        help="Operation to preflight.",
    )
    preflight.add_argument(
        "--new-name",
        metavar="NAME",
        help="New Version name (rename only). Must be a planning-bucket name, "
        "not a package release number.",
    )
    preflight.add_argument(
        "--confirm",
        metavar="TOKEN",
        help="Confirmation token '<op>:<version-id>' authorizing the operation.",
    )
    preflight.add_argument(
        "--allow-open-issues",
        action="store_true",
        default=False,
        help="Permit close/lock of a Version that still holds open issues.",
    )
    preflight.add_argument(
        "--historical-protected",
        action="store_true",
        default=False,
        help="Mark the Version as a retained historical record (blocks delete).",
    )
    preflight.add_argument(
        "--versions-json",
        metavar="PATH",
        help="Path to a list_versions(status=all) export to resolve the Version "
        "state by id.",
    )
    # Inline-state fallback when no snapshot is supplied.
    preflight.add_argument("--name", metavar="NAME", help="Version name (inline state).")
    preflight.add_argument("--status", metavar="STATUS", help="open|locked|closed (inline state).")
    preflight.add_argument("--issues-count", type=int, default=0, help="Total issues (inline state).")
    preflight.add_argument(
        "--open-issues-count", type=int, default=0, help="Open issues (inline state)."
    )
    preflight.add_argument(
        "--closed-issues-count", type=int, default=0, help="Closed issues (inline state)."
    )
    _add_json_option(preflight)
    preflight.set_defaults(func=cmd_redmine_version_preflight)
