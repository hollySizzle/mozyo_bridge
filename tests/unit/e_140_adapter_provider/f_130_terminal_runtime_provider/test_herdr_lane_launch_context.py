"""`LaneLaunchContext` as a validated authority value (Redmine #13647, review j#85875 F2).

Unit (`tests-placement-discovery-policy.md` 配置決定木 4): the subject is one pure value in
isolation — no store, no launch, no herdr.

The context is the carrier the create / heal boundary hands to the launch: its `lane_kind`
is the geometry authority, its `anchors` are the governance provenance, its `slot_specs` are
the per-slot plan. Its module docstring has claimed "validated on construction" since Tranche
1, but until this review only `lane_kind` was actually checked — a list of look-alike strings
became the context's anchors, and a non-`SlotLaunchSpec` entry travelled as far as the
resolver.

These cases live HERE, separate from the plan resolver's own guards, deliberately: the two
boundaries validate the same shapes, so testing only through the resolver would let one
guard's removal hide behind the other (measured in the previous round, j#85874).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_kind import LaneKindError  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer  # noqa: E402
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501,E402
    LaneLaunchContext,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_plan import (  # noqa: E501,E402
    LaneLaunchPlanError,
    SlotLaunchSpec,
)

ANCHOR = DecisionPointer(source="redmine", issue_id="13647", journal_id="85875")
SPEC = SlotLaunchSpec(
    workflow_role="implementer",
    profile_id="profile.implementer",
    provider="claude",
    launch_argv=("--model", "x"),
    physical_slot="first",
)


class LaneLaunchContextGeometryTest(unittest.TestCase):
    def test_the_default_context_carries_nothing(self) -> None:
        context = LaneLaunchContext()
        self.assertIsNone(context.lane_kind)
        self.assertEqual(context.anchors, ())
        self.assertEqual(context.slot_specs, ())
        self.assertFalse(context.has_lane_kind)
        self.assertFalse(context.has_slot_plan)

    def test_a_canonical_kind_is_kept_and_a_foreign_one_refuses(self) -> None:
        self.assertEqual(
            LaneLaunchContext(lane_kind="implementation").lane_kind, "implementation"
        )
        with self.assertRaises(LaneKindError):
            LaneLaunchContext(lane_kind="grandchild")


class LaneLaunchContextAuthorityFieldsTest(unittest.TestCase):
    """The carrier verifies what it carries (j#85875 F2)."""

    def test_anchors_must_be_decision_pointers(self) -> None:
        # A look-alike string here would read back as governance the durable store never
        # issued — the same defect the plan refuses, at the boundary that comes first.
        for bad in (["redmine#13647"], [7], [None], [{"issue": "13647"}]):
            with self.assertRaises(LaneLaunchPlanError) as caught:
                LaneLaunchContext(anchors=bad)
            self.assertIn("LaneLaunchContext.anchors", str(caught.exception))

    def test_slot_specs_must_be_slot_launch_specs(self) -> None:
        for bad in (["not-a-slot"], [7], [{"provider": "claude"}]):
            with self.assertRaises(LaneLaunchPlanError) as caught:
                LaneLaunchContext(slot_specs=bad)
            self.assertIn("LaneLaunchContext.slot_specs", str(caught.exception))

    def test_non_sequence_containers_refuse(self) -> None:
        # Including the shapes that used to raise a bare TypeError instead of this module's
        # single typed error.
        for kwargs in (
            {"anchors": 7},
            {"slot_specs": 7},
            {"anchors": "redmine"},
            {"slot_specs": {SPEC}},
            {"anchors": {ANCHOR}},
            {"anchors": {ANCHOR: 1}},
            {"slot_specs": {SPEC: 1}},
        ):
            with self.assertRaises(LaneLaunchPlanError):
                LaneLaunchContext(**kwargs)

    def test_the_context_owns_its_sequences(self) -> None:
        anchors = [ANCHOR]
        specs = [SPEC]
        context = LaneLaunchContext(anchors=anchors, slot_specs=specs)
        anchors.clear()
        specs.clear()
        self.assertEqual(context.anchors, (ANCHOR,))
        self.assertEqual(context.slot_specs, (SPEC,))
        self.assertIsInstance(context.anchors, tuple)
        self.assertIsInstance(context.slot_specs, tuple)

    def test_a_valid_context_reports_its_plan(self) -> None:
        context = LaneLaunchContext(
            lane_kind="implementation", anchors=[ANCHOR], slot_specs=[SPEC]
        )
        self.assertTrue(context.has_lane_kind)
        self.assertTrue(context.has_slot_plan)


class LaneLaunchContextSingleEvaluationTest(unittest.TestCase):
    """The carrier stores what it checked, too (review j#85885, same shape)."""

    def test_a_shifting_slot_sequence_stores_the_validated_value(self) -> None:
        foreign = SlotLaunchSpec(
            workflow_role="foreign",
            profile_id="foreign",
            provider="foreign",
            launch_argv=("x",),
            physical_slot="first",
        )

        class Shifting(list):
            reads = 0

            def __iter__(self):
                type(self).reads += 1
                return iter([SPEC] if type(self).reads == 1 else [foreign])

        context = LaneLaunchContext(slot_specs=Shifting([SPEC]))
        self.assertEqual(context.slot_specs, (SPEC,))
        self.assertEqual(Shifting.reads, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
