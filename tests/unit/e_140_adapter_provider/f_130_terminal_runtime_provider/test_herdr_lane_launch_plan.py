"""The whole-plan launch preflight (Redmine #13647 Tranche 2, Design Answer j#85645).

Unit (`tests-placement-discovery-policy.md` 配置決定木 4): the subject is the pure plan
resolver in isolation — its vocabularies (known providers / known roles) are injected data,
so nothing here reads a registry, a config, a store or a herdr.

The contract under test is "the pair is the unit of validation": a defect that only becomes
visible across slots (two slots claiming one workflow role, two entries for one physical
slot, one slot asked for two profiles) must be refused while the plan is still data, because
the alternative — discovering it while launching the second slot — leaves the first one live
as a partial lane.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_kind import LaneKindError  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer  # noqa: E402
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_plan import (  # noqa: E501,E402
    LaneLaunchPlanError,
    ResolvedLaneLaunchPlan,
    SlotLaunchSpec,
    resolve_lane_launch_plan,
    resolve_source_anchor,
)

PROVIDERS = frozenset({"claude", "codex"})
ROLES = frozenset({"coordinator", "implementer", "implementation_worker", "auditor"})


def _spec(**over) -> SlotLaunchSpec:
    base = dict(
        workflow_role="implementer",
        profile_id="profile.implementer",
        provider="claude",
        launch_argv=("--model", "x"),
        physical_slot="first",
    )
    base.update(over)
    return SlotLaunchSpec(**base)


def _resolve(specs, **over):
    kwargs = dict(
        lane_class="sublane",
        slot_specs=specs,
        known_providers=PROVIDERS,
        known_roles=ROLES,
    )
    kwargs.update(over)
    return resolve_lane_launch_plan(**kwargs)


class LaneLaunchPlanHappyPathTest(unittest.TestCase):
    def test_a_two_slot_pair_resolves(self) -> None:
        plan = _resolve(
            [
                _spec(),
                _spec(
                    workflow_role="coordinator",
                    profile_id="profile.coordinator",
                    provider="codex",
                    physical_slot="second",
                ),
            ]
        )
        self.assertIsInstance(plan, ResolvedLaneLaunchPlan)
        self.assertEqual(plan.workflow_roles, ("implementer", "coordinator"))
        self.assertEqual(plan.providers, ("claude", "codex"))
        self.assertEqual(plan.lane_class, "sublane")

    def test_no_slot_specs_is_an_empty_plan(self) -> None:
        # The pre-#13647 caller: no role-bearing plan, nothing required, nothing refused.
        plan = _resolve([])
        self.assertEqual(plan.slots, ())
        self.assertIsNone(plan.source_anchor)
        self.assertIsNone(plan.lane_kind)

    def test_the_geometry_axis_is_carried_and_validated(self) -> None:
        plan = _resolve([_spec()], lane_kind="implementation")
        self.assertEqual(plan.lane_kind, "implementation")
        with self.assertRaises(LaneKindError):
            _resolve([_spec()], lane_kind="grandchild")

    def test_same_provider_may_carry_distinct_profiles_in_distinct_slots(self) -> None:
        # Only a SAME-slot conflict is a contradiction; one provider legitimately runs two
        # differently-profiled slots of a pair.
        plan = _resolve(
            [
                _spec(),
                _spec(
                    workflow_role="auditor",
                    profile_id="profile.auditor",
                    physical_slot="second",
                ),
            ]
        )
        self.assertEqual(plan.providers, ("claude", "claude"))
        self.assertEqual(
            [s.profile_id for s in plan.slots],
            ["profile.implementer", "profile.auditor"],
        )


class LaneLaunchPlanRefusalTest(unittest.TestCase):
    def test_unresolved_slot_fields_refuse(self) -> None:
        for field, blank in (
            ("workflow_role", ""),
            ("profile_id", ""),
            ("provider", ""),
            ("launch_argv", ()),
        ):
            with self.assertRaises(LaneLaunchPlanError) as caught:
                _resolve([_spec(**{field: blank})])
            self.assertIn("slot 0", str(caught.exception))

    def test_unknown_workflow_role_refuses_instead_of_defaulting(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec(workflow_role="coordinator_assistant")])
        message = str(caught.exception)
        self.assertIn("unknown workflow role", message)
        self.assertIn("coordinator_assistant", message)

    def test_unregistered_provider_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec(provider="gemini")])
        self.assertIn("unregistered provider", str(caught.exception))

    def test_duplicate_workflow_role_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec(), _spec(provider="codex", physical_slot="second")])
        self.assertIn("claimed by two slots", str(caught.exception))

    def test_two_providers_claiming_one_physical_slot_refuse(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve(
                [
                    _spec(),
                    _spec(
                        workflow_role="coordinator",
                        profile_id="profile.coordinator",
                        provider="codex",
                    ),
                ]
            )
        self.assertIn("claimed by two providers", str(caught.exception))

    def test_one_slot_asked_for_two_profiles_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve(
                [
                    _spec(),
                    _spec(workflow_role="auditor", profile_id="profile.other"),
                ]
            )
        self.assertIn("different profile / argv", str(caught.exception))

    def test_one_slot_planned_twice_identically_still_refuses(self) -> None:
        # Two entries for one pair position: whichever launched second would win silently,
        # so even an identical duplicate is a plan defect rather than a no-op.
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec(), _spec(workflow_role="auditor")])
        self.assertIn("planned twice", str(caught.exception))

    def test_unpinned_positions_do_not_collide(self) -> None:
        # A caller that pins no physical slot is not asserting a position, so two unpinned
        # slots are not a position conflict (their roles still must differ).
        plan = _resolve(
            [
                _spec(physical_slot=""),
                _spec(
                    workflow_role="auditor",
                    profile_id="profile.auditor",
                    provider="codex",
                    physical_slot="",
                ),
            ]
        )
        self.assertEqual(len(plan.slots), 2)


class LaneLaunchPlanAnchorTest(unittest.TestCase):
    @staticmethod
    def _anchor(journal="85856", issue="13647") -> DecisionPointer:
        return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)

    def test_no_anchor_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_source_anchor(()))

    def test_one_anchor_resolves(self) -> None:
        anchor = self._anchor()
        self.assertEqual(resolve_source_anchor([anchor]), anchor)

    def test_repeated_identical_anchors_resolve(self) -> None:
        # The same durable record named twice is one governance fact, not a contradiction.
        self.assertEqual(
            resolve_source_anchor([self._anchor(), self._anchor()]), self._anchor()
        )

    def test_two_different_anchors_are_ambiguous_and_refuse(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            resolve_source_anchor([self._anchor(), self._anchor(journal="85857")])
        message = str(caught.exception)
        self.assertIn("ambiguous", message)
        self.assertIn("85857", message)

    def test_the_plan_carries_the_resolved_anchor(self) -> None:
        plan = _resolve([_spec()], anchors=[self._anchor()])
        self.assertEqual(plan.source_anchor, self._anchor())

    def test_the_plan_refuses_an_ambiguous_anchor_before_validating_slots(self) -> None:
        # Ambiguity about WHICH decision authorizes the launch is fatal even when every slot
        # is otherwise fine — the plan is never built from a guess.
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve(
                [_spec()], anchors=[self._anchor(), self._anchor(issue="13646")]
            )
        self.assertIn("ambiguous", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
