"""Vocabulary and value objects of the multi-goal workstream dispatch plan (#14636).

The records this module owns — the goal / existing-workstream inputs, the relation,
defect, rejection and workstream outputs, and the closed vocabularies they are written in —
are shared by every step of the plan, so they live apart from the step that produces them.
The design narrative (why the relation vocabulary is separate from the #12921 risk
vocabulary, why same-project goals fold before overlap is derived, what the two digests are
for) is in :mod:`.multi_goal_workstream_plan`; it is not repeated here.

Also owns the **normal form** the records are in: :func:`normalize_text` and
:func:`normalized_sequence` are what make the plan a function of the *set* of goals rather
than the order they arrived in, and every producer in this family applies them before a
record is built or digested.

Everything here is pure: frozen dataclasses, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
    ADMIT_ALLOW_DISPATCH,
    ADMIT_BLOCKED,
    ADMIT_NEEDS_OWNER_DECISION,
    ADMIT_SERIALIZE,
    RISK_FILE_OVERLAP,
    RISK_INVARIANT_OVERLAP,
    RISK_MERGE_ORDER_CONFLICT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import ClassifiedLane

# ---------------------------------------------------------------------------
# Plan schema.
# ---------------------------------------------------------------------------

#: The plan schema version. It participates in both digests, so a future field set can never
#: be mistaken for this one: a v2 plan digests differently even if every v1 field matches.
PLAN_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Workstream dispatch disposition vocabulary (machine-readable; literal regardless of UI
# language). A thin rename of the #12921 admission decision, plus the one distinction #12921
# cannot make because it never sees two candidates at once: reuse.
# ---------------------------------------------------------------------------

#: Dispatch this workstream now, as its own new delegated coordinator, alongside the others.
WORKSTREAM_PARALLEL = "parallel"
#: Dispatch this workstream now, but into a delegated coordinator that already serves this
#: project identity — either an existing one named in the request, or the single one this
#: plan folds several same-project goals onto. Never a new coordinator per goal.
WORKSTREAM_REUSE = "reuse"
#: Hold this workstream behind named predecessors (overlap / ordering / coordinator queue).
WORKSTREAM_SERIALIZE = "serialize"
#: Hold this workstream outright (a blocked dependency, or a typed intake defect).
WORKSTREAM_BLOCKED = "blocked"
#: Hold this workstream for the owner (unresolved design / release / credential / destructive).
WORKSTREAM_NEEDS_OWNER_DECISION = "needs_owner_decision"

WORKSTREAM_DISPOSITIONS = frozenset(
    {
        WORKSTREAM_PARALLEL,
        WORKSTREAM_REUSE,
        WORKSTREAM_SERIALIZE,
        WORKSTREAM_BLOCKED,
        WORKSTREAM_NEEDS_OWNER_DECISION,
    }
)

#: The dispositions that mean "this workstream may be acted on now". Everything else is a
#: hold, and #14637 / #14638 must not create, adopt or dispatch against it.
ACTIONABLE_DISPOSITIONS = frozenset({WORKSTREAM_PARALLEL, WORKSTREAM_REUSE})

#: The #12921 decision -> disposition map. ``allow_dispatch`` is refined to
#: :data:`WORKSTREAM_REUSE` when the workstream has a reuse basis; every other decision is a
#: hold and carries through unchanged.
_ADMISSION_TO_DISPOSITION: Mapping[str, str] = {
    ADMIT_ALLOW_DISPATCH: WORKSTREAM_PARALLEL,
    ADMIT_SERIALIZE: WORKSTREAM_SERIALIZE,
    ADMIT_BLOCKED: WORKSTREAM_BLOCKED,
    ADMIT_NEEDS_OWNER_DECISION: WORKSTREAM_NEEDS_OWNER_DECISION,
}

# ---------------------------------------------------------------------------
# Relation vocabulary (evidence: what was observed between two units). Closed set. NOT a
# decision vocabulary — see the module docstring.
# ---------------------------------------------------------------------------

#: Several goals in the request name the same project identity, so they are one workstream.
RELATION_SAME_PROJECT = "same_project"
#: Two workstreams declared an intersecting file surface.
RELATION_FILE_OVERLAP = "file_overlap"
#: Two workstreams declared an intersecting invariant / behavioral surface.
RELATION_INVARIANT_OVERLAP = "invariant_overlap"
#: A caller-asserted known merge-order conflict between two workstreams.
RELATION_MERGE_ORDER_CONFLICT = "merge_order_conflict"
#: A goal declared that it depends on another goal in the same request.
RELATION_DECLARED_DEPENDENCY = "declared_dependency"

WORKSTREAM_RELATIONS = frozenset(
    {
        RELATION_SAME_PROJECT,
        RELATION_FILE_OVERLAP,
        RELATION_INVARIANT_OVERLAP,
        RELATION_MERGE_ORDER_CONFLICT,
        RELATION_DECLARED_DEPENDENCY,
    }
)

#: Relation -> the existing #12921 risk it is asserted as. :data:`RELATION_SAME_PROJECT` is
#: absent on purpose: same-project goals are folded into one workstream, so there is nothing
#: left to serialize against. A declared dependency and a declared merge-order conflict share
#: :data:`RISK_MERGE_ORDER_CONFLICT` because both say the same thing to the admission
#: authority — "these two cannot land in an arbitrary order" — while staying distinguishable
#: as evidence.
_RELATION_TO_RISK: Mapping[str, str] = {
    RELATION_FILE_OVERLAP: RISK_FILE_OVERLAP,
    RELATION_INVARIANT_OVERLAP: RISK_INVARIANT_OVERLAP,
    RELATION_MERGE_ORDER_CONFLICT: RISK_MERGE_ORDER_CONFLICT,
    RELATION_DECLARED_DEPENDENCY: RISK_MERGE_ORDER_CONFLICT,
}

# ---------------------------------------------------------------------------
# Typed intake defects (resolved before admission; a defect can only hold, never relax).
# ---------------------------------------------------------------------------

#: The goal carries no durable id, so it cannot be keyed, replayed or de-duplicated.
INTAKE_MISSING_GOAL_ID = "missing_goal_id"
#: The goal names no project identity, so which workstream it belongs to is unknown.
INTAKE_MISSING_PROJECT_IDENTITY = "missing_project_identity"
#: The goal states no objective, so there is nothing for a delegated coordinator to take on.
INTAKE_MISSING_OBJECTIVE = "missing_objective"
#: The same goal id appears more than once carrying *different* facts. Which copy is
#: authoritative is not decidable from the request, so neither is admitted. (A byte-identical
#: repeat is an idempotent replay, not a defect: it is collapsed and recorded under
#: :attr:`MultiGoalWorkstreamPlan.collapsed_duplicate_goals`.)
INTAKE_AMBIGUOUS_GOAL_ID = "ambiguous_goal_id"
#: A goal declared a dependency on, or a merge-order conflict with, a goal id that is not in
#: the request. The referent is unknown, so the ordering constraint cannot be satisfied.
INTAKE_AMBIGUOUS_DEPENDENCY = "ambiguous_dependency"
#: The declared cross-workstream dependencies contain a cycle, so no serialization order
#: exists.
INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE = "ambiguous_dependency_cycle"
#: The same active lane issue appears more than once carrying *different* durable facts, so
#: its state class is not decidable. (Byte-identical repeats are collapsed.)
INTAKE_AMBIGUOUS_LANE_SIGNAL = "ambiguous_lane_signal"

INTAKE_DEFECTS = frozenset(
    {
        INTAKE_MISSING_GOAL_ID,
        INTAKE_MISSING_PROJECT_IDENTITY,
        INTAKE_MISSING_OBJECTIVE,
        INTAKE_AMBIGUOUS_GOAL_ID,
        INTAKE_AMBIGUOUS_DEPENDENCY,
        INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE,
        INTAKE_AMBIGUOUS_LANE_SIGNAL,
    }
)

#: The defects that make a goal unidentifiable, so it never becomes a workstream at all —
#: it is a :class:`RejectedGoal` instead. The remaining defects are found on workstreams that
#: *are* identified, and hold them through :func:`_combine_with_defects`.
REJECTING_INTAKE_DEFECTS = frozenset(
    {
        INTAKE_MISSING_GOAL_ID,
        INTAKE_MISSING_PROJECT_IDENTITY,
        INTAKE_MISSING_OBJECTIVE,
        INTAKE_AMBIGUOUS_GOAL_ID,
    }
)


class MultiGoalWorkstreamPlanError(ValueError):
    """An invalid plan construction (never a partial plan; fail-closed at the boundary)."""


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalCandidate:
    """One durable goal candidate as the caller read it from the durable record.

    Nothing here is discovered: the caller supplies every field. ``goal_id`` is the goal's
    durable identity (stable across restarts — it is what makes a replay recognisable),
    ``project_identity`` is the durable key of the project / workstream the goal belongs to
    (goals sharing it are *one* workstream), and ``objective`` is the short statement of what
    the goal is.

    The cross-goal facts name **other goal ids in the same request**:
    ``depends_on_goals`` (this goal must land after them) and
    ``merge_order_conflict_goals`` (a known merge-order conflict). The surface facts —
    ``file_surfaces`` (paths / path prefixes) and ``invariant_surfaces`` (named behavioural
    invariants) — are the declared surfaces this module intersects to *derive* overlap
    between workstreams; they are declarations, never a repository scan.

    The active-lane facts are passed straight through to #12921 with their meaning unchanged:
    ``file_overlap_lanes`` / ``invariant_overlap_lanes`` / ``merge_order_conflict_lanes``
    name active lane issue ids this goal overlaps, and ``dependency_lanes`` name active lanes
    it genuinely depends on (a lane in a blocked / callback-failed / unreadable state blocks
    the workstream; a coordinator-owned queue serialises it).

    ``unresolved_design_decision`` / ``release_publish_gate_active`` /
    ``credential_destructive_external_gate_active`` are the owner-territory gates.
    ``callback_miss_concern`` / ``coordinator_management_load`` / ``broad_bucket_only`` /
    ``goal_count_only`` are the rejected coordinator-convenience signals: recorded on the
    outcome, never decisive.
    """

    goal_id: str
    project_identity: str
    objective: str = ""
    depends_on_goals: tuple[str, ...] = ()
    merge_order_conflict_goals: tuple[str, ...] = ()
    file_surfaces: tuple[str, ...] = ()
    invariant_surfaces: tuple[str, ...] = ()
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
    goal_count_only: bool = False


@dataclass(frozen=True)
class ExistingWorkstream:
    """A delegated coordinator that already serves a project identity (caller-supplied).

    ``project_identity`` is the durable key it serves and ``coordinator_lane`` is its durable
    lane / anchor id. A planned workstream whose identity matches one of these is dispatched
    onto it (:data:`WORKSTREAM_REUSE`) instead of creating a second coordinator for the same
    project. This module only *records* that adoption target; performing the create-or-adopt
    is #14637.
    """

    project_identity: str
    coordinator_lane: str


# ---------------------------------------------------------------------------
# Output records.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeDefect:
    """One typed intake defect, with the subject it was found on.

    ``reason`` is one of :data:`INTAKE_DEFECTS`; ``subject`` is the goal id, project identity
    or lane issue it was found on (empty when the goal carried no id at all); ``detail`` is
    the short, non-private narrative (ids and literal vocabulary only).
    """

    reason: str
    subject: str
    detail: str = ""

    def as_payload(self) -> dict[str, object]:
        return {"reason": self.reason, "subject": self.subject, "detail": self.detail}


@dataclass(frozen=True)
class RejectedGoal:
    """A goal that never became a workstream, and the typed reason (#14558: typed blocked).

    Rejection happens before any admission decision, because the goal could not be
    *identified*: without an id, a project identity, an objective, or with a second copy of
    its id carrying different facts, there is no unit to admit. Nothing downstream may
    dispatch a rejected goal.
    """

    goal_id: str
    project_identity: str
    reason: str
    detail: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "goal_id": self.goal_id,
            "project_identity": self.project_identity,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class WorkstreamRelation:
    """One concrete relation observed between this workstream and a peer (evidence).

    ``relation`` is one of :data:`WORKSTREAM_RELATIONS`; ``peer`` is the peer workstream key
    (or this workstream's own key for :data:`RELATION_SAME_PROJECT`, which is internal to the
    unit); ``shared`` names the concrete shared items (the intersecting file / invariant
    surfaces, or the folded goal ids) so the journal says *what* overlapped rather than only
    *that* something did.
    """

    relation: str
    peer: str
    shared: tuple[str, ...] = ()

    @property
    def risk(self) -> str:
        """The #12921 risk this relation is asserted as, or ``""`` when it is not a risk."""
        return _RELATION_TO_RISK.get(self.relation, "")

    def as_payload(self) -> dict[str, object]:
        return {
            "relation": self.relation,
            "peer": self.peer,
            "shared": list(self.shared),
            "risk": self.risk,
        }


@dataclass(frozen=True)
class PlannedWorkstream:
    """One durable workstream with its typed dispatch disposition (#14636 acceptance).

    ``workstream_key`` is the project identity — the durable grouping key, so two goals about
    the same project are this one unit and share one delegated coordinator. ``goal_ids`` are
    its member goals in sorted order; more than one means the plan folded same-project goals
    together (:data:`RELATION_SAME_PROJECT`).

    ``disposition`` is the headline :data:`WORKSTREAM_*` token; ``admission_decision`` is the
    #12921 :data:`ADMIT_*` token it was derived from, kept so the authority behind the
    disposition stays visible. ``risk_reasons`` / ``rejected_nonreasons`` are #12921's
    evidence and ``relations`` is this module's. ``intake_defects`` are typed defects found
    on an *identified* workstream (an unknown referent, a dependency cycle); they combine
    with the admission decision through #12921's own severity ordering and can only hold.

    ``reuse_target`` is the existing delegated coordinator this workstream would be adopted
    onto (empty when a new one would be created), and ``creates_new_coordinator`` is the same
    fact as a predicate. Both are recorded whatever the disposition, so a held workstream
    still says where it would land once the hold clears.

    ``blocked_by`` names the peer workstreams and active lanes the hold is against.
    ``workstream_digest`` is the identity key (schema + project identity + member goals) that
    #14637 builds create-or-adopt idempotency on; it deliberately excludes the disposition so
    the same workstream keeps one key as its situation evolves.
    """

    workstream_key: str
    goal_ids: tuple[str, ...]
    disposition: str
    admission_decision: str
    workstream_digest: str
    objectives: tuple[str, ...] = ()
    reuse_target: str = ""
    relations: tuple[WorkstreamRelation, ...] = ()
    risk_reasons: tuple[str, ...] = ()
    rejected_nonreasons: tuple[str, ...] = ()
    intake_defects: tuple[IntakeDefect, ...] = ()
    blocked_by: tuple[str, ...] = ()
    file_surfaces: tuple[str, ...] = ()
    invariant_surfaces: tuple[str, ...] = ()
    next_safe_action: str = ""

    @property
    def actionable(self) -> bool:
        """True when this workstream may be created / adopted / dispatched now."""
        return self.disposition in ACTIONABLE_DISPOSITIONS

    @property
    def creates_new_coordinator(self) -> bool:
        """True when no existing delegated coordinator serves this project identity."""
        return not self.reuse_target

    @property
    def folded_goals(self) -> bool:
        """True when several same-project goals were folded into this one workstream."""
        return len(self.goal_ids) > 1

    def as_payload(self) -> dict[str, object]:
        return {
            "workstream_key": self.workstream_key,
            "goal_ids": list(self.goal_ids),
            "objectives": list(self.objectives),
            "disposition": self.disposition,
            "admission_decision": self.admission_decision,
            "actionable": self.actionable,
            "workstream_digest": self.workstream_digest,
            "reuse_target": self.reuse_target,
            "creates_new_coordinator": self.creates_new_coordinator,
            "folded_goals": self.folded_goals,
            "relations": [relation.as_payload() for relation in self.relations],
            "risk_reasons": list(self.risk_reasons),
            "rejected_nonreasons": list(self.rejected_nonreasons),
            "intake_defects": [defect.as_payload() for defect in self.intake_defects],
            "blocked_by": list(self.blocked_by),
            "file_surfaces": list(self.file_surfaces),
            "invariant_surfaces": list(self.invariant_surfaces),
            "next_safe_action": self.next_safe_action,
        }


@dataclass(frozen=True)
class MultiGoalWorkstreamPlan:
    """The replayable, order-independent multi-goal dispatch plan (#14636).

    ``workstreams`` are the planned units in ``workstream_key`` order; ``rejected_goals`` are
    the goals that never became one; ``classified_lanes`` is the active lane set projected
    onto its #12856 state class (the same single authority #12921 uses), in lane order;
    ``plan_intake_defects`` are defects that belong to the request rather than to any one
    workstream (a conflicting duplicate lane signal). ``collapsed_duplicate_goals`` records
    the byte-identical repeats that were folded away, so an idempotent replay is visible
    rather than silent.

    ``plan_digest`` covers the schema, every workstream identity **and** every disposition,
    plus the rejections and defects: it answers "is this the same plan", which is what a
    restart needs before it re-dispatches anything. It deliberately excludes the descriptive
    prose (``objectives``) — those change nothing about what would be dispatched, and folding
    them in would make a reworded goal look like a new plan and re-dispatch it. Everything
    that *is* decision-relevant reaches the digest: declared surfaces are covered through the
    relations they produce. ``advisory`` is always true — this surface plans, it never acts.
    """

    plan_digest: str
    workstreams: tuple[PlannedWorkstream, ...] = ()
    rejected_goals: tuple[RejectedGoal, ...] = ()
    classified_lanes: tuple[ClassifiedLane, ...] = ()
    plan_intake_defects: tuple[IntakeDefect, ...] = ()
    collapsed_duplicate_goals: tuple[str, ...] = ()
    schema_version: int = PLAN_SCHEMA_VERSION
    advisory: bool = True

    @property
    def counts_by_disposition(self) -> dict[str, int]:
        counts = {name: 0 for name in sorted(WORKSTREAM_DISPOSITIONS)}
        for workstream in self.workstreams:
            counts[workstream.disposition] = counts.get(workstream.disposition, 0) + 1
        return counts

    @property
    def actionable_workstreams(self) -> tuple[PlannedWorkstream, ...]:
        return tuple(w for w in self.workstreams if w.actionable)

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_digest": self.plan_digest,
            "advisory": self.advisory,
            "counts_by_disposition": self.counts_by_disposition,
            "workstreams": [w.as_payload() for w in self.workstreams],
            "rejected_goals": [g.as_payload() for g in self.rejected_goals],
            "classified_lanes": [lane.as_payload() for lane in self.classified_lanes],
            "plan_intake_defects": [d.as_payload() for d in self.plan_intake_defects],
            "collapsed_duplicate_goals": list(self.collapsed_duplicate_goals),
        }


