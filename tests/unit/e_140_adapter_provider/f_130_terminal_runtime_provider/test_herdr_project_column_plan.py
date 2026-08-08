"""Pure desired-order/relative-width planning for shared Herdr project columns."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_registry import WorkspaceRecord
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_plan import (  # noqa: E501
    ObservedUnitColumn,
    QUALITY_BEST_EFFORT,
    QUALITY_DEGRADED,
    QUALITY_EXACT,
    REASON_CONFIG_INVALID,
    REASON_CONFIG_MISSING,
    REASON_POSITION_TIE,
    REASON_POSITION_UNSPECIFIED,
    REASON_RATIO_UNREPRESENTABLE,
    REASON_RULE_CONTEXT_INCOMPLETE,
    REASON_UNIT_INPUT_INVALID,
    REASON_WIDTH_UNSPECIFIED,
    REASON_WORKSPACE_UNRESOLVED,
    UnitColumnKey,
    UnitColumnPreference,
    UnitColumnRuleFacts,
    plan_project_columns,
    resolve_project_column_plan,
)


def _observed(
    workspace_id: str,
    index: int,
    *,
    lane_id: str = "default",
    host_id: str = "local",
) -> ObservedUnitColumn:
    return ObservedUnitColumn(
        key=UnitColumnKey(workspace_id, lane_id, host_id),
        current_index=index,
    )


def _preference(
    workspace_id: str,
    index: int,
    *,
    position: int | None,
    width: float | None,
) -> UnitColumnPreference:
    return UnitColumnPreference(
        observed=_observed(workspace_id, index),
        position=position,
        relative_width=width,
    )


def _workspace(workspace_id: str, canonical_path: str) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=workspace_id,
        canonical_path=canonical_path,
        display_path=canonical_path,
        project_name=workspace_id,
        canonical_session=f"session-{workspace_id}",
        preset=None,
        preset_version=None,
        created_at="2026-08-08T00:00:00+00:00",
        updated_at="2026-08-08T00:00:00+00:00",
        last_seen=None,
    )


def _healthy_canonical(_path: str | None) -> dict[str, object]:
    return {"is_dir": True, "is_main_worktree": True}


class PurePlanTest(unittest.TestCase):
    def test_explicit_order_and_two_one_one_weights_produce_half_ratios(self) -> None:
        plan = plan_project_columns(
            (
                _preference("ws-a", 0, position=30, width=1),
                _preference("ws-b", 1, position=10, width=2),
                _preference("ws-c", 2, position=20, width=1),
            )
        )
        self.assertEqual(plan.quality, QUALITY_EXACT)
        self.assertEqual(plan.reasons, ())
        self.assertEqual(
            plan.desired_order,
            (
                UnitColumnKey("ws-b", "default"),
                UnitColumnKey("ws-c", "default"),
                UnitColumnKey("ws-a", "default"),
            ),
        )
        self.assertEqual(
            [(target.left_unit, target.ratio) for target in plan.ratio_targets],
            [
                (UnitColumnKey("ws-b", "default"), 0.5),
                (UnitColumnKey("ws-c", "default"), 0.5),
            ],
        )
        self.assertTrue(plan.requires_reorder)
        self.assertFalse(plan.executable)

    def test_tied_and_unspecified_positions_keep_live_relative_order(self) -> None:
        plan = plan_project_columns(
            (
                _preference("ws-c", 2, position=None, width=1),
                _preference("ws-a", 0, position=10, width=1),
                _preference("ws-b", 1, position=10, width=1),
            )
        )
        self.assertEqual(plan.quality, QUALITY_BEST_EFFORT)
        self.assertEqual(
            plan.reasons, (REASON_POSITION_UNSPECIFIED, REASON_POSITION_TIE)
        )
        self.assertEqual(
            plan.desired_order,
            (
                UnitColumnKey("ws-a", "default"),
                UnitColumnKey("ws-b", "default"),
                UnitColumnKey("ws-c", "default"),
            ),
        )
        self.assertFalse(plan.requires_reorder)

    def test_missing_width_defaults_to_one_and_is_visible_best_effort(self) -> None:
        plan = plan_project_columns(
            (
                _preference("ws-a", 0, position=10, width=2),
                _preference("ws-b", 1, position=20, width=None),
            )
        )
        self.assertEqual(plan.quality, QUALITY_BEST_EFFORT)
        self.assertEqual(plan.reasons, (REASON_WIDTH_UNSPECIFIED,))
        self.assertAlmostEqual(plan.ratio_targets[0].ratio, 2.0 / 3.0)

    def test_unrepresentable_ratio_is_degraded_and_has_zero_targets(self) -> None:
        plan = plan_project_columns(
            (
                _preference("ws-a", 0, position=10, width=100),
                _preference("ws-b", 1, position=20, width=1),
            )
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertEqual(plan.reasons, (REASON_RATIO_UNREPRESENTABLE,))
        self.assertEqual(plan.ratio_targets, ())
        self.assertFalse(plan.executable)

    def test_duplicate_identity_or_index_is_invalid_and_zero_target(self) -> None:
        duplicate = _preference("ws-a", 0, position=10, width=1)
        plan = plan_project_columns((duplicate, duplicate))
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertIn(REASON_UNIT_INPUT_INVALID, plan.reasons)
        self.assertEqual(plan.ratio_targets, ())

    def test_malformed_position_returns_typed_invalid_instead_of_sort_error(self) -> None:
        malformed = UnitColumnPreference(
            observed=_observed("ws-a", 0),
            position="first",  # type: ignore[arg-type]
            relative_width=1,
        )
        plan = plan_project_columns(
            (malformed, _preference("ws-b", 1, position=2, width=1))
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertEqual(plan.reasons, (REASON_UNIT_INPUT_INVALID,))
        self.assertEqual(plan.ratio_targets, ())

    def test_right_nested_weight_math_is_keyed_by_unit_identity(self) -> None:
        plan = plan_project_columns(
            tuple(
                _preference(f"ws-{index}", index, position=index, width=width)
                for index, width in enumerate((1, 2, 3))
            )
        )
        self.assertEqual(plan.quality, QUALITY_EXACT)
        self.assertEqual(
            [target.left_unit for target in plan.ratio_targets],
            [UnitColumnKey("ws-0", "default"), UnitColumnKey("ws-1", "default")],
        )
        self.assertAlmostEqual(plan.ratio_targets[0].ratio, 1.0 / 6.0)
        self.assertAlmostEqual(plan.ratio_targets[1].ratio, 2.0 / 5.0)

    def test_closed_ratio_boundaries_are_representable(self) -> None:
        one_nine = plan_project_columns(
            (
                _preference("ws-a", 0, position=0, width=1),
                _preference("ws-b", 1, position=1, width=9),
            )
        )
        nine_one = plan_project_columns(
            (
                _preference("ws-a", 0, position=0, width=9),
                _preference("ws-b", 1, position=1, width=1),
            )
        )
        self.assertEqual(one_nine.quality, QUALITY_EXACT)
        self.assertEqual(nine_one.quality, QUALITY_EXACT)
        self.assertEqual(one_nine.ratio_targets[0].ratio, 0.1)
        self.assertEqual(nine_one.ratio_targets[0].ratio, 0.9)

    def test_ratios_just_outside_closed_boundaries_are_not_representable(self) -> None:
        one_ten = plan_project_columns(
            (
                _preference("ws-a", 0, position=0, width=1),
                _preference("ws-b", 1, position=1, width=10),
            )
        )
        ten_one = plan_project_columns(
            (
                _preference("ws-a", 0, position=0, width=10),
                _preference("ws-b", 1, position=1, width=1),
            )
        )
        self.assertEqual(one_ten.quality, QUALITY_DEGRADED)
        self.assertEqual(ten_one.quality, QUALITY_DEGRADED)
        self.assertEqual(one_ten.ratio_targets, ())
        self.assertEqual(ten_one.ratio_targets, ())

    def test_any_unrepresentable_deep_ratio_removes_every_target(self) -> None:
        plan = plan_project_columns(
            tuple(
                _preference(f"ws-{index}", index, position=index, width=width)
                for index, width in enumerate((10, 0.9, 9.1))
            )
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertEqual(plan.ratio_targets, ())
        self.assertIn(REASON_RATIO_UNREPRESENTABLE, plan.reasons)

    def test_weighted_eleven_columns_are_not_rejected_by_equal_share_limit(self) -> None:
        weighted = plan_project_columns(
            tuple(
                _preference(
                    f"ws-{index}",
                    index,
                    position=index,
                    width=float(9 ** (10 - index)),
                )
                for index in range(11)
            )
        )
        equal = plan_project_columns(
            tuple(
                _preference(f"eq-{index}", index, position=index, width=1)
                for index in range(11)
            )
        )
        self.assertEqual(weighted.quality, QUALITY_EXACT)
        self.assertEqual(len(weighted.ratio_targets), 10)
        self.assertEqual(equal.quality, QUALITY_DEGRADED)
        self.assertEqual(equal.ratio_targets, ())

    def test_scaled_math_handles_huge_and_subnormal_finite_weights(self) -> None:
        huge = plan_project_columns(
            (
                _preference("huge-a", 0, position=0, width=1e308),
                _preference("huge-b", 1, position=1, width=1e308),
            )
        )
        tiny = plan_project_columns(
            (
                _preference("tiny-a", 0, position=0, width=5e-324),
                _preference("tiny-b", 1, position=1, width=5e-324),
            )
        )
        baseline = plan_project_columns(
            tuple(
                _preference(f"base-{index}", index, position=index, width=width)
                for index, width in enumerate((1, 2, 3))
            )
        )
        scaled = plan_project_columns(
            tuple(
                _preference(
                    f"scaled-{index}",
                    index,
                    position=index,
                    width=width,
                )
                for index, width in enumerate((1e307, 2e307, 3e307))
            )
        )
        self.assertEqual(huge.ratio_targets[0].ratio, 0.5)
        self.assertEqual(tiny.ratio_targets[0].ratio, 0.5)
        self.assertEqual(len(baseline.ratio_targets), len(scaled.ratio_targets))
        for baseline_target, scaled_target in zip(
            baseline.ratio_targets, scaled.ratio_targets
        ):
            self.assertAlmostEqual(baseline_target.ratio, scaled_target.ratio)


class CanonicalConfigResolutionTest(unittest.TestCase):
    def test_each_workspace_loads_only_its_registry_canonical_repo_config(self) -> None:
        records = {
            "ws-a": _workspace("ws-a", "/canonical/a"),
            "ws-b": _workspace("ws-b", "/canonical/b"),
        }
        seen_paths: list[Path] = []

        def workspace_loader(workspace_id, *, home=None):
            self.assertEqual(home, Path("/isolated/home"))
            return records.get(workspace_id)

        def config_loader(path: Path) -> RepoLocalConfig:
            seen_paths.append(path)
            workspace_id = "ws-a" if path == Path("/canonical/a") else "ws-b"
            position = 20 if workspace_id == "ws-a" else 10
            width = 1 if workspace_id == "ws-a" else 2
            return RepoLocalConfig.from_record(
                {
                    "presentation": {
                        "grouping": {
                            "unit_overrides": [
                                {
                                    "workspace_id": workspace_id,
                                    "lane_id": "default",
                                    "position": position,
                                    "relative_width": width,
                                }
                            ]
                        }
                    }
                }
            )

        plan = resolve_project_column_plan(
            (_observed("ws-a", 0), _observed("ws-b", 1)),
            home=Path("/isolated/home"),
            workspace_loader=workspace_loader,
            config_loader=config_loader,
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(seen_paths, [Path("/canonical/a"), Path("/canonical/b")])
        self.assertEqual(plan.quality, QUALITY_EXACT)
        self.assertEqual(
            plan.desired_order,
            (UnitColumnKey("ws-b", "default"), UnitColumnKey("ws-a", "default")),
        )
        self.assertAlmostEqual(plan.ratio_targets[0].ratio, 2.0 / 3.0)

    def test_default_grouping_is_visible_as_config_missing_best_effort(self) -> None:
        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-a", "/canonical/a"
            ),
            config_loader=lambda _path: RepoLocalConfig.default(),
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_BEST_EFFORT)
        self.assertEqual(
            plan.reasons,
            (
                REASON_CONFIG_MISSING,
                REASON_POSITION_UNSPECIFIED,
                REASON_WIDTH_UNSPECIFIED,
            ),
        )
        self.assertTrue(plan.executable)

    def test_unresolved_workspace_is_degraded_without_reading_any_config(self) -> None:
        config_reads = []
        plan = resolve_project_column_plan(
            (_observed("missing", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: None,
            config_loader=lambda path: config_reads.append(path),
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertEqual(plan.reasons[0], REASON_WORKSPACE_UNRESOLVED)
        self.assertEqual(plan.ratio_targets, ())
        self.assertEqual(config_reads, [])

    def test_invalid_config_is_degraded_and_zero_target(self) -> None:
        def invalid_config(_path: Path) -> RepoLocalConfig:
            raise RepoLocalConfigError("invalid")

        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-a", "/canonical/a"
            ),
            config_loader=invalid_config,
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertEqual(plan.reasons[0], REASON_CONFIG_INVALID)
        self.assertEqual(plan.ratio_targets, ())

    def test_column_fields_use_override_then_first_rule_value(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "presentation": {
                    "grouping": {
                        "membership_rules": [
                            {
                                "when": {},
                                "position": 20,
                                "relative_width": 3,
                            }
                        ],
                        "unit_overrides": [
                            {
                                "workspace_id": "ws-a",
                                "lane_id": "default",
                                "position": 10,
                            }
                        ],
                    }
                }
            }
        )
        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-a", "/canonical/a"
            ),
            config_loader=lambda _path: config,
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_EXACT)
        self.assertIsNotNone(plan.source_fingerprint)
        self.assertTrue(plan.executable)

    def test_unknown_prior_rule_fact_blocks_catch_all_plan(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "presentation": {
                    "grouping": {
                        "membership_rules": [
                            {
                                "when": {"project_id": "special"},
                                "position": 5,
                                "relative_width": 2,
                            },
                            {
                                "when": {},
                                "position": 10,
                                "relative_width": 1,
                            },
                        ]
                    }
                }
            }
        )
        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-a", "/canonical/a"
            ),
            config_loader=lambda _path: config,
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertIn(REASON_RULE_CONTEXT_INCOMPLETE, plan.reasons)
        self.assertFalse(plan.executable)

    def test_dedicated_fact_resolver_can_supply_project_rule_authority(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "presentation": {
                    "grouping": {
                        "membership_rules": [
                            {
                                "when": {"project_id": "special"},
                                "position": 1,
                                "relative_width": 2,
                            }
                        ]
                    }
                }
            }
        )
        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-a", "/canonical/a"
            ),
            config_loader=lambda _path: config,
            canonical_probe=_healthy_canonical,
            rule_fact_resolver=lambda _observed, _record: UnitColumnRuleFacts(
                repo_label="ws-a", project_id="special"
            ),
        )
        self.assertEqual(plan.quality, QUALITY_EXACT)
        self.assertNotIn(REASON_RULE_CONTEXT_INCOMPLETE, plan.reasons)

    def test_multiple_matching_overrides_are_config_invalid(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "presentation": {
                    "grouping": {
                        "unit_overrides": [
                            {
                                "workspace_id": "ws-a",
                                "lane_id": "default",
                                "relative_width": 1,
                            },
                            {
                                "workspace_id": "ws-a",
                                "lane_id": "default",
                                "position": 1,
                            },
                        ]
                    }
                }
            }
        )
        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-a", "/canonical/a"
            ),
            config_loader=lambda _path: config,
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertIn(REASON_CONFIG_INVALID, plan.reasons)
        self.assertEqual(plan.ratio_targets, ())

    def test_same_workspace_registry_and_config_are_read_once(self) -> None:
        workspace_reads = 0
        config_reads = 0
        config = RepoLocalConfig.from_record(
            {
                "presentation": {
                    "grouping": {
                        "unit_overrides": [
                            {
                                "workspace_id": "ws-a",
                                "lane_id": "lane-a",
                                "position": 1,
                                "relative_width": 1,
                            },
                            {
                                "workspace_id": "ws-a",
                                "lane_id": "lane-b",
                                "position": 2,
                                "relative_width": 1,
                            },
                        ]
                    }
                }
            }
        )

        def workspace_loader(_workspace_id, *, home=None):
            nonlocal workspace_reads
            workspace_reads += 1
            return _workspace("ws-a", "/canonical/a")

        def config_loader(_path):
            nonlocal config_reads
            config_reads += 1
            return config

        plan = resolve_project_column_plan(
            (
                _observed("ws-a", 0, lane_id="lane-a"),
                _observed("ws-a", 1, lane_id="lane-b"),
            ),
            workspace_loader=workspace_loader,
            config_loader=config_loader,
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_EXACT)
        self.assertEqual(workspace_reads, 1)
        self.assertEqual(config_reads, 1)
        self.assertEqual(len(plan.source_fingerprint or ""), 64)
        self.assertTrue(plan.executable)

    def test_source_fingerprint_is_stable_and_rule_fact_sensitive(self) -> None:
        common = {
            "workspace_loader": lambda _workspace_id, *, home=None: _workspace(
                "ws-a", "/canonical/a"
            ),
            "config_loader": lambda _path: RepoLocalConfig.default(),
            "canonical_probe": _healthy_canonical,
        }
        first = resolve_project_column_plan((_observed("ws-a", 0),), **common)
        repeated = resolve_project_column_plan((_observed("ws-a", 0),), **common)
        changed = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            **common,
            rule_fact_resolver=lambda _observed, _record: UnitColumnRuleFacts(
                repo_label="changed"
            ),
        )
        self.assertEqual(first.source_fingerprint, repeated.source_fingerprint)
        self.assertNotEqual(first.source_fingerprint, changed.source_fingerprint)
        self.assertTrue(first.executable)
        self.assertTrue(changed.executable)

    def test_dead_or_linked_canonical_path_is_zero_inference(self) -> None:
        config_reads = []
        for probe in (
            lambda _path: {"is_dir": False, "is_main_worktree": None},
            lambda _path: {"is_dir": True, "is_main_worktree": False},
        ):
            with self.subTest(probe=probe):
                plan = resolve_project_column_plan(
                    (_observed("ws-a", 0),),
                    workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                        "ws-a", "/private/canonical-sentinel"
                    ),
                    config_loader=lambda path: config_reads.append(path),
                    canonical_probe=probe,
                )
                self.assertEqual(plan.quality, QUALITY_DEGRADED)
                self.assertIn(REASON_WORKSPACE_UNRESOLVED, plan.reasons)
                self.assertNotIn("canonical-sentinel", repr(plan))
        self.assertEqual(config_reads, [])

    def test_invalid_config_path_never_leaks_into_plan(self) -> None:
        sentinel = "/private/canonical-sentinel"

        def invalid_config(_path: Path) -> RepoLocalConfig:
            raise RepoLocalConfigError(f"invalid config at {sentinel}")

        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-a", sentinel
            ),
            config_loader=invalid_config,
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertNotIn(sentinel, repr(plan))
        self.assertIsNone(plan.source_fingerprint)

    def test_registry_record_must_match_requested_workspace(self) -> None:
        plan = resolve_project_column_plan(
            (_observed("ws-a", 0),),
            workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                "ws-other", "/canonical/other"
            ),
            config_loader=lambda _path: RepoLocalConfig.default(),
            canonical_probe=_healthy_canonical,
        )
        self.assertEqual(plan.quality, QUALITY_DEGRADED)
        self.assertIn(REASON_WORKSPACE_UNRESOLVED, plan.reasons)
        self.assertFalse(plan.executable)

    def test_invalid_rule_fact_result_is_degraded_and_zero_target(self) -> None:
        for invalid_facts in ({"project_id": "special"}, UnitColumnRuleFacts(project_id=" ")):
            with self.subTest(invalid_facts=invalid_facts):
                plan = resolve_project_column_plan(
                    (_observed("ws-a", 0),),
                    workspace_loader=lambda _workspace_id, *, home=None: _workspace(
                        "ws-a", "/canonical/a"
                    ),
                    config_loader=lambda _path: RepoLocalConfig.default(),
                    canonical_probe=_healthy_canonical,
                    rule_fact_resolver=lambda _observed, _record: invalid_facts,  # type: ignore[arg-type,return-value]
                )
                self.assertEqual(plan.quality, QUALITY_DEGRADED)
                self.assertIn(REASON_RULE_CONTEXT_INCOMPLETE, plan.reasons)
                self.assertEqual(plan.ratio_targets, ())
                self.assertFalse(plan.executable)

    def test_nonlocal_or_empty_unit_identity_is_invalid(self) -> None:
        for key in (
            UnitColumnKey("ws-a", "default", "remote"),
            UnitColumnKey("", "default"),
            UnitColumnKey("ws-a", ""),
        ):
            with self.subTest(key=key):
                plan = plan_project_columns(
                    (
                        UnitColumnPreference(
                            observed=ObservedUnitColumn(key=key, current_index=0),
                            position=1,
                            relative_width=1,
                        ),
                    )
                )
                self.assertEqual(plan.quality, QUALITY_DEGRADED)
                self.assertIn(REASON_UNIT_INPUT_INVALID, plan.reasons)
                self.assertFalse(plan.executable)


if __name__ == "__main__":
    unittest.main()
