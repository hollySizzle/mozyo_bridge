"""The whole-plan launch preflight (Redmine #13647 Tranche 2, Design Answer j#85645).

Tranche 1 fixed a lane's pair *geometry* (which way the panes split, by lane-role kind).
This is the other half of the design's authority model: the **per-slot** plan — which
workflow role each slot carries, which named profile that role selects, which provider runs
it, and with which argv — resolved and validated ONCE, before the launch takes its first
irreversible step.

Why a whole-plan type rather than per-slot checks inside the launch loop: the launch
prepares a *pair*. A per-slot validation that fires while the second slot is being launched
leaves the first one already live — a partial lane, which is exactly the failure mode the
existing session-start preflights exist to prevent. Cross-slot defects (two slots claiming
the same workflow role, two entries for one physical slot, the same slot asked for two
different profiles) are only visible when the whole plan is in hand, so the plan is the unit
of validation and every defect below is a **typed zero-start**: no workspace, no tab, no
agent, no startup action.

Authority boundary (Design Answer j#85645, j#84266 — kept enforced by what this module does
NOT do):

- ``workflow_role`` / ``profile_id`` are **plan-only**. They never become an mzb1 assigned
  name, ``MOZYO_AGENT_ROLE`` (a provider token), or a route / attestation / retire identity —
  those stay provider-token-bound, and nothing here writes or reads them.
- roles are never *inferred*. A role is supplied by a caller that resolved it from durable
  governance, or the plan is not built at all (the pre-#13647 launch, unchanged). A supplied
  role that is not in the known vocabulary is a refusal, never a fallback.
- the plan's provenance is a durable :class:`DecisionPointer` — the SAME anchor vocabulary
  the lifecycle authority record stores, not a parallel one. Zero anchors (unresolved) or
  more than one distinct anchor (ambiguous / contradicting governance) both refuse: the
  design's "矛盾/複数 anchor で ambiguous なら guess せず zero-start".

Pure: frozen dataclasses, a typed error, and total functions over injected vocabularies
(known providers / known roles are passed in, never imported from an adapter). No I/O, no
config read, no registry lookup — the composition root supplies those, which is what lets
the whole contract be pinned without a live herdr, a store, or a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Optional, Sequence

from mozyo_bridge.core.state.lane_kind import optional_lane_kind
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer


class LaneLaunchPlanError(ValueError):
    """A lane launch plan is unresolvable / contradictory; fail closed (zero-start)."""


@dataclass(frozen=True)
class SlotLaunchSpec:
    """One slot's caller-resolved launch intent (pure value, pre-launch).

    ``workflow_role`` is the slot's governed responsibility (the vocabulary the caller's
    registry defines); ``profile_id`` is the named profile that role selects; ``provider``
    is the runtime that executes it; ``launch_argv`` is the exact resolved argv tail.
    ``physical_slot`` names the pair position the slot occupies (a caller-supplied label
    such as ``"first"`` / ``"second"``, or ``""`` when the caller does not pin one) — it is
    a *plan* coordinate, never a live ``%pane`` id.

    Every field is caller-supplied on purpose: this type asserts nothing about where the
    values came from, and the plan below refuses whatever it cannot fully resolve.
    """

    workflow_role: str = ""
    profile_id: str = ""
    provider: str = ""
    launch_argv: tuple[str, ...] = ()
    physical_slot: str = ""


@dataclass(frozen=True)
class ResolvedLaneLaunchPlan:
    """A fully resolved, validated pair plan — the last thing fixed before the first write.

    ``lane_kind`` / ``lane_class`` / ``placement`` are the Tranche 1 pair geometry;
    ``source_anchor`` is the durable governance record the whole plan was resolved from;
    ``slots`` are the validated per-slot plans in launch order.

    Constructed only by :func:`resolve_lane_launch_plan`, so an instance is by construction
    one that passed every whole-plan validation.
    """

    lane_class: str
    lane_kind: Optional[str] = None
    placement: tuple[Optional[str], Optional[Sequence[str]]] = (None, None)
    source_anchor: Optional[DecisionPointer] = None
    slots: tuple[SlotLaunchSpec, ...] = field(default_factory=tuple)

    @property
    def workflow_roles(self) -> tuple[str, ...]:
        """The plan's workflow roles in launch order (plan-only; never a route identity)."""
        return tuple(slot.workflow_role for slot in self.slots)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(slot.provider for slot in self.slots)


def resolve_source_anchor(
    anchors: Sequence[DecisionPointer],
) -> Optional[DecisionPointer]:
    """The ONE durable anchor this plan is resolved from, or fail closed.

    ``()`` yields ``None`` — a caller with no governance anchor simply builds no
    role-bearing plan (the pre-#13647 launch). One anchor, or several that name the exact
    same durable record, resolves to that anchor. Two *different* anchors mean the governance
    inputs contradict each other about which decision authorizes this launch, and picking
    one would be a guess about whose decision is live — so it refuses (Design Answer j#85645:
    "矛盾/複数 anchor で ambiguous なら guess せず zero-start").
    """
    distinct = []
    for anchor in anchors:
        if anchor is None:
            continue
        if anchor not in distinct:
            distinct.append(anchor)
    if not distinct:
        return None
    if len(distinct) > 1:
        raise LaneLaunchPlanError(
            "the launch plan's governance anchor is ambiguous: "
            f"{len(distinct)} different durable records claim this launch "
            f"({', '.join(sorted(f'{a.source}#{a.issue_id}@{a.journal_id}' for a in distinct))}). "
            "Resolve which decision authorizes it before launching; the plan is not built "
            "from a guess."
        )
    return distinct[0]


