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

from mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry import (
    CliCompositionConfig,
    ModuleRegistryError,
)
from mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter import (
    SURFACE_TEXT,
    SURFACE_TMUX_USER_OPTION,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry import (
    ProviderRegistryError,
    ProviderSelectionConfig,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config import DelegationConfig
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    DEFAULT_MANAGE_WORKTREE,
    DEFAULT_MERGE_ON_RETIRE,
    DEFAULT_PRESENTATION_SURFACE,
    REPO_LOCAL_CONFIG_VERSION,
    PresentationSelectionConfig,
    RepoLocalConfig,
    RepoLocalConfigError,
    SublaneIntegrationConfig,
    WorkUnitGranularityConfig,
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


class PresentationGroupingWiringTest(unittest.TestCase):
    """#12286: the presentation block carries the desired grouping config too."""

    def test_missing_grouping_is_behavior_preserving_default(self) -> None:
        # A presentation block with only a surface keeps an empty grouping config
        # and the default placement mode (no behavior change).
        config = RepoLocalConfig.from_record({"presentation": {"surface": "text"}})
        grouping = config.presentation.grouping
        self.assertEqual(grouping.project_groups, ())
        self.assertEqual(grouping.membership_rules, ())
        self.assertEqual(grouping.unit_overrides, ())
        self.assertEqual(
            grouping.project_group_presentation, "same_cockpit_column"
        )

    def test_grouping_and_placement_parse_under_presentation(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "presentation": {
                    "surface": "text",
                    "project_group_presentation": "project_group_tmux_window",
                    "project_groups": [
                        {"group_id": "project:alpha", "label": "Alpha"}
                    ],
                    "grouping": {
                        "membership_rules": [
                            {
                                "when": {"repo_label": "alpha"},
                                "group_id": "project:alpha",
                            }
                        ]
                    },
                }
            }
        )
        self.assertEqual(config.presentation.surface, "text")
        grouping = config.presentation.grouping
        self.assertEqual(len(grouping.project_groups), 1)
        self.assertEqual(grouping.project_groups[0].group_id, "project:alpha")
        self.assertEqual(len(grouping.membership_rules), 1)
        self.assertEqual(
            grouping.project_group_presentation, "project_group_tmux_window"
        )

    def test_surface_default_holds_with_grouping_present(self) -> None:
        config = RepoLocalConfig.from_record(
            {"presentation": {"project_group_presentation": "normal_window"}}
        )
        self.assertEqual(
            config.presentation.surface, DEFAULT_PRESENTATION_SURFACE
        )
        self.assertEqual(
            config.presentation.grouping.project_group_presentation,
            "normal_window",
        )

    def test_invalid_placement_fails_closed_as_repo_local_error(self) -> None:
        # The grouping schema's PresentationGroupingConfigError is re-raised as a
        # RepoLocalConfigError so the loader's single-except boundary catches it.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"presentation": {"project_group_presentation": "iterm_tab"}}
            )

    def test_delegation_window_policy_defaults_to_shared(self) -> None:
        # #13085: the default is the single sublane host window (`shared`).
        config = RepoLocalConfig.from_record({"presentation": {"surface": "text"}})
        self.assertEqual(
            config.presentation.grouping.delegation_window_policy, "shared"
        )

    def test_delegation_window_policy_is_settable_under_presentation(self) -> None:
        # #13015: the #12467 window-separation knob is forwarded through the
        # presentation block so a project can actually opt into `separate`.
        for mode in ("separate", "shared"):
            config = RepoLocalConfig.from_record(
                {"presentation": {"delegation_window_policy": mode}}
            )
            self.assertEqual(
                config.presentation.grouping.delegation_window_policy, mode
            )

    def test_invalid_delegation_window_policy_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"presentation": {"delegation_window_policy": "split_screen"}}
            )

    def test_dangling_group_reference_fails_closed_as_repo_local_error(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {
                    "presentation": {
                        "grouping": {
                            "membership_rules": [
                                {
                                    "when": {"repo_label": "x"},
                                    "group_id": "project:undeclared",
                                }
                            ]
                        }
                    }
                }
            )

    def test_authority_shaped_grouping_key_fails_closed(self) -> None:
        # A boundary-shaped key inside grouping is rejected (no routing / approval
        # leaks into the presentation grouping config).
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {
                    "presentation": {
                        "project_groups": [
                            {
                                "group_id": "g",
                                "label": "G",
                                "route_target": "%5",
                            }
                        ]
                    }
                }
            )


class DelegationWiringTest(unittest.TestCase):
    """``delegation`` top-level surface (#12549): default-preserving + delegated.

    The exhaustive child-candidate schema / resolver behavior lives in
    ``tests/test_delegation_project_config.py``; here we only pin that the
    top-level repo-local record composes the new surface, stays
    behavior-preserving when absent, and folds the delegation schema's own
    fail-closed error into ``RepoLocalConfigError`` (the single loader boundary).
    """

    def test_missing_delegation_is_behavior_preserving_default(self) -> None:
        self.assertEqual(
            RepoLocalConfig.from_record({"cli": {"disabled": ["cockpit"]}}).delegation,
            DelegationConfig.default(),
        )

    def test_empty_delegation_block_is_default(self) -> None:
        self.assertEqual(
            RepoLocalConfig.from_record({"delegation": {}}).delegation,
            DelegationConfig.default(),
        )

    def test_child_candidate_parses_under_top_level_record(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "delegation": {
                    "child_candidates": [
                        {
                            "child_project": "mozyo_bridge",
                            "capabilities": ["implementation"],
                        }
                    ]
                }
            }
        )
        self.assertEqual(len(config.delegation.child_candidates), 1)
        self.assertEqual(
            config.delegation.child_candidates[0].child_project, "mozyo_bridge"
        )

    def test_invalid_delegation_fails_closed_as_repo_local_error(self) -> None:
        # A delegation schema violation surfaces as the loader's own error, not a
        # bare DelegationConfigError, so the single fail-closed boundary holds.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"delegation": {"child_candidates": [{"child_project": "/abs/path"}]}}
            )

    def test_authority_shaped_delegation_key_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {
                    "delegation": {
                        "child_candidates": [
                            {"child_project": "mozyo_bridge", "target_pane": "%9"}
                        ]
                    }
                }
            )


