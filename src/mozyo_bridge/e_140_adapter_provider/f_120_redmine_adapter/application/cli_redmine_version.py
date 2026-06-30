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
            "cannot produce), or from a read-only live Redmine read with --live."
        ),
    )
    leaves.add_argument(
        "--version-id",
        required=True,
        metavar="ID",
        help="Redmine Version id the snapshot was exported for / to read live.",
    )
    # Exactly one input source: a static operator-exported snapshot, or an
    # explicit opt-in read-only live read. --live performs a network call, so it
    # must be asked for; without it the command stays snapshot-only and offline.
    source = leaves.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--issues-json",
        metavar="PATH",
        help=(
            "Path to a GET /issues.json?fixed_version_id=<id> export "
            '(``{"issues": [...]}`` or a bare list of issue mappings).'
        ),
    )
    source.add_argument(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Read the Version's issues live and read-only via "
            "GET /issues.json?fixed_version_id=<id>&status_id=* against the "
            "trusted Redmine (MOZYO_REDMINE_URL/MOZYO_REDMINE_API_KEY or the "
            "home credential file). Fails closed with an explicit reason and a "
            "non-zero exit when no credential/provider is available; never "
            "treats an unreadable Version as empty. Performs no Version write."
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
    # Inline-state fallback when no snapshot is supplied. Counts default to None
    # ("not provided"); a count-dependent op (delete/close/lock) needs either
    # --versions-json or all three counts, else it fails closed (counts_required).
    preflight.add_argument("--name", metavar="NAME", help="Version name (inline state).")
    preflight.add_argument("--status", metavar="STATUS", help="open|locked|closed (inline state).")
    preflight.add_argument(
        "--issues-count", type=int, default=None, help="Total issues (inline state)."
    )
    preflight.add_argument(
        "--open-issues-count", type=int, default=None, help="Open issues (inline state)."
    )
    preflight.add_argument(
        "--closed-issues-count", type=int, default=None, help="Closed issues (inline state)."
    )
    _add_json_option(preflight)
    preflight.set_defaults(func=cmd_redmine_version_preflight)
