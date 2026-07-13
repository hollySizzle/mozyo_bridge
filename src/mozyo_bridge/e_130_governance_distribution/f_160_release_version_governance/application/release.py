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
import sys
import tarfile
import tempfile
import time
import uuid
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
from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import (
    version_mirror,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import resolve_repo_root


EXIT_CLEAN = 0
EXIT_BLOCKER = 1
EXIT_TIMEOUT = 124


# Personal home / secret-shape patterns shared between source-tree and artifact
# checks — narrower than a raw `token|secret|password` word scan so the gate
# fails on leaks, not on release docs / scanner code discussing those terms.
_PERSONAL_PATH_PATTERNS = (
    r"/Users/[A-Za-z0-9._-]+/",
    r"/home/[A-Za-z0-9._-]+/",
    r"C:\\Users\\[A-Za-z0-9._-]+\\",
)
_SECRET_FILE_PATTERNS = (
    r"\.pypirc",
)
_SECRET_VALUE_PATTERNS = (
    r"(?i:\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\b\s*[:=]\s*[^<\s#][^\s#]*)",
    r"(?i:\b(?:ASANA|GITHUB|PYPI|TWINE|REDMINE)[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY)\b\s*[:=]\s*[^<\s#][^\s#]*)",
)
_TREE_SECRET_VALUE_PATTERNS = (
    r"(^|[^[:alnum:]_])([A-Za-z0-9]*_)*(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)(_ENV)?[[:space:]]*[:=][[:space:]]*[^<[:space:]#][^[:space:]#]*",
    r"(^|[^[:alnum:]_])([A-Za-z0-9]*_)*(ASANA|GITHUB|PYPI|TWINE|REDMINE)[A-Za-z0-9_]*(TOKEN|SECRET|PASSWORD|KEY)(_ENV)?[[:space:]]*[:=][[:space:]]*[^<[:space:]#][^[:space:]#]*",
)

# The grep patterns above cast a wide net for a single POSIX-ERE `git grep` /
# artifact pass; `_secret_assignment_is_real` is the second-stage classifier both
# scans post-filter through. The keyword sits in a *segment-bounded* identifier
# (prefix `(?:[A-Za-z0-9]*_)*` + only the `_ENV` suffix), so `_API_KEY` /
# `API_KEY_ENV:` match but a glued substring like `passwordless` does not; grep
# and classifier share this grammar so tree/artifact verdicts agree (R1-F2/R2-F2).
_SECRET_KEY_ALTERNATION = (
    # No leading `[A-Za-z0-9_]*` here: the outer prefix owns it (grep parity, R2-F2).
    r"(?:(?:ASANA|GITHUB|PYPI|TWINE|REDMINE)[A-Za-z0-9_]*"
    r"(?:TOKEN|SECRET|PASSWORD|KEY))"
    r"|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<key>(?:[A-Za-z0-9]*_)*(?:" + _SECRET_KEY_ALTERNATION + r")(?:_ENV)?)\s*[:=]\s*(?P<value>[^\s#]+)",
    re.IGNORECASE,
)
# Code-structure chars (call, index, dict, union, redirect) mark an expression;
# `.` / `/` survive so real token shapes do (JWTs, base64, `sk.live.…`; #12175
# j#60466), while dotted code refs go to `_is_attribute_path_reference`.
_SECRET_EXPRESSION_CHARS = frozenset("()[]{}|<>\\ \t")
# Python / JSON / shell literals that are never a credential value.
_SECRET_VALUE_KEYWORDS = frozenset(
    {"none", "true", "false", "null", "nil", "undefined", "..."}
)
# Case-insensitive substrings marking an explicit non-secret placeholder / sentinel.
_SECRET_PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "changeme",
    "change-me",
    "change_me",
    "redacted",
    "not-a-real",
    "not_a_real",
    "test-key",
    "test_key",
    "test-token",
    "test_token",
    "your-",
    "your_",
    "dummy",
    "sentinel",
    "sample",
    "fake",
    "xxxx",
)
# Credential-bearing env-var NAMES this project reads. A `*_ENV` constant bound
# to one of these names *where* a secret is read from, not the secret — the only
# value-side exemption. Shape can't separate an env-var name from a secret (both
# can be UPPER_SNAKE with a keyword), so this is an explicit allowlist, not a
# pattern; a new credential env var is flagged until added here (Redmine #13716
# R3-F1).
_KNOWN_CREDENTIAL_ENV_NAMES = frozenset({"MOZYO_REDMINE_API_KEY"})


