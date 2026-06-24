"""Delegated-coordinator-tree projection metadata read model (Redmine #12465).

US #12454 / Task #12465. Pure projection layer: derive the delegation
breadcrumb metadata (``lane_kind`` / ``delegation_depth`` / ``delegation_parent``
/ ``delegation_root``) for a parent -> delegated coordinator -> grandchild lane
tree from already-extracted *durable* facts.

The design source of truth is
``vibes/docs/logics/delegated-coordinator-cockpit-display.md`` (`## delegation
reference metadata` / `## cockpit 最小 metadata contract`); this module implements
only the metadata foundation from that design's `## 実装分割` follow-up list, so
later tasks (#12466 ``agents targets`` / cockpit-status columns, #12467
separate-window presentation policy) consume a single derived shape.

Boundaries (pinned by tests in ``tests/test_delegation_projection.py``):

- **Read model, no I/O.** This module performs no Redmine / tmux / event-store
  reads. The caller extracts the input facts — the parent-child-grandchild
  relationship is governance truth read from the Redmine issue parent link + the
  dispatch journal, and the lane identity from the workspace/lane/role resolver —
  and passes them in as :class:`DelegationSource` records. Derivation is pure and
  clock-free.
- **Projection, never a routing key.** The derived metadata is a display / audit
  breadcrumb. ``delegation_parent`` / ``delegation_root`` are *pointers for
  tracing the tree in the cockpit*, never a send target: this module does not
  import or touch handoff routing, the target resolver, or pane-send preflight,
  and adds no routing / handoff / approval / close authority field. Cross-lane
  handoff stays bound to the live ``--target-repo`` preflight, exactly as before.
- **Re-derivable from durable anchors.** The same record must be reconstructable
  from the Redmine parent link + dispatch journal alone; a tmux ``@mozyo_*`` pane
  option or a window title is only a projection cache of this derivation and is
  never the source of truth. :func:`delegation_user_options` produces that cache
  mapping, but the *writer* that sets the options on a pane is a separate
  follow-up task (#12466) — this module stops at the pure mapping.
- **Fail closed.** An unknown parent pointer, a cycle, a depth beyond the
  shallow-delegation maximum, or an unknown ``lane_kind`` token raises
  :class:`DelegationProjectionError` rather than emitting a half-derived record
  that would let a broken tree look healthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

# --- lane_kind vocabulary ---------------------------------------------------
# The coarse *display* kind from the design doc's
# ``lane_kind: coordinator | delegated_coordinator | implementation`` contract.
# This is intentionally coarser than ``role_profile.py``'s four routing roles
# (it folds implementation_gateway + implementation_worker into the single
# display kind ``implementation``); it carries no routing authority.
LANE_KIND_COORDINATOR = "coordinator"
LANE_KIND_DELEGATED_COORDINATOR = "delegated_coordinator"
LANE_KIND_IMPLEMENTATION = "implementation"
# Explicit "could not determine" kind so a caller without a durable kind fact
# states that honestly rather than guessing; a typo'd token still fails closed.
LANE_KIND_UNKNOWN = "unknown"

LANE_KINDS = frozenset(
    {
        LANE_KIND_COORDINATOR,
        LANE_KIND_DELEGATED_COORDINATOR,
        LANE_KIND_IMPLEMENTATION,
        LANE_KIND_UNKNOWN,
    }
)

# Shallow-delegation depth cap: parent (0) -> delegated (1) -> grandchild (2).
# The design doc fixes 3 levels as the display / retire-contract default and
# deliberately does not define a 4+ level default, so a deeper chain fails
# closed here rather than silently projecting an unsupported depth.
MAX_DELEGATION_DEPTH = 2

# --- tmux user-option projection-cache names --------------------------------
# Names only. These mirror the design doc's `## delegation reference metadata`
# user options. They are a *projection cache*, never routing authority; the pure
# mapping is :func:`delegation_user_options` and the pane *writer* is #12466.
OPTION_LANE_KIND = "@mozyo_lane_kind"
OPTION_DELEGATION_ROOT = "@mozyo_delegation_root"
OPTION_DELEGATION_PARENT = "@mozyo_delegation_parent"
OPTION_DELEGATION_DEPTH = "@mozyo_delegation_depth"


class DelegationProjectionError(ValueError):
    """A delegation tree could not be derived (unknown parent / cycle / depth)."""


@dataclass(frozen=True)
class DelegationSource:
    """One lane's durable-anchored delegation facts.

    Derived by the caller from the Redmine issue parent link + dispatch journal
    (the governance truth of "who delegated to whom") and the lane/workspace/role
    identity — **not** from a pane option or window title. ``unit_id`` is an
    opaque identity pointer (``unit:<host>:<workspace_id>:<lane_id>`` by the
    cockpit convention, but this module never parses it). ``delegation_parent``
    is the ``unit_id`` of the *direct* parent lane, or ``None`` for the tree root
    (the top coordinator). ``source_refs`` carry only human-traceable anchors
    (e.g. ``redmine:#12465#journal-63763``) and no path / secret.
    """

    unit_id: str
    lane_kind: str = LANE_KIND_UNKNOWN
    delegation_parent: str | None = None
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegationProjection:
    """Derived delegation breadcrumb projection for one lane / unit.

    A read model. ``delegation_root`` / ``delegation_parent`` are display / audit
    pointers for tracing the tree; they are **not** routing keys and must not be
    used to select a handoff target. ``delegation_depth`` is ``0`` for the root,
    ``1`` for a delegated coordinator, ``2`` for a grandchild lane.
    """

    unit_id: str
    lane_kind: str
    delegation_parent: str | None
    delegation_root: str
    delegation_depth: int
    source_refs: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "lane_kind": self.lane_kind,
            "delegation_parent": self.delegation_parent,
            "delegation_root": self.delegation_root,
            "delegation_depth": self.delegation_depth,
            "source_refs": list(self.source_refs),
        }


def _validate_lane_kind(unit_id: str, lane_kind: str) -> None:
    if lane_kind not in LANE_KINDS:
        raise DelegationProjectionError(
            f"unknown lane_kind {lane_kind!r} for {unit_id!r}; "
            f"expected one of {sorted(LANE_KINDS)}"
        )


def _walk_to_root(
    unit_id: str,
    index: Mapping[str, DelegationSource],
) -> tuple[str, int]:
    """Return ``(root_unit_id, depth)`` by walking the parent chain.

    Fails closed on an unknown parent pointer (the tree is not fully
    re-derivable), a cycle, or a depth beyond :data:`MAX_DELEGATION_DEPTH`.
    """
    depth = 0
    current = index[unit_id]
    seen = {unit_id}
    while current.delegation_parent is not None:
        parent_id = current.delegation_parent
        if parent_id not in index:
            raise DelegationProjectionError(
                f"{unit_id!r} references unknown delegation_parent {parent_id!r}; "
                "the delegation tree is not fully re-derivable from the durable anchors"
            )
        if parent_id in seen:
            raise DelegationProjectionError(
                f"delegation cycle detected at {parent_id!r} while walking from {unit_id!r}"
            )
        seen.add(parent_id)
        depth += 1
        if depth > MAX_DELEGATION_DEPTH:
            raise DelegationProjectionError(
                f"delegation depth for {unit_id!r} exceeds shallow-delegation "
                f"maximum {MAX_DELEGATION_DEPTH} (parent -> delegated -> grandchild)"
            )
        current = index[parent_id]
    return current.unit_id, depth


def derive_delegation_tree(
    sources: Iterable[DelegationSource],
) -> dict[str, DelegationProjection]:
    """Derive the :class:`DelegationProjection` for every lane in ``sources``.

    Pure and deterministic: identical inputs always yield identical records, and
    no Redmine / tmux / event-store / clock access happens here. Each lane's
    ``delegation_root`` and ``delegation_depth`` are derived by walking its
    durable ``delegation_parent`` chain, so the whole projection is a function of
    the durable anchors alone. Fails closed (:class:`DelegationProjectionError`)
    on a duplicate ``unit_id``, an unknown ``lane_kind``, an unknown parent
    pointer, a cycle, or a depth beyond :data:`MAX_DELEGATION_DEPTH`.
    """
    index: dict[str, DelegationSource] = {}
    for source in sources:
        if source.unit_id in index:
            raise DelegationProjectionError(
                f"duplicate unit_id {source.unit_id!r} in delegation sources"
            )
        _validate_lane_kind(source.unit_id, source.lane_kind)
        index[source.unit_id] = source

    projections: dict[str, DelegationProjection] = {}
    for unit_id, source in index.items():
        root_id, depth = _walk_to_root(unit_id, index)
        projections[unit_id] = DelegationProjection(
            unit_id=unit_id,
            lane_kind=source.lane_kind,
            delegation_parent=source.delegation_parent,
            delegation_root=root_id,
            delegation_depth=depth,
            source_refs=tuple(source.source_refs),
        )
    return projections


def derive_delegation_projection(
    unit_id: str,
    sources: Iterable[DelegationSource],
) -> DelegationProjection:
    """Derive the projection for a single ``unit_id`` within ``sources``.

    Convenience over :func:`derive_delegation_tree` for the common "I have the
    tree, give me one lane's breadcrumb" case. Fails closed
    (:class:`DelegationProjectionError`) when ``unit_id`` is absent so a caller
    never silently treats a missing lane as the root.
    """
    tree = derive_delegation_tree(sources)
    projection = tree.get(unit_id)
    if projection is None:
        raise DelegationProjectionError(
            f"unit_id {unit_id!r} is not present in the delegation sources"
        )
    return projection


def delegation_user_options(projection: DelegationProjection) -> dict[str, str]:
    """Map a derived projection to its ``@mozyo_delegation_*`` cache values.

    A pure string mapping consumed by the projection-cache *writer* (#12466); it
    performs no tmux I/O. The values are a cache of the derivation — a downstream
    writer may set them on a pane for at-a-glance display, but they are never the
    source of truth and never a routing target. The root lane (no parent) maps
    ``OPTION_DELEGATION_PARENT`` to the empty string so the option is explicitly
    "no parent" rather than absent.
    """
    return {
        OPTION_LANE_KIND: projection.lane_kind,
        OPTION_DELEGATION_ROOT: projection.delegation_root,
        OPTION_DELEGATION_PARENT: projection.delegation_parent or "",
        OPTION_DELEGATION_DEPTH: str(projection.delegation_depth),
    }


__all__: Iterable[str] = (
    "LANE_KIND_COORDINATOR",
    "LANE_KIND_DELEGATED_COORDINATOR",
    "LANE_KIND_IMPLEMENTATION",
    "LANE_KIND_UNKNOWN",
    "LANE_KINDS",
    "MAX_DELEGATION_DEPTH",
    "OPTION_LANE_KIND",
    "OPTION_DELEGATION_ROOT",
    "OPTION_DELEGATION_PARENT",
    "OPTION_DELEGATION_DEPTH",
    "DelegationProjectionError",
    "DelegationSource",
    "DelegationProjection",
    "derive_delegation_tree",
    "derive_delegation_projection",
    "delegation_user_options",
)
