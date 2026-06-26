"""Runtime resolution of the built-in provider selection (Redmine #12249).

This is the first connection of the internal provider-selection layer
(:class:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.ProviderSelectionConfig` /
:data:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.BUILTIN_PROVIDER_REGISTRY`, Redmine
#12035 / #12184) to a real runtime surface. Before this lane the repo-local
config's ``providers`` selection was *read and schema-validated* by the #12190
loader but never resolved against the live registry — the staged gap the
plugin-ready adapter boundary doc called out (``providers`` "have no runtime
resolution seam yet"). This module closes that gap, mirroring the #12191 CLI
composition wiring exactly:

- the **schema** layer (:meth:`ProviderSelectionConfig.from_record`) validates
  the selection's *shape* — closed keys, typed values, and rejection of the
  exact core-owned authority names;
- this **runtime** layer resolves that selection against the providers this
  build actually ships, so a present-but-invalid selection fails closed at the
  entrypoint rather than passing silently. This is the provider analogue of
  :meth:`BuiltinCliModuleRegistry.resolve_enabled`, which the CLI composition
  seam already calls.

What this layer deliberately does — and does not — do:

- It resolves the configured selection to concrete
  :class:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.BuiltinProvider` *descriptions*
  via :meth:`BuiltinProviderRegistry.resolve_selection`. The default (no
  selection) resolves every populated category to its current built-in default
  (``ticket`` -> ``redmine``, ``terminal_runtime`` -> ``tmux``, ``presentation``
  -> ``tmux-presentation``), so a missing / empty config leaves behavior
  unchanged.
- It fails closed on a selection that names an unknown provider id, an unknown
  category, or a category/provider mismatch — exactly the registry errors that
  shape-only schema validation cannot catch (authority-shaped category / provider
  names already fail at :class:`ProviderSelectionConfig` construction).
- It loads no code: the registry maps ids to pure descriptions, never to a
  module path, callable, or entry point, so resolution can never import or run a
  provider. No public ABI is added and no provider authority is delegated; the
  resolved mapping is internal.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry import (
    BUILTIN_PROVIDER_REGISTRY,
    BuiltinProvider,
    ProviderCategory,
    ProviderSelectionConfig,
)


def resolve_builtin_providers(
    config: Optional[ProviderSelectionConfig] = None,
) -> dict[ProviderCategory, BuiltinProvider]:
    """Resolve the configured built-in provider selection at runtime.

    Delegates to :meth:`BuiltinProviderRegistry.resolve_selection` on the
    module-level :data:`BUILTIN_PROVIDER_REGISTRY`, which validates the selection
    against the providers this build actually ships (fail-closed on an unknown
    category, an unknown provider id, or a category/provider mismatch) and
    returns each populated category mapped to its selected — or, by default,
    current built-in — provider. ``None`` / the default config resolves to the
    current built-ins, so the default composition is behavior-preserving.

    Raises :class:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.ProviderRegistryError`
    (a :class:`ValueError`) on any invalid selection, so a caller may fail closed
    with a single ``except`` — the same fail-closed contract the CLI family
    resolution uses at the composition entrypoint.
    """
    return BUILTIN_PROVIDER_REGISTRY.resolve_selection(config)


__all__ = ("resolve_builtin_providers",)
