"""The no-injected-snapshot fallback vocabulary — a single composition-supplied
snapshot (Redmine #13569 R2-F1, hardened by R3-F2).

The discovery / pane-resolution consumers take an injected
:class:`~.agent_provider_runtime_snapshot.AgentProviderRuntimeSnapshot`; when none is
injected they need the built-in provider vocabulary. R2-F1 moved that read off an
import-time global, but the R2 leaf still reached the ``e_140`` registry itself (through
five *function-local* imports, one per accessor) and cached five independent projections
— which is still an ``e_110 domain -> e_140 registry`` dependency and still not the single
immutable snapshot Design Answer j#76969 requires.

R3-F2 removes that dependency completely. This module holds ONE core-owned default
:class:`~.agent_provider_runtime_snapshot.AgentProviderRuntimeSnapshot`, *supplied by the
composition* — the ``e_140`` provider-registry factory
(:mod:`...f_160_provider_registry.application.agent_provider_runtime`) calls
:func:`set_default_snapshot` at its import (an ``e_140 -> e_110`` edge, the sanctioned
direction). Every real entrypoint imports that factory (``build_parser`` does), so the
default is registered before any consumer's fallback path runs. The ``builtin_*`` accessors
are thin, allocation-cheap *projections of that one snapshot* — there is no registry import
here (module or function-local) and no second source of truth, so all consumers that fall
back share the exact vocabulary the composition built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# e_110 -> e_110 (same f_120 domain), NOT the forbidden e_140 registry edge: the snapshot
# value object owns the receiver-agnostic host-process set the fallback widens by.
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot import (  # noqa: E501
    RECEIVER_AGNOSTIC_PROCESSES,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never an import-time e_140 edge
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot import (  # noqa: E501
        AgentProviderRuntimeSnapshot,
    )

#: The single core-owned fallback snapshot, set once by the composition (the e_140
#: provider-registry factory at import). ``None`` until then; :func:`default_snapshot`
#: raises rather than silently reaching a registry the domain must not import.
_default_snapshot: "AgentProviderRuntimeSnapshot | None" = None


class DefaultAgentProviderSnapshotUnset(RuntimeError):
    """A consumer needed the built-in fallback before the composition supplied it.

    The fix is never to import the ``e_140`` registry from here (that is the boundary
    R3-F2 removes) — it is to import the provider-registry factory
    (``...f_160_provider_registry.application.agent_provider_runtime``), which registers
    the default, or to inject a snapshot explicitly at the call.
    """


def set_default_snapshot(snapshot: "AgentProviderRuntimeSnapshot") -> None:
    """Register ``snapshot`` as the no-injection fallback (called by the composition).

    Idempotent-friendly: a later call replaces the default, so a test composition can
    substitute a synthetic built-in snapshot. Kept trivial so the e_140 factory can call
    it at import without ordering hazards.
    """
    global _default_snapshot
    _default_snapshot = snapshot


def default_snapshot() -> "AgentProviderRuntimeSnapshot":
    """The composition-supplied fallback snapshot; raise if it was never supplied."""
    if _default_snapshot is None:
        raise DefaultAgentProviderSnapshotUnset(
            "the built-in agent-provider snapshot was never supplied to e_110; import the "
            "provider-registry factory (e_140 f_160 application agent_provider_runtime) so "
            "it registers the default, or inject a snapshot at the call site"
        )
    return _default_snapshot


def builtin_provider_ids() -> "frozenset[str]":
    """The built-in provider ids (a projection of the one default snapshot)."""
    return default_snapshot().provider_ids


def builtin_discovery_aliases() -> "dict[str, str]":
    """``{window/pane alias -> provider id}`` for the built-in providers."""
    return default_snapshot().discovery_aliases()


def builtin_process_owners() -> "dict[str, str]":
    """``{process basename -> provider id}`` for the built-in providers (excludes host)."""
    return default_snapshot().process_owners()


def builtin_agent_commands() -> "dict[str, str]":
    """``{provider id -> command basename}`` for the built-in providers."""
    return default_snapshot().commands()


def builtin_agent_processes() -> "set[str]":
    """Provider process basenames plus the receiver-agnostic host runtimes (e.g. ``node``).

    Mirrors the pre-existing ``pane_resolver.AGENT_PROCESSES`` membership: every
    provider-owned process from the snapshot, widened by the receiver-agnostic host
    processes the snapshot treats as a weak agent-process hint.
    """
    return set(default_snapshot().agent_process_names()) | set(RECEIVER_AGNOSTIC_PROCESSES)


__all__ = (
    "DefaultAgentProviderSnapshotUnset",
    "set_default_snapshot",
    "default_snapshot",
    "builtin_provider_ids",
    "builtin_discovery_aliases",
    "builtin_process_owners",
    "builtin_agent_commands",
    "builtin_agent_processes",
)
