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
Policy``. The reason a local remote-tracking spelling is refused is
**ambiguity, not non-existence**: a remote may carry a branch literally named
``origin/<branch>`` (``refs/heads/origin/<branch>``), so `origin/main` can name
either "the branch `main`, spelled the way my clone shows it" or "the branch
actually called `origin/main` on the remote". Rewriting it to ``<branch>`` would
silently pick one reading and could retarget the artifact authority. Resolving
to zero refs is the *common* outcome, not the justification (Redmine #13883
j#79995 F3).

``git ls-remote`` matches a ref-name TAIL at ``/`` boundaries and this applies to
full paths too: with a branch ``foo/refs/heads/main`` on the remote,
``git ls-remote origin refs/heads/main`` returns BOTH it and ``refs/heads/main``.
So no spelling is exactly-one by construction — ``refs/heads/<branch>`` is the
canonical, least-ambiguous form, not a guaranteed-unique one. The exactly-one
requirement is enforced dynamically here and identically server-side, so such a
collision fails closed on both sides (j#79995 F1).

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

# Git's LOCAL remote-tracking namespace. Remotes publish branches under
# `refs/heads/`, so this spelling names the local mirror of a ref rather than
# the ref as the remote publishes it.
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
            f"({LOCAL_TRACKING_PREFIX}*), which mirrors a remote ref rather than "
            "naming it the way the remote publishes it (remotes publish branches "
            "under refs/heads/). Nothing was dispatched. Pass the ref as origin "
            f"spells it: --source-ref refs/heads/{branch or '<branch>'} "
            f"(canonical) or --source-ref {branch or '<branch>'}"
        )
    remote, sep, branch = value.partition("/")
    if sep and remote in configured_remotes(repo_root):
        die(
            f"source_ref {value!r} is ambiguous: {remote!r} is a configured "
            f"remote, so this is git's local spelling of "
            f"{LOCAL_TRACKING_PREFIX}{value} (the branch {branch!r} on "
            f"{remote}) — but {remote} could also carry a branch literally NAMED "
            f"{value!r}. Refusing rather than guessing, because guessing wrong "
            "would silently build a different commit (Redmine #13883). Nothing "
            "was dispatched. Say which you mean, as origin spells it:\n"
            f"  the branch {branch!r} on {remote}:   --source-ref "
            f"refs/heads/{branch}  (canonical) or --source-ref {branch}\n"
            f"  a branch literally named {value!r}: --source-ref "
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
        ``refs/tags/main``. This applies to full paths too: a branch
        ``foo/refs/heads/main`` collides with ``refs/heads/main`` itself, so no
        spelling is exactly-one by construction. Multi matches are refused,
        never ranked, and the refusal names the colliding refs.
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
        listed = "\n".join(f"  {name} -> {sha}" for sha, name in matches)
        # `git ls-remote` matches a ref-name TAIL at `/` boundaries, so the
        # recovery depends on whether a MORE SPECIFIC spelling still exists.
        # Decide that from the match facts, not from the shape of the input: a
        # `refs/` prefix does not mean "full ref path", because a branch may
        # legally be named `refs/foo` (which origin publishes as
        # `refs/heads/refs/foo`), and telling its owner to delete refs would
        # hide the full-path correction that actually works (j#80048 R2-F1).
        # If the input already equals one of the matched ref names, it IS the
        # full name and no re-spelling can narrow it further (j#79995 F1).
        names_the_ref_exactly = any(name == source_ref for _, name in matches)
        if names_the_ref_exactly:
            recovery = (
                f"{source_ref!r} is itself one of the refs above, so it is "
                "already the ref's full name and re-spelling cannot narrow it: "
                "`git ls-remote` matches a ref-name TAIL, and the refs above "
                "share this one. The exactly-one requirement is also enforced "
                "server-side, so this ref cannot be dispatched while the "
                "collision exists. Either rename/delete the colliding ref on "
                "origin, or pass a different ref that resolves to exactly one."
            )
        else:
            recovery = (
                f"{source_ref!r} is a tail pattern here — it names none of the "
                "refs above exactly, so a more specific spelling exists. Pass "
                "the full path of the one you mean, verbatim, e.g. --source-ref "
                f"{matches[0][1]}"
            )
        die(
            f"source_ref {source_ref!r} resolved to {len(matches)} origin refs; "
            f"require exactly one. Nothing was dispatched.\n{listed}\n{recovery}"
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
