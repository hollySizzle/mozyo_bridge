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

On top of the classification skeleton, Redmine #12184 adds the smallest
*provider-selection* layer: a typed, internal-only :class:`ProviderSelectionConfig`
(category -> chosen built-in provider id) and the registry's
:meth:`BuiltinProviderRegistry.resolve_selection` /
:meth:`BuiltinProviderRegistry.resolve_provider`. The default (empty config)
resolves every populated category to its current built-in default, so behavior
is unchanged. A selection may only name a provider id already registered in the
built-in registry *and* sitting in the selected category — there is still no
module path, callable, entry point, or dynamic import, so selection can never
introduce foreign code. Unknown category, unknown provider id, category/provider
mismatch, unknown config key, invalid type, and authority-shaped fields all fail
closed. This is the provider-side analogue of the CLI module registry's
``CliCompositionConfig`` / ``resolve_enabled`` (Redmine #12155).
"""

from __future__ import annotations

from collections.abc import Mapping
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


def _normalize_selections(
    value: object, *, source: str
) -> tuple[tuple[str, str], ...]:
    """Normalize a category->provider-id selection into validated, sorted pairs.

    Accepts either a :class:`~collections.abc.Mapping` (the common case) or an
    iterable of ``(category, provider_id)`` pairs; both normalize to a sorted
    tuple of pairs so the config stays frozen, hashable, and deterministic. A
    bare ``str``/``bytes`` is rejected (it is iterable but not a mapping of
    selections). Every key and value must be a non-empty ``str``; a duplicate
    category key, or a key/value naming a member of
    :data:`FORBIDDEN_PROVIDER_AUTHORITIES`, is rejected here so an authority-shaped
    field can never be smuggled into a provider selection. Category and provider
    *existence* are not checked here — that needs the registry and happens at
    resolution.
    """
    if isinstance(value, (str, bytes)):
        raise ProviderRegistryError(
            f"{source} selections must be a mapping of category->provider id, "
            f"not a bare {type(value).__name__}"
        )
    if isinstance(value, Mapping):
        items: list[tuple[object, object]] = list(value.items())
    else:
        try:
            raw = list(value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ProviderRegistryError(
                f"{source} selections must be a mapping or an iterable of "
                f"(category, provider id) pairs, got {type(value).__name__}"
            ) from exc
        items = []
        for pair in raw:
            if isinstance(pair, (str, bytes)) or not hasattr(pair, "__len__"):
                raise ProviderRegistryError(
                    f"{source} selections pairs must be (category, provider id) "
                    f"2-tuples; got {pair!r}"
                )
            if len(pair) != 2:
                raise ProviderRegistryError(
                    f"{source} selections pairs must have exactly 2 elements; "
                    f"got {pair!r}"
                )
            items.append((pair[0], pair[1]))
    seen: dict[str, str] = {}
    for key, prov in items:
        if not isinstance(key, str) or not key:
            raise ProviderRegistryError(
                f"{source} selection category keys must be non-empty strings; "
                f"got {key!r}"
            )
        if not isinstance(prov, str) or not prov:
            raise ProviderRegistryError(
                f"{source} selection provider ids must be non-empty strings; "
                f"got {prov!r} (for category {key!r})"
            )
        if key in FORBIDDEN_PROVIDER_AUTHORITIES or prov in FORBIDDEN_PROVIDER_AUTHORITIES:
            raise ProviderRegistryError(
                f"{source} selection may not name a core-owned authority "
                f"({key!r} -> {prov!r}); workflow / owner / close / routing "
                f"authority stays core-owned and is never a category or provider."
            )
        if key in seen:
            raise ProviderRegistryError(
                f"{source} selects category {key!r} more than once"
            )
        seen[key] = prov
    return tuple(sorted(seen.items()))


@dataclass(frozen=True)
class ProviderSelectionConfig:
    """Internal-only provider-selection config: category -> chosen provider id.

    The *only* thing config may do is name, per category, which already-registered
    built-in provider id to select. It cannot add a provider, supply a module
    path / callable / entry point, choose a provider outside the built-in
    registry, or grant authority — selection keys/values naming a
    :data:`FORBIDDEN_PROVIDER_AUTHORITIES` member are rejected at construction.

    The default (empty ``selections``) resolves every populated category to its
    current built-in default, so the default composition is behavior-preserving.
    Construction validates structure / types / authority shape only; whether a
    selected category and provider actually exist (and match) is a registry
    decision made in :meth:`BuiltinProviderRegistry.resolve_selection`.

    ``selections`` accepts a mapping (``{"ticket": "redmine"}``) or an iterable
    of pairs and is normalized to a sorted tuple of ``(category, provider_id)``
    pairs so the record stays frozen and hashable.
    """

    selections: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selections",
            _normalize_selections(self.selections, source="provider selection config"),
        )

    @classmethod
    def default(cls) -> "ProviderSelectionConfig":
        """The behavior-preserving default: no selections, current defaults."""
        return cls()

    @classmethod
    def from_record(cls, record: object) -> "ProviderSelectionConfig":
        """Build a config from a raw typed record, failing closed on stray keys.

        The record must be a mapping whose only recognized key is ``selections``.
        Any other top-level key is rejected (typo / authority-smuggling
        protection) rather than silently ignored, and a non-mapping record is an
        invalid type. This is the entry point for parsing an external/serialized
        config shape into the typed record.
        """
        if not isinstance(record, Mapping):
            raise ProviderRegistryError(
                f"provider selection record must be a mapping, got "
                f"{type(record).__name__}"
            )
        allowed = {"selections"}
        unknown = set(record) - allowed
        if unknown:
            raise ProviderRegistryError(
                f"unknown provider selection config key(s) {sorted(unknown)}; "
                f"allowed keys: {sorted(allowed)}"
            )
        return cls(selections=record.get("selections", ()))

    def selection_for(self, category: "ProviderCategory") -> Optional[str]:
        """The provider id selected for ``category``, or ``None`` if unselected."""
        if not isinstance(category, ProviderCategory):
            raise ProviderRegistryError(
                f"category must be a ProviderCategory, got {category!r}"
            )
        for key, prov in self.selections:
            if key == category.value:
                return prov
        return None

    @property
    def mapping(self) -> dict[str, str]:
        """The selections as a plain ``{category: provider_id}`` dict (a copy)."""
        return dict(self.selections)


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

    def _validate_selection_config(self, config: ProviderSelectionConfig) -> None:
        """Fail-closed check that every selection names a real built-in provider.

        Rejects (a) a selection keyed by a category name that is not a known
        :class:`ProviderCategory` (unknown category), (b) a provider id that is
        not registered (unknown provider), and (c) a registered provider that
        sits in a different category than the one selecting it (category/provider
        mismatch). Structure / type / authority-shape are already enforced by
        :class:`ProviderSelectionConfig`.
        """
        known = {c.value for c in ProviderCategory}
        unknown_categories = sorted(k for k, _ in config.selections if k not in known)
        if unknown_categories:
            raise ProviderRegistryError(
                f"config selects unknown categor(ies) {unknown_categories}; "
                f"known categories: {sorted(known)}"
            )
        for cat_value, provider_id in config.selections:
            provider = self.get(provider_id)
            if provider is None:
                raise ProviderRegistryError(
                    f"config selects unknown provider id {provider_id!r} for "
                    f"category {cat_value!r}; registered ids: {sorted(self._by_id)}"
                )
            if provider.category.value != cat_value:
                raise ProviderRegistryError(
                    f"provider {provider_id!r} is category "
                    f"{provider.category.value!r}, not {cat_value!r} "
                    f"(category/provider mismatch)"
                )

    def _unambiguous_default(
        self, category: ProviderCategory
    ) -> Optional[BuiltinProvider]:
        """The implicit default provider for ``category`` when config selects none.

        Returns the sole registered provider in the category (the current
        built-in shape: one provider per populated category), ``None`` when the
        category is empty, and **raises** when more than one provider is
        registered — an ambiguous category has no implicit default and requires
        an explicit selection (fail-closed).
        """
        members = self.by_category(category)
        if not members:
            return None
        if len(members) > 1:
            raise ProviderRegistryError(
                f"category {category.value!r} has multiple built-in providers "
                f"{[m.provider_id for m in members]} and no selection; an "
                f"explicit provider selection is required (no implicit default)."
            )
        return members[0]

    def resolve_provider(
        self,
        category: ProviderCategory,
        config: Optional[ProviderSelectionConfig] = None,
    ) -> BuiltinProvider:
        """Resolve the selected built-in provider for one ``category``.

        With the default (or ``None``) config the category's current built-in
        default is returned; a config may select another registered provider in
        the same category. Fails closed on every invalid selection (see
        :meth:`_validate_selection_config`) and on a category that has no
        resolvable provider (empty, or ambiguous with no selection).
        """
        if not isinstance(category, ProviderCategory):
            raise ProviderRegistryError(
                f"category must be a ProviderCategory, got {category!r}"
            )
        if config is None:
            config = ProviderSelectionConfig.default()
        self._validate_selection_config(config)
        chosen = config.selection_for(category)
        if chosen is not None:
            # Existence + category match were checked above.
            provider = self.get(chosen)
            assert provider is not None  # narrow for type-checkers
            return provider
        default = self._unambiguous_default(category)
        if default is None:
            raise ProviderRegistryError(
                f"category {category.value!r} has no resolvable provider: the "
                f"config selects none and no built-in provider is registered "
                f"for it."
            )
        return default

    def resolve_selection(
        self, config: Optional[ProviderSelectionConfig] = None
    ) -> dict[ProviderCategory, BuiltinProvider]:
        """Resolve every category that has a provider, for ``config``.

        Categories with a selection resolve to the selected provider; categories
        without a selection resolve to their unambiguous built-in default.
        Empty categories (no built-in provider, e.g. ``catalog`` / ``telemetry``)
        are simply absent from the result. An ambiguous category with no
        selection fails closed. With the default config the result is each
        populated category mapped to its current built-in default.
        """
        if config is None:
            config = ProviderSelectionConfig.default()
        self._validate_selection_config(config)
        resolved: dict[ProviderCategory, BuiltinProvider] = {}
        for category in ProviderCategory:
            chosen = config.selection_for(category)
            if chosen is not None:
                provider = self.get(chosen)
                assert provider is not None  # narrow; validated above
                resolved[category] = provider
                continue
            default = self._unambiguous_default(category)
            if default is not None:
                resolved[category] = default
        return resolved

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
    # The tmux attention presentation provider seam landed in #12156. It is
    # read/projection-first: it projects core attention records onto pane user
    # options and never owns routing or approval. ``provider_id`` matches the
    # real TmuxAttentionPresentationProvider.name.
    registry.register(
        BuiltinProvider(
            category=ProviderCategory.PRESENTATION,
            provider_id="tmux-presentation",
            summary="Built-in tmux attention presentation provider (projects "
            "core attention records onto pane user options; read-only).",
            capabilities=frozenset(
                {
                    "project_attention",
                    "tmux_user_option_projection",
                }
            ),
            safety_constraints=frozenset(
                {
                    "projection_only",
                    "no_routing_authority",
                    "no_owner_approval_decision",
                    "re_derivable_cache",
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
    "ProviderSelectionConfig",
)
