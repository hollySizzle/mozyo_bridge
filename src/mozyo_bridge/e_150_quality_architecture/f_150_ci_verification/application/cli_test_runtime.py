"""CLI parser registration for ``tests profile`` (Redmine #12754).

Adds the runtime-profiling subcommand onto the existing ``tests`` family
(Redmine #12752) rather than a new top-level command — test verification helpers
share one family. :func:`register_profile` takes the ``tests`` subparsers action
created by :mod:`...application.cli_test_impact` and registers ``profile``; the
handler lives in :mod:`...application.commands_test_runtime`.

The flags mirror ``python -m unittest discover`` (``--start-dir`` / ``--pattern``
/ ``--top-level-dir``) so the profiled run is the same discovery CI already does,
plus the slow-test budget knobs (``--threshold`` / ``--budget`` / ``--enforce``)
and the per-lane verbosity knob (``-v`` / ``-q``).
"""

from __future__ import annotations

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_runtime import (
    cmd_tests_profile,
)


def register_profile(tests_sub) -> None:
    """Register the ``profile`` subcommand onto the ``tests`` subparsers action."""
    profile = tests_sub.add_parser(
        "profile",
        help=(
            "Run the test suite with per-test timing and print a runtime summary "
            "(slow tests vs the budget threshold/exceptions). Same discovery as "
            "`python -m unittest discover`; the suite verdict is authoritative — "
            "slow tests only fail the lane under --enforce."
        ),
    )
    add_repo_option(profile)
    profile.add_argument(
        "--start-dir",
        dest="start_dir",
        default="tests",
        help="Discovery start dir, repo-relative (default: tests).",
    )
    profile.add_argument(
        "--pattern",
        default="test*.py",
        help="Test file glob pattern (default: test*.py).",
    )
    profile.add_argument(
        "--top-level-dir",
        dest="top_level_dir",
        default=None,
        help="unittest top-level dir (default: the start dir).",
    )
    profile.add_argument(
        "--budget",
        default=None,
        help=(
            "Slow-test budget document (default: <repo>/test_runtime_budget.yaml). "
            "Defines the threshold and the slow-test exception allowlist."
        ),
    )
    profile.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Override the slow-test threshold in seconds for this run.",
    )
    profile.add_argument(
        "--slowest",
        type=int,
        default=20,
        help="How many slowest tests to list (default: 20).",
    )
    profile.add_argument(
        "--enforce",
        action="store_true",
        default=False,
        help=(
            "Exit non-zero when a non-exempt test exceeds the threshold "
            "(opt-in enforcing lane; off by default so timing variance never "
            "makes a normal run flaky)."
        ),
    )
    profile.add_argument(
        "--failfast",
        action="store_true",
        default=False,
        help="Stop on the first failure/error (mirrors unittest --failfast).",
    )
    profile.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for the runtime summary (default: text).",
    )
    profile.add_argument(
        "-v",
        "--verbose",
        dest="verbosity",
        action="store_const",
        const=2,
        default=None,
        help="Verbose unittest output (investigation lane; default is quiet).",
    )
    profile.add_argument(
        "-q",
        "--quiet",
        dest="verbosity",
        action="store_const",
        const=0,
        help="Quiet unittest output (no per-test dots).",
    )
    profile.set_defaults(func=cmd_tests_profile)


__all__ = ("register_profile",)
