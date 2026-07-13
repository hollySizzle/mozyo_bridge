"""The handoff-receiver choice vocabulary, injectable from a provider snapshot
(Redmine #13569 Increment 2A, Design Answer j#76964 / Coordinator Answer j#76969).

The ``handoff send`` / ``message`` / ``ticketless-callback`` receiver-choice surfaces
all validate ``--to`` / ``--select-role`` against the recognized providers. Before
this increment each surface carried its own hard-coded ``["claude", "codex"]`` list.
This leaf module is the single derivation they share, resolved from an injected
:class:`~mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot.AgentProviderRuntimeSnapshot`
or, by default, the built-in snapshot — so a synthetic same-protocol provider becomes
a valid receiver at every surface without a literal edit.

It lives in its own leaf module (not ``cli_handoff`` and not the ``handoff`` domain,
which is at its module-health cap) so all three registrar modules can import it with
no import cycle — ``cli_handoff`` already imports the ``cli_handoff_select`` /
``cli_handoff_ticketless`` registrars, so a shared helper cannot live in ``cli_handoff``.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot import (
    AgentProviderRuntimeSnapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (
    BUILTIN_AGENT_PROVIDER_SNAPSHOT,
)


def receiver_choices(snapshot: AgentProviderRuntimeSnapshot | None = None) -> list[str]:
    """The sorted receiver vocabulary for a handoff CLI ``choices`` list.

    ``None`` uses the built-in provider snapshot — ``['claude', 'codex']``, byte-identical
    to the previous hard-coded lists. An injected snapshot supplies a synthetic provider
    set for a test / future composition without editing any receiver-choice call site.
    """
    source = snapshot if snapshot is not None else BUILTIN_AGENT_PROVIDER_SNAPSHOT
    return list(source.sorted_provider_ids())


__all__ = ("receiver_choices",)
