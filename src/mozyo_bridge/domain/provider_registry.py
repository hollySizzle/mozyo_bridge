"""Internal-only built-in provider registry skeleton (Redmine #12035).

This is the smallest classification layer for the built-in adapter boundary
designed in ``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine
#12001). It lets the codebase *name and classify* the built-in providers it
already ships — the Redmine ticket provider (Redmine #12034), the tmux runtime,
and the future ticket / presentation / catalog / telemetry categories — without
inventing any of the machinery a real plugin system would need.

It is deliberately **not** a plugin API:

- there is no dynamic import, no third-party entry point, and no user-script
  loading. A provider is registered by handing this module a pure
  :class:`BuiltinProvider` *description*, never a module path or a callable, so
  registration can never execute foreign code;
- there is no public ABI and no compatibility promise. These data shapes are
  internal and may change with no deprecation window;
- the registry classifies providers; it does **not** hand them authority. The
  authorities that stay core-owned — workflow gate truth, owner / close
  approval, and routing — are enumerated in
  :data:`FORBIDDEN_PROVIDER_AUTHORITIES`, and a :class:`BuiltinProvider` that
  tries to claim one as a capability is rejected at construction. This mirrors
  the ticket adapter seam, where ``classify_workflow_gate`` / ``owner_approval``
  are core functions a provider can never own.

The module is pure (dataclasses + a small in-memory mapping) and imports no
provider implementation, so the dependency only ever points provider -> core,
exactly like ``mozyo_bridge.domain.ticket_adapter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional


class ProviderCategory(str, Enum):
    """The built-in adapter categories from the design doc's Adapter Categories.

    These are the *core-owned* category names. A provider is classified into
    exactly one of them; new categories are added here in core, never supplied
    by a provider. The string values are stable enough to use as registry keys
    but carry no public-ABI promise.
    """

    TICKET = "ticket"
    PRESENTATION = "presentation"
    TERMINAL_RUNTIME = "terminal_runtime"
    CATALOG = "catalog"
    TELEMETRY = "telemetry"
    RELEASE_HELPER = "release_helper"


# Authorities core never delegates to a provider. A provider observes and
# supplies data; these decisions stay in core (see the ticket adapter seam:
# ``classify_workflow_gate`` and ``owner_approval`` are core functions, and the
# built-in provider exposes no approval/gate/routing API at all). Listing them
# here lets the registry *enforce* the boundary instead of only documenting it.
FORBIDDEN_PROVIDER_AUTHORITIES: frozenset[str] = frozenset(
    {
        "workflow_authority",
        "owner_approval",
        "close_approval",
        "routing_authority",
    }
)


class ProviderRegistryError(ValueError):
    """A built-in provider description or registration violates the contract."""


def _frozen_label_set(value: object, *, field: str, provider_id: str) -> frozenset[str]:
    """Normalize a descriptive label collection into a validated ``frozenset``.

    A bare ``str``/``bytes`` is **rejected** rather than normalized: both are
    iterable, so ``frozenset("owner_approval")`` would silently become a set of
    single characters and slip past the :data:`FORBIDDEN_PROVIDER_AUTHORITIES`
    check — exactly the authority leak this seam must prevent. Each entry must
    be a non-empty ``str``; any other input raises
    :class:`ProviderRegistryError`. ``list`` / ``tuple`` / ``set`` / ``frozenset``
    of strings normalize as before.
    """
    if isinstance(value, (str, bytes)):
        raise ProviderRegistryError(
            f"provider {provider_id!r} {field} must be a collection of strings, "
            f"not a bare {type(value).__name__}; a bare string is iterated "
            f"character-by-character and would bypass the authority check"
        )
    try:
        items = frozenset(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ProviderRegistryError(
            f"provider {provider_id!r} {field} must be an iterable of strings, "
            f"got {type(value).__name__}"
        ) from exc
    for item in items:
        if not isinstance(item, str) or not item:
            raise ProviderRegistryError(
                f"provider {provider_id!r} {field} entries must be non-empty "
                f"strings; got {item!r}"
            )
    return items


@dataclass(frozen=True)
class BuiltinProvider:
    """A pure description of one built-in provider — classification, not code.

    Fields:

    - ``category``: which :class:`ProviderCategory` this provider serves.
    - ``provider_id``: stable internal id (e.g. ``"redmine"``), unique within
      the registry.
    - ``summary``: one-line human description; public-safe, no private policy.
    - ``capabilities``: what mechanics the provider performs (e.g.
      ``"normalize_issue"``). Purely descriptive. It must **not** name any of
      :data:`FORBIDDEN_PROVIDER_AUTHORITIES`; doing so is rejected here so a
      provider can never declare itself an authority core reserves.
    - ``safety_constraints``: invariants the provider is required to uphold
      (e.g. ``"no_network_in_normalization"``). Recorded for review/audit; the
      registry does not execute them.
    - ``experimental``: ``True`` marks a not-yet-stable classification.

    The dataclass is frozen and holds no behavior; it is metadata about a
    provider, never a handle to one.
    """

    category: ProviderCategory
    provider_id: str
    summary: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    safety_constraints: frozenset[str] = field(default_factory=frozenset)
    experimental: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.category, ProviderCategory):
            raise ProviderRegistryError(
                f"category must be a ProviderCategory, got {self.category!r}"
            )
        if not self.provider_id:
            raise ProviderRegistryError("provider_id must be a non-empty string")
        # Normalize the descriptive sets to frozensets so callers may pass any
        # iterable of strings while the record stays immutable and hashable. A
        # bare str/bytes is rejected (see _frozen_label_set) so a single
        # forbidden-authority string can never be exploded into characters that
        # slip past the authority check below.
        object.__setattr__(
            self,
            "capabilities",
            _frozen_label_set(
                self.capabilities, field="capabilities", provider_id=self.provider_id
            ),
        )
        object.__setattr__(
            self,
            "safety_constraints",
            _frozen_label_set(
                self.safety_constraints,
                field="safety_constraints",
                provider_id=self.provider_id,
            ),
        )
        leaked = self.capabilities & FORBIDDEN_PROVIDER_AUTHORITIES
        if leaked:
            raise ProviderRegistryError(
                f"provider {self.provider_id!r} may not claim core-owned "
                f"authority as a capability: {sorted(leaked)}. These stay "
                f"core decisions (see plugin-ready-adapter-boundary.md)."
            )


class BuiltinProviderRegistry:
    """An in-memory classification of built-in providers.

    Registration takes a :class:`BuiltinProvider` description only — there is no
    code path that loads, imports, or executes a provider, so this is a
    catalogue, not an extension point. Ids are unique; re-registering an id is
    an error rather than a silent overwrite.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, BuiltinProvider] = {}

    def register(self, provider: BuiltinProvider) -> BuiltinProvider:
        """Record one built-in provider description; reject duplicate ids."""
        if not isinstance(provider, BuiltinProvider):
            raise ProviderRegistryError(
                "register expects a BuiltinProvider description, not "
                f"{type(provider).__name__}; the registry never loads code."
            )
        if provider.provider_id in self._by_id:
            raise ProviderRegistryError(
                f"duplicate provider id: {provider.provider_id!r}"
            )
        self._by_id[provider.provider_id] = provider
        return provider

    def get(self, provider_id: str) -> Optional[BuiltinProvider]:
        """Return the provider with ``provider_id``, or ``None`` if unregistered."""
        return self._by_id.get(provider_id)

    def providers(self) -> tuple[BuiltinProvider, ...]:
        """All registered providers, ordered by id for stable output."""
        return tuple(
            self._by_id[pid] for pid in sorted(self._by_id)
        )

    def by_category(
        self, category: ProviderCategory
    ) -> tuple[BuiltinProvider, ...]:
        """Registered providers in ``category``, ordered by id (possibly empty)."""
        return tuple(p for p in self.providers() if p.category is category)

    @staticmethod
    def categories() -> tuple[ProviderCategory, ...]:
        """Every known built-in category, whether or not a provider exists yet.

        A category with no provider is still a valid, expressible classification
        — that is the whole point of a skeleton: future ticket / presentation /
        catalog / telemetry providers have a home before they are written.
        """
        return tuple(ProviderCategory)

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._by_id

    def __iter__(self) -> Iterator[BuiltinProvider]:
        return iter(self.providers())

    def __len__(self) -> int:
        return len(self._by_id)


