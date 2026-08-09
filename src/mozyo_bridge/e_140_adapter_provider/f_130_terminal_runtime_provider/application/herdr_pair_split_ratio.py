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

That predicate — and nothing narrower — also decides what gets MEASURED. Everything the
rail needs is found from the one slot this run launched as a split
(:func:`splitting_slot`): the pair comes off the layout around it
(:func:`find_sibling_split`), and the order question is asked only about that slot
(:func:`order_is_deferred`). The sibling may belong to an earlier run, which is exactly the
target-only single-provider heal: it splits beside a live sibling and therefore owns the
divider. An earlier cut required BOTH panes to be this run's slots and so skipped that heal
entirely — divider created, declared ratio unapplied, run still reported successful (review
j#91217 R1-F1).

Fail-closed — a ratio that was not applied is never reported as applied
----------------------------------------------------------------------
Once past the created-divider guard the run OWES a measurement, so every step that could
quietly produce the wrong geometry ends in :data:`RATIO_FAILED` with a fixed reason token
rather than an optimistic default: an unparseable layout, a divider that is not uniquely
identifiable, a splitting slot found on the side herdr does not place it, a divider that is
not the one herdr would move for that pane, a ``pane resize`` that herdr rejected, and —
last — a final measurement that disagrees with the declared ratio. herdr silently clamps
both the per-call ``--amount`` (to 0.5) and the resulting ratio (to ``0.1..0.9``), so
*issuing* a resize proves nothing; only the closing :func:`pane layout` read does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from pathlib import Path

from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_command_effect import (  # noqa: E501
    EFFECT_CHANGED as RESIZE_CHANGED,
    EFFECT_UNCHANGED as RESIZE_UNCHANGED,
    EFFECT_UNKNOWN as RESIZE_UNKNOWN,
    parse_changed_effect,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _close_base_pane,
    _invoke,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_LAUNCHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    Runner,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_PROVIDERS,
)

#: No pair split ratio was actuated, and none was owed: the run created no divider of its
#: own (dry run / nothing launched / a first launch that only occupied the container), or no
#: ratio and direction resolved at all. Every one of those is decided BEFORE any layout is
#: read — once a run is known to have created a divider it owes a measurement, and any later
#: refusal is a :data:`RATIO_FAILED`. Review j#91217 R1-F1 is why that boundary is stated
#: this sharply: a "cannot identify the pair" case had been parked here, so a target-only
#: heal that really did create a divider dropped its declared ratio and reported success.
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

#: Typed effect reported by Herdr 0.8 for ``pane resize``.  Process exit zero is
#: not mutation evidence: the authoritative value is
#: ``result.resize.changed`` in the bundled response schema.
RESIZE_REFUSED = "refused"
#: The closed outcome vocabulary, in the order a reader should read it.
RATIO_OUTCOMES: tuple[str, ...] = (
    RATIO_NOT_APPLICABLE,
    RATIO_MATCHED,
    RATIO_APPLIED,
    RATIO_DEFERRED,
    RATIO_FAILED,
)

#: The outcomes a run may call SUCCESSFUL on this axis. Deliberately enumerated rather than
#: derived as "everything except :data:`RATIO_FAILED`" (review j#91418 R5-F1).
#:
#: The derived form was the defect: :attr:`SessionStartResult.ratio_ok` asked
#: ``outcome != RATIO_FAILED``, so a typo (``appllied``), a case variant (``APPLIED``), a
#: truncation (``deferred_until_full_relaunc``), an empty string, or any unrelated token all
#: reported the run as a success — while the SIBLING axis in the same module raises on an
#: unknown slot outcome. Declaring a closed vocabulary and then judging by a single negative
#: comparison means the declaration decides nothing.
#:
#: Subtracting from :data:`RATIO_OUTCOMES` would only move the defect: a token added to the
#: vocabulary would silently join the success side. Enumerating both halves makes adding one
#: a decision, and the #14569 regression pins it with TWO guards — the partition
#: (``RATIO_OUTCOMES == RATIO_SUCCESS_OUTCOMES | {RATIO_FAILED}``) AND this set's literal
#: membership. The partition alone is not enough: growing both sets together keeps it true,
#: which was measured before the second guard was added.
RATIO_SUCCESS_OUTCOMES: frozenset[str] = frozenset(
    {RATIO_NOT_APPLICABLE, RATIO_MATCHED, RATIO_APPLIED, RATIO_DEFERRED}
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

#: The Herdr resize direction that INCREASES the first child's share, per split direction.
#: Herdr 0.8 selects a direction-facing pane edge first, so the first pane actuates this
#: direction while the second pane actuates the inverse direction in a nested same-axis tree.
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
    """The nearest same-axis split containing ``pane``.

    ``pane layout`` exposes rectangles rather than the split tree, so the nearest
    ancestor on one axis is reconstructed as the smallest containing rectangle.
    This is the split Herdr uses only after its direction-facing edge search has no
    match.  In a nested same-axis layout, a resize toward the first side must address
    a pane on that side and a resize toward the second side must address a pane on the
    opposite side; callers that actuate such a divider must select that pane explicitly.

    Pair-only callers use this helper to prove the pair's own nearest split.  Shared
    multi-column callers additionally choose the pane on the requested divider edge
    before issuing ``pane resize``.
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
    """The two panes of the divider this run created, ordered first-then-second.

    Only pane IDS. Redmine #14569 review j#91217 R1-F1: an earlier cut also demanded both
    providers, which forced the pair to consist of two slots THIS run resolved — and a
    target-only single-provider heal has exactly one. That run splits beside a live sibling
    and therefore creates the divider, so refusing it left the declared ratio unapplied
    while the run still reported success. The sibling's ROLE was never needed: the ratio
    goes to whichever pane holds the first side, and the only order question
    (:func:`order_is_deferred`) is about the slot this run launched, whose provider it knows.
    """

    first_pane: str
    second_pane: str


def splitting_slot(slots: Sequence[object]) -> "Optional[object]":
    """The slot this run launched AS A SPLIT — the anchor everything else is found from.

    Launches enter the container at rising occupancies, so when this run created a divider
    (:func:`_created_pair_split`) the LAST launched slot is necessarily the one that carried
    ``--split``: its occupancy is the highest of the run, hence non-zero. Adopted / planned /
    stale / unattested slots launched nothing and are never the anchor.

    Anchoring on this run's own launch — rather than on "the pair's two slots" — is what
    makes the rail work for a target-only heal, whose sibling belongs to an earlier run
    (review j#91217 R1-F1).
    """
    launched = [
        slot
        for slot in slots
        if getattr(slot, "outcome", "") == SLOT_LAUNCHED and getattr(slot, "locator", "")
    ]
    return launched[-1] if launched else None


def find_sibling_split(
    layout: LayoutSnapshot, anchor: str, direction: str
) -> "Optional[tuple[str, SplitInfo]]":
    """``(sibling pane id, the divider)`` the anchor shares, or ``None`` if not unique.

    The pair is read off the LAYOUT rather than off this run's slot list, so a sibling
    launched by an earlier run counts exactly as much as one launched by this one. Both
    orderings are tried because the anchor's side is not assumed here — :func:`order_pair`
    decides it — and any ambiguity (no candidate, or more than one) is a refusal.
    """
    anchor_rect = layout.panes.get(anchor)
    if anchor_rect is None:
        return None
    matches: list = []
    for pane_id, rect in layout.panes.items():
        if pane_id == anchor:
            continue
        first, second = (
            (anchor_rect, rect) if order_pair(anchor_rect, rect, direction)
            else (rect, anchor_rect)
        )
        split = find_pair_split(layout, first, second, direction)
        if split is not None:
            matches.append((pane_id, split))
    return matches[0] if len(matches) == 1 else None


def exact_pair_permutation(order: object) -> "tuple[str, ...]":
    """``order`` as a validated provider permutation, or ``()`` — never a coercion.

    The same domain the declared ``order`` already has
    (``lane_placement._normalize_order``): every canonical provider exactly once, as
    strings. An unknown provider, a duplicate, a missing one, a non-string element or a
    non-sequence yields ``()``.

    Review j#91284 R3-F1: the previous version did ``str()`` on whatever it was handed, so
    ``("unknown", "codex")`` became a live authority in which ``codex`` was NOT the primary —
    and a gateway target-only heal then resized the pair and gave the gateway's declared
    share to the surviving worker, reporting ``applied``. ``None`` even became the provider
    name ``"None"``. Returning ``()`` for anything unrecognised means an unusable order can
    only ever cause a DEFERRAL (see :func:`order_is_deferred`), never a wrong division.
    """
    if isinstance(order, (str, bytes)) or not isinstance(order, (list, tuple)):
        return ()
    seen: list = []
    for element in order:
        if (
            not isinstance(element, str)
            or element not in LANE_PLACEMENT_PROVIDERS
            or element in seen
        ):
            return ()
        seen.append(element)
    return tuple(seen) if set(seen) == set(LANE_PLACEMENT_PROVIDERS) else ()


def effective_pair_order(
    config_order: "Optional[Sequence[str]]",
    pair_order: "Optional[Sequence[str]]",
    requested: "Optional[Sequence[str]]" = None,
) -> "tuple[str, ...]":
    """Whose side the declared ratio belongs to, as a provider order (Redmine #14569).

    Three layers, in the only order that survives a caller which shrank its request. Each is
    accepted only as an :func:`exact_pair_permutation`, so a layer that cannot answer is
    skipped rather than half-answering:

    1. a **declared / product-default** ``order`` names the primary outright;
    2. otherwise the run's **stable managed pair order** — supplied by the caller that knows
       it. An undeclared ``order`` is not "no order": the ``sublane`` product default leaves
       it undeclared precisely so the repo-local role binding's ``(gateway, worker)`` order
       is respected rather than overridden, and that binding is resolved above this layer;
    3. otherwise the run's **own requested providers**, which ARE the pair order whenever the
       request is a full pair. A shrunk request is not a permutation, so it contributes
       nothing — deliberately, since the one provider it holds would otherwise be trivially
       "first" in its own truncated list (review j#91284 R3-F1: that is the false attribution
       dressed as an answer, and the deferral it produced was a coincidence, not the rule).

    Empty means unattributable, and :func:`order_is_deferred` then defers rather than
    dividing a side it cannot attribute. Review j#91263 R2-F1 is layer 2: the rail used to
    read the effective order off the requested slots, which is correct only while the request
    IS the pair — a target-only replacement shrinks it to one provider, so healing the
    GATEWAY made the surviving worker the first side and the declared share went to the wrong
    role, reported as ``applied``.
    """
    for candidate in (config_order, pair_order, requested):
        resolved = exact_pair_permutation(candidate)
        if resolved:
            return resolved
    return ()


def order_is_deferred(anchor_provider: str, effective_order: "Sequence[str]") -> bool:
    """True iff the effective primary is the very slot this run had to place SECOND.

    Deliberately the SAME rule ``herdr_lane_topology.slot_placement`` already applies to the
    ``order`` axis: a splitting slot lands on the second side, so when it carries the
    effective ``order[0]`` the physical order cannot be satisfied. Applying the ratio there
    would hand ``order[0]``'s declared share to ``order[1]``, and moving a live pane is
    forbidden — hence :data:`RATIO_DEFERRED`.

    An EMPTY effective order defers too. That is the fail-safe end of
    :func:`effective_pair_order`: a run that cannot attribute the first side must not divide
    it, and a single-provider request whose caller supplied no stable pair order is exactly
    that case — the one provider it holds is trivially "first" in its own shrunk list, which
    is precisely the false attribution R2-F1 was.
    """
    if not effective_order:
        return True
    return anchor_provider == str(effective_order[0])


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
) -> str:
    """Issue one ``pane resize`` and return its typed Herdr 0.8 effect.

    ``--amount`` is rendered with fixed precision rather than Python's ``repr``: herdr parses
    it as an ``f32`` and rejects anything non-finite outright (measured j#91140 —
    ``invalid amount: nan`` / exit 2, layout unchanged), so a compact, unambiguous decimal is
    what the CLI is guaranteed to accept.

    Exit zero alone proves no mutation.  Herdr 0.8 reports the authoritative
    boolean at ``result.resize.changed``; a refused command, malformed envelope,
    wrong result type, or non-boolean value is ``unknown`` rather than a guessed
    success.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        HerdrSessionStartError,
    )

    try:
        completed = _invoke(
            binary,
            ["pane", "resize", "--pane", pane_id, "--direction", direction,
             "--amount", f"{amount:.6f}"],
            runner,
            timeout,
            env=env,
        )
    except HerdrSessionStartError:
        return RESIZE_REFUSED
    return parse_changed_effect(
        completed.stdout, result_type="pane_resize", envelope="resize"
    )


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
        actuator_pane = (
            pair.first_pane
            if token == _GROW_DIRECTION[direction]
            else pair.second_pane
        )
        resize_effect = _resize(
            actuator_pane, token, amount,
            binary=binary, runner=runner, timeout=timeout, env=env,
        )
        if resize_effect == RESIZE_UNCHANGED:
            return RATIO_FAILED, (
                f"herdr reported no change for 'pane resize --direction {token}'; "
                f"{detail}"
            )
        if resize_effect == RESIZE_REFUSED:
            return RATIO_FAILED, (
                f"herdr refused 'pane resize --direction {token}'; {detail}"
            )
        if resize_effect != RESIZE_CHANGED:
            return RATIO_FAILED, (
                f"herdr did not prove the effect of 'pane resize --direction {token}'; "
                f"{detail}"
            )
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
    pair_order: "Optional[Sequence[str]]",
    requested: "Optional[Sequence[str]]",
    config_ratio: Optional[float],
    launched: int,
    initial_occupancy: int,
    dry_run: bool,
    binary: str,
    runner: Runner,
    timeout: float,
    env,
) -> "tuple[str, str]":
    """Decide and (when owed) actuate this run's pair split ratio — ``(outcome, detail)``.

    Everything downstream hangs off ONE fact: the slot this run launched as a split
    (:func:`splitting_slot`). The pair is then read out of the layout around it, so a
    target-only single-provider heal — whose sibling belongs to an earlier run — is measured
    exactly like a fresh pair (review j#91217 R1-F1). Once past the created-divider guard
    this run OWES a measurement, so every remaining refusal is a typed failure rather than
    ``not_applicable``: the only outcomes that skip the ratio are the ones decided before
    that guard.
    """
    if dry_run:
        return RATIO_NOT_APPLICABLE, "dry run: nothing was launched, so nothing was divided"
    if not _created_pair_split(launched=launched, initial_occupancy=initial_occupancy):
        return RATIO_NOT_APPLICABLE, (
            "this run created no pair divider (nothing launched, or the only launch "
            "occupied an empty container); no live pair is resized"
        )
    if config_ratio is None or not config_split:
        return RATIO_NOT_APPLICABLE, "no effective pair split ratio / direction resolved"
    anchor = splitting_slot(list(result.slots))
    if anchor is None:
        return RATIO_FAILED, (
            "this run created a pair divider but reports no launched slot to locate it from"
        )
    layout = _read_layout(
        anchor.locator, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return RATIO_FAILED, "pane layout could not be read or parsed"
    found = find_sibling_split(layout, anchor.locator, config_split)
    if found is None:
        return RATIO_FAILED, (
            "no single pane shares an exactly-tiled divider with the slot this run split, "
            "so the pair's divider is ambiguous"
        )
    sibling, _split = found
    anchor_rect = layout.panes[anchor.locator]
    sibling_rect = layout.panes[sibling]
    # herdr places a splitting slot on the SECOND side, so the anchor is expected there and
    # the sibling holds the first. An anchor found on the first side is a layout this module
    # did not predict — fail closed rather than divide by a guess.
    if order_pair(anchor_rect, sibling_rect, config_split):
        return RATIO_FAILED, (
            "the slot this run split occupies the first side of its divider, which is not "
            "how herdr places a split; refusing to divide an unrecognised layout"
        )
    effective_order = effective_pair_order(config_order, pair_order, requested)
    if order_is_deferred(anchor.provider, effective_order):
        return RATIO_DEFERRED, (
            f"the effective primary {anchor.provider!r} could only be launched as the "
            f"split beside a live sibling, so the declared ratio {config_ratio:g} would go "
            f"to the other side; no live pane is swapped or resized "
            f"(effective order: {list(effective_order) or 'unattributable'})"
        )
    pair = PairPanes(first_pane=sibling, second_pane=anchor.locator)
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
    pair_order: "Optional[Sequence[str]]",
    requested: "Optional[Sequence[str]]",
    config_ratio: Optional[float],
    launched: int,
    initial_occupancy: int,
    dry_run: bool,
    binary: str,
    runner: Runner,
    timeout: float,
    env,
    project_coordinator: bool = False,
    store_home: object = None,
    top_workspace_id: str = "",
) -> None:
    """Finish the container this run launched into: reclaim, column, divide the pair.

    Runs as the LAST pass of a launch, after the bounded startup-health probe (#13948)
    has settled — not before it, which is where it used to sit. herdr reports a pane it
    has just started with the same row shape as shell residue (the ``agent`` field present
    and blank) until the provider boots into it, and resolving that ambiguity is the
    health pass's job: it holds ``shell_residue`` as *retryable* for a bounded deadline
    rather than as a verdict. Reading the inventory before that pass ran made a fresh,
    healthy project-coordinator pair fail its own first column in the live rollout
    (#14996 R3, finding j#100135). The column step now also refuses a read taken before
    that verdict exists, so the dependency is checked rather than merely positional.

    Within this call, the three steps compose in one order:

    1. **reclaim** the root panes this run created — closing one collapses the split tree,
       so anything measured before it would read a geometry about to change;
    2. **project column** (Redmine #14996 R2) — a pair freshly appended to either shared
       coordinator placement workspace is bounced into its own full-height column. It runs
       BEFORE the ratio because it rebuilds the very divider the ratio is measured against;
       ``project_coordinator`` is ``True`` only for a ``shared_space`` default coordinator
       or a ``role_grouped_space`` project coordinator, which keeps this an opt-in step
       rather than a general live-relayout rail (see
       :mod:`...herdr_project_column_reflow`);
    3. **pair split ratio** (#14569) — divide the pair this run created at the declared
       ratio, measured against the geometry the two steps above settled on.

    All three record onto ``result`` rather than raising: a reclaim failure has always been
    cosmetic residue, and a column / ratio failure leaves a fully live pair whose only
    defect is its placement — killing agents over that would be a far worse outcome than
    reporting it (Design Answer j#91127, "既存agentをkill/closeせず"). The report is not
    cosmetic either: ``SessionStartResult.ok`` reads both axes, so a failure is not
    exit-code success.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E501
        reflow_project_columns,
    )

    _reclaim_root_panes(result, binary=binary, runner=runner, timeout=timeout, env=env)
    result.column_outcome, result.column_detail = reflow_project_columns(
        result,
        project_coordinator=project_coordinator,
        home=Path(store_home) if store_home else mozyo_bridge_home(),
        top_workspace_id=top_workspace_id,
        launched=launched,
        initial_occupancy=initial_occupancy,
        dry_run=dry_run,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
    )
    result.ratio_outcome, result.ratio_detail = _pair_geometry(
        result,
        config_split=config_split,
        config_order=config_order,
        pair_order=pair_order,
        requested=requested,
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
    "RATIO_SUCCESS_OUTCOMES",
    "RATIO_TOLERANCE",
    "LayoutSnapshot",
    "PaneRect",
    "PairPanes",
    "SplitInfo",
    "effective_pair_order",
    "exact_pair_permutation",
    "finalize_container_geometry",
    "find_pair_split",
    "find_sibling_split",
    "governing_split",
    "order_is_deferred",
    "order_pair",
    "pane_extent",
    "parse_pane_layout",
    "ratio_verdict",
    "resize_step",
    "split_extent",
    "splitting_slot",
)
