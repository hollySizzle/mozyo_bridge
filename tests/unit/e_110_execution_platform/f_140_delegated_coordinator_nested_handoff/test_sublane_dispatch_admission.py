"""Pure sublane dispatch admission gate tests (Redmine #13290).

Pins the fail-closed / override policy that wires the single #12855 fill-decision
authority into the live dispatch path:

- no fill context -> the gate is not armed and the dispatch proceeds unchanged;
- ``dispatch_next`` -> the gate permits the dispatch;
- each of the five concrete ``stop_*`` decisions -> fail closed without an override,
  and proceed (recording the reason) with an explicit override;
- the concrete stop reason always comes from :func:`evaluate_fill_decision`, never a
  second vocabulary defined here.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_dispatch_admission import (  # noqa: E501
    FILL_GATE_DISPATCH,
    FILL_GATE_NOT_ARMED,
    FILL_GATE_STOP_BLOCKED,
    FILL_GATE_STOP_OVERRIDDEN,
    REASON_FILL_STOP,
    evaluate_dispatch_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (  # noqa: E501
    FILL_DISPATCH_NEXT,
    FILL_STOP_COORDINATOR_BLOCKING,
    FILL_STOP_NO_READY_WORK,
    FILL_STOP_OVERLAP,
    FILL_STOP_OWNER_OR_RELEASE_GATE,
    FILL_STOP_SOFT_PROFILE_FULL,
    FillDecisionInputs,
    LaneState,
)


def _dispatch_next_inputs() -> FillDecisionInputs:
    """A caller-supplied lane set that resolves to ``dispatch_next``.

    An ``implementing`` lane (not coordinator-blocking) plus ready independent work
    within remaining capacity, and no owner/release gate.
    """
    return FillDecisionInputs(
        lanes=(LaneState(issue="1", state_class="implementing"),),
        ready_independent_work=2,
        ready_overlapping_work=0,
        capacity_remaining=2,
        owner_or_release_gate_active=False,
    )


# One representative input set per concrete stop token. Each is a real
# `evaluate_fill_decision` stop; the gate never invents its own reason.
_STOP_INPUTS = {
    FILL_STOP_OWNER_OR_RELEASE_GATE: FillDecisionInputs(
        ready_independent_work=1,
        capacity_remaining=1,
        owner_or_release_gate_active=True,
    ),
    FILL_STOP_COORDINATOR_BLOCKING: FillDecisionInputs(
        lanes=(LaneState(issue="2", state_class="owner_waiting"),),
        ready_independent_work=1,
        capacity_remaining=1,
    ),
    FILL_STOP_OVERLAP: FillDecisionInputs(
        ready_independent_work=0,
        ready_overlapping_work=1,
        capacity_remaining=1,
    ),
    FILL_STOP_NO_READY_WORK: FillDecisionInputs(
        ready_independent_work=0,
        ready_overlapping_work=0,
        capacity_remaining=1,
    ),
    FILL_STOP_SOFT_PROFILE_FULL: FillDecisionInputs(
        ready_independent_work=3,
        capacity_remaining=0,
    ),
}


class NotArmedTests(unittest.TestCase):
    def test_no_fill_context_is_not_armed(self):
        decision = evaluate_dispatch_admission(None)
        self.assertEqual(decision.gate, FILL_GATE_NOT_ARMED)
        self.assertFalse(decision.armed)
        self.assertFalse(decision.is_blocked)
        self.assertIsNone(decision.fill)
        self.assertIsNone(decision.fill_decision)

    def test_override_alone_cannot_arm_or_block(self):
        # An override reason with no fill context is a no-op: nothing to override.
        decision = evaluate_dispatch_admission(None, override_reason="anything")
        self.assertEqual(decision.gate, FILL_GATE_NOT_ARMED)
        self.assertFalse(decision.is_blocked)
        self.assertIsNone(decision.override_reason)


class DispatchNextTests(unittest.TestCase):
    def test_dispatch_next_permits_dispatch(self):
        decision = evaluate_dispatch_admission(_dispatch_next_inputs())
        self.assertEqual(decision.gate, FILL_GATE_DISPATCH)
        self.assertTrue(decision.armed)
        self.assertFalse(decision.is_blocked)
        self.assertFalse(decision.overridden)
        self.assertEqual(decision.fill_decision, FILL_DISPATCH_NEXT)

    def test_dispatch_next_ignores_override(self):
        # A permitted dispatch does not become an "override" just because a reason
        # was passed — there was no stop to override.
        decision = evaluate_dispatch_admission(
            _dispatch_next_inputs(), override_reason="unused"
        )
        self.assertEqual(decision.gate, FILL_GATE_DISPATCH)
        self.assertIsNone(decision.override_reason)


class StopFailClosedTests(unittest.TestCase):
    def test_every_stop_token_fails_closed_without_override(self):
        for token, inputs in _STOP_INPUTS.items():
            with self.subTest(stop=token):
                decision = evaluate_dispatch_admission(inputs)
                self.assertEqual(decision.gate, FILL_GATE_STOP_BLOCKED)
                self.assertTrue(decision.is_blocked)
                self.assertFalse(decision.overridden)
                # The concrete stop reason comes from evaluate_fill_decision.
                self.assertEqual(decision.fill_decision, token)
                self.assertIn(token, decision.reason)
                self.assertIsNone(decision.override_reason)

    def test_blocked_reason_token_registered(self):
        # The fail-closed reason token the actuator records for a stop block.
        self.assertEqual(REASON_FILL_STOP, "fill_decision_stop")


class StopOverrideTests(unittest.TestCase):
    def test_every_stop_token_proceeds_with_explicit_override(self):
        for token, inputs in _STOP_INPUTS.items():
            with self.subTest(stop=token):
                decision = evaluate_dispatch_admission(
                    inputs, override_reason="owner intent #13229 j#72635"
                )
                self.assertEqual(decision.gate, FILL_GATE_STOP_OVERRIDDEN)
                self.assertFalse(decision.is_blocked)
                self.assertTrue(decision.overridden)
                self.assertEqual(decision.fill_decision, token)
                self.assertEqual(
                    decision.override_reason, "owner intent #13229 j#72635"
                )

    def test_blank_override_does_not_unblock(self):
        for blank in ("", "   ", None):
            with self.subTest(override=repr(blank)):
                decision = evaluate_dispatch_admission(
                    _STOP_INPUTS[FILL_STOP_NO_READY_WORK], override_reason=blank
                )
                self.assertEqual(decision.gate, FILL_GATE_STOP_BLOCKED)
                self.assertTrue(decision.is_blocked)

    def test_override_reason_is_trimmed(self):
        decision = evaluate_dispatch_admission(
            _STOP_INPUTS[FILL_STOP_NO_READY_WORK], override_reason="  spaced  "
        )
        self.assertEqual(decision.override_reason, "spaced")


class PayloadTests(unittest.TestCase):
    def test_payload_carries_gate_and_fill(self):
        decision = evaluate_dispatch_admission(
            _STOP_INPUTS[FILL_STOP_COORDINATOR_BLOCKING],
            override_reason="explicit",
        )
        payload = decision.as_payload()
        self.assertEqual(payload["gate"], FILL_GATE_STOP_OVERRIDDEN)
        self.assertTrue(payload["armed"])
        self.assertTrue(payload["overridden"])
        self.assertEqual(payload["fill_decision"], FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(payload["override_reason"], "explicit")
        # The nested fill payload is the #12855 authority's own envelope.
        self.assertEqual(
            payload["fill"]["fill_decision"], FILL_STOP_COORDINATOR_BLOCKING
        )

    def test_not_armed_payload_has_no_fill(self):
        payload = evaluate_dispatch_admission(None).as_payload()
        self.assertEqual(payload["gate"], FILL_GATE_NOT_ARMED)
        self.assertIsNone(payload["fill"])
        self.assertIsNone(payload["fill_decision"])


if __name__ == "__main__":
    unittest.main()
