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


ANCHOR = DecisionPointer(source="redmine", issue_id="13647", journal_id="85859")


def _resolve(specs, **over):
    """Resolve with defaults that make the plan *valid*, so each test varies one thing.

    The defaults mirror a real launch: the request starts exactly the providers the specs
    describe, and the plan names the one durable decision it was resolved from.
    """
    specs = list(specs)
    kwargs = dict(
        lane_class="sublane",
        slot_specs=specs,
        known_providers=PROVIDERS,
        known_roles=ROLES,
        request_providers=[s.provider for s in specs],
        anchors=[ANCHOR] if specs else [],
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
        # The pre-#13647 caller: no role-bearing plan, nothing required, nothing refused —
        # not even an anchor or a matching request.
        plan = _resolve([], request_providers=["claude", "codex"])
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

    def test_a_blank_position_refuses(self) -> None:
        # Review j#85859 F2: a blank position was an escape hatch out of the collision check
        # — two "unpinned" slots never collided — and an earlier revision of this suite
        # pinned that hole as correct. A plan that claims to describe the pair states where
        # each slot goes.
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve(
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
        self.assertIn("pins no physical slot", str(caught.exception))


class LaneLaunchPlanRequestReconciliationTest(unittest.TestCase):
    """The plan must account for EXACTLY the slots this launch starts (j#85859 F2)."""

    def test_a_plan_describing_fewer_slots_refuses(self) -> None:
        # The defect this closes: the peer slot would start with nothing having declared
        # what it is — the partial lane the whole-plan gate exists to prevent.
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], request_providers=["claude", "codex"])
        message = str(caught.exception)
        self.assertIn("describes 1 slot(s) but this launch starts 2", message)

    def test_a_plan_describing_more_slots_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError):
            _resolve(
                [
                    _spec(),
                    _spec(
                        workflow_role="coordinator",
                        profile_id="profile.coordinator",
                        provider="codex",
                        physical_slot="second",
                    ),
                ],
                request_providers=["claude"],
            )

    def test_a_plan_for_providers_this_launch_does_not_start_refuses(self) -> None:
        # Same cardinality, wrong providers: the plan is internally consistent and still
        # describes a different launch.
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec(provider="codex")], request_providers=["claude"])
        message = str(caught.exception)
        self.assertIn("does not describe this launch's providers", message)
        self.assertIn("unplanned: claude", message)
        self.assertIn("planned but not launched: codex", message)

    def test_launch_order_is_not_pinned(self) -> None:
        # Placement reorders providers AFTER this preflight, so a plan listed in the other
        # order is the same pair, not a defect.
        plan = _resolve(
            [
                _spec(),
                _spec(
                    workflow_role="coordinator",
                    profile_id="profile.coordinator",
                    provider="codex",
                    physical_slot="second",
                ),
            ],
            request_providers=["codex", "claude"],
        )
        self.assertEqual(len(plan.slots), 2)


class LaneLaunchPlanImmutabilityTest(unittest.TestCase):
    """A validated plan cannot change afterwards (j#85859 F3)."""

    def test_the_slot_copies_its_argv(self) -> None:
        # `frozen=True` only stops re-binding: a caller who passed a list kept a live handle
        # to the validated plan's command (measured: ['--model','x'] -> [] after clear()).
        argv = ["--model", "x"]
        plan = _resolve([_spec(launch_argv=argv)])
        argv.clear()
        self.assertEqual(plan.slots[0].launch_argv, ("--model", "x"))

    def test_a_string_argv_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _spec(launch_argv="--model x")
        self.assertIn("not an argv", str(caught.exception))

    def test_a_non_string_token_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError):
            _spec(launch_argv=["--model", 7])

    def test_the_slot_sequence_is_a_tuple(self) -> None:
        plan = _resolve([_spec()])
        self.assertIsInstance(plan.slots, tuple)
        self.assertIsInstance(plan.slots[0].launch_argv, tuple)

    def test_the_resolver_copies_the_placement_order(self) -> None:
        # Review j#85863: the order sequence IS the launch geometry (which provider occupies
        # the container), so a caller-owned list here means the validated geometry could
        # change between the preflight and the launch that acts on it.
        order = ["claude", "codex"]
        plan = _resolve([_spec()], placement=("right", order))
        order.clear()
        self.assertEqual(plan.placement, ("right", ("claude", "codex")))

    def test_the_public_constructor_owns_its_sequences(self) -> None:
        # The type is public, so it must own its data on EVERY construction path — not only
        # the one the resolver takes.
        slots = [_spec()]
        order = ["claude"]
        plan = ResolvedLaneLaunchPlan(
            lane_class="sublane", slots=slots, placement=("down", order)
        )
        slots.clear()
        order.clear()
        self.assertEqual(len(plan.slots), 1)
        self.assertIsInstance(plan.slots, tuple)
        self.assertEqual(plan.placement, ("down", ("claude",)))

    def test_the_constructor_refuses_malformed_sequences(self) -> None:
        for kwargs in (
            {"slots": ["not a slot"]},
            {"slots": "claude"},
            {"placement": ("right", ["claude", 7])},
            {"placement": ("right", "claude")},
            {"placement": (1, None)},
            {"placement": ("right",)},
        ):
            with self.assertRaises(LaneLaunchPlanError):
                ResolvedLaneLaunchPlan(lane_class="sublane", **kwargs)

    def test_an_absent_placement_stays_the_neutral_pair(self) -> None:
        plan = ResolvedLaneLaunchPlan(lane_class="sublane")
        self.assertEqual(plan.placement, (None, None))
        self.assertEqual(ResolvedLaneLaunchPlan(lane_class="sublane", placement=None).placement, (None, None))


class LaneLaunchPlanAnchorTest(unittest.TestCase):
    @staticmethod
    def _anchor(journal="85856", issue="13647") -> DecisionPointer:
        return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)

    def test_no_anchor_resolves_to_none_when_not_required(self) -> None:
        self.assertIsNone(resolve_source_anchor(()))

    def test_no_anchor_refuses_when_required(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            resolve_source_anchor((), required=True)
        self.assertIn("requires the durable governance record", str(caught.exception))

    def test_a_role_bearing_plan_requires_an_anchor(self) -> None:
        # Review j#85859 F1: a plan that assigns governed responsibilities without naming
        # the decision that assigned them cannot be told apart from a guessed one.
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], anchors=[])
        self.assertIn("requires the durable governance record", str(caught.exception))

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
