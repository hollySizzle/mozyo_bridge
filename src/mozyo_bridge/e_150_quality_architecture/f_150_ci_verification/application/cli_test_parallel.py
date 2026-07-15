"""CLI parser registration for ``tests parallel`` (Redmine #13733).

Adds the isolated-process parallel runner onto the existing ``tests`` family
(Redmine #12752) next to ``resolve`` / ``profile`` — the test-verification
helpers share one family and the same ``f_150_ci_verification`` feature.
:func:`register_parallel` registers both the public ``parallel`` subcommand and
the hidden ``_shard-worker`` subcommand that ``parallel`` spawns per shard.

The discovery flags (``--start-dir`` / ``--pattern`` / ``--top-level-dir``)
mirror ``python -m unittest discover`` so the parallel run covers the identical
test set as the authoritative serial discovery; the parallelism knobs
(``--jobs`` / ``--durations`` / ``--serial-policy`` / ``--shard-timeout`` /
``--failfast``) control the shard plan and its fail-closed guards.
"""

from __future__ import annotations

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_parallel import (
    cmd_tests_parallel,
    cmd_tests_shard_worker,
)


def register_parallel(tests_sub) -> None:
    """Register ``parallel`` (+ hidden ``_shard-worker``) onto ``tests``."""
    parallel = tests_sub.add_parser(
        "parallel",
        help=(
            "Run the whole suite across isolated process shards (Redmine #13733). "
            "Same discovery as `python -m unittest discover -s tests`; same test "
            "set and green/red verdict as the serial run, aggregated fail-closed "
            "(a shard failure / timeout / crash / import error never reads as "
            "green). Each shard runs in its own HOME/TMPDIR/MOZYO_BRIDGE_HOME "
            "(kept functional for nested python/git) and cannot touch the live "
            "Herdr lane."
        ),
    )
    add_repo_option(parallel)
    parallel.add_argument(
        "--start-dir",
        dest="start_dir",
        default="tests",
        help="Discovery start dir, repo-relative (default: tests).",
    )
    parallel.add_argument(
        "--pattern",
        default="test*.py",
        help="Test file glob pattern (default: test*.py).",
    )
    parallel.add_argument(
        "--top-level-dir",
        dest="top_level_dir",
        default=None,
        help="unittest top-level dir (default: the start dir).",
    )
    parallel.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=None,
        help=(
            "Number of concurrent shard workers (default: host CPU count, or the "
            "policy's default_jobs). Shards are over-partitioned relative to jobs "
            "so the workers drain a finer queue."
        ),
    )
    parallel.add_argument(
        "--shards",
        type=int,
        default=None,
        help=(
            "Number of parallel shards to partition modules into (default: "
            "jobs * 4, capped at the module count). More shards balance load and "
            "make --failfast skip more queued work."
        ),
    )
    parallel.add_argument(
        "--durations",
        default=None,
        help=(
            "Optional per-module duration manifest (JSON: {module: seconds} or a "
            "`tests profile --format json` document) for weighted shard balancing. "
            "Absent -> shards are balanced by discovered test count."
        ),
    )
    parallel.add_argument(
        "--serial-policy",
        dest="serial_policy",
        default=None,
        help=(
            "Parallel-run policy document (default: <repo>/test_parallel_policy.yaml). "
            "Defines the serial-bucket module patterns + default jobs/timeout."
        ),
    )
    parallel.add_argument(
        "--shard-timeout",
        dest="shard_timeout",
        type=float,
        default=None,
        help=(
            "Per-shard wall-clock timeout in seconds (default: the policy's "
            "shard_timeout_seconds, else no timeout). A timed-out shard is "
            "fail-closed."
        ),
    )
    parallel.add_argument(
        "--failfast",
        action="store_true",
        default=False,
        help=(
            "Once a shard fails, stop launching still-queued shards and pass "
            "unittest --failfast to each shard (aggregate stays red; in-flight "
            "shards finish)."
        ),
    )
    parallel.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for the aggregate summary (default: text).",
    )
    parallel.set_defaults(func=cmd_tests_parallel)

    # Hidden per-shard worker entry point. Not for direct use; `parallel` spawns
    # it once per shard with a spec file. Kept in the same family so the single
    # `python -m mozyo_bridge` entry point can host it.
    worker = tests_sub.add_parser(
        "_shard-worker",
        help="(internal) run one parallel shard; invoked by `tests parallel`.",
    )
    add_repo_option(worker)
    worker.add_argument("--spec", required=True, help="Shard spec JSON file.")
    worker.add_argument("--result", required=True, help="Shard result JSON output file.")
    worker.set_defaults(func=cmd_tests_shard_worker)


__all__ = ("register_parallel",)
