"""Desired presentation grouping schema + launch-placement resolver tests (#12263).

Pins the first code that parses and applies the cockpit Project Group grouping
config whose field contract was fixed docs-only by Redmine #12262 and whose
projection model was fixed by Redmine #12253:

- the closed schema (``version`` / ``project_groups`` / ``grouping`` with its
  ``membership_rules`` / ``unit_overrides`` / ``defaults``) and its
  behavior-preserving empty default;
- fail-closed rejection of unknown keys, unsupported versions, duplicate or
  dangling group references, unknown projections, and target / route / approval /
  credential-shaped keys (no routing-authority leakage);
- launch placement resolution by workspace / project / lane context: default
  when config is absent, configured via rule + override, ungrouped when nothing
  matches, and *visible* degraded status for identity conflict / desired-unit
  missing — never a silent reroute.

No tmux, file IO, or CLI is exercised here — schema + pure resolver only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.presentation_grouping import (
    ALLOWED_PROJECTIONS,
    STATUS_CONFIGURED,
    STATUS_DEFAULT,
    STATUS_DESIRED_UNIT_MISSING,
    STATUS_IDENTITY_CONFLICT,
    STATUS_UNGROUPED,
    GroupingDefaults,
    LaunchContext,
    PresentationGroupingConfig,
    PresentationGroupingConfigError,
    diagnose_unit_overrides,
    resolve_launch_placement,
)


def _full_config() -> PresentationGroupingConfig:
    """A representative valid config exercising groups, rules, overrides, defaults."""
    return PresentationGroupingConfig.from_record(
        {
            "version": 1,
            "project_groups": [
                {"group_id": "project:alpha", "label": "Alpha", "sort_key": 10},
                {
                    "group_id": "project:beta",
                    "label": "Beta",
                    "collapsed": True,
                    "description": "Beta units",
                },
                {"group_id": "project:misc", "label": "Misc"},
            ],
            "grouping": {
                "membership_rules": [
                    {
                        "when": {"repo_label": "alpha"},
                        "group_id": "project:alpha",
                        "position": 5,
                        "preferred_projection": "cockpit_pane",
                    },
                    {
                        "when": {"lane_prefix": "issue_"},
                        "group_id": "project:beta",
                        "pinned": True,
                    },
                ],
                "unit_overrides": [
                    {
                        "workspace_id": "ws-special",
                        "lane_id": "default",
                        "preferred_group": "project:beta",
                        "position": 1,
                        "hidden": True,
                        "label_override": "Special",
                    }
                ],
                "defaults": {
                    "unknown_unit_group": "project:misc",
                    "degraded_display": "needs operator attention",
                    "collapsed": True,
                    "preferred_projection": "normal_window",
                },
            },
        }
    )


class DefaultBehaviorTest(unittest.TestCase):
    """Missing / empty config is behavior-preserving (no grouping change)."""

    def test_none_config_is_default_placement_by_repo_label(self) -> None:
        placement = resolve_launch_placement(
            None, LaunchContext(workspace_id="ws1", repo_label="alpha")
        )
        self.assertEqual(placement.status, STATUS_DEFAULT)
        self.assertIsNone(placement.group_id)
        self.assertEqual(placement.label, "alpha")
        self.assertFalse(placement.pinned)
        self.assertFalse(placement.hidden)

    def test_default_falls_back_to_workspace_id_when_no_repo_label(self) -> None:
        placement = resolve_launch_placement(None, LaunchContext(workspace_id="ws1"))
        self.assertEqual(placement.status, STATUS_DEFAULT)
        self.assertEqual(placement.label, "ws1")

    def test_empty_record_resolves_to_default_config(self) -> None:
        self.assertEqual(
            PresentationGroupingConfig.from_record({}),
            PresentationGroupingConfig.default(),
        )

    def test_empty_config_object_still_default_placement(self) -> None:
        placement = resolve_launch_placement(
            PresentationGroupingConfig.default(),
            LaunchContext(workspace_id="ws1", repo_label="alpha"),
        )
        self.assertEqual(placement.status, STATUS_DEFAULT)
        self.assertIsNone(placement.group_id)


class ConfiguredPlacementTest(unittest.TestCase):
    """A populated config places launches by override, then rule, then default."""

    def test_membership_rule_by_repo_label(self) -> None:
        placement = resolve_launch_placement(
            _full_config(),
            LaunchContext(workspace_id="ws1", lane_id="default", repo_label="alpha"),
        )
        self.assertEqual(placement.status, STATUS_CONFIGURED)
        self.assertEqual(placement.group_id, "project:alpha")
        self.assertEqual(placement.label, "Alpha")
        self.assertEqual(placement.position, 5)
        self.assertEqual(placement.preferred_projection, "cockpit_pane")

    def test_membership_rule_by_lane_prefix(self) -> None:
        placement = resolve_launch_placement(
            _full_config(),
            LaunchContext(
                workspace_id="ws2", lane_id="issue_12263_x", repo_label="other"
            ),
        )
        self.assertEqual(placement.status, STATUS_CONFIGURED)
        self.assertEqual(placement.group_id, "project:beta")
        self.assertTrue(placement.pinned)
        # group beta declares collapsed:true -> placement inherits it.
        self.assertTrue(placement.collapsed)

    def test_unit_override_wins_over_membership_rule(self) -> None:
        # ws-special also matches the alpha rule by repo_label, but the override
        # selector (workspace_id+lane_id) takes precedence.
        placement = resolve_launch_placement(
            _full_config(),
            LaunchContext(
                workspace_id="ws-special", lane_id="default", repo_label="alpha"
            ),
        )
        self.assertEqual(placement.status, STATUS_CONFIGURED)
        self.assertEqual(placement.group_id, "project:beta")
        self.assertEqual(placement.position, 1)
        self.assertTrue(placement.hidden)
        self.assertEqual(placement.label, "Special")  # label_override

    def test_no_match_falls_back_to_unknown_unit_group(self) -> None:
        placement = resolve_launch_placement(
            _full_config(),
            LaunchContext(workspace_id="ws-unknown", lane_id="default", repo_label="z"),
        )
        self.assertEqual(placement.status, STATUS_CONFIGURED)
        self.assertEqual(placement.group_id, "project:misc")

    def test_no_match_and_no_default_group_is_ungrouped(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {
                "project_groups": [{"group_id": "project:alpha", "label": "Alpha"}],
                "grouping": {
                    "membership_rules": [
                        {"when": {"repo_label": "alpha"}, "group_id": "project:alpha"}
                    ]
                },
            }
        )
        placement = resolve_launch_placement(
            config, LaunchContext(workspace_id="ws9", repo_label="zeta")
        )
        self.assertEqual(placement.status, STATUS_UNGROUPED)
        self.assertIsNone(placement.group_id)
        self.assertEqual(placement.label, "zeta")

    def test_predicate_not_carried_by_context_does_not_match(self) -> None:
        # A rule keyed on project_id must not fire when context lacks project_id.
        config = PresentationGroupingConfig.from_record(
            {
                "project_groups": [{"group_id": "project:alpha", "label": "Alpha"}],
                "grouping": {
                    "membership_rules": [
                        {"when": {"project_id": "100"}, "group_id": "project:alpha"}
                    ]
                },
            }
        )
        placement = resolve_launch_placement(
            config, LaunchContext(workspace_id="ws1", repo_label="alpha")
        )
        self.assertEqual(placement.status, STATUS_UNGROUPED)

    def test_multi_predicate_rule_requires_all(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {
                "project_groups": [{"group_id": "project:alpha", "label": "Alpha"}],
                "grouping": {
                    "membership_rules": [
                        {
                            "when": {"repo_label": "alpha", "project_id": "100"},
                            "group_id": "project:alpha",
                        }
                    ]
                },
            }
        )
        # repo_label matches but project_id does not -> no match.
        miss = resolve_launch_placement(
            config,
            LaunchContext(workspace_id="ws1", repo_label="alpha", project_id="999"),
        )
        self.assertEqual(miss.status, STATUS_UNGROUPED)
        # both match -> configured.
        hit = resolve_launch_placement(
            config,
            LaunchContext(workspace_id="ws1", repo_label="alpha", project_id="100"),
        )
        self.assertEqual(hit.group_id, "project:alpha")


class DegradedDisplayTest(unittest.TestCase):
    """Runtime drift surfaces as visible degraded status, never a silent reroute."""

    def test_identity_conflict_when_observed_workspace_differs(self) -> None:
        placement = resolve_launch_placement(
            _full_config(),
            LaunchContext(
                workspace_id="ws1",
                repo_label="alpha",
                observed_workspace_id="ws-other",
            ),
        )
        self.assertEqual(placement.status, STATUS_IDENTITY_CONFLICT)
        # placement is still computed (display), preflight decides side effects.
        self.assertEqual(placement.group_id, "project:alpha")
        self.assertEqual(placement.diagnostic, "needs operator attention")

    def test_identity_conflict_when_observed_lane_differs(self) -> None:
        placement = resolve_launch_placement(
            _full_config(),
            LaunchContext(
                workspace_id="ws-special",
                lane_id="default",
                observed_lane_id="lane-zzz",
            ),
        )
        self.assertEqual(placement.status, STATUS_IDENTITY_CONFLICT)
        self.assertEqual(placement.group_id, "project:beta")

    def test_no_conflict_when_observed_matches(self) -> None:
        placement = resolve_launch_placement(
            _full_config(),
            LaunchContext(
                workspace_id="ws1",
                repo_label="alpha",
                observed_workspace_id="ws1",
                observed_lane_id="default",
            ),
        )
        self.assertEqual(placement.status, STATUS_CONFIGURED)

    def test_diagnose_unit_overrides_flags_missing_unit(self) -> None:
        config = _full_config()
        # ws-special override is not among the observed live units.
        flagged = diagnose_unit_overrides(config, frozenset({("ws1", "default")}))
        self.assertEqual(len(flagged), 1)
        override, status = flagged[0]
        self.assertEqual(override.workspace_id, "ws-special")
        self.assertEqual(status, STATUS_DESIRED_UNIT_MISSING)

    def test_diagnose_unit_overrides_no_flag_when_live(self) -> None:
        config = _full_config()
        flagged = diagnose_unit_overrides(
            config, frozenset({("ws-special", "default")})
        )
        self.assertEqual(flagged, ())


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


if __name__ == "__main__":
    unittest.main()
