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
    AgentLaunchConfig,
    DEFAULT_MANAGE_WORKTREE,
    DEFAULT_MERGE_ON_RETIRE,
    DEFAULT_PRESENTATION_SURFACE,
    REPO_LOCAL_CONFIG_VERSION,
    LanePlacementConfig,
    LanePlacementError,
    PresentationSelectionConfig,
    RepoLocalConfig,
    RepoLocalConfigError,
    RoleProviderBindingConfig,
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
                "agent_launch": {"sublane_claude_model": "claude-opus-4-8"},
            }
        )
        self.assertEqual(config.cli.disabled, frozenset({"cockpit"}))
        self.assertEqual(config.providers.selections, (("ticket", "redmine"),))
        self.assertEqual(config.presentation.surface, SURFACE_TEXT)
        self.assertEqual(
            config.agent_launch.sublane_claude_model, "claude-opus-4-8"
        )

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

    def test_terminal_transport_default_is_tmux_off(self) -> None:
        # An absent terminal_transport block resolves to tmux (herdr off),
        # behavior-preserving.
        config = RepoLocalConfig.from_record({})
        self.assertEqual(config.terminal_transport.backend, "tmux")
        self.assertFalse(config.terminal_transport.herdr_enabled)

    def test_terminal_transport_herdr_selected(self) -> None:
        config = RepoLocalConfig.from_record({"terminal_transport": {"backend": "herdr"}})
        self.assertEqual(config.terminal_transport.backend, "herdr")
        self.assertTrue(config.terminal_transport.herdr_enabled)

    def test_terminal_transport_invalid_backend_fails_closed(self) -> None:
        # A TerminalTransportError is re-raised as RepoLocalConfigError so the
        # loader keeps a single fail-closed boundary.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"terminal_transport": {"backend": "ssh"}})

    def test_terminal_transport_unknown_key_fails_closed(self) -> None:
        # The herdr binary is not a config field (trusted-env only); a
        # herdr_binary key fails closed.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"terminal_transport": {"herdr_binary": "/opt/herdr"}}
            )


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


class AgentLaunchConfigTest(unittest.TestCase):
    """The per-role / lane managed-pane launch model knob (Redmine #13155)."""

    def test_default_is_behavior_preserving(self) -> None:
        default = AgentLaunchConfig.default()
        self.assertIsNone(default.sublane_claude_model)
        self.assertEqual(RepoLocalConfig.default().agent_launch, default)

    def test_none_and_empty_resolve_to_default(self) -> None:
        self.assertEqual(
            AgentLaunchConfig.from_record(None), AgentLaunchConfig.default()
        )
        self.assertEqual(
            AgentLaunchConfig.from_record({}), AgentLaunchConfig.default()
        )

    def test_missing_block_keeps_default(self) -> None:
        config = RepoLocalConfig.from_record({"cli": {"disabled": ["cockpit"]}})
        self.assertEqual(config.agent_launch, AgentLaunchConfig.default())

    def test_full_record_maps_model(self) -> None:
        config = RepoLocalConfig.from_record(
            {"agent_launch": {"sublane_claude_model": "claude-opus-4-8"}}
        )
        self.assertEqual(
            config.agent_launch.sublane_claude_model, "claude-opus-4-8"
        )

    def test_explicit_supported_version_accepted(self) -> None:
        config = AgentLaunchConfig.from_record(
            {"version": REPO_LOCAL_CONFIG_VERSION, "sublane_claude_model": "sonnet"}
        )
        self.assertEqual(config.sublane_claude_model, "sonnet")

    def test_invalid_model_value_fails_closed(self) -> None:
        # Not an opaque shell string: empty, whitespace, shell metachars, spaces,
        # flag-shaped, and non-string values all fail closed.
        for bad in ("", "   ", "opus 4", "opus;rm", "-model", "a b", 5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(RepoLocalConfigError):
                    AgentLaunchConfig.from_record({"sublane_claude_model": bad})

    def test_invalid_model_via_top_level_record(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"agent_launch": {"sublane_claude_model": "bad; rm -rf"}}
            )

    def test_direct_construction_validates_model(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig(sublane_claude_model="has space")

    def test_unknown_key_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig.from_record({"sublane_claud_model": "sonnet"})

    def test_non_mapping_record_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"agent_launch": ["sonnet"]})

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig.from_record({"version": 2})


