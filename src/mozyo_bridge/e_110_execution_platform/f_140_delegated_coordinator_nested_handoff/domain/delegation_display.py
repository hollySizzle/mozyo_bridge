"""Delegated-coordinator-tree display projection over discovered targets (#12466).

Consumes the closed #12465 ``delegation_projection`` foundation to produce the
display-only ``KIND`` / ``DEPTH`` / ``PARENT`` breadcrumb that ``agents targets``
appends per candidate (Redmine #12466). It is strictly a projection: nothing
here feeds target resolution, the role resolver, ``--target-repo``, approval,
close, or send preflight. The design source of truth is
``vibes/docs/logics/delegated-coordinator-cockpit-display.md`` (`## cockpit 最小
metadata contract`, follow-up #2).

Boundaries:

- **Read model, no I/O.** Operates on already-discovered ``TargetCandidate``
  records and their ``@mozyo_lane_kind`` / ``@mozyo_delegation_parent`` projection
  cache; it touches no tmux / Redmine / clock.
- **Non-authoritative.** ``DelegationDisplay`` carries no routing / handoff /
  approval / close field, and the breadcrumb is added alongside the canonical
  ``TargetRecord`` projection (like attention, #11952), never folded into it.
- **Fail soft.** A missing kind fact, an off-contract ``lane_kind``, an
  intra-lane disagreement, or a tree the foundation rejects (unknown parent /
  cycle / depth > 2) degrades to a blank or diagnostic display row rather than
  raising or blocking the read-only table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

DELEGATION_STATUS_NONE = "none"  # no durable kind fact -> blank display columns
DELEGATION_STATUS_DERIVED = "derived"  # depth/root derived from the parent chain
DELEGATION_STATUS_DIAGNOSTIC = "diagnostic"  # broken/off-contract tree -> shown, untrusted


@dataclass(frozen=True)
class DelegationDisplay:
    """One target's delegated-coordinator-tree breadcrumb for the cockpit display.

    A display projection over the #12465 ``delegation_projection`` foundation,
    surfaced by ``agents targets`` (Redmine #12466). It is **never** a routing /
    handoff / approval / close key — the same non-authoritative contract the
    foundation pins. ``status`` records whether the breadcrumb was derived
    (``derived``), absent (``none`` -> blank columns), or could not be coherently
    derived (``diagnostic`` -> shown for the operator but explicitly untrusted,
    never silently treated as a healthy tree).
    """

    lane_kind: str
    delegation_depth: int | None
    delegation_parent: str
    delegation_root: str
    status: str

    def as_payload(self) -> dict:
        return {
            "lane_kind": self.lane_kind,
            "delegation_depth": self.delegation_depth,
            "delegation_parent": self.delegation_parent,
            "delegation_root": self.delegation_root,
            "status": self.status,
        }


_DELEGATION_NONE = DelegationDisplay(
    lane_kind="",
    delegation_depth=None,
    delegation_parent="",
    delegation_root="",
    status=DELEGATION_STATUS_NONE,
)


def _delegation_unit_id(candidate) -> str:
    """Stable per-lane unit pointer for a candidate's delegation projection.

    The delegation tree is per *lane* (a lane's codex gateway and claude worker
    share it), so both panes fold onto one ``<workspace_id>/<lane_id>`` pointer.
    This is also the format the ``@mozyo_delegation_parent`` projection cache
    must use to point at a parent lane so the foundation can walk the chain. It
    is a display pointer, never a send target.
    """
    return f"{candidate.workspace_id or ''}/{candidate.lane_id or ''}"


def derive_targets_delegation(candidates: Iterable) -> dict[str, DelegationDisplay]:
    """Derive each target's delegated-coordinator breadcrumb (Redmine #12466).

    Consumes the closed #12465 ``delegation_projection`` foundation: folds the
    candidates into per-lane delegation units (keyed by
    ``<workspace_id>/<lane_id>``), builds one ``DelegationSource`` per unit that
    carries a contract ``@mozyo_lane_kind`` fact, and runs
    ``derive_delegation_tree`` to compute ``delegation_depth`` /
    ``delegation_root`` from the durable parent chain. Returns
    ``{pane_id: DelegationDisplay}`` for every candidate.

    Display-only and fail-soft, per the #12466 dispatch:

    - A target with no ``@mozyo_lane_kind`` -> :data:`DELEGATION_STATUS_NONE`
      (blank columns); a missing delegation fact is empty display state, not
      route authority.
    - An off-contract ``lane_kind``, a disagreement between the panes of one
      lane, or a tree the foundation rejects (unknown parent / cycle /
      depth > 2) -> :data:`DELEGATION_STATUS_DIAGNOSTIC`: the contract kind is
      still shown so the operator sees the broken breadcrumb, but depth / root
      are withheld rather than half-derived. This never raises and never blocks
      the read-only table.

    The result feeds the additive KIND / DEPTH / PARENT display columns only; it
    is not part of the canonical ``TargetRecord`` routing projection.
    """
    from mozyo_bridge.domain.delegation_projection import (
        LANE_KINDS,
        DelegationProjectionError,
        DelegationSource,
        derive_delegation_tree,
    )

    # 1. Fold candidates into per-lane delegation units. A unit "claims" a
    #    lane_kind / delegation_parent from any pane that carries it; panes that
    #    disagree mark the unit conflicted (-> diagnostic, never half-trusted).
    unit_of: dict[str, str] = {}  # pane_id -> unit_id
    unit_kind: dict[str, str] = {}  # unit_id -> claimed lane_kind
    unit_parent: dict[str, str] = {}  # unit_id -> claimed delegation_parent
    conflicted: set[str] = set()
    for candidate in candidates:
        unit_id = _delegation_unit_id(candidate)
        unit_of[candidate.pane_id] = unit_id
        kind = (candidate.lane_kind or "").strip()
        parent = (candidate.delegation_parent or "").strip()
        if not kind:
            continue
        if unit_id not in unit_kind:
            unit_kind[unit_id] = kind
            unit_parent[unit_id] = parent
        elif unit_kind[unit_id] != kind or unit_parent[unit_id] != parent:
            conflicted.add(unit_id)

    # 2. Build foundation sources only for units with a contract lane_kind and no
    #    intra-lane conflict. Off-contract / conflicted units skip derivation but
    #    are still surfaced as diagnostic below.
    sources = [
        DelegationSource(
            unit_id=unit_id,
            lane_kind=kind,
            delegation_parent=unit_parent[unit_id] or None,
        )
        for unit_id, kind in unit_kind.items()
        if unit_id not in conflicted and kind in LANE_KINDS
    ]

    # 3. Derive depth / root from the durable parent chain. Fail soft: a tree the
    #    foundation rejects degrades its units to diagnostic (projections stays
    #    empty) rather than raising or blocking the table.
    projections: dict[str, object] = {}
    if sources:
        try:
            projections = derive_delegation_tree(sources)
        except DelegationProjectionError:
            projections = {}

    # 4. Resolve a DelegationDisplay per unit, then map it back to each pane_id.
    display_of_unit: dict[str, DelegationDisplay] = {}
    for unit_id, kind in unit_kind.items():
        proj = projections.get(unit_id)
        if proj is not None:
            display_of_unit[unit_id] = DelegationDisplay(
                lane_kind=proj.lane_kind,
                delegation_depth=proj.delegation_depth,
                delegation_parent=proj.delegation_parent or "",
                delegation_root=proj.delegation_root,
                status=DELEGATION_STATUS_DERIVED,
            )
        else:
            # Off-contract kind, conflicted lane, or a rejected/too-deep tree:
            # show the kind only when it is a contract value; withhold depth/root.
            display_of_unit[unit_id] = DelegationDisplay(
                lane_kind=kind if kind in LANE_KINDS else "",
                delegation_depth=None,
                delegation_parent=unit_parent.get(unit_id, "") or "",
                delegation_root="",
                status=DELEGATION_STATUS_DIAGNOSTIC,
            )

    return {
        pane_id: display_of_unit.get(unit_id, _DELEGATION_NONE)
        for pane_id, unit_id in unit_of.items()
    }


def delegation_cells(display: DelegationDisplay | None) -> tuple[str, str, str]:
    """Render one target's ``(KIND, DEPTH, PARENT)`` text cells (Redmine #12466).

    Blank (``-``) when there is no delegation fact; a diagnostic tree shows the
    kind with ``?`` depth so a broken breadcrumb is visible but never reads as a
    healthy derived depth.
    """
    if display is None:
        return ("-", "-", "-")
    kind = display.lane_kind or "-"
    if display.delegation_depth is not None:
        depth = str(display.delegation_depth)
    elif display.status == DELEGATION_STATUS_DIAGNOSTIC:
        depth = "?"  # broken/off-contract tree: shown but explicitly untrusted
    else:
        depth = "-"
    parent = display.delegation_parent or "-"
    return (kind, depth, parent)


__all__ = (
    "DELEGATION_STATUS_NONE",
    "DELEGATION_STATUS_DERIVED",
    "DELEGATION_STATUS_DIAGNOSTIC",
    "DelegationDisplay",
    "derive_targets_delegation",
    "delegation_cells",
)
