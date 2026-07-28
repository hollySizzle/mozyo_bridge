"""Intake and relation derivation for the multi-goal workstream plan (#14636).

Everything that happens **before** a dispatch decision is taken: normalizing the request
into its order-independent form, partitioning the goals into the identifiable ones, the
typed rejections and the idempotent replays, grouping the identifiable ones into workstreams
by project identity, and deriving the concrete relations between the resulting workstreams.

Relation derivation is the part #12921 cannot do for itself: it is *told* that a candidate
overlaps an active lane, whereas here the overlaps hold between the candidates themselves
and nobody has asserted them yet. The derivation's output is fed into #12921's existing risk
fields by :mod:`.multi_goal_workstream_plan`; no decision is taken here.

Grouping runs before derivation on purpose — two goals about the same project share every
surface by construction, so deriving first would report a total conflict between two units
that were never two units.

Everything here is pure: frozen dataclasses, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_records import (
    INTAKE_AMBIGUOUS_DEPENDENCY,
    INTAKE_AMBIGUOUS_GOAL_ID,
    INTAKE_AMBIGUOUS_LANE_SIGNAL,
    INTAKE_MISSING_GOAL_ID,
    INTAKE_MISSING_OBJECTIVE,
    INTAKE_MISSING_PROJECT_IDENTITY,
    RELATION_DECLARED_DEPENDENCY,
    RELATION_FILE_OVERLAP,
    RELATION_INVARIANT_OVERLAP,
    RELATION_MERGE_ORDER_CONFLICT,
    RELATION_SAME_PROJECT,
    GoalCandidate,
    IntakeDefect,
    RejectedGoal,
    WorkstreamRelation,
    normalize_text,
    normalized_sequence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import LaneSignal

# ---------------------------------------------------------------------------
# Normalization (pure): the order-independence and duplicate rules.
# ---------------------------------------------------------------------------


def _normalized_goal(goal: GoalCandidate) -> GoalCandidate:
    """The order-independent normal form of one goal candidate (pure).

    Every text field is trimmed and every tuple is de-duplicated and sorted, so two requests
    that differ only in ordering or repetition normalize to the identical value — which is
    what makes the duplicate rule below a *content* comparison rather than a spelling one.
    """
    return GoalCandidate(
        goal_id=normalize_text(goal.goal_id),
        project_identity=normalize_text(goal.project_identity),
        objective=normalize_text(goal.objective),
        depends_on_goals=normalized_sequence(goal.depends_on_goals),
        merge_order_conflict_goals=normalized_sequence(goal.merge_order_conflict_goals),
        file_surfaces=normalized_sequence(goal.file_surfaces),
        invariant_surfaces=normalized_sequence(goal.invariant_surfaces),
        file_overlap_lanes=normalized_sequence(goal.file_overlap_lanes),
        invariant_overlap_lanes=normalized_sequence(goal.invariant_overlap_lanes),
        merge_order_conflict_lanes=normalized_sequence(goal.merge_order_conflict_lanes),
        dependency_lanes=normalized_sequence(goal.dependency_lanes),
        unresolved_design_decision=bool(goal.unresolved_design_decision),
        release_publish_gate_active=bool(goal.release_publish_gate_active),
        credential_destructive_external_gate_active=bool(
            goal.credential_destructive_external_gate_active
        ),
        callback_miss_concern=bool(goal.callback_miss_concern),
        coordinator_management_load=bool(goal.coordinator_management_load),
        broad_bucket_only=bool(goal.broad_bucket_only),
        goal_count_only=bool(goal.goal_count_only),
    )


def normalize_lane_signals(
    signals: Sequence[LaneSignal],
) -> tuple[tuple[LaneSignal, ...], tuple[IntakeDefect, ...]]:
    """Collapse identical lane signals, refuse conflicting ones, sort by issue (pure).

    An issue named twice with the *same* durable facts is one fact stated twice; named twice
    with *different* facts it has no decidable state class, so it is dropped with a typed
    :data:`INTAKE_AMBIGUOUS_LANE_SIGNAL` rather than silently resolved by input order — a
    dependency on it then fails closed to a hard block through #12921's own unreadable-
    dependency rule.
    """
    by_issue: dict[str, LaneSignal] = {}
    conflicting: set[str] = set()
    for signal in signals:
        issue = normalize_text(signal.issue)
        if not issue:
            continue
        existing = by_issue.get(issue)
        if existing is None:
            by_issue[issue] = signal
        elif existing != signal:
            conflicting.add(issue)
    defects = tuple(
        IntakeDefect(
            reason=INTAKE_AMBIGUOUS_LANE_SIGNAL,
            subject=issue,
            detail="the same active lane was supplied with conflicting durable facts",
        )
        for issue in sorted(conflicting)
    )
    kept = tuple(
        by_issue[issue] for issue in sorted(by_issue) if issue not in conflicting
    )
    return kept, defects


def partition_goals(
    goals: Sequence[GoalCandidate],
) -> tuple[tuple[GoalCandidate, ...], tuple[RejectedGoal, ...], tuple[str, ...]]:
    """Split normalized goals into admissible ones, rejections, and collapsed duplicates.

    Rejection covers the defects that make a goal *unidentifiable* (:data:`_REJECTING_DEFECTS`):
    no id, no project identity, no objective, or a second copy of one id carrying different
    facts. A byte-identical repeat is not a defect — it is an idempotent replay, so it is
    collapsed and recorded.
    """
    normalized = [_normalized_goal(goal) for goal in goals]

    by_id: dict[str, GoalCandidate] = {}
    conflicting: set[str] = set()
    collapsed: list[str] = []
    rejected: list[RejectedGoal] = []
    unidentified: list[GoalCandidate] = []

    for goal in normalized:
        if not goal.goal_id:
            unidentified.append(goal)
            continue
        existing = by_id.get(goal.goal_id)
        if existing is None:
            by_id[goal.goal_id] = goal
        elif existing == goal:
            collapsed.append(goal.goal_id)
        else:
            conflicting.add(goal.goal_id)

    for goal in unidentified:
        rejected.append(
            RejectedGoal(
                goal_id="",
                project_identity=goal.project_identity,
                reason=INTAKE_MISSING_GOAL_ID,
                detail="a goal without a durable id cannot be keyed or replayed",
            )
        )

    admissible: list[GoalCandidate] = []
    for goal_id in sorted(by_id):
        goal = by_id[goal_id]
        if goal_id in conflicting:
            rejected.append(
                RejectedGoal(
                    goal_id=goal_id,
                    project_identity=goal.project_identity,
                    reason=INTAKE_AMBIGUOUS_GOAL_ID,
                    detail="the same goal id was supplied with conflicting facts",
                )
            )
        elif not goal.project_identity:
            rejected.append(
                RejectedGoal(
                    goal_id=goal_id,
                    project_identity="",
                    reason=INTAKE_MISSING_PROJECT_IDENTITY,
                    detail="which workstream the goal belongs to is not stated",
                )
            )
        elif not goal.objective:
            rejected.append(
                RejectedGoal(
                    goal_id=goal_id,
                    project_identity=goal.project_identity,
                    reason=INTAKE_MISSING_OBJECTIVE,
                    detail="the goal states nothing for a delegated coordinator to take on",
                )
            )
        else:
            admissible.append(goal)

    # Sort on every field that is emitted, not just the id: two goals can both arrive with no
    # id at all, and ranking them by id alone would leave their order — and therefore the plan
    # digest — decided by the sequence they were supplied in.
    rejected.sort(
        key=lambda item: (item.goal_id, item.project_identity, item.reason, item.detail)
    )
    return tuple(admissible), tuple(rejected), tuple(sorted(set(collapsed)))


# ---------------------------------------------------------------------------
# Grouping and relation derivation (pure): the part #12921 cannot see for itself.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkstreamDraft:
    """The grouped, pre-admission form of one workstream.

    Not a plan record: it is the intermediate the derivation and the admission step share,
    holding the goals that were folded onto one project identity and the union of the facts
    they declared.
    """

    key: str
    goals: tuple[GoalCandidate, ...]

    @property
    def goal_ids(self) -> tuple[str, ...]:
        return tuple(goal.goal_id for goal in self.goals)

    @property
    def file_surfaces(self) -> tuple[str, ...]:
        return normalized_sequence(s for goal in self.goals for s in goal.file_surfaces)

    @property
    def invariant_surfaces(self) -> tuple[str, ...]:
        return normalized_sequence(s for goal in self.goals for s in goal.invariant_surfaces)

    def any_flag(self, name: str) -> bool:
        return any(getattr(goal, name) for goal in self.goals)

    def lanes(self, name: str) -> tuple[str, ...]:
        return normalized_sequence(lane for goal in self.goals for lane in getattr(goal, name))


def group_into_workstreams(
    goals: Sequence[GoalCandidate],
) -> tuple[WorkstreamDraft, ...]:
    """Group admissible goals by project identity, in sorted key order (pure).

    Grouping happens *before* any overlap derivation on purpose: two goals about the same
    project share every surface by construction, so deriving overlap first would report a
    total conflict between two units that were never two units.
    """
    by_key: dict[str, list[GoalCandidate]] = {}
    for goal in goals:
        by_key.setdefault(goal.project_identity, []).append(goal)
    return tuple(
        WorkstreamDraft(
            key=key, goals=tuple(sorted(by_key[key], key=lambda goal: goal.goal_id))
        )
        for key in sorted(by_key)
    )


def derive_relations(
    draft: WorkstreamDraft,
    peers: Sequence[WorkstreamDraft],
    key_of_goal: Mapping[str, str],
) -> tuple[tuple[WorkstreamRelation, ...], tuple[IntakeDefect, ...]]:
    """Derive every relation between one workstream and its peers (pure).

    Surface overlap is *derived* by intersecting the declared file / invariant surfaces —
    this is the fact #12921 is normally told and here nobody has told it yet. Declared
    dependencies and merge-order conflicts are *asserted* per goal against another goal id;
    they are resolved to that goal's workstream, and a reference to a goal that is not in the
    request is a typed :data:`INTAKE_AMBIGUOUS_DEPENDENCY` rather than a silently dropped
    constraint.

    A reference that resolves back to this same workstream (a same-project sibling) is not a
    relation: a unit cannot be ordered against itself.
    """
    relations: list[WorkstreamRelation] = []
    defects: list[IntakeDefect] = []

    if len(draft.goals) > 1:
        relations.append(
            WorkstreamRelation(
                relation=RELATION_SAME_PROJECT, peer=draft.key, shared=draft.goal_ids
            )
        )

    own_files = set(draft.file_surfaces)
    own_invariants = set(draft.invariant_surfaces)
    for peer in peers:
        if peer.key == draft.key:
            continue
        shared_files = sorted(own_files & set(peer.file_surfaces))
        if shared_files:
            relations.append(
                WorkstreamRelation(
                    relation=RELATION_FILE_OVERLAP,
                    peer=peer.key,
                    shared=tuple(shared_files),
                )
            )
        shared_invariants = sorted(own_invariants & set(peer.invariant_surfaces))
        if shared_invariants:
            relations.append(
                WorkstreamRelation(
                    relation=RELATION_INVARIANT_OVERLAP,
                    peer=peer.key,
                    shared=tuple(shared_invariants),
                )
            )

    for relation_name, field_name in (
        (RELATION_DECLARED_DEPENDENCY, "depends_on_goals"),
        (RELATION_MERGE_ORDER_CONFLICT, "merge_order_conflict_goals"),
    ):
        by_peer: dict[str, set[str]] = {}
        for goal in draft.goals:
            for referenced in getattr(goal, field_name):
                peer_key = key_of_goal.get(referenced)
                if peer_key is None:
                    defects.append(
                        IntakeDefect(
                            reason=INTAKE_AMBIGUOUS_DEPENDENCY,
                            subject=goal.goal_id,
                            detail=(
                                f"{relation_name} references {referenced}, which is not an "
                                f"admissible goal in this request"
                            ),
                        )
                    )
                    continue
                if peer_key == draft.key:
                    continue
                by_peer.setdefault(peer_key, set()).add(referenced)
        for peer_key in sorted(by_peer):
            relations.append(
                WorkstreamRelation(
                    relation=relation_name,
                    peer=peer_key,
                    shared=tuple(sorted(by_peer[peer_key])),
                )
            )

    relations.sort(key=lambda item: (item.relation, item.peer, item.shared))
    defects.sort(key=lambda item: (item.reason, item.subject, item.detail))
    return tuple(relations), tuple(defects)


def dependency_cycle_keys(
    drafts: Sequence[WorkstreamDraft],
    relations_by_key: Mapping[str, Sequence[WorkstreamRelation]],
) -> frozenset[str]:
    """The workstream keys that sit on a declared-dependency cycle (pure).

    A cycle means no serialization order exists, so every key on it is held rather than
    ordered arbitrarily. Computed over :data:`RELATION_DECLARED_DEPENDENCY` edges only:
    a merge-order conflict is symmetric by nature and says nothing about direction, so
    reading it as a directed edge would manufacture cycles that were never declared.
    """
    edges: dict[str, tuple[str, ...]] = {
        draft.key: tuple(
            sorted(
                relation.peer
                for relation in relations_by_key.get(draft.key, ())
                if relation.relation == RELATION_DECLARED_DEPENDENCY
            )
        )
        for draft in drafts
    }
    on_cycle: set[str] = set()
    # Iterative depth-first walk with an explicit stack: a request is caller-supplied and a
    # long declared chain must not depend on the interpreter's recursion limit.
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {key: WHITE for key in edges}
    for root in sorted(edges):
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        color[root] = GREY
        path.append(root)
        while stack:
            node, index = stack[-1]
            neighbours = edges.get(node, ())
            if index >= len(neighbours):
                stack.pop()
                color[node] = BLACK
                path.pop()
                continue
            stack[-1] = (node, index + 1)
            neighbour = neighbours[index]
            if neighbour not in color:
                continue
            if color[neighbour] == GREY:
                # Back edge: everything from the neighbour onward on the current path is on
                # the cycle.
                start = path.index(neighbour)
                on_cycle.update(path[start:])
            elif color[neighbour] == WHITE:
                color[neighbour] = GREY
                path.append(neighbour)
                stack.append((neighbour, 0))
    return frozenset(on_cycle)


__all__ = (
    "WorkstreamDraft",
    "normalize_lane_signals",
    "partition_goals",
    "group_into_workstreams",
    "derive_relations",
    "dependency_cycle_keys",
)
