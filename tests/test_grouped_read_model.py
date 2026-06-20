"""Grouped cockpit read model tests (Redmine #12264).

Pins the first code that generates the grouped cockpit read model as a
home-state projection, composing the #12263 desired presentation grouping config
/ launch-placement resolver with the #12224 runtime observation envelope and
home-scoped observed Units:

- normal grouped projection (config groups + membership rules -> Project Group
  views, behavior-preserving default when config is absent);
- missing / stale observation (observed_at / freshness / stale_reason carried,
  never derived to fresh / healthy, group with no live target shown stale);
- config / runtime contradiction (identity_conflict from a contradicting live
  identity, desired_unit_missing from an override with no observed Unit);
- hidden (desired) vs active (observed) separation (a hidden Unit with a live
  target is shown in a separate bucket, never dropped);
- no action-permission leakage (no row / payload carries a target / pane / route
  / send / approval / credential field).

No tmux, file IO, or CLI is exercised here — pure projection only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.grouped_read_model import (
    GROUP_SOURCE_DEFAULT,
    GROUP_SOURCE_DESIRED,
    GROUPED_READ_MODEL_DIAGNOSTIC_ONLY_NOTE,
    UNIT_STATUS_CONTRADICTED,
    UNIT_STATUS_OBSERVED,
    UNIT_STATUS_PARTIAL,
    UNIT_STATUS_STALE,
    UNIT_STATUS_UNKNOWN,
    UNIT_STATUS_UNREADABLE,
    UNKNOWN_OBSERVATION,
    GroupedReadModel,
    ObservedUnit,
    ProjectGroupView,
    UnitView,
    build_grouped_read_model,
)
from mozyo_bridge.domain.presentation_grouping import (
    STATUS_DESIRED_UNIT_MISSING,
    STATUS_IDENTITY_CONFLICT,
    PresentationGroupingConfig,
)
from mozyo_bridge.domain.presentation_grouping import (
    _FORBIDDEN_KEY_PARTS as FORBIDDEN_KEY_PARTS,
)
from mozyo_bridge.domain.runtime_observation import (
    CONTRADICTION_LIVE_RUNTIME_CONFLICT,
    DISPLAY_STATE_HEALTHY,
    DISPLAY_STATE_RELOAD_REQUIRED,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    READABILITY_PARTIAL,
    READABILITY_READABLE,
    READABILITY_UNREADABLE,
    SOURCE_TMUX,
    STALE_REASON_AGE_EXCEEDED,
    STALE_REASON_SOURCE_UNREADABLE,
    STRENGTH_STRONG_RUNTIME_SIGNAL,
    RuntimeObservationSnapshot,
)


def _fresh_observation(observed_at: str = "2026-06-19T14:00:00Z") -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at=observed_at,
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=None,
        contradiction=None,
        display_state=DISPLAY_STATE_HEALTHY,
    )


def _stale_observation(observed_at: str = "2026-06-19T10:00:00Z") -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at=observed_at,
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_STALE,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=STALE_REASON_AGE_EXCEEDED,
        contradiction=None,
        display_state=DISPLAY_STATE_RELOAD_REQUIRED,
    )


def _unreadable_observation() -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at=None,
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_UNKNOWN,
        readability=READABILITY_UNREADABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=STALE_REASON_SOURCE_UNREADABLE,
        contradiction=None,
        display_state=DISPLAY_STATE_RELOAD_REQUIRED,
    )


def _contradicted_observation() -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at="2026-06-19T14:00:00Z",
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason="contradicted",
        contradiction=CONTRADICTION_LIVE_RUNTIME_CONFLICT,
        display_state="unknown",
    )


GROUPED_CONFIG_RECORD = {
    "version": 1,
    "project_groups": [
        {"group_id": "project:alpha", "label": "Alpha", "sort_key": 20},
        {"group_id": "project:bravo", "label": "Bravo", "sort_key": 10},
    ],
    "grouping": {
        "membership_rules": [
            {"when": {"repo_label": "alpha"}, "group_id": "project:alpha"},
            {"when": {"repo_label": "bravo"}, "group_id": "project:bravo"},
        ],
    },
}


class NormalGroupedProjectionTests(unittest.TestCase):
    def test_units_group_under_declared_project_groups(self) -> None:
        config = PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD)
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                lane_id="default",
                repo_label="alpha",
                active=True,
                observation=_fresh_observation(),
            ),
            ObservedUnit(
                workspace_id="ws-b",
                lane_id="default",
                repo_label="bravo",
                active=True,
                observation=_fresh_observation(),
            ),
        ]
        model = build_grouped_read_model(config, units)

        # Declared groups appear in sort_key order: bravo (10) before alpha (20).
        self.assertEqual(
            [group.group_id for group in model.groups],
            ["project:bravo", "project:alpha"],
        )
        for group in model.groups:
            self.assertEqual(group.source, GROUP_SOURCE_DESIRED)
            self.assertFalse(group.stale)  # each has a live target
            self.assertEqual(len(group.units), 1)
            (unit,) = group.units
            self.assertEqual(unit.status, UNIT_STATUS_OBSERVED)
            self.assertTrue(unit.active)
            self.assertFalse(unit.needs_reload)
            self.assertEqual(unit.freshness, FRESHNESS_FRESH)

    def test_missing_config_is_behavior_preserving_default(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                lane_id="default",
                repo_label="alpha",
                active=True,
                observation=_fresh_observation(),
            )
        ]
        model = build_grouped_read_model(None, units)

        self.assertEqual(len(model.groups), 1)
        (group,) = model.groups
        self.assertIsNone(group.group_id)
        self.assertEqual(group.source, GROUP_SOURCE_DEFAULT)
        (unit,) = group.units
        self.assertEqual(unit.label, "alpha")  # repo label fallback
        self.assertEqual(unit.status, UNIT_STATUS_OBSERVED)
        self.assertIsNone(unit.group_id)

    def test_declared_group_with_no_live_target_is_stale_not_dropped(self) -> None:
        config = PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD)
        # Only an alpha unit observed; bravo group has no members at all.
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                active=True,
                observation=_fresh_observation(),
            )
        ]
        model = build_grouped_read_model(config, units)
        by_id = {group.group_id: group for group in model.groups}
        self.assertIn("project:bravo", by_id)  # empty declared group still shown
        self.assertTrue(by_id["project:bravo"].stale)
        self.assertEqual(by_id["project:bravo"].units, ())
        self.assertFalse(by_id["project:alpha"].stale)


class FreshnessTests(unittest.TestCase):
    def test_stale_observation_is_visible_and_never_fresh(self) -> None:
        config = PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD)
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                active=True,
                observation=_stale_observation(),
            )
        ]
        model = build_grouped_read_model(config, units)
        unit = model.all_units()[0]
        self.assertEqual(unit.status, UNIT_STATUS_STALE)
        self.assertEqual(unit.freshness, FRESHNESS_STALE)
        self.assertEqual(unit.stale_reason, STALE_REASON_AGE_EXCEEDED)
        self.assertTrue(unit.needs_reload)
        # active is observed-liveness, independent of freshness: a stale snapshot
        # of a Unit that still has a live target keeps active True (group not stale).
        self.assertTrue(unit.active)
        by_id = {group.group_id: group for group in model.groups}
        self.assertFalse(by_id[unit.group_id].stale)

    def test_unobserved_unit_reads_unknown(self) -> None:
        units = [ObservedUnit(workspace_id="ws-a", repo_label="alpha")]
        model = build_grouped_read_model(None, units)
        unit = model.all_units()[0]
        self.assertEqual(unit.status, UNIT_STATUS_UNKNOWN)
        self.assertEqual(unit.freshness, FRESHNESS_UNKNOWN)
        self.assertIsNone(unit.observed_at)
        self.assertTrue(unit.needs_reload)
        self.assertFalse(unit.active)

    def test_unreadable_observation_status(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                observation=_unreadable_observation(),
            )
        ]
        model = build_grouped_read_model(None, units)
        self.assertEqual(model.all_units()[0].status, UNIT_STATUS_UNREADABLE)

    def test_contradicted_observation_status(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                active=True,
                observation=_contradicted_observation(),
            )
        ]
        model = build_grouped_read_model(None, units)
        unit = model.all_units()[0]
        self.assertEqual(unit.status, UNIT_STATUS_CONTRADICTED)
        self.assertEqual(unit.contradiction, CONTRADICTION_LIVE_RUNTIME_CONFLICT)

    def test_overall_snapshot_degraded_marks_model_needs_reload(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                active=True,
                observation=_fresh_observation(),
            )
        ]
        model = build_grouped_read_model(
            None, units, observation=_stale_observation()
        )
        self.assertTrue(model.needs_reload)
        self.assertTrue(
            any("reload" in note for note in model.diagnostics),
        )

    def test_fresh_overall_snapshot_does_not_need_reload(self) -> None:
        model = build_grouped_read_model(
            None,
            [ObservedUnit(workspace_id="ws-a", observation=_fresh_observation())],
            observation=_fresh_observation(),
        )
        self.assertFalse(model.needs_reload)


class ContradictionTests(unittest.TestCase):
    def test_identity_conflict_is_visible(self) -> None:
        # Live observed identity contradicts the launch identity.
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                lane_id="default",
                repo_label="alpha",
                observed_workspace_id="ws-OTHER",
                active=True,
                observation=_fresh_observation(),
            )
        ]
        model = build_grouped_read_model(
            PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD), units
        )
        unit = model.all_units()[0]
        self.assertEqual(unit.status, STATUS_IDENTITY_CONFLICT)
        self.assertIsNotNone(unit.diagnostic)

    def test_desired_unit_missing_from_override_with_no_observed_unit(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {
                "version": 1,
                "project_groups": [
                    {"group_id": "project:alpha", "label": "Alpha"},
                ],
                "grouping": {
                    "unit_overrides": [
                        {
                            "workspace_id": "ws-ghost",
                            "lane_id": "default",
                            "preferred_group": "project:alpha",
                            "position": 5,
                        }
                    ],
                },
            }
        )
        # No observed Unit matches the override selector.
        model = build_grouped_read_model(config, [])
        missing = [
            unit
            for unit in model.all_units()
            if unit.status == STATUS_DESIRED_UNIT_MISSING
        ]
        self.assertEqual(len(missing), 1)
        (ghost,) = missing
        self.assertEqual(ghost.workspace_id, "ws-ghost")
        self.assertEqual(ghost.group_id, "project:alpha")
        self.assertFalse(ghost.active)
        self.assertIsNone(ghost.observed_at)
        self.assertTrue(
            any("desired_unit_missing" in note for note in model.diagnostics)
        )

    def test_present_override_does_not_flag_missing(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {
                "version": 1,
                "project_groups": [{"group_id": "project:alpha", "label": "Alpha"}],
                "grouping": {
                    "unit_overrides": [
                        {
                            "workspace_id": "ws-a",
                            "lane_id": "default",
                            "preferred_group": "project:alpha",
                        }
                    ]
                },
            }
        )
        model = build_grouped_read_model(
            config,
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    lane_id="default",
                    active=True,
                    observation=_fresh_observation(),
                )
            ],
        )
        self.assertFalse(
            any(
                unit.status == STATUS_DESIRED_UNIT_MISSING
                for unit in model.all_units()
            )
        )


class HiddenActiveSeparationTests(unittest.TestCase):
    def test_hidden_unit_with_live_target_is_separated_not_dropped(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {
                "version": 1,
                "project_groups": [{"group_id": "project:alpha", "label": "Alpha"}],
                "grouping": {
                    "unit_overrides": [
                        {
                            "workspace_id": "ws-a",
                            "lane_id": "default",
                            "preferred_group": "project:alpha",
                            "hidden": True,
                        }
                    ]
                },
            }
        )
        model = build_grouped_read_model(
            config,
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    lane_id="default",
                    active=True,  # active
                    observation=_fresh_observation(),
                )
            ],
        )
        (group,) = model.groups
        # The hidden Unit is not in the visible bucket but IS surfaced in the
        # hidden bucket, with its observed liveness preserved (active True).
        self.assertEqual(group.units, ())
        self.assertEqual(len(group.hidden_units), 1)
        (hidden_unit,) = group.hidden_units
        self.assertTrue(hidden_unit.hidden)
        self.assertTrue(hidden_unit.active)
        # The group keeps a live target, so it is not stale despite hiding it.
        self.assertFalse(group.stale)
        # all_units() still includes it — never dropped.
        self.assertIn(hidden_unit, model.all_units())


class NoActionPermissionLeakageTests(unittest.TestCase):
    def _assert_no_boundary_tokens(self, names) -> None:
        for name in names:
            lowered = name.lower()
            for part in FORBIDDEN_KEY_PARTS:
                self.assertNotIn(
                    part,
                    lowered,
                    msg=f"read-model field {name!r} carries boundary token {part!r}",
                )

    def test_dataclass_fields_carry_no_routing_or_authority_token(self) -> None:
        for cls in (ObservedUnit, UnitView, ProjectGroupView, GroupedReadModel):
            self._assert_no_boundary_tokens(cls.__dataclass_fields__.keys())

    def test_payload_keys_carry_no_routing_or_authority_token(self) -> None:
        config = PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD)
        model = build_grouped_read_model(
            config,
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    repo_label="alpha",
                    active=True,
                    observation=_fresh_observation(),
                )
            ],
        )
        payload = model.as_payload()
        # Scan every nested key in the JSON projection.
        keys: set[str] = set(payload)
        for group in payload["groups"]:
            keys.update(group)
            for unit in group["units"] + group["hidden_units"]:
                keys.update(unit)
        # observation envelope keys are observation-quality only by construction.
        keys.update(payload["observation"])
        self._assert_no_boundary_tokens(keys)

    def test_payload_carries_projection_only_boundary_note(self) -> None:
        model = build_grouped_read_model(None, [])
        self.assertEqual(
            model.as_payload()["boundary_note"],
            GROUPED_READ_MODEL_DIAGNOSTIC_ONLY_NOTE,
        )

    def test_payload_has_no_truth_like_workflow_fields(self) -> None:
        model = build_grouped_read_model(
            None,
            [ObservedUnit(workspace_id="ws-a", observation=_fresh_observation())],
        )
        forbidden = {"completed", "approved", "current_status", "delivered", "accepted"}
        unit_payload = model.as_payload()["groups"][0]["units"][0]
        self.assertEqual(set(unit_payload) & forbidden, set())


def _partial_observation() -> RuntimeObservationSnapshot:
    # freshness=fresh but the source was only partially readable -> reload_required.
    return RuntimeObservationSnapshot(
        observed_at="2026-06-19T14:00:00Z",
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_PARTIAL,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason="reload_required",
        contradiction=None,
        display_state=DISPLAY_STATE_RELOAD_REQUIRED,
    )


def _fresh_but_reload_required_observation() -> RuntimeObservationSnapshot:
    # A defensively inconsistent snapshot: fresh + readable fields, yet
    # display_state demands reload. It must still never read as observed.
    return RuntimeObservationSnapshot(
        observed_at="2026-06-19T14:00:00Z",
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason="reload_required",
        contradiction=None,
        display_state=DISPLAY_STATE_RELOAD_REQUIRED,
    )


class PartialReloadRequiredTests(unittest.TestCase):
    """Review j#61833/j#61835 finding 1: partial / reload_required is not observed."""

    def test_partial_readability_is_degraded_not_observed(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                active=True,
                observation=_partial_observation(),
            )
        ]
        model = build_grouped_read_model(None, units)
        unit = model.all_units()[0]
        self.assertNotEqual(unit.status, UNIT_STATUS_OBSERVED)
        self.assertEqual(unit.status, UNIT_STATUS_PARTIAL)
        self.assertTrue(unit.needs_reload)

    def test_reload_required_display_state_is_never_observed(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                active=True,
                observation=_fresh_but_reload_required_observation(),
            )
        ]
        model = build_grouped_read_model(None, units)
        unit = model.all_units()[0]
        self.assertNotEqual(unit.status, UNIT_STATUS_OBSERVED)
        self.assertTrue(unit.needs_reload)


