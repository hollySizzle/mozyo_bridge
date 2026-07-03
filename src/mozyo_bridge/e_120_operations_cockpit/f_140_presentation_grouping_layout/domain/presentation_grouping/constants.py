"""Closed display-only vocabulary for the desired presentation grouping config.

Pure data only — version, the closed key sets of every record level, the
built-in projection / Project-Group presentation / placement-surface
vocabularies, the placement status values, and the default lane. Every other
submodule imports its vocabulary from here so the schema, validators, authority
guard, resolver, and degraded classifier never disagree on the allowed values.

This module holds **no logic and no boundary decisions** (those live in
``validation`` / ``authority`` / ``config`` / ``placement`` / ``degraded``); it
is the single leaf the rest of the package depends on.
"""

from __future__ import annotations

#: The supported grouping config record version. ``version`` is optional and
#: defaults to this; any other value is rejected so a future, not-yet-understood
#: schema never reads as version 1.
PRESENTATION_GROUPING_VERSION: int = 1

#: Closed top-level keys of the desired presentation grouping record.
GROUPING_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "project_groups",
        "grouping",
        "project_group_presentation",
        "delegation_window_policy",
    }
)

#: Closed keys of one ``project_groups[]`` entry (#12262 schema field contract).
PROJECT_GROUP_KEYS: frozenset[str] = frozenset(
    {"group_id", "label", "sort_key", "collapsed", "description"}
)

#: Closed keys of the ``grouping`` block.
GROUPING_KEYS: frozenset[str] = frozenset(
    {"membership_rules", "unit_overrides", "defaults"}
)

#: Closed keys of one ``membership_rules[]`` entry.
MEMBERSHIP_RULE_KEYS: frozenset[str] = frozenset(
    {"when", "group_id", "position", "pinned", "hidden", "preferred_projection"}
)

#: Public-safe facts a rule ``when`` predicate may match. Each is derivable from
#: registry / repo-local metadata without reading live pane identity. Naming any
#: other predicate (a module, callable, target, or route) fails closed.
MEMBERSHIP_PREDICATE_KEYS: frozenset[str] = frozenset(
    {
        "workspace_id",
        "repo_label",
        "project_id",
        "fixed_version_id",
        "lane_id",
        "lane_prefix",
    }
)

#: Closed keys of one ``unit_overrides[]`` entry. ``workspace_id`` + ``lane_id``
#: (+ optional ``host_id``) are the selector; the rest are desired display fields.
UNIT_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        "workspace_id",
        "lane_id",
        "host_id",
        "preferred_group",
        "position",
        "pinned",
        "hidden",
        "preferred_projection",
        "label_override",
    }
)

#: Closed keys of the ``grouping.defaults`` block (display fallback only).
GROUPING_DEFAULTS_KEYS: frozenset[str] = frozenset(
    {
        "missing_group",
        "unknown_unit_group",
        "collapsed",
        "preferred_projection",
        "degraded_display",
    }
)

#: The built-in projections a Unit may prefer. ``cockpit_pane`` is primary;
#: ``normal_window`` is the retained compatibility projection
#: (``unit-presentation-state-db.md`` ``projection_preferences``). A config may
#: *prefer* one of these; it may never invent a new projection.
ALLOWED_PROJECTIONS: frozenset[str] = frozenset({"cockpit_pane", "normal_window"})

#: Desired *display-placement* modes for a whole Project Group view (Redmine
#: #12286 / #12290, ``unit-target-model.md`` ``#### Project Group tmux-window
#: presentation``). This is presentation-only metadata describing *how* the
#: Project Group layer is desired to be laid out for the operator — it is never
#: routing / approval / close authority and never a guaranteed window / tab / OS
#: result:
#:
#: - ``same_cockpit_column`` (the default) keeps the current single cockpit
#:   column — the behavior-preserving placement;
#: - ``project_group_tmux_window`` is an opt-in request for one tmux window per
#:   Project Group. In some iTerm2 control-mode builds that renders as a native
#:   tab, but mozyo never *guarantees* tab / OS-window behavior — it requests a
#:   tmux-layer window only;
#: - ``normal_window`` is the retained compatibility projection.
#:
#: It is deliberately a *separate* field from a Unit's ``preferred_projection``
#: (``cockpit_pane`` / ``normal_window``): that selects a Unit projection
#: *surface*, this selects Project-Group *display placement*. Naming any other
#: value fails closed.
PROJECT_GROUP_PRESENTATION_SAME_COLUMN: str = "same_cockpit_column"
PROJECT_GROUP_PRESENTATION_TMUX_WINDOW: str = "project_group_tmux_window"
PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW: str = "normal_window"
PROJECT_GROUP_PRESENTATION_MODES: frozenset[str] = frozenset(
    {
        PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
        PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
        PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
    }
)

#: Missing ``project_group_presentation`` preserves current behavior exactly: a
#: single cockpit column.
DEFAULT_PROJECT_GROUP_PRESENTATION: str = PROJECT_GROUP_PRESENTATION_SAME_COLUMN