class LaunchArgvConfigTest(unittest.TestCase):
    """The provider-agnostic ``agent_launch.launch_argv`` override (Redmine #13425)."""

    def test_default_resolves_to_empty_everywhere(self) -> None:
        cfg = AgentLaunchConfig.default()
        for provider in ("claude", "codex"):
            for lane_class in ("default", "sublane"):
                self.assertEqual(cfg.resolve_launch_argv(provider, lane_class), [])

    def test_new_schema_parse_and_resolve(self) -> None:
        cfg = AgentLaunchConfig.from_record(
            {
                "launch_argv": {
                    "codex": {
                        "default": ["--config", "model_reasoning_effort=xhigh"],
                        "sublane": ["--config", "model_reasoning_effort=high"],
                    },
                    "claude": {"sublane": ["--model", "claude-opus-4-8"]},
                }
            }
        )
        self.assertEqual(
            cfg.resolve_launch_argv("codex", "default"),
            ["--config", "model_reasoning_effort=xhigh"],
        )
        self.assertEqual(
            cfg.resolve_launch_argv("codex", "sublane"),
            ["--config", "model_reasoning_effort=high"],
        )
        self.assertEqual(
            cfg.resolve_launch_argv("claude", "sublane"),
            ["--model", "claude-opus-4-8"],
        )
        # An unset slot is empty (claude default was not configured).
        self.assertEqual(cfg.resolve_launch_argv("claude", "default"), [])

    def test_resolve_returns_fresh_list(self) -> None:
        cfg = AgentLaunchConfig.from_record(
            {"launch_argv": {"codex": {"default": ["--x"]}}}
        )
        first = cfg.resolve_launch_argv("codex", "default")
        first.append("--mutated")
        self.assertEqual(cfg.resolve_launch_argv("codex", "default"), ["--x"])

    def test_old_key_folds_to_claude_sublane(self) -> None:
        cfg = AgentLaunchConfig.from_record(
            {"sublane_claude_model": "claude-opus-4-8"}
        )
        self.assertEqual(
            cfg.resolve_launch_argv("claude", "sublane"),
            ["--model", "claude-opus-4-8"],
        )
        # The fold is sublane-only; default Claude and any Codex slot stay empty.
        self.assertEqual(cfg.resolve_launch_argv("claude", "default"), [])
        self.assertEqual(cfg.resolve_launch_argv("codex", "sublane"), [])

    def test_old_and_other_new_slots_coexist(self) -> None:
        cfg = AgentLaunchConfig.from_record(
            {
                "sublane_claude_model": "claude-opus-4-8",
                "launch_argv": {"codex": {"default": ["--config", "e=xhigh"]}},
            }
        )
        self.assertEqual(
            cfg.resolve_launch_argv("claude", "sublane"),
            ["--model", "claude-opus-4-8"],
        )
        self.assertEqual(
            cfg.resolve_launch_argv("codex", "default"), ["--config", "e=xhigh"]
        )

    def test_old_and_new_same_slot_conflict_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig.from_record(
                {
                    "sublane_claude_model": "claude-opus-4-8",
                    "launch_argv": {"claude": {"sublane": ["--model", "sonnet"]}},
                }
            )

    def test_reserved_managed_flag_fails_closed(self) -> None:
        for bad in (["--permission-mode", "plan"], ["--permission-mode=plan"]):
            with self.subTest(bad=bad):
                with self.assertRaises(RepoLocalConfigError):
                    AgentLaunchConfig.from_record(
                        {"launch_argv": {"claude": {"default": bad}}}
                    )

    def test_unknown_provider_or_lane_class_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig.from_record(
                {"launch_argv": {"grok": {"default": ["--x"]}}}
            )
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig.from_record(
                {"launch_argv": {"claude": {"main": ["--x"]}}}
            )

    def test_bad_tokens_fail_closed(self) -> None:
        bad_argvs = (
            [""],  # empty token
            ["--"],  # option terminator can hide later mozyo-managed flags
            ["a\nb"],  # newline
            ["a\tb"],  # tab / control char
            [5],  # non-string
            "--model",  # bare string, not a list
        )
        for bad in bad_argvs:
            with self.subTest(bad=bad):
                with self.assertRaises(RepoLocalConfigError):
                    AgentLaunchConfig.from_record(
                        {"launch_argv": {"claude": {"default": bad}}}
                    )

    def test_non_mapping_launch_argv_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig.from_record({"launch_argv": ["--x"]})

    def test_path_like_flag_value_allowed(self) -> None:
        # A path in a flag *value* is legitimate (#13425 Q3); only the executable /
        # argv[0] stays mozyo-controlled, and this is a value, not argv[0].
        cfg = AgentLaunchConfig.from_record(
            {"launch_argv": {"claude": {"default": ["--add-dir", "/some/path"]}}}
        )
        self.assertEqual(
            cfg.resolve_launch_argv("claude", "default"), ["--add-dir", "/some/path"]
        )

    def test_direct_construction_validates_launch_argv(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AgentLaunchConfig(launch_argv=(("claude", "default", ("a\nb",)),))

    def test_config_stays_hashable(self) -> None:
        cfg = AgentLaunchConfig.from_record(
            {"launch_argv": {"codex": {"default": ["--config", "e=xhigh"]}}}
        )
        self.assertIsInstance(hash(cfg), int)


class ProviderBindingConfigTest(unittest.TestCase):
    """The role -> provider binding override sub-record (Redmine #13157)."""

    def test_default_is_behavior_preserving(self) -> None:
        default = RoleProviderBindingConfig.default()
        self.assertEqual(RepoLocalConfig.default().provider_binding, default)
        self.assertEqual(default.advisory_warnings(), ())

    def test_missing_block_keeps_default(self) -> None:
        config = RepoLocalConfig.from_record({"cli": {"disabled": ["cockpit"]}})
        self.assertEqual(
            config.provider_binding, RoleProviderBindingConfig.default()
        )

    def test_override_maps_through_top_level(self) -> None:
        config = RepoLocalConfig.from_record(
            {"provider_binding": {"bindings": {"auditor": "claude"}}}
        )
        self.assertEqual(
            config.provider_binding.binding.provider_for("auditor"), "claude"
        )

    def test_unknown_role_via_top_level_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"provider_binding": {"bindings": {"reviewer": "claude"}}}
            )

    def test_unknown_sub_key_via_top_level_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"provider_binding": {"binding": {}}})

    def test_non_mapping_block_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"provider_binding": ["auditor=claude"]})

    def test_provider_binding_is_a_recognized_top_level_key(self) -> None:
        # It is NOT rejected as an unknown top-level key or a boundary-shaped key.
        config = RepoLocalConfig.from_record({"provider_binding": {}})
        self.assertEqual(
            config.provider_binding, RoleProviderBindingConfig.default()
        )


