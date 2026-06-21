"""The fail-closed error for the desired presentation grouping config."""

from __future__ import annotations


class PresentationGroupingConfigError(ValueError):
    """The desired presentation grouping record violates the closed schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    domain error :class:`~mozyo_bridge.domain.repo_local_config.RepoLocalConfigError`.
    """
