"""Unit tests for the #13157 repo-local role -> provider binding override config.

Pins the closed-schema ``provider_binding`` sub-record that live-wires the #12673
:class:`RoleProviderBinding` seam:

- an unset / empty block is behavior-preserving (the legacy codex/claude default);
- overrides are reflected in the resolved binding, with a closed role vocabulary
  (unknown role fails closed) and an open provider vocabulary (``grok`` binds);
- the closed schema rejects an unknown top-level key, a non-mapping record / bindings,
  an unsupported version, a non-string role key, and an empty provider (fail-closed);
- the auditor == implementer advisory warning is emitted (never a hard block), and every
  differing binding (including the default) yields no warning.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    RoleProviderBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding_config import (
    PROVIDER_BINDING_CONFIG_VERSION,
    RoleProviderBindingConfig,
    RoleProviderBindingConfigError,
)


class DefaultBindingTest(unittest.TestCase):
    def test_default_reproduces_legacy_codex_claude_map(self) -> None:
        config = RoleProviderBindingConfig.default()
        self.assertEqual(config.binding, RoleProviderBinding.default())
        self.assertEqual(config.binding.provider_for("auditor"), PROVIDER_CODEX)
        self.assertEqual(config.binding.provider_for("implementer"), PROVIDER_CLAUDE)
        self.assertEqual(config.binding.provider_for("coordinator"), PROVIDER_CODEX)

    def test_none_and_empty_resolve_to_default(self) -> None:
        self.assertEqual(
            RoleProviderBindingConfig.from_record(None),
            RoleProviderBindingConfig.default(),
        )
        self.assertEqual(
            RoleProviderBindingConfig.from_record({}),
            RoleProviderBindingConfig.default(),
        )

    def test_absent_bindings_key_resolves_to_default(self) -> None:
        config = RoleProviderBindingConfig.from_record(
            {"version": PROVIDER_BINDING_CONFIG_VERSION}
        )
        self.assertEqual(config, RoleProviderBindingConfig.default())

    def test_default_emits_no_warning(self) -> None:
        self.assertEqual(RoleProviderBindingConfig.default().advisory_warnings(), ())


class OverrideBindingTest(unittest.TestCase):
    def test_override_is_reflected(self) -> None:
        config = RoleProviderBindingConfig.from_record(
            {"bindings": {"auditor": "claude"}}
        )
        self.assertEqual(config.binding.provider_for("auditor"), "claude")
        # Untouched roles keep the default provider.
        self.assertEqual(config.binding.provider_for("coordinator"), PROVIDER_CODEX)

    def test_open_provider_vocabulary(self) -> None:
        # A never-before-seen provider binds with no code change (the point of #12673).
        config = RoleProviderBindingConfig.from_record(
            {"bindings": {"implementer": "grok"}}
        )
        self.assertEqual(config.binding.provider_for("implementer"), "grok")

    def test_explicit_supported_version_accepted(self) -> None:
        config = RoleProviderBindingConfig.from_record(
            {"version": PROVIDER_BINDING_CONFIG_VERSION, "bindings": {"owner": "grok"}}
        )
        self.assertEqual(config.binding.provider_for("owner"), "grok")

    def test_config_is_hashable(self) -> None:
        # RepoLocalConfig is a frozen dataclass composed of these; the record must hash.
        config = RoleProviderBindingConfig.from_record({"bindings": {"auditor": "claude"}})
        self.assertIsInstance(hash(config), int)


class FailClosedSchemaTest(unittest.TestCase):
    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig.from_record({"bindings": {"reviewer": "claude"}})

    def test_empty_provider_fails_closed(self) -> None:
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(RoleProviderBindingConfigError):
                    RoleProviderBindingConfig.from_record(
                        {"bindings": {"auditor": bad}}
                    )

    def test_unknown_top_level_key_fails_closed(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig.from_record({"binding": {}})

    def test_non_mapping_record_fails_closed(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig.from_record(["auditor=claude"])

    def test_non_mapping_bindings_fails_closed(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig.from_record({"bindings": ["auditor=claude"]})

    def test_non_string_role_key_fails_closed(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig.from_record({"bindings": {5: "claude"}})

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig.from_record({"version": 2})

    def test_bool_version_fails_closed(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig.from_record({"version": True})

    def test_direct_construction_validates_overrides(self) -> None:
        with self.assertRaises(RoleProviderBindingConfigError):
            RoleProviderBindingConfig(overrides=(("reviewer", "claude"),))


class AdvisoryWarningTest(unittest.TestCase):
    def test_auditor_equals_implementer_warns_but_does_not_block(self) -> None:
        # Both explicitly bound to the same provider -> advisory warning, no exception.
        config = RoleProviderBindingConfig.from_record(
            {"bindings": {"auditor": "claude", "implementer": "claude"}}
        )
        warnings = config.advisory_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("auditor", warnings[0])
        self.assertIn("implementer", warnings[0])
        self.assertIn("claude", warnings[0])

    def test_merged_collision_with_default_warns(self) -> None:
        # Only auditor is set, but the default implementer is already claude, so the
        # RESOLVED (merged) binding collides -> the warning fires on the merged result.
        config = RoleProviderBindingConfig.from_record(
            {"bindings": {"auditor": "claude"}}
        )
        self.assertEqual(len(config.advisory_warnings()), 1)

    def test_differing_providers_no_warning(self) -> None:
        config = RoleProviderBindingConfig.from_record(
            {"bindings": {"auditor": "codex", "implementer": "grok"}}
        )
        self.assertEqual(config.advisory_warnings(), ())


if __name__ == "__main__":
    unittest.main()
