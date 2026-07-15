"""Event-driven turn/gate reconciliation — pure state machine (Redmine #13758).

The heart of the event-driven reconciler: after a runtime ``turn_ended`` **wake edge**
(or a bounded deadline tick), re-read the Redmine durable gate at the exact dispatch
anchor / lane generation and decide, with a fixed vocabulary and fail-closed defaults,
one of: deliver a coordinator callback once (the expected gate advanced), send a bounded
same-lane self-heal to the ``expected_next_owner`` (the gate is outstanding and Redmine
did not move), escalate to the coordinator once (the self-heal ladder is exhausted), do
nothing (no outstanding gate / already terminal), or zero-send (fail-closed: unreadable /
stale generation / ambiguous route).

This module is **pure** — total functions over already-resolved observations, free of
I/O, the live inventory, the Redmine client, and the CAS store — so every branch is pinned
by tests that touch no process and no DB. The application half (:mod:`...application.` …)
supplies the observations from the existing supervisor lease / wake, the callback outbox,
and the Redmine re-read, and actuates the decision through the **existing** callback
outbox (no second outbox / ledger, j#79337).

Two conceptual chains (issue description):

- self-heal ladder — ``turn_active -> turn_ended_gate_pending -> self_heal_attempt_1 ->
  self_heal_attempt_2 -> coordinator_escalation``;
- delivery — ``gate_recorded -> callback_pending -> notified``.

The delivery chain (``gate_recorded -> callback_pending -> notified``) is already owned by
the callback outbox FSM (``core/state/callback_outbox.py``); this module owns the self-heal
ladder and the decision of *deliver vs self-heal vs escalate vs zero-send* per reconcile
cycle. A gate that advances mid-turn is delivered promptly by the existing outbox
**discovery** path (acceptance §2) — this machine does not delay it — and a reconcile cycle
that later re-observes the advanced gate simply confirms and closes (routing through the
same outbox key, which is idempotent).

Design authority: j#78002 (state model / retry policy / fail-closed boundary), j#78056 /
j#79337 (implementation request), owner intent j#78280 (Herdr signal is a wake hint,
Redmine journal is workflow truth), doctrine ``vibes/docs/logics/ack-completion-receiver-
state.md`` (turn/ACK are wake evidence, never promoted to a workflow gate) and
``vibes/docs/logics/managed-state-model.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authority import (
    TARGET_TURN_ENDED,
)

# ---------------------------------------------------------------------------
# Reconcile lifecycle phases (the persisted self-heal-ladder state).
# ---------------------------------------------------------------------------
#: A dispatch is live and the target's turn has not ended — nothing to reconcile yet.
RECONCILE_TURN_ACTIVE = "turn_active"
#: The turn ended but the expected durable gate has not been observed — a reconcile cycle
#: is due (re-read Redmine at the exact anchor).
RECONCILE_TURN_ENDED_GATE_PENDING = "turn_ended_gate_pending"
#: One bounded same-lane self-heal has been sent to the ``expected_next_owner``.
RECONCILE_SELF_HEAL_1 = "self_heal_attempt_1"
#: A second (final) bounded self-heal has been sent to the ``expected_next_owner``.
RECONCILE_SELF_HEAL_2 = "self_heal_attempt_2"
#: The self-heal ladder is exhausted — the anomaly has been escalated to the coordinator
#: once; further no-progress cycles are suppressed (acceptance §5 "以後 duplicate 抑止").
RECONCILE_COORDINATOR_ESCALATION = "coordinator_escalation"
#: The expected gate advanced and the coordinator callback was **enqueued** (the outbox row
#: exists), but its durable delivery is not yet confirmed. This is the ``callback_pending``
#: step of the issue's ``gate_recorded -> callback_pending -> notified`` chain: enqueue is
#: not delivery (``ack-completion-receiver-state.md``), so the record does not read
#: ``notified`` until the outbox durably ``delivered`` the row (Redmine #13758 review F4).
RECONCILE_CALLBACK_PENDING = "callback_pending"
#: The expected gate advanced and the coordinator callback was durably DELIVERED once
#: (the outbox row reached ``delivered``) — terminal.
RECONCILE_NOTIFIED = "notified"
#: The reconcile closed with no callback owed (no outstanding gate, or an explicit
#: deferral / hibernate / retire) — terminal.
RECONCILE_CLOSED = "closed"

RECONCILE_PHASES = frozenset(
    {
        RECONCILE_TURN_ACTIVE,
        RECONCILE_TURN_ENDED_GATE_PENDING,
        RECONCILE_SELF_HEAL_1,
        RECONCILE_SELF_HEAL_2,
        RECONCILE_COORDINATOR_ESCALATION,
        RECONCILE_CALLBACK_PENDING,
        RECONCILE_NOTIFIED,
        RECONCILE_CLOSED,
    }
)

#: The two terminal phases — a reconcile record here owes no further action for its
#: current dispatch anchor (a new anchor / generation starts a fresh record).
_TERMINAL_PHASES = frozenset({RECONCILE_NOTIFIED, RECONCILE_CLOSED})

# ---------------------------------------------------------------------------
# Decision actions (what the application half must actuate).
# ---------------------------------------------------------------------------
#: No send. The phase may still advance to a terminal (``closed``) — e.g. no outstanding
#: gate, or a duplicate of an already-notified / already-escalated cycle.
RECONCILE_ACTION_NONE = "none"
#: Deliver the coordinator callback exactly once through the existing outbox (the expected
#: gate advanced). Route is the coordinator.
RECONCILE_ACTION_DELIVER = "deliver_coordinator"
#: Send one bounded same-lane self-heal to the ``expected_next_owner`` (NOT hard-coded to a
#: reviewer). Route is the resolved expected owner.
RECONCILE_ACTION_SELF_HEAL = "self_heal"
#: Escalate the anomaly to the coordinator once. Route is the coordinator.
RECONCILE_ACTION_ESCALATE = "escalate_coordinator"
#: Fail-closed: send nothing and mutate no persisted state (the record is left byte-
#: unchanged). Redmine unreadable, generation unknown / mismatched, or route ambiguous —
#: never guess a lane (acceptance §8).
RECONCILE_ACTION_ZERO_SEND = "zero_send"

RECONCILE_ACTIONS = frozenset(
    {
        RECONCILE_ACTION_NONE,
        RECONCILE_ACTION_DELIVER,
        RECONCILE_ACTION_SELF_HEAL,
        RECONCILE_ACTION_ESCALATE,
        RECONCILE_ACTION_ZERO_SEND,
    }
)

# ---------------------------------------------------------------------------
# Decision reasons (machine-readable fixed tokens; literal regardless of UI language).
# ---------------------------------------------------------------------------
# zero-send (fail-closed)
REASON_REDMINE_UNREADABLE = "reconcile_redmine_unreadable"
REASON_GENERATION_MISMATCH = "reconcile_generation_mismatch"
REASON_ROUTE_AMBIGUOUS = "reconcile_route_ambiguous"
# deliver
REASON_GATE_ADVANCED = "reconcile_gate_advanced"
# none
REASON_CALLBACK_DELIVERED = "reconcile_callback_delivered"
REASON_NO_OUTSTANDING_GATE = "reconcile_no_outstanding_gate"
REASON_TERMINAL_DISPOSITION = "reconcile_terminal_disposition"
REASON_ALREADY_NOTIFIED = "reconcile_already_notified"
REASON_ALREADY_ESCALATED = "reconcile_already_escalated"
REASON_NOT_TURN_END_EDGE = "reconcile_not_turn_end_edge"
# self-heal
REASON_SELF_HEAL_1 = "reconcile_self_heal_attempt_1"
REASON_SELF_HEAL_2 = "reconcile_self_heal_attempt_2"
# escalate
REASON_THREE_STRIKE = "reconcile_three_strike"
REASON_DEADLINE_EXCEEDED = "reconcile_deadline_exceeded"
REASON_SELF_HEAL_UNCERTAIN = "reconcile_self_heal_uncertain"

# ---------------------------------------------------------------------------
# Generation-correlation status of the re-read (exact action / review generation).
# ---------------------------------------------------------------------------
#: The re-read correlated at the exact dispatch anchor / lane generation.
GEN_MATCH = "match"
#: The live generation does not match the reconcile record's generation (superseded /
#: recycled) — a stale reconcile; zero-send (never act on a stale generation).
GEN_MISMATCH = "mismatch"
#: The generation could not be established (blank / unknown) — zero-send, never guess.
GEN_UNKNOWN = "unknown"

GEN_STATUSES = frozenset({GEN_MATCH, GEN_MISMATCH, GEN_UNKNOWN})

# ---------------------------------------------------------------------------
# Route-resolution status of the ``expected_next_owner`` (self-heal target).
# ---------------------------------------------------------------------------
#: The expected next owner resolves to exactly one same-lane semantic receiver.
ROUTE_RESOLVED = "resolved"
#: The expected next owner cannot be resolved (unknown owner) — zero-send.
ROUTE_UNRESOLVED = "unresolved"
#: More than one candidate receiver — never guess which; zero-send.
ROUTE_AMBIGUOUS = "ambiguous"

ROUTE_STATUSES = frozenset({ROUTE_RESOLVED, ROUTE_UNRESOLVED, ROUTE_AMBIGUOUS})

#: The self-heal ladder cap: two bounded self-heals, the third no-progress cycle escalates.
SELF_HEAL_MAX_ATTEMPTS = 2
#: The coordinator route label (the existing callback-outbox coordinator route).
COORDINATOR_ROUTE = "coordinator"


def _norm(value: object) -> str:
    return str(value or "").strip()


def is_turn_end_edge(prior_runtime: object, observed_runtime: object) -> bool:
    """Is ``observed_runtime`` a real busy->turn_ended *edge*, not a persistent re-observation?

    Policy §1 (issue description): the ``busy -> turn_ended`` transition is a wake hint, but
    a persistent ``turn_ended`` (``done``) level re-observed on a later snapshot is NOT a new
    event and must not increment any counter or re-trigger a reconcile cycle. The edge is:
    the observed runtime is :data:`~...dispatch_authority.TARGET_TURN_ENDED` AND the prior
    runtime was something else. When the prior runtime was already ``turn_ended`` (the level
    persists until the next input), this returns ``False`` — the re-observation is folded.

    A blank / unknown prior is treated as "not turn_ended", so the FIRST time a fresh
    reconciler observes ``turn_ended`` it is an edge (there is a real transition into the
    level from an unobserved prior). Duplicate-wake suppression for that first edge is the
    application's job (the supervisor-wake PK coalescing), not this predicate's.
    """
    return _norm(observed_runtime) == TARGET_TURN_ENDED and (
        _norm(prior_runtime) != TARGET_TURN_ENDED
    )


@dataclass(frozen=True)
class ReconcileObservation:
    """The action-time facts a reconcile cycle re-reads before deciding. (all fail-closed)

    Every field defaults to the *unsafe-blocking* side so a missing observation zero-sends
    or does nothing rather than guessing a lane:

    - ``redmine_readable`` — the Redmine durable record was re-read successfully (workflow
      truth). ``False`` -> zero-send (never degrade an unreadable authority to "gate absent"
      and self-heal blind).
    - ``generation_status`` — the correlation of the re-read against the record's exact
      dispatch anchor / lane generation (:data:`GEN_MATCH` / :data:`GEN_MISMATCH` /
      :data:`GEN_UNKNOWN`). Anything but :data:`GEN_MATCH` zero-sends.
    - ``gate_advanced`` — the expected handoff-worthy gate is now recorded at the exact
      anchor (the durable record moved). Delivered once to the coordinator. Effective only
      together with ``advanced_gate_journal`` (a gate cannot be delivered without its exact
      source journal — Redmine #13758 review F3).
    - ``advanced_gate`` / ``advanced_gate_journal`` — the EXACT gate token and Redmine journal
      id the advanced gate was recorded at (the gate's own ``## Gate:`` journal). The
      coordinator-callback outbox key is keyed on BOTH so it is byte-identical to the discovery
      path's key for the same gate (one delivery, never two — acceptance §1 / review F3): the
      discovery path keys on the actual gate marker, so the deliver key must use the actual
      advanced gate token, not the canonical expected one. An empty journal with
      ``gate_advanced`` is treated as "not advanced yet" (fail-closed: never deliver anchorless).
    - ``callback_delivered`` — the coordinator callback for this gate has DURABLY delivered
      (the outbox deliver-key row reached ``delivered``). Only then does the record advance to
      ``notified``; an enqueued-but-undelivered callback stays ``callback_pending`` (enqueue is
      not delivery — review F4).
    - ``has_outstanding_gate`` — a gate was expected for this dispatch (there is something a
      turn end should have produced). ``False`` + not advanced -> a plain turn-end / ack with
      nothing owed -> no notify (acceptance §6).
    - ``terminal_disposition`` — the lane was explicitly deferred / hibernated / retired; the
      reconcile closes (attempt state is closed, §5 end).
    - ``deadline_exceeded`` — the bounded self-heal deadline elapsed with no progress -> the
      anomaly escalates (§4/§5).
    - ``prior_send_uncertain`` — the previous self-heal send is in the outbox ``uncertain``
      state (ACK-only / crash after send). Never blind-resend; surface to the coordinator
      (acceptance §8 "uncertain send は … coordinator に可視化").
    - ``route_status`` / ``expected_next_owner`` — the same-lane self-heal target resolution.
      A non-:data:`ROUTE_RESOLVED` status (or a blank owner) zero-sends a self-heal (never
      guess the receiver). Escalation to the coordinator is unaffected (the coordinator route
      is its own fail-closed resolution).
    """

    redmine_readable: bool = False
    generation_status: str = GEN_UNKNOWN
    gate_advanced: bool = False
    advanced_gate: str = ""
    advanced_gate_journal: str = ""
    callback_delivered: bool = False
    has_outstanding_gate: bool = False
    terminal_disposition: bool = False
    deadline_exceeded: bool = False
    prior_send_uncertain: bool = False
    route_status: str = ROUTE_UNRESOLVED
    expected_next_owner: str = ""

    @property
    def gate_advanced_effective(self) -> bool:
        """A gate is deliverable only with its exact source journal (review F3, fail-closed)."""
        return bool(self.gate_advanced) and bool(_norm(self.advanced_gate_journal))


@dataclass(frozen=True)
class ReconcileDecision:
    """The pure decision. ``action`` is the only send/no-send authority.

    - ``route`` — the callback route for a send (:data:`COORDINATOR_ROUTE` for
      deliver/escalate, the ``expected_next_owner`` for a self-heal, else ``""``).
    - ``next_phase`` — the phase to persist. On :data:`RECONCILE_ACTION_ZERO_SEND`,
      ``mutates_state`` is ``False`` and ``next_phase`` echoes the input phase (the record is
      left byte-unchanged).
    - ``next_failure_count`` — the edge-based reconcile-failure counter to persist
      (incremented only on a completed no-progress cycle, never per snapshot; §5).
    - ``mutates_state`` — whether the application must CAS-persist the new phase / counter. A
      zero-send never mutates; every other decision does.
    """

    action: str
    reason: str
    next_phase: str
    next_failure_count: int
    route: str = ""
    mutates_state: bool = True

    @property
    def is_zero_send(self) -> bool:
        return self.action == RECONCILE_ACTION_ZERO_SEND

    @property
    def sends(self) -> bool:
        return self.action in (
            RECONCILE_ACTION_DELIVER,
            RECONCILE_ACTION_SELF_HEAL,
            RECONCILE_ACTION_ESCALATE,
        )


def advance_reconcile(
    *,
    phase: str,
    reconcile_failure_count: int,
    observation: ReconcileObservation,
) -> ReconcileDecision:
    """Decide one reconcile cycle. (pure, fail-closed, ordered)

    Called once per completed reconcile cycle — a real ``turn_ended`` wake edge (see
    :func:`is_turn_end_edge`; duplicate wakes are folded upstream by the supervisor-wake PK)
    or a bounded deadline tick. The ordering is deliberate: the most fundamental fail-closed
    guards first (an unreadable authority or a stale generation can never be acted on), then
    the terminal / progress outcomes, then the self-heal ladder. No branch defaults to a
    send; the counter increments only on the completed no-progress cycle (edge-based, §5),
    and a zero-send mutates nothing.
    """
    current = _norm(phase)
    count = int(reconcile_failure_count)

    # 1. Fundamental fail-closed guards. An unreadable Redmine or a non-matching generation
    #    is never acted on and never mutates the record (acceptance §8; owner intent j#78280
    #    "Redmine structured journal がworkflow truth").
    if not observation.redmine_readable:
        return _zero_send(current, count, REASON_REDMINE_UNREADABLE)
    if _norm(observation.generation_status) != GEN_MATCH:
        return _zero_send(current, count, REASON_GENERATION_MISMATCH)

    # 2. An explicit deferral / hibernate / retire closes the attempt (§5 end). Checked before
    #    the gate outcomes: a retired lane owes no callback even if a stale gate lingers.
    if observation.terminal_disposition:
        return _none(RECONCILE_CLOSED, count, REASON_TERMINAL_DISPOSITION)

    # 3. The expected gate advanced -> deliver the coordinator callback exactly once
    #    (acceptance §1). Enqueue is not delivery (review F4): a gate-advanced cycle enqueues
    #    and moves to ``callback_pending``; only a subsequent cycle that observes the outbox
    #    row DURABLY ``delivered`` (``callback_delivered``) advances to ``notified``. An
    #    already-notified record does not re-deliver (the outbox UNIQUE fence also guards the
    #    wire). A gate with no exact source journal is not deliverable (review F3, fail-closed).
    if observation.gate_advanced_effective:
        if current == RECONCILE_NOTIFIED:
            return _none(RECONCILE_NOTIFIED, count, REASON_ALREADY_NOTIFIED)
        if observation.callback_delivered:
            # The outbox durably delivered the coordinator callback -> terminal notified.
            return _none(RECONCILE_NOTIFIED, count, REASON_CALLBACK_DELIVERED)
        return ReconcileDecision(
            action=RECONCILE_ACTION_DELIVER,
            reason=REASON_GATE_ADVANCED,
            next_phase=RECONCILE_CALLBACK_PENDING,
            next_failure_count=count,
            route=COORDINATOR_ROUTE,
        )

    # 4. No outstanding gate -> a plain turn-end / ack with nothing owed -> no notify
    #    (acceptance §6). The reconcile closes.
    if not observation.has_outstanding_gate:
        return _none(RECONCILE_CLOSED, count, REASON_NO_OUTSTANDING_GATE)

    # 5. Outstanding gate, no durable progress. Once escalated, suppress further escalation
    #    (acceptance §5 "以後 duplicate 抑止") — a terminal no-op, no counter change.
    if current == RECONCILE_COORDINATOR_ESCALATION:
        return _none(RECONCILE_COORDINATOR_ESCALATION, count, REASON_ALREADY_ESCALATED)

    # 5a. A prior self-heal whose delivery is uncertain is never blind-resent; the anomaly is
    #     surfaced to the coordinator (acceptance §8). This pre-empts the ladder.
    if observation.prior_send_uncertain:
        return _escalate(count, REASON_SELF_HEAL_UNCERTAIN)

    # 5b. The edge-based counter increments on this completed no-progress cycle (§5). The
    #     deadline elapsing OR reaching the 2-attempt cap escalates once; otherwise a bounded
    #     self-heal to the expected owner.
    next_count = count + 1
    if observation.deadline_exceeded:
        return _escalate(max(next_count, SELF_HEAL_MAX_ATTEMPTS + 1), REASON_DEADLINE_EXCEEDED)
    if next_count > SELF_HEAL_MAX_ATTEMPTS:
        return _escalate(next_count, REASON_THREE_STRIKE)

    # 5c. A self-heal must resolve to exactly one same-lane receiver; otherwise zero-send
    #     (never guess a lane, acceptance §8). Escalation above is unaffected (coordinator
    #     route is its own fail-closed resolution).
    if _norm(observation.route_status) != ROUTE_RESOLVED or not _norm(
        observation.expected_next_owner
    ):
        return _zero_send(current, count, REASON_ROUTE_AMBIGUOUS)

    if next_count == 1:
        next_phase, reason = RECONCILE_SELF_HEAL_1, REASON_SELF_HEAL_1
    else:  # next_count == 2 (== SELF_HEAL_MAX_ATTEMPTS)
        next_phase, reason = RECONCILE_SELF_HEAL_2, REASON_SELF_HEAL_2
    return ReconcileDecision(
        action=RECONCILE_ACTION_SELF_HEAL,
        reason=reason,
        next_phase=next_phase,
        next_failure_count=next_count,
        route=_norm(observation.expected_next_owner),
    )


def _zero_send(phase: str, count: int, reason: str) -> ReconcileDecision:
    """Fail-closed: send nothing, mutate nothing (the record is left byte-unchanged)."""
    return ReconcileDecision(
        action=RECONCILE_ACTION_ZERO_SEND,
        reason=reason,
        next_phase=phase,
        next_failure_count=count,
        route="",
        mutates_state=False,
    )


def _none(next_phase: str, count: int, reason: str) -> ReconcileDecision:
    """No send, but persist a (possibly terminal) phase transition."""
    return ReconcileDecision(
        action=RECONCILE_ACTION_NONE,
        reason=reason,
        next_phase=next_phase,
        next_failure_count=count,
        route="",
    )


def _escalate(count: int, reason: str) -> ReconcileDecision:
    return ReconcileDecision(
        action=RECONCILE_ACTION_ESCALATE,
        reason=reason,
        next_phase=RECONCILE_COORDINATOR_ESCALATION,
        next_failure_count=count,
        route=COORDINATOR_ROUTE,
    )


__all__ = (
    "RECONCILE_TURN_ACTIVE",
    "RECONCILE_TURN_ENDED_GATE_PENDING",
    "RECONCILE_SELF_HEAL_1",
    "RECONCILE_SELF_HEAL_2",
    "RECONCILE_COORDINATOR_ESCALATION",
    "RECONCILE_CALLBACK_PENDING",
    "RECONCILE_NOTIFIED",
    "RECONCILE_CLOSED",
    "RECONCILE_PHASES",
    "RECONCILE_ACTION_NONE",
    "RECONCILE_ACTION_DELIVER",
    "RECONCILE_ACTION_SELF_HEAL",
    "RECONCILE_ACTION_ESCALATE",
    "RECONCILE_ACTION_ZERO_SEND",
    "RECONCILE_ACTIONS",
    "REASON_REDMINE_UNREADABLE",
    "REASON_GENERATION_MISMATCH",
    "REASON_ROUTE_AMBIGUOUS",
    "REASON_GATE_ADVANCED",
    "REASON_CALLBACK_DELIVERED",
    "REASON_NO_OUTSTANDING_GATE",
    "REASON_TERMINAL_DISPOSITION",
    "REASON_ALREADY_NOTIFIED",
    "REASON_ALREADY_ESCALATED",
    "REASON_NOT_TURN_END_EDGE",
    "REASON_SELF_HEAL_1",
    "REASON_SELF_HEAL_2",
    "REASON_THREE_STRIKE",
    "REASON_DEADLINE_EXCEEDED",
    "REASON_SELF_HEAL_UNCERTAIN",
    "GEN_MATCH",
    "GEN_MISMATCH",
    "GEN_UNKNOWN",
    "GEN_STATUSES",
    "ROUTE_RESOLVED",
    "ROUTE_UNRESOLVED",
    "ROUTE_AMBIGUOUS",
    "ROUTE_STATUSES",
    "SELF_HEAL_MAX_ATTEMPTS",
    "COORDINATOR_ROUTE",
    "is_turn_end_edge",
    "ReconcileObservation",
    "ReconcileDecision",
    "advance_reconcile",
)
