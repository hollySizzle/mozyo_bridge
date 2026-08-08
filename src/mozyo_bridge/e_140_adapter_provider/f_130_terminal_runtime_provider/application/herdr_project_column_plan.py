"""Desired order and weighted-width plan for authorized Herdr project columns.

This is the non-mutating planning slice of Redmine #14606.  It combines the
existing repo-local presentation declaration with an already observed Unit
order, but it does not read or write pane geometry and does not decide Unit
identity.  A later actuator must use ``ProjectColumnAuthority`` and revalidate
the live layout before turning this plan into pane operations.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.core.state.workspace_registry import (
    WorkspaceRecord,
    load_workspace_by_id,
    probe_canonical_liveness,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (  # noqa: E501
    LaunchContext,
    PresentationGroupingConfigError,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.placement import (  # noqa: E501
    resolve_unit_column_preferences,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
    RepoLocalConfigError,
)

QUALITY_EXACT = "exact"
QUALITY_BEST_EFFORT = "best_effort"
QUALITY_DEGRADED = "degraded"

REASON_POSITION_UNSPECIFIED = "position_unspecified"
REASON_POSITION_TIE = "position_tie"
REASON_WIDTH_UNSPECIFIED = "width_unspecified"
REASON_CONFIG_MISSING = "config_missing"
REASON_WORKSPACE_UNRESOLVED = "workspace_unresolved"
REASON_CONFIG_INVALID = "config_invalid"
REASON_RULE_CONTEXT_INCOMPLETE = "rule_context_incomplete"
REASON_UNIT_INPUT_INVALID = "unit_input_invalid"
REASON_RATIO_UNREPRESENTABLE = "ratio_unrepresentable"

_REASON_ORDER = (
    REASON_WORKSPACE_UNRESOLVED,
    REASON_CONFIG_INVALID,
    REASON_RULE_CONTEXT_INCOMPLETE,
    REASON_UNIT_INPUT_INVALID,
    REASON_RATIO_UNREPRESENTABLE,
    REASON_CONFIG_MISSING,
    REASON_POSITION_UNSPECIFIED,
    REASON_POSITION_TIE,
    REASON_WIDTH_UNSPECIFIED,
)
_DEGRADED_REASONS = frozenset(
    {
        REASON_WORKSPACE_UNRESOLVED,
        REASON_CONFIG_INVALID,
        REASON_RULE_CONTEXT_INCOMPLETE,
        REASON_UNIT_INPUT_INVALID,
        REASON_RATIO_UNREPRESENTABLE,
    }
)

# Measured Herdr 0.8 divider bounds.  A plan outside this closed range is not
# approximated: it is degraded and carries no executable ratio targets.
HERDR_MIN_DIVIDER_RATIO = 0.1
HERDR_MAX_DIVIDER_RATIO = 0.9
_HERDR_MIN_DIVIDER_FRACTION = Fraction(1, 10)
_HERDR_MAX_DIVIDER_FRACTION = Fraction(9, 10)
SOURCE_FINGERPRINT_VERSION = 2
_LIVENESS_FINGERPRINT_FIELDS = (
    "exists",
    "is_dir",
    "is_git",
    "is_main_worktree",
)


@dataclass(frozen=True, order=True)
class UnitColumnKey:
    """Durable Unit identity used by the plan; never a pane locator."""

    workspace_id: str
    lane_id: str
    host_id: str = "local"


@dataclass(frozen=True)
class ObservedUnitColumn:
    """One authorized live column reduced to the facts planning needs.

    No pane locator is accepted: a later actuator resolves the whole live Unit
    set from authority after preview. Optional project facts allow declarative
    membership predicates to resolve without guessing them from a path or
    display order.
    """

    key: UnitColumnKey
    current_index: int


@dataclass(frozen=True)
class UnitColumnRuleFacts:
    """Authoritative public-safe facts available to membership rules."""

    repo_label: Optional[str] = None
    project_id: Optional[str] = None
    fixed_version_id: Optional[str] = None


@dataclass(frozen=True)
class UnitColumnPreference:
    """Observed Unit plus its resolved display-only order/width preference."""

    observed: ObservedUnitColumn
    position: Optional[int] = None
    relative_width: Optional[float] = None


@dataclass(frozen=True)
class UnitColumnRatioTarget:
    """Desired divider ratio keyed by the Unit to its left, never a pane."""

    left_unit: UnitColumnKey
    ratio: float


@dataclass(frozen=True)
class ProjectColumnPlan:
    """Pure desired order and right-nested divider-ratio plan."""

    quality: str
    reasons: tuple[str, ...]
    desired_order: tuple[UnitColumnKey, ...]
    ratio_targets: tuple[UnitColumnRatioTarget, ...]
    requires_reorder: bool = False
    source_fingerprint: Optional[str] = None

    @property
    def executable(self) -> bool:
        """Whether a later preview/confirm actuator may consume the targets."""
        fingerprint = self.source_fingerprint
        return (
            self.quality != QUALITY_DEGRADED
            and isinstance(fingerprint, str)
            and len(fingerprint) == 64
            and all(character in "0123456789abcdef" for character in fingerprint)
        )


WorkspaceLoader = Callable[..., Optional[WorkspaceRecord]]
ConfigLoader = Callable[[Path], RepoLocalConfig]
CanonicalProbe = Callable[[Optional[str]], Mapping[str, object]]
RuleFactResolver = Callable[
    [ObservedUnitColumn, WorkspaceRecord], UnitColumnRuleFacts
]


def _registry_rule_facts(
    _observed: ObservedUnitColumn, record: WorkspaceRecord
) -> UnitColumnRuleFacts:
    """Default facts: registry label only; project/version are not invented."""

    return UnitColumnRuleFacts(repo_label=record.project_name or None)


def _ordered_reasons(reasons: Sequence[str]) -> tuple[str, ...]:
    known = set(reasons)
    return tuple(reason for reason in _REASON_ORDER if reason in known)


def _valid_preference(preference: UnitColumnPreference) -> bool:
    observed = preference.observed
    if (
        not isinstance(observed.key.workspace_id, str)
        or not observed.key.workspace_id.strip()
        or observed.key.workspace_id != observed.key.workspace_id.strip()
        or not isinstance(observed.key.lane_id, str)
        or not observed.key.lane_id.strip()
        or observed.key.lane_id != observed.key.lane_id.strip()
        or observed.key.host_id != "local"
        or isinstance(observed.current_index, bool)
        or not isinstance(observed.current_index, int)
        or observed.current_index < 0
    ):
        return False
    if preference.position is not None and (
        isinstance(preference.position, bool) or not isinstance(preference.position, int)
    ):
        return False
    width = preference.relative_width
    if width is not None:
        if isinstance(width, bool) or not isinstance(width, (int, float)):
            return False
        try:
            normalized_width = float(width)
        except (OverflowError, ValueError):
            return False
        if not math.isfinite(normalized_width) or normalized_width <= 0.0:
            return False
    return True


def _canonical_registry_path(value: object) -> Path | None:
    """Accept only the absolute, already-canonical path stored by the registry writer.

    Calling ``Path.resolve`` on an untrusted relative row would turn the process cwd
    into workspace authority.  The registry writer stores a resolved absolute path,
    so legacy or tampered rows that do not have that shape must fail closed.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    candidate = Path(value)
    if not candidate.is_absolute() or str(candidate) != value:
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if candidate != resolved:
        return None
    return resolved


