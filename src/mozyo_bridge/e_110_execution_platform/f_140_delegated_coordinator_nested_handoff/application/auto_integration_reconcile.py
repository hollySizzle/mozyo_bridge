"""Recovering an action stranded between a mutation and its receipt (review j#96611 finding 5).

R1 could DETECT the state — a durable intent with no outcome — and then stopped forever. That is
not the replay-safe recovery #14825's acceptance asks for, and "an operator resolves it by
reading what actually landed" was a sentence with no executable contract behind it.

This is that contract. It asks the world the one question the durable record cannot answer — did
the mutation land? — and closes the stranded admission with the answer.

**The measurement decides, and only the measurement.** Nothing here reasons about what the run
*probably* did. A push either put a commit on the target ref or it did not, and the remote is
the authority for that; a merge writes an object and moves no ref, so re-running it is safe by
construction and the stranded admission closes as "not landed" without probing anything.

**Three outcomes, and the third one is not an inconvenience.** ``landed`` and ``not_landed``
close the admission. ``ambiguous`` — the probe itself could not be carried out — leaves it OPEN,
which keeps the action stopped. That is deliberate: a reconciliation that guesses when it cannot
see is the same defect as the run it is recovering from, one layer further out. An unreadable
remote is not an unpushed commit.

**Recovery cannot forge.** The store's :meth:`~...auto_integration_ledger.SqliteLedgerStore.resolve_intent`
requires an OPEN admission to close, so this path can never invent an outcome for a step nobody
began, and it marks the row ``reconciled`` so a later reader can tell a measured resolution from
the receipt of the run that actually did the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (  # noqa: E501
    AutoIntegrationUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    AutoIntegrationLedgerError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    PUSH_ACCEPTED,
    STEP_INTEGRATION_APPLY,
    STEP_INTEGRATION_CI,
    STEP_PUSH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    IntegrationActionRecord,
    StepOutcome,
    completed_steps,
    is_full_sha,
)

#: Nothing was stranded — the action has no open admission, so there is nothing to recover.
RECONCILED_NOTHING_STRANDED = "nothing_stranded"
#: The mutation was observed to have landed; the admission is closed with its receipt-equivalent.
RECONCILED_LANDED = "landed"
#: The mutation was observed NOT to have landed; the admission is closed and the step may re-run.
RECONCILED_NOT_LANDED = "not_landed"
#: The probe could not be carried out. The admission stays OPEN and the action stays stopped.
RECONCILED_AMBIGUOUS = "ambiguous"
#: A stranded step this reconciler has no measurement for. Also leaves the admission open.
RECONCILED_UNKNOWN_STEP = "unknown_step"
#: The store refused the resolution (an unreadable ledger, a duplicate `done`).
RECONCILED_STORE_REFUSED = "store_refused"


@dataclass(frozen=True)
class Reconciliation:
    """What one recovery attempt established about a stranded step."""

    status: str
    step: str = ""
    head: str = ""
    observation: str = ""
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Whether the admission was closed. ``ambiguous`` is deliberately not resolved."""
        return self.status in (RECONCILED_LANDED, RECONCILED_NOT_LANDED)


