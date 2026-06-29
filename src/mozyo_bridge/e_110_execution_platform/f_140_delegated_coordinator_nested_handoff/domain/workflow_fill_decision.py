"""Advisory Post-Dispatch Fill Loop / sublane fill decision (Redmine #12855).

The coordinator-sublane spine
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### Post-Dispatch
Fill Loop`) defines a fixed vocabulary for "after a dispatch / drain, do I fill the
pipeline with another sublane, or stop — and if I stop, for which concrete reason?".
Until now that vocabulary lived only in docs, so when an agent did not read (or
forgot) the spine, a sublane could *look* stalled even though the documented rule
said to dispatch the next independent lane. This module puts the decision on a
machine-readable command surface.

It is the **pure, advisory** policy. Given an already-classified summary of the
active lane set (each lane's :data:`LANE_STATE_*` class), the count of ready
independent / overlapping implementation work, the local soft-profile capacity that
remains, and whether an owner / release / credential / destructive gate is active,
:func:`evaluate_fill_decision` returns one fixed :data:`FILL_*` token plus the
concrete next drain action.

Scope boundaries (issue #12855 j#68506) the policy **must not** cross:

- it never selects or creates a Redmine backlog issue (the ``ready_*_work`` counts
  are supplied by the caller, not discovered here);
- it never creates or adopts a lane / worktree;
- it is **advisory only** — :attr:`FillDecisionOutcome.advisory` is always true and
  no caller is meant to hard-block a handoff on it (MVP; Redmine-aware preflight is
  #12856, DB-backed runtime is #12857).

The single most important invariant, stated explicitly because the operational
failure it guards against is real: an active ``implementing`` lane is **not** a stop
reason. :func:`is_coordinator_blocking` excludes it, so a lane set of only
``implementing`` lanes (with ready independent work and remaining capacity) resolves
to :data:`FILL_DISPATCH_NEXT`, never to a stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Lane state classes (machine-readable; literal regardless of UI language).
#
# Mirrors the spine's `### Lane State Classes`. The coordinator-blocking subset is
# the authority for "drain before opening optional work"; `implementing` is
# deliberately NOT in it.
# ---------------------------------------------------------------------------

LANE_STATE_IMPLEMENTING = "implementing"
LANE_STATE_CALLBACK_DUE = "callback_due"
LANE_STATE_REVIEW_WAITING = "review_waiting"
LANE_STATE_OWNER_WAITING = "owner_waiting"
LANE_STATE_INTEGRATION_WAITING = "integration_waiting"
LANE_STATE_CLOSE_WAITING = "close_waiting"
LANE_STATE_BLOCKED = "blocked"
LANE_STATE_RETIRE_READY = "retire_ready"
LANE_STATE_IDLE = "idle"

# The coordinator-blocking states (spine `### Lane State Classes`): these must be
# drained before opening optional new work. `implementing` / `retire_ready` / `idle`
# are intentionally excluded — `implementing` is positive pipeline occupancy, not a
# stop reason.
COORDINATOR_BLOCKING_STATES = frozenset(
    {
        LANE_STATE_CALLBACK_DUE,
        LANE_STATE_REVIEW_WAITING,
        LANE_STATE_OWNER_WAITING,
        LANE_STATE_INTEGRATION_WAITING,
        LANE_STATE_CLOSE_WAITING,
        LANE_STATE_BLOCKED,
    }
)

LANE_STATES = frozenset(
    {
        LANE_STATE_IMPLEMENTING,
        LANE_STATE_CALLBACK_DUE,
        LANE_STATE_REVIEW_WAITING,
        LANE_STATE_OWNER_WAITING,
        LANE_STATE_INTEGRATION_WAITING,
        LANE_STATE_CLOSE_WAITING,
        LANE_STATE_BLOCKED,
        LANE_STATE_RETIRE_READY,
        LANE_STATE_IDLE,
    }
)


# ---------------------------------------------------------------------------
# Fill decision vocabulary (spine `### Post-Dispatch Fill Loop`; the `fill_decision`
# field of the Bandwidth Record Template). One fixed token per terminal outcome.
# ---------------------------------------------------------------------------

FILL_DISPATCH_NEXT = "dispatch_next"
FILL_STOP_NO_READY_WORK = "stop_no_ready_work"
FILL_STOP_OVERLAP = "stop_overlap"
FILL_STOP_COORDINATOR_BLOCKING = "stop_coordinator_blocking"
FILL_STOP_SOFT_PROFILE_FULL = "stop_soft_profile_full"
FILL_STOP_OWNER_OR_RELEASE_GATE = "stop_owner_or_release_gate"

FILL_DECISIONS = frozenset(
    {
        FILL_DISPATCH_NEXT,
        FILL_STOP_NO_READY_WORK,
        FILL_STOP_OVERLAP,
        FILL_STOP_COORDINATOR_BLOCKING,
        FILL_STOP_SOFT_PROFILE_FULL,
        FILL_STOP_OWNER_OR_RELEASE_GATE,
    }
)

# ``next_drain_action`` — the Bandwidth Record Template's drain pointer, derived from
# the decision + lane set so the advisory output maps straight onto the journal field.
NEXT_DRAIN_NONE = "none"
NEXT_DRAIN_REVIEW = "review"
NEXT_DRAIN_OWNER = "owner_aggregation"
NEXT_DRAIN_INTEGRATION = "integration"
NEXT_DRAIN_CLOSE = "close"
NEXT_DRAIN_BLOCKER = "blocker"
NEXT_DRAIN_RETIREMENT = "retirement"


def is_coordinator_blocking(state_class: str) -> bool:
    """True when a lane in ``state_class`` must be drained before optional new work.

    The authority for the spine's "drain first" rule. Critically, an
    ``implementing`` lane is **not** coordinator-blocking: it is positive pipeline
    occupancy, so a lane set of only ``implementing`` lanes never forces a stop.
    ``retire_ready`` / ``idle`` are likewise not blocking (they are cleanup signals,
    not drain-before-dispatch states).
    """
    return state_class in COORDINATOR_BLOCKING_STATES


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneState:
    """One active lane, classified from the durable record (never from pane layout).

    ``issue`` is the lane's Redmine issue id (display / journal pointer only).
    ``state_class`` is one of :data:`LANE_STATE_*`. An unrecognized ``state_class`` is
    treated conservatively as coordinator-blocking by :func:`evaluate_fill_decision`
    (fail toward stopping rather than over-dispatching on a misread class).
    """

    issue: str
    state_class: str

    def coordinator_blocking(self) -> bool:
        # Unknown classes are treated as blocking (conservative): an unrecognized
        # lane state is more safely drained than dispatched past.
        return self.state_class not in LANE_STATES or is_coordinator_blocking(
            self.state_class
        )


@dataclass(frozen=True)
class FillDecisionInputs:
    """The already-classified lane-set summary the fill policy decides from.

    Every field is supplied by the caller (an operator / coordinator) — the policy
    discovers nothing. ``ready_independent_work`` is the count of ready
    implementation work items that do **not** overlap an active lane;
    ``ready_overlapping_work`` is the count of ready work that does overlap
    (file / invariant / merge-order). ``capacity_remaining`` is the slot count left
    within the local soft profile. ``owner_or_release_gate_active`` is true when an
    owner-decision / release / credential / destructive-operation gate is active.
    """

    lanes: tuple[LaneState, ...] = ()
    ready_independent_work: int = 0
    ready_overlapping_work: int = 0
    capacity_remaining: int = 0
    owner_or_release_gate_active: bool = False

    def coordinator_blocking_lanes(self) -> tuple[LaneState, ...]:
        return tuple(lane for lane in self.lanes if lane.coordinator_blocking())

    def implementing_lanes(self) -> tuple[LaneState, ...]:
        return tuple(
            lane for lane in self.lanes if lane.state_class == LANE_STATE_IMPLEMENTING
        )

    def retire_ready_lanes(self) -> tuple[LaneState, ...]:
        return tuple(
            lane for lane in self.lanes if lane.state_class == LANE_STATE_RETIRE_READY
        )


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillDecisionOutcome:
    """The replayable, advisory result of one Post-Dispatch Fill Loop evaluation.

    ``fill_decision`` is the fixed :data:`FILL_*` token (the spine's `fill_decision`
    field). ``next_drain_action`` is the :data:`NEXT_DRAIN_*` pointer. ``reason`` is a
    short human explanation. ``advisory`` is always true: this output is informational
    and must not be used to hard-block a handoff (MVP; #12855).

    The ``active_implementing`` / ``coordinator_blocking`` issue lists and the
    ``ready_independent_work`` / ``capacity_remaining`` counts are echoed so the output
    maps directly onto the Bandwidth Record Template a coordinator journals.
    """

    fill_decision: str
    reason: str
    next_drain_action: str
    active_implementing: tuple[str, ...]
    coordinator_blocking: tuple[str, ...]
    ready_independent_work: int
    capacity_remaining: int
    advisory: bool = True

    @property
    def should_dispatch(self) -> bool:
        return self.fill_decision == FILL_DISPATCH_NEXT

    def as_payload(self) -> dict[str, object]:
        return {
            "fill_decision": self.fill_decision,
            "reason": self.reason,
            "next_drain_action": self.next_drain_action,
            "active_implementing": list(self.active_implementing),
            "coordinator_blocking": list(self.coordinator_blocking),
            "ready_independent_work": self.ready_independent_work,
            "capacity_remaining": self.capacity_remaining,
            "advisory": self.advisory,
            "should_dispatch": self.should_dispatch,
        }


# Drain-order priority among coordinator-blocking states (spine `### Drain Order`):
# owner_waiting first, then review, integration, close, then blocked / callback_due.
_BLOCKING_DRAIN_PRIORITY: tuple[tuple[str, str], ...] = (
    (LANE_STATE_OWNER_WAITING, NEXT_DRAIN_OWNER),
    (LANE_STATE_REVIEW_WAITING, NEXT_DRAIN_REVIEW),
    (LANE_STATE_INTEGRATION_WAITING, NEXT_DRAIN_INTEGRATION),
    (LANE_STATE_CLOSE_WAITING, NEXT_DRAIN_CLOSE),
    (LANE_STATE_BLOCKED, NEXT_DRAIN_BLOCKER),
    (LANE_STATE_CALLBACK_DUE, NEXT_DRAIN_BLOCKER),
)


def _blocking_drain_action(blocking: Iterable[LaneState]) -> str:
    """The most urgent drain action across the present coordinator-blocking lanes."""
    present = {lane.state_class for lane in blocking}
    for state_class, action in _BLOCKING_DRAIN_PRIORITY:
        if state_class in present:
            return action
    # All blocking lanes carry unrecognized state classes: a generic blocker drain.
    return NEXT_DRAIN_BLOCKER


def evaluate_fill_decision(inputs: FillDecisionInputs) -> FillDecisionOutcome:
    """Resolve the advisory Post-Dispatch Fill Loop decision (pure, #12855).

    Precedence (most-blocking first), matching the spine's admission + drain order:

    1. an active owner-decision / release / credential / destructive gate ->
       :data:`FILL_STOP_OWNER_OR_RELEASE_GATE` (the admission rule forbids opening
       lower-priority optional work while such a gate is active);
    2. any coordinator-blocking lane (review / owner / integration / close / blocked /
       callback_due) -> :data:`FILL_STOP_COORDINATOR_BLOCKING` (drain first);
    3. no ready independent work, but ready work that overlaps an active lane ->
       :data:`FILL_STOP_OVERLAP` (serialize on the dependency);
    4. no ready work at all -> :data:`FILL_STOP_NO_READY_WORK`;
    5. ready independent work exists but the soft profile has no capacity ->
       :data:`FILL_STOP_SOFT_PROFILE_FULL`;
    6. otherwise -> :data:`FILL_DISPATCH_NEXT`.

    Step 2 is where "an ``implementing`` lane alone is not a stop reason" is enforced:
    :meth:`FillDecisionInputs.coordinator_blocking_lanes` excludes ``implementing``,
    so a lane set of only ``implementing`` lanes (with ready independent work and
    capacity) reaches step 6 and dispatches.
    """
    implementing = tuple(lane.issue for lane in inputs.implementing_lanes())
    blocking_lanes = inputs.coordinator_blocking_lanes()
    blocking = tuple(lane.issue for lane in blocking_lanes)
    retire_ready = inputs.retire_ready_lanes()

    def outcome(decision: str, reason: str, next_drain: str) -> FillDecisionOutcome:
        return FillDecisionOutcome(
            fill_decision=decision,
            reason=reason,
            next_drain_action=next_drain,
            active_implementing=implementing,
            coordinator_blocking=blocking,
            ready_independent_work=inputs.ready_independent_work,
            capacity_remaining=inputs.capacity_remaining,
        )

    if inputs.owner_or_release_gate_active:
        return outcome(
            FILL_STOP_OWNER_OR_RELEASE_GATE,
            "an owner-decision / release / credential / destructive gate is active; "
            "do not open lower-priority optional work until it is resolved",
            NEXT_DRAIN_OWNER,
        )

    if blocking_lanes:
        return outcome(
            FILL_STOP_COORDINATOR_BLOCKING,
            "coordinator-blocking lanes must be drained before opening new work: "
            + ", ".join(f"{lane.issue}={lane.state_class}" for lane in blocking_lanes),
            _blocking_drain_action(blocking_lanes),
        )

    retire_drain = NEXT_DRAIN_RETIREMENT if retire_ready else NEXT_DRAIN_NONE

    if inputs.ready_independent_work <= 0:
        if inputs.ready_overlapping_work > 0:
            return outcome(
                FILL_STOP_OVERLAP,
                "the only ready work overlaps an active lane "
                "(file / invariant / merge order); serialize rather than dispatch",
                retire_drain,
            )
        return outcome(
            FILL_STOP_NO_READY_WORK,
            "no ready independent implementation work remains to dispatch",
            retire_drain,
        )

    if inputs.capacity_remaining <= 0:
        return outcome(
            FILL_STOP_SOFT_PROFILE_FULL,
            "the local soft profile has no remaining capacity for another active "
            "implementation sublane",
            retire_drain,
        )

    return outcome(
        FILL_DISPATCH_NEXT,
        "coordinator-blocking states are drained and ready independent work remains "
        "within the soft profile; an active implementing lane is not a stop reason",
        NEXT_DRAIN_NONE,
    )


__all__ = (
    "LANE_STATE_IMPLEMENTING",
    "LANE_STATE_CALLBACK_DUE",
    "LANE_STATE_REVIEW_WAITING",
    "LANE_STATE_OWNER_WAITING",
    "LANE_STATE_INTEGRATION_WAITING",
    "LANE_STATE_CLOSE_WAITING",
    "LANE_STATE_BLOCKED",
    "LANE_STATE_RETIRE_READY",
    "LANE_STATE_IDLE",
    "LANE_STATES",
    "COORDINATOR_BLOCKING_STATES",
    "FILL_DISPATCH_NEXT",
    "FILL_STOP_NO_READY_WORK",
    "FILL_STOP_OVERLAP",
    "FILL_STOP_COORDINATOR_BLOCKING",
    "FILL_STOP_SOFT_PROFILE_FULL",
    "FILL_STOP_OWNER_OR_RELEASE_GATE",
    "FILL_DECISIONS",
    "NEXT_DRAIN_NONE",
    "NEXT_DRAIN_REVIEW",
    "NEXT_DRAIN_OWNER",
    "NEXT_DRAIN_INTEGRATION",
    "NEXT_DRAIN_CLOSE",
    "NEXT_DRAIN_BLOCKER",
    "NEXT_DRAIN_RETIREMENT",
    "is_coordinator_blocking",
    "LaneState",
    "FillDecisionInputs",
    "FillDecisionOutcome",
    "evaluate_fill_decision",
)
