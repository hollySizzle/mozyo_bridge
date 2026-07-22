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
Design Answer j#76564): a ``lane_class -> {split, order}`` table that decides the herdr
pane pair's split *direction* (``right`` / ``down``) and the provider *order* (which
provider occupies first / left / top, which one splits beside it) per lane class. It is a
**future launch policy** only — it constructs the ``agent start`` argv a fresh launch / heal
uses; it is never a live layout / liveness / route authority, and never reconfigures an
already-live pair (Non-goal: no live relayout).

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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.shared.lane_kind import LANE_KINDS

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

#: The closed set of recognized keys inside a lane-class placement object. Both are
#: individually optional; a missing field inherits the legacy launch discipline for that
#: lane class (Design Answer j#76564 Q1 / Q3), so an empty ``{}`` object is a no-op.
LANE_PLACEMENT_CLASS_KEYS: frozenset[str] = frozenset({"split", "order"})

#: The closed lane-KIND vocabulary of the ``by_lane_kind`` block (Redmine #13647,
#: disposition j#85650 P3): the three canonical delegation-geometry tokens
#: ``coordinator`` (親) / ``delegated_coordinator`` (子) / ``implementation`` (孫).
#: The single source of truth is :data:`mozyo_bridge.shared.lane_kind.LANE_KINDS`;
#: there is deliberately no ``parent`` / ``child`` / ``grandchild`` config alias
#: (the machine vocabulary is exactly these three tokens).
LANE_PLACEMENT_LANE_KINDS: frozenset[str] = LANE_KINDS

#: The additive top-level ``lane_placement`` key that carries the lane-KIND placement
#: table (Redmine #13647). Deliberately a *separate* nested block from the flat
#: ``default`` / ``sublane`` lane-CLASS keys so the two axes never collide: the
#: lane-class axis (#13646) stays byte-for-byte, and the lane-kind axis is a finer
#: partition consulted at higher precedence (Design Answer j#85645).
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
    """The effective placement policy for one lane class (pure value).

    Both fields are ``None`` when the lane class inherits the legacy launch discipline —
    :attr:`split` ``None`` means "emit the lane class's historical split flag" (server
    default for ``default``, literal ``--split right`` for ``sublane``); :attr:`order`
    ``None`` means "keep the requested launch order". A configured value overrides only its
    own field, so a partial object (``split`` set, ``order`` absent) changes only the split
    direction (Design Answer j#76564 Q3). ``order`` is a full permutation of
    :data:`LANE_PLACEMENT_PROVIDERS` when set.
    """

    split: Optional[str] = None
    order: Optional[tuple[str, ...]] = None


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


def parse_lane_placement_record(
    record: object, *, source: str
) -> "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]]], ...]":
    """Normalize a ``lane_placement`` lane-class mapping into sorted, frozen triples.

    Shape: ``lane_class -> {split?, order?}`` where ``lane_class`` is a
    :data:`LANE_PLACEMENT_LANE_CLASSES` value. Returns a sorted tuple of
    ``(lane_class, split, order)`` triples (hashable, so the composing
    :class:`RepoLocalConfig` stays hashable), with ``split`` / ``order`` each ``None`` when
    absent. The ``version`` key is stripped by the composing
    ``LanePlacementConfig.from_record`` before this is called, so every remaining key must
    be a lane class. A non-mapping block, an unknown lane class, an unknown class-object
    key, a non-mapping class object, or an invalid ``split`` / ``order`` value fails closed.
    ``None`` yields ``()`` (no override, byte-for-byte historical).
    """
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise LanePlacementError(
            f"{source} 'lane_placement' must be a mapping of lane_class -> "
            f"{{split, order}}, got {type(record).__name__}"
        )
    triples: list = []
    for lane_class, obj in record.items():
        if not isinstance(lane_class, str) or lane_class not in LANE_PLACEMENT_LANE_CLASSES:
            raise LanePlacementError(
                f"{source} 'lane_placement' lane-class key must be one of "
                f"{sorted(LANE_PLACEMENT_LANE_CLASSES)}, got {lane_class!r}"
            )
        if not isinstance(obj, Mapping):
            raise LanePlacementError(
                f"{source} 'lane_placement.{lane_class}' must be a mapping with optional "
                f"'split' / 'order' keys, got {type(obj).__name__}"
            )
        for key in obj:
            if not isinstance(key, str) or key not in LANE_PLACEMENT_CLASS_KEYS:
                raise LanePlacementError(
                    f"{source} 'lane_placement.{lane_class}' has unknown key {key!r}; "
                    f"allowed keys: {sorted(LANE_PLACEMENT_CLASS_KEYS)}"
                )
        split = _normalize_split(obj.get("split"), lane_class=lane_class, source=source)
        order = _normalize_order(obj.get("order"), lane_class=lane_class, source=source)
        triples.append((lane_class, split, order))
    return tuple(sorted(triples))


