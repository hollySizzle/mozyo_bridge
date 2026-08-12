"""Equal-width verification and actuation for shared project columns (#15098).

The sibling project-column reflow owns pane detach/attach choreography. This
module owns the orthogonal step that follows it: prove the resulting columns are
a right-nested tree, resize only the verified RIGHT-axis ancestors, and measure
that every full-height project column differs in width by at most one cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_managed_column_scope import (  # noqa: E501
    ManagedColumnScope,
    managed_column_scope,
    managed_column_scope_matches,
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
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    CoordinatorPane,
)

# Herdr clamps divider ratios into ``0.1..0.9``. Equal columns need a root ratio
# ``1 / column_count``, so eleven columns cannot be represented.
MAX_EQUAL_PROJECT_COLUMNS = 10


@dataclass(frozen=True)
class ColumnRatioTarget:
    """One right-axis divider and the equal-share ratio it must reach."""

    pane: str
    right_pane: str
    ratio: float
    managed_scope: "Optional[ManagedColumnScope]" = None


def _tab_bounds(layout: LayoutSnapshot) -> "Optional[tuple[int, int, int, int]]":
    rects = list(layout.panes.values())
    if not rects:
        return None
    return (
        min(rect.x for rect in rects),
        min(rect.y for rect in rects),
        max(rect.x + rect.width for rect in rects),
        max(rect.y + rect.height for rect in rects),
    )


def _scope(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> Optional[ManagedColumnScope]:
    return managed_column_scope(
        layout,
        tuple(
            tuple(pane.locator for pane in members)
            for _key, members in sorted(groups.items())
        ),
    )


def _bounds(scope: ManagedColumnScope) -> tuple[int, int, int, int]:
    rect = scope.bounds
    return (rect.x, rect.y, rect.x + rect.width, rect.y + rect.height)


def _column_span(
    rects: Sequence[PaneRect], bounds: "tuple[int, int, int, int]"
) -> "Optional[tuple[int, int]]":
    """``(x, width)`` iff the panes stack into one full-height column."""
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
    if any(upper.y + upper.height != lower.y for upper, lower in zip(stacked, stacked[1:])):
        return None
    return (stacked[0].x, stacked[0].width)


def columnar_verdict(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> "tuple[bool, str]":
    """Whether every project pair owns one full-height column tiling the tab."""
    for key, members in sorted(groups.items()):
        for pane in members:
            if pane.locator not in layout.panes:
                return False, (
                    f"pane {pane.locator!r} of pair {key!r} is not in the tab"
                )
    scope = _scope(layout, groups)
    if scope is None:
        return False, "the managed panes do not form one isolated split subtree"
    bounds = _bounds(scope)
    spans = []
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
    """Whether the full-height columns differ in width by at most one cell."""
    columnar, reason = columnar_verdict(layout, groups)
    if not columnar:
        return False, reason
    scope = _scope(layout, groups)
    assert scope is not None
    bounds = _bounds(scope)
    widths = []
    for members in groups.values():
        span = _column_span([layout.panes[pane.locator] for pane in members], bounds)
        assert span is not None
        widths.append(span[1])
    if max(widths) - min(widths) > 1:
        return False, f"project column widths are not equal within one cell: {sorted(widths)}"
    return True, ""


def plan_equal_column_ratios(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> "tuple[Optional[tuple[ColumnRatioTarget, ...]], str]":
    """Plan ``1/N, 1/(N-1), ...`` ratios for a proved right-nested tree."""
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
    scope = _scope(layout, groups)
    assert scope is not None
    bounds = _bounds(scope)
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
            ColumnRatioTarget(
                pane_id,
                ordered[index + 1][2],
                1.0 / (count - index),
                managed_scope=scope,
            )
        )
    if ordered[0][0] != x0:
        return None, "the leftmost project column does not start at the tab boundary"
    return tuple(targets), ""


def read_pane_layout(
    pane_id: str, *, binary: str, runner, timeout: float, env
) -> Optional[LayoutSnapshot]:
    """Read and parse one Herdr tab layout, or ``None`` on refusal."""
    try:
        completed = _invoke(
            binary, ["pane", "layout", "--pane", pane_id], runner, timeout, env=env
        )
    except HerdrSessionStartError:
        return None
    return parse_pane_layout(completed.stdout)


def _resize_column_ratio(
    target: ColumnRatioTarget, *, binary: str, runner, timeout: float, env,
    authority_check: "Optional[Callable[[], bool]]" = None,
    layout_check: "Optional[Callable[[LayoutSnapshot], bool]]" = None,
) -> "tuple[bool, str]":
    """Drive one verified RIGHT-axis divider to its planned ratio."""
    changed = False
    detail = ""
    for pass_index in range(MAX_RESIZE_PASSES + 1):
        if authority_check is not None and not authority_check():
            return changed, "managed generation authority changed before resize"
        layout = read_pane_layout(
            target.pane, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if layout is None:
            return changed, "pane layout could not be read after project-column resize"
        if target.managed_scope is not None and not managed_column_scope_matches(
            layout, target.managed_scope
        ):
            return changed, "the managed project-column boundary changed before resize"
        if layout_check is not None and not layout_check(layout):
            return changed, "the managed Unit topology changed before resize"
        rect = layout.panes.get(target.pane)
        bounds = (
            _bounds(target.managed_scope)
            if target.managed_scope is not None
            else _tab_bounds(layout)
        )
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
        boundary_x = rect.x + rect.width
        admitted = (
            target.managed_scope.pane_ids
            if target.managed_scope is not None
            else frozenset(layout.panes)
        )
        immediate_right = [
            pane_id
            for pane_id, pane_rect in layout.panes.items()
            if pane_id in admitted
            if pane_rect.x == boundary_x
            and pane_rect.y == y0
            and pane_rect.x + pane_rect.width <= x1
            and pane_rect.y + pane_rect.height <= y1
        ]
        if immediate_right != [target.right_pane]:
            return changed, (
                "the planned right-side resize actuator is no longer the "
                "immediate project column"
            )
        actuator_pane = target.pane if direction == "right" else target.right_pane
        if authority_check is not None and not authority_check():
            return changed, "managed generation authority changed before resize"
        try:
            _invoke(
                binary,
                [
                    "pane", "resize", "--pane", actuator_pane,
                    "--direction", direction, "--amount", f"{amount:.6f}",
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
        if target.managed_scope is not None and not managed_column_scope_matches(
            measured, target.managed_scope
        ):
            return changed, "the managed project-column boundary changed during resize"
        if layout_check is not None and not layout_check(measured):
            return changed, "the managed Unit topology changed during resize"
        measured_rect = measured.panes.get(target.pane)
        measured_split = (
            governing_split(measured, measured_rect, "right")
            if measured_rect is not None
            else None
        )
        if measured_split is None:
            return changed, f"pane {target.pane!r} lost its right-axis divider after resize"
        if abs(measured_split.ratio - target.ratio) >= distance:
            return changed, (
                "herdr stopped moving a project divider toward its equal-width "
                f"target; {detail}"
            )
    return changed, f"project divider did not reach its equal-width target; {detail}"


def balance_project_columns(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
    *,
    binary: str,
    runner,
    timeout: float,
    env,
    authority_check: "Optional[Callable[[], bool]]" = None,
    layout_check: "Optional[Callable[[LayoutSnapshot], bool]]" = None,
) -> "tuple[bool, str]":
    """Equalise a proved right-nested column layout — ``(changed, refusal)``."""
    targets, refusal = plan_equal_column_ratios(layout, groups)
    if targets is None:
        return False, refusal
    changed = False
    for target in targets:
        target_changed, refusal = _resize_column_ratio(
            target, binary=binary, runner=runner, timeout=timeout, env=env,
            authority_check=authority_check,
            layout_check=layout_check,
        )
        changed = changed or target_changed
        if refusal:
            return changed, refusal
    return changed, ""


__all__ = (
    "MAX_EQUAL_PROJECT_COLUMNS",
    "ColumnRatioTarget",
    "balance_project_columns",
    "balanced_column_verdict",
    "columnar_verdict",
    "plan_equal_column_ratios",
    "read_pane_layout",
)