def _is_attribute_path_reference(inner: str) -> bool:
    """True if `inner` is a dotted code reference rather than a token literal.

    A dotted code reference (``os.environ``, ``config.API_KEY``, ``self.api_key``)
    has every dot-separated segment a digit-free Python identifier. A non-ident
    or digit-bearing segment (``abc.def.123``, ``sk.live.abc123``, JWT chunks)
    makes it a literal, not a reference.
    """
    if "." not in inner:
        return False
    for segment in inner.split("."):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_]*", segment):
            return False
    return True


def _secret_value_is_real(value: str) -> bool:
    """Classify a captured assignment value as a real credential literal.

    Rejects code structure / expressions, keyword literals, placeholders, and
    bare identifier / constant / type / attribute references (an ``os.environ``
    read, ``None``, a ``str`` annotation, an uppercase constant, a test
    sentinel). Known env-var names under a ``*_ENV`` key are exempted in
    ``_secret_assignment_is_real``. Accepts an opaque literal — the non-placeholder
    RHS of an ``api_key`` / ``*_API_KEY`` assignment — INCLUDING token-shaped
    literals with credential punctuation (``.`` ``/`` ``+`` / padding ``=``: JWTs,
    base64); see #12175 / #13695 tests.
    """
    raw = value.strip().rstrip(",;)]}")
    quoted = False
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        inner = raw[1:-1]
        quoted = True
    else:
        # Strip a stray string delimiter (`None"` captured from `"api_key=None"`).
        inner = raw.strip("\"'")
    if not inner:
        return False
    # Punctuation-only values (stray quotes/separators) are never a leaked secret.
    if not any(ch.isalnum() for ch in inner):
        return False
    # Code-structure chars mark an expression; `.` / `/` excluded so tokens survive.
    if any(ch in _SECRET_EXPRESSION_CHARS for ch in raw):
        return False
    lowered = inner.lower()
    if lowered in _SECRET_VALUE_KEYWORDS:
        return False
    if any(marker in lowered for marker in _SECRET_PLACEHOLDER_MARKERS):
        return False
    # Unquoted name references aren't values (quoted / digit-bearing = literal).
    if not quoted:
        # Digit-free bare identifier = name reference (constant/type/env; #12693).
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", inner) and not any(
            ch.isdigit() for ch in inner
        ):
            return False
        if _is_attribute_path_reference(inner):  # os.environ, config.API_KEY
            return False
    return True


def _secret_assignment_is_real(content: str) -> bool:
    """True if any `key [:=] value` in `content` is a real literal. The only
    value-side exemption: a ``*_ENV`` key whose value is in the known env-name
    allowlist (`_KNOWN_CREDENTIAL_ENV_NAMES`). Membership — not shape — is the
    safe authority, so any other literal under a ``*_ENV`` key blocks (#13716)."""
    for match in _SECRET_ASSIGNMENT_RE.finditer(content):
        if not _secret_value_is_real(match.group("value")):
            continue
        inner = match.group("value").strip().rstrip(",;)]}").strip("\"'")
        if (
            match.group("key").upper().endswith("_ENV")
            and inner in _KNOWN_CREDENTIAL_ENV_NAMES
        ):
            continue
        return True
    return False


def _real_secret_grep_lines(grep_stdout: str) -> list[str]:
    """Keep only `git grep -n` lines whose value is a real credential literal.

    `git grep -n` emits `path:lineno:content`; split off the content (which may
    itself contain colons) before classifying.
    """
    real: list[str] = []
    for line in grep_stdout.splitlines():
        parts = line.split(":", 2)
        content = parts[2] if len(parts) == 3 else line
        if _secret_assignment_is_real(content):
            real.append(line)
    return real


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
    return "|".join(_PERSONAL_PATH_PATTERNS)


def _tree_secret_value_grep_pattern() -> str:
    return "|".join(_TREE_SECRET_VALUE_PATTERNS)