def validate_lane_placement(
    placements: "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]]], ...]",
    *,
    source: str,
) -> None:
    """Fail closed on an invalid ``placements`` tuple (lane_class / split / order).

    Runs the full validation so a directly-constructed
    ``LanePlacementConfig(placements=...)`` is checked as thoroughly as one parsed from a
    record (mirrors :func:`...agent_launch_argv.validate_launch_argv`): every lane class is
    in vocabulary, no lane class appears twice, every ``split`` is a valid direction, and
    every ``order`` is a full permutation of :data:`LANE_PLACEMENT_PROVIDERS`.
    """
    seen: set = set()
    for lane_class, split, order in placements:
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


def parse_lane_kind_placement_record(
    record: object, *, source: str
) -> "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]]], ...]":
    """Normalize a ``by_lane_kind`` mapping into sorted, frozen triples (Redmine #13647).

    Shape: ``lane_kind -> {split?, order?}`` where ``lane_kind`` is a
    :data:`LANE_PLACEMENT_LANE_KINDS` token. Returns a sorted tuple of
    ``(lane_kind, split, order)`` triples (hashable), reusing the SAME per-field
    validators as the lane-class table (:func:`_normalize_split` /
    :func:`_normalize_order`), so the geometry vocabulary is identical on both
    axes. A non-mapping block, an unknown lane kind (e.g. a ``parent`` / ``child``
    alias — there is no such alias), an unknown class-object key, a non-mapping
    class object, or an invalid ``split`` / ``order`` value fails closed. ``None``
    yields ``()`` (no override, byte-for-byte historical).
    """
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise LanePlacementError(
            f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}' must be a "
            f"mapping of lane_kind -> {{split, order}}, got {type(record).__name__}"
        )
    triples: list = []
    for lane_kind, obj in record.items():
        if not isinstance(lane_kind, str) or lane_kind not in LANE_PLACEMENT_LANE_KINDS:
            raise LanePlacementError(
                f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}' lane-kind "
                f"key must be one of {sorted(LANE_PLACEMENT_LANE_KINDS)}, got {lane_kind!r}"
            )
        if not isinstance(obj, Mapping):
            raise LanePlacementError(
                f"{source} 'lane_placement.{LANE_PLACEMENT_BY_LANE_KIND_KEY}.{lane_kind}' "
                f"must be a mapping with optional 'split' / 'order' keys, got "
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
        triples.append((lane_kind, split, order))
    return tuple(sorted(triples))


def validate_lane_kind_placement(
    placements: "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]]], ...]",
    *,
    source: str,
) -> None:
    """Fail closed on an invalid ``by_lane_kind`` triple tuple (Redmine #13647).

    The lane-kind sibling of :func:`validate_lane_placement`, so a directly-built
    ``LanePlacementConfig(kind_placements=...)`` is checked as thoroughly as one
    parsed from a record: every lane kind is in vocabulary, no kind appears twice,
    every ``split`` is a valid direction, and every ``order`` is a full permutation
    of :data:`LANE_PLACEMENT_PROVIDERS`.
    """
    seen: set = set()
    for lane_kind, split, order in placements:
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

    - :attr:`placements` — a frozen, sorted tuple of ``(lane_class, split, order)`` triples
      parsed from the ``lane_placement: {lane_class: {split, order}}`` mapping. ``split`` is
      a :data:`LANE_PLACEMENT_SPLIT_DIRECTIONS` value or ``None`` (inherit legacy);
      ``order`` is a full permutation of :data:`LANE_PLACEMENT_PROVIDERS` or ``None`` (keep
      the requested launch order). Both are individually optional per lane class.

    Boundary, kept enforced in code (this is *launch geometry intent*, not authority):

    - **Future launch policy, never live layout.** It constructs the ``agent start`` argv a
      fresh launch / heal uses; it is never a live layout / liveness / route authority and
      never reconfigures an already-live pair (Non-goal: no live relayout — herdr rejects
      same-tab re-split).
    - **Geometry only.** ``split`` names a herdr split direction and ``order`` names launch
      providers; the block can never address a live pane / target / route, name an
      executable, or grant approval / close / send authority.
    - **Default-preserving.** No block ⇒ empty ⇒ no argv change, so a repo with no
      ``lane_placement`` block launches exactly as before (Design Answer j#76564 Q3).
    """

    version: int = LANE_PLACEMENT_CONFIG_VERSION
    placements: "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]]], ...]" = ()
    #: The lane-KIND placement table parsed from the ``by_lane_kind`` block (Redmine
    #: #13647). A frozen, sorted tuple of ``(lane_kind, split, order)`` triples,
    #: disjoint from :attr:`placements` (the lane-CLASS table). Empty when the block
    #: is absent, so a repo with no ``by_lane_kind`` block is byte-for-byte pre-#13647.
    kind_placements: "tuple[tuple[str, Optional[str], Optional[tuple[str, ...]]], ...]" = (
        field(default=())
    )

    def __post_init__(self) -> None:
        # Validate on construction too, so a directly-built LanePlacementConfig(...) is
        # checked as thoroughly as one parsed from a record (no dataclass back door).
        validate_lane_placement(self.placements, source="lane placement config")
        validate_lane_kind_placement(self.kind_placements, source="lane placement config")

    def resolve_by_lane_kind(self, lane_kind: str) -> ResolvedPlacement:
        """The effective :class:`ResolvedPlacement` for ``lane_kind``, or legacy (Redmine #13647).

        The lane-KIND lookup consulted at higher precedence than
        :meth:`resolve` (the lane-CLASS lookup). Returns the configured
        ``(split, order)`` for a present lane kind, else a legacy
        :class:`ResolvedPlacement` (both ``None``) so the caller falls through to
        the lane-class layer. A lane kind absent from ``by_lane_kind`` — or an empty
        table — resolves to legacy, so the fall-through is byte-for-byte.
        """
        for entry_kind, split, order in self.kind_placements:
            if entry_kind == lane_kind:
                return ResolvedPlacement(split=split, order=order)
        return ResolvedPlacement()

    def has_lane_kind(self, lane_kind: str) -> bool:
        """True iff ``by_lane_kind`` explicitly declares ``lane_kind`` (Redmine #13647).

        The precedence gate: the lane-kind layer is consulted ONLY when the config
        explicitly declares that kind, so an undeclared kind falls through to the
        lane-class layer (byte-invariant), never shadowing it with an empty override.
        """
        return any(entry_kind == lane_kind for entry_kind, _, _ in self.kind_placements)

    def resolve(self, lane_class: str) -> ResolvedPlacement:
        """The effective :class:`ResolvedPlacement` for ``lane_class`` (the single source).

        Returns the configured ``(split, order)`` for a present lane class, else a legacy
        :class:`ResolvedPlacement` (both ``None`` — inherit the historical launch
        discipline). ``prepare_session`` reads this once per run to decide the launch order
        and the per-slot split direction (Design Answer j#76564 Q4).
        """
        for entry_class, split, order in self.placements:
            if entry_class == lane_class:
                return ResolvedPlacement(split=split, order=order)
        return ResolvedPlacement()

    @classmethod
    def default(cls) -> "LanePlacementConfig":
        """The behavior-preserving default: no placement override."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "LanePlacementConfig":
        """Normalize a ``lane_placement`` sub-record into a typed policy (fail-closed).

        ``None`` or an empty mapping yields the behavior-preserving default. A non-mapping
        record, an unknown top-level key (an unknown lane class), an unsupported version, an
        unknown class-object key, or an invalid ``split`` / ``order`` value raises
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
    "LANE_PLACEMENT_SPLIT_DIRECTIONS",
    "LANE_PLACEMENT_CLASS_KEYS",
    "LANE_PLACEMENT_KEYS",
    "LanePlacementConfig",
    "LanePlacementError",
    "ResolvedPlacement",
    "parse_lane_placement_record",
    "parse_lane_kind_placement_record",
    "validate_lane_placement",
    "validate_lane_kind_placement",
)
