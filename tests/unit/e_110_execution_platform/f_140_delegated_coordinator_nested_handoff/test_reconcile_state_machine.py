"""Unit tests for the event-driven turn/gate reconcile state machine (Redmine #13758).

Pins the fixed-vocabulary, fail-closed decisions of
:mod:`...domain.reconcile_state_machine` against the issue acceptance criteria:

- turn ended + gate advanced -> exactly-once coordinator callback (§1);
- gate advanced regardless of runtime turn state -> not delayed (§2, delivery branch);
- edge + outstanding gate + Redmine not moved -> self-heal 1 to expected owner, 0 coord (§3);
- second no-progress cycle -> self-heal 2 (§4);
- third consecutive no-progress cycle -> coordinator escalation once, then suppressed (§5);
- outstanding-gate-absent plain turn-end / ack -> 0 callbacks (§6);
- persistent-done re-observation is not an edge -> no counter growth (§7);
- unknown / mismatched generation, ambiguous route, unreadable Redmine -> zero-send (§8);
- uncertain prior send -> escalate for visibility, never blind-resend (§8).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authority import (
    TARGET_AWAITING_INPUT,
    TARGET_BUSY,
    TARGET_TURN_ENDED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_state_machine import (
    COORDINATOR_ROUTE,
    GEN_MATCH,
    GEN_MISMATCH,
    GEN_UNKNOWN,
    REASON_ALREADY_ESCALATED,
    REASON_ALREADY_NOTIFIED,
    REASON_CALLBACK_DELIVERED,
    REASON_DEADLINE_EXCEEDED,
    REASON_GATE_ADVANCED,
    REASON_GENERATION_MISMATCH,
    REASON_NO_OUTSTANDING_GATE,
    REASON_REDMINE_UNREADABLE,
    REASON_ROUTE_AMBIGUOUS,
    REASON_SELF_HEAL_1,
    REASON_SELF_HEAL_2,
    REASON_SELF_HEAL_UNCERTAIN,
    REASON_TERMINAL_DISPOSITION,
    REASON_THREE_STRIKE,
    RECONCILE_ACTION_DELIVER,
    RECONCILE_ACTION_ESCALATE,
    RECONCILE_ACTION_NONE,
    RECONCILE_ACTION_SELF_HEAL,
    RECONCILE_ACTION_ZERO_SEND,
    RECONCILE_CALLBACK_PENDING,
    RECONCILE_CLOSED,
    RECONCILE_COORDINATOR_ESCALATION,
    RECONCILE_NOTIFIED,
    RECONCILE_SELF_HEAL_1,
    RECONCILE_SELF_HEAL_2,
    RECONCILE_TURN_ENDED_GATE_PENDING,
    ROUTE_AMBIGUOUS,
    ROUTE_RESOLVED,
    ROUTE_UNRESOLVED,
    ReconcileObservation,
    advance_reconcile,
    is_turn_end_edge,
)

WORKER = "implementation_worker"


def _obs(**kw) -> ReconcileObservation:
    """A readable, exact-generation observation with an outstanding gate + resolved route.

    Defaults are the *self-heal-eligible* baseline; each test overrides only what it probes.
    """
    base = dict(
        redmine_readable=True,
        generation_status=GEN_MATCH,
        gate_advanced=False,
        advanced_gate_journal="79368",
        callback_delivered=False,
        has_outstanding_gate=True,
        terminal_disposition=False,
        deadline_exceeded=False,
        prior_send_uncertain=False,
        route_status=ROUTE_RESOLVED,
        expected_next_owner=WORKER,
        is_edge=True,  # default to a genuine turn-end edge; non-edge cases override
    )
    base.update(kw)
    return ReconcileObservation(**base)


class TurnEndEdgeTest(unittest.TestCase):
    def test_busy_to_turn_ended_is_an_edge(self):
        self.assertTrue(is_turn_end_edge(TARGET_BUSY, TARGET_TURN_ENDED))

    def test_awaiting_to_turn_ended_is_an_edge(self):
        self.assertTrue(is_turn_end_edge(TARGET_AWAITING_INPUT, TARGET_TURN_ENDED))

    def test_blank_or_unknown_prior_is_not_an_edge(self):
        # review R4-F2: a fresh record / restart / first attach (no positive prior active
        # observation) is NOT a turn edge — no evidence of a busy->turn_ended transition, so a
        # persistent-done worker at restart does not fabricate a self-heal.
        self.assertFalse(is_turn_end_edge("", TARGET_TURN_ENDED))
        self.assertFalse(is_turn_end_edge("unknown", TARGET_TURN_ENDED))

    def test_persistent_done_reobservation_is_not_an_edge(self):
        # §7: a persistent turn_ended level re-observed on a later snapshot is not a new event.
        self.assertFalse(is_turn_end_edge(TARGET_TURN_ENDED, TARGET_TURN_ENDED))

    def test_non_turn_ended_observation_is_never_an_edge(self):
        self.assertFalse(is_turn_end_edge(TARGET_BUSY, TARGET_BUSY))
        self.assertFalse(is_turn_end_edge(TARGET_TURN_ENDED, TARGET_BUSY))


class FailClosedGuardTest(unittest.TestCase):
    def test_unreadable_redmine_zero_sends_and_does_not_mutate(self):
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=1,
            observation=_obs(redmine_readable=False),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ZERO_SEND)
        self.assertEqual(d.reason, REASON_REDMINE_UNREADABLE)
        self.assertFalse(d.mutates_state)
        self.assertEqual(d.next_failure_count, 1)  # counter untouched
        self.assertEqual(d.next_phase, RECONCILE_TURN_ENDED_GATE_PENDING)  # phase untouched
        self.assertEqual(d.route, "")

    def test_unknown_generation_zero_sends(self):
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(generation_status=GEN_UNKNOWN),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ZERO_SEND)
        self.assertEqual(d.reason, REASON_GENERATION_MISMATCH)
        self.assertFalse(d.mutates_state)

    def test_mismatched_generation_zero_sends_even_with_advanced_gate(self):
        # A stale generation is never acted on, even if that (stale) generation's gate moved.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(generation_status=GEN_MISMATCH, gate_advanced=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ZERO_SEND)
        self.assertEqual(d.reason, REASON_GENERATION_MISMATCH)

    def test_ambiguous_route_zero_sends_a_self_heal(self):
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(route_status=ROUTE_AMBIGUOUS),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ZERO_SEND)
        self.assertEqual(d.reason, REASON_ROUTE_AMBIGUOUS)
        self.assertFalse(d.mutates_state)

    def test_unresolved_owner_zero_sends_a_self_heal(self):
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(route_status=ROUTE_UNRESOLVED, expected_next_owner=""),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ZERO_SEND)
        self.assertEqual(d.reason, REASON_ROUTE_AMBIGUOUS)


class GateAdvancedTest(unittest.TestCase):
    def test_gate_advanced_enqueues_then_pends_not_notified(self):
        # §1 + review F4: gate advanced -> deliver (enqueue) and move to callback_pending;
        # enqueue is not delivery, so it does NOT jump to notified.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(gate_advanced=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_DELIVER)
        self.assertEqual(d.reason, REASON_GATE_ADVANCED)
        self.assertEqual(d.next_phase, RECONCILE_CALLBACK_PENDING)
        self.assertEqual(d.route, COORDINATOR_ROUTE)
        self.assertTrue(d.sends)

    def test_callback_delivered_advances_to_notified(self):
        # review F4: only the durable outbox delivery advances callback_pending -> notified.
        d = advance_reconcile(
            phase=RECONCILE_CALLBACK_PENDING,
            reconcile_failure_count=0,
            observation=_obs(gate_advanced=True, callback_delivered=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_NONE)
        self.assertEqual(d.reason, REASON_CALLBACK_DELIVERED)
        self.assertEqual(d.next_phase, RECONCILE_NOTIFIED)
        self.assertFalse(d.sends)

    def test_gate_advanced_without_journal_is_not_delivered(self):
        # review F3 fail-closed: a gate cannot be delivered without its exact source journal.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(gate_advanced=True, advanced_gate_journal=""),
        )
        self.assertNotEqual(d.action, RECONCILE_ACTION_DELIVER)  # falls through to self-heal

    def test_gate_advanced_after_self_heal_still_delivers(self):
        # §2/§3->progress: a self-heal that worked (gate finally advanced) delivers the callback.
        d = advance_reconcile(
            phase=RECONCILE_SELF_HEAL_2,
            reconcile_failure_count=2,
            observation=_obs(gate_advanced=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_DELIVER)
        self.assertEqual(d.next_phase, RECONCILE_CALLBACK_PENDING)

    def test_already_notified_does_not_redeliver(self):
        d = advance_reconcile(
            phase=RECONCILE_NOTIFIED,
            reconcile_failure_count=0,
            observation=_obs(gate_advanced=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_NONE)
        self.assertEqual(d.reason, REASON_ALREADY_NOTIFIED)
        self.assertFalse(d.sends)

    def test_gate_advanced_delivers_even_when_agent_working_again(self):
        # §2: the durable gate callback is not delayed for the runtime turn state — the FSM
        # decides on gate_advanced independent of any runtime token.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=1,
            observation=_obs(gate_advanced=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_DELIVER)


class NoOutstandingGateTest(unittest.TestCase):
    def test_plain_turn_end_no_gate_does_not_notify(self):
        # §6: outstanding-gate-absent done / ack -> no callback, close the reconcile.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(has_outstanding_gate=False),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_NONE)
        self.assertEqual(d.reason, REASON_NO_OUTSTANDING_GATE)
        self.assertEqual(d.next_phase, RECONCILE_CLOSED)
        self.assertFalse(d.sends)

    def test_terminal_disposition_closes_without_notify(self):
        d = advance_reconcile(
            phase=RECONCILE_SELF_HEAL_1,
            reconcile_failure_count=1,
            observation=_obs(terminal_disposition=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_NONE)
        self.assertEqual(d.reason, REASON_TERMINAL_DISPOSITION)
        self.assertEqual(d.next_phase, RECONCILE_CLOSED)


class SelfHealLadderTest(unittest.TestCase):
    def test_first_no_progress_cycle_self_heals_to_expected_owner(self):
        # §3: edge + outstanding gate + Redmine not moved -> self-heal 1, 0 coordinator.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_SELF_HEAL)
        self.assertEqual(d.reason, REASON_SELF_HEAL_1)
        self.assertEqual(d.next_phase, RECONCILE_SELF_HEAL_1)
        self.assertEqual(d.next_failure_count, 1)
        self.assertEqual(d.route, WORKER)  # expected owner, NOT coordinator, NOT hard reviewer
        self.assertNotEqual(d.route, COORDINATOR_ROUTE)

    def test_second_no_progress_cycle_self_heals_again(self):
        # §4: second consecutive no-progress -> self-heal 2, still 0 coordinator.
        d = advance_reconcile(
            phase=RECONCILE_SELF_HEAL_1,
            reconcile_failure_count=1,
            observation=_obs(),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_SELF_HEAL)
        self.assertEqual(d.reason, REASON_SELF_HEAL_2)
        self.assertEqual(d.next_phase, RECONCILE_SELF_HEAL_2)
        self.assertEqual(d.next_failure_count, 2)
        self.assertEqual(d.route, WORKER)

    def test_third_consecutive_failure_escalates_once(self):
        # §5: third consecutive no-progress -> coordinator escalation once.
        d = advance_reconcile(
            phase=RECONCILE_SELF_HEAL_2,
            reconcile_failure_count=2,
            observation=_obs(),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ESCALATE)
        self.assertEqual(d.reason, REASON_THREE_STRIKE)
        self.assertEqual(d.next_phase, RECONCILE_COORDINATOR_ESCALATION)
        self.assertEqual(d.next_failure_count, 3)
        self.assertEqual(d.route, COORDINATOR_ROUTE)

    def test_after_escalation_further_cycles_are_suppressed(self):
        # §5: "以後 duplicate 抑止" — a fourth no-progress cycle does not re-escalate.
        d = advance_reconcile(
            phase=RECONCILE_COORDINATOR_ESCALATION,
            reconcile_failure_count=3,
            observation=_obs(),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_NONE)
        self.assertEqual(d.reason, REASON_ALREADY_ESCALATED)
        self.assertFalse(d.sends)
        self.assertEqual(d.next_failure_count, 3)  # no growth

    def test_full_ladder_sequence(self):
        # The whole 1->2->escalate->suppressed sequence, threading the persisted state.
        phase, count = RECONCILE_TURN_ENDED_GATE_PENDING, 0
        seq = []
        for _ in range(4):
            d = advance_reconcile(
                phase=phase, reconcile_failure_count=count, observation=_obs()
            )
            seq.append((d.action, d.next_failure_count))
            phase, count = d.next_phase, d.next_failure_count
        self.assertEqual(
            seq,
            [
                (RECONCILE_ACTION_SELF_HEAL, 1),
                (RECONCILE_ACTION_SELF_HEAL, 2),
                (RECONCILE_ACTION_ESCALATE, 3),
                (RECONCILE_ACTION_NONE, 3),
            ],
        )


class EdgeGatingTest(unittest.TestCase):
    def test_non_edge_sweep_does_not_self_heal_or_increment(self):
        # review R2-F1: a bounded-reconciliation sweep (no turn-end edge) must not advance the
        # self-heal ladder or the counter.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(is_edge=False),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_NONE)
        self.assertFalse(d.sends)
        self.assertFalse(d.mutates_state)  # no revision bump on a non-edge no-progress sweep
        self.assertEqual(d.next_failure_count, 0)

    def test_repeated_non_edge_sweeps_never_reach_three_strike(self):
        phase, count = RECONCILE_TURN_ENDED_GATE_PENDING, 0
        for _ in range(5):
            d = advance_reconcile(
                phase=phase, reconcile_failure_count=count, observation=_obs(is_edge=False)
            )
            self.assertNotEqual(d.action, RECONCILE_ACTION_ESCALATE)
            phase, count = d.next_phase, d.next_failure_count
        self.assertEqual(count, 0)  # never grew without an edge

    def test_non_edge_sweep_still_delivers_advanced_gate(self):
        # A bounded sweep still catches a missed gate delivery.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(is_edge=False, gate_advanced=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_DELIVER)

    def test_non_edge_sweep_still_escalates_on_deadline(self):
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(is_edge=False, deadline_exceeded=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ESCALATE)


class DeadlineAndUncertainTest(unittest.TestCase):
    def test_deadline_exceeded_escalates_even_at_first_cycle(self):
        # §4/§5: a bounded self-heal deadline elapsing escalates, bypassing the remaining ladder.
        d = advance_reconcile(
            phase=RECONCILE_TURN_ENDED_GATE_PENDING,
            reconcile_failure_count=0,
            observation=_obs(deadline_exceeded=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ESCALATE)
        self.assertEqual(d.reason, REASON_DEADLINE_EXCEEDED)
        self.assertEqual(d.route, COORDINATOR_ROUTE)
        self.assertGreaterEqual(d.next_failure_count, 3)

    def test_uncertain_prior_send_escalates_for_visibility(self):
        # §8: an uncertain prior self-heal is never blind-resent; surface to the coordinator.
        d = advance_reconcile(
            phase=RECONCILE_SELF_HEAL_1,
            reconcile_failure_count=1,
            observation=_obs(prior_send_uncertain=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_ESCALATE)
        self.assertEqual(d.reason, REASON_SELF_HEAL_UNCERTAIN)
        self.assertEqual(d.route, COORDINATOR_ROUTE)

    def test_uncertain_does_not_reescalate_after_escalation(self):
        d = advance_reconcile(
            phase=RECONCILE_COORDINATOR_ESCALATION,
            reconcile_failure_count=3,
            observation=_obs(prior_send_uncertain=True),
        )
        self.assertEqual(d.action, RECONCILE_ACTION_NONE)
        self.assertEqual(d.reason, REASON_ALREADY_ESCALATED)


if __name__ == "__main__":
    unittest.main()
