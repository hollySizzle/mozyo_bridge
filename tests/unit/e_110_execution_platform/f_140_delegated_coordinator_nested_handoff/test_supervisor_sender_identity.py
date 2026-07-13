"""Background-service delivery authority tests (Redmine #13683 design answer j#77216, Model A').

The R3 evidence for the ``background_service`` delivery authority: every fail-closed path is a
zero-send (no lease / no claim / foreign workspace / no or ambiguous target / generation mismatch /
lease expired), an authorized delivery goes through the transport seam to the exact re-resolved
target, a genuine uncertain outcome does not blind-retry, and the delivery env is scrubbed of agent
identity + stamped as a separated origin.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_INFLIGHT
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
    BackgroundServiceCallbackSender,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    background_transport_env,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.background_service_delivery import (
    AUTH_AMBIGUOUS_TARGET,
    AUTH_FOREIGN_WORKSPACE,
    AUTH_GENERATION_MISMATCH,
    AUTH_NO_CLAIM,
    AUTH_NO_LEASE,
    AUTH_NO_TARGET,
    AUTH_OK,
    BACKGROUND_SERVICE_ORIGIN,
    DeliveryTarget,
    TargetResolution,
    authorize_background_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
)

NOW = "2026-07-13T00:00:00+00:00"


def _row(workspace_id: str, *, claim_token: str = "tok") -> CallbackOutboxRow:
    return CallbackOutboxRow(
        source="redmine", issue="13683", journal="77065", normalized_gate="review_request",
        callback_route="coordinator", state=CALLBACK_INFLIGHT, attempts=0, max_attempts=3,
        send_attempted=True, notification_kind="review_request", notification_summary="",
        gate_mismatch=False, detail="", payload="", claim_token=claim_token, workspace_id=workspace_id,
    )


def _target(workspace_id: str, *, generation: str = "", locator: str = "%1") -> DeliveryTarget:
    return DeliveryTarget(
        workspace_id=workspace_id, lane="default", receiver="codex", issue="13683",
        journal="77065", generation=generation, locator=locator,
    )


class _FakeResolver:
    def __init__(self, targets):
        self._targets = tuple(targets)

    def resolve(self, row):
        return TargetResolution.of(self._targets)


class _RecordingTransport:
    def __init__(self, result: HandoffDeliveryResult):
        self._result = result
        self.calls = []

    def deliver(self, row, target):
        self.calls.append((row, target))
        return self._result


# ---------------------------------------------------------------------------
# Pure authorization matrix.
# ---------------------------------------------------------------------------


class AuthorizeMatrixTest(unittest.TestCase):
    def _authorize(self, **over):
        base = dict(
            expected_workspace="wsA", row_workspace="wsA", has_lease=True, has_claim=True,
            resolution=TargetResolution.of([_target("wsA")]), expected_generation="",
        )
        base.update(over)
        return authorize_background_delivery(**base)

    def test_authorized_happy_path(self):
        d = self._authorize()
        self.assertTrue(d.authorized)
        self.assertEqual(d.reason, AUTH_OK)
        self.assertEqual(d.target.workspace_id, "wsA")

    def test_foreign_row_workspace(self):
        d = self._authorize(row_workspace="wsB")
        self.assertFalse(d.authorized)
        self.assertEqual(d.reason, AUTH_FOREIGN_WORKSPACE)

    def test_no_lease(self):
        self.assertEqual(self._authorize(has_lease=False).reason, AUTH_NO_LEASE)

    def test_no_claim(self):
        self.assertEqual(self._authorize(has_claim=False).reason, AUTH_NO_CLAIM)

    def test_no_target(self):
        self.assertEqual(self._authorize(resolution=TargetResolution.of([])).reason, AUTH_NO_TARGET)

    def test_ambiguous_target(self):
        d = self._authorize(resolution=TargetResolution.of([_target("wsA"), _target("wsA", locator="%2")]))
        self.assertEqual(d.reason, AUTH_AMBIGUOUS_TARGET)

    def test_resolved_target_foreign_workspace(self):
        d = self._authorize(resolution=TargetResolution.of([_target("wsB")]))
        self.assertEqual(d.reason, AUTH_FOREIGN_WORKSPACE)

    def test_generation_mismatch(self):
        d = self._authorize(
            resolution=TargetResolution.of([_target("wsA", generation="g2")]), expected_generation="g1"
        )
        self.assertEqual(d.reason, AUTH_GENERATION_MISMATCH)

    def test_generation_absent_is_no_constraint(self):
        d = self._authorize(resolution=TargetResolution.of([_target("wsA", generation="")]), expected_generation="")
        self.assertTrue(d.authorized)


# ---------------------------------------------------------------------------
# Sender: lease + claim authority, resolution, transport, outcome mapping.
# ---------------------------------------------------------------------------


class BackgroundServiceSenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")

    def _sender(self, *, workspace_id="wsA", holder="superX", resolver=None, transport=None, now=NOW):
        self.transport = transport or _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        return BackgroundServiceCallbackSender(
            workspace_id=workspace_id, holder=holder, lease_store=self.lease_store,
            target_resolver=resolver or _FakeResolver([_target(workspace_id)]),
            transport=self.transport, now_fn=lambda: now,
        )

    def test_authorized_delivery_calls_transport_delivered(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender()
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][1].workspace_id, "wsA")

    def test_no_lease_is_zero_send_not_sent(self):
        # no lease acquired at all
        sender = self._sender()
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)  # retryable, NOT uncertain
        self.assertEqual(result.persist_reason, AUTH_NO_LEASE)
        self.assertEqual(self.transport.calls, [])  # transport never invoked

    def test_expired_lease_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=100)  # expires 00:01:40
        sender = self._sender(now="2026-07-13T01:00:00+00:00")  # past expiry
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(self.transport.calls, [])

    def test_lease_held_by_other_holder_is_zero_send(self):
        self.lease_store.acquire("wsA", "otherHolder", now=NOW, ttl_seconds=600)
        sender = self._sender(holder="superX")
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_NO_LEASE)
        self.assertEqual(self.transport.calls, [])

    def test_no_claim_token_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender()
        result = sender(_row("wsA", claim_token=""))  # unclaimed row
        self.assertEqual(result.persist_reason, AUTH_NO_CLAIM)
        self.assertEqual(self.transport.calls, [])

    def test_foreign_row_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender()
        result = sender(_row("wsB"))  # foreign workspace row
        self.assertEqual(result.persist_reason, AUTH_FOREIGN_WORKSPACE)
        self.assertEqual(self.transport.calls, [])

    def test_no_target_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender(resolver=_FakeResolver([]))
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_NO_TARGET)
        self.assertEqual(self.transport.calls, [])

    def test_ambiguous_target_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender(resolver=_FakeResolver([_target("wsA"), _target("wsA", locator="%2")]))
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_AMBIGUOUS_TARGET)
        self.assertEqual(self.transport.calls, [])

    def test_unresolvable_route_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)

        class _Raises:
            def resolve(self, row):
                raise RuntimeError("route ledger unreadable")

        sender = self._sender(resolver=_Raises())
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_NO_TARGET)  # fail-closed -> no target
        self.assertEqual(self.transport.calls, [])

    def test_transport_uncertain_does_not_retry(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender(transport=_RecordingTransport(HandoffDeliveryResult("blocked", "turn_start_unconfirmed")))
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_UNCERTAIN)  # no blind retry (boundary 5)

    def test_transport_exception_is_uncertain(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)

        class _BoomTransport:
            calls = []

            def deliver(self, row, target):
                raise RuntimeError("subprocess exploded")

        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=_FakeResolver([_target("wsA")]), transport=_BoomTransport(), now_fn=lambda: NOW,
        )
        self.assertEqual(sender(_row("wsA")).outcome, SEND_UNCERTAIN)


class TwoWorkspaceExactRouteTest(unittest.TestCase):
    """R3 evidence: 2 workspaces each deliver to their own exact target; cross-ws is zero-send."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        self.lease_store.acquire("wsB", "superX", now=NOW, ttl_seconds=600)
        self.tA = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        self.tB = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        self.sA = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=_FakeResolver([_target("wsA", locator="%A")]), transport=self.tA, now_fn=lambda: NOW,
        )
        self.sB = BackgroundServiceCallbackSender(
            workspace_id="wsB", holder="superX", lease_store=self.lease_store,
            target_resolver=_FakeResolver([_target("wsB", locator="%B")]), transport=self.tB, now_fn=lambda: NOW,
        )

    def test_each_workspace_delivers_to_its_own_target(self):
        self.assertEqual(self.sA(_row("wsA")).outcome, SEND_DELIVERED)
        self.assertEqual(self.sB(_row("wsB")).outcome, SEND_DELIVERED)
        self.assertEqual(self.tA.calls[0][1].locator, "%A")
        self.assertEqual(self.tB.calls[0][1].locator, "%B")

    def test_cross_workspace_row_is_zero_send(self):
        # wsA's authority handed a wsB row -> foreign -> zero-send, neither transport fires.
        result = self.sA(_row("wsB"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(self.tA.calls, [])


class BackgroundTransportEnvTest(unittest.TestCase):
    def test_env_scrubs_agent_identity_and_stamps_origin(self):
        polluted = {"MOZYO_AGENT_ROLE": "codex", "MOZYO_LANE_ID": "foreign", "MOZYO_WORKSPACE_ID": "wsZ", "PATH": "/bin"}
        with mock.patch.dict("os.environ", polluted, clear=True):
            env = background_transport_env("wsA")
        self.assertNotIn("MOZYO_AGENT_ROLE", env)
        self.assertNotIn("MOZYO_LANE_ID", env)
        self.assertEqual(env["MOZYO_WORKSPACE_ID"], "wsA")
        self.assertEqual(env["MOZYO_DELIVERY_ORIGIN"], BACKGROUND_SERVICE_ORIGIN)
        self.assertEqual(env["PATH"], "/bin")  # unrelated env preserved

    def test_ambient_pollution_does_not_grant_authority(self):
        # Even with a polluted agent env, without a lease the delivery is zero-send: the authority is
        # lease + claim, NOT the env identity.
        d = Path(tempfile.mkdtemp())
        lease_store = SupervisorLeaseStore(path=d / "lease.sqlite")  # no lease acquired
        transport = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=lease_store,
            target_resolver=_FakeResolver([_target("wsA")]), transport=transport, now_fn=lambda: NOW,
        )
        with mock.patch.dict("os.environ", {"MOZYO_AGENT_ROLE": "codex", "MOZYO_WORKSPACE_ID": "wsA"}, clear=True):
            result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
