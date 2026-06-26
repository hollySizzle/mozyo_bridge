"""Static plugin manifest schema / validator tests (Redmine #12250).

Pins the declarative-only review manifest boundary designed in
``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine #12001): valid
minimal / rich metadata, and fail-closed rejection of each forbidden class —
dynamic import / entry point / callable / shell / install-run keys, private-path
and secret-shaped values, authority-shaped permissions, invented categories,
unknown keys, and packaging-metadata duplication. No file IO and no manifest code
is exercised here; the validator reads an already-parsed mapping.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_150_plugin_manifest_marketplace.domain.plugin_manifest import (
    PACKAGING_METADATA_FIELDS,
    PLUGIN_MANIFEST_KEYS,
    PLUGIN_MANIFEST_VERSION,
    PluginManifest,
    PluginManifestError,
    validate_plugin_manifest,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry import (
    FORBIDDEN_PROVIDER_AUTHORITIES,
    ProviderCategory,
)


class ValidManifestTest(unittest.TestCase):
    def test_minimal_manifest_is_just_a_plugin_id(self) -> None:
        manifest = validate_plugin_manifest({"plugin_id": "sample-review-plugin"})
        self.assertEqual("sample-review-plugin", manifest.plugin_id)
        self.assertEqual(PLUGIN_MANIFEST_VERSION, manifest.manifest_version)
        self.assertEqual("", manifest.summary)
        self.assertEqual(frozenset(), manifest.categories)
        self.assertEqual(frozenset(), manifest.capabilities)
        self.assertEqual(frozenset(), manifest.declared_permissions)
        self.assertEqual(frozenset(), manifest.safety_constraints)
        self.assertFalse(manifest.experimental)

    def test_rich_manifest_round_trips_declarative_metadata(self) -> None:
        manifest = validate_plugin_manifest(
            {
                "manifest_version": 1,
                "plugin_id": "acme-ticket-adapter",
                "summary": "Candidate ticket adapter, declarative review metadata.",
                "categories": ["ticket", "presentation"],
                "capabilities": ["normalize_issue", "normalize_issue", "project_attention"],
                "declared_permissions": ["read_issue", "render_pane_title"],
                "safety_constraints": ["no_network_in_normalization"],
                "experimental": True,
            }
        )
        self.assertEqual(
            frozenset({ProviderCategory.TICKET, ProviderCategory.PRESENTATION}),
            manifest.categories,
        )
        self.assertEqual(("presentation", "ticket"), manifest.category_names)
        # Duplicate capability normalizes to a frozenset (no double entry).
        self.assertEqual(
            frozenset({"normalize_issue", "project_attention"}), manifest.capabilities
        )
        self.assertEqual(
            frozenset({"read_issue", "render_pane_title"}),
            manifest.declared_permissions,
        )
        self.assertTrue(manifest.experimental)

    def test_manifest_is_frozen_and_hashable(self) -> None:
        manifest = validate_plugin_manifest({"plugin_id": "p"})
        self.assertEqual(manifest, validate_plugin_manifest({"plugin_id": "p"}))
        self.assertIsInstance(hash(manifest), int)
        with self.assertRaises(Exception):
            manifest.plugin_id = "other"  # type: ignore[misc]

    def test_categories_accept_provider_category_members_directly(self) -> None:
        manifest = PluginManifest(
            plugin_id="p", categories=[ProviderCategory.TELEMETRY]
        )
        self.assertEqual(frozenset({ProviderCategory.TELEMETRY}), manifest.categories)


class RecordShapeTest(unittest.TestCase):
    def test_non_mapping_record_is_rejected(self) -> None:
        for bad in (None, [], "plugin", 3):
            with self.assertRaises(PluginManifestError):
                validate_plugin_manifest(bad)

    def test_missing_plugin_id_is_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"summary": "no id"})

    def test_empty_plugin_id_is_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"plugin_id": ""})

    def test_unknown_top_level_key_fails_closed(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"plugin_id": "p", "extra": "nope"})

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"plugin_id": "p", "manifest_version": 2})

    def test_bool_version_does_not_read_as_v1(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"plugin_id": "p", "manifest_version": True})

    def test_non_bool_experimental_is_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"plugin_id": "p", "experimental": "yes"})

    def test_bare_string_capabilities_cannot_explode_into_chars(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"plugin_id": "p", "capabilities": "owner"})


class NoExecutionBoundaryTest(unittest.TestCase):
    """dynamic import / entry point / callable / shell / install-run is rejected."""

    def test_code_loading_keys_are_rejected(self) -> None:
        for key in (
            "import",
            "imports",
            "module",
            "entry_point",
            "entrypoint",
            "callable",
            "exec",
            "eval",
            "script",
            "load_path",
            "sys_path",
            "dynamic_import",
        ):
            with self.subTest(key=key):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest({"plugin_id": "p", key: "x"})

    def test_shell_install_run_keys_are_rejected(self) -> None:
        for key in (
            "shell",
            "command",
            "cmd",
            "subprocess",
            "spawn",
            "install",
            "uninstall",
            "setup",
            "build",
            "run",
            "launch",
            "post_install_hook",
        ):
            with self.subTest(key=key):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest({"plugin_id": "p", key: "x"})

    def test_nested_forbidden_key_is_caught(self) -> None:
        # A forbidden key hidden inside an otherwise-allowed field's value still
        # fails closed via the recursive pre-scan.
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest(
                {"plugin_id": "p", "capabilities": [{"entry_point": "pkg:main"}]}
            )

    def test_executable_capability_labels_are_rejected(self) -> None:
        # Regression for #12250 review j#61753: a capability *label value* (not
        # just a key) that names executable behavior must fail closed — these
        # fields are exactly where declarative behavior labels live.
        for label in (
            "dynamic_import",
            "entry_point_loader",
            "shell_exec",
            "run_install_command",
            "post_install_hook",
            "spawn_subprocess",
            "eval_expression",
        ):
            with self.subTest(label=label):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest(
                        {"plugin_id": "p", "capabilities": [label]}
                    )

    def test_executable_safety_constraint_labels_are_rejected(self) -> None:
        for label in ("runs_install_hook", "loads_module", "dynamic_import_allowed"):
            with self.subTest(label=label):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest(
                        {"plugin_id": "p", "safety_constraints": [label]}
                    )

    def test_executable_permission_labels_are_rejected(self) -> None:
        for label in ("dynamic_import", "load_module", "entry_point_loader"):
            with self.subTest(label=label):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest(
                        {"plugin_id": "p", "declared_permissions": [label]}
                    )

    def test_executable_label_rejected_via_direct_constructor(self) -> None:
        # The fail-closed guard lives in __post_init__, so direct construction is
        # screened identically to from_record (review j#61753 reproduction).
        with self.assertRaises(PluginManifestError):
            PluginManifest(plugin_id="p", capabilities={"dynamic_import"})

    def test_descriptive_labels_remain_allowed(self) -> None:
        # Ordinary non-executable descriptive labels are still accepted.
        manifest = validate_plugin_manifest(
            {
                "plugin_id": "p",
                "capabilities": ["normalize_issue", "project_attention"],
                "safety_constraints": [
                    "no_network_in_normalization",
                    "projection_only",
                ],
            }
        )
        self.assertIn("normalize_issue", manifest.capabilities)
        self.assertIn("projection_only", manifest.safety_constraints)


class BoundaryValueTest(unittest.TestCase):
    """private path / secret-shaped value fail closed wherever they appear."""

    def test_private_path_value_is_rejected(self) -> None:
        for path in ("/opt/app/state", "~/plugin-data", "C:/plugins/data"):
            with self.subTest(path=path):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest({"plugin_id": "p", "summary": path})

    def test_private_path_in_a_capability_label_is_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest(
                {"plugin_id": "p", "capabilities": ["/var/lib/secret-store"]}
            )

    def test_secret_shaped_value_is_rejected(self) -> None:
        # DROP-*-SENTINEL is the sanctioned secret placeholder; the validator
        # rejects it on the embedded credential token, no real secret needed.
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest(
                {"plugin_id": "p", "summary": "uses DROP-TOKEN-SENTINEL value"}
            )

    def test_secret_shaped_safety_constraint_is_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest(
                {"plugin_id": "p", "safety_constraints": ["stores_api_key_locally"]}
            )


class AuthorityPermissionTest(unittest.TestCase):
    """authority-shaped permission fails closed; aligned with provider registry."""

    def test_exact_core_owned_authorities_are_rejected_as_permissions(self) -> None:
        # Every core-owned provider authority is also rejected as a declared
        # permission, so the manifest and the provider registry cannot drift.
        for authority in FORBIDDEN_PROVIDER_AUTHORITIES:
            with self.subTest(authority=authority):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest(
                        {"plugin_id": "p", "declared_permissions": [authority]}
                    )

    def test_authority_shaped_permissions_are_rejected(self) -> None:
        for perm in (
            "owner_approval",
            "close_issue",
            "review_gate_write",
            "routing_authority",
            "send_handoff",
            "approve_release",
            "install_dependency",
            "shell_exec",
            "delete_workspace",
        ):
            with self.subTest(perm=perm):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest(
                        {"plugin_id": "p", "declared_permissions": [perm]}
                    )

    def test_descriptive_read_permission_is_allowed(self) -> None:
        manifest = validate_plugin_manifest(
            {"plugin_id": "p", "declared_permissions": ["read_issue", "list_panes"]}
        )
        self.assertEqual(
            frozenset({"read_issue", "list_panes"}), manifest.declared_permissions
        )


class CategoryVocabularyTest(unittest.TestCase):
    def test_unknown_category_is_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            validate_plugin_manifest({"plugin_id": "p", "categories": ["payments"]})

    def test_categories_are_the_core_owned_provider_vocabulary(self) -> None:
        manifest = validate_plugin_manifest(
            {"plugin_id": "p", "categories": [c.value for c in ProviderCategory]}
        )
        self.assertEqual(
            frozenset(ProviderCategory), manifest.categories
        )


class NoSecondSourceOfTruthTest(unittest.TestCase):
    """No packaging metadata is duplicated; such a key is rejected by name."""

    def test_packaging_fields_are_rejected_with_a_dedicated_message(self) -> None:
        for field in PACKAGING_METADATA_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(PluginManifestError) as ctx:
                    validate_plugin_manifest({"plugin_id": "p", field: "x"})
                self.assertIn("packaging metadata", str(ctx.exception))

    def test_packaging_fields_are_disjoint_from_manifest_keys(self) -> None:
        # The schema cannot both accept and reject the same field name.
        self.assertEqual(
            frozenset(), PLUGIN_MANIFEST_KEYS & PACKAGING_METADATA_FIELDS
        )

    def test_version_and_description_are_not_stored(self) -> None:
        # Spot-check the two most tempting duplicates: packaging version /
        # description live only in the Claude plugin manifests.
        for field in ("version", "description"):
            with self.subTest(field=field):
                with self.assertRaises(PluginManifestError):
                    validate_plugin_manifest({"plugin_id": "p", field: "1.2.3"})


if __name__ == "__main__":
    unittest.main()
