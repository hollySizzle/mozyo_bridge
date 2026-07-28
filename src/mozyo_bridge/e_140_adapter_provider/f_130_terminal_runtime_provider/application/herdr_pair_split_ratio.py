"""Post-launch container geometry: reclaim the roots, then divide the pair (#14569).

The cohesive sibling of :mod:`herdr_session_start` that owns everything a run does to the
*container* **after every launch has succeeded**, so the composition root holds one call
and no geometry logic of its own:

1. **root-pane reclaim** — close the empty herdr root panes this run created (the #13330
   workspace base pane and the #13411 lane tab root pane). Relocated here unchanged from
   the session-start composition root: it is the first half of "finish shaping the
   container", and it must run BEFORE step 2 because closing the root collapses the split
   tree the ratio is measured against.
2. **declared pair split ratio** — herdr 0.7.4 ``agent start`` has no ``--ratio`` flag
   (live ``--help`` characterization, j#91140), so the pair's relative division cannot ride
   on the launch argv the way ``--split`` does. It is actuated once, afterwards, with
   herdr-native ``pane resize --amount`` and then **measured** against ``pane layout``.

Boundary — this never becomes a live-relayout rail (issue Non-goals, Design Answer j#91127)
-------------------------------------------------------------------------------------------
The only divider this module ever moves is **one this run just created**. Concretely
:func:`_created_pair_split` demands that a launch in this run split an already-occupied
container; an all-adopt run, a dry run, and a first-launch-only run all create no divider
and actuate nothing. Loading a config actuates nothing at all — there is no path from
``config.yaml`` to a live pane except through a launch that completes a pair. No pane is
ever closed, moved, swapped, focused or killed here beyond the root reclaim this run's own
``workspace create`` / ``tab create`` incurred.

Fail-closed — a ratio that was not applied is never reported as applied
----------------------------------------------------------------------
Every step that could quietly produce the wrong geometry ends in :data:`RATIO_FAILED` with
a fixed reason token rather than an optimistic default: an unparseable layout, a pair whose
governing split cannot be uniquely identified, a split whose direction is not the one that
was declared, a ``pane resize`` that herdr rejected, and — last — a final measurement that
disagrees with the declared ratio. herdr silently clamps both the per-call ``--amount``
(to 0.5) and the resulting ratio (to ``0.1..0.9``), so *issuing* a resize proves nothing;
only the closing :func:`pane layout` read does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _close_base_pane,
    _invoke,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    Runner,
)

#: No pair split ratio was actuated, and none was owed. The run created no divider of its
#: own (dry run / nothing launched / a first launch that only occupied the container), or
#: the pair's two panes are not both this run's slots — see the accompanying detail. This
#: is the ONLY non-failure outcome that skips the measurement, and it never means "a
#: declared ratio was quietly dropped on a pair this run did divide".
RATIO_NOT_APPLICABLE = "not_applicable"
#: The pair's split already sat at the declared ratio; measured read-only, no resize
#: issued. A no-op is a success (Design Answer j#91127) — the common case for the product
#: default ``0.5`` on a freshly split pair.
RATIO_MATCHED = "matched"
#: A ``pane resize`` was issued and the closing ``pane layout`` read confirms the declared
#: ratio, within :data:`RATIO_TOLERANCE` and one cell of ``round(extent * ratio)``.
RATIO_APPLIED = "applied"
#: The configured ``order[0]`` physically landed on the SECOND side of the split (the
#: order-deferred heal of Design Answer j#91127): its sibling was already live, so the
#: primary could only be launched beside it. Applying the ratio here would hand
#: ``order[0]``'s share to ``order[1]``, and swapping / bouncing a live pane is forbidden,
#: so the run declares the deferral instead of claiming either. A full relaunch of the pair
#: realizes both the order and the ratio.
RATIO_DEFERRED = "deferred_until_full_relaunch"
#: The ratio was owed on a split this run created and could not be established. Never
#: reported as success; the pair is left exactly as herdr placed it (no agent is closed).
RATIO_FAILED = "failed"

#: The closed outcome vocabulary, in the order a reader should read it.
RATIO_OUTCOMES: tuple[str, ...] = (
    RATIO_NOT_APPLICABLE,
    RATIO_MATCHED,
    RATIO_APPLIED,
    RATIO_DEFERRED,
    RATIO_FAILED,
)

#: How far the measured split ratio may sit from the declared one and still count as
#: applied. herdr stores split ratios as ``f32`` and its resize arithmetic leaves visible
#: residue (measured j#91140: ``0.40000004`` / ``0.50000006`` / ``0.70000005``), so an exact
#: comparison would reject a correct layout. ``1e-3`` is ~13x tighter than a single terminal
#: cell on a 75-row split, so it cannot hide a division an operator could see.
RATIO_TOLERANCE = 1e-3

#: How many resize passes may be issued before the run gives up. herdr clamps ``--amount``
#: to **0.5 per call** (measured j#91140: ``0.1 + 0.55`` / ``+0.79`` / ``+0.8`` all land on
#: ``0.6``), so the widest legal move — ``0.1`` to ``0.9`` — needs two. Each pass recomputes
#: its delta from the freshly measured ratio rather than trusting the previous one, so the
#: loop self-corrects against the clamp instead of encoding its value; the bound and the
#: strict-progress check below are what stop an unreachable target from spinning.
MAX_RESIZE_PASSES = 4

#: The herdr resize direction that INCREASES the first child's share, per split direction
#: (measured j#91140: ``--direction down`` moved a ``down`` split's divider down, growing
#: the top pane, from either member pane).
_GROW_DIRECTION: Mapping[str, str] = {"down": "down", "right": "right"}
#: ...and the one that decreases it.
_SHRINK_DIRECTION: Mapping[str, str] = {"down": "up", "right": "left"}


@dataclass(frozen=True)
class PaneRect:
    """One pane's or split's cell rectangle as ``pane layout`` reports it (pure value)."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class SplitInfo:
    """One divider in a tab's layout tree: its axis, its share, and the area it governs.

    ``ratio`` is the FIRST child's share — the left pane under ``direction: right``, the top
    pane under ``direction: down`` (measured j#91140: a 75-row ``down`` split at ratio 0.5
    renders 38/37, at 0.6 renders 45/30). ``pane layout`` deliberately does **not** name a
    split's children, which is why :func:`find_pair_split` has to identify the pair's own
    divider geometrically.
    """

    split_id: str
    direction: str
    ratio: float
    rect: PaneRect


