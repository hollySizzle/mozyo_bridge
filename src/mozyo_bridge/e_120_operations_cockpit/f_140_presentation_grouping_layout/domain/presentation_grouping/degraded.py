"""Degraded display classification for the desired presentation grouping config.

The grouping surface never silently reroutes on runtime drift; instead it
classifies the drift as a *visible* degraded condition the read model can show.
This module owns that classification for config-vs-live-Unit drift:
:func:`diagnose_unit_overrides` flags an override whose Unit is not among the
known live Units as :data:`STATUS_DESIRED_UNIT_MISSING`.

The sibling degraded condition — ``identity_conflict`` — is detected from the
launch context (:meth:`LaunchContext.has_identity_conflict`) and applied during
placement resolution
(:mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.placement`); the shared status
vocabulary lives in
:mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.constants`. This is a read-model
diagnostic only — it resolves no routing and decides no side effect.
"""

from __future__ import annotations

from .config import PresentationGroupingConfig, UnitOverride
from .constants import STATUS_DESIRED_UNIT_MISSING


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