class DefaultLabelGroupingTests(unittest.TestCase):
    """Review j#61835 finding 1: config-absent default groups by repo/workspace label."""

    def test_distinct_repo_labels_form_distinct_default_groups(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                repo_label="alpha",
                active=True,
                observation=_fresh_observation(),
            ),
            ObservedUnit(
                workspace_id="ws-b",
                repo_label="bravo",
                active=True,
                observation=_fresh_observation(),
            ),
        ]
        model = build_grouped_read_model(None, units)
        # Two distinct labeled default groups, not one mixed unlabeled bucket.
        self.assertEqual(len(model.groups), 2)
        labels = sorted(group.label for group in model.groups)
        self.assertEqual(labels, ["alpha", "bravo"])
        for group in model.groups:
            self.assertEqual(group.source, GROUP_SOURCE_DEFAULT)
            self.assertIsNone(group.group_id)
            self.assertEqual(len(group.all_units()), 1)

    def test_same_label_units_share_one_default_group(self) -> None:
        units = [
            ObservedUnit(
                workspace_id="ws-a",
                lane_id="default",
                repo_label="alpha",
                active=True,
                observation=_fresh_observation(),
            ),
            ObservedUnit(
                workspace_id="ws-a",
                lane_id="issue-1",
                repo_label="alpha",
                active=True,
                observation=_fresh_observation(),
            ),
        ]
        model = build_grouped_read_model(None, units)
        self.assertEqual(len(model.groups), 1)
        (group,) = model.groups
        self.assertEqual(group.label, "alpha")
        self.assertEqual(len(group.all_units()), 2)


