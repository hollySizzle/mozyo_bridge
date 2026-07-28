"""Multi-goal -> durable workstream dispatch plan (Redmine #14636).

When an owner hands the root coordinator *several* large goals at once, the question
#14558 has to answer is not "which lane do I fill next" but "**which durable workstreams
are these, and which of them may run at the same time**". The engine parts already exist —
#12855 (:mod:`...domain.workflow_fill_decision`) decides whether to fill at all, #12856
(:mod:`...domain.sublane_admission`) classifies a lane's durable state, #12921
(:mod:`...domain.lane_admission_risk`) decides one candidate's admission against the active
lane set, and #12920 (:mod:`...domain.lane_set_dispatch_plan`) plans one *bucket*. What is
missing is the step *before* all of them: several goals are not yet a lane set at all.

This module is that step, and only that step. It normalizes goal candidates into durable
**workstreams** keyed by project identity, derives the concrete relations between them, and
returns a typed, replayable plan saying — per workstream — whether it may be dispatched in
parallel, folded into / adopted onto an existing delegated coordinator (reuse), held behind
a named predecessor (serialize), or held outright (blocked / owner decision).

**It owns no admission authority.** Every decision that moves a workstream off "dispatch it"
comes from #12921 :func:`...lane_admission_risk.evaluate_lane_admission`, over the *closed*
risk vocabulary that module owns. This module's own contribution is the part #12921 cannot
see: #12921 is told "candidate X overlaps active lane Y", whereas here the overlaps hold
*between the candidates themselves*, and nobody has asserted them yet. So the derivation
runs first and its output is fed into the existing risk fields — there is no second
classifier and no second set of risk names.

Two vocabularies therefore appear on the output and they are deliberately not the same one:

- :data:`WORKSTREAM_RELATIONS` — *what was observed* between two units (same project, file
  overlap, invariant overlap, merge-order conflict, declared dependency). Evidence.
- the #12921 :data:`VALID_ADMISSION_RISKS` / :data:`ADMIT_*` tokens — *why the decision
  moved*. Authority.

The acceptance condition requires the five findings to be told apart individually, and a
relation is what tells them apart: a declared cross-workstream dependency and a declared
merge-order conflict both feed the single existing
:data:`...lane_admission_risk.RISK_MERGE_ORDER_CONFLICT` risk — both are exactly "these two
cannot land in an arbitrary order" — but they stay distinguishable as
:data:`RELATION_DECLARED_DEPENDENCY` vs :data:`RELATION_MERGE_ORDER_CONFLICT` in the
evidence. Minting a *risk* token for "dependency" would have created the competing decision
authority #14636 forbids; dropping the distinction would have failed the acceptance
condition. Recording it as evidence does neither.

**Same-project reuse is not a risk at all.** Two goals that name the same project identity
do not serialize against each other — they are the *same* workstream, and one delegated
coordinator serves both (:data:`RELATION_SAME_PROJECT`). That is why the derivation groups
before it admits: grouping first turns what would look like a total file overlap between two
candidates into one candidate with a wider surface.

**Intake defects are resolved before admission, never instead of it.** A goal that cannot be
identified (no id, no project identity, no objective, or a second copy of the same id
carrying different facts) never becomes a workstream — it is a :class:`RejectedGoal`, so
nothing downstream can dispatch it. A workstream that *is* identified but self-contradictory
(it depends on a goal that is not in the request, or sits on a dependency cycle) is still
put through the admission decision, and the typed defect is then combined with that decision
using #12921's own :data:`...lane_admission_risk.ADMISSION_DECISION_SEVERITY` ordering. A
defect can therefore only ever *hold* a workstream; it can never relax an admission decision
that was already more severe.

**Order independence and the digest.** The plan is a pure function of the *set* of goals,
not of the order they arrived in: every input tuple is trimmed, de-duplicated and sorted
during normalization, so a restart that replays the same goals in a different order produces
a byte-identical payload. :attr:`MultiGoalWorkstreamPlan.plan_digest` and
:attr:`PlannedWorkstream.workstream_digest` are the idempotency-key foundation #14637 builds
create-or-adopt on, and they answer two different questions on purpose: the workstream digest
covers *identity* (schema, project identity, member goals) so the same workstream keeps the
same key as its situation evolves, while the plan digest covers identity **and** every
disposition, so "the plan changed" is detectable. Both use a length-prefixed canonical
encoding under a domain tag (the :mod:`...domain.callback_recovery_key` idiom): plain
delimiter joining is not injective, and two different requests that digested identically
would have the second one silently suppressed as a replay.

Scope boundaries (carried over from #12919 / #12920 / #12921), the plan **must not** cross:

- it discovers nothing — every goal, project identity, dependency, surface and lane signal
  is supplied by the caller from the durable record; there is no network call and no Redmine
  read;
- it never creates or selects a Redmine issue, never creates / adopts a lane or a delegated
  coordinator, and never sends a handoff. Persisting the create-or-adopt state is #14637 and
  riding the plan through ``workflow step`` is #14638;
- it never treats coordinator convenience — including *the number of goals in the request* —
  as a reason to hold anything. Those signals are recorded through #12921's
  :data:`...lane_admission_risk.INVALID_SERIALIZATION_NONREASONS` and are never decisive.

**Where the rest of the family lives.** This module is the planner — the admission step, the
builder and the journal renderer. Its three siblings hold the parts more than one step needs,
and the design narrative above is not repeated in them:

- :mod:`.multi_goal_workstream_records` — the vocabularies and value objects, plus the
  normal form (:func:`.normalize_text` / :func:`.normalized_sequence`) that makes the plan a
  function of the *set* of goals;
- :mod:`.multi_goal_workstream_intake` — normalization, partitioning and relation derivation,
  i.e. everything that happens before a decision is taken;
- :mod:`.multi_goal_workstream_identity` — the two digests.

Everything in the family is pure: frozen dataclasses, no I/O.
"""