def _normalized_liveness_facts(value: object) -> Mapping[str, object] | None:
    """Reduce the canonical probe result to stable, typed fingerprint inputs."""

    if not isinstance(value, Mapping):
        return None
    facts: dict[str, object] = {}
    for field in _LIVENESS_FINGERPRINT_FIELDS:
        fact = value.get(field)
        if fact is not None and not isinstance(fact, bool):
            return None
        facts[field] = fact
    return facts


def _right_nested_ratio(weights: Sequence[float]) -> Fraction:
    """Return an exact decimal ratio for a finite positive suffix.

    Config numbers are already normalized to finite floats. Their stable decimal
    spellings preserve intended scale equivalence while ``Fraction`` avoids both
    overflow and a rounded value crossing Herdr's closed 0.1/0.9 boundary.
    Conversion back to float happens only after the exact boundary check.
    """

    exact_weights = tuple(Fraction(str(weight)) for weight in weights)
    return exact_weights[0] / sum(exact_weights)


def plan_project_columns(
    preferences: Sequence[UnitColumnPreference],
    *,
    reasons: Sequence[str] = (),
    source_fingerprint: Optional[str] = None,
) -> ProjectColumnPlan:
    """Build a stable order and weighted right-nested divider ratios.

    Explicit ``position`` values sort first, ascending. Equal positions and all
    unspecified positions retain their live order. Missing widths use weight 1.
    Those recoverable gaps make the plan ``best_effort``; malformed input or a
    ratio outside Herdr's representable range makes it ``degraded`` with no ratio
    targets, so a later actuator has a simple zero-write rule.
    """
    preferences = tuple(preferences)
    accumulated = list(reasons)
    keys = [entry.observed.key for entry in preferences]
    indexes = [entry.observed.current_index for entry in preferences]
    invalid_input = (
        any(not _valid_preference(entry) for entry in preferences)
        or len(set(keys)) != len(keys)
        or len(set(indexes)) != len(indexes)
    )
    if invalid_input:
        accumulated.append(REASON_UNIT_INPUT_INVALID)
        return ProjectColumnPlan(
            quality=QUALITY_DEGRADED,
            reasons=_ordered_reasons(accumulated),
            desired_order=tuple(keys),
            ratio_targets=(),
            requires_reorder=False,
            source_fingerprint=source_fingerprint,
        )

    if any(entry.position is None for entry in preferences):
        accumulated.append(REASON_POSITION_UNSPECIFIED)
    explicit_positions = [
        entry.position for entry in preferences if entry.position is not None
    ]
    if len(set(explicit_positions)) != len(explicit_positions):
        accumulated.append(REASON_POSITION_TIE)
    if any(entry.relative_width is None for entry in preferences):
        accumulated.append(REASON_WIDTH_UNSPECIFIED)

    ordered = tuple(
        sorted(
            preferences,
            key=lambda entry: (
                entry.position is None,
                entry.position if entry.position is not None else 0,
                entry.observed.current_index,
            ),
        )
    )
    desired_order = tuple(entry.observed.key for entry in ordered)
    live_order = tuple(
        entry.observed.key
        for entry in sorted(preferences, key=lambda entry: entry.observed.current_index)
    )

    ordered_reasons = _ordered_reasons(accumulated)
    if any(reason in _DEGRADED_REASONS for reason in ordered_reasons):
        return ProjectColumnPlan(
            quality=QUALITY_DEGRADED,
            reasons=ordered_reasons,
            desired_order=desired_order,
            ratio_targets=(),
            requires_reorder=desired_order != live_order,
            source_fingerprint=source_fingerprint,
        )

    weights = [
        float(entry.relative_width) if entry.relative_width is not None else 1.0
        for entry in ordered
    ]
    ratios = []
    for index in range(len(ordered) - 1):
        suffix = weights[index:]
        exact_ratio = _right_nested_ratio(suffix)
        if (
            exact_ratio < _HERDR_MIN_DIVIDER_FRACTION
            or exact_ratio > _HERDR_MAX_DIVIDER_FRACTION
        ):
            accumulated.append(REASON_RATIO_UNREPRESENTABLE)
            return ProjectColumnPlan(
                quality=QUALITY_DEGRADED,
                reasons=_ordered_reasons(accumulated),
                desired_order=desired_order,
                ratio_targets=(),
                requires_reorder=desired_order != live_order,
                source_fingerprint=source_fingerprint,
            )
        ratios.append(float(exact_ratio))

    targets = tuple(
        UnitColumnRatioTarget(entry.observed.key, ratio)
        for entry, ratio in zip(ordered[:-1], ratios)
    )

    final_reasons = _ordered_reasons(accumulated)
    return ProjectColumnPlan(
        quality=QUALITY_BEST_EFFORT if final_reasons else QUALITY_EXACT,
        reasons=final_reasons,
        desired_order=desired_order,
        ratio_targets=targets,
        requires_reorder=desired_order != live_order,
        source_fingerprint=source_fingerprint,
    )


