"""Event-driven reconcile runtime: one bounded reconcile cycle (Redmine #13758).

Composes the pure turn/gate state machine
(:mod:`...domain.reconcile_state_machine`) with the persisted reconcile bookkeeping
(:mod:`mozyo_bridge.core.state.reconcile_state`) and the **existing** callback outbox
(:mod:`mozyo_bridge.core.state.callback_outbox`) into one reconcile cycle: open the
per-dispatch record, re-read Redmine for the action-time observation, decide, and — for a
send decision — enqueue through the outbox and CAS-persist the new phase / counter.

The reconcile decision never sends directly and never re-reads Redmine here: all I/O is
injected. ``observe`` is the Redmine re-read seam (the live adapter mapping structured gate
markers -> a :class:`...reconcile_state_machine.ReconcileObservation` connects at the
installed-artifact E2E, #13492; unit tests inject a fake). Delivery reuses the existing
outbox — **no second outbox / ledger** (j#79337) — with the state machine's ``next_phase``
as the outbox ``normalized_gate`` token, so ``self_heal_attempt_1`` / ``self_heal_attempt_2``
/ ``coordinator_escalation`` are distinct one-time idempotency keys (each fires exactly once;
a duplicate wake at the same phase is deduped by the UNIQUE fence).

Actuation order is enqueue-then-advance (crash-idempotent): the outbox row is the durable
delivery intent, the reconcile row is derived bookkeeping. A crash between them re-runs the
cycle, and both writes are idempotent (the outbox UNIQUE fence, the store's exact-revision
CAS), so nothing is double-sent and nothing is lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.core.state.callback_outbox import (
    CallbackOutbox,
    CallbackOutboxKey,
)
from mozyo_bridge.core.state.reconcile_state import (
    ReconcileStateKey,
    ReconcileStateRecord,
    ReconcileStateStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_state_machine import (
    COORDINATOR_ROUTE,
    RECONCILE_ACTION_DELIVER,
    RECONCILE_ACTION_ESCALATE,
    RECONCILE_ACTION_SELF_HEAL,
    RECONCILE_COORDINATOR_ESCALATION,
    RECONCILE_TURN_ENDED_GATE_PENDING,
    ReconcileDecision,
    ReconcileObservation,
    advance_reconcile,
)

#: The ticket source for reconcile-owned outbox keys (Redmine is the workflow truth).
RECONCILE_SOURCE = "redmine"
#: Outbox notification kinds for reconcile-owned sends (fixed tokens).
KIND_SELF_HEAL = "self_heal"
KIND_ESCALATION = "reconcile_escalation"


#: The Redmine re-read seam: given the cycle + the current record, return the action-time
#: observation the pure state machine decides over. The live adapter maps structured gate
#: markers at the exact anchor / generation -> a fail-closed observation (#13492); a unit
#: test injects a fake. It must never fabricate readability: an unreadable Redmine returns
#: ``redmine_readable=False`` (the state machine zero-sends).
Observe = Callable[["ReconcileCycleInput", ReconcileStateRecord], ReconcileObservation]


@dataclass(frozen=True)
class ReconcileCycleInput:
    """The identity + expectations of one dispatch a reconcile cycle reconciles.

    ``dispatch_journal`` is the journal id of the dispatch anchor (the durable
    implementation / review request), used as the outbox ``journal`` so every reconcile
    send correlates to the exact anchor. ``expected_gate`` / ``expected_next_owner`` are the
    re-derivable expectations (from Redmine + the lane registry) this dispatch should have
    produced; ``target_lane`` / ``target_generation`` are the durable target seam the outbox
    delivery authority binds the re-resolved live target to.
    """

    key: ReconcileStateKey
    issue_id: str
    dispatch_journal: str
    expected_gate: str
    expected_next_owner: str
    lane_generation: int = 0
    deadline: str = ""
    target_lane: str = ""
    target_generation: str = ""


def outbox_key_for(
    cycle: ReconcileCycleInput, decision: ReconcileDecision
) -> CallbackOutboxKey:
    """The outbox idempotency key for a reconcile send decision. (pure)

    The ``normalized_gate`` token distinguishes the reconcile send kinds so each fires
    exactly once through the shared UNIQUE fence:

    - deliver -> the advanced gate itself (collides-and-dedups with the discovery path's
      coordinator callback for the same gate — exactly-once, §1);
    - self-heal -> the target phase (``self_heal_attempt_1`` / ``self_heal_attempt_2``), so
      the two attempts are distinct keys but each attempt is idempotent under duplicate wakes;
    - escalate -> ``coordinator_escalation``, so repeated no-progress cycles after the
      escalation re-enqueue the same key and are deduped (§5 "以後 duplicate 抑止").
    """
    if decision.action == RECONCILE_ACTION_DELIVER:
        gate = cycle.expected_gate
        route = COORDINATOR_ROUTE
    elif decision.action == RECONCILE_ACTION_ESCALATE:
        gate = RECONCILE_COORDINATOR_ESCALATION
        route = COORDINATOR_ROUTE
    else:  # self-heal
        gate = decision.next_phase
        route = decision.route
    return CallbackOutboxKey(
        source=RECONCILE_SOURCE,
        issue=str(cycle.issue_id).strip(),
        journal=str(cycle.dispatch_journal).strip(),
        normalized_gate=str(gate).strip(),
        callback_route=str(route).strip(),
        workspace_id=str(cycle.key.workspace_id).strip(),
    )


def reconcile_once(
    cycle: ReconcileCycleInput,
    *,
    observe: Observe,
    outbox: CallbackOutbox,
    store: ReconcileStateStore,
    now: Optional[str] = None,
) -> dict:
    """Run one reconcile cycle for a dispatch; return a redaction-safe report.

    Open-or-adopt the reconcile row (a duplicate wake never resets the accumulated counter),
    re-read Redmine into an observation, decide with the pure state machine, and — only for a
    send decision — enqueue through the existing outbox (route + gate token per
    :func:`outbox_key_for`) then CAS-persist the new phase / counter. A zero-send /
    fail-closed decision mutates nothing (the record is left byte-unchanged). The report
    names the action, reason, route, and outbox result (no pane id / credential).
    """
    # 1. Open-or-adopt the per-dispatch record. ``open_cycle`` never resets a returning row's
    #    counter (acceptance §7); a new dispatch is a new anchor, hence a fresh record.
    store.open_cycle(
        cycle.key,
        lane_generation=int(cycle.lane_generation),
        issue_id=cycle.issue_id,
        latest_journal_id=cycle.dispatch_journal,
        expected_gate=cycle.expected_gate,
        expected_next_owner=cycle.expected_next_owner,
        deadline=cycle.deadline,
        phase=RECONCILE_TURN_ENDED_GATE_PENDING,
        now=now,
    )
    record = store.get(cycle.key)
    if record is None:  # a concurrent delete between open and read — degrade to a fresh cycle.
        return {"action": "none", "reason": "reconcile_record_absent", "sent": False}

    # 2. Action-time Redmine re-read (injected). The workflow truth, never the runtime signal.
    observation = observe(cycle, record)

    # 3. Pure decision.
    decision = advance_reconcile(
        phase=record.phase,
        reconcile_failure_count=record.reconcile_failure_count,
        observation=observation,
    )

    # 4. Actuate a send through the EXISTING outbox (enqueue-before-advance, idempotent).
    outbox_state = ""
    enqueued = None
    if decision.sends:
        key = outbox_key_for(cycle, decision)
        kind = _kind_for(decision)
        receiver = (
            "" if decision.route == COORDINATOR_ROUTE else str(decision.route).strip()
        )
        enqueued = outbox.enqueue(
            key,
            notification_kind=kind,
            target_lane=str(cycle.target_lane or "").strip(),
            target_receiver=receiver,
            target_generation=str(cycle.target_generation or "").strip(),
            now=now,
        )
        outbox_state = enqueued.current_state

    # 5. Persist the bookkeeping if the decision mutates state (never on a zero-send).
    persisted = None
    if decision.mutates_state:
        persisted = store.advance(
            cycle.key,
            expected_revision=record.revision,
            next_phase=decision.next_phase,
            next_failure_count=decision.next_failure_count,
            last_disposition=decision.reason,
            escalated=decision.next_phase == RECONCILE_COORDINATOR_ESCALATION,
            callback_outbox_state=outbox_state,
            last_observed_runtime="",
            now=now,
        )

    return {
        "action": decision.action,
        "reason": decision.reason,
        "route": decision.route,
        "next_phase": decision.next_phase,
        "reconcile_failure_count": decision.next_failure_count,
        "sent": bool(decision.sends),
        "outbox_inserted": bool(enqueued.inserted) if enqueued is not None else False,
        "outbox_state": outbox_state,
        "persisted": bool(persisted.applied) if persisted is not None else False,
    }


def _kind_for(decision: ReconcileDecision) -> str:
    if decision.action == RECONCILE_ACTION_SELF_HEAL:
        return KIND_SELF_HEAL
    if decision.action == RECONCILE_ACTION_ESCALATE:
        return KIND_ESCALATION
    return str(decision.reason)  # deliver -> the gate-advanced reason token


__all__ = (
    "RECONCILE_SOURCE",
    "KIND_SELF_HEAL",
    "KIND_ESCALATION",
    "Observe",
    "ReconcileCycleInput",
    "outbox_key_for",
    "reconcile_once",
)
