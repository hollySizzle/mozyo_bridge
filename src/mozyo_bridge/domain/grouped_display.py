"""Grouped cockpit sublane display view (Redmine #12255).

This module is the **display / render projection** a grouped cockpit UI reads to
draw the Project Group -> Unit -> (role) view. It is the slice that finally makes
the predecessors' read model and freshness view *renderable as one row set*: it
joins

- the #12264 grouped read model (:mod:`mozyo_bridge.domain.grouped_read_model`):
  *which* Project Group each Unit displays under, its desired/observed placement,
  and (now) its observed Codex / Claude role-pane presence; with
- the #12266 reload / freshness view
  (:mod:`mozyo_bridge.domain.grouped_reload_view`): the human-facing freshness
  label and the explicit ``reload_required`` flag at every level.

into a single :class:`GroupedDisplayView` whose rows carry exactly what the
acceptance criteria (Redmine #12255) ask a grouped cockpit to *show*:

- a **Project Group header** (:attr:`GroupDisplaySection.header_label` /
  :attr:`~GroupDisplaySection.source` / :attr:`~GroupDisplaySection.managed`);
- per Unit, a distinguishable **lane label** (:attr:`UnitDisplayRow.lane_label`),
  **issue label** (:attr:`~UnitDisplayRow.issue_label`), and **Codex / Claude
  role panes** (:attr:`~UnitDisplayRow.roles` / :attr:`~UnitDisplayRow.role_label`
  — role *names* only, never a pane id / target);
- and **stale / unknown / unmanaged** state kept visible: the row's degraded
  ``status`` / ``state_label`` / ``freshness_label`` / ``reload_required`` are
  surfaced rather than collapsed to "current", a default-grouped Unit reads as
  ``managed=False`` ("unmanaged"), and an empty / no-live-target group stays
  ``stale`` rather than dropped.

Boundary, kept enforced in code and pinned by tests
(``unit-target-model.md`` ``### Project Group projection`` /
``public-private-boundary.md`` ``Public Record Constraints`` /
``runtime-observability-boundary.md`` ``### Freshness / fail-safe semantics``):

- **Display only, never routing / approval / close / completion authority.** The
  view answers "how is the grouped cockpit drawn?", not "where do I send?".
  ``roles`` names the observed role *presence* (``codex`` / ``claude``), never a
  deliverable endpoint — no row carries a ``target`` / ``pane`` / ``route`` /
  ``send`` / ``approval`` / ``credential`` field. A grouped Unit action re-resolves
  its live Target through the action-time live preflight in
  :mod:`mozyo_bridge.application.cockpit_ui` (#12265), regardless of this view
  (:data:`GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE`).
- **Fail-safe freshness, never ``healthy`` from a degraded snapshot.** Freshness /
  ``reload_required`` are read straight from the #12266 reload view, whose facts
  derive from the read model's observation envelope; a stale / unreadable /
  contradicted / unobserved row never reads as current.
- **Public-safe labels only.** ``lane_label`` / ``issue_label`` /
  ``header_label`` are the read model's public-safe display labels; the view adds
  no private path / host / operator layout / color policy (those stay in a private
  consumer's config), keeping #12255's non-goal "private color / layout policy の
  OSS default 化" out of the OSS default.

The module is pure (dataclasses + derivation helpers) and imports only from the
domain layer, mirroring the object-to-object discipline of #12264 / #12266 (no
served endpoint / HTML page is wired here).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.domain.grouped_read_model import (
    GROUP_SOURCE_DESIRED,
    GroupedReadModel,
    ProjectGroupView,
    UnitView,
)
from mozyo_bridge.domain.grouped_reload_view import (
    GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE,
    GroupedReloadView,
    ReloadAffordance,
    UnitFreshnessView,
    build_grouped_reload_view,
)

#: The canonical display order for agent role-pane presence (the role vocabulary
#: is ``cockpit_layout.ROLES`` = codex, claude; kept as plain names here so the
#: display never depends on the live pane layout). Codex (owner-facing gateway)
#: is shown first, then Claude, then any other observed role name sorted, so a
#: Unit's role panes read in a stable order.
ROLE_DISPLAY_ORDER: "tuple[str, ...]" = ("codex", "claude")

#: Shown for a Unit row that has no observed live role pane (degraded / inactive),
#: so an empty role set reads as "nothing live", never as a routing blank.
NO_ROLES_LABEL: str = "—"

#: Public-safe human labels for the read-model Unit ``status`` vocabulary
#: (``grouped_read_model.UNIT_STATUS_*`` / ``presentation_grouping`` statuses).
#: Vocabulary tokens only (never a path / host / secret); the degraded labels read
#: as attention, never as current.
_STATE_LABELS: "dict[str, str]" = {
    "observed": "observed",
    "stale": "stale",
    "partial": "partial",
    "unreadable": "unreadable",
    "contradicted": "contradicted",
    "unknown": "unknown",
    "identity_conflict": "identity conflict",
    "desired_unit_missing": "desired unit missing",
}

#: The boundary statement the display view carries: drawing the grouped cockpit is
#: a display act only — it moves no workflow gate and authorizes no side effect
#: (those re-preflight live at action time).
GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE: str = (
    "Grouped sublane display is a render projection only: its Project Group "
    "headers, lane / issue labels, and Codex / Claude role-pane presence are "
    "display facts, not routing / approval / review / close / completion "
    "authority. Role presence carries role names only, never a deliverable pane; "
    "a grouped Unit action re-resolves its target through an action-time live "
    "preflight regardless of this view."
)


def _canonical_roles(roles: "tuple[str, ...]") -> "tuple[str, ...]":
    """Distinct, public-safe role names in canonical display order.

    Trims blanks and de-duplicates (case-folded for the comparison, original
    casing preserved on first sight), orders the known agent roles by
    :data:`ROLE_DISPLAY_ORDER`, then appends any other observed role name sorted —
    so the role-pane presence reads in a stable order regardless of the order the
    observation supplied them in. Carries role names only; never a pane id.
    """
    seen: "dict[str, str]" = {}
    for role in roles:
        if role is None:
            continue
        name = role.strip()
        if not name:
            continue
        key = name.lower()
        if key not in seen:
            seen[key] = name
    known = [seen[r] for r in ROLE_DISPLAY_ORDER if r in seen]
    extra = sorted(
        seen[key] for key in seen if key not in ROLE_DISPLAY_ORDER
    )
    return tuple(known + extra)


def _role_label(roles: "tuple[str, ...]") -> str:
    """A human-facing role-pane presence label (public-safe, names only)."""
    if not roles:
        return NO_ROLES_LABEL
    return ", ".join(roles)


def _state_label(status: str) -> str:
    """A public-safe human label for a Unit's read-model status."""
    return _STATE_LABELS.get(status, status)


