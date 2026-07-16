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


def spelling_refusal(value: str, *, remotes: set[str]) -> str | None:
    """Return why `value` is not an accepted `source_ref` spelling, else None.

    Split out of ``validate`` so the same policy can be applied without dying —
    ``preflight`` must know whether a ref it is about to CITE would itself be
    refused, rather than handing the operator an un-pasteable correction
    (Redmine #13883 j#80124 R4-F1).
    """
    if not CHARSET_RE.match(value):
        return (
            f"source_ref {value!r} must be a single plain ref name matching "
            "[A-Za-z0-9._/-]+ (no globs, refspec metacharacters, or "
            "whitespace); this mirrors the trusted workflow gate and keeps the "
            "value shell-safe. Nothing was dispatched"
        )
    if value.startswith(LOCAL_TRACKING_PREFIX):
        tracked = value[len(LOCAL_TRACKING_PREFIX) :]
        _, _, branch = tracked.partition("/")
        return (
            f"source_ref {value!r} names git's LOCAL remote-tracking namespace "
            f"({LOCAL_TRACKING_PREFIX}*), which mirrors a remote ref rather than "
            "naming it the way the remote publishes it (remotes publish branches "
            "under refs/heads/). Nothing was dispatched. Pass the ref as origin "
            f"spells it: --source-ref refs/heads/{branch or '<branch>'} "
            f"(canonical) or --source-ref {branch or '<branch>'}"
        )
    remote, sep, branch = value.partition("/")
    if sep and remote in remotes:
        return (
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
    return None


def validate(value: str, *, repo_root: Path) -> None:
    """Refuse `source_ref` spellings that cannot be accepted as-is.

    Rejections carry the exact, pasteable correction to use. Nothing is
    rewritten: see the module docstring for why normalization is unsafe here.
    """
    refusal = spelling_refusal(value, remotes=configured_remotes(repo_root))
    if refusal is not None:
        die(refusal)


def _resolves_uniquely(name: str, matches: list[tuple[str, str]]) -> bool:
    """True if re-passing `name` would match only itself.

    Decidable from `matches` alone, with no extra query: any ref S that matches
    the pattern `name` ends with `/` + name, and `name` in turn ends with the
    original pattern — so S ends with the original pattern too and is therefore
    already in `matches`. Checking `matches` is thus complete.
    """
    return not any(
        other != name and other.endswith("/" + name) for _, other in matches
    )


def _actionable_corrections(
    matches: list[tuple[str, str]], source_sha: str, *, remotes: set[str]
) -> list[str]:
    """Return matched refs that could actually be passed as `source_ref`.

    A citation is only useful if re-passing it reaches dispatch, which needs ALL
    of: the tip is `source_sha` (it is the approved candidate's lineage, not
    merely some ref), the name is an accepted spelling (the helper would not
    refuse it), and it resolves uniquely. Selecting on any one of these alone —
    "it's the longest", so it resolves uniquely — cites refs that carry a
    different commit or that the helper itself rejects (Redmine #13883 j#80124
    R4-F1). Nothing here guesses which ref the operator meant; it only removes
    the ones that provably cannot work.
    """
    return [
        name
        for sha, name in matches
        if sha == source_sha
        and spelling_refusal(name, remotes=remotes) is None
        and _resolves_uniquely(name, matches)
    ]


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
        # `git ls-remote` matches a ref-name TAIL at `/` boundaries. Why the
        # input matched several refs is context; what the operator can DO about
        # it comes from the match facts, never from the shape of the input (a
        # `refs/` prefix does not mean "full ref path": a branch may legally be
        # named `refs/foo`, j#80048 R2-F1).
        if any(name == source_ref for _, name in matches):
            context = (
                f"{source_ref!r} is itself one of the refs above, so it is "
                "already that ref's full name and re-spelling cannot narrow it."
            )
        else:
            context = (
                f"{source_ref!r} is a tail pattern here — it names none of the "
                "refs above exactly."
            )
        # Only cite refs that would actually reach dispatch if pasted back.
        # "Resolves uniquely" alone is not enough: such a ref may carry a
        # different commit than the approved candidate, or be a name this helper
        # refuses outright (j#80124 R4-F1). Where nothing qualifies, say so
        # rather than inventing a correction.
        actionable = _actionable_corrections(
            matches, source_sha, remotes=configured_remotes(repo_root)
        )
        if len(actionable) == 1:
            recovery = (
                f"--source-ref {actionable[0]} carries source_sha and resolves "
                "uniquely — pass it verbatim."
            )
        elif actionable:
            options = "\n".join(f"  --source-ref {name}" for name in actionable)
            recovery = (
                "These carry source_sha and resolve uniquely. Pass the approved "
                f"one verbatim (they are not interchangeable):\n{options}"
            )
        elif any(sha == source_sha for sha, _ in matches):
            recovery = (
                f"The ref(s) above carrying source_sha {source_sha} cannot be "
                "named on their own here — each is a tail of another listed ref, "
                "or is not an accepted source_ref spelling — so no re-spelling "
                "reaches this candidate. Resolve the collision on origin, or "
                "push/name a ref that carries source_sha and can be named alone."
            )
        else:
            recovery = (
                f"None of the refs above has source_sha {source_sha} as its tip, "
                "so none of them is this candidate's lineage. Push or name a ref "
                "whose tip is that SHA."
            )
        die(
            f"source_ref {source_ref!r} resolved to {len(matches)} origin refs; "
            f"require exactly one. Nothing was dispatched.\n{listed}\n{context}\n"
            f"{recovery}"
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
