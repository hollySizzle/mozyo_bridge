"""HandoffCallbackSender tests (Redmine #13520 / US #13518).

The one-send adapter maps an injected handoff ``(status, reason)`` onto the closed send
outcome and is fail-safe: a ``send_fn`` that raises is uncertain (never auto-retried).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffCallbackSender,
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
)


def _row() -> CallbackOutboxRow:
    return CallbackOutboxRow(
        source="redmine",
        issue="13518",
        journal="75094",
        normalized_gate="implementation_done",
        callback_route="coordinator",
        state="inflight",
        attempts=0,
        max_attempts=3,
        send_attempted=True,
        notification_kind="implementation_done",
        notification_summary="",
        gate_mismatch=False,
        detail="",
        payload="",
    )


class HandoffCallbackSenderTest(unittest.TestCase):
    def test_delivered_maps_from_sent_ok(self):
        sender = HandoffCallbackSender(lambda row: HandoffDeliveryResult("sent", "ok"))
        self.assertEqual(sender(_row()), SEND_DELIVERED)

    def test_not_sent_maps_from_deterministic_block(self):
        sender = HandoffCallbackSender(lambda row: HandoffDeliveryResult("blocked", "invalid_args"))
        self.assertEqual(sender(_row()), SEND_NOT_SENT)

    def test_uncertain_maps_from_ambiguous_block(self):
        sender = HandoffCallbackSender(
            lambda row: HandoffDeliveryResult("blocked", "turn_start_unconfirmed")
        )
        self.assertEqual(sender(_row()), SEND_UNCERTAIN)

    def test_post_injection_block_is_uncertain_not_retryable(self):
        # #13520 review F2: a post-injection rail outcome must not become a retryable not_sent.
        for reason in ("receiver_blocked", "turn_start_absent"):
            with self.subTest(reason=reason):
                sender = HandoffCallbackSender(
                    lambda row, r=reason: HandoffDeliveryResult("blocked", r)
                )
                self.assertEqual(sender(_row()), SEND_UNCERTAIN)

    def test_send_fn_exception_is_fail_safe_uncertain(self):
        def boom(row):
            raise RuntimeError("mid-send explosion")

        sender = HandoffCallbackSender(boom)
        self.assertEqual(sender(_row()), SEND_UNCERTAIN)

    def test_send_fn_is_invoked_once_per_row(self):
        calls = []
        sender = HandoffCallbackSender(
            lambda row: calls.append(row) or HandoffDeliveryResult("sent", "ok")
        )
        sender(_row())
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
