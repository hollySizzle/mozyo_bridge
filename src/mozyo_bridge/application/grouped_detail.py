"""Grouped cockpit Unit detail / command preview projection (Redmine #12296).

When an operator selects a Unit / Target in the served grouped cockpit (the
Project Group -> Unit -> Target display the #12264 read model projects and the
#12255 display view renders), they need to see, on **one screen**, the safe
actions that Unit's row could next seed and the preflight boundary those actions
run under. This module is that **detail / command preview projection**: it maps a
single grouped read-model row (:class:`~mozyo_bridge.domain.grouped_read_model.UnitView`)
to a :class:`GroupedUnitDetailView` carrying the Unit's public-safe identity /
state plus a list of :class:`CommandPreview` rows — one per actionable role pane x
cockpit action (``reveal`` / ``jump``).

Boundary, kept enforced in code and pinned by tests
(``unit-target-model.md`` ``### Project Group projection`` /
``runtime-observability-boundary.md`` ``## Action-Time Live Preflight Boundary`` /
``public-private-boundary.md`` ``## Public Record Constraints`` /
``plugin-ready-adapter-boundary.md`` "command preview / confirm semantics"):

- **Preview, never authority.** Every :class:`CommandPreview` is *display
  information*. It describes which cockpit action a row could seed and whether
  that action looks currently available, but it is **not** a permission and never
  performs a side effect. Each preview carries ``live_preflight_required=True``:
  executing the named action still routes through the established action-time live
  preflight in :mod:`mozyo_bridge.application.cockpit_ui`
  (:func:`~mozyo_bridge.application.cockpit_ui.grouped_reveal` /
  :func:`~mozyo_bridge.application.cockpit_ui.grouped_jump` ->
  :func:`~mozyo_bridge.application.cockpit_ui._resolve_unit_target`), which
  re-resolves the candidate identity against a *fresh* inventory and fails closed
  on every uncertainty. This projection can only *refuse* (fail closed), never
  *permit*.
- **Stale / contradictory / ambiguous -> unavailable.** Availability is derived
  from the displayed row alone and is fail-closed, mirroring the refusal gates of
  :func:`~mozyo_bridge.application.cockpit_ui.candidate_unit_selector` /
  ``_resolve_unit_target`` as a preview: a degraded row (``needs_reload`` — stale /
  unreadable / contradicted / identity_conflict / desired_unit_missing / partial /
  unknown), a non-local ``host_id``, or a row with no observed live role pane
  yields **no available command**, each with a visible reason. A non-``default``
  ``lane_id`` is **not** a blocking condition (Redmine #12293): the cockpit
  inventory now reads each pane's ``@mozyo_lane_id`` and splits a workspace's lanes
  into faithful, distinct Units, so a non-default lane is a first-class identity
  selector the previewed command carries (and the live preflight narrows its match
  set by), never a capability gap. A row that *looks* available stays a candidate
  only: the live preflight may still reject it as ambiguous (more than one live
  pane) / missing / stale at action time — including a missing or ambiguous lane —
  which is why ``live_preflight_required`` is always set. The live, non-mutating
  counterpart that actually re-queries the inventory (and so can observe
  ambiguity) is
  :func:`~mozyo_bridge.application.cockpit_ui.grouped_action_preview`.
- **Public-safe only.** The detail carries the row's public-safe identity
  (``workspace_id`` / ``lane_id`` / ``host_id``), display label, status /
  freshness tokens, and observed role *names* — never a pane id / target, repo
  path, credential, or prompt body. The command ``selector`` is exactly the
  public-safe identity the grouped action endpoint already accepts (#12265); the
  command ``summary`` is a generic action description with no concrete path.

The module is pure (dataclasses + derivation helpers): it reads a read-model row
and the action-surface constants, performs no I/O, and resolves no pane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.application.cockpit_ui import DEFAULT_HOST
from mozyo_bridge.domain.attention import ROLE_CLAUDE, ROLE_CODEX
from mozyo_bridge.domain.grouped_display import ROLE_DISPLAY_ORDER
from mozyo_bridge.domain.grouped_read_model import UnitView

#: The agent roles a grouped cockpit action can seed (``reveal`` / ``jump`` resolve
#: a live pane by ``(workspace_id, role)``). A row's observed ``roles`` is filtered
#: to this set before previewing — an observed non-agent role name is not an
#: actionable cockpit target here.
ACTIONABLE_ROLES: "frozenset[str]" = frozenset({ROLE_CODEX, ROLE_CLAUDE})

#: The cockpit actions a Unit row can preview. ``kind`` is the action verb,
#: ``endpoint`` is the served POST path that performs it (its own action-time live
#: preflight is the authority), and ``summary`` is a public-safe description that
#: names no concrete path / pane / target.
_ACTION_CATALOG: "tuple[tuple[str, str, str], ...]" = (
    (
        "reveal",
        "/api/actions/grouped-reveal",
        "Reveal the unit's repo root in Finder, resolved live at action time.",
    ),
    (
        "jump",
        "/api/actions/grouped-jump",
        "Switch the attached tmux client to the unit's window, resolved live "
        "at action time.",
    ),
)

#: The boundary statement the detail view carries: a Unit detail / command preview
#: is a display projection only — it lists candidate actions and a fail-closed
#: availability hint, but authorizes nothing. Executing a previewed action
#: re-resolves the candidate identity through the action-time live preflight,
#: which may still fail closed (stale / ambiguous / missing).
GROUPED_DETAIL_DIAGNOSTIC_ONLY_NOTE: str = (
    "Grouped unit detail is a command preview only: each listed action is a "
    "display hint, not routing / approval / review / close / completion "
    "authority. Availability is derived fail-closed from the displayed row and "
    "never permits a side effect; executing an action re-resolves the unit "
    "identity through the action-time live preflight, which may still fail "
    "closed if the target is stale, ambiguous, or missing."
)


@dataclass(frozen=True)
class CommandPreview:
    """One previewed cockpit action for a selected Unit row (display only).

    Names the action (:attr:`kind`), the role pane it would target
    (:attr:`role` — a public-safe role name, never a pane id / target), the served
    :attr:`endpoint` that performs it, and a public-safe :attr:`summary`.
    :attr:`available` is the fail-closed projection hint and :attr:`unavailable_reason`
    explains a ``False``. :attr:`selector` is the public-safe identity the action
    endpoint accepts (present only when available; ``None`` when the action cannot
    be seeded). :attr:`live_preflight_required` is always ``True`` — even an
    available preview is a candidate, never a permission: the action re-preflights
    live before any side effect.
    """

    kind: str
    role: Optional[str]
    endpoint: str
    summary: str
    available: bool
    live_preflight_required: bool = True
    unavailable_reason: Optional[str] = None
    selector: Optional[dict] = None

    def as_payload(self) -> dict:
        return {
            "kind": self.kind,
            "role": self.role,
            "endpoint": self.endpoint,
            "summary": self.summary,
            "available": self.available,
            "live_preflight_required": self.live_preflight_required,
            "unavailable_reason": self.unavailable_reason,
            "selector": dict(self.selector) if self.selector is not None else None,
        }


@dataclass(frozen=True)
class GroupedUnitDetailView:
    """The detail / command preview for one selected grouped Unit (display only).

    A consumer draws, from this object: the Unit's public-safe identity
    (:attr:`workspace_id` / :attr:`lane_id` / :attr:`host_id` / :attr:`unit_id`),
    its display :attr:`label`, its visible :attr:`status` / :attr:`freshness`
    state (with :attr:`stale_reason` / :attr:`contradiction` when degraded), the
    observed live role panes (:attr:`roles`), and a list of :attr:`commands` — the
    safe cockpit actions and whether each looks currently available.
    :attr:`actions_available` is True when at least one command is available;
    :attr:`unavailable_reason` carries the whole-unit reason when none are. It is
    not workflow truth and not action permission
    (:data:`GROUPED_DETAIL_DIAGNOSTIC_ONLY_NOTE`).
    """

    unit_id: str
    workspace_id: str
    lane_id: str
    host_id: str
    label: Optional[str]
    status: str
    freshness: str
    observed_at: Optional[str]
    stale_reason: Optional[str]
    contradiction: Optional[str]
    active: bool
    roles: "tuple[str, ...]"
    actions_available: bool
    unavailable_reason: Optional[str]
    commands: "tuple[CommandPreview, ...]" = ()

    def as_payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "host_id": self.host_id,
            "label": self.label,
            "status": self.status,
            "freshness": self.freshness,
            "observed_at": self.observed_at,
            "stale_reason": self.stale_reason,
            "contradiction": self.contradiction,
            "active": self.active,
            "roles": list(self.roles),
            "actions_available": self.actions_available,
            "unavailable_reason": self.unavailable_reason,
            "commands": [command.as_payload() for command in self.commands],
            "boundary_note": GROUPED_DETAIL_DIAGNOSTIC_ONLY_NOTE,
        }


def _actionable_roles(unit: UnitView) -> "tuple[str, ...]":
    """The observed agent role panes, in canonical display order, deduplicated.

    Filters the row's observed ``roles`` to :data:`ACTIONABLE_ROLES` (an observed
    non-agent role name is not a cockpit action target) and orders them by
    :data:`ROLE_DISPLAY_ORDER`. Carries role names only, never a pane id.
    """
    observed = {
        role for role in unit.roles if role in ACTIONABLE_ROLES
    }
    return tuple(role for role in ROLE_DISPLAY_ORDER if role in observed)


def _blocking_reason(unit: UnitView, roles: "tuple[str, ...]") -> Optional[str]:
    """The fail-closed reason no action can be seeded from this row, or ``None``.

    Mirrors, as a *preview*, the refusal gates of ``candidate_unit_selector`` and
    ``_resolve_unit_target`` (most-blocking first): a degraded (``needs_reload``)
    row, a non-local host, or a row with no observed live agent role pane.
    ``None`` means a command may be previewed as available — still a candidate
    only, re-checked by the live preflight before any side effect.

    The Unit ``lane_id`` is **not** a blocking condition (Redmine #12293): the
    cockpit inventory now reads each pane's ``@mozyo_lane_id`` and splits a
    workspace's lanes into faithful, distinct Units, so a non-default lane is a
    first-class identity selector the live preflight (``_resolve_unit_target``)
    narrows the match set by — never a capability gap. The previewed command's
    selector carries the row's ``lane_id`` so the action re-resolves against the
    same lane live.
    """
    if unit.needs_reload:
        return (
            "the displayed unit row is not current (status="
            f"{unit.status!r}); reload and live-preflight before acting on it."
        )
    if unit.host_id != DEFAULT_HOST:
        return (
            f"grouped action cannot resolve a non-local host ({unit.host_id!r}); "
            "the cockpit inventory observes the local tmux server only. Use an "
            "explicit live target."
        )
    if not unit.active or not roles:
        return (
            "no observed live target for this unit (it may have exited); reload "
            "and live-preflight before acting on it."
        )
    return None


def build_grouped_unit_detail(unit: UnitView) -> GroupedUnitDetailView:
    """Build the detail / command preview for one selected grouped Unit row.

    Pure projection: derives, from a #12264 read-model row, the Unit's public-safe
    identity / state and a list of :class:`CommandPreview` rows — one per actionable
    role pane x cockpit action (``reveal`` / ``jump``) when the row is actionable,
    or one per action (role-less, unavailable) carrying the fail-closed
    :func:`_blocking_reason` when it is not.

    Availability is a *preview hint* only and fail-closed: a degraded / remote /
    no-live-target row yields no available command. Even an
    available command stays a candidate (``live_preflight_required``): the named
    action re-resolves the identity through the action-time live preflight before
    any side effect, which may still reject it as ambiguous / missing / stale. No
    field grants a side-effect permission and no field carries a pane id / path /
    credential / prompt body.
    """
    roles = _actionable_roles(unit)
    blocking = _blocking_reason(unit, roles)
    commands: list[CommandPreview] = []
    if blocking is None:
        # Actionable: a candidate command per observed role pane x action.
        for role in roles:
            selector = {
                "workspace_id": unit.workspace_id,
                "lane_id": unit.lane_id,
                "host_id": unit.host_id,
                "role": role,
            }
            for kind, endpoint, summary in _ACTION_CATALOG:
                commands.append(
                    CommandPreview(
                        kind=kind,
                        role=role,
                        endpoint=endpoint,
                        summary=summary,
                        available=True,
                        unavailable_reason=None,
                        selector=dict(selector),
                    )
                )
    else:
        # Not actionable: list the actions that exist but are unavailable, with
        # the visible reason — the action is shown, never silently dropped.
        for kind, endpoint, summary in _ACTION_CATALOG:
            commands.append(
                CommandPreview(
                    kind=kind,
                    role=None,
                    endpoint=endpoint,
                    summary=summary,
                    available=False,
                    unavailable_reason=blocking,
                    selector=None,
                )
            )

    actions_available = any(command.available for command in commands)
    return GroupedUnitDetailView(
        unit_id=unit.unit_id,
        workspace_id=unit.workspace_id,
        lane_id=unit.lane_id,
        host_id=unit.host_id,
        label=unit.label,
        status=unit.status,
        freshness=unit.freshness,
        observed_at=unit.observed_at,
        stale_reason=unit.stale_reason,
        contradiction=unit.contradiction,
        active=unit.active,
        roles=roles,
        actions_available=actions_available,
        unavailable_reason=None if actions_available else blocking,
        commands=tuple(commands),
    )


__all__ = (
    "ACTIONABLE_ROLES",
    "GROUPED_DETAIL_DIAGNOSTIC_ONLY_NOTE",
    "CommandPreview",
    "GroupedUnitDetailView",
    "build_grouped_unit_detail",
)
