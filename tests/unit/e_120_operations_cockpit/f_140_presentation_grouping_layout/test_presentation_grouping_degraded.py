"""Desired presentation grouping degraded-display classification tests (#12263, #12322).

Pins the visible-degraded side of the cockpit Project Group grouping config —
runtime drift surfaces as a *visible* degraded status, never a silent reroute:

- ``identity_conflict`` when a live observation contradicts the launch identity
  (the placement is still computed and shown; the action-time preflight decides
  any side effect);
- ``diagnose_unit_overrides`` flagging an override whose Unit is not among the
  known live Units as ``desired_unit_missing``.

These exercise the
:mod:`mozyo_bridge.domain.presentation_grouping.degraded` classifier and the
identity-conflict path of
:mod:`~mozyo_bridge.domain.presentation_grouping.placement` through the package
facade. Schema / validation lives in ``test_presentation_grouping_schema``;
ordinary placement precedence lives in ``test_presentation_grouping_placement``.
No tmux, file IO, or CLI here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.presentation_grouping import (
    STATUS_CONFIGURED,
    STATUS_DESIRED_UNIT_MISSING,
    STATUS_IDENTITY_CONFLICT,
    LaunchContext,
    PresentationGroupingConfig,
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


if __name__ == "__main__":
    unittest.main()
