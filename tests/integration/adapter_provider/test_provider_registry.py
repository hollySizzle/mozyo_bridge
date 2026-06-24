"""Internal built-in provider registry skeleton tests (Redmine #12035).

Pins the smallest classification layer for the built-in adapter boundary
(Redmine #12001 design doc): the core-owned category vocabulary, the pure
:class:`BuiltinProvider` description, the enforced rule that a provider may not
claim a core-owned authority, the in-memory registry lookups, and the
non-goals (no dynamic loading / no plugin entry point). No network or provider
code is exercised here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.provider_registry import (
    BUILTIN_PROVIDER_REGISTRY,
    FORBIDDEN_PROVIDER_AUTHORITIES,
    BuiltinProvider,
    BuiltinProviderRegistry,
    ProviderCategory,
    ProviderRegistryError,
    ProviderSelectionConfig,
)
from mozyo_bridge.application.tmux_attention_presentation_provider import (
    PROVIDER_NAME as TMUX_PRESENTATION_PROVIDER_NAME,
)
from mozyo_bridge.infrastructure.redmine_ticket_provider import (
    PROVIDER_NAME as REDMINE_PROVIDER_NAME,
)


class ProviderCategoryVocabularyTest(unittest.TestCase):
    def test_categories_cover_the_design_doc_adapter_categories(self) -> None:
        self.assertEqual(
            {
                "ticket",
                "presentation",
                "terminal_runtime",
                "catalog",
                "telemetry",
                "release_helper",
            },
            {c.value for c in ProviderCategory},
        )

    def test_registry_categories_are_expressible_without_a_provider(self) -> None:
        # The catalog / telemetry categories have no built-in provider yet, but
        # they are still valid classifications a future provider can slot into —
        # that is what the skeleton enables. (presentation gained a provider in
        # #12156; it is covered by SeededBuiltinsTest below.)
        cats = BuiltinProviderRegistry.categories()
        for empty in (
            ProviderCategory.CATALOG,
            ProviderCategory.TELEMETRY,
            ProviderCategory.RELEASE_HELPER,
        ):
            self.assertIn(empty, cats)
            self.assertEqual((), BUILTIN_PROVIDER_REGISTRY.by_category(empty))


class BuiltinProviderDescriptionTest(unittest.TestCase):
    def test_normalizes_iterables_to_frozensets(self) -> None:
        provider = BuiltinProvider(
            category=ProviderCategory.TELEMETRY,
            provider_id="otel",
            summary="example",
            capabilities=["ingest", "ingest", "freshness"],
            safety_constraints=("unknown_on_stale",),
        )
        self.assertEqual(frozenset({"ingest", "freshness"}), provider.capabilities)
        self.assertEqual(
            frozenset({"unknown_on_stale"}), provider.safety_constraints
        )
        # Frozen + hashable so descriptions stay immutable metadata.
        self.assertEqual(provider, provider)
        hash(provider)

    def test_bare_string_capabilities_is_rejected_not_exploded(self) -> None:
        # Regression (review #59179): a bare str is iterable, so a naive
        # frozenset(...) would explode "owner_approval" into single characters
        # and slip a forbidden authority past the check. It must raise instead.
        with self.assertRaises(ProviderRegistryError):
            BuiltinProvider(
                category=ProviderCategory.TICKET,
                provider_id="rogue",
                summary="smuggles authority via a bare string",
                capabilities="owner_approval",
            )
        # A harmless bare string is rejected for the same structural reason.
        with self.assertRaises(ProviderRegistryError):
            BuiltinProvider(
                category=ProviderCategory.TICKET,
                provider_id="rogue",
                summary="x",
                capabilities="normalize_issue",
            )
        # bytes are rejected on the same grounds.
        with self.assertRaises(ProviderRegistryError):
            BuiltinProvider(
                category=ProviderCategory.TELEMETRY,
                provider_id="rogue",
                summary="x",
                safety_constraints=b"unknown_on_stale",
            )

    def test_non_string_and_empty_entries_are_rejected(self) -> None:
        for bad in ({"ok", ""}, {"ok", 1}, {"ok", None}):
            with self.assertRaises(ProviderRegistryError):
                BuiltinProvider(
                    category=ProviderCategory.TICKET,
                    provider_id="rogue",
                    summary="x",
                    capabilities=bad,  # type: ignore[arg-type]
                )

    def test_empty_provider_id_is_rejected(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            BuiltinProvider(
                category=ProviderCategory.TICKET, provider_id="", summary="x"
            )

    def test_non_category_is_rejected(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            BuiltinProvider(
                category="ticket",  # type: ignore[arg-type]
                provider_id="x",
                summary="x",
            )


class ForbiddenAuthorityTest(unittest.TestCase):
    def test_forbidden_authorities_are_the_core_owned_decisions(self) -> None:
        self.assertEqual(
            {
                "workflow_authority",
                "owner_approval",
                "close_approval",
                "routing_authority",
            },
            set(FORBIDDEN_PROVIDER_AUTHORITIES),
        )

    def test_provider_cannot_claim_a_forbidden_authority(self) -> None:
        for authority in FORBIDDEN_PROVIDER_AUTHORITIES:
            with self.assertRaises(ProviderRegistryError, msg=authority):
                BuiltinProvider(
                    category=ProviderCategory.TICKET,
                    provider_id="rogue",
                    summary="tries to grab authority",
                    capabilities={"normalize_issue", authority},
                )

    def test_no_seeded_provider_claims_a_forbidden_authority(self) -> None:
        for provider in BUILTIN_PROVIDER_REGISTRY:
            self.assertEqual(
                frozenset(),
                provider.capabilities & FORBIDDEN_PROVIDER_AUTHORITIES,
                msg=provider.provider_id,
            )


class RegistryLookupTest(unittest.TestCase):
    def test_register_rejects_duplicate_id(self) -> None:
        registry = BuiltinProviderRegistry()
        registry.register(
            BuiltinProvider(
                category=ProviderCategory.TICKET,
                provider_id="dup",
                summary="first",
            )
        )
        with self.assertRaises(ProviderRegistryError):
            registry.register(
                BuiltinProvider(
                    category=ProviderCategory.TICKET,
                    provider_id="dup",
                    summary="second",
                )
            )

    def test_register_refuses_non_description_objects(self) -> None:
        # The registry classifies descriptions; it never accepts a module path,
        # callable, or live object to "load" — that is the plugin-loading
        # non-goal expressed as a guard.
        registry = BuiltinProviderRegistry()
        for not_a_description in ("mozyo_bridge.something", object(), lambda: None):
            with self.assertRaises(ProviderRegistryError):
                registry.register(not_a_description)  # type: ignore[arg-type]

    def test_get_and_contains(self) -> None:
        self.assertIn("redmine", BUILTIN_PROVIDER_REGISTRY)
        self.assertIsNone(BUILTIN_PROVIDER_REGISTRY.get("does-not-exist"))
        self.assertNotIn("does-not-exist", BUILTIN_PROVIDER_REGISTRY)

    def test_providers_are_id_sorted(self) -> None:
        ids = [p.provider_id for p in BUILTIN_PROVIDER_REGISTRY.providers()]
        self.assertEqual(sorted(ids), ids)


class SeededBuiltinsTest(unittest.TestCase):
    def test_redmine_ticket_provider_is_classified(self) -> None:
        provider = BUILTIN_PROVIDER_REGISTRY.get("redmine")
        self.assertIsNotNone(provider)
        assert provider is not None  # narrow for type-checkers
        self.assertIs(ProviderCategory.TICKET, provider.category)
        self.assertFalse(provider.experimental)
        # The registry id matches the real built-in provider's name (#12034).
        self.assertEqual(REDMINE_PROVIDER_NAME, provider.provider_id)
        self.assertEqual(
            (provider,), BUILTIN_PROVIDER_REGISTRY.by_category(ProviderCategory.TICKET)
        )

    def test_tmux_runtime_provider_is_classified(self) -> None:
        provider = BUILTIN_PROVIDER_REGISTRY.get("tmux")
        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertIs(ProviderCategory.TERMINAL_RUNTIME, provider.category)
        self.assertIn("not_durable_identity", provider.safety_constraints)

    def test_tmux_presentation_provider_is_classified(self) -> None:
        provider = BUILTIN_PROVIDER_REGISTRY.get("tmux-presentation")
        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertIs(ProviderCategory.PRESENTATION, provider.category)
        # The registry id matches the real built-in provider's name (#12156).
        self.assertEqual(TMUX_PRESENTATION_PROVIDER_NAME, provider.provider_id)
        # Read-only projection: the constraints restate the boundary, and the
        # registry's authority check (ForbiddenAuthorityTest) already pins that
        # the capabilities claim no core-owned authority.
        self.assertIn("projection_only", provider.safety_constraints)
        self.assertEqual(
            (provider,),
            BUILTIN_PROVIDER_REGISTRY.by_category(ProviderCategory.PRESENTATION),
        )


def _two_ticket_registry() -> BuiltinProviderRegistry:
    """A registry with two providers in one category, for selection tests.

    The built-in registry seeds exactly one provider per populated category, so
    a *meaningful* "valid selection between alternatives" / "ambiguous default"
    case needs a registry that holds more than one provider in a category.
    """
    registry = BuiltinProviderRegistry()
    registry.register(
        BuiltinProvider(
            category=ProviderCategory.TICKET, provider_id="redmine", summary="a"
        )
    )
    registry.register(
        BuiltinProvider(
            category=ProviderCategory.TICKET, provider_id="asana", summary="b"
        )
    )
    return registry


class ProviderSelectionConfigTest(unittest.TestCase):
    def test_default_is_empty_and_behavior_preserving(self) -> None:
        config = ProviderSelectionConfig.default()
        self.assertEqual((), config.selections)
        self.assertEqual({}, config.mapping)
        self.assertIsNone(config.selection_for(ProviderCategory.TICKET))

    def test_mapping_input_normalizes_to_sorted_pairs(self) -> None:
        config = ProviderSelectionConfig(
            selections={"ticket": "redmine", "terminal_runtime": "tmux"}
        )
        self.assertEqual(
            (("terminal_runtime", "tmux"), ("ticket", "redmine")),
            config.selections,
        )
        self.assertEqual("redmine", config.selection_for(ProviderCategory.TICKET))
        # Frozen + hashable so the typed record stays immutable metadata.
        hash(config)
        self.assertEqual(config, ProviderSelectionConfig(selections={"ticket": "redmine", "terminal_runtime": "tmux"}))

    def test_pairs_iterable_input_is_accepted(self) -> None:
        config = ProviderSelectionConfig(selections=[("ticket", "redmine")])
        self.assertEqual("redmine", config.selection_for(ProviderCategory.TICKET))

    def test_from_record_rejects_unknown_top_level_key(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig.from_record(
                {"selections": {"ticket": "redmine"}, "providers": {}}
            )

    def test_from_record_rejects_non_mapping_record(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig.from_record([("ticket", "redmine")])

    def test_from_record_mixed_type_unknown_keys_fail_closed(self) -> None:
        # Regression (review j#60633 finding 1): mixed-type unknown top-level
        # keys must fail through ProviderRegistryError, not leak the raw
        # TypeError that sorting a mixed-type set would otherwise raise.
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig.from_record({1: "x", "x": "y"})

    def test_mapping_is_not_accepted_as_a_pair(self) -> None:
        # Regression (review j#60633 finding 2): a Mapping with keys 0 and 1 is
        # not a schema-shaped (category, provider) pair and must be rejected,
        # not normalized via pair[0] / pair[1] indexing.
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig.from_record(
                {"selections": [{0: "ticket", 1: "redmine"}]}
            )
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig(selections=[{0: "ticket", 1: "redmine"}])
        # A set is also not an ordered 2-element pair sequence.
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig(selections=[{"ticket", "redmine"}])

    def test_from_record_accepts_selections_only(self) -> None:
        config = ProviderSelectionConfig.from_record(
            {"selections": {"ticket": "redmine"}}
        )
        self.assertEqual("redmine", config.selection_for(ProviderCategory.TICKET))
        self.assertEqual(ProviderSelectionConfig.default(), ProviderSelectionConfig.from_record({}))

    def test_invalid_types_fail_closed(self) -> None:
        for bad in (
            "ticket",  # bare string is not a selection mapping
            {"ticket": 1},  # non-string provider id
            {1: "redmine"},  # non-string category key
            {"ticket": ""},  # empty provider id
            {"": "redmine"},  # empty category key
            42,  # not iterable
        ):
            with self.assertRaises(ProviderRegistryError, msg=repr(bad)):
                ProviderSelectionConfig(selections=bad)  # type: ignore[arg-type]

    def test_authority_shaped_selection_is_rejected(self) -> None:
        for authority in FORBIDDEN_PROVIDER_AUTHORITIES:
            # authority as a category key
            with self.assertRaises(ProviderRegistryError, msg=f"key:{authority}"):
                ProviderSelectionConfig(selections={authority: "redmine"})
            # authority as a selected provider id
            with self.assertRaises(ProviderRegistryError, msg=f"val:{authority}"):
                ProviderSelectionConfig(selections={"ticket": authority})

    def test_duplicate_category_via_pairs_is_rejected(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig(
                selections=[("ticket", "redmine"), ("ticket", "asana")]
            )

    def test_selection_for_requires_a_category(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig().selection_for("ticket")  # type: ignore[arg-type]


class ResolveSelectionTest(unittest.TestCase):
    def test_default_config_resolves_current_builtin_defaults(self) -> None:
        resolved = BUILTIN_PROVIDER_REGISTRY.resolve_selection()
        self.assertEqual(
            "redmine", resolved[ProviderCategory.TICKET].provider_id
        )
        self.assertEqual(
            "tmux", resolved[ProviderCategory.TERMINAL_RUNTIME].provider_id
        )
        self.assertEqual(
            "tmux-presentation",
            resolved[ProviderCategory.PRESENTATION].provider_id,
        )
        # Empty categories have no built-in provider, so they are simply absent.
        self.assertNotIn(ProviderCategory.CATALOG, resolved)
        self.assertNotIn(ProviderCategory.TELEMETRY, resolved)

    def test_resolve_provider_default_matches_singleton(self) -> None:
        provider = BUILTIN_PROVIDER_REGISTRY.resolve_provider(
            ProviderCategory.TICKET
        )
        self.assertEqual("redmine", provider.provider_id)

    def test_explicit_selection_of_registered_provider(self) -> None:
        registry = _two_ticket_registry()
        config = ProviderSelectionConfig(selections={"ticket": "asana"})
        self.assertEqual(
            "asana",
            registry.resolve_provider(ProviderCategory.TICKET, config).provider_id,
        )
        self.assertEqual(
            "asana",
            registry.resolve_selection(config)[ProviderCategory.TICKET].provider_id,
        )

    def test_ambiguous_category_without_selection_fails_closed(self) -> None:
        registry = _two_ticket_registry()
        with self.assertRaises(ProviderRegistryError):
            registry.resolve_provider(ProviderCategory.TICKET)
        with self.assertRaises(ProviderRegistryError):
            registry.resolve_selection()

    def test_unknown_category_selection_fails_closed(self) -> None:
        config = ProviderSelectionConfig(selections=[("not_a_category", "redmine")])
        with self.assertRaises(ProviderRegistryError):
            BUILTIN_PROVIDER_REGISTRY.resolve_selection(config)

    def test_unknown_provider_selection_fails_closed(self) -> None:
        config = ProviderSelectionConfig(selections={"ticket": "ghost"})
        with self.assertRaises(ProviderRegistryError):
            BUILTIN_PROVIDER_REGISTRY.resolve_provider(ProviderCategory.TICKET, config)

    def test_category_provider_mismatch_fails_closed(self) -> None:
        # tmux is a real provider, but it is terminal_runtime, not ticket.
        config = ProviderSelectionConfig(selections={"ticket": "tmux"})
        with self.assertRaises(ProviderRegistryError):
            BUILTIN_PROVIDER_REGISTRY.resolve_selection(config)

    def test_empty_category_has_no_resolvable_provider(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            BUILTIN_PROVIDER_REGISTRY.resolve_provider(ProviderCategory.CATALOG)

    def test_resolve_provider_requires_a_category(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            BUILTIN_PROVIDER_REGISTRY.resolve_provider("ticket")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
