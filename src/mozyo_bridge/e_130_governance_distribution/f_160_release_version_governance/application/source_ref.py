"""`source_ref` spelling policy + action-time origin preflight (Redmine #13883).

The exact-candidate TestPyPI dispatch passes ``source_ref`` to the trusted
workflow, which resolves it with ``git ls-remote origin <source_ref>`` as
lineage evidence for ``source_sha``. That makes ``source_ref`` a **ref literal
as ORIGIN spells it**, which is not the same string git shows locally: a clone
displays ``origin/<branch>`` for what origin itself calls
``refs/heads/<branch>``.

Run ``29481593519`` passed the local spelling straight through. The helper
dispatched, and the workflow's ref gate then resolved zero refs and failed the
run before build. This module moves that proof to action time on the client so
an unresolvable ref costs zero dispatches.

Policy is **reject with an exact correction**, never silent normalization; the
decision and its rejected alternative live in
``vibes/docs/logics/release-helper-contract.md`` -> ``source_ref Spelling
Policy``. The short version: ``origin/<branch>`` is genuinely ambiguous, because
a remote may carry a branch literally named ``origin/<branch>``
(``refs/heads/origin/<branch>``), so rewriting it to ``<branch>`` would silently
retarget the artifact authority.

Split out of ``release.py`` rather than grown inside it: that module is already
over the module-health threshold, and ref-spelling policy is a self-contained,
separately testable concern (same posture as ``version_mirror``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from mozyo_bridge.shared.errors import die

# `source_ref` charset, mirrored from the trusted workflow gate: no globs,
# refspec metacharacters, or whitespace. It doubles as the shell-safety guard —
# a value that clears it carries no quoting or substitution payload. Keep this
# in lockstep with the `case "$SOURCE_REF" in *[!A-Za-z0-9._/-]* | "")` gate in
# .github/workflows/testpypi.yml; the client must never be looser than the
# server it mirrors.
CHARSET_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# Git's LOCAL remote-tracking namespace. A ref spelled this way never exists
# under that name on the remote itself.
LOCAL_TRACKING_PREFIX = "refs/remotes/"

_GIT_HINT = "install git: https://git-scm.com/"


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _require_git() -> None:
    if shutil.which("git") is None:
        die(f"required executable not found in PATH: git ({_GIT_HINT})")


def configured_remotes(repo_root: Path) -> set[str]:
    """Return the repo's configured git remote names.

    Lets a LOCAL remote-tracking spelling (``origin/main``) be told apart from a
    legitimate slash-bearing branch name (``feature/release``): only a first
    segment that is an actually-configured remote makes the value a tracking
    name. Nothing is inferred from the shape alone.
    """
    _require_git()
    result = _run(["git", "remote"], cwd=repo_root)
    if result.returncode != 0:
        die(
            "cannot list git remotes to validate --source-ref "
            f"({result.stderr.strip() or 'git remote failed'}); run the helper "
            "inside the release repo (or pass --repo <path>)"
        )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def validate(value: str, *, repo_root: Path) -> None:
    """Refuse `source_ref` spellings that cannot exist on origin.

    Rejections carry the exact, pasteable correction to use. Nothing is
    rewritten: see the module docstring for why normalization is unsafe here.
    """
    if not CHARSET_RE.match(value):
        die(
            f"source_ref {value!r} must be a single plain ref name matching "
            "[A-Za-z0-9._/-]+ (no globs, refspec metacharacters, or "
            "whitespace); this mirrors the trusted workflow gate and keeps the "
            "value shell-safe. Nothing was dispatched"
        )
    if value.startswith(LOCAL_TRACKING_PREFIX):
        tracked = value[len(LOCAL_TRACKING_PREFIX) :]
        _, _, branch = tracked.partition("/")
        die(
            f"source_ref {value!r} names git's LOCAL remote-tracking namespace "
            f"({LOCAL_TRACKING_PREFIX}*), which never exists under that name on "
            "the remote, so `git ls-remote origin <source_ref>` resolves zero "
            "refs. Nothing was dispatched. Pass the ref as origin spells it: "
            f"--source-ref refs/heads/{branch or '<branch>'} (canonical) or "
            f"--source-ref {branch or '<branch>'}"
        )
    remote, sep, branch = value.partition("/")
    if sep and remote in configured_remotes(repo_root):
        die(
            f"source_ref {value!r} is a LOCAL remote-tracking name ({remote!r} "
            "is a configured remote, so this is git's local spelling of "
            f"{LOCAL_TRACKING_PREFIX}{value}). It is not how the ref is spelled "
            f"ON {remote}, so `git ls-remote origin <source_ref>` resolves zero "
            "refs and the workflow fails only AFTER the run has started "
            "(Redmine #13883). Nothing was dispatched. Pass the ref as origin "
            f"spells it: --source-ref refs/heads/{branch} (canonical, always "
            f"exactly one) or --source-ref {branch}. If you literally mean a "
            f"branch NAMED {value!r} on {remote}, pass --source-ref "
            f"refs/heads/{value}"
        )


def preflight(source_ref: str, source_sha: str, *, repo_root: Path) -> str:
    """Resolve `source_ref` on origin BEFORE dispatch; return the resolved ref name.

    Client mirror of the trusted workflow gate ``Verify source_ref resolves to
    source SHA on origin``: require EXACTLY ONE non-peel origin ref whose current
    tip is ``source_sha``. Zero / multi / mismatch all die here, so the caller's
    dispatch is unreachable and the dispatch count stays 0.

    Two git semantics this depends on, both mirrored from the server gate:

      * ``git ls-remote <pattern>`` matches a ref-name TAIL, it is not an exact
        lookup — a plain ``main`` also matches ``refs/heads/origin/main`` and
        ``refs/tags/main``. Multi matches are refused, never ranked.
      * Peel lines (``^{}``) are dropped so an annotated tag counts once, which
        means a tag's non-peel tip is the TAG OBJECT rather than the commit; a
        tag ``source_ref`` therefore surfaces here as a mismatch, exactly as it
        would server-side.

    This mirrors the trusted gate, it does not replace it: an untrusted client's
    result is never a substitute for the workflow's own inline verification.
    """
    _require_git()
    result = _run(["git", "ls-remote", "origin", source_ref], cwd=repo_root)
    if result.returncode != 0:
        die(
            f"git ls-remote origin {source_ref!r} failed "
            f"({result.stderr.strip() or 'no stderr'}); cannot prove the ref "
            "resolves on origin. Nothing was dispatched (fail-closed)"
        )
    matches: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, name = parts
        if name.endswith("^{}"):
            continue
        matches.append((sha, name))
    if not matches:
        die(
            f"source_ref {source_ref!r} resolved to 0 refs on origin; require "
            "exactly one. Nothing was dispatched. Confirm the ref is pushed "
            f"(`git ls-remote origin {source_ref}`) and spelled as origin "
            "spells it (canonical: refs/heads/<branch>)"
        )
    if len(matches) > 1:
        listed = ", ".join(f"{name} -> {sha}" for sha, name in matches)
        die(
            f"source_ref {source_ref!r} resolved to {len(matches)} origin refs "
            f"({listed}); require exactly one. Nothing was dispatched. "
            "`git ls-remote` matches a ref-name TAIL, so a plain name can also "
            "match refs/tags/<name> or refs/heads/<prefix>/<name>; "
            "disambiguate with the full path, e.g. --source-ref "
            "refs/heads/<branch>"
        )
    resolved_sha, resolved_name = matches[0]
    if resolved_sha != source_sha:
        die(
            f"source_ref {source_ref!r} resolves on origin to {resolved_name} "
            f"whose tip is {resolved_sha}, not source_sha {source_sha}. "
            "Nothing was dispatched. Either the candidate is not that ref's tip "
            "(push it, or name the ref that carries the SHA), or the ref is an "
            "annotated tag whose non-peel tip is the tag object, not the commit"
        )
    return resolved_name
