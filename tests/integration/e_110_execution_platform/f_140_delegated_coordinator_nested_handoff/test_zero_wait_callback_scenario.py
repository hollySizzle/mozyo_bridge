"""Zero-wait / mozyo-only coordinator flow scenario (Redmine #13521 / US #13518).

One cohesive end-to-end scenario over the public callback surface (store + classifier +
orchestrator + CLI facade), asserting the US #13518 Acceptance bullets as a narrative rather
than in isolation:

1. dispatch後、coordinatorはblocking waitせずturnを終了 — the outbox model never blocks; a
   handoff-worthy gate becomes a durable `pending` callback and the turn ends (no poll here).
2. handoff-worthy durable gate更新がcallbackとしてnew turnを起動 — a classified gate is
   delivered exactly once (the coordinator new-turn trigger).
3. watcher再起動・delivery failure・重複eventでもlost/duplicate actionが発生しない — a duplicate
   event across a restart enqueues no new row; an uncertain delivery is never auto-retried; a
   crashed inflight row recovers to pending (pre-send) without a duplicate send.
4. 通常E2Eに raw herdr wait/read/list/send・pane/tmux操作が出ない — the whole flow runs through
   the `workflow callbacks` mozyo facade; deliver fail-closes on the bare CLI.
5. callback本文が誤っていてもjournal再読によりreview/close等を誤承認しない — a notification that
   claims the wrong kind is delivered under the JOURNAL's gate (mismatch recorded), and a
   journal with no gate marker is dead-lettered (never delivered on a prose guess).
"""

from __future__ import annotations

import argparse
import json as _json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_callbacks as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
    SEND_UNCERTAIN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
)


class _FakeSource:
    def __init__(self, entries):
        self._entries = entries

    def read_entries(self, issue_id):
        return self._entries.get(str(issue_id), [])


def _entry(issue, journal, gate):
    return RedmineJournalEntry(
        issue_id=issue, journal_id=journal, notes=f"[mozyo:workflow-event:gate={gate}]"
    )


def _cli_args(**over):
    base = dict(
        json=False, store_path=None, sweep=False, ingest=False, deliver=False,
        candidate=None, redmine_json=None, poll=False, source_issue=None, since=None,
        cursor=None, limit=32,
    )
    base.update(over)
    return argparse.Namespace(**base)


class ZeroWaitCallbackScenarioTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "wf.sqlite"
        self.outbox = CallbackOutbox(path=self.store_path)
        # A source-of-truth Redmine issue with a real implementation_done gate on j#75094,
        # and a note on j#75200 that carries NO structured gate marker (prose only).
        self.source = _FakeSource(
            {
                "13518": [
                    _entry("13518", "75094", "implementation_done"),
                    RedmineJournalEntry("13518", "75200", "work continues, looks done to me"),
                ]
            }
        )
        self.processor = CallbackOutboxProcessor(self.outbox, self.source)

    def test_full_zero_wait_flow(self):
        # (1)+(2) A handoff-worthy gate becomes a durable pending callback (no blocking wait).
        #         A wrong-kind notification (claims review_request) and a marker-less journal
        #         are ingested together — the journal is the authority for both.
        ingest = self.processor.ingest(
            [
                CallbackCandidate("13518", "75094", "coordinator", "review_request"),  # wrong kind
                CallbackCandidate("13518", "75200", "coordinator", "review_request"),  # no marker
            ]
        )
        self.assertEqual(ingest.enqueued, 2)
        pending = self.outbox.read(states=[CALLBACK_PENDING])
        # (5) The wrong-kind notification is delivered under the JOURNAL's gate, mismatch flagged.
        self.assertEqual([(r.journal, r.normalized_gate) for r in pending], [("75094", "implementation_done")])
        self.assertTrue(pending[0].gate_mismatch)
        # (5) The marker-less journal is dead-lettered (never delivered on a prose guess).
        dead = self.outbox.read(states=[CALLBACK_DEAD_LETTER])
        self.assertEqual([r.journal for r in dead], ["75200"])

        # (3) Duplicate event across a "watcher restart" (fresh processor, same store) -> no new row.
        restart = CallbackOutboxProcessor(CallbackOutbox(path=self.store_path), self.source)
        again = restart.ingest([CallbackCandidate("13518", "75094", "coordinator", "review_request")])
        self.assertEqual(again.enqueued, 0)

        # (3) First delivery attempt is uncertain (ACK-only) -> NOT auto-retried on the next pass.
        first = self.processor.deliver(lambda row: SEND_UNCERTAIN)
        self.assertEqual([d.resulting_state for d in first.delivered], ["uncertain"])
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())  # nothing left pending
        sent = []
        self.processor.deliver(lambda row: sent.append(row) or SEND_DELIVERED)
        self.assertEqual(sent, [])  # uncertain row is never re-claimed -> no duplicate action

    def test_crash_recovery_no_duplicate(self):
        self.processor.ingest([CallbackCandidate("13518", "75094", "coordinator", "implementation_done")])
        # Simulate a crash right after claim (pre-send): row stuck inflight, no outcome.
        self.outbox.claim_pending()
        # A fresh deliver pass recovers it (pre-send -> pending) and delivers exactly once.
        # stale_seconds=0 treats the just-claimed row as abandoned (a real crash would be older).
        calls = []
        report = self.processor.deliver(
            lambda row: calls.append(row.journal) or SEND_DELIVERED, stale_seconds=0
        )
        self.assertEqual(report.recovered[0].state, CALLBACK_PENDING)
        self.assertEqual(calls, ["75094"])  # exactly one send
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_flows_through_mozyo_facade_only(self):
        # (4) The whole flow is reachable via `workflow callbacks`; the bare deliver fail-closes
        #     (no raw herdr / tmux primitive anywhere in the operator path).
        snapshot = Path(self._tmp.name) / "issue.json"
        snapshot.write_text(
            _json.dumps(
                {"issue": {"id": "13518", "journals": [
                    {"id": "75094", "notes": "[mozyo:workflow-event:gate=implementation_done]"}]}}
            ),
            encoding="utf-8",
        )
        rc = cli.cmd_workflow_callbacks(
            _cli_args(
                ingest=True, store_path=str(self.store_path), redmine_json=str(snapshot),
                candidate=[cli._parse_candidate("13518:75094:coordinator:implementation_done")],
            )
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)
        # A sweep surfaces the backlog and sends nothing.
        self.assertEqual(cli.cmd_workflow_callbacks(_cli_args(sweep=True, store_path=str(self.store_path))), 0)
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)
        # Deliver flows through the facade's real sender (the send port shells out to the
        # sanctioned `mozyo-bridge handoff send`, not a raw herdr/tmux primitive). Patched here
        # to keep the test hermetic; the live send is verified under #13490 with QA-only anchors.
        from mozyo_bridge.core.state.callback_outbox import CALLBACK_DELIVERED

        orig = cli._callback_sender
        cli._callback_sender = lambda args: (lambda row: SEND_DELIVERED)
        try:
            rc = cli.cmd_workflow_callbacks(_cli_args(deliver=True, store_path=str(self.store_path)))
        finally:
            cli._callback_sender = orig
        self.assertEqual(rc, 0)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)


if __name__ == "__main__":
    unittest.main()
