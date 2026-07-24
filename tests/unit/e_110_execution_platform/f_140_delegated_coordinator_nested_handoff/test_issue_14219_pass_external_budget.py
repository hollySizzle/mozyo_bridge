"""The bounded pass's ONE external-mutation budget (Redmine #14219 T3 Final Disposition j#87188 = B).

A ``run_once`` bounded pass performs AT MOST ONE external mutation total across ALL workspaces —
one callback delivery (a receiver wake) OR a reconcile side-effect OR a hibernate actuation.
Delivery holds first priority for that single slot; a deterministic zero-send (``not_sent``) does
NOT consume it; an UNCERTAIN send consumes it. Budget-deferred rows stay PENDING (released at the
pre-send edge — no attempt bump), so the next pass delivers them (row-level exactly-once preserved).

Co-verified as the #14150 callback-supervisor residual (same lane) — j#87188 boundary 6.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
    WorkflowRuntimeStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    pass_external_budget as pxb,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    HibernateAttempt,
    HibernatePassResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MappingRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_HIBERNATE_BUDGET_DEFERRED,
    SKIP_PASS_BUDGET_SPENT,
    SUPERVISION_BOUNDED_RECONCILIATION,
)

CLOCK = "2026-07-13T00:00:00+00:00"


def _payload(issue: str, journal: str) -> dict:
    return {
        "issue": {"id": issue},
        "journals": [{
            "id": journal,
            "notes": "## Gate: review_request\n[mozyo:workflow-event:gate=review_request:conclusion=pending]",
        }],
    }


class BudgetPrimitivesTest(unittest.TestCase):
    def test_defer_fence_defers_only_once_spent(self) -> None:
        b = {"mutated": False, "uncertain": False, "reads": 0}
        fence = pxb.external_budget_defer_fence(b)
        self.assertEqual(fence("row"), (False, ""))
        b["mutated"] = True
        self.assertEqual(fence("row"), (True, pxb.PASS_BUDGET_SPENT_DEFER))

    def test_budgeted_sender_spends_on_delivered_and_uncertain_not_on_not_sent(self) -> None:
        for outcome, spends in (("delivered", True), ("busy", True), ("not_sent", False)):
            with self.subTest(outcome=outcome):
                b = {"mutated": False, "uncertain": False}
                pxb.budgeted_sender(lambda row: outcome, b)("row")
                self.assertEqual(pxb.budget_spent(b), spends)

    def test_compose_short_circuits_on_first_defer(self) -> None:
        a = lambda row: (False, "")
        c = lambda row: (True, "c")
        self.assertEqual(pxb.compose_defer_fences(a, c, None)("row"), (True, "c"))
        self.assertIsNone(pxb.compose_defer_fences(None, None))

    def test_a_sender_raise_consumes_the_budget_as_uncertain(self) -> None:
        # R4-F1: the sender is called AFTER the send-edge, so a raise = UNKNOWN external effect. The
        # wrapper spends the budget as uncertain (so no later row/workspace continues) then re-raises.
        b = {"mutated": False, "uncertain": False}

        def _boom(row):
            raise RuntimeError("transport blew up mid-send")

        with self.assertRaises(RuntimeError):
            pxb.budgeted_sender(_boom, b)("row")
        self.assertTrue(b["uncertain"])
        self.assertTrue(pxb.budget_spent(b))


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        sp = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=sp)
        self.outbox = CallbackOutbox(path=sp)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")

    def _ws(self, wsid):
        return SupervisedWorkspace(workspace_id=wsid, canonical_path=str(self.dir / wsid))

    def _source_for(self, mapping):
        # One source object whose read_entries dispatches by issue id to the right payload.
        sources = {i: MappingRedmineJournalSource(payload=p) for i, p in mapping.items()}

        def factory(ws):
            class _S:
                def read_entries(_s, issue_id):
                    src = sources.get(str(issue_id))
                    return src.read_entries(issue_id) if src is not None else ()
            return _S()
        return factory

    def _supervisor(self, *, workspaces, rosters, source_factory, sender, leg=None):
        return WorkspaceCallbackSupervisor(
            holder="superX", lease_store=self.lease_store, store=self.store, outbox=self.outbox,
            workspaces_fn=lambda: workspaces,
            roster_fn=lambda ws: (tuple(rosters[ws.workspace_id]), ""),
            redmine_source_fn=source_factory,
            sender_fn=lambda ws: sender,
            clock=lambda: CLOCK,
            hibernate_leg_fn=leg,
        )


class PassBudgetRunOnceTest(_Harness):
    def test_two_workspaces_deliver_at_most_one_callback(self) -> None:
        # Both wsA and wsB have a deliverable review_request. Deterministic (id) order: wsA delivers,
        # spending the pass's one external mutation; wsB is budget-deferred at the PRE-SEND edge with
        # its row left PENDING. Design Answer j#87266: budget bounds only the external send — wsB is
        # NOT wholesale-skipped (it still reads / partitions / supplies events), so its skipped_reason
        # is blank and its deferred row is delivered on the NEXT pass with no duplicate.
        calls: list = []
        sup = self._supervisor(
            workspaces=[self._ws("wsB"), self._ws("wsA")],
            rosters={"wsA": ("100",), "wsB": ("200",)},
            source_factory=self._source_for({"100": _payload("100", "1"), "200": _payload("200", "2")}),
            sender=lambda row: calls.append(row) or "delivered",
        )
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        self.assertEqual(len(calls), 1)  # ONE external mutation for the whole pass
        self.assertEqual(report.delivered, 1)
        by = {w.workspace_id: w for w in report.workspaces}
        self.assertEqual(by["wsA"].delivered, 1)
        self.assertEqual(by["wsB"].skipped_reason, "")  # NOT wholesale-skipped; deferred at send edge
        self.assertEqual(by["wsB"].delivered, 0)
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)  # wsB row still pending
        report2 = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)  # remainder next pass
        self.assertEqual(len(calls), 2)  # wsB now delivered; wsA NOT re-sent (no duplicate)
        self.assertEqual(report2.delivered, 1)

    def test_a_deterministic_block_does_not_consume_the_budget(self) -> None:
        # wsA's send is a deterministic zero-send (not_sent): it does NOT spend the budget, so wsB
        # still gets to deliver (Final Disposition j#87188 boundary 4).
        outcomes = iter(["not_sent", "delivered"])
        delivered_rows: list = []

        def sender(row):
            o = next(outcomes)
            if o == "delivered":
                delivered_rows.append(row)
            return o

        sup = self._supervisor(
            workspaces=[self._ws("wsA"), self._ws("wsB")],
            rosters={"wsA": ("100",), "wsB": ("200",)},
            source_factory=self._source_for({"100": _payload("100", "1"), "200": _payload("200", "2")}),
            sender=sender,
        )
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        by = {w.workspace_id: w for w in report.workspaces}
        self.assertNotEqual(by["wsB"].skipped_reason, SKIP_PASS_BUDGET_SPENT)  # wsB was NOT skipped
        self.assertEqual(report.delivered, 1)  # only wsB positively delivered
        self.assertEqual(len(delivered_rows), 1)

    def test_an_uncertain_send_consumes_the_budget(self) -> None:
        # wsA's send is UNCERTAIN (unknown outcome) -> spends the budget -> wsB performs no external
        # send this pass (no blind continuation behind an unknown external effect). Design Answer
        # j#87266: wsB is budget-deferred at the pre-send edge (row left pending), NOT wholesale-
        # skipped — its skipped_reason is blank and it delivered nothing.
        sup = self._supervisor(
            workspaces=[self._ws("wsA"), self._ws("wsB")],
            rosters={"wsA": ("100",), "wsB": ("200",)},
            source_factory=self._source_for({"100": _payload("100", "1"), "200": _payload("200", "2")}),
            sender=lambda row: "busy",  # neither delivered nor not_sent -> uncertain
        )
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        by = {w.workspace_id: w for w in report.workspaces}
        self.assertEqual(by["wsB"].skipped_reason, "")  # NOT wholesale-skipped; deferred at send edge
        self.assertEqual(by["wsB"].delivered, 0)
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)  # wsB row still pending

    def test_a_delivery_defers_the_folded_hibernate(self) -> None:
        # Delivery + hibernate share the ONE budget: wsA delivers, so its folded hibernate defers.
        hib_calls: list = []

        def leg(ws, renew, budget=None, restrict_issues=None):
            hib_calls.append(ws.workspace_id)
            return HibernatePassResult(
                attempts=(HibernateAttempt(issue="100", lane="l", kind="actuated", released=1),),
                mutations=1, empty_pass=False,
            )

        sup = self._supervisor(
            workspaces=[self._ws("wsA")],
            rosters={"wsA": ("100",)},
            source_factory=self._source_for({"100": _payload("100", "1")}),
            sender=lambda row: "delivered",
            leg=leg,
        )
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        w = report.workspaces[0]
        self.assertEqual(w.delivered, 1)
        self.assertEqual(hib_calls, [])  # the delivery spent the budget -> hibernate leg never ran
        self.assertEqual(w.hibernate_disposition, SKIP_HIBERNATE_BUDGET_DEFERRED)
        self.assertEqual(report.hibernate_applied, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
