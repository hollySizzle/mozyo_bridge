"""Built-in agent provider profile registry — the packaged-data load (Redmine #13441).

The runtime entry point for the data-driven agent-launch vocabulary. It reads the
wheel-packaged ``agent_provider_profiles.yaml`` through :mod:`importlib.resources`
(a package-anchored resource — never a cwd / worktree path walk, so a hostile repo
checkout can neither shadow the built-in profiles nor inject one), validates it
through :class:`~.agent_provider_profile_config.AgentProviderProfileConfig`, and
exposes the single seeded :data:`AGENT_PROVIDER_PROFILES` registry plus the derived
vocabularies the launch / discovery layers used to hard-code.

This mirrors :mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile`:
the schema module stays pure, and the packaged-artifact read lives here. A malformed
or missing artifact fails closed at import (:class:`AgentProviderProfileError`)
rather than letting the launch layer run on a partially-understood provider
contract.

The derived accessors below are what let the old constants become one-line
projections of the registry:

===========================================  =========================================
old hard-coded constant                       now derived from
===========================================  =========================================
``pane_resolver.AGENT_COMMANDS``              :func:`agent_commands`
``pane_resolver.AGENT_LABELS``                :func:`agent_provider_ids`
``pane_resolver.AGENT_PROCESSES``             :func:`agent_process_names`
``agent_discovery.AGENT_KINDS``               :func:`agent_provider_ids`
``herdr_target_resolution.AGENT_PROVIDERS``   :func:`agent_provider_ids`
``agent_launch_argv.LAUNCH_ARGV_PROVIDERS``   :func:`agent_provider_ids`
``agent_launch_argv.RESERVED_MANAGED_FLAGS``  :func:`reserved_managed_flags`
===========================================  =========================================

Not derived from the registry (deliberately): ``herdr_launch_command.LAUNCH_PROVIDERS``
— the *default launch topology*. Registering a profile makes a provider expressible,
never launched; what mozyo actually starts stays a separate contract (Design Answer
j#76725).
"""

from __future__ import annotations

from importlib import resources
from typing import Optional

import yaml

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AGENT_PROVIDER_PROFILE_RESOURCE,
    AgentCapability,
    AgentProviderProfile,
    AgentProviderProfileConfig,
    AgentProviderProfileError,
    AgentProviderProfileRegistry,
    ManagedFlagConcept,
)


def load_agent_provider_config() -> AgentProviderProfileConfig:
    """Read + validate the wheel-packaged built-in profile artifact.

    Package-anchored (``importlib.resources``), so resolution never depends on the
    cwd, the worktree, or an operator-supplied path. A malformed artifact raises
    :class:`AgentProviderProfileError` — the launch layer must never fall back to a
    guessed provider contract.
    """
    text = (
        resources.files(__package__)
        .joinpath(AGENT_PROVIDER_PROFILE_RESOURCE)
        .read_text(encoding="utf-8")
    )
    try:
        record = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - malformed packaged artifact
        raise AgentProviderProfileError(
            f"packaged agent provider profiles ({AGENT_PROVIDER_PROFILE_RESOURCE}) "
            f"are not valid YAML: {exc}"
        ) from exc
    return AgentProviderProfileConfig.from_record(record)


def _seed_registry() -> AgentProviderProfileRegistry:
    """Seed the built-in registry once, at import, from the packaged artifact."""
    return load_agent_provider_config().to_registry()


#: The built-in agent provider profiles (``claude`` / ``codex`` today). Seeded once
#: at import from pure packaged data; nothing here loads or executes provider code.
AGENT_PROVIDER_PROFILES: AgentProviderProfileRegistry = _seed_registry()


def agent_provider_ids() -> frozenset[str]:
    """The launch / identity provider vocabulary (the old closed ``{claude, codex}``)."""
    return AGENT_PROVIDER_PROFILES.provider_ids()


