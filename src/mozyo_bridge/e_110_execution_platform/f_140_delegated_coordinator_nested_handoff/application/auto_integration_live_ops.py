"""Live subprocess adapter for the #13686 auto-integration actuator (Redmine #13686).

The concrete :class:`~...application.auto_integration_actuator.AutoIntegrationGitOperations`
implementation: real ``git`` invocations for the read probes the action-time preflight needs
and for the three weak mutations the port defines. It replaces the deliberately gated
``LiveSublaneGitOperations.merge_to_integration_branch`` ``NotImplementedError``, which was
held closed until this issue's design consultation (j#77124) and owner decision (j#96335)
settled what an auto-integration is allowed to be.

What the adapter can and cannot express is the safety story, so it is enumerated here:

- **Push is always non-force.** ``git push <remote> <sha>:refs/heads/<branch>``. There is no
  ``--force``, no ``--force-with-lease`` (still a force), and no ``+`` refspec — the last is
  additionally refused by construction, because ``+`` is how a force is spelled *inside* a
  refspec and a branch name is not trusted to be free of it.
- **Merge is `--no-ff` inside a dedicated worktree**, never in the lane's checkout, and never
  with a conflict resolution strategy. A conflict aborts the merge and is reported.
- **`git worktree remove` runs without `--force`.** git itself then refuses a dirty or
  unregistered worktree, so the refusal has a second, independent enforcer.
- **No ref is deleted here — local or remote.** Both deletes were shipped and both were
  removed, because neither could enforce its own condition in one invocation:

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
    form was reproduced destroying a commit that landed in the window between them.

  Deleting a lane branch is an operator step in the ``preflight_sublane_retire`` runbook,
  where ``git branch -d`` refuses unmerged work and a human decides. Nothing here deletes or
  rewrites any ref.

Every probe fails closed: a ``git`` that could not run has proven nothing, so a failed
invocation reads as the unsafe answer (not a workspace, not an ancestor, not reachable,
dirty) rather than as a permissive one. The spawn-failure mapping mirrors
:meth:`...application.sublane_integration.LiveSublaneGitOperations._run` — a missing ``cwd``
after a host reboot (#14499) raises ``FileNotFoundError`` rather than exiting non-zero, and
letting that escape a read-only preflight is how six production runs ended in tracebacks.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    MergeResult,
    PushResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    EMPTY_TARGET_HEAD,
    IntegrationWorktree,
)

#: The default shared remote. Named rather than inlined so the one place a remote is chosen
#: is visible; it is not operator-configurable here because the config schema deliberately
#: has no key that could redirect a push.
DEFAULT_REMOTE = "origin"

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

    def _run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run ``git`` in :attr:`repo_root` (or ``cwd``), mapping a spawn failure to a failure.

        ``subprocess.run(cwd=...)`` raises ``FileNotFoundError`` — not a non-zero exit — when
        ``cwd`` does not exist, and ``git`` missing from ``PATH`` raises the same ``OSError``
        family. Both are mapped onto a failed result so a read-only probe fails closed
        instead of raising out of a preflight (#14499).
        """
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd or self.repo_root,
                text=True,
                capture_output=True,
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

    def describe_integration_worktree(
        self, *, path: str, lane_worktree: str
    ) -> IntegrationWorktree:
        """Measure the facts that decide whether ``path`` may be integrated in (read-only).

        Every field is measured, and every failure to measure reads as the UNSAFE answer:
        an unreadable worktree list means "not registered", an unreadable status means "not
        clean". The identity comparison that matters — is this the lane's own checkout? — is
        done on resolved common paths rather than on the strings, so a symlinked or
        differently-spelled path cannot present the lane's worktree as a different one.
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
        try:
            is_lane = candidate.resolve() == Path(lane_worktree).resolve()
        except OSError:
            # Unresolvable paths cannot be shown to be different, so they are treated as the
            # same — the fail-closed reading of the one question this exists to answer.
            is_lane = True
        branch = self._run("rev-parse", "--abbrev-ref", "HEAD", cwd=candidate)
        return IntegrationWorktree(
            path=str(path),
            registered=registered,
            is_lane_worktree=is_lane,
            clean=not self.worktree_dirty(worktree_path=str(path)),
            checked_out_branch=branch.stdout.strip() if branch.returncode == 0 else "",
        )

    # -- mutations --------------------------------------------------------

    def apply_merge(
        self,
        *,
        source_head: str,
        target_ref: str,
        integration_worktree: str,
        expected_target_head: str,
    ) -> MergeResult:
        """Merge ``source_head`` into ``target_ref`` inside ``integration_worktree``.

        Runs entirely in the dedicated worktree (j#77124): the lane's own checkout never has
        the target branch checked out, so a failed merge cannot strand the lane on someone
        else's branch. A conflict is aborted (``git merge --abort``) so the dedicated
        worktree is left usable, and reported — never resolved by a strategy flag.
        """
        branch = _checked_branch(target_ref)
        worktree = Path(integration_worktree)
        switched = self._run("switch", branch, cwd=worktree)
        if switched.returncode != 0:
            return MergeResult(
                conflicted=True,
                detail=(
                    f"could not switch the integration worktree to {branch}: "
                    f"{switched.stderr.strip()}"
                ),
            )
        # R6 review j#96391 finding 1: the merge's target parent must be the commit the
        # action measured on the REMOTE, not whatever this worktree's local branch points at.
        # Measured wrong: a dedicated worktree holding one extra unreviewed commit produced a
        # merge containing it, and the push was accepted because it was still a fast-forward.
        local_tip = self._run("rev-parse", "--verify", "HEAD", cwd=worktree)
        local_head = local_tip.stdout.strip() if local_tip.returncode == 0 else ""
        if not _is_full_sha(expected_target_head) or local_head != expected_target_head:
            return MergeResult(
                conflicted=True,
                detail=(
                    f"the integration worktree's {branch} is at {local_head or 'an unreadable head'}, "
                    f"not the expected target {expected_target_head}; refusing to merge onto an "
                    "unverified parent"
                ),
            )
        merged = self._run(
            "merge", "--no-ff", "--no-edit", "--end-of-options", source_head, cwd=worktree
        )
        if merged.returncode != 0:
            self._run("merge", "--abort", cwd=worktree)
            return MergeResult(
                conflicted=True,
                detail=(
                    "merge conflicted and was aborted; auto-resolution and auto-rebase are "
                    f"prohibited: {merged.stderr.strip()}"
                ),
            )
        head = self._run("rev-parse", "--verify", "HEAD", cwd=worktree)
        integration_head = head.stdout.strip() if head.returncode == 0 else ""
        if not _is_full_sha(integration_head):
            return MergeResult(
                conflicted=True,
                detail="the merge reported success but its head could not be resolved",
            )
        return MergeResult(
            conflicted=False,
            integration_head=integration_head,
            detail=f"merged {source_head} into {branch} in the dedicated worktree",
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

    def remove_worktree(self, *, worktree_path: str) -> bool:
        """Remove the worktree without ``--force`` (git refuses a dirty / unregistered one)."""
        result = self._run("worktree", "remove", "--end-of-options", worktree_path)
        return result.returncode == 0


__all__ = (
    "DEFAULT_REMOTE",
    "UnsafeRefspecError",
    "LiveAutoIntegrationGitOperations",
)
