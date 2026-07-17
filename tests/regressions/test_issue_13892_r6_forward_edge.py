"""Redmine #13892 R6-F4 — the forward edge guard must be driven, not read as source text.

The R5 "driving test" for this edge asserted `inspect.getsource(execute_herdr_forward)` string
order and separately called the `target_is_retiring` seam. Neither runs the function, so the
guard could be deleted, bypassed by a condition, or moved AFTER the send and the test would
still pass as long as the source text survived. j#80620 had already recorded, and I had already
accepted, that `inspect.getsource` is not a behavioral guard — then the same shape was written
again.

These drive the real `execute_herdr_forward` with the real `ForwardOutboxFence` and a fake send
port, and count sends.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.forward_outbox_fence import ForwardOutboxFence
from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    slot_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_forward_send import (  # noqa: E501
    execute_herdr_forward,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

from tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_herdr_forward_send import (  # noqa: E501
    CODEX,
    WS,
    _CountingPort,
    _grandparent,
    _row,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    project_gateway_lane_id,
)


class ForwardEdgeRetirementGuardTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.home = Path(d)
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.fence = ForwardOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.args = argparse.Namespace()
        self.gw_lane = project_gateway_lane_id("alpha")
        self.retirement = ScratchRetirementFence(home=self.home)

    def _unit_for(self, lane_id):
        codex = encode_assigned_name(WS, "codex", lane_id)
        claude = encode_assigned_name(WS, "claude", lane_id)
        return RetirementUnit(WS, lane_id, slot_digest([codex, claude]))

    def _forward(self, port):
        """The REAL forward entry, with a real fence and a fake send port."""
        from tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_herdr_forward_send import (  # noqa: E501
            HerdrForwardSendTest,
        )

        harness = HerdrForwardSendTest("setUp")
        harness.home = self.home
        harness.fence = self.fence
        harness.args = self.args
        return harness._run(
            _grandparent(),
            sender_lane="default",
            gateway_lane_ids={self.gw_lane},
            rows=[_row(self.gw_lane)],
            port=port,
            provider=CODEX,
        )

    def test_a_retiring_target_stops_the_forward_send(self):
        port = _CountingPort()
        with self.retirement.transaction(
            self._unit_for(self.gw_lane), live_pair_present=True
        ) as txn:
            txn.reserve(pinned=(("codex", "%9"), ("claude", "%8")))
            result = self._forward(port)
        self.assertEqual(port.calls, [], "sent=0: never forward into a retiring pair")
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, "herdr_forward_target_retiring")

    def test_no_retirement_does_not_over_block_an_ordinary_forward(self):
        """Control: an absent authority is the ordinary case and must still send."""
        port = _CountingPort()
        result = self._forward(port)
        self.assertTrue(result.sent)
        self.assertEqual(len(port.calls), 1)

    def test_an_unreadable_authority_stops_the_forward_send(self):
        port = _CountingPort()
        with self.retirement.transaction(
            self._unit_for(self.gw_lane), live_pair_present=True
        ) as txn:
            txn.reserve(pinned=(("codex", "%9"), ("claude", "%8")))
        self.retirement.seal_path.write_text("deadbeef")  # identity mismatch
        result = self._forward(port)
        self.assertEqual(port.calls, [], "a send we cannot prove is safe is not sent")
        self.assertFalse(result.sent)


if __name__ == "__main__":
    unittest.main()
