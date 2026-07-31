"""Pair-placement vocabulary + validation for ``lane_placement`` (Redmine #13646).

The self-contained sibling of the repo-local config schema — it mirrors
:mod:`...domain.agent_launch_argv` / :mod:`...domain.role_provider_binding_config`: the
lane-class / split-direction / provider-order vocabulary and the structural / value
validators of the ``lane_placement`` block live here, so
:class:`~...domain.repo_local_config.LanePlacementConfig` stays a thin field contract and
the governance-config module stays within the module-health budget while the placement
rules are one cohesive unit.

It raises its own :class:`LanePlacementError` and imports **nothing** from
``repo_local_config`` (one-way dependency, exactly like ``agent_launch_argv``); the
composing ``LanePlacementConfig`` re-raises it as ``RepoLocalConfigError`` so the public
config-failure boundary stays uniform.

``lane_placement`` is the config-driven herdr pair placement knob (owner intent 2026-07-12,
Design Answer j#76564): a ``lane_class -> {split, order, ratio}`` table that decides the
herdr pane pair's split *direction* (``right`` / ``down``), the provider *order* (which
provider occupies first / left / top, which one splits beside it), and — since Redmine
#14569 (owner intent 2026-07-27, Design Answer j#91127) — the *relative share* of that one
split the first provider takes, per lane class. It is a **future launch policy** only: it
shapes what a fresh launch / heal builds, and never reconfigures a pair it did not just
complete (Non-goal: no live relayout).

The ratio axis is deliberately a **relative** share, not a pixel / column / row count
(issue Non-goals): herdr stores a split as a ratio and re-derives the pane rects from the
container, so a declared ratio survives a terminal resize while a fixed extent would not.

**Declaration vs effective geometry (Redmine #14568).** Everything in this module that
*parses* or *reports* the block answers "what did the operator declare", and an absent
declaration stays absent there. What a LAUNCH uses is a different question, answered only by
:meth:`LanePlacementConfig.resolve_effective`: it composes ``by_lane_kind`` > lane class >
:func:`product_default_placement`, and that bottom layer is ``split: down`` on both lane
classes. So an undeclared workspace is NOT the pre-#14568 launch — #13646's undeclared
byte-invariance was deliberately superseded by owner intent 2026-07-27. Read a "default" /
"absent" / "no override" statement below as scoped to the declaration, never to the geometry.

The config KEY is ``lane_placement`` — deliberately NOT ``pane_placement``: the repo-local
schema boundary (:data:`...repo_local_config._FORBIDDEN_KEY_PARTS`) rejects any config key
containing ``pane`` (a live-pane-addressing shape) before the allowed-key check, so a
declarative launch-policy block is named for the lane class it keys on, never the live pane
it eventually places (Design Answer j#76564 Q1, worker characterization j#76559).

Axis: ``lane_class`` (``default`` = the coordinator / auditor pair, ``sublane`` = a lane
worker / gateway) — the SAME lane-class axis as ``agent_launch`` but a DIFFERENT concern
(pane geometry, not launch-argv tokens): the two are resolved independently and never
merged (Design Answer j#76564 Q4).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Optional

from mozyo_bridge.core.state.lane_kind import LANE_KINDS

#: The closed lane-class vocabulary of ``lane_placement`` (Redmine #13646). Matches
#: ``agent_launch``'s lane-class axis: ``default`` (the coordinator / auditor pair) and
#: ``sublane`` (a lane worker / gateway). ``version`` is handled by the composing
#: ``LanePlacementConfig.from_record`` (like every other sub-block), so it is not a
#: lane-class key here.
LANE_PLACEMENT_LANE_CLASSES: frozenset[str] = frozenset({"default", "sublane"})

#: The closed provider vocabulary an ``order`` permutation ranges over. A valid ``order`` is
#: an EXACT permutation of this set (both providers, once each) — never a subset, superset,
#: duplicate, or unknown provider (Design Answer j#76564 Q1).
LANE_PLACEMENT_PROVIDERS: frozenset[str] = frozenset({"claude", "codex"})

#: The closed split-direction vocabulary. herdr 0.7.1 ``agent start`` accepts exactly
#: ``--split right|down`` (live ``--help`` characterization j#76559), so a config ``split``
#: is one of these two literals or fails closed.
LANE_PLACEMENT_SPLIT_DIRECTIONS: frozenset[str] = frozenset({"right", "down"})

#: The closed set of recognized keys inside a lane-class placement object. All three are
#: individually optional (Design Answer j#76564 Q1 / Q3, j#91127): a missing field is simply
#: UNDECLARED. An empty ``{}`` object therefore records a lane-class entry that declares
#: no field — it is a no-op on the *declaration*, but NOT on the launch: since Redmine
#: #14568 each undeclared field resolves through
#: :meth:`LanePlacementConfig.resolve_effective` to :func:`product_default_placement`
#: (``split: down``, and since #14569 ``ratio: 0.5``), so ``{}`` and an absent block land
#: the pair identically — vertically, evenly. Rolling a lane class back takes an explicit
#: ``split: right``, never an empty object.
LANE_PLACEMENT_CLASS_KEYS: frozenset[str] = frozenset({"split", "order", "ratio"})

#: The inclusive bounds a declared ``ratio`` must fall in (Redmine #14569, Design Answer
#: j#91127). Deliberately herdr's OWN effective domain rather than the wider ``0..1`` its
#: CLI parser accepts: herdr 0.7.4's layout clamps a split ratio into ``0.1..0.9``
#: (live-measured j#91140 — driving a 0.5 split down by 0.9 lands on 0.1, not 0.0), so a
#: config value outside it could never be the geometry the pair actually takes. Narrowing
#: the schema to the effective domain is what keeps a DECLARED value and the EFFECTIVE
#: layout the same number; accepting ``0.95`` and letting herdr silently clamp it to
#: ``0.9`` is the drift this rejects (j#91127 "silent clamp を採らない理由").
LANE_PLACEMENT_RATIO_MIN: float = 0.1
LANE_PLACEMENT_RATIO_MAX: float = 0.9

#: The closed lane-KIND vocabulary of the ``by_lane_kind`` block (Redmine #13647,
#: disposition j#85650 P3): the three canonical delegation-geometry tokens
#: ``coordinator`` (親) / ``delegated_coordinator`` (子) / ``implementation`` (孫).
#: The single source of truth is :data:`mozyo_bridge.core.state.lane_kind.LANE_KINDS`;
#: there is deliberately no ``parent`` / ``child`` / ``grandchild`` config alias
#: (the machine vocabulary is exactly these three tokens).
LANE_PLACEMENT_LANE_KINDS: frozenset[str] = LANE_KINDS

#: The additive top-level ``lane_placement`` key that carries the lane-KIND placement
#: table (Redmine #13647). Deliberately a *separate* nested block from the flat
#: ``default`` / ``sublane`` lane-CLASS keys so the two axes never collide: adding this
#: block left the lane-class axis's own PARSING and resolution untouched (#13646), and the
#: lane-kind axis is a finer partition consulted at higher precedence (Design Answer
#: j#85645). That disjointness is about the two axes, not about the launch geometry an
#: undeclared repo gets — Redmine #14568 changed the latter for both axes at once.
LANE_PLACEMENT_BY_LANE_KIND_KEY = "by_lane_kind"

#: The supported ``lane_placement`` record version. Kept intentionally identical to
#: :data:`...repo_local_config.REPO_LOCAL_CONFIG_VERSION`; the two are small and
#: deliberately duplicated so neither layer depends on the other (the same one-way rule the
#: sibling ``agent_launch_argv`` / ``_MODEL_TOKEN_RE`` duplication already follows). A
#: record's ``version`` is optional and defaults to this; any other value is rejected so a
#: future, not-yet-understood schema never reads as version 1.
LANE_PLACEMENT_CONFIG_VERSION: int = 1

#: The closed set of recognized top-level keys inside the ``lane_placement`` block: an
#: optional ``version`` plus the lane-class keys. The block carries *pane-geometry launch
#: intent only* — split direction + provider order — never any routing / target /
#: owner-approval / close / send authority.
LANE_PLACEMENT_KEYS: frozenset[str] = frozenset(
    {"version", LANE_PLACEMENT_BY_LANE_KIND_KEY} | set(LANE_PLACEMENT_LANE_CLASSES)
)


class LanePlacementError(ValueError):
    """A ``lane_placement`` block violates the closed schema (fail-closed).

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    repo-local domain errors. The composing
    :class:`~...domain.repo_local_config.LanePlacementConfig` re-raises this as its own
    ``RepoLocalConfigError`` so the repo-local config loader keeps a single fail-closed
    boundary.
    """


@dataclass(frozen=True)
class ResolvedPlacement:
    """The placement policy declared for one lane class / kind (pure value).

    A ``None`` field is UNDECLARED, not a value: :meth:`LanePlacementConfig.resolve` and
    :meth:`resolve_by_lane_kind` return what the operator wrote and nothing else, so a
    partial object (``split`` set, ``order`` absent) declares only the split direction
    (Design Answer j#76564 Q3). ``order`` is a full permutation of
    :data:`LANE_PLACEMENT_PROVIDERS` when set. ``ratio`` (Redmine #14569) is the share of
    the pair's split that ``order[0]`` — the LEFT pane under ``split: right``, the TOP pane
    under ``split: down`` — occupies, in ``LANE_PLACEMENT_RATIO_MIN..MAX``.

    What an undeclared field falls back to is :func:`product_default_placement`, applied
    once at the end of the precedence chain by :meth:`LanePlacementConfig.resolve_effective`
    (Redmine #14568) — never here, so "the operator declared it" stays distinguishable from
    "the product decided it" (the distinction ``config status`` reports as
    ``declared`` / ``default``).
    """

    split: Optional[str] = None
    order: Optional[tuple[str, ...]] = None
    ratio: Optional[float] = None


#: The PRODUCT-DEFAULT placement per lane class — the bottom of the
#: ``by_lane_kind > lane_class > product default`` precedence (Redmine #14568, owner intent
#: 2026-07-27 + scope amendment j#89848).
#:
#: This deliberately SUPERSEDES the #13646 unset byte-invariance. Before it, an undeclared
#: lane class inherited the historical launch discipline — the ``default`` pair emitted no
#: ``--split`` at all (herdr server default) and a ``sublane`` emitted the literal
#: ``--split right`` — and both render HORIZONTALLY on real herdr (live-measured j#76622:
#: herdr's own default direction is ``right``). Owner intent is that a workspace which
#: declares nothing gets the VERTICAL pair on both, so the default moves here rather than
#: every adopter having to write the same block. Explicit ``split: right`` is the rollback,
#: per lane class and per lane kind.
#:
#: ``order`` differs by lane class because only one of the two has a role binding to
#: respect:
#:
#: - ``default`` — the bare ``mozyo`` coordinator pair launches the FIXED
#:   ``default_agent_topology.DEFAULT_EXPECTED_AGENTS`` topology, whose launch order is
#:   ``claude`` first; with a vertical split that would put the implementer on TOP. The
#:   product default pins ``(codex, claude)`` so the coordinator deterministically occupies
#:   the upper pane under the default Codex/Claude binding. A workspace on a different
#:   binding declares ``lane_placement.default.order`` explicitly.
#: - ``sublane`` — a lane already launches ``(gateway, worker)`` resolved from the
#:   repo-local role binding (``sublane_actuator_herdr_ops._launch_providers``), so a
#:   product-default ``order`` here would OVERRIDE that binding rather than respect it. It
#:   stays undeclared (keep the requested order), which puts the gateway in the upper pane
#:   — ``codex`` under the default binding — while a rebound binding still launches ITS
#:   providers in ITS order.
#:
#: ``ratio`` (Redmine #14569) is ``0.5`` on BOTH lane classes, deliberately: an even pair is
#: the geometry #14568's undeclared default already lands on, so adopting the ratio axis
#: changes nothing for a workspace that declares no ``ratio``. Unlike ``order``, ``ratio``
#: carries no role binding to override — it is a share of the split, not a choice of
#: provider — so there is no reason to leave it undeclared on ``sublane``.
PRODUCT_DEFAULT_PLACEMENTS: "Mapping[str, ResolvedPlacement]" = MappingProxyType(
    {
        "default": ResolvedPlacement(split="down", order=("codex", "claude"), ratio=0.5),
        "sublane": ResolvedPlacement(split="down", ratio=0.5),
    }
)


def product_default_placement(lane_class: str) -> ResolvedPlacement:
    """The product default for ``lane_class`` (Redmine #14568).

    The single source of the undeclared-field fallback, read by BOTH the launch chokepoint
    (:meth:`LanePlacementConfig.resolve_effective`, via the herdr placement decisions) and
    the operator-facing ``config status`` projection, so the geometry a fresh launch takes
    and the geometry status reports can never drift apart.

    An unknown lane class yields a fully-undeclared :class:`ResolvedPlacement` rather than
    guessing a geometry for a class this build has no product opinion about. The closed
    class vocabulary (:data:`LANE_PLACEMENT_LANE_CLASSES`) is enforced at parse time, so
    this is unreachable from a parsed config.
    """
    return PRODUCT_DEFAULT_PLACEMENTS.get(lane_class, ResolvedPlacement())


def _normalize_order(order: object, *, lane_class: str, source: str) -> Optional[tuple[str, ...]]:
    """Return a validated provider-order tuple, or ``None`` when the field is absent.

    Redmine #13646 (Design Answer j#76564 Q1): ``order`` is an EXACT permutation of
    :data:`LANE_PLACEMENT_PROVIDERS` — a list of every provider exactly once. A non-list, a
    non-string element, an unknown provider, a duplicate, or a wrong-length list fails
    closed so a partial / ambiguous order can never silently drop a provider.
    """
    if order is None:
        return None
    if isinstance(order, (str, bytes)) or not isinstance(order, (list, tuple)):
        raise LanePlacementError(
            f"{source} 'lane_placement.{lane_class}.order' must be a list naming each "
            f"provider {sorted(LANE_PLACEMENT_PROVIDERS)} exactly once, got "
            f"{type(order).__name__}"
        )
    seen: list = []
    for element in order:
        if not isinstance(element, str) or element not in LANE_PLACEMENT_PROVIDERS:
            raise LanePlacementError(
                f"{source} 'lane_placement.{lane_class}.order' element must be one of "
                f"{sorted(LANE_PLACEMENT_PROVIDERS)}, got {element!r}"
            )
        if element in seen:
            raise LanePlacementError(
                f"{source} 'lane_placement.{lane_class}.order' lists provider "
                f"{element!r} more than once; it must be an exact permutation"
            )
        seen.append(element)
    if set(seen) != LANE_PLACEMENT_PROVIDERS:
        missing = sorted(LANE_PLACEMENT_PROVIDERS - set(seen))
        raise LanePlacementError(
            f"{source} 'lane_placement.{lane_class}.order' must name every provider "
            f"{sorted(LANE_PLACEMENT_PROVIDERS)} exactly once; missing {missing}"
        )
    return tuple(seen)


def _normalize_split(split: object, *, lane_class: str, source: str) -> Optional[str]:
    """Return a validated split direction, or ``None`` when the field is absent.

    Redmine #13646: ``split`` is one of :data:`LANE_PLACEMENT_SPLIT_DIRECTIONS`
    (``right`` / ``down``, the herdr 0.7.1 ``agent start --split`` vocabulary). Anything
    else fails closed.
    """
    if split is None:
        return None
    if not isinstance(split, str) or split not in LANE_PLACEMENT_SPLIT_DIRECTIONS:
        raise LanePlacementError(
            f"{source} 'lane_placement.{lane_class}.split' must be one of "
            f"{sorted(LANE_PLACEMENT_SPLIT_DIRECTIONS)}, got {split!r}"
        )
    return split


def _normalize_ratio(ratio: object, *, lane_class: str, source: str) -> Optional[float]:
    """Return a validated pair-split ratio, or ``None`` when the field is absent.

    Redmine #14569 (Design Answer j#91127). The whole point of this validator is that an
    unusable value must fail at CONFIG PARSE time, before any pane actuation — herdr's own
    CLI would take a ``0.95`` and its layout would silently clamp the pair to ``0.9``
    (live-measured j#91140), leaving the declared number and the rendered geometry
    permanently disagreeing. So the four rejected shapes are, in order:

    - **``bool``** — ``True`` is an ``int`` in Python and would otherwise sail through the
      numeric check as ``1.0``. A YAML ``ratio: yes`` is a typo, not a ratio.
    - **non-numeric** — a string (including ``"0.6"``), list, mapping, ``None``-like
      sentinel: never coerced. ``ratio: "0.6"`` is a quoting mistake and reads as one.
    - **non-finite** — ``nan`` / ``±inf`` (reachable from YAML as ``.nan`` / ``.inf``).
      They compare falsely against bounds (``nan`` fails EVERY comparison), so they are
      rejected by an explicit :func:`math.isfinite` test rather than by the range test.
    - **out of range** — outside
      :data:`LANE_PLACEMENT_RATIO_MIN`..:data:`LANE_PLACEMENT_RATIO_MAX`, herdr's own
      effective domain.

    An ``int`` in range is accepted and widened (there is no in-range integer today, since
    the bounds exclude ``0`` and ``1``, but the numeric contract does not depend on that).
    """
    if ratio is None:
        return None
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise LanePlacementError(
            f"{source} 'lane_placement.{lane_class}.ratio' must be a number in "
            f"{LANE_PLACEMENT_RATIO_MIN}..{LANE_PLACEMENT_RATIO_MAX}, got "
            f"{type(ratio).__name__} {ratio!r}"
        )
    value = float(ratio)
    if not math.isfinite(value):
        raise LanePlacementError(
            f"{source} 'lane_placement.{lane_class}.ratio' must be a finite number in "
            f"{LANE_PLACEMENT_RATIO_MIN}..{LANE_PLACEMENT_RATIO_MAX}, got {ratio!r}"
        )
    if not (LANE_PLACEMENT_RATIO_MIN <= value <= LANE_PLACEMENT_RATIO_MAX):
        raise LanePlacementError(
            f"{source} 'lane_placement.{lane_class}.ratio' must be within "
            f"{LANE_PLACEMENT_RATIO_MIN}..{LANE_PLACEMENT_RATIO_MAX} (herdr's own "
            f"effective split domain; a value outside it could never be the geometry the "
            f"pair takes), got {value!r}"
        )
    return value


def parse_lane_placement_record(
    record: object, *, source: str
) -> "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]], Optional[float]], ...]":
    """Normalize a ``lane_placement`` lane-class mapping into sorted, frozen records.

    Shape: ``lane_class -> {split?, order?, ratio?}`` where ``lane_class`` is a
    :data:`LANE_PLACEMENT_LANE_CLASSES` value. Returns a sorted tuple of
    ``(lane_class, split, order, ratio)`` records (hashable, so the composing
    :class:`RepoLocalConfig` stays hashable), with ``split`` / ``order`` / ``ratio`` each
    ``None`` when absent. The ``version`` key is stripped by the composing
    ``LanePlacementConfig.from_record`` before this is called, so every remaining key must
    be a lane class. A non-mapping block, an unknown lane class, an unknown class-object
    key, a non-mapping class object, or an invalid ``split`` / ``order`` value fails closed.
    ``None`` yields ``()`` — nothing DECLARED on this axis. What an undeclared lane class
    then launches with is decided one layer up, by
    :meth:`LanePlacementConfig.resolve_effective` (product default since #14568); this
    parser reports the operator's record and nothing more.
    """
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise LanePlacementError(
            f"{source} 'lane_placement' must be a mapping of lane_class -> "
            f"{{split, order, ratio}}, got {type(record).__name__}"
        )
    records: list = []
    for lane_class, obj in record.items():
        if not isinstance(lane_class, str) or lane_class not in LANE_PLACEMENT_LANE_CLASSES:
            raise LanePlacementError(
                f"{source} 'lane_placement' lane-class key must be one of "
                f"{sorted(LANE_PLACEMENT_LANE_CLASSES)}, got {lane_class!r}"
            )
        if not isinstance(obj, Mapping):
            raise LanePlacementError(
                f"{source} 'lane_placement.{lane_class}' must be a mapping with optional "
                f"'split' / 'order' / 'ratio' keys, got {type(obj).__name__}"
            )
        for key in obj:
            if not isinstance(key, str) or key not in LANE_PLACEMENT_CLASS_KEYS:
                raise LanePlacementError(
                    f"{source} 'lane_placement.{lane_class}' has unknown key {key!r}; "
                    f"allowed keys: {sorted(LANE_PLACEMENT_CLASS_KEYS)}"
                )
        split = _normalize_split(obj.get("split"), lane_class=lane_class, source=source)
        order = _normalize_order(obj.get("order"), lane_class=lane_class, source=source)
        ratio = _normalize_ratio(obj.get("ratio"), lane_class=lane_class, source=source)
        records.append((lane_class, split, order, ratio))
    return tuple(sorted(records))


def validate_lane_placement(
    placements: (
        "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]], Optional[float]], ...]"
    ),
    *,
    source: str,
) -> None:
    """Fail closed on an invalid ``placements`` tuple (lane_class / split / order / ratio).

    Runs the full validation so a directly-constructed
    ``LanePlacementConfig(placements=...)`` is checked as thoroughly as one parsed from a
    record (mirrors :func:`...agent_launch_argv.validate_launch_argv`): every lane class is
    in vocabulary, no lane class appears twice, every ``split`` is a valid direction, every
    ``order`` is a full permutation of :data:`LANE_PLACEMENT_PROVIDERS`, and every ``ratio``
    is a finite number inside herdr's effective split domain.
    """
    seen: set = set()
    for lane_class, split, order, ratio in placements:
        if lane_class not in LANE_PLACEMENT_LANE_CLASSES:
            raise LanePlacementError(
                f"{source} 'lane_placement' lane_class must be one of "
                f"{sorted(LANE_PLACEMENT_LANE_CLASSES)}, got {lane_class!r}"
            )
        if lane_class in seen:
            raise LanePlacementError(
                f"{source} 'lane_placement' lists lane_class {lane_class!r} more than once"
            )
        seen.add(lane_class)
        _normalize_split(split, lane_class=lane_class, source=source)
        if order is not None:
            _normalize_order(list(order), lane_class=lane_class, source=source)
        _normalize_ratio(ratio, lane_class=lane_class, source=source)


def parse_lane_kind_placement_record(
    record: object, *, source: str
) -> "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]], Optional[float]], ...]":
    """Normalize a ``by_lane_kind`` mapping into sorted, frozen records (Redmine #13647).

    Shape: ``lane_kind -> {split?, order?, ratio?}`` where ``lane_kind`` is a
    :data:`LANE_PLACEMENT_LANE_KINDS` token. Returns a sorted tuple of
    ``(lane_kind, split, order, ratio)`` records (hashable), reusing the SAME per-field
    validators as the lane-class table (:func:`_normalize_split` /
    :func:`_normalize_order` / :func:`_normalize_ratio`), so the geometry vocabulary is
    identical on both axes. A non-mapping block, an unknown lane kind (e.g. a ``parent`` /
    ``child`` alias — there is no such alias), an unknown class-object key, a non-mapping
    class object, or an invalid ``split`` / ``order`` / ``ratio`` value fails closed.
    ``None`` yields ``()`` — nothing DECLARED on the lane-kind axis, so every kind falls
    through to the lane-class layer (and, undeclared there too, to the product default).
    """
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise LanePlacementError(
            f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}' must be a "
            f"mapping of lane_kind -> {{split, order, ratio}}, got {type(record).__name__}"
        )
    records: list = []
    for lane_kind, obj in record.items():
        if not isinstance(lane_kind, str) or lane_kind not in LANE_PLACEMENT_LANE_KINDS:
            raise LanePlacementError(
                f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}' lane-kind "
                f"key must be one of {sorted(LANE_PLACEMENT_LANE_KINDS)}, got {lane_kind!r}"
            )
        if not isinstance(obj, Mapping):
            raise LanePlacementError(
                f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}.{lane_kind}' "
                f"must be a mapping with optional 'split' / 'order' / 'ratio' keys, got "
                f"{type(obj).__name__}"
            )
        for key in obj:
            if not isinstance(key, str) or key not in LANE_PLACEMENT_CLASS_KEYS:
                raise LanePlacementError(
                    f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}."
                    f"{lane_kind}' has unknown key {key!r}; allowed keys: "
                    f"{sorted(LANE_PLACEMENT_CLASS_KEYS)}"
                )
        # The per-field validators embed their label in the error path; pass the
        # kind under the ``by_lane_kind.<kind>`` prefix so a message reads
        # 'lane_placement.by_lane_kind.coordinator.order', accurate to the axis.
        label = f"{LANE_PLACEMENT_BY_LANE_KIND_KEY}.{lane_kind}"
        split = _normalize_split(obj.get("split"), lane_class=label, source=source)
        order = _normalize_order(obj.get("order"), lane_class=label, source=source)
        ratio = _normalize_ratio(obj.get("ratio"), lane_class=label, source=source)
        records.append((lane_kind, split, order, ratio))
    return tuple(sorted(records))


def validate_lane_kind_placement(
    placements: (
        "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]], Optional[float]], ...]"
    ),
    *,
    source: str,
) -> None:
    """Fail closed on an invalid ``by_lane_kind`` record tuple (Redmine #13647).

    The lane-kind sibling of :func:`validate_lane_placement`, so a directly-built
    ``LanePlacementConfig(kind_placements=...)`` is checked as thoroughly as one
    parsed from a record: every lane kind is in vocabulary, no kind appears twice,
    every ``split`` is a valid direction, every ``order`` is a full permutation
    of :data:`LANE_PLACEMENT_PROVIDERS`, and every ``ratio`` is inside herdr's
    effective split domain.
    """
    seen: set = set()
    for lane_kind, split, order, ratio in placements:
        if lane_kind not in LANE_PLACEMENT_LANE_KINDS:
            raise LanePlacementError(
                f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}' lane_kind "
                f"must be one of {sorted(LANE_PLACEMENT_LANE_KINDS)}, got {lane_kind!r}"
            )
        if lane_kind in seen:
            raise LanePlacementError(
                f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}' lists "
                f"lane_kind {lane_kind!r} more than once"
            )
        seen.add(lane_kind)
        label = f"{LANE_PLACEMENT_BY_LANE_KIND_KEY}.{lane_kind}"
        _normalize_split(split, lane_class=label, source=source)
        if order is not None:
            _normalize_order(list(order), lane_class=label, source=source)
        _normalize_ratio(ratio, lane_class=label, source=source)


@dataclass(frozen=True)
class LanePlacementConfig:
    """The lane-class herdr pane-pair placement knob (Redmine #13646) — field contract.

    The typed contract for the ``lane_placement`` block of ``.mozyo-bridge/config.yaml``.
    It lives in this sibling (not in ``repo_local_config``) so the governance-config module
    stays within its module-health budget while the placement rules — vocabulary,
    validators, and this contract — remain one cohesive unit. ``RepoLocalConfig`` composes
    it and re-raises :class:`LanePlacementError` as its own ``RepoLocalConfigError``, so the
    public config-failure boundary stays uniform.

    Value field:

    - :attr:`placements` — a frozen, sorted tuple of ``(lane_class, split, order, ratio)``
      records parsed from the ``lane_placement: {lane_class: {split, order, ratio}}``
      mapping. ``split`` is a :data:`LANE_PLACEMENT_SPLIT_DIRECTIONS` value or ``None``;
      ``order`` is a full permutation of :data:`LANE_PLACEMENT_PROVIDERS` or ``None`` (keep
      the requested launch order); ``ratio`` is ``order[0]``'s share of the pair's split, in
      :data:`LANE_PLACEMENT_RATIO_MIN`..:data:`LANE_PLACEMENT_RATIO_MAX`, or ``None``. All
      three are individually optional per lane class.

    Boundary, kept enforced in code (this is *launch geometry intent*, not authority):

    - **Future launch policy, never live layout.** It shapes what a fresh launch / heal
      builds — the ``agent start`` argv, and (since Redmine #14569) the one post-launch
      ``pane resize`` that divides the pair this run completed. It is never a live layout /
      liveness / route authority: loading a config actuates nothing, and no pane this run
      did not just place is ever moved, swapped, or resized (Non-goal: no live relayout).
    - **Geometry only.** ``split`` names a herdr split direction, ``order`` names launch
      providers, and ``ratio`` names a share of one split; the block can never address a
      live pane / target / route, name an executable, or grant approval / close / send
      authority.
    - **Declaration-preserving, not default-preserving** (Redmine #14568). An absent block
      still parses to an empty policy — :attr:`placements` and :attr:`kind_placements` are
      both ``()`` — but an undeclared FIELD no longer means "launch exactly as before": it
      resolves through :meth:`resolve_effective` to :func:`product_default_placement`
      (``split: down`` on both lane classes). The #13646 unset byte-invariance is
      deliberately superseded; ``split: right`` is the per-class / per-kind rollback. The
      ratio axis added in #14569 does NOT extend that supersession: its product default
      ``0.5`` is the even division an undeclared pair already got, so adding the axis is
      geometry-preserving for every workspace that declares no ``ratio``.
    """

    version: int = LANE_PLACEMENT_CONFIG_VERSION
    placements: (
        "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]], Optional[float]], ...]"
    ) = ()
    #: The lane-KIND placement table parsed from the ``by_lane_kind`` block (Redmine
    #: #13647). A frozen, sorted tuple of ``(lane_kind, split, order, ratio)`` records,
    #: disjoint from :attr:`placements` (the lane-CLASS table). Empty when the block
    #: is absent, so a repo with no ``by_lane_kind`` block declares nothing on this axis
    #: and resolves exactly as it did pre-#13647 — i.e. through the lane-class layer, whose
    #: own undeclared fallback is the #14568 / #14569 product default.
    kind_placements: (
        "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]], Optional[float]], ...]"
    ) = field(default=())

    def __post_init__(self) -> None:
        # Validate on construction too, so a directly-built LanePlacementConfig(...) is
        # checked as thoroughly as one parsed from a record (no dataclass back door).
        validate_lane_placement(self.placements, source="lane placement config")
        validate_lane_kind_placement(self.kind_placements, source="lane placement config")

    def resolve_by_lane_kind(self, lane_kind: str) -> ResolvedPlacement:
        """The DECLARED :class:`ResolvedPlacement` for ``lane_kind`` (the lane-KIND layer).

        The lane-KIND lookup consulted at higher precedence than :meth:`resolve` (the
        lane-CLASS lookup). Returns the configured ``(split, order, ratio)`` for a present
        lane kind, else a fully undeclared :class:`ResolvedPlacement` (every field ``None``)
        so the caller falls through to the lane-class layer. Like :meth:`resolve` it reports
        the declaration only — :meth:`resolve_effective` is what a launch reads.
        """
        for entry_kind, split, order, ratio in self.kind_placements:
            if entry_kind == lane_kind:
                return ResolvedPlacement(split=split, order=order, ratio=ratio)
        return ResolvedPlacement()

    def has_lane_kind(self, lane_kind: str) -> bool:
        """True iff ``by_lane_kind`` explicitly declares ``lane_kind`` (Redmine #13647).

        The precedence gate: the lane-kind layer is consulted ONLY when the config
        explicitly declares that kind, so an undeclared kind falls through to the
        lane-class layer intact, never shadowing it with an empty override.
        """
        return any(entry[0] == lane_kind for entry in self.kind_placements)

    def resolve(self, lane_class: str) -> ResolvedPlacement:
        """The DECLARED :class:`ResolvedPlacement` for ``lane_class`` (the lane-CLASS layer).

        Returns the configured ``(split, order, ratio)`` for a present lane class, else a
        fully undeclared :class:`ResolvedPlacement` (every field ``None``). It reports the
        operator's declaration only; :meth:`resolve_effective` is what a launch reads,
        because it also applies the product default to whatever this layer leaves
        undeclared.
        """
        for entry_class, split, order, ratio in self.placements:
            if entry_class == lane_class:
                return ResolvedPlacement(split=split, order=order, ratio=ratio)
        return ResolvedPlacement()

    def resolve_effective(
        self, lane_class: str, lane_kind: "Optional[str]" = None
    ) -> ResolvedPlacement:
        """The geometry a launch actually uses: declaration, else product default (#14568).

        The single placement-policy authority ``prepare_session`` reads once per run. It
        composes the whole ``by_lane_kind > lane_class > product default`` precedence, so no
        caller re-implements a layer and the launch path holds no fallback of its own:

        1. **lane-kind layer** — consulted ONLY when a durable ``lane_kind`` is supplied AND
           the config explicitly declares that kind (:meth:`has_lane_kind`). A declared kind
           SHADOWS the lane-class entry wholesale, exactly as it did pre-#14568 (Redmine
           #13647, Design Answer j#85645): a kind that declares ``order`` alone does not
           inherit its lane class's ``split`` — nor, since Redmine #14569, its ``ratio``.
           The ratio axis joins the existing wholesale shadowing rather than introducing a
           per-field merge; a kind that wants its class's ratio restates it (j#91127).
        2. **lane-class layer** — otherwise :meth:`resolve` (``default`` / ``sublane``).
        3. **product default** — every field still undeclared after 1–2 is filled from
           :func:`product_default_placement`, per field. This is the layer #14568 adds; it
           is what makes an undeclared workspace launch its pairs ``down``, and (#14569)
           divide them evenly.

        ``lane_kind is None`` (no durable kind fact — the launch path's fallback) and a
        config with no matching ``by_lane_kind`` entry both fall straight through to step 2,
        so the kind axis still never shadows the class axis with an empty override.
        """
        declared = (
            self.resolve_by_lane_kind(lane_kind)
            if lane_kind is not None and self.has_lane_kind(lane_kind)
            else self.resolve(lane_class)
        )
        fallback = product_default_placement(lane_class)
        return ResolvedPlacement(
            split=declared.split if declared.split is not None else fallback.split,
            order=declared.order if declared.order is not None else fallback.order,
            ratio=declared.ratio if declared.ratio is not None else fallback.ratio,
        )

    @classmethod
    def default(cls) -> "LanePlacementConfig":
        """The empty policy: no declaration on either axis.

        "Default" here names the DECLARATION state (nothing declared), not the launch
        geometry. It is deliberately not behavior-preserving since Redmine #14568: a launch
        reading this policy resolves every field through :meth:`resolve_effective` to
        :func:`product_default_placement`, so an undeclared workspace splits ``down`` — and
        (Redmine #14569) divides that split evenly, ``ratio: 0.5``.
        """
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "LanePlacementConfig":
        """Normalize a ``lane_placement`` sub-record into a typed policy (fail-closed).

        ``None`` or an empty mapping yields the empty policy (:meth:`default` — nothing
        DECLARED; the launch geometry it resolves to is the product default, not the
        pre-#14568 launch). A non-mapping record, an unknown top-level key (an unknown lane
        class), an unsupported version, an unknown class-object key, or an invalid
        ``split`` / ``order`` / ``ratio`` value raises
        :class:`LanePlacementError` (which ``RepoLocalConfig`` re-raises as
        ``RepoLocalConfigError``). The top-level *boundary-token* screen for the
        ``lane_placement`` key itself stays with the repo-local schema, which rejects a
        ``pane``-shaped key before ever reaching here.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise LanePlacementError(
                "lane placement config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        for key in record:
            if not isinstance(key, str) or key not in LANE_PLACEMENT_KEYS:
                raise LanePlacementError(
                    f"lane placement config record has unknown key {key!r}; allowed "
                    f"keys: {sorted(LANE_PLACEMENT_KEYS)}"
                )
        version = record.get("version", LANE_PLACEMENT_CONFIG_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise LanePlacementError(
                f"lane placement config record 'version' must be an integer, got {version!r}"
            )
        if version != LANE_PLACEMENT_CONFIG_VERSION:
            raise LanePlacementError(
                f"unsupported lane placement config record version {version!r}; this "
                f"build understands version {LANE_PLACEMENT_CONFIG_VERSION}"
            )
        # Split the two axes: the flat lane-CLASS keys (``default`` / ``sublane``)
        # stay in ``class_record`` (parsed exactly as pre-#13647), and the nested
        # ``by_lane_kind`` block is parsed by the lane-KIND sibling parser. Neither
        # axis's parser ever sees the other's keys.
        kind_record = record.get(LANE_PLACEMENT_BY_LANE_KIND_KEY)
        class_record = {
            key: value
            for key, value in record.items()
            if key not in ("version", LANE_PLACEMENT_BY_LANE_KIND_KEY)
        }
        placements = parse_lane_placement_record(
            class_record or None, source="lane placement config"
        )
        kind_placements = parse_lane_kind_placement_record(
            kind_record, source="lane placement config"
        )
        return cls(
            version=version, placements=placements, kind_placements=kind_placements
        )


__all__ = (
    "LANE_PLACEMENT_CONFIG_VERSION",
    "LANE_PLACEMENT_LANE_CLASSES",
    "LANE_PLACEMENT_LANE_KINDS",
    "LANE_PLACEMENT_BY_LANE_KIND_KEY",
    "LANE_PLACEMENT_PROVIDERS",
    "LANE_PLACEMENT_RATIO_MAX",
    "LANE_PLACEMENT_RATIO_MIN",
    "LANE_PLACEMENT_SPLIT_DIRECTIONS",
    "LANE_PLACEMENT_CLASS_KEYS",
    "LANE_PLACEMENT_KEYS",
    "PRODUCT_DEFAULT_PLACEMENTS",
    "LanePlacementConfig",
    "LanePlacementError",
    "ResolvedPlacement",
    "product_default_placement",
    "parse_lane_placement_record",
    "parse_lane_kind_placement_record",
    "validate_lane_placement",
    "validate_lane_kind_placement",
)
