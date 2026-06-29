"""Redmine-aware sublane admission / fill preflight (Redmine #12856).

The advisory Post-Dispatch Fill Loop policy (#12855,
:mod:`...domain.workflow_fill_decision`) decides "given an *already-classified* lane
set, dispatch another sublane or stop — and for which concrete reason?". It
deliberately left the hard part to the caller: turning what a lane's Redmine
issue/journal actually says into one of the :data:`LANE_STATE_*` classes. In practice
that classification is exactly where a coordinator mis-stops a healthy pipeline (it
reads an ``implementing`` lane as if it were blocking) or, worse, dispatches new work
past an unread ``review_request`` / ``callback_delivery_failed``.

This module closes that gap. It is the **pure, advisory** Redmine-aware preflight:

- :func:`classify_lane_state` maps a :class:`LaneSignal` (the durable-record facts a
  journal sweep already yields — latest gate kind, review conclusion, callback state,
  commit/integration disposition, issue open/closed) onto exactly one
  :data:`LANE_STATE_*` class, mirroring the spine's `### Lane State Classes`. It is
  **fail-closed**: an unrecognized gate, or any signal it cannot place, classifies to
  :data:`LANE_STATE_BLOCKED` (coordinator-blocking) so the preflight stops rather than
  dispatches past an unreadable lane.
- :func:`evaluate_sublane_admission` classifies every supplied lane signal, then feeds
  the resulting lane set straight into the #12855
  :func:`evaluate_fill_decision`, so the admission/fill decision and its concrete
  :data:`FILL_*` stop reason come from the single existing authority — this module adds
  the classification, not a second decision vocabulary.

Scope boundaries (issue #12856 j#68548) the policy **must not** cross:

- it discovers nothing — every :class:`LaneSignal` and the ready-work / capacity /
  owner-gate inputs are supplied by the caller from the durable record, not read live
  here (live Redmine discovery / persistence is #12857; a Redmine watcher is a
  non-goal);
- it never selects / creates a Redmine issue and never creates / adopts a lane;
- it is **advisory only** — :attr:`SublaneAdmissionOutcome.advisory` is always true and
  no caller is meant to hard-block a handoff on it yet (MVP; the spine's `###
  Implementation Request Preflight` connection comes after the classification is
  proven stable in real use).

The single most important invariant, carried over from the spine and #12855: an active
``implementing`` lane is **not** a stop reason. A lane whose latest gate is ``start`` /
``progress`` (or a ``review`` that requested changes, sending work back to the
implementer) classifies to :data:`LANE_STATE_IMPLEMENTING`, which
:func:`evaluate_fill_decision` excludes from the coordinator-blocking set; so a lane set
of only ``implementing`` lanes with ready independent work and capacity resolves to
:data:`FILL_DISPATCH_NEXT`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FILL_DISPATCH_NEXT,
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
    FillDecisionInputs,
    FillDecisionOutcome,
    LaneState,
    evaluate_fill_decision,
)

# ---------------------------------------------------------------------------
# Durable-gate vocabulary (machine-readable; literal regardless of UI language).
#
# The latest gate kind recorded on a lane's Redmine issue is the primary signal for
# its state class. These mirror the governed gate names (`### Gate Schema`); the policy
# only needs the subset that moves a lane between state classes.
# ---------------------------------------------------------------------------

GATE_NONE = "none"
GATE_START = "start"
GATE_PROGRESS = "progress"
GATE_IMPLEMENTATION_DONE = "implementation_done"
GATE_REVIEW_REQUEST = "review_request"
GATE_REVIEW = "review"
GATE_OWNER_CLOSE_APPROVAL = "owner_close_approval"
GATE_CLOSE = "close"
GATE_BLOCKED = "blocked"

GATE_KINDS = frozenset(
    {
        GATE_NONE,
        GATE_START,
        GATE_PROGRESS,
        GATE_IMPLEMENTATION_DONE,
        GATE_REVIEW_REQUEST,
        GATE_REVIEW,
        GATE_OWNER_CLOSE_APPROVAL,
        GATE_CLOSE,
        GATE_BLOCKED,
    }
)

# Review conclusion (only consulted when the latest gate is ``review``). ``approved``
# moves the lane forward to owner aggregation; ``changes_requested`` sends it back to
# the implementer (so it is ``implementing``, not blocking); ``pending`` means the
# audit itself is still owed.
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_CHANGES_REQUESTED = "changes_requested"

REVIEW_CONCLUSIONS = frozenset(
    {REVIEW_PENDING, REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED}
)

# Callback state (mirrors the spine's ``$callback_sweep`` classification). A dispatch
# happened but the expected callback / durable gate is missing or failed.
CALLBACK_NONE = "none"
CALLBACK_DUE = "due"
CALLBACK_DELIVERY_FAILED = "delivery_failed"

CALLBACK_STATES = frozenset({CALLBACK_NONE, CALLBACK_DUE, CALLBACK_DELIVERY_FAILED})


# ---------------------------------------------------------------------------
# Input: one lane's durable-record facts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneSignal:
    """The durable-record facts for one active lane (never pane layout).

    Every field is a fact a journal sweep already produces; the policy reads them, it
    does not discover them.

    ``issue`` is the lane's Redmine issue id (display / journal pointer only).
    ``latest_gate`` is one of :data:`GATE_*` — the most recent gate recorded on the
    issue. ``review_conclusion`` is consulted only when ``latest_gate`` is
    :data:`GATE_REVIEW`. ``callback_state`` is one of :data:`CALLBACK_*`.
    ``commit_bearing`` marks work that produced commits (so it can be
    ``integration_waiting`` until merged / pushed / patch-equivalent / explicitly
    deferred). ``integration_recorded`` is true once that integration disposition (or a
    no-commit determination) is in the durable record. ``issue_open`` reflects the
    Redmine issue status (open vs closed). ``blocker_recorded`` marks a recorded
    blocker / failed handoff / unresolved dependency.
    """

    issue: str
    latest_gate: str = GATE_NONE
    review_conclusion: str = REVIEW_PENDING
    callback_state: str = CALLBACK_NONE
    commit_bearing: bool = False
    integration_recorded: bool = False
    issue_open: bool = True
    blocker_recorded: bool = False


def classify_lane_state(signal: LaneSignal) -> str:
    """Classify one :class:`LaneSignal` into a :data:`LANE_STATE_*` class (pure).

    Mirrors the spine's `### Lane State Classes`. Precedence is most-blocking /
    most-specific first so a lane lands in exactly one class:

    1. a recorded blocker (or a ``blocked`` gate) -> :data:`LANE_STATE_BLOCKED`;
    2. ``callback_state`` of ``delivery_failed`` ->
       :data:`LANE_STATE_CALLBACK_DELIVERY_FAILED`; of ``due`` ->
       :data:`LANE_STATE_CALLBACK_DUE` (a dispatch happened but the expected callback /
       durable gate is missing);
    3. a ``start`` / ``progress`` gate, or a ``review`` that requested changes (work is
       back with the implementer) -> :data:`LANE_STATE_IMPLEMENTING` (**not** blocking);
    4. ``implementation_done`` / ``review_request``, or a ``review`` still pending ->
       :data:`LANE_STATE_REVIEW_WAITING` (Codex audit owed);
    5. a ``review`` approved -> :data:`LANE_STATE_OWNER_WAITING` (owner aggregation is
       the next coordinator-only action; the integration concern surfaces after owner
       approval is recorded);
    6. ``owner_close_approval`` recorded -> :data:`LANE_STATE_INTEGRATION_WAITING` if
       the work is commit-bearing and integration is not yet recorded, else
       :data:`LANE_STATE_CLOSE_WAITING` while the issue is still open, else
       :data:`LANE_STATE_RETIRE_READY`;
    7. a ``close`` gate -> :data:`LANE_STATE_INTEGRATION_WAITING` for a closed issue
       whose commit-bearing work is still unmerged (the spine's "closed issue with only
       unmerged sublane commits is integration_waiting, not retire_ready"), else
       :data:`LANE_STATE_RETIRE_READY`;
    8. no gate -> :data:`LANE_STATE_IDLE`.

    Fail-closed: any gate outside :data:`GATE_KINDS` classifies to
    :data:`LANE_STATE_BLOCKED` so the preflight drains an unreadable lane rather than
    dispatching past it.
    """
    gate = signal.latest_gate

    # 1. explicit blocker (gate or recorded blocker) — most blocking.
    if signal.blocker_recorded or gate == GATE_BLOCKED:
        return LANE_STATE_BLOCKED

    # 2. callback failure / due — a dispatch happened but the durable pointer is broken.
    if signal.callback_state == CALLBACK_DELIVERY_FAILED:
        return LANE_STATE_CALLBACK_DELIVERY_FAILED
    if signal.callback_state == CALLBACK_DUE:
        return LANE_STATE_CALLBACK_DUE

    # 3. actively implementing (positive pipeline occupancy, never a stop reason).
    if gate in (GATE_START, GATE_PROGRESS):
        return LANE_STATE_IMPLEMENTING
    if gate == GATE_REVIEW and signal.review_conclusion == REVIEW_CHANGES_REQUESTED:
        return LANE_STATE_IMPLEMENTING

    # 4. Codex audit owed.
    if gate in (GATE_IMPLEMENTATION_DONE, GATE_REVIEW_REQUEST):
        return LANE_STATE_REVIEW_WAITING
    if gate == GATE_REVIEW and signal.review_conclusion == REVIEW_PENDING:
        return LANE_STATE_REVIEW_WAITING

    # 5. review approved — owner aggregation is the next coordinator-only action.
    if gate == GATE_REVIEW and signal.review_conclusion == REVIEW_APPROVED:
        return LANE_STATE_OWNER_WAITING

    # 6. owner close approval recorded — integration, then close, then retirement.
    if gate == GATE_OWNER_CLOSE_APPROVAL:
        if signal.commit_bearing and not signal.integration_recorded:
            return LANE_STATE_INTEGRATION_WAITING
        if signal.issue_open:
            return LANE_STATE_CLOSE_WAITING
        return LANE_STATE_RETIRE_READY

    # 7. close gate — retire_ready unless commit-bearing work is still unmerged.
    if gate == GATE_CLOSE:
        if signal.commit_bearing and not signal.integration_recorded:
            return LANE_STATE_INTEGRATION_WAITING
        return LANE_STATE_RETIRE_READY

    # 8. no gate — no active durable work.
    if gate == GATE_NONE:
        return LANE_STATE_IDLE

    # Fail-closed: an unrecognized gate is drained, not dispatched past.
    return LANE_STATE_BLOCKED


# ---------------------------------------------------------------------------
# Inputs to the admission/fill preflight.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SublaneAdmissionInputs:
    """The caller-supplied facts the admission/fill preflight decides from.

    ``lane_signals`` are the durable-record facts for each active lane (classified
    here, unlike #12855 which takes pre-classified lanes). The remaining fields are the
    same advisory inputs :class:`FillDecisionInputs` consumes and are passed straight
    through: ``ready_independent_work`` / ``ready_overlapping_work`` are the counts of
    ready implementation work that does / does not overlap an active lane,
    ``capacity_remaining`` is the slots left within the local soft profile, and
    ``owner_or_release_gate_active`` is true when an owner-decision / release /
    credential / destructive gate is active.
    """

    lane_signals: tuple[LaneSignal, ...] = ()
    ready_independent_work: int = 0
    ready_overlapping_work: int = 0
    capacity_remaining: int = 0
    owner_or_release_gate_active: bool = False


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------

# ``admission_decision`` — the spine's Bandwidth Record Template field. Derived from the
# fill decision so the advisory output maps straight onto the journal: a dispatch maps
# to ``dispatch_sublane``, any concrete stop maps to ``stop_and_drain``.
ADMISSION_DISPATCH_SUBLANE = "dispatch_sublane"
ADMISSION_STOP_AND_DRAIN = "stop_and_drain"


@dataclass(frozen=True)
class ClassifiedLane:
    """A lane signal paired with the :data:`LANE_STATE_*` class it classified to."""

    issue: str
    state_class: str

    def as_payload(self) -> dict[str, object]:
        return {"issue": self.issue, "state_class": self.state_class}


@dataclass(frozen=True)
class SublaneAdmissionOutcome:
    """The replayable, advisory result of one Redmine-aware admission/fill preflight.

    ``classified_lanes`` is the per-lane classification (the Bandwidth Record
    Template's ``current_lanes``). ``fill`` is the underlying #12855
    :class:`FillDecisionOutcome` (the authority for ``fill_decision`` /
    ``next_drain_action`` and the concrete stop reason). ``admission_decision`` is the
    derived :data:`ADMISSION_*` token. ``advisory`` is always true: the preflight is
    informational and must not hard-block a handoff yet (MVP; #12856).
    """

    classified_lanes: tuple[ClassifiedLane, ...]
    fill: FillDecisionOutcome
    admission_decision: str
    advisory: bool = True

    @property
    def should_dispatch(self) -> bool:
        return self.fill.should_dispatch

    @property
    def fill_decision(self) -> str:
        return self.fill.fill_decision

    @property
    def reason(self) -> str:
        return self.fill.reason

    @property
    def next_drain_action(self) -> str:
        return self.fill.next_drain_action

    def as_payload(self) -> dict[str, object]:
        return {
            "admission_decision": self.admission_decision,
            "advisory": self.advisory,
            "should_dispatch": self.should_dispatch,
            "classified_lanes": [lane.as_payload() for lane in self.classified_lanes],
            "fill": self.fill.as_payload(),
        }


def evaluate_sublane_admission(
    inputs: SublaneAdmissionInputs,
) -> SublaneAdmissionOutcome:
    """Classify each lane signal, then resolve the admission/fill decision (pure).

    The Redmine-aware step is :func:`classify_lane_state` on every supplied
    :class:`LaneSignal`; the decision itself is delegated unchanged to the #12855
    :func:`evaluate_fill_decision`, so there is exactly one decision authority and one
    :data:`FILL_*` vocabulary. ``admission_decision`` is derived from that result.
    """
    classified = tuple(
        ClassifiedLane(issue=signal.issue, state_class=classify_lane_state(signal))
        for signal in inputs.lane_signals
    )
    fill = evaluate_fill_decision(
        FillDecisionInputs(
            lanes=tuple(
                LaneState(issue=lane.issue, state_class=lane.state_class)
                for lane in classified
            ),
            ready_independent_work=inputs.ready_independent_work,
            ready_overlapping_work=inputs.ready_overlapping_work,
            capacity_remaining=inputs.capacity_remaining,
            owner_or_release_gate_active=inputs.owner_or_release_gate_active,
        )
    )
    admission_decision = (
        ADMISSION_DISPATCH_SUBLANE
        if fill.fill_decision == FILL_DISPATCH_NEXT
        else ADMISSION_STOP_AND_DRAIN
    )
    return SublaneAdmissionOutcome(
        classified_lanes=classified,
        fill=fill,
        admission_decision=admission_decision,
    )


def render_admission_journal(outcome: SublaneAdmissionOutcome) -> str:
    """Render the outcome as the spine's Bandwidth Record Template (pure).

    Produces the `### Bandwidth Record Template` markdown a coordinator pastes into the
    Redmine dispatch-decision journal, so the advisory output is replayable in the
    durable record. Only issue IDs and state classes are emitted — never private paths
    or operator-specific cockpit details (the template's own constraint).
    """
    fill = outcome.fill

    def _join(items: Iterable[str]) -> str:
        items = list(items)
        return ", ".join(items) if items else "none"

    lines = ["## Sublane dispatch decision", "", "- current_lanes:"]
    if outcome.classified_lanes:
        lines.extend(
            f"  - {lane.issue}: {lane.state_class}"
            for lane in outcome.classified_lanes
        )
    else:
        lines.append("  - none")
    lines.extend(
        [
            f"- coordinator_blocking_states: {_join(fill.coordinator_blocking)}",
            f"- active_implementing_lanes: {_join(fill.active_implementing)}",
            f"- ready_independent_work: {fill.ready_independent_work}",
            f"- capacity_remaining: {fill.capacity_remaining}",
            f"- admission_decision: {outcome.admission_decision}",
            f"- post_dispatch_fill_check: done",
            f"- fill_decision: {fill.fill_decision}",
            f"- reason: {fill.reason}",
            f"- next_drain_action: {fill.next_drain_action}",
            f"- advisory: {str(outcome.advisory).lower()}",
        ]
    )
    return "\n".join(lines)


__all__ = (
    "GATE_NONE",
    "GATE_START",
    "GATE_PROGRESS",
    "GATE_IMPLEMENTATION_DONE",
    "GATE_REVIEW_REQUEST",
    "GATE_REVIEW",
    "GATE_OWNER_CLOSE_APPROVAL",
    "GATE_CLOSE",
    "GATE_BLOCKED",
    "GATE_KINDS",
    "REVIEW_PENDING",
    "REVIEW_APPROVED",
    "REVIEW_CHANGES_REQUESTED",
    "REVIEW_CONCLUSIONS",
    "CALLBACK_NONE",
    "CALLBACK_DUE",
    "CALLBACK_DELIVERY_FAILED",
    "CALLBACK_STATES",
    "ADMISSION_DISPATCH_SUBLANE",
    "ADMISSION_STOP_AND_DRAIN",
    "LaneSignal",
    "ClassifiedLane",
    "SublaneAdmissionInputs",
    "SublaneAdmissionOutcome",
    "classify_lane_state",
    "evaluate_sublane_admission",
    "render_admission_journal",
)
