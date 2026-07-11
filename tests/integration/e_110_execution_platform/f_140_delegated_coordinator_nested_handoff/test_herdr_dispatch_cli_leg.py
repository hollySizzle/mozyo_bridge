"""CLI dispatch-leg integration (Redmine #13489 increment 2).

Drives :func:`execute_herdr_dispatch` end-to-end with a real temp fence and injected seams: a
resolved AUTHORIZE decision + a counting send prove the leg reserves + sends exactly once, and a
repeat is zero additional send. A non-AUTHORIZE decision (no authorization) is zero send.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    dispatch_outbox_fence_path,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    herdr_dispatch_authority,
    herdr_dispatch_cli,
    herdr_workflow_step,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_cli import (
    execute_herdr_dispatch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    herdr_dispatch_cli as _cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    sublane_worker_dispatch_herdr_ops as _herdr_ops,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
    DISPATCH_DELIVERED,
    DISPATCH_FENCE_UNAVAILABLE,
    DISPATCH_SKIPPED,
    DISPATCH_UNCERTAIN,
    TURN_START_STARTED,
    SendOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authority import (
    MONITOR,
    REASON_NO_AUTHORIZATION,
    TARGET_AWAITING_INPUT,
    DispatchDecision,
    decide_dispatch_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    DispatchAuthorization,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain import (
    herdr_target_resolution,
)

WS = "ws1"
LANE = "issue_13489"
ISSUE = "13489"
ANCHOR = "redmine:issue=13489:journal=74766"


def _auth(**over) -> DispatchAuthorization:
    fields = dict(
        action_id="act-1",
        source_gate="74999",
        issue=ISSUE,
        workspace_id=WS,
        lane_id=LANE,
        target_role="implementation_worker",
        target_assigned_name="mzb1_ws1_claude_issue_13489",
        action="dispatch_worker",
        conclusion="authorized",
        authorized_by_role="coordinator",
        journal="75010",
    )
    fields.update(over)
    return DispatchAuthorization(**fields)


class _Identity:
    ok = True

    def __init__(self):
        self.identity = self
        self.workspace_id = WS
        self.lane_id = LANE
        self.role = "claude"


class _Counter:
    def __init__(self):
        self.calls = 0

    def factory(self, args, authorization, journal, repo_root, env):
        def _send():
            self.calls += 1
            return SendOutcome(turn_start=TURN_START_STARTED, detail="fake send")

        return _send


class DispatchLegTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()  # explicit init; reserve never auto-creates (F1)
        self._orig = {
            "sender": herdr_target_resolution.resolve_sender_identity,
            "anchor_ws": herdr_workflow_step._anchor_workspace_id,
            "decision": herdr_dispatch_authority.resolve_dispatch_decision,
        }
        herdr_target_resolution.resolve_sender_identity = lambda environ, anchor_workspace_id=None: _Identity()
        herdr_workflow_step._anchor_workspace_id = lambda repo_root: WS

    def tearDown(self):
        herdr_target_resolution.resolve_sender_identity = self._orig["sender"]
        herdr_workflow_step._anchor_workspace_id = self._orig["anchor_ws"]
        herdr_dispatch_authority.resolve_dispatch_decision = self._orig["decision"]
        self._tmp.cleanup()

    def _set_decision(self, decision: DispatchDecision):
        herdr_dispatch_authority.resolve_dispatch_decision = (
            lambda *a, **k: decision
        )

    def _args(self):
        return argparse.Namespace(repo=str(self.home))

    def test_authorized_sends_exactly_once_and_repeat_zero(self):
        self._set_decision(
            decide_dispatch_authority(
                authorization=_auth(), superseded=False, target_runtime=TARGET_AWAITING_INPUT
            )
        )
        counter = _Counter()
        r1 = execute_herdr_dispatch(
            self._args(), ANCHOR, env={"x": "y"}, send_factory=counter.factory, fence=self.fence
        )
        self.assertEqual(r1.result, DISPATCH_DELIVERED)
        self.assertEqual(counter.calls, 1)
        # Repeat: never-send (the fence already holds the key delivered).
        r2 = execute_herdr_dispatch(
            self._args(), ANCHOR, env={"x": "y"}, send_factory=counter.factory, fence=self.fence
        )
        self.assertEqual(r2.result, DISPATCH_SKIPPED)
        self.assertEqual(counter.calls, 1)

    def test_no_authorization_is_zero_send(self):
        self._set_decision(
            DispatchDecision(MONITOR, REASON_NO_AUTHORIZATION, "no authorization")
        )
        counter = _Counter()
        r = execute_herdr_dispatch(
            self._args(), ANCHOR, env={"x": "y"}, send_factory=counter.factory, fence=self.fence
        )
        self.assertEqual(r.result, DISPATCH_SKIPPED)
        self.assertEqual(counter.calls, 0)

    def test_store_loss_is_fence_unavailable_zero_send(self):
        # Product path (F1): a delivered action, then the fence DB is lost -> a repeat with the
        # SAME still-authorized action must be fence-unavailable (zero send), never a re-send.
        self._set_decision(
            decide_dispatch_authority(
                authorization=_auth(), superseded=False, target_runtime=TARGET_AWAITING_INPUT
            )
        )
        counter = _Counter()
        execute_herdr_dispatch(
            self._args(),
            ANCHOR,
            env={"x": "y"},
            send_factory=counter.factory,
            fence=DispatchOutboxFence(home=self.home),
        )
        dispatch_outbox_fence_path(self.home).unlink()  # the fence DB is lost (sidecar remains)
        counter2 = _Counter()
        r = execute_herdr_dispatch(
            self._args(),
            ANCHOR,
            env={"x": "y"},
            send_factory=counter2.factory,
            fence=DispatchOutboxFence(home=self.home),
        )
        self.assertEqual(r.result, DISPATCH_FENCE_UNAVAILABLE)
        self.assertEqual(counter2.calls, 0)

    def _run_default_leg_with_ops_turn_start(self, ops_turn_start):
        """Drive the DEFAULT product send factory with a faked ops turn-start (F2)."""
        self._set_decision(
            decide_dispatch_authority(
                authorization=_auth(), superseded=False, target_runtime=TARGET_AWAITING_INPUT
            )
        )
        orig_locator = _cli._resolve_target_locator
        orig_ts = _herdr_ops.HerdrWorkerDispatchOps.dispatch_to_worker_turn_start
        _cli._resolve_target_locator = lambda name, env: "pane1"
        _herdr_ops.HerdrWorkerDispatchOps.dispatch_to_worker_turn_start = (
            lambda self, **kw: (0, ops_turn_start)
        )
        try:
            return execute_herdr_dispatch(
                self._args(),
                ANCHOR,
                env={"x": "y"},
                fence=DispatchOutboxFence(home=self.home),
            )  # send_factory=None -> the real default product leg
        finally:
            _cli._resolve_target_locator = orig_locator
            _herdr_ops.HerdrWorkerDispatchOps.dispatch_to_worker_turn_start = orig_ts

    def test_default_leg_started_is_delivered(self):
        # F2: the DEFAULT product leg reaches `delivered` on a real positive turn-start.
        r = self._run_default_leg_with_ops_turn_start("started")
        self.assertEqual(r.result, DISPATCH_DELIVERED)

    def test_default_leg_ack_without_turn_start_is_uncertain(self):
        # F2: an ACK landing without the receiver's turn starting -> uncertain, not delivered.
        r = self._run_default_leg_with_ops_turn_start("delivered_not_started")
        self.assertEqual(r.result, DISPATCH_UNCERTAIN)

    def test_recover_and_new_action_sends_once(self):
        # After a loss + operator recover(), a reconciled NEW action_id sends exactly once.
        dispatch_outbox_fence_path(self.home).unlink()
        DispatchOutboxFence(home=self.home).recover()
        self._set_decision(
            decide_dispatch_authority(
                authorization=_auth(action_id="act-2"),
                superseded=False,
                target_runtime=TARGET_AWAITING_INPUT,
            )
        )
        counter = _Counter()
        r = execute_herdr_dispatch(
            self._args(),
            ANCHOR,
            env={"x": "y"},
            send_factory=counter.factory,
            fence=DispatchOutboxFence(home=self.home),
        )
        self.assertEqual(r.result, DISPATCH_DELIVERED)
        self.assertEqual(counter.calls, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
