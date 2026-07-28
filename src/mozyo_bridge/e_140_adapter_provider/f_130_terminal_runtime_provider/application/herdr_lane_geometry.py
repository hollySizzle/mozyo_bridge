"""herdr pane GEOMETRY decisions — the pure placement core (Redmine #13411 / #13646).

Cohesive sibling of :mod:`herdr_lane_topology` (which resolves *which container* a
lane joins — the #13380 workspace axis and the #13411 tab axis) and of
:mod:`herdr_session_start` (the subprocess-driving orchestrator). This module owns
the complementary question: **given the container, how are this run's slots placed
inside it** — launch order, container occupancy, split direction, and the
first-launch focus.

Extracted from ``herdr_lane_topology`` as a leaf so that module stays inside its
module-health budget while the geometry decisions grow the second split axis
Redmine #14567 adds (an inter-lane direction distinct from the pair's own). The
extraction was a boundary move: every function below arrived verbatim, and the
#14567 changes are layered on top of it, not folded into the move.

Dependency direction is one-way — this module imports the inventory reader
:func:`herdr_lane_topology._lane_live_slot_tabs`; ``herdr_lane_topology`` imports
nothing from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    product_default_placement,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _lane_live_slot_tabs,
)


def resolve_launch_order(
    providers: Sequence[str], config_order: Optional[Sequence[str]]
) -> list:
    """The requested providers, reordered by the configured launch order (pure).

    Config-driven placement (Redmine #13646, Design Answer j#76564 Q2): ``config_order`` is
    a full provider permutation naming who occupies the container FIRST (and therefore who
    splits beside them). Reordering the REQUESTED providers is the only way to realize a
    role order, because herdr ``agent start`` has no pane-target flag — order is launch
    order (live ``--help`` characterization j#76559).

    It never grows the request: a single-provider heal stays a single provider (an ``order``
    naming both providers must not launch an unrequested peer). ``None`` returns the
    requested sequence unchanged (byte-invariant).
    """
    if config_order is None:
        return list(providers)
    rank = {provider: index for index, provider in enumerate(config_order)}
    return sorted(providers, key=lambda provider: rank.get(provider, len(rank)))


def initial_lane_occupancy(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
    *,
    lane_class: str,
    target_tab: str,
    lane_slot_tabs: Sequence[str],
    count_default_lane: bool,
) -> int:
    """How many of THIS LANE's slots already occupy the container a launch splits into.

    The container differs by lane class (Redmine #13411 tab axis + #13646 default axis):

    - a ``sublane``'s container is its dedicated lane TAB, so only same-lane live slots
      ALREADY IN ``target_tab`` count. Read from the whole live inventory, not this run's
      requested plans (review j#74433 finding 1): a single-provider heal requests one
      provider, so the lane's OTHER live slot is in the inventory but never in ``plans`` —
      counting requested adopts alone would drop the split a heal beside a live tabbed
      sibling needs. A freshly minted tab starts empty (0), so its first launch occupies
      and its second splits. A loose (pre-#13411, tab-less) heal has no ``target_tab`` and
      counts 0, so it stays loose — byte-invariant.
    - the ``default`` lane has no tab, so its container is the project WORKSPACE itself:
      the coordinator pair's own live slots in ``target_workspace``. This is what makes a
      fresh pair's 2nd slot split beside the 1st and a heal split beside the live sibling.
      ``count_default_lane`` is False when nothing launches, when there is no resolved
      target workspace, or when the run resolved no split direction at all — the last of
      which no longer includes "the operator declared nothing", because Redmine #14568
      gives the default lane a product-default direction.

    Live slots count regardless of how this run classified them (adopt / unattested /
    stale): they occupy a pane either way, and a launch must split beside a live pane.

    Under ``shared_tab`` (Redmine #14567) this is only HALF the question: the container is
    shared with other lanes, so "does this slot split at all" is answered by
    :func:`initial_container_occupancy` (every lane's slots) while this — the lane's own
    count — answers "is this slot the lane's FIRST, and therefore placed on the inter-lane
    axis rather than beside its own pair sibling". Under ``per_lane_tab`` and for the
    default lane the two counts are equal by construction, which is why the pre-#14567
    single-counter behaviour is preserved exactly.
    """
    if lane_class == "sublane":
        return sum(1 for tab in lane_slot_tabs if tab == target_tab) if target_tab else 0
    if not count_default_lane:
        return 0
    return len(_lane_live_slot_tabs(rows, workspace_id, target_workspace, lane_id))


def initial_container_occupancy(
    lane_occupancy: int,
    *,
    shared_tab: bool,
    target_tab: str,
    host_slot_tabs: "Sequence[tuple[str, str]]",
) -> int:
    """How many slots — of ANY lane — already occupy the container (Redmine #14567).

    The predicate "does a launching slot split, or does it occupy an empty container".

    - ``per_lane_tab`` (and every default-lane launch): the container holds only this
      lane's slots, so this IS ``lane_occupancy`` — byte-for-byte the pre-#14567 single
      counter. ``host_slot_tabs`` is not consulted at all.
    - ``shared_tab``: the container is the project's one shared tab, so every lane's live
      slots in it count. Counting only the lane's own would make the SECOND lane's first
      launch believe the tab is empty and emit no ``--split``, landing it on herdr's own
      default geometry instead of beside the lanes already there.

    ``host_slot_tabs`` is the ``(lane_id, tab_id)`` inventory of the host workspace
    (:func:`herdr_shared_tab.host_lane_slot_tabs`); only slots already in ``target_tab``
    count, so a loose pane or a slot in another tab never inflates the count. A tab this
    run just minted has no slots and correctly counts 0.
    """
    if not shared_tab:
        return lane_occupancy
    if not target_tab:
        return 0
    return sum(1 for _, tab in host_slot_tabs if tab == target_tab)


def resolve_placement_policy_for_role(
    lane_placement: object, lane_class: str, lane_kind: "Optional[str]"
) -> "tuple[Optional[str], Optional[tuple[str, ...]]]":
    """This lane's EFFECTIVE ``(split, order)`` — declaration, else product default.

    The one adapter between the repo-local ``lane_placement`` config record (Redmine
    #13646 / #13647) and the pure placement decisions below, so the session-start
    composition root holds no config-shape knowledge and this module holds no placement
    fallback of its own: the whole ``by_lane_kind > lane_class > product default``
    precedence lives on the config type (``resolve_effective``), and this returns what it
    decided as the plain pair the decisions below consume.

    Redmine #14568 moved the bottom of that chain from "inherit the legacy launch
    discipline" to :func:`product_default_placement` (``split: down`` on both lane
    classes), so an undeclared workspace now gets a real, non-``None`` split here. That is
    what turns on the first-launch focus and the default-lane occupancy read below — both
    of which used to key on "did the operator declare anything", a question no longer worth
    asking now that the product always has an opinion.

    A ``None`` config object (a caller with no placement policy at all — no production
    launch path) resolves to the product default too, so the pure-function contract and the
    configured one cannot drift into two different geometries.
    """
    if lane_placement is None:
        resolved = product_default_placement(lane_class)
    else:
        resolved = lane_placement.resolve_effective(lane_class, lane_kind)  # type: ignore[attr-defined]
    return resolved.split, resolved.order


#: The direction a lane's FIRST slot splits on when it joins a shared tab another lane
#: already occupies (Redmine #14567, Design Answer j#91144 Decision 4): each lane becomes a
#: column, and ``lane_placement`` then splits the pair inside that column (product default
#: ``down``). Deliberately a constant, NOT a config field: the shared tab's column order and
#: relative width are Redmine #14604's axis, and this build must not pre-empt that schema.
#: It is also never used outside ``shared_tab`` — under ``per_lane_tab`` a lane's first slot
#: lands in an empty tab of its own and splits nothing.
INTER_LANE_SPLIT_DIRECTION = "right"


def resolve_focus_first_launch(
    *,
    split_direction: str,
    launch_count: int,
    lane_occupancy: int,
) -> bool:
    """True iff this run's FIRST launch must carry ``--focus`` (pure).

    The R1-F1 fix (review j#76613, Design Answer R1 j#76616). herdr splits a container's
    ACTIVE pane and ``agent start`` has no pane-target flag, so when every launch is
    ``--no-focus`` the container's empty ROOT pane stays active: the second slot's
    ``--split <dir>`` splits the root rather than the first agent, and reclaiming the root
    (after all launches, #13330) collapses that split away — leaving only the outer default
    ``right`` split the first agent implicitly created. The intended direction silently
    never applies (live-measured on BOTH the tab-less default pair and the lane tab: the
    pre-#13646 ``--split right`` literal only *looked* correct because it coincides with
    herdr's default direction, j#76622). Focusing the first launch pins the container's
    split target to that agent, so the second slot splits the AGENT and the direction
    survives the reclaim.

    The three conditions:

    - ``split_direction`` non-empty — this run will actually ask for a direction, so it has
      one to lose. Redmine #14568 keys this on the EFFECTIVE direction rather than on the
      pre-#14568 "did the operator declare a placement" predicate: with a product default
      of ``down`` on both lane classes, an undeclared workspace is exactly the case that
      needs the focus, and asking whether the operator wrote a block would silently hand it
      the collapsed ``right`` layout the fix exists to prevent.
    - ``lane_occupancy == 0`` — the lane has no slot in the container yet, so THIS run
      places the pane its second slot must split. A heal / mixed adopt joins a container
      where the lane's own live sibling is already the split target; a live pane is never
      focused / moved / swapped.
    - ``launch_count >= 2`` — a full pair. A single-provider request has no second slot to
      place, so the focus policy never fires.

    Redmine #14567 keys the first condition on the LANE's occupancy rather than the
    container's. Under ``per_lane_tab`` the two are identical, so nothing changes there.
    Under ``shared_tab`` they diverge exactly once, and it is the case that matters: a
    fresh lane joining a tab other lanes already occupy has ``container_occupancy > 0`` but
    ``lane_occupancy == 0``, so its first slot both SPLITS (on the inter-lane axis) and must
    be FOCUSED — otherwise its pair sibling would split whichever pane herdr happened to
    have active, i.e. another lane's pane, and the pair would be torn apart. That makes this
    the one place where a splitting slot is focused; the pre-#14567 wording "a splitting
    slot never is" described the single-container world and no longer holds.
    """
    if lane_occupancy != 0 or launch_count < 2:
        return False
    return bool(split_direction)


@dataclass(frozen=True)
class ContainerPlan:
    """How this run places its launches inside the target container (pure value).

    Two split directions, never one (Redmine #14567, Design Answer j#91144 Decision 4): a
    shared tab holds several lanes, so "beside the pair sibling" and "beside the previous
    lane" are different placements and a single ``split_direction`` cannot express both.

    - :attr:`split_direction` — the PAIR-internal ``--split`` value, from ``lane_placement``
      (``""`` = none). Used by every slot that splits beside its own lane sibling.
    - :attr:`inter_lane_split` — the ``--split`` value a lane's FIRST slot uses when it
      joins a container another lane already occupies (:data:`INTER_LANE_SPLIT_DIRECTION`
      under ``shared_tab``, ``""`` otherwise — under ``per_lane_tab`` no slot is ever
      placed relative to another lane).
    - :attr:`occupancy` — how many slots of ANY lane already occupy the container, so the
      first launch into a fresh one occupies and the rest split beside what is there.
    - :attr:`lane_occupancy` — how many of THIS lane's slots occupy it, which selects
      between the two directions above. Equal to :attr:`occupancy` outside ``shared_tab``.
    - :attr:`focus_first` — whether the lane's first launch must carry ``--focus`` to own
      the split target its sibling will use (the R1-F1 fix).
    """

    # Every field is required on purpose: a default on either #14567 field would let a
    # caller that forgot to wire the new axis construct a plan that silently reads as
    # "per-lane tab", which is exactly the drift this split exists to prevent.
    split_direction: str
    inter_lane_split: str
    occupancy: int
    lane_occupancy: int
    focus_first: bool


def resolve_container_plan(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
    *,
    lane_class: str,
    target_tab: str,
    lane_slot_tabs: Sequence[str],
    config_split: Optional[str],
    launch_count: int,
    shared_tab: bool = False,
    host_slot_tabs: "Sequence[tuple[str, str]]" = (),
) -> ContainerPlan:
    """The whole container placement plan for this run (pure; the single entry point).

    Composes the decisions the session-start composition root needs — the two split
    directions to render, the container's and the lane's initial occupancy
    (:func:`initial_container_occupancy` / :func:`initial_lane_occupancy`), and whether the
    lane's first launch must own the split target (:func:`resolve_focus_first_launch`) — so
    the orchestrator makes ONE call and holds no placement logic of its own.

    ``config_split`` arrives already resolved through the full precedence
    (:func:`resolve_placement_policy_for_role`), so the direction is just rendered here:
    ``""`` (emit no ``--split``) is reachable only for a caller that resolved no policy at
    all. Redmine #14568 gives every lane class a product default, so the default lane now
    reads its occupancy on every launching run — that read is what makes the pair's 2nd
    slot split beside the 1st instead of landing on herdr's own default geometry.

    ``shared_tab`` (Redmine #14567) is the only thing that separates the two occupancies
    and turns on :data:`INTER_LANE_SPLIT_DIRECTION`. It defaults to False — and
    ``host_slot_tabs`` to empty — so every pre-#14567 caller and every ``per_lane_tab``
    repo produce byte-for-byte the previous plan: with it False the container occupancy IS
    the lane occupancy and the inter-lane direction is ``""``, which no slot can ever
    select (a slot only reads it when the lane's occupancy is zero while the container's is
    not — impossible when they are equal).

    It is additionally ANDed with the lane class HERE, not only at the call site. The
    shared tab is a sublane-only container — the coordinator pair has no tab at all — and a
    caller that passed the raw config mode through would silently corrupt the default
    lane's plan: its ``target_tab`` is always empty, so the shared branch of
    :func:`initial_container_occupancy` would report 0 for a coordinator HEAL that actually
    has a live sibling, and the healing slot would emit no ``--split`` at all. Deciding it
    in one place makes that unreachable rather than dependent on every caller remembering.
    """
    shared_tab = bool(shared_tab and lane_class == "sublane")
    split_direction = config_split or ""
    lane_occupancy = initial_lane_occupancy(
        rows,
        workspace_id,
        target_workspace,
        lane_id,
        lane_class=lane_class,
        target_tab=target_tab,
        lane_slot_tabs=lane_slot_tabs,
        count_default_lane=bool(launch_count and target_workspace and split_direction),
    )
    occupancy = initial_container_occupancy(
        lane_occupancy,
        shared_tab=shared_tab,
        target_tab=target_tab,
        host_slot_tabs=host_slot_tabs,
    )
    focus_first = resolve_focus_first_launch(
        split_direction=split_direction,
        launch_count=launch_count,
        lane_occupancy=lane_occupancy,
    )
    return ContainerPlan(
        split_direction=split_direction,
        inter_lane_split=INTER_LANE_SPLIT_DIRECTION if shared_tab else "",
        occupancy=occupancy,
        lane_occupancy=lane_occupancy,
        focus_first=focus_first,
    )


def slot_placement(
    kind: str,
    provider: str,
    *,
    split_direction: str,
    inter_lane_split: str,
    occupancy: int,
    lane_occupancy: int,
    config_order: Optional[Sequence[str]],
    focus_first: bool = False,
) -> "tuple[str, bool, bool]":
    """One slot's ``(--split value, focus, order_deferred)`` decision (pure).

    A slot splits only when it actually LAUNCHES into an already-occupied container; the
    container's first launch occupies it and emits no ``--split``. Adopted / planned /
    stale / unattested slots launch nothing, so they never carry a placement flag.

    WHICH direction it splits on is the lane's own occupancy (Redmine #14567, Design Answer
    j#91144 Decision 4):

    - ``lane_occupancy == 0`` — the lane has nothing in the container yet, so this slot is
      placed relative to ANOTHER lane and takes ``inter_lane_split``. Reachable only under
      ``shared_tab``: elsewhere a lane with no slots in its container faces an empty
      container, which the ``occupancy <= 0`` branch above already returned.
    - otherwise — the slot is placed beside its own pair sibling and takes
      ``split_direction`` (the ``lane_placement`` geometry).

    ``focus`` is set on the lane's FIRST launch when ``focus_first`` applies (see
    :func:`resolve_focus_first_launch`): that pins the split target to this lane's first
    agent so its sibling splits THAT agent — not the empty root pane that would be
    reclaimed out from under the split (R1-F1, j#76613 / j#76616), and not another lane's
    pane that merely happened to be active in a shared tab. It is therefore set on both
    first-launch branches: the one that occupies an empty container and the one that opens
    the lane's column beside an existing lane.

    ``order_deferred`` (Design Answer j#76564 Q2) flags the one case the configured order
    cannot be satisfied physically: the configured PRIMARY (``config_order[0]`` — the
    provider that should occupy the container) is launching as a split beside a sibling
    that is already live. herdr ``agent start`` has no pane-target flag and moving a live
    pane is forbidden (no live relayout), so the launch proceeds in the configured
    direction and the caller records ``order_deferred_until_full_relaunch`` instead of
    silently claiming the order was applied. A full relaunch of the pair realizes it. The
    inter-lane branch never defers: a lane opening its own column launches its configured
    primary FIRST, so the order is satisfied, not postponed.
    """
    if kind != "launch":
        return "", False, False
    if occupancy <= 0:
        return "", bool(focus_first), False
    if lane_occupancy <= 0:
        return inter_lane_split, bool(focus_first), False
    deferred = bool(config_order is not None and provider == config_order[0])
    return split_direction, False, deferred


__all__ = (
    "INTER_LANE_SPLIT_DIRECTION",
    "ContainerPlan",
    "initial_container_occupancy",
    "initial_lane_occupancy",
    "resolve_container_plan",
    "resolve_focus_first_launch",
    "resolve_launch_order",
    "resolve_placement_policy_for_role",
    "slot_placement",
)
