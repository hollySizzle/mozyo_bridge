"""Read-only release helper surfaces.

This module implements the helper command families admitted by
``vibes/docs/logics/release-helper-contract.md``: `release check tree`,
`release check scaffold`, `release check artifact`, `release check workflow`,
`release workflow runs`, and `release workflow wait`. All helpers are
read-only / no-dispatch / no-mutation; they inspect git state, build
artifacts, scaffold state, and GitHub Actions run status without altering the
worktree, remote refs, PyPI artifacts, or workflow dispatch state.

Mutation-bearing helpers (``release bump``, ``release publish``) are out of
scope for this module and live in a separate follow-up task.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

from mozyo_bridge.scaffold.rules import (
    MANIFEST_RELATIVE_PATH,
    PRESETS,
    install_rules,
    portable_rule_path,
    scaffold_status,
    write_scaffold,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import resolve_repo_root


EXIT_CLEAN = 0
EXIT_BLOCKER = 1
EXIT_TIMEOUT = 124


# Personal home / secret-shape patterns shared between source-tree and
# artifact checks. The release-flow.md grep adds `.env` to the source tree
# pattern but not to the artifact pattern (artifacts never carry `.env`
# files), so the two callers compose their own pattern from these
# constants.
_PERSONAL_PATH_PATTERNS = (
    r"/Users/",
    r"/home/[^/]+/",
    r"C:\\Users\\",
)
_SECRET_SHAPE_PATTERNS = (
    r"pypirc",
    r"token",
    r"secret",
    r"password",
)


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _require_command(executable: str, *, hint: str | None = None) -> None:
    if shutil.which(executable) is None:
        suffix = f" ({hint})" if hint else ""
        die(f"required executable not found in PATH: {executable}{suffix}")


def _print_section(title: str) -> None:
    print(f"## {title}")


# ---------------------------------------------------------------------------
# release check tree
# ---------------------------------------------------------------------------


def _git_grep_pathspecs() -> list[str]:
    # Match the release-flow.md grep: exclude generated / vendored trees so
    # the helper does not flag false positives that operators already filter
    # out by hand.
    return [
        ":!*.pyc",
        ":!build",
        ":!dist",
        ":!.git",
        ":!.venv",
        ":!tmp",
    ]


def _tree_grep_pattern() -> str:
    return "|".join(
        list(_PERSONAL_PATH_PATTERNS) + [r"\.env"] + list(_SECRET_SHAPE_PATTERNS)
    )


def cmd_release_check_tree(args: argparse.Namespace) -> int:
    """Reproduce the Source Tree Hygiene block of `release-flow.md`.

    Strict-fail on any personal-path or secret-shape hit so the operator
    cannot accidentally release a tree carrying host-specific paths or
    credential-shape tokens. `git status --short --branch` and the historical
    `git log -S'/Users/'` listing are printed for audit context; they do not
    on their own cause exit non-zero.
    """
    _require_command("git")
    repo_root = resolve_repo_root(getattr(args, "repo", None))

    blockers: list[str] = []

    _print_section("git status")
    status = _run(["git", "status", "--short", "--branch"], cwd=repo_root)
    if status.stdout:
        print(status.stdout, end="" if status.stdout.endswith("\n") else "\n")
    if status.returncode != 0:
        # `git status` exiting non-zero means we are not inside a git
        # checkout, which is itself a release blocker (the helper cannot
        # vouch for the tree).
        if status.stderr:
            print(status.stderr, end="" if status.stderr.endswith("\n") else "\n")
        blockers.append("git status failed")

    _print_section("git log -S'/Users/' (audit context)")
    log = _run(
        [
            "git",
            "log",
            "--all",
            "-S/Users/",
            "--",
            "AGENTS.md",
            "CLAUDE.md",
            "src",
            "skills",
            "vibes",
            "README.md",
            "pyproject.toml",
        ],
        cwd=repo_root,
    )
    if log.stdout:
        print(log.stdout, end="" if log.stdout.endswith("\n") else "\n")
    else:
        print("(no history hits)")

    _print_section("git grep (release blocker)")
    pattern = _tree_grep_pattern()
    grep = _run(
        ["git", "grep", "-nE", pattern, "--", *_git_grep_pathspecs()],
        cwd=repo_root,
    )
    # `git grep` exits 0 on hit and 1 on no-hit. Anything else is an
    # invocation error.
    if grep.returncode == 0 and grep.stdout:
        print(grep.stdout, end="" if grep.stdout.endswith("\n") else "\n")
        blockers.append("git grep hit personal path or secret-shape token")
    elif grep.returncode == 1:
        print("(no matches)")
    else:
        if grep.stderr:
            print(grep.stderr, end="" if grep.stderr.endswith("\n") else "\n")
        blockers.append("git grep failed")

    if blockers:
        print("")
        print("result: blocker")
        for item in blockers:
            print(f"- {item}")
        return EXIT_BLOCKER
    print("")
    print("result: clean")
    return EXIT_CLEAN


# ---------------------------------------------------------------------------
# release check scaffold
# ---------------------------------------------------------------------------


def _grep_personal_paths_in(paths: Iterable[Path]) -> list[tuple[Path, int, str]]:
    pattern = re.compile("|".join(_PERSONAL_PATH_PATTERNS))
    hits: list[tuple[Path, int, str]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append((path, lineno, line))
    return hits


def cmd_release_check_scaffold(args: argparse.Namespace) -> int:
    """Reproduce Fresh Scaffold Smoke from `release-flow.md`.

    For each supported preset, scaffold into an isolated tmp home and tmp
    target, then assert (a) the generated router files do not leak a host
    home path, (b) they contain the portable
    ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}`` rule path expansion, and (c)
    ``scaffold status`` reports clean. Strict-fail on the first assertion
    miss across presets so the operator does not get a partial-pass signal.
    """
    blockers: list[str] = []

    with tempfile.TemporaryDirectory(prefix="mozyo-release-scaffold-") as tmp_str:
        tmp = Path(tmp_str)
        home = tmp / "home"
        home.mkdir(parents=True, exist_ok=True)

        _print_section("rules install --home <tmp>")
        try:
            installed = install_rules(home)
        except SystemExit as exc:
            print(f"rules install failed: {exc}")
            return EXIT_BLOCKER
        for path in installed:
            print(f"installed: {path}")
        if not installed:
            print("rules: already up to date")

        for preset in PRESETS:
            _print_section(f"scaffold rules {preset}")
            project = tmp / f"project-{preset}"
            project.mkdir(parents=True, exist_ok=True)
            try:
                written = write_scaffold(preset, project, home=home)
            except SystemExit as exc:
                print(f"scaffold rules {preset} failed: {exc}")
                blockers.append(f"scaffold rules {preset} failed")
                continue
            for path in written:
                print(f"wrote: {path}")

            agents_md = project / "AGENTS.md"
            claude_md = project / "CLAUDE.md"
            manifest = project / MANIFEST_RELATIVE_PATH

            host_hits = _grep_personal_paths_in([agents_md, claude_md, manifest])
            if host_hits:
                for path, lineno, line in host_hits:
                    print(f"host-path-hit: {path}:{lineno}: {line.rstrip()}")
                blockers.append(f"{preset}: host-path leak in scaffold output")

            portable_marker = portable_rule_path(preset)
            for required in (agents_md, claude_md, manifest):
                if not required.exists():
                    blockers.append(f"{preset}: missing {required.name}")
                    continue
                content = required.read_text(encoding="utf-8")
                if portable_marker not in content:
                    print(
                        f"portable-marker-missing: {required.name} does not "
                        f"contain {portable_marker!r}"
                    )
                    blockers.append(
                        f"{preset}: portable rule path missing in {required.name}"
                    )

            status = scaffold_status(project, home=home)
            if status.get("clean"):
                print(f"scaffold status: clean ({preset})")
            else:
                print(
                    "scaffold status: drift detected "
                    f"({preset}); central_status="
                    f"{status.get('central_status')!r}"
                )
                blockers.append(f"{preset}: scaffold status not clean")

    print("")
    if blockers:
        print("result: blocker")
        for item in blockers:
            print(f"- {item}")
        return EXIT_BLOCKER
    print("result: clean")
    return EXIT_CLEAN


# ---------------------------------------------------------------------------
# release check artifact
# ---------------------------------------------------------------------------


def _artifact_grep_pattern() -> str:
    return "|".join(list(_PERSONAL_PATH_PATTERNS) + list(_SECRET_SHAPE_PATTERNS))


def _extract_artifact(artifact: Path, dest: Path) -> Path:
    name = artifact.name
    target = dest / artifact.stem
    target.mkdir(parents=True, exist_ok=True)
    if name.endswith(".whl"):
        with zipfile.ZipFile(artifact) as zf:
            zf.extractall(target)
    elif name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(artifact, "r:gz") as tf:
            tf.extractall(target)
    else:
        die(f"unsupported artifact shape: {artifact}")
    return target


def _grep_artifact_tree(root: Path, pattern: re.Pattern[str]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append((path, lineno, line))
    return hits


def cmd_release_check_artifact(args: argparse.Namespace) -> int:
    """Reproduce Build Artifact Inspection from `release-flow.md`.

    Honors the `release check` family's read-only / no-mutation invariant:
    the helper never touches the repo's ``dist/`` directory. Instead it
    asks ``python -m build`` to write into an isolated tmp outdir, then
    extracts every produced wheel / sdist and scans the extracted trees
    for personal home paths and secret-shape tokens. The scan is
    strict-fail; matches are printed so the operator can record
    disposition in the Asana task. False-positive disposition stays with
    the operator — the helper does not auto-dismiss any hit.
    """
    repo_root = resolve_repo_root(getattr(args, "repo", None))

    blockers: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mozyo-release-artifact-") as tmp_str:
        tmp = Path(tmp_str)
        build_outdir = tmp / "dist"
        build_outdir.mkdir(parents=True, exist_ok=True)
        extract_root = tmp / "extracted"
        extract_root.mkdir(parents=True, exist_ok=True)

        _print_section("python -m build --outdir <tmp>")
        print(f"outdir: {build_outdir}")
        build = _run(
            ["python", "-m", "build", "--outdir", str(build_outdir)],
            cwd=repo_root,
        )
        if build.stdout:
            print(build.stdout, end="" if build.stdout.endswith("\n") else "\n")
        if build.returncode != 0:
            if build.stderr:
                print(build.stderr, end="" if build.stderr.endswith("\n") else "\n")
            print("")
            print("result: blocker")
            print("- python -m build failed")
            return EXIT_BLOCKER

        artifacts = sorted(p for p in build_outdir.iterdir() if p.is_file())
        _print_section("dist artifacts")
        for artifact in artifacts:
            print(f"artifact: {artifact}")
        if not artifacts:
            print("")
            print("result: blocker")
            print("- python -m build produced no artifacts")
            return EXIT_BLOCKER

        pattern = re.compile(_artifact_grep_pattern())
        for artifact in artifacts:
            extracted = _extract_artifact(artifact, extract_root)
            _print_section(f"scan {artifact.name}")
            hits = _grep_artifact_tree(extracted, pattern)
            if not hits:
                print("(no matches)")
                continue
            for path, lineno, line in hits:
                rel = path.relative_to(extract_root)
                print(f"{rel}:{lineno}: {line.rstrip()}")
            blockers.append(
                f"{artifact.name}: personal path or secret-shape match"
            )

    print("")
    if blockers:
        print("result: blocker (false-positive disposition stays with operator)")
        for item in blockers:
            print(f"- {item}")
        return EXIT_BLOCKER
    print("result: clean")
    return EXIT_CLEAN


# ---------------------------------------------------------------------------
# release check workflow / release workflow runs / release workflow wait
# ---------------------------------------------------------------------------


_GH_HINT = "install GitHub CLI: https://cli.github.com/"


def _gh_run_view(run_id: str) -> dict[str, object]:
    _require_command("gh", hint=_GH_HINT)
    result = _run(
        [
            "gh",
            "run",
            "view",
            run_id,
            "--json",
            "status,conclusion,databaseId,headSha,workflowName,htmlUrl,createdAt,updatedAt",
        ]
    )
    if result.returncode != 0:
        die(
            "gh run view failed for run-id "
            f"{run_id!r}: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        die(f"gh run view returned non-JSON output: {exc}")
        raise AssertionError("unreachable")
    if not isinstance(payload, dict):
        die("gh run view returned non-object JSON")
    return payload


def _workflow_exit_code(status: str | None, conclusion: str | None) -> int:
    # Per the release-helper contract:
    #   "observed failure を non-zero exit で返すだけ".
    # Map success to clean, every other terminal/non-terminal state to
    # non-zero so calling scripts can distinguish "green" from "not green".
    if status == "completed" and conclusion == "success":
        return EXIT_CLEAN
    return EXIT_BLOCKER


def cmd_release_check_workflow(args: argparse.Namespace) -> int:
    """Print run status / conclusion for a single GitHub Actions run.

    No judgment is performed: ``success`` exits 0, every other state exits
    non-zero. Operator decides whether to re-run, accept, or rollback.
    """
    payload = _gh_run_view(args.run_id)
    status = payload.get("status")
    conclusion = payload.get("conclusion")
    workflow_name = payload.get("workflowName")
    head_sha = payload.get("headSha")
    url = payload.get("htmlUrl")
    print(f"run_id: {args.run_id}")
    print(f"workflow: {workflow_name}")
    print(f"status: {status}")
    print(f"conclusion: {conclusion}")
    print(f"head_sha: {head_sha}")
    print(f"html_url: {url}")
    return _workflow_exit_code(
        status if isinstance(status, str) else None,
        conclusion if isinstance(conclusion, str) else None,
    )


def cmd_release_workflow_runs(args: argparse.Namespace) -> int:
    """List recent runs of a workflow with the columns the contract names."""
    _require_command("gh", hint=_GH_HINT)
    fields = "databaseId,createdAt,status,conclusion,headSha,url,workflowName"
    result = _run(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            args.workflow,
            "--limit",
            str(args.limit),
            "--json",
            fields,
        ]
    )
    if result.returncode != 0:
        die(
            "gh run list failed for workflow "
            f"{args.workflow!r}: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        die(f"gh run list returned non-JSON output: {exc}")
        raise AssertionError("unreachable")
    if not isinstance(runs, list):
        die("gh run list returned non-array JSON")
    print("RUN_ID\tCREATED_AT\tSTATUS\tCONCLUSION\tHEAD_SHA\tHTML_URL")
    for entry in runs:
        if not isinstance(entry, dict):
            continue
        row = [
            str(entry.get("databaseId") or ""),
            str(entry.get("createdAt") or ""),
            str(entry.get("status") or ""),
            str(entry.get("conclusion") or ""),
            str(entry.get("headSha") or ""),
            str(entry.get("url") or ""),
        ]
        print("\t".join(row))
    return EXIT_CLEAN


def cmd_release_workflow_wait(args: argparse.Namespace) -> int:
    """Poll a single run-id until ``completed`` or until --timeout elapses.

    Exits with the canonical timeout code (124) when the run does not reach
    ``completed`` in time. Otherwise mirrors ``release check workflow``: 0
    on ``success`` and non-zero on every other terminal conclusion.
    """
    _require_command("gh", hint=_GH_HINT)
    deadline = time.monotonic() + float(args.timeout)
    poll = max(1.0, float(getattr(args, "poll", 5.0) or 5.0))
    last_status: str | None = None
    last_conclusion: str | None = None
    while time.monotonic() < deadline:
        payload = _gh_run_view(args.run_id)
        status = payload.get("status")
        conclusion = payload.get("conclusion")
        last_status = status if isinstance(status, str) else None
        last_conclusion = conclusion if isinstance(conclusion, str) else None
        if last_status == "completed":
            print(f"status: {last_status}")
            print(f"conclusion: {last_conclusion}")
            return _workflow_exit_code(last_status, last_conclusion)
        time.sleep(poll)
    print(f"status: {last_status}")
    print(f"conclusion: {last_conclusion}")
    print(f"timeout: exceeded {args.timeout}s without reaching completed")
    return EXIT_TIMEOUT
