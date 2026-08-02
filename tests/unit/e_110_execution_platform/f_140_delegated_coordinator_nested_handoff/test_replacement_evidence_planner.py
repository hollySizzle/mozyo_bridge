"""Evidence-aware participant planner contract (Redmine #14741 j#97047 decision 1).

Unit-placed rather than in the #14741 regression family: this pins a new service CONTRACT
rather than re-pinning a reproduced defect (`tests-placement-discovery-policy.md`).

Every port is injected, so the planner is exercised without a store, a lane or a launch.
The legacy case asserts the receipt port is never CALLED — the cheapest way to state "a
pre-#14741 replacement is unchanged, including its cost".
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    ParticipantPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner import (  # noqa: E402,E501
    PLAN_EVIDENCE_PINNED,
    PLAN_LEGACY_UNCHANGED,
    EvidencePlanRefused,
    PlanningContext,
    ReplacementEvidencePlanner,
)

WORKSPACE = "wA"
CONTEXT = PlanningContext(
    workspace_id=WORKSPACE, lane_id="issue_14741", expected_update_cause="update_relaunch"
)
#: The typed LAUNCH cause a replacement pins — NOT the observed screen id.
CAUSE = "update_relaunch"

ACTION = "startup-ir1-" + "a" * 64
LEGACY_ACTION = "startup-" + "b" * 64
GEN = "lane-gen-1"
REV = "7"


def _pin(**kw):
    base = dict(
        lane_id="issue_14741",
        role="gateway",
        provider="codex",
        assigned_name="mzb1_wA_codex_lane",
        old_locator="wA:p1",
        lane_generation=GEN,
        lane_revision=REV,
    )
    base.update(kw)
    return ParticipantPin(**base)


def _generation(action_id=ACTION, phase="attested", role="gateway", lane="issue_14741",
                workspace="wA", assigned="mzb1_wA_codex_lane", locator="wA:p1"):
    return SimpleNamespace(
        startup_action_id=action_id, phase=phase, role=role, lane_id=lane,
        workspace_id=workspace, assigned_name=assigned, locator=locator,
    )


def _evidence(action_id=ACTION, blocker="update_prompt_available", bound=True, **kw):
    key = SimpleNamespace(
        workspace_id=kw.get("workspace_id", "wA"),
        lane_id=kw.get("lane_id", "issue_14741"),
        provider=kw.get("provider", "codex"),
        assigned_name=kw.get("assigned_name", "mzb1_wA_codex_lane"),
        startup_action_id=action_id,
    )
    return SimpleNamespace(key=key, blocker_id=blocker, bound=bound)


def _update_cause(provider, blocker_id):
    """The port e_140 supplies: observed screen -> typed launch cause."""
    if provider == "codex" and blocker_id in (
        "update_prompt_available", "update_in_progress"
    ):
        return CAUSE
    return ""


_DEFAULT = object()


class _Ports:
    """Injected doubles that record whether they were consulted at all."""

    def __init__(self, *, generation=_DEFAULT, lifecycle=(GEN, REV), evidence=None,
                 generation_error=None, evidence_error=None, lifecycle_error=None):
        # A sentinel, not `None`: "no generation recorded" is a case this fixture has to be
        # able to express, and `None`-means-default would have silently swallowed it.
        self._generation = _generation() if generation is _DEFAULT else generation
        self._lifecycle = lifecycle
        self._evidence = evidence
        self._generation_error = generation_error
        self._evidence_error = evidence_error
        self._lifecycle_error = lifecycle_error
        self.evidence_calls = 0
        self.lifecycle_calls = 0

    def generations(self, assigned_name):
        if self._generation_error:
            raise self._generation_error
        return self._generation

    def lifecycle(self, lane_id):
        self.lifecycle_calls += 1
        if self._lifecycle_error:
            raise self._lifecycle_error
        return self._lifecycle

    def evidence(self, **kw):
        self.evidence_calls += 1
        if self._evidence_error:
            raise self._evidence_error
        return self._evidence

    def planner(self, capability=None, update_cause=None):
        return ReplacementEvidencePlanner(
            generations=self.generations,
            lifecycle=self.lifecycle,
            evidence=self.evidence,
            update_cause=update_cause or _update_cause,
            capability=capability if capability is not None else _is_capable,
        )


def _is_capable(action_id):
    return str(action_id).startswith("startup-ir1-")


class LegacyPositiveControlTest(unittest.TestCase):
    def test_a_legacy_generation_is_byte_exact_and_opens_no_receipt_store(self) -> None:
        ports = _Ports(generation=_generation(action_id=LEGACY_ACTION))
        pin = _pin()
        plan = ports.planner().plan([pin], CONTEXT)
        self.assertEqual(plan.outcome, PLAN_LEGACY_UNCHANGED)
        self.assertEqual(plan.participants, (pin,))
        self.assertIs(plan.participants[0], pin, "the same object, not a rebuilt copy")
        self.assertEqual(ports.evidence_calls, 0, "the receipt store is never opened")
        self.assertEqual(ports.lifecycle_calls, 0)

    def test_an_empty_plan_is_legacy_and_touches_nothing(self) -> None:
        ports = _Ports()
        plan = ports.planner().plan([], CONTEXT)
        self.assertEqual(plan.participants, ())
        self.assertEqual(plan.outcome, PLAN_LEGACY_UNCHANGED)
        self.assertEqual(ports.evidence_calls, 0)


class ReceiptCapablePlanningTest(unittest.TestCase):
    def test_a_fully_agreeing_generation_is_planned_with_its_triplet(self) -> None:
        ports = _Ports(evidence=_evidence())
        planned = ports.planner().plan([_pin()], CONTEXT)
        self.assertEqual(planned.outcome, PLAN_EVIDENCE_PINNED)
        pin = planned.participants[0]
        self.assertEqual(pin.evidence_workspace_id, "wA")
        self.assertEqual(pin.evidence_startup_action_id, ACTION)
        self.assertEqual(pin.evidence_cause, CAUSE, "the typed launch cause, not the screen id")

    def test_every_existing_authority_on_the_input_pin_is_carried_across(self) -> None:
        ports = _Ports(evidence=_evidence())
        original = _pin(
            is_self=True, lane_revision=REV, lane_generation=GEN, phase="launch_owed"
        )
        pin = ports.planner().plan([original], CONTEXT).participants[0]
        for attr in (
            "lane_id", "role", "provider", "assigned_name", "old_locator",
            "is_self", "lane_revision", "lane_generation", "phase",
        ):
            self.assertEqual(
                getattr(pin, attr), getattr(original, attr), f"{attr} must not drift"
            )

    def test_planning_is_deterministic_and_idempotent(self) -> None:
        ports = _Ports(evidence=_evidence())
        once = ports.planner().plan([_pin()], CONTEXT).participants[0]
        twice = ports.planner().plan([once], CONTEXT).participants[0]
        self.assertEqual(once, twice, "re-planning a correct pin reproduces it exactly")


class RefusalTest(unittest.TestCase):
    """Every refusal is a whole-plan refusal: zero plan, zero launch, zero store write."""

    def _refuses(self, reason, ports, pin=None, capability=None):
        with self.assertRaises(EvidencePlanRefused) as ctx:
            ports.planner(capability=capability).plan([pin or _pin()], CONTEXT)
        self.assertEqual(ctx.exception.reason, reason)

    def test_an_unclassifiable_action_is_never_treated_as_legacy(self) -> None:
        def boom(action_id):
            raise ValueError("unclassifiable")

        self._refuses("unknown_action_shape", _Ports(), capability=boom)

    def test_an_absent_or_unreadable_generation_refuses(self) -> None:
        self._refuses("generation_unavailable", _Ports(generation=None))
        self._refuses(
            "generation_unavailable", _Ports(generation_error=OSError("unreadable"))
        )

    def test_a_participant_with_no_assigned_name_refuses(self) -> None:
        """There is no slot to look a generation up for."""
        planner = _Ports().planner()
        with self.assertRaises(EvidencePlanRefused) as ctx:
            # The lane must match the context, so the refusal under test is reached rather
            # than the (now earlier) lane-scope gate.
            planner.plan(
                [SimpleNamespace(assigned_name="  ", lane_id=CONTEXT.lane_id, role="r")],
                CONTEXT,
            )
        self.assertEqual(ctx.exception.reason, "generation_unavailable")

    def test_a_pending_generation_refuses(self) -> None:
        """A launch that never proved it came up cannot support a replacement's evidence."""
        self._refuses(
            "generation_not_attested", _Ports(generation=_generation(phase="pending"))
        )

    def test_a_generation_for_a_different_participant_refuses(self) -> None:
        self._refuses(
            "generation_mismatch", _Ports(generation=_generation(role="worker"))
        )
        self._refuses(
            "generation_mismatch", _Ports(generation=_generation(lane="issue_other"))
        )

    def test_a_missing_or_partial_lifecycle_refuses(self) -> None:
        self._refuses("lifecycle_unavailable", _Ports(lifecycle=None))
        self._refuses("lifecycle_unavailable", _Ports(lifecycle=("", REV)))
        self._refuses("lifecycle_unavailable", _Ports(lifecycle=(GEN, "")))
        self._refuses(
            "lifecycle_unavailable", _Ports(lifecycle_error=OSError("component missing"))
        )

    def test_a_lane_that_moved_since_the_pin_refuses(self) -> None:
        ports = _Ports(evidence=_evidence())
        self._refuses(
            "lifecycle_mismatch", ports, pin=_pin(lane_generation="lane-gen-OLD")
        )
        self._refuses("lifecycle_mismatch", ports, pin=_pin(lane_revision="6"))

    def test_a_receipt_capable_pin_without_a_lifecycle_pin_refuses(self) -> None:
        """Audit j#97062 finding 2: an empty pin is not 'consistent with anything'."""
        ports = _Ports(evidence=_evidence())
        self._refuses("lifecycle_mismatch", ports, pin=_pin(lane_generation=""))
        self._refuses("lifecycle_mismatch", ports, pin=_pin(lane_revision=""))

    def test_a_foreign_generation_refuses(self) -> None:
        """Audit j#97062 finding 1: workspace, assigned name and locator are compared."""
        for label, generation in (
            ("other workspace", _generation(workspace="wOTHER")),
            ("other assigned name", _generation(assigned="mzb1_wA_codex_two")),
            ("recycled locator", _generation(locator="wA:pOTHER")),
        ):
            with self.subTest(label=label):
                self._refuses(
                    "generation_mismatch", _Ports(generation=generation, evidence=_evidence())
                )

    def test_consumed_or_unbound_evidence_refuses(self) -> None:
        """Audit j#97062 finding 3: consumed evidence must never re-arm a plan."""
        self._refuses("evidence_not_bound", _Ports(evidence=_evidence(bound=False)))

    def test_a_screen_that_is_not_update_derived_refuses(self) -> None:
        """Audit j#97062 finding 4: a trust prompt is a blocker, not a launch cause."""
        self._refuses(
            "cause_not_update_derived", _Ports(evidence=_evidence(blocker="trust_dialog"))
        )

    def test_absent_or_unreadable_evidence_refuses(self) -> None:
        self._refuses("evidence_unavailable", _Ports(evidence=None))
        self._refuses(
            "evidence_unavailable", _Ports(evidence_error=OSError("store corrupt"))
        )

    def test_evidence_for_another_generation_or_slot_refuses(self) -> None:
        other_action = "startup-ir1-" + "9" * 64
        self._refuses(
            "evidence_mismatch", _Ports(evidence=_evidence(action_id=other_action))
        )
        self._refuses(
            "evidence_mismatch",
            _Ports(evidence=_evidence(assigned_name="mzb1_wA_codex_two")),
        )
        self._refuses("evidence_mismatch", _Ports(evidence=_evidence(provider="claude")))
        self._refuses("evidence_mismatch", _Ports(evidence=_evidence(blocker="")))

    def test_a_divergent_pre_existing_triplet_refuses(self) -> None:
        ports = _Ports(evidence=_evidence())
        self._refuses(
            "divergent_pre_pin",
            ports,
            pin=_pin(
                evidence_workspace_id="wOTHER",
                evidence_startup_action_id=ACTION,
                evidence_cause="update_prompt_available",
            ),
        )
        self._refuses(
            "divergent_pre_pin",
            ports,
            pin=_pin(
                evidence_workspace_id="wA",
                evidence_startup_action_id=ACTION,
                evidence_cause="some_other_cause",
            ),
        )

    def test_a_legacy_action_carrying_evidence_refuses(self) -> None:
        """A legacy generation with a triplet is not a legacy generation."""
        ports = _Ports(generation=_generation(action_id=LEGACY_ACTION))
        self._refuses(
            "divergent_pre_pin",
            ports,
            pin=_pin(
                evidence_workspace_id="wA",
                evidence_startup_action_id=ACTION,
                evidence_cause=CAUSE,
            ),
        )


