"""Desired presentation grouping schema + launch-placement resolver (Redmine #12263).

This package is the typed *schema boundary* and the pure *placement resolver* for
the repo-local **desired presentation grouping** config — the cockpit Project
Group layer whose field contract was fixed (docs-only) by Redmine #12262 in
``vibes/docs/logics/unit-presentation-state-db.md`` and whose projection model
(Project Group -> Unit -> Target) was fixed by Redmine #12253 in
``vibes/docs/logics/unit-target-model.md``. Those predecessors defined the
shape; this package is the first code that *parses* that shape and *resolves*
which Project Group a launching sublane is displayed under, from its
workspace / project / lane context.

It is the grouping analogue of
:class:`~mozyo_bridge.domain.repo_local_config.PresentationSelectionConfig`
(Redmine #12189), which selects a projection *surface*. Grouping is a separate
concern — *which display group a Unit belongs to* — so it is its own typed
record here rather than folded into the surface-selection record.

Boundary, kept enforced in code:

- **Display grouping only — never routing / approval / liveness authority.** The
  resolver maps context to a *desired* Project Group and view preferences. It
  resolves no handoff target, asserts no liveness, and grants no owner approval /
  review / close authority. Routing stays with the live resolver / pane preflight
  (the Start Gate's "routing target は live resolver / pane preflight に委ねる").
- **Default / missing config is behavior-preserving.** ``None`` resolves through
  :func:`resolve_launch_placement` to a default placement keyed on the public
  repo / workspace label — the current ungrouped behavior — so a repo with no
  grouping config never changes how a sublane is placed.
- **Closed schema, fail-closed on authority leaks.** Unknown keys, an unsupported
  version, a duplicate / dangling group reference, an unknown projection, or a
  *key* — or an identity / diagnostic *value* (``group_id`` / the group
  references / ``degraded_display``) — shaped like a target / pane / route / send
  / approval / credential / module is rejected through
  :class:`PresentationGroupingConfigError` (a :class:`ValueError`) — never a
  silent normalization. Free public display prose (``label`` / ``description`` /
  ``label_override``) is author-asserted public-safe and not token-scanned, so a
  legitimate label such as "Code Review" is preserved.
- **Degraded display, not silent reroute, for runtime drift.** An override that
  names a Unit that is not live, or a live identity that contradicts the launch
  context, resolves to a *visible* degraded placement status
  (``desired_unit_missing`` / ``identity_conflict``); the action-time preflight
  still decides any side effect.

Responsibilities are split across cohesive submodules so no single file is the
catch-all sink the oversized module had become (Redmine #12322): the closed
display vocabulary (:mod:`.constants`), the fail-closed error (:mod:`.errors`),
the generic shape validators (:mod:`.validation`), the authority / routing leak
guard (:mod:`.authority`), the typed schema records + membership-rule resolution
+ unit-override normalization (:mod:`.config`), the placement / group-window
resolver (:mod:`.placement`), and the degraded display classifier
(:mod:`.degraded`). The **current table / seed / migration** responsibilities are
deliberately *not* here — they live in
:mod:`mozyo_bridge.presentation_state` (Redmine #12304), which imports this
package's schema rather than the reverse.

The package is pure (dataclasses + validation helpers) and imports nothing from
the application layer, so the dependency only ever points within the domain.
This module re-exports the full public surface so existing
``from mozyo_bridge.domain.presentation_grouping import ...`` imports are
unchanged by the split.
"""

from __future__ import annotations

from .authority import _FORBIDDEN_KEY_PARTS
from .config import (
    GroupingDefaults,
    LaunchContext,
    MembershipRule,
    PresentationGroupingConfig,
    ProjectGroup,
    UnitOverride,
)
from .constants import (
    ALLOWED_PROJECTIONS,
    DEFAULT_DELEGATION_WINDOW_POLICY,
    DEFAULT_LANE,
    DEFAULT_PROJECT_GROUP_PRESENTATION,
    DELEGATION_WINDOW_POLICY_MODES,
    DELEGATION_WINDOW_POLICY_SEPARATE,
    DELEGATION_WINDOW_POLICY_SHARED,
    GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
    GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    GROUP_WINDOW_SURFACE_NORMAL_WINDOW,
    GROUPING_CONFIG_KEYS,
    GROUPING_DEFAULTS_KEYS,
    GROUPING_KEYS,
    MEMBERSHIP_PREDICATE_KEYS,
    MEMBERSHIP_RULE_KEYS,
    PRESENTATION_GROUPING_VERSION,
    PROJECT_GROUP_KEYS,
    PROJECT_GROUP_PRESENTATION_MODES,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
    STATUS_CONFIGURED,
    STATUS_DEFAULT,
    STATUS_DESIRED_UNIT_MISSING,
    STATUS_IDENTITY_CONFLICT,
    STATUS_UNGROUPED,
    UNIT_OVERRIDE_KEYS,
)
from .degraded import diagnose_unit_overrides
from .delegation_window import (
    DELEGATION_WINDOW_STATUS_DIAGNOSTIC,
    DELEGATION_WINDOW_STATUS_NONE,
    DELEGATION_WINDOW_STATUS_RESOLVED,
    DelegationWindowDisplay,
    resolve_delegation_window_display,
)
from .errors import PresentationGroupingConfigError
from .placement import (
    GroupPlacement,
    GroupWindowDecision,
    resolve_group_window_placement,
    resolve_launch_placement,
)

__all__ = (
    "PRESENTATION_GROUPING_VERSION",
    "GROUPING_CONFIG_KEYS",
    "PROJECT_GROUP_KEYS",
    "GROUPING_KEYS",
    "MEMBERSHIP_RULE_KEYS",
    "MEMBERSHIP_PREDICATE_KEYS",
    "UNIT_OVERRIDE_KEYS",
    "GROUPING_DEFAULTS_KEYS",
    "ALLOWED_PROJECTIONS",
    "DEFAULT_LANE",
    "PROJECT_GROUP_PRESENTATION_SAME_COLUMN",
    "PROJECT_GROUP_PRESENTATION_TMUX_WINDOW",
    "PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW",
    "PROJECT_GROUP_PRESENTATION_MODES",
    "DEFAULT_PROJECT_GROUP_PRESENTATION",
    "DELEGATION_WINDOW_POLICY_SEPARATE",
    "DELEGATION_WINDOW_POLICY_SHARED",
    "DELEGATION_WINDOW_POLICY_MODES",
    "DEFAULT_DELEGATION_WINDOW_POLICY",
    "DELEGATION_WINDOW_STATUS_NONE",
    "DELEGATION_WINDOW_STATUS_RESOLVED",
    "DELEGATION_WINDOW_STATUS_DIAGNOSTIC",
    "DelegationWindowDisplay",
    "resolve_delegation_window_display",
    "STATUS_DEFAULT",
    "STATUS_CONFIGURED",
    "STATUS_UNGROUPED",
    "STATUS_DESIRED_UNIT_MISSING",
    "STATUS_IDENTITY_CONFLICT",
    "GROUP_WINDOW_SURFACE_COCKPIT_COLUMN",
    "GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW",
    "GROUP_WINDOW_SURFACE_NORMAL_WINDOW",
    "PresentationGroupingConfigError",
    "ProjectGroup",
    "MembershipRule",
    "UnitOverride",
    "GroupingDefaults",
    "LaunchContext",
    "GroupPlacement",
    "GroupWindowDecision",
    "PresentationGroupingConfig",
    "resolve_launch_placement",
    "resolve_group_window_placement",
    "diagnose_unit_overrides",
)
