"""Stateful workflow runtime — first vertical slice (Redmine #12857).

The advisory fill decision (#12855, :mod:`...domain.workflow_fill_decision`) and the
Redmine-aware admission preflight (#12856, :mod:`...domain.sublane_admission`) both
improve a *single* in-the-moment decision, but neither **remembers** anything: each call
is handed an already-assembled lane set and answers "dispatch or stop?". The operational
failures the spine (``vibes/docs/logics/coordinator-sublane-development-flow.md`` `###
背景` / `### 設計思想`) names — a lane that *looks* stalled, a missed ``review_request`` /
callback, an owner / integration drain that silently rots — are state-memory failures:
nothing folds the durable Redmine journal stream into "where is each lane now, and what
is the next action?".

This module is the **pure, advisory** first slice of that runtime. It is deliberately
small and reuses both existing authorities rather than inventing a third decision
vocabulary:

- :class:`LaneEvent` is one durable event a journal sweep already yields, keyed by a
  ``event_id`` durable anchor (the journal pointer ``issue:journal``). It carries the
  same lane facts #12856 :class:`LaneSignal` consumes.
- :func:`replay_events` folds an ordered event log into per-lane state with **duplicate
  suppression**: an ``event_id`` seen before is skipped, so replaying the same durable
  log is idempotent (the spine's "Redmine journal is the event source + durable anchor;
  duplicate suppression lives in the runtime"). Last accepted event per issue wins.
- :func:`evaluate_workflow_runtime` classifies the replayed state via the #12856
  :func:`classify_lane_state`, runs the #12856 admission/#12855 fill decision unchanged,
  and derives a per-lane :class:`LanePendingAction` read model plus one overall
  :class:`NextAction` — exactly the ``workflow.state`` + ``workflow.next_action`` the
  spine wants every workflow-aware command result to carry.

Scope boundaries (issue #12857 j#68572 — this is the first vertical slice, not the full
runtime) the policy **must not** cross:

- it discovers nothing and persists nothing — events are supplied / replayed by the
  caller, not polled from a live Redmine watcher (that watcher is #12672) and not written
  to a DB here (durable DB persistence is residual; the pure fold + ``event_id`` anchor
  is the MVP-level "replayable durable anchor");
- it never selects / creates a Redmine issue and never creates / adopts a lane;
- it does not bind workflow role to runtime provider (#12673) — ``owner_role`` is the
  abstract workflow role, never a ``codex`` / ``claude`` provider;
- it is **advisory only** — :attr:`WorkflowRuntimeState.advisory` is always true; no
  caller hard-blocks on it yet (advisory first, enforcement later).

The single most important invariant, carried from the spine / #12855 / #12856 unchanged:
an active ``implementing`` lane is **not** a stop reason. The overall next action never
returns a drain/hold target *past* a coordinator-blocking lane, but an ``implementing``
lane alone (with ready independent work and capacity) resolves to dispatching the next
sublane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    CALLBACK_NONE,
    GATE_NONE,
    REVIEW_PENDING,
    ClassifiedLane,
    LaneSignal,
    SublaneAdmissionInputs,
    SublaneAdmissionOutcome,
    evaluate_sublane_admission,
    render_admission_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FILL_DISPATCH_NEXT,
    FILL_STOP_COORDINATOR_BLOCKING,
    FILL_STOP_OWNER_OR_RELEASE_GATE,
    LANE_STATE_BLOCKED,
    LANE_STATE_CALLBACK_DELIVERY_FAILED,
    LANE_STATE_CALLBACK_DUE,
    LANE_STATE_CLOSE_WAITING,
    LANE_STATE_IDLE,
    LANE_STATE_IMPLEMENTING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_RETIRE_READY,
    LANE_STATE_REVIEW_WAITING,
)

# ---------------------------------------------------------------------------
# Workflow roles (abstract; literal regardless of UI language). The spine's `### 設計思想`
# keeps role as the source of truth and provider (codex / claude) as a binding (#12673);
# this slice only emits the role, never the provider.
# ---------------------------------------------------------------------------

ROLE_NONE = "none"
ROLE_COORDINATOR = "coordinator"
ROLE_AUDITOR = "auditor"
ROLE_IMPLEMENTER = "implementer"
ROLE_OWNER = "owner"

# ---------------------------------------------------------------------------
# Per-lane pending-action vocabulary (the owed action for one lane's state class). This
# is a read-model projection of the existing lane state classes — NOT a new decision
# vocabulary: each token is the action implied by exactly one :data:`LANE_STATE_*` class.
# ---------------------------------------------------------------------------

ACTION_NONE = "none"
ACTION_AWAIT_IMPLEMENTATION = "await_implementation"
ACTION_DELIVER_CALLBACK = "deliver_callback"
ACTION_REDELIVER_CALLBACK = "redeliver_callback"
ACTION_PERFORM_REVIEW = "perform_review"
ACTION_AGGREGATE_OWNER_APPROVAL = "aggregate_owner_approval"
ACTION_INTEGRATE = "integrate"
ACTION_CLOSE_ISSUE = "close_issue"
ACTION_RESOLVE_BLOCKER = "resolve_blocker"
ACTION_RETIRE_LANE = "retire_lane"

# Overall next-action-only tokens (no single owed lane drives them).
ACTION_DISPATCH_NEXT_SUBLANE = "dispatch_next_sublane"
ACTION_RESOLVE_OWNER_OR_RELEASE_GATE = "resolve_owner_or_release_gate"
ACTION_HOLD = "hold"

# state class -> (pending action token, owner role). The owner role is the abstract
# workflow actor who owns that action (never a runtime provider). ``implementing`` is
# owned by the implementer and is positive pipeline occupancy, never a coordinator stop.
_STATE_ACTION: dict[str, tuple[str, str]] = {
    LANE_STATE_IMPLEMENTING: (ACTION_AWAIT_IMPLEMENTATION, ROLE_IMPLEMENTER),
    LANE_STATE_CALLBACK_DUE: (ACTION_DELIVER_CALLBACK, ROLE_COORDINATOR),
    LANE_STATE_CALLBACK_DELIVERY_FAILED: (ACTION_REDELIVER_CALLBACK, ROLE_COORDINATOR),
    LANE_STATE_REVIEW_WAITING: (ACTION_PERFORM_REVIEW, ROLE_AUDITOR),
    LANE_STATE_OWNER_WAITING: (ACTION_AGGREGATE_OWNER_APPROVAL, ROLE_COORDINATOR),
    LANE_STATE_INTEGRATION_WAITING: (ACTION_INTEGRATE, ROLE_COORDINATOR),
    LANE_STATE_CLOSE_WAITING: (ACTION_CLOSE_ISSUE, ROLE_COORDINATOR),
    LANE_STATE_BLOCKED: (ACTION_RESOLVE_BLOCKER, ROLE_COORDINATOR),
    LANE_STATE_RETIRE_READY: (ACTION_RETIRE_LANE, ROLE_COORDINATOR),
    LANE_STATE_IDLE: (ACTION_NONE, ROLE_NONE),
}

# Drain order among coordinator-blocking lanes when selecting the single overall target.
# Mirrors the spine `### Drain Order` (the same order #12855 `_BLOCKING_DRAIN_PRIORITY`
# encodes for ``next_drain_action``): owner aggregation first, then review, integration,
# close, then blocked / callback states. Used only to pick *which* blocking lane the
# overall next action points at; the decision to stop-and-drain is the #12855 authority's.
_BLOCKING_TARGET_ORDER: tuple[str, ...] = (
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_REVIEW_WAITING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_CLOSE_WAITING,
    LANE_STATE_BLOCKED,
    LANE_STATE_CALLBACK_DUE,
    LANE_STATE_CALLBACK_DELIVERY_FAILED,
)


def pending_action_for(state_class: str) -> tuple[str, str]:
    """Map one :data:`LANE_STATE_*` class to its ``(action token, owner role)`` (pure).

    An unrecognized state class maps to :data:`ACTION_RESOLVE_BLOCKER` /
    :data:`ROLE_COORDINATOR` — fail-closed, consistent with #12856 treating an
    unreadable lane as coordinator-blocking rather than dispatchable.
    """
    return _STATE_ACTION.get(state_class, (ACTION_RESOLVE_BLOCKER, ROLE_COORDINATOR))


# ---------------------------------------------------------------------------
# Event model.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneEvent:
    """One durable lane event from the Redmine journal stream (never pane layout).

    ``event_id`` is the durable anchor that makes replay idempotent: the journal pointer
    (e.g. ``"12857:68572"``). Two events with the same ``event_id`` are the same durable
    fact; :func:`replay_events` keeps the first and suppresses the rest. ``issue`` is the
    lane's Redmine issue id. The remaining fields are exactly the #12856
    :class:`LaneSignal` facts the latest event for an issue contributes — they are read,
    never discovered.
    """

    event_id: str
    issue: str
    gate: str = GATE_NONE
    review_conclusion: str = REVIEW_PENDING
    callback_state: str = CALLBACK_NONE
    commit_bearing: bool = False
    integration_recorded: bool = False
    issue_open: bool = True
    blocker_recorded: bool = False

    def to_signal(self) -> LaneSignal:
        """Project this event onto the #12856 :class:`LaneSignal` it contributes."""
        return LaneSignal(
            issue=self.issue,
            latest_gate=self.gate,
            review_conclusion=self.review_conclusion,
            callback_state=self.callback_state,
            commit_bearing=self.commit_bearing,
            integration_recorded=self.integration_recorded,
            issue_open=self.issue_open,
            blocker_recorded=self.blocker_recorded,
        )


