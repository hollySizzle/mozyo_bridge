"""Lazy built-in agent-provider vocabulary — the no-injected-snapshot fallback
(Redmine #13569 R2-F1).

The discovery / pane-resolution consumers take an injected
:class:`~.agent_provider_runtime_snapshot.AgentProviderRuntimeSnapshot`; when none is
injected they need the built-in provider vocabulary. Reading that vocabulary used to be a
module-level ``e_110 domain -> e_140 registry`` import frozen at import time — the two
things Design Answer j#76969 forbids (an import-time split, a domain -> registry singleton
edge). This leaf module is the single place that fallback lives: each accessor reads the
packaged profile registry through a **function-local** import and caches it, so nothing is
frozen at import and the injected path never touches the registry. Consumers import these
functions (an ``e_110 -> e_110`` edge) instead of the registry directly.
"""

from __future__ import annotations

_ids_cache: "frozenset[str] | None" = None
_aliases_cache: "dict[str, str] | None" = None
_process_owners_cache: "dict[str, str] | None" = None
_commands_cache: "dict[str, str] | None" = None
_processes_cache: "set[str] | None" = None


def builtin_provider_ids() -> "frozenset[str]":
    """The built-in provider ids (lazy, cached)."""
    global _ids_cache
    if _ids_cache is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            agent_provider_ids,
        )

        _ids_cache = agent_provider_ids()
    return _ids_cache


def builtin_discovery_aliases() -> "dict[str, str]":
    """``{window/pane alias -> provider id}`` for the built-in providers (lazy, cached)."""
    global _aliases_cache
    if _aliases_cache is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            agent_discovery_aliases,
        )

        _aliases_cache = agent_discovery_aliases()
    return _aliases_cache


def builtin_process_owners() -> "dict[str, str]":
    """``{process basename -> provider id}`` for the built-in providers (lazy, cached)."""
    global _process_owners_cache
    if _process_owners_cache is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            agent_process_owners,
        )

        _process_owners_cache = agent_process_owners()
    return _process_owners_cache


def builtin_agent_commands() -> "dict[str, str]":
    """``{provider id -> command basename}`` for the built-in providers (lazy, cached)."""
    global _commands_cache
    if _commands_cache is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            agent_commands,
        )

        _commands_cache = agent_commands()
    return _commands_cache


def builtin_agent_processes() -> "set[str]":
    """Provider process basenames plus the receiver-agnostic ``node`` (lazy, cached)."""
    global _processes_cache
    if _processes_cache is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            agent_process_names,
        )

        _processes_cache = set(agent_process_names()) | {"node"}
    return _processes_cache


__all__ = (
    "builtin_provider_ids",
    "builtin_discovery_aliases",
    "builtin_process_owners",
    "builtin_agent_commands",
    "builtin_agent_processes",
)
