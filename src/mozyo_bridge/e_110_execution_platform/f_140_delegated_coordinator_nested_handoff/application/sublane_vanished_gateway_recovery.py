"""Plan (or resume) the durable recovery of a vanished gateway (Redmine #14741 j#97147).

The authority half of the vanished-gateway heal, with no live effect of any kind: nothing
here launches, closes, sends or appends. It answers two questions and writes at most one
durable row.

**Is this launch's recovery a plain heal, or does it owe an identity receipt?** Read from
the participant's own CURRENT launch-generation row under an explicit home, matched on every
identity axis, and classified from the action id's SHAPE (j#96892 / j#97105). Only an exact
legacy ``startup-<64hex>`` is `legacy_direct`, and that path opens no receipt store and
writes no transaction -- which is what keeps every pre-#14741 heal byte-invariant. Missing,
unreadable, pending, mismatched or unclassifiable is a typed refusal; there is no fallback,
because "assume legacy" is the fail-open this whole ticket exists to close.

**Is this a new recovery or the same one again?** The action id is deterministic
(:mod:`...domain.vanished_gateway_recovery`), so a retry addresses the row the first attempt
wrote. A stored row is compared as a whole and RESUMED -- never re-planned, re-read against
the current generation, enriched or superseded (j#97121): past the plan, the manifest is the
authority, and the world is allowed to have moved on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E501
    OUTCOME_LEGACY_DIRECT,
    OUTCOME_RECEIPT_PLANNED,
    OUTCOME_REPLAYED,
    REDISPATCH_GATEWAY_ONCE,
    REFUSE_EVIDENCE_UNAVAILABLE,
    REFUSE_FOREIGN_TRANSACTION,
    REFUSE_GENERATION_MISMATCH,
    REFUSE_GENERATION_UNAVAILABLE,
    REFUSE_REQUEST_INVALID,
    REFUSE_UNKNOWN_ACTION_SHAPE,
    RESUME_GATE,
    ParticipantAuthority,
    RecoveryDecision,
    RequestAnchor,
    recovery_action_id,
    refuse,
)

#: A recovery is its own first generation. It is never a re-anchor of someone else's action,
#: so there is no earlier generation for it to supersede.
RECOVERY_ACTION_GENERATION = 1


@dataclass(frozen=True)
class RecoveryPlan:
    """The durable outcome of planning, with the row it addresses. (pure value)"""

    decision: RecoveryDecision
    action_id: str = ""
    participants: tuple = ()

    @property
    def refused(self) -> bool:
        return self.decision.refused


def _pointers(anchor: RequestAnchor):
    """The decision and continuation a recovery row carries.

    Both name the ORIGINAL implementation request: the decision is the durable record that
    authorised the work, and the continuation is what a completed recovery must re-deliver.
    The gateway's own semantic action is used, never the worker's -- two continuations that
    differ only in who redispatches would otherwise be indistinguishable in a stored row.
    """
    from mozyo_bridge.core.state.replacement_transaction_model import (
        ContinuationPointer,
        DecisionPointer,
    )

    decision = DecisionPointer(
        source=anchor.source, issue_id=anchor.issue_id, journal_id=anchor.journal_id
    )
    continuation = ContinuationPointer(
        source=anchor.source,
        issue_id=anchor.issue_id,
        journal_id=anchor.journal_id,
        expected_gate=RESUME_GATE,
        next_semantic_action=REDISPATCH_GATEWAY_ONCE,
    )
    return decision, continuation


def _current_generation(home: Optional[Path], authority: ParticipantAuthority):
    """The participant's own current launch-generation row, or a typed refusal."""
    from mozyo_bridge.core.state.herdr_launch_generation import (
        GENERATION_ATTESTED,
        HerdrLaunchGenerationStore,
    )

    try:
        row = HerdrLaunchGenerationStore(home=home).read(authority.assigned_name)
    except Exception:  # noqa: BLE001 - an unreadable authority is a refusal, not a guess
        return refuse(REFUSE_GENERATION_UNAVAILABLE, "the launch generation authority could not be read")
    if row is None:
        return refuse(REFUSE_GENERATION_UNAVAILABLE, "no launch generation is recorded")
    for attr, expected in (
        ("workspace_id", authority.workspace_id),
        ("lane_id", authority.lane_id),
        ("role", authority.role),
        ("assigned_name", authority.assigned_name),
        ("locator", authority.old_locator),
    ):
        if getattr(row, attr, None) != expected:
            return refuse(
                REFUSE_GENERATION_MISMATCH,
                "the current launch generation is not the participant this recovery names",
            )
    if getattr(row, "phase", None) != GENERATION_ATTESTED:
        return refuse(
            REFUSE_GENERATION_MISMATCH, "the current launch generation is not attested"
        )
    return row


def _pin(authority: ParticipantAuthority):
    from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin

    return ParticipantPin(
        lane_id=authority.lane_id,
        role=authority.role,
        provider=authority.provider,
        assigned_name=authority.assigned_name,
        old_locator=authority.old_locator,
        is_self=False,
        lane_revision=authority.lane_revision,
        lane_generation=authority.lane_generation,
        evidence_workspace_id=authority.evidence_workspace_id,
        evidence_startup_action_id=authority.evidence_startup_action_id,
        evidence_cause=authority.evidence_cause,
    )


