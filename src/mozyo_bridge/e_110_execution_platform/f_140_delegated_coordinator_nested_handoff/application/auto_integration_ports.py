"""The ports the #13686 actuator acts through, and the value objects they exchange.

Split from :mod:`...application.auto_integration_actuator` to keep that module inside the
module-health line budget; the boundary is a real one either way — these are the seams the
actuator is defined against, and the use case is what composes them.

What this interface can and cannot express IS the safety story, arrived at over seven review
rounds. Only weak mutations exist: a non-force push, a merge in a dedicated worktree bound to
the expected target parent, a ``worktree remove`` without ``--force``, and a branch delete git
itself refuses while any worktree holds the branch. There is no force push, no rebase, no
remote ref delete, and no unconditional ref delete anywhere here. An operation this port
cannot express is one the actuator cannot perform, which is the point — R2 review j#96350
finding 1 was resolved by DELETING the remote-branch delete rather than guarding it, because a
remote ref delete has no non-force compare-and-swap.

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
    IntegrationWorktree,
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

    Only two mutations exist on the integration side (``apply_merge`` / ``push_non_force``)
    and two on the cleanup side, and every one of them is the *weak* form: there is no force
    push, no rebase, no ``--force`` worktree removal, and no unconditional ref delete
    anywhere in this interface. An operation this port cannot express is one the actuator
    cannot perform, which is the point — and it is why R1 review j#96344 finding 1 was
    resolved by DELETING ``delete_remote_branch`` from this interface rather than by adding a
    guard in front of it: a remote ref delete has no non-force compare-and-swap, so it cannot
    be offered safely, so it is not offered.
    """

    def apply_merge(
        self,
        *,
        source_head: str,
        target_ref: str,
        integration_worktree: str,
        expected_target_head: str,
    ) -> MergeResult:
        """Merge ``source_head`` into ``target_ref`` inside ``integration_worktree``.

        The dedicated worktree is required (j#77124): the lane's own worktree never checks
        the target branch out. A conflict is reported, never auto-resolved.

        ``expected_target_head`` is the commit the merge's target parent MUST be. R6 review
        j#96391 finding 1: the adapter switched to the target branch and merged onto whatever
        that worktree's local tip happened to be, so a dedicated worktree carrying an extra
        unreviewed commit produced a merge containing it — and the push, being a fast-forward
        from the remote's point of view, was accepted. Reading the target head from the remote
        (R4) and merging onto the remote's commit are two different claims; only the first was
        implemented. An implementation MUST refuse when the local target tip differs.
        """
        ...

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        """Push ``source_head`` to ``target_ref`` with a normal, non-force push."""
        ...

    def describe_integration_worktree(
        self, *, path: str, lane_worktree: str
    ) -> IntegrationWorktree:
        """MEASURE the dedicated integration worktree's identity (read-only).

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

    def branch_tip(self, branch: str) -> str:
        """The full SHA ``branch`` points at, or ``""``."""
        ...

    def branch_checked_out_elsewhere(self, branch: str) -> bool:
        """True iff any worktree still holds ``branch`` checked out (fail-closed)."""
        ...

    def remove_worktree(self, *, worktree_path: str) -> bool:
        """Remove the worktree at ``worktree_path`` without ``--force``."""
        ...

    def delete_local_branch(self, *, branch: str, expected_tip: str) -> bool:
        """Compare-and-swap delete: remove ``branch`` only while it points at ``expected_tip``."""
        ...


@runtime_checkable
class ManagedProcessOperations(Protocol):
    """Releasing the lane's managed pane / process — the one Git-independent cleanup step."""

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
