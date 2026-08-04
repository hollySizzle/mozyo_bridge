"""Hermetic contract fixture for valid Redmine handoff anchors (Redmine #14246)."""

from __future__ import annotations

from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_anchor_authority import (
    RedmineAnchorRead,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_anchor_source import (
    LiveRedmineAnchorSource,
)


class MatchingRedmineAnchorSource:
    """Observe each requested issue/journal as one valid ownership pair."""

    def read_anchor(self, issue: str, journal: str) -> RedmineAnchorRead:
        return RedmineAnchorRead(issue, journal, True, issue, (journal,))


def matching_redmine_anchor_source_patch():
    """Patch only the live constructor; the production decision gate still executes."""

    return mock.patch.object(
        LiveRedmineAnchorSource,
        "from_environment",
        return_value=MatchingRedmineAnchorSource(),
    )


__all__ = (
    "MatchingRedmineAnchorSource",
    "matching_redmine_anchor_source_patch",
)
