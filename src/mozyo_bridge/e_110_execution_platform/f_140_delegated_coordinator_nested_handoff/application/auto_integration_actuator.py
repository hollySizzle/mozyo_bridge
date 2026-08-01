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
    MODE_COORDINATOR_CONFIRMED,
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
    decide_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    CoordinatorConfirmation,
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


@runtime_checkable
class CoordinatorConfirmationResolver(Protocol):
    """Resolves a coordinator confirmation from the durable record it is recorded at.

    R2 accepted a :class:`CoordinatorConfirmation` straight from the caller, so a forged one
    naming a nonexistent anchor authorized an actuation (j#96350 finding 4): typing a
    self-assertion does not stop it being a self-assertion. The caller now supplies only an
    *anchor* — where to look — and this port does the looking.

    An implementation MUST: fresh-read the anchor from the source of truth; confirm the
    record there confirms **this exact action key**; and derive ``issuer_role`` from the
    record's own author rather than from anything the caller said. It returns ``None`` for
    every failure — unreadable anchor, absent record, wrong action, non-coordinator author —
    because "we could not establish a confirmation" and "there is no confirmation" lead to
    the same fail-closed place.
    """

    def resolve(
        self, *, anchor: str, action_key: str
    ) -> Optional[CoordinatorConfirmation]: ...


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
    #: The confirmation the resolver returned for this action (``None`` when none resolved).
    resolved_confirmation: Optional[CoordinatorConfirmation] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "states": list(self.states),
            "outcomes": [outcome.as_payload() for outcome in self.outcomes],
            "integration_head": self.integration_head,
            "measured_worktree": (
                self.measured_worktree.as_payload() if self.measured_worktree else None
            ),
            "resolved_confirmation": (
                self.resolved_confirmation.as_payload()
                if self.resolved_confirmation
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

    def as_payload(self) -> dict[str, object]:
        return {
            "states": list(self.states),
            "outcomes": [outcome.as_payload() for outcome in self.outcomes],
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
    confirmations: Optional[CoordinatorConfirmationResolver] = None
    lane_worktree: str = ""
    integration_worktree_path: str = ""

    # -- integration ------------------------------------------------------

    def run_integration(
        self,
        record: IntegrationActionRecord,
        preflight: IntegrationPreflight,
        *,
        ledger: Sequence[StepOutcome] = (),
    ) -> IntegrationRunReport:
        """Drive the integration machine until it rests, performing each decided step.

        The loop is decision-driven: it asks for the next step, performs exactly that step,
        appends the outcome, and asks again. It stops as soon as the decision is terminal, as
        soon as a step is refused, or as soon as it reaches the asynchronous CI gate — which
        it records ``pending`` rather than waiting on, because it cannot make a run finish.

        The preflight is **not** re-derived between steps. It is a snapshot of the world at
        action time, and the action key binds the run to it; a world that has changed
        produces a different key on the next call, which is where the re-validation belongs.
        """
        report = IntegrationRunReport()
        working_ledger: List[StepOutcome] = list(ledger)
        preflight = self._measure(record, preflight, report)
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
                self.integration_policy, record, preflight, ledger=working_ledger
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
                    self.integration_policy, record, preflight, ledger=working_ledger
                )
                report.states.append(report.final_decision.state)
                return report

    def _measure(
        self,
        record: IntegrationActionRecord,
        preflight: IntegrationPreflight,
        report: IntegrationRunReport,
    ) -> IntegrationPreflight:
        """Replace the caller-supplied safety facts with the actuator's own measurements.

        The two fields overwritten here are the ones R2 review j#96350 found a caller could
        forge. Whatever the caller put in them is discarded — not merged, not preferred when
        "more specific", discarded — because a value the caller chose cannot be evidence
        about the caller's own request.

        The measurement runs unconditionally rather than only for the disposition that needs
        it: deciding *whether* to measure from the same preflight the measurement is meant to
        replace would make the skip forgeable too.
        """
        worktree = self.operations.describe_integration_worktree(
            path=self.integration_worktree_path,
            lane_worktree=self.lane_worktree,
        )
        report.measured_worktree = worktree

        confirmation: Optional[CoordinatorConfirmation] = None
        if self.integration_policy.mode == MODE_COORDINATOR_CONFIRMED:
            if self.confirmations is not None:
                confirmation = self.confirmations.resolve(
                    anchor=preflight.coordinator_confirmation_anchor,
                    action_key=record.action_key,
                )
            # No resolver injected: nothing can be resolved, so nothing is confirmed. The
            # decision then reports `coordinator_confirmation_required` and stops — the
            # fail-closed reading, not an implicit approval.
        report.resolved_confirmation = confirmation

        return dataclasses.replace(
            preflight,
            integration_worktree=worktree,
            coordinator_confirmation=confirmation,
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
                    outcome=OUTCOME_BLOCKED,
                    detail=result.detail or "merge conflict; not auto-resolved",
                )
            report.integration_head = result.integration_head
            return StepOutcome(
                action_key=decision.action_key,
                step=step,
                outcome=OUTCOME_DONE,
                detail=result.detail,
                head=result.integration_head,
            )

        if step == STEP_PUSH:
            # A merge-commit disposition pushes the commit the apply produced; a
            # fast-forward pushes the source head itself.
            pushed_head = report.integration_head or record.source_head
            result = self.operations.push_non_force(
                source_head=pushed_head, target_ref=record.target_ref
            )
            if not result.accepted:
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
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
                outcome=OUTCOME_DONE,
                detail=result.detail,
                head=pushed_head,
            )

        # STEP_INTEGRATION_CI — asynchronous; this use case cannot settle it.
        return StepOutcome(
            action_key=decision.action_key,
            step=STEP_INTEGRATION_CI,
            outcome=OUTCOME_PENDING,
            detail=(
                "CI on the exact integration SHA is an asynchronous gate; re-run once the "
                "run has settled, recording it done and supplying the verdict"
            ),
            head=report.integration_head or record.source_head,
        )

    # -- cleanup ----------------------------------------------------------

    def run_cleanup(
        self,
        record: CleanupActionRecord,
        preflight: CleanupPreflight,
        *,
        ledger: Sequence[StepOutcome] = (),
    ) -> CleanupRunReport:
        """Drive the post-close cleanup machine until it rests, performing each decided step."""
        report = CleanupRunReport()
        working_ledger: List[StepOutcome] = list(ledger)

        while True:
            decision = decide_cleanup(
                self.cleanup_policy, record, preflight, ledger=working_ledger
            )
            report.states.append(decision.state)
            report.final_decision = decision
            if decision.next_step is None:
                return report

            outcome = self._perform_cleanup_step(decision, record)
            report.outcomes.append(outcome)
            working_ledger.append(outcome)
            if outcome.outcome != OUTCOME_DONE:
                report.final_decision = decide_cleanup(
                    self.cleanup_policy, record, preflight, ledger=working_ledger
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
                    outcome=OUTCOME_BLOCKED,
                    detail="no managed-process port injected; the process was not released",
                )
            released = self.processes.release_process(
                issue=record.issue, lane_generation=record.lane_generation
            )
            return _settled(decision.action_key, step, released, "process release")

        if step == STEP_WORKTREE_REMOVE:
            removed = self.operations.remove_worktree(
                worktree_path=record.worktree_path
            )
            return _settled(
                decision.action_key, step, removed, "worktree removal (no --force)"
            )

        deleted = self.operations.delete_local_branch(
            branch=record.branch, expected_tip=record.recorded_source_head
        )
        return _settled(
            decision.action_key,
            STEP_LOCAL_BRANCH_DELETE,
            deleted,
            "compare-and-swap local branch delete (never `git branch -D`)",
        )


def _settled(action_key: str, step: str, succeeded: bool, what: str) -> StepOutcome:
    """A step outcome for an operation that either happened or did not.

    A failed operation is ``blocked``, never retried with a stronger form: the whole point of
    the port's weak operations is that there is no stronger form to fall back to.
    """
    return StepOutcome(
        action_key=action_key,
        step=step,
        outcome=OUTCOME_DONE if succeeded else OUTCOME_BLOCKED,
        detail=f"{what} {'succeeded' if succeeded else 'failed; nothing forced'}",
    )


__all__: Tuple[str, ...] = (
    "integration_policy_from_config",
    "cleanup_policy_from_config",
    "PushResult",
    "MergeResult",
    "AutoIntegrationGitOperations",
    "ManagedProcessOperations",
    "CoordinatorConfirmationResolver",
    "IntegrationRunReport",
    "CleanupRunReport",
    "AutoIntegrationUseCase",
)
