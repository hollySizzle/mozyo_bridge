"""CLI parser registration for the ``tests`` family (Redmine #12752 / #12754).

Registers a thin top-level ``tests`` command with the test-verification
subcommands:

- ``tests resolve`` — map changed source paths (explicit args, or git-derived
  via ``--staged`` / ``--all-changed``) to direct + bounded-context neighbor
  tests, with a fail-closed full/neighbor fallback for unmapped paths
  (Redmine #12752).
- ``tests profile`` — run the suite with per-test timing and print a runtime
  summary against the slow-test budget (Redmine #12754); registered from
  :mod:`...application.cli_test_runtime`.

The family stays read-only with no routing / approval authority. Handlers live
in :mod:`...application.commands_test_impact` and
:mod:`...application.commands_test_runtime`; this module wires the parser,
matching the ``health`` family shape.
"""

from __future__ import annotations

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.cli_test_runtime import (
    register_profile,
)
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
        "--base",
        metavar="REF",
        default=None,
        help=(
            "Derive changed paths from `git diff <REF>...HEAD` (merge-base diff) "
            "instead of the working tree. This is the CI lane's entry point: the "
            "quick lane diffs against the PR merge target, so local and CI feed "
            "the identical resolver. Ignored when explicit PATHS are given."
        ),
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

    register_profile(tests_sub)
