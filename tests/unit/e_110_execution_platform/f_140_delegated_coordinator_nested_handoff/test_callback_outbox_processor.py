"""Callback outbox processor tests (Redmine #13520 / US #13518, design answer j#75098).

End-to-end over ingest -> deliver -> sweep with an in-memory journal source + a fake sender,
covering the verification matrix:

- ingest classifies against the exact journal and enqueues (classified -> pending;
  unclassified -> dead_letter); a duplicate event / watcher restart enqueues no new row;
- deliver fires one send per claimed row and maps the outcome (delivered / known-not-sent
  bounded retry -> dead_letter / uncertain -> no auto-retry);
- a source read failure is fail-closed unclassified (dead_letter), never delivered;
- a notification-vs-journal mismatch is delivered under the journal gate with the flag set;
- sweep reconciles inflight rows and surfaces the pending + dead-letter backlog once.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    CallbackSendResult,
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    RedmineJournalSource,
)


class _FakeSource:
    """An in-memory :class:`RedmineJournalSource` with per-issue journal entries."""

    def __init__(self, entries: dict[str, list[RedmineJournalEntry]]) -> None:
        self._entries = entries

    def read_entries(self, issue_id: str):
        return self._entries.get(str(issue_id), [])


class _RaisingSource:
    def read_entries(self, issue_id: str):
        raise RuntimeError("redmine unreachable")


def _entry(issue: str, journal: str, gate: str) -> RedmineJournalEntry:
    return RedmineJournalEntry(
        issue_id=issue,
        journal_id=journal,
        notes=f"gate journal [mozyo:workflow-event:gate={gate}]",
    )


class _ProcessorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "workflow-runtime.sqlite"
        self.outbox = CallbackOutbox(path=self.path)

    def _processor(self, source: RedmineJournalSource) -> CallbackOutboxProcessor:
        return CallbackOutboxProcessor(self.outbox, source)


class IngestTest(_ProcessorTestCase):
    def test_classified_enqueues_pending_unclassified_dead_letters(self):
        source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        proc = self._processor(source)
        report = proc.ingest(
            [
                CallbackCandidate("13518", "75094", "coordinator", "implementation_done"),
                CallbackCandidate("13518", "99999", "coordinator", "implementation_done"),
            ]
        )
        self.assertEqual(report.enqueued, 2)
        self.assertEqual(report.dead_lettered, 1)
        pending = self.outbox.read(states=[CALLBACK_PENDING])
        dead = self.outbox.read(states=[CALLBACK_DEAD_LETTER])
        self.assertEqual([r.journal for r in pending], ["75094"])
        self.assertEqual([r.journal for r in dead], ["99999"])

    def test_duplicate_event_enqueues_one_row_across_restart(self):
        source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        cand = [CallbackCandidate("13518", "75094", "coordinator", "implementation_done")]
        self._processor(source).ingest(cand)
        # A watcher restart => a brand-new processor over the same durable store.
        again = CallbackOutboxProcessor(CallbackOutbox(path=self.path), source)
        report = again.ingest(cand)
        self.assertEqual(report.enqueued, 0)
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(len(self.outbox.read()), 1)

    def test_source_read_failure_is_fail_closed_dead_letter(self):
        proc = self._processor(_RaisingSource())
        report = proc.ingest(
            [CallbackCandidate("13518", "75094", "coordinator", "implementation_done")]
        )
        self.assertEqual(report.dead_lettered, 1)
        dead = self.outbox.read(states=[CALLBACK_DEAD_LETTER])
        self.assertEqual(len(dead), 1)
        self.assertIn("source_unreadable", dead[0].detail)

    def test_notification_mismatch_persists_flag_under_journal_gate(self):
        source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        proc = self._processor(source)
        proc.ingest(
            [CallbackCandidate("13518", "75094", "coordinator", "review_request")]
        )
        row = self.outbox.read()[0]
        self.assertEqual(row.normalized_gate, "implementation_done")
        self.assertTrue(row.gate_mismatch)

    def test_ingest_advances_cursor(self):
        source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        self._processor(source).ingest(
            [CallbackCandidate("13518", "75094", "coordinator", "implementation_done")],
            cursor="75094",
        )
        self.assertEqual(self.outbox.read_cursor("redmine"), "75094")


class DeliverTest(_ProcessorTestCase):
    def _ingest_one(self, gate: str = "implementation_done", journal: str = "75094"):
        source = _FakeSource({"13518": [_entry("13518", journal, gate)]})
        proc = self._processor(source)
        proc.ingest([CallbackCandidate("13518", journal, "coordinator", gate)])
        return proc

    def test_delivered(self):
        proc = self._ingest_one()
        report = proc.deliver(lambda row: SEND_DELIVERED)
        self.assertEqual([d.resulting_state for d in report.delivered], ["delivered"])
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_known_not_sent_bounded_retry_then_dead_letter(self):
        source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        proc = self._processor(source)
        # max_attempts is enqueue-time; ingest uses the default (3). Drive not_sent to exhaust.
        proc.ingest([CallbackCandidate("13518", "75094", "coordinator", "implementation_done")])
        states = []
        for _ in range(3):
            report = proc.deliver(lambda row: SEND_NOT_SENT)
            if report.delivered:
                states.append(report.delivered[0].resulting_state)
        # attempts 1,2 -> pending (re-claimed next pass); attempt 3 -> dead_letter.
        self.assertEqual(states, [CALLBACK_PENDING, CALLBACK_PENDING, CALLBACK_DEAD_LETTER])
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)

    def test_uncertain_is_not_auto_retried(self):
        proc = self._ingest_one()
        proc.deliver(lambda row: SEND_UNCERTAIN)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_UNCERTAIN)
        # A second deliver pass claims nothing (uncertain is not pending) -> no duplicate send.
        sent = []
        report = proc.deliver(lambda row: sent.append(row) or SEND_DELIVERED)
        self.assertEqual(sent, [])
        self.assertEqual(report.delivered, [])

    def test_unknown_send_token_is_fail_safe_uncertain(self):
        proc = self._ingest_one()
        proc.deliver(lambda row: "surprise")
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_UNCERTAIN)

    def test_persist_receipt_evidence_propagates_to_delivery_outcome(self):
        # #13520 review R2-F6: a CallbackSendResult's persist evidence reaches the report payload;
        # the delivered outcome is unchanged (the outbox row is the durability authority).
        proc = self._ingest_one()
        report = proc.deliver(
            lambda row: CallbackSendResult(SEND_DELIVERED, persist_ok=False, persist_reason="transport_error")
        )
        outcome = report.delivered[0]
        self.assertEqual(outcome.resulting_state, "delivered")  # NOT gated on the failed persist
        self.assertFalse(outcome.persist_ok)
        self.assertEqual(outcome.persist_reason, "transport_error")
        self.assertEqual(outcome.as_payload()["persist_reason"], "transport_error")

    def test_bare_string_sender_leaves_persist_evidence_absent(self):
        proc = self._ingest_one()
        report = proc.deliver(lambda row: SEND_DELIVERED)  # legacy string sender
        self.assertIsNone(report.delivered[0].persist_ok)
        self.assertEqual(report.delivered[0].persist_reason, "")

    def test_deliver_recovers_a_crashed_inflight_row_first(self):
        proc = self._ingest_one()
        # Simulate a crash mid-claim: claim leaves the row inflight, no outcome recorded.
        self.outbox.claim_pending()
        # A fresh deliver pass recovers it (pre-send -> pending) and then delivers it.
        # stale_seconds=0 treats the just-claimed row as abandoned (a real crash would be older).
        report = proc.deliver(lambda row: SEND_DELIVERED, stale_seconds=0)
        self.assertEqual(len(report.recovered), 1)
        self.assertEqual(report.recovered[0].state, CALLBACK_PENDING)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)


class OwnershipLostReportTest(_ProcessorTestCase):
    """F2-R1 (#13520 j#75167): the report reflects the ACTUAL persisted state after lease loss."""

    def test_lease_loss_mid_send_reports_reconciled_state_not_delivered(self):
        source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        proc = self._processor(source)
        proc.ingest([CallbackCandidate("13518", "75094", "coordinator", "implementation_done")])
        # A second processor over the same store; the sender simulates the lease expiring
        # mid-send so the second processor reconciles the row to uncertain (token cleared).
        other = CallbackOutbox(path=self.path)

        def racing_sender(row):
            other.recover_inflight(stale_seconds=0)
            return SEND_DELIVERED

        report = proc.deliver(racing_sender)
        outcome = report.delivered[0]
        # Double-send prevention holds, but the report must NOT claim delivered.
        self.assertEqual(outcome.resulting_state, CALLBACK_UNCERTAIN)
        self.assertTrue(outcome.ownership_lost)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_UNCERTAIN)

    def test_normal_delivery_reports_delivered_without_ownership_loss(self):
        source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        proc = self._processor(source)
        proc.ingest([CallbackCandidate("13518", "75094", "coordinator", "implementation_done")])
        outcome = proc.deliver(lambda row: SEND_DELIVERED).delivered[0]
        self.assertEqual(outcome.resulting_state, CALLBACK_DELIVERED)
        self.assertFalse(outcome.ownership_lost)