def _foreign(**kw):
    """A participant that is NOT a :class:`ParticipantPin`.

    Needed because ``ParticipantPin.__post_init__`` strips its own fields, so a padded
    value cannot reach the planner through the domain type at all. The planner takes
    ``Sequence[Any]``, so the exactness it owes is the exactness it applies to whatever it
    is handed -- not one inherited from a constructor it does not control.
    """
    base = dict(
        lane_id="issue_14741",
        role="gateway",
        provider="codex",
        assigned_name="mzb1_wA_codex_lane",
        old_locator="wA:p1",
        lane_generation=GEN,
        lane_revision=REV,
        evidence_workspace_id="",
        evidence_startup_action_id="",
        evidence_cause="",
        is_self=False,
        phase="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _RenderedToken:
    """An authority value that is only a token once something renders it."""

    def __str__(self) -> str:  # pragma: no cover - exercised via the planner
        return "issue_14741"


class _Hostile:
    """An authority value that answers every question with a host path.

    Not a contrived shape: it stands for any value the planner did not construct — a row
    from a store, a decoded payload, an adapter's object — whose dunders are not the
    planner's to trust.
    """

    _BOOM = "/private/host/path"

    def __eq__(self, other):  # pragma: no cover - raising IS the behaviour under test
        raise OSError(self._BOOM)

    def __ne__(self, other):  # pragma: no cover
        raise OSError(self._BOOM)

    def __str__(self):  # pragma: no cover
        raise OSError(self._BOOM)

    def __bool__(self):  # pragma: no cover
        raise OSError(self._BOOM)

    def __len__(self):  # pragma: no cover
        raise OSError(self._BOOM)


class _HostileText(str):
    """The same, wearing ``str``.

    This is why the planner tests ``type(x) is str`` and not ``isinstance``: a subclass
    passes every ``isinstance`` check and then decides for itself what ``==`` means.
    """

    def __new__(cls):
        return super().__new__(cls, "x")

    def __eq__(self, other):  # pragma: no cover
        raise OSError(_Hostile._BOOM)

    def __ne__(self, other):  # pragma: no cover
        raise OSError(_Hostile._BOOM)

    def __str__(self):  # pragma: no cover
        raise OSError(_Hostile._BOOM)

    __hash__ = str.__hash__


class CurrentRowIsTheOnlyBindingTest(unittest.TestCase):
    """Ruling j#97105: capability is read from the participant's OWN current row.

    A legacy pass-through decided from a row that belongs to someone else would be reading
    a stranger's action id as proof that THIS participant predates identity receipts -- the
    capability laundering j#96892 closed, arriving through the cheaper door.
    """

    def _refuses(self, reason, **ports):
        with self.assertRaises(EvidencePlanRefused) as ctx:
            _Ports(**ports).planner().plan([_pin()], CONTEXT)
        self.assertEqual(ctx.exception.reason, reason)

    def test_a_legacy_action_on_a_foreign_row_does_not_pass_through(self) -> None:
        for label, generation in (
            ("another workspace", _generation(action_id=LEGACY_ACTION, workspace="wB")),
            ("another lane", _generation(action_id=LEGACY_ACTION, lane="issue_OTHER")),
            ("another role", _generation(action_id=LEGACY_ACTION, role="worker")),
            ("another slot", _generation(action_id=LEGACY_ACTION, assigned="someone_else")),
            ("a recycled pane", _generation(action_id=LEGACY_ACTION, locator="wA:p9")),
        ):
            with self.subTest(label=label):
                self._refuses("generation_mismatch", generation=generation)

    def test_a_legacy_action_on_its_own_current_row_still_passes_through(self) -> None:
        """The positive control: an exactly-matched legacy row is unchanged and free."""
        ports = _Ports(generation=_generation(action_id=LEGACY_ACTION))
        the_pin = _pin()
        plan = ports.planner().plan([the_pin], CONTEXT)
        self.assertEqual(plan.outcome, PLAN_LEGACY_UNCHANGED)
        self.assertIs(plan.participants[0], the_pin)
        self.assertEqual(ports.evidence_calls, 0)
        self.assertEqual(ports.lifecycle_calls, 0)

    def test_a_pending_row_refuses_before_capability_is_consulted(self) -> None:
        """Absent / unreadable / not-attested stay typed refusals, whatever the shape."""
        seen = []
        with self.assertRaises(EvidencePlanRefused) as ctx:
            _Ports(generation=_generation(action_id=LEGACY_ACTION, phase="pending")).planner(
                capability=lambda action_id: seen.append(action_id) or False
            ).plan([_pin()], CONTEXT)
        self.assertEqual(ctx.exception.reason, "generation_not_attested")
        self.assertEqual(seen, [])


class NontextAuthorityTest(unittest.TestCase):
    """Audit j#97083: a foreign value must never get to run code on the way to a refusal.

    At ec60a315 the legacy triplet was read with ``value != ""`` and every other axis with
    ``isinstance(value, str)`` followed by a comparison — so a hostile ``__ne__``/``__eq__``
    replaced the typed refusal with a raw ``OSError`` carrying a host path.
    """

    def _refuses_typed(self, label, the_pin=None, planner_kw=None, **ports):
        with self.subTest(label=label):
            try:
                _Ports(**ports).planner(**(planner_kw or {})).plan(
                    [the_pin if the_pin is not None else _foreign()], CONTEXT
                )
            except EvidencePlanRefused as refusal:
                self.assertNotIn(_Hostile._BOOM, str(refusal))
                self.assertNotIn("OSError", str(refusal))
            except Exception as raw:  # noqa: BLE001 - the defect this test exists for
                self.fail(f"{label}: raw {type(raw).__name__} escaped: {raw}")
            else:
                self.fail(f"{label}: a hostile authority value was ACCEPTED")

    def _sweep(self, hostile, kind: str) -> None:
        legacy = dict(generation=_generation(action_id=LEGACY_ACTION))
        capable = dict(evidence=_evidence())
        for attr in (
            "evidence_workspace_id",
            "evidence_startup_action_id",
            "evidence_cause",
        ):
            pin = _foreign(**{attr: hostile})
            self._refuses_typed(f"{kind} legacy {attr}", pin, **legacy)
            self._refuses_typed(f"{kind} receipt pre-pin {attr}", pin, **capable)
        for attr in (
            "lane_id",
            "role",
            "provider",
            "assigned_name",
            "old_locator",
            "lane_generation",
            "lane_revision",
        ):
            self._refuses_typed(
                f"{kind} pin {attr}", _foreign(**{attr: hostile}), **capable
            )
        for label, generation in (
            ("phase", _generation(phase=hostile)),
            ("action id", _generation(action_id=hostile)),
            ("locator", _generation(locator=hostile)),
        ):
            self._refuses_typed(
                f"{kind} generation {label}", generation=generation, evidence=_evidence()
            )
        self._refuses_typed(f"{kind} evidence key lane", evidence=_evidence(lane_id=hostile))
        self._refuses_typed(f"{kind} evidence blocker", evidence=_evidence(blocker=hostile))
        self._refuses_typed(
            f"{kind} cause port result",
            evidence=_evidence(),
            planner_kw={"update_cause": lambda provider, blocker: hostile},
        )
        self._refuses_typed(f"{kind} lifecycle result", evidence=_evidence(), lifecycle=hostile)
        self._refuses_typed(
            f"{kind} lifecycle member", evidence=_evidence(), lifecycle=(hostile, REV)
        )

    def test_no_axis_lets_a_hostile_object_run_code(self) -> None:
        self._sweep(_Hostile(), "object")

    def test_no_axis_lets_a_hostile_str_subclass_run_code(self) -> None:
        self._sweep(_HostileText(), "str subclass")

    def test_a_clean_legacy_participant_is_unaffected(self) -> None:
        """The positive control: closing these holes did not close the legacy path."""
        ports = _Ports(generation=_generation(action_id=LEGACY_ACTION))
        the_pin = _foreign()
        plan = ports.planner().plan([the_pin], CONTEXT)
        self.assertEqual(plan.outcome, PLAN_LEGACY_UNCHANGED)
        self.assertIs(plan.participants[0], the_pin)
        self.assertEqual(ports.evidence_calls, 0)
        self.assertEqual(ports.lifecycle_calls, 0)


class ExactAuthorityTest(unittest.TestCase):
    """Audit j#97074: normalising before comparing laundered foreign representations.

    Every case here was ACCEPTED at 9bcb1af0, where each authority axis was compared as
    ``str(value or "").strip()`` -- so a padded or renderable value was turned into the
    canonical token first and then found to match it.
    """

    def _refuses(self, reason, the_pin=None, planner_kw=None, **ports):
        with self.assertRaises(EvidencePlanRefused) as ctx:
            _Ports(**ports).planner(**(planner_kw or {})).plan(
                [the_pin if the_pin is not None else _pin()], CONTEXT
            )
        self.assertEqual(ctx.exception.reason, reason)

    def test_a_padded_generation_workspace_is_not_the_workspace(self) -> None:
        self._refuses(
            "generation_mismatch", generation=_generation(workspace=" wA "), evidence=_evidence()
        )

    def test_a_padded_evidence_key_lane_is_not_the_lane(self) -> None:
        self._refuses("evidence_mismatch", evidence=_evidence(lane_id=" issue_14741 "))

    def test_a_padded_cause_token_is_not_the_cause(self) -> None:
        self._refuses(
            "cause_not_update_derived",
            evidence=_evidence(),
            planner_kw={"update_cause": lambda provider, blocker: " " + CAUSE + " "},
        )

    def test_a_padded_action_id_is_refused_before_it_is_classified(self) -> None:
        """A padded id must never reach the capability port.

        A shape test would answer "not receipt-capable" for it and route a capable action
        down the legacy path -- fail-open in the one direction this ticket exists to close.
        """
        seen = []
        self._refuses(
            "unknown_action_shape",
            generation=_generation(action_id=" " + ACTION + " "),
            evidence=_evidence(),
            planner_kw={"capability": lambda action_id: seen.append(action_id) or True},
        )
        self.assertEqual(seen, [], "the capability port was never asked")

    def test_a_padded_attested_phase_is_not_attested(self) -> None:
        self._refuses(
            "generation_not_attested",
            generation=_generation(phase=" attested "),
            evidence=_evidence(),
        )

    def test_a_nontext_authority_is_never_rendered_into_a_token(self) -> None:
        for label, kw, reason in (
            ("bool phase", dict(generation=_generation(phase=True)), "generation_not_attested"),
            (
                "bytes action id",
                dict(generation=_generation(action_id=ACTION.encode())),
                "unknown_action_shape",
            ),
            (
                "numeric locator",
                dict(generation=_generation(locator=1)),
                "generation_mismatch",
            ),
        ):
            with self.subTest(label=label):
                self._refuses(reason, evidence=_evidence(), **kw)

    def test_a_renderable_participant_lane_is_not_the_lane(self) -> None:
        self._refuses(
            "lane_out_of_context", the_pin=_foreign(lane_id=_RenderedToken()), evidence=_evidence()
        )

    def test_a_padded_participant_axis_is_not_that_axis(self) -> None:
        for label, kw, reason in (
            ("lane", dict(lane_id=" issue_14741 "), "lane_out_of_context"),
            ("assigned name", dict(assigned_name=" mzb1_wA_codex_lane "), "generation_unavailable"),
            ("lane generation", dict(lane_generation=" " + GEN + " "), "lifecycle_mismatch"),
        ):
            with self.subTest(label=label):
                self._refuses(reason, the_pin=_foreign(**kw), evidence=_evidence())

    def test_a_legacy_participant_carrying_a_padded_triplet_still_refuses(self) -> None:
        """Presence, not well-formedness.

        Reading this slot with the exact-token helper would call a padded triplet ABSENT
        and wave the participant through as legacy -- the same laundering, inverted.
        """
        self._refuses(
            "divergent_pre_pin",
            the_pin=_foreign(evidence_workspace_id=" wA "),
            generation=_generation(action_id=LEGACY_ACTION),
        )

    def test_a_clean_legacy_participant_is_still_returned_unchanged(self) -> None:
        """The positive control for the two tests above."""
        ports = _Ports(generation=_generation(action_id=LEGACY_ACTION))
        the_pin = _foreign()
        plan = ports.planner().plan([the_pin], CONTEXT)
        self.assertEqual(plan.outcome, PLAN_LEGACY_UNCHANGED)
        self.assertIs(plan.participants[0], the_pin, "returned byte-exact, not rebuilt")
        self.assertEqual(ports.evidence_calls, 0)
        self.assertEqual(ports.lifecycle_calls, 0)


class ClosedContextTest(unittest.TestCase):
    """Audit j#97065: the context is authority, and every port answer is closed."""

    def _refuses(self, reason, ports, *, pin=None, context=CONTEXT, **kw):
        with self.assertRaises(EvidencePlanRefused) as ctx:
            ports.planner(**kw).plan([pin or _pin()], context)
        self.assertEqual(ctx.exception.reason, reason)

    def test_a_participant_from_another_lane_refuses(self) -> None:
        """Finding 1: the context lane was never compared."""
        foreign = PlanningContext(
            workspace_id=WORKSPACE,
            lane_id="issue_FOREIGN",
            expected_update_cause="update_relaunch",
        )
        self._refuses(
            "lane_out_of_context", _Ports(evidence=_evidence()), context=foreign
        )

    def test_an_invalid_context_refuses_before_anything_is_read(self) -> None:
        ports = _Ports(evidence=_evidence())
        for label, context in (
            ("blank workspace", PlanningContext("", "issue_14741", "update_relaunch")),
            ("blank lane", PlanningContext(WORKSPACE, "", "update_relaunch")),
            ("blank cause", PlanningContext(WORKSPACE, "issue_14741", "")),
            ("padded", PlanningContext(" wA ", "issue_14741", "update_relaunch")),
        ):
            with self.subTest(label=label):
                self._refuses("context_invalid", ports, context=context)
        self.assertEqual(ports.evidence_calls, 0, "refused before any port is consulted")

    def test_an_arbitrary_cause_token_is_not_an_authority(self) -> None:
        """Finding 2: non-empty is a shape, not a cause."""
        self._refuses(
            "cause_not_update_derived",
            _Ports(evidence=_evidence()),
            update_cause=lambda provider, blocker: "arbitrary_nonempty",
        )

    def test_a_port_exception_never_renders_its_body(self) -> None:
        """Finding 3: the generation port leaked its message, host path included."""
        ports = _Ports(generation_error=OSError("/private/host/path exploded"))
        with self.assertRaises(EvidencePlanRefused) as ctx:
            ports.planner().plan([_pin()], CONTEXT)
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.reason, "generation_unavailable")
        self.assertNotIn("/private/host/path", message)
        self.assertNotIn("exploded", message)
        self.assertIsInstance(ctx.exception.__cause__, OSError, "the chain is kept")

    def test_an_unusable_lifecycle_shape_is_typed_not_a_raw_error(self) -> None:
        """Finding 4: a 1-element answer escaped as a raw ValueError."""
        for label, lifecycle in (
            ("one element", (GEN,)),
            ("three elements", (GEN, REV, "extra")),
            ("not iterable", 7),
            ("a string", GEN),
            ("booleans", (True, False)),
            ("padded tokens", (" " + GEN, REV)),
        ):
            with self.subTest(label=label):
                self._refuses("lifecycle_unavailable", _Ports(lifecycle=lifecycle))

    def test_a_capability_port_that_is_not_an_exact_bool_refuses(self) -> None:
        self._refuses(
            "unknown_action_shape", _Ports(), capability=lambda action_id: "yes"
        )
        self._refuses("unknown_action_shape", _Ports(), capability=lambda action_id: 1)


class RefusalSafetyTest(unittest.TestCase):
    """Audit j#97062 finding 5: a refusal is durable-record safe verbatim."""

    def test_no_exception_text_or_host_path_reaches_the_refusal(self) -> None:
        ports = _Ports(
            evidence_error=OSError("/Users/secret/home/receipts.sqlite is unreadable")
        )
        with self.assertRaises(EvidencePlanRefused) as ctx:
            ports.planner().plan([_pin()], CONTEXT)
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.reason, "evidence_unavailable")
        self.assertNotIn("/Users/", message)
        self.assertNotIn("secret", message)


if __name__ == "__main__":
    unittest.main()
