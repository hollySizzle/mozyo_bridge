"""Unit tests for reconcile callback delivery routing (Redmine #13758 review F2).

Pins the semantic-receiver -> provider resolution and its effect on the REAL send port's
argv (the review required the routing to be verified through the send port, not only at row
creation): a worker self-heal is delivered ``--to claude`` (same-lane), every other / unknown
receiver stays ``--to codex`` (the pre-#13758 default; existing discovery / coordinator rows
are unchanged).
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_delivery_route import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    callback_receiver_provider,
)


def _row(*, callback_route, target_receiver="", normalized_gate="g"):
    return CallbackOutboxRow(
        source="redmine",
        issue="13758",
        journal="79337",
        normalized_gate=normalized_gate,
        callback_route=callback_route,
        state="inflight",
        attempts=0,
        max_attempts=3,
        send_attempted=False,
        notification_kind="self_heal",
        notification_summary="",
        gate_mismatch=False,
        detail="",
        payload="",
        workspace_id="ws1",
        target_receiver=target_receiver,
    )


class ProviderResolverTest(unittest.TestCase):
    def test_worker_receiver_resolves_to_claude(self):
        row = _row(callback_route="implementation_worker", target_receiver="implementation_worker")
        self.assertEqual(callback_receiver_provider(row), PROVIDER_CLAUDE)

    def test_coordinator_resolves_to_codex(self):
        self.assertEqual(callback_receiver_provider(_row(callback_route="coordinator")), PROVIDER_CODEX)

    def test_gateway_resolves_to_codex(self):
        row = _row(callback_route="implementation_gateway", target_receiver="implementation_gateway")
        self.assertEqual(callback_receiver_provider(row), PROVIDER_CODEX)

    def test_unknown_receiver_fails_closed_to_codex(self):
        self.assertEqual(callback_receiver_provider(_row(callback_route="review_return:lane-a")), PROVIDER_CODEX)

    def test_target_receiver_takes_precedence_over_route(self):
        # If the two disagree, the semantic receiver (target_receiver) binds the provider.
        row = _row(callback_route="coordinator", target_receiver="implementation_worker")
        self.assertEqual(callback_receiver_provider(row), PROVIDER_CLAUDE)


class SendPortArgvTest(unittest.TestCase):
    """The routing must reach the REAL send port argv (review F2), via an injected runner."""

    def _capture_argv(self, row):
        captured = {}

        def fake_runner(argv):
            captured["argv"] = argv
            return 0, '{"status": "delivered", "reason": "ok"}'

        HandoffCallbackSendPort(runner=fake_runner, attested_workspace_id="ws1")(row)
        return captured["argv"]

    def test_worker_self_heal_argv_is_to_claude(self):
        argv = self._capture_argv(
            _row(callback_route="implementation_worker", target_receiver="implementation_worker")
        )
        i = argv.index("--to")
        self.assertEqual(argv[i + 1], "claude")

    def test_coordinator_callback_argv_is_to_codex(self):
        argv = self._capture_argv(_row(callback_route="coordinator"))
        i = argv.index("--to")
        self.assertEqual(argv[i + 1], "codex")

    def test_escalation_row_argv_is_to_codex(self):
        row = _row(callback_route="coordinator", normalized_gate="coordinator_escalation")
        argv = self._capture_argv(row)
        i = argv.index("--to")
        self.assertEqual(argv[i + 1], "codex")


if __name__ == "__main__":
    unittest.main()
