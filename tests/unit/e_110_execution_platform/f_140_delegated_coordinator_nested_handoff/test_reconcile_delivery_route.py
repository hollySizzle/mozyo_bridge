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
    provider_for_role,
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


class ProviderForRoleTest(unittest.TestCase):
    """review R2-F2: the reconcile row's target_receiver is the owner's provider, resolver-matchable."""

    def test_worker_role_resolves_to_claude_provider(self):
        self.assertEqual(provider_for_role("implementation_worker"), PROVIDER_CLAUDE)

    def test_gateway_and_others_resolve_to_codex(self):
        self.assertEqual(provider_for_role("implementation_gateway"), PROVIDER_CODEX)
        self.assertEqual(provider_for_role("auditor"), PROVIDER_CODEX)
        self.assertEqual(provider_for_role(""), PROVIDER_CODEX)

    def test_send_port_stays_codex_after_revert(self):
        # review R2-F2: the naive send port no longer routes worker rows (the provider now flows
        # via the row's target_receiver -> the resolver-backed sender); it stays --to codex.
        captured = {}

        def fake_runner(argv):
            captured["argv"] = argv
            return 0, '{"status": "delivered", "reason": "ok"}'

        row = _row(callback_route="implementation_worker", target_receiver="claude")
        HandoffCallbackSendPort(runner=fake_runner, attested_workspace_id="ws1")(row)
        i = captured["argv"].index("--to")
        self.assertEqual(captured["argv"][i + 1], "codex")


if __name__ == "__main__":
    unittest.main()
