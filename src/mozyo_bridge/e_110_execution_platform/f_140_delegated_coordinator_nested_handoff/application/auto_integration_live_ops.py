"""Live subprocess adapter for the #13686 auto-integration actuator (Redmine #13686).

The concrete :class:`~...application.auto_integration_actuator.AutoIntegrationGitOperations`
implementation: real ``git`` invocations for the read probes the action-time preflight needs
and for the two weak mutations the port defines. It replaces the deliberately gated
``LiveSublaneGitOperations.merge_to_integration_branch`` ``NotImplementedError``, which was
held closed until this issue's design consultation (j#77124) and owner decision (j#96335)
settled what an auto-integration is allowed to be.

What the adapter can and cannot express is the safety story, so it is enumerated here:

- **Push is always non-force.** ``git push <remote> <sha>:refs/heads/<branch>``. There is no
  ``--force``, no ``--force-with-lease`` (still a force), and no ``+`` refspec — the last is
  additionally refused by construction, because ``+`` is how a force is spelled *inside* a
  refspec and a branch name is not trusted to be free of it.
- **Merge is built from objects**, never inside a checkout: ``merge-tree --write-tree`` writes
  the merged tree and ``commit-tree`` wraps it with the measured target as first parent. No
  worktree is switched, no index is written, no ref moves, and a conflict is reported rather
  than resolved. Review j#96406 finding 1 is why: the previous form merged inside a *path*
  whose identity an earlier probe had established, and a foreign lane's checkout swapped onto
  that path was switched off its own branch and had the merge built on it.
- **Nothing here is destructive.** A ref delete, and now a worktree removal, were shipped and
  removed, because none of them could enforce its own condition in one invocation:

  - the remote delete (R1, retired by review j#96344 finding 1) had no compare-and-swap
    against the remote tip and ran even when every local condition failed; a real CAS on a
    remote ref needs ``--force-with-lease``, a force, prohibited by j#96335;
  - the local delete (retired by review j#96396 finding 1) needed the tip to still be the
    recorded one *and* no worktree to hold the branch. Measured on git 2.50.1:
    ``update-ref -d <ref> <tip>`` compare-and-swaps the tip but deletes a branch a linked
    worktree is standing on and leaves that worktree's ``HEAD`` unresolvable; ``branch -D``
    refuses the held branch atomically but takes no tip constraint (a second argument is
    read as another *branch name*); and ``update-ref --stdin`` refuses ``verify`` +
    ``delete`` on one ref (``multiple updates for ref ... not allowed``). R7's two-invocation
    form was reproduced destroying a commit that landed in the window between them;
  - the worktree removal (retired by review j#96401 finding 1) named its target by *path*.
    ``git worktree remove`` checks that the path is registered and clean — both inside the
    invocation, both worth having — but nothing about *whose* checkout it is; that was a
    separate ``worktree list`` probe. Reproduced: replacing our lane's checkout with a
    foreign lane's clean one at the same path between the probe and the removal removed the
    foreign checkout, and the step recorded ``done``. Measured alternatives, all on git
    2.50.1: ``worktree remove`` has no expected-identity argument; the admin entry name under
    ``$GIT_DIR/worktrees`` is reused after such a swap, so it is not instance identity; and
    while ``worktree lock`` genuinely pins the path→entry binding (a competitor's ``remove``
    is refused, ``prune`` skips it, and even after an ``rm -rf`` a re-``add`` fails with *"is
    a missing but locked worktree"*), **no mutation runs while the lock is held** —
    ``remove`` and ``move`` both demand ``-f -f`` — so the unlock reopens the window, and the
    lock is not even attributable, since anyone may ``worktree unlock`` without the reason.

  Removing a lane's worktree and branch is an operator step in the ``preflight_sublane_retire``
  runbook, where a human decides. Nothing here removes a checkout or touches any ref.

Every probe fails closed: a ``git`` that could not run has proven nothing, so a failed
invocation reads as the unsafe answer (not a workspace, not an ancestor, not reachable,
dirty) rather than as a permissive one. The spawn-failure mapping mirrors
:meth:`...application.sublane_integration.LiveSublaneGitOperations._run` — a missing ``cwd``
after a host reboot (#14499) raises ``FileNotFoundError`` rather than exiting non-zero, and
letting that escape a read-only preflight is how six production runs ended in tracebacks.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    MERGE_COMMIT_ERROR,
    MERGE_CONTENT_CONFLICT,
    MERGE_ERROR,
    MERGE_INVALID_INPUT,
    MERGE_MERGED,
    MERGE_PRIMITIVE_UNSUPPORTED,
    MergeResult,
    PushResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    EMPTY_TARGET_HEAD,
    LaneWorktree,
)

#: The default shared remote. Named rather than inlined so the one place a remote is chosen
#: is visible; it is not operator-configurable here because the config schema deliberately
#: has no key that could redirect a push.
DEFAULT_REMOTE = "origin"

#: The identity every actuator-built merge commit carries. A LITERAL, not the host's git
#: configuration: review j#96412 finding 1 requires that the same action produce the same
#: commit id, and an identity that varies by host or by `user.name` cannot do that. It also
#: says plainly in `git log` that no human authored this commit.
ACTUATOR_IDENTITY_NAME = "mozyo-bridge auto-integration"
ACTUATOR_IDENTITY_EMAIL = "auto-integration@mozyo-bridge.invalid"

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_full_sha(value: str) -> bool:
    return len(value) == 40 and all(character in _HEX_DIGITS for character in value)


class UnsafeRefspecError(ValueError):
    """A ref name could not be turned into a provably non-force refspec.

    Raised rather than returned: this is not a gate a caller may observe and proceed past.
    A branch name carrying ``+``, whitespace, or a leading ``-`` would change what the
    constructed ``git push`` argv *means* — ``+`` spells a force inside a refspec, and a
    leading ``-`` turns the value into an option — so the argv is never built at all.
    """


def _checked_branch(ref: str) -> str:
    """Return ``ref`` as a bare branch name, or raise :class:`UnsafeRefspecError`.

    Accepts either ``<branch>`` or ``refs/heads/<branch>`` and normalizes to the bare name,
    so the caller's target ref spelling does not decide whether the refspec is safe.
    """
    candidate = (ref or "").strip()
    if candidate.startswith("refs/heads/"):
        candidate = candidate[len("refs/heads/") :]
    if not candidate:
        raise UnsafeRefspecError("target ref is empty")
    if candidate.startswith("-"):
        raise UnsafeRefspecError(
            f"target ref {ref!r} starts with '-' and would be read as an option"
        )
    forbidden = set("+ \t\n:^~?*[\\")
    if any(character in forbidden for character in candidate):
        raise UnsafeRefspecError(
            f"target ref {ref!r} contains a character that would change the refspec's "
            "meaning ('+' spells a force); refusing to construct the push"
        )
    return candidate


@dataclass(frozen=True)
class LiveAutoIntegrationGitOperations:
    """Subprocess-backed auto-integration Git operations for a concrete repo root.

    ``repo_root`` is the checkout every command runs in. ``remote`` is the shared remote a
    push targets. Both are constructor state rather than per-call arguments so a single
    instance cannot be redirected mid-run.
    """

    repo_root: Path
    remote: str = DEFAULT_REMOTE

    # -- infrastructure ---------------------------------------------------

    def _run(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``git`` in :attr:`repo_root` (or ``cwd``), mapping a spawn failure to a failure.

        ``subprocess.run(cwd=...)`` raises ``FileNotFoundError`` — not a non-zero exit — when
        ``cwd`` does not exist, and ``git`` missing from ``PATH`` raises the same ``OSError``
        family. Both are mapped onto a failed result so a read-only probe fails closed
        instead of raising out of a preflight (#14499).

        ``env`` OVERLAYS the inherited environment rather than replacing it, so ``PATH`` and
        the credential helpers keep working while the caller pins the few variables that would
        otherwise make an operation non-deterministic (:meth:`apply_merge`).
        """
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd or self.repo_root,
                text=True,
                capture_output=True,
                env={**os.environ, **env} if env else None,
            )
        except OSError as exc:
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=127,
                stdout="",
                stderr=f"git could not be run in {cwd or self.repo_root}: {type(exc).__name__}",
            )

    # -- read probes ------------------------------------------------------

    def is_git_workspace(self) -> bool:
        result = self._run("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def resolve_head(self, ref: str) -> str:
        """The full commit SHA ``ref`` resolves to, :data:`EMPTY_TARGET_HEAD` if it does not exist.

        Returning the explicit sentinel rather than ``""`` keeps "the target does not exist
        yet" a stated fact that the action record can carry, instead of an omission that
        would compare equal to an unmeasured field.
        """
        result = self._run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
        if result.returncode != 0:
            return EMPTY_TARGET_HEAD
        candidate = result.stdout.strip()
        return candidate if _is_full_sha(candidate) else EMPTY_TARGET_HEAD

    def is_ancestor(self, *, ancestor: str, descendant: str) -> bool:
        """True iff ``ancestor`` is an ancestor of ``descendant`` (fail-closed).

        This answers both "is the source already integrated" (target contains source) and
        "is a fast-forward possible" (source contains the expected target head), so the two
        questions share one probe and cannot drift apart.
        """
        if ancestor == EMPTY_TARGET_HEAD:
            # An empty target is contained in everything: creating the branch at the source
            # head is a fast-forward from nothing.
            return True
        if not (_is_full_sha(ancestor) and _is_full_sha(descendant)):
            return False
        result = self._run("merge-base", "--is-ancestor", ancestor, descendant)
        return result.returncode == 0

    def worktree_dirty(self, *, worktree_path: str = "") -> bool:
        """True iff the worktree has uncommitted / untracked changes (fail-closed).

        An unreadable status reads dirty: a checkout that cannot be inspected is never
        reported clean, because "clean" is what authorizes removing it.
        """
        cwd = Path(worktree_path) if worktree_path else self.repo_root
        result = self._run("status", "--porcelain", cwd=cwd)
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())

    def remote_branch_tip(self, branch: str) -> str:
        """The shared remote's CURRENT tip for ``branch``, or ``""`` (fresh, read-only).

        Observed with ``git ls-remote``, which asks the remote, rather than from the cached
        ``refs/remotes/*`` tracking refs — a stale tracking ref can assert a tip the remote
        no longer has, and #14066 fixed exactly that class of false reachability proof for
        the sibling terminal-retire path. No fetch, no ref update, no mutation.
        """
        name = _checked_branch(branch)
        result = self._run(
            "ls-remote", "--heads", "--end-of-options", self.remote, f"refs/heads/{name}"
        )
        if result.returncode != 0:
            return ""
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == f"refs/heads/{name}" and _is_full_sha(parts[0]):
                return parts[0]
        return ""

    def commit_on_remote(self, commit: str, *, branch: str) -> bool:
        """True iff ``commit`` is reachable from the remote's CURRENT ``branch`` tip (fail-closed).

        Two steps, and both matter: the tip is read fresh from the remote
        (:meth:`remote_branch_tip`), and the ancestry is then computed locally against that
        exact tip. A tip the local clone does not have as an object is not proof of anything,
        so it fails closed rather than assuming the local view is complete.
        """
        if not _is_full_sha(commit):
            return False
        tip = self.remote_branch_tip(branch)
        if not tip:
            return False
        if self._run("cat-file", "-e", f"{tip}^{{commit}}").returncode != 0:
            return False
        return self.is_ancestor(ancestor=commit, descendant=tip)

    def describe_lane_worktree(self, *, path: str) -> LaneWorktree:
        """Measure the lane's own checkout (read-only), failing closed on every unknown.

        Every failure to measure reads as the UNSAFE answer: an unreadable worktree list
        means "not registered", an unreadable status means "not clean", an unreadable HEAD
        means no branch. Registration is compared on RESOLVED paths rather than on the
        strings, so a symlinked or differently-spelled path is still recognised.

        This is a read probe about the source of an integration. It no longer describes a
        checkout anything is performed in — see :meth:`apply_merge`.
        """
        candidate = Path(path)
        listed = self._run("worktree", "list", "--porcelain")
        registered = False
        if listed.returncode == 0:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            for line in listed.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                entry = Path(line[len("worktree ") :].strip())
                try:
                    entry_resolved = entry.resolve()
                except OSError:
                    entry_resolved = entry
                if entry_resolved == resolved:
                    registered = True
                    break
        branch = self._run("rev-parse", "--abbrev-ref", "HEAD", cwd=candidate)
        return LaneWorktree(
            path=str(path),
            registered=registered,
            clean=not self.worktree_dirty(worktree_path=str(path)),
            checked_out_branch=branch.stdout.strip() if branch.returncode == 0 else "",
        )

    # -- mutations --------------------------------------------------------

    def _merge_tree_supported(self) -> bool:
        """Is ``merge-tree --write-tree`` available? Probed by version, never inferred.

        Review j#96412 finding 2: an unrecognized exit code is not evidence that the
        primitive is missing — ``merge-tree`` exits 1 for a missing object exactly as it does
        for a content conflict (measured). So the capability question is asked directly, and
        an unreadable or unparseable version reads as "not supported" rather than as "assume
        it works".
        """
        result = self._run("--version")
        if result.returncode != 0:
            return False
        for token in result.stdout.split():
            parts = token.split(".")
            if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            return (int(parts[0]), int(parts[1])) >= (2, 38)
        return False

    def _commit_date(self, commit: str) -> str:
        """The committer date of ``commit``, in a form ``commit-tree`` accepts verbatim.

        This is where the merge commit's timestamp comes from, and the choice is the whole
        point of review j#96412 finding 1: the clock is not a function of the action, and a
        commit built from the clock is a different object every time it is built. A commit's
        own committer date IS a function of the action — the action key covers the source
        head, and this value is part of that object.
        """
        result = self._run("show", "-s", "--format=%cI", "--end-of-options", commit)
        return result.stdout.strip() if result.returncode == 0 else ""

    def apply_merge(
        self, *, source_head: str, target_ref: str, expected_target_head: str
    ) -> MergeResult:
        """Create the merge commit **as objects**, touching no worktree, index, ref or HEAD.

        ``git merge-tree --write-tree <target> <source>`` writes the merged tree into the
        object database and prints its id; ``git commit-tree`` then wraps it with the two
        parents. Every input is an object id, so there is no name for anything to re-point
        between the decision and the mutation — which is the whole point.

        R9 review j#96406 finding 1 is why this is not a worktree merge any more. The previous
        form switched a *path* to the target branch and merged there, with that path's identity
        established by an earlier probe. Reproduced on real git: swapping a foreign lane's
        clean checkout onto that path between the probe and the merge switched the foreign
        checkout off its own branch and built the merge commit on it, and it reported success.
        A non-force push and an exact-SHA CI gate what *lands*; neither undoes a checkout
        someone else was standing in.

        **The commit is a function of the action.** R10 review j#96412 finding 1 measured the
        same action producing two different SHAs a second apart, because ``commit-tree`` takes
        its identity from the host's configuration and its timestamps from the clock — two
        inputs no action key covers, and which I had overlooked when calling this operation
        "object ids only". Both are pinned here: a fixed literal identity (so two hosts agree)
        and the source commit's own committer date (so two runs agree). A replay after a crash
        rebuilds the *same* object, which is what makes the ledger's idempotency real rather
        than asserted.

        Failure is a typed status, never a boolean. ``merge-tree`` exits 1 for a missing
        object exactly as it does for a real conflict (measured), so the exit code alone
        cannot classify: a conflict prints the merged tree's id first, an operational failure
        prints none. "Unsupported" is a separate question, asked of the version.
        """
        if not _is_full_sha(expected_target_head) or not _is_full_sha(source_head):
            return MergeResult(
                status=MERGE_INVALID_INPUT,
                detail=(
                    "refusing to merge: both the source and the expected target must be full "
                    f"commit SHAs (source={source_head!r} target={expected_target_head!r})"
                ),
            )
        branch = _checked_branch(target_ref)
        if not self._merge_tree_supported():
            return MergeResult(
                status=MERGE_PRIMITIVE_UNSUPPORTED,
                detail=(
                    "`git merge-tree --write-tree` requires git >= 2.38; this workspace's git "
                    "cannot build a merge without a checkout, and merging in one is not an "
                    "available fallback (#13686 j#96406)"
                ),
            )
        merged = self._run(
            "merge-tree",
            "--write-tree",
            "--end-of-options",
            expected_target_head,
            source_head,
        )
        first_line = merged.stdout.strip().splitlines()[0].strip() if merged.stdout.strip() else ""
        tree = first_line if _is_full_sha(first_line) else ""
        if merged.returncode != 0:
            # A conflict names the tree it produced; an operational failure names nothing.
            if merged.returncode == 1 and tree:
                return MergeResult(
                    status=MERGE_CONTENT_CONFLICT,
                    detail=(
                        "the branches conflict in content; auto-resolution and auto-rebase "
                        f"are prohibited: {merged.stdout.strip()[:400]}"
                    ),
                )
            return MergeResult(
                status=MERGE_ERROR,
                detail=(
                    f"the object-level merge failed (exit {merged.returncode}) without "
                    "producing a tree; this is NOT a content conflict and NOT proof that the "
                    f"primitive is unavailable: {merged.stderr.strip()[:300]}"
                ),
            )
        if not tree:
            return MergeResult(
                status=MERGE_ERROR,
                detail="the merge reported success but named no tree; refusing to commit it",
            )
        source_date = self._commit_date(source_head)
        if not source_date:
            return MergeResult(
                status=MERGE_ERROR,
                detail=(
                    f"could not read {source_head}'s committer date, which the merge commit's "
                    "timestamps are derived from; refusing to fall back to the clock"
                ),
            )
        committed = self._run(
            "commit-tree",
            tree,
            "-p",
            expected_target_head,
            "-p",
            source_head,
            "-m",
            f"Merge {source_head} into {branch}",
            env={
                "GIT_AUTHOR_NAME": ACTUATOR_IDENTITY_NAME,
                "GIT_AUTHOR_EMAIL": ACTUATOR_IDENTITY_EMAIL,
                "GIT_AUTHOR_DATE": source_date,
                "GIT_COMMITTER_NAME": ACTUATOR_IDENTITY_NAME,
                "GIT_COMMITTER_EMAIL": ACTUATOR_IDENTITY_EMAIL,
                "GIT_COMMITTER_DATE": source_date,
            },
        )
        integration_head = committed.stdout.strip() if committed.returncode == 0 else ""
        if not _is_full_sha(integration_head):
            return MergeResult(
                status=MERGE_COMMIT_ERROR,
                detail=(
                    "the merged tree could not be committed: "
                    f"{committed.stderr.strip()[:200]}"
                ),
            )
        return MergeResult(
            status=MERGE_MERGED,
            integration_head=integration_head,
            detail=(
                f"merged {source_head} onto {expected_target_head} as objects "
                "(first parent is the measured target; no worktree, index or ref touched; "
                "identity and timestamps derived from the action, so a replay rebuilds the "
                "same commit)"
            ),
        )

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        """Push ``source_head`` to ``target_ref`` with a normal, non-force push.

        The refspec is built from a validated bare branch name, so it can never carry the
        leading ``+`` that spells a force. A rejection means the remote moved; the answer is
        to re-form the action against the new head, and this adapter offers no other one.
        """
        branch = _checked_branch(target_ref)
        if not _is_full_sha(source_head):
            return PushResult(
                accepted=False,
                rejected=False,
                detail=f"refusing to push {source_head!r}: not a full commit SHA",
            )
        result = self._run(
            "push",
            "--atomic",
            "--end-of-options",
            self.remote,
            f"{source_head}:refs/heads/{branch}",
        )
        if result.returncode == 0:
            return PushResult(
                accepted=True,
                detail=f"pushed {source_head} to {self.remote}/{branch} (non-force)",
            )
        return PushResult(
            accepted=False,
            rejected=True,
            detail=(
                f"non-force push to {self.remote}/{branch} was rejected; re-form the action "
                f"against the new target head (never force, never rebase): "
                f"{result.stderr.strip()}"
            ),
        )


__all__ = (
    "DEFAULT_REMOTE",
    "UnsafeRefspecError",
    "LiveAutoIntegrationGitOperations",
)
