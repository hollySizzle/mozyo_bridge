"""Desired presentation grouping schema / validation / authority-guard tests (#12263, #12322).

Pins the closed-schema parsing side of the cockpit Project Group grouping config
(field contract fixed docs-only by Redmine #12262):

- fail-closed rejection of unknown keys, unsupported versions, duplicate or
  dangling group references, unknown predicates, and unknown projections
  (config schema + generic validation);
- fail-closed rejection of target / route / approval / credential-shaped keys
  *and* identity / diagnostic values, with the documented free-prose carve-out
  for ``label`` / ``description`` / ``label_override`` (authority leak guard);
- the #12286 ``project_group_presentation`` display-placement mode: default,
  opt-in round-trip, and fail-closed on unknown / authority-shaped values.

These exercise the
:mod:`mozyo_bridge.domain.presentation_grouping.config` /
:mod:`~mozyo_bridge.domain.presentation_grouping.validation` /
:mod:`~mozyo_bridge.domain.presentation_grouping.authority` submodules through
the package's public facade. The placement resolver and degraded classifier are
covered by ``test_presentation_grouping_placement`` /
``test_presentation_grouping_degraded``. No tmux, file IO, or CLI here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.presentation_grouping import (
    ALLOWED_PROJECTIONS,
    DEFAULT_PROJECT_GROUP_PRESENTATION,
    PROJECT_GROUP_PRESENTATION_MODES,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
    PresentationGroupingConfig,
    PresentationGroupingConfigError,
)


class FailClosedSchemaTest(unittest.TestCase):
    """Closed schema: unknown / unsupported / dangling / authority-shaped reject."""

    def test_unknown_top_level_key(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record({"unexpected": 1})

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record({"version": 2})

    def test_non_integer_version_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record({"version": True})

    def test_non_mapping_record_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record([1, 2, 3])  # type: ignore[arg-type]

    def test_duplicate_group_id_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [
                        {"group_id": "project:x", "label": "X"},
                        {"group_id": "project:x", "label": "X2"},
                    ]
                }
            )

    def test_group_missing_required_field(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"project_groups": [{"group_id": "project:x"}]}
            )

    def test_rule_references_unknown_group_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "membership_rules": [
                            {"when": {"repo_label": "x"}, "group_id": "project:absent"}
                        ]
                    },
                }
            )

    def test_override_references_unknown_group_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "unit_overrides": [
                            {
                                "workspace_id": "w",
                                "lane_id": "default",
                                "preferred_group": "project:absent",
                            }
                        ]
                    },
                }
            )

    def test_default_references_unknown_group_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {"defaults": {"unknown_unit_group": "project:absent"}},
                }
            )

    def test_unknown_membership_predicate_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "membership_rules": [
                            {"when": {"pane_id": "%5"}, "group_id": "project:x"}
                        ]
                    },
                }
            )

    def test_unknown_projection_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "membership_rules": [
                            {
                                "when": {"repo_label": "x"},
                                "group_id": "project:x",
                                "preferred_projection": "holographic_window",
                            }
                        ]
                    },
                }
            )

    def test_allowed_projections_are_exactly_the_two_builtins(self) -> None:
        self.assertEqual(ALLOWED_PROJECTIONS, frozenset({"cockpit_pane", "normal_window"}))


class NoRoutingAuthorityLeakageTest(unittest.TestCase):
    """Authority / routing / credential-shaped config never parses (fail closed)."""

    def test_route_shaped_top_level_key_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record({"routing": {"to": "codex"}})

    def test_target_shaped_group_key_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [
                        {"group_id": "project:x", "label": "X", "target_pane": "%3"}
                    ]
                }
            )

    def test_approval_shaped_override_key_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "unit_overrides": [
                            {
                                "workspace_id": "w",
                                "lane_id": "default",
                                "owner_approval": True,
                            }
                        ]
                    },
                }
            )

    def test_send_route_in_predicate_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "membership_rules": [
                            {"when": {"send_target": "%2"}, "group_id": "project:x"}
                        ]
                    },
                }
            )

    def test_module_shaped_key_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"module_path": "evil.plugin", "project_groups": []}
            )

    def test_credential_shaped_default_key_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"grouping": {"defaults": {"api_token": "x"}}}
            )

    # --- boundary-shaped VALUES, not just keys (review of #12263) ---

    def test_target_shaped_group_id_value_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"project_groups": [{"group_id": "target:%3", "label": "X"}]}
            )

    def test_credential_shaped_group_id_value_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"project_groups": [{"group_id": "secret:token", "label": "X"}]}
            )

    def test_route_shaped_membership_rule_group_id_value_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "membership_rules": [
                            {"when": {"repo_label": "x"}, "group_id": "route:codex"}
                        ]
                    },
                }
            )

    def test_target_shaped_preferred_group_value_rejected(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {
                    "project_groups": [{"group_id": "project:x", "label": "X"}],
                    "grouping": {
                        "unit_overrides": [
                            {
                                "workspace_id": "w",
                                "lane_id": "default",
                                "preferred_group": "pane:%2",
                            }
                        ]
                    },
                }
            )

    def test_boundary_shaped_default_group_value_rejected(self) -> None:
        for field_name, value in (
            ("missing_group", "credential:x"),
            ("unknown_unit_group", "send:codex"),
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(PresentationGroupingConfigError):
                    PresentationGroupingConfig.from_record(
                        {"grouping": {"defaults": {field_name: value}}}
                    )

    def test_boundary_shaped_degraded_display_value_rejected(self) -> None:
        # degraded_display is operator-facing diagnostic text -> token-guarded.
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"grouping": {"defaults": {"degraded_display": "owner approval needed"}}}
            )

    def test_free_display_label_with_common_word_is_allowed(self) -> None:
        # Documented carve-out: label / description / label_override are public-safe
        # free prose and are NOT token-scanned, so a legitimate "Code Review" label
        # is preserved rather than rejected as boundary-shaped.
        config = PresentationGroupingConfig.from_record(
            {
                "project_groups": [
                    {
                        "group_id": "project:x",
                        "label": "Code Review",
                        "description": "Closed and owner-pending items",
                    }
                ],
                "grouping": {
                    "unit_overrides": [
                        {
                            "workspace_id": "w",
                            "lane_id": "default",
                            "preferred_group": "project:x",
                            "label_override": "Review queue",
                        }
                    ]
                },
            }
        )
        self.assertEqual(config.project_groups[0].label, "Code Review")
        self.assertEqual(
            config.project_groups[0].description, "Closed and owner-pending items"
        )
        self.assertEqual(config.unit_overrides[0].label_override, "Review queue")


class ProjectGroupPresentationTest(unittest.TestCase):
    """The #12286 display-placement mode: default / opt-in / fail-closed."""

    def test_missing_field_defaults_to_same_cockpit_column(self) -> None:
        # Missing config preserves current behavior exactly.
        self.assertEqual(
            DEFAULT_PROJECT_GROUP_PRESENTATION,
            PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
        )
        self.assertEqual(
            PresentationGroupingConfig.default().project_group_presentation,
            PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
        )
        self.assertEqual(
            PresentationGroupingConfig.from_record(
                {"project_groups": [{"group_id": "g", "label": "G"}]}
            ).project_group_presentation,
            PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
        )

    def test_opt_in_modes_round_trip(self) -> None:
        for mode in (
            PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
            PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
            PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
        ):
            config = PresentationGroupingConfig.from_record(
                {"project_group_presentation": mode}
            )
            self.assertEqual(config.project_group_presentation, mode)

    def test_only_placement_field_with_no_groups_is_valid(self) -> None:
        # A placement preference with no groups is the ungrouped default layout.
        config = PresentationGroupingConfig.from_record(
            {"project_group_presentation": PROJECT_GROUP_PRESENTATION_TMUX_WINDOW}
        )
        self.assertEqual(config.project_groups, ())
        self.assertEqual(
            config.project_group_presentation,
            PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
        )

    def test_invalid_value_fails_closed(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"project_group_presentation": "iterm_tab"}
            )

    def test_authority_shaped_value_fails_closed(self) -> None:
        # An authority / routing-shaped value is not a known mode -> rejected; the
        # placement mode can never become a routing / approval target.
        for value in ("route_to_owner", "approve", "%5", True, 1):
            with self.assertRaises(PresentationGroupingConfigError):
                PresentationGroupingConfig.from_record(
                    {"project_group_presentation": value}
                )

    def test_mode_set_is_exactly_the_three_documented_modes(self) -> None:
        self.assertEqual(
            PROJECT_GROUP_PRESENTATION_MODES,
            frozenset(
                {
                    PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
                    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
                    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