@dataclass(frozen=True)
class StrandedActionReconciler:
    """Closes an integration action's stranded admission with a measured answer."""

    use_case: AutoIntegrationUseCase

    def reconcile(self, record: IntegrationActionRecord) -> Reconciliation:
        """Measure what actually happened for the stranded step, and close it."""
        stranded = self._open_step(record.action_key)
        if stranded is None:
            return Reconciliation(status=RECONCILED_NOTHING_STRANDED)

        if stranded == STEP_PUSH:
            return self._reconcile_push(record)
        if stranded in (STEP_INTEGRATION_APPLY, STEP_INTEGRATION_CI):
            # Neither moves a ref. The apply writes a commit OBJECT into the store and the CI
            # step only observes an asynchronous gate, so re-running either is safe and — for
            # the apply — deterministic: the same action rebuilds the same commit. Closing as
            # "not landed" is therefore a statement about the ref, which is what the next
            # decision reads, not a claim that no object was written.
            return self._close(
                record,
                step=stranded,
                landed=False,
                observation=(
                    f"{stranded} moves no ref: an interrupted attempt left the target "
                    "unchanged, so the step is re-runnable"
                ),
                detail=(
                    "the stranded step neither moved a ref nor published anything; it will be "
                    "decided again on the next run"
                ),
            )
        return Reconciliation(
            status=RECONCILED_UNKNOWN_STEP,
            step=stranded,
            detail=(
                f"no measurement is defined for a stranded {stranded!r}; the admission stays "
                "open and the action stays stopped"
            ),
        )

    # -- the push, which is the one that publishes -------------------------

    def _reconcile_push(self, record: IntegrationActionRecord) -> Reconciliation:
        """Did the interrupted push put its commit on the target ref?

        The head asked about is the one the push WOULD have offered: a merge disposition
        publishes the commit its own apply produced, a fast-forward publishes the source head.
        Reading it from this action's own trusted apply receipt is the same rule the push step
        itself follows — a merge must publish the commit its apply created, or nothing.
        """
        ops = self.use_case.operations
        head = self._pushed_head(record)
        if not is_full_sha(head):
            return Reconciliation(
                status=RECONCILED_AMBIGUOUS,
                step=STEP_PUSH,
                detail=(
                    "the head this push would have offered cannot be determined from the "
                    "ledger, so what to look for on the target is unknown"
                ),
            )

        # The probe must be able to ask the question at all. An unreadable target tip is not an
        # unpushed commit, and folding the two would let a network failure re-authorize a push.
        if not str(ops.remote_branch_tip(record.target_ref) or "").strip():
            return Reconciliation(
                status=RECONCILED_AMBIGUOUS,
                step=STEP_PUSH,
                head=head,
                detail=(
                    f"the target ref {record.target_ref!r} could not be read, so whether the "
                    "push landed is unknown; the admission stays open"
                ),
            )

        landed = bool(ops.commit_on_remote(head, branch=record.target_ref))
        return self._close(
            record,
            step=STEP_PUSH,
            landed=landed,
            head=head,
            observation=(
                f"remote {record.target_ref} "
                f"{'contains' if landed else 'does not contain'} {head}"
            ),
            detail=(
                "the interrupted push is observed on the target; recording the receipt it "
                "never got to write"
                if landed
                else "the interrupted push is not on the target; the step may be offered again"
            ),
        )

    def _pushed_head(self, record: IntegrationActionRecord) -> str:
        """The commit the stranded push would have published (this action's own, or none)."""
        trusted = completed_steps(
            self.use_case.ledger.read(action_key=record.action_key),
            action_key=record.action_key,
            recorded_by=self.use_case.recorder_id,
        )
        applied = trusted.get(STEP_INTEGRATION_APPLY)
        if applied is not None and is_full_sha(applied.head):
            return applied.head
        return record.source_head

    # -- closing -----------------------------------------------------------

    def _close(
        self,
        record: IntegrationActionRecord,
        *,
        step: str,
        landed: bool,
        observation: str,
        detail: str,
        head: str = "",
    ) -> Reconciliation:
        resolution = StepOutcome(
            action_key=record.action_key,
            step=step,
            outcome=OUTCOME_DONE if landed else OUTCOME_BLOCKED,
            recorded_by=self.use_case.recorder_id,
            head=head if landed else "",
            push_status=PUSH_ACCEPTED if (landed and step == STEP_PUSH) else "",
            detail=f"reconciled after an interrupted run: {detail}",
        )
        resolver = getattr(self.use_case.ledger, "resolve_intent", None)
        if resolver is None:
            return Reconciliation(
                status=RECONCILED_AMBIGUOUS,
                step=step,
                head=head,
                detail=(
                    "this ledger holds no admissions, so there is nothing to reconcile and "
                    "nothing that would make the resolution durable"
                ),
            )
        try:
            resolver(
                action_key=record.action_key,
                step=step,
                resolution=resolution,
                observation=observation,
            )
        except AutoIntegrationLedgerError as exc:
            return Reconciliation(
                status=RECONCILED_STORE_REFUSED,
                step=step,
                head=head,
                observation=observation,
                detail=str(exc),
            )
        return Reconciliation(
            status=RECONCILED_LANDED if landed else RECONCILED_NOT_LANDED,
            step=step,
            head=head if landed else "",
            observation=observation,
            detail=detail,
        )

    def _open_step(self, action_key: str) -> Optional[str]:
        reader = getattr(self.use_case.ledger, "unresolved_intents", None)
        if reader is None:
            return None
        open_intents = reader(action_key=action_key)
        return str(open_intents[0].step) if open_intents else None


__all__ = (
    "RECONCILED_AMBIGUOUS",
    "RECONCILED_LANDED",
    "RECONCILED_NOTHING_STRANDED",
    "RECONCILED_NOT_LANDED",
    "RECONCILED_STORE_REFUSED",
    "RECONCILED_UNKNOWN_STEP",
    "Reconciliation",
    "StrandedActionReconciler",
)
