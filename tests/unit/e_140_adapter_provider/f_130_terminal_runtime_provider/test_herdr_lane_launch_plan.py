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
LANE_CLASSES = frozenset({"default", "sublane"})
SPLITS = frozenset({"right", "down"})


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
        known_lane_classes=LANE_CLASSES,
        known_splits=SPLITS,
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
        # The vocabulary refusal is re-typed at this boundary (j#85870) so the launch's
        # single `except` still turns it into a typed zero-start; the cause is asserted in
        # LaneLaunchPlanTypeBoundaryTest.
        with self.assertRaises(LaneLaunchPlanError):
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
        self.assertIn("not an ordered token sequence", str(caught.exception))

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


class LaneLaunchPlanTypeBoundaryTest(unittest.TestCase):
    """The value objects check their own fields on EVERY construction path (j#85870).

    A type annotation is documentation and ``frozen=True`` only stops re-binding, so neither
    stops a caller handing this boundary an `int` provider or a `None` role. These pin the
    STRUCTURAL half of the contract — the half that holds even for a plan built directly,
    without the resolver — because the type is public and a plan's consumer cannot tell which
    path built it.
    """

    BAD_SCALARS = (7, None, ["x"], object())

    def test_every_slot_scalar_field_must_be_a_string(self) -> None:
        for field in ("workflow_role", "profile_id", "provider", "physical_slot"):
            for bad in self.BAD_SCALARS:
                with self.assertRaises(LaneLaunchPlanError) as caught:
                    _spec(**{field: bad})
                self.assertIn(f"SlotLaunchSpec.{field} must be a string", str(caught.exception))

    def test_plan_scalars_must_be_strings(self) -> None:
        for bad in self.BAD_SCALARS:
            with self.assertRaises(LaneLaunchPlanError):
                ResolvedLaneLaunchPlan(lane_class=bad)
        for bad in (7, ["x"], object()):
            with self.assertRaises(LaneLaunchPlanError):
                ResolvedLaneLaunchPlan(lane_class="sublane", lane_kind=bad)

    def test_the_anchor_must_be_a_decision_pointer(self) -> None:
        # A provenance token that merely LOOKS like a record would read back as governance
        # the durable store never issued.
        for bad in ("redmine#13647", 7, {"issue": "13647"}):
            with self.assertRaises(LaneLaunchPlanError) as caught:
                ResolvedLaneLaunchPlan(lane_class="sublane", source_anchor=bad)
            self.assertIn("must be a DecisionPointer", str(caught.exception))

    def test_the_resolver_refuses_a_non_pointer_anchor(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], anchors=["not a pointer"])
        self.assertIn("must be a DecisionPointer", str(caught.exception))

    def test_the_exported_anchor_resolver_refuses_a_non_pointer(self) -> None:
        # `resolve_source_anchor` is exported and callable on its own, so it carries its own
        # contract — asserting only through `resolve_lane_launch_plan` would let the plan
        # constructor's guard mask the loss of this one (measured: a mutation removing this
        # check was invisible until this case existed).
        for bad in ("redmine#13647", 7, {"issue": "13647"}):
            with self.assertRaises(LaneLaunchPlanError) as caught:
                resolve_source_anchor([bad])
            self.assertIn("must be a DecisionPointer", str(caught.exception))

    def test_the_launch_provider_list_is_type_checked(self) -> None:
        for bad in (None, "claude", [7], object()):
            with self.assertRaises(LaneLaunchPlanError):
                _resolve([_spec()], request_providers=bad)

    def test_a_vocabulary_refusal_surfaces_as_this_module_error(self) -> None:
        # The launch catches THIS module's error to build its typed zero-start, so a lane-kind
        # vocabulary refusal that escaped as LaneKindError would leave the caller with an
        # untyped failure. The cause chain keeps the original error visible.
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], lane_kind="grandchild")
        self.assertIsInstance(caught.exception.__cause__, LaneKindError)

    def test_a_direct_plan_is_type_valid_but_not_launch_validated(self) -> None:
        # The documented split: a directly built plan passes structural validation and is
        # deliberately NOT claimed to be reconciled with any launch (no anchor requirement,
        # no vocabulary check, no request comparison happens here).
        plan = ResolvedLaneLaunchPlan(
            lane_class="sublane", slots=[_spec(workflow_role="not-a-known-role")]
        )
        self.assertEqual(plan.workflow_roles, ("not-a-known-role",))
        self.assertIsNone(plan.source_anchor)
        # ...while the resolver — the launch-validating entry — refuses the same slot.
        with self.assertRaises(LaneLaunchPlanError):
            _resolve([_spec(workflow_role="not-a-known-role")])


class LaneLaunchPlanInjectedVocabularyTest(unittest.TestCase):
    """The injected vocabularies are inputs too, and are checked like one (j#85875 F1)."""

    VOCABULARY_FIELDS = (
        "known_roles",
        "known_providers",
        "known_lane_classes",
        "known_splits",
    )

    def test_a_bare_string_vocabulary_refuses(self) -> None:
        # The dangerous case: `role in "ximplementerx"` is a SUBSTRING test, so the
        # fail-closed vocabulary check silently stops being one.
        for field in self.VOCABULARY_FIELDS:
            with self.assertRaises(LaneLaunchPlanError) as caught:
                _resolve([_spec()], **{field: "ximplementerxclaudexsublanexrightx"})
            self.assertIn("substring test", str(caught.exception))

    def test_a_non_iterable_or_mistyped_vocabulary_refuses(self) -> None:
        for field in self.VOCABULARY_FIELDS:
            for bad in (None, [7], {7: "x"}):
                with self.assertRaises(LaneLaunchPlanError):
                    _resolve([_spec()], **{field: bad})


