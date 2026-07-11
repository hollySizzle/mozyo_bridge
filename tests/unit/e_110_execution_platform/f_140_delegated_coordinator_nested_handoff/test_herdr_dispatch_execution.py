"""Fenced dispatch execution regressions (Redmine #13489 increment 2).

The mandated negative / crash / repeat / reconcile regressions over the reserve+send+outcome
boundary, each with a counting send seam so "zero additional send" is asserted directly.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    FENCE_DELIVERED,
    FENCE_UNCERTAIN,
    DispatchOutboxFence,
    dispatch_outbox_fence_path,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
    DISPATCH_DELIVERED,
    DISPATCH_FENCE_UNAVAILABLE,
    DISPATCH_SKIPPED,
    DISPATCH_UNCERTAIN,
    TURN_START_ACK_ONLY,
    TURN_START_STARTED,
    SendOutcome,
    execute_dispatch,
    fence_key_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    DispatchAuthorization,
)


def _auth(**over) -> DispatchAuthorization:
    fields = dict(
        action_id="act-1",
        source_gate="74999",
        issue="13489",
        workspace_id="ws1",
        lane_id="issue_13489",
        target_role="implementation_worker",
        target_assigned_name="mzb1_ws1_claude_issue_13489",
        action="dispatch_worker",
        conclusion="authorized",
        authorized_by_role="coordinator",
        journal="75010",
    )
    fields.update(over)
    return DispatchAuthorization(**fields)


class _Counter:
    def __init__(self, turn_start=TURN_START_STARTED, raises=None):
        self.calls = 0
        self.turn_start = turn_start
        self.raises = raises

    def __call__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return SendOutcome(turn_start=self.turn_start, detail="fake send")


class ExecuteDispatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()  # explicit init; reserve never auto-creates (F1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_fence_sends_exactly_once(self):
        send = _Counter()
        r = execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        self.assertEqual(r.result, DISPATCH_DELIVERED)
        self.assertEqual(send.calls, 1)
        self.assertEqual(self.fence.state_of(fence_key_for(_auth())), FENCE_DELIVERED)

    def test_repeat_same_action_zero_additional_send(self):
        send = _Counter()
        execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        r2 = execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        self.assertEqual(r2.result, DISPATCH_SKIPPED)
        self.assertEqual(send.calls, 1)  # no additional send

    def test_crash_after_send_leaves_uncertain_and_repeat_zero_send(self):
        # Send raises after (potentially) landing -> uncertain, one attempt.
        send = _Counter(raises=RuntimeError("crash after send"))
        r1 = execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        self.assertEqual(r1.result, DISPATCH_UNCERTAIN)
        self.assertEqual(send.calls, 1)
        self.assertEqual(self.fence.state_of(fence_key_for(_auth())), FENCE_UNCERTAIN)
        # Repeat: never auto-retried -> zero additional send.
        send2 = _Counter()
        r2 = execute_dispatch(authorization=_auth(), fence=self.fence, send=send2)
        self.assertEqual(r2.result, DISPATCH_SKIPPED)
        self.assertEqual(send2.calls, 0)

    def test_crash_after_reserve_before_send_repeat_zero_send(self):
        # Simulate reserve-then-crash: reserve the key, then a fresh caller repeats.
        self.fence.reserve(fence_key_for(_auth()))  # prior reserve exists (crash window)
        send = _Counter()
        r = execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        self.assertEqual(r.result, DISPATCH_SKIPPED)
        self.assertEqual(send.calls, 0)  # never-send on a reserved crash-window key

    def test_ack_only_without_turn_start_is_uncertain(self):
        # A delivery ACK that is NOT a turn-start confirmation must be uncertain, not delivered.
        send = _Counter(turn_start=TURN_START_ACK_ONLY)
        r = execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        self.assertEqual(r.result, DISPATCH_UNCERTAIN)
        self.assertEqual(send.calls, 1)
        self.assertEqual(self.fence.state_of(fence_key_for(_auth())), FENCE_UNCERTAIN)

    def test_store_loss_after_delivered_no_resend(self):
        # reserve+deliver, then the fence DB is lost -> a repeat is fence-unavailable, zero send.
        send = _Counter()
        execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        dispatch_outbox_fence_path(self.home).unlink()
        send2 = _Counter()
        r = execute_dispatch(
            authorization=_auth(), fence=DispatchOutboxFence(home=self.home), send=send2
        )
        self.assertEqual(r.result, DISPATCH_FENCE_UNAVAILABLE)
        self.assertEqual(send2.calls, 0)

    def test_new_action_id_after_reconcile_sends_once(self):
        send = _Counter()
        execute_dispatch(authorization=_auth(), fence=self.fence, send=send)
        # Operator reconcile issues a NEW action_id -> a distinct key -> one fresh send.
        send2 = _Counter()
        r = execute_dispatch(authorization=_auth(action_id="act-2"), fence=self.fence, send=send2)
        self.assertEqual(r.result, DISPATCH_DELIVERED)
        self.assertEqual(send2.calls, 1)

    def test_corrupt_fence_no_send(self):
        path = dispatch_outbox_fence_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a sqlite db")
        send = _Counter()
        r = execute_dispatch(authorization=_auth(), fence=DispatchOutboxFence(home=self.home), send=send)
        self.assertEqual(r.result, DISPATCH_FENCE_UNAVAILABLE)
        self.assertEqual(send.calls, 0)
        self.assertFalse(r.sent)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
