"""Typed schema records for the desired presentation grouping config (#12262).

This module owns the *config schema* boundary plus the per-record normalization
each schema type performs at construction:

- :class:`ProjectGroup` — a declared public-safe display grouping of Units;
- :class:`MembershipRule` — a declarative rule + its **membership rule
  resolution** (:meth:`MembershipRule.matches`);
- :class:`UnitOverride` — an explicit per-Unit display override + its **unit
  override normalization** (:meth:`UnitOverride.from_record`) and selector
  (:meth:`UnitOverride.selects`);
- :class:`GroupingDefaults` — display fallback only;
- :class:`LaunchContext` — the workspace / project / lane facts a launch is
  placed by, including identity-conflict detection;
- :class:`PresentationGroupingConfig` — the whole closed schema, with
  cross-reference validation.

Type-and-shape validators come from
:mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.validation`; the authority /
routing leak guard comes from
:mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.authority`. The placement
*resolver* and the degraded *classifier* are deliberately downstream
(:mod:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.placement` /
:mod:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.degraded`) so this module never
imports them — the dependency only ever points toward the leaves.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from .authority import (
    _optional_guarded_str,
    _reject_boundary_value,
    _reject_unknown_keys,
)
from .constants import (
    DEFAULT_DELEGATION_WINDOW_POLICY,
    DEFAULT_LANE,
    DEFAULT_PROJECT_GROUP_PRESENTATION,
    GROUPING_CONFIG_KEYS,
    GROUPING_DEFAULTS_KEYS,
    GROUPING_KEYS,
    MEMBERSHIP_PREDICATE_KEYS,
    MEMBERSHIP_RULE_KEYS,
    PROJECT_GROUP_KEYS,
    UNIT_OVERRIDE_KEYS,
)
from .errors import PresentationGroupingConfigError
from .validation import (
    _checked_delegation_window_policy,
    _checked_project_group_presentation,
    _checked_version,
    _optional_bool,
    _optional_int,
    _optional_projection,
    _optional_str,
    _require_mapping,
    _require_sequence,
    _required_str,
)


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
    #: Desired window-separation policy for a delegated-coordinator tree (#12467).
    #: ``separate`` (default) projects a delegated coordinator and its grandchild
    #: worker to distinct windows; ``shared`` folds them into one display group.
    #: Display-only metadata, never routing / approval / send-preflight authority.
    delegation_window_policy: str = DEFAULT_DELEGATION_WINDOW_POLICY

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
        delegation_window_policy = _checked_delegation_window_policy(
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
            delegation_window_policy=delegation_window_policy,
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