#: Desired *window-separation policy* for a delegated-coordinator tree (Redmine
#: #12467, ``delegated-coordinator-cockpit-display.md`` ``## window 分離方針``).
#: This is presentation-only metadata describing whether a delegated coordinator
#: and its grandchild implementation lane are *desired* to be projected into
#: separate cockpit windows / columns or shared into one — it is never routing /
#: approval / close authority, never a guaranteed window / tab / OS result, and
#: never used by handoff target selection, the role resolver, ``--target-repo``,
#: or send preflight. It is a display knob in the existing
#: ``unit_overrides`` / ``defaults`` family and adds no new authority key:
#:
#: - ``separate`` (the default) keeps the documented behavior — a delegated
#:   coordinator (depth 1) and its grandchild worker (depth 2) each project to
#:   their own window so a ``callback_due`` / ``review_waiting`` coordinator row
#:   and an ``implementing`` worker row stay independently readable;
#: - ``shared`` is an opt-in request to fold the delegated coordinator and its
#:   grandchild into one display group. Choosing ``shared`` never relaxes the
#:   fixed invariants (cross-lane handoff still routes through the target-lane
#:   Codex gateway; owner approval still aggregates to the parent coordinator;
#:   durable callback requirements still hold).
#:
#: Naming any other value fails closed.
DELEGATION_WINDOW_POLICY_SEPARATE: str = "separate"
DELEGATION_WINDOW_POLICY_SHARED: str = "shared"
DELEGATION_WINDOW_POLICY_MODES: frozenset[str] = frozenset(
    {
        DELEGATION_WINDOW_POLICY_SEPARATE,
        DELEGATION_WINDOW_POLICY_SHARED,
    }
)

#: Missing ``delegation_window_policy`` preserves the documented default: a
#: delegated coordinator and its grandchild worker project to separate windows.
DEFAULT_DELEGATION_WINDOW_POLICY: str = DELEGATION_WINDOW_POLICY_SEPARATE

#: The default lane id every non-lane construction lands on (mirrors
#: :data:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout.DEFAULT_LANE`). Kept as a local
#: constant so this domain package imports nothing from the cockpit layout layer.
DEFAULT_LANE: str = "default"

#: Placement status values. ``configured`` / ``default`` / ``ungrouped`` are
#: ordinary outcomes; ``desired_unit_missing`` / ``identity_conflict`` are
#: *visible* degraded outcomes that never silently reroute.
STATUS_DEFAULT: str = "default"
STATUS_CONFIGURED: str = "configured"
STATUS_UNGROUPED: str = "ungrouped"
STATUS_DESIRED_UNIT_MISSING: str = "desired_unit_missing"
STATUS_IDENTITY_CONFLICT: str = "identity_conflict"

#: The launcher / cockpit-append placement *surface* a desired
#: ``project_group_presentation`` mode maps to (Redmine #12302). These name *how*
#: a launching sublane is laid out, never a routing / approval target:
#:
#: - ``cockpit_column`` — the behavior-preserving single shared cockpit column
#:   (the only surface the current single-window cockpit append executes);
#: - ``group_tmux_window`` — the desired per-Project-Group tmux window (#12290);
#:   a *desired* presentation only, never a guaranteed window / iTerm tab;
#: - ``normal_window`` — the retained compatibility projection (a normal,
#:   non-cockpit window).
GROUP_WINDOW_SURFACE_COCKPIT_COLUMN: str = "cockpit_column"
GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW: str = "group_tmux_window"
GROUP_WINDOW_SURFACE_NORMAL_WINDOW: str = "normal_window"

#: The sublane separate-window placement *surface* (Redmine #13015): a launching
#: sublane (a non-default lane — worktree / clone / relocated checkout) laid out
#: in its own explicit sublane tmux window under
#: ``delegation_window_policy: separate``, completing the expected
#: ``cockpit main window -> project window -> sublane window`` topology. Like the
#: other surfaces this names *how* the sublane is laid out — a tmux-layer request
#: only, never routing / approval / close authority and never a guaranteed
#: window / iTerm tab / OS window.
GROUP_WINDOW_SURFACE_LANE_TMUX_WINDOW: str = "lane_tmux_window"

#: Deterministic window-key prefix for a sublane's own window (Redmine #13015).
#: The launcher stamps ``lane:<workspace_id>/<lane_id>`` as the window-level
#: ``@mozyo_group_id`` marker so it can relocate the sublane's existing window
#: without trusting window names; the ``lane:`` prefix namespaces these keys away
#: from configured Project Group ids. Display grouping only — never Unit identity
#: (that stays on the pane options) and never a routing key.
SUBLANE_WINDOW_KEY_PREFIX: str = "lane:"

#: Map each desired ``project_group_presentation`` mode to its placement surface.
_PRESENTATION_MODE_TO_SURFACE: "dict[str, str]" = {
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN: GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW: GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW: GROUP_WINDOW_SURFACE_NORMAL_WINDOW,
}
