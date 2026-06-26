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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.presentation_grouping import (
    ALLOWED_PROJECTIONS,
    DEFAULT_DELEGATION_WINDOW_POLICY,
    DEFAULT_PROJECT_GROUP_PRESENTATION,
    DELEGATION_WINDOW_POLICY_MODES,
    DELEGATION_WINDOW_POLICY_SEPARATE,
    DELEGATION_WINDOW_POLICY_SHARED,
    DELEGATION_WINDOW_STATUS_DIAGNOSTIC,
    DELEGATION_WINDOW_STATUS_NONE,
    DELEGATION_WINDOW_STATUS_RESOLVED,
    PROJECT_GROUP_PRESENTATION_MODES,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
    PresentationGroupingConfig,
    PresentationGroupingConfigError,
    resolve_delegation_window_display,
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


class DelegationWindowPolicyConfigTest(unittest.TestCase):
    """The #12467 ``delegation_window_policy`` display knob: default / round-trip /
    fail-closed. A closed display-only vocabulary that never becomes routing
    authority, mirroring the ``project_group_presentation`` contract."""

    def test_default_is_separate(self) -> None:
        config = PresentationGroupingConfig.from_record(None)
        self.assertEqual(
            config.delegation_window_policy, DEFAULT_DELEGATION_WINDOW_POLICY
        )
        self.assertEqual(
            config.delegation_window_policy, DELEGATION_WINDOW_POLICY_SEPARATE
        )

    def test_missing_field_preserves_default(self) -> None:
        config = PresentationGroupingConfig.from_record({"version": 1})
        self.assertEqual(
            config.delegation_window_policy, DELEGATION_WINDOW_POLICY_SEPARATE
        )

    def test_explicit_modes_round_trip(self) -> None:
        for mode in (
            DELEGATION_WINDOW_POLICY_SEPARATE,
            DELEGATION_WINDOW_POLICY_SHARED,
        ):
            config = PresentationGroupingConfig.from_record(
                {"delegation_window_policy": mode}
            )
            self.assertEqual(config.delegation_window_policy, mode)

    def test_invalid_value_fails_closed(self) -> None:
        with self.assertRaises(PresentationGroupingConfigError):
            PresentationGroupingConfig.from_record(
                {"delegation_window_policy": "split_screen"}
            )

    def test_authority_shaped_value_fails_closed(self) -> None:
        # An authority / routing-shaped value is not a known policy -> rejected;
        # the window policy can never become a routing / approval target.
        for value in ("route_to_owner", "approve", "%5", True, 1):
            with self.assertRaises(PresentationGroupingConfigError):
                PresentationGroupingConfig.from_record(
                    {"delegation_window_policy": value}
                )

    def test_mode_set_is_exactly_the_two_documented_policies(self) -> None:
        self.assertEqual(
            DELEGATION_WINDOW_POLICY_MODES,
            frozenset(
                {
                    DELEGATION_WINDOW_POLICY_SEPARATE,
                    DELEGATION_WINDOW_POLICY_SHARED,
                }
            ),
        )

    def test_coexists_with_project_group_presentation(self) -> None:
        # The two top-level display knobs are independent fields.
        config = PresentationGroupingConfig.from_record(
            {
                "project_group_presentation": PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
                "delegation_window_policy": DELEGATION_WINDOW_POLICY_SHARED,
            }
        )
        self.assertEqual(
            config.project_group_presentation, PROJECT_GROUP_PRESENTATION_TMUX_WINDOW
        )
        self.assertEqual(
            config.delegation_window_policy, DELEGATION_WINDOW_POLICY_SHARED
        )


class DelegationWindowResolverTest(unittest.TestCase):
    """The #12467 display-only resolver: separate/shared projection over the
    closed #12466 delegated-tree breadcrumb, fail-soft and non-authoritative."""

    def _resolve(self, policy, **kw):
        base = dict(
            lane_kind="implementation",
            delegation_depth=2,
            delegation_unit="wsA/deleg",
            delegation_root="wsA/root",
            status="derived",
        )
        base.update(kw)
        return resolve_delegation_window_display(policy, **base)

    def test_separate_keeps_grandchild_in_its_own_window(self) -> None:
        win = self._resolve(DELEGATION_WINDOW_POLICY_SEPARATE)
        self.assertTrue(win.separated)
        self.assertEqual(win.window_group, "wsA/deleg")
        self.assertEqual(win.status, DELEGATION_WINDOW_STATUS_RESOLVED)
        self.assertEqual(win.policy, DELEGATION_WINDOW_POLICY_SEPARATE)

    def test_shared_folds_grandchild_onto_tree_root(self) -> None:
        win = self._resolve(DELEGATION_WINDOW_POLICY_SHARED)
        self.assertFalse(win.separated)
        self.assertEqual(win.window_group, "wsA/root")
        self.assertEqual(win.status, DELEGATION_WINDOW_STATUS_RESOLVED)

    def test_delegated_coordinator_depth1_follows_policy(self) -> None:
        sep = self._resolve(
            DELEGATION_WINDOW_POLICY_SEPARATE,
            lane_kind="delegated_coordinator",
            delegation_depth=1,
        )
        self.assertTrue(sep.separated)
        self.assertEqual(sep.window_group, "wsA/deleg")
        shared = self._resolve(
            DELEGATION_WINDOW_POLICY_SHARED,
            lane_kind="delegated_coordinator",
            delegation_depth=1,
        )
        self.assertFalse(shared.separated)
        self.assertEqual(shared.window_group, "wsA/root")

    def test_root_is_always_its_own_window_regardless_of_policy(self) -> None:
        for policy in (
            DELEGATION_WINDOW_POLICY_SEPARATE,
            DELEGATION_WINDOW_POLICY_SHARED,
        ):
            win = self._resolve(
                policy,
                lane_kind="coordinator",
                delegation_depth=0,
                delegation_unit="wsA/root",
            )
            self.assertTrue(win.separated)
            self.assertEqual(win.window_group, "wsA/root")
            self.assertEqual(win.status, DELEGATION_WINDOW_STATUS_RESOLVED)

    def test_no_delegation_fact_yields_none_status(self) -> None:
        win = self._resolve(
            DELEGATION_WINDOW_POLICY_SEPARATE,
            lane_kind="",
            delegation_depth=None,
            status="none",
        )
        self.assertEqual(win.status, DELEGATION_WINDOW_STATUS_NONE)
        self.assertFalse(win.separated)
        self.assertEqual(win.window_group, "")
        # The effective policy is still echoed so the surface is explicit.
        self.assertEqual(win.policy, DELEGATION_WINDOW_POLICY_SEPARATE)

    def test_diagnostic_tree_withholds_decision(self) -> None:
        win = self._resolve(
            DELEGATION_WINDOW_POLICY_SHARED,
            lane_kind="coordinator",
            delegation_depth=None,
            status="diagnostic",
        )
        self.assertEqual(win.status, DELEGATION_WINDOW_STATUS_DIAGNOSTIC)
        self.assertFalse(win.separated)
        self.assertEqual(win.window_group, "")

    def test_unexpected_policy_degrades_to_default(self) -> None:
        # The display layer never raises on a drifted policy; the config layer is
        # the fail-closed boundary. An unknown value resolves under the default.
        win = self._resolve("bogus")
        self.assertEqual(win.policy, DEFAULT_DELEGATION_WINDOW_POLICY)
        self.assertTrue(win.separated)  # default `separate`

    def test_payload_carries_no_routing_or_authority_field(self) -> None:
        payload = self._resolve(DELEGATION_WINDOW_POLICY_SHARED).as_payload()
        self.assertEqual(
            set(payload),
            {"window_policy", "window_separated", "window_group", "window_status"},
        )
        forbidden = {
            "target",
            "pane_id",
            "route",
            "send",
            "approval",
            "close",
            "role",
            "repo_root",
        }
        self.assertEqual(forbidden & set(payload), set())


if __name__ == "__main__":
    unittest.main()
