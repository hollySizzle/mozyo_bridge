"""Build the injected agent-provider runtime snapshot from a registry
(Redmine #13569 Increment 2A, Design Answer j#76964 / Coordinator Answer j#76969).

This is the ONE place that reads an
:class:`~..domain.agent_provider_profile_config.AgentProviderProfileRegistry` and
projects its *mechanical* vocabulary into the core-owned, injectable
:class:`~mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot.AgentProviderRuntimeSnapshot`.
Keeping the read here — in the adapter/provider layer — is what lets the execution-platform
consumers hold a snapshot value without importing the registry singleton themselves
(the minimal-dependency boundary of j#76964: no ``e_110`` domain -> ``e_140`` global).

The factory adds no policy: launchability is derived from exactly the two checks the
launch path already enforces in
:func:`..application.agent_provider_executable.require_launchable` — the profile's
interaction protocol must be one the launch mechanism can drive, and the profile must
declare the interactive-TUI capability. So a snapshot's ``is_launchable`` answer can
never disagree with what the launch preflight would decide. Role, binding, route, and
default topology are deliberately absent (they are separate contracts).
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot import (
    AgentProviderRuntimeSnapshot,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_vocab import (
    set_default_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (
    LAUNCHABLE_PROTOCOLS,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    AGENT_PROVIDER_PROFILES,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentCapability,
    AgentProviderProfileRegistry,
)


def _profile_is_launchable(protocol, capabilities_holder) -> bool:
    """Whether a profile is mechanically launchable — the ``require_launchable`` checks.

    Kept in lock-step with :func:`..agent_provider_executable.require_launchable`: a
    provider is launchable iff its protocol is one the launch mechanism can drive AND
    it declares the interactive-TUI capability. Deriving the snapshot's answer from the
    same two predicates means the snapshot can never say "launchable" for a provider the
    preflight would reject (or vice versa).
    """
    return (
        protocol in LAUNCHABLE_PROTOCOLS
        and capabilities_holder.has_capability(AgentCapability.INTERACTIVE_TUI)
    )


def build_runtime_snapshot(
    registry: Optional[AgentProviderProfileRegistry] = None,
) -> AgentProviderRuntimeSnapshot:
    """Project ``registry`` (default: the built-in profiles) into a frozen snapshot.

    Reads the registry's already-validated derived vocabulary — provider ids, command
    basenames, discovery aliases, process owners — and computes the mechanical
    launchability per provider. The result is an immutable value the composition root
    injects into every consumer, so a synthetic provider present only in an injected
    ``registry`` reaches all of them without any global monkeypatch (the acceptance
    invariant of j#76969). ``registry is None`` uses the built-in singleton, so the
    default composition is behavior-preserving.
    """
    source = AGENT_PROVIDER_PROFILES if registry is None else registry
    launchable = {
        profile.provider_id: _profile_is_launchable(profile.protocol, profile)
        for profile in source.profiles()
    }
    return AgentProviderRuntimeSnapshot.from_projection(
        provider_ids=source.provider_ids(),
        commands=source.commands(),
        discovery_aliases=source.discovery_aliases(),
        process_owners=source.process_owners(),
        launchable=launchable,
    )


#: The built-in agent-provider runtime snapshot, seeded once from the packaged
#: profiles. Every consumer's ``snapshot`` parameter defaults to this, so existing
#: call sites keep the built-in vocabulary and behavior; a test / future composition
#: injects a different snapshot to exercise a synthetic provider set.
BUILTIN_AGENT_PROVIDER_SNAPSHOT: AgentProviderRuntimeSnapshot = build_runtime_snapshot()

# Redmine #13569 R3-F2: this factory (the ONE place that reads the registry) is also the
# ONE place that supplies the e_110 consumers' no-injection fallback. Registering the
# built-in snapshot as their single core-owned default here — an e_140 -> e_110 edge, the
# sanctioned direction — is what lets the e_110 discovery/pane-resolution domain drop its
# own e_140 registry import entirely (module AND function-local). Any composition that
# needs a consumer's fallback imports this factory (build_parser does), which runs this
# registration; the e_110 domain never reaches back into the registry itself.
set_default_snapshot(BUILTIN_AGENT_PROVIDER_SNAPSHOT)


__all__ = (
    "BUILTIN_AGENT_PROVIDER_SNAPSHOT",
    "build_runtime_snapshot",
)
