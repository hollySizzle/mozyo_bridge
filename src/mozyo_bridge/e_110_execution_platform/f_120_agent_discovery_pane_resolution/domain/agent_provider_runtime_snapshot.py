"""Immutable agent-provider runtime snapshot — the injected mechanical vocabulary
(Redmine #13569 Increment 2A, Design Answer j#76964 / Coordinator Answer j#76969).

Increment 1 (#13441) turned the agent-launch layer's provider knowledge into
projections of a data registry
(:class:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config.AgentProviderProfileRegistry`).
But the consumers (discovery, pane resolution, handoff receiver validation,
status, doctor) still each reached that knowledge through the *global* built-in
singleton — either by importing ``AGENT_PROVIDER_PROFILES`` / calling
``agent_provider_ids()`` at import time, or by carrying their own hard-coded
``{"claude", "codex"}`` literal. Two problems follow from that:

- a synthetic same-protocol provider added only for a test (or a future rebind)
  is invisible to a consumer that froze the vocabulary at import — the only way to
  make the consumer see it was to monkeypatch the global, which hides exactly the
  registry-split seam the feature exists to exercise (the #13441 R2-F1 lesson,
  restated for the consumer sweep);
- there was no single value the composition root could build once and thread to
  every consumer, so "all consumers agree on one provider vocabulary" was an
  accident of the shared global, not an injected invariant.

This module is that single value: a **frozen, pure projection** of one registry's
*mechanical* vocabulary — provider ids, the command basename per provider, the
alias / process ownership maps, and the mechanical launchability derived from each
profile's protocol + interactive-TUI capability. The composition root builds ONE
snapshot from the built-in registry (or, in a test, from a synthetic registry) and
injects the SAME instance into every consumer, so a provider is expressible to all
of them or to none — never to a subset that depends on import order.

**What the snapshot deliberately does NOT carry** (the hard fence of j#76969):

- no workflow role, no ``provider_binding`` (role -> provider), no route / gate /
  approval authority — those stay core-owned in
  :mod:`...f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding`
  and are injected as a *separate* object (Increment 2B);
- no default pair / launch topology — registering a profile makes a provider
  *expressible*, never *launched*; the expected topology is a separate input the
  status / doctor / launch consumers receive alongside this snapshot (see the
  ``known_providers`` vs ``expected_providers`` split, j#76969);
- no host executable path / argv / callable — the verified absolute executable is
  resolved at launch time by the trusted resolver, never from this value.

The snapshot is built by the factory in
:mod:`mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime`,
which keeps the ``e_110`` consumers from importing the ``e_140`` registry singleton
directly (Design Answer j#76964 minimal-dependency boundary). This module is pure:
a frozen dataclass over plain strings, no IO, no registry import.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

#: The core "no provider identified" sentinel. A snapshot never contains this as a
#: provider id (the registry schema forbids a profile from claiming it, #13441
#: R1-F3); it is named here so a consumer can keep answering "unknown" for an
#: unrecognized pane without importing the discovery module.
AGENT_KIND_UNKNOWN: str = "unknown"

#: Process basenames that identify a *host runtime*, not a provider — both built-in
#: CLIs are Node programs, so a ``node`` process is receiver-agnostic. The registry
#: schema forbids a profile from claiming these (#13441 R1-F3); the snapshot keeps
#: them as a weak agent-process hint that can never name a provider, mirroring the
#: pre-existing ``pane_resolver`` behavior.
RECEIVER_AGNOSTIC_PROCESSES: frozenset[str] = frozenset({"node"})


@dataclass(frozen=True)
class AgentProviderRuntimeSnapshot:
    """One registry's mechanical provider vocabulary, frozen for injection.

    Every field is an immutable projection of the source registry, so a consumer
    that holds a snapshot reads a stable vocabulary that cannot drift under it and
    cannot be mutated by a caller. The snapshot answers only *mechanical* questions
    — "is this a known provider?", "what command basename does it run?", "which
    provider owns this pane alias / process?", "can the launch mechanism drive it?"
    — never a role, a binding, a route, or a default topology.

    Build it through
    :func:`...f_160_provider_registry.application.agent_provider_runtime.build_runtime_snapshot`,
    never by hand at a call site: the factory is the one place that reads a registry.
    """

    #: Every registered provider id (the launch / identity vocabulary). Sorted-stable
    #: order is not guaranteed by a set; use :meth:`sorted_provider_ids` for display.
    provider_ids: frozenset[str]
    #: ``{provider_id: command basename}`` — a basename, never a resolved host path.
    _commands: Mapping[str, str]
    #: ``{pane/window alias: provider_id}`` for discovery classification.
    _discovery_aliases: Mapping[str, str]
    #: ``{process basename: provider_id}`` — exact-one across providers (registry
    #: enforces no duplicate). Excludes the receiver-agnostic host processes.
    _process_owners: Mapping[str, str]
    #: ``{provider_id: mechanically launchable?}`` — protocol the launch mechanism can
    #: drive AND the interactive-TUI capability. A provider that is expressible but
    #: not launchable (a different-protocol profile) is ``False`` here, so a consumer
    #: can recognize it without offering to start it.
    _launchable: Mapping[str, bool]

    # ------------------------------------------------------------------ builders
    @classmethod
    def from_projection(
        cls,
        *,
        provider_ids: frozenset[str],
        commands: Mapping[str, str],
        discovery_aliases: Mapping[str, str],
        process_owners: Mapping[str, str],
        launchable: Mapping[str, bool],
    ) -> "AgentProviderRuntimeSnapshot":
        """Freeze already-derived projections into a snapshot (the factory's seam).

        Each mapping is copied into a read-only :class:`~types.MappingProxyType` so a
        caller cannot mutate the snapshot after construction. The factory is expected
        to pass registry-derived, already-validated mappings; this constructor adds no
        registry knowledge of its own (it stays pure and IO-free).
        """
        return cls(
            provider_ids=frozenset(provider_ids),
            _commands=MappingProxyType(dict(commands)),
            _discovery_aliases=MappingProxyType(dict(discovery_aliases)),
            _process_owners=MappingProxyType(dict(process_owners)),
            _launchable=MappingProxyType(dict(launchable)),
        )

    # ------------------------------------------------------------------ queries
    def is_provider(self, provider_id: object) -> bool:
        """Whether ``provider_id`` is a recognized provider (never raises)."""
        return provider_id in self.provider_ids

    def sorted_provider_ids(self) -> tuple[str, ...]:
        """The provider ids in deterministic (sorted) order, for CLI choices / display."""
        return tuple(sorted(self.provider_ids))

    def command_for(self, provider_id: str) -> str | None:
        """The command basename for ``provider_id``, or ``None`` if unknown."""
        return self._commands.get(provider_id)

    def commands(self) -> dict[str, str]:
        """A plain ``{provider_id: command basename}`` copy (for display / iteration)."""
        return dict(self._commands)

    def provider_for_alias(self, alias: object) -> str | None:
        """The provider a discovery ``alias`` (window/pane name) identifies, or ``None``."""
        if not isinstance(alias, str):
            return None
        return self._discovery_aliases.get(alias)

    def discovery_aliases(self) -> dict[str, str]:
        """A plain ``{pane/window alias: provider_id}`` copy (for display / the fallback).

        Symmetric with :meth:`commands` / :meth:`process_owners`; lets the
        no-injected-snapshot fallback project the whole alias map off this one snapshot
        instead of re-reading the registry (Redmine #13569 R3-F2).
        """
        return dict(self._discovery_aliases)

    def provider_for_process(self, process: object) -> str | None:
        """The provider a ``process`` basename identifies, or ``None``.

        Receiver-agnostic host processes (``node``) are never in the owner map, so
        this returns ``None`` for them — they identify a runtime, not a provider.
        """
        if not isinstance(process, str):
            return None
        return self._process_owners.get(process)

    def process_owners(self) -> dict[str, str]:
        """A plain ``{process basename: provider_id}`` copy (excludes host processes)."""
        return dict(self._process_owners)

    def agent_process_names(self) -> frozenset[str]:
        """Every process basename that identifies some provider (excludes ``node``)."""
        return frozenset(self._process_owners)

    def is_agent_process(self, process: object) -> bool:
        """Whether ``process`` is a provider process OR a receiver-agnostic host runtime.

        Mirrors the pre-existing ``pane_resolver.AGENT_PROCESSES`` membership: a
        provider-owned process, or ``node`` (the weak agent-process hint that names a
        runtime, not a provider).
        """
        if not isinstance(process, str):
            return False
        return process in self._process_owners or process in RECEIVER_AGNOSTIC_PROCESSES

    def is_launchable(self, provider_id: object) -> bool:
        """Whether the launch mechanism can mechanically drive ``provider_id``.

        ``False`` for an unknown provider and for a provider whose profile declares a
        protocol the launch mechanism cannot drive (never raises), so a consumer can
        recognize a provider it must not offer to start.
        """
        if not isinstance(provider_id, str):
            return False
        return self._launchable.get(provider_id, False)


__all__ = (
    "AGENT_KIND_UNKNOWN",
    "RECEIVER_AGNOSTIC_PROCESSES",
    "AgentProviderRuntimeSnapshot",
)
