"""Desired presentation grouping launch-placement resolver tests (#12263, #12302, #12322).

Pins the placement-resolution side of the cockpit Project Group grouping config:

- ``resolve_launch_placement`` precedence — default when config is absent,
  configured via override-then-rule, ungrouped when nothing matches — by
  workspace / project / lane context;
- ``resolve_group_window_placement`` — mapping a ``project_group_presentation``
  mode + a resolved ``GroupPlacement`` to the desired / executed launcher
  surface, with the opt-in surfaces visibly degrading to the shared cockpit
  column (never a silent reroute) and an unknown mode failing closed.

These exercise the
:mod:`mozyo_bridge.domain.presentation_grouping.placement` submodule through the
package facade. Schema / validation / authority-guard rejection lives in
``test_presentation_grouping_schema``; degraded-condition classification lives
in ``test_presentation_grouping_degraded``. No tmux, file IO, or CLI here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.presentation_grouping import (
    GROUP_WINDOW_SURFACE_COCKPIT_COLUMN,
    GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    GROUP_WINDOW_SURFACE_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW,
    PROJECT_GROUP_PRESENTATION_SAME_COLUMN,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
    STATUS_CONFIGURED,
    STATUS_DEFAULT,
    STATUS_UNGROUPED,
    GroupPlacement,
    GroupWindowDecision,
    LaunchContext,
    PresentationGroupingConfig,
    PresentationGroupingConfigError,
    resolve_group_window_placement,
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


class ResolveGroupWindowPlacementTest(unittest.TestCase):
    """The launcher / cockpit-append placement decision resolver (#12302).

    Maps a configured ``project_group_presentation`` mode + a resolved
    ``GroupPlacement`` to the desired/executed surface the cockpit launcher reads.
    Pure: no tmux, no IO. ``same_cockpit_column`` is behavior-preserving; the
    opt-in surfaces record the *desired* placement but visibly degrade to the
    shared cockpit column (never a silent reroute); an unknown mode fails closed.
    """

    def _placement(self, **over) -> GroupPlacement:
        base = dict(status=STATUS_CONFIGURED, group_id="project:alpha", label="Alpha")
        base.update(over)
        return GroupPlacement(**base)

    def test_same_cockpit_column_is_behavior_preserving(self) -> None:
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_SAME_COLUMN, self._placement()
        )
        self.assertIsInstance(decision, GroupWindowDecision)
        self.assertEqual(decision.desired_surface, GROUP_WINDOW_SURFACE_COCKPIT_COLUMN)
        self.assertEqual(decision.executed_surface, GROUP_WINDOW_SURFACE_COCKPIT_COLUMN)
        self.assertFalse(decision.degraded)
        self.assertIsNone(decision.diagnostic)
        # group identity is carried for display only.
        self.assertEqual(decision.group_id, "project:alpha")
        self.assertEqual(decision.label, "Alpha")
        self.assertIsNone(decision.desired_window_name)

    def test_tmux_window_records_desired_but_degrades_to_column(self) -> None:
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_TMUX_WINDOW, self._placement()
        )
        self.assertEqual(
            decision.desired_surface, GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW
        )
        # Visibly degraded: the executed surface stays the shared cockpit column so
        # the duplicate-detection / pane-identity gate is preserved.
        self.assertEqual(decision.executed_surface, GROUP_WINDOW_SURFACE_COCKPIT_COLUMN)
        self.assertTrue(decision.degraded)
        self.assertIsNotNone(decision.diagnostic)
        # public-safe desired window name = the group's display label.
        self.assertEqual(decision.desired_window_name, "Alpha")

    def test_tmux_window_default_group_uses_repo_label_window_name(self) -> None:
        # No configured group (default placement) -> the implicit per-repo group;
        # the window name falls back to the repo/workspace label, never None-erased.
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
            GroupPlacement(status=STATUS_DEFAULT, group_id=None, label="mozyo_bridge"),
        )
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.desired_window_name, "mozyo_bridge")

    def test_normal_window_records_desired_but_degrades_to_column(self) -> None:
        decision = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_NORMAL_WINDOW, self._placement()
        )
        self.assertEqual(decision.desired_surface, GROUP_WINDOW_SURFACE_NORMAL_WINDOW)
        self.assertEqual(decision.executed_surface, GROUP_WINDOW_SURFACE_COCKPIT_COLUMN)
        self.assertTrue(decision.degraded)
        self.assertIsNotNone(decision.diagnostic)
        self.assertIsNone(decision.desired_window_name)

    def test_unknown_mode_fails_closed(self) -> None:
        for value in ("iterm_tab", "route_to_owner", "", "cockpit_column"):
            with self.assertRaises(PresentationGroupingConfigError):
                resolve_group_window_placement(value, self._placement())

    def test_decision_as_dict_is_public_safe_display_only(self) -> None:
        payload = resolve_group_window_placement(
            PROJECT_GROUP_PRESENTATION_TMUX_WINDOW, self._placement()
        ).as_dict()
        self.assertEqual(
            set(payload),
            {
                "presentation_mode",
                "desired_surface",
                "executed_surface",
                "group_id",
                "label",
                "desired_window_name",
                "degraded",
                "diagnostic",
            },
        )
        # No routing / pane / target keys leak into the display payload.
        for forbidden in ("pane", "target", "route", "pane_id", "command"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
