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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.provider_registry import (
    BUILTIN_PROVIDER_REGISTRY,
    FORBIDDEN_PROVIDER_AUTHORITIES,
    BuiltinProvider,
    BuiltinProviderRegistry,
    ProviderCategory,
    ProviderRegistryError,
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
        # The catalog / telemetry / presentation categories have no built-in
        # provider yet, but they are still valid classifications a future
        # provider can slot into — that is what the skeleton enables.
        cats = BuiltinProviderRegistry.categories()
        for empty in (
            ProviderCategory.CATALOG,
            ProviderCategory.TELEMETRY,
            ProviderCategory.PRESENTATION,
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


if __name__ == "__main__":
    unittest.main()
