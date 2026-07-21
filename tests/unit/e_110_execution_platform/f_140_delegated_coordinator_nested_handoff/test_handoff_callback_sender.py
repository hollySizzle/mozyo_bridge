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
        self.assertEqual(sender(_row()).outcome, SEND_DELIVERED)

    def test_not_sent_maps_from_deterministic_block(self):
        sender = HandoffCallbackSender(lambda row: HandoffDeliveryResult("blocked", "invalid_args"))
        self.assertEqual(sender(_row()).outcome, SEND_NOT_SENT)

    def test_uncertain_maps_from_ambiguous_block(self):
        sender = HandoffCallbackSender(
            lambda row: HandoffDeliveryResult("blocked", "turn_start_unconfirmed")
        )
        self.assertEqual(sender(_row()).outcome, SEND_UNCERTAIN)

    def test_post_injection_block_is_uncertain_not_retryable(self):
        # #13520 review F2: a post-injection rail outcome must not become a retryable not_sent.
        for reason in ("receiver_blocked", "turn_start_absent"):
            with self.subTest(reason=reason):
                sender = HandoffCallbackSender(
                    lambda row, r=reason: HandoffDeliveryResult("blocked", r)
                )
                self.assertEqual(sender(_row()).outcome, SEND_UNCERTAIN)

    def test_persist_evidence_propagates_through_the_sender(self):
        # #13520 review R2-F6: the sender surfaces the send port's persist receipt evidence.
        sender = HandoffCallbackSender(
            lambda row: HandoffDeliveryResult("sent", "ok", persist_ok=False, persist_reason="write_optin_unset")
        )
        result = sender(_row())
        self.assertEqual(result.outcome, SEND_DELIVERED)  # outcome unchanged by evidence
        self.assertFalse(result.persist_ok)
        self.assertEqual(result.persist_reason, "write_optin_unset")

    def test_send_fn_exception_is_fail_safe_uncertain(self):
        def boom(row):
            raise RuntimeError("mid-send explosion")

        sender = HandoffCallbackSender(boom)
        self.assertEqual(sender(_row()).outcome, SEND_UNCERTAIN)

    def test_send_fn_is_invoked_once_per_row(self):
        calls = []
        sender = HandoffCallbackSender(
            lambda row: calls.append(row) or HandoffDeliveryResult("sent", "ok")
        )
        sender(_row())
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class SendReasonIsCarriedAndNormalized(unittest.TestCase):
    """Redmine #14248 review j#85410 F1 — the send edge's reason must survive, secret-safe.

    `send_outcome_for_delivery` consumed `result.reason` and the sender then DROPPED it, so a
    downstream reader saw `not_sent` with no way to tell an authorization refusal from a
    transport precondition. The installed smoke's diagnostic field was consequently pinned at
    `null`. These pin that it is carried, normalized to the closed allowlist, and never raw.
    """

    def _send(self, status, reason):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (  # noqa: E501
            HandoffCallbackSender, HandoffDeliveryResult,
        )

        sender = HandoffCallbackSender(
            lambda row: HandoffDeliveryResult(status=status, reason=reason)
        )
        return sender(object())

    def test_a_known_zero_send_reason_is_carried(self):
        result = self._send("blocked", "target_unavailable")
        self.assertEqual(result.outcome, "not_sent")
        self.assertEqual(result.send_reason, "target_unavailable")

    def test_an_unknown_reason_normalizes_to_the_fixed_token_and_drops_the_raw_value(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E501
            UNRECOGNIZED_ZERO_SEND_REASON,
        )

        secret = "/Users/someone/.mozyo_bridge/token-abc123"
        result = self._send("blocked", secret)
        self.assertEqual(result.send_reason, UNRECOGNIZED_ZERO_SEND_REASON)
        self.assertNotIn("token-abc123", result.send_reason)
        self.assertNotIn("/Users", result.send_reason)

    def test_send_reason_never_changes_the_outcome(self):
        # Observability only: the same status/reason pair maps to the same outcome as before.
        self.assertEqual(self._send("sent", "ok").outcome, "delivered")
        self.assertEqual(self._send("blocked", "marker_timeout").outcome, "uncertain")

    def test_delivery_outcome_payload_exposes_send_reason(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (  # noqa: E501
            DeliveryOutcome,
        )
        from mozyo_bridge.core.state.callback_outbox import CallbackOutboxKey

        key = CallbackOutboxKey(source="redmine", issue="1", journal="2", normalized_gate="g", callback_route="coordinator")
        payload = DeliveryOutcome(
            key=key, send_outcome="not_sent", resulting_state="pending",
            send_reason="target_unavailable",
        ).as_payload()
        self.assertIn("send_reason", payload)
        self.assertEqual(payload["send_reason"], "target_unavailable")
