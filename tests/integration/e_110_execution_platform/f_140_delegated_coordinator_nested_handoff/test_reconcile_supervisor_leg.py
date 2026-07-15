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

    def _leg(self, markers, *, live_generation=1, disposition="active"):
        return reconcile_leg_once(
            issue="13758",
            workspace_id="ws1",
            lane_id="lane-a",
            markers=markers,
            live_generation=live_generation,
            lifecycle_disposition=disposition,
            outbox=self.outbox,
            reconcile_store=self.store,
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
        # The record is anchored on the awaited gate.
        key = ReconcileStateKey("ws1", "lane-a", "13758:await:implementation_done")
        self.assertIsNotNone(self.store.get(key))

    def test_review_request_position_self_heals_to_gateway(self):
        rep = self._leg(markers=[_Marker(gate="review_request", journal="79368")])
        self.assertEqual(rep["action"], "self_heal")
        self.assertEqual(rep["route"], "implementation_gateway")


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
            reconcile_leg_fn=lambda wsid, issue, source: calls.append((wsid, issue)),
        )
        supervisor.run_once()
        self.assertIn(("ws1", "13758"), calls)


if __name__ == "__main__":
    unittest.main()
