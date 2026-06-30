"""Risk-based lane admission decision (Redmine #12921).

The advisory fill / admission policy already in this package decides, for an *aggregate*
lane set, whether to fill the pipeline at all: #12855
(:mod:`...domain.workflow_fill_decision`) returns a :data:`FILL_*` token, and #12856
(:mod:`...domain.sublane_admission`) classifies durable-record facts first and then
delegates to it. Neither answers the question this module is about: **for one concrete
candidate lane, given concrete engineering / workflow risk against the active lane set,
do I dispatch it in parallel, serialize it, hold it as blocked, or escalate it to the
owner — and for which concrete reason?**

The user correction this module makes machine-checkable (Redmine #12670 j#69283, owner
correction): *coordinator convenience is not a valid lane reduction reason.* Reducing the
number of active implementation lanes "because a first callback might be missed", "because
callback / review management is burdensome", or "because the bucket is broad" trades real
parallel-development throughput for nothing concrete. So this policy admits a candidate by
default and only moves it off :data:`ADMIT_ALLOW_DISPATCH` when a **concrete** risk fires.

The concrete risks are a *closed* vocabulary (:data:`VALID_ADMISSION_RISKS`), mirroring the
spine's `## 帯域 / admission / pipeline fill` (`### Lane State Classes` / `### Admission
Rule` / `### Drain Order`):

- file overlap / invariant overlap / merge-order conflict with an active lane ->
  :data:`ADMIT_SERIALIZE`;
- a genuine dependency on a coordinator-owned review / owner / integration / close queue ->
  :data:`ADMIT_SERIALIZE`;
- an unresolved design decision, a release / tag / publish gate, or a credential /
  destructive / external-operation gate -> :data:`ADMIT_NEEDS_OWNER_DECISION`;
- a genuine dependency on a lane in an actual ``blocked`` / ``callback_delivery_failed``
  state (or a dependency whose state cannot be read) -> :data:`ADMIT_BLOCKED`.

The *invalid* non-reasons are an equally closed vocabulary
(:data:`INVALID_SERIALIZATION_NONREASONS`). They are **recorded** on the outcome — so a
coordinator who was tempted to serialize for convenience sees them named and rejected — but
they never move the decision off :data:`ADMIT_ALLOW_DISPATCH` on their own.

Scope boundaries (carried over from #12855 / #12856), the policy **must not** cross:

- it discovers nothing — every fact (the candidate id, the active lane signals, the
  overlap / dependency / gate facts) is supplied by the caller from the durable record,
  not read live here;
- it never selects / creates a Redmine issue and never creates / adopts a lane;
- it is **advisory only** — :attr:`LaneAdmissionOutcome.advisory` is always true and no
  caller is meant to hard-block a handoff on it yet.

Active lane state classification (AC4) reuses the single existing authority,
:func:`...domain.sublane_admission.classify_lane_state`, so there is exactly one place that
maps durable-record facts onto a :data:`LANE_STATE_*` class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    ClassifiedLane,
    LaneSignal,
    classify_lane_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    LANE_STATE_BLOCKED,
    LANE_STATE_CALLBACK_DELIVERY_FAILED,
    LANE_STATE_CALLBACK_DUE,
    LANE_STATE_CLOSE_WAITING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_REVIEW_WAITING,
)

# ---------------------------------------------------------------------------
# Admission decision vocabulary (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------

ADMIT_ALLOW_DISPATCH = "allow_dispatch"
ADMIT_SERIALIZE = "serialize"
ADMIT_BLOCKED = "blocked"
ADMIT_NEEDS_OWNER_DECISION = "needs_owner_decision"

ADMISSION_DECISIONS = frozenset(
    {
        ADMIT_ALLOW_DISPATCH,
        ADMIT_SERIALIZE,
        ADMIT_BLOCKED,
        ADMIT_NEEDS_OWNER_DECISION,
    }
)

# ---------------------------------------------------------------------------
# Concrete admission risk vocabulary (the only valid reasons to move a candidate off
# allow_dispatch). Closed set, mirroring the spine's serialization examples.
# ---------------------------------------------------------------------------

RISK_FILE_OVERLAP = "file_overlap"
RISK_INVARIANT_OVERLAP = "invariant_overlap"
RISK_MERGE_ORDER_CONFLICT = "merge_order_conflict"
RISK_COORDINATOR_OWNED_QUEUE = "coordinator_owned_review_owner_integration_queue"
RISK_UNRESOLVED_DESIGN_DECISION = "unresolved_design_decision"
RISK_RELEASE_PUBLISH_GATE = "release_tag_publish_gate"
RISK_CREDENTIAL_DESTRUCTIVE_EXTERNAL_GATE = (
    "credential_destructive_external_operation_gate"
)
RISK_BLOCKED_OR_CALLBACK_FAILURE = "blocked_or_callback_delivery_failure"

VALID_ADMISSION_RISKS = frozenset(
    {
        RISK_FILE_OVERLAP,
        RISK_INVARIANT_OVERLAP,
        RISK_MERGE_ORDER_CONFLICT,
        RISK_COORDINATOR_OWNED_QUEUE,
        RISK_UNRESOLVED_DESIGN_DECISION,
        RISK_RELEASE_PUBLISH_GATE,
        RISK_CREDENTIAL_DESTRUCTIVE_EXTERNAL_GATE,
        RISK_BLOCKED_OR_CALLBACK_FAILURE,
    }
)

# Each concrete risk maps to exactly one admission decision.
_RISK_DECISION: dict[str, str] = {
    RISK_BLOCKED_OR_CALLBACK_FAILURE: ADMIT_BLOCKED,
    RISK_UNRESOLVED_DESIGN_DECISION: ADMIT_NEEDS_OWNER_DECISION,
    RISK_RELEASE_PUBLISH_GATE: ADMIT_NEEDS_OWNER_DECISION,
    RISK_CREDENTIAL_DESTRUCTIVE_EXTERNAL_GATE: ADMIT_NEEDS_OWNER_DECISION,
    RISK_FILE_OVERLAP: ADMIT_SERIALIZE,
    RISK_INVARIANT_OVERLAP: ADMIT_SERIALIZE,
    RISK_MERGE_ORDER_CONFLICT: ADMIT_SERIALIZE,
    RISK_COORDINATOR_OWNED_QUEUE: ADMIT_SERIALIZE,
}

# Decision severity (most severe first). The headline decision is the most severe across
# all fired risks; every fired risk is still listed on the outcome. owner/release/
# credential/destructive/unresolved-design gates rank first (spine `### Drain Order` step
# 1-2: production/release/credential/destructive + owner decision drain before all else),
# then a concrete blocked dependency, then a serialize-able overlap / queue.
_DECISION_SEVERITY: tuple[str, ...] = (
    ADMIT_NEEDS_OWNER_DECISION,
    ADMIT_BLOCKED,
    ADMIT_SERIALIZE,
    ADMIT_ALLOW_DISPATCH,
)

# ---------------------------------------------------------------------------
# Invalid (coordinator-convenience) non-reasons. Recorded, never decisive.
# ---------------------------------------------------------------------------

# 「callback を取りこぼしそう」 — a *speculative* worry about a first callback (NOT an
# actual callback_delivery_failure, which is a concrete blocked dependency).
NONREASON_CALLBACK_MISS_RISK = "callback_miss_risk"
# 「管理が大変」 — coordinator callback / review management burden.
NONREASON_COORDINATOR_MANAGEMENT_LOAD = "coordinator_management_load"
# 「broad bucket だから」 — the bucket / version is broad, with no concrete overlap.
NONREASON_BROAD_BUCKET = "broad_bucket"

INVALID_SERIALIZATION_NONREASONS = frozenset(
    {
        NONREASON_CALLBACK_MISS_RISK,
        NONREASON_COORDINATOR_MANAGEMENT_LOAD,
        NONREASON_BROAD_BUCKET,
    }
)

# Active lane state classes that mean "a coordinator-owned queue must drain before this
# candidate's declared dependency clears" — a genuine sequencing dependency, NOT the
# coordinator merely finding the queue burdensome.
_COORDINATOR_QUEUE_STATES = frozenset(
    {
        LANE_STATE_REVIEW_WAITING,
        LANE_STATE_OWNER_WAITING,
        LANE_STATE_INTEGRATION_WAITING,
        LANE_STATE_CLOSE_WAITING,
        LANE_STATE_CALLBACK_DUE,
    }
)

# Active lane state classes that hard-block a candidate depending on them.
_HARD_BLOCK_STATES = frozenset(
    {
        LANE_STATE_BLOCKED,
        LANE_STATE_CALLBACK_DELIVERY_FAILED,
    }
)


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneAdmissionInputs:
    """The caller-supplied facts the risk-based admission decision is made from.

    Every field is a durable-record fact the caller supplies; the policy discovers
    nothing.

    ``candidate_issue`` is the Redmine issue id of the lane being considered for
    dispatch. ``active_lane_signals`` are the durable-record facts of the currently
    active lanes (classified here via :func:`classify_lane_state` for the narrative and
    to resolve the state of any declared dependency).

    The concrete-overlap facts name the active lane issue ids the candidate overlaps:
    ``file_overlap_lanes`` (shared files), ``invariant_overlap_lanes`` (shared
    invariant / behavioral surface), ``merge_order_conflict_lanes`` (a known
    merge-order conflict). ``dependency_lanes`` are active lanes whose completion /
    queue the candidate genuinely depends on; each is classified from its signal and a
    ``blocked`` / ``callback_delivery_failed`` (or unreadable) dependency blocks the
    candidate, while a coordinator-owned review / owner / integration / close /
    callback_due dependency serializes it. A dependency on an actively ``implementing``
    (or ``idle`` / ``retire_ready``) lane is *not* by itself a risk — if a real ordering
    concern exists the caller declares it via ``merge_order_conflict_lanes``.

    ``unresolved_design_decision`` / ``release_publish_gate_active`` /
    ``credential_destructive_external_gate_active`` are owner-territory gates.

    ``callback_miss_concern`` / ``coordinator_management_load`` / ``broad_bucket_only``
    are the rejected coordinator-convenience signals: they are recorded on the outcome
    but never move the decision off :data:`ADMIT_ALLOW_DISPATCH` on their own.
    """

    candidate_issue: str
    active_lane_signals: tuple[LaneSignal, ...] = ()
    file_overlap_lanes: tuple[str, ...] = ()
    invariant_overlap_lanes: tuple[str, ...] = ()
    merge_order_conflict_lanes: tuple[str, ...] = ()
    dependency_lanes: tuple[str, ...] = ()
    unresolved_design_decision: bool = False
    release_publish_gate_active: bool = False
    credential_destructive_external_gate_active: bool = False
    callback_miss_concern: bool = False
    coordinator_management_load: bool = False
    broad_bucket_only: bool = False


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionRisk:
    """One concrete admission risk that fired, with the lanes it implicates.

    ``reason`` is one of :data:`VALID_ADMISSION_RISKS`; ``decision`` is the
    :data:`ADMIT_*` token it maps to; ``lanes`` are the active lane issue ids that
    triggered it (empty for a candidate-global gate such as a release gate).
    """

    reason: str
    decision: str
    lanes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "decision": self.decision,
            "lanes": list(self.lanes),
        }


@dataclass(frozen=True)
class LaneAdmissionOutcome:
    """The replayable, advisory result of one risk-based lane admission decision.

    ``candidate_issue`` is the lane considered. ``decision`` is the headline
    :data:`ADMIT_*` token (the most severe across all ``risks``). ``risks`` lists every
    concrete risk that fired (not just the deciding one) so the journal narrative is
    complete. ``rejected_nonreasons`` lists every coordinator-convenience signal that was
    supplied and rejected — present even when the decision is ``allow_dispatch`` so the
    user correction is observable. ``classified_lanes`` is the active lane state map
    (durable-record classification, AC4). ``next_safe_action`` is the journal-friendly
    next step. ``advisory`` is always true.
    """

    candidate_issue: str
    decision: str
    risks: tuple[AdmissionRisk, ...]
    rejected_nonreasons: tuple[str, ...]
    classified_lanes: tuple[ClassifiedLane, ...]
    next_safe_action: str
    advisory: bool = True

    @property
    def should_dispatch(self) -> bool:
        return self.decision == ADMIT_ALLOW_DISPATCH

    @property
    def risk_reasons(self) -> tuple[str, ...]:
        return tuple(risk.reason for risk in self.risks)

    def as_payload(self) -> dict[str, object]:
        return {
            "candidate_issue": self.candidate_issue,
            "decision": self.decision,
            "should_dispatch": self.should_dispatch,
            "risks": [risk.as_payload() for risk in self.risks],
            "rejected_nonreasons": list(self.rejected_nonreasons),
            "classified_lanes": [lane.as_payload() for lane in self.classified_lanes],
            "next_safe_action": self.next_safe_action,
            "advisory": self.advisory,
        }


def _join(items: Iterable[str]) -> str:
    items = [item for item in items if item]
    return ", ".join(items) if items else "none"


def _next_safe_action(
    candidate: str, decision: str, risks: tuple[AdmissionRisk, ...]
) -> str:
    """The journal-friendly next safe action for the headline decision (pure)."""
    deciding_lanes: list[str] = []
    for risk in risks:
        if risk.decision == decision:
            deciding_lanes.extend(risk.lanes)
    lanes = _join(deciding_lanes)
    if decision == ADMIT_ALLOW_DISPATCH:
        return (
            f"dispatch {candidate} as a parallel implementation sublane; "
            "no concrete engineering/workflow risk blocks parallel admission"
        )
    if decision == ADMIT_SERIALIZE:
        return (
            f"serialize {candidate} behind {lanes}; dispatch once the file/invariant "
            "overlap, merge order, or coordinator-owned queue clears"
        )
    if decision == ADMIT_BLOCKED:
        return (
            f"hold {candidate}; resolve the blocked / callback-delivery failure on "
            f"{lanes} before dispatch"
        )
    # ADMIT_NEEDS_OWNER_DECISION
    return (
        f"escalate {candidate} to the owner; resolve the unresolved design decision / "
        "release / credential / destructive gate before dispatch"
    )


def evaluate_lane_admission(inputs: LaneAdmissionInputs) -> LaneAdmissionOutcome:
    """Resolve the advisory risk-based lane admission decision (pure, #12921).

    Classifies each active lane signal (AC4, via :func:`classify_lane_state`), gathers
    every concrete risk that fires against the candidate, derives the headline
    :data:`ADMIT_*` decision as the most severe across those risks, and records any
    supplied coordinator-convenience non-reason without letting it change the decision.

    A candidate with no concrete risk resolves to :data:`ADMIT_ALLOW_DISPATCH` even when
    coordinator-convenience signals are present — the user correction made
    machine-checkable.
    """
    classified = tuple(
        ClassifiedLane(issue=signal.issue, state_class=classify_lane_state(signal))
        for signal in inputs.active_lane_signals
    )
    state_by_issue = {lane.issue: lane.state_class for lane in classified}

    risks: list[AdmissionRisk] = []

    def add_risk(reason: str, lanes: Iterable[str] = ()) -> None:
        deduped = tuple(dict.fromkeys(lane for lane in lanes if lane))
        risks.append(
            AdmissionRisk(reason=reason, decision=_RISK_DECISION[reason], lanes=deduped)
        )

    # Concrete overlap risks (caller-asserted against named active lanes).
    if inputs.file_overlap_lanes:
        add_risk(RISK_FILE_OVERLAP, inputs.file_overlap_lanes)
    if inputs.invariant_overlap_lanes:
        add_risk(RISK_INVARIANT_OVERLAP, inputs.invariant_overlap_lanes)
    if inputs.merge_order_conflict_lanes:
        add_risk(RISK_MERGE_ORDER_CONFLICT, inputs.merge_order_conflict_lanes)

    # Dependency-state risks (classified from the durable record). A dependency whose
    # signal is absent is fail-closed to a hard block: an unreadable dependency is held,
    # never dispatched past.
    hard_block_deps: list[str] = []
    queue_deps: list[str] = []
    for dep in inputs.dependency_lanes:
        if not dep:
            continue
        state = state_by_issue.get(dep)
        if state is None or state in _HARD_BLOCK_STATES:
            hard_block_deps.append(dep)
        elif state in _COORDINATOR_QUEUE_STATES:
            queue_deps.append(dep)
        # implementing / idle / retire_ready dependency: not a risk by itself.
    if queue_deps:
        add_risk(RISK_COORDINATOR_OWNED_QUEUE, queue_deps)
    if hard_block_deps:
        add_risk(RISK_BLOCKED_OR_CALLBACK_FAILURE, hard_block_deps)

    # Owner-territory gates (candidate-global; no implicated active lane).
    if inputs.unresolved_design_decision:
        add_risk(RISK_UNRESOLVED_DESIGN_DECISION)
    if inputs.release_publish_gate_active:
        add_risk(RISK_RELEASE_PUBLISH_GATE)
    if inputs.credential_destructive_external_gate_active:
        add_risk(RISK_CREDENTIAL_DESTRUCTIVE_EXTERNAL_GATE)

    # Rejected coordinator-convenience non-reasons: recorded, never decisive.
    rejected: list[str] = []
    if inputs.callback_miss_concern:
        rejected.append(NONREASON_CALLBACK_MISS_RISK)
    if inputs.coordinator_management_load:
        rejected.append(NONREASON_COORDINATOR_MANAGEMENT_LOAD)
    if inputs.broad_bucket_only:
        rejected.append(NONREASON_BROAD_BUCKET)

    fired_decisions = {risk.decision for risk in risks}
    decision = ADMIT_ALLOW_DISPATCH
    for candidate_decision in _DECISION_SEVERITY:
        if candidate_decision == ADMIT_ALLOW_DISPATCH or candidate_decision in fired_decisions:
            decision = candidate_decision
            break

    risks_tuple = tuple(risks)
    return LaneAdmissionOutcome(
        candidate_issue=inputs.candidate_issue,
        decision=decision,
        risks=risks_tuple,
        rejected_nonreasons=tuple(rejected),
        classified_lanes=classified,
        next_safe_action=_next_safe_action(inputs.candidate_issue, decision, risks_tuple),
    )


def render_lane_admission_journal(outcome: LaneAdmissionOutcome) -> str:
    """Render the outcome as a journal-friendly admission-decision narrative (pure).

    Produces the markdown a coordinator pastes into the Redmine dispatch-decision
    journal, complementing the #12856 Bandwidth Record Template with the candidate-level
    risk decision. Only issue IDs, state classes, and the literal risk / non-reason
    vocabularies are emitted — never private paths or operator-specific cockpit details.
    """
    lines = [
        "## Lane admission decision",
        "",
        f"- candidate_issue: {outcome.candidate_issue}",
        f"- admission_decision: {outcome.decision}",
        "- active_lanes:",
    ]
    if outcome.classified_lanes:
        lines.extend(
            f"  - {lane.issue}: {lane.state_class}" for lane in outcome.classified_lanes
        )
    else:
        lines.append("  - none")
    lines.append("- risk_reasons:")
    if outcome.risks:
        lines.extend(
            f"  - {risk.reason} ({risk.decision}): {_join(risk.lanes)}"
            for risk in outcome.risks
        )
    else:
        lines.append("  - none")
    lines.extend(
        [
            f"- rejected_nonreasons: {_join(outcome.rejected_nonreasons)}",
            f"- next_safe_action: {outcome.next_safe_action}",
            f"- advisory: {str(outcome.advisory).lower()}",
        ]
    )
    return "\n".join(lines)


__all__ = (
    "ADMIT_ALLOW_DISPATCH",
    "ADMIT_SERIALIZE",
    "ADMIT_BLOCKED",
    "ADMIT_NEEDS_OWNER_DECISION",
    "ADMISSION_DECISIONS",
    "RISK_FILE_OVERLAP",
    "RISK_INVARIANT_OVERLAP",
    "RISK_MERGE_ORDER_CONFLICT",
    "RISK_COORDINATOR_OWNED_QUEUE",
    "RISK_UNRESOLVED_DESIGN_DECISION",
    "RISK_RELEASE_PUBLISH_GATE",
    "RISK_CREDENTIAL_DESTRUCTIVE_EXTERNAL_GATE",
    "RISK_BLOCKED_OR_CALLBACK_FAILURE",
    "VALID_ADMISSION_RISKS",
    "NONREASON_CALLBACK_MISS_RISK",
    "NONREASON_COORDINATOR_MANAGEMENT_LOAD",
    "NONREASON_BROAD_BUCKET",
    "INVALID_SERIALIZATION_NONREASONS",
    "LaneAdmissionInputs",
    "AdmissionRisk",
    "LaneAdmissionOutcome",
    "evaluate_lane_admission",
    "render_lane_admission_journal",
)
