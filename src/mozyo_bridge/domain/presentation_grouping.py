"""Desired presentation grouping schema + launch-placement resolver (Redmine #12263).

This is the typed *schema boundary* and the pure *placement resolver* for the
repo-local **desired presentation grouping** config — the cockpit Project Group
layer whose field contract was fixed (docs-only) by Redmine #12262 in
``vibes/docs/logics/unit-presentation-state-db.md`` and whose projection model
(Project Group -> Unit -> Target) was fixed by Redmine #12253 in
``vibes/docs/logics/unit-target-model.md``. Those predecessors defined the
shape; this module is the first code that *parses* that shape and *resolves*
which Project Group a launching sublane is displayed under, from its
workspace / project / lane context.

It is the grouping analogue of
:class:`~mozyo_bridge.domain.repo_local_config.PresentationSelectionConfig`
(Redmine #12189), which selects a projection *surface*. Grouping is a separate
concern — *which display group a Unit belongs to* — so it is its own typed
record here rather than folded into the surface-selection record. Wiring this
config into the on-disk ``.mozyo-bridge/config.yaml`` loader and the live
cockpit append path is a later code task (it precedes Redmine #12264); this lane
delivers only the in-memory schema + resolver, mirroring the staged discipline
of the surface-selection lanes (#12189 schema -> #12190 load -> #12191 wire).

Boundary, kept enforced in code:

- **Display grouping only — never routing / approval / liveness authority.** The
  resolver maps context to a *desired* Project Group and view preferences. It
  resolves no handoff target, asserts no liveness, and grants no owner approval /
  review / close authority. Routing stays with the live resolver / pane preflight
  (the Start Gate's "routing target は live resolver / pane preflight に委ねる").
- **Default / missing config is behavior-preserving.** ``None`` resolves through
  :meth:`resolve_launch_placement` to a default placement keyed on the public
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

The module is pure (dataclasses + validation helpers) and imports nothing from
the application layer, so the dependency only ever points within the domain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional

#: The supported grouping config record version. ``version`` is optional and
#: defaults to this; any other value is rejected so a future, not-yet-understood
#: schema never reads as version 1.
PRESENTATION_GROUPING_VERSION: int = 1

#: Closed top-level keys of the desired presentation grouping record.
GROUPING_CONFIG_KEYS: frozenset[str] = frozenset(
    {"version", "project_groups", "grouping", "project_group_presentation"}
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

#: The default lane id every non-lane construction lands on (mirrors
#: :data:`mozyo_bridge.domain.cockpit_layout.DEFAULT_LANE`). Kept as a local
#: constant so this domain module imports nothing from the cockpit layout layer.
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

#: Map each desired ``project_group_presentation`` mode to its placement surface.
_PRESENTATION_MODE_TO_SURFACE: "dict[str, str]" = {
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN: GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW: GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW: GROUP_WINDOW_SURFACE_NORMAL_WINDOW,
}

#: Substrings in a config key that signal an attempt to cross a boundary this
#: surface does not own: load / execute code, name a module / callable / entry
#: point, grant or alter authority / approval / routing / send safety, address a
#: target / pane / route, or carry a credential. Scanned only against *unknown*
#: keys (every allowed key in this module is curated boundary-safe), so it gives
#: a deliberate-looking audit message without false-positiving on legitimate
#: display keys such as ``description``.
_FORBIDDEN_KEY_PARTS: tuple[str, ...] = (
    "import",
    "module",
    "callable",
    "entry",
    "plugin",
    "exec",
    "eval",
    "load",
    "authority",
    "approval",
    "approve",
    "grant",
    "owner",
    "review",
    "close",
    "routing",
    "route",
    "send",
    "target",
    "pane",
    "secret",
    "token",
    "password",
    "credential",
    "command",
    "script",
)


class PresentationGroupingConfigError(ValueError):
    """The desired presentation grouping record violates the closed schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    domain error :class:`~mozyo_bridge.domain.repo_local_config.RepoLocalConfigError`.
    """


def _reject_unknown_keys(
    record: "Mapping[object, object]", *, allowed: "frozenset[str]", source: str
) -> None:
    """Fail closed on a non-string / boundary-shaped / unknown record key.

    Keys must be non-empty strings; a key outside ``allowed`` whose name carries
    a :data:`_FORBIDDEN_KEY_PARTS` token is rejected with a boundary-specific
    message (so smuggling a ``target`` / ``route`` / ``approval`` key reads as a
    deliberate rejection in an audit); any other key outside ``allowed`` is a
    plain unknown-key rejection (closed schema / typo protection).
    """
    for key in record:
        if not isinstance(key, str) or not key:
            raise PresentationGroupingConfigError(
                f"{source} record keys must be non-empty strings; got {key!r}"
            )
        if key in allowed:
            continue
        lowered = key.lower()
        for part in _FORBIDDEN_KEY_PARTS:
            if part in lowered:
                raise PresentationGroupingConfigError(
                    f"{source} record key {key!r} may not carry a boundary token: "
                    f"grouping config is display-only and may never load code, "
                    f"address a target / pane / route, grant authority, or carry a "
                    f"credential (matched forbidden token {part!r})."
                )
        raise PresentationGroupingConfigError(
            f"{source} record has unknown key {key!r}; allowed keys: {sorted(allowed)}"
        )


def _reject_boundary_value(value: object, *, source: str, field_name: str) -> None:
    """Fail closed on a boundary-shaped string *value* in an identity / diagnostic field.

    ``unit-presentation-state-db.md`` validation marks a boundary-shaped
    *key/value* — not only a key — invalid config. The portable group keys
    (``group_id`` / ``preferred_group`` / ``missing_group`` /
    ``unknown_unit_group``) are stable join / pointer keys, and
    ``degraded_display`` is operator-facing diagnostic text; like a key, none of
    them may name a ``target`` / ``pane`` / ``route`` / ``send`` / ``approval`` /
    ``credential`` / ``module`` boundary (the same :data:`_FORBIDDEN_KEY_PARTS`
    vocabulary). A non-string value is ignored (the field's own type check
    handles it). Free public display text — ``label`` / ``description`` /
    ``label_override`` — is deliberately *not* scanned here: it is inert prose,
    not an identity or diagnostic key, and legitimately contains words such as
    "review" or "closed".
    """
    if not isinstance(value, str):
        return
    lowered = value.lower()
    for part in _FORBIDDEN_KEY_PARTS:
        if part in lowered:
            raise PresentationGroupingConfigError(
                f"{source} '{field_name}' value {value!r} may not carry a boundary "
                f"token: a grouping key / diagnostic is display-only and may never "
                f"name a target / pane / route, grant authority, or carry a "
                f"credential (matched forbidden token {part!r})."
            )


def _optional_guarded_str(
    value: object, *, source: str, field_name: str
) -> Optional[str]:
    """An optional non-empty string that is also boundary-token guarded.

    For identity / pointer keys (``group_id`` and the group references) and
    operator-facing diagnostic text (``degraded_display``): the value must be a
    public-safe display string carrying no boundary token. Free naming prose
    (``label`` / ``description`` / ``label_override``) uses the plain
    :func:`_optional_str` instead — see :func:`_reject_boundary_value`.
    """
    text = _optional_str(value, source=source, field_name=field_name)
    _reject_boundary_value(text, source=source, field_name=field_name)
    return text


def _require_mapping(value: object, *, source: str) -> "Mapping[object, object]":
    if not isinstance(value, Mapping):
        raise PresentationGroupingConfigError(
            f"{source} must be a mapping (a YAML table), got {type(value).__name__}"
        )
    return value


def _require_sequence(value: object, *, source: str) -> "list[object]":
    """Accept a YAML list (but not a bare string/mapping) as a sequence."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise PresentationGroupingConfigError(
            f"{source} must be a list, got {type(value).__name__}"
        )
    return list(value)