def _source_fingerprint(entries: Sequence[Mapping[str, object]]) -> str:
    """Hash normalized registry/config/context authority without exposing paths."""

    ordered = sorted(
        entries,
        key=lambda entry: (
            str(entry["workspace_id"]),
            str(entry["lane_id"]),
            str(entry.get("host_id") or ""),
        ),
    )
    encoded = json.dumps(
        {
            "algorithm_version": SOURCE_FINGERPRINT_VERSION,
            "units": ordered,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_rule_facts(value: object) -> bool:
    if not isinstance(value, UnitColumnRuleFacts):
        return False
    return all(
        fact is None or (isinstance(fact, str) and bool(fact.strip()))
        for fact in (value.repo_label, value.project_id, value.fixed_version_id)
    )


def resolve_project_column_plan(
    observed_columns: Sequence[ObservedUnitColumn],
    *,
    home: Optional[Path] = None,
    workspace_loader: WorkspaceLoader = load_workspace_by_id,
    config_loader: ConfigLoader = load_repo_local_config,
    canonical_probe: CanonicalProbe = probe_canonical_liveness,
    rule_fact_resolver: RuleFactResolver = _registry_rule_facts,
) -> ProjectColumnPlan:
    """Resolve each Unit's canonical repo config, then build the pure plan.

    Workspace identity comes only from the registry. Missing/corrupt registry
    authority, a dead or linked-worktree canonical path, an invalid config, or
    insufficient rule facts degrades the whole plan and yields zero executable
    targets. The same workspace/config is read only once per observation, and no
    path, label, cwd, session, or neighboring column is used as an identity
    fallback.
    """

    observed_columns = tuple(observed_columns)
    preferences: list[UnitColumnPreference] = []
    reasons: list[str] = []
    workspace_cache: dict[
        str, tuple[WorkspaceRecord, Path, Mapping[str, object]] | None
    ] = {}
    config_cache: dict[Path, RepoLocalConfig | None] = {}
    fingerprint_entries: list[Mapping[str, object]] = []

    for observed in observed_columns:
        workspace_id = observed.key.workspace_id
        if workspace_id not in workspace_cache:
            authority: tuple[
                WorkspaceRecord, Path, Mapping[str, object]
            ] | None = None
            try:
                record = workspace_loader(workspace_id, home=home)
                if record is not None and record.workspace_id == workspace_id:
                    canonical_path = _canonical_registry_path(record.canonical_path)
                    liveness = (
                        _normalized_liveness_facts(canonical_probe(str(canonical_path)))
                        if canonical_path is not None
                        else None
                    )
                    if liveness is not None and liveness.get(
                        "is_dir"
                    ) is True and liveness.get(
                        "is_main_worktree"
                    ) is not False:
                        authority = (
                            record,
                            canonical_path,
                            liveness,
                        )
            except Exception:  # noqa: BLE001 - registry/probe is an IO boundary
                authority = None
            workspace_cache[workspace_id] = authority

        authority = workspace_cache[workspace_id]
        if authority is None:
            reasons.append(REASON_WORKSPACE_UNRESOLVED)
            preferences.append(UnitColumnPreference(observed=observed))
            continue
        record, canonical_path, liveness = authority

        if canonical_path not in config_cache:
            try:
                config_cache[canonical_path] = config_loader(canonical_path)
            except (
                RepoLocalConfigError,
                PresentationGroupingConfigError,
                OSError,
            ):
                config_cache[canonical_path] = None
        repo_config = config_cache[canonical_path]
        if repo_config is None:
            reasons.append(REASON_CONFIG_INVALID)
            preferences.append(UnitColumnPreference(observed=observed))
            continue

        grouping = repo_config.presentation.grouping
        try:
            rule_facts = rule_fact_resolver(observed, record)
        except Exception:  # noqa: BLE001 - fact authority is an injected boundary
            reasons.append(REASON_RULE_CONTEXT_INCOMPLETE)
            rule_facts = UnitColumnRuleFacts()
        if not _valid_rule_facts(rule_facts):
            reasons.append(REASON_RULE_CONTEXT_INCOMPLETE)
            rule_facts = UnitColumnRuleFacts()
        context = LaunchContext(
            workspace_id=workspace_id,
            lane_id=observed.key.lane_id,
            host_id=observed.key.host_id,
            repo_label=rule_facts.repo_label,
            project_id=rule_facts.project_id,
            fixed_version_id=rule_facts.fixed_version_id,
        )
        try:
            column_preferences = resolve_unit_column_preferences(grouping, context)
        except PresentationGroupingConfigError:
            reasons.append(REASON_CONFIG_INVALID)
            preferences.append(UnitColumnPreference(observed=observed))
            continue

        if not grouping.membership_rules and not grouping.unit_overrides:
            reasons.append(REASON_CONFIG_MISSING)
        if column_preferences.context_incomplete:
            reasons.append(REASON_RULE_CONTEXT_INCOMPLETE)
        preferences.append(
            UnitColumnPreference(
                observed=observed,
                position=column_preferences.position,
                relative_width=column_preferences.relative_width,
            )
        )
        fingerprint_entries.append(
            {
                "workspace_id": workspace_id,
                "lane_id": observed.key.lane_id,
                "host_id": observed.key.host_id,
                "canonical_path": str(canonical_path),
                "repo_label": rule_facts.repo_label,
                "project_id": rule_facts.project_id,
                "fixed_version_id": rule_facts.fixed_version_id,
                "canonical_liveness": liveness,
                "grouping": asdict(grouping),
            }
        )

    fingerprint = (
        _source_fingerprint(fingerprint_entries)
        if observed_columns and len(fingerprint_entries) == len(observed_columns)
        else None
    )
    return plan_project_columns(
        preferences,
        reasons=reasons,
        source_fingerprint=fingerprint,
    )


__all__ = (
    "HERDR_MAX_DIVIDER_RATIO",
    "HERDR_MIN_DIVIDER_RATIO",
    "SOURCE_FINGERPRINT_VERSION",
    "ObservedUnitColumn",
    "ProjectColumnPlan",
    "QUALITY_BEST_EFFORT",
    "QUALITY_DEGRADED",
    "QUALITY_EXACT",
    "REASON_CONFIG_INVALID",
    "REASON_CONFIG_MISSING",
    "REASON_POSITION_TIE",
    "REASON_POSITION_UNSPECIFIED",
    "REASON_RATIO_UNREPRESENTABLE",
    "REASON_RULE_CONTEXT_INCOMPLETE",
    "REASON_UNIT_INPUT_INVALID",
    "REASON_WIDTH_UNSPECIFIED",
    "REASON_WORKSPACE_UNRESOLVED",
    "UnitColumnKey",
    "UnitColumnPreference",
    "UnitColumnRatioTarget",
    "UnitColumnRuleFacts",
    "plan_project_columns",
    "resolve_project_column_plan",
)