class HostAwareMissingOverrideTests(unittest.TestCase):
    """Review j#61833/j#61835 finding 2: missing-override detection respects host_id."""

    def _config(self) -> PresentationGroupingConfig:
        return PresentationGroupingConfig.from_record(
            {
                "version": 1,
                "project_groups": [{"group_id": "project:x", "label": "X"}],
                "grouping": {
                    "unit_overrides": [
                        {
                            "host_id": "remote",
                            "workspace_id": "ws-a",
                            "lane_id": "default",
                            "preferred_group": "project:x",
                        }
                    ]
                },
            }
        )

    def test_host_specific_override_not_masked_by_other_host(self) -> None:
        # Override targets host=remote; only a host=local Unit on the same
        # workspace/lane is observed -> the remote desired Unit must still surface.
        model = build_grouped_read_model(
            self._config(),
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    lane_id="default",
                    host_id="local",
                    active=True,
                    observation=_fresh_observation(),
                )
            ],
        )
        missing = [
            unit
            for unit in model.all_units()
            if unit.status == STATUS_DESIRED_UNIT_MISSING
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].host_id, "remote")
        self.assertTrue(any("host remote" in note for note in model.diagnostics))

    def test_host_specific_override_present_when_host_matches(self) -> None:
        model = build_grouped_read_model(
            self._config(),
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    lane_id="default",
                    host_id="remote",
                    active=True,
                    observation=_fresh_observation(),
                )
            ],
        )
        self.assertFalse(
            any(
                unit.status == STATUS_DESIRED_UNIT_MISSING
                for unit in model.all_units()
            )
        )

    def test_host_unspecified_override_is_any_host(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {
                "version": 1,
                "project_groups": [{"group_id": "project:x", "label": "X"}],
                "grouping": {
                    "unit_overrides": [
                        {
                            "workspace_id": "ws-a",
                            "lane_id": "default",
                            "preferred_group": "project:x",
                        }
                    ]
                },
            }
        )
        # A host-unspecified override matches any host -> not missing.
        model = build_grouped_read_model(
            config,
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    lane_id="default",
                    host_id="local",
                    active=True,
                    observation=_fresh_observation(),
                )
            ],
        )
        self.assertFalse(
            any(
                unit.status == STATUS_DESIRED_UNIT_MISSING
                for unit in model.all_units()
            )
        )


