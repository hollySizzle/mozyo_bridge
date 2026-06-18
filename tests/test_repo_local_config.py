"""Repo-local YAML config schema tests (Redmine #12189).

Pins the typed schema boundary for ``.mozyo-bridge/config.yaml``:

- the closed top-level record (``version`` / ``cli`` / ``providers`` /
  ``presentation``) and the projection-only ``presentation`` surface selection;
- behavior-preserving defaults: ``None`` / empty record / missing block all
  resolve to the current built-in behavior;
- fail-closed rejection of unknown keys, non-mapping records, unsupported
  versions, and module- / callable- / entry-point- / authority- / routing- /
  target- / pane- / credential-shaped fields, raised as a domain error rather
  than a raw parser exception;
- that ``cli`` / ``providers`` delegate to their existing sub-record
  ``from_record`` and inherit their fail-closed behavior.

No file IO, parsing, tmux, or CLI composition is exercised here — schema only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.module_registry import (
    CliCompositionConfig,
    ModuleRegistryError,
)
from mozyo_bridge.domain.presentation_adapter import (
    SURFACE_TEXT,
    SURFACE_TMUX_USER_OPTION,
)
from mozyo_bridge.domain.provider_registry import (
    ProviderRegistryError,
    ProviderSelectionConfig,
)
from mozyo_bridge.domain.repo_local_config import (
    DEFAULT_PRESENTATION_SURFACE,
    REPO_LOCAL_CONFIG_VERSION,
    PresentationSelectionConfig,
    RepoLocalConfig,
    RepoLocalConfigError,
)


class DefaultBehaviorPreservingTest(unittest.TestCase):
    def test_default_factory(self) -> None:
        config = RepoLocalConfig.default()
        self.assertEqual(config.cli, CliCompositionConfig.default())
        self.assertEqual(config.providers, ProviderSelectionConfig.default())
        self.assertEqual(config.presentation, PresentationSelectionConfig.default())

    def test_none_record_is_default(self) -> None:
        self.assertEqual(RepoLocalConfig.from_record(None), RepoLocalConfig.default())

    def test_empty_mapping_is_default(self) -> None:
        # An empty repo-local file (``yaml.safe_load`` -> {}) must preserve
        # behavior exactly, not fail closed.
        self.assertEqual(RepoLocalConfig.from_record({}), RepoLocalConfig.default())

    def test_default_presentation_surface_is_tmux_user_option(self) -> None:
        self.assertEqual(DEFAULT_PRESENTATION_SURFACE, SURFACE_TMUX_USER_OPTION)
        self.assertEqual(
            PresentationSelectionConfig.default().surface, SURFACE_TMUX_USER_OPTION
        )

    def test_record_is_frozen_and_hashable(self) -> None:
        config = RepoLocalConfig.default()
        self.assertEqual(hash(config), hash(RepoLocalConfig.default()))
        with self.assertRaises(Exception):
            config.cli = CliCompositionConfig.default()  # type: ignore[misc]


class ValidRecordTest(unittest.TestCase):
    def test_explicit_supported_version_accepted(self) -> None:
        config = RepoLocalConfig.from_record({"version": REPO_LOCAL_CONFIG_VERSION})
        self.assertEqual(config, RepoLocalConfig.default())

    def test_full_record_maps_each_surface(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "version": 1,
                "cli": {"disabled": ["cockpit"]},
                "providers": {"selections": {"ticket": "redmine"}},
                "presentation": {"surface": SURFACE_TEXT},
            }
        )
        self.assertEqual(config.cli.disabled, frozenset({"cockpit"}))
        self.assertEqual(config.providers.selections, (("ticket", "redmine"),))
        self.assertEqual(config.presentation.surface, SURFACE_TEXT)

    def test_partial_record_only_cli(self) -> None:
        config = RepoLocalConfig.from_record({"cli": {"disabled": ["cockpit"]}})
        self.assertEqual(config.cli.disabled, frozenset({"cockpit"}))
        self.assertEqual(config.providers, ProviderSelectionConfig.default())
        self.assertEqual(config.presentation, PresentationSelectionConfig.default())

    def test_partial_record_only_providers(self) -> None:
        config = RepoLocalConfig.from_record(
            {"providers": {"selections": {"ticket": "redmine"}}}
        )
        self.assertEqual(config.providers.selections, (("ticket", "redmine"),))
        self.assertEqual(config.cli, CliCompositionConfig.default())

    def test_partial_record_only_presentation(self) -> None:
        config = RepoLocalConfig.from_record({"presentation": {"surface": SURFACE_TEXT}})
        self.assertEqual(config.presentation.surface, SURFACE_TEXT)
        self.assertEqual(config.cli, CliCompositionConfig.default())


class TopLevelFailClosedTest(unittest.TestCase):
    def test_non_mapping_record_rejected(self) -> None:
        for bad in ([], "version: 1", 1, ("version", 1)):
            with self.subTest(bad=bad):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record(bad)

    def test_unknown_top_level_key_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"clti": {}})

    def test_non_string_key_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({1: {}})

    def test_unsupported_version_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"version": 2})

    def test_version_bool_rejected(self) -> None:
        # ``bool`` is an ``int`` subclass; ``version: true`` must not read as 1.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"version": True})

    def test_version_non_integer_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"version": "1"})

    def test_boundary_shaped_keys_rejected(self) -> None:
        # Code loading, authority, routing, target/pane, and credential shapes
        # all fail closed before reaching the unknown-key check.
        for key in (
            "module_path",
            "cli_registrar",
            "entry_point",
            "plugin",
            "exec_hook",
            "owner_approval",
            "close_approval",
            "routing_table",
            "route_override",
            "send_default",
            "target_pane",
            "pane_id",
            "role",
            "credential",
            "api_key",
            "auth_token",
        ):
            with self.subTest(key=key):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record({key: "x"})


class DelegatedSubRecordFailClosedTest(unittest.TestCase):
    def test_cli_sub_record_fails_closed(self) -> None:
        # A bare string would iterate char-by-char; the CLI sub-record rejects it.
        with self.assertRaises(ModuleRegistryError):
            RepoLocalConfig.from_record({"cli": {"disabled": "cockpit"}})

    def test_cli_sub_record_must_be_mapping(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            RepoLocalConfig.from_record({"cli": ["cockpit"]})

    def test_providers_sub_record_unknown_key_fails_closed(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            RepoLocalConfig.from_record({"providers": {"selectionz": {}}})

    def test_providers_sub_record_must_be_mapping(self) -> None:
        with self.assertRaises(ProviderRegistryError):
            RepoLocalConfig.from_record({"providers": "redmine"})


class ProviderSelectionBoundaryTokenTest(unittest.TestCase):
    """The schema boundary screens provider-selection categories / ids for
    module / callable / authority / target / credential shapes — the provider
    record itself only rejects the exact core-owned authority names (j#60754).
    """

    def test_credential_shaped_category_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"providers": {"selections": {"api_key": "x"}}})

    def test_credential_shaped_provider_id_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"providers": {"selections": {"ticket": "api_key"}}}
            )

    def test_target_shaped_category_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"providers": {"selections": {"target_pane": "x"}}}
            )

    def test_module_shaped_provider_id_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"providers": {"selections": {"ticket": "module_path"}}}
            )

    def test_authority_shaped_category_rejected_at_boundary(self) -> None:
        # Caught by the boundary screen before the provider record's own exact
        # authority check; both fail closed, the boundary error fires first.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"providers": {"selections": {"owner_approval": "x"}}}
            )

    def test_boundary_shaped_tokens_rejected(self) -> None:
        for category, provider_id in (
            ("ticket", "callable_lookup"),
            ("ticket", "entry_point"),
            ("ticket", "send_default"),
            ("routing", "redmine"),
            ("close", "redmine"),
            ("plugin", "redmine"),
            ("ticket", "credential"),
        ):
            with self.subTest(category=category, provider_id=provider_id):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record(
                        {"providers": {"selections": {category: provider_id}}}
                    )

    def test_legitimate_selection_still_accepted(self) -> None:
        config = RepoLocalConfig.from_record(
            {"providers": {"selections": {"ticket": "redmine"}}}
        )
        self.assertEqual(config.providers.selections, (("ticket", "redmine"),))


class PresentationSelectionTest(unittest.TestCase):
    def test_default_and_none(self) -> None:
        self.assertEqual(
            PresentationSelectionConfig.from_record(None),
            PresentationSelectionConfig.default(),
        )
        self.assertEqual(
            PresentationSelectionConfig.from_record({}).surface,
            SURFACE_TMUX_USER_OPTION,
        )

    def test_text_surface_selectable(self) -> None:
        self.assertEqual(
            PresentationSelectionConfig.from_record({"surface": SURFACE_TEXT}).surface,
            SURFACE_TEXT,
        )

    def test_explicit_version_accepted(self) -> None:
        config = PresentationSelectionConfig.from_record(
            {"version": 1, "surface": SURFACE_TEXT}
        )
        self.assertEqual(config.surface, SURFACE_TEXT)

    def test_unknown_surface_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig.from_record({"surface": "web_viewer"})

    def test_non_string_surface_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig.from_record({"surface": 1})

    def test_direct_construction_validates_surface(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig(surface="nope")

    def test_non_mapping_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig.from_record(["text"])

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig.from_record({"surfase": SURFACE_TEXT})

    def test_unsupported_version_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig.from_record({"version": 9})

    def test_projection_only_boundary_keys_rejected(self) -> None:
        # Presentation must stay projection-only: no target/pane/route/send/
        # approve/close/credential/authority field may be expressed.
        for key in (
            "target",
            "pane",
            "route",
            "send",
            "approve",
            "close",
            "credential",
            "owner_approval",
            "routing",
        ):
            with self.subTest(key=key):
                with self.assertRaises(RepoLocalConfigError):
                    PresentationSelectionConfig.from_record(
                        {key: "x", "surface": SURFACE_TEXT}
                    )

    def test_via_top_level_record(self) -> None:
        config = RepoLocalConfig.from_record(
            {"presentation": {"surface": SURFACE_TEXT}}
        )
        self.assertEqual(config.presentation.surface, SURFACE_TEXT)

    def test_invalid_surface_via_top_level_record(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"presentation": {"surface": "nope"}})


if __name__ == "__main__":
    unittest.main()
