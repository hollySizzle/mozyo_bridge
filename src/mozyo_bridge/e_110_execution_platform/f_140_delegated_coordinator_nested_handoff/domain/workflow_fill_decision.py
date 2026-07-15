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

Redmine #13756 added the second half of that invariant. A state class alone cannot say
*who owns the next action*, so ``review_waiting`` stopped the pipeline even when the
review had already been delivered to a dedicated same-lane gateway and a duplicate
main-coordinator review was forbidden. Two orthogonal axes now travel with each lane —
neither of which lies about the state class:

- **actionability / next-action owner** (:mod:`...domain.lane_actionability`): a lane
  whose next action is genuinely owned by a dedicated gateway / worker
  (``delegated_in_flight``), or which waits on a durable external condition
  (``non_actionable_wait``), occupies capacity but is **not** a coordinator stop reason.
  Every such claim must be earned — a failed delivery, a missing callback expectation,
  an overdue callback, or an unnameable owner all revert the lane to blocking (an ACK is
  not completion).
- **execution surface** (:mod:`...domain.lane_execution_surface`): only a *verified
  managed sublane* can make a non-blocking claim at all, and only verified managed
  sublanes are counted in the capacity projection. An internal task agent never consumes
  or fills sublane capacity (#13756 j#78320).

Both axes default to the fail-closed value, so a legacy ``LaneState(issue, state_class)``
behaves exactly as it did before #13756.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ACTIONABILITY_COORDINATOR_ACTIONABLE,
    ACTIONABILITY_DELEGATED_IN_FLIGHT,
    ACTIONABILITY_NON_ACTIONABLE_WAIT,
    ActionabilityClaim,
    ActionabilityVerdict,
    resolve_actionability,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (
    CapacityProjection,
    LaneProvenance,
    SurfaceItem,
    project_capacity,
    resolve_execution_surface,
)

# ---------------------------------------------------------------------------
# Lane state classes (machine-readable; literal regardless of UI language).
#
# Mirrors the spine's `### Lane State Classes`. The coordinator-blocking subset is
# the authority for "drain before opening optional work"; `implementing` is
# deliberately NOT in it.
# ---------------------------------------------------------------------------

LANE_STATE_IMPLEMENTING = "implementing"
LANE_STATE_CALLBACK_DUE = "callback_due"
LANE_STATE_CALLBACK_DELIVERY_FAILED = "callback_delivery_failed"
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
        LANE_STATE_CALLBACK_DELIVERY_FAILED,
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
        LANE_STATE_CALLBACK_DELIVERY_FAILED,
        LANE_STATE_REVIEW_WAITING,
        LANE_STATE_OWNER_WAITING,
        LANE_STATE_INTEGRATION_WAITING,
        LANE_STATE_CLOSE_WAITING,
        LANE_STATE_BLOCKED,
        LANE_STATE_RETIRE_READY,
        LANE_STATE_IDLE,
    }
)

