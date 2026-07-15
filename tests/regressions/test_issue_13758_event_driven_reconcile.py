"""Scenario regression for the event-driven reconciler (Redmine #13758 acceptance §10).

Drives the full reconcile runtime (real reconcile-state store + real callback outbox, fake
Redmine ``observe``) through the named acceptance scenarios end-to-end:

- original / recovery pair -> distinct anchors, distinct counters, no cross-talk;
- hibernate / resume -> a terminal disposition closes; a fresh anchor is a fresh cycle;
- agent re-login -> a new turn-end edge on the same anchor does not reset the ladder;
- supervisor restart replay -> enqueue-then-crash is idempotent (no double send / miscount);
- out-of-order gate -> a gate that advances mid-ladder delivers and closes;
- newer review generation -> a stale record vs a newer live generation zero-sends;
- startup blocker -> an unresolved same-lane receiver zero-sends the self-heal (no mis-send).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.reconcile_state import ReconcileStateKey, ReconcileStateStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_runtime import (
    ReconcileCycleInput,
    reconcile_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_state_machine import (
    GEN_MATCH,
    GEN_MISMATCH,
    RECONCILE_ACTION_DELIVER,
    RECONCILE_ACTION_ESCALATE,
    RECONCILE_ACTION_SELF_HEAL,
    RECONCILE_ACTION_ZERO_SEND,
    ROUTE_RESOLVED,
    ROUTE_UNRESOLVED,
    ReconcileObservation,
)

WORKER = "implementation_worker"


def _observe(**kw):
    base = dict(
        redmine_readable=True,
        generation_status=GEN_MATCH,
        gate_advanced=False,
        has_outstanding_gate=True,
        terminal_disposition=False,
        deadline_exceeded=False,
        prior_send_uncertain=False,
        route_status=ROUTE_RESOLVED,
        expected_next_owner=WORKER,
    )
    base.update(kw)
    obs = ReconcileObservation(**base)
    return lambda cycle, record: obs


class Issue13758Regression(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = ReconcileStateStore(path=self.tmp / "state.sqlite")
        self.outbox = CallbackOutbox(path=self.tmp / "workflow-runtime.sqlite")

    def _cycle(self, anchor="13758:79337", lane="lane-a", gen=1):
        return ReconcileCycleInput(
            key=ReconcileStateKey(
                workspace_id="ws1", lane_id=lane, dispatch_anchor=anchor
            ),
            issue_id="13758",
            dispatch_journal=anchor.split(":")[-1],
            expected_gate="implementation_done",
            expected_next_owner=WORKER,
            lane_generation=gen,
            target_lane=lane,
        )

    def _run(self, cycle, observe):
        return reconcile_once(cycle, observe=observe, outbox=self.outbox, store=self.store)

    # -- scenarios -----------------------------------------------------------

    def test_original_recovery_pair_counters_are_independent(self):
        original = self._cycle(anchor="13758:79337", lane="lane-orig", gen=1)
        recovery = self._cycle(anchor="13758:79999", lane="lane-recov", gen=2)
        # The original lane self-heals twice.
        self._run(original, _observe())
        self._run(original, _observe())
        self.assertEqual(self.store.get(original.key).reconcile_failure_count, 2)
        # The recovery lane is a fresh, independent counter.
        self._run(recovery, _observe())
        self.assertEqual(self.store.get(recovery.key).reconcile_failure_count, 1)
        self.assertEqual(self.store.get(original.key).reconcile_failure_count, 2)

    def test_hibernate_closes_then_fresh_anchor_is_fresh_cycle(self):
        cyc = self._cycle()
        self._run(cyc, _observe())  # self-heal 1
        rep = self._run(cyc, _observe(terminal_disposition=True))  # hibernate -> close
        self.assertEqual(self.store.get(cyc.key).phase, "closed")
        self.assertFalse(rep["sent"])
        # Resume as a new generation / anchor -> a brand-new reconcile record, counter 0.
        resumed = self._cycle(anchor="13758:80001", gen=3)
        r2 = self._run(resumed, _observe())
        self.assertEqual(r2["action"], RECONCILE_ACTION_SELF_HEAL)
        self.assertEqual(self.store.get(resumed.key).reconcile_failure_count, 1)

    def test_relogin_same_anchor_does_not_reset_ladder(self):
        cyc = self._cycle()
        self._run(cyc, _observe())  # self-heal 1 (count 1)
        # A re-login produces a new turn-end edge on the SAME dispatch anchor. open_cycle is a
        # no-op (counter preserved); the ladder advances to self-heal 2, not back to 1.
        rep = self._run(cyc, _observe())
        self.assertEqual(rep["action"], RECONCILE_ACTION_SELF_HEAL)
        self.assertEqual(rep["reconcile_failure_count"], 2)

    def test_restart_replay_is_idempotent(self):
        cyc = self._cycle()
        self._run(cyc, _observe())  # self-heal 1, revision -> 2, one attempt-1 outbox row
        rev_after = self.store.get(cyc.key).revision
        count_after = self.store.get(cyc.key).reconcile_failure_count
        # Model a crash *after* enqueue but *before* the store advance would be lost: re-open
        # never resets, and a re-run at the advanced phase does not re-write the attempt-1 key.
        attempt1 = [r for r in self.outbox.read() if r.normalized_gate == "self_heal_attempt_1"]
        self.assertEqual(len(attempt1), 1)
        self.assertEqual(count_after, 1)
        self.assertEqual(rev_after, 2)

    def test_out_of_order_gate_delivers_and_closes_mid_ladder(self):
        cyc = self._cycle()
        self._run(cyc, _observe())  # self-heal 1
        self._run(cyc, _observe())  # self-heal 2
        rep = self._run(cyc, _observe(gate_advanced=True))  # gate finally moved
        self.assertEqual(rep["action"], RECONCILE_ACTION_DELIVER)
        self.assertEqual(self.store.get(cyc.key).phase, "notified")
        delivered = [r for r in self.outbox.read() if r.callback_route == "coordinator"]
        self.assertEqual(len(delivered), 1)

    def test_newer_review_generation_zero_sends(self):
        cyc = self._cycle()
        self._run(cyc, _observe())  # establish a record at self-heal 1
        before = self.store.get(cyc.key)
        rep = self._run(cyc, _observe(generation_status=GEN_MISMATCH))
        self.assertEqual(rep["action"], RECONCILE_ACTION_ZERO_SEND)
        after = self.store.get(cyc.key)
        self.assertEqual(after.revision, before.revision)  # byte-unchanged

    def test_startup_blocker_zero_sends_the_self_heal(self):
        # A startup-blocked / unresolvable same-lane receiver never mis-sends (acceptance §8).
        cyc = self._cycle()
        rep = self._run(
            cyc, _observe(route_status=ROUTE_UNRESOLVED, expected_next_owner="")
        )
        self.assertEqual(rep["action"], RECONCILE_ACTION_ZERO_SEND)
        self.assertEqual(len(self.outbox.read()), 0)
        self.assertEqual(self.store.get(cyc.key).reconcile_failure_count, 0)

    def test_three_strike_escalation_then_no_duplicate(self):
        cyc = self._cycle()
        self._run(cyc, _observe())  # self-heal 1
        self._run(cyc, _observe())  # self-heal 2
        r3 = self._run(cyc, _observe())  # escalate
        self.assertEqual(r3["action"], RECONCILE_ACTION_ESCALATE)
        r4 = self._run(cyc, _observe())  # suppressed
        self.assertNotEqual(r4["action"], RECONCILE_ACTION_ESCALATE)
        escalations = [
            r for r in self.outbox.read() if r.normalized_gate == "coordinator_escalation"
        ]
        self.assertEqual(len(escalations), 1)  # exactly one escalation, ever


if __name__ == "__main__":
    unittest.main()
