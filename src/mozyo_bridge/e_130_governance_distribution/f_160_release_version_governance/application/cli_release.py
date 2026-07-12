"""CLI parser registration for the release helper command family.

Split out of ``application/cli.py`` (Redmine #12141). Behavior-preserving;
the release helper handlers themselves live in ``application/release.py``.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import (
    cmd_release_bump,
    cmd_release_check_artifact,
    cmd_release_check_drift,
    cmd_release_check_scaffold,
    cmd_release_check_tree,
    cmd_release_check_workflow,
    cmd_release_publish,
    cmd_release_workflow_runs,
    cmd_release_workflow_wait,
)


def register(sub) -> None:
    """Register the `release` subcommand tree onto ``sub``."""
    release = sub.add_parser(
        "release",
        help=(
            "Read-only release helper surfaces (`check tree|scaffold|"
            "artifact|workflow`, `workflow runs|wait`). Helpers do not "
            "dispatch workflows, bump versions, commit, push, tag, or "
            "create GitHub releases."
        ),
    )
    release_sub = release.add_subparsers(dest="release_command", required=True)

    release_check = release_sub.add_parser(
        "check",
        help="Read-only release guardrail checks (tree / scaffold / artifact / workflow)",
    )
    release_check_sub = release_check.add_subparsers(
        dest="release_check_command", required=True
    )

    release_check_tree = release_check_sub.add_parser(
        "tree",
        help=(
            "Run Source Tree Hygiene from release-flow.md. Strict-fail on "
            "personal home paths or secret-shape tokens in tracked files."
        ),
    )
    add_repo_option(release_check_tree)
    release_check_tree.set_defaults(func=cmd_release_check_tree)

    release_check_scaffold = release_check_sub.add_parser(
        "scaffold",
        help=(
            "Run Fresh Scaffold Smoke for every preset in an isolated home "
            "and target. Strict-fail on host-path leakage, missing portable "
            "rule path, or scaffold-status drift."
        ),
    )
    release_check_scaffold.set_defaults(func=cmd_release_check_scaffold)

    release_check_artifact = release_check_sub.add_parser(
        "artifact",
        help=(
            "Run python -m build, extract every produced artifact, and scan "
            "for personal home paths and secret-shape tokens. Strict-fail on "
            "any match; the operator records false-positive disposition in "
            "Asana before re-running."
        ),
    )
    add_repo_option(release_check_artifact)
    release_check_artifact.set_defaults(func=cmd_release_check_artifact)

    release_check_drift = release_check_sub.add_parser(
        "drift",
        help=(
            "Run canonical renderer + plugin mirror drift gates as one "
            "release check. Reproduces `mozyo-bridge scaffold canonical "
            "--check` (router pair + governed workflow pair) and "
            "`scripts/sync_plugin_skill.sh --check` (plugin mirror). "
            "Strict-fail on either drift; recovery hints name the "
            "real CLI commands operators copy-paste."
        ),
    )
    add_repo_option(release_check_drift)
    release_check_drift.set_defaults(func=cmd_release_check_drift)

    release_check_workflow = release_check_sub.add_parser(
        "workflow",
        help=(
            "Fetch a single GitHub Actions run's status and conclusion via "
            "`gh run view`. No dispatch, no judgment; success exits 0 and "
            "every other state exits non-zero."
        ),
    )
    release_check_workflow.add_argument(
        "--run-id",
        dest="run_id",
        required=True,
        help="GitHub Actions run id to inspect (databaseId, not the URL fragment)",
    )
    release_check_workflow.set_defaults(func=cmd_release_check_workflow)

    release_workflow = release_sub.add_parser(
        "workflow",
        help="GitHub Actions polling / summary helpers (read-only)",
    )
    release_workflow_sub = release_workflow.add_subparsers(
        dest="release_workflow_command", required=True
    )

    release_workflow_runs = release_workflow_sub.add_parser(
        "runs",
        help=(
            "List the most recent runs of a workflow with created_at / "
            "status / conclusion / head_sha / html_url."
        ),
    )
    release_workflow_runs.add_argument(
        "--workflow",
        required=True,
        help="Workflow file name or id (e.g. `testpypi.yml`, `publish.yml`, `Test`)",
    )
    release_workflow_runs.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of runs to list (default 10)",
    )
    release_workflow_runs.set_defaults(func=cmd_release_workflow_runs)

    release_workflow_wait = release_workflow_sub.add_parser(
        "wait",
        help=(
            "Poll a single run-id until it reaches `completed` or until "
            "--timeout elapses. Resumable; no judgment. Exit 124 on timeout."
        ),
    )
    release_workflow_wait.add_argument(
        "--run-id",
        dest="run_id",
        required=True,
        help="GitHub Actions run id to wait on",
    )
    release_workflow_wait.add_argument(
        "--timeout",
        type=float,
        required=True,
        help="Maximum seconds to wait before exiting with code 124",
    )
    release_workflow_wait.add_argument(
        "--poll",
        type=float,
        default=5.0,
        help="Polling interval in seconds (default 5.0)",
    )
    release_workflow_wait.set_defaults(func=cmd_release_workflow_wait)

    release_bump = release_sub.add_parser(
        "bump",
        help=(
            "Atomically rewrite the contract-declared release-version "
            "mirror set in the worktree (`--to VERSION`) or print its "
            "current state (`--check`). Never commits, pushes, or tags."
        ),
    )
    add_repo_option(release_bump)
    bump_mode = release_bump.add_mutually_exclusive_group(required=True)
    bump_mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read-only: print each mirror file's current version literal, "
            "the latest `Release vX.Y.Z` commit, and the `v*` tag list. "
            "Exits non-zero when mirror-set values disagree."
        ),
    )
    bump_mode.add_argument(
        "--to",
        metavar="VERSION",
        help=(
            "Rewrite every mirror-set file to VERSION in the worktree. "
            "Strict-fail if any mirror-set file's version literal cannot "
            "be located. Idempotent on same value. Operator still owns "
            "`git commit` / `git push` / `git tag -a`."
        ),
    )
    release_bump.set_defaults(func=cmd_release_bump)

    release_publish = release_sub.add_parser(
        "publish",
        help=(
            "Release publish helpers: TestPyPI workflow dispatch, "
            "production GitHub Release trigger (default dry-run), and "
            "plan summarization. No GA/beta judgment is automated."
        ),
    )
    add_repo_option(release_publish)
    publish_mode = release_publish.add_mutually_exclusive_group(required=True)
    publish_mode.add_argument(
        "--testpypi",
        action="store_true",
        help=(
            "Dispatch the exact-candidate TestPyPI workflow via `gh "
            "workflow run testpypi.yml --ref main` with the exact "
            "reviewed candidate as inputs (Redmine #13601). The workflow "
            "event ref stays `main`; --source-sha is the artifact "
            "authority, --expected-version the version it must carry, and "
            "--source-ref the approved origin ref it must currently "
            "resolve from. All three are required. The run is correlated "
            "to a unique dispatch nonce (no latest-one guessing); polling "
            "is delegated to `release workflow wait`."
        ),
    )
    publish_mode.add_argument(
        "--pypi",
        action="store_true",
        help=(
            "Assemble the `gh release create vX.Y.Z --verify-tag "
            "--title vX.Y.Z --notes-file PATH` invocation. Default "
            "dry-run; --execute required to actually create the "
            "GitHub Release. Requires --tag and --notes-file."
        ),
    )
    publish_mode.add_argument(
        "--plan",
        action="store_true",
        help=(
            "Enumerate operator-takeable options based on current git "
            "ref / pyproject version / latest `Test` workflow run / "
            "TestPyPI existing version. No judgment."
        ),
    )
    release_publish.add_argument(
        "--source-sha",
        dest="source_sha",
        help=(
            "Exact 40-hex commit SHA to build and publish under `--testpypi` "
            "(artifact authority; validated as an immutable full SHA)."
        ),
    )
    release_publish.add_argument(
        "--expected-version",
        dest="expected_version",
        help=(
            "Exact package version X.Y.Z that --source-sha must carry in both "
            "mirror files, passed to the workflow as a fail-closed gate under "
            "`--testpypi`."
        ),
    )
    release_publish.add_argument(
        "--source-ref",
        dest="source_ref",
        help=(
            "Approved origin integration/release-candidate ref that must "
            "currently resolve to --source-sha (lineage evidence) under "
            "`--testpypi`."
        ),
    )
    release_publish.add_argument(
        "--version",
        help=(
            "Deprecated alias for --expected-version under `--testpypi` "
            "(kept for backward compatibility)."
        ),
    )
    release_publish.add_argument(
        "--tag",
        help="Annotated tag `vX.Y.Z` for `--pypi` GitHub Release",
    )
    release_publish.add_argument(
        "--notes-file",
        dest="notes_file",
        help=(
            "Path to the release notes markdown file passed to "
            "`gh release create --notes-file`. Required for `--pypi`."
        ),
    )
    release_publish.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Required to actually invoke `gh release create` under "
            "`--pypi`. Without this flag the helper only prints the "
            "command it would run."
        ),
    )
    release_publish.set_defaults(func=cmd_release_publish)
