"""CLI parser registration for the ``tests`` family (Redmine #12752).

Registers a thin top-level ``tests`` command with one subcommand:

- ``tests resolve`` — map changed source paths (explicit args, or git-derived
  via ``--staged`` / ``--all-changed``) to direct + bounded-context neighbor
  tests, with a fail-closed full/neighbor fallback for unmapped paths.

The family is deliberately minimal: one subcommand, read-only, no routing /
approval authority. New ``tests`` subcommands and the broader workflow-step
command surface are coordinated with Redmine #12755; this lane adds only the
impact resolver. Handlers live in :mod:`...application.commands_test_impact`;
this module only wires the parser, matching the ``health`` family shape.
"""

from __future__ import annotations

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_impact import (
    cmd_tests_resolve,
)


def register(sub) -> None:
    """Register the ``tests`` command group onto ``sub``."""
    tests = sub.add_parser(
        "tests",
        help=(
            "Test verification helpers (Redmine #12752): resolve changed source "
            "paths to focused test targets for local and CI reuse. Read-only; no "
            "routing, approval, or close authority."
        ),
    )
    tests_sub = tests.add_subparsers(dest="tests_command", required=True)

    resolve = tests_sub.add_parser(
        "resolve",
        help=(
            "Map changed source paths to direct + bounded-context neighbor "
            "tests. With no PATHS, derive changed paths from git. Unmapped "
            "paths escalate fail-closed to a full/neighbor fallback rather than "
            "silently selecting nothing."
        ),
    )
    add_repo_option(resolve)
    resolve.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help=(
            "Explicit repo-relative changed paths. When omitted, paths come from "
            "git (unstaged by default; see --staged / --all-changed)."
        ),
    )
    resolve.add_argument(
        "--staged",
        action="store_true",
        default=False,
        help="Use git staged (cached) changes instead of explicit PATHS.",
    )
    resolve.add_argument(
        "--all-changed",
        dest="all_changed",
        action="store_true",
        default=False,
        help="Use unstaged + untracked changes (instead of unstaged-only).",
    )
    resolve.add_argument(
        "--format",
        choices=("text", "json", "targets"),
        default="text",
        help=(
            "Output format: 'text' (human review), 'json' (full plan), or "
            "'targets' (newline-separated `python -m unittest` arguments; a full "
            "recommendation prints `discover -s tests`)."
        ),
    )
    resolve.set_defaults(func=cmd_tests_resolve)
