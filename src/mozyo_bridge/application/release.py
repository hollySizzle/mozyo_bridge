"""Release helper surfaces.

This module implements the helper command families admitted by
``vibes/docs/logics/release-helper-contract.md``:

- read-only checks: `release check tree`, `release check scaffold`,
  `release check artifact`, `release check workflow`;
- read-only workflow polling: `release workflow runs`, `release workflow wait`;
- bounded-mutation bump: `release bump --check`, `release bump --to <version>`;
- bounded-mutation publish: `release publish --testpypi --version <X.Y.Z>`,
  `release publish --pypi --tag vX.Y.Z [--execute]`,
  `release publish --plan`.

`release bump --to` only rewrites files in the contract-declared mirror set
(read at runtime from ``release-helper-contract.md`` — never hardcoded).
`release publish --pypi` is dry-run by default and only invokes
``gh release create`` when ``--execute`` is passed explicitly. No helper
commits, pushes, tags, rolls back, or judges GA vs beta.
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


# ---------------------------------------------------------------------------
# Release-version mirror set
# ---------------------------------------------------------------------------


_CONTRACT_DOC_RELATIVE = Path("vibes/docs/logics/release-helper-contract.md")
_MIRROR_SET_ANCHOR = "release-version mirror set は以下の"

# Per-file-extension version field handlers. The set of file extensions
# accepted here is the helper's interpretation surface; the SET OF FILES
# is read from the contract doc at runtime. Adding a new mirror file
# whose extension is not represented here will strict-fail at
# `_load_mirror_set`, so the helper cannot silently mutate an
# unrecognized file shape.
_MIRROR_KIND_HANDLERS: dict[str, dict[str, object]] = {
    ".toml": {
        "pattern": re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE),
        "format": 'version = "{value}"',
        "label": "[project].version",
    },
    ".py": {
        "pattern": re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE),
        "format": '__version__ = "{value}"',
        "label": "__version__",
    },
}


# Loose PEP 440 / SemVer hybrid recognizer. Tight enough to reject shell
# meta or paths, loose enough to admit the documented shapes
# (`0.1.0a1`, `0.1.0`, `0.1.0rc1`, `0.1.1`, etc.). The helper does not
# pick between alpha / beta / GA; it only validates the literal shape.
_VERSION_RE = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?$"
)
_TAG_RE = re.compile(
    r"^v[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?$"
)


def _validate_version(value: str) -> None:
    if not _VERSION_RE.match(value):
        die(
            f"version literal {value!r} does not match the accepted PEP 440 "
            "shape (`X.Y.Z`, `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN`, optional "
            "`.postN` / `.devN`)"
        )


def _validate_tag(value: str) -> None:
    if not _TAG_RE.match(value):
        die(
            f"tag {value!r} must match `vX.Y.Z` shape (optionally with "
            "`aN` / `bN` / `rcN` / `.postN` / `.devN` suffix)"
        )


def _parse_mirror_set_paths(contract_text: str) -> list[str]:
    """Extract the mirror-set bullet list from the contract doc.

    The contract doc names the mirror set as a bullet list immediately
    following the anchor phrase ``release-version mirror set は以下の``.
    Each bullet's first backtick-quoted token is the file path. The
    bullet list ends at the first blank line after the bullets start.
    """
    start = contract_text.find(_MIRROR_SET_ANCHOR)
    if start < 0:
        die(
            "release-helper-contract.md does not contain the mirror-set "
            f"anchor {_MIRROR_SET_ANCHOR!r}; update the contract before "
            "running release bump"
        )
    paths: list[str] = []
    bullet_started = False
    for line in contract_text[start:].splitlines():
        stripped = line.lstrip()
        if stripped.startswith("- `"):
            bullet_started = True
            after = stripped[3:]
            close = after.find("`")
            if close < 0:
                continue
            paths.append(after[:close])
        elif bullet_started and not stripped:
            break
    if not paths:
        die(
            "release-helper-contract.md mirror-set section has no bullet "
            "entries; cannot determine which files to operate on"
        )
    return paths


def _load_mirror_set(repo_root: Path) -> list[tuple[Path, dict[str, object]]]:
    """Return the contract-declared mirror set as `(absolute_path, handler)`.

    The set is read from ``release-helper-contract.md`` so it stays in
    lockstep with the contract; this is the same doc the contract requires
    the helper to follow when the set changes. Files whose extension is not
    represented in ``_MIRROR_KIND_HANDLERS`` are strict-fail rather than
    silently skipped — the helper would otherwise miss a contract-mandated
    target.
    """
    contract_path = repo_root / _CONTRACT_DOC_RELATIVE
    if not contract_path.exists():
        die(
            f"contract doc not found at {contract_path}; cannot determine "
            "the release-version mirror set"
        )
    try:
        contract_text = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"failed to read contract doc {contract_path}: {exc}")
        raise AssertionError("unreachable")
    paths = _parse_mirror_set_paths(contract_text)
    resolved: list[tuple[Path, dict[str, object]]] = []
    for raw in paths:
        path = repo_root / raw
        if not path.exists():
            die(
                f"contract names mirror set file {raw!r} but it does not "
                f"exist at {path}; update the contract or restore the file "
                "before running release bump"
            )
        ext = path.suffix.lower()
        handler = _MIRROR_KIND_HANDLERS.get(ext)
        if handler is None:
            die(
                f"contract names mirror set file {raw!r} but the helper has "
                f"no version-field handler for extension {ext!r}; update "
                "_MIRROR_KIND_HANDLERS together with the contract before "
                "running release bump"
            )
        resolved.append((path, handler))
    return resolved


def _extract_current_version(path: Path, handler: dict[str, object]) -> str:
    text = path.read_text(encoding="utf-8")
    pattern = handler["pattern"]
    assert isinstance(pattern, re.Pattern)
    match = pattern.search(text)
    if match is None:
        die(
            f"could not locate version literal in {path} using regex "
            f"{pattern.pattern!r}; mirror set may have drifted from the "
            "contract"
        )
        raise AssertionError("unreachable")
    return match.group(1)


def _replace_version(path: Path, handler: dict[str, object], new_version: str) -> bool:
    """Replace the version literal in `path`. Returns True if file changed."""
    text = path.read_text(encoding="utf-8")
    pattern = handler["pattern"]
    fmt = handler["format"]
    assert isinstance(pattern, re.Pattern) and isinstance(fmt, str)
    rewritten, count = pattern.subn(fmt.format(value=new_version), text, count=1)
    if count == 0:
        die(
            f"could not rewrite version literal in {path}; pattern "
            f"{pattern.pattern!r} did not match. Aborting before "
            "partially-mutated mirror set."
        )
    if rewritten == text:
        return False
    path.write_text(rewritten, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# release bump
# ---------------------------------------------------------------------------


def cmd_release_bump(args: argparse.Namespace) -> int:
    """Dispatch on the mutually-exclusive `--check` / `--to` mode flag.

    Per the contract, `release bump` is a single-purpose helper that only
    rewrites the authoritative release-version mirror set. The mode flag
    decides whether the helper inspects (`--check`) or rewrites (`--to`),
    and neither mode commits, pushes, or tags.
    """
    repo_root = resolve_repo_root(getattr(args, "repo", None))
    mirror = _load_mirror_set(repo_root)
    if getattr(args, "check", False):
        return _bump_check(repo_root, mirror)
    target = getattr(args, "to", None)
    if not target:
        die("release bump requires exactly one of --check or --to <version>")
    _validate_version(target)
    return _bump_to(repo_root, mirror, target)


def _bump_check(repo_root: Path, mirror: list[tuple[Path, dict[str, object]]]) -> int:
    _print_section("release-version mirror set (contract-declared)")
    values: list[tuple[Path, str]] = []
    for path, handler in mirror:
        current = _extract_current_version(path, handler)
        label = handler["label"]
        print(f"{path.relative_to(repo_root)}\t{label}\t{current}")
        values.append((path, current))

    _print_section("git tags (v*)")
    tags = _run(["git", "tag", "--list", "v*"], cwd=repo_root)
    if tags.stdout:
        print(tags.stdout, end="" if tags.stdout.endswith("\n") else "\n")
    else:
        print("(no v* tags)")

    _print_section("last release commit")
    log = _run(
        ["git", "log", "--oneline", "-1", "--grep=^Release v"],
        cwd=repo_root,
    )
    if log.stdout:
        print(log.stdout, end="" if log.stdout.endswith("\n") else "\n")
    else:
        print("(no `Release vX.Y.Z` commit found in current branch)")

    print("")
    distinct = {value for _path, value in values}
    if len(distinct) > 1:
        print("result: blocker (mirror set values disagree)")
        return EXIT_BLOCKER
    print("result: clean")
    return EXIT_CLEAN


def _bump_to(
    repo_root: Path,
    mirror: list[tuple[Path, dict[str, object]]],
    target: str,
) -> int:
    _print_section(f"release bump --to {target}")
    # Two-phase: extract every current version first (which `die`s on any
    # missing literal) so the helper never leaves the mirror set in a
    # partially-rewritten state. Only after all extracts succeed do we
    # write back.
    current_versions: list[tuple[Path, dict[str, object], str]] = []
    for path, handler in mirror:
        current_versions.append((path, handler, _extract_current_version(path, handler)))

    changed: list[Path] = []
    unchanged: list[Path] = []
    for path, handler, current in current_versions:
        if current == target:
            print(f"{path.relative_to(repo_root)}: already at {target} (no-op)")
            unchanged.append(path)
            continue
        if _replace_version(path, handler, target):
            print(
                f"{path.relative_to(repo_root)}: rewrote "
                f"{current} -> {target}"
            )
            changed.append(path)
        else:
            unchanged.append(path)

    _print_section("git status (post-bump)")
    status = _run(["git", "status", "--short"], cwd=repo_root)
    if status.stdout:
        print(status.stdout, end="" if status.stdout.endswith("\n") else "\n")
    else:
        print("(clean)")

    if changed:
        _print_section("git diff (post-bump)")
        diff = _run(
            ["git", "diff", "--", *[str(p.relative_to(repo_root)) for p in changed]],
            cwd=repo_root,
        )
        if diff.stdout:
            print(diff.stdout, end="" if diff.stdout.endswith("\n") else "\n")

    print("")
    if not changed:
        print(f"result: no-op (mirror set was already at {target})")
    else:
        print(
            "result: mirror set rewritten in worktree; operator owns the "
            "commit (`git commit -m \"Release v" + target + "\"`)"
        )
    return EXIT_CLEAN


# ---------------------------------------------------------------------------
# release publish
# ---------------------------------------------------------------------------


def _gh_dispatch_testpypi(version: str) -> dict[str, str]:
    _require_command("gh", hint=_GH_HINT)
    _validate_version(version)
    dispatch = _run(
        [
            "gh",
            "workflow",
            "run",
            "testpypi.yml",
            "--ref",
            "main",
            "-f",
            f"version={version}",
        ]
    )
    if dispatch.returncode != 0:
        die(
            "gh workflow run testpypi.yml failed: "
            f"{dispatch.stderr.strip() or dispatch.stdout.strip()}"
        )

    # `gh workflow run` does not return the run-id directly; surface it via a
    # follow-up list call. The most recent run on the workflow is the one we
    # just dispatched. The helper sleeps once to give the run a moment to
    # register before listing — this is best-effort and the operator can
    # always re-list via `release workflow runs --workflow testpypi.yml`.
    time.sleep(2)
    list_result = _run(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            "testpypi.yml",
            "--limit",
            "1",
            "--json",
            "databaseId,url,createdAt,headSha,status",
        ]
    )
    runs: list[dict[str, object]] = []
    if list_result.returncode == 0:
        try:
            payload = json.loads(list_result.stdout)
            if isinstance(payload, list):
                runs = [entry for entry in payload if isinstance(entry, dict)]
        except json.JSONDecodeError:
            runs = []
    if not runs:
        return {
            "run_id": "",
            "url": "",
            "created_at": "",
            "head_sha": "",
            "status": "",
        }
    entry = runs[0]
    return {
        "run_id": str(entry.get("databaseId") or ""),
        "url": str(entry.get("url") or ""),
        "created_at": str(entry.get("createdAt") or ""),
        "head_sha": str(entry.get("headSha") or ""),
        "status": str(entry.get("status") or ""),
    }


def _gh_release_create_command(
    tag: str, notes_file: Path, title: str | None = None
) -> list[str]:
    return [
        "gh",
        "release",
        "create",
        tag,
        "--verify-tag",
        "--title",
        title or tag,
        "--notes-file",
        str(notes_file),
    ]


def _publish_testpypi(args: argparse.Namespace) -> int:
    version = getattr(args, "version", None)
    if not version:
        die("release publish --testpypi requires --version <X.Y.Z>")
    info = _gh_dispatch_testpypi(version)
    _print_section("dispatched TestPyPI workflow")
    print(f"workflow: testpypi.yml")
    print(f"ref: main")
    print(f"version: {version}")
    print(f"run_id: {info['run_id'] or '(unresolved; re-run `release workflow runs --workflow testpypi.yml`)'}")
    print(f"url: {info['url']}")
    print(f"head_sha: {info['head_sha']}")
    print(f"status: {info['status']}")
    print("")
    print("Next: `mozyo-bridge release workflow wait --run-id "
          f"{info['run_id'] or '<run-id>'} --timeout <seconds>`")
    return EXIT_CLEAN


def _publish_pypi(args: argparse.Namespace) -> int:
    tag = getattr(args, "tag", None)
    notes_file = getattr(args, "notes_file", None)
    execute = bool(getattr(args, "execute", False))
    if not tag:
        die("release publish --pypi requires --tag vX.Y.Z")
    if not notes_file:
        die(
            "release publish --pypi requires --notes-file <path>; the helper "
            "does not author release notes"
        )
    _validate_tag(tag)
    notes_path = Path(notes_file).expanduser().resolve()
    if not notes_path.exists():
        die(f"release notes file does not exist: {notes_path}")
    if not notes_path.is_file():
        die(f"release notes path is not a file: {notes_path}")

    command = _gh_release_create_command(tag, notes_path)
    _print_section("release publish --pypi" + (" --execute" if execute else " (dry-run)"))
    print(f"tag: {tag}")
    print(f"notes_file: {notes_path}")
    print("command: " + " ".join(command))

    if not execute:
        print("")
        print(
            "result: dry-run (no GitHub Release created). Re-run with "
            "`--execute` to invoke `gh release create`."
        )
        return EXIT_CLEAN

    _require_command("gh", hint=_GH_HINT)
    result = _run(command)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
        print("")
        print("result: gh release create failed")
        return EXIT_BLOCKER
    print("")
    print(
        "result: GitHub Release created. Production publish workflow "
        "(`.github/workflows/publish.yml`) is fired by the `release: "
        "published` event; confirm via `release workflow runs --workflow "
        "publish.yml`."
    )
    return EXIT_CLEAN


def _testpypi_existing_version(version: str) -> str | None:
    """Return the TestPyPI publish status for `version`.

    Returns the string ``"present"`` when TestPyPI has the version,
    ``"absent"`` when the project exists but lacks the version,
    ``"project_missing"`` when the project itself is not on TestPyPI, or
    ``None`` on transport error. The helper does not judge — operator
    decides whether the result is acceptable.
    """
    import urllib.error  # local imports to keep top-level import surface
    import urllib.request

    url = "https://test.pypi.org/pypi/mozyo-bridge/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # nosec - public read-only endpoint
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "project_missing"
        return None
    except urllib.error.URLError:
        return None
    except (ValueError, OSError):
        return None
    releases = data.get("releases") if isinstance(data, dict) else None
    if not isinstance(releases, dict):
        return None
    return "present" if version in releases else "absent"


def _publish_plan(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(getattr(args, "repo", None))
    mirror = _load_mirror_set(repo_root)
    pyproject_path = None
    for path, _handler in mirror:
        if path.name == "pyproject.toml":
            pyproject_path = path
            break
    if pyproject_path is None:
        die(
            "release publish --plan expects `pyproject.toml` in the mirror "
            "set; contract may have drifted from the helper assumptions"
        )

    pyproject_handler = next(
        handler for path, handler in mirror if path == pyproject_path
    )
    current_version = _extract_current_version(pyproject_path, pyproject_handler)

    _print_section("git ref")
    head = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    print(f"head: {head.stdout.strip()}")
    print(f"branch: {branch.stdout.strip()}")

    _print_section("pyproject version")
    print(f"version: {current_version}")

    _print_section("latest `Test` workflow run")
    _require_command("gh", hint=_GH_HINT)
    test_runs = _run(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            "Test",
            "--limit",
            "1",
            "--json",
            "databaseId,createdAt,status,conclusion,headSha,url",
        ]
    )
    if test_runs.returncode == 0:
        try:
            entries = json.loads(test_runs.stdout)
        except json.JSONDecodeError:
            entries = []
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            entry = entries[0]
            print(f"run_id: {entry.get('databaseId')}")
            print(f"created_at: {entry.get('createdAt')}")
            print(f"status: {entry.get('status')}")
            print(f"conclusion: {entry.get('conclusion')}")
            print(f"head_sha: {entry.get('headSha')}")
            print(f"url: {entry.get('url')}")
        else:
            print("(no Test workflow runs found)")
    else:
        print(
            "(gh run list failed: "
            f"{test_runs.stderr.strip() or test_runs.stdout.strip()})"
        )

    _print_section("TestPyPI existing version check")
    testpypi_status = _testpypi_existing_version(current_version)
    if testpypi_status is None:
        print(
            f"version {current_version}: unknown (TestPyPI API unreachable; "
            "rerun later or check https://test.pypi.org/project/mozyo-bridge/)"
        )
    elif testpypi_status == "project_missing":
        print(f"version {current_version}: project not yet on TestPyPI")
    else:
        print(f"version {current_version}: {testpypi_status} on TestPyPI")

    _print_section("operator options")
    print(
        "- TestPyPI rehearsal: "
        f"`mozyo-bridge release publish --testpypi --version {current_version}`"
    )
    print(
        "- production publish dry-run: "
        f"`mozyo-bridge release publish --pypi --tag v{current_version} "
        "--notes-file <path>`"
    )
    print(
        "- production publish execute: append `--execute` to the dry-run "
        "command above (creates a GitHub Release; fires publish.yml)"
    )
    print(
        "- workflow polling: "
        "`mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>`"
    )
    print("")
    print("Helper does not judge GA vs beta vs patch. Choice stays with operator.")
    return EXIT_CLEAN


def cmd_release_publish(args: argparse.Namespace) -> int:
    """Dispatch on the mutually-exclusive mode flag.

    The contract enumerates exactly three mode flags
    (``--testpypi`` / ``--pypi`` / ``--plan``); the CLI enforces
    mutual exclusion at parse time, and per-mode required secondary
    args are validated here.
    """
    if getattr(args, "testpypi", False):
        return _publish_testpypi(args)
    if getattr(args, "pypi", False):
        return _publish_pypi(args)
    if getattr(args, "plan", False):
        return _publish_plan(args)
    die(
        "release publish requires exactly one of --testpypi / --pypi / --plan"
    )
    raise AssertionError("unreachable")
