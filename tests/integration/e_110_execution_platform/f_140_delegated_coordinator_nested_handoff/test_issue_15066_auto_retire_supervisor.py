"""Supervisor ordering and shared-budget integration tests for Redmine #15066."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    HibernatePassResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_retire_leg import (  # noqa: E501
    RetireAttempt,
    RetirePassResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MappingRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_HIBERNATE_BUDGET_DEFERRED,
    SKIP_RETIRE_BUDGET_DEFERRED,
    SKIP_RETIRE_DELIVERY_UNCERTAIN,
    SUPERVISION_BOUNDED_RECONCILIATION,
)


class AutoRetireSupervisorIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        runtime = self.root / "runtime.sqlite"
        self.lease_store = SupervisorLeaseStore(path=self.root / "lease.sqlite")
        self.store = WorkflowRuntimeStore(path=runtime)
        self.outbox = CallbackOutbox(path=runtime)
        self.ws = SupervisedWorkspace(
            workspace_id="wsA", canonical_path=str(self.root / "repo")
        )

    def _supervisor(
        self, *, roster=(), source=None, sender=None, retire=None, hibernate=None,
        auto_integration=None, reconcile=None,
    ):
        return WorkspaceCallbackSupervisor(
            holder="supervisor-A",
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws],
            roster_fn=lambda _ws: (tuple(roster), ""),
            redmine_source_fn=lambda _ws: source,
            sender_fn=lambda _ws: sender or (lambda _row: "delivered"),
            clock=lambda: "2026-08-07T00:00:00+00:00",
            reconcile_leg_fn=reconcile,
            retire_leg_fn=retire,
            hibernate_leg_fn=hibernate,
            auto_integration_leg_fn=auto_integration,
        )

    def test_retire_runs_on_held_lease_before_hibernate_and_spends_budget(self) -> None:
        order = []

        def retire(_ws, renew, _budget, *, restrict_issues=None):
            self.assertIsNone(restrict_issues)
            lease = self.lease_store.holder_of("wsA")
            self.assertIsNotNone(lease)
            self.assertEqual(lease.holder, "supervisor-A")
            self.assertTrue(renew())
            order.append("retire")
            return RetirePassResult(
                attempts=(
                    RetireAttempt(
                        issue="15066",
                        lane="issue_15066_auto_retire",
                        lane_generation=2,
                        revision=4,
                        state="retired",
                        mutated=True,
                    ),
                )
            )

        def hibernate(*_args, **_kwargs):
            order.append("hibernate")
            return HibernatePassResult()

        report = self._supervisor(retire=retire, hibernate=hibernate).run_once(
            mode=SUPERVISION_BOUNDED_RECONCILIATION
        )
        outcome = report.workspaces[0]
        self.assertEqual(order, ["retire"])
        self.assertEqual(outcome.retire_mutations, 1)
        self.assertEqual(
            outcome.hibernate_disposition, SKIP_HIBERNATE_BUDGET_DEFERRED
        )
        self.assertIsNone(self.lease_store.holder_of("wsA"))

    def test_callback_delivery_keeps_priority_and_defers_retire(self) -> None:
        source = MappingRedmineJournalSource(
            payload={
                "issue": {"id": "15066"},
                "journals": [
                    {
                        "id": "100700",
                        "notes": (
                            "## Gate: review_request\n"
                            "[mozyo:workflow-event:gate=review_request:conclusion=pending]"
                        ),
                    }
                ],
            }
        )
        retire_calls = []

        def retire(*args, **kwargs):
            retire_calls.append((args, kwargs))
            return RetirePassResult()

        report = self._supervisor(
            roster=("15066",), source=source, retire=retire
        ).run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        outcome = report.workspaces[0]
        self.assertEqual(outcome.delivered, 1)
        self.assertEqual(retire_calls, [])
        self.assertEqual(outcome.retire_disposition, SKIP_RETIRE_BUDGET_DEFERRED)

    def test_uncertain_auto_integration_stops_retire(self) -> None:
        source = MappingRedmineJournalSource(
            payload={"issue": {"id": "15066"}, "journals": []}
        )
        retire_calls = []

        def uncertain_auto_integration(*_args):
            raise RuntimeError("boom")

        report = self._supervisor(
            roster=("15066",),
            source=source,
            retire=lambda *args, **kwargs: retire_calls.append((args, kwargs)),
            auto_integration=uncertain_auto_integration,
        ).run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)

        outcome = report.workspaces[0]
        self.assertEqual(retire_calls, [])
        self.assertEqual(
            outcome.retire_disposition, SKIP_RETIRE_DELIVERY_UNCERTAIN
        )

    def test_uncertain_reconcile_stops_retire(self) -> None:
        source = MappingRedmineJournalSource(
            payload={"issue": {"id": "15066"}, "journals": []}
        )
        retire_calls = []

        def uncertain_reconcile(*_args):
            raise RuntimeError("boom")

        report = self._supervisor(
            roster=("15066",),
            source=source,
            retire=lambda *args, **kwargs: retire_calls.append((args, kwargs)),
            reconcile=uncertain_reconcile,
        ).run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)

        outcome = report.workspaces[0]
        self.assertEqual(retire_calls, [])
        self.assertEqual(
            outcome.retire_disposition, SKIP_RETIRE_DELIVERY_UNCERTAIN
        )


if __name__ == "__main__":
    unittest.main()
