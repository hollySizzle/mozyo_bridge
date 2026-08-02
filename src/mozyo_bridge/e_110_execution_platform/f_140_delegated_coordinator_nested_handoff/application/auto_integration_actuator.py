"""Guarded auto-integration / retirement-cleanup actuator composition (Redmine #13686).

Composes the two pure #13686 state machines
(:mod:`...domain.auto_integration_policy`, :mod:`...domain.retirement_cleanup_policy`) with
an **injected** Git operations port, mirroring the established #12604 / #12557 executor
pattern: the decision is the authority and the use case never re-decides. All real ``git``
side effects sit behind a Protocol, so the classical tests drive fakes and the live
subprocess adapter (:mod:`...application.auto_integration_live_ops`) is one swappable
implementation rather than a hard dependency of the decision path.

Three parts:

- :func:`integration_policy_from_config` translates the governance config block
  (:class:`~mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records.AutoIntegrationConfig`)
  into the integration policy. The application layer owns the translation so the domain never
  imports the governance schema. There is no cleanup counterpart: the cleanup machine has no
  configurable step left (its Git steps were withdrawn), so a config→policy translation there
  would produce a record nothing reads.
- :class:`AutoIntegrationUseCase` executes **one decided step at a time** and returns the
  step's outcome. It performs only the side effect the decision authorized and never
  substitutes a stronger one: no ``--force``, no rebase, no ref delete of any kind, no remote
  ref rewrite, and no worktree removal. Each executed step is appended to the ledger the next
  decision reads, which is what makes a partial failure resumable without a duplicate merge
  or push.
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
from typing import Callable, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    AutoIntegrationAdmissionError,
    AutoIntegrationLedgerError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_admission_pin import (  # noqa: E501
    AdmittedActionPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ports import (
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
    LaneWorktree,
    StepOutcome,
    checked_merge_status,
    checked_push_status,
    completed_steps,
    is_full_sha,
    decide_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    STEP_PROCESS_RETIRE,
    CleanupActionRecord,
    CleanupDecision,
    CleanupPreflight,
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
    #: What the actuator MEASURED about the LANE's checkout, as opposed to what the caller
    #: claimed. Recorded so the durable record shows the measurement. There is no dedicated
    #: integration worktree to record any more: the merge is built from objects
    #: (j#96406 finding 1).
    measured_worktree: Optional[LaneWorktree] = None
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

# There is no "which steps need an admission" table any more. R1 had one, listing the two
# mutating integration steps and the one cleanup step, so the CI step could be recorded without
# one. Review j#96611 finding 3 made the receipt the ONLY way to record an outcome at all, and a
# table of exemptions from that is a table of ways to write a row nobody was admitted to write.
# Every step claims its admission; the ones that mutate are simply the ones where losing the
# receipt matters.


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

    ``lane_issue`` / ``lane_generation`` / ``lane_worktree`` / ``lane_branch`` are this
    actuator's own identity — constructor state rather than per-call arguments, so a single
    instance cannot be redirected mid-run. The first two exist because review j#96406
    finding 2 found the gap: the cleanup verified the record's *branch and path* while the
    operation it authorized acts on the record's *issue and lane generation*. Verifying one
    pair and mutating on another is not a check at all, so every value the mutation consumes
    is now compared against this actuator's own.

    There is no ``integration_worktree_path``. The merge is assembled from objects, so there
    is no checkout for the actuator to be pointed at (j#96406 finding 1).

    There is no ``cleanup_policy``: with every Git step withdrawn from the cleanup machine
    (review j#96401 finding 1 took the last one), nothing there is configurable, and the
    ``operations`` port is not consulted by :meth:`run_cleanup` at all.
    """

    operations: AutoIntegrationGitOperations
    #: The policy this actuator was CONSTRUCTED with. Used directly only when no
    #: :attr:`policy_source` is bound — a directly-constructed use case (the classical tests)
    #: has no repository to re-read.
    integration_policy: AutoIntegrationPolicy
    processes: Optional[ManagedProcessOperations] = None
    authority: Optional[DurableAuthorityReader] = None
    ledger: LedgerStore = field(default_factory=InMemoryLedgerStore)
    lane_worktree: str = ""
    lane_branch: str = ""
    #: The issue and lane generation this actuator is bound to. Compared against the values a
    #: cleanup record supplies, because those are the values ``release_process`` acts on.
    lane_issue: str = ""
    lane_generation: Optional[int] = None
    #: Re-reads the policy from the repository before EVERY decision (review j#96650 finding 4).
    #: R2 fixed two action-time reads and left this one a construction-time snapshot, so a
    #: config tightened from ``auto`` to ``disabled`` — or ``ff_only`` from False to True —
    #: between construction and the mutation was never observed. ``mode`` and ``ff_only`` are
    #: exactly as much a gate as the target branch is, and a gate read once is a gate read at
    #: the wrong time. ``None`` keeps :attr:`integration_policy`.
    policy_source: Optional[Callable[[], AutoIntegrationPolicy]] = None
    #: Production-only mutation capability.  ``ledger`` is deliberately the read surface;
    #: review j#96650 demonstrated that handing arbitrary callers the begin+append pair lets
    #: them manufacture an accepted push.  Direct/in-memory tests leave this ``None`` and their
    #: process-local ledger remains combined.
    _ledger_writer: object = field(default=None, repr=False, compare=False)
    #: Production-only exact resume-frame pin. Direct/in-memory tests keep ``None``.
    _admission_pin: Optional[AdmittedActionPin] = field(
        default=None, repr=False, compare=False
    )

    #: The writer receipt this actuator stamps on the steps it records. R4 derived it from
    #: public constructor values, so a caller could reproduce it; R5 made it an unguessable
    #: per-instance token but left the dataclass mutable, so it could simply be reassigned
    #: (R5 review j#96385 finding 1). The class is frozen again and the field is ``init=False``,
    #: so it is neither supplied nor rewritable through the public surface.
    #:
    #: This is a boundary WITHIN one process, not an authority boundary across processes. The
    #: production store supplies durable provenance and admission CAS, but the trusted principal
    #: is the OS account that also owns the repository and mutation credentials; a private Python
    #: surface cannot authenticate mutually hostile same-UID code.
    _receipt: str = field(default_factory=lambda: f"receipt:{uuid.uuid4().hex}", init=False)

    def _policy(self) -> AutoIntegrationPolicy:
        """The policy in force RIGHT NOW, re-read when this actuator has a source for it."""
        if self.policy_source is None:
            return self.integration_policy
        return self.policy_source()

    def _mutation_ledger(self) -> object:
        return self._ledger_writer if self._ledger_writer is not None else self.ledger

    @property
    def recorder_id(self) -> str:
        """This actuator's ledger provenance — what it stamps on the steps it records.

        A store that owns a writer identity supplies it, and this actuator adopts it. That is
        what makes a resume across processes possible at all: the per-instance receipt below is
        by construction unrecognisable to the next process, so with it as the provenance a
        continuation would count nothing the earlier run recorded and would offer the push again.
        The receipt remains the answer for a store with no identity of its own (the in-memory
        default), where a process-local boundary is the honest one — that store's entries do not
        outlive the process either (Redmine #14825 item 4).
        """
        declared = str(
            getattr(self._mutation_ledger(), "writer_id", "") or ""
        ).strip()
        return declared or self._receipt

    def _unresolved_intent(self, action_key: str) -> Optional[str]:
        """The step whose side effect may have run and whose outcome never arrived, if any.

        A store that records no intents answers ``None``, which preserves #13686's behaviour
        exactly. A durable store that does answers with the open step's name, and the run stops:
        a crash between a ``git push`` and its receipt leaves the ledger with no push entry, and
        a resume that reads only outcomes cannot tell that from "the push has not been offered
        yet" — so it offers it again. The intent is the record of the unknown, and an unknown
        must not resolve to the convenient reading (Redmine #14825 item 4).
        """
        reader = getattr(self.ledger, "unresolved_intents", None)
        if reader is None:
            return None
        open_intents = reader(action_key=action_key)
        return str(open_intents[0].step) if open_intents else None

    def _begin_step(self, action_key: str, step: str) -> "Tuple[str, Optional[str]]":
        """Claim the admission to run ``step``, returning ``(receipt, refusal)``.

        A store with no admission concept answers ``("", None)`` — that is #13686's in-memory
        default, whose entries do not outlive the process anyway. A durable store either mints
        the receipt this run must present with its outcome, or REFUSES because another run
        already holds the admission (Redmine #14825 review j#96611 finding 4: R1 read the open
        intents and then inserted one, so two runs were both admitted to the same push). A
        refusal is not retried here: it means somebody else is mutating, or crashed mid-mutation,
        and either way a second mutation is what must not happen.
        """
        if self._admission_pin is not None:
            self._admission_pin.require_mutation_key(action_key)
        opener = getattr(self._mutation_ledger(), "begin_step", None)
        if opener is None:
            return "", None
        try:
            return str(getattr(opener(action_key=action_key, step=step), "receipt", "")), None
        except AutoIntegrationAdmissionError as refusal:
            return "", str(refusal)

    def _record(self, outcome: StepOutcome, receipt: str) -> None:
        """Append ``outcome`` under the admission that produced it."""
        if self._admission_pin is not None:
            self._admission_pin.require_mutation_key(outcome.action_key)
        writer = self._mutation_ledger()
        append = getattr(writer, "append", None)
        if append is None:
            raise AutoIntegrationLedgerError(
                "the actuator has no ledger mutation capability; no outcome was recorded"
            )
        append(outcome, receipt=receipt)

    def _resolve_intent(self, **kwargs) -> None:
        """Recovery-only write through the same private mutation capability."""
        if self._admission_pin is not None:
            self._admission_pin.require_mutation_key(str(kwargs.get("action_key", "")))
        resolver = getattr(self._mutation_ledger(), "resolve_intent", None)
        if resolver is None:
            raise AutoIntegrationLedgerError(
                "the actuator has no reconciliation mutation capability"
            )
        resolver(**kwargs)

    def register_durable_action(self, action: object) -> None:
        """Register an immutable resume frame before the first production mutation."""
        if self._admission_pin is not None:
            self._admission_pin.require_frame(action)
        register = getattr(self._mutation_ledger(), "register_action", None)
        if register is None:
            raise AutoIntegrationLedgerError(
                "the actuator has no durable action registry"
            )
        register(action)

    def mark_action_awaiting_ci(
        self, *, action_key: str, landed_head: str, ci_workflow: str
    ) -> None:
        if self._admission_pin is not None:
            self._admission_pin.require_integration_key(action_key)
        marker = getattr(self._mutation_ledger(), "mark_action_awaiting_ci", None)
        if marker is None:
            raise AutoIntegrationLedgerError(
                "the actuator has no durable continuation registry"
            )
        marker(
            action_key=action_key,
            landed_head=landed_head,
            ci_workflow=ci_workflow,
        )

    def mark_action_terminal(
        self, *, action_key: str, state: str, landed_head: str, detail: str = ""
    ) -> None:
        if self._admission_pin is not None:
            self._admission_pin.require_integration_key(action_key)
        marker = getattr(self._mutation_ledger(), "mark_action_terminal", None)
        if marker is None:
            raise AutoIntegrationLedgerError(
                "the actuator has no durable continuation registry"
            )
        marker(
            action_key=action_key,
            state=state,
            landed_head=landed_head,
            detail=detail,
        )

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
        if self._admission_pin is not None:
            self._admission_pin.require_integration_key(record.action_key)
        report = IntegrationRunReport()
        stranded = self._unresolved_intent(record.action_key)
        if stranded is not None:
            # Deliberately NOT appended to the ledger: appending an outcome resolves the open
            # intent, and this run has established nothing about what that step did.
            report.outcomes.append(
                StepOutcome(
                    action_key=record.action_key,
                    step=stranded,
                    outcome=OUTCOME_BLOCKED,
                    recorded_by=self.recorder_id,
                    detail=(
                        f"a previous run opened {stranded!r} and never recorded its outcome; "
                        "the side effect may have happened, so nothing further runs until an "
                        "operator reconciles what landed"
                    ),
                )
            )
            return report
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
                self._policy(),
                record,
                preflight,
                ledger=working_ledger,
                trusted_recorder=self.recorder_id,
            )
            report.states.append(decision.state)
            report.final_decision = decision
            if decision.next_step is None:
                return report

            # The admission lands BEFORE the side effect, for every step: claiming it
            # afterwards would leave exactly the window it exists to close, and a step with no
            # admission cannot record an outcome at all (the receipt IS the admission).
            receipt, refused = self._begin_step(record.action_key, decision.next_step)
            if refused is not None:
                report.outcomes.append(
                    StepOutcome(
                        action_key=record.action_key,
                        step=decision.next_step,
                        outcome=OUTCOME_BLOCKED,
                        recorded_by=self.recorder_id,
                        detail=refused,
                    )
                )
                return report
            outcome = self._perform_integration_step(
                decision, record, report, preflight=preflight
            )
            report.outcomes.append(outcome)
            self._record(outcome, receipt)
            working_ledger.append(outcome)
            if outcome.outcome != OUTCOME_DONE:
                # A refused or unsettled step stops the run here. Re-deciding would only
                # produce the same step again, and looping on it would turn a fail-closed
                # refusal into a spin.
                report.final_decision = decide_integration(
                    self._policy(),
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
        lane = ops.describe_lane_worktree(path=self.lane_worktree)
        report.measured_worktree = lane

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
            review_generation_admissible=(
                authority.review_generation_admissible
                and bool(authority.review_generation)
                and authority.review_generation == record.review_generation
            ),
            target_identity_known=authority.target_identity_known,
            callbacks_drained=authority.callbacks_drained,
            owner_gates_resolved=authority.owner_gates_resolved,
            source_ci=authority.source_ci,
            integration_ci=integration_ci,
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
            # There is no checkout to verify before this call. The apply used to be preceded
            # by a re-assertion that a dedicated worktree was admissible, because the next
            # thing to happen was a `git switch` on a real checkout; review j#96406 finding 1
            # showed that no amount of verification in front of a *path* is worth anything,
            # since the path can be re-pointed after the check. What goes to the port now is
            # two object ids and a branch name used for the message.
            result = self.operations.apply_merge(
                source_head=record.source_head,
                target_ref=record.target_ref,
                # The parent the merge must sit on: the freshly measured remote target.
                expected_target_head=preflight.observed_target_head,
            )
            status = checked_merge_status(result.status)
            if status != MERGE_MERGED:
                # The status is a FIELD of the durable outcome, not a prefix on its prose.
                # j#96412 finding 2 asked for the typed status to reach the durable record and
                # R11 answered by string-formatting it into `detail`, which is the same
                # unparseable sentence with more words (j#96417 finding 2). An unknown status
                # becomes `unrecognized_status` — a value a consumer can match on — rather
                # than being folded into a sentence only a human would notice.
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    recorded_by=self.recorder_id,
                    outcome=OUTCOME_BLOCKED,
                    merge_status=status,
                    detail=result.detail,
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
                merge_status=status,
                git_version=result.git_version,
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
            # The TYPED status decides, and the typed status is recorded. R21 read
            # `accepted` — a derived boolean — and left the reason in `detail` as prose, so
            # the durable record could not tell a lost race from a git that never ran, and the
            # only recovery it suggested was the one for a lost race (j#96516 finding 1). An
            # unrecognized status is not a success: a port answering outside the vocabulary
            # has not shown that anything landed.
            status = checked_push_status(result.status)
            if status != PUSH_ACCEPTED:
                return StepOutcome(
                    action_key=decision.action_key,
                    step=step,
                    recorded_by=self.recorder_id,
                    outcome=OUTCOME_BLOCKED,
                    push_status=status,
                    detail=(
                        result.detail
                        or f"{BLOCKED_PUSH_REJECTED}: the push did not land ({status})"
                    ),
                )
            return StepOutcome(
                action_key=decision.action_key,
                step=step,
                recorded_by=self.recorder_id,
                outcome=OUTCOME_DONE,
                push_status=status,
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

    def _measure_cleanup(self, record: CleanupActionRecord) -> CleanupPreflight:
        """Build the ENTIRE cleanup preflight from this actuator's own reading (no caller input).

        R3 review j#96368 finding 2 is the reason this exists, and it was the heaviest finding
        of the round: every one of the fifteen facts gating the destructive steps was
        caller-supplied, and the reproduction removed a **foreign lane's worktree and deleted
        its branch** on nothing but those booleans.

        It no longer touches Git. Every Git step was withdrawn — the two ref deletes first
        (j#96396 finding 1) and then the worktree removal (j#96401 finding 1) — and the probes
        that gated them went with them rather than being kept as measured-but-unread values.
        The ledger is not consulted here either: the phase-aware re-measurement R6 needed
        existed because the removal changed what the next probe could see, and with no
        mutation of the world there is nothing for a later measurement to disagree with.

        Two things are still read, and both still matter for the one remaining step:

        - the durable authority, fresh, because releasing a lane's managed process before its
          work is integrated, its CI settled, or its callbacks drained abandons the lane;
        - **whether the record names THIS actuator's lane**, answered by comparing it against
          this actuator's own configuration. Retiring another lane's process is a cross-lane
          side effect exactly as removing its checkout was, and the answer must not come from
          the record that is asking.
        """
        authority = (
            self.authority.read_cleanup_authority(record=record)
            if self.authority is not None
            else CleanupAuthority()
        )
        return CleanupPreflight(
            # NOT ``record.integration_action_key``. R11 filled this from the very field the
            # decision compares it against, so ``preflight.authorizing_action_key !=
            # record.integration_action_key`` could not be true and the gate the cleanup opens
            # with was unreachable — a record authorized itself (Redmine #14825 item 5). The
            # authorizing action now comes from the durable authority, which reads which
            # integration action actually completed for this lane. An authority that cannot name
            # one leaves this empty, and an empty key matches no record's, so the gate refuses.
            authorizing_action_key=authority.authorizing_action_key,
            issue_closed=authority.issue_closed,
            integration_confirmed=authority.integration_confirmed,
            integration_ci_settled_green=authority.integration_ci_settled_green,
            callbacks_drained=authority.callbacks_drained,
            owner_gates_resolved=authority.owner_gates_resolved,
            # Every value the mutation consumes is compared, not merely some of them.
            # Review j#96406 finding 2: this checked the record's branch and path while
            # `release_process` acts on its issue and lane generation, so the pair being
            # verified was not the pair being used. An unbound expectation (the actuator
            # never configured with an issue / generation) fails closed rather than
            # matching whatever the record says.
            lane_is_foreign=not (
                record.worktree_path == self.lane_worktree
                and record.branch == self.lane_branch
                and record.issue == self.lane_issue
                and self.lane_generation is not None
                and record.lane_generation == self.lane_generation
            ),
        )

    def run_cleanup(self, record: CleanupActionRecord) -> CleanupRunReport:
        """Drive the post-close cleanup machine until it rests, performing each decided step.

        As with :meth:`run_integration` there is no caller preflight and no caller ledger:
        everything the machine decides from is measured here (:meth:`_measure_cleanup`).
        """
        if self._admission_pin is not None:
            self._admission_pin.require_cleanup(record)
        report = CleanupRunReport()
        stranded = self._unresolved_intent(record.action_key)
        if stranded is not None:
            report.outcomes.append(
                StepOutcome(
                    action_key=record.action_key,
                    step=stranded,
                    outcome=OUTCOME_BLOCKED,
                    recorded_by=self.recorder_id,
                    detail=(
                        f"a previous run opened {stranded!r} and never recorded its outcome; "
                        "the process may already have been released, so nothing further runs"
                    ),
                )
            )
            return report
        working_ledger: List[StepOutcome] = list(
            self.ledger.read(action_key=record.action_key)
        )
        while True:
            # Still re-read before every step. The reason R5/R6 needed it was that this
            # machine's own mutations changed what a later probe could see; nothing it does
            # now moves the world, but the durable authority is read fresh regardless — an
            # owner gate or callback can be raised by somebody else between two steps.
            preflight = self._measure_cleanup(record)
            report.measured_preflight = preflight
            decision = decide_cleanup(
                record,
                preflight,
                ledger=working_ledger,
                trusted_recorder=self.recorder_id,
            )
            report.states.append(decision.state)
            report.final_decision = decision
            if decision.next_step is None:
                return report

            receipt, refused = self._begin_step(record.action_key, decision.next_step)
            if refused is not None:
                report.outcomes.append(
                    StepOutcome(
                        action_key=record.action_key,
                        step=decision.next_step,
                        outcome=OUTCOME_BLOCKED,
                        recorded_by=self.recorder_id,
                        detail=refused,
                    )
                )
                return report
            outcome = self._perform_cleanup_step(decision, record)
            report.outcomes.append(outcome)
            self._record(outcome, receipt)
            working_ledger.append(outcome)
            if outcome.recorded_by != self.recorder_id:
                # Unreachable by construction, and asserted rather than assumed: an outcome
                # the next decision will not count would make this loop spin forever.
                raise AssertionError(
                    "a recorded cleanup outcome lost this actuator's provenance"
                )
            if outcome.outcome != OUTCOME_DONE:
                report.final_decision = decide_cleanup(
                    record,
                    self._measure_cleanup(record),
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

        # No step falls through to a default. Until R7 the local branch delete sat here as the
        # unnamed tail of this dispatch — a shape that runs *something* for any step the
        # decision names — and review j#96396 finding 2 caught the record that came out of it
        # claiming a compare-and-swap while the argv was `git branch -D`. There is nothing
        # destructive left to reach, and an unrecognized step is refused rather than mapped
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
    "PushResult",
    "MergeResult",
    "MERGE_MERGED",
    "MERGE_CONTENT_CONFLICT",
    "MERGE_PRIMITIVE_UNSUPPORTED",
    "MERGE_PROBE_ERROR",
    "MERGE_SANDBOX_ERROR",
    "MERGE_INVALID_INPUT",
    "MERGE_NONDETERMINISTIC_CONFIG",
    "MERGE_ERROR",
    "MERGE_COMMIT_ERROR",
    "MERGE_UNRECOGNIZED",
    "MERGE_STATUSES",
    "PUSH_ACCEPTED",
    "PUSH_INVALID_INPUT",
    "PUSH_REMOTE_MOVED",
    "PUSH_REMOTE_REFUSED",
    "PUSH_OPERATIONAL_ERROR",
    "PUSH_UNRECOGNIZED",
    "PUSH_STATUSES",
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
