"""herdr forward send adapter tests (Redmine #13583 Increment 3).

The design's injected-send-count contract (j#76417 point 7): the adapter calls the single forward
send port EXACTLY ONCE on the positive path (ok target + open fence), ZERO times on every negative
(missing / ambiguous / locator-missing / self target, held / unavailable fence), and never re-sends
a delivered forward (a repeat is a duplicate zero-send). Uses the real ForwardOutboxFence over a
temp home + real mzb1 encode/decode + a counting fake send port, so the count is asserted without a
live herdr / Redmine.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.forward_outbox_fence import ForwardOutboxFence
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    decode_assigned_name,
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    project_gateway_lane_id,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    WorkflowRoleResolution,
    STATUS_RESOLVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_forward_send import (
    SEND_DELIVERED,
    SEND_FAILED,
    ForwardSendOutcome,
    execute_herdr_forward,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
CODEX = "codex"


def _row(lane, *, provider=CODEX, ws=WS, locator="live-loc"):
    return {AGENT_KEY_NAME: encode_assigned_name(ws, provider, lane), "locator": locator}


def _locator_of(row):
    return row.get("locator", "")


class _CountingPort:
    """A fake ForwardSendPort that records every send (so the count is assertable)."""

    def __init__(self, result=SEND_DELIVERED):
        self.calls = []
        self.result = result

    def send(self, plan, target, *, args):
        self.calls.append((plan.direction, target.assigned_name, target.locator))
        return ForwardSendOutcome(result=self.result, rc=0, detail="fake send")


def _grandparent():
    return WorkflowRoleResolution(status=STATUS_RESOLVED, role=ROLE_GRANDPARENT_COORDINATOR)


def _gateway(scope):
    return WorkflowRoleResolution(
        status=STATUS_RESOLVED, role=ROLE_PROJECT_GATEWAY, project_scope=scope,
        lane_id=project_gateway_lane_id(scope),
    )


class HerdrForwardSendTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.fence = ForwardOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.args = argparse.Namespace()

    def _run(self, resolution, *, sender_lane, gateway_lane_ids, rows, port):
        return execute_herdr_forward(
            resolution,
            args=self.args,
            workspace_id=WS,
            sender_lane_id=sender_lane,
            target_provider=CODEX,
            gateway_lane_ids=frozenset(gateway_lane_ids),
            rows=rows,
            decode=decode_assigned_name,
            locator_of=_locator_of,
            fence=self.fence,
            send_port=port,
        )

    # --- grandparent -> single live gateway ------------------------------

    def test_grandparent_single_live_gateway_sends_exactly_once(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(
            _grandparent(), sender_lane="default", gateway_lane_ids={gw_lane},
            rows=[_row(gw_lane)], port=port,
        )
        self.assertTrue(res.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(res.send.result, SEND_DELIVERED)

    def test_repeat_forward_is_duplicate_zero_send(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        rows = [_row(gw_lane)]
        first = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw_lane}, rows=rows, port=port)
        self.assertTrue(first.sent)
        second = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw_lane}, rows=rows, port=port)
        self.assertFalse(second.sent)
        self.assertEqual(second.reason, "herdr_forward_duplicate")
        self.assertEqual(len(port.calls), 1)  # the delivered forward never re-sends

    def test_zero_live_gateways_is_zero_send_missing(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw_lane}, rows=[], port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "missing")
        self.assertEqual(len(port.calls), 0)

    def test_two_live_gateways_is_zero_send_ambiguous(self):
        a, b = project_gateway_lane_id("alpha"), project_gateway_lane_id("beta")
        port = _CountingPort()
        res = self._run(
            _grandparent(), sender_lane="default", gateway_lane_ids={a, b},
            rows=[_row(a), _row(b)], port=port,
        )
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "ambiguous")
        self.assertEqual(len(port.calls), 0)

    def test_gateway_without_locator_is_zero_send(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(
            _grandparent(), sender_lane="default", gateway_lane_ids={gw_lane},
            rows=[_row(gw_lane, locator="")], port=port,
        )
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "locator_missing")
        self.assertEqual(len(port.calls), 0)

    # --- gateway -> child with self-fence --------------------------------

    def test_gateway_single_child_sends_once(self):
        gw_lane = project_gateway_lane_id("alpha")
        child_lane = "issue_1234"
        port = _CountingPort()
        res = self._run(
            _gateway("alpha"), sender_lane=gw_lane, gateway_lane_ids={gw_lane},
            rows=[_row(gw_lane), _row(child_lane)], port=port,  # own gateway + a child
        )
        self.assertTrue(res.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertIn("delegated_coordinator", port.calls[0][0])

    def test_gateway_self_only_candidate_is_zero_send_self(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        # Only the sender's own gateway lane is present, and it is a bound gateway anyway.
        res = self._run(
            _gateway("alpha"), sender_lane=gw_lane, gateway_lane_ids={gw_lane},
            rows=[_row(gw_lane)], port=port,
        )
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "missing")  # the only slot is a bound gateway (excluded)
        self.assertEqual(len(port.calls), 0)

    def test_gateway_child_equal_to_self_lane_is_self_fenced(self):
        # A non-gateway child slot that happens to share the sender's lane id -> self-fence.
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(
            _gateway("alpha"), sender_lane="issue_self", gateway_lane_ids={gw_lane},
            rows=[_row("issue_self")], port=port,
        )
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "self")
        self.assertEqual(len(port.calls), 0)

    def test_gateway_two_children_is_ambiguous_zero_send(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(
            _gateway("alpha"), sender_lane=gw_lane, gateway_lane_ids={gw_lane},
            rows=[_row("issue_1"), _row("issue_2")], port=port,
        )
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "ambiguous")
        self.assertEqual(len(port.calls), 0)

    # --- send outcome recording ------------------------------------------

    def test_failed_send_marks_fence_uncertain_not_retried(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort(result=SEND_FAILED)
        first = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw_lane}, rows=[_row(gw_lane)], port=port)
        self.assertTrue(first.sent)
        self.assertEqual(first.send.result, SEND_FAILED)
        # A second attempt sees the uncertain fence and never re-sends (no blind retry).
        second = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw_lane}, rows=[_row(gw_lane)], port=port)
        self.assertFalse(second.sent)
        self.assertEqual(len(port.calls), 1)

    def test_wrong_workspace_rows_are_ignored(self):
        gw_lane = project_gateway_lane_id("alpha")
        port = _CountingPort()
        foreign = {AGENT_KEY_NAME: encode_assigned_name("f" * 32, CODEX, gw_lane), "locator": "x"}
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw_lane}, rows=[foreign], port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "missing")
        self.assertEqual(len(port.calls), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
