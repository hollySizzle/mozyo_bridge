"""Provider ghost-composer render-signal schema (Redmine #14065 Phase 2).

The closed ``ghost_composer_signals`` list a provider profile may carry: the render
``style_provenance`` value(s) that positively identify this provider's *ghost* idle
composer — a placeholder rendered into the composer that is NOT real unsent input
(#14064). Phase 1 (#14065) proved the discriminator is the render style, and the
live item-7 diagnostic (j#82180) admitted **exactly one** value across both built-in
providers: ``dim``. ``normal`` / ``mixed`` / ``unknown`` are never a ghost signal —
they preserve — so the admitted set is a strict, hardcoded subset of the render
vocabulary and a profile that names anything else fails closed at load.

A profile that declares no ``ghost_composer_signals`` admits *no* ghost signal: a
dim render for that provider is preserved, never emptied. So the field is opt-in
per provider and its absence is the fail-closed default (IR j#82181 item 1: schema
migration / legacy profile default is fail-closed preserve).

Kept in its own leaf module — like ``agent_provider_startup_blocker`` — so the
oversized ``agent_provider_profile_config`` (module-health) gains only the small
wiring, and the closed vocabulary + validator stay cohesive. Dependency direction:
this borrows the render vocabulary from the sibling ``f_130`` terminal-runtime
domain (both are ``e_140`` provider-adapter domains) and the shared error lazily
from ``agent_provider_profile_config`` (inside the raising path), so there is no
import cycle.
"""

from __future__ import annotations

from collections.abc import Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.pane_render_observation import (  # noqa: E501
    STYLE_PROVENANCE_DIM,
)

#: The render ``style_provenance`` values admitted as a ghost-composer signal. Live
#: admission j#82180 established exactly ``dim`` (stable 3/3 cross-provider on held
#: ghosts, separated from the exact-same-text ``normal`` real input). This is a
#: STRICT subset of the render vocabulary; growing it is a deliberate, reviewable act
#: gated on new live evidence, never a silent widening.
ADMITTED_GHOST_COMPOSER_SIGNALS: frozenset[str] = frozenset({STYLE_PROVENANCE_DIM})

#: A provider carries at most this many signals (today: one — ``dim``). A larger list
#: is a data mistake, not a real contract, given the admitted set is a singleton.
MAX_GHOST_COMPOSER_SIGNALS = len(ADMITTED_GHOST_COMPOSER_SIGNALS)


def normalize_ghost_composer_signals(
    value: object, *, provider_id: str
) -> tuple[str, ...]:
    """Validate a profile's ``ghost_composer_signals`` into a frozen tuple (fail-closed).

    Rejects a non-list, a non-string / blank entry, a value outside
    :data:`ADMITTED_GHOST_COMPOSER_SIGNALS` (so ``normal`` / ``mixed`` / ``unknown``
    can never be declared a ghost signal), a duplicate, and an over-long list. The
    empty list is valid and means "no ghost signal admitted" (the fail-closed default).
    """
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
        AgentProviderProfileError,
    )

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AgentProviderProfileError(
            f"agent provider profile {provider_id!r} 'ghost_composer_signals' must be "
            f"a list of admitted render style-provenance strings, got "
            f"{type(value).__name__}"
        )
    signals: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} 'ghost_composer_signals' "
                f"entries must be non-empty strings; got {entry!r}"
            )
        token = entry.strip()
        if token not in ADMITTED_GHOST_COMPOSER_SIGNALS:
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} 'ghost_composer_signals' entry "
                f"{token!r} is not an admitted ghost signal; allowed: "
                f"{sorted(ADMITTED_GHOST_COMPOSER_SIGNALS)}. Only a render style that has "
                f"been live-admitted (j#82180: 'dim') may empty a pending composer; "
                f"normal / mixed / unknown always preserve."
            )
        if token in signals:
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} declares duplicate "
                f"'ghost_composer_signals' entry {token!r}"
            )
        signals.append(token)
    if len(signals) > MAX_GHOST_COMPOSER_SIGNALS:
        raise AgentProviderProfileError(
            f"agent provider profile {provider_id!r} declares {len(signals)} "
            f"ghost_composer_signals; the bound is {MAX_GHOST_COMPOSER_SIGNALS}"
        )
    return tuple(signals)


__all__ = (
    "ADMITTED_GHOST_COMPOSER_SIGNALS",
    "MAX_GHOST_COMPOSER_SIGNALS",
    "normalize_ghost_composer_signals",
)