def agent_commands() -> dict[str, str]:
    """``{provider_id: command basename}`` — the ``AGENT_COMMANDS`` replacement.

    A *basename*, not a resolved path: the verified absolute executable is produced
    at launch by :mod:`..application.agent_provider_executable`, so committed data
    never names the binary that runs.
    """
    return AGENT_PROVIDER_PROFILES.commands()


def agent_process_names() -> frozenset[str]:
    """Process basenames that identify a registered provider (discovery vocabulary)."""
    return AGENT_PROVIDER_PROFILES.process_names()


def agent_discovery_aliases() -> dict[str, str]:
    """``{alias: provider_id}`` for pane/window/process role discovery."""
    return AGENT_PROVIDER_PROFILES.discovery_aliases()


def agent_process_owners() -> dict[str, str]:
    """``{process basename: provider_id}`` — exact-one, validated at load (R1-F3)."""
    return AGENT_PROVIDER_PROFILES.process_owners()


def reserved_managed_flags() -> dict[str, tuple[str, ...]]:
    """``{provider_id: reserved flags}`` — the ``RESERVED_MANAGED_FLAGS`` replacement.

    The flags an operator's repo ``launch_argv`` may not re-specify, derived from
    each profile's managed-flag spellings rather than a hard-coded table (#13425 Q4).
    """
    return AGENT_PROVIDER_PROFILES.reserved_managed_flags()


def managed_flag_for(
    provider_id: str, concept: ManagedFlagConcept
) -> Optional[str]:
    """This provider's spelling of a managed ``concept``, or ``None``.

    Fails closed on an unknown provider: a launch must never render a managed flag
    for a provider mozyo does not know.
    """
    return AGENT_PROVIDER_PROFILES.require(provider_id).managed_flag(concept)


def provider_has_capability(provider_id: str, capability: AgentCapability) -> bool:
    """Whether ``provider_id`` declares a mechanical ``capability`` (fail-closed)."""
    return AGENT_PROVIDER_PROFILES.require(provider_id).has_capability(capability)


def require_profile(provider_id: str) -> AgentProviderProfile:
    """The profile for ``provider_id``, raising on an unknown provider."""
    return AGENT_PROVIDER_PROFILES.require(provider_id)


# --- Non-raising accessors -------------------------------------------------
#
# The launch path must fail closed on an unknown provider, so it uses the
# ``require``-based accessors above. Introspection surfaces (``doctor``, the
# permission-mode policy resolver) are asked about arbitrary agent labels and must
# answer "no" rather than raise — a diagnostic that crashes on an unknown label is
# worse than one that reports nothing. These two are that boundary.


def provider_supports(provider_id: str, capability: AgentCapability) -> bool:
    """Whether ``provider_id`` is registered AND declares ``capability``.

    ``False`` for an unknown provider (never raises), so a policy/diagnostic caller
    can ask about any agent label safely.
    """
    profile = AGENT_PROVIDER_PROFILES.get(provider_id)
    return profile is not None and profile.has_capability(capability)


def provider_managed_flag(
    provider_id: str, concept: ManagedFlagConcept
) -> Optional[str]:
    """``provider_id``'s spelling of ``concept``, or ``None`` (unknown provider ok).

    This is what lets a managed flag be *spelled by data* at the launch chokepoints:
    rename the flag in the profile and every renderer follows, with no source edit.
    """
    profile = AGENT_PROVIDER_PROFILES.get(provider_id)
    if profile is None:
        return None
    return profile.managed_flag(concept)


__all__ = (
    "AGENT_PROVIDER_PROFILES",
    "agent_commands",
    "agent_discovery_aliases",
    "agent_process_names",
    "agent_process_owners",
    "agent_provider_ids",
    "load_agent_provider_config",
    "managed_flag_for",
    "provider_has_capability",
    "provider_managed_flag",
    "provider_supports",
    "require_profile",
    "reserved_managed_flags",
)