class EmptyInputTests(unittest.TestCase):
    def test_no_units_no_config_yields_empty_model(self) -> None:
        model = build_grouped_read_model(None, [])
        self.assertEqual(model.groups, ())
        self.assertEqual(model.observation, UNKNOWN_OBSERVATION)
        self.assertTrue(model.needs_reload)


class ProjectGroupPresentationSurfacingTest(unittest.TestCase):
    """The read model surfaces the desired placement mode as display metadata (#12286)."""

    def test_default_config_surfaces_same_cockpit_column(self) -> None:
        model = build_grouped_read_model(None, [])
        self.assertEqual(
            model.project_group_presentation, "same_cockpit_column"
        )
        self.assertEqual(
            model.as_payload()["project_group_presentation"],
            "same_cockpit_column",
        )

    def test_opt_in_mode_flows_from_config_to_payload(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {"project_group_presentation": "project_group_tmux_window"}
        )
        model = build_grouped_read_model(config, [])
        self.assertEqual(
            model.project_group_presentation, "project_group_tmux_window"
        )
        self.assertEqual(
            model.as_payload()["project_group_presentation"],
            "project_group_tmux_window",
        )

    def test_placement_mode_carries_no_routing_authority(self) -> None:
        # The mode is metadata; it adds no target / pane / route / approval field.
        config = PresentationGroupingConfig.from_record(
            {"project_group_presentation": "normal_window"}
        )
        payload = build_grouped_read_model(config, []).as_payload()
        for forbidden in ("target", "pane", "route", "send", "approval"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
