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

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    BLOCKED_PUSH_REJECTED,
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
    StepOutcome,
    decide_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    STEP_LOCAL_BRANCH_DELETE,
    STEP_PROCESS_RETIRE,
    STEP_REMOTE_BRANCH_DELETE,
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
        require_source_ci=config.require_source_ci,
        require_integration_ci=config.require_integration_ci,
    )


def cleanup_policy_from_config(
    config: AutoIntegrationConfig,
) -> RetirementCleanupPolicy:
    """Translate the ``auto_integration`` config block into the cleanup policy."""
    return RetirementCleanupPolicy(
        remove_worktree=config.remove_worktree,
        delete_local_branch=config.delete_local_branch,
        delete_remote_branch=config.delete_remote_branch,
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
    and three on the cleanup side, and every one of them is the *weak* form: there is no
    force push, no rebase, no ``--force`` worktree removal, and no unconditional ref delete
    anywhere in this interface. An operation this port cannot express is one the actuator
    cannot perform, which is the point.
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

    def remove_worktree(self, *, worktree_path: str) -> bool:
        """Remove the worktree at ``worktree_path`` without ``--force``."""
        ...

    def delete_local_branch(self, *, branch: str, expected_tip: str) -> bool:
        """Compare-and-swap delete: remove ``branch`` only while it points at ``expected_tip``."""
        ...

    def delete_remote_branch(self, *, branch: str) -> bool:
        """Delete ``branch`` on the remote. Only reached when explicitly enabled."""
        ...


@runtime_checkable
class ManagedProcessOperations(Protocol):
    """Releasing the lane's managed pane / process — the one Git-independent cleanup step."""

    def release_process(self, *, issue: str, lane_generation: int) -> bool: ...


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

    def as_payload(self) -> dict[str, object]:
        return {
            "states": list(self.states),
            "outcomes": [outcome.as_payload() for outcome in self.outcomes],
            "integration_head": self.integration_head,
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

    ``integration_worktree`` is the dedicated checkout a merge-commit disposition is applied
    in. It is required only for that disposition; a fast-forward never needs one, so the
    fast-forward default path works with it unset.
    """

    operations: AutoIntegrationGitOperations
    integration_policy: AutoIntegrationPolicy
    cleanup_policy: RetirementCleanupPolicy = field(
        default_factory=RetirementCleanupPolicy.default
    )
    processes: Optional[ManagedProcessOperations] = None
    integration_worktree: str = ""

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

            outcome = self._perform_integration_step(decision, record, report)
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

    def _perform_integration_step(
        self,
        decision: IntegrationDecision,
        record: IntegrationActionRecord,
        report: IntegrationRunReport,
    ) -> StepOutcome:
        """Perform exactly the step the decision authorized (no substitutions)."""
        step = decision.next_step
        if step == STEP_INTEGRATION_APPLY:
            if not self.integration_worktree:
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    outcome=OUTCOME_BLOCKED,
                    detail=(
                        "a merge-commit disposition requires a dedicated integration "
                        "worktree; the lane's own worktree must not check out the target"
                    ),
                )
            result = self.operations.apply_merge(
                source_head=record.source_head,
                target_ref=record.target_ref,
                integration_worktree=self.integration_worktree,
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

        if step == STEP_LOCAL_BRANCH_DELETE:
            deleted = self.operations.delete_local_branch(
                branch=record.branch, expected_tip=record.recorded_source_head
            )
            return _settled(
                decision.action_key,
                step,
                deleted,
                "compare-and-swap local branch delete (never `git branch -D`)",
            )

        deleted = self.operations.delete_remote_branch(branch=record.branch)
        return _settled(
            decision.action_key,
            STEP_REMOTE_BRANCH_DELETE,
            deleted,
            "remote branch delete (explicitly enabled by config)",
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
    "IntegrationRunReport",
    "CleanupRunReport",
    "AutoIntegrationUseCase",
)
