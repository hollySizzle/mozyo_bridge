"""Factory: project the provider registry's ghost signals into the injected policy.

Redmine #14065 Phase 2. The pending-composer render gate (e110) decides whether a
text pending candidate is an emptyable ghost, but it must not import the e140 provider
registry / singleton (IR j#82181 item 2). This factory is the one place that reads the
registry's v3 ``ghost_composer_signals`` and builds the frozen
:class:`~...domain.sublane_ghost_composer_gate.GhostComposerRenderPolicy` the e110 rails
receive by injection — the same "e140 builds a snapshot the e110 consumers are handed"
pattern as :func:`...agent_provider_runtime.build_runtime_snapshot`.

Only a provider that *declares* a ghost signal appears in the policy; a provider that
declares none admits nothing (the fail-closed default), so a dim render for it preserves.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_ghost_composer_gate import (  # noqa: E501
    GhostComposerRenderPolicy,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
    AgentProviderProfileRegistry,
)


def build_ghost_composer_policy(
    registry: "Optional[AgentProviderProfileRegistry]" = None,
) -> GhostComposerRenderPolicy:
    """Build the injected ghost policy from ``registry`` (default: the built-in profiles).

    Reads each profile's ``ghost_composer_signals`` and produces a
    ``{provider_id: admitted style_provenances}`` policy. A provider declaring none is
    omitted (admits nothing). The default reads the built-in singleton here, in e140, so
    the e110 gate never imports it.
    """
    if registry is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            AGENT_PROVIDER_PROFILES,
        )

        registry = AGENT_PROVIDER_PROFILES
    pairs: dict[str, frozenset[str]] = {}
    for provider_id in registry.provider_ids():
        profile = registry.require(provider_id)
        if profile.ghost_composer_signals:
            pairs[provider_id] = frozenset(profile.ghost_composer_signals)
    return GhostComposerRenderPolicy.from_pairs(pairs)


__all__ = ("build_ghost_composer_policy",)
