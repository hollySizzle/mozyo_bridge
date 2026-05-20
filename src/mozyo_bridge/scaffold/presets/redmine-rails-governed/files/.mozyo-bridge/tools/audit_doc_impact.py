"""Audit doc impact across changed paths.

Distributed by the mozyo-bridge `redmine-rails-governed` preset.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from docs_catalog import CATALOG_PATH, REPO_ROOT, load_catalog, resolve_audit_documents


IGNORED_PATH_PARTS = {".git", "__pycache__"}
IGNORED_SUFFIXES = {".pyc"}
GENERATE_TOOL = REPO_ROOT / ".mozyo-bridge/tools/generate_file_conventions.py"


def should_skip_path(path: str) -> bool:
    parts = set(Path(path).parts)
    if parts & IGNORED_PATH_PARTS:
        return True
    return Path(path).suffix in IGNORED_SUFFIXES


def git_changed_paths(staged: bool, all_changed: bool) -> list[str]:
    commands: list[list[str]] = []
    if staged:
        commands.append(["git", "diff", "--cached", "--name-only"])
    if all_changed:
        commands.append(["git", "diff", "--name-only"])
        commands.append(["git", "ls-files", "--others", "--exclude-standard"])
    if not commands:
        commands.append(["git", "diff", "--name-only"])

    paths: list[str] = []
    seen: set[str] = set()
    for command in commands:
        output = subprocess.check_output(command, cwd=REPO_ROOT, text=True)
        for line in output.splitlines():
            path = line.strip()
            if not path or path in seen or should_skip_path(path):
                continue
            seen.add(path)
            paths.append(path)
    return paths


def render_results(paths: list[str]) -> int:
    catalog = load_catalog(CATALOG_PATH)
    if not paths:
        print("No changed paths.")
        return 0

    for path in paths:
        result = resolve_audit_documents(catalog, path)
        print(f"[{result['path']}]")
        documents = result["documents"]
        if documents:
            print("documents_to_read:")
            for document in documents:
                sources = ", ".join(document["sources"])
                print(
                    f"- {document['type']} {document['id']} -> {document['canonical_path']} (source: {sources})"
                )
        else:
            print("documents_to_read:")
            print("- none")
        if result["notes"]:
            print("notes:")
            for note in result["notes"]:
                print(f"- {note}")
        print()
    return 0


def run_generated_check(generated_output: str | None) -> int:
    command = ["python3", str(GENERATE_TOOL), "--check"]
    if generated_output:
        command.extend(["--output", generated_output])
    return subprocess.call(command, cwd=REPO_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="Show docs impacted by changed files")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="Use staged changes")
    group.add_argument("--all-changed", action="store_true", help="Use unstaged and untracked changes")
    parser.add_argument(
        "--check-generated",
        action="store_true",
        help="Check that the generated file_conventions output is up to date",
    )
    parser.add_argument(
        "--generated-output",
        help="Override the generated file_conventions output path for the drift check",
    )
    args = parser.parse_args()

    paths = git_changed_paths(staged=args.staged, all_changed=args.all_changed)
    result = render_results(paths)
    if result != 0:
        return result
    if args.check_generated:
        return run_generated_check(args.generated_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