@dataclass(frozen=True)
class UnitDisplayRow:
    """One rendered Unit row in the grouped cockpit display.

    Carries the display facts the acceptance criteria name — :attr:`lane_label`
    (the Unit's lane), :attr:`issue_label` (its public-safe display label), and
    :attr:`roles` / :attr:`role_label` (its observed Codex / Claude role-pane
    *presence*) — plus the degraded-state facts that must stay visible
    (:attr:`status` / :attr:`state_label` / :attr:`freshness_label` /
    :attr:`reload_required`). :attr:`managed` is False when the row is in a
    default / ungrouped bucket (no desired grouping config) so an unmanaged Unit is
    distinguishable, never hidden.

    Display only: it carries **no** ``target`` / ``pane`` / ``route`` / ``send`` /
    ``approval`` / ``credential`` field. ``roles`` is role-name presence, not a
    delivery endpoint; ``active`` is the observed liveness boolean.
    """

    unit_id: str
    lane_label: str
    issue_label: Optional[str]
    roles: "tuple[str, ...]"
    role_label: str
    status: str
    state_label: str
    freshness: str
    freshness_label: str
    observed_at: Optional[str]
    stale_reason: Optional[str]
    contradiction: Optional[str]
    active: bool
    hidden: bool
    pinned: bool
    managed: bool
    reload_required: bool
    diagnostic: Optional[str]

    def as_payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "lane_label": self.lane_label,
            "issue_label": self.issue_label,
            "roles": list(self.roles),
            "role_label": self.role_label,
            "status": self.status,
            "state_label": self.state_label,
            "freshness": self.freshness,
            "freshness_label": self.freshness_label,
            "observed_at": self.observed_at,
            "stale_reason": self.stale_reason,
            "contradiction": self.contradiction,
            "active": self.active,
            "hidden": self.hidden,
            "pinned": self.pinned,
            "managed": self.managed,
            "reload_required": self.reload_required,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class GroupDisplaySection:
    """A rendered Project Group section: a header plus its Unit rows.

    :attr:`header_label` is the public-safe group label the header shows;
    :attr:`source` is ``desired_presentation`` (a declared config group) or
    ``default`` (the behavior-preserving ungrouped bucket), and :attr:`managed`
    mirrors it (True only for a declared group) so an *unmanaged* default group is
    distinguishable rather than dressed up as configured. :attr:`stale` carries the
    read model's "no live Target among members" fact unchanged (an empty / dead
    group reads stale, never dropped); :attr:`reload_required` is True when any
    member row is not current. ``units`` are the visible rows, ``hidden_units`` the
    config-hidden rows (shown separately, never collapsed away).
    """

    group_id: Optional[str]
    header_label: Optional[str]
    source: str
    managed: bool
    stale: bool
    collapsed: bool
    position: Optional[int]
    reload_required: bool
    units: "tuple[UnitDisplayRow, ...]" = ()
    hidden_units: "tuple[UnitDisplayRow, ...]" = ()

    def all_units(self) -> "tuple[UnitDisplayRow, ...]":
        """Every member row (visible then hidden), display order preserved."""
        return self.units + self.hidden_units

    def as_payload(self) -> dict:
        return {
            "group_id": self.group_id,
            "header_label": self.header_label,
            "source": self.source,
            "managed": self.managed,
            "stale": self.stale,
            "collapsed": self.collapsed,
            "position": self.position,
            "reload_required": self.reload_required,
            "units": [unit.as_payload() for unit in self.units],
            "hidden_units": [unit.as_payload() for unit in self.hidden_units],
        }