def _tracked_secret_file_hits(repo_root: Path) -> tuple[list[str], bool]:
    tracked = _run(
        ["git", "ls-files", "-z", ".env", ".env.*", ".pypirc"],
        cwd=repo_root,
    )
    if tracked.returncode != 0:
        return [], False
    hits = [
        path
        for path in tracked.stdout.split("\0")
        if path and path not in {".env.example"}
    ]
    return hits, True


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
    secret_file_hits, secret_file_check_ok = _tracked_secret_file_hits(repo_root)
    hygiene_grep = _run(
        ["git", "grep", "-nE", pattern, "--", *_git_grep_pathspecs()],
        cwd=repo_root,
    )
    secret_grep = _run(
        [
            "git",
            "grep",
            "-nEi",
            _tree_secret_value_grep_pattern(),
            "--",
            *_git_grep_pathspecs(),
        ],
        cwd=repo_root,
    )
    # `git grep` exits 0 on hit and 1 on no-hit. Anything else is an
    # invocation error.
    matched = False
    if secret_file_hits:
        matched = True
        for path in secret_file_hits:
            print(f"{path}: tracked local-secret file")
    elif not secret_file_check_ok:
        blockers.append("git ls-files failed")
    # Personal-path hits are unambiguous and block as-is. Secret-value hits are
    # only candidates: post-filter them through the credential classifier so the
    # gate fails on real literal secrets but not on code that merely names a
    # credential identifier (env reads, type annotations, references, sentinels).
    if hygiene_grep.returncode == 0 and hygiene_grep.stdout:
        matched = True
        print(
            hygiene_grep.stdout,
            end="" if hygiene_grep.stdout.endswith("\n") else "\n",
        )
    elif hygiene_grep.returncode not in (0, 1):
        if hygiene_grep.stderr:
            print(
                hygiene_grep.stderr,
                end="" if hygiene_grep.stderr.endswith("\n") else "\n",
            )
        blockers.append("git grep failed")
    if secret_grep.returncode in (0, 1):
        real_secret_lines = _real_secret_grep_lines(secret_grep.stdout)
        if real_secret_lines:
            matched = True
            for line in real_secret_lines:
                print(line)
    else:
        if secret_grep.stderr:
            print(
                secret_grep.stderr,
                end="" if secret_grep.stderr.endswith("\n") else "\n",
            )
        blockers.append("git grep failed")
    if matched:
        blockers.append("git grep hit personal path or secret-shape token")
    if not matched and not blockers:
        print("(no matches)")

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
            _print_section(f"scaffold apply {preset}")
            project = tmp / f"project-{preset}"
            project.mkdir(parents=True, exist_ok=True)
            try:
                written = write_scaffold(preset, project, home=home)
            except SystemExit as exc:
                print(f"scaffold apply {preset} failed: {exc}")
                blockers.append(f"scaffold apply {preset} failed")
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
# release check drift
# ---------------------------------------------------------------------------


_PLUGIN_SKILL_SYNC_RELATIVE = Path("scripts/sync_plugin_skill.sh")


