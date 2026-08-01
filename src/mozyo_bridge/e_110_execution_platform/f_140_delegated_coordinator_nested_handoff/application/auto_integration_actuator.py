"""Guarded auto-integration / retirement-cleanup actuator composition (Redmine #13686).

Composes the two pure #13686 state machines
(:mod:`...domain.auto_integration_policy`, :mod:`...domain.retirement_cleanup_policy`) with
an **injected** Git operations port, mirroring the established #12604 / #12557 executor
pattern: the decision is the authority and the use case never re-decides. All real ``git``
side effects sit behind a Protocol, so the classical tests drive fakes and the live
subprocess adapter (:mod:`...application.auto_integration_live_ops`) is one swappable
implementation rather than a hard dependency of the decision path.

Three parts:

- :func:`integration_policy_from_config` / :func:`cleanup_policy_from_config` translate the
  governance config block
  (:class:`~mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records.AutoIntegrationConfig`)
  into the two domain policies. The application layer owns the translation so neither domain
  module imports the governance schema.
- :class:`AutoIntegrationUseCase` executes **one decided step at a time** and returns the
  step's outcome. It performs only the side effect the decision authorized and never
  substitutes a stronger one: no ``--force``, no rebase, no ``git branch -D``, no remote ref
  rewrite. Each executed step is appended to the ledger the next decision reads, which is
  what makes a partial failure resumable without a duplicate merge, push, or delete.
- :class:`IntegrationRunReport` is the replayable record of a run: the states it passed
  through and the outcome of every step, so a durable journal can be rendered from it.

The CI gate is deliberately **not** actuated here. ``integration_ci`` is an asynchronous
gate: this use case cannot make a CI run finish, so it records the step as
:data:`~...domain.auto_integration_policy.OUTCOME_PENDING` and stops. The caller re-runs
once the run has *settled*, recording it ``done`` and supplying the verdict as
``integration_ci_green`` — "the run settled" and "the run was green" are separate facts, and
a single synchronous command never assumes either.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    BLOCKED_PUSH_REJECTED,
    DISPOSITION_MERGE_COMMIT,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_PENDING,
    STEP_INTEGRATION_APPLY,
    STEP_INTEGRATION_CI,
    STEP_PUSH,
    AutoIntegrationPolicy,
    IntegrationActionRecord,
    IntegrationDecision,
    IntegrationPreflight,
    IntegrationWorktree,
    StepOutcome,
    completed_steps,
    is_full_sha,
    decide_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    STEP_LOCAL_BRANCH_DELETE,
    STEP_PROCESS_RETIRE,
    STEP_WORKTREE_REMOVE,
    CleanupActionRecord,
    CleanupDecision,
    CleanupPreflight,
    RetirementCleanupPolicy,
    decide_cleanup,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (
    AutoIntegrationConfig,
)


def integration_policy_from_config(
    config: AutoIntegrationConfig,
) -> AutoIntegrationPolicy:
    """Translate the ``auto_integration`` config block into the integration policy.

    A behavior-preserving identity mapping of the integration fields, kept in the
    application layer so the pure domain never depends on the governance config schema. The
    literal ``mode`` vocabulary is deliberately the same in both records, so an unrecognized
    value stays unrecognized (the decision fails closed on it) rather than being silently
    translated into an actionable one.
    """
    return AutoIntegrationPolicy(
        mode=config.mode,
        integration_branch=config.integration_branch,
        ff_only=config.ff_only,
    )


def cleanup_policy_from_config(
    config: AutoIntegrationConfig,
) -> RetirementCleanupPolicy:
    """Translate the ``auto_integration`` config block into the cleanup policy."""
    return RetirementCleanupPolicy(
        remove_worktree=config.remove_worktree,
        delete_local_branch=config.delete_local_branch,
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
        self, *, source_head: str, target_ref: str, integration_worktree: str
    ) -> MergeResult:
        """Merge ``source_head`` into ``target_ref`` inside ``integration_worktree``.

        The dedicated worktree is required (j#77124): the lane's own worktree never checks
        the target branch out. A conflict is reported, never auto-resolved.
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
        """The full commit SHA ``ref`` resolves to (read-only, fail-closed)."""
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


# ---------------------------------------------------------------------------
# Run report.
# ---------------------------------------------------------------------------


@dataclass
class IntegrationRunReport:
    """The replayable record of one actuator run.

    ``states`` is every state the run rested in, in order, so a durable journal shows the
    path taken rather than only where it ended. ``outcomes`` is the step ledger this run
    produced; appending it to the caller's stored ledger is what makes the next run resume
    instead of repeat.
    """

    states: List[str] = field(default_factory=list)
    outcomes: List[StepOutcome] = field(default_factory=list)
    final_decision: Optional[IntegrationDecision] = None
    #: The exact commit the integration produced on the target, when one was produced. Empty
    #: for a fast-forward (which creates no commit) and for every refusal.
    integration_head: str = ""
    #: What the actuator MEASURED about the dedicated integration worktree, as opposed to
    #: what the caller claimed. Recorded so the durable record shows the measurement.
    measured_worktree: Optional[IntegrationWorktree] = None
    #: The durable-record authority this run READ (as opposed to anything a caller claimed).
    measured_authority: Optional["IntegrationAuthority"] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "states": list(self.states),
            "outcomes": [outcome.as_payload() for outcome in self.outcomes],
            "integration_head": self.integration_head,
            "measured_worktree": (
                self.measured_worktree.as_payload() if self.measured_worktree else None
            ),
            "measured_authority": (
                dataclasses.asdict(self.measured_authority)
                if self.measured_authority
                else None
            ),
            "final_decision": (
                self.final_decision.as_payload() if self.final_decision else None
            ),
        }


