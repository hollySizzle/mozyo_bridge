from __future__ import annotations

import json
import unittest
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    ProjectGroupDecision,
    coordinator_panes_in,
    group_by_pair,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_placement import (  # noqa: E501
    COLUMN_MOVE_LEFT,
    COLUMN_MOVE_RIGHT,
    COLUMN_WIDTH_DECREASE,
    COLUMN_WIDTH_INCREASE,
    HerdrProjectColumnPlacement,
    PLACEMENT_APPLIED,
    PLACEMENT_DEFERRED,
    PLACEMENT_PARTIAL,
    PLACEMENT_REFUSED,
    PREVIEW_DEFERRED,
    PREVIEW_MATCHED,
    PREVIEW_READY,
    PREVIEW_REFUSED,
    REASON_ADJUSTMENT_INVALID,
    REASON_EDGE_REACHED,
    REASON_STALE_PREVIEW,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_plan import (  # noqa: E501
    ProjectColumnPlan,
    QUALITY_EXACT,
    UnitColumnKey,
    UnitColumnRatioTarget,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
    encode_assigned_name,
)
from tests.support.herdr_pane_tree import PaneTreeHerdr, Split


class _Authority:
    def resolve(self, rows, *, target_workspace, **_kwargs):
        panes, refusal = coordinator_panes_in(rows, target_workspace)
        if refusal:
            return ProjectGroupDecision.refused(refusal)
        return ProjectGroupDecision(groups=group_by_pair(panes))


def _name(workspace_id: str, provider: str) -> str:
    return encode_assigned_name(workspace_id, provider, DEFAULT_LANE)


def _plan(desired, ratios, fingerprint="a" * 64):
    def resolve(observed, *, home=None):
        del home
        return ProjectColumnPlan(
            quality=QUALITY_EXACT,
            reasons=(),
            desired_order=tuple(desired),
            ratio_targets=tuple(
                UnitColumnRatioTarget(key, ratio) for key, ratio in ratios
            ),
            requires_reorder=tuple(item.key for item in observed) != tuple(desired),
            source_fingerprint=fingerprint,
        )

    return resolve


class ProjectColumnPlacementTest(unittest.TestCase):
    def setUp(self):
        self.herdr = PaneTreeHerdr("w1")
        self.tab = self.herdr.new_tab()
        self.key_a = UnitColumnKey("project-a", DEFAULT_LANE)
        self.key_b = UnitColumnKey("project-b", DEFAULT_LANE)
        self.columns = self.herdr.seed_columns(
            self.tab,
            [
                [_name("project-a", "codex"), _name("project-a", "claude")],
                [_name("project-b", "codex"), _name("project-b", "claude")],
            ],
        )

    def service(self, plan_resolver, *, generation_resolver=None, runner=None):
        return HerdrProjectColumnPlacement(
            home=Path("/unused"),
            target_workspace="w1",
            top_workspace_id="",
            binary="herdr",
            runner=runner or self.herdr,
            timeout=1.0,
            authority=_Authority(),
            generation_resolver=(
                generation_resolver
                or (lambda pane: f"generation:{pane.assigned_name}")
            ),
            plan_resolver=plan_resolver,
        )

    def test_preview_matches_when_order_and_width_are_already_configured(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )

        preview = service.preview()

        self.assertEqual(PREVIEW_MATCHED, preview.status)
        self.assertEqual((), preview.operations)
        self.assertFalse(
            any(call[:2] in (["pane", "move"], ["pane", "swap"], ["pane", "resize"])
                for call in self.herdr.calls)
        )

    def test_apply_reorders_full_units_preserves_internal_ratios_and_resizes(self):
        # Pin distinct internal ratios: the lower panes must return with these exact values.
        root = self.tab.root
        self.assertIsInstance(root, Split)
        self.assertIsInstance(root.first, Split)
        self.assertIsInstance(root.second, Split)
        root.first.ratio = 0.4
        root.second.ratio = 0.6
        service = self.service(
            _plan((self.key_b, self.key_a), ((self.key_b, 0.35),))
        )

        preview = service.preview()
        result = service.apply(preview)

        self.assertEqual(PREVIEW_READY, preview.status)
        self.assertEqual(PLACEMENT_APPLIED, result.status)
        self.assertIsNotNone(result.after)
        self.assertEqual(PREVIEW_MATCHED, result.after.status)
        self.assertEqual((self.key_b, self.key_a), result.after.current_order)
        final_root = self.tab.root
        self.assertIsInstance(final_root, Split)
        self.assertAlmostEqual(0.35, final_root.ratio, places=3)
        self.assertIsInstance(final_root.first, Split)
        self.assertIsInstance(final_root.second, Split)
        self.assertAlmostEqual(0.6, final_root.first.ratio, places=6)
        self.assertAlmostEqual(0.4, final_root.second.ratio, places=6)
        self.assertEqual(1, len(self.herdr.swaps))
        self.assertTrue(self.herdr.resizes)
        self.assertEqual(1, len(self.herdr.tabs))

    def test_incomplete_unit_is_deferred_without_mutation(self):
        self.tab.remove(self.columns[1][1])
        self.herdr.agents.pop(self.columns[1][1])
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )

        result = service.converge()

        self.assertEqual(PLACEMENT_DEFERRED, result.status)
        self.assertEqual(PREVIEW_DEFERRED, result.before.status)
        self.assertFalse(self.herdr.swaps)
        self.assertFalse(self.herdr.resizes)

    def test_layout_drift_after_preview_refuses_before_mutation(self):
        service = self.service(
            _plan((self.key_b, self.key_a), ((self.key_b, 0.5),))
        )
        preview = service.preview()
        root = self.tab.root
        self.assertIsInstance(root, Split)
        root.ratio = 0.45

        result = service.apply(preview)

        self.assertEqual(PLACEMENT_REFUSED, result.status)
        self.assertEqual(REASON_STALE_PREVIEW, result.reason)
        self.assertFalse(self.herdr.swaps)
        self.assertFalse(self.herdr.resizes)

    def test_unproved_swap_reports_partial_failure_and_restores_lowers(self):
        service = self.service(
            _plan((self.key_b, self.key_a), ((self.key_b, 0.5),))
        )
        preview = service.preview()
        self.herdr.swap_unchanged = True

        result = service.apply(preview)

        self.assertNotEqual(PLACEMENT_APPLIED, result.status)
        self.assertEqual(1, len(self.herdr.tabs))
        self.assertEqual(set(self.herdr.agents), set(self.tab.panes()))

    def test_malformed_detach_response_recovers_the_measured_singleton(self):
        service = self.service(
            _plan((self.key_b, self.key_a), ((self.key_b, 0.5),))
        )
        preview = service.preview()
        lower = self.columns[0][1]
        self.herdr.move_malformed_after_geometry.add(lower)

        result = service.apply(preview)

        self.assertEqual(PLACEMENT_PARTIAL, result.status)
        self.assertEqual(1, len(self.herdr.tabs))
        self.assertEqual(set(self.herdr.agents), set(self.tab.panes()))
        self.assertNotIn("remain outside", result.recovery)
        self.assertIn("safe return was attempted", result.recovery)

    def test_generation_drift_after_preview_refuses_before_mutation(self):
        reads = {}

        def generation(pane):
            reads[pane.assigned_name] = reads.get(pane.assigned_name, 0) + 1
            return f"generation:{pane.assigned_name}:{reads[pane.assigned_name]}"

        service = self.service(
            _plan((self.key_b, self.key_a), ((self.key_b, 0.5),)),
            generation_resolver=generation,
        )
        preview = service.preview()

        result = service.apply(preview)

        self.assertEqual(PLACEMENT_REFUSED, result.status)
        self.assertEqual(REASON_STALE_PREVIEW, result.reason)
        self.assertFalse(self.herdr.swaps)
        self.assertFalse(self.herdr.resizes)

    def test_source_change_after_first_detach_stops_and_recovers(self):
        calls = 0

        def changing(observed, *, home=None):
            nonlocal calls
            del home
            calls += 1
            return ProjectColumnPlan(
                quality=QUALITY_EXACT,
                reasons=(),
                desired_order=(self.key_b, self.key_a),
                ratio_targets=(UnitColumnRatioTarget(self.key_b, 0.5),),
                requires_reorder=tuple(item.key for item in observed)
                != (self.key_b, self.key_a),
                source_fingerprint=("a" if calls < 4 else "b") * 64,
            )

        service = self.service(changing)
        preview = service.preview()

        result = service.apply(preview)

        self.assertEqual(PLACEMENT_PARTIAL, result.status)
        self.assertFalse(self.herdr.swaps)
        self.assertEqual(1, len(self.herdr.tabs))
        self.assertEqual(set(self.herdr.agents), set(self.tab.panes()))

    def test_changed_swap_without_geometry_is_partial_and_recovers(self):
        service = self.service(
            _plan((self.key_b, self.key_a), ((self.key_b, 0.5),))
        )
        preview = service.preview()
        self.herdr.swap_without_geometry = True

        result = service.apply(preview)

        self.assertEqual(PLACEMENT_PARTIAL, result.status)
        self.assertEqual(1, len(self.herdr.tabs))
        self.assertEqual((self.key_a, self.key_b), service.preview().current_order)

    def test_resize_changed_false_is_a_known_zero_mutation_refusal(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.35),))
        )
        preview = service.preview()
        self.herdr.resize_unchanged = True

        result = service.apply(preview)

        self.assertEqual(PLACEMENT_REFUSED, result.status)
        self.assertEqual(1, len(self.herdr.resizes))
        self.assertFalse(self.herdr.swaps)

    def test_three_units_converge_through_multiple_adjacent_swaps(self):
        herdr = PaneTreeHerdr("w1")
        tab = herdr.new_tab()
        key_c = UnitColumnKey("project-c", DEFAULT_LANE)
        herdr.seed_columns(
            tab,
            [
                [_name("project-a", "codex"), _name("project-a", "claude")],
                [_name("project-b", "codex"), _name("project-b", "claude")],
                [_name("project-c", "codex"), _name("project-c", "claude")],
            ],
        )
        service = HerdrProjectColumnPlacement(
            home=Path("/unused"),
            target_workspace="w1",
            top_workspace_id="",
            binary="herdr",
            runner=herdr,
            timeout=1.0,
            authority=_Authority(),
            generation_resolver=lambda pane: f"generation:{pane.assigned_name}",
            plan_resolver=_plan(
                (key_c, self.key_a, self.key_b),
                ((key_c, 0.3), (self.key_a, 4.0 / 7.0)),
            ),
        )

        result = service.converge()

        self.assertEqual(PLACEMENT_APPLIED, result.status)
        self.assertIsNotNone(result.after)
        self.assertEqual(
            (key_c, self.key_a, self.key_b),
            result.after.current_order,
        )
        self.assertEqual(2, len(herdr.swaps))
        self.assertTrue(herdr.resizes)
        self.assertEqual(1, len(herdr.tabs))

    def test_untouched_unit_ratio_drift_stops_before_the_next_effect(self):
        herdr = PaneTreeHerdr("w1")
        tab = herdr.new_tab()
        key_c = UnitColumnKey("project-c", DEFAULT_LANE)
        herdr.seed_columns(
            tab,
            [
                [_name("project-a", "codex"), _name("project-a", "claude")],
                [_name("project-b", "codex"), _name("project-b", "claude")],
                [_name("project-c", "codex"), _name("project-c", "claude")],
            ],
        )

        def drift_after_first_detach(argv, **kwargs):
            completed = herdr(argv, **kwargs)
            tail = list(argv[1:])
            if tail[:2] == ["pane", "move"] and "--new-tab" in tail and herdr._moves == 1:
                root = tab.root
                self.assertIsInstance(root, Split)
                self.assertIsInstance(root.second, Split)
                self.assertIsInstance(root.second.second, Split)
                root.second.second.ratio = 0.7
            return completed

        service = HerdrProjectColumnPlacement(
            home=Path("/unused"),
            target_workspace="w1",
            top_workspace_id="",
            binary="herdr",
            runner=drift_after_first_detach,
            timeout=1.0,
            authority=_Authority(),
            generation_resolver=lambda pane: f"generation:{pane.assigned_name}",
            plan_resolver=_plan(
                (self.key_b, self.key_a, key_c),
                ((self.key_b, 1 / 3), (self.key_a, 0.5)),
            ),
        )

        result = service.converge()

        self.assertEqual(PLACEMENT_PARTIAL, result.status)
        self.assertFalse(herdr.swaps)
        self.assertEqual(
            1,
            len(
                [
                    call
                    for call in herdr.calls
                    if call[:2] == ["pane", "move"] and "--new-tab" in call
                ]
            ),
        )

    def test_public_payload_never_contains_runtime_handles_or_generations(self):
        service = self.service(
            _plan((self.key_b, self.key_a), ((self.key_b, 0.5),))
        )
        preview = service.preview()

        rendered = json.dumps(preview.as_payload(), sort_keys=True)

        for pane_id in self.herdr.agents:
            self.assertNotIn(pane_id, rendered)
        self.assertNotIn("generation:", rendered)
        self.assertNotIn(self.tab.tab_id, rendered)

    def test_live_move_right_preserves_measured_widths_and_pair_ratios(self):
        root = self.tab.root
        self.assertIsInstance(root, Split)
        self.assertIsInstance(root.first, Split)
        self.assertIsInstance(root.second, Split)
        root.ratio = 0.3
        root.first.ratio = 0.4
        root.second.ratio = 0.6
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.3),))
        )

        preview = service.preview_adjustment(self.key_a, COLUMN_MOVE_RIGHT)
        self.assertIsNotNone(preview.evidence)
        measured_widths = {
            column.key: preview.evidence.layout.panes[column.top.pane_id].width
            for column in preview.evidence.columns
        }
        result = service.apply_adjustment(preview)

        self.assertEqual(PREVIEW_READY, preview.status)
        self.assertEqual((self.key_b, self.key_a), preview.desired_order)
        self.assertEqual(COLUMN_MOVE_RIGHT, preview.operations[0])
        self.assertEqual(1, preview.selected_current_position)
        self.assertEqual(2, preview.selected_target_position)
        self.assertAlmostEqual(
            preview.selected_current_width_share,
            preview.selected_target_width_share,
        )
        self.assertEqual(PLACEMENT_APPLIED, result.status)
        self.assertIn("no saved configuration was changed", result.detail)
        final_root = self.tab.root
        self.assertIsInstance(final_root, Split)
        expected = measured_widths[self.key_b] / sum(measured_widths.values())
        self.assertAlmostEqual(expected, final_root.ratio, places=3)
        self.assertIsInstance(final_root.first, Split)
        self.assertIsInstance(final_root.second, Split)
        self.assertAlmostEqual(0.6, final_root.first.ratio, places=6)
        self.assertAlmostEqual(0.4, final_root.second.ratio, places=6)
        self.assertEqual(1, len(self.herdr.swaps))

    def test_live_width_increase_resizes_only_the_selected_unit(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )

        preview = service.preview_adjustment(self.key_a, COLUMN_WIDTH_INCREASE)
        result = service.apply_adjustment(preview)

        self.assertEqual(PREVIEW_READY, preview.status)
        self.assertEqual((self.key_a, self.key_b), preview.desired_order)
        self.assertEqual(COLUMN_WIDTH_INCREASE, preview.operations[0])
        self.assertEqual(1, preview.selected_current_position)
        self.assertEqual(1, preview.selected_target_position)
        self.assertAlmostEqual(0.5, preview.selected_current_width_share)
        self.assertAlmostEqual(1.25 / 2.25, preview.selected_target_width_share)
        self.assertEqual(PLACEMENT_APPLIED, result.status)
        root = self.tab.root
        self.assertIsInstance(root, Split)
        self.assertAlmostEqual(1.25 / 2.25, root.ratio, places=3)
        self.assertFalse(self.herdr.swaps)
        self.assertEqual(1, len(self.herdr.resizes))

    def test_live_move_left_uses_the_inverse_adjacent_swap(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )

        preview = service.preview_adjustment(self.key_b, COLUMN_MOVE_LEFT)
        result = service.apply_adjustment(preview)

        self.assertEqual(PREVIEW_READY, preview.status)
        self.assertEqual((self.key_b, self.key_a), preview.desired_order)
        self.assertEqual(COLUMN_MOVE_LEFT, preview.operations[0])
        self.assertEqual(PLACEMENT_APPLIED, result.status)
        self.assertEqual(1, len(self.herdr.swaps))

    def test_live_width_decrease_uses_the_inverse_weight_step(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )

        preview = service.preview_adjustment(self.key_a, COLUMN_WIDTH_DECREASE)
        result = service.apply_adjustment(preview)

        self.assertEqual(PREVIEW_READY, preview.status)
        self.assertEqual(COLUMN_WIDTH_DECREASE, preview.operations[0])
        self.assertAlmostEqual(0.5, preview.selected_current_width_share)
        self.assertAlmostEqual(0.8 / 1.8, preview.selected_target_width_share)
        self.assertEqual(PLACEMENT_APPLIED, result.status)
        root = self.tab.root
        self.assertIsInstance(root, Split)
        self.assertAlmostEqual(0.8 / 1.8, root.ratio, places=3)
        self.assertFalse(self.herdr.swaps)
        self.assertEqual(1, len(self.herdr.resizes))
        resize = self.herdr.resizes[0]
        self.assertEqual(
            self.columns[1][0],
            resize[resize.index("--pane") + 1],
        )
        self.assertEqual("left", resize[resize.index("--direction") + 1])

    def test_middle_unit_width_decrease_addresses_the_right_sibling_divider(self):
        herdr = PaneTreeHerdr("w1")
        tab = herdr.new_tab()
        key_c = UnitColumnKey("project-c", DEFAULT_LANE)
        columns = herdr.seed_columns(
            tab,
            [
                [_name("project-a", "codex"), _name("project-a", "claude")],
                [_name("project-b", "codex"), _name("project-b", "claude")],
                [_name("project-c", "codex"), _name("project-c", "claude")],
            ],
        )
        service = HerdrProjectColumnPlacement(
            home=Path("/unused"),
            target_workspace="w1",
            top_workspace_id="",
            binary="herdr",
            runner=herdr,
            timeout=1.0,
            authority=_Authority(),
            generation_resolver=lambda pane: f"generation:{pane.assigned_name}",
            plan_resolver=_plan(
                (self.key_a, self.key_b, key_c),
                ((self.key_a, 0.5), (self.key_b, 0.5)),
            ),
        )

        preview = service.preview_adjustment(self.key_b, COLUMN_WIDTH_DECREASE)
        result = service.apply_adjustment(preview)

        self.assertEqual(PREVIEW_READY, preview.status)
        self.assertEqual(PLACEMENT_APPLIED, result.status)
        self.assertEqual(2, len(herdr.resizes))
        first, second = herdr.resizes
        self.assertEqual(columns[0][0], first[first.index("--pane") + 1])
        self.assertEqual("right", first[first.index("--direction") + 1])
        self.assertEqual(columns[2][0], second[second.index("--pane") + 1])
        self.assertEqual("left", second[second.index("--direction") + 1])

    def test_partial_width_failure_does_not_claim_an_unattempted_safe_return(self):
        herdr = PaneTreeHerdr("w1")
        tab = herdr.new_tab()
        key_c = UnitColumnKey("project-c", DEFAULT_LANE)
        herdr.seed_columns(
            tab,
            [
                [_name("project-a", "codex"), _name("project-a", "claude")],
                [_name("project-b", "codex"), _name("project-b", "claude")],
                [_name("project-c", "codex"), _name("project-c", "claude")],
            ],
        )

        def refuse_second_resize(argv, **kwargs):
            tail = list(argv[1:])
            if tail[:2] == ["pane", "resize"] and herdr.resizes:
                herdr.resize_unchanged = True
            return herdr(argv, **kwargs)

        service = HerdrProjectColumnPlacement(
            home=Path("/unused"),
            target_workspace="w1",
            top_workspace_id="",
            binary="herdr",
            runner=refuse_second_resize,
            timeout=1.0,
            authority=_Authority(),
            generation_resolver=lambda pane: f"generation:{pane.assigned_name}",
            plan_resolver=_plan(
                (self.key_a, self.key_b, key_c),
                ((self.key_a, 0.5), (self.key_b, 0.5)),
            ),
        )

        result = service.apply_adjustment(
            service.preview_adjustment(self.key_b, COLUMN_WIDTH_DECREASE)
        )

        self.assertEqual(PLACEMENT_PARTIAL, result.status)
        self.assertNotIn("safe return was attempted", result.recovery)
        self.assertIn("may be partially changed", result.recovery)

    def test_move_at_edge_is_a_measured_zero_write(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )

        preview = service.preview_adjustment(self.key_a, COLUMN_MOVE_LEFT)
        result = service.apply_adjustment(preview)

        self.assertEqual(PREVIEW_MATCHED, preview.status)
        self.assertEqual(REASON_EDGE_REACHED, preview.reason)
        self.assertEqual(PLACEMENT_REFUSED, result.status)
        self.assertEqual(REASON_ADJUSTMENT_INVALID, result.reason)
        self.assertFalse(self.herdr.swaps)
        self.assertFalse(self.herdr.resizes)

    def test_live_adjustment_does_not_require_repo_placement_configuration(self):
        def unavailable_config(*_args, **_kwargs):
            raise RuntimeError("repo placement configuration is unavailable")

        service = self.service(unavailable_config)

        preview = service.preview_adjustment(self.key_a, COLUMN_MOVE_RIGHT)

        self.assertEqual(PREVIEW_READY, preview.status)
        self.assertEqual((self.key_b, self.key_a), preview.desired_order)
        self.assertFalse(self.herdr.swaps)
        self.assertFalse(self.herdr.resizes)

    def test_preview_refuses_if_live_order_changes_between_observations(self):
        key_c = UnitColumnKey("project-c", DEFAULT_LANE)
        self.herdr = PaneTreeHerdr("w1")
        self.tab = self.herdr.new_tab()
        self.columns = self.herdr.seed_columns(
            self.tab,
            [
                [_name("project-a", "codex"), _name("project-a", "claude")],
                [_name("project-b", "codex"), _name("project-b", "claude")],
                [_name("project-c", "codex"), _name("project-c", "claude")],
            ],
        )
        reads = 0

        def drift_before_second_observation(argv, **kwargs):
            nonlocal reads
            if list(argv[1:3]) == ["pane", "layout"]:
                reads += 1
                if reads == 2:
                    self.assertTrue(
                        self.tab.swap(self.columns[1][0], self.columns[2][0])
                    )
                    self.assertTrue(
                        self.tab.swap(self.columns[1][1], self.columns[2][1])
                    )
            return self.herdr(argv, **kwargs)

        service = HerdrProjectColumnPlacement(
            home=Path("/unused"),
            target_workspace="w1",
            top_workspace_id="",
            binary="herdr",
            runner=drift_before_second_observation,
            timeout=1.0,
            authority=_Authority(),
            generation_resolver=lambda pane: f"generation:{pane.assigned_name}",
            plan_resolver=_plan(
                (self.key_a, self.key_b, key_c),
                ((self.key_a, 1 / 3), (self.key_b, 0.5)),
            ),
        )

        preview = service.preview_adjustment(self.key_a, COLUMN_MOVE_RIGHT)

        self.assertEqual(PREVIEW_REFUSED, preview.status)
        self.assertEqual(REASON_STALE_PREVIEW, preview.reason)
        self.assertFalse(self.herdr.swaps)
        self.assertFalse(self.herdr.resizes)

    def test_unknown_unit_and_stale_adjustment_are_zero_write(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )
        unknown = service.preview_adjustment(
            UnitColumnKey("unknown", DEFAULT_LANE), COLUMN_MOVE_RIGHT
        )
        self.assertEqual(REASON_ADJUSTMENT_INVALID, unknown.reason)

        preview = service.preview_adjustment(self.key_a, COLUMN_MOVE_RIGHT)
        root = self.tab.root
        self.assertIsInstance(root, Split)
        root.ratio = 0.45
        result = service.apply_adjustment(preview)

        self.assertEqual(PLACEMENT_REFUSED, result.status)
        self.assertEqual(REASON_STALE_PREVIEW, result.reason)
        self.assertFalse(self.herdr.swaps)
        self.assertFalse(self.herdr.resizes)

    def test_adjustment_payload_never_contains_runtime_handles_or_generations(self):
        service = self.service(
            _plan((self.key_a, self.key_b), ((self.key_a, 0.5),))
        )

        preview = service.preview_adjustment(self.key_a, COLUMN_MOVE_RIGHT)
        rendered = json.dumps(preview.as_payload(), sort_keys=True)

        for pane_id in self.herdr.agents:
            self.assertNotIn(pane_id, rendered)
        self.assertNotIn("generation:", rendered)
        self.assertNotIn(self.tab.tab_id, rendered)


if __name__ == "__main__":
    unittest.main()