def cmd_release_check_drift(args: argparse.Namespace) -> int:
    """Run canonical-renderer and plugin-mirror drift gates as one release check.

    Bundles two pre-existing drift gates so a release operator (and CI)
    can fail fast on either canonical-rendered guardrail output drift or
    plugin-mirror drift without invoking the unit-test suite. Reproduces:

    - ``mozyo-bridge scaffold canonical --check --repo <root>`` (Redmine
      #10345 / #10426): router pair + governed preset workflow pair.
    - ``scripts/sync_plugin_skill.sh --check`` (Redmine #10663): plugin
      skill mirror (`plugins/mozyo-bridge-agent/skills/...`).

    Honors the ``release check`` family invariants: read-only, idempotent,
    strict-fail (exit 1) on any drift, no implicit mutation. Each sub-check
    runs independently so a clean tree on one side still fails the
    overall command if the other side drifted.
    """
    repo_root = resolve_repo_root(getattr(args, "repo", None))
    blockers: list[str] = []

    _print_section("scaffold canonical --check")
    # The canonical check must run the *target tree's* package: staged
    # release copies (and dev checkouts under an interpreter without the
    # package installed) are not importable as `mozyo_bridge` from the
    # subprocess's default sys.path, so prepend the target `src` layout.
    canonical_env = os.environ.copy()
    target_src = repo_root / "src"
    if (target_src / "mozyo_bridge" / "__init__.py").is_file():
        existing_pythonpath = canonical_env.get("PYTHONPATH")
        canonical_env["PYTHONPATH"] = (
            str(target_src)
            if not existing_pythonpath
            else f"{target_src}{os.pathsep}{existing_pythonpath}"
        )
    canonical = _run(
        [sys.executable, "-m", "mozyo_bridge", "scaffold", "canonical", "--check", "--repo", str(repo_root)],
        cwd=repo_root,
        env=canonical_env,
    )
    if canonical.stdout:
        print(canonical.stdout, end="" if canonical.stdout.endswith("\n") else "\n")
    if canonical.stderr:
        print(canonical.stderr, end="" if canonical.stderr.endswith("\n") else "\n")
    if canonical.returncode != 0:
        blockers.append(
            "scaffold canonical drift detected; rerun "
            "`mozyo-bridge scaffold canonical` (no --check) and recommit."
        )

    _print_section("sync_plugin_skill.sh --check")
    sync_script = repo_root / _PLUGIN_SKILL_SYNC_RELATIVE
    if not sync_script.is_file():
        # Missing script is itself a release blocker — the gate would
        # otherwise pass silently because nothing ran.
        print(f"missing sync script: {sync_script}")
        blockers.append(
            f"plugin skill sync script missing at {sync_script}; "
            "restore from the repo or branch source."
        )
    else:
        _require_command("sh")
        mirror = _run(
            ["sh", str(sync_script), "--check"],
            cwd=repo_root,
        )
        if mirror.stdout:
            print(mirror.stdout, end="" if mirror.stdout.endswith("\n") else "\n")
        if mirror.stderr:
            print(mirror.stderr, end="" if mirror.stderr.endswith("\n") else "\n")
        if mirror.returncode != 0:
            blockers.append(
                "plugin skill mirror drift detected; rerun "
                "`scripts/sync_plugin_skill.sh` (no --check, from the repo root) and recommit."
            )

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
    return "|".join(list(_PERSONAL_PATH_PATTERNS) + list(_SECRET_VALUE_PATTERNS))


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


