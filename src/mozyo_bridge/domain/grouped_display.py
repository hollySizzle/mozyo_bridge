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
  :attr:`~GroupDisplaySection.source` / :attr:`~GroupDisplaySection.managed`)
  carrying a projection-only **attention / freshness summary**
  (:attr:`~GroupDisplaySection.summary`, Redmine #12297): the header's
  active-lane / reload-required / attention-candidate counts so an operator can
  tell which group / Unit to look at first, with the whole-projection roll-up on
  :attr:`GroupedDisplayView.summary`;
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
    UNIT_STATUS_CONTRADICTED,
    GroupedReadModel,
    ProjectGroupView,
    UnitView,
)
from mozyo_bridge.domain.presentation_grouping import (
    DEFAULT_PROJECT_GROUP_PRESENTATION,
    STATUS_DESIRED_UNIT_MISSING,
    STATUS_IDENTITY_CONFLICT,
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

#: Read-model ``status`` values that make a Unit row an *attention candidate* in
#: the header summary: a config-vs-runtime contradiction the operator must
#: *resolve* (not a mere staleness a reload fixes). These are the
#: contradiction-class statuses — ``contradicted`` (live identity contradicts the
#: launch identity), ``identity_conflict``, ``desired_unit_missing``. They are
#: deliberately NOT the governance ``blocked`` state of
#: ``cockpit-attention-state.md`` (which is derived from the Redmine durable
#: record): this summary is a projection over the display rows only and never
#: reads governance truth, so it surfaces *attention candidates*, never an
#: authoritative blocked / owner-waiting / review-waiting verdict.
ATTENTION_CANDIDATE_STATUSES: "frozenset[str]" = frozenset(
    (UNIT_STATUS_CONTRADICTED, STATUS_IDENTITY_CONFLICT, STATUS_DESIRED_UNIT_MISSING)
)

#: The boundary statement the attention / freshness summary carries: it is a
#: projection over the display rows' observation facts, never a duplication of
#: Redmine governance truth and never an action permission.
GROUPED_SUMMARY_DIAGNOSTIC_ONLY_NOTE: str = (
    "Group header attention / freshness summary is a projection over the "
    "displayed rows only: its active-lane / reload-required / attention-candidate "
    "counts are derived from observed liveness and freshness, never from a Redmine "
    "journal body, owner approval, review state, or governance blocked truth (that "
    "stays with the durable record). It grants no routing / approval / review / "
    "close authority and helps an operator pick which group / Unit to look at "
    "first, nothing more."
)

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
class GroupAttentionSummary:
    """A projection-only attention / freshness summary for a header (Redmine #12297).

    The header-level roll-up an operator reads to decide *which group / Unit to
    look at first*, as three independent counts over a set of Unit rows:

    - :attr:`active_lanes` — rows with a live Target (observed liveness);
    - :attr:`reload_required` — rows whose snapshot is not current (stale /
      partial / unreadable / unknown / contradicted), i.e. a reload / live
      preflight is needed before trusting the row — taken straight from each
      row's already-tested ``reload_required`` flag;
    - :attr:`attention` — *attention candidates*: the contradiction-class subset
      (``contradicted`` / ``identity_conflict`` / ``desired_unit_missing``) the
      operator must *resolve* rather than merely reload.

    The counts are independent projections, not a partition: a Unit may be both
    active and reload-required, and every attention candidate is also
    reload-required (``attention`` narrows :attr:`reload_required` to the rows a
    reload will not fix). :attr:`total` is the member count they range over.

    **Projection only, never governance truth or action permission.** Every count
    is derived from a display fact already on the row (``active`` / observation
    freshness / read-model ``status``); the summary reads no Redmine journal body,
    owner approval, review state, or governance ``blocked`` truth — duplicating
    that into the UI is the explicit non-goal of #12297. It carries no
    ``target`` / ``pane`` / ``route`` / ``send`` / ``approval`` / ``credential``
    field and grants no routing / approval / review / close authority
    (:data:`GROUPED_SUMMARY_DIAGNOSTIC_ONLY_NOTE`).
    """

    total: int = 0
    active_lanes: int = 0
    reload_required: int = 0
    attention: int = 0

    @property
    def needs_attention(self) -> bool:
        """True when any member is reload-required or an attention candidate.

        A fail-safe single "look here" flag; it authorizes nothing.
        """
        return self.reload_required > 0 or self.attention > 0

    def as_payload(self) -> dict:
        return {
            "total": self.total,
            "active_lanes": self.active_lanes,
            "reload_required": self.reload_required,
            "attention": self.attention,
            "needs_attention": self.needs_attention,
        }


def _summarize(rows: "tuple[UnitDisplayRow, ...]") -> GroupAttentionSummary:
    """Roll member rows up into a projection-only attention / freshness summary.

    Counts over *every* member row passed in (callers include both visible and
    hidden rows) so a hidden-but-active or hidden-degraded Unit still registers.
    Each count is an independent projection of a fact already on the row —
    ``active`` (live Target), ``reload_required`` (snapshot not current), and a
    contradiction-class ``status`` (:data:`ATTENTION_CANDIDATE_STATUSES`) — never
    a re-read of governance truth.
    """
    return GroupAttentionSummary(
        total=len(rows),
        active_lanes=sum(1 for row in rows if row.active),
        reload_required=sum(1 for row in rows if row.reload_required),
        attention=sum(
            1 for row in rows if row.status in ATTENTION_CANDIDATE_STATUSES
        ),
    )


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

    :attr:`workspace_id` / :attr:`lane_id` / :attr:`host_id` are the Unit's
    public-safe *identity* facts (the same identity the read model's ``UnitView``
    carries), surfaced so a grouped cockpit action can seed the candidate-Unit
    selector (``cockpit_ui.candidate_unit_selector`` / the ``grouped-reveal`` /
    ``grouped-jump`` endpoints). They are an identity selector, **not** a routing
    target: the side effect still re-resolves them to a single live pane through
    the action-time live preflight (``cockpit_ui._resolve_unit_target``), which
    fails closed on a stale / ambiguous / non-local / non-default-lane candidate.
    """

    unit_id: str
    workspace_id: str
    lane_id: str
    host_id: str
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
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "host_id": self.host_id,
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

    :attr:`summary` is the projection-only attention / freshness roll-up the
    Project Group header shows (Redmine #12297) — active-lane / reload-required /
    attention-candidate counts over *all* member rows (visible and hidden) so an
    operator can tell from the header which group to look at first.
    """

    group_id: Optional[str]
    header_label: Optional[str]
    source: str
    managed: bool
    stale: bool
    collapsed: bool
    position: Optional[int]
    reload_required: bool
    summary: GroupAttentionSummary
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
            "summary": self.summary.as_payload(),
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

    :attr:`summary` is the whole-projection attention / freshness roll-up over
    every member Unit (Redmine #12297) — the same projection-only counts each
    :class:`GroupDisplaySection` carries, aggregated, so an operator can read the
    overall active-lane / reload-required / attention-candidate load at a glance.
    """

    observed_at: Optional[str]
    freshness: str
    freshness_label: str
    display_state: str
    reload_required: bool
    needs_attention: bool
    summary: GroupAttentionSummary = GroupAttentionSummary()
    reload: ReloadAffordance = ReloadAffordance()
    groups: "tuple[GroupDisplaySection, ...]" = ()
    diagnostics: "tuple[str, ...]" = ()
    #: The desired Project-Group display-placement mode (#12286), carried verbatim
    #: from the read model. Display-only metadata a cockpit may render (e.g. "this
    #: view requests project_group_tmux_window"); it requests a layout, never a
    #: routing target or a guaranteed window / tab.
    project_group_presentation: str = DEFAULT_PROJECT_GROUP_PRESENTATION

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
            "summary": self.summary.as_payload(),
            "reload": self.reload.as_payload(),
            "groups": [group.as_payload() for group in self.groups],
            "diagnostics": list(self.diagnostics),
            "project_group_presentation": self.project_group_presentation,
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
        workspace_id=unit.workspace_id,
        lane_id=unit.lane_id,
        host_id=unit.host_id,
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
    members = units + hidden
    reload_required = any(row.reload_required for row in members)
    return GroupDisplaySection(
        group_id=group.group_id,
        header_label=group.label,
        source=group.source,
        managed=managed,
        stale=group.stale,
        collapsed=group.collapsed,
        position=group.position,
        reload_required=reload_required,
        summary=_summarize(members),
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
    # Whole-projection roll-up: summarize every member row across all groups so
    # the overall active-lane / reload-required / attention-candidate load is
    # readable at the top, mirroring each group header's own summary.
    all_rows = tuple(row for group in groups for row in group.all_units())
    return GroupedDisplayView(
        observed_at=reload.observed_at,
        freshness=reload.freshness,
        freshness_label=reload.freshness_label,
        display_state=reload.display_state,
        reload_required=reload.reload_required,
        needs_attention=reload.needs_attention,
        summary=_summarize(all_rows),
        reload=reload.reload,
        groups=groups,
        diagnostics=model.diagnostics,
        project_group_presentation=model.project_group_presentation,
    )


__all__ = (
    "ROLE_DISPLAY_ORDER",
    "NO_ROLES_LABEL",
    "ATTENTION_CANDIDATE_STATUSES",
    "GROUPED_SUMMARY_DIAGNOSTIC_ONLY_NOTE",
    "GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE",
    "GroupAttentionSummary",
    "UnitDisplayRow",
    "GroupDisplaySection",
    "GroupedDisplayView",
    "build_grouped_display_view",
)
