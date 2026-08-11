"""Exact geometry scope for terminal-bound managed panes (#15227).

Cosmetic root panes are preserved, but they are not geometry authority.  This
module proves that the managed panes form one independently tiled split subtree
and fingerprints everything outside it so an effect never crosses a drifting
root/external boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    LayoutSnapshot,
    PaneRect,
    SplitInfo,
)


def _contains(outer: PaneRect, inner: PaneRect) -> bool:
    return (
        outer.x <= inner.x
        and outer.y <= inner.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


def _strictly_contains(outer: PaneRect, inner: PaneRect) -> bool:
    return outer != inner and _contains(outer, inner)


def _overlaps(first: PaneRect, second: PaneRect) -> bool:
    return (
        first.x < second.x + second.width
        and second.x < first.x + first.width
        and first.y < second.y + second.height
        and second.y < first.y + first.height
    )


def _rect_key(rect: PaneRect) -> tuple[int, int, int, int]:
    return (rect.x, rect.y, rect.width, rect.height)


def _split_key(split: SplitInfo) -> tuple[object, ...]:
    return (split.split_id, split.direction, split.ratio, *_rect_key(split.rect))


def _tiles(first: PaneRect, second: PaneRect, parent: SplitInfo) -> bool:
    if parent.direction == "down":
        ordered = sorted((first, second), key=lambda rect: rect.y)
        top, lower = ordered
        return (
            top.x == lower.x == parent.rect.x
            and top.width == lower.width == parent.rect.width
            and top.y == parent.rect.y
            and lower.y == top.y + top.height
            and top.height + lower.height == parent.rect.height
        )
    ordered = sorted((first, second), key=lambda rect: rect.x)
    left, right = ordered
    return (
        left.y == right.y == parent.rect.y
        and left.height == right.height == parent.rect.height
        and left.x == parent.rect.x
        and right.x == left.x + left.width
        and left.width + right.width == parent.rect.width
    )


def _exact_split_tree(
    panes: Sequence[PaneRect], splits: Sequence[SplitInfo], bounds: PaneRect
) -> bool:
    """Reconstruct the unique rectangle tree; count/bbox alone is not proof."""
    if len(splits) != len(panes) - 1:
        return False
    if len({_rect_key(split.rect) for split in splits}) != len(splits):
        return False
    roots = [split for split in splits if split.rect == bounds]
    if len(panes) == 1:
        return not splits and panes[0] == bounds
    if len(roots) != 1:
        return False
    # Split rectangles must be laminar. A partial overlap cannot describe a tree.
    for index, first in enumerate(splits):
        for second in splits[index + 1 :]:
            if (
                _overlaps(first.rect, second.rect)
                and not _contains(first.rect, second.rect)
                and not _contains(second.rect, first.rect)
            ):
                return False
    nodes: list[tuple[str, int, PaneRect]] = [
        ("pane", index, rect) for index, rect in enumerate(panes)
    ] + [
        ("split", index, split.rect)
        for index, split in enumerate(splits)
        if split.rect != bounds
    ]
    children: dict[int, list[PaneRect]] = {index: [] for index in range(len(splits))}
    for _kind, _index, rect in nodes:
        parents = [
            (candidate.rect.width * candidate.rect.height, split_index)
            for split_index, candidate in enumerate(splits)
            if _strictly_contains(candidate.rect, rect)
        ]
        if not parents:
            return False
        smallest_area = min(area for area, _index in parents)
        nearest = [index for area, index in parents if area == smallest_area]
        if len(nearest) != 1:
            return False
        children[nearest[0]].append(rect)
    return all(
        len(child_rects) == 2 and _tiles(child_rects[0], child_rects[1], split)
        for index, split in enumerate(splits)
        for child_rects in (children[index],)
    )


@dataclass(frozen=True)
class ManagedColumnScope:
    """Private proof that a set of pane groups owns one split subtree."""

    tab_id: str
    pane_groups: tuple[tuple[str, ...], ...] = field(repr=False)
    bounds: PaneRect = field(repr=False)
    external_fingerprint: tuple[object, ...] = field(repr=False)

    @property
    def pane_ids(self) -> frozenset[str]:
        return frozenset(pane for group in self.pane_groups for pane in group)

    @property
    def fingerprint(self) -> tuple[object, ...]:
        """Exact non-secret identity of the frozen managed/external boundary."""
        return (
            self.tab_id,
            self.pane_groups,
            _rect_key(self.bounds),
            self.external_fingerprint,
        )


def managed_column_scope(
    layout: LayoutSnapshot,
    pane_groups: Iterable[Sequence[str]],
) -> Optional[ManagedColumnScope]:
    """Build the exact managed subtree scope, or ``None`` on ambiguity/drift."""
    groups = tuple(tuple(group) for group in pane_groups)
    pane_ids = tuple(pane for group in groups for pane in group)
    if (
        not layout.tab_id
        or not groups
        or any(not group for group in groups)
        or any(type(pane) is not str or not pane or pane.strip() != pane for pane in pane_ids)
        or len(set(pane_ids)) != len(pane_ids)
        or any(pane not in layout.panes for pane in pane_ids)
    ):
        return None
    managed = [layout.panes[pane] for pane in pane_ids]
    bounds = PaneRect(
        min(rect.x for rect in managed),
        min(rect.y for rect in managed),
        max(rect.x + rect.width for rect in managed) - min(rect.x for rect in managed),
        max(rect.y + rect.height for rect in managed) - min(rect.y for rect in managed),
    )
    if bounds.width <= 0 or bounds.height <= 0:
        return None
    if any(rect.width <= 0 or rect.height <= 0 for rect in managed):
        return None
    if any(
        _overlaps(first, second)
        for index, first in enumerate(managed)
        for second in managed[index + 1 :]
    ):
        return None
    if sum(rect.width * rect.height for rect in managed) != bounds.width * bounds.height:
        return None

    unmanaged = {
        pane: rect for pane, rect in layout.panes.items() if pane not in set(pane_ids)
    }
    if any(_overlaps(bounds, rect) for rect in unmanaged.values()):
        return None

    internal: list[SplitInfo] = []
    external: list[SplitInfo] = []
    for split in layout.splits:
        if _contains(bounds, split.rect):
            internal.append(split)
        elif _contains(split.rect, bounds) or not _overlaps(split.rect, bounds):
            external.append(split)
        else:
            return None
    if (
        len({_split_key(split) for split in internal}) != len(internal)
        or not _exact_split_tree(managed, internal, bounds)
    ):
        return None
    external_fingerprint: tuple[object, ...] = (
        tuple(sorted((pane, *_rect_key(rect)) for pane, rect in unmanaged.items())),
        tuple(sorted(_split_key(split) for split in external)),
    )
    return ManagedColumnScope(
        layout.tab_id,
        groups,
        bounds,
        external_fingerprint,
    )


def managed_column_scope_matches(
    layout: LayoutSnapshot, expected: ManagedColumnScope
) -> bool:
    """Re-prove one fresh full layout against the frozen external boundary."""
    current = managed_column_scope(layout, expected.pane_groups)
    return (
        current is not None
        and current.tab_id == expected.tab_id
        and current.bounds == expected.bounds
        and current.external_fingerprint == expected.external_fingerprint
    )


def managed_external_boundary_matches(
    layout: LayoutSnapshot,
    expected: ManagedColumnScope,
    *,
    present_managed_ids: Iterable[str],
) -> bool:
    """Verify the frozen outside boundary while managed panes are detached.

    Reflow intentionally changes the internal subtree between effects.  It may
    not change, replace, or absorb the cosmetic root/external split around it.
    """
    present = frozenset(present_managed_ids)
    if (
        layout.tab_id != expected.tab_id
        or not present.issubset(expected.pane_ids)
        or not present.issubset(layout.panes)
    ):
        return False
    present_rects = [layout.panes[pane] for pane in present]
    if (
        not present_rects
        or any(not _contains(expected.bounds, rect) for rect in present_rects)
        or any(
            _overlaps(first, second)
            for index, first in enumerate(present_rects)
            for second in present_rects[index + 1 :]
        )
        or sum(rect.width * rect.height for rect in present_rects)
        != expected.bounds.width * expected.bounds.height
    ):
        return False
    external_panes, external_splits = expected.external_fingerprint
    current_external_panes = tuple(
        sorted(
            (pane, *_rect_key(rect))
            for pane, rect in layout.panes.items()
            if pane not in present
        )
    )
    if current_external_panes != external_panes:
        return False
    bounds = expected.bounds
    current_external_splits = tuple(
        sorted(
            _split_key(split)
            for split in layout.splits
            if not _contains(bounds, split.rect)
        )
    )
    if current_external_splits != external_splits:
        return False
    phase = managed_column_scope(layout, (tuple(sorted(present)),))
    return phase is not None and phase.bounds == expected.bounds


__all__ = (
    "ManagedColumnScope",
    "managed_column_scope",
    "managed_column_scope_matches",
    "managed_external_boundary_matches",
)