from __future__ import annotations

from typing import Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
    ADMISSION_DECISION_SEVERITY,
    ADMIT_BLOCKED,
    RISK_FILE_OVERLAP,
    RISK_INVARIANT_OVERLAP,
    RISK_MERGE_ORDER_CONFLICT,
    LaneAdmissionInputs,
    LaneAdmissionOutcome,
    evaluate_lane_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_identity import (
    plan_content_digest,
    workstream_identity_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_intake import (
    WorkstreamDraft,
    dependency_cycle_keys,
    derive_relations,
    group_into_workstreams,
    normalize_lane_signals,
    partition_goals,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_records import (
    INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE,
    WORKSTREAM_BLOCKED,
    WORKSTREAM_NEEDS_OWNER_DECISION,
    WORKSTREAM_PARALLEL,
    WORKSTREAM_REUSE,
    WORKSTREAM_SERIALIZE,
    _ADMISSION_TO_DISPOSITION,
    ExistingWorkstream,
    GoalCandidate,
    IntakeDefect,
    MultiGoalWorkstreamPlan,
    MultiGoalWorkstreamPlanError,
    PlannedWorkstream,
    WorkstreamRelation,
    normalize_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import ClassifiedLane, LaneSignal, classify_lane_state

# ---------------------------------------------------------------------------


def _admission_inputs(
    draft: WorkstreamDraft,
    relations: Sequence[WorkstreamRelation],
    lane_signals: Sequence[LaneSignal],
) -> LaneAdmissionInputs:
    """Assert this workstream's derived relations as #12921 risk facts (pure).

    Each relation is asserted as the risk :data:`_RELATION_TO_RISK` maps it to, alongside the
    caller's own active-lane overlap / dependency facts. The peer *workstream keys* ride in
    the overlap fields next to active *lane ids*: #12921 records those fields verbatim as the
    lanes a risk implicates and only resolves ``dependency_lanes`` against the classified
    signals, so a peer key names the offender correctly without being mistaken for a lane
    whose state must be read.
    """
    by_risk: dict[str, set[str]] = {}
    for relation in relations:
        risk = relation.risk
        if risk:
            by_risk.setdefault(risk, set()).add(relation.peer)

    def risk_lanes(risk: str, declared: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(by_risk.get(risk, set()) | set(declared)))

    return LaneAdmissionInputs(
        candidate_issue=draft.key,
        active_lane_signals=tuple(lane_signals),
        file_overlap_lanes=risk_lanes(RISK_FILE_OVERLAP, draft.lanes("file_overlap_lanes")),
        invariant_overlap_lanes=risk_lanes(
            RISK_INVARIANT_OVERLAP, draft.lanes("invariant_overlap_lanes")
        ),
        merge_order_conflict_lanes=risk_lanes(
            RISK_MERGE_ORDER_CONFLICT, draft.lanes("merge_order_conflict_lanes")
        ),
        dependency_lanes=draft.lanes("dependency_lanes"),
        unresolved_design_decision=draft.any_flag("unresolved_design_decision"),
        release_publish_gate_active=draft.any_flag("release_publish_gate_active"),
        credential_destructive_external_gate_active=draft.any_flag(
            "credential_destructive_external_gate_active"
        ),
        callback_miss_concern=draft.any_flag("callback_miss_concern"),
        coordinator_management_load=draft.any_flag("coordinator_management_load"),
        broad_bucket_only=draft.any_flag("broad_bucket_only"),
        goal_count_only=draft.any_flag("goal_count_only"),
    )


def _combine_with_defects(admission_decision: str, has_defects: bool) -> str:
    """Rank a typed intake defect against the admission decision (pure).

    Ranked with #12921's own :data:`ADMISSION_DECISION_SEVERITY`, so a defect can only ever
    hold a workstream — never relax a decision that was already more severe (an owner gate
    stays an owner gate even when the request also mis-referenced a goal id).
    """
    if not has_defects:
        return admission_decision
    order = list(ADMISSION_DECISION_SEVERITY)
    return min(
        (admission_decision, ADMIT_BLOCKED),
        key=lambda decision: order.index(decision),
    )


def _next_safe_action(
    key: str, disposition: str, reuse_target: str, blocked_by: Sequence[str]
) -> str:
    """The journal-friendly next safe step for one workstream (pure narrative).

    The *decision* is #12921's; only the wording is this module's, because #12921 has no word
    for "adopt the existing delegated coordinator" — it never sees two candidates at once.
    """
    against = ", ".join(blocked_by) if blocked_by else "none"
    if disposition == WORKSTREAM_PARALLEL:
        return (
            f"create one delegated coordinator for {key} and dispatch it alongside the "
            "other parallel workstreams; no concrete relation holds it"
        )
    if disposition == WORKSTREAM_REUSE:
        if reuse_target:
            return (
                f"adopt the existing delegated coordinator {reuse_target} for {key} instead "
                "of creating a second one for the same project identity"
            )
        return (
            f"create one delegated coordinator for {key} and dispatch the folded "
            "same-project goals through it, not one coordinator per goal"
        )
    if disposition == WORKSTREAM_SERIALIZE:
        return (
            f"serialize {key} behind {against}; dispatch once the file / invariant overlap, "
            "declared order, or coordinator-owned queue clears"
        )
    if disposition == WORKSTREAM_BLOCKED:
        return (
            f"hold {key}; resolve the blocked dependency or the typed intake defect "
            f"({against}) before any create-or-adopt"
        )
    return (
        f"escalate {key} to the owner through the coordinator; resolve the unresolved "
        "design decision / release / credential / destructive gate before dispatch"
    )


def _blocked_by(
    outcome: LaneAdmissionOutcome, decision: str, defects: Sequence[IntakeDefect]
) -> tuple[str, ...]:
    """The peers / lanes / defect subjects the hold is against (pure)."""
    names: set[str] = set()
    for risk in outcome.risks:
        if risk.decision == decision:
            names.update(risk.lanes)
    if decision == ADMIT_BLOCKED:
        names.update(defect.subject for defect in defects if defect.subject)
    return tuple(sorted(names - {""}))


# ---------------------------------------------------------------------------


def build_multi_goal_workstream_plan(
    goals: Sequence[GoalCandidate],
    *,
    active_lane_signals: Sequence[LaneSignal] = (),
    existing_workstreams: Sequence[ExistingWorkstream] = (),
) -> MultiGoalWorkstreamPlan:
    """Classify several goals into a typed workstream dispatch plan (pure, #14636).

    ``goals`` are the durable goal candidates, in any order — the plan is a function of the
    set, not the sequence. ``active_lane_signals`` are the durable-record facts of the lanes
    already running (classified once, with #12856's single authority, and reused for both the
    plan projection and #12921's dependency resolution). ``existing_workstreams`` name the
    delegated coordinators that already serve a project identity, so a matching workstream is
    adopted rather than duplicated.

    Runs in four steps, in this order for the reasons the module docstring gives: partition
    the goals (identifiable / rejected / collapsed replay), group the identifiable ones by
    project identity, derive the relations between the resulting workstreams, then take each
    workstream's dispatch decision from #12921 over those relations and hold it further if a
    typed intake defect ranks more severe.

    Raises :class:`MultiGoalWorkstreamPlanError` when an ``existing_workstreams`` entry is
    unusable — an entry with no project identity or no coordinator lane would silently fail to
    match and produce a *second* coordinator for a project that already has one, which is the
    exact duplication this plan exists to prevent.
    """
    reuse_by_key: dict[str, str] = {}
    for existing in existing_workstreams:
        identity = normalize_text(existing.project_identity)
        lane = normalize_text(existing.coordinator_lane)
        if not identity or not lane:
            raise MultiGoalWorkstreamPlanError(
                "existing workstream entries need both a project_identity and a "
                f"coordinator_lane; got project_identity={identity!r} "
                f"coordinator_lane={lane!r}. An unusable entry would not match, and the "
                "plan would create a second coordinator for a project that already has one"
            )
        previous = reuse_by_key.get(identity)
        if previous is not None and previous != lane:
            raise MultiGoalWorkstreamPlanError(
                f"project identity {identity!r} is claimed by two different delegated "
                f"coordinators ({previous!r} and {lane!r}); which one to adopt is not "
                "decidable from this request"
            )
        reuse_by_key[identity] = lane

    lane_signals, lane_defects = normalize_lane_signals(active_lane_signals)
    classified = tuple(
        ClassifiedLane(issue=signal.issue, state_class=classify_lane_state(signal))
        for signal in lane_signals
    )

    admissible, rejected, collapsed = partition_goals(goals)
    drafts = group_into_workstreams(admissible)
    key_of_goal = {goal.goal_id: goal.project_identity for goal in admissible}

    relations_by_key: dict[str, tuple[WorkstreamRelation, ...]] = {}
    defects_by_key: dict[str, list[IntakeDefect]] = {}
    for draft in drafts:
        relations, defects = derive_relations(draft, drafts, key_of_goal)
        relations_by_key[draft.key] = relations
        defects_by_key[draft.key] = list(defects)

    for key in dependency_cycle_keys(drafts, relations_by_key):
        defects_by_key[key].append(
            IntakeDefect(
                reason=INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE,
                subject=key,
                detail="the declared cross-workstream dependencies admit no landing order",
            )
        )

    workstreams: list[PlannedWorkstream] = []
    for draft in drafts:
        relations = relations_by_key[draft.key]
        defects = tuple(
            sorted(
                defects_by_key[draft.key],
                key=lambda item: (item.reason, item.subject, item.detail),
            )
        )
        outcome = evaluate_lane_admission(
            _admission_inputs(draft, relations, lane_signals)
        )
        decision = _combine_with_defects(outcome.decision, bool(defects))
        reuse_target = reuse_by_key.get(draft.key, "")
        disposition = _ADMISSION_TO_DISPOSITION[decision]
        if disposition == WORKSTREAM_PARALLEL and (
            reuse_target or len(draft.goals) > 1
        ):
            disposition = WORKSTREAM_REUSE
        blocked_by = _blocked_by(outcome, decision, defects)
        workstreams.append(
            PlannedWorkstream(
                workstream_key=draft.key,
                goal_ids=draft.goal_ids,
                disposition=disposition,
                admission_decision=decision,
                workstream_digest=workstream_identity_digest(draft.key, draft.goal_ids),
                objectives=tuple(goal.objective for goal in draft.goals),
                reuse_target=reuse_target,
                relations=relations,
                risk_reasons=outcome.risk_reasons,
                rejected_nonreasons=outcome.rejected_nonreasons,
                intake_defects=defects,
                blocked_by=blocked_by,
                file_surfaces=draft.file_surfaces,
                invariant_surfaces=draft.invariant_surfaces,
                next_safe_action=_next_safe_action(
                    draft.key, disposition, reuse_target, blocked_by
                ),
            )
        )

    workstreams_tuple = tuple(workstreams)
    return MultiGoalWorkstreamPlan(
        plan_digest=plan_content_digest(workstreams_tuple, rejected, lane_defects, collapsed),
        workstreams=workstreams_tuple,
        rejected_goals=rejected,
        classified_lanes=classified,
        plan_intake_defects=lane_defects,
        collapsed_duplicate_goals=collapsed,
    )


# ---------------------------------------------------------------------------


def _join(items: Sequence[str]) -> str:
    kept = [item for item in items if item]
    return ", ".join(kept) if kept else "none"


def render_multi_goal_plan_journal(plan: MultiGoalWorkstreamPlan) -> str:
    """Render the plan as a journal-friendly dispatch-plan narrative (pure).

    Produces the markdown a coordinator pastes into the Redmine dispatch-decision journal.
    Only goal / project / lane ids and the literal disposition / relation / defect / risk
    vocabularies are emitted — never private paths or operator-specific cockpit details.
    """
    counts = plan.counts_by_disposition
    lines = [
        "## Multi-goal workstream dispatch plan",
        "",
        f"- schema_version: {plan.schema_version}",
        f"- plan_digest: {plan.plan_digest}",
        "- counts_by_disposition:",
        f"  - parallel: {counts[WORKSTREAM_PARALLEL]}",
        f"  - reuse: {counts[WORKSTREAM_REUSE]}",
        f"  - serialize: {counts[WORKSTREAM_SERIALIZE]}",
        f"  - blocked: {counts[WORKSTREAM_BLOCKED]}",
        f"  - needs_owner_decision: {counts[WORKSTREAM_NEEDS_OWNER_DECISION]}",
        "- workstreams:",
    ]
    if plan.workstreams:
        for workstream in plan.workstreams:
            lines.append(
                f"  - {workstream.workstream_key}: {workstream.disposition} "
                f"(goals={_join(workstream.goal_ids)}; "
                f"reuse_target={workstream.reuse_target or 'none'}; "
                f"risks={_join(workstream.risk_reasons)}; "
                f"blocked_by={_join(workstream.blocked_by)})"
            )
            for relation in workstream.relations:
                lines.append(
                    f"    - relation {relation.relation} -> {relation.peer}: "
                    f"{_join(relation.shared)}"
                )
            for defect in workstream.intake_defects:
                lines.append(f"    - defect {defect.reason}: {defect.subject}")
    else:
        lines.append("  - none")

    lines.append("- rejected_goals:")
    if plan.rejected_goals:
        for goal in plan.rejected_goals:
            lines.append(
                f"  - {goal.goal_id or '(no id)'}: {goal.reason} "
                f"(project={goal.project_identity or 'none'})"
            )
    else:
        lines.append("  - none")

    lines.append("- active_lanes:")
    if plan.classified_lanes:
        lines.extend(
            f"  - {lane.issue}: {lane.state_class}" for lane in plan.classified_lanes
        )
    else:
        lines.append("  - none")

    lines.extend(
        [
            "- plan_intake_defects: "
            f"{_join([d.reason + ':' + d.subject for d in plan.plan_intake_defects])}",
            f"- collapsed_duplicate_goals: {_join(plan.collapsed_duplicate_goals)}",
            f"- advisory: {str(plan.advisory).lower()}",
        ]
    )
    return "\n".join(lines)


__all__ = (
    "build_multi_goal_workstream_plan",
    "render_multi_goal_plan_journal",
)
