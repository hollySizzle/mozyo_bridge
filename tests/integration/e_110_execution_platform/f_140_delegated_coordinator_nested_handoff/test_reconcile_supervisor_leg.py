"""Integration tests for the reconcile supervisor leg + its supervisor wiring (#13758 F1).

Drives the production reconcile leg (real reconcile-state store + real callback outbox, fake
markers / lifecycle facts) — the composition root the review required — and asserts the
reconciler actually runs on the supervisor path:

- a lane awaiting the worker's implementation_done, no durable progress -> a self-heal row
  routed to the worker (turn-ended -> re-read -> self-heal, §3);
- the awaited gate landed -> a coordinator deliver row keyed on the gate's own journal (§1);
- a lane in a non-worker-owed position (approved review) -> no reconcile (§6);
- a terminal (hibernated) disposition -> the reconcile closes, no send (§5 end);
- an unknown live generation -> zero-send (fail-closed, §8);
- WorkspaceCallbackSupervisor invokes the wired leg fn per issue (the wiring itself).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.reconcile_state import ReconcileStateKey, ReconcileStateStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_supervisor_leg import (
    reconcile_leg_once,
)


@dataclass(frozen=True)
class _Marker:
    gate: str
    journal: str
    review_conclusion: str = ""


class ReconcileLegHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = ReconcileStateStore(path=self.tmp / "state.sqlite")
        self.outbox = CallbackOutbox(path=self.tmp / "workflow-runtime.sqlite")

    def _leg(self, markers, *, live_generation=1, disposition="active", is_edge=True):
        return reconcile_leg_once(
            issue="13758",
            workspace_id="ws1",
            lane_id="lane-a",
            markers=markers,
            live_generation=live_generation,
            lifecycle_disposition=disposition,
            outbox=self.outbox,
            reconcile_store=self.store,
            is_edge=is_edge,
        )

    def _rows(self):
        return self.outbox.read()


class SelfHealFlow(ReconcileLegHarness):
    def test_awaiting_implementation_done_self_heals_to_worker(self):
        # No gate-bearing marker yet -> the worker owes implementation_done -> self-heal.
        rep = self._leg(markers=[])
        self.assertIsNotNone(rep)
        self.assertEqual(rep["action"], "self_heal")
        self.assertEqual(rep["route"], "implementation_worker")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].callback_route, "implementation_worker")
        self.assertEqual(rows[0].normalized_gate, "self_heal_attempt_1")
        # review R2-F2: the delivery target_receiver is the worker PROVIDER (claude), a
        # resolver-matchable identity, not the raw role token.
        self.assertEqual(rows[0].target_receiver, "claude")
        # review R2-F3: the record is anchored on issue + generation + dispatch baseline + gate.
        key = ReconcileStateKey("ws1", "lane-a", "13758:g1:from0:await:implementation_done")
        self.assertIsNotNone(self.store.get(key))

    def test_review_request_position_self_heals_to_gateway(self):
        rep = self._leg(markers=[_Marker(gate="review_request", journal="79368")])
        self.assertEqual(rep["action"], "self_heal")
        self.assertEqual(rep["route"], "implementation_gateway")

    def test_non_edge_sweep_does_not_self_heal(self):
        # review R2-F1: a bounded-reconciliation sweep (is_edge=False) does not self-heal.
        rep = self._leg(markers=[], is_edge=False)
        self.assertEqual(rep["action"], "none")
        self.assertEqual(len(self._rows()), 0)

    def test_correction_loop_same_gate_new_round_is_a_distinct_record(self):
        # review R2-F3: changes_requested -> correction re-awaits implementation_done in a NEW
        # round (from the review_result position) -> a distinct record, fresh counter.
        self._leg(markers=[])  # round-1 await implementation_done from baseline 0
        # round 2: review_result(changes_requested) landed at 79500 -> re-await implementation_done.
        rep = self._leg(
            markers=[
                _Marker(gate="implementation_done", journal="79400"),
                _Marker(gate="review_request", journal="79420"),
                _Marker(gate="review_result", journal="79500", review_conclusion="changes_requested"),
            ]
        )
        self.assertEqual(rep["action"], "self_heal")
        # a distinct anchor keyed on the review_result baseline (79500), not the round-1 one.
        round2 = ReconcileStateKey(
            "ws1", "lane-a", "13758:g1:from79500:await:implementation_done"
        )
        self.assertIsNotNone(self.store.get(round2))
        self.assertEqual(self.store.get(round2).reconcile_failure_count, 1)  # fresh counter


class DeliverFlow(ReconcileLegHarness):
    def test_awaited_gate_landed_delivers_coordinator_keyed_on_gate_journal(self):
        # First cycle: awaiting implementation_done -> self-heal, records baseline "0".
        self._leg(markers=[])
        # Now the worker produced implementation_done at journal 79400 (> baseline 0).
        rep = self._leg(markers=[_Marker(gate="implementation_done", journal="79400")])
        self.assertEqual(rep["action"], "deliver_coordinator")
        deliver = [r for r in self._rows() if r.callback_route == "coordinator"]
        self.assertEqual(len(deliver), 1)
        # review F3: keyed on the gate's own journal.
        self.assertEqual(deliver[0].journal, "79400")
        self.assertEqual(deliver[0].normalized_gate, "implementation_done")


class NoReconcileFlow(ReconcileLegHarness):
    def test_approved_review_is_not_reconciled(self):
        rep = self._leg(
            markers=[_Marker(gate="review_result", journal="79400", review_conclusion="approved")]
        )
        self.assertIsNone(rep)  # owner-owed close -> the reconciler does not self-heal
        self.assertEqual(len(self._rows()), 0)

    def test_terminal_disposition_closes_without_send(self):
        rep = self._leg(markers=[], disposition="hibernated")
        self.assertEqual(rep["action"], "none")
        self.assertEqual(len(self._rows()), 0)

    def test_unknown_generation_zero_sends(self):
        rep = self._leg(markers=[], live_generation=0)
        self.assertEqual(rep["action"], "zero_send")
        self.assertEqual(len(self._rows()), 0)


class SupervisorWiringTest(unittest.TestCase):
    """The supervisor invokes the wired reconcile_leg_fn per supervised issue (the wiring)."""

    def test_supervisor_calls_reconcile_leg_fn(self):
        from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
            SupervisedWorkspace,
            WorkspaceCallbackSupervisor,
        )

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        calls = []

        supervisor = WorkspaceCallbackSupervisor(
            holder="test-holder",
            lease_store=SupervisorLeaseStore(path=tmp / "lease.sqlite"),
            store=WorkflowRuntimeStore(path=tmp / "wf.sqlite"),
            outbox=CallbackOutbox(path=tmp / "wf.sqlite"),
            workspaces_fn=lambda: [SupervisedWorkspace(workspace_id="ws1", canonical_path=str(tmp))],
            roster_fn=lambda ws: (("13758",), ""),
            redmine_source_fn=lambda ws: None,
            sender_fn=lambda ws: (lambda row: "delivered"),
            reconcile_leg_fn=lambda wsid, issue, source, is_edge: calls.append(
                (wsid, issue, is_edge)
            ),
        )
        # A local wake for the issue -> is_edge=True is threaded to the leg.
        supervisor.run_once(mode="local_wake", wake_hints=[("ws1", "13758")])
        self.assertIn(("ws1", "13758", True), calls)


if __name__ == "__main__":
    unittest.main()