@dataclass(frozen=True)
class GroupedDisplayView:
    """The grouped cockpit sublane display — the single object a UI renders from.

    A consumer draws, from this object: a whole-projection freshness line
    (:attr:`observed_at` / :attr:`freshness_label` / :attr:`display_state`), a
    reload indicator (:attr:`reload_required` / :attr:`needs_attention`) and the
    :attr:`reload` affordance, then each :class:`GroupDisplaySection` as a Project
    Group header with its :class:`UnitDisplayRow` rows. It is not workflow truth
    and not action permission (:data:`GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE`).
    """

    observed_at: Optional[str]
    freshness: str
    freshness_label: str
    display_state: str
    reload_required: bool
    needs_attention: bool
    reload: ReloadAffordance = ReloadAffordance()
    groups: "tuple[GroupDisplaySection, ...]" = ()
    diagnostics: "tuple[str, ...]" = ()

    def all_units(self) -> "tuple[UnitDisplayRow, ...]":
        units: "list[UnitDisplayRow]" = []
        for group in self.groups:
            units.extend(group.all_units())
        return tuple(units)

    def as_payload(self) -> dict:
        return {
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "freshness_label": self.freshness_label,
            "display_state": self.display_state,
            "reload_required": self.reload_required,
            "needs_attention": self.needs_attention,
            "reload": self.reload.as_payload(),
            "groups": [group.as_payload() for group in self.groups],
            "diagnostics": list(self.diagnostics),
            "boundary_note": GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE,
        }