@dataclass
class CleanupRunReport:
    """The replayable record of one cleanup run (the mirror of :class:`IntegrationRunReport`)."""

    states: List[str] = field(default_factory=list)
    outcomes: List[StepOutcome] = field(default_factory=list)
    final_decision: Optional[CleanupDecision] = None
    #: What this actuator MEASURED, as opposed to anything a caller claimed.
    measured_preflight: Optional[CleanupPreflight] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "states": list(self.states),
            "outcomes": [outcome.as_payload() for outcome in self.outcomes],
            "measured_preflight": (
                dataclasses.asdict(self.measured_preflight)
                if self.measured_preflight
                else None
            ),
            "final_decision": (
                self.final_decision.as_payload() if self.final_decision else None
            ),
        }


# ---------------------------------------------------------------------------
# Use case.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoIntegrationUseCase:
    """Runs the #13686 integration and cleanup machines against the injected ports.

    **The actuator measures its own safety facts.** Two of the preflight's fields are not
    read from the caller at all: the dedicated integration worktree and the coordinator
    confirmation. R1 typed them and R2 review j#96350 (findings 3 and 4) showed that typing
    changed nothing while the *caller* still supplied the values — a record naming the lane's
    own worktree with ``is_lane_worktree=False``, or a confirmation naming an anchor that does
    not exist, both passed. Whoever measures a safety fact is its authority, so
    :meth:`run_integration` overwrites both from its own ports before deciding. A caller
    supplies only pointers: which path, and which anchor.

    ``lane_worktree`` is this actuator's own lane checkout and
    ``integration_worktree_path`` the dedicated one — constructor state rather than per-call
    arguments, so a single instance cannot be redirected mid-run.
    """

    operations: AutoIntegrationGitOperations
    integration_policy: AutoIntegrationPolicy
    cleanup_policy: RetirementCleanupPolicy = field(
        default_factory=RetirementCleanupPolicy.default
    )
    processes: Optional[ManagedProcessOperations] = None
    authority: Optional[DurableAuthorityReader] = None
    lane_worktree: str = ""
    lane_branch: str = ""
    integration_worktree_path: str = ""

    @property
    def recorder_id(self) -> str:
        """This actuator's ledger provenance — what it stamps on the steps it records.

        R3 review j#96368 finding 3: without it, any caller-authored ledger entry counted as
        a completed step. Derived from the actuator's own lane identity so two lanes cannot
        claim each other's work.
        """
        return f"actuator:{self.lane_worktree}|{self.lane_branch}"

    # -- integration ------------------------------------------------------

    def run_integration(
        self,
        record: IntegrationActionRecord,
        *,
        ledger: Sequence[StepOutcome] = (),
    ) -> IntegrationRunReport:
        """Drive the integration machine until it rests, performing each decided step.

        The loop is decision-driven: it asks for the next step, performs exactly that step,
        appends the outcome, and asks again. It stops as soon as the decision is terminal, as
        soon as a step is refused, or as soon as it reaches the asynchronous CI gate — which
        it records ``pending`` rather than waiting on, because it cannot make a run finish.

        There is no ``preflight`` parameter: the actuator measures the world itself
        (:meth:`_measure`). A caller cannot hand this method a fact, only an action record —
        which is identity, not evidence. The measurement is taken once per run and not
        re-derived between steps; the action key binds the run to it, and a world that has
        changed produces a different key on the next call, which is where re-validation
        belongs.
        """
        report = IntegrationRunReport()
        working_ledger: List[StepOutcome] = list(ledger)
        preflight = self._measure(record, report, ledger=working_ledger)
        # A resumed run has no in-memory memory of the commit a previous run's apply
        # produced, but the ledger recorded it. Without this the push after a resumed apply
        # would push the SOURCE head instead of the merge commit — silently integrating a
        # different commit than the one the apply created.
        for entry in working_ledger:
            if (
                entry.action_key == record.action_key
                and entry.step == STEP_INTEGRATION_APPLY
                and entry.outcome == OUTCOME_DONE
                and entry.head
            ):
                report.integration_head = entry.head

        while True:
            decision = decide_integration(
                self.integration_policy,
                record,
                preflight,
                ledger=working_ledger,
                trusted_recorder=self.recorder_id,
            )
            report.states.append(decision.state)
            report.final_decision = decision
            if decision.next_step is None:
                return report

            outcome = self._perform_integration_step(
                decision, record, report, preflight=preflight
            )
            report.outcomes.append(outcome)
            working_ledger.append(outcome)
            if outcome.outcome != OUTCOME_DONE:
                # A refused or unsettled step stops the run here. Re-deciding would only
                # produce the same step again, and looping on it would turn a fail-closed
                # refusal into a spin.
                report.final_decision = decide_integration(
                    self.integration_policy,
                    record,
                    preflight,
                    ledger=working_ledger,
                    trusted_recorder=self.recorder_id,
                )
                report.states.append(report.final_decision.state)
                return report

    def _measure(
        self,
        record: IntegrationActionRecord,
        report: IntegrationRunReport,
        *,
        ledger: Sequence[StepOutcome],
    ) -> IntegrationPreflight:
        """Build the ENTIRE integration preflight from this actuator's own measurements.

        R3 review j#96368 finding 1: R3 overwrote two fields and took the rest — target head,
        review generation, origin reachability, source CI, dirty / foreign / unpushed,
        callback and owner gates — verbatim from the caller, so the mutation authority was
        still the requester's. There is no caller preflight any more. The caller supplies the
        action record (identity) and this actuator's own lane configuration; every safety fact
        below is read from a port at action time.

        Anything a port cannot answer stays at its unsatisfied value, so an unreadable world
        blocks rather than admits.
        """
        ops = self.operations
        if not ops.is_git_workspace():
            return IntegrationPreflight(is_git_workspace=False)

        observed_target = ops.resolve_head(record.target_ref)
        lane = ops.describe_integration_worktree(
            path=self.lane_worktree, lane_worktree=self.lane_worktree
        )
        integration_worktree = ops.describe_integration_worktree(
            path=self.integration_worktree_path, lane_worktree=self.lane_worktree
        )
        report.measured_worktree = integration_worktree

        authority = (
            self.authority.read_integration_authority(record=record)
            if self.authority is not None
            else IntegrationAuthority()
        )
        report.measured_authority = authority

        # The CI gate is about the commit the push RECORDED landing, so it is read only once
        # that head exists in this actuator's own ledger.
        landed = completed_steps(
            ledger, action_key=record.action_key, recorded_by=self.recorder_id
        ).get(STEP_PUSH)
        integration_ci = (
            self.authority.read_integration_ci(
                record=record, integration_head=landed.head
            )
            if self.authority is not None and landed is not None and landed.head
            else None
        )

        return IntegrationPreflight(
            is_git_workspace=True,
            observed_target_head=observed_target,
            fast_forward_possible=ops.is_ancestor(
                ancestor=record.expected_target_head, descendant=record.source_head
            ),
            already_integrated=ops.is_ancestor(
                ancestor=record.source_head, descendant=observed_target
            ),
            # Patch equivalence is a CLAIM requiring explicit evidence, not a measurement.
            # There is no probe that can establish it, so it is not offered here at all
            # rather than defaulted to a value the actuator cannot justify.
            patch_equivalent_evidence=False,
            merge_conflict=False,  # discovered by the apply step itself, never predicted
            source_worktree_dirty=not lane.clean,
            # "Foreign" is answered from the actuator's OWN identity: the lane checkout must
            # be a registered worktree holding this actuator's lane branch.
            worktree_is_foreign=(
                not lane.registered or lane.checked_out_branch != self.lane_branch
            ),
            unpushed_unique_commits=not ops.commit_on_remote(
                record.source_head, branch=self.lane_branch
            ),
            source_head_matches_review=(
                bool(authority.reviewed_head)
                and authority.reviewed_head == record.source_head
            ),
            source_origin_reachable=ops.commit_on_remote(
                record.source_head, branch=self.lane_branch
            ),
            review_generation_admissible=authority.review_generation_admissible,
            target_identity_known=authority.target_identity_known,
            callbacks_drained=authority.callbacks_drained,
            owner_gates_resolved=authority.owner_gates_resolved,
            source_ci=authority.source_ci,
            integration_ci=integration_ci,
            integration_worktree=integration_worktree,
        )

    def _perform_integration_step(
        self,
        decision: IntegrationDecision,
        record: IntegrationActionRecord,
        report: IntegrationRunReport,
        *,
        preflight: IntegrationPreflight,
    ) -> StepOutcome:
        """Perform exactly the step the decision authorized (no substitutions)."""
        step = decision.next_step
        if step == STEP_INTEGRATION_APPLY:
            worktree = preflight.integration_worktree
            # The decision authorizes this step only after `IntegrationWorktree` passed its
            # own admissibility, so an inadmissible one cannot reach here. Re-asserted rather
            # than assumed: this is the last point before a `git switch` on a real checkout,
            # and the cost of the belt is one branch.
            if worktree is None or worktree.admissibility_errors():
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    recorded_by=self.recorder_id,
                    outcome=OUTCOME_BLOCKED,
                    detail=(
                        "a merge-commit disposition requires a verified dedicated "
                        "integration worktree; the lane's own worktree must never check "
                        "out the target branch (j#77124)"
                    ),
                )
            result = self.operations.apply_merge(
                source_head=record.source_head,
                target_ref=record.target_ref,
                integration_worktree=worktree.path,
            )
            if result.conflicted:
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    recorded_by=self.recorder_id,
                    outcome=OUTCOME_BLOCKED,
                    detail=result.detail or "merge conflict; not auto-resolved",
                )
            if not is_full_sha(result.integration_head):
                # A merge that reports success without naming the commit it created has not
                # been shown to have created one. The live adapter already treats this as a
                # failure; the use case does too, so no port implementation can slip a
                # headless "success" into the ledger for the push step to inherit.
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    outcome=OUTCOME_BLOCKED,
                    recorded_by=self.recorder_id,
                    detail=(
                        "the merge reported success but named no commit; refusing to treat "
                        "it as applied"
                    ),
                )
            report.integration_head = result.integration_head
            return StepOutcome(
                action_key=decision.action_key,
                step=step,
                recorded_by=self.recorder_id,
                outcome=OUTCOME_DONE,
                detail=result.detail,
                head=result.integration_head,
            )

        if step == STEP_PUSH:
            # A merge-commit disposition pushes the commit the apply produced; a
            # fast-forward pushes the source head itself.
            # R3 review j#96368 finding 3: R3 removed this fallback from the DECISION and left
            # it here, in the layer that actually pushes — so a merge resume whose apply head
            # was unrecorded pushed the source head and only failed the check afterwards. A
            # merge must push the commit its own apply produced, or push nothing.
            pushed_head = report.integration_head
            if decision.disposition == DISPOSITION_MERGE_COMMIT and not pushed_head:
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    outcome=OUTCOME_BLOCKED,
                    recorded_by=self.recorder_id,
                    detail=(
                        "a merge disposition has no trusted apply head to push; refusing to "
                        "fall back to the source head"
                    ),
                )
            pushed_head = pushed_head or record.source_head
            if decision.disposition != DISPOSITION_MERGE_COMMIT:
                pushed_head = record.source_head
            result = self.operations.push_non_force(
                source_head=pushed_head, target_ref=record.target_ref
            )
            if not result.accepted:
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    recorded_by=self.recorder_id,
                    outcome=OUTCOME_BLOCKED,
                    detail=(
                        result.detail
                        or f"{BLOCKED_PUSH_REJECTED}: the remote moved; re-form the action "
                        "against the new target head (never force, never rebase)"
                    ),
                )
            return StepOutcome(
                action_key=decision.action_key,
                step=step,
                recorded_by=self.recorder_id,
                outcome=OUTCOME_DONE,
                detail=result.detail,
                head=pushed_head,
            )

        # STEP_INTEGRATION_CI — asynchronous; this use case cannot settle it.
        return StepOutcome(
            action_key=decision.action_key,
            step=STEP_INTEGRATION_CI,
            recorded_by=self.recorder_id,
            outcome=OUTCOME_PENDING,
            detail=(
                "CI on the exact integration SHA is an asynchronous gate; re-run once the "
                "run has settled, recording it done and supplying the verdict"
            ),
            head=report.integration_head or record.source_head,
        )

    # -- cleanup ----------------------------------------------------------

    def _measure_cleanup(
        self, record: CleanupActionRecord, *, target_ref: str
    ) -> CleanupPreflight:
        """Build the ENTIRE cleanup preflight from this actuator's own measurements.

        R3 review j#96368 finding 2 is the reason this exists, and it was the heaviest finding
        of the round: every one of the fifteen facts gating the destructive steps was
        caller-supplied, and the reproduction removed a **foreign lane's worktree and deleted
        its branch** on nothing but those booleans. The integration side can at worst integrate
        the wrong thing; this side destroys another lane's work.

        The identity question is answered from the actuator's OWN configuration, not from the
        record: a cleanup may only touch this actuator's lane worktree and lane branch. A CAS
        on the branch tip cannot substitute for it — an unchanged tip says the branch has not
        moved, not that it is ours.
        """
        ops = self.operations
        if not ops.is_git_workspace():
            authority = (
                self.authority.read_cleanup_authority(record=record)
                if self.authority is not None
                else CleanupAuthority()
            )
            return CleanupPreflight(
                is_git_workspace=False,
                authorizing_action_key=record.integration_action_key,
                issue_closed=authority.issue_closed,
                integration_confirmed=authority.integration_confirmed,
                integration_ci_settled_green=authority.integration_ci_settled_green,
                callbacks_drained=authority.callbacks_drained,
                owner_gates_resolved=authority.owner_gates_resolved,
            )

        # Is this lane's own? Measured against the actuator's identity, both ways.
        is_ours = (
            record.worktree_path == self.lane_worktree
            and record.branch == self.lane_branch
        )
        lane = ops.describe_integration_worktree(
            path=record.worktree_path, lane_worktree=self.lane_worktree
        )
        tip = ops.branch_tip(record.branch)
        authority = (
            self.authority.read_cleanup_authority(record=record)
            if self.authority is not None
            else CleanupAuthority()
        )
        return CleanupPreflight(
            is_git_workspace=True,
            authorizing_action_key=record.integration_action_key,
            issue_closed=authority.issue_closed,
            integration_confirmed=authority.integration_confirmed,
            integration_ci_settled_green=authority.integration_ci_settled_green,
            callbacks_drained=authority.callbacks_drained,
            owner_gates_resolved=authority.owner_gates_resolved,
            worktree_is_foreign=not is_ours,
            worktree_clean=lane.clean,
            worktree_path_registered=lane.registered,
            branch_checked_out_elsewhere=ops.branch_checked_out_elsewhere(record.branch),
            unpushed_unique_commits=not ops.commit_on_remote(tip, branch=record.branch),
            branch_reachable_from_target=ops.is_ancestor(
                ancestor=tip, descendant=ops.resolve_head(target_ref)
            ),
            # Patch equivalence needs explicit evidence; no probe establishes it.
            branch_patch_equivalent=False,
            branch_tip=tip,
        )

    def run_cleanup(
        self,
        record: CleanupActionRecord,
        *,
        target_ref: str,
        ledger: Sequence[StepOutcome] = (),
    ) -> CleanupRunReport:
        """Drive the post-close cleanup machine until it rests, performing each decided step.

        As with :meth:`run_integration` there is no caller preflight: ``target_ref`` is the
        integration branch the lane's work must be reachable from, and everything else is
        measured (:meth:`_measure_cleanup`).
        """
        report = CleanupRunReport()
        working_ledger: List[StepOutcome] = list(ledger)
        preflight = self._measure_cleanup(record, target_ref=target_ref)
        report.measured_preflight = preflight

        while True:
            decision = decide_cleanup(
                self.cleanup_policy,
                record,
                preflight,
                ledger=working_ledger,
                trusted_recorder=self.recorder_id,
            )
            report.states.append(decision.state)
            report.final_decision = decision
            if decision.next_step is None:
                return report

            outcome = self._perform_cleanup_step(decision, record)
            report.outcomes.append(outcome)
            working_ledger.append(outcome)
            if outcome.recorded_by != self.recorder_id:
                # Unreachable by construction, and asserted rather than assumed: an outcome
                # the next decision will not count would make this loop spin forever.
                raise AssertionError(
                    "a recorded cleanup outcome lost this actuator's provenance"
                )
            if outcome.outcome != OUTCOME_DONE:
                report.final_decision = decide_cleanup(
                    self.cleanup_policy,
                    record,
                    preflight,
                    ledger=working_ledger,
                    trusted_recorder=self.recorder_id,
                )
                report.states.append(report.final_decision.state)
                return report

    def _perform_cleanup_step(
        self, decision: CleanupDecision, record: CleanupActionRecord
    ) -> StepOutcome:
        """Perform exactly the cleanup step the decision authorized (no substitutions)."""
        step = decision.next_step
        if step == STEP_PROCESS_RETIRE:
            if self.processes is None:
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    recorded_by=self.recorder_id,
                    outcome=OUTCOME_BLOCKED,
                    detail="no managed-process port injected; the process was not released",
                )
            released = self.processes.release_process(
                issue=record.issue, lane_generation=record.lane_generation
            )
            return _settled(
                decision.action_key, step, released, "process release",
                recorded_by=self.recorder_id,
            )

        if step == STEP_WORKTREE_REMOVE:
            removed = self.operations.remove_worktree(
                worktree_path=record.worktree_path
            )
            return _settled(
                decision.action_key,
                step,
                removed,
                "worktree removal (no --force)",
                recorded_by=self.recorder_id,
            )

        deleted = self.operations.delete_local_branch(
            branch=record.branch, expected_tip=record.recorded_source_head
        )
        return _settled(
            decision.action_key,
            STEP_LOCAL_BRANCH_DELETE,
            deleted,
            "compare-and-swap local branch delete (never `git branch -D`)",
            recorded_by=self.recorder_id,
        )


def _settled(
    action_key: str, step: str, succeeded: bool, what: str, *, recorded_by: str
) -> StepOutcome:
    """A step outcome for an operation that either happened or did not.

    A failed operation is ``blocked``, never retried with a stronger form: the whole point of
    the port's weak operations is that there is no stronger form to fall back to.

    ``recorded_by`` is required rather than defaulted: an outcome without the actuator's
    provenance is not counted by the next decision, so a missing stamp would silently turn a
    completed step into one the run repeats forever.
    """
    return StepOutcome(
        action_key=action_key,
        step=step,
        outcome=OUTCOME_DONE if succeeded else OUTCOME_BLOCKED,
        detail=f"{what} {'succeeded' if succeeded else 'failed; nothing forced'}",
        recorded_by=recorded_by,
    )


__all__: Tuple[str, ...] = (
    "integration_policy_from_config",
    "cleanup_policy_from_config",
    "PushResult",
    "MergeResult",
    "AutoIntegrationGitOperations",
    "ManagedProcessOperations",
    "DurableAuthorityReader",
    "IntegrationAuthority",
    "CleanupAuthority",
    "IntegrationRunReport",
    "CleanupRunReport",
    "AutoIntegrationUseCase",
)
