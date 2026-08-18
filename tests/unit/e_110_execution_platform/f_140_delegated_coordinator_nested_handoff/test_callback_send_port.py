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
    RECEIVER_PROVIDER_UNRESOLVED,
    HandoffCallbackSendPort,
)


def _row(route="coordinator", workspace_id="", target_receiver="codex"):
    # target_receiver defaults to a stamped provider token so these hermetic tests never fall
    # through to the live coordinator role-authority resolver (#15707); the receiver-derivation
    # tests below override it explicitly.
    return CallbackOutboxRow(
        source="redmine", issue="13518", journal="75094",
        normalized_gate="implementation_done", callback_route=route, state="inflight",
        attempts=0, max_attempts=3, send_attempted=True, notification_kind="implementation_done",
        notification_summary="", gate_mismatch=False, detail="", payload="", workspace_id=workspace_id,
        target_receiver=target_receiver,
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

    def test_attested_sender_refuses_a_row_with_no_workspace(self):
        # #13518 review R3-F3: an attested sender requires an EXACT workspace match — a row with no
        # workspace id (a legacy / unattested row) is refused, never routed on ambient env.
        calls = []
        port = HandoffCallbackSendPort(
            runner=lambda argv: calls.append(argv) or (0, '{"status": "sent", "reason": "ok"}'),
            attested_workspace_id="A",
        )
        result = port(_row(workspace_id=""))
        self.assertEqual((result.status, result.reason), ("blocked", "workspace_unattested_row"))
        self.assertEqual(calls, [])  # nothing was fired for the unattested row


class CoordinatorReceiverDerivationTest(unittest.TestCase):
    """#15707 (a): a coordinator-route row derives ``--to`` from the role authority.

    j#108012 measured the hardcoded ``--to codex`` contradicting the claude pane the same
    ``coordinator`` route resolved to after the coordinator rebind (#13229) — refused as
    ``invalid_args`` by the receiver-binding fence. The port now derives the token: stamped
    ``target_receiver`` first, else the injected coordinator role-authority resolver,
    fail-closed (never a silent ``codex`` guess) when neither yields a provider token.
    """

    def _capture_port(self, **kwargs):
        calls = []
        port = HandoffCallbackSendPort(
            runner=lambda argv: calls.append(argv) or (0, '{"status": "sent", "reason": "ok"}'),
            **kwargs,
        )
        return port, calls

    def _to_value(self, argv):
        return argv[argv.index("--to") + 1]

    def test_stamped_claude_receiver_wins(self):
        port, calls = self._capture_port()
        result = port(_row(target_receiver="claude"))
        self.assertEqual(result.status, "sent")
        self.assertEqual(self._to_value(calls[0]), "claude")

    def test_stamped_codex_receiver_wins(self):
        port, calls = self._capture_port()
        port(_row(target_receiver="codex"))
        self.assertEqual(self._to_value(calls[0]), "codex")

    def test_blank_stamp_falls_back_to_role_authority_resolver(self):
        port, calls = self._capture_port(coordinator_provider_resolver=lambda: "claude")
        result = port(_row(target_receiver=""))
        self.assertEqual(result.status, "sent")
        self.assertEqual(self._to_value(calls[0]), "claude")

    def test_non_provider_stamp_falls_back_to_resolver(self):
        # A semantic role token (not a provider) is not a valid --to; the role authority answers.
        port, calls = self._capture_port(coordinator_provider_resolver=lambda: "codex")
        port(_row(target_receiver="coordinator"))
        self.assertEqual(self._to_value(calls[0]), "codex")

    def test_resolver_failure_is_a_typed_pre_send_refusal(self):
        def unresolved():
            raise RuntimeError("role authority unavailable")

        port, calls = self._capture_port(coordinator_provider_resolver=unresolved)
        result = port(_row(target_receiver=""))
        self.assertEqual(
            (result.status, result.reason), ("blocked", RECEIVER_PROVIDER_UNRESOLVED)
        )
        # The port refused before invoking the handoff: a deterministic zero-send, so the
        # producer stage token classifies it as bounded-retry not_sent (never uncertain).
        self.assertEqual(result.injection_stage, "not_sent")
        self.assertEqual(calls, [])

    def test_resolver_returning_non_provider_token_refuses(self):
        port, calls = self._capture_port(coordinator_provider_resolver=lambda: "gpt")
        result = port(_row(target_receiver=""))
        self.assertEqual(
            (result.status, result.reason), ("blocked", RECEIVER_PROVIDER_UNRESOLVED)
        )
        self.assertEqual(calls, [])

    def test_non_coordinator_route_keeps_the_codex_literal(self):
        # #13758 R2-F2 disposition preserved: a worker row's provider flows via the
        # resolver-backed background sender, never this port — even a stamped claude
        # target_receiver does not flip this port's token for a non-coordinator route.
        port, calls = self._capture_port(
            coordinator_provider_resolver=lambda: (_ for _ in ()).throw(AssertionError(
                "the coordinator resolver must not be consulted for a non-coordinator route"
            ))
        )
        port(_row(route="implementation_worker", target_receiver="claude"))
        self.assertEqual(self._to_value(calls[0]), "codex")


if __name__ == "__main__":
    unittest.main()
