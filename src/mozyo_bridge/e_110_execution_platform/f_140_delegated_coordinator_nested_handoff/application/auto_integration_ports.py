"""The ports the #13686 actuator acts through, and the value objects they exchange.

Split from :mod:`...application.auto_integration_actuator` to keep that module inside the
module-health line budget; the boundary is a real one either way — these are the seams the
actuator is defined against, and the use case is what composes them.

What this interface can and cannot express IS the safety story, arrived at over ten review
rounds. Only two mutations exist, both on the integration side: a non-force push, and a merge
built **from objects** onto the expected target parent — no checkout, index, ref or HEAD is
involved. There is no force push, no rebase, **no ref delete of any kind, and no worktree
removal** — an operation this port cannot express is one the actuator cannot perform, which
is the point.

Three destructive operations were removed rather than guarded, and one sentence retired all
three: *an operation whose safety condition cannot be enforced by the operation itself is not
offered.*

- the **remote branch delete** (j#96344 finding 1) — a remote ref delete has no non-force
  compare-and-swap;
- the **local branch delete** (j#96396 finding 1) — its two conditions cannot be enforced by
  any single git invocation, and the guarded two-call form was reproduced destroying a commit
  that landed between the check and the delete;
- the **worktree removal** (j#96401 finding 1) — ``git worktree remove`` identifies its
  target by a *path*, which another actor can re-point between the identity probe and the
  removal; reproduced removing a foreign lane's checkout. ``git worktree lock`` does pin that
  binding, but no mutation may run while it is held, so the unlock that must precede the
  removal reopens the window.

The cleanup half therefore needs nothing from this port at all: its one remaining step is on
:class:`ManagedProcessOperations`, and that primitive's arguments *are* the identity it acts
on.

The read side matters as much: every fact that gates a mutation is measured through a probe
here or read through :class:`DurableAuthorityReader`, never accepted from the caller. That
boundary is the subject of j#96344 -> j#96350 -> j#96368 -> j#96379 -> j#96385 -> j#96391.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    LEDGER_PUSH_STATUS_UNSOUND,
    MERGE_COMMIT_ERROR,
    MERGE_CONTENT_CONFLICT,
    MERGE_ERROR,
    MERGE_INVALID_INPUT,
    MERGE_MERGED,
    MERGE_NONDETERMINISTIC_CONFIG,
    MERGE_PRIMITIVE_UNSUPPORTED,
    MERGE_PROBE_ERROR,
    MERGE_SANDBOX_ERROR,
    MERGE_STATUSES,
    MERGE_UNRECOGNIZED,
    PUSH_ACCEPTED,
    PUSH_INVALID_INPUT,
    PUSH_OPERATIONAL_ERROR,
    PUSH_REMOTE_MOVED,
    PUSH_REMOTE_REFUSED,
    PUSH_STATUSES,
    PUSH_UNRECOGNIZED,
    checked_push_status,
    IntegrationActionRecord,
    IntegrationCiEvidence,
    LaneWorktree,
    StepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    CleanupActionRecord,
)


# ---------------------------------------------------------------------------
# Injected Git operations port.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    """The outcome of a normal, non-force push, as ONE value from a closed vocabulary.

    R21 wrote this as two booleans and described them as three states. Review j#96516
    finding 1 showed both halves of why that fails. ``PushResult(accepted=True,
    rejected=True)`` was constructible and the use case read it as a success — a state that
    means nothing, treated as the state that means everything. And because the adapter had
    only one failure flag, it set ``rejected`` for every non-zero exit, so a ``git`` that
    could not be spawned was durably recorded as "the remote moved; re-form the action
    against the new head" (reproduced). Re-forming it would never have helped.

    So the states are the vocabulary, and the two booleans are DERIVED from it — a
    contradiction is not rejected by validation, it cannot be written down. Each status names
    a different recovery, which is the reason the distinction has to reach the durable
    record: fix the input, re-form the action, take it up with whoever owns the remote's
    policy, or investigate the environment.

    There is deliberately no field that would let a caller retry a lost race as a force — the
    resolution is to re-form the action against the new target head.
    """

    status: str
    detail: str = ""

    @property
    def accepted(self) -> bool:
        """The push landed. Derived, so nothing can be both accepted and something else."""
        return self.status == PUSH_ACCEPTED

    @property
    def rejected(self) -> bool:
        """A lost race, and ONLY that. Not "the push failed" — see the vocabulary."""
        return self.status == PUSH_REMOTE_MOVED


# The merge-status vocabulary lives in the DOMAIN (`...domain.auto_integration_records`) and
# is re-exported here for the port's callers. It has to: the durable `StepOutcome` records the
# status, and a domain record cannot depend on an application-layer literal (j#96417 finding
# 2 required the status reach the durable outcome, which is where it is defined).

@dataclass(frozen=True)
class MergeResult:
    """The outcome of building a merge commit, as a typed status rather than a boolean.

    ``integration_head`` is the exact commit the merge produced — a *different* commit from
    the source head, which is why the two are recorded separately (the same reason the
    Hibernate Evidence Marker Contract splits ``head`` from ``integration_head``: one head
    cannot prove a merge-commit integration).

    R10 review j#96412 finding 2 is why ``status`` exists. This record carried a single
    ``conflicted: bool``, and five different outcomes collapsed into it: invalid arguments, a
    real content conflict, an unusable primitive, an unreadable object, and a failed commit.
    Worse, the adapter classified them by exit code, and ``merge-tree`` exits **1 for a
    missing object exactly as it does for a conflict** (measured) — so "this object does not
    exist" was recorded as "the branches conflict". That is the same defect as j#96396
    finding 2, made in the round that claimed to have learned it, and it is the same defect
    as the very first review's "a boolean cannot be audited": ten rounds of replacing
    booleans with records, and this is one I introduced.

    ``conflicted`` remains as a derived property so callers that only need "may I proceed?"
    keep reading correctly, but nothing may *classify* on it.
    """

    status: str
    integration_head: str = ""
    detail: str = ""
    #: The exact ``git --version`` this merge ran under, so a durable record can say which git
    #: produced the commit rather than leaving "same version" as an unverifiable sentence
    #: (j#96435 finding 4).
    git_version: str = ""

    @property
    def conflicted(self) -> bool:
        """True for every non-success. Deliberately derived: a refusal is not a diagnosis."""
        return self.status != MERGE_MERGED

    @property
    def is_content_conflict(self) -> bool:
        """The one outcome that means the *branches* disagree rather than the run failed."""
        return self.status == MERGE_CONTENT_CONFLICT

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "integration_head": self.integration_head,
            "detail": self.detail,
            "git_version": self.git_version,
        }


@runtime_checkable
class AutoIntegrationGitOperations(Protocol):
    """The Git operations the actuator needs, injected so tests drive fakes.

    Exactly two mutations exist, both on the integration side (``apply_merge`` /
    ``push_non_force``), and both are the *weak* form: no force push, no rebase, **no ref
    delete and no worktree removal anywhere in this interface**. An operation this port
    cannot express is one the actuator cannot perform, which is the point — and it is why
    ``delete_remote_branch``, ``delete_local_branch`` and ``remove_worktree`` were each
    DELETED here rather than guarded (see the module docstring for what each one could not
    enforce about itself).
    """

    def apply_merge(
        self, *, source_head: str, target_ref: str, expected_target_head: str
    ) -> MergeResult:
        """Build the merge commit **as objects**: no checkout, no index, no ref, no HEAD.

        Every argument is an object id or a name used only for the commit message, so there is
        nothing here that another actor can re-point between the decision and the mutation.
        An implementation MUST make ``expected_target_head`` the first parent, MUST NOT switch
        or modify any checkout, and MUST NOT move any ref — the push step is the only thing
        that publishes the result.

        There used to be an ``integration_worktree`` argument, and the whole j#77124
        dedicated-checkout apparatus behind it. R6 review j#96391 finding 1 first bound the
        merge's parent to the measured remote target (the adapter had been merging onto
        whatever the checkout's local tip happened to be), and review j#96406 finding 1 then
        reproduced the deeper problem: a foreign lane's clean checkout swapped onto that path
        between the identity probe and the merge was switched off its own branch and had the
        merge built on it, and the call still returned ``conflicted=False``. A path is a name;
        a gate in front of a name is a check, not a guarantee. Objects have no such gap.

        A conflict is reported and never auto-resolved. An implementation MUST return a
        :data:`MERGE_STATUSES` value that says which outcome occurred — a content conflict, an
        unsupported primitive, invalid input, an operational merge failure, or a failed commit
        — and MUST NOT infer "unsupported" from an unrecognized exit code.

        **Determinism, stated as exactly what is enforced.** Two runs of the same action MUST
        produce the same commit id, given the same repository content and the same git
        version. That is narrower than R11's "a function of its arguments alone", which was
        not true: review j#96412 finding 1 measured the host identity and the clock leaking
        in, and j#96417 finding 1 measured ``i18n.commitEncoding`` doing the same.

        An implementation MUST NOT satisfy that by checking the repository for hazards and
        refusing when it finds them. Earlier revisions of this contract required exactly that
        for a configured ``merge.<name>.driver``, and review j#96435 finding 1 reproduced a
        driver added *between* the check and the merge, whose shell command then rewrote the
        merged content. A check and a mutation in two invocations are never bound to the same
        instant. What is required instead is that repository-local state cannot reach the
        merge at all — the reference implementation builds the merge in a throwaway git
        directory whose object store is the repository's and whose config, attributes and
        ``shallow`` are empty.

        An implementation MUST record the exact git version it ran under on a successful
        result: "the same version" is otherwise unverifiable after the fact (j#96441
        finding 4).
        """
        ...

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        """Push ``source_head`` to ``target_ref`` with a normal, non-force push.

        **What an implementation MUST return**, from the closed vocabulary in
        :data:`~...domain.auto_integration_records.PUSH_STATUSES`. The statuses are not
        severities to pick from — each names a DIFFERENT RECOVERY, which is the whole reason
        they may not be collapsed:

        - :data:`PUSH_ACCEPTED` — it landed.
        - :data:`PUSH_INVALID_INPUT` — nothing was attempted: an argument could not be used
          (an incomplete ``source_head``, or a ``target_ref`` that cannot be spelled as a
          provably non-force refspec). Recovery: fix the input. **Refusing an unusable input
          is a return value, not an exception** — R19 and R20 documented a raise here on two
          premises the adapter itself refutes (j#96492 finding 4, j#96499 finding 1).
        - :data:`PUSH_REMOTE_MOVED` — a lost race; the remote's ref is not an ancestor of what
          we offered. Recovery: re-form the action against the new target head. This is the
          only status that means that, and an implementation MUST establish it from what the
          remote actually said about THIS ref — **never by inferring it from a non-zero exit
          status**, which is how R21 recorded a git that could not be spawned as a lost race
          (j#96516 finding 1).
        - :data:`PUSH_REMOTE_REFUSED` — the remote answered and declined the update (a hook, a
          protected branch). Recovery: whoever owns that policy, not a new target head.
        - :data:`PUSH_OPERATIONAL_ERROR` — the push could not be carried out, or the result
          said nothing about our ref at all. Recovery: investigate the environment. An
          implementation MUST NOT report anything about the remote's ref here, because it does
          not know anything about it.

        ``PushResult.accepted`` and ``.rejected`` are DERIVED from the status, so an
        implementation cannot report a contradiction; ``rejected`` continues to mean
        :data:`PUSH_REMOTE_MOVED` and nothing else. A status outside the vocabulary is read as
        :data:`PUSH_UNRECOGNIZED` and is not a success.
        """
        ...

    def describe_lane_worktree(self, *, path: str) -> LaneWorktree:
        """MEASURE the LANE's own checkout (read-only) — the source side of an integration.

        On the port, not merely on the live adapter, because the use case must be able to
        call it. R2 had this probe on the adapter only and never invoked it, so the use case
        re-checked the *caller's* booleans and handed the caller's own path to
        ``apply_merge`` — a forged record naming the lane's worktree passed (j#96350 finding
        3). Whoever measures a safety fact is the authority for it, and that must be the
        actuator.
        """
        ...

    def resolve_head(self, ref: str) -> str:
        """The full commit SHA a LOCAL ``ref`` resolves to (read-only, fail-closed)."""
        ...

    def remote_branch_tip(self, branch: str) -> str:
        """The shared remote's CURRENT tip for ``branch``, read fresh (``""`` on failure).

        "On failure" includes a ``branch`` this implementation cannot safely hand to its
        backend at all: a read that cannot ask the question answers ``""``, it does not raise.
        The actuator performs this read for the action's target ref *before* the apply that
        classifies an unusable ref as ``invalid_input``, so an implementation that raises here
        takes the run down instead of blocking it (j#96461 finding 2).

        The target gate's authority. R4 review j#96379 finding 4: the gate used
        :meth:`resolve_head`, a local ``git rev-parse``, while this fresh ``ls-remote`` probe
        already existed on the same adapter and went unused — so a target another clone had
        advanced still read as its old SHA and the drift was invisible.
        """
        ...

    def is_ancestor(self, *, ancestor: str, descendant: str) -> bool:
        """True iff ``ancestor`` is an ancestor of ``descendant`` (read-only, fail-closed)."""
        ...

    def worktree_dirty(self, *, worktree_path: str = "") -> bool:
        """True iff the worktree has uncommitted / untracked changes (fail-closed)."""
        ...

    def commit_on_remote(self, commit: str, *, branch: str) -> bool:
        """True iff ``commit`` is reachable from the remote's current ``branch`` tip."""
        ...


@runtime_checkable
class ManagedProcessOperations(Protocol):
    """Releasing the lane's managed pane / process — the only cleanup step there is.

    It survived the three destructive withdrawals for a structural reason rather than a lucky
    one: ``release_process`` is parameterized by the identity it acts on, so there is no
    window in which the thing named by its arguments becomes something else. A path and a ref
    name are late-bound; ``issue`` + ``lane_generation`` is not.
    """

    def release_process(self, *, issue: str, lane_generation: int) -> bool: ...


@dataclass(frozen=True)
class IntegrationAuthority:
    """The durable-record facts an integration needs, as read from the source of truth.

    These are the ones no git probe can answer: whether the latest review generation is
    admissible and which head it approved, whether the target ref is an allowlisted
    integration branch, whether callbacks and owner gates are settled, and the source
    branch's CI evidence. R3 review j#96368 finding 1 found them taken verbatim from the
    caller, so an integration could be authorized by the requester's own say-so.

    Every field defaults to its unsatisfied value: a reader that cannot answer leaves the
    gate closed rather than open.
    """

    review_generation_admissible: bool = False
    #: The exact review_request journal that identifies the current approved generation.
    #: Kept separate from the boolean because an action key names this value; an arbitrary
    #: non-empty caller string must not mint a second action under the same approval.
    review_generation: str = ""
    #: The exact head the latest admissible review approved. Compared against the action's
    #: source head, so "reviewed" cannot mean "some earlier commit was reviewed".
    reviewed_head: str = ""
    target_identity_known: bool = False
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False
    source_ci: Optional[IntegrationCiEvidence] = None


@dataclass(frozen=True)
class CleanupAuthority:
    """The durable-record facts a post-close cleanup needs (the destructive half).

    R3 review j#96368 finding 2: every one of these was caller-supplied, and the independent
    reproduction removed a *foreign* lane's worktree and deleted its branch on that basis.
    Both of those steps have since been withdrawn (j#96396 / j#96401 finding 1), and what
    these still gate is the managed-process release — a cross-lane side effect in exactly the
    same way, so they are still read fresh from the durable record.

    ``authorizing_action_key`` (Redmine #14825 item 5) is the integration action the DURABLE
    LIFECYCLE says put this lane's work on the target. It is here because the check that reads it
    could not fail: the cleanup decision compares the preflight's authorizing key against the
    record's ``integration_action_key``, and #13686 filled the preflight FROM that same field —
    two sides of a comparison sourced from one value. A record could therefore authorize itself.
    The reader now answers this from the actuator's own ledger (which integration action actually
    ran to completion for this issue, generation and source head), so the comparison has two
    independently sourced sides again. An empty value is the unsatisfied default: an authority
    that cannot name the authorizing action has not authorized anything.
    """

    issue_closed: bool = False
    integration_confirmed: bool = False
    integration_ci_settled_green: bool = False
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False
    authorizing_action_key: str = ""


@runtime_checkable
class LedgerStore(Protocol):
    """The actuator's own record of what it has done, read and appended by the actuator.

    R4 review j#96379 finding 1: the ledger arrived as a caller-supplied sequence and the
    provenance stamped on entries was derived from public constructor values, so a caller
    could author entries indistinguishable from the actuator's own — claiming a push that
    never happened, or slipping a foreign apply head into the commit the push would use.
    Handing the caller the ledger is the same mistake as handing it the preflight.

    An implementation MUST persist and return whole :class:`StepOutcome` records including
    ``recorded_by`` (:meth:`StepOutcome.as_payload` carries it), and MUST NOT accept entries
    from anywhere but :meth:`append`.

    ``receipt`` is the admission token a durable implementation issues before a mutation and
    requires back with its outcome (Redmine #14825 review j#96611 finding 3: stamping a
    provenance onto an unexamined payload authenticates the file, not the claim). It is
    optional on the Protocol because a process-local store has no admission to authenticate —
    its entries do not outlive the process either — and an implementation that ignores it MUST
    say so, rather than appearing to check something it does not.
    """

    def read(self, *, action_key: str) -> Sequence[StepOutcome]: ...

    def append(self, outcome: StepOutcome, *, receipt: str = "") -> None: ...


@dataclass
class InMemoryLedgerStore:
    """A process-local :class:`LedgerStore` — the default when no durable store is bound.

    Its lifetime is one actuator instance, so a resume across processes finds nothing and the
    run starts over rather than trusting a ledger it cannot attribute. That is the fail-closed
    reading: an unrecoverable ledger means "nothing is known to have run", never "what the
    caller says ran".
    """

    entries: List[StepOutcome] = field(default_factory=list)

    def read(self, *, action_key: str) -> Sequence[StepOutcome]:
        return [entry for entry in self.entries if entry.action_key == action_key]

    def append(self, outcome: StepOutcome, *, receipt: str = "") -> None:
        """Append unconditionally. ``receipt`` is accepted and IGNORED, deliberately.

        There is no admission to authenticate: this store's entries die with the process, so
        the trust question the token answers does not arise here. Saying that plainly is the
        point — a fake check would be worse than none.
        """
        self.entries.append(outcome)


@runtime_checkable
class DurableAuthorityReader(Protocol):
    """Reads the authority facts from the durable record (Redmine), fresh, at action time.

    An implementation MUST read the source of truth rather than any caller-provided cache,
    and MUST leave a field at its unsatisfied default when it cannot establish the fact.
    """

    def read_integration_authority(
        self, *, record: IntegrationActionRecord
    ) -> IntegrationAuthority: ...

    def current_review_generation(self, *, record: IntegrationActionRecord) -> str:
        """Read the exact currently-approved request journal for ``record`` fresh."""
        ...

    def read_integration_ci(
        self, *, record: IntegrationActionRecord, integration_head: str
    ) -> Optional[IntegrationCiEvidence]: ...

    def read_cleanup_authority(
        self, *, record: CleanupActionRecord
    ) -> CleanupAuthority: ...


__all__ = (
    "PushResult",
    "MergeResult",
    "AutoIntegrationGitOperations",
    "ManagedProcessOperations",
    "IntegrationAuthority",
    "CleanupAuthority",
    "DurableAuthorityReader",
    "LedgerStore",
    "InMemoryLedgerStore",
)
