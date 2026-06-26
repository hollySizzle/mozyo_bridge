"""Generic scalar / shape validators for the grouping config.

These are the *type-and-shape* checks of the closed schema: a record is a
mapping, a field is a non-empty string / optional bool / optional int, the
``version`` is the supported one, a projection / Project-Group presentation mode
is one of the built-in vocabulary values. They fail closed through
:class:`PresentationGroupingConfigError` and never silently normalize.

The *authority / boundary-leak* guard (forbidden tokens in keys and identity /
diagnostic values) is a separate concern and lives in
:mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.authority`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Optional

from .constants import (
    ALLOWED_PROJECTIONS,
    DEFAULT_DELEGATION_WINDOW_POLICY,
    DEFAULT_PROJECT_GROUP_PRESENTATION,
    DELEGATION_WINDOW_POLICY_MODES,
    PRESENTATION_GROUPING_VERSION,
    PROJECT_GROUP_PRESENTATION_MODES,
)
from .errors import PresentationGroupingConfigError


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


def _checked_delegation_window_policy(
    record: "Mapping[object, object]", *, source: str
) -> str:
    """Return the desired delegated-coordinator window-separation policy, fail-closed.

    ``delegation_window_policy`` is optional and defaults to
    :data:`DEFAULT_DELEGATION_WINDOW_POLICY` (``separate``), so a missing field
    preserves the documented default. Any value outside
    :data:`DELEGATION_WINDOW_POLICY_MODES` — including a boundary- / authority-
    shaped string — is rejected rather than silently normalized; the policy is a
    closed display-only vocabulary, never a routing / approval target (Redmine
    #12467).
    """
    value = record.get(
        "delegation_window_policy", DEFAULT_DELEGATION_WINDOW_POLICY
    )
    if not isinstance(value, str) or value not in DELEGATION_WINDOW_POLICY_MODES:
        raise PresentationGroupingConfigError(
            f"{source} 'delegation_window_policy' must be one of "
            f"{sorted(DELEGATION_WINDOW_POLICY_MODES)} when present, got {value!r}"
        )
    return value