class LanePlacementConfigTest(unittest.TestCase):
    """The closed ``lane_placement`` schema (Redmine #13646, Design Answer j#76564).

    Declares the herdr pane-pair split direction + provider order per lane class. Absent
    is behavior-preserving; every unknown / invalid shape fails closed. The block is a
    future-launch policy, never a live-layout / route authority — which is also why the
    key is ``lane_placement`` and NOT ``pane_placement`` (the boundary screen rejects any
    key containing ``pane``).
    """

    def test_absent_block_is_behavior_preserving(self) -> None:
        config = RepoLocalConfig.from_record({})
        self.assertEqual(config.lane_placement, LanePlacementConfig.default())
        self.assertEqual(config.lane_placement.placements, ())
        # Every lane class resolves to the legacy (inherit-everything) placement.
        for lane_class in ("default", "sublane"):
            resolved = config.lane_placement.resolve(lane_class)
            self.assertIsNone(resolved.split)
            self.assertIsNone(resolved.order)

    def test_full_record_resolves_both_lane_classes(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "lane_placement": {
                    "default": {"split": "down", "order": ["codex", "claude"]},
                    "sublane": {"split": "right", "order": ["claude", "codex"]},
                }
            }
        )
        default = config.lane_placement.resolve("default")
        self.assertEqual(default.split, "down")
        self.assertEqual(default.order, ("codex", "claude"))
        sublane = config.lane_placement.resolve("sublane")
        self.assertEqual(sublane.split, "right")
        self.assertEqual(sublane.order, ("claude", "codex"))

    def test_fields_are_individually_optional(self) -> None:
        # A partial object configures only its own field; the other inherits legacy.
        config = RepoLocalConfig.from_record(
            {"lane_placement": {"default": {"split": "down"}, "sublane": {}}}
        )
        default = config.lane_placement.resolve("default")
        self.assertEqual(default.split, "down")
        self.assertIsNone(default.order)
        sublane = config.lane_placement.resolve("sublane")
        self.assertIsNone(sublane.split)
        self.assertIsNone(sublane.order)

    def test_absent_lane_class_inherits_legacy(self) -> None:
        config = RepoLocalConfig.from_record(
            {"lane_placement": {"sublane": {"split": "down"}}}
        )
        self.assertIsNone(config.lane_placement.resolve("default").split)
        self.assertEqual(config.lane_placement.resolve("sublane").split, "down")

    def test_pane_placement_key_rejected_by_the_boundary_screen(self) -> None:
        # The block is deliberately NOT named `pane_placement`: `_FORBIDDEN_KEY_PARTS`
        # contains `pane`, so such a key is rejected as a boundary token BEFORE the
        # unknown-key check (worker characterization j#76559 / Design Answer j#76564 Q1).
        with self.assertRaises(RepoLocalConfigError) as ctx:
            RepoLocalConfig.from_record(
                {"pane_placement": {"default": {"split": "down"}}}
            )
        self.assertIn("boundary token", str(ctx.exception))

    def test_unknown_lane_class_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"lane_placement": {"main": {"split": "down"}}})

    def test_unknown_class_key_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"lane_placement": {"default": {"direction": "down"}}}
            )

    def test_invalid_split_direction_rejected(self) -> None:
        for bad in ("up", "left", "RIGHT", "", "vertical", 1, True):
            with self.subTest(split=bad):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record(
                        {"lane_placement": {"default": {"split": bad}}}
                    )

    def test_order_must_be_an_exact_permutation(self) -> None:
        # Missing provider, duplicate, unknown provider, a bare string, and a non-list
        # all fail closed — a partial order can never silently drop a provider.
        for bad in (
            ["codex"],
            ["codex", "codex"],
            ["codex", "claude", "gemini"],
            "codex,claude",
            {"codex": 1},
            [],
        ):
            with self.subTest(order=bad):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record(
                        {"lane_placement": {"sublane": {"order": bad}}}
                    )

    def test_non_mapping_block_and_class_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"lane_placement": ["default"]})
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"lane_placement": {"default": "down"}})

    def test_unsupported_version_rejected(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"lane_placement": {"version": 99, "default": {"split": "down"}}}
            )

    def test_direct_construction_is_validated(self) -> None:
        # A directly-constructed config is checked as thoroughly as a parsed one, so a
        # bad value can never enter through the dataclass back door. The sibling raises its
        # own LanePlacementError; RepoLocalConfig.from_record re-raises it as a
        # RepoLocalConfigError so the loader keeps one fail-closed boundary (asserted above).
        with self.assertRaises(LanePlacementError):
            LanePlacementConfig(placements=(("default", "sideways", None),))
        with self.assertRaises(LanePlacementError):
            LanePlacementConfig(placements=(("nowhere", "down", None),))
        with self.assertRaises(LanePlacementError):
            LanePlacementConfig(placements=(("default", None, ("codex",)),))

    def test_config_is_hashable(self) -> None:
        # The placements tuple keeps RepoLocalConfig hashable (the frozen-record rule).
        config = RepoLocalConfig.from_record(
            {"lane_placement": {"default": {"split": "down", "order": ["codex", "claude"]}}}
        )
        self.assertIsInstance(hash(config.lane_placement), int)


if __name__ == "__main__":
    unittest.main()
