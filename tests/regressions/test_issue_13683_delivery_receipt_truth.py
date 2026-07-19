"""Delivery-receipt truth: the supervisor's ``delivered`` equals actual receiver wakes (Redmine #13683 R2).

Installed a16 (j#82329) supplied 22 events and reported ``delivered=2`` while the target same-lane
gateway stayed ``turn_ended`` — the ``delivered`` counter diverged from the receiver's durable state
because the supervisor counted ``len(deliver["delivered"])``, which is EVERY claimed row that reached
the send edge (delivered / uncertain / retry / reconciled-away), not the rows that positively woke a
receiver. This pins the corrected receipt binding: a claimed row counts as ``delivered`` ONLY when its
durable ``resulting_state`` is ``CALLBACK_DELIVERED``; a busy / ambiguous / unavailable receiver held
as a retryable (``pending``) or terminal (``uncertain``) receipt is ``blocked`` — a receipt held, not a
wake — across the active-issue pass, the workspace roll-up, the report, and the backlog drain. The
"root busy" state is represented in fixtures by the injected sender's send outcome (no live actuation).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
    WorkflowRuntimeStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    partition_delivery_receipts,
)

ISSUE = "13683"


def _review_request_payload(issue: str = ISSUE, journal: str = "82327") -> dict:
    return {
        "issue": {"id": issue},
        "journals": [
            {
                "id": journal,
                "notes": (
                    "## Gate: review_request\n"
                    "[mozyo:workflow-event:gate=review_request:conclusion=pending]"
                ),
            }
        ],
    }


class _FixedSender:
    """A callback sender that returns a fixed send outcome — the fixture stand-in for a busy receiver."""

    def __init__(self, outcome: str) -> None:
        self.calls: list = []
        self._outcome = outcome

    def __call__(self, row) -> str:
        self.calls.append(row)
        return self._outcome


class PartitionDeliveryReceiptsTest(unittest.TestCase):
    """The pure receipt classifier: only a durable ``delivered`` state is a delivery."""

    def test_dicts_partition_by_resulting_state(self) -> None:
        outcomes = [
            {"resulting_state": CALLBACK_DELIVERED},
            {"resulting_state": CALLBACK_UNCERTAIN},
            {"resulting_state": CALLBACK_PENDING},
        ]
        self.assertEqual(
            partition_delivery_receipts(outcomes, delivered_state=CALLBACK_DELIVERED), (1, 2)
        )

    def test_objects_partition_by_resulting_state(self) -> None:
        class _O:
            def __init__(self, state: str) -> None:
                self.resulting_state = state

        outcomes = [_O(CALLBACK_DELIVERED), _O(CALLBACK_DELIVERED), _O(CALLBACK_UNCERTAIN)]
        self.assertEqual(
            partition_delivery_receipts(outcomes, delivered_state=CALLBACK_DELIVERED), (2, 1)
        )

    def test_empty_is_zero_zero(self) -> None:
        self.assertEqual(partition_delivery_receipts([], delivered_state=CALLBACK_DELIVERED), (0, 0))
        self.assertEqual(partition_delivery_receipts(None, delivered_state=CALLBACK_DELIVERED), (0, 0))


class SupervisorReceiptTruthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "supervisor-lease.sqlite")
        self.source = MappingRedmineJournalSource(payload=_review_request_payload())
        self.ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))

    def _run(self, outcome: str):
        sender = _FixedSender(outcome)
        sup = WorkspaceCallbackSupervisor(
            holder="superX",
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws],
            roster_fn=lambda ws: ((ISSUE,), ""),
            redmine_source_fn=lambda ws: self.source,
            sender_fn=lambda ws: sender,
            clock=lambda: "2026-07-19T00:00:00+00:00",
        )
        report = sup.run_once()
        return report, sender

    def test_real_delivery_counts_delivered_zero_blocked(self) -> None:
        report, sender = self._run(SEND_DELIVERED)
        ws = report.workspaces[0]
        self.assertEqual(len(sender.calls), 1)  # the review_request was attempted once
        self.assertEqual(ws.issues[0].delivered, 1)
        self.assertEqual(ws.issues[0].blocked, 0)
        self.assertEqual(ws.delivered, 1)
        self.assertEqual(ws.blocked, 0)
        self.assertEqual(report.delivered, 1)
        self.assertEqual(report.blocked, 0)
        # The durable receipt agrees with the projected count.
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DELIVERED])), 1)

    def test_busy_post_injection_uncertain_is_blocked_not_delivered(self) -> None:
        # The a16 divergence: a busy receiver (marker/Enter issued, turn-start unconfirmed) is uncertain.
        report, sender = self._run(SEND_UNCERTAIN)
        ws = report.workspaces[0]
        self.assertEqual(len(sender.calls), 1)  # the send WAS attempted
        self.assertEqual(ws.issues[0].delivered, 0)  # but NOT counted as a delivery (the a16 bug)
        self.assertEqual(ws.issues[0].blocked, 1)
        self.assertEqual(ws.delivered, 0)
        self.assertEqual(ws.blocked, 1)
        self.assertEqual(report.delivered, 0)
        self.assertEqual(report.blocked, 1)
        # The receiver was NOT woken: the durable row is a held uncertain receipt, not delivered.
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DELIVERED])), 0)
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_UNCERTAIN])), 1)

    def test_busy_pre_injection_not_sent_is_blocked_and_held_retryable(self) -> None:
        # A busy receiver rejected before any injection is a deterministic not-sent -> bounded retry.
        report, sender = self._run(SEND_NOT_SENT)
        ws = report.workspaces[0]
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(ws.issues[0].delivered, 0)
        self.assertEqual(ws.issues[0].blocked, 1)
        self.assertEqual(report.delivered, 0)
        self.assertEqual(report.blocked, 1)
        # Held as a retryable pending receipt (re-claimable next sweep once the receiver is free) —
        # never counted as a delivery, never dropped.
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DELIVERED])), 0)
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)

    def test_payload_surfaces_blocked_for_diagnostic_parity(self) -> None:
        report, _sender = self._run(SEND_UNCERTAIN)
        payload = report.as_payload()
        self.assertEqual(payload["delivered"], 0)
        self.assertEqual(payload["blocked"], 1)
        ws_payload = payload["workspaces"][0]
        self.assertEqual(ws_payload["blocked"], 1)
        self.assertEqual(ws_payload["issues"][0]["blocked"], 1)
        self.assertEqual(ws_payload["issues"][0]["delivered"], 0)


if __name__ == "__main__":
    unittest.main()
