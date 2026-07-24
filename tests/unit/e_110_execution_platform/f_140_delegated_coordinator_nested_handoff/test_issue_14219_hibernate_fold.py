"""The auto-hibernate leg folded into the bounded pass (Redmine #14219 T3, Answer j#87108).

The T3 ruling (Design Consultation Answer j#87108) folds the hibernate leg into the SAME
``run_once`` bounded pass as ONE leg AFTER the callback/outbox delivery + reconcile legs, reachable
from BOTH ``local_wake`` (event-wake primary) and ``bounded_reconciliation`` (timer/restart
fallback), sharing the pass's one lifecycle-mutation budget and the existing delivery priority.
``local_drain`` never folds it; the standalone ``SUPERVISION_HIBERNATE`` mode stays only as a
production-unreachable internal/test seam delegating to the SAME primitive.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    HibernateAttempt,
    HibernatePassResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    workspace_hibernate_leg as fold_mod,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_LEASE_LOST,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_DRAIN,
    SUPERVISION_LOCAL_WAKE,
    WorkspaceSupervisionOutcome,
)

CLOCK = "2026-07-24T00:00:00+00:00"


def _boom_source(ws):  # pragma: no cover - the hibernate fold never resolves a provider source
    raise AssertionError("the folded hibernate leg must not resolve a ticket-provider source")


def _mutating_leg(calls, *, mutations=1, kind="actuated", reason="", raises=False):
    def leg(ws, renew, budget=None):
        calls.append(ws.workspace_id)
        if raises:
            raise RuntimeError("leg exploded")
        attempts = (
            (HibernateAttempt(issue="1", lane="l", kind=kind, reason=reason, revision=2),)
            if (mutations or kind)
            else ()
        )
        return HibernatePassResult(attempts=attempts, mutations=mutations, empty_pass=not attempts)
    return leg


class _FoldHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=store_path)
        self.outbox = CallbackOutbox(path=store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "supervisor-lease.sqlite")

    def _ws(self, wsid):
        return SupervisedWorkspace(workspace_id=wsid, canonical_path=str(self.dir / wsid))

    def _supervisor(self, *, leg=None, workspaces, roster_fn=None, holder="superX",
                    release_after=True):
        return WorkspaceCallbackSupervisor(
            holder=holder,
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: workspaces,
            roster_fn=roster_fn or (lambda ws: ((), "")),  # empty roster -> no active issues
            redmine_source_fn=_boom_source,
            sender_fn=lambda ws: (lambda row: "delivered"),
            clock=lambda: CLOCK,
            release_after=release_after,
            hibernate_leg_fn=leg,
        )


class FoldedRunOnceTest(_FoldHarness):
    def test_bounded_reconciliation_folds_the_hibernate_leg(self) -> None:
        calls: list = []
        report = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsA")]
        ).run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        self.assertEqual(calls, ["wsA"])  # the leg ran as the after-stage of the reconcile pass
        outcome = report.workspaces[0]
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 1)
        self.assertEqual(report.hibernate_applied, 1)

    def test_local_wake_folds_the_hibernate_leg(self) -> None:
        calls: list = []
        report = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsA")]
        ).run_once(mode=SUPERVISION_LOCAL_WAKE)
        self.assertEqual(calls, ["wsA"])
        self.assertTrue(report.workspaces[0].hibernate_ran)

    def test_local_drain_never_folds_the_hibernate_leg(self) -> None:
        calls: list = []
        self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")]).run_once(
            mode=SUPERVISION_LOCAL_DRAIN
        )
        self.assertEqual(calls, [])  # #14150 provider-free drain never mixes hibernate

    def test_the_fold_shares_one_mutation_budget_across_workspaces(self) -> None:
        calls: list = []
        report = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsB"), self._ws("wsA")]
        ).run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        # Deterministic order (wsA before wsB): the first mutates, the second is budget-deferred
        # WITHOUT its leg ever running (zero extra actuation).
        self.assertEqual(calls, ["wsA"])
        self.assertEqual(report.hibernate_applied, 1)
        by_id = {w.workspace_id: w for w in report.workspaces}
        self.assertTrue(by_id["wsA"].hibernate_ran)
        self.assertFalse(by_id["wsB"].hibernate_ran)


class FoldDeferTest(_FoldHarness):
    """fold_hibernate_stage defer semantics pinned directly over crafted base outcomes."""

    def _base(self, wsid, **kw):
        base = dict(workspace_id=wsid, lease_acquired=True, lease_reason="granted_fresh")
        base.update(kw)
        return WorkspaceSupervisionOutcome(**base)

    def test_defers_when_the_workspace_delivered_a_callback(self) -> None:
        # "callback/outbox delivery ... の優先度を先に保つ" — a prior leg that mutated (delivered)
        # this pass defers hibernate for that workspace (its leg never runs).
        calls: list = []
        sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
        # A base outcome that DELIVERED (via a fake issue outcome would be heavy; deliver via backlog).
        base = self._base("wsA", backlog_delivered=1)
        merged = fold_mod.fold_hibernate_stage(sup, [base])
        self.assertEqual(calls, [])  # delivered -> hibernate deferred, leg untouched
        self.assertFalse(merged[0].hibernate_ran)

    def test_defers_on_an_uncertain_delivery(self) -> None:
        for skip in (SKIP_LEASE_LOST, SKIP_ROSTER_UNREADABLE):
            with self.subTest(skip=skip):
                calls: list = []
                sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
                base = self._base("wsA", skipped_reason=skip)
                fold_mod.fold_hibernate_stage(sup, [base])
                self.assertEqual(calls, [])  # uncertain -> no blind continuation

    def test_runs_on_a_quiet_lease_held_workspace(self) -> None:
        calls: list = []
        sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
        base = self._base("wsA")  # no delivery, no uncertainty
        merged = fold_mod.fold_hibernate_stage(sup, [base])
        self.assertEqual(calls, ["wsA"])
        self.assertTrue(merged[0].hibernate_ran)

    def test_no_lease_workspace_is_left_untouched(self) -> None:
        calls: list = []
        sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
        base = self._base("wsA", lease_acquired=False, skipped_reason="lease_refused_by_other")
        merged = fold_mod.fold_hibernate_stage(sup, [base])
        self.assertEqual(calls, [])
        self.assertEqual(merged[0], base)  # unchanged


class HibernateObservabilityRollupTest(_FoldHarness):
    def test_report_rolls_up_the_hibernate_metrics_and_empty_pass(self) -> None:
        applied = WorkspaceSupervisionOutcome(
            workspace_id="wsA", lease_acquired=True, lease_reason="g",
            hibernate_ran=True, hibernate_mutations=1,
            hibernate_attempts=(
                {"issue": "1", "lane": "l", "kind": "actuated", "reason": "", "revision": 2},
            ),
        )
        blocked = WorkspaceSupervisionOutcome(
            workspace_id="wsB", lease_acquired=True, lease_reason="g",
            hibernate_ran=True, hibernate_mutations=0,
            hibernate_attempts=(
                {"issue": "2", "lane": "m", "kind": "blocked", "reason": "lane_hibernated",
                 "revision": 0},
            ),
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
            SupervisorReport,
        )

        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=(applied, blocked)
        )
        self.assertEqual(report.hibernate_applied, 1)
        self.assertEqual(report.hibernate_released_capacity, 1)
        self.assertEqual(report.hibernate_candidates, 2)
        self.assertEqual(report.hibernate_blocked, 1)
        self.assertEqual(report.hibernate_closed_reasons, ("lane_hibernated",))
        self.assertFalse(report.empty_pass)  # a hibernate mutation is NOT an empty pass
        payload = report.as_payload()
        self.assertIn("hibernate", payload)
        self.assertEqual(payload["hibernate"]["applied"], 1)
        self.assertEqual(payload["hibernate"]["released_capacity"], 1)

    def test_a_pass_that_only_evaluated_deferred_candidates_is_not_empty(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
            SupervisorReport,
        )

        deferred = WorkspaceSupervisionOutcome(
            workspace_id="wsA", lease_acquired=True, lease_reason="g",
            hibernate_ran=True, hibernate_mutations=0,
            hibernate_attempts=(
                {"issue": "1", "lane": "l", "kind": "deferred", "reason": "x", "revision": 0},
            ),
        )
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=(deferred,)
        )
        self.assertFalse(report.empty_pass)  # an attempt was evaluated -> not empty


class NoStandaloneActivationSurfaceTest(unittest.TestCase):
    """Answer j#87108 §3: the CLI / event pump / scheduler adapter must NEVER select the standalone
    SUPERVISION_HIBERNATE mode or expose a public --hibernate action / third scheduler cadence."""

    def _src(self, module):
        return inspect.getsource(module)

    def test_the_cli_never_selects_the_standalone_hibernate_mode(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_workflow_supervisor,
        )

        text = self._src(cli_workflow_supervisor)
        self.assertNotIn("SUPERVISION_HIBERNATE", text)
        self.assertNotIn("--hibernate", text)

    def test_the_event_pump_never_selects_the_standalone_hibernate_mode(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            reconcile_event_pump,
        )

        self.assertNotIn("SUPERVISION_HIBERNATE", self._src(reconcile_event_pump))

    def test_the_launchd_adapter_has_no_third_hibernate_agent(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_launchd,
        )

        text = self._src(supervisor_launchd)
        self.assertNotIn("--hibernate", text)
        self.assertNotIn("HIBERNATE_AGENT", text)
        # Owned dual-agent lifecycle preserved (reconcile + drain only).
        self.assertEqual(len(supervisor_launchd.SUPERVISOR_AGENTS), 2)


class HibernateRollupVocabularyDriftGuardTest(unittest.TestCase):
    """The domain roll-up classifies the application leg's attempt-kind tokens; pin they agree."""

    def test_the_classified_kinds_are_real_leg_tokens(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            hibernate_actuation_leg as leg,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            hibernate_report_rollup as roll,
        )

        leg_kinds = {
            getattr(leg, name) for name in dir(leg)
            if name.startswith("ATTEMPT_") and isinstance(getattr(leg, name), str)
        }
        classified = (
            roll._HIBERNATE_APPLIED_KINDS | roll._HIBERNATE_BLOCKED_KINDS
            | roll._HIBERNATE_UNCERTAIN_KINDS | roll._HIBERNATE_DEFERRED_KINDS
        )
        # Every classified token is a real leg attempt-kind (no drift / typo).
        self.assertTrue(classified <= leg_kinds, classified - leg_kinds)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
