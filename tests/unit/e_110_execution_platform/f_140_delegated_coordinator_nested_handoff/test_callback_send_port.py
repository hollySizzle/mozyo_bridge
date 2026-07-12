"""HandoffCallbackSendPort tests (Redmine #13520 review F1).

The real send port shells out to ``mozyo-bridge handoff send`` once, parses the structured
DeliveryOutcome, and is fail-safe: a runner failure / unparseable output -> conservative
``blocked`` (never a crash, never an optimistic delivered).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_send_port import (
    HandoffCallbackSendPort,
)


def _row(route="coordinator", workspace_id=""):
    return CallbackOutboxRow(
        source="redmine", issue="13518", journal="75094",
        normalized_gate="implementation_done", callback_route=route, state="inflight",
        attempts=0, max_attempts=3, send_attempted=True, notification_kind="implementation_done",
        notification_summary="", gate_mismatch=False, detail="", payload="", workspace_id=workspace_id,
    )


class HandoffCallbackSendPortTest(unittest.TestCase):
    def test_parses_sent_ok_outcome(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return 0, '{"status": "sent", "reason": "ok"}'
        port = HandoffCallbackSendPort(runner=runner)
        result = port(_row())
        self.assertEqual((result.status, result.reason), ("sent", "ok"))
        # It fired the sanctioned handoff to the row's callback route with its anchor, once.
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertIn("handoff", argv)
        self.assertIn("send", argv)
        self.assertIn("coordinator", argv)
        self.assertIn("75094", argv)
        # F1-R1: the callback outcome is persisted durably through the sanctioned path.
        self.assertIn("--persist-delivery", argv)

    def test_parses_blocked_outcome(self):
        port = HandoffCallbackSendPort(
            runner=lambda argv: (1, 'noise\n{"status": "blocked", "reason": "invalid_args"}')
        )
        result = port(_row())
        self.assertEqual((result.status, result.reason), ("blocked", "invalid_args"))

    def test_runner_exception_is_fail_safe(self):
        def boom(argv):
            raise OSError("no such binary")
        result = HandoffCallbackSendPort(runner=boom)(_row())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "inject_failed")  # -> uncertain, never a crash

    def test_unparseable_output_is_fail_safe_uncertain(self):
        result = HandoffCallbackSendPort(runner=lambda argv: (0, "no json here"))(_row())
        # A clean rc without a structured outcome cannot confirm a turn-start -> uncertain.
        self.assertEqual((result.status, result.reason), ("blocked", "turn_start_unconfirmed"))

    def test_persist_receipt_is_surfaced_as_observable_evidence(self):
        # #13520 review F6: the port parses the --persist-delivery receipt (distinct JSON line)
        # and surfaces it as evidence, without affecting the send outcome.
        stdout = (
            '{"status": "sent", "reason": "ok"}\n'
            '{"persisted": true, "reason": "ok", "record_class": "delivery_notification", '
            '"provider": "redmine", "location": "redmine:issue=13518:journal=75094"}'
        )
        result = HandoffCallbackSendPort(runner=lambda argv: (0, stdout))(_row())
        self.assertEqual((result.status, result.reason), ("sent", "ok"))
        self.assertTrue(result.persist_ok)
        self.assertEqual(result.persist_reason, "ok")

    def test_delivered_not_gated_on_failed_persist(self):
        # A confirmed turn-start is still `sent/ok` even when the durable Redmine receipt did NOT
        # persist (outbox is the durability authority; the receipt is best-effort evidence).
        stdout = (
            '{"status": "sent", "reason": "ok"}\n'
            '{"persisted": false, "reason": "write_optin_unset", '
            '"record_class": "delivery_notification", "provider": "redmine", "location": null}'
        )
        result = HandoffCallbackSendPort(runner=lambda argv: (0, stdout))(_row())
        self.assertEqual((result.status, result.reason), ("sent", "ok"))  # NOT gated on persist
        self.assertFalse(result.persist_ok)
        self.assertEqual(result.persist_reason, "write_optin_unset")

    def test_no_receipt_line_leaves_persist_evidence_unknown(self):
        result = HandoffCallbackSendPort(
            runner=lambda argv: (0, '{"status": "sent", "reason": "ok"}')
        )(_row())
        self.assertIsNone(result.persist_ok)
        self.assertEqual(result.persist_reason, "")

    def test_foreign_workspace_row_is_refused_before_send(self):
        # #13520 review R2-F5: a sender attested for workspace A never routes workspace B's row.
        calls = []
        port = HandoffCallbackSendPort(
            runner=lambda argv: calls.append(argv) or (0, '{"status": "sent", "reason": "ok"}'),
            attested_workspace_id="A",
        )
        result = port(_row(workspace_id="B"))
        self.assertEqual((result.status, result.reason), ("blocked", "workspace_mismatch"))
        self.assertEqual(calls, [])  # no handoff was fired for the foreign workspace's row

    def test_matching_workspace_row_sends(self):
        port = HandoffCallbackSendPort(
            runner=lambda argv: (0, '{"status": "sent", "reason": "ok"}'), attested_workspace_id="A",
        )
        self.assertEqual(port(_row(workspace_id="A")).status, "sent")

    def test_unpinned_sender_is_backcompat(self):
        # attested_workspace_id="" (default) skips the pin — single-workspace / legacy behavior.
        port = HandoffCallbackSendPort(runner=lambda argv: (0, '{"status": "sent", "reason": "ok"}'))
        self.assertEqual(port(_row(workspace_id="B")).status, "sent")


if __name__ == "__main__":
    unittest.main()