# The blocking states the main coordinator owns **by construction** (#13756). No
# actionability claim delegates them away, because there is nobody else who could act:
#
# - `owner_waiting` — only the main coordinator aggregates owner approval;
# - `integration_waiting` — integration disposition is coordinator authority (a lane
#   implementer may push only its own issue / lane branch);
# - `close_waiting` — close needs owner approval the coordinator collects;
# - `callback_delivery_failed` — the delegation *failed to land*, so by definition
#   nothing is in flight and the send is back on the coordinator.
#
# The delegable blocking states are the rest: `review_waiting` (a review really can be
# owned by a dedicated same-lane gateway), `callback_due` (a short delegated in-flight
# window), and `blocked` (which may be waiting on a durable external condition).
MAIN_COORDINATOR_OWNED_STATES = frozenset(
    {
        LANE_STATE_OWNER_WAITING,
        LANE_STATE_INTEGRATION_WAITING,
        LANE_STATE_CLOSE_WAITING,
        LANE_STATE_CALLBACK_DELIVERY_FAILED,
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
# The fixed blocked result for "sublanes were asked for, but the high-level managed
# actuation rail is unavailable" (#13756 j#78320 item 4). The coordinator returns this
# instead of substituting internal task agents, direct edits, main-lane work, or bare
# worktrees for the sublanes it cannot open.
FILL_STOP_ACTUATION_UNAVAILABLE = "stop_actuation_unavailable"
# The fail-closed stop for a lane set that contains an unverifiable execution surface
# (#13756 j#78320 item 5; Review j#78471 finding 1). A lane whose surface claim is
# free-form or is a `managed_sublane` claim that did not verify resolves to
# `unknown` / `unverified_surface`. The coordinator cannot trust the capacity reasoning
# over a lane set it cannot classify, so it stops rather than dispatching past it. This
# does NOT fire for the legacy `unspecified` no-claim surface (that stays compatible).
FILL_STOP_UNVERIFIED_SURFACE = "stop_unverified_surface"

FILL_DECISIONS = frozenset(
    {
        FILL_DISPATCH_NEXT,
        FILL_STOP_NO_READY_WORK,
        FILL_STOP_OVERLAP,
        FILL_STOP_COORDINATOR_BLOCKING,
        FILL_STOP_SOFT_PROFILE_FULL,
        FILL_STOP_OWNER_OR_RELEASE_GATE,
        FILL_STOP_ACTUATION_UNAVAILABLE,
        FILL_STOP_UNVERIFIED_SURFACE,
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
    treated conservatively as coordinator-blocking (fail toward stopping rather than
    over-dispatching on a misread class).

    ``claim`` and ``provenance`` are the #13756 axes, and both default to the
    fail-closed value: a ``LaneState(issue, state_class)`` built by a pre-#13756 caller
    claims ``coordinator_actionable`` / ``main_coordinator`` on an ``unspecified``
    execution surface, which reproduces the old behaviour exactly — every blocking state
    blocks. Only an *explicit* claim, from a lane that presents verifiable managed-sublane
    provenance, can be non-blocking.
    """

    issue: str
    state_class: str
    claim: ActionabilityClaim = field(default_factory=ActionabilityClaim)
    provenance: LaneProvenance = field(default_factory=LaneProvenance)

    def state_is_coordinator_blocking(self) -> bool:
        """True when the *state class alone* would block (unknown classes included)."""
        # Unknown classes are treated as blocking (conservative): an unrecognized
        # lane state is more safely drained than dispatched past.
        return self.state_class not in LANE_STATES or is_coordinator_blocking(
            self.state_class
        )

    def state_is_main_owned(self) -> bool:
        """True when no actionability claim may delegate this state away.

        An unrecognized state class is main-owned too: if the class cannot be read, the
        claim attached to it cannot be trusted either, so no delegation rescues it.
        """
        return (
            self.state_class not in LANE_STATES
            or self.state_class in MAIN_COORDINATOR_OWNED_STATES
        )

    def verdict(self) -> ActionabilityVerdict:
        """Resolve this lane's effective actionability + blocking verdict (#13756)."""
        return resolve_actionability(
            self.claim,
            self.provenance,
            state_is_coordinator_blocking=self.state_is_coordinator_blocking(),
            state_is_main_owned=self.state_is_main_owned(),
        )

    def coordinator_blocking(self) -> bool:
        """True when this lane must be drained before opening optional new work.

        Post-#13756 this is the *resolved* verdict, not the raw state class: a
        ``review_waiting`` lane whose review is verifiably in flight on a dedicated
        gateway does not block. With the fail-closed defaults it is identical to the
        pre-#13756 state-class test.
        """
        return self.verdict().coordinator_blocking

    def as_record(self) -> dict[str, object]:
        """The per-item, machine-verifiable record for the durable decision template.

        #13756 j#78320 item 3 / Review j#78471 finding 4: a fill decision must carry each
        lane's full provenance so a later audit can replay the non-blocking claim, the
        duplicate-identity check, and the cap arithmetic. Every provenance field the
        caller supplied is echoed, alongside the resolved actionability verdict/reason and
        the resolved execution surface, so nothing about the verdict is left implicit.
        """
        verdict = self.verdict()
        provenance = self.provenance
        return {
            "issue": self.issue,
            "state_class": self.state_class,
            "actionability": verdict.actionability,
            "actionability_reason": verdict.reason,
            "coordinator_blocking": verdict.coordinator_blocking,
            "next_action_owner": self.claim.next_action_owner,
            "delivery_state": self.claim.delivery_state,
            "callback_expected": self.claim.callback_expected,
            "callback_overdue": self.claim.callback_overdue,
            "unblock_condition": self.claim.unblock_condition,
            "execution_surface_claim": provenance.execution_surface,
            "execution_surface_resolved": resolve_execution_surface(provenance),
            "workspace": provenance.workspace,
            "lane": provenance.lane,
            "issue_generation": provenance.issue_generation,
            "lifecycle_revision": provenance.lifecycle_revision,
            "durable_anchor": provenance.durable_anchor,
            "gateway_identity": provenance.gateway_identity,
            "worker_identity": provenance.worker_identity,
            "dispatch_ack": provenance.dispatch_ack,
        }


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
    # #13756 j#78320: the high-level managed-sublane actuation rail is available. When
    # false, the policy returns the fixed `stop_actuation_unavailable` blocked result —
    # it never degrades to "dispatch something else".
    managed_sublane_actuation_available: bool = True
    # Optional hard cap on concurrent managed sublanes (the repo-local soft profile's
    # `lane_count <= 10`). When set, it *lowers* the caller's `capacity_remaining` to
    # what the cap still allows, counting only verified managed sublanes — so internal
    # task agents can neither consume the cap nor be used to claim it is filled.
    sublane_hard_cap: int | None = None

    def coordinator_blocking_lanes(self) -> tuple[LaneState, ...]:
        return tuple(lane for lane in self.lanes if lane.coordinator_blocking())

    def delegated_in_flight_lanes(self) -> tuple[LaneState, ...]:
        return tuple(
            lane
            for lane in self.lanes
            if lane.verdict().actionability == ACTIONABILITY_DELEGATED_IN_FLIGHT
        )

    def non_actionable_wait_lanes(self) -> tuple[LaneState, ...]:
        return tuple(
            lane
            for lane in self.lanes
            if lane.verdict().actionability == ACTIONABILITY_NON_ACTIONABLE_WAIT
        )

    def implementing_lanes(self) -> tuple[LaneState, ...]:
        return tuple(
            lane for lane in self.lanes if lane.state_class == LANE_STATE_IMPLEMENTING
        )

    def retire_ready_lanes(self) -> tuple[LaneState, ...]:
        return tuple(
            lane for lane in self.lanes if lane.state_class == LANE_STATE_RETIRE_READY
        )

    def capacity_projection(self) -> CapacityProjection:
        """The verified execution-surface projection (#13756 j#78320 item 2).

        The only authority for a narrated lane count. Built from each lane's *resolved*
        blocking verdict, so "worker confirmed productive" cannot include a lane the
        coordinator still owes work on.
        """
        return project_capacity(
            SurfaceItem(
                provenance=lane.provenance,
                coordinator_blocking=lane.coordinator_blocking(),
            )
            for lane in self.lanes
        )

    def effective_capacity_remaining(self) -> int:
        """``capacity_remaining``, further limited by the managed-sublane hard cap.

        The cap counts **verified managed sublanes only**. A coordinator that ran five
        internal task agents has not consumed a single sublane slot — and equally cannot
        present them as five filled slots.
        """
        capacity = self.capacity_remaining
        if self.sublane_hard_cap is None:
            return capacity
        resident = self.capacity_projection().resident_managed_sublanes
        return min(capacity, self.sublane_hard_cap - resident)


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
    # #13756: the lanes that are occupied but are *not* coordinator stop reasons. They
    # are echoed so the journal can show what the coordinator deliberately did not stop
    # for, and by whose authority.
    delegated_in_flight: tuple[str, ...] = ()
    non_actionable_wait: tuple[str, ...] = ()
    # #13756 j#78320: the verified execution-surface counts. The only honest source for a
    # narrated lane count.
    capacity_projection: CapacityProjection = field(default_factory=CapacityProjection)
    # #13756 j#78320 item 3 / Review j#78471 finding 4: the per-lane provenance records
    # (one :meth:`LaneState.as_record` dict per input lane), so a later audit can replay
    # every non-blocking claim, the duplicate-identity check, and the cap arithmetic from
    # the durable decision alone.
    lanes: tuple[dict[str, object], ...] = ()

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
            "delegated_in_flight": list(self.delegated_in_flight),
            "non_actionable_wait": list(self.non_actionable_wait),
            "ready_independent_work": self.ready_independent_work,
            "capacity_remaining": self.capacity_remaining,
            "capacity_projection": self.capacity_projection.as_payload(),
            "lanes": [dict(record) for record in self.lanes],
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
    (LANE_STATE_CALLBACK_DELIVERY_FAILED, NEXT_DRAIN_BLOCKER),
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
    """Resolve the advisory Post-Dispatch Fill Loop decision (pure, #12855 / #13756).

    Precedence (most-blocking first), matching the spine's admission + drain order:

    1. an active owner-decision / release / credential / destructive gate ->
       :data:`FILL_STOP_OWNER_OR_RELEASE_GATE` (the admission rule forbids opening
       lower-priority optional work while such a gate is active);
    2. the managed-sublane actuation rail is unavailable ->
       :data:`FILL_STOP_ACTUATION_UNAVAILABLE` (#13756 j#78320: a fixed blocked result;
       the coordinator must not substitute task agents / direct edits / main-lane work /
       bare worktrees for the sublanes it cannot open);
    3. the lane set contains an unverifiable execution surface ->
       :data:`FILL_STOP_UNVERIFIED_SURFACE` (#13756 j#78320 item 5: a free-form or
       failed-verification surface fails closed; the coordinator will not reason about a
       lane set it cannot classify);
    4. any lane whose next action the **main coordinator** owns (review / owner /
       integration / close / blocked / callback_due that is not verifiably delegated) ->
       :data:`FILL_STOP_COORDINATOR_BLOCKING` (drain first);
    5. no ready independent work, but ready work that overlaps an active lane ->
       :data:`FILL_STOP_OVERLAP` (serialize on the dependency);
    6. no ready work at all -> :data:`FILL_STOP_NO_READY_WORK`;
    7. ready independent work exists but capacity (soft profile, further limited by the
       managed-sublane hard cap) is exhausted -> :data:`FILL_STOP_SOFT_PROFILE_FULL`;
    8. otherwise -> :data:`FILL_DISPATCH_NEXT`.

    Step 3 is where both halves of the invariant live.
    :meth:`FillDecisionInputs.coordinator_blocking_lanes` excludes ``implementing`` (a
    lane set of only ``implementing`` lanes with ready work and capacity dispatches), and
    since #13756 it also excludes lanes whose next action is verifiably owned by a
    dedicated gateway / worker (``delegated_in_flight``) or by a durable external
    condition (``non_actionable_wait``). Those lanes still occupy capacity and still show
    up in the projection — they simply are not work the main coordinator can do, so they
    cannot be a reason for it to stop.
    """
    implementing = tuple(lane.issue for lane in inputs.implementing_lanes())
    blocking_lanes = inputs.coordinator_blocking_lanes()
    blocking = tuple(lane.issue for lane in blocking_lanes)
    delegated = tuple(lane.issue for lane in inputs.delegated_in_flight_lanes())
    waiting = tuple(lane.issue for lane in inputs.non_actionable_wait_lanes())
    retire_ready = inputs.retire_ready_lanes()
    projection = inputs.capacity_projection()
    capacity_remaining = inputs.effective_capacity_remaining()
    lane_records = tuple(lane.as_record() for lane in inputs.lanes)

    def outcome(decision: str, reason: str, next_drain: str) -> FillDecisionOutcome:
        return FillDecisionOutcome(
            fill_decision=decision,
            reason=reason,
            next_drain_action=next_drain,
            active_implementing=implementing,
            coordinator_blocking=blocking,
            delegated_in_flight=delegated,
            non_actionable_wait=waiting,
            ready_independent_work=inputs.ready_independent_work,
            capacity_remaining=capacity_remaining,
            capacity_projection=projection,
            lanes=lane_records,
        )

    if inputs.owner_or_release_gate_active:
        return outcome(
            FILL_STOP_OWNER_OR_RELEASE_GATE,
            "an owner-decision / release / credential / destructive gate is active; "
            "do not open lower-priority optional work until it is resolved",
            NEXT_DRAIN_OWNER,
        )

    if not inputs.managed_sublane_actuation_available:
        return outcome(
            FILL_STOP_ACTUATION_UNAVAILABLE,
            "the high-level managed-sublane actuation rail is unavailable; report zero "
            "productive sublanes rather than substituting internal task agents, direct "
            "edits, main-lane work, or bare worktrees",
            NEXT_DRAIN_BLOCKER,
        )

    if projection.unverified_surface > 0:
        return outcome(
            FILL_STOP_UNVERIFIED_SURFACE,
            "the lane set contains "
            f"{projection.unverified_surface} lane(s) with an unverifiable execution "
            "surface (free-form, or a managed-sublane claim whose provenance did not "
            "verify, or an ambiguous duplicate identity); the coordinator will not "
            "dispatch over a lane set it cannot classify",
            NEXT_DRAIN_BLOCKER,
        )

    if blocking_lanes:
        return outcome(
            FILL_STOP_COORDINATOR_BLOCKING,
            "lanes whose next action the main coordinator owns must be drained before "
            "opening new work: "
            + ", ".join(
                f"{lane.issue}={lane.state_class}({lane.verdict().reason})"
                for lane in blocking_lanes
            ),
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

    if capacity_remaining <= 0:
        return outcome(
            FILL_STOP_SOFT_PROFILE_FULL,
            "the local soft profile has no remaining capacity for another active "
            "implementation sublane",
            retire_drain,
        )

    return outcome(
        FILL_DISPATCH_NEXT,
        "no lane needs a main-coordinator action and ready independent work remains "
        "within the soft profile; neither an active implementing lane nor a verifiably "
        "delegated / externally-waiting lane is a stop reason",
        NEXT_DRAIN_NONE,
    )


__all__ = (
    "LANE_STATE_IMPLEMENTING",
    "LANE_STATE_CALLBACK_DUE",
    "LANE_STATE_CALLBACK_DELIVERY_FAILED",
    "LANE_STATE_REVIEW_WAITING",
    "LANE_STATE_OWNER_WAITING",
    "LANE_STATE_INTEGRATION_WAITING",
    "LANE_STATE_CLOSE_WAITING",
    "LANE_STATE_BLOCKED",
    "LANE_STATE_RETIRE_READY",
    "LANE_STATE_IDLE",
    "LANE_STATES",
    "COORDINATOR_BLOCKING_STATES",
    "MAIN_COORDINATOR_OWNED_STATES",
    "FILL_DISPATCH_NEXT",
    "FILL_STOP_NO_READY_WORK",
    "FILL_STOP_OVERLAP",
    "FILL_STOP_COORDINATOR_BLOCKING",
    "FILL_STOP_SOFT_PROFILE_FULL",
    "FILL_STOP_OWNER_OR_RELEASE_GATE",
    "FILL_STOP_ACTUATION_UNAVAILABLE",
    "FILL_STOP_UNVERIFIED_SURFACE",
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
