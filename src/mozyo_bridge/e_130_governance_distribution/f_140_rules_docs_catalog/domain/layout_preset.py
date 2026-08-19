"""Declarative pair-geometry presets for ``layout preset`` (Redmine #15708).

The pure vocabulary + record-transform layer behind ``mozyo-bridge layout preset``: a
closed set of named geometry presets (owner intent 2026-08-18 — "上下 / 左右 を簡単に
変えられるプリセット") that each expand to a ``lane_placement`` lane-class declaration
(:mod:`.lane_placement`, Redmine #13646 / #14568 / #14569). A preset is a *shorthand for
writing the existing declarative surface*, never a second placement authority: applying
one rewrites only the ``lane_placement`` block of ``.mozyo-bridge/config.yaml``, and what
a launch then does is decided by the same ``LanePlacementConfig.resolve_effective``
chain every fresh launch / heal already reads.

Boundary, kept enforced in code (display / placement only — #15708 acceptance 2):

- **Declaration, never live relayout.** Expanding a preset produces a config record;
  nothing here (or in the CLI above it) moves, swaps, resizes, creates, or closes a live
  pane. The live-effect matrix (:data:`LIVE_EFFECT_MATRIX`) states that typed, instead of
  simulating a mutation the runtime cannot perform: existing dedicated pairs take the
  explicit ``herdr pair-placement`` preview/apply route (#14608), and shared-tab column
  re-splitting has no one-shot Herdr API at all (:data:`HERDR_API_GAPS`, runbook-measured;
  upstream deliberately unfiled per owner policy #13648).
- **Preserves what it does not own.** A preset owns the lane-class ``split`` (and
  ``ratio`` only when the caller passes one). A declared ``order`` (a role-binding
  concern), a declared ``ratio`` (when the caller passes none), and the entire
  ``by_lane_kind`` block survive verbatim; declared lane KINDS that would shadow the
  preset (``resolve_effective`` consults a declared kind wholesale, #13647) are reported
  in :attr:`PresetApplication.shadowed_lane_kinds`, never silently deleted.
- **Single validation authority.** The produced block is re-parsed through
  :meth:`LanePlacementConfig.from_record` before it is returned, so an unusable ratio /
  split fails with the same fail-closed error a hand-written config would — this module
  adds no second validator that could drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_BY_LANE_KIND_KEY,
    LANE_PLACEMENT_LANE_CLASSES,
    LANE_PLACEMENT_RATIO_MAX,
    LANE_PLACEMENT_RATIO_MIN,
    LanePlacementConfig,
    LanePlacementError,
)

#: The closed preset vocabulary (Redmine #15708): preset name -> the herdr split
#: direction its lane-class declaration carries. ``stacked`` is the vertical pair (上下,
#: ``split: down`` — the #14568 product default), ``side-by-side`` is the horizontal pair
#: (左右, ``split: right`` — the pre-#14568 rollback direction). Deliberately exactly the
#: two directions Herdr 0.8 ``pane split --direction`` accepts; a preset can never name a
#: geometry the runtime cannot build fresh.
LAYOUT_PRESETS: "Mapping[str, str]" = MappingProxyType(
    {
        "stacked": "down",
        "side-by-side": "right",
    }
)

#: What applying a preset does — and deliberately does not do — to each unit population
#: (Redmine #15708 acceptance 3: state the boundary typed instead of simulating a live
#: mutation). Keys are unit populations, values are closed result tokens:
#:
#: - ``fresh_units`` — a fresh launch / heal reads the written ``lane_placement`` through
#:   ``resolve_effective`` automatically; declaring the preset IS the application.
#: - ``existing_dedicated_pair`` — a saved config never reconfigures a live pair
#:   (``lane_placement`` is future-launch policy, #13646 Non-goal); the explicit
#:   preview-first route is ``mozyo-bridge herdr pair-placement preview`` / ``apply``
#:   (#14608).
#: - ``existing_shared_tab_columns`` — no supported one-shot path at all: Herdr has no
#:   same-tab re-split API (:data:`HERDR_API_GAPS`), and the two-stage bounce the product
#:   command uses is scoped to a dedicated two-pane tab, not a shared column tab.
LIVE_EFFECT_MATRIX: "Mapping[str, str]" = MappingProxyType(
    {
        "fresh_units": "applied_at_fresh_launch",
        "existing_dedicated_pair": "requires_explicit_pair_placement_apply",
        "existing_shared_tab_columns": "unsupported_same_tab_re_split",
    }
)

#: The Herdr API gaps this feature runs into, as typed upstream follow-up candidates
#: (Redmine #15708 acceptance 3). One entry today: Herdr 0.7.1–0.8 has no one-shot
#: same-tab re-split / rotate API (``pane move --tab <same>`` is a no-op, ``pane swap``
#: keeps the direction), so converting a LIVE pair's split direction requires the
#: two-stage bounce recipe (``herdr-live-relayout-runbook.md``). Known and recorded since
#: #13648; the upstream request is deliberately unfiled per owner policy there.
HERDR_API_GAPS: "tuple[str, ...]" = ("same_tab_re_split_api_absent",)

#: The status-classification token for a declared geometry no preset matches (a mixed /
#: hand-tuned ``lane_placement``). Deliberately not a preset name: ``status`` reports it,
#: ``apply`` never accepts it.
LAYOUT_PRESET_CUSTOM = "custom"


class LayoutPresetError(ValueError):
    """A preset name / record shape this vocabulary cannot expand (fail-closed)."""


@dataclass(frozen=True)
class PresetApplication:
    """The outcome of expanding one preset against one raw config record (pure value).

    - :attr:`preset` / :attr:`split` — the applied vocabulary entry.
    - :attr:`ratio` — the caller-declared ratio, or ``None`` (existing declarations kept).
    - :attr:`lane_placement_record` — the NEW ``lane_placement`` block, already re-parsed
      through :meth:`LanePlacementConfig.from_record` (single validation authority).
    - :attr:`changes` — human-readable per-field transitions, empty when nothing changed.
    - :attr:`shadowed_lane_kinds` — declared ``by_lane_kind`` kinds that shadow the
      lane-class preset wholesale (#13647 precedence); preserved verbatim, warned typed.
    - :attr:`already_matching` — ``True`` when the record already declared exactly this
      geometry (apply is a no-op).
    """

    preset: str
    split: str
    ratio: Optional[float]
    lane_placement_record: "Mapping[str, object]"
    changes: "tuple[str, ...]"
    shadowed_lane_kinds: "tuple[str, ...]"
    already_matching: bool


def normalize_preset_name(name: object) -> str:
    """Return a validated preset name, or fail closed on anything outside the vocabulary."""
    if not isinstance(name, str) or name not in LAYOUT_PRESETS:
        raise LayoutPresetError(
            f"unknown layout preset {name!r}; available presets: {sorted(LAYOUT_PRESETS)}"
        )
    return name


def _class_entry(existing: object, *, lane_class: str) -> "dict[str, object]":
    """Return a mutable copy of one existing lane-class object (``{}`` when absent).

    A present-but-non-mapping entry fails closed here with the record path in the
    message rather than deep inside the final re-validation, so the caller learns which
    key is malformed before any transform output exists.
    """
    if existing is None:
        return {}
    if not isinstance(existing, Mapping):
        raise LayoutPresetError(
            f"'lane_placement.{lane_class}' must be a mapping to apply a preset over, "
            f"got {type(existing).__name__}"
        )
    return dict(existing)


def apply_preset_to_lane_placement(
    lane_placement: object, *, preset: str, ratio: "Optional[float]" = None
) -> PresetApplication:
    """Expand ``preset`` over an existing raw ``lane_placement`` block (pure transform).

    Returns a :class:`PresetApplication` whose :attr:`~PresetApplication.lane_placement_record`
    is the block to write back: every lane CLASS (``default`` / ``sublane``) gets
    ``split`` set to the preset's direction, ``ratio`` set only when the caller passed
    one, and its declared ``order`` / (caller-absent) ``ratio`` preserved. The
    ``version`` key and the whole ``by_lane_kind`` block pass through verbatim — declared
    kinds are *reported* as shadowing (they take wholesale precedence over the lane-class
    layer, #13647), never rewritten or dropped.

    Fail-closed: the preset name must be in :data:`LAYOUT_PRESETS`, the block (when
    present) must be a mapping of mappings, and the produced record must re-parse through
    :meth:`LanePlacementConfig.from_record` — the same parser a hand-edited config faces —
    so an out-of-domain ``ratio`` (outside ``0.1..0.9``) or any structural damage raises
    before anything could be written.
    """
    preset_name = normalize_preset_name(preset)
    split = LAYOUT_PRESETS[preset_name]
    if lane_placement is not None and not isinstance(lane_placement, Mapping):
        raise LayoutPresetError(
            "'lane_placement' must be a mapping (a YAML table) to apply a preset over, "
            f"got {type(lane_placement).__name__}"
        )
    existing: "Mapping[str, object]" = lane_placement or {}

    new_block: "dict[str, object]" = {}
    if "version" in existing:
        new_block["version"] = existing["version"]

    changes: "list[str]" = []
    for lane_class in sorted(LANE_PLACEMENT_LANE_CLASSES):
        entry = _class_entry(existing.get(lane_class), lane_class=lane_class)
        old_split = entry.get("split")
        if old_split != split:
            changes.append(
                f"lane_placement.{lane_class}.split: {old_split!r} -> {split!r}"
            )
        entry["split"] = split
        if ratio is not None:
            old_ratio = entry.get("ratio")
            if old_ratio != ratio:
                changes.append(
                    f"lane_placement.{lane_class}.ratio: {old_ratio!r} -> {ratio!r}"
                )
            entry["ratio"] = ratio
        new_block[lane_class] = entry

    kind_block = existing.get(LANE_PLACEMENT_BY_LANE_KIND_KEY)
    shadowed: "tuple[str, ...]" = ()
    if kind_block is not None:
        # Preserved verbatim — validated (like everything else) by the final re-parse.
        new_block[LANE_PLACEMENT_BY_LANE_KIND_KEY] = kind_block
        if isinstance(kind_block, Mapping):
            # Any DECLARED kind shadows the lane-class layer wholesale (a kind declaring
            # only `order` still resolves its split to the product default, not to the
            # lane class — #13647 Design Answer j#85645), so every declared kind is a
            # shadow of this preset, whatever fields it declares.
            shadowed = tuple(sorted(str(kind) for kind in kind_block))

    try:
        parsed = LanePlacementConfig.from_record(new_block)
        before = (
            LanePlacementConfig.from_record(dict(existing))
            if isinstance(existing, Mapping) and existing
            else LanePlacementConfig.default()
        )
    except LanePlacementError as exc:
        raise LayoutPresetError(str(exc)) from exc

    return PresetApplication(
        preset=preset_name,
        split=split,
        ratio=ratio,
        lane_placement_record=MappingProxyType(new_block),
        changes=tuple(changes),
        shadowed_lane_kinds=shadowed,
        already_matching=(parsed == before),
    )


def classify_effective_preset(config: LanePlacementConfig) -> str:
    """Which preset the EFFECTIVE lane-class geometry matches (else ``custom``).

    Reads the same ``resolve_effective`` chain a fresh launch reads (declaration, else
    product default — #14568), per lane class, on the lane-CLASS axis only: a preset is
    matched when every lane class resolves to that preset's split direction. Declared
    ``by_lane_kind`` entries do not change the classification (they are a finer, separate
    axis the status surface reports alongside), and a mixed per-class geometry is
    :data:`LAYOUT_PRESET_CUSTOM` rather than a guess.
    """
    splits = {
        config.resolve_effective(lane_class).split
        for lane_class in LANE_PLACEMENT_LANE_CLASSES
    }
    if len(splits) == 1:
        split = next(iter(splits))
        for preset_name, preset_split in LAYOUT_PRESETS.items():
            if preset_split == split:
                return preset_name
    return LAYOUT_PRESET_CUSTOM


__all__ = (
    "HERDR_API_GAPS",
    "LAYOUT_PRESETS",
    "LAYOUT_PRESET_CUSTOM",
    "LIVE_EFFECT_MATRIX",
    "LANE_PLACEMENT_RATIO_MAX",
    "LANE_PLACEMENT_RATIO_MIN",
    "LayoutPresetError",
    "PresetApplication",
    "apply_preset_to_lane_placement",
    "classify_effective_preset",
    "normalize_preset_name",
)