class LaneLaunchPlanOrderedSequenceTest(unittest.TestCase):
    """Order-bearing fields accept ordered sequences only (j#85875 F3).

    A set iterates in an order that is not part of the value, so the same plan would fix a
    different argv / launch order in a different process — the opposite of "fixed before the
    first write".
    """

    def test_argv_refuses_unordered_containers(self) -> None:
        for bad in ({"--model", "x"}, {"--model": "x"}, frozenset({"a"})):
            with self.assertRaises(LaneLaunchPlanError) as caught:
                _spec(launch_argv=bad)
            self.assertIn("ordered sequence", str(caught.exception))

    def test_plan_slots_refuse_unordered_containers(self) -> None:
        with self.assertRaises(LaneLaunchPlanError):
            ResolvedLaneLaunchPlan(lane_class="sublane", slots={_spec()})

    def test_placement_order_refuses_unordered_containers(self) -> None:
        with self.assertRaises(LaneLaunchPlanError):
            ResolvedLaneLaunchPlan(
                lane_class="sublane", placement=("right", {"claude", "codex"})
            )

    def test_request_providers_refuse_unordered_containers(self) -> None:
        with self.assertRaises(LaneLaunchPlanError):
            _resolve([_spec()], request_providers={"claude"})

    def test_ordered_sequences_are_accepted_and_owned(self) -> None:
        plan = _resolve([_spec(launch_argv=["--model", "x"])], placement=("right", ["claude"]))
        self.assertEqual(plan.slots[0].launch_argv, ("--model", "x"))
        self.assertEqual(plan.placement, ("right", ("claude",)))


class LaneLaunchPlanClosedGeometryTest(unittest.TestCase):
    """A "resolved" plan carries geometry the system actually recognises (j#85875 F4)."""

    def test_an_unknown_lane_class_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], lane_class="foreign")
        self.assertIn("unknown lane class", str(caught.exception))

    def test_an_unknown_split_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], placement=("diagonal", None))
        self.assertIn("unknown placement split", str(caught.exception))

    def test_an_order_naming_an_unregistered_provider_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], placement=("right", ["foreign"]))
        self.assertIn("unregistered provider", str(caught.exception))

    def test_an_order_repeating_a_provider_refuses(self) -> None:
        with self.assertRaises(LaneLaunchPlanError) as caught:
            _resolve([_spec()], placement=("right", ["claude", "claude"]))
        self.assertIn("twice", str(caught.exception))

    def test_a_resolved_geometry_is_carried(self) -> None:
        plan = _resolve([_spec()], placement=("down", ["claude", "codex"]))
        self.assertEqual(plan.placement, ("down", ("claude", "codex")))

    def test_the_launch_order_is_still_not_compared_with_the_request(self) -> None:
        # j#85859 F2's boundary is unchanged: placement reorders AFTER the preflight, so the
        # geometry's order is validated as a VALUE, never against the request's order.
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
            placement=("right", ["codex", "claude"]),
        )
        self.assertEqual(plan.providers, ("claude", "codex"))


class LaunchPreflightVocabularyWiringTest(unittest.TestCase):
    """The application injects the CANONICAL vocabularies, not a convenient superset.

    The plan leaf takes its vocabularies as data, which is what keeps it pure — but that
    also means its fail-closed behaviour is only as good as what the composition root hands
    it. A mutation widening the injected sets was invisible to every other test (measured),
    so this pins the wiring itself: the preflight passes the same frozensets the config
    context defines (Redmine #13646 §5.1) and the launch's own provider / role registries.
    """

    def test_the_preflight_passes_the_canonical_sets(self) -> None:
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501
            WORKFLOW_ROLES,
        )
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
            LANE_PLACEMENT_LANE_CLASSES,
            LANE_PLACEMENT_SPLIT_DIRECTIONS,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_preflight import (  # noqa: E501
            validate_session_request,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain import (  # noqa: E501
            herdr_lane_launch_plan as plan_module,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501
            LaneLaunchContext,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
            AGENT_PROVIDERS,
        )

        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return plan_module.ResolvedLaneLaunchPlan(lane_class=kwargs["lane_class"])

        context = LaneLaunchContext(
            anchors=[ANCHOR],
            slot_specs=[
                _spec(workflow_role="implementer", provider="claude"),
            ],
        )
        with patch.object(plan_module, "resolve_lane_launch_plan", _capture):
            validate_session_request(
                providers=["claude"],
                lane_id="lane-x",
                coordinator_placement_mode="per_project_space",
                claude_permission_mode_default="auto",
                env={},
                error_type=RuntimeError,
                launch_context=context,
            )
        self.assertEqual(captured["known_roles"], WORKFLOW_ROLES)
        self.assertEqual(captured["known_providers"], AGENT_PROVIDERS)
        self.assertEqual(captured["known_lane_classes"], LANE_PLACEMENT_LANE_CLASSES)
        self.assertEqual(captured["known_splits"], LANE_PLACEMENT_SPLIT_DIRECTIONS)
        self.assertEqual(tuple(captured["request_providers"]), ("claude",))


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
