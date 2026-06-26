"""Runtime resolution of the built-in presentation surface selection (Redmine #12251).

This is the presentation analogue of
:mod:`mozyo_bridge.application.provider_runtime` (Redmine #12249): it connects
the internal presentation-selection layer
(:class:`~mozyo_bridge.domain.repo_local_config.PresentationSelectionConfig`,
Redmine #12189) to a real runtime surface — the configured projection *surface*
is resolved against the built-in presentation providers this build actually
ships. Before this lane the repo-local config's ``presentation`` selection was
*read and schema-validated* by the #12190 loader but never resolved to a
concrete provider — the staged gap the plugin-ready adapter boundary doc called
out (``presentation`` "providers hardcode their surface and have no runtime
resolution seam yet"). This module closes that gap, mirroring the #12191 CLI
composition and #12249 provider resolution wiring:

- the **schema** layer (:meth:`PresentationSelectionConfig.from_record`)
  validates the selection's *shape* — closed keys, a string surface, and
  rejection of any surface outside the core-owned
  :data:`~mozyo_bridge.domain.presentation_adapter.PRESENTATION_SURFACES`
  vocabulary (an unknown surface, or a target / pane / route / send / approve /
  credential-shaped key, fails closed there);
- this **runtime** layer resolves that validated surface to the concrete
  built-in :class:`~mozyo_bridge.domain.presentation_adapter.PresentationProvider`
  that owns it, so a present-but-unrealizable selection fails closed at the
  entrypoint rather than passing silently. This is the presentation analogue of
  :meth:`BuiltinProviderRegistry.resolve_selection`.

What this layer deliberately does — and does not — do:

- It maps a configured surface to the built-in provider that *already* projects
  onto it. The surface -> provider table is built from the providers' own
  ``surface`` attributes, so the resolution and the providers can never drift
  apart (the same single-source-of-truth discipline the tmux provider uses for
  its option names). The default selection (``tmux_user_option``) resolves to the
  tmux provider, so a missing / empty config leaves projection behavior
  unchanged.
- It fails closed on a surface that is core-recognized but has no built-in
  provider yet — the case shape-only schema validation cannot catch (an
  unrecognized surface, like an authority-shaped or target-shaped config, already
  fails at :class:`PresentationSelectionConfig` construction). The failure is a
  :class:`PresentationRuntimeError` (a :class:`ValueError`), so a caller may fail
  closed with a single ``except`` — the same fail-closed contract the provider
  resolution uses.
- Resolution selects which built-in projection provider renders display; it
  loads no code (the providers are imported built-in singletons, never a module
  path / callable / entry point), adds no public ABI, and delegates no routing /
  owner approval / close / workflow authority. The boundary is read / projection
  first: a presentation selection can only choose *how* core records are
  displayed, never *what* is true.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.application.text_attention_presentation_provider import (
    TEXT_ATTENTION_PRESENTATION_PROVIDER,
)
from mozyo_bridge.application.tmux_attention_presentation_provider import (
    TMUX_ATTENTION_PRESENTATION_PROVIDER,
)
from mozyo_bridge.domain.presentation_adapter import PresentationProvider
from mozyo_bridge.domain.repo_local_config import PresentationSelectionConfig


class PresentationRuntimeError(ValueError):
    """A presentation selection cannot be resolved to a built-in provider.

    Raised when a core-recognized surface has no built-in projection provider in
    this build. It subclasses :class:`ValueError` so the CLI entrypoint can fail
    closed on it alongside the other repo-local config resolution errors.
    """


# The built-in presentation providers this build ships. Keyed by each provider's
# own ``surface`` attribute so the resolution table is *derived from* the
# providers, never a hand-maintained parallel list — a provider and its surface
# selection can therefore never disagree about which surface it serves. Adding a
# new built-in surface provider here is the only way to make a new surface
# runtime-resolvable; the surface vocabulary itself stays core-owned in
# :data:`PRESENTATION_SURFACES`.
_BUILTIN_PRESENTATION_PROVIDERS: tuple[PresentationProvider, ...] = (
    TMUX_ATTENTION_PRESENTATION_PROVIDER,
    TEXT_ATTENTION_PRESENTATION_PROVIDER,
)

_PRESENTATION_PROVIDERS_BY_SURFACE: dict[str, PresentationProvider] = {
    provider.surface: provider for provider in _BUILTIN_PRESENTATION_PROVIDERS
}


def resolve_presentation_provider(
    config: Optional[PresentationSelectionConfig] = None,
) -> PresentationProvider:
    """Resolve the configured presentation surface to its built-in provider.

    ``None`` / the default config resolves to the tmux provider (the
    behavior-preserving default surface ``tmux_user_option``), so a missing /
    empty ``presentation`` block never changes how attention is projected. A
    non-default but realizable selection (e.g. ``text``) resolves to that
    surface's built-in provider.

    The ``config.surface`` is already constrained to
    :data:`PRESENTATION_SURFACES` by :class:`PresentationSelectionConfig`
    construction, so an unrecognized / authority- / target-shaped surface never
    reaches here. This layer fails closed on the remaining case schema validation
    cannot see — a core-recognized surface with no built-in provider — by raising
    :class:`PresentationRuntimeError`.
    """
    if config is None:
        config = PresentationSelectionConfig.default()
    surface = config.surface
    provider = _PRESENTATION_PROVIDERS_BY_SURFACE.get(surface)
    if provider is None:
        # Defensive: the surface is core-recognized (it passed config
        # validation) but no built-in provider projects onto it in this build.
        # Fail closed rather than silently returning the default surface — a
        # selection that cannot be realized must surface as an error.
        raise PresentationRuntimeError(
            f"presentation surface {surface!r} is core-recognized but has no "
            f"built-in projection provider in this build; resolvable surfaces: "
            f"{sorted(_PRESENTATION_PROVIDERS_BY_SURFACE)}"
        )
    return provider


__all__ = (
    "PresentationRuntimeError",
    "resolve_presentation_provider",
)
