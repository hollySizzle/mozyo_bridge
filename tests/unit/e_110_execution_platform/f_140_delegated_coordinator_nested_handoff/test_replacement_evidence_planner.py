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
    ReplacementEvidencePlanner,
)

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
    )
    base.update(kw)
    return ParticipantPin(**base)


def _generation(action_id=ACTION, phase="attested", role="gateway", lane="issue_14741"):
    return SimpleNamespace(
        startup_action_id=action_id, phase=phase, role=role, lane_id=lane
    )


def _evidence(action_id=ACTION, cause="update_prompt_available", **kw):
    key = SimpleNamespace(
        workspace_id=kw.get("workspace_id", "wA"),
        lane_id=kw.get("lane_id", "issue_14741"),
        provider=kw.get("provider", "codex"),
        assigned_name=kw.get("assigned_name", "mzb1_wA_codex_lane"),
        startup_action_id=action_id,
    )
    return SimpleNamespace(key=key, blocker_id=cause)


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

    def planner(self, capability=None):
        return ReplacementEvidencePlanner(
            generations=self.generations,
            lifecycle=self.lifecycle,
            evidence=self.evidence,
            capability=capability if capability is not None else _is_capable,
        )


def _is_capable(action_id):
    return str(action_id).startswith("startup-ir1-")


class LegacyPositiveControlTest(unittest.TestCase):
    def test_a_legacy_generation_is_byte_exact_and_opens_no_receipt_store(self) -> None:
        ports = _Ports(generation=_generation(action_id=LEGACY_ACTION))
        pin = _pin()
        plan = ports.planner().plan([pin])
        self.assertEqual(plan.outcome, PLAN_LEGACY_UNCHANGED)
        self.assertEqual(plan.participants, (pin,))
        self.assertIs(plan.participants[0], pin, "the same object, not a rebuilt copy")
        self.assertEqual(ports.evidence_calls, 0, "the receipt store is never opened")
        self.assertEqual(ports.lifecycle_calls, 0)

    def test_an_empty_plan_is_legacy_and_touches_nothing(self) -> None:
        ports = _Ports()
        plan = ports.planner().plan([])
        self.assertEqual(plan.participants, ())
        self.assertEqual(plan.outcome, PLAN_LEGACY_UNCHANGED)
        self.assertEqual(ports.evidence_calls, 0)


class ReceiptCapablePlanningTest(unittest.TestCase):
    def test_a_fully_agreeing_generation_is_planned_with_its_triplet(self) -> None:
        ports = _Ports(evidence=_evidence())
        planned = ports.planner().plan([_pin()])
        self.assertEqual(planned.outcome, PLAN_EVIDENCE_PINNED)
        pin = planned.participants[0]
        self.assertEqual(pin.evidence_workspace_id, "wA")
        self.assertEqual(pin.evidence_startup_action_id, ACTION)
        self.assertEqual(pin.evidence_cause, "update_prompt_available")

    def test_every_existing_authority_on_the_input_pin_is_carried_across(self) -> None:
        ports = _Ports(evidence=_evidence())
        original = _pin(
            is_self=True, lane_revision=REV, lane_generation=GEN, phase="launch_owed"
        )
        pin = ports.planner().plan([original]).participants[0]
        for attr in (
            "lane_id", "role", "provider", "assigned_name", "old_locator",
            "is_self", "lane_revision", "lane_generation", "phase",
        ):
            self.assertEqual(
                getattr(pin, attr), getattr(original, attr), f"{attr} must not drift"
            )

    def test_planning_is_deterministic_and_idempotent(self) -> None:
        ports = _Ports(evidence=_evidence())
        once = ports.planner().plan([_pin()]).participants[0]
        twice = ports.planner().plan([once]).participants[0]
        self.assertEqual(once, twice, "re-planning a correct pin reproduces it exactly")


class RefusalTest(unittest.TestCase):
    """Every refusal is a whole-plan refusal: zero plan, zero launch, zero store write."""

    def _refuses(self, reason, ports, pin=None, capability=None):
        with self.assertRaises(EvidencePlanRefused) as ctx:
            ports.planner(capability=capability).plan([pin or _pin()])
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
            planner.plan([SimpleNamespace(assigned_name="  ", lane_id="l", role="r")])
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
        self._refuses("evidence_mismatch", _Ports(evidence=_evidence(cause="")))

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
                evidence_cause="update_in_progress",
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
                evidence_cause="update_prompt_available",
            ),
        )


if __name__ == "__main__":
    unittest.main()
