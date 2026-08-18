"""Redmine #15707 (c) — a busy-coordinator dead-letter must be explicitly re-deliverable.

The fixed symptom (#15700 j#107939 / #15702 j#107933): a coordinator (Claude Code) mid-turn
makes every callback injection a ``precondition_not_idle`` zero-send; three bounded retries
exhaust the budget and the row terminalizes to ``dead_letter`` — after which NO command could
ever deliver it (``--run-once`` claims only ``pending``; the #13974 invariants forbid replay /
restart resurrection), so a legitimately deliverable review callback was permanently lost to
the automatic paths and the lane looked stalled.

Pins, end-to-end over the real processor + sender adapter:

1. the measured busy shape: ``precondition_not_idle`` zero-sends bounded-retry into
   ``dead_letter`` and the detail carries the normalized reason;
2. the structural exclusion (unchanged): a dead-lettered row is invisible to further deliver
   passes — the redrive is out-of-band, not a new automatic path;
3. the explicit fingerprint-gated redrive returns the row to ``pending`` and a normal deliver
   pass against the now-idle coordinator delivers it;
4. the redrive never bypasses: the redriven row flows through the same deliver claim, and a
   stale observation (wrong fingerprint) zero-writes.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import (  # noqa: E402
    REDRIVE_FINGERPRINT_MISMATCH,
    REDRIVE_REQUEUED,
    CallbackOutbox,
)
from mozyo_bridge.core.state.workflow_runtime_store import (  # noqa: E402
    CALLBACK_DEAD_LETTER,
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (  # noqa: E402,E501
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (  # noqa: E402,E501
    HandoffCallbackSender,
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E402,E501
    RedmineJournalEntry,
)

ISSUE = "15700"
JOURNAL = "107938"


class _FakeSource:
    def __init__(self, entries):
        self._entries = entries

    def read_entries(self, issue_id):
        return self._entries.get(str(issue_id), [])


def _busy_sender():
    """The measured coordinator-mid-turn shape: a deterministic pre-injection zero-send."""
    return HandoffCallbackSender(
        lambda row: HandoffDeliveryResult(
            "blocked", "precondition_not_idle", injection_stage="not_sent"
        )
    )


def _idle_sender(sent):
    return HandoffCallbackSender(
        lambda row: sent.append(row) or HandoffDeliveryResult("sent", "ok", injection_stage="submitted_confirmed")
    )


class BusyDeadLetterRedriveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(path=Path(self._tmp.name) / "workflow-runtime.sqlite")
        source = _FakeSource(
            {
                ISSUE: [
                    RedmineJournalEntry(
                        issue_id=ISSUE,
                        journal_id=JOURNAL,
                        notes="[mozyo:workflow-event:gate=review_result]",
                    )
                ]
            }
        )
        self.processor = CallbackOutboxProcessor(self.outbox, source)
        self.processor.ingest(
            [CallbackCandidate(ISSUE, JOURNAL, "coordinator", "review_result")]
        )

    def _exhaust_against_busy_coordinator(self) -> None:
        busy = _busy_sender()
        for _ in range(3):  # the default bounded budget
            self.processor.deliver(busy)

    def test_busy_retries_dead_letter_with_the_normalized_reason(self) -> None:
        self._exhaust_against_busy_coordinator()
        row = self.outbox.read()[0]
        self.assertEqual(row.state, CALLBACK_DEAD_LETTER)
        self.assertIn("precondition_not_idle", row.detail)

    def test_dead_letter_stays_invisible_to_automatic_delivery(self) -> None:
        self._exhaust_against_busy_coordinator()
        sent: list = []
        report = self.processor.deliver(_idle_sender(sent))
        self.assertEqual(sent, [])
        self.assertEqual(report.delivered, [])

    def test_explicit_redrive_then_idle_delivery_completes_the_callback(self) -> None:
        self._exhaust_against_busy_coordinator()
        row, fingerprint = self.outbox.dead_letter_fingerprints()[0]
        # A stale observation zero-writes; the exact one requeues.
        self.assertEqual(
            self.outbox.requeue_dead_letter(row.key, expect_fingerprint="stale"),
            REDRIVE_FINGERPRINT_MISMATCH,
        )
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)
        self.assertEqual(
            self.outbox.requeue_dead_letter(row.key, expect_fingerprint=fingerprint),
            REDRIVE_REQUEUED,
        )
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_PENDING)
        # The redriven row is delivered by the NORMAL fenced pass once the coordinator is idle
        # — the redrive re-admitted it, nothing was sent out-of-band.
        sent: list = []
        self.processor.deliver(_idle_sender(sent))
        self.assertEqual([r.journal for r in sent], [JOURNAL])
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_a_redriven_row_that_stays_busy_dead_letters_again_bounded(self) -> None:
        self._exhaust_against_busy_coordinator()
        row, fingerprint = self.outbox.dead_letter_fingerprints()[0]
        self.outbox.requeue_dead_letter(row.key, expect_fingerprint=fingerprint)
        # The grant is ONE fresh bounded budget, not an unbounded retry loop.
        for _ in range(8):
            self.processor.deliver(_busy_sender())
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)


if __name__ == "__main__":
    unittest.main()
