"""Project-column geometry for the shared coordinator workspace (Redmine #14996 R2).

``shared_space`` and the project-coordinator surface of ``role_grouped_space``
collect every project's coordinator pair in ONE herdr workspace so an operator
oversees them all at once. The pairs converge correctly
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
- **Only the exact-labelled shared coordinator workspace** (``coordinators`` or
  ``project-coordinators``), only when this run
  freshly launched a FULL pair into a tab that already holds another project's
  coordinator panes. An adopt-only run, a single-provider heal, a dry run and a
  first-project launch all resolve :data:`COLUMN_NOT_APPLICABLE` and move nothing.
- **Only panes proved to be coordinator panes, and proved BEFORE the first move.**
  A decodable assigned name is not enough: its ``role`` field is a PROVIDER token
  (``codex`` / ``claude``), not a workflow role, so decoding alone cannot tell a
  project coordinator from an implementation slot that was mis-placed into this
  workspace (review j#99885 finding_2 bounced one) or from the TOP pair, which
  belongs in its own dedicated workspace (review j#99904 finding_1 moved six
  panes). :func:`resolve_project_groups` is the only producer a plan may consume,
  and it joins: provider shape, both halves of the mode's default-lane invariant
  (including ``workspace_id != top_workspace_id``), the durable ``lane_kind`` of
  every foreign NAMED lane, and — for every FOREIGN pane — a detected provider
  EQUAL to its decoded role, a self-attestation accepted by the canonical
  :func:`evaluate_attestation` (which pins the process generation), and a cwd that
  the identity model's own resolver maps back to the workspace its name claims.
  Unresolved evidence REFUSES; it is never filtered away, because a filtered-away
  stale sibling made a pair look healthy and four panes moved before the closing
  verdict caught it (review j#99904 finding_2). Identity and route authority stay
  untouched — a bounce moves a pane, it never closes, restarts or renames one.
- **Every placement is explicitly targeted.** Each step passes ``--target-pane``,
  so the result does not depend on which pane happened to be active before the
  launch (j#99845: "起動前focus非依存").
- **Only after the canonical startup pass has settled, and only if it admitted
  this run's own launch.** A booting pane and a dead one report the same inventory
  row, so this pass reads the workspace only once the bounded startup probe
  (#13948) has turned that ambiguity into a verdict (:func:`_premature_read_refusal`
  — #14996 R3, live finding j#100135); and because the authority exempts own panes
  from the startup attestation, a verdict that is not ``healthy`` refuses the whole
  step before the read rather than being lost (:func:`_unadmitted_launch_refusal` —
  review j#100188 finding_1, which measured six live pane moves without it).
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

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    CoordinatorPane,
    OwnSlot,
    ProjectColumnAuthority,
    coordinator_panes_in,
    project_column_authority,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    MAX_RESIZE_PASSES,
    LayoutSnapshot,
    PaneRect,
    governing_split,
    parse_pane_layout,
    ratio_verdict,
    resize_step,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _invoke,
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_LAUNCHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    HEALTH_HEALTHY,
    HEALTH_NOT_PROBED,
    HEALTH_OUTCOMES,
)

#: No project-column reflow was owed. The resting value: every non-shared coordinator
#: placement, a dry run, an adopt-only run, a single-provider heal, and the first project
#: to reach the shared workspace all land here without reading a layout.
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

#: Herdr clamps a divider ratio into ``0.1..0.9``. Equal columns need the root
#: ratio ``1 / column_count``, so eleven columns cannot be represented and must
#: be refused before a pane is moved rather than silently clamped unevenly.
MAX_EQUAL_PROJECT_COLUMNS = 10


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


@dataclass(frozen=True)
class ColumnRatioTarget:
    """One right-axis divider and the equal-share ratio it must reach."""

    pane: str
    ratio: float



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


def balanced_column_verdict(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> "tuple[bool, str]":
    """Whether the full-height project columns differ in width by at most one cell."""
    columnar, reason = columnar_verdict(layout, groups)
    if not columnar:
        return False, reason
    bounds = _tab_bounds(layout)
    assert bounds is not None  # proved by ``columnar_verdict``
    widths = []
    for members in groups.values():
        span = _column_span(
            [layout.panes[pane.locator] for pane in members], bounds
        )
        assert span is not None  # proved by ``columnar_verdict``
        widths.append(span[1])
    if max(widths) - min(widths) > 1:
        return False, f"project column widths are not equal within one cell: {sorted(widths)}"
    return True, ""


def plan_equal_column_ratios(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> "tuple[Optional[tuple[ColumnRatioTarget, ...]], str]":
    """Plan left-to-right ratios for a right-nested project-column tree.

    For ``N`` columns, the leftmost divider gives its first column ``1/N`` of
    the full tab, the next gives its column ``1/(N-1)`` of the remaining
    subtree, and so on. The structure is proved before the first resize: each
    addressed pane's nearest right-axis ancestor must span from that column to
    the tab's right edge. An unfamiliar tree is refused rather than resized by
    analogy.
    """
    columnar, reason = columnar_verdict(layout, groups)
    if not columnar:
        return None, reason
    count = len(groups)
    if count > MAX_EQUAL_PROJECT_COLUMNS:
        return None, (
            f"{count} project columns require a root ratio below Herdr's 0.1 minimum"
        )
    if count < 2:
        return (), ""
    bounds = _tab_bounds(layout)
    assert bounds is not None
    x0, y0, x1, y1 = bounds
    ordered = []
    for key, members in groups.items():
        panes = sorted(
            members,
            key=lambda pane: (layout.panes[pane.locator].y, pane.locator),
        )
        rect = layout.panes[panes[0].locator]
        ordered.append((rect.x, key, panes[0].locator, rect))
    ordered.sort(key=lambda entry: (entry[0], entry[1]))
    targets = []
    for index, (_x, key, pane_id, rect) in enumerate(ordered[:-1]):
        split = governing_split(layout, rect, "right")
        expected = PaneRect(rect.x, y0, x1 - rect.x, y1 - y0)
        if split is None or split.rect != expected:
            return None, (
                f"project column {key!r} is not the first child of the expected "
                "right-nested divider"
            )
        targets.append(
            ColumnRatioTarget(pane=pane_id, ratio=1.0 / (count - index))
        )
    if ordered[0][0] != x0:
        return None, "the leftmost project column does not start at the tab boundary"
    return tuple(targets), ""


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


def _resize_column_ratio(
    target: ColumnRatioTarget,
    *,
    binary: str,
    runner,
    timeout: float,
    env,
) -> "tuple[bool, str]":
    """Drive one right-axis project divider to its planned ratio.

    The addressed split is re-derived from every measured layout. A successful
    command is not evidence that the divider moved: lack of strict progress is a
    typed refusal, matching the pair-ratio rail's clamp discipline.
    """
    changed = False
    detail = ""
    for pass_index in range(MAX_RESIZE_PASSES + 1):
        layout = read_pane_layout(
            target.pane, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if layout is None:
            return changed, "pane layout could not be read after project-column resize"
        rect = layout.panes.get(target.pane)
        bounds = _tab_bounds(layout)
        if rect is None or bounds is None:
            return changed, f"project-column target pane {target.pane!r} is absent"
        x0, y0, x1, y1 = bounds
        split = governing_split(layout, rect, "right")
        expected = PaneRect(rect.x, y0, x1 - rect.x, y1 - y0)
        if split is None or split.rect != expected or rect.x < x0:
            return changed, (
                f"pane {target.pane!r} no longer resolves to its expected "
                "right-nested project divider"
            )
        matched, detail = ratio_verdict(split, rect, target.ratio)
        if matched:
            return changed, ""
        if pass_index == MAX_RESIZE_PASSES:
            break
        distance = abs(split.ratio - target.ratio)
        direction, amount = resize_step(split.ratio, target.ratio, "right")
        try:
            _invoke(
                binary,
                [
                    "pane", "resize", "--pane", target.pane,
                    "--direction", direction,
                    "--amount", f"{amount:.6f}",
                ],
                runner,
                timeout,
                env=env,
            )
        except HerdrSessionStartError as exc:
            return changed, (
                f"herdr refused project-column resize for pane {target.pane!r} ({exc})"
            )
        changed = True
        measured = read_pane_layout(
            target.pane, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if measured is None:
            return changed, "pane layout could not be read after project-column resize"
        measured_rect = measured.panes.get(target.pane)
        measured_split = (
            governing_split(measured, measured_rect, "right")
            if measured_rect is not None
            else None
        )
        if measured_split is None:
            return changed, (
                f"pane {target.pane!r} lost its right-axis divider after resize"
            )
        if abs(measured_split.ratio - target.ratio) >= distance:
            return changed, (
                "herdr stopped moving a project divider toward its equal-width "
                f"target; {detail}"
            )
    return changed, f"project divider did not reach its equal-width target; {detail}"


def _balance_project_columns(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
    *,
    binary: str,
    runner,
    timeout: float,
    env,
) -> "tuple[bool, str]":
    """Equalise a proved right-nested column layout — ``(changed, refusal)``."""
    targets, refusal = plan_equal_column_ratios(layout, groups)
    if targets is None:
        return False, refusal
    changed = False
    for target in targets:
        target_changed, refusal = _resize_column_ratio(
            target, binary=binary, runner=runner, timeout=timeout, env=env
        )
        changed = changed or target_changed
        if refusal:
            return changed, refusal
    return changed, ""


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
    """``{locator: assigned_name}`` for the panes in ``target_workspace``.

    A read of what the workspace holds, taken before and after the bounce. A
    refusal from the reader means the inventory changed into a shape the authority
    would not accept, which is itself a difference worth failing on, so it is
    folded into the map as a sentinel rather than swallowed.
    """
    panes, refusal = coordinator_panes_in(rows, target_workspace)
    if refusal:
        return {"": refusal}
    return {pane.locator: pane.assigned_name for pane in panes}


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


def _is_settled_health(value: object) -> bool:
    """True only for a health verdict the canonical probe has actually written."""
    return (
        isinstance(value, str)
        and value in HEALTH_OUTCOMES
        and value != HEALTH_NOT_PROBED
    )


def _unadmitted_launch_refusal(launched_slots: Sequence[object]) -> str:
    """Refuse to move live panes for a launch that did not pass startup admission.

    Distinct from the ordering question above: the pass has RUN, and it said no.

    Why this run's own verdict has to be a precondition here, when foreign panes
    are judged from the workspace instead: the authority deliberately exempts this
    run's own panes from the startup attestation, because a just-launched slot
    could not yet answer it (review j#99931 finding_1). Moving the geometry pass
    after pass 3 made it answerable — so keeping the exemption while declining to
    read the verdict would exempt the fact twice. An own-side
    ``attestation_mismatch`` / ``locator_drift`` / ``provider_exited`` is not
    reconstructible from the next inventory read, and the measured result was six
    live pane moves on a launch the run itself went on to report as not ok
    (review j#100188 finding_1, reproduced here).

    The admitted set is exactly ``{HEALTH_HEALTHY}`` — a closed policy, not a
    carve-out list. :mod:`...domain.startup_health` states it: "Only
    ``HEALTH_HEALTHY`` is a positive success verdict", and an unwrapped launch is
    ``attestation_unavailable`` precisely so it cannot read as green. Tokens that
    look merely informational (``startup_evidence_unavailable``,
    ``attestation_unavailable``) are included for that reason: each one already
    makes ``SessionStartResult.ok`` false, so admitting it here would only mean
    causing a live geometry effect for a run that is reporting failure anyway.
    Nothing is killed either way; the pair stays live and its placement is what
    the run declines to change.
    """
    unadmitted = sorted(
        (_norm(getattr(slot, "locator", "")), getattr(slot, "health", ""))
        for slot in launched_slots
        if _norm(getattr(slot, "locator", ""))
        and _is_settled_health(getattr(slot, "health", None))
        and getattr(slot, "health", "") != HEALTH_HEALTHY
    )
    if not unadmitted:
        return ""
    named = ", ".join(f"{locator!r} ({health})" for locator, health in unadmitted)
    return (
        f"this run's own launch did not pass startup admission — pane(s) {named}; "
        "refusing to place a column for a launch the startup pass did not admit; "
        "no live pane was moved"
    )


def _premature_read_refusal(launched_slots: Sequence[object]) -> str:
    """Refuse to read the inventory before the canonical liveness pass settled.

    A pane herdr has just started reports the SAME row shape as shell residue —
    the ``agent`` field present and blank — until the provider boots into it. That
    ambiguity is not this module's to resolve: the canonical startup pass already
    owns it, and owns it as a *deadline* rather than a verdict, which is why
    ``HEALTH_SHELL_RESIDUE`` sits in its retryable set (#13948) and is only
    reported once a bounded number of re-observations still see it.

    The live rollout ran this geometry pass BEFORE that one, so a fresh, healthy
    server-management pair was called shell residue and its first column failed
    (#14996 R3, live finding j#100135). The fix is the call order, and this is what
    keeps it from being merely positional: a launched slot still carrying
    :data:`HEALTH_NOT_PROBED` means the pass that decides liveness has not run over
    it, so the read is premature and is refused — at zero pane moves — instead of
    being judged. A slot missing the axis entirely is treated the same way; absent
    evidence of the pass is not evidence that it ran.

    Slots with no locator are outside this question rather than exempt from it:
    the probe only ever targets a locator, so an unaddressable slot carries no
    ordering evidence either way, and it is refused one step later by the authority
    on the axis that names its actual defect (j#99955) rather than being reported
    here under a cause that is not its own.

    "Settled" is membership in the health vocabulary, not "some non-empty string":
    the canonical `_norm` is ``str(value).strip()``, so ``None`` / ``0`` / ``[]``
    would each normalise to something that is not ``not_probed`` and read as a
    settled verdict — the same promotion of a malformed value that j#99971 found on
    the inventory side. Every verdict a real probe writes is a member, so nothing
    a launch legitimately produces is refused by asking.
    """
    premature = sorted(
        _norm(getattr(slot, "locator", ""))
        for slot in launched_slots
        if _norm(getattr(slot, "locator", ""))
        and not _is_settled_health(getattr(slot, "health", None))
    )
    if not premature:
        return ""
    return (
        f"the startup-liveness pass has not settled pane(s) {premature!r}, so their "
        "inventory rows cannot yet distinguish a booting provider from shell "
        "residue; no live pane was moved"
    )


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
    top_workspace_id: str = "",
    authority: "Optional[ProjectColumnAuthority]" = None,
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
            "this run is not a fresh shared project-coordinator launch"
        )
    if launched < 2 or initial_occupancy != 0:
        return COLUMN_NOT_APPLICABLE, (
            "no project column is appended by an adopt-only run or a heal beside a "
            "live sibling; no live pane is moved"
        )
    target_workspace = _norm(result.herdr_workspace_id)
    if not target_workspace:
        return COLUMN_FAILED, "the run reports no resolved shared herdr workspace"
    # EVERY launched slot is handed over, including one whose locator is blank.
    # Filtering those out here would be the same silent exclusion the authority
    # refuses on inside the workspace: a launch this run reports but cannot address
    # is a contradiction the run must fail on, not a row to drop on the way in.
    launched_slots = tuple(
        slot for slot in result.slots if getattr(slot, "outcome", "") == SLOT_LAUNCHED
    )
    # Two questions about this run's own launch, in the only order they compose in:
    # whether the canonical pass has answered at all, then what it answered. Both
    # land before the inventory read, so either costs zero pane moves.
    refusal = _premature_read_refusal(launched_slots) or _unadmitted_launch_refusal(
        launched_slots
    )
    if refusal:
        return COLUMN_FAILED, refusal
    own_slots = tuple(
        OwnSlot(
            locator=getattr(slot, "locator", ""),
            assigned_name=getattr(slot, "assigned_name", ""),
            provider=getattr(slot, "provider", ""),
        )
        for slot in launched_slots
    )
    own_launched = tuple(slot.locator for slot in own_slots)
    rows = _list_rows(binary, runner, timeout)
    decision = (authority or project_column_authority(home)).resolve(
        rows,
        target_workspace=target_workspace,
        own_slots=own_slots,
        # The run's own claim is an INPUT to the join, not a second derivation
        # beside it: a result naming one project whose slots were another's live
        # panes once reported a column for a project the workspace does not hold
        # (review j#99938 finding_2).
        expected_own_key=(result.workspace_id, _norm(result.lane_id) or DEFAULT_LANE),
        top_workspace_id=top_workspace_id,
    )
    if not decision.ok:
        return COLUMN_FAILED, f"{decision.refusal}; no live pane was moved"
    groups = decision.groups
    own_key = decision.own_key
    if not [key for key in groups if key != own_key]:
        return COLUMN_NOT_APPLICABLE, (
            "this project is the only coordinator pair in the shared workspace, so "
            "its pair already owns the whole tab"
        )
    if len(groups) > MAX_EQUAL_PROJECT_COLUMNS:
        return COLUMN_FAILED, (
            f"{len(groups)} project columns require a root ratio below Herdr's "
            "0.1 minimum; no live pane was moved"
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
    before = _identity_map(rows, target_workspace)
    if columnar:
        return _verify_reflow(
            before,
            groups,
            target_workspace,
            tab_id,
            anchor=own_launched[0],
            geometry_changed=False,
            binary=binary,
            runner=runner,
            timeout=timeout,
            env=env,
        )
    plan, refusal = plan_project_columns(layout, groups, own_key, own_launched)
    if plan is None:
        return COLUMN_FAILED, f"{refusal} (observed geometry: {reason})"
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
        geometry_changed=True,
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
    geometry_changed: bool,
    binary: str,
    runner,
    timeout: float,
    env,
) -> "tuple[str, str]":
    """Measure and equalise the produced columns — identity first, then geometry.

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
    resized, refusal = _balance_project_columns(
        layout,
        groups,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
    )
    if refusal:
        return COLUMN_FAILED, refusal
    closing = read_pane_layout(
        anchor, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if closing is None:
        return COLUMN_FAILED, "the balanced pane layout could not be read or parsed"
    balanced, reason = balanced_column_verdict(closing, groups)
    if not balanced:
        return COLUMN_FAILED, f"the project columns are still not balanced: {reason}"
    final_inventory = _identity_map(
        _list_rows(binary, runner, timeout), target_workspace
    )
    if final_inventory != before:
        return COLUMN_FAILED, (
            "the shared workspace inventory changed during project-column balancing"
        )
    outcome = COLUMN_APPLIED if geometry_changed or resized else COLUMN_MATCHED
    action = "now own" if outcome == COLUMN_APPLIED else "already own"
    return outcome, (
        f"{len(groups)} project pair(s) {action} equal-width full-height columns "
        f"in tab {tab_id}"
    )


__all__ = (
    "COLUMN_APPLIED",
    "COLUMN_FAILED",
    "COLUMN_MATCHED",
    "COLUMN_NOT_APPLICABLE",
    "COLUMN_OUTCOMES",
    "COLUMN_SUCCESS_OUTCOMES",
    "ColumnAttach",
    "ColumnRatioTarget",
    "ColumnReflowPlan",
    "CoordinatorPane",
    "attach_pane",
    "balanced_column_verdict",
    "columnar_verdict",
    "coordinator_panes_in",
    "detach_pane",
    "plan_project_columns",
    "plan_equal_column_ratios",
    "read_pane_layout",
    "reflow_project_columns",
)