def resolve_lane_launch_plan(
    *,
    lane_class: str,
    slot_specs: Sequence[SlotLaunchSpec],
    known_providers: Collection[str],
    known_roles: Collection[str],
    lane_kind: Optional[str] = None,
    placement: tuple[Optional[str], Optional[Sequence[str]]] = (None, None),
    anchors: Sequence[DecisionPointer] = (),
) -> ResolvedLaneLaunchPlan:
    """Validate a whole pair plan, or fail closed before anything is launched.

    Every refusal below is a :class:`LaneLaunchPlanError` raised while the plan is still
    just data — the caller has created no workspace, tab, agent or startup action — which is
    what makes "reject the pair" a safe outcome rather than a half-built lane.

    The validations (Design Answer j#85645 "whole-plan preflight"):

    1. **unresolved slot** — a slot missing its role, profile, provider or argv is not a
       plan, it is a hole. Launching it would either start the wrong thing or start nothing
       and leave its peer live;
    2. **unknown workflow role / unregistered provider** — a token outside the caller's
       vocabulary cannot select a profile or an adapter. It is refused rather than degraded
       to a default, because a silent default is how a slot ends up running someone else's
       responsibility;
    3. **duplicate workflow role** — two slots claiming the same governed responsibility is
       a contradiction about who does the work (and, downstream, about who a role-addressed
       handoff belongs to);
    4. **duplicate physical slot** — two entries for one pair position; whichever launched
       second would silently win;
    5. **same (physical slot, provider) with a different profile or argv** — the same slot
       asked to be two different things. Distinct providers legitimately carry distinct
       profiles, so only a *same-slot* conflict refuses;
    6. **ambiguous governance anchor** — see :func:`resolve_source_anchor`.

    ``slot_specs=()`` returns an empty plan (no roles, no anchor requirement): the launch is
    byte-for-byte the pre-#13647 one, so an unconfigured / legacy caller is unaffected.
    """
    kind = optional_lane_kind(lane_kind, source="ResolvedLaneLaunchPlan.lane_kind")
    anchor = resolve_source_anchor(anchors)
    slots = tuple(slot_specs)
    if not slots:
        return ResolvedLaneLaunchPlan(
            lane_class=lane_class,
            lane_kind=kind,
            placement=placement,
            source_anchor=anchor,
            slots=(),
        )
    seen_roles: dict = {}
    by_position: dict = {}
    for index, slot in enumerate(slots):
        where = f"slot {index} ({slot.provider or 'no provider'})"
        for label, value in (
            ("workflow role", slot.workflow_role),
            ("profile id", slot.profile_id),
            ("provider", slot.provider),
        ):
            if not value:
                raise LaneLaunchPlanError(
                    f"{where} has no {label}: a launch plan is not built from a partially "
                    "resolved slot (nothing was launched)"
                )
        if not slot.launch_argv:
            raise LaneLaunchPlanError(
                f"{where} resolved no launch argv for profile {slot.profile_id!r}; "
                "refusing to launch a slot whose command is unresolved"
            )
        if slot.workflow_role not in known_roles:
            raise LaneLaunchPlanError(
                f"{where} carries unknown workflow role {slot.workflow_role!r}; known "
                f"roles: {', '.join(sorted(known_roles))}. A supplied-but-unregistered "
                "role fails closed rather than falling back to a default responsibility"
            )
        if slot.provider not in known_providers:
            raise LaneLaunchPlanError(
                f"{where} names unregistered provider {slot.provider!r}; known providers: "
                f"{', '.join(sorted(known_providers))}"
            )
        if slot.workflow_role in seen_roles:
            raise LaneLaunchPlanError(
                f"workflow role {slot.workflow_role!r} is claimed by two slots "
                f"({seen_roles[slot.workflow_role]} and {where}); one responsibility "
                "cannot be carried by two slots of the same pair"
            )
        seen_roles[slot.workflow_role] = where
        position = slot.physical_slot
        if position:
            previous = by_position.get(position)
            if previous is None:
                by_position[position] = slot
            elif previous.provider == slot.provider:
                if (
                    previous.profile_id != slot.profile_id
                    or previous.launch_argv != slot.launch_argv
                ):
                    raise LaneLaunchPlanError(
                        f"physical slot {position!r} is planned twice for provider "
                        f"{slot.provider!r} with different profile / argv "
                        f"({previous.profile_id!r} vs {slot.profile_id!r}); the plan does "
                        "not choose between them"
                    )
                raise LaneLaunchPlanError(
                    f"physical slot {position!r} is planned twice for provider "
                    f"{slot.provider!r}; one pair position holds one slot"
                )
            else:
                raise LaneLaunchPlanError(
                    f"physical slot {position!r} is claimed by two providers "
                    f"({previous.provider!r} and {slot.provider!r}); one pair position "
                    "holds one slot"
                )
    return ResolvedLaneLaunchPlan(
        lane_class=lane_class,
        lane_kind=kind,
        placement=placement,
        source_anchor=anchor,
        slots=slots,
    )


__all__ = (
    "LaneLaunchPlanError",
    "ResolvedLaneLaunchPlan",
    "SlotLaunchSpec",
    "resolve_lane_launch_plan",
    "resolve_source_anchor",
)
