"""CLI parser registration for ``tests run`` (Redmine #14757).

Adds the isolated focused/full runner onto the existing ``tests`` family next to
``resolve`` / ``profile`` / ``parallel``. :func:`register_run` registers the
public ``run`` subcommand; :func:`add_no_isolate_flag` adds the escape hatch
all three entry points share, and :func:`add_isolation_flags` adds it together
with the internal already-isolated marker that only the self-re-exec'ing entry
points (``profile`` / ``parallel``) need.
"""

from __future__ import annotations

import argparse

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_run import (
    ISOLATED_FLAG,
    cmd_tests_run,
)


def add_reveal_paths_flag(parser) -> None:
    """Local-debug opt-in for absolute paths in the verdict (j#100490 item 4).

    Default output identifies homes by role/ordinal/digest, because these verdicts
    are pasted into Redmine journals and CI logs where an absolute path discloses
    the operator's account name and local layout. An operator debugging on their
    own machine can ask for the real paths.
    """
    parser.add_argument(
        "--reveal-paths",
        dest="reveal_paths",
        action="store_true",
        default=False,
        help=(
            "Local debug only: print absolute operator-home and task-root paths "
            "instead of role/ordinal/digest labels. Do not use for output that "
            "will be pasted into a ticket or CI log."
        ),
    )


def add_no_isolate_flag(parser) -> None:
    """Add the escape hatch every isolated entry point shares."""
    add_reveal_paths_flag(parser)
    parser.add_argument(
        "--no-isolate",
        dest="no_isolate",
        action="store_true",
        default=False,
        help=(
            "Operator/debug escape hatch: run WITHOUT the process home fence and "
            "WITHOUT the operator-home guard. Announced on stderr; a run made "
            "this way is not a verification record."
        ),
    )


def add_isolation_flags(parser) -> None:
    """Flags for an entry point that re-execs *itself* into the fenced child.

    ``tests profile`` / ``tests parallel`` keep running their own logic in the
    child, so they need the marker that stops a third generation from spawning.
    ``tests run`` does not take it: its child is ``python -m unittest``, never
    itself, so there is no recursion to break and a flag that did nothing would
    only mislead.
    """
    add_no_isolate_flag(parser)
    parser.add_argument(
        ISOLATED_FLAG,
        dest="already_isolated",
        action="store_true",
        default=False,
        # Internal: set on the re-exec'ed child so it runs in-process instead of
        # spawning a third generation.
        help=argparse.SUPPRESS,
    )


def register_run(tests_sub) -> None:
    """Register ``run`` onto the ``tests`` subparsers action."""
    run = tests_sub.add_parser(
        "run",
        help=(
            "Run `python -m unittest` in a process isolated from the operator's "
            "shared mozyo-bridge home (Redmine #14757), and fail the run if that "
            "home changed -- even when every test passed. With no arguments, "
            "runs the authoritative full discovery (`discover -s tests`); pass "
            "focused targets after `--`. HOME is left alone; the home contract, "
            "TMPDIR/TMP/TEMP and the XDG roots are pinned into one task-specific "
            "temp root and the live cockpit-session env is dropped. The task "
            "root lives under the system temp dir, or under MOZYO_TESTS_TMPDIR "
            "when that names an existing writable directory (declarative escape "
            "from a quota-pressured /tmp, Redmine #15710)."
        ),
    )
    add_repo_option(run)
    add_no_isolate_flag(run)
    run.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for the isolation verdict (default: text).",
    )
    run.add_argument(
        "unittest_args",
        nargs=argparse.REMAINDER,
        metavar="-- UNITTEST_ARGS",
        help=(
            "Arguments passed verbatim to `python -m unittest` after `--` (e.g. "
            "`-- tests.unit.foo.test_bar`, or `-- discover -s tests -v`). "
            "Omitted -> `discover -s tests`."
        ),
    )
    run.set_defaults(func=cmd_tests_run)


__all__ = ("add_isolation_flags", "add_no_isolate_flag", "register_run")