@dataclass(frozen=True)
class ReplayResult:
    """The folded state of one replayed event log (pure, replayable).

    ``signals`` is the per-lane latest :class:`LaneSignal` in first-seen issue order.
    ``applied_event_ids`` and ``suppressed_event_ids`` record the duplicate-suppression
    outcome so the fold is auditable: replaying the same log yields the same partition.
    """

    signals: tuple[LaneSignal, ...]
    applied_event_ids: tuple[str, ...]
    suppressed_event_ids: tuple[str, ...]

    @property
    def event_count(self) -> int:
        return len(self.applied_event_ids) + len(self.suppressed_event_ids)


def replay_events(events: Iterable[LaneEvent]) -> ReplayResult:
    """Fold an ordered durable event log into per-lane state (pure, idempotent).

    Duplicate suppression: the first event for each ``event_id`` is applied; any later
    event carrying an already-seen ``event_id`` is suppressed (the same durable fact is
    not folded twice). Among the *applied* events, the last one for a given issue wins —
    a lane's state is its most recent durable gate. The result is order-stable on the
    input and identical across replays of the same log, so the ``event_id`` set is the
    replayable durable anchor for the folded state.
    """
    seen: set[str] = set()
    applied: list[str] = []
    suppressed: list[str] = []
    # issue -> latest applied signal; insertion order preserved for first-seen ordering.
    latest: dict[str, LaneSignal] = {}

    for event in events:
        if event.event_id in seen:
            suppressed.append(event.event_id)
            continue
        seen.add(event.event_id)
        applied.append(event.event_id)
        latest[event.issue] = event.to_signal()

    return ReplayResult(
        signals=tuple(latest.values()),
        applied_event_ids=tuple(applied),
        suppressed_event_ids=tuple(suppressed),
    )


