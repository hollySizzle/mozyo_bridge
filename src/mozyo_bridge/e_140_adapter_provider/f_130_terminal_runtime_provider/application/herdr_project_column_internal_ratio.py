"""Measured internal-ratio preservation for shared Herdr Unit columns."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    LayoutSnapshot,
    find_pair_split,
    governing_split,
    ratio_verdict,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    CoordinatorPane,
)


@dataclass(frozen=True)
class ColumnInternalRatio:
    """One complete Unit's measured vertical divider before any pane is moved."""

    key: tuple[str, str]
    top: str
    lower: str
    ratio: float


def _shared_vertical_divider(
    layout: LayoutSnapshot,
    key: tuple[str, str],
    top: str,
    lower: str,
):
    """Return the measurable divider shared by ``top`` and ``lower``.

    The two panes need not have equal widths: while a fresh Unit is still nested
    inside the upper half of an older Unit, the older top pane is narrower than
    its lower pane.  The vertical cells and their nearest shared ``down`` split
    still identify the divider unambiguously.
    """

    top_rect = layout.panes.get(top)
    lower_rect = layout.panes.get(lower)
    if top_rect is None or lower_rect is None:
        return None, None, None, f"Unit {key!r} lost a pane"
    top_split = governing_split(layout, top_rect, "down")
    lower_split = governing_split(layout, lower_rect, "down")
    if (
        top_rect.y >= lower_rect.y
        or top_split is None
        or top_split != lower_split
        or top_rect.y != top_split.rect.y
        or top_rect.y + top_rect.height != lower_rect.y
        or lower_rect.y + lower_rect.height
        != top_split.rect.y + top_split.rect.height
        or not math.isfinite(top_split.ratio)
        or not 0.1 <= top_split.ratio <= 0.9
    ):
        return None, None, None, (
            f"Unit {key!r} has no single measurable vertical divider shared "
            "by its top and lower panes"
        )
    return top_rect, lower_rect, top_split, ""


def capture_internal_ratios(
    layout: LayoutSnapshot,
    groups: Mapping[tuple[str, str], tuple[CoordinatorPane, ...]],
) -> tuple[tuple[ColumnInternalRatio, ...], str]:
    """Capture each complete Unit's effective vertical divider, or refuse.

    The just-appended pair can sit inside the first side of an older Unit's
    divider, so the older top and lower panes no longer tile that divider by
    themselves. The nearest governing ``down`` split shared by both panes is
    still the divider Herdr will discard when the lower pane is detached. Its
    measured ratio is therefore the value that must be supplied on reattach.
    """

    captured = []
    for key, members in sorted(groups.items()):
        if len(members) == 1:
            continue
        if len(members) != 2:
            return (), f"Unit {key!r} has {len(members)} panes, not a complete pair"
        rects = []
        for pane in members:
            rect = layout.panes.get(pane.locator)
            if rect is None:
                return (), f"pane {pane.locator!r} of Unit {key!r} is absent from layout"
            rects.append((rect, pane.locator))
        rects.sort(key=lambda entry: (entry[0].y, entry[1]))
        (top_rect, top), (lower_rect, lower) = rects
        top_rect, _lower_rect, top_split, refusal = _shared_vertical_divider(
            layout, key, top, lower
        )
        if refusal:
            return (), refusal
        matched, detail = ratio_verdict(top_split, top_rect, top_split.ratio)
        if not matched:
            return (), (
                f"Unit {key!r} opening divider disagrees with rendered geometry: "
                f"{detail}"
            )
        captured.append(ColumnInternalRatio(key, top, lower, top_split.ratio))
    return tuple(captured), ""


def effective_internal_ratios_match(
    layout: LayoutSnapshot,
    expected: Sequence[ColumnInternalRatio],
) -> tuple[bool, str]:
    """Verify saved dividers even while the tab has its opening L shape.

    Failure recovery may restore the exact pre-reflow nesting rather than the
    final full-height columns.  This verifier therefore accepts the same
    measurable shared divider as :func:`capture_internal_ratios`, while still
    requiring its stored ratio and rendered cell extent to agree with the saved
    value.
    """

    for item in expected:
        top_rect, _lower_rect, split, refusal = _shared_vertical_divider(
            layout, item.key, item.top, item.lower
        )
        if refusal:
            return False, refusal
        matched, detail = ratio_verdict(split, top_rect, item.ratio)
        if not matched:
            return False, f"Unit {item.key!r}: {detail}"
    return True, ""


def internal_ratios_match(
    layout: LayoutSnapshot,
    expected: Sequence[ColumnInternalRatio],
) -> tuple[bool, str]:
    """Verify every saved Unit divider from the rendered closing geometry."""

    for item in expected:
        top_rect = layout.panes.get(item.top)
        lower_rect = layout.panes.get(item.lower)
        if top_rect is None or lower_rect is None:
            return False, f"Unit {item.key!r} lost a pane"
        split = find_pair_split(layout, top_rect, lower_rect, "down")
        if split is None or governing_split(layout, top_rect, "down") != split:
            return False, f"Unit {item.key!r} has no verified vertical divider"
        matched, detail = ratio_verdict(split, top_rect, item.ratio)
        if not matched:
            return False, f"Unit {item.key!r}: {detail}"
    return True, ""


__all__ = (
    "ColumnInternalRatio",
    "capture_internal_ratios",
    "effective_internal_ratios_match",
    "internal_ratios_match",
)
