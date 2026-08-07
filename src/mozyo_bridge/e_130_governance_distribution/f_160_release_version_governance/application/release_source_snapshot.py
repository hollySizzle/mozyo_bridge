"""Isolated current-source build for the read-only artifact release check."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ArtifactExtractor = Callable[[Path, Path], Path]
ArtifactScanner = Callable[
    [Path, re.Pattern[str]], list[tuple[Path, int, str]]
]


def _copy_release_source_snapshot(
    repo_root: Path, destination: Path, *, run: RunCommand
) -> str | None:
    """Copy tracked plus non-ignored current files without touching the source."""
    listed = run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=repo_root,
    )
    if listed.returncode != 0:
        detail = (
            listed.stderr.strip()
            or listed.stdout.strip()
            or "git ls-files failed"
        )
        return f"cannot enumerate release source: {detail}"

    entries: list[tuple[str, Path]] = []
    for raw in dict.fromkeys(listed.stdout.split("\0")):
        if not raw:
            continue
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            return f"unsafe release source path from git: {raw!r}"
        entries.append((raw, relative))

    listed_parent_paths = frozenset(
        parent
        for _raw, relative in entries
        for parent in relative.parents
        if parent != Path(".")
    )
    resolved_repo = repo_root.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for raw, relative in entries:
        source = repo_root / relative
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_dir() and not source.is_symlink():
            if relative in listed_parent_paths:
                # A deleted cached file may now be a non-ignored directory.
                # Git lists both the stale cached path and its current children;
                # materialize the children and skip only this container entry.
                continue
            return f"release source entry is not a file or symlink: {raw!r}"
        if source.is_symlink():
            try:
                link_target = os.readlink(source)
            except OSError as exc:
                return f"cannot inspect release source symlink {raw!r}: {exc}"
            if Path(link_target).is_absolute():
                return f"absolute symlink is unsafe in release source: {raw!r}"
            try:
                resolved_target = source.resolve(strict=False)
                resolved_target.relative_to(resolved_repo)
            except (OSError, RuntimeError, ValueError):
                return (
                    "release source symlink escapes the repository snapshot: "
                    f"{raw!r}"
                )
        elif not source.is_file():
            return f"release source entry is not a file or symlink: {raw!r}"
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target, follow_symlinks=False)
        except OSError as exc:
            return f"cannot copy release source {raw!r}: {exc}"
    return None


def run_artifact_check(
    *,
    repo_root: Path,
    run: RunCommand,
    extract_artifact: ArtifactExtractor,
    scan_artifact: ArtifactScanner,
    personal_path_patterns: Sequence[str],
    python_executable: str | None = None,
) -> int:
    """Build and scan a temporary source snapshot; return 0 clean or 1 blocker."""
    blockers: list[str] = []
    executable = python_executable or sys.executable
    with tempfile.TemporaryDirectory(prefix="mozyo-release-artifact-") as tmp_str:
        tmp = Path(tmp_str)
        source_root = tmp / "source"
        build_outdir = tmp / "dist"
        extract_root = tmp / "extracted"
        build_outdir.mkdir(parents=True, exist_ok=True)
        extract_root.mkdir(parents=True, exist_ok=True)

        snapshot_error = _copy_release_source_snapshot(
            repo_root, source_root, run=run
        )
        if snapshot_error is not None:
            print("result: blocker")
            print(f"- {snapshot_error}")
            return 1

        print("## python -m build --outdir <tmp>")
        print(f"outdir: {build_outdir}")
        build = run(
            [executable, "-m", "build", "--outdir", str(build_outdir)],
            cwd=source_root,
        )
        if build.stdout:
            print(build.stdout, end="" if build.stdout.endswith("\n") else "\n")
        if build.returncode != 0:
            if build.stderr:
                print(build.stderr, end="" if build.stderr.endswith("\n") else "\n")
            print("\nresult: blocker\n- python -m build failed")
            return 1

        artifacts = sorted(
            path for path in build_outdir.iterdir() if path.is_file()
        )
        print("## dist artifacts")
        for artifact in artifacts:
            print(f"artifact: {artifact}")
        if not artifacts:
            print("\nresult: blocker\n- python -m build produced no artifacts")
            return 1

        personal_pattern = re.compile("|".join(personal_path_patterns))
        for artifact in artifacts:
            extracted = extract_artifact(artifact, extract_root)
            print(f"## scan {artifact.name}")
            hits = scan_artifact(extracted, personal_pattern)
            if not hits:
                print("(no matches)")
                continue
            for path, lineno, line in hits:
                relative = path.relative_to(extract_root)
                print(f"{relative}:{lineno}: {line.rstrip()}")
            blockers.append(
                f"{artifact.name}: personal path or secret-shape match"
            )

    print("")
    if blockers:
        print("result: blocker (false-positive disposition stays with operator)")
        for item in blockers:
            print(f"- {item}")
        return 1
    print("result: clean")
    return 0


__all__ = ("run_artifact_check",)