def _grep_artifact_tree(
    root: Path, personal_pattern: re.Pattern[str]
) -> list[tuple[Path, int, str]]:
    # Personal-path matches block as-is; secret-value matches are post-filtered
    # through the same credential classifier as `release check tree` so the
    # artifact scan does not block on code that merely names a credential.
    hits: list[tuple[Path, int, str]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = Path(dirpath) / filename
            if filename == ".pypirc" or filename == ".env" or (
                filename.startswith(".env.") and filename != ".env.example"
            ):
                hits.append((path, 0, "artifact contains local-secret file"))
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if personal_pattern.search(line) or _secret_assignment_is_real(line):
                    hits.append((path, lineno, line))
    return hits


def cmd_release_check_artifact(args: argparse.Namespace) -> int:
    """Reproduce Build Artifact Inspection from `release-flow.md`.

    Honors the `release check` family's read-only / no-mutation invariant:
    the helper never touches the repo's ``dist/`` directory. Instead it
    asks the current Python interpreter to run ``-m build`` into an
    isolated tmp outdir, then
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
            [sys.executable, "-m", "build", "--outdir", str(build_outdir)],
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

        personal_pattern = re.compile("|".join(_PERSONAL_PATH_PATTERNS))
        for artifact in artifacts:
            extracted = _extract_artifact(artifact, extract_root)
            _print_section(f"scan {artifact.name}")
            hits = _grep_artifact_tree(extracted, personal_pattern)
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
            "status,conclusion,databaseId,headSha,workflowName,url,createdAt,updatedAt",
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
    url = payload.get("url")
    print(f"run_id: {args.run_id}")
    print(f"workflow: {workflow_name}")
    print(f"status: {status}")
    print(f"conclusion: {conclusion}")
    print(f"head_sha: {head_sha}")
    print(f"url: {url}")
    return _workflow_exit_code(
        status if isinstance(status, str) else None,
        conclusion if isinstance(conclusion, str) else None,
    )


def cmd_release_workflow_runs(args: argparse.Namespace) -> int:
    """List recent runs of a workflow with the columns the contract names."""
    _require_command("gh", hint=_GH_HINT)
    # `name` carries the run-name, which the TestPyPI exact-candidate dispatch
    # stamps with `dispatch_nonce`; surfacing it lets operators correlate a
    # dispatch to its run deterministically (Redmine #13601).
    fields = "databaseId,name,createdAt,status,conclusion,headSha,url,workflowName"
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
    print("RUN_ID\tCREATED_AT\tSTATUS\tCONCLUSION\tHEAD_SHA\tHTML_URL\tRUN_NAME")
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
            str(entry.get("name") or ""),
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


# The mirror-set constants (contract path, anchor, per-extension version-field
# handlers) and the PEP 440 version recognizer live in the stdlib-only
# ``version_mirror`` module (this Feature package) so this installed helper and
# the dependency-free TestPyPI dev-version script (``scripts/
# compute_testpypi_dev_version.py``) build on one source of truth. The mirror
# resolution / extract / rewrite functions below are thin wrappers that
# translate ``version_mirror.MirrorError`` into ``die`` for the helper's
# operator-facing exit contract.

_TAG_RE = re.compile(
    r"^v[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?$"
)

# Exact 40-hex lowercase commit SHA. The TestPyPI exact-candidate dispatch
# (Redmine #13601) treats the SHA as the artifact authority, so it must be a
# full immutable SHA, never an abbreviation or a ref name.
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _validate_version(value: str) -> None:
    if not version_mirror.is_valid_version(value):
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


def _validate_source_sha(value: str) -> None:
    if not _SOURCE_SHA_RE.match(value):
        die(
            f"source SHA {value!r} must be an exact 40-hex lowercase commit SHA "
            "(the artifact authority for the exact-candidate TestPyPI dispatch); "
            "abbreviations and ref names are refused"
        )


def _load_mirror_set(repo_root: Path) -> list[tuple[Path, dict[str, object]]]:
    """Return the contract-declared mirror set as `(absolute_path, handler)`.

    Thin wrapper over ``version_mirror.load_mirror_set`` that translates a
    ``MirrorError`` (missing contract doc / anchor / mirror file / unhandled
    extension) into the helper's ``die`` exit contract. The mirror set is read
    from ``release-helper-contract.md`` so it stays in lockstep with the
    contract.
    """
    try:
        return version_mirror.load_mirror_set(repo_root)
    except version_mirror.MirrorError as exc:
        die(str(exc))
        raise AssertionError("unreachable")


def _extract_current_version(path: Path, handler: dict[str, object]) -> str:
    text = path.read_text(encoding="utf-8")
    try:
        return version_mirror.extract_version(text, handler)
    except version_mirror.MirrorError as exc:
        die(f"{exc} (file: {path})")
        raise AssertionError("unreachable")


def _replace_version(path: Path, handler: dict[str, object], new_version: str) -> bool:
    """Replace the version literal in `path`. Returns True if file changed."""
    text = path.read_text(encoding="utf-8")
    try:
        rewritten = version_mirror.replace_version(text, handler, new_version)
    except version_mirror.MirrorError as exc:
        die(f"{exc} (file: {path})")
        raise AssertionError("unreachable")
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


def _new_dispatch_nonce() -> str:
    """Return a unique token embedded in the workflow run-name.

    The TestPyPI workflow (Redmine #13601) echoes ``dispatch_nonce`` into its
    ``run-name`` so the just-dispatched run can be correlated by exact nonce
    match rather than a latest-one guess. Split out so tests can pin a fixed
    nonce.
    """
    return uuid.uuid4().hex


def _correlate_dispatch_run(
    nonce: str, *, attempts: int = 6, delay: float = 2.0
) -> dict[str, str]:
    """Deterministically correlate the dispatched run by its run-name nonce.

    The workflow embeds ``dispatch_nonce`` in its run-name, so the just-
    dispatched run is identified by exact nonce match — never by picking the
    most recent run (which could be a concurrent dev-path publish or another
    operator's dispatch). Returns a dict whose ``match`` is one of
    ``"one"`` / ``"none"`` / ``"many"``; only an exact single match is a green
    path, everything else is surfaced fail-closed to the caller.
    """
    matches: list[dict[str, object]] = []
    for attempt in range(max(1, attempts)):
        result = _run(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "testpypi.yml",
                "--limit",
                "40",
                "--json",
                "databaseId,name,url,createdAt,headSha,status",
            ]
        )
        runs: list[dict[str, object]] = []
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                if isinstance(payload, list):
                    runs = [entry for entry in payload if isinstance(entry, dict)]
            except json.JSONDecodeError:
                runs = []
        matches = [entry for entry in runs if nonce in str(entry.get("name") or "")]
        if matches:
            break
        if attempt + 1 < max(1, attempts):
            time.sleep(delay)
    if len(matches) == 1:
        entry = matches[0]
        return {
            "match": "one",
            "run_id": str(entry.get("databaseId") or ""),
            "name": str(entry.get("name") or ""),
            "url": str(entry.get("url") or ""),
            "created_at": str(entry.get("createdAt") or ""),
            "head_sha": str(entry.get("headSha") or ""),
            "status": str(entry.get("status") or ""),
        }
    if len(matches) > 1:
        return {"match": "many"}
    return {"match": "none"}


def _gh_dispatch_testpypi(
    source_sha: str, expected_version: str, source_ref: str, nonce: str
) -> dict[str, str]:
    """Dispatch the main-fixed exact-candidate TestPyPI workflow.

    The workflow definition / event ref stays ``main``; the exact reviewed
    candidate is passed as inputs. The workflow checks out ``source_sha`` and
    fail-closed verifies SHA / version mirror / Test CI / uniqueness / ref
    lineage before build+publish. Run correlation is by ``dispatch_nonce``, not
    latest-one guessing.
    """
    _require_command("gh", hint=_GH_HINT)
    dispatch = _run(
        [
            "gh",
            "workflow",
            "run",
            "testpypi.yml",
            "--ref",
            "main",
            "-f",
            f"source_sha={source_sha}",
            "-f",
            f"expected_version={expected_version}",
            "-f",
            f"source_ref={source_ref}",
            "-f",
            f"dispatch_nonce={nonce}",
        ]
    )
    if dispatch.returncode != 0:
        die(
            "gh workflow run testpypi.yml failed: "
            f"{dispatch.stderr.strip() or dispatch.stdout.strip()}"
        )
    return _correlate_dispatch_run(nonce)


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
    """Dispatch the exact-candidate TestPyPI workflow (Redmine #13601).

    Requires the exact ``--source-sha`` (artifact authority), an
    ``--expected-version`` the SHA must carry, and a ``--source-ref`` (approved
    origin lineage the SHA must currently resolve from). The legacy
    ``--version`` flag is accepted as an alias for ``--expected-version``. The
    dispatch is correlated to its run deterministically by nonce; ambiguous or
    not-yet-registered correlation is surfaced fail-closed rather than guessing
    the most recent run.
    """
    source_sha = getattr(args, "source_sha", None)
    expected_version = getattr(args, "expected_version", None) or getattr(
        args, "version", None
    )
    source_ref = getattr(args, "source_ref", None)
    if not source_sha:
        die("release publish --testpypi requires --source-sha <40-hex commit SHA>")
    if not expected_version:
        die(
            "release publish --testpypi requires --expected-version <X.Y.Z> "
            "(or the --version alias)"
        )
    if not source_ref:
        die(
            "release publish --testpypi requires --source-ref <approved origin "
            "ref that resolves to source_sha>"
        )
    _validate_source_sha(source_sha)
    _validate_version(expected_version)
    nonce = _new_dispatch_nonce()
    info = _gh_dispatch_testpypi(source_sha, expected_version, source_ref, nonce)

    _print_section("dispatched TestPyPI workflow (exact candidate)")
    print("workflow: testpypi.yml")
    print("ref: main")
    print(f"source_sha: {source_sha}")
    print(f"source_ref: {source_ref}")
    print(f"expected_version: {expected_version}")
    print(f"dispatch_nonce: {nonce}")

    if info.get("match") == "one":
        run_id = info.get("run_id", "")
        print(f"run_id: {run_id}")
        print(f"run_name: {info.get('name', '')}")
        print(f"url: {info.get('url', '')}")
        print(f"run_head_sha: {info.get('head_sha', '')}")
        print(f"status: {info.get('status', '')}")
        print("")
        print(
            "Next: `mozyo-bridge release workflow wait --run-id "
            f"{run_id} --timeout <seconds>`"
        )
        return EXIT_CLEAN

    # Fail-closed: never fall back to a latest-one guess. The nonce lives in the
    # run-name, so the operator can always correlate deterministically later.
    print("run_id: (not deterministically correlated)")
    print("")
    if info.get("match") == "many":
        print(
            "result: blocker (multiple runs matched the dispatch nonce; "
            "do NOT assume the latest)"
        )
    else:
        print(
            "result: blocker (dispatched run not yet correlated by nonce; "
            "do NOT assume the latest)"
        )
    print(
        "Correlate deterministically by nonce: `mozyo-bridge release workflow "
        f"runs --workflow testpypi.yml` and pick the run whose name contains "
        f"{nonce!r}."
    )
    return EXIT_BLOCKER


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
