"""Actuate a planned vanished-gateway recovery, up to a live attested gateway (#14741 j#97184).

The stored row from :mod:`.sublane_vanished_gateway_recovery` is the ONLY authority here.
This module re-reads it, proves it is still that exact action, and hands it to the existing
exact-generation actuator -- the same one the gateway-refresh and stale-worker rails use, so
the close / action-bound launch / attestation / evidence-discharge choreography is not
reimplemented for this rail.

It stops at ``recovered_ready``. A live attested gateway is not a delivered
implementation request: the continuation send and its ledger confirmation are a separate
tranche, and naming this outcome "completed" would be the kind of claim that later has to be
walked back. Nothing here sends anything.

What it deliberately does NOT do: re-plan, re-read the launch generation / lifecycle /
receipt planner, enrich or supersede the row. Past the plan the manifest is the authority
and the world is allowed to have moved on (j#97121).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E501
    RECOVERY_ACTION_GENERATION,
    RecoveryPlan,
    stored_row_is_this_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E501
    OUTCOME_RECEIPT_PLANNED,
    OUTCOME_REPLAYED,
    RequestAnchor,
)

#: The gateway is live, attested to this exact action, and its update evidence is
#: discharged. NOT "completed" and NOT "redispatched": nothing has been delivered yet.
RECOVERED_READY = "recovered_ready"

#: The plan is not one this executor acts on (legacy direct, a refusal, or an unknown shape).
STOPPED_NOT_ACTIONABLE = "plan_not_actionable"
#: The stored row is absent, unreadable, or is not this exact recovery any more.
STOPPED_TRANSACTION_UNAVAILABLE = "transaction_unavailable"
#: The actuator refused or could not finish. The exact step is carried as its own token.
STOPPED_ACTUATION = "actuation_stopped"

#: The plan outcomes this executor accepts. They are ONE family: they differ in when the row
#: was observed, not in what authority it carries (ruling j#97162).
ACTIONABLE_OUTCOMES = (OUTCOME_RECEIPT_PLANNED, OUTCOME_REPLAYED)


@dataclass(frozen=True)
class RecoveryActuation:
    """What the actuation did, in closed tokens only. (pure value)"""

    outcome: str = ""
    stopped: str = ""
    detail: str = ""
    action_id: str = ""

    @property
    def ready(self) -> bool:
        return self.outcome == RECOVERED_READY


def _stopped(reason: str, detail: str = "", action_id: str = "") -> RecoveryActuation:
    return RecoveryActuation(stopped=reason, detail=detail, action_id=action_id)


def actuate_vanished_gateway_recovery(
    *,
    plan: RecoveryPlan,
    anchor: RequestAnchor,
    store: Any,
    workspace_id: str,
    holder: str,
    actuation_port: Any,
    launch_authority: Optional[Any] = None,
    store_admission: Optional[Any] = None,
    clock: Optional[Any] = None,
) -> RecoveryActuation:
    """Drive the planned recovery to a live attested gateway, or stop with a typed reason.

    ``store`` is the SAME transaction store the plan was written through: the evidence
    completion is bound to ``store.path.parent`` from the actuator's first construction, so
    what a plan armed and what a discharge clears cannot address different homes.
    """
    from mozyo_bridge.core.state.replacement_preservation import (
        assess_worker_recovery_preservation,
    )
    from mozyo_bridge.core.state.replacement_transaction import (
        ReplacementTransactionKey,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
        ReplacementActuatorUseCase,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_completion import (  # noqa: E501
        build_update_evidence_completion,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
        ACTUATION_RECOVERED,
    )

    if not isinstance(plan, RecoveryPlan) or not isinstance(anchor, RequestAnchor):
        return _stopped(STOPPED_NOT_ACTIONABLE, "not an exact plan")
    outcome = getattr(getattr(plan, "decision", None), "outcome", None)
    if type(outcome) is not str or outcome not in ACTIONABLE_OUTCOMES:
        # `legacy_direct`, any refusal, and anything unrecognised: this executor is the
        # receipt-capable rail only, and it acts on nothing else.
        return _stopped(STOPPED_NOT_ACTIONABLE, "the plan is not a receipt-capable recovery")
    action_id = getattr(plan, "action_id", "")
    if type(action_id) is not str or not action_id:
        return _stopped(STOPPED_NOT_ACTIONABLE, "the plan carries no action id")

    try:
        key = ReplacementTransactionKey(workspace_id, action_id)
        stored = store.get(key)
    except Exception:  # noqa: BLE001 - KI / SystemExit / GeneratorExit propagate
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the transaction authority could not be read",
            action_id,
        )
    if stored is None:
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE, "no such recovery transaction", action_id
        )
    try:
        same = stored_row_is_this_recovery(
            stored, key=key, anchor=anchor, action_id=action_id
        )
    except Exception:  # noqa: BLE001 - a hostile row is input, not truth
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored transaction could not be read",
            action_id,
        )
    if not same:
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored row at this key is not this recovery",
            action_id,
        )

    kwargs = dict(
        preservation_policy=assess_worker_recovery_preservation,
        # From the FIRST construction, and bound to this store's own home (j#97131): an
        # actuator that reached its consume step with no completion port would fail closed
        # after a live relaunch, which is the expensive place to discover a wiring gap.
        evidence_completion=build_update_evidence_completion(Path(store.path).parent),
    )
    if launch_authority is not None:
        kwargs["launch_authority"] = launch_authority
    if store_admission is not None:
        kwargs["store_admission"] = store_admission
    if clock is not None:
        kwargs["clock"] = clock
    try:
        result = ReplacementActuatorUseCase(store, actuation_port, **kwargs).drive_worker_recovery(
            key, holder=holder, expected_action_generation=RECOVERY_ACTION_GENERATION
        )
    except Exception:  # noqa: BLE001 - no adapter prose reaches this rail's surface
        return _stopped(STOPPED_ACTUATION, "the actuator could not be driven", action_id)

    status = getattr(result, "status", None)
    if status == ACTUATION_RECOVERED:
        return RecoveryActuation(outcome=RECOVERED_READY, action_id=action_id)
    # The actuator's statuses are closed tokens, so the step it stopped on is carried as-is
    # and nothing else is: no exception body, no adapter detail, no locator.
    return _stopped(
        STOPPED_ACTUATION,
        status if type(status) is str else "",
        action_id,
    )


__all__ = (
    "ACTIONABLE_OUTCOMES",
    "RECOVERED_READY",
    "STOPPED_ACTUATION",
    "STOPPED_NOT_ACTIONABLE",
    "STOPPED_TRANSACTION_UNAVAILABLE",
    "RecoveryActuation",
    "actuate_vanished_gateway_recovery",
)