def _unit_row(
    unit: UnitView,
    managed: bool,
    freshness_index: "dict[str, UnitFreshnessView]",
) -> UnitDisplayRow:
    """Render one read-model row to a display row.

    Lane / issue labels and role-pane presence come from the read model; the
    human freshness label and the explicit ``reload_required`` flag come from the
    #12266 reload view (looked up by the row's opaque ``unit_id``). If the reload
    view somehow lacks the row, it fails safe to the row's own derived
    ``needs_reload`` and freshness so the row never reads as current by omission.
    """
    fresh = freshness_index.get(unit.unit_id)
    if fresh is not None:
        freshness_label = fresh.freshness_label
        reload_required = fresh.reload_required
    else:
        freshness_label = unit.freshness
        reload_required = unit.needs_reload
    roles = _canonical_roles(unit.roles)
    return UnitDisplayRow(
        unit_id=unit.unit_id,
        lane_label=unit.lane_id,
        issue_label=unit.label,
        roles=roles,
        role_label=_role_label(roles),
        status=unit.status,
        state_label=_state_label(unit.status),
        freshness=unit.freshness,
        freshness_label=freshness_label,
        observed_at=unit.observed_at,
        stale_reason=unit.stale_reason,
        contradiction=unit.contradiction,
        active=unit.active,
        hidden=unit.hidden,
        pinned=unit.pinned,
        managed=managed,
        reload_required=reload_required,
        diagnostic=unit.diagnostic,
    )


def _group_section(
    group: ProjectGroupView,
    freshness_index: "dict[str, UnitFreshnessView]",
) -> GroupDisplaySection:
    """Render one read-model Project Group to a display section.

    ``managed`` is True only for a declared (``desired_presentation``) group, so a
    default / ungrouped bucket reads as unmanaged. Visible and hidden rows are kept
    in separate buckets (display order preserved); ``reload_required`` is True when
    any member row is not current, and ``stale`` carries the read model's
    no-live-target fact unchanged.
    """
    managed = group.source == GROUP_SOURCE_DESIRED
    units = tuple(_unit_row(unit, managed, freshness_index) for unit in group.units)
    hidden = tuple(
        _unit_row(unit, managed, freshness_index) for unit in group.hidden_units
    )
    reload_required = any(
        row.reload_required for row in units + hidden
    )
    return GroupDisplaySection(
        group_id=group.group_id,
        header_label=group.label,
        source=group.source,
        managed=managed,
        stale=group.stale,
        collapsed=group.collapsed,
        position=group.position,
        reload_required=reload_required,
        units=units,
        hidden_units=hidden,
    )


def build_grouped_display_view(
    model: GroupedReadModel,
    reload_view: "Optional[GroupedReloadView]" = None,
) -> GroupedDisplayView:
    """Build the grouped sublane display view from a grouped read model.

    Pure projection: joins the #12264 read model (group placement, lane / issue
    labels, Codex / Claude role-pane presence) with the #12266 reload / freshness
    view (the freshness labels and explicit ``reload_required`` flags), producing
    the single object a grouped cockpit renders. ``reload_view`` is derived from
    ``model`` when not supplied, so a caller can pass a shared one or let this
    build it.

    It adds no new freshness / routing *authority* — every freshness fact comes
    from the reload view (whose degraded snapshots never derive ``healthy``), and
    no row carries a ``target`` / ``pane`` / ``route`` / ``approval`` field. Stale /
    unknown / unmanaged state stays visible: degraded ``status`` / freshness and
    default-group (``managed=False``) rows are surfaced, never hidden.
    """
    reload = reload_view if reload_view is not None else build_grouped_reload_view(model)
    freshness_index: "dict[str, UnitFreshnessView]" = {
        unit.unit_id: unit for unit in reload.all_units()
    }
    groups = tuple(_group_section(group, freshness_index) for group in model.groups)
    return GroupedDisplayView(
        observed_at=reload.observed_at,
        freshness=reload.freshness,
        freshness_label=reload.freshness_label,
        display_state=reload.display_state,
        reload_required=reload.reload_required,
        needs_attention=reload.needs_attention,
        reload=reload.reload,
        groups=groups,
        diagnostics=model.diagnostics,
    )


__all__ = (
    "ROLE_DISPLAY_ORDER",
    "NO_ROLES_LABEL",
    "GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE",
    "UnitDisplayRow",
    "GroupDisplaySection",
    "GroupedDisplayView",
    "build_grouped_display_view",
)