# ---------------------------------------------------------------------------
# Normal form (pure): what makes the plan order-independent.
# ---------------------------------------------------------------------------


def normalize_text(value: object) -> str:
    """The trimmed textual form of a caller-supplied value (``None`` -> empty)."""
    return str(value if value is not None else "").strip()


def normalized_sequence(values: Iterable[str]) -> tuple[str, ...]:
    """Trim, drop empties, de-duplicate and sort — the order-independence normal form."""
    return tuple(sorted({normalize_text(value) for value in values} - {""}))


__all__ = (
    "PLAN_SCHEMA_VERSION",
    "WORKSTREAM_PARALLEL",
    "WORKSTREAM_REUSE",
    "WORKSTREAM_SERIALIZE",
    "WORKSTREAM_BLOCKED",
    "WORKSTREAM_NEEDS_OWNER_DECISION",
    "WORKSTREAM_DISPOSITIONS",
    "ACTIONABLE_DISPOSITIONS",
    "RELATION_SAME_PROJECT",
    "RELATION_FILE_OVERLAP",
    "RELATION_INVARIANT_OVERLAP",
    "RELATION_MERGE_ORDER_CONFLICT",
    "RELATION_DECLARED_DEPENDENCY",
    "WORKSTREAM_RELATIONS",
    "INTAKE_MISSING_GOAL_ID",
    "INTAKE_MISSING_PROJECT_IDENTITY",
    "INTAKE_MISSING_OBJECTIVE",
    "INTAKE_AMBIGUOUS_GOAL_ID",
    "INTAKE_AMBIGUOUS_DEPENDENCY",
    "INTAKE_AMBIGUOUS_DEPENDENCY_CYCLE",
    "INTAKE_AMBIGUOUS_LANE_SIGNAL",
    "INTAKE_DEFECTS",
    "REJECTING_INTAKE_DEFECTS",
    "MultiGoalWorkstreamPlanError",
    "GoalCandidate",
    "ExistingWorkstream",
    "IntakeDefect",
    "RejectedGoal",
    "WorkstreamRelation",
    "PlannedWorkstream",
    "MultiGoalWorkstreamPlan",
    "normalize_text",
    "normalized_sequence",
)