# ---------------------------------------------------------------------------
# Read model: per-lane pending action + one overall next action.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanePendingAction:
    """One lane's classified state plus the owed action / owner (read-model row)."""

    issue: str
    state_class: str
    action: str
    owner_role: str

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "state_class": self.state_class,
            "action": self.action,
            "owner_role": self.owner_role,
        }


@dataclass(frozen=True)
class NextAction:
    """The single overall next action the runtime recommends (advisory).

    ``action`` is one :data:`ACTION_*` token, ``owner_role`` the abstract workflow actor
    that owns it, ``target_issue`` the lane it concerns (empty for a dispatch / hold that
    targets no single lane), and ``reason`` the #12855/#12856 explanation it inherits.
    """

    action: str
    owner_role: str
    target_issue: str
    reason: str

    def as_payload(self) -> dict[str, object]:
        return {
            "action": self.action,
            "owner_role": self.owner_role,
            "target_issue": self.target_issue,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkflowRuntimeState:
    """The replayable, advisory ``workflow.state`` + ``workflow.next_action`` envelope.

    ``lane_actions`` is the per-lane read model (the spine's owner_role / lane /
    next_action projection). ``admission`` is the underlying #12856 outcome — the single
    authority for the fill/admission decision and the classified lane states.
    ``next_action`` is the one overall recommended action. ``applied_event_ids`` /
    ``suppressed_event_ids`` echo the replay's duplicate-suppression outcome so the state
    is reproducible from the durable anchor set. ``advisory`` is always true.
    """

    lane_actions: tuple[LanePendingAction, ...]
    admission: SublaneAdmissionOutcome
    next_action: NextAction
    applied_event_ids: tuple[str, ...]
    suppressed_event_ids: tuple[str, ...]
    advisory: bool = True

    @property
    def fill_decision(self) -> str:
        return self.admission.fill_decision

    @property
    def admission_decision(self) -> str:
        return self.admission.admission_decision

    def as_payload(self) -> dict[str, object]:
        return {
            "advisory": self.advisory,
            "next_action": self.next_action.as_payload(),
            "state": {
                "lane_actions": [row.as_payload() for row in self.lane_actions],
                "applied_event_ids": list(self.applied_event_ids),
                "suppressed_event_ids": list(self.suppressed_event_ids),
                "admission": self.admission.as_payload(),
            },
        }


def _select_blocking_target(
    classified: tuple[ClassifiedLane, ...],
) -> ClassifiedLane | None:
    """Pick the single coordinator-blocking lane the overall action points at.

    Uses the spine's drain order (:data:`_BLOCKING_TARGET_ORDER`); within the most urgent
    present class, the first lane in classified (replay) order. Returns None when no
    listed blocking class is present (the caller then falls through to its own default).
    """
    by_state: dict[str, ClassifiedLane] = {}
    for lane in classified:
        # First lane wins for each state class (stable on replay order).
        by_state.setdefault(lane.state_class, lane)
    for state_class in _BLOCKING_TARGET_ORDER:
        if state_class in by_state:
            return by_state[state_class]
    return None


def _derive_next_action(
    admission: SublaneAdmissionOutcome,
) -> NextAction:
    """Derive the one overall next action from the #12855/#12856 admission outcome (pure).

    The admission outcome is the single decision authority; this only maps it onto a
    coordinator-facing action + owner + target:

    - dispatch -> :data:`ACTION_DISPATCH_NEXT_SUBLANE` (no single target lane);
    - owner / release gate stop -> :data:`ACTION_RESOLVE_OWNER_OR_RELEASE_GATE`;
    - coordinator-blocking stop -> the most urgent blocking lane's own pending action
      (so the precise owed action — e.g. ``redeliver_callback`` vs ``resolve_blocker`` —
      and its owner / target issue surface, not a collapsed drain token);
    - any other stop (no ready work / overlap / capacity) -> the most urgent
      *non-blocking* owed lane action: retirement, else awaiting an implementing lane,
      else :data:`ACTION_HOLD`.
    """
    fill = admission.fill
    reason = fill.reason
    classified = admission.classified_lanes

    if admission.should_dispatch:
        return NextAction(ACTION_DISPATCH_NEXT_SUBLANE, ROLE_COORDINATOR, "", reason)

    if fill.fill_decision == FILL_STOP_OWNER_OR_RELEASE_GATE:
        return NextAction(
            ACTION_RESOLVE_OWNER_OR_RELEASE_GATE, ROLE_OWNER, "", reason
        )

    if fill.fill_decision == FILL_STOP_COORDINATOR_BLOCKING:
        target = _select_blocking_target(classified)
        if target is not None:
            action, owner = pending_action_for(target.state_class)
            return NextAction(action, owner, target.issue, reason)
        # Defensive: a blocking decision with no listed blocking class is unexpected;
        # fail-closed to a coordinator blocker resolution rather than a dispatch/hold.
        return NextAction(ACTION_RESOLVE_BLOCKER, ROLE_COORDINATOR, "", reason)

    # Remaining stops: no ready work / overlap / soft-profile full. Nothing is
    # coordinator-blocking, so surface the most useful non-blocking owed action.
    retire = next(
        (l for l in classified if l.state_class == LANE_STATE_RETIRE_READY), None
    )
    if retire is not None:
        action, owner = pending_action_for(retire.state_class)
        return NextAction(action, owner, retire.issue, reason)

    implementing = next(
        (l for l in classified if l.state_class == LANE_STATE_IMPLEMENTING), None
    )
    if implementing is not None:
        action, owner = pending_action_for(implementing.state_class)
        return NextAction(action, owner, implementing.issue, reason)

    return NextAction(ACTION_HOLD, ROLE_NONE, "", reason)


def evaluate_workflow_runtime(
    events: Iterable[LaneEvent],
    *,
    ready_independent_work: int = 0,
    ready_overlapping_work: int = 0,
    capacity_remaining: int = 0,
    owner_or_release_gate_active: bool = False,
) -> WorkflowRuntimeState:
    """Replay events into lane state and derive ``workflow.state`` + ``next_action``.

    The first vertical slice of the #12857 runtime, composed entirely from existing
    authorities:

    1. :func:`replay_events` folds the durable event log into per-lane signals with
       duplicate suppression (the runtime's memory + replayable anchor);
    2. the #12856 :func:`evaluate_sublane_admission` classifies each signal and runs the
       #12855 fill decision unchanged (the single decision authority);
    3. each classified lane gets its owed :class:`LanePendingAction`, and
       :func:`_derive_next_action` reduces the whole set to one overall :class:`NextAction`.

    The advisory ready-work / capacity / owner-gate inputs are passed straight through to
    the admission policy. The result is advisory and reproducible from the event log.
    """
    replay = replay_events(events)
    admission = evaluate_sublane_admission(
        SublaneAdmissionInputs(
            lane_signals=replay.signals,
            ready_independent_work=ready_independent_work,
            ready_overlapping_work=ready_overlapping_work,
            capacity_remaining=capacity_remaining,
            owner_or_release_gate_active=owner_or_release_gate_active,
        )
    )
    lane_actions = tuple(
        LanePendingAction(
            issue=lane.issue,
            state_class=lane.state_class,
            action=action,
            owner_role=owner,
        )
        for lane in admission.classified_lanes
        for action, owner in (pending_action_for(lane.state_class),)
    )
    return WorkflowRuntimeState(
        lane_actions=lane_actions,
        admission=admission,
        next_action=_derive_next_action(admission),
        applied_event_ids=replay.applied_event_ids,
        suppressed_event_ids=replay.suppressed_event_ids,
    )


def render_runtime_journal(state: WorkflowRuntimeState) -> str:
    """Render the runtime state as a replayable durable record (pure).

    Reuses the #12856 Bandwidth Record Template (the admission decision) and appends the
    runtime read model — the overall next action, its owner / target, and the
    duplicate-suppression outcome — so the advisory ``workflow.state`` /
    ``workflow.next_action`` is reproducible from the durable anchor set. Only issue ids,
    state classes, action / role tokens, and event-id anchors are emitted — never private
    paths or operator-specific cockpit details.
    """

    def _join(items: Iterable[str]) -> str:
        items = list(items)
        return ", ".join(items) if items else "none"

    next_action = state.next_action
    lines = [
        render_admission_journal(state.admission),
        "",
        "## Workflow runtime next action",
        "",
        f"- next_action: {next_action.action}",
        f"- owner_role: {next_action.owner_role}",
        f"- target_issue: {next_action.target_issue or 'none'}",
        "- lane_actions:",
    ]
    if state.lane_actions:
        lines.extend(
            f"  - {row.issue}: {row.state_class} -> {row.action} ({row.owner_role})"
            for row in state.lane_actions
        )
    else:
        lines.append("  - none")
    lines.extend(
        [
            f"- applied_event_ids: {_join(state.applied_event_ids)}",
            f"- suppressed_event_ids: {_join(state.suppressed_event_ids)}",
            f"- advisory: {str(state.advisory).lower()}",
        ]
    )
    return "\n".join(lines)


__all__ = (
    "ROLE_NONE",
    "ROLE_COORDINATOR",
    "ROLE_AUDITOR",
    "ROLE_IMPLEMENTER",
    "ROLE_OWNER",
    "ACTION_NONE",
    "ACTION_AWAIT_IMPLEMENTATION",
    "ACTION_DELIVER_CALLBACK",
    "ACTION_REDELIVER_CALLBACK",
    "ACTION_PERFORM_REVIEW",
    "ACTION_AGGREGATE_OWNER_APPROVAL",
    "ACTION_INTEGRATE",
    "ACTION_CLOSE_ISSUE",
    "ACTION_RESOLVE_BLOCKER",
    "ACTION_RETIRE_LANE",
    "ACTION_DISPATCH_NEXT_SUBLANE",
    "ACTION_RESOLVE_OWNER_OR_RELEASE_GATE",
    "ACTION_HOLD",
    "pending_action_for",
    "LaneEvent",
    "ReplayResult",
    "replay_events",
    "LanePendingAction",
    "NextAction",
    "WorkflowRuntimeState",
    "evaluate_workflow_runtime",
    "render_runtime_journal",
)
