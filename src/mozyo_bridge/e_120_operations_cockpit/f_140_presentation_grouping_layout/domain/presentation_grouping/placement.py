"""Launch-placement resolution for the desired presentation grouping config.

This module owns the **placement resolution** responsibility: mapping a
:class:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.config.LaunchContext` (plus a
parsed config) to a :class:`GroupPlacement`, and a desired
``project_group_presentation`` mode (plus that placement) to a
:class:`GroupWindowDecision`.

Boundary, kept enforced in code: resolution is **display grouping only — never
routing / approval / liveness authority.** It maps context to a *desired*
Project Group and view preferences; it resolves no handoff target, asserts no
liveness, and grants no owner approval / review / close authority. A live
observation that contradicts the launch identity resolves to a *visible*
degraded ``identity_conflict`` status (never a silent reroute); the action-time
preflight still decides any side effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import LaunchContext, PresentationGroupingConfig
from .constants import (
    _PRESENTATION_MODE_TO_SURFACE,
    GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
    GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    GROUP_WINDOW_SURFACE_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_MODES,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
    STATUS_CONFIGURED,
    STATUS_DEFAULT,
    STATUS_IDENTITY_CONFLICT,
    STATUS_UNGROUPED,
)
from .errors import PresentationGroupingConfigError


@dataclass(frozen=True)
class GroupPlacement:
    """The resolved desired placement of a launching sublane.

    Display-only: ``group_id`` / ``label`` / ``position`` / ``pinned`` /
    ``hidden`` / ``collapsed`` / ``preferred_projection`` describe *where and how*
    the Unit is shown, never a routing target or an approval. ``status`` records
    whether the placement came from config, the behavior-preserving default, or a
    visible degraded condition; ``diagnostic`` carries human-facing degraded
    wording when present.
    """

    status: str
    group_id: Optional[str] = None
    label: Optional[str] = None
    position: Optional[int] = None
    pinned: bool = False
    hidden: bool = False
    collapsed: bool = False
    preferred_projection: Optional[str] = None
    diagnostic: Optional[str] = None


def _placement_from_group(
    config: PresentationGroupingConfig,
    *,
    status: str,
    group_id: Optional[str],
    position: Optional[int],
    pinned: Optional[bool],
    hidden: Optional[bool],
    preferred_projection: Optional[str],
    label_override: Optional[str] = None,
    diagnostic: Optional[str] = None,
) -> GroupPlacement:
    """Compose a placement, layering group + rule/override + defaults preferences.

    Precedence for each display field: the matched rule / override value, then
    the group's own value, then ``grouping.defaults``. ``label`` prefers an
    override's ``label_override`` over the group's declared label.
    """
    group = config.group(group_id)
    defaults = config.defaults
    label = label_override or (group.label if group is not None else None)
    if preferred_projection is None:
        preferred_projection = defaults.preferred_projection
    collapsed = False
    if group is not None and group.collapsed is not None:
        collapsed = group.collapsed
    elif defaults.collapsed is not None:
        collapsed = defaults.collapsed
    return GroupPlacement(
        status=status,
        group_id=group_id,
        label=label,
        position=position,
        pinned=bool(pinned) if pinned is not None else False,
        hidden=bool(hidden) if hidden is not None else False,
        collapsed=collapsed,
        preferred_projection=preferred_projection,
        diagnostic=diagnostic,
    )


def resolve_launch_placement(
    config: "Optional[PresentationGroupingConfig]",
    context: LaunchContext,
) -> GroupPlacement:
    """Resolve which Project Group a launching sublane is displayed under.

    Resolution is display-only; it never resolves a handoff target or asserts
    liveness. Precedence, highest first:

    1. an explicit ``unit_overrides`` entry selecting this workspace/lane;
    2. the first ``membership_rules`` entry whose ``when`` predicates match;
    3. the ``grouping.defaults.unknown_unit_group`` fallback, if declared;
    4. an ungrouped placement (no group), never a fabricated one.

    Outcomes:

    - ``None`` / empty config -> ``status=default``: the behavior-preserving
      placement keyed on the public repo / workspace label, so a repo with no
      grouping config is placed exactly as before.
    - a matched override / rule -> ``status=configured``.
    - no match and no ``unknown_unit_group`` -> ``status=ungrouped`` (group_id
      ``None``); target discovery is never failed by an absent group.
    - a live observation contradicting the launch identity ->
      ``status=identity_conflict`` (the placement is still computed and shown; the
      action-time preflight decides any side effect).
    """
    if config is None or (
        not config.project_groups
        and not config.membership_rules
        and not config.unit_overrides
    ):
        # No grouping config: preserve current behavior — group by the public
        # repo / workspace label (an implementation default), never a fabricated
        # config group.
        fallback_label = context.repo_label or context.workspace_id
        return GroupPlacement(
            status=STATUS_DEFAULT,
            group_id=None,
            label=fallback_label,
        )

    conflict = context.has_identity_conflict()
    status = STATUS_IDENTITY_CONFLICT if conflict else STATUS_CONFIGURED
    diagnostic = (
        config.defaults.degraded_display if conflict else None
    ) or ("live identity contradicts launch identity" if conflict else None)

    for override in config.unit_overrides:
        if override.selects(context):
            return _placement_from_group(
                config,
                status=status,
                group_id=override.preferred_group,
                position=override.position,
                pinned=override.pinned,
                hidden=override.hidden,
                preferred_projection=override.preferred_projection,
                label_override=override.label_override,
                diagnostic=diagnostic,
            )

    for rule in config.membership_rules:
        if rule.matches(context):
            return _placement_from_group(
                config,
                status=status,
                group_id=rule.group_id,
                position=rule.position,
                pinned=rule.pinned,
                hidden=rule.hidden,
                preferred_projection=rule.preferred_projection,
                diagnostic=diagnostic,
            )

    unknown_unit_group = config.defaults.unknown_unit_group
    if unknown_unit_group is not None:
        return _placement_from_group(
            config,
            status=status,
            group_id=unknown_unit_group,
            position=None,
            pinned=None,
            hidden=None,
            preferred_projection=None,
            diagnostic=diagnostic,
        )

    return GroupPlacement(
        status=STATUS_IDENTITY_CONFLICT if conflict else STATUS_UNGROUPED,
        group_id=None,
        label=context.repo_label or context.workspace_id,
        preferred_projection=config.defaults.preferred_projection,
        diagnostic=diagnostic,
    )


@dataclass(frozen=True)
class GroupWindowDecision:
    """The desired launcher / cockpit-append placement for a launching sublane.

    Resolved from the configured ``project_group_presentation`` mode plus the
    Unit's resolved :class:`GroupPlacement`. Display-only: it describes *where*
    the operator desires the sublane laid out and which surface the launcher will
    actually use, and is never a routing / approval / close authority and never a
    guaranteed tmux window / iTerm tab / OS window.

    - :attr:`presentation_mode` is the configured (desired) mode.
    - :attr:`desired_surface` is the surface the mode asks for (one of the
      ``GROUP_WINDOW_SURFACE_*`` values).
    - :attr:`executed_surface` is the surface the launcher actually uses now. The
      current single-window cockpit append only executes ``cockpit_column``; the
      opt-in surfaces (``group_tmux_window`` / ``normal_window``) record the
      *desired* placement but :attr:`degraded` to ``cockpit_column`` rather than
      silently spawning a second window that would bypass the duplicate-detection
      / pane-identity gate (acceptance: visible degrade, never silent reroute).
    - :attr:`desired_window_name` is the public-safe display name of the desired
      per-group window (``group_tmux_window`` only), or ``None``.
    - :attr:`diagnostic` carries the human-facing degrade wording when
      :attr:`degraded`.
    """

    presentation_mode: str
    desired_surface: str
    executed_surface: str
    group_id: Optional[str] = None
    label: Optional[str] = None
    desired_window_name: Optional[str] = None
    degraded: bool = False
    diagnostic: Optional[str] = None

    def as_dict(self) -> "dict[str, object]":
        return {
            "presentation_mode": self.presentation_mode,
            "desired_surface": self.desired_surface,
            "executed_surface": self.executed_surface,
            "group_id": self.group_id,
            "label": self.label,
            "desired_window_name": self.desired_window_name,
            "degraded": self.degraded,
            "diagnostic": self.diagnostic,
        }


def resolve_group_window_placement(
    presentation_mode: str,
    placement: GroupPlacement,
    *,
    execute_group_window: bool = False,
) -> GroupWindowDecision:
    """Resolve the desired launcher / cockpit-append placement (Redmine #12302, #12330).

    Maps the configured ``project_group_presentation`` mode + the resolved
    :class:`GroupPlacement` to a :class:`GroupWindowDecision` the cockpit
    launcher / append path reads. Fail-closed and never a silent reroute:

    - ``same_cockpit_column`` (the default) -> the behavior-preserving shared
      cockpit column; not degraded.
    - ``project_group_tmux_window`` -> the per-Project-Group tmux window (#12290).
      Whether it actually *executes* is gated by ``execute_group_window``:

      * ``execute_group_window=False`` (the default, behavior-preserving for
        callers that only project the *desired* placement) keeps
        :attr:`executed_surface` at ``cockpit_column`` and the decision
        :attr:`degraded` with a visible diagnostic — the single-window degrade
        path #12302 shipped.
      * ``execute_group_window=True`` (the cockpit launcher when it can faithfully
        place per-group windows, #12330) sets :attr:`executed_surface` to
        ``group_tmux_window`` and is **not** degraded: the launcher creates /
        appends / focuses the group's own tmux window while keeping the same
        ``workspace + lane`` duplicate gate and pane-identity stamping. A tmux
        window / iTerm tab is still never *guaranteed* — it is a tmux-layer
        request only and never routing / approval / close authority.

    - ``normal_window`` -> the retained compatibility projection; always recorded
      as desired and degraded to the cockpit column (``execute_group_window`` does
      not relaunch a normal window — that is out of this surface's scope).
    - any other value fails closed (:class:`PresentationGroupingConfigError`),
      mirroring the closed display-only vocabulary; never normalized silently.
    """
    if presentation_mode not in PROJECT_GROUP_PRESENTATION_MODES:
        raise PresentationGroupingConfigError(
            f"project_group_presentation must be one of "
            f"{sorted(PROJECT_GROUP_PRESENTATION_MODES)}, got {presentation_mode!r}"
        )
    desired_surface = _PRESENTATION_MODE_TO_SURFACE[presentation_mode]

    if presentation_mode == PROJECT_GROUP_PRESENTATION_SAME_COLUMN:
        # Behavior-preserving default: the launcher uses the shared cockpit column
        # exactly as before. group_id / label are carried for display only.
        return GroupWindowDecision(
            presentation_mode=presentation_mode,
            desired_surface=GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
            executed_surface=GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
            group_id=placement.group_id,
            label=placement.label,
        )

    if presentation_mode == PROJECT_GROUP_PRESENTATION_TMUX_WINDOW:
        # Per-Project-Group tmux window. The public-safe window name is the
        # group's display label (its portable group_id when unlabeled, the
        # repo/workspace label for the implicit per-repo default group).
        window_name = placement.label or placement.group_id
        if execute_group_window:
            # Faithful execution (#12330): the launcher places the sublane in the
            # group's own tmux window. Not degraded — the duplicate-detection /
            # pane-identity gate is preserved across windows by the launcher, not by
            # collapsing back to the shared column. Still display-only: a tmux
            # window / iTerm tab is requested, never guaranteed, and never routing /
            # approval / close authority.
            return GroupWindowDecision(
                presentation_mode=presentation_mode,
                desired_surface=GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
                executed_surface=GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
                group_id=placement.group_id,
                label=placement.label,
                desired_window_name=window_name,
                degraded=False,
                diagnostic=None,
            )
        diagnostic = (
            "project_group_tmux_window is a desired per-Project-Group tmux window; "
            "this caller keeps the sublane in the shared cockpit column to preserve "
            "the duplicate-detection / pane-identity gate and never guarantees a "
            "tmux window / iTerm tab."
        )
        return GroupWindowDecision(
            presentation_mode=presentation_mode,
            desired_surface=GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
            executed_surface=GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
            group_id=placement.group_id,
            label=placement.label,
            desired_window_name=window_name,
            degraded=True,
            diagnostic=diagnostic,
        )

    # normal_window: the retained compatibility projection. `mozyo cockpit` keeps
    # the sublane as a cockpit column rather than relaunching it as a normal
    # window — recorded as desired, visibly degraded.
    diagnostic = (
        "normal_window is the retained compatibility projection; `mozyo cockpit` "
        "keeps the sublane as a cockpit column and does not relaunch it as a "
        "normal window."
    )
    return GroupWindowDecision(
        presentation_mode=presentation_mode,
        desired_surface=GROUP_WINDOW_SURFACE_NORMAL_WINDOW,
        executed_surface=GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
        group_id=placement.group_id,
        label=placement.label,
        degraded=True,
        diagnostic=diagnostic,
    )
