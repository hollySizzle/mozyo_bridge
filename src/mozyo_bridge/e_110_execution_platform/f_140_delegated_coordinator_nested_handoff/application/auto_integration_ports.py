"""The ports the #13686 actuator acts through, and the value objects they exchange.

Split from :mod:`...application.auto_integration_actuator` to keep that module inside the
module-health line budget; the boundary is a real one either way — these are the seams the
actuator is defined against, and the use case is what composes them.

What this interface can and cannot express IS the safety story, arrived at over nine review
rounds. Only two mutations exist, both on the integration side: a non-force push, and a merge
in a dedicated worktree bound to the expected target parent. There is no force push, no
rebase, **no ref delete of any kind, and no worktree removal** — an operation this port
cannot express is one the actuator cannot perform, which is the point.

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
    """The outcome of a normal, non-force push.

    ``rejected`` is what a lost race looks like: the remote moved and a non-force push
    cannot advance it. There is deliberately no field that would let a caller retry it as a
    force — the resolution is to re-form the action against the new target head.
    """

    accepted: bool
    rejected: bool = False
    detail: str = ""


@dataclass(frozen=True)
class MergeResult:
    """The outcome of applying a merge-commit disposition in a dedicated worktree.

    ``integration_head`` is the exact commit the merge produced — a *different* commit from
    the source head, which is why the two are recorded separately (the same reason the
    Hibernate Evidence Marker Contract splits ``head`` from ``integration_head``: one head
    cannot prove a merge-commit integration).
    """

    conflicted: bool
    integration_head: str = ""
    detail: str = ""


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

        A conflict is reported, never auto-resolved, and it MUST be distinguishable from the
        merge primitive being unavailable.
        """
        ...

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        """Push ``source_head`` to ``target_ref`` with a normal, non-force push."""
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
    """

    issue_closed: bool = False
    integration_confirmed: bool = False
    integration_ci_settled_green: bool = False
    callbacks_drained: bool = False
    owner_gates_resolved: bool = False


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
    """

    def read(self, *, action_key: str) -> Sequence[StepOutcome]: ...

    def append(self, outcome: StepOutcome) -> None: ...


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

    def append(self, outcome: StepOutcome) -> None:
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