def plan_vanished_gateway_recovery(
    *,
    store: Any,
    home: Optional[Path],
    anchor: RequestAnchor,
    authority: ParticipantAuthority,
) -> RecoveryPlan:
    """Classify this recovery and, if it owes a receipt, plan its durable transaction.

    Zero live effect. The only write this can perform is one ``plan_transaction``, and only
    on the receipt-capable path with a complete bound evidence triplet.
    """
    from mozyo_bridge.core.state.replacement_transaction import (
        ReplacementTransactionKey,
    )

    if not isinstance(anchor, RequestAnchor) or not isinstance(
        authority, ParticipantAuthority
    ):
        return RecoveryPlan(decision=refuse(REFUSE_REQUEST_INVALID, "not an exact request"))

    action_id = recovery_action_id(anchor, authority)
    key = ReplacementTransactionKey(authority.workspace_id, action_id)

    # A stored row for THIS exact action is resumed, not re-decided (j#97121).
    stored = store.get(key)
    if stored is not None:
        return _replay(stored, anchor, authority, action_id)

    generation = _current_generation(home, authority)
    if isinstance(generation, RecoveryDecision):
        return RecoveryPlan(decision=generation, action_id=action_id)

    from mozyo_bridge.core.state.startup_action_capability import (
        CAPABILITY_IDENTITY_RECEIPT,
        CAPABILITY_LEGACY,
        action_capability,
    )

    try:
        capability = action_capability(getattr(generation, "startup_action_id", None))
    except Exception:  # noqa: BLE001 - an unclassifiable action is never legacy
        return RecoveryPlan(
            decision=refuse(
                REFUSE_UNKNOWN_ACTION_SHAPE,
                "the startup action id matches no known shape",
            ),
            action_id=action_id,
        )

    if capability == CAPABILITY_LEGACY:
        # The pre-#14741 heal, unchanged: no receipt store is opened and no row is written.
        return RecoveryPlan(
            decision=RecoveryDecision(outcome=OUTCOME_LEGACY_DIRECT, action_id=action_id),
            action_id=action_id,
        )
    if capability != CAPABILITY_IDENTITY_RECEIPT:
        return RecoveryPlan(
            decision=refuse(
                REFUSE_UNKNOWN_ACTION_SHAPE, "unrecognised startup action capability"
            ),
            action_id=action_id,
        )

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner_composition import (  # noqa: E501
        plan_participants_with_evidence,
    )

    planning = plan_participants_with_evidence(
        [_pin(authority)],
        home=home,
        workspace_id=authority.workspace_id,
        lane_id=authority.lane_id,
    )
    if planning.refused:
        # Zero transaction write: absent, corrupt, stale, foreign or incomplete evidence
        # leaves this recovery unplanned rather than planned on an unproven launch.
        return RecoveryPlan(
            decision=refuse(REFUSE_EVIDENCE_UNAVAILABLE, planning.refusal),
            action_id=action_id,
        )
    planned = tuple(planning.participants)
    if len(planned) != 1 or not planned[0].evidence_startup_action_id:
        return RecoveryPlan(
            decision=refuse(
                REFUSE_EVIDENCE_UNAVAILABLE,
                "a receipt-capable recovery must pin exactly one evidenced participant",
            ),
            action_id=action_id,
        )

    decision, continuation = _pointers(anchor)
    outcome = store.plan_transaction(
        key,
        action_generation=RECOVERY_ACTION_GENERATION,
        decision=decision,
        continuation=continuation,
        participants=list(planned),
    )
    current = store.get(key)
    if current is None:
        return RecoveryPlan(
            decision=refuse(REFUSE_FOREIGN_TRANSACTION, "the row vanished after the plan"),
            action_id=action_id,
        )
    if not outcome.applied:
        # A concurrent planner won the race. That is not a conflict: the id is deterministic,
        # so whatever is there was planned from this same request -- verify and resume it.
        return _replay(current, anchor, authority, action_id)
    return RecoveryPlan(
        decision=RecoveryDecision(outcome=OUTCOME_RECEIPT_PLANNED, action_id=action_id),
        action_id=action_id,
        participants=planned,
    )


def _replay(stored, anchor: RequestAnchor, authority: ParticipantAuthority, action_id: str):
    """Resume an existing row after proving it is THIS action, whole.

    The stored manifest is the authority: nothing here re-reads the current generation, the
    lifecycle or the receipt store, and nothing enriches or supersedes. Past the plan, those
    have all legitimately moved on (j#97121).
    """
    from mozyo_bridge.core.state.replacement_transaction_model import (
        participant_authority_matches,
    )

    decision, continuation = _pointers(anchor)
    if (
        getattr(stored, "action_generation", None) != RECOVERY_ACTION_GENERATION
        or getattr(stored, "decision", None) != decision
        or getattr(stored, "continuation", None) != continuation
        or len(getattr(stored, "participants", ()) or ()) != 1
    ):
        return RecoveryPlan(
            decision=refuse(
                REFUSE_FOREIGN_TRANSACTION,
                "a different authority is already acting on this recovery",
            ),
            action_id=action_id,
        )
    pin = _pin(authority)
    stored_pin = stored.participants[0]
    if not participant_authority_matches(stored_pin, pin):
        return RecoveryPlan(
            decision=refuse(
                REFUSE_FOREIGN_TRANSACTION,
                "the stored participant is not the gateway this recovery names",
            ),
            action_id=action_id,
        )
    return RecoveryPlan(
        decision=RecoveryDecision(outcome=OUTCOME_REPLAYED, action_id=action_id),
        action_id=action_id,
        participants=(stored_pin,),
    )


__all__ = (
    "RECOVERY_ACTION_GENERATION",
    "RecoveryPlan",
    "plan_vanished_gateway_recovery",
)
