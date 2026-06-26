"""Grouped cockpit reload / freshness UX view (Redmine #12266).

This module is the **reload / freshness UX layer** over the #12264 grouped
cockpit read model (:mod:`mozyo_bridge.domain.grouped_read_model`). It is the
grouped-view counterpart of what #12225 did for the flat pane cockpit
(``cockpit_ui.attach_observation`` / the ``renderObservation`` + Reload button):
it answers, for the Project Group -> Unit view a grouped cockpit renders, the
two operator questions the Start Gate (#12266 j#61875) names —

- *when was this grouped display observed, and is it still current?* — surfaced
  as the whole-projection ``observed_at`` / ``freshness`` / ``display_state`` and
  an explicit :attr:`GroupedReloadView.reload_required`, plus per-group and
  per-Unit ``reload_required`` so a degraded row reads as needing a reload rather
  than as current; and
- *how do I reload?* — the manual reload affordance semantics
  (:class:`ReloadAffordance`): a display descriptor of the **Reload** control's
  behavior, not a side-effecting command.

It exists as a separate projection (not extra fields on the read model) because
the read model deliberately does **not** serialize ``needs_reload`` — "Derived
(not carried) so the row's stored data never names a reload / loading verb"
(:class:`~mozyo_bridge.domain.grouped_read_model.UnitView`). The *display* layer
is where ``reload_required`` becomes an explicit, rendered flag and where the
reload affordance's semantics live. Like #12264, this is an object-to-object
slice: it derives a view from a :class:`GroupedReadModel` object and wires no
served endpoint / HTML page (no grouped page exists yet), keeping the staged
discipline schema -> resolver -> read model -> reload view.

Boundary, kept enforced in code and pinned by tests
(``runtime-observability-boundary.md`` ``### Freshness / fail-safe semantics`` /
``### Contract handoff to follow-up issues`` ``#12225`` / ``## Future Push /
Sidecar Observer Scope Split``):

- **Display / diagnostic only, never action permission or workflow truth.**
  Reloading the grouped view refreshes the displayed snapshot only; it updates no
  workflow gate (owner approval / review / routing / close / completion stay with
  the Redmine durable record) and authorizes no side effect — a grouped Unit
  action re-resolves its target through the action-time live preflight in
  :mod:`mozyo_bridge.application.cockpit_ui` regardless of this view's freshness
  (:data:`GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE`).
- **Fail-safe freshness, never ``healthy`` from a degraded snapshot.** The
  freshness fields and ``reload_required`` are read straight from the read
  model's ``needs_reload`` / observation envelope, whose ``display_state`` is
  never ``healthy`` for a stale / unreadable / contradictory snapshot. A snapshot
  may be *shown* (its ``freshness`` label is visible) but never reads as current.
- **Explicit reload only, no background observer.** v1 freshness is explicit
  reload + action-time live preflight; this view's affordance is operator-driven
  (:attr:`ReloadAffordance.auto` is ``False``) and adds no continuous polling /
  push / sidecar / OTel observer (:data:`GROUPED_RELOAD_EXPLICIT_ONLY_NOTE`).
- **Public-safe, no routing authority.** The view carries only freshness facts,
  a public-safe display ``label``, and the opaque ``unit_id`` provenance key — no
  ``target`` / ``pane`` / ``route`` / ``send`` / ``approval`` / ``credential``
  field, and no private path / host / operator policy.

The module is pure (dataclasses + derivation helpers) and imports only from the
domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.domain.grouped_read_model import (
    GroupedReadModel,
    ProjectGroupView,
    UnitView,
)
from mozyo_bridge.domain.runtime_observation import (
    DISPLAY_STATE_HEALTHY,
    DISPLAY_STATE_RELOAD_REQUIRED,
    DISPLAY_STATE_UNKNOWN,
    FRESHNESS_EXPIRED,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
)

#: The default label of the manual reload control in the grouped cockpit view.
RELOAD_AFFORDANCE_LABEL: str = "Reload"

#: The boundary statement the reload affordance carries: reloading the grouped
#: view is a display / diagnostic act only — it moves no workflow gate and
#: authorizes no side-effecting action (those re-preflight live at action time).
GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE: str = (
    "Reloading the grouped view refreshes the displayed snapshot only: it does "
    "not update workflow truth, owner approval, review, routing, close, or "
    "completion (those stay with the Redmine durable record), and it authorizes "
    "no side-effecting action — a grouped Unit action re-resolves its target "
    "through an action-time live preflight regardless of this view's freshness."
)

#: The v1-scope statement the reload affordance carries: freshness is held by
#: explicit reload + action-time live preflight, with no background observer.
GROUPED_RELOAD_EXPLICIT_ONLY_NOTE: str = (
    "v1 freshness is explicit reload plus action-time live preflight only: this "
    "view adds no continuous polling, push, sidecar, or background freshness "
    "observer; a missed change degrades to reload_required / unknown, never to "
    "a silently current display."
)

#: Human-facing freshness labels, public-safe (vocabulary tokens only, never a
#: path / host / secret). The ``unknown`` / degraded labels read as attention,
#: never as current.
_FRESHNESS_LABELS: "dict[str, str]" = {
    FRESHNESS_FRESH: "fresh",
    FRESHNESS_STALE: "stale",
    FRESHNESS_EXPIRED: "expired",
    FRESHNESS_UNKNOWN: "unknown",
}


def _freshness_label(
    freshness: str,
    stale_reason: Optional[str],
    contradiction: Optional[str],
) -> str:
    """A public-safe, human display label for a freshness / contradiction pair.

    Built from the observation vocabulary only (never a path / host / secret), so
    it is safe to render and to paste into a public record. A contradiction is the
    strongest visible condition; otherwise the label is the freshness class, with
    the ``stale_reason`` appended when present so a non-fresh row explains itself.
    A degraded label never reads as current.
    """
    if contradiction is not None:
        return f"contradicted ({contradiction})"
    base = _FRESHNESS_LABELS.get(freshness, FRESHNESS_UNKNOWN)
    if freshness == FRESHNESS_FRESH:
        return base
    if stale_reason:
        return f"{base} ({stale_reason})"
    return base


@dataclass(frozen=True)
class ReloadAffordance:
    """Display descriptor for the manual grouped-view reload control.

    The "button semantics" the Start Gate asks for, as data rather than rendered
    markup: a consumer renders a control labeled :attr:`label` that, when the
    operator activates it, re-fetches / rebuilds the grouped read model. It is
    **always** :attr:`available` — including when the view is already fresh — so
    the operator can refresh on demand, and it is never :attr:`auto`-triggered
    (no background observer drives it; v1 freshness is explicit reload +
    action-time live preflight). The notes pin that reloading is display-only and
    authorizes no side effect.
    """

    label: str = RELOAD_AFFORDANCE_LABEL
    available: bool = True
    auto: bool = False
    diagnostic_only_note: str = GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE
    explicit_only_note: str = GROUPED_RELOAD_EXPLICIT_ONLY_NOTE

    def as_payload(self) -> dict:
        return {
            "label": self.label,
            "available": self.available,
            "auto": self.auto,
            "diagnostic_only_note": self.diagnostic_only_note,
            "explicit_only_note": self.explicit_only_note,
        }


@dataclass(frozen=True)
class UnitFreshnessView:
    """One Unit row's reload / freshness UX projection (display-only).

    Surfaces, for a single grouped read-model row, the freshness facts a consumer
    renders plus the explicit :attr:`reload_required` flag the read model itself
    does not serialize. Carries only the opaque ``unit_id`` provenance key and a
    public-safe display ``label`` — no routing / authority field — because this
    view never seeds an action (a grouped action takes identity from the read
    model's ``candidate_unit_selector`` and re-preflights live).
    """

    unit_id: str
    label: Optional[str]
    status: str
    freshness: str
    freshness_label: str
    observed_at: Optional[str]
    stale_reason: Optional[str]
    contradiction: Optional[str]
    reload_required: bool

    def as_payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "label": self.label,
            "status": self.status,
            "freshness": self.freshness,
            "freshness_label": self.freshness_label,
            "observed_at": self.observed_at,
            "stale_reason": self.stale_reason,
            "contradiction": self.contradiction,
            "reload_required": self.reload_required,
        }


@dataclass(frozen=True)
class GroupFreshnessView:
    """A Project Group's reload / freshness UX projection (display-only).

    ``stale`` is the read model's "no live Target among members" fact (carried
    verbatim); :attr:`reload_required` is True when any member Unit row is not
    current (so a group with a stale / unreadable / contradicted member reads as
    needing attention) — the two are orthogonal: an empty declared group is
    ``stale`` with nothing to reload, and a group whose only member is a stale
    snapshot is both. ``units`` holds every member (visible then hidden), display
    order preserved.
    """

    group_id: Optional[str]
    label: Optional[str]
    stale: bool
    reload_required: bool
    units: "tuple[UnitFreshnessView, ...]" = ()

    def as_payload(self) -> dict:
        return {
            "group_id": self.group_id,
            "label": self.label,
            "stale": self.stale,
            "reload_required": self.reload_required,
            "units": [unit.as_payload() for unit in self.units],
        }


@dataclass(frozen=True)
class GroupedReloadView:
    """The grouped cockpit reload / freshness UX view — a display projection.

    A consumer renders, from this object: a "last observed / freshness" line for
    the whole grouped projection (:attr:`observed_at` / :attr:`freshness` /
    :attr:`freshness_label` / :attr:`display_state`), a reload-needed indicator
    (:attr:`reload_required` for the whole snapshot, :attr:`needs_attention` when
    any group also needs one), per-group / per-Unit freshness, and the manual
    :attr:`reload` affordance. It is not workflow truth and not action permission
    (:data:`GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE`).
    """

    observed_at: Optional[str]
    freshness: str
    display_state: str
    freshness_label: str
    reload_required: bool
    groups: "tuple[GroupFreshnessView, ...]" = ()
    reload: ReloadAffordance = ReloadAffordance()
    diagnostics: "tuple[str, ...]" = ()

    @property
    def needs_attention(self) -> bool:
        """True when the whole-projection snapshot OR any group needs a reload.

        A fail-safe roll-up for a single "reload recommended" indicator: a UI may
        show attention even when the top-level snapshot reads fresh but a per-Unit
        observation is stale / unreadable / contradicted. It authorizes nothing.
        """
        return self.reload_required or any(
            group.reload_required for group in self.groups
        )

    def all_units(self) -> "tuple[UnitFreshnessView, ...]":
        units: "list[UnitFreshnessView]" = []
        for group in self.groups:
            units.extend(group.units)
        return tuple(units)

    def as_payload(self) -> dict:
        return {
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "display_state": self.display_state,
            "freshness_label": self.freshness_label,
            "reload_required": self.reload_required,
            "needs_attention": self.needs_attention,
            "groups": [group.as_payload() for group in self.groups],
            "reload": self.reload.as_payload(),
            "diagnostics": list(self.diagnostics),
            "boundary_note": GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE,
        }


def _unit_freshness_view(unit: UnitView) -> UnitFreshnessView:
    """Project one read-model row to its reload / freshness UX row.

    ``reload_required`` is the read model's derived ``needs_reload`` (True unless
    the row is a fresh, readable, uncontradicted, placed Unit), surfaced here as
    an explicit display flag. Only the row's opaque ``unit_id`` and public-safe
    ``label`` are carried — no routing identity.
    """
    return UnitFreshnessView(
        unit_id=unit.unit_id,
        label=unit.label,
        status=unit.status,
        freshness=unit.freshness,
        freshness_label=_freshness_label(
            unit.freshness, unit.stale_reason, unit.contradiction
        ),
        observed_at=unit.observed_at,
        stale_reason=unit.stale_reason,
        contradiction=unit.contradiction,
        reload_required=unit.needs_reload,
    )


def _group_freshness_view(group: ProjectGroupView) -> GroupFreshnessView:
    """Project one Project Group to its reload / freshness UX group.

    Members are taken in the read model's display order (visible then hidden) so
    a hidden-but-active Unit still shows its freshness. ``reload_required`` is
    True when any member row is not current; ``stale`` carries the read model's
    "no live Target" fact unchanged.
    """
    units = tuple(_unit_freshness_view(unit) for unit in group.all_units())
    return GroupFreshnessView(
        group_id=group.group_id,
        label=group.label,
        stale=group.stale,
        reload_required=any(unit.reload_required for unit in units),
        units=units,
    )


def build_grouped_reload_view(model: GroupedReadModel) -> GroupedReloadView:
    """Derive the reload / freshness UX view from a grouped read model.

    Pure projection: reads the model's whole-projection observation envelope and
    each row's carried freshness, surfacing ``reload_required`` explicitly at
    every level (whole view / group / Unit) and attaching the manual reload
    affordance descriptor. It adds no new freshness *authority* — every freshness
    fact comes from the read model, whose degraded snapshots never derive
    ``healthy`` — and it carries no routing / approval field.
    """
    observation = model.observation
    groups = tuple(_group_freshness_view(group) for group in model.groups)
    return GroupedReloadView(
        observed_at=observation.observed_at,
        freshness=observation.freshness,
        display_state=observation.display_state,
        freshness_label=_freshness_label(
            observation.freshness,
            observation.stale_reason,
            observation.contradiction,
        ),
        reload_required=model.needs_reload,
        groups=groups,
        reload=ReloadAffordance(),
        diagnostics=model.diagnostics,
    )


# Re-exported for callers that assert display-state vocabulary against this view.
__all__ = (
    "RELOAD_AFFORDANCE_LABEL",
    "GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE",
    "GROUPED_RELOAD_EXPLICIT_ONLY_NOTE",
    "DISPLAY_STATE_HEALTHY",
    "DISPLAY_STATE_RELOAD_REQUIRED",
    "DISPLAY_STATE_UNKNOWN",
    "ReloadAffordance",
    "UnitFreshnessView",
    "GroupFreshnessView",
    "GroupedReloadView",
    "build_grouped_reload_view",
)
