"""Unit tests for the #12673 workflow-role -> runtime-provider binding.

Covers the schema / default / override boundary and the ``role via provider`` display:

- the default binding reproduces the legacy codex/claude map (compatibility);
- the role vocabulary is closed (an unknown role override fails closed) while the
  provider vocabulary is open (a never-before-seen provider like ``grok`` binds with no
  code change — the binding is not provider-fixed);
- overrides merge on top of the default / an existing binding, last-write-wins, and never
  mutate the base;
- ``parse_binding_overrides`` is a fail-closed ROLE=PROVIDER parser;
- ``format_role_via_provider`` shows both the role and the provider, with explicit
  placeholders rather than a silently dropped side.
"""

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    KNOWN_PROVIDERS,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    ROLE_IMPLEMENTATION_WORKER,
    ROLE_PROJECT_GATEWAY,
    ROLE_ROOT_COORDINATOR,
    RoleProviderBinding,
    RoleProviderBindingError,
    WORKFLOW_ROLES,
    format_role_via_provider,
    parse_binding_overrides,
)


class DefaultBindingTest(unittest.TestCase):
    def test_default_reproduces_legacy_codex_claude_map(self):
        binding = RoleProviderBinding.default()
        # gateway / coordination / audit / owner run on codex; implementation on claude.
        self.assertEqual(binding.provider_for("coordinator"), PROVIDER_CODEX)
        self.assertEqual(binding.provider_for("auditor"), PROVIDER_CODEX)
        self.assertEqual(binding.provider_for("owner"), PROVIDER_CODEX)
        self.assertEqual(binding.provider_for("implementer"), PROVIDER_CLAUDE)

    def test_default_covers_extended_12670_lane_roles(self):
        binding = RoleProviderBinding.default()
        self.assertEqual(binding.provider_for(ROLE_ROOT_COORDINATOR), PROVIDER_CODEX)
        self.assertEqual(binding.provider_for(ROLE_PROJECT_GATEWAY), PROVIDER_CODEX)
        self.assertEqual(
            binding.provider_for(ROLE_IMPLEMENTATION_WORKER), PROVIDER_CLAUDE
        )

    def test_unbound_or_unknown_role_fails_closed_to_none(self):
        binding = RoleProviderBinding.default()
        # "none"/unknown owner has no provider -> None, never a guessed default.
        self.assertIsNone(binding.provider_for("none"))
        self.assertIsNone(binding.provider_for("grafana"))
        self.assertIsNone(binding.provider_for(""))

    def test_normalizes_whitespace_on_lookup(self):
        self.assertEqual(
            RoleProviderBinding.default().provider_for("  auditor  "), PROVIDER_CODEX
        )


class OverrideBoundaryTest(unittest.TestCase):
    def test_override_rebinds_a_role_to_a_new_provider(self):
        binding = RoleProviderBinding.default().with_overrides({"auditor": "grok"})
        self.assertEqual(binding.provider_for("auditor"), "grok")
        # other roles untouched.
        self.assertEqual(binding.provider_for("implementer"), PROVIDER_CLAUDE)

    def test_open_provider_vocabulary_accepts_unknown_surface(self):
        # The acceptance criterion: the binding is NOT fixed to codex/claude. A provider
        # outside KNOWN_PROVIDERS binds with no code change.
        self.assertNotIn("grok", KNOWN_PROVIDERS)
        binding = RoleProviderBinding.default().with_overrides(
            {"implementer": "grok"}
        )
        self.assertEqual(binding.provider_for("implementer"), "grok")

    def test_unknown_role_override_fails_closed(self):
        with self.assertRaises(RoleProviderBindingError):
            RoleProviderBinding.default().with_overrides({"reviewer": "codex"})

    def test_empty_provider_override_fails_closed(self):
        with self.assertRaises(RoleProviderBindingError):
            RoleProviderBinding.default().with_overrides({"auditor": "  "})

    def test_override_does_not_mutate_base_binding(self):
        base = RoleProviderBinding.default()
        base.with_overrides({"auditor": "grok"})
        self.assertEqual(base.provider_for("auditor"), PROVIDER_CODEX)

    def test_overrides_chain_last_write_wins(self):
        binding = (
            RoleProviderBinding.default()
            .with_overrides({"auditor": "grok"})
            .with_overrides({"auditor": "gemini"})
        )
        self.assertEqual(binding.provider_for("auditor"), "gemini")

    def test_as_mapping_is_a_detached_copy(self):
        binding = RoleProviderBinding.default()
        mapping = binding.as_mapping()
        mapping["auditor"] = "mutated"
        self.assertEqual(binding.provider_for("auditor"), PROVIDER_CODEX)


class ParseOverridesTest(unittest.TestCase):
    def test_parses_role_equals_provider_specs(self):
        self.assertEqual(
            parse_binding_overrides(["auditor=grok", "implementer=claude"]),
            {"auditor": "grok", "implementer": "claude"},
        )

    def test_blank_specs_are_skipped(self):
        self.assertEqual(parse_binding_overrides(["", "  ", "auditor=codex"]),
                         {"auditor": "codex"})

    def test_missing_equals_fails_closed(self):
        with self.assertRaises(RoleProviderBindingError):
            parse_binding_overrides(["auditor codex"])

    def test_unknown_role_fails_closed(self):
        with self.assertRaises(RoleProviderBindingError):
            parse_binding_overrides(["reviewer=codex"])

    def test_empty_provider_fails_closed(self):
        with self.assertRaises(RoleProviderBindingError):
            parse_binding_overrides(["auditor="])

    def test_parsed_overrides_feed_with_overrides(self):
        overrides = parse_binding_overrides(["project_gateway=grok"])
        binding = RoleProviderBinding.default().with_overrides(overrides)
        self.assertEqual(binding.provider_for(ROLE_PROJECT_GATEWAY), "grok")


class DisplayTest(unittest.TestCase):
    def test_format_shows_role_and_provider_together(self):
        self.assertEqual(format_role_via_provider("auditor", "codex"), "auditor via codex")

    def test_unresolved_provider_uses_explicit_placeholder(self):
        self.assertEqual(
            format_role_via_provider("auditor", ""), "auditor via <unresolved>"
        )

    def test_unknown_role_uses_explicit_placeholder(self):
        self.assertEqual(
            format_role_via_provider("", "codex"), "<unknown_role> via codex"
        )

    def test_binding_describe_uses_bound_provider(self):
        binding = RoleProviderBinding.default().with_overrides({"auditor": "grok"})
        self.assertEqual(binding.describe("auditor"), "auditor via grok")


class VocabularyTest(unittest.TestCase):
    def test_closed_role_vocabulary_excludes_none(self):
        self.assertNotIn("none", WORKFLOW_ROLES)
        self.assertIn("auditor", WORKFLOW_ROLES)
        self.assertIn(ROLE_IMPLEMENTATION_WORKER, WORKFLOW_ROLES)

    def test_every_default_role_is_in_the_vocabulary(self):
        for role in RoleProviderBinding.default().roles():
            self.assertIn(role, WORKFLOW_ROLES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
