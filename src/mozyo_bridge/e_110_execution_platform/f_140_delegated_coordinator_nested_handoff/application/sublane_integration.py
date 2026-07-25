"""Sublane Git worktree / retire-merge composition (Redmine #12604).

Composes the pure #12604 decision core
(:mod:`...domain.sublane_integration_policy`) with an **injected** Git operations
port, mirroring the established #12557 executor pattern: the decision is authority and
the use case never re-decides; all real ``git`` side effects are behind a Protocol so
the classical tests drive fakes and the credential / destructive live wiring stays a
deferred, gated follow-up.

Three parts:

- :func:`policy_from_config` translates the governance config knob
  (:class:`~mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config.SublaneIntegrationConfig`)
  into the domain :class:`SublaneIntegrationPolicy`. The application layer owns this
  translation so the domain never imports the governance schema.
- :class:`SublaneIntegrationUseCase` runs the launch and retire decisions against the
  injected :class:`SublaneGitOperations` port. The runtime preflight is the final
  authority: the use case probes git facts, takes the durable-record invariants from the
  caller, asks the pure policy, and performs *only* the additive side effect the
  decision authorizes (create a worktree; attempt a merge). It never removes a worktree,
  deletes a branch, kills a pane, or touches a remote — the destructive retirement ops
  stay coordinator-owned (Sublane Retirement Drain).
- :class:`LiveSublaneGitOperations` is the subprocess adapter for the read probes and
  the additive ``git worktree add``. The stateful retire-time merge execution and the
  destructive retire CLI are deliberately **not** wired here: the
  ``worktree-lifecycle-boundary.md`` boundary doc routes a core Git-worktree lifecycle
  *command* (and any destructive merge / remove orchestration) through a separate issue
  + Design Consultation. :meth:`LiveSublaneGitOperations.merge_to_integration_branch`
  therefore fails closed with a pointer to that gate rather than silently performing a
  branch checkout + merge in this lane.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    LAUNCH_CREATE_WORKTREE,
    LaunchPreflight,
    RetireDecision,
    RetirePreflight,
    SublaneIntegrationPolicy,
    WorktreeLaunchDecision,
    decide_retire_integration,
    decide_worktree_launch,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    SublaneIntegrationConfig,
)


def policy_from_config(config: SublaneIntegrationConfig) -> SublaneIntegrationPolicy:
    """Translate the governance config knob into the domain policy (pure mapping).

    A behavior-preserving identity mapping of the three operational fields; kept in the
    application layer so the pure domain never depends on the governance config schema.
    """
    return SublaneIntegrationPolicy(
        manage_worktree=config.manage_worktree,
        integration_branch=config.integration_branch,
        merge_on_retire=config.merge_on_retire,
    )


# ---------------------------------------------------------------------------
# Committed-blob read states (Redmine #14258). Separate tokens for "the ref resolves
# and the path is not tracked there" and "the ref itself is unreadable": the first is a
# legitimate absence, the second is unknowable and must fail closed.
# ---------------------------------------------------------------------------

#: A full commit SHA: exactly 40 lowercase hex digits. A pin that is not this shape is not a
#: pin, so it is refused rather than passed on to `git worktree add`.
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: Forces git's own ambiguity warning ON for one command, whatever the repo config says.
#: This is the whole of the pseudo-ref rule this code carries (#14258 R9): git decides what
#: is ambiguous, and the only things controlled here are that the ambient config cannot
#: silence it and that the verdict is read WITHOUT parsing the message (so it is
#: locale-independent too).
_FORCE_AMBIGUITY_WARNING: tuple[str, ...] = ("-c", "core.warnAmbiguousRefs=true")

BLOB_PRESENT = "blob_present"
BLOB_ABSENT = "blob_absent"
BLOB_REF_UNRESOLVABLE = "blob_ref_unresolvable"


# ---------------------------------------------------------------------------
# Injected Git operations port.
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneGitOperations(Protocol):
    """The Git operations the use case needs, injected so tests drive fakes.

    Read probes (``is_git_workspace`` / ``worktree_exists`` / ``worktree_dirty`` /
    ``integration_branch_resolved``) are side-effect-free. ``create_worktree`` is the
    single additive mutation the launch path performs. ``merge_to_integration_branch``
    is the retire-time merge: it returns ``True`` when the merge **conflicts** (so the
    decision can fail closed to ``integration_blocked``) and ``False`` on a clean merge.
    There is intentionally no remove / delete / pane-kill / push method — the
    destructive retirement ops are coordinator-owned, not this use case's.
    """

    def is_git_workspace(self) -> bool: ...

    def worktree_exists(self, branch: str) -> bool: ...

    def create_worktree(
        self, *, branch: str, worktree_path: str, base_ref: Optional[str] = None
    ) -> None: ...

    def worktree_dirty(self) -> bool: ...

    def integration_branch_resolved(self, branch: Optional[str]) -> bool: ...

    def merge_to_integration_branch(self, branch: Optional[str]) -> bool: ...


# ---------------------------------------------------------------------------
# Caller-supplied durable-record invariants for the retire decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetireInvariants:
    """The config-undisableable retire invariants, read from the durable record.

    These are the facts no ``config.yaml`` can switch off (the config schema has no key
    for them). The coordinator supplies them from the Redmine issue / journal state; the
    use case never infers them from git. Defaults are the *unsatisfied* / safe-failing
    values for the safety-critical ones so a caller that forgets a field fails closed —
    except the ones that are true by construction at a retire attempt.

    Redmine #13602 (Design Consultation j#76403, Option A): there is deliberately no
    ``owner_approval_present`` invariant — routine green-preflight retirement is coordinator
    authority. ``issue_closed`` abstracts over the close contract that applied to the issue
    type (a child Task/Test/Bug via ``task_close`` with no owner_close_approval; a US /
    standalone issue via an owner_close_approval-backed close — central preset
    ``US-Level Audit Model``), which the coordinator asserts as a single closed fact; retire
    never re-collects the owner close approval. An outstanding owner-approval-waiting still
    blocks via ``callbacks_drained``.
    """

    target_identity_known: bool = False
    verification_passed: bool = False
    issue_closed: bool = False
    callbacks_drained: bool = False
    durable_record_recorded: bool = False
    #: The latest review generation is admissible for integration (#13518 review R2-F7 / R4-F3): the
    #: latest generation is approved with NO unresolved blocking finding
    #: (:func:`...domain.review_generation.evaluate_integration_admissible`). Like every other
    #: invariant here it defaults to the UNSATISFIED (fail-closed) value — a caller that omits it is
    #: BLOCKED, never default-admitted; the coordinator supplies the measured / durable-record
    #: admissibility. (Previously this one field defaulted True, an inconsistent bypass — R4-F3.)
    latest_generation_admissible: bool = False


# ---------------------------------------------------------------------------
# Use case.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SublaneIntegrationUseCase:
    """Composes the #12604 launch / retire decisions with the injected git port."""

    operations: SublaneGitOperations
    policy: SublaneIntegrationPolicy

    def plan_launch(
        self, *, branch: str, worktree_path: str
    ) -> WorktreeLaunchDecision:
        """Decide the launch default path and perform the additive creation, if any.

        Probes ``is_git_workspace`` / ``worktree_exists`` through the port, builds the
        :class:`LaunchPreflight` (the target identity is known only when both a branch
        and a worktree path are supplied), asks the pure
        :func:`decide_worktree_launch`, and creates the worktree **only** when the
        decision is :data:`LAUNCH_CREATE_WORKTREE`. Every other action (skip / reuse /
        blocked) performs no side effect.
        """
        is_git = self.operations.is_git_workspace()
        identity_known = bool(branch) and bool(worktree_path)
        worktree_exists = (
            self.operations.worktree_exists(branch)
            if is_git and identity_known
            else False
        )
        preflight = LaunchPreflight(
            is_git_workspace=is_git,
            worktree_exists=worktree_exists,
            branch_resolved=bool(branch),
            target_identity_known=identity_known,
        )
        decision = decide_worktree_launch(self.policy, preflight)
        if decision.action == LAUNCH_CREATE_WORKTREE:
            self.operations.create_worktree(branch=branch, worktree_path=worktree_path)
        return decision

    def evaluate_retire(self, *, invariants: RetireInvariants) -> RetireDecision:
        """Decide whether the lane may retire; attempt the merge only when safe.

        The runtime preflight is the final authority: git facts are probed through the
        port, the invariants come from the durable record, and the pure
        :func:`decide_retire_integration` decides. The merge is attempted **only** when
        every non-merge gate already passes — so a dirty worktree, an open issue, an
        undrained callback, or a failed verification blocks retirement *before* any merge
        runs. A merge conflict then re-decides to ``integration_blocked``.
        """
        # R2-F7 / R3-F2 integration latest-generation fence: the inadmissible-generation stop is now
        # threaded through the pure :func:`decide_retire_integration` as a first-class preflight
        # invariant (the SAME authority the actual CLI retire path uses — no separate early-return
        # that only this non-CLI use case honoured). A stale last-write-wins approval never
        # integrates: the fence blocks BEFORE any merge because a merge is attempted only after every
        # non-merge gate (this one included) already passes.
        is_git = self.operations.is_git_workspace()
        target = self.policy.integration_branch
        worktree_dirty = self.operations.worktree_dirty() if is_git else False
        branch_resolved = (
            self.operations.integration_branch_resolved(target)
            if is_git and self.policy.merge_on_retire
            else True
        )

        base_preflight = RetirePreflight(
            is_git_workspace=is_git,
            worktree_dirty=worktree_dirty,
            integration_branch_resolved=branch_resolved,
            merge_conflict=False,
            target_identity_known=invariants.target_identity_known,
            verification_passed=invariants.verification_passed,
            issue_closed=invariants.issue_closed,
            callbacks_drained=invariants.callbacks_drained,
            durable_record_recorded=invariants.durable_record_recorded,
            latest_generation_admissible=invariants.latest_generation_admissible,
        )

        # First decide WITHOUT attempting the merge. If anything blocks (including an
        # unresolved target branch), retire is refused and no merge is performed.
        decision = decide_retire_integration(self.policy, base_preflight)
        if decision.is_blocked:
            return decision

        # Clean so far. Attempt the merge only if the policy opted in and we are in a
        # Git workspace; a conflict re-decides to integration_blocked.
        if is_git and self.policy.merge_on_retire:
            conflict = self.operations.merge_to_integration_branch(target)
            if conflict:
                return decide_retire_integration(
                    self.policy,
                    RetirePreflight(
                        is_git_workspace=is_git,
                        worktree_dirty=worktree_dirty,
                        integration_branch_resolved=branch_resolved,
                        merge_conflict=True,
                        target_identity_known=invariants.target_identity_known,
                        verification_passed=invariants.verification_passed,
                        issue_closed=invariants.issue_closed,
                        callbacks_drained=invariants.callbacks_drained,
                        durable_record_recorded=invariants.durable_record_recorded,
                        # R4-F3: propagate the fence (default is now fail-closed) so a merge-conflict
                        # re-decision does not spuriously add stale_review_generation after step 1
                        # already admitted the generation.
                        latest_generation_admissible=invariants.latest_generation_admissible,
                    ),
                )
        return decision