def _seed_builtin_registry() -> BuiltinProviderRegistry:
    """Build the registry of providers this codebase actually ships today.

    Only providers that already exist as built-in code are listed; their ids
    match the real implementations (``redmine`` -> the ticket provider singleton,
    ``tmux`` -> the runtime client). The remaining categories stay intentionally
    empty until a provider is written for them — no placeholder is invented.
    """
    registry = BuiltinProviderRegistry()
    # The Redmine ticket provider seam landed in #12034. Capabilities mirror the
    # RedmineTicketProvider mechanics; the safety constraints restate the
    # core-owned boundary the provider must never cross.
    registry.register(
        BuiltinProvider(
            category=ProviderCategory.TICKET,
            provider_id="redmine",
            summary="Built-in Redmine ticket provider (normalizes API/anchor "
            "shapes into core ticket records).",
            capabilities=frozenset(
                {
                    "normalize_issue",
                    "normalize_journals",
                    "normalize_comments",
                    "refs_from_anchor",
                    "issue_url",
                }
            ),
            safety_constraints=frozenset(
                {
                    "no_workflow_gate_classification",
                    "no_owner_approval_decision",
                    "no_network_in_normalization",
                    "no_subject_surface",
                }
            ),
        )
    )
    # tmux is the built-in terminal runtime. It observes liveness and delivers
    # sends; it is never durable identity and holds no routing authority.
    registry.register(
        BuiltinProvider(
            category=ProviderCategory.TERMINAL_RUNTIME,
            provider_id="tmux",
            summary="Built-in tmux terminal runtime provider (pane send / "
            "capture / listing, fail-closed).",
            capabilities=frozenset(
                {
                    "pane_send",
                    "pane_capture",
                    "pane_listing",
                    "target_preflight",
                }
            ),
            safety_constraints=frozenset(
                {
                    "fail_closed_on_ambiguous_target",
                    "not_durable_identity",
                    "observation_is_not_completion_truth",
                }
            ),
        )
    )
    return registry


# Module-level singleton: the classification of today's built-in providers.
# Seeded once at import from pure descriptions; nothing here loads provider code.
BUILTIN_PROVIDER_REGISTRY = _seed_builtin_registry()


__all__ = (
    "BUILTIN_PROVIDER_REGISTRY",
    "BuiltinProvider",
    "BuiltinProviderRegistry",
    "FORBIDDEN_PROVIDER_AUTHORITIES",
    "ProviderCategory",
    "ProviderRegistryError",
)
