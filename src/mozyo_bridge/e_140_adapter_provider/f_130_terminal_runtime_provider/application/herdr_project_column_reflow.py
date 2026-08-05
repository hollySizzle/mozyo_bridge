"""Project-column geometry for the shared coordinator workspace (Redmine #14996 R2).

``role_grouped_space`` collects every project's coordinator pair in ONE herdr
workspace so an operator oversees them all at once. The pairs converge correctly
— identity, cwd and workspace are right — but the *geometry* did not: a second
project's pair landed as an L rather than its own column (live finding j#99833 —
the first project's Codex in the top left, the appended pair stacked in the top
right, and the first project's Claude spanning the whole bottom row).

Why a launch-argv fix cannot reach it
-------------------------------------
Measured on herdr 0.7.4, in a disposable instance:

- ``agent start`` has **no pane-target flag**. A launch splits whichever pane is
  active, so where the appended pair lands is not something the argv decides.
- After the first project's pair, the tab's ROOT divider is the one between that
  pair's two panes. Every subsequent ``pane split`` subdivides a *leaf*, so the
  root ``down`` divider — the full-width one the bottom pane spans — survives
  every launch. No sequence of leaf splits produces two full-height columns.
- ``pane move <p> --tab <same tab>`` is a ``same_tab`` no-op, ``pane swap`` only
  exchanges positions, and ``pane resize`` only moves an existing divider. A
  ``pane move`` from another tab *without* ``--target-pane`` also nests into the
  focused pane rather than inserting at the root.

So the appended column can only be created by briefly detaching one pane of the
column it splits — exactly the two-step bounce the live-relayout runbook
(#13648 recipe B) fixes, and exactly what the operator did by hand in #15017
j#99831 to restore the even 2x2. Redmine #14996 j#99845 authorises that narrow
relayout for this one case and requires it to be verified rather than assumed.

Boundary — narrow, launch-time, verified (j#99845)
--------------------------------------------------
- **Only the exact-labelled project-coordinator workspace**, only when this run
  freshly launched a FULL pair into a tab that already holds another project's
  coordinator panes. An adopt-only run, a single-provider heal, a dry run and a
  first-project launch all resolve :data:`COLUMN_NOT_APPLICABLE` and move nothing.
- **Only panes proved to be coordinator panes.** A decodable assigned name is not
  enough: its ``role`` field is a PROVIDER token (``codex`` / ``claude``), not a
  workflow role, so decoding alone cannot tell a project coordinator from an
  implementation slot that was mis-placed into (or lingered in) this workspace —
  review j#99885 finding_2 reproduced exactly that, with an ``implementation``
  lane chosen as the anchor and one of its panes bounced. The set that reaches a
  plan is therefore joined against three authorities and is otherwise a zero-move
  typed refusal: live-ness (:func:`classify_named_slot`), the mode's own default-
  lane invariant, and the durable ``lane_kind`` of every NAMED lane. Identity and
  route authority stay untouched — a bounce moves a pane, it never closes,
  restarts or renames one.
- **Every placement is explicitly targeted.** Each step passes ``--target-pane``,
  so the result does not depend on which pane happened to be active before the
  launch (j#99845: "起動前focus非依存").
- **Fail closed, and say what is still detached.** A refused step stops the
  sequence, best-effort re-attaches whatever this run had detached, and reports
  :data:`COLUMN_FAILED`. A pane that vanished, a temp tab that cannot be
  accounted for, or a final layout that is not columnar is never a success.

This is not a general live-relayout rail: nothing here reads a config, nothing
here touches a pair the run did not append to, and the only foreign pane it ever
moves is the one whose column the new pair splits — returned to the same place
beneath its own partner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.lane_kind import LANE_KIND_DELEGATED_COORDINATOR
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    load_lane_lifecycle_readonly,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_PROVIDERS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    LayoutSnapshot,
    PaneRect,
    parse_pane_layout,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _invoke,
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_LAUNCHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)

#: No project-column reflow was owed. The resting value: every non-role-grouped
#: placement, a dry run, an adopt-only run, a single-provider heal, and the first
#: project to reach the shared workspace all land here without reading a layout.
COLUMN_NOT_APPLICABLE = "not_applicable"
#: The tab was ALREADY columnar after the launch, so nothing was moved. A success
#: that costs zero pane moves — checked before any bounce, never assumed.
COLUMN_MATCHED = "matched"
#: The bounce ran and the closing ``pane layout`` read confirms every project pair
#: owns one full-height column. Only a measured layout produces this token.
COLUMN_APPLIED = "applied"
#: The reflow was owed and could not be established. Never reported as success;
#: the detail names the refusing step and any pane left outside the tab.
COLUMN_FAILED = "failed"

#: The closed outcome vocabulary, in the order a reader should read it.
COLUMN_OUTCOMES: tuple[str, ...] = (
    COLUMN_NOT_APPLICABLE,
    COLUMN_MATCHED,
    COLUMN_APPLIED,
    COLUMN_FAILED,
)

#: The outcomes a run may call successful on this axis. Enumerated rather than
#: derived as "everything except :data:`COLUMN_FAILED`" — the same discipline
#: :data:`...herdr_pair_split_ratio.RATIO_SUCCESS_OUTCOMES` adopted after a typo
#: in the negative comparison reported unknown tokens as success (j#91418 R5-F1).
COLUMN_SUCCESS_OUTCOMES: frozenset = frozenset(
    {COLUMN_NOT_APPLICABLE, COLUMN_MATCHED, COLUMN_APPLIED}
)


@dataclass(frozen=True)
class CoordinatorPane:
    """One identity-decoded coordinator pane living in the shared workspace."""

    locator: str
    assigned_name: str
    workspace_id: str
    lane_id: str
    role: str

    @property
    def pair_key(self) -> "tuple[str, str]":
        """The project pair this pane belongs to: its mozyo workspace + lane."""
        return (self.workspace_id, self.lane_id or DEFAULT_LANE)


@dataclass(frozen=True)
class ColumnAttach:
    """One targeted re-placement: put ``pane`` on ``direction`` of ``target``."""

    pane: str
    direction: str
    target: str


@dataclass(frozen=True)
class ColumnReflowPlan:
    """The bounce that turns the appended pair into its own full-height column.

    ``detach`` panes leave for a temp tab (herdr auto-closes it once empty), in
    order; ``attach`` puts them back, each against an explicit ``--target-pane``.
    The plan is pure: it names panes and directions, it runs nothing.
    """

    detach: "tuple[str, ...]"
    attach: "tuple[ColumnAttach, ...]"
    anchor_pane: str


def coordinator_panes_in(
    rows: Sequence[Mapping[str, object]], target_workspace: str
) -> "tuple[CoordinatorPane, ...]":
    """Every LIVE identity-decoded slot whose pane sits in ``target_workspace``.

    Identity is the herdr assigned name, never the pane position: a row we cannot
    decode, or one located in another herdr workspace, contributes nothing.

    A row :func:`classify_named_slot` reads as :data:`SLOT_STALE` contributes
    nothing either (review j#99885 finding_2). Stale rows are shell residue whose
    durable identity outlived its agent; letting one into a group would let this
    module reason about — and bounce — a pane whose provider is gone. It is the
    same liveness authority the sibling
    :func:`...herdr_role_grouped_space.validate_role_grouped_inventory` applies to
    this run's own lane, applied here to every project in the workspace.

    Decoding is necessary but NOT sufficient to call a pane a coordinator: the
    assigned name's ``role`` is a provider token. :func:`resolve_project_groups`
    is what turns these panes into project pairs, and it is the only producer a
    plan may consume.
    """
    panes: list = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            continue
        locator = _agent_locator(row)
        if not locator or _workspace_prefix(locator) != target_workspace:
            continue
        if classify_named_slot(row) == SLOT_STALE:
            continue
        panes.append(
            CoordinatorPane(
                locator=locator,
                assigned_name=_norm(row.get(AGENT_KEY_NAME)),
                workspace_id=decoded.identity.workspace_id,
                lane_id=decoded.identity.lane_id or DEFAULT_LANE,
                role=decoded.identity.role,
            )
        )
    return tuple(panes)


def group_by_pair(
    panes: Sequence[CoordinatorPane],
) -> "dict[tuple[str, str], tuple[CoordinatorPane, ...]]":
    """Panes grouped by ``(workspace_id, lane_id)`` — grouping only, no authority."""
    groups: dict = {}
    for pane in panes:
        groups.setdefault(pane.pair_key, []).append(pane)
    return {key: tuple(members) for key, members in groups.items()}


def _provider_shape_refusal(
    key: "tuple[str, str]", members: Sequence[CoordinatorPane]
) -> str:
    """``""`` iff this group's providers are a shape a coordinator pair can have.

    A distinct, non-empty subset of the canonical providers. Review j#99885
    finding_3 reproduced the hole this closes: two rows carrying the SAME assigned
    name were grouped as a pair, so an identity conflict — which the sibling
    resolver already fails closed on for this run's own lane — was reshaped as if
    it were a healthy codex/claude pair.

    A group of ONE live provider is deliberately allowed (finding_3 verdict
    j#99888 / dispute j#99890): :func:`_column_span` proves a full-height column
    from the layout regardless of how many panes stack in it, so a project that is
    currently short a slot still owns a real column. Failing here would report a
    neighbour's missing slot as THIS run's column failure — a mis-attribution, and
    one the slot / health axes already own.
    """
    providers = [pane.role for pane in members]
    unknown = sorted({p for p in providers if p not in LANE_PLACEMENT_PROVIDERS})
    if unknown:
        return (
            f"project pair {key!r} carries unrecognised provider(s) {unknown!r}; "
            "refusing to reshape a group this plan cannot identify"
        )
    if len(set(providers)) != len(providers):
        return (
            f"project pair {key!r} carries duplicate provider(s) {sorted(providers)!r} "
            "— an identity conflict, not a coordinator pair"
        )
    if len(providers) > len(LANE_PLACEMENT_PROVIDERS):
        return (
            f"project pair {key!r} holds {len(providers)} live panes, more than a "
            "coordinator pair can have"
        )
    return ""


def _lane_kind_index(home: Path) -> "Optional[dict[tuple[str, str], str]]":
    """``{(workspace_id, lane_id): lane_kind}`` from the durable lifecycle store.

    ``None`` when the store cannot be read version-compatibly — the same
    fail-closed disposition :func:`load_lane_lifecycle_readonly` defines, carried
    through so an unreadable authority refuses the reflow instead of letting a
    named lane default into "probably a coordinator".
    """
    records = load_lane_lifecycle_readonly(home=home)
    if records is None:
        return None
    return {
        (record.repo_workspace_id, _norm(record.lane_id) or DEFAULT_LANE): _norm(
            record.lane_kind
        )
        for record in records
    }


def resolve_project_groups(
    rows: Sequence[Mapping[str, object]],
    target_workspace: str,
    *,
    home: Path,
    own_key: "Optional[tuple[str, str]]" = None,
) -> "tuple[dict[tuple[str, str], tuple[CoordinatorPane, ...]], str]":
    """``(project pairs, refusal)`` — the only group producer a plan may consume.

    Three authorities, in the order that keeps the common case free of the
    heaviest one (review j#99885 finding_2 / finding_3):

    1. **live-ness and provider shape** (:func:`coordinator_panes_in`,
       :func:`_provider_shape_refusal`) — pure, from the inventory row.
    2. **the mode's default-lane invariant** — under ``role_grouped_space`` every
       DEFAULT lane is a coordinator; that is the same rule
       :func:`...herdr_role_grouped_space.is_role_grouped_project_coordinator`
       enforces on this run's own lane, and it needs no store read. A workspace
       holding only default lanes — the ordinary case — therefore never opens the
       lifecycle store at all.
    3. **the durable ``lane_kind``** for every FOREIGN named lane, read from the
       generation-bound lifecycle store. Only ``delegated_coordinator`` joins the
       coordinator role group. An ``implementation`` lane in this workspace is a
       mis-placement this axis must not silently reshape, and a missing / unknown
       kind — or a store that cannot be read — is not evidence of one either. All
       three are a refusal, which the caller turns into a ZERO-MOVE typed failure.

    ``own_key`` is exempt from (3) and only from (3): this run's own lane kind was
    already proved by the caller — ``role_grouped_space`` classified it through
    :func:`...herdr_role_grouped_space.is_role_grouped_project_coordinator` before
    anything launched, which is the authority that decided this workspace was its
    placement at all. Re-deriving it from the lifecycle store would not strengthen
    that; it would only fail a managed ``delegated_coordinator`` whose durable row
    is written on a different edge than its launch, which is a live path (measured
    against ``HerdrSublaneActuatorOps.append_lane_column``). The finding this
    exemption preserves is about FOREIGN panes, and those keep the full join.

    A non-empty refusal means no plan may be built; the groups returned with it
    are not usable.
    """
    groups = group_by_pair(coordinator_panes_in(rows, target_workspace))
    for key, members in sorted(groups.items()):
        refusal = _provider_shape_refusal(key, members)
        if refusal:
            return {}, refusal
    named = sorted(
        key for key in groups if key[1] != DEFAULT_LANE and key != own_key
    )
    if not named:
        return groups, ""
    index = _lane_kind_index(home)
    if index is None:
        return {}, (
            "the durable lane-kind authority is unreadable, so the named lane(s) "
            f"{named!r} in this workspace cannot be proved to be project coordinators"
        )
    for key in named:
        kind = index.get(key, "")
        if kind == LANE_KIND_DELEGATED_COORDINATOR:
            continue
        if not kind:
            return {}, (
                f"named lane {key!r} has no durable lane-kind; refusing to treat it as "
                "a project coordinator"
            )
        return {}, (
            f"named lane {key!r} has durable lane-kind {kind!r}, not "
            f"{LANE_KIND_DELEGATED_COORDINATOR!r}; a non-coordinator lane in the shared "
            "project-coordinator workspace is a placement this plan will not reshape"
        )
    return groups, ""


def _tab_bounds(layout: LayoutSnapshot) -> "Optional[tuple[int, int, int, int]]":
    """``(x0, y0, x1, y1)`` spanned by every pane in the tab, or ``None`` if empty."""
    rects = list(layout.panes.values())
    if not rects:
        return None
    return (
        min(rect.x for rect in rects),
        min(rect.y for rect in rects),
        max(rect.x + rect.width for rect in rects),
        max(rect.y + rect.height for rect in rects),
    )


def _column_span(
    rects: Sequence[PaneRect], bounds: "tuple[int, int, int, int]"
) -> "Optional[tuple[int, int]]":
    """``(x, width)`` iff these panes stack into ONE full-height column.

    Full height matters as much as equal width: the defect's bottom pane had the
    right x but spanned the whole tab, so a width-only test would have called the
    L a column.
    """
    if not rects:
        return None
    _x0, y0, _x1, y1 = bounds
    xs = {rect.x for rect in rects}
    widths = {rect.width for rect in rects}
    if len(xs) != 1 or len(widths) != 1:
        return None
    stacked = sorted(rects, key=lambda rect: rect.y)
    if stacked[0].y != y0 or stacked[-1].y + stacked[-1].height != y1:
        return None
    for upper, lower in zip(stacked, stacked[1:]):
        if upper.y + upper.height != lower.y:
            return None
    return (stacked[0].x, stacked[0].width)


def columnar_verdict(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> "tuple[bool, str]":
    """``(is_columnar, reason)`` — does every project pair own one column?

    The acceptance in j#99833 stated geometrically: each pair stacks into a
    full-height column of equal width, and the columns tile the tab left to right
    without overlapping (``他project pairの領域へ跨らない``).
    """
    bounds = _tab_bounds(layout)
    if bounds is None:
        return False, "the tab layout reports no panes"
    spans: list = []
    for key, members in sorted(groups.items()):
        rects = []
        for pane in members:
            rect = layout.panes.get(pane.locator)
            if rect is None:
                return False, f"pane {pane.locator!r} of pair {key!r} is not in the tab"
            rects.append(rect)
        span = _column_span(rects, bounds)
        if span is None:
            return False, (
                f"pair {key!r} does not stack into one full-height column of equal width"
            )
        spans.append(span)
    spans.sort()
    x0, _y0, x1, _y1 = bounds
    cursor = x0
    for x, width in spans:
        if x != cursor:
            return False, "the project columns do not tile the tab left to right"
        cursor = x + width
    if cursor != x1:
        return False, "the project columns do not span the full tab width"
    return True, ""


def _anchor_group(
    layout: LayoutSnapshot,
    foreign: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> "tuple[str, tuple[str, ...], str]":
    """``(top pane, panes to bounce, refusal)`` for the column the new one splits.

    The rightmost live column is the anchor, so the appended project lands on the
    right and no existing column is reordered (j#99833 acceptance 3: left-right
    order is launch-order best-effort, and unrelated columns are left alone).
    Relative x order survives a detach — removing a pane only collapses splits —
    so the anchor chosen from this layout is still the rightmost one once this
    run's own panes have left.
    """
    ranked: list = []
    for key, members in foreign.items():
        rects = []
        for pane in members:
            rect = layout.panes.get(pane.locator)
            if rect is None:
                return "", (), f"pane {pane.locator!r} of pair {key!r} is not in the tab"
            rects.append((rect, pane.locator))
        if len(rects) > 2:
            return "", (), (
                f"project pair {key!r} holds {len(rects)} live panes; refusing to "
                "reshape a pair whose shape this plan does not recognise"
            )
        stacked = sorted(rects, key=lambda entry: (entry[0].y, entry[1]))
        ranked.append((max(rect.x for rect, _ in rects), stacked, key))
    if not ranked:
        return "", (), "no other project coordinator pair shares the workspace"
    ranked.sort(key=lambda entry: (entry[0], entry[2]))
    _x, stacked, _key = ranked[-1]
    return stacked[0][1], tuple(locator for _rect, locator in stacked[1:]), ""


def plan_project_columns(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
    own_key: "tuple[str, str]",
    own_launched: Sequence[str],
) -> "tuple[Optional[ColumnReflowPlan], str]":
    """``(plan, refusal)`` for appending ``own_launched`` as its own column (pure).

    The sequence, all of it explicitly targeted:

    1. this run's two panes leave for temp tabs, which collapses the tab back to
       the geometry the other projects had before the launch;
    2. the anchor column's lower pane leaves too, so the anchor's top pane is the
       leaf a ``right`` split turns into a root-level column boundary;
    3. the new primary attaches to the RIGHT of the anchor top, the new secondary
       ``down`` from it, and the anchor's lower pane returns ``down`` beneath its
       own partner — restoring it to exactly where it was.
    """
    own = tuple(locator for locator in own_launched if locator)
    if len(own) != 2:
        return None, (
            f"a project column is appended by a full fresh pair; this run reports "
            f"{len(own)} launched locator(s)"
        )
    if own_key not in groups:
        return None, "this run's own pair is not in the shared workspace inventory"
    own_members = {pane.locator for pane in groups[own_key]}
    missing = [locator for locator in own if locator not in own_members]
    if missing:
        return None, f"launched pane(s) {missing!r} carry no decodable pair identity"
    foreign = {key: members for key, members in groups.items() if key != own_key}
    anchor_top, anchor_rest, refusal = _anchor_group(layout, foreign)
    if refusal:
        return None, refusal
    for locator in own:
        if layout.panes.get(locator) is None:
            return None, f"launched pane {locator!r} is not in the tab layout"
    return (
        ColumnReflowPlan(
            detach=(own[1], own[0]) + anchor_rest,
            attach=(
                ColumnAttach(pane=own[0], direction="right", target=anchor_top),
                ColumnAttach(pane=own[1], direction="down", target=own[0]),
            )
            + tuple(
                ColumnAttach(pane=locator, direction="down", target=anchor_top)
                for locator in anchor_rest
            ),
            anchor_pane=anchor_top,
        ),
        "",
    )


def read_pane_layout(
    pane_id: str, *, binary: str, runner, timeout: float, env
) -> Optional[LayoutSnapshot]:
    """``herdr pane layout --pane <id>``, parsed; ``None`` on refusal / bad payload."""
    try:
        completed = _invoke(
            binary, ["pane", "layout", "--pane", pane_id], runner, timeout, env=env
        )
    except HerdrSessionStartError:
        return None
    return parse_pane_layout(completed.stdout)


def _move_result(stdout: object) -> "Optional[tuple[str, str]]":
    """``(pane_id, tab_id)`` a ``pane move`` landed on, or ``None`` if it did not.

    ``changed`` is checked rather than the exit code: ``pane move --tab <same
    tab>`` exits 0 while reporting ``changed:false`` / ``reason:same_tab`` and
    moving nothing, so a zero exit is not evidence that the pane went anywhere.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    move = result.get("move_result")
    if not isinstance(move, Mapping) or move.get("changed") is not True:
        return None
    pane = move.get("pane")
    if not isinstance(pane, Mapping):
        return None
    pane_id = _norm(pane.get("pane_id"))
    tab_id = _norm(pane.get("tab_id"))
    return (pane_id, tab_id) if pane_id and tab_id else None


def detach_pane(
    pane_id: str, *, binary: str, runner, timeout: float, env
) -> "tuple[str, str]":
    """Bounce ``pane_id`` out to its own temp tab — ``(temp tab id, refusal)``.

    herdr auto-closes the temp tab when its last pane leaves, so the pane's way
    back is the only cleanup this ever needs.
    """
    try:
        completed = _invoke(
            binary,
            ["pane", "move", pane_id, "--new-tab", "--no-focus"],
            runner,
            timeout,
            env=env,
        )
    except HerdrSessionStartError as exc:
        return "", f"herdr refused to detach pane {pane_id!r} ({exc})"
    landed = _move_result(completed.stdout)
    if landed is None:
        return "", f"herdr reported no completed move for pane {pane_id!r}"
    return landed[1], ""


def attach_pane(
    attach: ColumnAttach, tab_id: str, *, binary: str, runner, timeout: float, env
) -> str:
    """Place one detached pane against an explicit target — ``""`` iff it landed."""
    try:
        completed = _invoke(
            binary,
            [
                "pane", "move", attach.pane,
                "--tab", tab_id,
                "--split", attach.direction,
                "--target-pane", attach.target,
                "--no-focus",
            ],
            runner,
            timeout,
            env=env,
        )
    except HerdrSessionStartError as exc:
        return (
            f"herdr refused to place pane {attach.pane!r} {attach.direction} of "
            f"{attach.target!r} ({exc})"
        )
    landed = _move_result(completed.stdout)
    if landed is None:
        return f"herdr reported no completed move for pane {attach.pane!r}"
    if landed[1] != tab_id:
        return (
            f"pane {attach.pane!r} landed in tab {landed[1]!r}, not the shared "
            f"project-coordinator tab {tab_id!r}"
        )
    return ""


def _identity_map(
    rows: Sequence[Mapping[str, object]], target_workspace: str
) -> "dict[str, str]":
    """``{locator: assigned_name}`` for the decoded panes in ``target_workspace``."""
    return {
        pane.locator: pane.assigned_name
        for pane in coordinator_panes_in(rows, target_workspace)
    }


def _restore_detached(
    detached: Sequence[str],
    tab_id: str,
    anchor: str,
    *,
    binary: str,
    runner,
    timeout: float,
    env,
) -> "tuple[str, ...]":
    """Best-effort return of still-detached panes; the ones that stayed out.

    A failed reflow must not leave an agent parked in an invisible temp tab with
    no record of it. Each pane is placed back beneath the anchor — not its
    original divider, which no longer exists — and whatever could not be placed is
    named in the failure detail rather than silently abandoned.
    """
    stranded: list = []
    for pane_id in detached:
        refusal = attach_pane(
            ColumnAttach(pane=pane_id, direction="down", target=anchor),
            tab_id,
            binary=binary,
            runner=runner,
            timeout=timeout,
            env=env,
        )
        if refusal:
            stranded.append(pane_id)
    return tuple(stranded)


def reflow_project_columns(
    result,
    *,
    project_coordinator: bool,
    launched: int,
    initial_occupancy: int,
    dry_run: bool,
    binary: str,
    runner,
    timeout: float,
    env,
    home: Path,
) -> "tuple[str, str]":
    """Give this run's appended pair its own column — ``(outcome, detail)``.

    Owed only by a FRESH full pair appended to a shared project-coordinator
    workspace that already holds another project (``initial_occupancy == 0`` is
    this pair's own occupancy — an adopt or a heal has a live sibling and is left
    alone). Everything before the first pane move is decided from identity and a
    measured layout; everything after it is measured again before the run claims
    the geometry it wanted.

    ``project_coordinator`` is the caller's placement classification and the ONLY
    thing it contributes; the pair's identity and its resolved herdr workspace are
    read off ``result``, which already carries both, so the two can never disagree.
    """
    if not project_coordinator or dry_run:
        return COLUMN_NOT_APPLICABLE, (
            "this run is not a fresh role-grouped project-coordinator launch"
        )
    if launched < 2 or initial_occupancy != 0:
        return COLUMN_NOT_APPLICABLE, (
            "no project column is appended by an adopt-only run or a heal beside a "
            "live sibling; no live pane is moved"
        )
    target_workspace = _norm(result.herdr_workspace_id)
    if not target_workspace:
        return COLUMN_FAILED, "the run reports no resolved shared herdr workspace"
    own_launched = tuple(
        slot.locator
        for slot in result.slots
        if getattr(slot, "outcome", "") == SLOT_LAUNCHED and getattr(slot, "locator", "")
    )
    rows = _list_rows(binary, runner, timeout)
    own_key = (result.workspace_id, _norm(result.lane_id) or DEFAULT_LANE)
    groups, group_refusal = resolve_project_groups(
        rows, target_workspace, home=home, own_key=own_key
    )
    if group_refusal:
        return COLUMN_FAILED, f"{group_refusal}; no live pane was moved"
    if not [key for key in groups if key != own_key]:
        return COLUMN_NOT_APPLICABLE, (
            "this project is the only coordinator pair in the shared workspace, so "
            "its pair already owns the whole tab"
        )
    if not own_launched:
        return COLUMN_FAILED, "this run launched a pair but reports no live locator"
    layout = read_pane_layout(
        own_launched[0], binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return COLUMN_FAILED, "pane layout could not be read or parsed"
    tab_id = _norm(layout.tab_id)
    if not tab_id:
        return COLUMN_FAILED, "the shared project-coordinator tab id is unreadable"
    columnar, reason = columnar_verdict(layout, groups)
    if columnar:
        return COLUMN_MATCHED, (
            "every project pair already owns one full-height column; no pane was moved"
        )
    plan, refusal = plan_project_columns(layout, groups, own_key, own_launched)
    if plan is None:
        return COLUMN_FAILED, f"{refusal} (observed geometry: {reason})"
    before = _identity_map(rows, target_workspace)
    detached: list = []
    for pane_id in plan.detach:
        _temp_tab, step_refusal = detach_pane(
            pane_id, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if step_refusal:
            stranded = _restore_detached(
                detached, tab_id, plan.anchor_pane,
                binary=binary, runner=runner, timeout=timeout, env=env,
            )
            return COLUMN_FAILED, _stranded_detail(step_refusal, stranded)
        detached.append(pane_id)
    for attach in plan.attach:
        step_refusal = attach_pane(
            attach, tab_id, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if step_refusal:
            # `attach.pane` is still detached (it is removed only on success), so it
            # is restored beneath the anchor along with the rest rather than being
            # dropped from the accounting the failure detail is built from.
            stranded = _restore_detached(
                tuple(detached), tab_id, plan.anchor_pane,
                binary=binary, runner=runner, timeout=timeout, env=env,
            )
            return COLUMN_FAILED, _stranded_detail(step_refusal, stranded)
        detached.remove(attach.pane)
    return _verify_reflow(
        before,
        groups,
        target_workspace,
        tab_id,
        anchor=plan.anchor_pane,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
    )


def _stranded_detail(refusal: str, stranded: Sequence[str]) -> str:
    """A failure detail that never hides a pane left outside the shared tab."""
    if not stranded:
        return f"{refusal}; every detached pane was returned to the shared tab"
    return (
        f"{refusal}; pane(s) {sorted(stranded)!r} are NOT in the shared "
        "project-coordinator tab and need the live-relayout runbook to be replaced"
    )


def _verify_reflow(
    before: "Mapping[str, str]",
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
    target_workspace: str,
    tab_id: str,
    *,
    anchor: str,
    binary: str,
    runner,
    timeout: float,
    env,
) -> "tuple[str, str]":
    """Measure what the bounce actually produced — identity first, then geometry.

    Identity comes first deliberately: a layout that looks right tells us nothing
    if a pane came back under a different assigned name, and that is the property
    the whole placement model rests on (``spec-herdr-native-identity``).
    """
    after = _identity_map(_list_rows(binary, runner, timeout), target_workspace)
    if after != before:
        lost = sorted(set(before) - set(after))
        changed = sorted(
            locator
            for locator in set(before) & set(after)
            if before[locator] != after[locator]
        )
        return COLUMN_FAILED, (
            "the shared workspace inventory changed across the reflow "
            f"(missing: {lost!r}, renamed: {changed!r}); the geometry is not claimed"
        )
    layout = read_pane_layout(
        anchor, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return COLUMN_FAILED, "the closing pane layout could not be read or parsed"
    if _norm(layout.tab_id) != tab_id:
        return COLUMN_FAILED, (
            f"the closing layout reports tab {layout.tab_id!r}, not {tab_id!r}"
        )
    columnar, reason = columnar_verdict(layout, groups)
    if not columnar:
        return COLUMN_FAILED, f"the reflowed tab is still not columnar: {reason}"
    return COLUMN_APPLIED, (
        f"{len(groups)} project pair(s) each own one full-height column in tab {tab_id}"
    )


__all__ = (
    "COLUMN_APPLIED",
    "COLUMN_FAILED",
    "COLUMN_MATCHED",
    "COLUMN_NOT_APPLICABLE",
    "COLUMN_OUTCOMES",
    "COLUMN_SUCCESS_OUTCOMES",
    "ColumnAttach",
    "ColumnReflowPlan",
    "CoordinatorPane",
    "attach_pane",
    "columnar_verdict",
    "coordinator_panes_in",
    "detach_pane",
    "group_by_pair",
    "plan_project_columns",
    "read_pane_layout",
    "reflow_project_columns",
    "resolve_project_groups",
)