# ---------------------------------------------------------------------------
# Live subprocess adapter (reads + additive worktree add; merge execution gated).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveSublaneGitOperations:
    """Subprocess-backed :class:`SublaneGitOperations` for a concrete repo root.

    Implements the read probes and the additive ``git worktree add``. The stateful
    retire-time merge (which would check out the integration branch and merge into it)
    and the destructive retire CLI are deferred to a separate issue + Design Consultation
    per ``vibes/docs/logics/worktree-lifecycle-boundary.md`` (the boundary doc's
    ``scope 境界 / Design Consultation triggers``); :meth:`merge_to_integration_branch`
    fails closed rather than performing it here.
    """

    repo_root: Path

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
        )

    def is_git_workspace(self) -> bool:
        result = self._run("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def worktree_exists(self, branch: str) -> bool:
        # A worktree backing ``branch`` shows up in ``git worktree list --porcelain`` as
        # a ``branch refs/heads/<branch>`` line.
        result = self._run("worktree", "list", "--porcelain")
        if result.returncode != 0:
            return False
        needle = f"branch refs/heads/{branch}"
        return any(line.strip() == needle for line in result.stdout.splitlines())

    def create_worktree(
        self, *, branch: str, worktree_path: str, base_ref: Optional[str] = None
    ) -> None:
        # #13293: a supplied ``base_ref`` is appended as the ``<commit-ish>`` positional
        # so the new branch is cut from that ref instead of the ambient checkout HEAD
        # (the j#72677 base trap: a stale main checkout would otherwise branch a lane
        # from an unintended base). ``None`` keeps the historical HEAD-based behavior.
        args = ["worktree", "add", worktree_path, "-b", branch]
        base = (base_ref or "").strip()
        if base:
            args.append(base)
        result = self._run(*args)
        if result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for branch {branch!r} at {worktree_path!r}"
                + (f" from base {base!r}" if base else "")
                + f": {result.stderr.strip()}"
            )

    def resolve_commit(self, ref: str) -> str:
        """Resolve ``ref`` to a single immutable full commit SHA, or ``""`` (Redmine #14258 R1).

        A **string** ref is not a pin. The launcher preflight reads the config at the lane's
        base and ``git worktree add`` then materializes it, and between those two operations a
        branch / remote-tracking ref can advance — measured: the preflight admitted a v2 config
        and the worktree materialized a v99 one, defeating the gate that exists precisely to
        keep an unverified config out of a new worktree (review j#87746 R1). Callers resolve
        once, here, and use the returned commit for BOTH.

        Fail-closed on everything that is not exactly one commit: an unresolvable ref, a
        non-commit object, output that is not a single 40-hex line, or an **ambiguous** name.
        ``""`` means "no pin", and every caller treats that as a zero-mutation refusal rather
        than falling back to the ref.

        Ambiguity is decided by **git**, not by a model of git kept here (reviews j#87762 R5,
        j#87772 R8, j#87777 R9). Modelling it was wrong twice in ways only a reviewer found:
        the resolution order's first entry (``$GIT_DIR/<name>``) was missed, and then the
        pseudo-ref boundary was inferred to be upper-case names when the real criterion is
        whether the file's *content* is a valid ref (``.git/config`` is unambiguous because it
        is INI, not because it is lower-case). So the judgment is delegated to the authority
        and this code controls only how the question is asked:

        - :data:`_FORCE_AMBIGUITY_WARNING` turns git's own warning on for this one command, so
          a repo's ``core.warnAmbiguousRefs=false`` cannot silence it;
        - the verdict is "did git say anything at all while succeeding" — the message is never
          parsed, so a translated warning reads the same as an English one.

        An earlier revision also counted the ``refs/…`` candidates directly as a second
        signal. A mutation probe showed no test could tell whether that code was present, and
        it carried a partial model of the very rule this delegation exists to stop maintaining
        — so it was removed rather than shipped unpinned.

        Deliberate over-refusal: any warning git emits while succeeding fails this closed, not
        just an ambiguity one. That is the intended trade — the refusal is zero-mutation, a pin
        git cannot produce silently is not one to trust, and an explicit full SHA always
        resolves without a warning (measured), so a caller who needs to proceed has an exact,
        unambiguous way to say so.
        """
        result = self._run(
            *_FORCE_AMBIGUITY_WARNING,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
        )
        if result.returncode != 0:
            return ""
        if (result.stderr or "").strip():
            return ""
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            return ""
        commit = lines[0]
        return commit if _FULL_SHA_RE.fullmatch(commit) else ""

    def committed_blob(self, *, ref: str, relpath: str) -> tuple[str, str]:
        """Read a committed file's text at ``ref`` without checking anything out (#14258).

        Returns ``(state, text)`` where ``state`` is :data:`BLOB_PRESENT` (text is the blob),
        :data:`BLOB_ABSENT` (the ref resolves and the path is simply not tracked there), or
        :data:`BLOB_REF_UNRESOLVABLE` (the ref itself could not be read — nothing about the
        path is knowable). The presence question is answered by ``ls-tree`` rather than by
        interpreting ``git show``'s failure text, so "no such path at this ref" and "no such
        ref" are never conflated into one fail-open "absent".

        The ``sublane create`` launcher gate needs this: the lane worktree does not exist when
        the gate must refuse, and the config the lane will get is the blob at its base ref —
        reading the primary checkout's working file instead would be a proxy for the target.
        Read-only: ``ls-tree`` / ``show`` touch no ref, index, or working tree.
        """
        listed = self._run("ls-tree", ref, "--", relpath)
        if listed.returncode != 0:
            return BLOB_REF_UNRESOLVABLE, ""
        if not listed.stdout.strip():
            return BLOB_ABSENT, ""
        shown = self._run("show", f"{ref}:{relpath}")
        if shown.returncode != 0:
            # Listed but unreadable (a submodule / non-blob entry, or a git failure): the
            # content is unknowable, which is not the same as absent.
            return BLOB_REF_UNRESOLVABLE, ""
        return BLOB_PRESENT, shown.stdout

    def worktree_dirty(self) -> bool:
        result = self._run("status", "--porcelain")
        if result.returncode != 0:
            # An unreadable status is treated as dirty (fail-closed): never report a
            # worktree we cannot inspect as clean.
            return True
        return bool(result.stdout.strip())

    def integration_branch_resolved(self, branch: Optional[str]) -> bool:
        target = branch if branch else "HEAD"
        result = self._run("rev-parse", "--verify", "--quiet", target)
        return result.returncode == 0 and bool(result.stdout.strip())

    def merge_to_integration_branch(self, branch: Optional[str]) -> bool:
        raise NotImplementedError(
            "live retire-time merge execution is gated: the stateful branch checkout + "
            "merge orchestration and the destructive retire CLI are deferred to a "
            "separate issue + Design Consultation per worktree-lifecycle-boundary.md. "
            "The pure decision (decide_retire_integration) and this use case are wired "
            "and tested with fakes; only the live actuator is gated."
        )


__all__ = (
    "BLOB_ABSENT",
    "BLOB_PRESENT",
    "BLOB_REF_UNRESOLVABLE",
    "policy_from_config",
    "SublaneGitOperations",
    "RetireInvariants",
    "SublaneIntegrationUseCase",
    "LiveSublaneGitOperations",
)