class SweepTest(_ProcessorTestCase):
    def test_sweep_surfaces_pending_and_dead_letter_and_recovers_inflight(self):
        source = _FakeSource(
            {
                "13518": [
                    _entry("13518", "75094", "implementation_done"),
                    _entry("13518", "75096", "review_request"),
                ]
            }
        )
        proc = self._processor(source)
        proc.ingest(
            [
                CallbackCandidate("13518", "75094", "coordinator", "implementation_done"),
                CallbackCandidate("13518", "75096", "coordinator", "review_request"),
                CallbackCandidate("13518", "99999", "coordinator", "implementation_done"),
            ]
        )
        # Leave 75096 inflight+post-send (a crash), 75094 pending, 99999 dead_letter.
        # Claim both pending rows, then only mark the send edge on 75096.
        self.outbox.claim_pending()
        self.outbox.mark_sending(
            CallbackOutboxKey("redmine", "13518", "75096", "review_request", "coordinator")
        )
        report = proc.sweep(stale_seconds=0)
        recovered_states = {r.journal: r.state for r in report.recovered}
        self.assertEqual(recovered_states["75094"], CALLBACK_PENDING)  # pre-send -> retry
        self.assertEqual(recovered_states["75096"], CALLBACK_UNCERTAIN)  # post-send -> uncertain
        self.assertEqual([r.journal for r in report.pending], ["75094"])
        self.assertEqual([r.journal for r in report.dead_letter], ["99999"])


if __name__ == "__main__":
    unittest.main()
