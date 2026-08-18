"""Redmine #15707 (a) — the coordinator callback's ``--to`` must follow the coordinator rebind.

The fixed symptom (#15704 j#108012): the callback send port hardcoded ``--to codex`` while the
same ``coordinator`` route resolved to the claude pane the rebound coordinator role actually
binds (``agents.roles.coordinator: implementation`` -> provider claude, #13229), so the
receiver-binding fence refused every gateway->coordinator callback as ``invalid_args`` —
``injection_stage=not_sent``, and the row bounded-retried into ``dead_letter`` while the
coordinator looked "stalled".

Pins:

1. a coordinator-route row whose enqueue stamped ``target_receiver`` (the binding-resolved
   provider) is delivered with exactly that token — no literal can contradict the route;
2. a coordinator-route row with a BLANK stamp derives the token through the real coordinator
   role authority (``resolve_coordinator_provider``) under the incident binding — claude;
3. the derivation failing is a typed pre-send refusal (``receiver_provider_unresolved``,
   stage ``not_sent``) that classifies as bounded-retry ``not_sent`` — never a silent
   ``codex`` guess, never an ``uncertain`` terminal — and the token is allowlisted so the
   durable diagnostic survives normalization.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_send_port import (  # noqa: E402,E501
    RECEIVER_PROVIDER_UNRESOLVED,
    HandoffCallbackSendPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E402,E501
    SEND_NOT_SENT,
    ZERO_SEND_REASON_ALLOWLIST,
    normalize_zero_send_reason,
    send_outcome_for_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E402,E501
    PROVIDER_CLAUDE,
    ROLE_COORDINATOR,
    RoleProviderBinding,
)

#: The incident binding (#15631 / #13229): coordinator rebound onto the implementation
#: provider — exactly what ``agents.roles.coordinator: implementation`` resolves to.
COORDINATOR_REBOUND = RoleProviderBinding.default().with_overrides(
    {ROLE_COORDINATOR: PROVIDER_CLAUDE}
)


def _row(target_receiver=""):
    return CallbackOutboxRow(
        source="redmine", issue="15704", journal="108010",
        normalized_gate="review", callback_route="coordinator", state="inflight",
        attempts=0, max_attempts=3, send_attempted=True, notification_kind="review_result",
        notification_summary="", gate_mismatch=False, detail="", payload="",
        workspace_id="ws-15707", target_receiver=target_receiver,
    )


def _capture_port(**kwargs):
    calls = []
    port = HandoffCallbackSendPort(
        runner=lambda argv: calls.append(argv) or (0, '{"status": "sent", "reason": "ok"}'),
        **kwargs,
    )
    return port, calls


def _to_value(argv):
    return argv[argv.index("--to") + 1]


class StampedReceiverBindsTheToken(unittest.TestCase):
    def test_the_stamped_binding_resolved_provider_is_the_token(self) -> None:
        port, calls = _capture_port()
        result = port(_row(target_receiver=PROVIDER_CLAUDE))
        self.assertEqual(result.status, "sent")
        self.assertEqual(_to_value(calls[0]), PROVIDER_CLAUDE)


class BlankStampDerivesFromTheRoleAuthority(unittest.TestCase):
    def test_the_incident_binding_derives_claude_not_the_old_literal(self) -> None:
        # The exact j#108012 arrangement: a coordinator-route row with no stamped receiver,
        # under the coordinator->claude rebind. The port's DEFAULT resolver chain
        # (resolve_coordinator_provider) must answer claude — the pre-#15707 literal `codex`
        # is the refused contradiction.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            workflow_provider_resolution,
        )

        port, calls = _capture_port()
        with mock.patch.object(
            workflow_provider_resolution,
            "load_workflow_binding",
            return_value=(COORDINATOR_REBOUND, ()),
        ):
            result = port(_row(target_receiver=""))
        self.assertEqual(result.status, "sent")
        self.assertEqual(_to_value(calls[0]), PROVIDER_CLAUDE)


class UnresolvedDerivationFailsClosedAsNotSent(unittest.TestCase):
    def test_refusal_is_typed_and_pre_send(self) -> None:
        def unresolved():
            raise RuntimeError("role authority unavailable")

        port, calls = _capture_port(coordinator_provider_resolver=unresolved)
        result = port(_row(target_receiver=""))
        self.assertEqual(
            (result.status, result.reason), ("blocked", RECEIVER_PROVIDER_UNRESOLVED)
        )
        self.assertEqual(result.injection_stage, "not_sent")
        self.assertEqual(calls, [])  # zero bytes typed: the handoff was never invoked

    def test_refusal_classifies_as_bounded_retry_not_uncertain(self) -> None:
        # The dead-letter path stays bounded-retry (the row can be redriven after a config
        # repair) instead of poisoning to the never-retried `uncertain` terminal.
        outcome = send_outcome_for_delivery(
            "blocked", RECEIVER_PROVIDER_UNRESOLVED, injection_stage="not_sent"
        )
        self.assertEqual(outcome, SEND_NOT_SENT)

    def test_refusal_reason_survives_durable_normalization(self) -> None:
        self.assertIn(RECEIVER_PROVIDER_UNRESOLVED, ZERO_SEND_REASON_ALLOWLIST)
        self.assertEqual(
            normalize_zero_send_reason(RECEIVER_PROVIDER_UNRESOLVED),
            RECEIVER_PROVIDER_UNRESOLVED,
        )


if __name__ == "__main__":
    unittest.main()