class SublaneIntegrationConfigTest(unittest.TestCase):
    """The sublane Git worktree / retire-merge policy knob (Redmine #12604)."""

    def test_default_is_behavior_preserving(self) -> None:
        default = SublaneIntegrationConfig.default()
        self.assertEqual(default.manage_worktree, DEFAULT_MANAGE_WORKTREE)
        self.assertEqual(default.merge_on_retire, DEFAULT_MERGE_ON_RETIRE)
        self.assertIsNone(default.integration_branch)
        self.assertEqual(RepoLocalConfig.default().sublane_integration, default)

    def test_none_and_empty_resolve_to_default(self) -> None:
        self.assertEqual(
            SublaneIntegrationConfig.from_record(None),
            SublaneIntegrationConfig.default(),
        )
        self.assertEqual(
            SublaneIntegrationConfig.from_record({}),
            SublaneIntegrationConfig.default(),
        )

    def test_missing_block_keeps_default(self) -> None:
        config = RepoLocalConfig.from_record({"cli": {"disabled": ["cockpit"]}})
        self.assertEqual(
            config.sublane_integration, SublaneIntegrationConfig.default()
        )

    def test_full_record_maps_each_field(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "sublane_integration": {
                    "manage_worktree": False,
                    "integration_branch": "main",
                    "merge_on_retire": False,
                }
            }
        )
        si = config.sublane_integration
        self.assertFalse(si.manage_worktree)
        self.assertEqual(si.integration_branch, "main")
        self.assertFalse(si.merge_on_retire)

    def test_explicit_supported_version_accepted(self) -> None:
        config = SublaneIntegrationConfig.from_record(
            {"version": REPO_LOCAL_CONFIG_VERSION, "merge_on_retire": False}
        )
        self.assertFalse(config.merge_on_retire)

    def test_non_bool_manage_worktree_fails_closed(self) -> None:
        # ``1`` must not silently read as True — the fail-closed boundary.
        for bad in (1, 0, "true", None):
            with self.subTest(bad=bad):
                with self.assertRaises(RepoLocalConfigError):
                    SublaneIntegrationConfig.from_record({"manage_worktree": bad})

    def test_non_bool_merge_on_retire_fails_closed(self) -> None:
        for bad in (1, 0, "false"):
            with self.subTest(bad=bad):
                with self.assertRaises(RepoLocalConfigError):
                    SublaneIntegrationConfig.from_record({"merge_on_retire": bad})

    def test_empty_or_non_string_integration_branch_fails_closed(self) -> None:
        for bad in ("", "   ", 5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(RepoLocalConfigError):
                    SublaneIntegrationConfig.from_record({"integration_branch": bad})

    def test_unknown_key_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            SublaneIntegrationConfig.from_record({"merge_on_retyre": True})

    def test_invariant_authority_shaped_key_fails_closed(self) -> None:
        # The owner-approval / close / callback invariants cannot be smuggled in as
        # config keys — a boundary-shaped key is rejected by the closed schema.
        for boundary_key in (
            "owner_approval",
            "close_on_retire",
            "callback_target",
            "send_on_retire",
        ):
            with self.subTest(boundary_key=boundary_key):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record(
                        {"sublane_integration": {boundary_key: True}}
                    )

    def test_non_mapping_record_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"sublane_integration": ["main"]})

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            SublaneIntegrationConfig.from_record({"version": 2})


class WorkUnitGranularityWiringTest(unittest.TestCase):
    """The governed work-unit granularity knob (Redmine #13002)."""

    def test_default_is_user_story(self) -> None:
        default = WorkUnitGranularityConfig.default()
        self.assertEqual(default.granularity, "user_story")
        self.assertEqual(RepoLocalConfig.default().work_unit, default)

    def test_missing_block_keeps_user_story_default(self) -> None:
        config = RepoLocalConfig.from_record({"cli": {"disabled": ["cockpit"]}})
        self.assertEqual(config.work_unit, WorkUnitGranularityConfig.default())

    def test_valid_record_selects_granularity(self) -> None:
        config = RepoLocalConfig.from_record(
            {"work_unit": {"version": 1, "granularity": "leaf_issue"}}
        )
        self.assertEqual(config.work_unit.granularity, "leaf_issue")

    def test_invalid_granularity_fails_closed_as_repo_local_error(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"work_unit": {"granularity": "sprint"}})

    def test_unknown_key_fails_closed_as_repo_local_error(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"work_unit": {"unit": "user_story"}})

    def test_non_mapping_record_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"work_unit": "user_story"})


if __name__ == "__main__":
    unittest.main()
