"""CLI parser registration for the desired-presentation current-table family.

Registers the top-level ``presentation`` command (Redmine #12304) with two
subcommands:

- ``presentation seed`` — migrate the static repo-local ``.mozyo-bridge/config.yaml``
  presentation block into the home-scoped desired presentation current tables
  (idempotent / non-destructive; ``--dry-run`` previews);
- ``presentation show`` — read-only inspector of those tables + seed provenance.

Handlers live in :mod:`mozyo_bridge.application.commands_presentation`; this
module only wires the parser, matching the split used by the other CLI families.
"""
from __future__ import annotations

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands_presentation import (
    cmd_presentation_seed,
    cmd_presentation_show,
)


def register(sub) -> None:
    """Register the ``presentation`` command group onto ``sub``."""
    presentation = sub.add_parser(
        "presentation",
        help=(
            "Desired presentation current tables (Redmine #12304): seed the "
            "home-scoped cockpit-group membership / projection preferences from "
            "the repo-local `.mozyo-bridge/config.yaml` presentation block, or "
            "inspect them. Display-only desired state — never handoff routing, "
            "liveness, approval, or close authority."
        ),
    )
    presentation_sub = presentation.add_subparsers(
        dest="presentation_command", required=True
    )

    seed = presentation_sub.add_parser(
        "seed",
        help=(
            "Seed/migrate the repo-local presentation config into the home-scoped "
            "desired presentation current tables. Idempotent (a re-run of an "
            "unchanged config writes nothing) and non-destructive (never deletes a "
            "row); records the source config version."
        ),
    )
    add_repo_option(seed)
    seed.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Compute and print the planned seed without writing any row.",
    )
    seed.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=False,
        help="Emit the seed result as JSON.",
    )
    seed.set_defaults(func=cmd_presentation_seed)

    show = presentation_sub.add_parser(
        "show",
        help=(
            "Read-only inspector of the desired presentation current tables "
            "(cockpit_group_membership / projection_preferences) and the recorded "
            "seed provenance."
        ),
    )
    show.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=False,
        help="Emit the current tables + provenance as JSON.",
    )
    show.set_defaults(func=cmd_presentation_show)
