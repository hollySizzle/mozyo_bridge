"""Grouped cockpit read model as a home-state projection (Redmine #12264).

This module is the first code that *generates the grouped cockpit read model* —
the Project Group -> Unit view a cockpit UI reads — by composing three pure
inputs the predecessors fixed:

- the **repo-local desired presentation grouping config** and its launch-placement
  resolver (Redmine #12263, :mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping`):
  *which* Project Group a Unit is desired to display under;
- a **live observation envelope** per Unit and for the whole projection
  (Redmine #12224, :mod:`mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation`): *how fresh /
  readable / contradicted* the runtime observation behind each row is
  (``observed_at`` / ``freshness`` / ``stale_reason`` / ``contradiction``);
- **home-scoped runtime / projection inputs** (:class:`ObservedUnit`): the
  public-safe identity facts of each observed Unit plus whether it currently has
  a live Target.

The projection model it serves is fixed by
``vibes/docs/logics/unit-target-model.md`` (``### Project Group projection`` /
``ProjectGroupRecord``) and the read-model freshness / contradiction rules by
``vibes/docs/logics/unit-presentation-state-db.md`` (``desired presentation
config`` boundary) and ``vibes/docs/logics/runtime-observability-boundary.md``.

Boundary, kept enforced in code (the read model is a **projection only**):

- **Projection, never action permission.** The read model maps config + observed
  Units + observation envelopes to a *display* grouping. It resolves no handoff
  Target, asserts no liveness as truth, and grants no owner approval / review /
  close / routing authority. The output dataclasses deliberately carry **no**
  ``target`` / ``pane`` / ``route`` / ``send`` / ``approval`` field — a side
  effect's permission stays with the side-effecting command's action-time live
  preflight (``runtime-observability-boundary.md`` ``## Action-Time Live Preflight
  Boundary``). The naming (``*View`` / ``*ReadModel``) signals this: these are
  projection views, distinct from the canonical ``TargetRecord`` / ``UnitRecord``
  that carry a live delivery endpoint.
- **Visible degraded state, never silent correction.** When config and runtime
  observation disagree the row keeps a *visible* status — ``identity_conflict``
  (live identity contradicts the launch identity), ``desired_unit_missing`` (an
  override names a Unit not in the observed set), ``stale`` / ``unreadable`` /
  ``contradicted`` (the observation envelope is degraded) — rather than rerouting
  or hiding the drift (``unit-presentation-state-db.md`` fallback matrix).
- **Fail-safe freshness, never ``healthy`` from a degraded snapshot.** Freshness
  is derived through :mod:`runtime_observation`, whose ``display_state`` is never
  ``healthy`` for a stale / unreadable / contradictory snapshot. A snapshot may be
  *shown* (its ``freshness`` label is visible) but never reads as current /
  action-allowed.
- **Hidden (desired) and active (observed) stay separate.** ``hidden`` is a
  desired display preference resolved from config; ``active`` is an observed
  runtime fact (the Unit has a live Target). A hidden Unit with a live Target is
  shown in a separate hidden bucket with ``active=True`` — never dropped, killed,
  detached, or rerouted (fallback matrix: "display hidden preference and live
  availability separately").
- **Behavior-preserving default.** With no grouping config each Unit falls into a
  default group keyed on its public repo / workspace label, so distinct projects
  stay in distinct labeled default groups — the current behavior.

On-disk config loading (``.mozyo-bridge/config.yaml``) and DB current-table
migration are deliberately **out of scope** here: this slice consumes config /
observation *objects* and stays a pure projection, mirroring the staged
discipline of #12263 (schema -> resolver -> read model). The ``presentation:``
namespace shape for the on-disk loader (surface selection vs grouping, the open
design note carried from #12263) is now fixed in ``unit-presentation-state-db.md``
(``config namespace / path / ownership``): grouping shares the single
``presentation:`` namespace with the #12189 surface selection
(``presentation.surface`` selection, ``presentation.project_groups`` /
``presentation.grouping`` grouping, a shared ``presentation.version``), and the
on-disk loader split was wired by #12286. This object-to-object slice still only
consumes config objects and asserts no namespace shape.

The module is pure (dataclasses + derivation helpers) and imports only from the
domain layer, so the dependency only ever points within the domain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (
    DEFAULT_LANE,
    DEFAULT_PROJECT_GROUP_PRESENTATION,
    STATUS_DESIRED_UNIT_MISSING,
    STATUS_IDENTITY_CONFLICT,
    GroupPlacement,
    LaunchContext,
    PresentationGroupingConfig,
    ProjectGroup,
    UnitOverride,
    resolve_launch_placement,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (
    DISPLAY_STATE_UNKNOWN,
    FRESHNESS_EXPIRED,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    METHOD_PROJECTION_READ,
    READABILITY_PARTIAL,
    READABILITY_READABLE,
    READABILITY_UNREADABLE,
    SOURCE_CACHE,
    STALE_REASON_MISSING_SOURCE,
    STRENGTH_PROJECTION_ONLY,
    RuntimeObservationSnapshot,
)

#: Unit read-model presentation status values. ``observed`` is the ordinary
#: fresh / readable / placed outcome; the rest are *visible* degraded conditions
#: that never silently reroute. ``identity_conflict`` / ``desired_unit_missing``
#: are re-used verbatim from :mod:`presentation_grouping` so the grouping config
#: layer and the read-model layer speak one status vocabulary.
UNIT_STATUS_OBSERVED: str = "observed"
UNIT_STATUS_STALE: str = "stale"
UNIT_STATUS_PARTIAL: str = "partial"
UNIT_STATUS_UNREADABLE: str = "unreadable"
UNIT_STATUS_CONTRADICTED: str = "contradicted"
UNIT_STATUS_UNKNOWN: str = "unknown"

#: ``ProjectGroupRecord.source`` values (``unit-target-model.md``). A group built
#: from a declared config Project Group is ``desired_presentation``; the
#: behavior-preserving ungrouped bucket is ``default``.
GROUP_SOURCE_DESIRED: str = "desired_presentation"
GROUP_SOURCE_DEFAULT: str = "default"

#: The boundary statement the read model carries. Building / showing the grouped
#: read model is a display projection only; it moves no workflow gate and
#: authorizes no side-effecting action.
GROUPED_READ_MODEL_DIAGNOSTIC_ONLY_NOTE: str = (
    "Grouped read model is a display projection only: it does not resolve a "
    "handoff target, assert liveness as truth, or grant approval / review / "
    "close / routing authority. A side effect's permission stays with the "
    "side-effecting command's action-time live preflight."
)

#: The observation envelope for a Unit that was never observed (no runtime /
#: projection snapshot supplied). Deterministic and time-independent: a missing
#: ``observed_at`` derives ``freshness=unknown`` / ``display_state=unknown`` /
#: ``stale_reason=missing_source`` (the fail-safe semantics of #12224), so an
#: unobserved Unit never reads as fresh.
UNKNOWN_OBSERVATION: RuntimeObservationSnapshot = RuntimeObservationSnapshot(
    observed_at=None,
    source=SOURCE_CACHE,
    method=METHOD_PROJECTION_READ,
    freshness=FRESHNESS_UNKNOWN,
    readability=READABILITY_READABLE,
    strength=STRENGTH_PROJECTION_ONLY,
    stale_reason=STALE_REASON_MISSING_SOURCE,
    contradiction=None,
    display_state=DISPLAY_STATE_UNKNOWN,
)


def _unit_status_from_observation(
    observation: RuntimeObservationSnapshot,
) -> str:
    """Derive a Unit's visible read-model status from its observation envelope.

    Precedence is most-severe-first (the attention-derivation posture: a stronger
    degraded signal beats a weaker one). A degraded observation never resolves to
    ``observed``; ``observed`` requires a fully readable, fresh, uncontradicted
    snapshot — equivalently, ``not observation.needs_reload``. A ``partial``
    readability or a ``reload_required`` display_state is fail-closed to a visible
    degraded status (never ``observed``), matching ``runtime_observation`` and the
    fail-safe semantics of ``runtime-observability-boundary.md``. Identity conflict
    and desired-unit-missing are decided by the caller (they are config-vs-runtime
    facts, not observation-quality facts) and take precedence over anything derived
    here.
    """
    if observation.contradiction is not None:
        return UNIT_STATUS_CONTRADICTED
    if observation.readability == READABILITY_UNREADABLE:
        return UNIT_STATUS_UNREADABLE
    if observation.readability == READABILITY_PARTIAL:
        return UNIT_STATUS_PARTIAL
    if observation.freshness in (FRESHNESS_STALE, FRESHNESS_EXPIRED):
        return UNIT_STATUS_STALE
    if (
        observation.freshness == FRESHNESS_UNKNOWN
        or observation.display_state == DISPLAY_STATE_UNKNOWN
    ):
        return UNIT_STATUS_UNKNOWN
    # Final fail-safe: any snapshot still flagged for reload (e.g. a
    # reload_required display_state with otherwise-fresh fields) is never
    # ``observed``.
    if observation.needs_reload:
        return UNIT_STATUS_STALE
    return UNIT_STATUS_OBSERVED


@dataclass(frozen=True)
class ObservedUnit:
    """A home-scoped runtime / projection observation of one Unit (read-model input).

    The identity facts are the public-safe ones a launch context is keyed on
    (``unit-presentation-state-db.md`` "Runtime / registry から導出する値").
    ``observed_workspace_id`` / ``observed_lane_id`` are *optional* live
    observations that, when they contradict the launch identity, surface a visible
    ``identity_conflict``. ``active`` is the observed liveness fact (a live Target
    exists for this Unit) — a runtime observation, kept separate from the desired
    ``hidden`` preference that the config resolves. ``roles`` is the observed set of
    *agent role names* (``codex`` / ``claude``) that currently have a live pane for
    this Unit — a display-safe presence refinement of ``active`` (``active`` =
    "at least one live Target"; ``roles`` = "which roles are live"). It carries
    **role names only**, never a pane id / session / target, so a consumer can show
    "this Unit has a Codex and a Claude pane" without the row becoming a routing
    endpoint (``unit-target-model.md`` Project Group -> Unit -> Target is a *display*
    hierarchy; the pane id stays routing authority resolved live at action time).
    ``observation`` is the per-Unit freshness envelope (the latest
    target-observation cache); when omitted the Unit reads as never-observed
    (:data:`UNKNOWN_OBSERVATION`).
    """

    workspace_id: str
    lane_id: str = DEFAULT_LANE
    host_id: str = "local"
    repo_label: Optional[str] = None
    project_id: Optional[str] = None
    fixed_version_id: Optional[str] = None
    observed_workspace_id: Optional[str] = None
    observed_lane_id: Optional[str] = None
    active: bool = False
    roles: "tuple[str, ...]" = ()
    observation: RuntimeObservationSnapshot = UNKNOWN_OBSERVATION

    def unit_id(self) -> str:
        """The portable Unit key (``unit-target-model.md`` UnitRecord shape)."""
        return f"unit:{self.host_id}:{self.workspace_id}:{self.lane_id}"

    def launch_context(self) -> LaunchContext:
        """The :class:`LaunchContext` this Unit is placed by (#12263 resolver input)."""
        return LaunchContext(
            workspace_id=self.workspace_id,
            lane_id=self.lane_id,
            host_id=self.host_id,
            repo_label=self.repo_label,
            project_id=self.project_id,
            fixed_version_id=self.fixed_version_id,
            observed_workspace_id=self.observed_workspace_id,
            observed_lane_id=self.observed_lane_id,
        )


@dataclass(frozen=True)
class UnitView:
    """One read-model row: desired placement + observed liveness + freshness.

    Display-only. ``hidden`` is the *desired* preference (config); ``active`` is
    the *observed* runtime fact (a live Target exists). They are independent: a
    hidden Unit may be active, and the read model surfaces both rather than
    collapsing them. The freshness fields (``observed_at`` / ``freshness`` /
    ``stale_reason`` / ``contradiction``) are carried verbatim from the observation
    envelope so a consumer can label staleness; the convenience
    :attr:`needs_reload` is *derived* from ``status`` (not a carried field).

    The carried fields and the JSON payload deliberately contain **no** routing /
    authority vocabulary (``target`` / ``pane`` / ``route`` / ``send`` /
    ``approval`` / ``credential`` …) — a side effect's permission is not on this
    row. ``active`` names the observed liveness boolean, not a delivery endpoint;
    ``roles`` names the observed agent-role *presence* (role names only, no pane id
    / target), so the display can distinguish a Unit's Codex / Claude panes without
    carrying a deliverable endpoint.
    """

    unit_id: str
    workspace_id: str
    lane_id: str
    host_id: str
    label: Optional[str]
    group_id: Optional[str]
    status: str
    position: Optional[int] = None
    pinned: bool = False
    hidden: bool = False
    active: bool = False
    roles: "tuple[str, ...]" = ()
    preferred_projection: Optional[str] = None
    observed_at: Optional[str] = None
    freshness: str = FRESHNESS_UNKNOWN
    stale_reason: Optional[str] = None
    contradiction: Optional[str] = None
    diagnostic: Optional[str] = None

    @property
    def needs_reload(self) -> bool:
        """True unless the row is a fresh, readable, uncontradicted, placed Unit.

        Derived (not carried) so the row's stored data never names a reload /
        loading verb; a consumer must reload / live-preflight before trusting any
        non-``observed`` row as current. It never authorizes a side effect.
        """
        return self.status != UNIT_STATUS_OBSERVED

    def as_payload(self) -> dict:
        """A JSON-safe projection of this row (display facts only)."""
        return {
            "unit_id": self.unit_id,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "host_id": self.host_id,
            "label": self.label,
            "group_id": self.group_id,
            "status": self.status,
            "position": self.position,
            "pinned": self.pinned,
            "hidden": self.hidden,
            "active": self.active,
            "roles": list(self.roles),
            "preferred_projection": self.preferred_projection,
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "stale_reason": self.stale_reason,
            "contradiction": self.contradiction,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class ProjectGroupView:
    """A Project Group row: a public-safe display grouping of Unit views.

    Mirrors ``unit-target-model.md`` ``ProjectGroupRecord`` (``group_id`` /
    ``label`` / ``source`` / ``units`` / display ``position`` / ``collapsed`` /
    ``stale``). ``units`` holds the visible Units and ``hidden_units`` the Units a
    config preference marks hidden — kept in a separate bucket so a hidden-but-
    active Unit is displayed apart rather than dropped. ``stale`` is True when the
    group has no live Target among its members (fallback matrix: "group has no
    live targets -> display empty/stale group; do not fabricate targets"), so an
    empty declared group is shown stale rather than invented away.
    """

    group_id: Optional[str]
    label: Optional[str]
    source: str
    units: "tuple[UnitView, ...]" = ()
    hidden_units: "tuple[UnitView, ...]" = ()
    position: Optional[int] = None
    collapsed: bool = False
    stale: bool = False

    def all_units(self) -> "tuple[UnitView, ...]":
        """Every member Unit (visible then hidden), display order preserved."""
        return self.units + self.hidden_units

    def as_payload(self) -> dict:
        return {
            "group_id": self.group_id,
            "label": self.label,
            "source": self.source,
            "position": self.position,
            "collapsed": self.collapsed,
            "stale": self.stale,
            "units": [unit.as_payload() for unit in self.units],
            "hidden_units": [unit.as_payload() for unit in self.hidden_units],
        }


@dataclass(frozen=True)
class GroupedReadModel:
    """The grouped cockpit read model — a home-state projection.

    A timestamped display projection: ``groups`` are ordered Project Group views,
    ``observation`` is the whole-projection freshness envelope (when it was last
    refreshed), and ``diagnostics`` carries human-facing degraded notes. It is not
    workflow truth and not action permission (see
    :data:`GROUPED_READ_MODEL_DIAGNOSTIC_ONLY_NOTE`).
    """

    groups: "tuple[ProjectGroupView, ...]" = ()
    observation: RuntimeObservationSnapshot = UNKNOWN_OBSERVATION
    diagnostics: "tuple[str, ...]" = ()
    #: The desired Project-Group display-placement mode resolved from config
    #: (#12286). Display-only metadata a cockpit surfaces (``same_cockpit_column``
    #: default / ``project_group_tmux_window`` / ``normal_window``); it requests a
    #: layout, never a routing target or a guaranteed window / tab.
    project_group_presentation: str = DEFAULT_PROJECT_GROUP_PRESENTATION

    @property
    def needs_reload(self) -> bool:
        """True when the whole-projection snapshot is fail-closed (not current).

        A consumer must treat the projection as needing a reload / live preflight
        before trusting it as current; it never authorizes a side effect either
        way.
        """
        return self.observation.needs_reload

    def all_units(self) -> "tuple[UnitView, ...]":
        units: list[UnitView] = []
        for group in self.groups:
            units.extend(group.all_units())
        return tuple(units)

    def as_payload(self) -> dict:
        return {
            "groups": [group.as_payload() for group in self.groups],
            "observation": self.observation.as_payload(),
            "diagnostics": list(self.diagnostics),
            "project_group_presentation": self.project_group_presentation,
            "boundary_note": GROUPED_READ_MODEL_DIAGNOSTIC_ONLY_NOTE,
        }


def _unit_view_from_observed(
    observed: ObservedUnit, placement: GroupPlacement
) -> UnitView:
    """Build a read-model row from an observed Unit and its resolved placement.

    ``status`` precedence: an ``identity_conflict`` placement (live identity
    contradicts the launch identity) is the strongest visible condition; otherwise
    the status is derived from the observation envelope. ``active`` is the observed
    liveness fact, independent of the desired ``hidden`` preference.
    """
    observation = observed.observation
    if placement.status == STATUS_IDENTITY_CONFLICT:
        status = STATUS_IDENTITY_CONFLICT
    else:
        status = _unit_status_from_observation(observation)
    return UnitView(
        unit_id=observed.unit_id(),
        workspace_id=observed.workspace_id,
        lane_id=observed.lane_id,
        host_id=observed.host_id,
        label=placement.label,
        group_id=placement.group_id,
        status=status,
        position=placement.position,
        pinned=placement.pinned,
        hidden=placement.hidden,
        active=observed.active,
        roles=observed.roles,
        preferred_projection=placement.preferred_projection,
        observed_at=observation.observed_at,
        freshness=observation.freshness,
        stale_reason=observation.stale_reason,
        contradiction=observation.contradiction,
        diagnostic=placement.diagnostic,
    )


def _unit_view_from_missing_override(
    override: UnitOverride, group: Optional[ProjectGroup]
) -> UnitView:
    """Build a ``desired_unit_missing`` row for an override with no observed Unit.

    The config wants this Unit displayed but the runtime observation set does not
    contain it — a visible config/runtime contradiction (fallback matrix:
    "unit override references unknown workspace/lane -> degraded display
    ``desired_unit_missing``"). It is shown in its desired group, inactive and
    unobserved, never invented as live.
    """
    label = override.label_override or (group.label if group is not None else None)
    return UnitView(
        unit_id=f"unit:{override.host_id or 'local'}:{override.workspace_id}:{override.lane_id}",
        workspace_id=override.workspace_id,
        lane_id=override.lane_id,
        host_id=override.host_id or "local",
        label=label,
        group_id=override.preferred_group,
        status=STATUS_DESIRED_UNIT_MISSING,
        position=override.position,
        pinned=bool(override.pinned) if override.pinned is not None else False,
        hidden=bool(override.hidden) if override.hidden is not None else False,
        active=False,
        preferred_projection=override.preferred_projection,
        observed_at=None,
        freshness=FRESHNESS_UNKNOWN,
        stale_reason=STALE_REASON_MISSING_SOURCE,
        contradiction=None,
        diagnostic="desired unit is not in the observed runtime set",
    )


def _group_sort_key(
    group: Optional[ProjectGroup], index: int
) -> "tuple[int, int, str, int]":
    """A total order for declared groups: int sort_key, then str, then declared order.

    Uses fixed-type tuple slots so an int and a string ``sort_key`` never compare
    against each other (which would raise); declaration ``index`` is the stable
    tiebreaker.
    """
    if group is None:
        return (2, 0, "", index)
    sort_key = group.sort_key
    if isinstance(sort_key, bool):  # bool is an int subclass; treat as "no key"
        return (1, 0, "", index)
    if isinstance(sort_key, int):
        return (0, sort_key, "", index)
    if isinstance(sort_key, str):
        return (1, 0, sort_key, index)
    return (1, 0, "", index)


def _unit_order_key(unit: UnitView) -> "tuple[int, int, str]":
    """Order Units in a group by ``position`` (set first, ascending) then unit id."""
    if unit.position is None:
        return (1, 0, unit.unit_id)
    return (0, unit.position, unit.unit_id)


def build_grouped_read_model(
    config: "Optional[PresentationGroupingConfig]",
    observed_units: "Sequence[ObservedUnit]",
    *,
    observation: "Optional[RuntimeObservationSnapshot]" = None,
) -> GroupedReadModel:
    """Generate the grouped cockpit read model from desired config + observations.

    Composes, per Unit, the #12263 launch-placement resolution (desired Project
    Group) with the #12224 observation envelope (freshness / contradiction), then
    buckets the resulting rows into Project Group views.

    - ``config`` ``None`` / empty -> the behavior-preserving default: each Unit
      lands in a default group keyed on its public repo / workspace label, so
      distinct projects / sublanes stay in distinct (labeled) default groups.
    - A config override that names a Unit not in ``observed_units`` becomes a
      visible ``desired_unit_missing`` row in its desired group (config/runtime
      contradiction), never a fabricated live Unit.
    - Declared Project Groups always appear (in ``sort_key`` then declared order),
      even when empty; an empty group, or one with no live Target, is shown
      ``stale`` rather than dropped.
    - ``observation`` is the whole-projection freshness envelope; omitted, the
      projection reads as never-refreshed (:data:`UNKNOWN_OBSERVATION`).

    The result is a projection: no row carries a routing Target, and no field
    grants a side-effect permission.
    """
    config = config or PresentationGroupingConfig.default()
    overall_observation = observation if observation is not None else UNKNOWN_OBSERVATION

    # Resolve every observed Unit to a desired placement + freshness row.
    rows: list[UnitView] = []
    for observed in observed_units:
        placement = resolve_launch_placement(config, observed.launch_context())
        rows.append(_unit_view_from_observed(observed, placement))

    # Surface config overrides that select no observed Unit as visible
    # desired_unit_missing rows (config/runtime contradiction). Detection uses the
    # override's own host-aware selector (``UnitOverride.selects`` — the same
    # selector ``resolve_launch_placement`` applies), so it is the exact
    # complement of the present-override path and a host-specific override
    # (``host_id`` set) is NOT masked by another host's same workspace/lane. A
    # host-unspecified override stays an any-host selector. This deliberately
    # supersedes the coarser ``presentation_grouping.diagnose_unit_overrides``
    # (workspace/lane-keyed), which is blind to ``host_id``.
    contexts = [observed.launch_context() for observed in observed_units]
    diagnostics: list[str] = []
    for override in config.unit_overrides:
        if any(override.selects(context) for context in contexts):
            continue
        group = config.group(override.preferred_group)
        rows.append(_unit_view_from_missing_override(override, group))
        host_note = f", host {override.host_id}" if override.host_id else ""
        diagnostics.append(
            f"desired_unit_missing: override for "
            f"({override.workspace_id}, {override.lane_id}{host_note}) "
            f"has no observed Unit"
        )

    # Bucket rows by group_id. ``None`` is the ungrouped / default bucket.
    rows_by_group: dict[Optional[str], list[UnitView]] = {}
    for row in rows:
        rows_by_group.setdefault(row.group_id, []).append(row)

    groups: list[ProjectGroupView] = []

    # Declared Project Groups first, in sort order, even when empty.
    ordered_declared = sorted(
        enumerate(config.project_groups),
        key=lambda pair: _group_sort_key(pair[1], pair[0]),
    )
    declared_ids: set[str] = set()
    for _index, group in ordered_declared:
        declared_ids.add(group.group_id)
        members = rows_by_group.get(group.group_id, [])
        groups.append(
            _build_group_view(
                group_id=group.group_id,
                label=group.label,
                source=GROUP_SOURCE_DESIRED,
                members=members,
                position=group.sort_key if isinstance(group.sort_key, int)
                and not isinstance(group.sort_key, bool) else None,
                collapsed=_group_collapsed(group, config),
            )
        )

    # The default / ungrouped rows (group_id None) are the behavior-preserving
    # default: bucket them by their public-safe repo / workspace label so distinct
    # projects / sublanes form distinct, labeled default groups rather than one
    # mixed unlabeled bucket (#12264: the cockpit reads a grouped view; the
    # fallback matrix keys the config-absent default on repo / workspace label).
    # group_id stays ``None`` because no config declared these groups; the group's
    # display identity is its label.
    default_collapsed = (
        bool(config.defaults.collapsed)
        if config.defaults.collapsed is not None
        else False
    )
    default_buckets: dict[Optional[str], list[UnitView]] = {}
    for row in rows_by_group.get(None, []):
        default_buckets.setdefault(row.label, []).append(row)
    for label in sorted(default_buckets, key=lambda text: (text is None, text or "")):
        groups.append(
            _build_group_view(
                group_id=None,
                label=label,
                source=GROUP_SOURCE_DEFAULT,
                members=default_buckets[label],
                position=None,
                collapsed=default_collapsed,
            )
        )

    # Any group referenced only by rows (should not happen given reference
    # validation, but stay defensive) is surfaced rather than silently dropped.
    for group_id, members in rows_by_group.items():
        if group_id is None or group_id in declared_ids:
            continue
        groups.append(
            _build_group_view(
                group_id=group_id,
                label=None,
                source=GROUP_SOURCE_DESIRED,
                members=members,
                position=None,
                collapsed=False,
            )
        )

    if overall_observation.needs_reload:
        diagnostics.append(
            "projection snapshot is not current; reload / live preflight required "
            "before trusting it (it never authorizes a side effect)"
        )

    return GroupedReadModel(
        groups=tuple(groups),
        observation=overall_observation,
        diagnostics=tuple(diagnostics),
        project_group_presentation=config.project_group_presentation,
    )


def _group_collapsed(group: ProjectGroup, config: PresentationGroupingConfig) -> bool:
    if group.collapsed is not None:
        return group.collapsed
    if config.defaults.collapsed is not None:
        return config.defaults.collapsed
    return False


def _build_group_view(
    *,
    group_id: Optional[str],
    label: Optional[str],
    source: str,
    members: "list[UnitView]",
    position: Optional[int],
    collapsed: bool,
) -> ProjectGroupView:
    """Split members into visible / hidden buckets and derive group staleness."""
    visible = sorted(
        (unit for unit in members if not unit.hidden), key=_unit_order_key
    )
    hidden = sorted(
        (unit for unit in members if unit.hidden), key=_unit_order_key
    )
    # A group is stale when it has no live Target among any of its members
    # (visible or hidden) — including a declared group with no members at all.
    stale = not any(unit.active for unit in members)
    return ProjectGroupView(
        group_id=group_id,
        label=label,
        source=source,
        units=tuple(visible),
        hidden_units=tuple(hidden),
        position=position,
        collapsed=collapsed,
        stale=stale,
    )


__all__ = (
    "UNIT_STATUS_OBSERVED",
    "UNIT_STATUS_STALE",
    "UNIT_STATUS_PARTIAL",
    "UNIT_STATUS_UNREADABLE",
    "UNIT_STATUS_CONTRADICTED",
    "UNIT_STATUS_UNKNOWN",
    "GROUP_SOURCE_DESIRED",
    "GROUP_SOURCE_DEFAULT",
    "GROUPED_READ_MODEL_DIAGNOSTIC_ONLY_NOTE",
    "UNKNOWN_OBSERVATION",
    "ObservedUnit",
    "UnitView",
    "ProjectGroupView",
    "GroupedReadModel",
    "build_grouped_read_model",
)
