"""mozyo-bridge package."""

__version__ = "0.12.1"

# Redmine #13569 R3-F2: composition bootstrap. The e_110 discovery / pane-resolution
# domain no longer imports the e_140 provider registry for its no-injected-snapshot
# fallback; instead the fallback is a single core-owned snapshot the composition supplies.
# Importing the provider-registry factory here runs its one-time
# `set_default_snapshot(BUILTIN)` registration (an e_140 -> e_110 edge, the sanctioned
# direction). Because Python runs this package `__init__` before any `mozyo_bridge.*`
# submodule import, the default is registered before any consumer — even a module-level
# `from pane_resolver import AGENT_COMMANDS` frozen at import — can reach the fallback.
# This is the process bootstrap wiring the core-owned default; it is not a domain->registry
# edge (which stays forbidden and is pinned by test_provider_consumer_r3_corrections).
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application import (  # noqa: E402,F401
    agent_provider_runtime as _agent_provider_runtime_bootstrap,
)
