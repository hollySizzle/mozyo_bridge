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
  substitutes a stronger one: no ``--force``, no rebase, no ref delete of any kind, no remote
  ref rewrite. Each executed step is appended to the ledger the next decision reads, which is
  what makes a partial failure resumable without a duplicate merge or push.
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
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ports import (
    AutoIntegrationGitOperations,
    CleanupAuthority,
    DurableAuthorityReader,
    InMemoryLedgerStore,
    IntegrationAuthority,
    LedgerStore,
    ManagedProcessOperations,
    MergeResult,
    PushResult,
)
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
    return RetirementCleanupPolicy(remove_worktree=config.remove_worktree)


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

    **The actuator measures its own safety facts, and owns its own ledger.** There is no
    caller preflight and no caller ledger: a caller supplies an action record — identity —
    and nothing else. Every fact that gates a mutation is read at action time from a port
    (git probes, the durable authority reader) or from this actuator's own configuration,
    and every completed step is read back from a :class:`LedgerStore` this instance owns.

    Four review rounds arrived at that. j#96344 said a boolean cannot be audited; j#96350
    said typing it changes nothing while the caller still supplies the value; j#96368 said
    measuring two facts does not move the authority while the rest are supplied; j#96379 said
    the same of the ledger, and that a provenance derived from public constructor values is
    reproducible by the caller. The receipt is now an unguessable per-instance token.

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
    ledger: LedgerStore = field(default_factory=InMemoryLedgerStore)
    lane_worktree: str = ""
    lane_branch: str = ""
    integration_worktree_path: str = ""

    #: The writer receipt this actuator stamps on the steps it records. R4 derived it from
    #: public constructor values, so a caller could reproduce it; R5 made it an unguessable
    #: per-instance token but left the dataclass mutable, so it could simply be reassigned
    #: (R5 review j#96385 finding 1). The class is frozen again and the field is ``init=False``,
    #: so it is neither supplied nor rewritable through the public surface.
    #:
    #: This is a boundary WITHIN one process, not an authority boundary across processes: a
    #: durable store with an authenticated writer identity is the real answer, and it belongs
    #: with the production data plane the durable authority reader needs.
    _receipt: str = field(default_factory=lambda: f"receipt:{uuid.uuid4().hex}", init=False)

    @property
    def recorder_id(self) -> str:
        """This actuator's ledger provenance — what it stamps on the steps it records."""
        return self._receipt

    # -- integration ------------------------------------------------------

    def run_integration(
        self, record: IntegrationActionRecord
    ) -> IntegrationRunReport:
        """Drive the integration machine until it rests, performing each decided step.

        The loop is decision-driven: it asks for the next step, performs exactly that step,
        appends the outcome, and asks again. It stops as soon as the decision is terminal, as
        soon as a step is refused, or as soon as it reaches the asynchronous CI gate — which
        it records ``pending`` rather than waiting on, because it cannot make a run finish.

        There is no ``preflight`` parameter: the actuator measures the world itself
        (:meth:`_measure`). A caller cannot hand this method a fact, only an action record —
        which is identity, not evidence. The measurement is re-taken before **every** step,
        because this actuator's own mutations change the world it is deciding about.
        """
        report = IntegrationRunReport()
        # The ledger comes from this actuator's own store, never from an argument.
        working_ledger: List[StepOutcome] = list(
            self.ledger.read(action_key=record.action_key)
        )
        # A resumed run has no in-memory memory of the commit a previous run's apply
        # produced, but the ledger recorded it. Without this the push after a resumed apply
        # would push the SOURCE head instead of the merge commit — silently integrating a
        # different commit than the one the apply created.
        # R4 review j#96379 finding 1: this restoration ignored `recorded_by`, so a foreign
        # apply entry's head became the commit the push would use — the decision's provenance
        # fence did not protect the MUTATION INPUT. Both now read the same validated view.
        trusted = completed_steps(
            working_ledger, action_key=record.action_key, recorded_by=self.recorder_id
        )
        applied = trusted.get(STEP_INTEGRATION_APPLY)
        if applied is not None and is_full_sha(applied.head):
            report.integration_head = applied.head

        while True:
            # R5 review j#96385 findings 2 and 3: a snapshot taken once, before the first
            # mutation, is stale for every mutation after it — and this actuator acts on a
            # world its own mutations change. Re-measuring here re-verifies the target
            # immediately before the push AND immediately after it, which is what lets the
            # post-push gate ask "is what we landed still there" instead of comparing the
            # target to an expectation our own push has already invalidated.
            preflight = self._measure(record, report, ledger=working_ledger)
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
            self.ledger.append(outcome)
            working_ledger.append(outcome)
            if outcome.outcome != OUTCOME_DONE:
                # A refused or unsettled step stops the run here. Re-deciding would only
                # produce the same step again, and looping on it would turn a fail-closed
                # refusal into a spin.
                report.final_decision = decide_integration(
                    self.integration_policy,
                    record,
                    self._measure(record, report, ledger=working_ledger),
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

        # The target's head is the REMOTE's, read fresh. A local ref is this clone's stale
        # opinion of a branch other clones also push to (j#96379 finding 4).
        observed_target = ops.remote_branch_tip(record.target_ref)
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
        trusted_view = completed_steps(
            ledger, action_key=record.action_key, recorded_by=self.recorder_id
        )
        landed_push = trusted_view.get(STEP_PUSH)
        integration_ci = (
            self.authority.read_integration_ci(
                record=record, integration_head=landed_push.head
            )
            if self.authority is not None
            and landed_push is not None
            and landed_push.head
            else None
        )

        landed = trusted_view.get(STEP_PUSH)
        return IntegrationPreflight(
            is_git_workspace=True,
            observed_target_head=observed_target,
            # Post-push: is what we landed still on the target? Measured against the same
            # fresh remote tip, so a force push or reset after ours is visible.
            landed_head_on_target=(
                landed is not None
                and is_full_sha(landed.head)
                and ops.is_ancestor(ancestor=landed.head, descendant=observed_target)
            ),
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
                # The parent the merge must sit on: the freshly measured remote target, not
                # whatever the dedicated worktree happens to have checked out.
                expected_target_head=preflight.observed_target_head,
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
        self, record: CleanupActionRecord, *, ledger: Sequence[StepOutcome]
    ) -> CleanupPreflight:
        """Build the ENTIRE cleanup preflight from this actuator's own measurements.

        R3 review j#96368 finding 2 is the reason this exists, and it was the heaviest finding
        of the round: every one of the fifteen facts gating the destructive steps was
        caller-supplied, and the reproduction removed a **foreign lane's worktree and deleted
        its branch** on nothing but those booleans. The integration side can at worst integrate
        the wrong thing; this side destroys another lane's work.

        The identity question is answered from the actuator's OWN configuration, not from the
        record: a cleanup may only touch this actuator's lane worktree.

        Five branch-shaped measurements used to be taken here for the local branch delete
        (its tip, whether anything held it, whether it was reachable from the configured
        target, whether it had unique unpushed commits). They are gone with the step
        (j#96396 finding 1) rather than left measured-but-unread — a probe whose answer no
        gate consults is how R4 shipped a `checked_out_branch` that was measured and never
        compared.
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

        lane = ops.describe_integration_worktree(
            path=record.worktree_path, lane_worktree=self.lane_worktree
        )
        # Ownership is PHASE-AWARE (R6 review j#96391 finding 2). Before the removal it is
        # three facts: the record names this actuator's lane, the checkout is that worktree,
        # and it currently holds this actuator's branch — R4 gated on the string pair alone
        # and measured `checked_out_branch` without using it, so a registered worktree holding
        # a foreign branch was removed.
        #
        # After OUR OWN removal those facts cannot be re-measured: the path is gone, so the
        # branch probe answers empty and the identity test would fail forever, blocking the
        # branch cleanup it is supposed to precede. R6 shipped exactly that, and the fake hid
        # it by clearing `registered` while leaving `checked_out_branch` set. Once a trusted
        # remove receipt exists the question changes to "did we own it when we removed it, and
        # is the path now actually gone" — the first was established at that time and recorded,
        # the second is measurable.
        removed = completed_steps(
            ledger, action_key=record.action_key, recorded_by=self.recorder_id
        ).get(STEP_WORKTREE_REMOVE)
        names_our_lane = (
            record.worktree_path == self.lane_worktree
            and record.branch == self.lane_branch
        )
        if removed is None:
            is_ours = (
                names_our_lane
                and lane.is_lane_worktree
                and lane.checked_out_branch == self.lane_branch
            )
        else:
            is_ours = names_our_lane and not lane.registered
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
            # Once removed, the removal itself is the evidence these held: the step only ran
            # because they did, and a vanished path cannot answer them again.
            worktree_clean=lane.clean if removed is None else True,
            worktree_path_registered=lane.registered if removed is None else True,
        )

    def run_cleanup(self, record: CleanupActionRecord) -> CleanupRunReport:
        """Drive the post-close cleanup machine until it rests, performing each decided step.

        As with :meth:`run_integration` there is no caller preflight and no caller ledger:
        everything the machine decides from is measured here (:meth:`_measure_cleanup`).
        """
        report = CleanupRunReport()
        working_ledger: List[StepOutcome] = list(
            self.ledger.read(action_key=record.action_key)
        )
        while True:
            # Re-measured before every step rather than once per run: `remove_worktree`
            # changes what the next measurement of this lane can see, and a preflight taken
            # before a step is not a description of the world after it.
            preflight = self._measure_cleanup(record, ledger=working_ledger)
            report.measured_preflight = preflight
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
            self.ledger.append(outcome)
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
                    self._measure_cleanup(record, ledger=working_ledger),
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

        # No step falls through to a destructive default. Until R7 the local branch delete sat
        # here as the unnamed tail of this dispatch — a shape that runs *something* for any
        # step the decision names — and review j#96396 finding 2 caught the record that came
        # out of it claiming a compare-and-swap while the argv was `git branch -D`. There is
        # no ref delete to reach now, and an unrecognized step is refused rather than mapped
        # onto whatever operation happens to be last.
        return StepOutcome(
            action_key=decision.action_key,
            step=str(step or "unknown"),
            recorded_by=self.recorder_id,
            outcome=OUTCOME_BLOCKED,
            detail=(
                f"cleanup step {step!r} is not one this actuator performs; nothing was run"
            ),
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
    "LedgerStore",
    "InMemoryLedgerStore",
    "IntegrationAuthority",
    "CleanupAuthority",
    "IntegrationRunReport",
    "CleanupRunReport",
    "AutoIntegrationUseCase",
)