@dataclass(frozen=True)
class LayoutSnapshot:
    """A parsed ``pane layout`` payload — every pane rect and every split in one tab."""

    tab_id: str
    panes: Mapping[str, PaneRect]
    splits: tuple[SplitInfo, ...]


def _rect_from(record: object) -> Optional[PaneRect]:
    """A :class:`PaneRect` from a layout ``rect`` object, or ``None`` if malformed."""
    if not isinstance(record, Mapping):
        return None
    values = []
    for key in ("x", "y", "width", "height"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        values.append(value)
    return PaneRect(*values)


def parse_pane_layout(stdout: object) -> Optional[LayoutSnapshot]:
    """Parse a ``herdr pane layout`` payload, or ``None`` when it is not one (fail-closed).

    Real herdr 0.7.4 shape (measured j#91140)::

        {"result": {"type": "pane_layout",
                    "layout": {"tab_id": "w4:t1", "workspace_id": "w4",
                               "panes": [{"pane_id": "w4:p1", "rect": {...}}, ...],
                               "splits": [{"id": "split_0_root", "direction": "down",
                                           "ratio": 0.5, "rect": {...}}, ...]}}}

    A payload that is not JSON, not a ``pane_layout`` envelope, or that carries a pane /
    split whose rect is not four integers yields ``None`` — the caller then fails the ratio
    closed rather than measuring against a half-understood layout. A pane id repeated in the
    payload is likewise rejected: silently keeping one of two rects for the same id would
    make the tiling test below decide on an arbitrary half of the evidence.
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
    if not isinstance(result, Mapping) or result.get("type") != "pane_layout":
        return None
    layout = result.get("layout")
    if not isinstance(layout, Mapping):
        return None
    panes: dict[str, PaneRect] = {}
    raw_panes = layout.get("panes")
    if not isinstance(raw_panes, Sequence) or isinstance(raw_panes, (str, bytes)):
        return None
    for entry in raw_panes:
        if not isinstance(entry, Mapping):
            return None
        pane_id = entry.get("pane_id")
        rect = _rect_from(entry.get("rect"))
        if not isinstance(pane_id, str) or not pane_id or rect is None:
            return None
        if pane_id in panes:
            return None
        panes[pane_id] = rect
    splits: list[SplitInfo] = []
    raw_splits = layout.get("splits")
    if not isinstance(raw_splits, Sequence) or isinstance(raw_splits, (str, bytes)):
        return None
    for entry in raw_splits:
        if not isinstance(entry, Mapping):
            return None
        direction = entry.get("direction")
        ratio = entry.get("ratio")
        rect = _rect_from(entry.get("rect"))
        if direction not in _GROW_DIRECTION or rect is None:
            return None
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            return None
        splits.append(
            SplitInfo(
                split_id=str(entry.get("id") or ""),
                direction=str(direction),
                ratio=float(ratio),
                rect=rect,
            )
        )
    tab_id = layout.get("tab_id")
    return LayoutSnapshot(
        tab_id=tab_id if isinstance(tab_id, str) else "",
        panes=panes,
        splits=tuple(splits),
    )


def _tiles(first: PaneRect, second: PaneRect, rect: PaneRect, direction: str) -> bool:
    """True iff ``first`` then ``second`` EXACTLY tile ``rect`` along ``direction``.

    Exact tiling — not a bounding-box match — is what makes the identification sound. In a
    three-pane tab the union of two panes that are NOT siblings measured identical to the
    root split's rect (live-measured j#91140), so a bounding-box test would have picked an
    ancestor divider and resized somebody else's lane. Requiring the pair to fill the
    rectangle edge-to-edge on both axes rejects that: the non-sibling pane is narrower than
    the split it appears to span.
    """
    if direction == "down":
        return (
            first.x == second.x == rect.x
            and first.width == second.width == rect.width
            and first.y == rect.y
            and second.y == first.y + first.height
            and first.height + second.height == rect.height
        )
    return (
        first.y == second.y == rect.y
        and first.height == second.height == rect.height
        and first.x == rect.x
        and second.x == first.x + first.width
        and first.width + second.width == rect.width
    )


def order_pair(first: PaneRect, second: PaneRect, direction: str) -> bool:
    """True iff ``first`` is the FIRST child (left / top) of a ``direction`` split."""
    return first.y < second.y if direction == "down" else first.x < second.x


def find_pair_split(
    layout: LayoutSnapshot, first: PaneRect, second: PaneRect, direction: str
) -> Optional[SplitInfo]:
    """The unique split the two panes exactly tile, or ``None`` (0 or >1 candidates).

    ``first`` must already be the first child (see :func:`order_pair`). Ambiguity is a
    refusal, not a pick: two dividers that both claim the pair means the layout is not the
    one this module reasoned about.
    """
    matches = [
        split
        for split in layout.splits
        if split.direction == direction and _tiles(first, second, split.rect, direction)
    ]
    return matches[0] if len(matches) == 1 else None


def _contains(outer: PaneRect, inner: PaneRect) -> bool:
    return (
        outer.x <= inner.x
        and inner.x + inner.width <= outer.x + outer.width
        and outer.y <= inner.y
        and inner.y + inner.height <= outer.y + outer.height
    )


def governing_split(
    layout: LayoutSnapshot, pane: PaneRect, direction: str
) -> Optional[SplitInfo]:
    """The split ``pane resize --pane <pane> --direction <dir>`` will actually move.

    herdr resolves a resize to the **nearest ancestor split whose axis matches the
    direction**, regardless of which side of it the addressed pane sits on (measured
    j#91140: from either member of a ``down`` split, ``--direction down`` grew the top
    pane). Nearest-ancestor is reconstructed here as the SMALLEST same-axis split whose
    rect contains the pane, since ``pane layout`` exposes rects rather than a tree.

    The caller compares this against the pair's own split before issuing anything. That
    comparison is the shared-tab guard (Redmine #14567 lands every sublane in one tab): if
    the pair's divider is not the one herdr would move, the run refuses instead of silently
    resizing an OUTER divider and rearranging a neighbouring lane.
    """
    candidates = [
        split
        for split in layout.splits
        if split.direction == direction and _contains(split.rect, pane)
    ]
    if not candidates:
        return None
    smallest = min(candidates, key=lambda s: s.rect.width * s.rect.height)
    return smallest


def split_extent(split: SplitInfo) -> int:
    """The cell extent ``ratio`` divides: the split's height (``down``) or width."""
    return split.rect.height if split.direction == "down" else split.rect.width


def pane_extent(rect: PaneRect, direction: str) -> int:
    """One pane's extent along ``direction``'s axis."""
    return rect.height if direction == "down" else rect.width


def resize_step(current: float, target: float, direction: str) -> "tuple[str, float]":
    """The ``(--direction, --amount)`` one pass toward ``target`` should issue.

    The sign lives in the direction token, never in the amount: herdr rejects nothing about
    a negative amount, it simply moves the divider the wrong way, so the delta's sign is
    translated into ``down``/``up`` (or ``right``/``left``) and the amount is its magnitude.
    """
    delta = target - current
    token = _GROW_DIRECTION[direction] if delta > 0 else _SHRINK_DIRECTION[direction]
    return token, abs(delta)


def ratio_verdict(
    split: SplitInfo, first: PaneRect, target: float
) -> "tuple[bool, str]":
    """``(matches, detail)`` for a measured split against the declared ratio.

    Two independent checks, because they can fail apart:

    - the stored **ratio** is within :data:`RATIO_TOLERANCE` of the declaration (this is the
      number that survives a terminal resize, so it is the real contract);
    - the rendered **first-child extent** is within one cell of ``round(extent * ratio)``
      (the display rounding herdr applies, measured j#91140). A ratio that reads right while
      the panes do not is a layout this module does not understand, so it is not credited.
    """
    extent = split_extent(split)
    observed_extent = pane_extent(first, split.direction)
    expected_extent = round(extent * target)
    ratio_ok = abs(split.ratio - target) <= RATIO_TOLERANCE
    extent_ok = abs(observed_extent - expected_extent) <= 1
    detail = (
        f"declared={target:g} observed_ratio={split.ratio:.6g} "
        f"first_pane_extent={observed_extent}/{extent} expected~{expected_extent}"
    )
    return (ratio_ok and extent_ok), detail


def _created_pair_split(*, launched: int, initial_occupancy: int) -> bool:
    """True iff a launch in THIS run split an already-occupied container.

    The launches enter the container at occupancies ``o, o+1, ... o+n-1``; a slot emits
    ``--split`` exactly when its occupancy is non-zero (``herdr_lane_topology.slot_placement``),
    so at least one divider is this run's iff the container was already occupied or this run
    launched a second slot into it. This predicate — not "a pair exists" — is what keeps the
    module off live pairs: an all-adopt run launches nothing and therefore owns no divider.
    """
    return launched >= 1 and (initial_occupancy > 0 or launched >= 2)


@dataclass(frozen=True)
class PairPanes:
    """The two panes of the pair this run completed, already ordered first-then-second."""

    first_pane: str
    second_pane: str
    first_provider: str
    second_provider: str


def _pair_slots(slots: Sequence[object]) -> "Optional[tuple[object, object]]":
    """This run's two live pair slots, in launch order, or ``None``.

    Both must be slots this run actually resolved to a live pane (``launched`` /
    ``adopted``) with distinct locators. A ``stale`` / ``unattested`` surfacing is a
    read-only report about somebody else's pane, never a pair member.
    """
    live = [
        slot
        for slot in slots
        if getattr(slot, "outcome", "") in (SLOT_LAUNCHED, SLOT_ADOPTED)
        and getattr(slot, "locator", "")
    ]
    if len(live) != 2 or live[0].locator == live[1].locator:  # type: ignore[attr-defined]
        return None
    return live[0], live[1]


def intended_primary(
    slots: Sequence[object], config_order: "Optional[Sequence[str]]"
) -> str:
    """Which provider the declared ratio's share belongs to (the effective ``order[0]``).

    A declared ``order`` names it outright. With no declared order — the product default for
    the ``sublane`` class, which deliberately leaves ``order`` undeclared so the repo-local
    role binding is respected rather than overridden — the effective first provider is the
    one this run put first, i.e. the first requested slot. Either way the answer is a
    provider, so the geometry check below compares roles and not pane ids.
    """
    if config_order:
        return str(config_order[0])
    return str(getattr(slots[0], "provider", "")) if slots else ""


def _reclaim_root_panes(
    result, *, binary: str, runner: Runner, timeout: float, env: Optional[Mapping[str, str]]
) -> None:
    """Close the empty root panes this run created (Redmine #13330 / #13411).

    Relocated verbatim from the session-start composition root. Reclaim runs only after
    EVERY launch succeeded (a launch failure raised before the caller reached here, so
    reaching this point means all agents are live and the workspace / tab is safe to keep
    with just its agent panes). Close only the exact root pane ids this run captured; a
    close failure is non-fatal cosmetic residue. The workspace base pane (#13330) and the
    lane tab root pane (#13411) are distinct handles — reclaim each independently, never
    one guessed for the other.
    """
    if result.base_pane_id:
        reclaimed, detail = _close_base_pane(
            binary, result.base_pane_id, runner, timeout, env
        )
        result.base_pane_reclaimed = reclaimed
        result.base_pane_detail = detail
    if result.tab_pane_id:
        reclaimed, detail = _close_base_pane(
            binary, result.tab_pane_id, runner, timeout, env
        )
        result.tab_pane_reclaimed = reclaimed
        result.tab_pane_detail = detail


def _read_layout(
    pane_id: str, *, binary: str, runner: Runner, timeout: float, env
) -> Optional[LayoutSnapshot]:
    """``herdr pane layout --pane <id>``, parsed; ``None`` on any refusal or bad payload."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        HerdrSessionStartError,
    )

    try:
        completed = _invoke(
            binary, ["pane", "layout", "--pane", pane_id], runner, timeout, env=env
        )
    except HerdrSessionStartError:
        # A read that herdr refused is not evidence about the layout. The caller turns this
        # into a typed ratio failure; it never falls back to "assume it is fine".
        return None
    return parse_pane_layout(completed.stdout)


def _resize(
    pane_id: str,
    direction: str,
    amount: float,
    *,
    binary: str,
    runner: Runner,
    timeout: float,
    env,
) -> bool:
    """Issue one ``pane resize``; ``False`` when herdr refused it.

    ``--amount`` is rendered with fixed precision rather than Python's ``repr``: herdr parses
    it as an ``f32`` and rejects anything non-finite outright (measured j#91140 —
    ``invalid amount: nan`` / exit 2, layout unchanged), so a compact, unambiguous decimal is
    what the CLI is guaranteed to accept.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        HerdrSessionStartError,
    )

    try:
        _invoke(
            binary,
            ["pane", "resize", "--pane", pane_id, "--direction", direction,
             "--amount", f"{amount:.6f}"],
            runner,
            timeout,
            env=env,
        )
    except HerdrSessionStartError:
        return False
    return True


def _measure_pair(
    layout: Optional[LayoutSnapshot], pair: PairPanes, direction: str
) -> "tuple[Optional[SplitInfo], Optional[PaneRect], str]":
    """``(pair split, first-child rect, refusal reason)`` for one layout read.

    Returns the reason — never a guess — when the pair cannot be located: an unreadable
    payload, a pane missing from it, a divider that is not uniquely identifiable, a divider
    whose axis is not the declared one, or a divider that is not the one herdr would move
    for this pane (the shared-tab guard, see :func:`governing_split`).
    """
    if layout is None:
        return None, None, "pane layout could not be read or parsed"
    first = layout.panes.get(pair.first_pane)
    second = layout.panes.get(pair.second_pane)
    if first is None or second is None:
        return None, None, "the pair's panes are not both present in the tab layout"
    if not order_pair(first, second, direction):
        return None, None, "the pair's panes are not ordered first-then-second"
    split = find_pair_split(layout, first, second, direction)
    if split is None:
        return None, None, (
            "no single split is exactly tiled by the pair, so its divider is ambiguous"
        )
    governing = governing_split(layout, first, direction)
    if governing is None or governing.rect != split.rect:
        return None, None, (
            "a resize addressed at the pair would move an outer divider, not the pair's "
            "own; refusing to rearrange a neighbouring pane"
        )
    return split, first, ""


def _apply_ratio(
    pair: PairPanes,
    split: SplitInfo,
    first: PaneRect,
    *,
    direction: str,
    target: float,
    binary: str,
    runner: Runner,
    timeout: float,
    env,
) -> "tuple[str, str]":
    """Drive the pair's divider to ``target`` and MEASURE the result — ``(outcome, detail)``.

    ``split`` / ``first`` are the caller's opening measurement (it already had to read the
    layout to decide which pane holds the first side), so the read is not repeated here.

    The loop is the answer to herdr's two silent clamps (measured j#91140): ``--amount`` is
    capped at 0.5 per call and the resulting ratio at ``0.1..0.9``. Each pass therefore
    recomputes its step from a fresh measurement instead of from what it asked for, and the
    outcome is decided by the closing read, not by the resize exiting 0.

    Termination is bounded three ways, so an unreachable target cannot spin: the pass count
    (:data:`MAX_RESIZE_PASSES`), a match, and a pass that failed to move the ratio strictly
    closer to the target. The last one is what catches a clamped-out target — the divider
    stops moving and the run reports the residual rather than issuing the same request
    forever.
    """
    matched, detail = ratio_verdict(split, first, target)
    if matched:
        return RATIO_MATCHED, detail
    for _ in range(MAX_RESIZE_PASSES):
        distance = abs(split.ratio - target)
        token, amount = resize_step(split.ratio, target, direction)
        if not _resize(
            pair.first_pane, token, amount,
            binary=binary, runner=runner, timeout=timeout, env=env,
        ):
            return RATIO_FAILED, f"herdr refused 'pane resize --direction {token}'; {detail}"
        split, first, reason = _measure_pair(
            _read_layout(
                pair.first_pane, binary=binary, runner=runner, timeout=timeout, env=env
            ),
            pair,
            direction,
        )
        if split is None or first is None:
            return RATIO_FAILED, reason
        matched, detail = ratio_verdict(split, first, target)
        if matched:
            return RATIO_APPLIED, detail
        if abs(split.ratio - target) >= distance:
            # The divider did not move closer: herdr clamped the request. Report the
            # residual instead of repeating a call that has already stopped working.
            return RATIO_FAILED, f"herdr stopped moving the divider short of the target; {detail}"
    return RATIO_FAILED, f"the divider did not reach the declared ratio; {detail}"


def _pair_geometry(
    result,
    *,
    config_split: Optional[str],
    config_order: "Optional[Sequence[str]]",
    config_ratio: Optional[float],
    launched: int,
    initial_occupancy: int,
    dry_run: bool,
    binary: str,
    runner: Runner,
    timeout: float,
    env,
) -> "tuple[str, str]":
    """Decide and (when owed) actuate this run's pair split ratio — ``(outcome, detail)``."""
    if dry_run:
        return RATIO_NOT_APPLICABLE, "dry run: nothing was launched, so nothing was divided"
    if not _created_pair_split(launched=launched, initial_occupancy=initial_occupancy):
        return RATIO_NOT_APPLICABLE, (
            "this run created no pair divider (nothing launched, or the only launch "
            "occupied an empty container); no live pair is resized"
        )
    if config_ratio is None or not config_split:
        return RATIO_NOT_APPLICABLE, "no effective pair split ratio / direction resolved"
    slots = list(result.slots)
    pair_slots = _pair_slots(slots)
    if pair_slots is None:
        return RATIO_NOT_APPLICABLE, (
            "the pair's two panes are not both slots of this run, so the ratio's "
            "order-relative side cannot be identified; no pane is resized"
        )
    launch_first, launch_second = pair_slots
    primary = intended_primary(slots, config_order)
    layout = _read_layout(
        launch_first.locator, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return RATIO_FAILED, "pane layout could not be read or parsed"
    rect_first = layout.panes.get(launch_first.locator)
    rect_second = layout.panes.get(launch_second.locator)
    if rect_first is None or rect_second is None:
        return RATIO_FAILED, "the pair's panes are not both present in the tab layout"
    # Which pane PHYSICALLY holds the first (left / top) side decides whose share the
    # declared ratio is. herdr places the splitting slot second, so on a heal the surviving
    # sibling is first even when the configured primary is the one being launched.
    if order_pair(rect_first, rect_second, config_split):
        first_slot, second_slot = launch_first, launch_second
    else:
        first_slot, second_slot = launch_second, launch_first
    if primary and first_slot.provider != primary:
        return RATIO_DEFERRED, (
            f"the configured primary {primary!r} landed on the second side of the split "
            f"(its sibling was already live), so the declared ratio {config_ratio:g} would "
            f"go to {first_slot.provider!r}; no live pane is swapped or resized"
        )
    pair = PairPanes(
        first_pane=first_slot.locator,
        second_pane=second_slot.locator,
        first_provider=first_slot.provider,
        second_provider=second_slot.provider,
    )
    split, first, reason = _measure_pair(layout, pair, config_split)
    if split is None or first is None:
        return RATIO_FAILED, reason
    return _apply_ratio(
        pair,
        split,
        first,
        direction=config_split,
        target=float(config_ratio),
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
    )


def finalize_container_geometry(
    result,
    *,
    config_split: Optional[str],
    config_order: "Optional[Sequence[str]]",
    config_ratio: Optional[float],
    launched: int,
    initial_occupancy: int,
    dry_run: bool,
    binary: str,
    runner: Runner,
    timeout: float,
    env,
) -> None:
    """Finish the container this run launched into: reclaim the roots, divide the pair.

    The single call the session-start composition root makes. Reclaim comes first because
    closing the created root pane collapses the split tree; measuring before it would read a
    geometry that is about to change. Both halves record onto ``result`` rather than
    raising: a reclaim failure has always been cosmetic residue, and a ratio failure leaves
    a fully live pair whose only defect is its division — killing agents over that would be
    a far worse outcome than reporting it (Design Answer j#91127, "既存agentをkill/closeせず").
    The report is not cosmetic either: ``SessionStartResult.ok`` reads the ratio axis, so a
    failure is not exit-code success.
    """
    _reclaim_root_panes(result, binary=binary, runner=runner, timeout=timeout, env=env)
    result.ratio_outcome, result.ratio_detail = _pair_geometry(
        result,
        config_split=config_split,
        config_order=config_order,
        config_ratio=config_ratio,
        launched=launched,
        initial_occupancy=initial_occupancy,
        dry_run=dry_run,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
    )


__all__ = (
    "MAX_RESIZE_PASSES",
    "RATIO_APPLIED",
    "RATIO_DEFERRED",
    "RATIO_FAILED",
    "RATIO_MATCHED",
    "RATIO_NOT_APPLICABLE",
    "RATIO_OUTCOMES",
    "RATIO_TOLERANCE",
    "LayoutSnapshot",
    "PaneRect",
    "PairPanes",
    "SplitInfo",
    "finalize_container_geometry",
    "find_pair_split",
    "governing_split",
    "intended_primary",
    "order_pair",
    "pane_extent",
    "parse_pane_layout",
    "ratio_verdict",
    "resize_step",
    "split_extent",
)
