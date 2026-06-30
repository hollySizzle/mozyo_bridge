"""Version-bucket lane-set dispatch plan (Redmine #12920).

A coordinator who picks one issue / one lane by hand, one at a time, trades real
parallel-development throughput for nothing concrete (the owner correction made
machine-checkable in #12921: *coordinator convenience is not a valid lane-reduction
reason*). This module is the read-only **planning** seam that replaces that manual pick:
given a resolved lane *bucket* (a Redmine Version today, #12919) it enumerates the
bucket's open leaf issues, classifies each as a dispatch candidate, and projects the
coordinator-owned queue the candidates must be admitted against — all in one pure value.

It deliberately owns **no new policy**. It is the composition of two existing
authorities:

- the bucket / leaf rule is #12919's (:class:`...lane_bucket_provider.LaneBucket` and its
  :attr:`open_leaf_issues`); this module never re-decides what a bucket or a leaf is. The
  :class:`...lane_bucket_provider.BucketResolution` is read as a *published record type*
  across the context boundary (the #12919 module exists precisely so dispatch can read it
  — #12919 docstring: *"the lane-bucket source boundary lane-set / dispatch depends on"*),
  not by reaching into the Redmine adapter.
- the per-candidate dispatch decision is #12921's
  (:func:`...lane_admission_risk.evaluate_lane_admission`); this module maps that decision
  onto the issue's stable plan vocabulary and adds nothing to the admission logic. Active
  lane state classification is #12856's single authority
  (:func:`...sublane_admission.classify_lane_state`), reused for the queue projection.

The plan vocabulary (the acceptance condition's ``dispatchable`` / ``standby`` /
``blocked`` / ``needs_owner_decision``) is a thin rename of the #12921 admission decision,
fixed in one mapping (:data:`_ADMISSION_TO_PLAN`):

- :data:`ADMIT_ALLOW_DISPATCH` -> :data:`PLAN_DISPATCHABLE`;
- :data:`ADMIT_SERIALIZE` -> :data:`PLAN_STANDBY`;
- :data:`ADMIT_BLOCKED` -> :data:`PLAN_BLOCKED`;
- :data:`ADMIT_NEEDS_OWNER_DECISION` -> :data:`PLAN_NEEDS_OWNER_DECISION`.

Scope boundaries (carried over from #12919 / #12921, the plan **must not** cross):

- it discovers nothing — the bucket resolution, the active lane signals, and every
  per-candidate risk fact are supplied by the caller from a snapshot / the durable record,
  not read live here; there is no network call and no Redmine read;
- it never selects / creates a Redmine issue, never creates / adopts a lane, and never
  sends a handoff. ``mode`` records *dispatch intent* (:data:`MODE_DRY_RUN` /
  :data:`MODE_EXECUTE`) but the value is identical and side-effect-free either way: the
  plan only *emits* the governed route (:data:`RECOMMENDED_ROUTE` — coordinator Codex ->
  sublane Codex gateway -> same-lane Claude, the #12918 route gate's allowed path) that a
  coordinator would run through the existing handoff primitive. Unattended / automatic
  dispatch is an explicit #12920 non-goal, so this surface never auto-sends.

Everything here is pure: frozen dataclasses, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
    ADMIT_ALLOW_DISPATCH,
    ADMIT_BLOCKED,
    ADMIT_NEEDS_OWNER_DECISION,
    ADMIT_SERIALIZE,
    LaneAdmissionInputs,
    LaneAdmissionOutcome,
    evaluate_lane_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    LaneSignal,
    classify_lane_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
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
from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.lane_bucket_provider import (
    BucketResolution,
    LaneBucket,
    LaneBucketIssue,
)

# ---------------------------------------------------------------------------
# Plan classification vocabulary (machine-readable; literal regardless of UI language).
# A thin rename of the #12921 admission decision so the plan reads in the acceptance
# condition's words while the decision authority stays single.
# ---------------------------------------------------------------------------

PLAN_DISPATCHABLE = "dispatchable"
PLAN_STANDBY = "standby"
PLAN_BLOCKED = "blocked"
PLAN_NEEDS_OWNER_DECISION = "needs_owner_decision"

PLAN_CLASSIFICATIONS = frozenset(
    {
        PLAN_DISPATCHABLE,
        PLAN_STANDBY,
        PLAN_BLOCKED,
        PLAN_NEEDS_OWNER_DECISION,
    }
)

_ADMISSION_TO_PLAN: Mapping[str, str] = {
    ADMIT_ALLOW_DISPATCH: PLAN_DISPATCHABLE,
    ADMIT_SERIALIZE: PLAN_STANDBY,
    ADMIT_BLOCKED: PLAN_BLOCKED,
    ADMIT_NEEDS_OWNER_DECISION: PLAN_NEEDS_OWNER_DECISION,
}

# Dispatch intent. The value is read-only either way (see module docstring): execute does
# not auto-send — it only labels the plan as the one a coordinator intends to act on.
MODE_DRY_RUN = "dry_run"
MODE_EXECUTE = "execute"

DISPATCH_MODES = frozenset({MODE_DRY_RUN, MODE_EXECUTE})

# The governed dispatch route every candidate would take: coordinator Codex -> sublane
# Codex gateway -> same-lane Claude (the #12918 gateway route gate's allowed path). The
# plan only emits it; it never rides it.
RECOMMENDED_ROUTE = "coordinator_codex -> sublane_codex_gateway -> same_lane_claude"

# Why a bucket issue is not a dispatch candidate at all (it never reaches the admission
# decision). Closed: no work to dispatch. Not-leaf: an umbrella whose open children are
# the real leaves. Distinct from the #12921 per-candidate decision and from #12919's
# bucket-level BucketSkip vocabulary.
ISSUE_SKIP_CLOSED = "issue_closed"
ISSUE_SKIP_NOT_LEAF = "not_leaf"

ISSUE_SKIP_REASONS = frozenset({ISSUE_SKIP_CLOSED, ISSUE_SKIP_NOT_LEAF})


class LaneSetDispatchPlanError(ValueError):
    """An invalid dispatch-plan construction (e.g. an unknown dispatch mode)."""


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateDispatchFacts:
    """The caller-supplied per-candidate risk facts for one bucket leaf issue.

    Every field is a durable-record fact the caller supplies; the plan discovers nothing.
    These map one-to-one onto the #12921 :class:`LaneAdmissionInputs` risk fields and
    carry the same meaning: the overlap tuples name the active lane issue ids the
    candidate overlaps, ``dependency_lanes`` name active lanes the candidate genuinely
    depends on (classified from the shared active-lane signals), and the owner-gate /
    coordinator-convenience flags are the same owner-territory gates and rejected
    non-reasons. ``expected_changed_surface`` is the short caller-supplied note of the
    file / behavioral surface the candidate is expected to touch (used for the plan's
    overlap reasoning and journal narrative; never discovered here).
    """

    expected_changed_surface: str = ""
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


_NO_FACTS = CandidateDispatchFacts()


# ---------------------------------------------------------------------------
# Output records.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchCandidate:
    """One bucket leaf issue with its plan classification (#12920 acceptance fields).

    Carries every field the acceptance condition requires per candidate: ``issue_id`` /
    ``tracker`` / ``parent_id`` / ``bucket_id`` / ``bucket_name`` (its bucket),
    ``expected_changed_surface``, ``skip_reason`` (why it is not dispatchable now; empty
    for a :data:`PLAN_DISPATCHABLE` candidate), and ``recommended_route`` (the governed
    route a dispatch would take, :data:`RECOMMENDED_ROUTE`). ``classification`` is the
    plan vocabulary token; ``admission_decision`` is the underlying #12921
    :data:`ADMIT_*` token it was mapped from; ``risk_reasons`` / ``rejected_nonreasons``
    are the #12921 decision evidence; ``next_safe_action`` is the journal-friendly step.
    """

    issue_id: str
    tracker: Optional[str]
    parent_id: Optional[str]
    bucket_id: str
    bucket_name: Optional[str]
    classification: str
    admission_decision: str
    recommended_route: str
    expected_changed_surface: str = ""
    skip_reason: str = ""
    risk_reasons: tuple[str, ...] = ()
    rejected_nonreasons: tuple[str, ...] = ()
    next_safe_action: str = ""

    @property
    def dispatchable(self) -> bool:
        return self.classification == PLAN_DISPATCHABLE

    def as_payload(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "tracker": self.tracker,
            "parent_id": self.parent_id,
            "bucket_id": self.bucket_id,
            "bucket_name": self.bucket_name,
            "classification": self.classification,
            "admission_decision": self.admission_decision,
            "recommended_route": self.recommended_route,
            "expected_changed_surface": self.expected_changed_surface,
            "skip_reason": self.skip_reason,
            "risk_reasons": list(self.risk_reasons),
            "rejected_nonreasons": list(self.rejected_nonreasons),
            "next_safe_action": self.next_safe_action,
        }


@dataclass(frozen=True)
class SkippedBucketIssue:
    """A bucket issue that is not a dispatch candidate at all, with the reason.

    A closed issue (:data:`ISSUE_SKIP_CLOSED`) or an open non-leaf umbrella
    (:data:`ISSUE_SKIP_NOT_LEAF`) never reaches the admission decision; it is recorded
    here so the plan accounts for every issue in the bucket rather than silently dropping
    it.
    """

    issue_id: str
    tracker: Optional[str]
    parent_id: Optional[str]
    reason: str

    def as_payload(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "tracker": self.tracker,
            "parent_id": self.parent_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CoordinatorQueueState:
    """The coordinator-owned queue the candidates are admitted against (#12920 AC).

    Projects every active lane signal onto its #12856 :data:`LANE_STATE_*` class (via the
    single authority :func:`classify_lane_state`) and groups the lane issue ids by class.
    The acceptance condition names ``review_waiting`` / ``owner_waiting`` /
    ``integration_waiting`` explicitly; the remaining classes are kept so the queue is
    fully visible before dispatch (a blocked / callback-failed lane is exactly what a
    candidate may depend on). ``active_lanes`` is the ``issue: state_class`` map in input
    order.
    """

    active_lanes: tuple[tuple[str, str], ...] = ()
    review_waiting: tuple[str, ...] = ()
    owner_waiting: tuple[str, ...] = ()
    integration_waiting: tuple[str, ...] = ()
    close_waiting: tuple[str, ...] = ()
    callback_due: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    callback_delivery_failed: tuple[str, ...] = ()
    implementing: tuple[str, ...] = ()
    idle: tuple[str, ...] = ()
    retire_ready: tuple[str, ...] = ()

    @property
    def total_active(self) -> int:
        return len(self.active_lanes)

    def as_payload(self) -> dict[str, object]:
        return {
            "total_active": self.total_active,
            "active_lanes": [
                {"issue": issue, "state_class": state}
                for issue, state in self.active_lanes
            ],
            "review_waiting": list(self.review_waiting),
            "owner_waiting": list(self.owner_waiting),
            "integration_waiting": list(self.integration_waiting),
            "close_waiting": list(self.close_waiting),
            "callback_due": list(self.callback_due),
            "blocked": list(self.blocked),
            "callback_delivery_failed": list(self.callback_delivery_failed),
            "implementing": list(self.implementing),
            "idle": list(self.idle),
            "retire_ready": list(self.retire_ready),
        }


@dataclass(frozen=True)
class LaneSetDispatchPlan:
    """The replayable, read-only lane-set dispatch plan for one bucket (#12920).

    ``resolved`` is false when the bucket itself could not be resolved (a closed / locked /
    missing Version, #12919); then ``bucket_skip`` carries the #12919 skip payload and
    there are no candidates. ``candidates`` are the open leaf issues, each classified;
    ``skipped_issues`` are the bucket issues that were never candidates (closed / non-leaf).
    ``queue_state`` is the coordinator-owned queue projection. ``mode`` is the dispatch
    intent (read-only either way). ``advisory`` is always true.
    """

    bucket_id: str
    source_kind: Optional[str]
    resolved: bool
    mode: str
    queue_state: CoordinatorQueueState
    bucket_name: Optional[str] = None
    bucket_status: Optional[str] = None
    parent_us: Optional[str] = None
    is_umbrella: bool = False
    candidates: tuple[DispatchCandidate, ...] = ()
    skipped_issues: tuple[SkippedBucketIssue, ...] = ()
    bucket_skip: Optional[dict[str, object]] = None
    advisory: bool = True

    @property
    def counts_by_classification(self) -> dict[str, int]:
        counts = {name: 0 for name in sorted(PLAN_CLASSIFICATIONS)}
        for candidate in self.candidates:
            counts[candidate.classification] = counts.get(candidate.classification, 0) + 1
        return counts

    @property
    def dispatchable_candidates(self) -> tuple[DispatchCandidate, ...]:
        return tuple(c for c in self.candidates if c.dispatchable)

    def as_payload(self) -> dict[str, object]:
        return {
            "bucket_id": self.bucket_id,
            "bucket_name": self.bucket_name,
            "bucket_status": self.bucket_status,
            "source_kind": self.source_kind,
            "resolved": self.resolved,
            "mode": self.mode,
            "advisory": self.advisory,
            "parent_us": self.parent_us,
            "is_umbrella": self.is_umbrella,
            "recommended_route": RECOMMENDED_ROUTE,
            "bucket_skip": self.bucket_skip,
            "queue_state": self.queue_state.as_payload(),
            "counts_by_classification": self.counts_by_classification,
            "candidates": [c.as_payload() for c in self.candidates],
            "skipped_issues": [s.as_payload() for s in self.skipped_issues],
        }


# ---------------------------------------------------------------------------
# Builders (pure).
# ---------------------------------------------------------------------------

# State class -> the CoordinatorQueueState field that collects its lanes.
_QUEUE_FIELDS: Mapping[str, str] = {
    LANE_STATE_REVIEW_WAITING: "review_waiting",
    LANE_STATE_OWNER_WAITING: "owner_waiting",
    LANE_STATE_INTEGRATION_WAITING: "integration_waiting",
    LANE_STATE_CLOSE_WAITING: "close_waiting",
    LANE_STATE_CALLBACK_DUE: "callback_due",
    LANE_STATE_BLOCKED: "blocked",
    LANE_STATE_CALLBACK_DELIVERY_FAILED: "callback_delivery_failed",
    LANE_STATE_IMPLEMENTING: "implementing",
    LANE_STATE_IDLE: "idle",
    LANE_STATE_RETIRE_READY: "retire_ready",
}


def project_coordinator_queue(
    active_lane_signals: Sequence[LaneSignal],
) -> CoordinatorQueueState:
    """Project the active lane signals onto the coordinator-owned queue state (pure).

    Each signal is classified with the single #12856 authority
    :func:`classify_lane_state`; the lane issue ids are then grouped by state class. The
    ``active_lanes`` map preserves input order.
    """
    active: list[tuple[str, str]] = []
    buckets: dict[str, list[str]] = {field_name: [] for field_name in _QUEUE_FIELDS.values()}
    for signal in active_lane_signals:
        state = classify_lane_state(signal)
        active.append((signal.issue, state))
        field_name = _QUEUE_FIELDS.get(state)
        if field_name is not None:
            buckets[field_name].append(signal.issue)
    return CoordinatorQueueState(
        active_lanes=tuple(active),
        **{name: tuple(values) for name, values in buckets.items()},
    )


def _skip_reason_for(outcome: LaneAdmissionOutcome) -> str:
    """The candidate's skip reason: empty when dispatchable, else the decisive risks.

    A dispatchable candidate has no skip reason. Otherwise the deciding risks (those whose
    admission decision equals the headline decision) name *why* it is held, so the plan
    reads the same as #12921's ``next_safe_action`` without re-deriving it.
    """
    if outcome.decision == ADMIT_ALLOW_DISPATCH:
        return ""
    deciding = [
        risk.reason for risk in outcome.risks if risk.decision == outcome.decision
    ]
    return ", ".join(deciding) if deciding else outcome.decision


def _candidate_from_issue(
    issue: LaneBucketIssue,
    bucket: LaneBucket,
    active_lane_signals: Sequence[LaneSignal],
    facts: CandidateDispatchFacts,
) -> DispatchCandidate:
    """Classify one open leaf issue into a :class:`DispatchCandidate` (pure).

    Delegates the decision to the #12921 :func:`evaluate_lane_admission` over the shared
    active lane signals and this candidate's caller-supplied risk facts, then maps the
    :data:`ADMIT_*` decision onto the plan vocabulary. The route is the governed
    :data:`RECOMMENDED_ROUTE` for every candidate (the path a dispatch *would* take), not
    a per-candidate choice.
    """
    outcome = evaluate_lane_admission(
        LaneAdmissionInputs(
            candidate_issue=issue.issue_id,
            active_lane_signals=tuple(active_lane_signals),
            file_overlap_lanes=facts.file_overlap_lanes,
            invariant_overlap_lanes=facts.invariant_overlap_lanes,
            merge_order_conflict_lanes=facts.merge_order_conflict_lanes,
            dependency_lanes=facts.dependency_lanes,
            unresolved_design_decision=facts.unresolved_design_decision,
            release_publish_gate_active=facts.release_publish_gate_active,
            credential_destructive_external_gate_active=(
                facts.credential_destructive_external_gate_active
            ),
            callback_miss_concern=facts.callback_miss_concern,
            coordinator_management_load=facts.coordinator_management_load,
            broad_bucket_only=facts.broad_bucket_only,
        )
    )
    classification = _ADMISSION_TO_PLAN[outcome.decision]
    return DispatchCandidate(
        issue_id=issue.issue_id,
        tracker=issue.tracker,
        parent_id=issue.parent_id,
        bucket_id=bucket.bucket_id,
        bucket_name=bucket.name,
        classification=classification,
        admission_decision=outcome.decision,
        recommended_route=RECOMMENDED_ROUTE,
        expected_changed_surface=facts.expected_changed_surface,
        skip_reason=_skip_reason_for(outcome),
        risk_reasons=outcome.risk_reasons,
        rejected_nonreasons=outcome.rejected_nonreasons,
        next_safe_action=outcome.next_safe_action,
    )


def build_dispatch_plan(
    resolution: BucketResolution,
    *,
    active_lane_signals: Sequence[LaneSignal] = (),
    candidate_facts: Optional[Mapping[str, CandidateDispatchFacts]] = None,
    mode: str = MODE_DRY_RUN,
) -> LaneSetDispatchPlan:
    """Build the read-only lane-set dispatch plan for one bucket (pure, #12920).

    ``resolution`` is the #12919 :class:`BucketResolution` the caller obtained from a
    :class:`...lane_bucket_provider.LaneBucketProvider` (a Redmine ``fixed_version``
    snapshot today). When it is a skip — a closed / locked / missing Version — the plan is
    unresolved and carries the skip payload with no candidates. Otherwise each open leaf
    issue is classified via #12921 against the shared ``active_lane_signals`` and its
    per-issue ``candidate_facts`` (a candidate with no facts has no concrete risk, so it is
    dispatchable). Closed and non-leaf bucket issues are recorded under
    ``skipped_issues``. ``mode`` records dispatch intent but never changes behavior — the
    plan is side-effect-free in both modes.
    """
    if mode not in DISPATCH_MODES:
        raise LaneSetDispatchPlanError(
            f"unknown dispatch mode: {mode!r}; expected one of {sorted(DISPATCH_MODES)}"
        )
    facts_by_issue = dict(candidate_facts or {})
    queue_state = project_coordinator_queue(active_lane_signals)

    if not resolution.resolved:
        skip = resolution.skip
        return LaneSetDispatchPlan(
            bucket_id=(skip.bucket_id if skip is not None else "") or "",
            source_kind=None,
            resolved=False,
            mode=mode,
            queue_state=queue_state,
            bucket_skip=skip.as_dict() if skip is not None else None,
        )

    bucket = resolution.bucket
    candidates: list[DispatchCandidate] = []
    skipped: list[SkippedBucketIssue] = []
    leaf_ids = {issue.issue_id for issue in bucket.open_leaf_issues}
    for issue in bucket.open_leaf_issues:
        facts = facts_by_issue.get(issue.issue_id, _NO_FACTS)
        candidates.append(
            _candidate_from_issue(issue, bucket, active_lane_signals, facts)
        )
    for issue in bucket.issues:
        if issue.issue_id in leaf_ids:
            continue
        reason = ISSUE_SKIP_CLOSED if issue.is_closed else ISSUE_SKIP_NOT_LEAF
        skipped.append(
            SkippedBucketIssue(
                issue_id=issue.issue_id,
                tracker=issue.tracker,
                parent_id=issue.parent_id,
                reason=reason,
            )
        )

    return LaneSetDispatchPlan(
        bucket_id=bucket.bucket_id,
        source_kind=bucket.source_kind,
        resolved=True,
        mode=mode,
        queue_state=queue_state,
        bucket_name=bucket.name,
        bucket_status=bucket.status,
        parent_us=bucket.parent_us,
        is_umbrella=bucket.is_umbrella,
        candidates=tuple(candidates),
        skipped_issues=tuple(skipped),
    )


def _join(items: Sequence[str]) -> str:
    kept = [item for item in items if item]
    return ", ".join(kept) if kept else "none"


def render_dispatch_plan_journal(plan: LaneSetDispatchPlan) -> str:
    """Render the plan as a journal-friendly dispatch-plan narrative (pure).

    Produces the markdown a coordinator pastes into the Redmine dispatch-decision journal:
    the bucket, the coordinator-owned queue state (review / owner / integration waiting),
    and each candidate's classification + skip reason + governed route. Only issue ids,
    state classes, and the literal plan / route vocabularies are emitted — never private
    paths or operator-specific cockpit details.
    """
    queue = plan.queue_state
    lines = [
        "## Lane-set dispatch plan",
        "",
        f"- bucket_id: {plan.bucket_id}",
        f"- bucket_name: {plan.bucket_name or 'none'}",
        f"- source_kind: {plan.source_kind or 'none'}",
        f"- resolved: {str(plan.resolved).lower()}",
        f"- mode: {plan.mode}",
        f"- recommended_route: {RECOMMENDED_ROUTE}",
    ]
    if not plan.resolved:
        skip = plan.bucket_skip or {}
        lines.append(
            f"- bucket_skip: {skip.get('reason', 'unknown')} "
            f"({skip.get('detail', '') or 'no detail'})"
        )
        lines.append(f"- advisory: {str(plan.advisory).lower()}")
        return "\n".join(lines)

    counts = plan.counts_by_classification
    lines.extend(
        [
            "- coordinator_owned_queue:",
            f"  - active_lanes: {queue.total_active}",
            f"  - review_waiting: {_join(queue.review_waiting)}",
            f"  - owner_waiting: {_join(queue.owner_waiting)}",
            f"  - integration_waiting: {_join(queue.integration_waiting)}",
            "- counts_by_classification:",
            f"  - dispatchable: {counts[PLAN_DISPATCHABLE]}",
            f"  - standby: {counts[PLAN_STANDBY]}",
            f"  - blocked: {counts[PLAN_BLOCKED]}",
            f"  - needs_owner_decision: {counts[PLAN_NEEDS_OWNER_DECISION]}",
            "- candidates:",
        ]
    )
    if plan.candidates:
        for candidate in plan.candidates:
            skip = candidate.skip_reason or "none"
            parent = candidate.parent_id or "none"
            lines.append(
                f"  - {candidate.issue_id} ({candidate.tracker or 'unknown'}, "
                f"parent={parent}): {candidate.classification}; "
                f"skip_reason={skip}"
            )
    else:
        lines.append("  - none")
    if plan.skipped_issues:
        lines.append("- skipped_issues:")
        for skipped in plan.skipped_issues:
            lines.append(f"  - {skipped.issue_id}: {skipped.reason}")
    lines.append(f"- advisory: {str(plan.advisory).lower()}")
    return "\n".join(lines)


__all__ = (
    "PLAN_DISPATCHABLE",
    "PLAN_STANDBY",
    "PLAN_BLOCKED",
    "PLAN_NEEDS_OWNER_DECISION",
    "PLAN_CLASSIFICATIONS",
    "MODE_DRY_RUN",
    "MODE_EXECUTE",
    "DISPATCH_MODES",
    "RECOMMENDED_ROUTE",
    "ISSUE_SKIP_CLOSED",
    "ISSUE_SKIP_NOT_LEAF",
    "ISSUE_SKIP_REASONS",
    "LaneSetDispatchPlanError",
    "CandidateDispatchFacts",
    "DispatchCandidate",
    "SkippedBucketIssue",
    "CoordinatorQueueState",
    "LaneSetDispatchPlan",
    "project_coordinator_queue",
    "build_dispatch_plan",
    "render_dispatch_plan_journal",
)