def _checked_version(record: "Mapping[object, object]", *, source: str) -> int:
    version = record.get("version", PRESENTATION_GROUPING_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise PresentationGroupingConfigError(
            f"{source} 'version' must be an integer, got {version!r}"
        )
    if version != PRESENTATION_GROUPING_VERSION:
        raise PresentationGroupingConfigError(
            f"unsupported {source} version {version!r}; this build understands "
            f"version {PRESENTATION_GROUPING_VERSION}"
        )
    return version


def _required_str(value: object, *, source: str, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PresentationGroupingConfigError(
            f"{source} '{field_name}' must be a non-empty string, got {value!r}"
        )
    return value


def _optional_str(value: object, *, source: str, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PresentationGroupingConfigError(
            f"{source} '{field_name}' must be a non-empty string when present, "
            f"got {value!r}"
        )
    return value


def _optional_bool(value: object, *, source: str, field_name: str) -> Optional[bool]:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise PresentationGroupingConfigError(
            f"{source} '{field_name}' must be a boolean when present, got {value!r}"
        )
    return value


def _optional_int(value: object, *, source: str, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PresentationGroupingConfigError(
            f"{source} '{field_name}' must be an integer when present, got {value!r}"
        )
    return value


def _optional_projection(
    value: object, *, source: str, field_name: str
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in ALLOWED_PROJECTIONS:
        raise PresentationGroupingConfigError(
            f"{source} '{field_name}' must be a built-in projection "
            f"{sorted(ALLOWED_PROJECTIONS)} when present, got {value!r}"
        )
    return value


def _checked_project_group_presentation(
    record: "Mapping[object, object]", *, source: str
) -> str:
    """Return the desired Project-Group display-placement mode, fail-closed.

    ``project_group_presentation`` is optional and defaults to
    :data:`DEFAULT_PROJECT_GROUP_PRESENTATION` (``same_cockpit_column``), so a
    missing field preserves current behavior exactly. Any value outside
    :data:`PROJECT_GROUP_PRESENTATION_MODES` — including a boundary- / authority-
    shaped string — is rejected rather than silently normalized; the mode is a
    closed display-only vocabulary, never a routing / approval target.
    """
    value = record.get(
        "project_group_presentation", DEFAULT_PROJECT_GROUP_PRESENTATION
    )
    if not isinstance(value, str) or value not in PROJECT_GROUP_PRESENTATION_MODES:
        raise PresentationGroupingConfigError(
            f"{source} 'project_group_presentation' must be one of "
            f"{sorted(PROJECT_GROUP_PRESENTATION_MODES)} when present, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class ProjectGroup:
    """A declared Project Group: a public-safe display grouping of Units."""

    group_id: str
    label: str
    sort_key: Optional[object] = None  # int or str display order
    collapsed: Optional[bool] = None
    description: Optional[str] = None

    @classmethod
    def from_record(cls, record: object, *, source: str) -> "ProjectGroup":
        mapping = _require_mapping(record, source=source)
        _reject_unknown_keys(mapping, allowed=PROJECT_GROUP_KEYS, source=source)
        group_id = _required_str(
            mapping.get("group_id"), source=source, field_name="group_id"
        )
        # group_id is a stable portable join / pointer key, so a target / pane /
        # route / credential-shaped value is a boundary leak (a key that may not
        # carry a boundary token, per unit-presentation-state-db.md validation).
        # ``label`` / ``description`` are free public display prose and are not
        # token-scanned (see _reject_boundary_value).
        _reject_boundary_value(group_id, source=source, field_name="group_id")
        label = _required_str(mapping.get("label"), source=source, field_name="label")
        sort_key = mapping.get("sort_key")
        if sort_key is not None and (
            isinstance(sort_key, bool) or not isinstance(sort_key, (int, str))
        ):
            raise PresentationGroupingConfigError(
                f"{source} 'sort_key' must be an integer or string when present, "
                f"got {sort_key!r}"
            )
        return cls(
            group_id=group_id,
            label=label,
            sort_key=sort_key,
            collapsed=_optional_bool(
                mapping.get("collapsed"), source=source, field_name="collapsed"
            ),
            description=_optional_str(
                mapping.get("description"), source=source, field_name="description"
            ),
        )


@dataclass(frozen=True)
class MembershipRule:
    """A declarative rule deriving a group from public-safe context facts."""

    when: "tuple[tuple[str, str], ...]"  # predicate (key, value) pairs, AND-ed
    group_id: Optional[str] = None
    position: Optional[int] = None
    pinned: Optional[bool] = None
    hidden: Optional[bool] = None
    preferred_projection: Optional[str] = None

    @classmethod
    def from_record(cls, record: object, *, source: str) -> "MembershipRule":
        mapping = _require_mapping(record, source=source)
        _reject_unknown_keys(mapping, allowed=MEMBERSHIP_RULE_KEYS, source=source)
        when_block = mapping.get("when", {})
        when_map = _require_mapping(when_block, source=f"{source} 'when'")
        _reject_unknown_keys(
            when_map, allowed=MEMBERSHIP_PREDICATE_KEYS, source=f"{source} 'when'"
        )
        predicates: list[tuple[str, str]] = []
        for pkey in sorted(when_map):  # deterministic order; matching is AND
            predicates.append(
                (
                    pkey,
                    _required_str(
                        when_map.get(pkey), source=f"{source} 'when'", field_name=pkey
                    ),
                )
            )
        return cls(
            when=tuple(predicates),
            group_id=_optional_guarded_str(
                mapping.get("group_id"), source=source, field_name="group_id"
            ),
            position=_optional_int(
                mapping.get("position"), source=source, field_name="position"
            ),
            pinned=_optional_bool(
                mapping.get("pinned"), source=source, field_name="pinned"
            ),
            hidden=_optional_bool(
                mapping.get("hidden"), source=source, field_name="hidden"
            ),
            preferred_projection=_optional_projection(
                mapping.get("preferred_projection"),
                source=source,
                field_name="preferred_projection",
            ),
        )

    def matches(self, context: "LaunchContext") -> bool:
        """True when every ``when`` predicate is satisfied by ``context``.

        A predicate whose context fact is unknown (``None``) cannot match, so a
        rule never fires on a fact the launch context does not actually carry. An
        empty ``when`` matches everything (an explicit catch-all rule).
        """
        for pkey, pvalue in self.when:
            if pkey == "lane_prefix":
                lane = context.lane_id
                if lane is None or not lane.startswith(pvalue):
                    return False
                continue
            actual = context.predicate_fact(pkey)
            if actual is None or actual != pvalue:
                return False
        return True


@dataclass(frozen=True)
class UnitOverride:
    """An explicit desired display override for one known Unit."""

    workspace_id: str
    lane_id: str
    host_id: Optional[str] = None
    preferred_group: Optional[str] = None
    position: Optional[int] = None
    pinned: Optional[bool] = None
    hidden: Optional[bool] = None
    preferred_projection: Optional[str] = None
    label_override: Optional[str] = None

    @classmethod
    def from_record(cls, record: object, *, source: str) -> "UnitOverride":
        mapping = _require_mapping(record, source=source)
        _reject_unknown_keys(mapping, allowed=UNIT_OVERRIDE_KEYS, source=source)
        return cls(
            workspace_id=_required_str(
                mapping.get("workspace_id"), source=source, field_name="workspace_id"
            ),
            lane_id=_required_str(
                mapping.get("lane_id"), source=source, field_name="lane_id"
            ),
            host_id=_optional_str(
                mapping.get("host_id"), source=source, field_name="host_id"
            ),
            preferred_group=_optional_guarded_str(
                mapping.get("preferred_group"),
                source=source,
                field_name="preferred_group",
            ),
            position=_optional_int(
                mapping.get("position"), source=source, field_name="position"
            ),
            pinned=_optional_bool(
                mapping.get("pinned"), source=source, field_name="pinned"
            ),
            hidden=_optional_bool(
                mapping.get("hidden"), source=source, field_name="hidden"
            ),
            preferred_projection=_optional_projection(
                mapping.get("preferred_projection"),
                source=source,
                field_name="preferred_projection",
            ),
            label_override=_optional_str(
                mapping.get("label_override"),
                source=source,
                field_name="label_override",
            ),
        )

    def selects(self, context: "LaunchContext") -> bool:
        """True when this override's identity selector matches ``context``.

        ``host_id`` is matched only when the override constrains it; an override
        with no ``host_id`` applies regardless of host (the common single-host
        case).
        """
        if self.host_id is not None and self.host_id != context.host_id:
            return False
        return (
            self.workspace_id == context.workspace_id
            and self.lane_id == context.lane_id
        )


@dataclass(frozen=True)
class GroupingDefaults:
    """Display fallback only — never identity / routing / workflow invention."""

    missing_group: Optional[str] = None
    unknown_unit_group: Optional[str] = None
    collapsed: Optional[bool] = None
    preferred_projection: Optional[str] = None
    degraded_display: Optional[str] = None

    @classmethod
    def from_record(cls, record: object, *, source: str) -> "GroupingDefaults":
        mapping = _require_mapping(record, source=source)
        _reject_unknown_keys(mapping, allowed=GROUPING_DEFAULTS_KEYS, source=source)
        return cls(
            missing_group=_optional_guarded_str(
                mapping.get("missing_group"), source=source, field_name="missing_group"
            ),
            unknown_unit_group=_optional_guarded_str(
                mapping.get("unknown_unit_group"),
                source=source,
                field_name="unknown_unit_group",
            ),
            collapsed=_optional_bool(
                mapping.get("collapsed"), source=source, field_name="collapsed"
            ),
            preferred_projection=_optional_projection(
                mapping.get("preferred_projection"),
                source=source,
                field_name="preferred_projection",
            ),
            # degraded_display is operator-facing diagnostic text surfaced in the
            # read-model status channel, so it is boundary-token guarded too (per
            # the review of #12263).
            degraded_display=_optional_guarded_str(
                mapping.get("degraded_display"),
                source=source,
                field_name="degraded_display",
            ),
        )


@dataclass(frozen=True)
class LaunchContext:
    """The workspace / project / lane facts a sublane launch is placed by.

    Identity facts come from the registry / workspace anchor and the lane the
    launch targets. ``observed_workspace_id`` / ``observed_lane_id`` are *optional*
    live-preflight observations; when present and contradicting the launch
    identity they drive a visible ``identity_conflict`` placement — they never
    grant routing authority, and the action-time preflight still decides any side
    effect.
    """

    workspace_id: str
    lane_id: str = DEFAULT_LANE
    host_id: str = "local"
    repo_label: Optional[str] = None
    project_id: Optional[str] = None
    fixed_version_id: Optional[str] = None
    observed_workspace_id: Optional[str] = None
    observed_lane_id: Optional[str] = None

    def predicate_fact(self, key: str) -> Optional[str]:
        """The public-safe fact a membership predicate keyed ``key`` matches."""
        return {
            "workspace_id": self.workspace_id,
            "repo_label": self.repo_label,
            "project_id": self.project_id,
            "fixed_version_id": self.fixed_version_id,
            "lane_id": self.lane_id,
        }.get(key)

    def has_identity_conflict(self) -> bool:
        """True when a live observation contradicts the launch identity."""
        if (
            self.observed_workspace_id is not None
            and self.observed_workspace_id != self.workspace_id
        ):
            return True
        if (
            self.observed_lane_id is not None
            and self.observed_lane_id != self.lane_id
        ):
            return True
        return False


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


@dataclass(frozen=True)
class PresentationGroupingConfig:
    """The desired presentation grouping config (schema only).

    Parses and pins the #12262 field contract. Cross-references are validated at
    construction: every group a rule / override / default names must be declared
    in :attr:`project_groups`, and ``group_id`` is unique — a dangling or
    duplicate reference fails closed rather than producing a silently empty group.
    """

    project_groups: "tuple[ProjectGroup, ...]" = ()
    membership_rules: "tuple[MembershipRule, ...]" = ()
    unit_overrides: "tuple[UnitOverride, ...]" = ()
    defaults: GroupingDefaults = field(default_factory=GroupingDefaults)
    #: Desired display-placement of the whole Project Group view (#12286). A
    #: behavior-preserving ``same_cockpit_column`` by default; display-only
    #: metadata, never routing / approval / window guarantee.
    project_group_presentation: str = DEFAULT_PROJECT_GROUP_PRESENTATION

    @classmethod
    def default(cls) -> "PresentationGroupingConfig":
        """The behavior-preserving empty grouping config."""
        return cls()

    def group_ids(self) -> "frozenset[str]":
        return frozenset(group.group_id for group in self.project_groups)

    def group(self, group_id: Optional[str]) -> Optional[ProjectGroup]:
        if group_id is None:
            return None
        for group in self.project_groups:
            if group.group_id == group_id:
                return group
        return None

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "PresentationGroupingConfig":
        """Normalize a parsed grouping record into a typed config.

        ``None`` or an empty mapping yields the behavior-preserving empty config,
        so a missing grouping block never changes how a sublane is placed. Fails
        closed on a non-mapping record, an unsupported version, a boundary-shaped
        or unknown key, a duplicate ``group_id``, or a rule / override / default
        that references an undeclared group.
        """
        if record is None:
            return cls.default()
        mapping = _require_mapping(record, source="grouping config")
        if not mapping:
            return cls.default()
        _reject_unknown_keys(
            mapping, allowed=GROUPING_CONFIG_KEYS, source="grouping config"
        )
        _checked_version(mapping, source="grouping config")
        project_group_presentation = _checked_project_group_presentation(
            mapping, source="grouping config"
        )

        project_groups: list[ProjectGroup] = []
        seen_group_ids: set[str] = set()
        for index, entry in enumerate(
            _require_sequence(
                mapping.get("project_groups", []), source="grouping config 'project_groups'"
            )
        ):
            group = ProjectGroup.from_record(
                entry, source=f"project_groups[{index}]"
            )
            if group.group_id in seen_group_ids:
                raise PresentationGroupingConfigError(
                    f"duplicate group_id {group.group_id!r} in project_groups"
                )
            seen_group_ids.add(group.group_id)
            project_groups.append(group)

        membership_rules: list[MembershipRule] = []
        unit_overrides: list[UnitOverride] = []
        defaults = GroupingDefaults()
        if "grouping" in mapping:
            grouping = _require_mapping(
                mapping["grouping"], source="grouping config 'grouping'"
            )
            _reject_unknown_keys(
                grouping, allowed=GROUPING_KEYS, source="grouping config 'grouping'"
            )
            for index, entry in enumerate(
                _require_sequence(
                    grouping.get("membership_rules", []),
                    source="grouping 'membership_rules'",
                )
            ):
                membership_rules.append(
                    MembershipRule.from_record(
                        entry, source=f"membership_rules[{index}]"
                    )
                )
            for index, entry in enumerate(
                _require_sequence(
                    grouping.get("unit_overrides", []),
                    source="grouping 'unit_overrides'",
                )
            ):
                unit_overrides.append(
                    UnitOverride.from_record(entry, source=f"unit_overrides[{index}]")
                )
            if "defaults" in grouping:
                defaults = GroupingDefaults.from_record(
                    grouping["defaults"], source="grouping 'defaults'"
                )

        config = cls(
            project_groups=tuple(project_groups),
            membership_rules=tuple(membership_rules),
            unit_overrides=tuple(unit_overrides),
            defaults=defaults,
            project_group_presentation=project_group_presentation,
        )
        config._validate_group_references()
        return config

    def _validate_group_references(self) -> None:
        """Fail closed when a rule / override / default names an undeclared group.

        A dangling reference is config error, not degraded display: the schema
        author named a group that does not exist, which the fallback matrix marks
        invalid config (there is no ``unknown_group`` degraded display in this
        build). Live-target drift is handled separately at resolution time.
        """
        known = self.group_ids()
        for index, rule in enumerate(self.membership_rules):
            if rule.group_id is not None and rule.group_id not in known:
                raise PresentationGroupingConfigError(
                    f"membership_rules[{index}] references unknown group "
                    f"{rule.group_id!r}; declared groups: {sorted(known)}"
                )
        for index, override in enumerate(self.unit_overrides):
            if (
                override.preferred_group is not None
                and override.preferred_group not in known
            ):
                raise PresentationGroupingConfigError(
                    f"unit_overrides[{index}] references unknown group "
                    f"{override.preferred_group!r}; declared groups: {sorted(known)}"
                )
        for field_name in ("missing_group", "unknown_unit_group"):
            value = getattr(self.defaults, field_name)
            if value is not None and value not in known:
                raise PresentationGroupingConfigError(
                    f"grouping defaults '{field_name}' references unknown group "
                    f"{value!r}; declared groups: {sorted(known)}"
                )


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


def diagnose_unit_overrides(
    config: PresentationGroupingConfig,
    known_units: "frozenset[tuple[str, str]]",
) -> "tuple[tuple[UnitOverride, str], ...]":
    """Flag config overrides whose Unit is not among the known live Units.

    ``known_units`` is the set of ``(workspace_id, lane_id)`` the read model has
    actually observed. An override selecting a Unit outside that set is a visible
    ``desired_unit_missing`` degraded condition (the fallback matrix), surfaced so
    the read model can display it rather than silently dropping it. This is a
    read-model diagnostic only — it resolves no routing and decides no side effect.
    """
    flagged: list[tuple[UnitOverride, str]] = []
    for override in config.unit_overrides:
        if (override.workspace_id, override.lane_id) not in known_units:
            flagged.append((override, STATUS_DESIRED_UNIT_MISSING))
    return tuple(flagged)


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
    "PROJECT_GROUP_PRESENTATION_SAME_COLUMN",
    "PROJECT_GROUP_PRESENTATION_TMUX_WINDOW",
    "PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW",
    "PROJECT_GROUP_PRESENTATION_MODES",
    "DEFAULT_PROJECT_GROUP_PRESENTATION",
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
