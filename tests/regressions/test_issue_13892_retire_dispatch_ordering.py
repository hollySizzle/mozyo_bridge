"""Redmine #13892 R3-F1 — the retire/dispatch race, pinned from BOTH sides.

The reviewer's condition is not "the verdict says blocked" but **closed=0 / sent=0**. A
synthetic "an obligation appears from nowhere" fixture cannot show that: it fabricates an
interleaving that the two-sided ordering makes unreachable. These drive BOTH real authorities.

The ordering (design j#80526): the retire publishes its `pending` intent BEFORE reading
obligations, and every dispatch checks the retirement authority AFTER winning its outbox
reserve. Whichever side publishes first, the other does nothing:

- retire's pending lands first -> the dispatch sees it and sends nothing;
- the dispatch's reserve lands first -> the retire's obligation read sees it and closes nothing.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import unittest
from pathlib import Path

import mozyo_bridge.core.state.scratch_retirement_fence as srf
from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence, FenceKey
from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    slot_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E501
    DISPATCH_SKIPPED,
    SendOutcome,
    TURN_START_STARTED,
    execute_dispatch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
    DispatchAuthorization,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
    REASON_WORK_OBLIGATION_PRESENT,
    run_session_retire,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.scratch_pair_retire import (  # noqa: E501
    STATE_BLOCKED,
    STATE_GREEN,
)

LANE, GW, WK = "dogfood13892", "codex", "claude"


class _R:
    def __init__(self, closed=(), failed=()):
        self.closed, self.failed = tuple(closed), tuple(failed)


class RetireDispatchOrderingTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        self.repo = Path(__file__).resolve().parents[2]
        self.ws = herdr_workspace_segment(self.repo)
        self.gw = encode_assigned_name(self.ws, GW, LANE)
        self.wk = encode_assigned_name(self.ws, WK, LANE)
        self.unit = RetirementUnit(self.ws, LANE, slot_digest([self.gw, self.wk]))
        self.fence = ScratchRetirementFence(home=self.home)
        self.outbox = DispatchOutboxFence(home=self.home)
        self.outbox.bootstrap()
        # The dispatch side resolves its own fence from the home; point it at the temp one.
        original = srf.ScratchRetirementFence.__init__
        home = self.home
        srf.ScratchRetirementFence.__init__ = (
            lambda s, path=None, home=home: original(s, path, home=home)
        )
        self.addCleanup(setattr, srf.ScratchRetirementFence, "__init__", original)
        self.sends = []

    def _send(self):
        self.sends.append(1)
        return SendOutcome(turn_start=TURN_START_STARTED)

    def _auth(self, target):
        return DispatchAuthorization(
            action_id="act1", source_gate="gate", issue="13999", workspace_id=self.ws,
            lane_id=LANE, target_role="worker", target_assigned_name=target,
            action="dispatch", conclusion="c", authorized_by_role="coordinator",
            journal="1",
        )

    def _ops(self, rows):
        test = self

        class Ops:
            def __init__(self):
                self._rows = list(rows)
                self.close_calls = []
                self.recorded = []

            def agent_rows(self):
                return list(self._rows)

            def runtime_state(self, loc):
                return "awaiting_input"

            def observe_composer(self, loc):
                return (True, False)

            def lifecycle_record_absent(self, ws, lane):
                return True

            def open_obligations(self, ws, names):
                from mozyo_bridge.core.state.dispatch_outbox_fence import (
                    DispatchOutboxFence as F,
                )

                return F(home=test.home).obligations_for_targets(
                    workspace_id=ws, target_assigned_names=tuple(names)
                )

            def retirement_transaction(self, unit, *, live_pair_present):
                return test.fence.transaction(unit, live_pair_present=live_pair_present)

            def peek_retirement(self, unit):
                return test.fence.peek(unit)

            def close(self, ws, lane, targets):
                self.close_calls.append(tuple(targets))
                locs = {loc for _r, loc in targets}
                self._rows = [r for r in self._rows if r.get("pane") not in locs]
                return _R(closed=tuple(targets))

            def record_retirement(self, **kw):
                self.recorded.append(kw)
                return "recorded"

        return Ops()

    def _pair(self):
        return [
            {"name": self.gw, "pane": "%1", "agent": GW},
            {"name": self.wk, "pane": "%2", "agent": WK},
        ]

    def test_retire_first_then_dispatch_sends_nothing(self):
        """The retire's pending is published before its obligation read -> dispatch aborts."""
        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("codex", "%1"), ("claude", "%2")))
            result = execute_dispatch(
                authorization=self._auth(self.wk), fence=self.outbox, send=self._send
            )
        self.assertEqual(result.result, DISPATCH_SKIPPED)
        self.assertFalse(result.sent)
        self.assertEqual(self.sends, [], "sent=0: never send into a retiring pair")

    def test_dispatch_first_then_retire_closes_nothing(self):
        """The dispatch's reserve lands first -> the retire's obligation read sees it."""
        self.outbox.reserve(FenceKey(self.ws, LANE, "13999", "1", "act1", self.wk))
        ops = self._ops(self._pair())
        result = run_session_retire(
            argparse.Namespace(lane=LANE, execute=True, json=False, repo=None),
            self.repo, ops=ops,
        )
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_WORK_OBLIGATION_PRESENT)
        self.assertEqual(ops.close_calls, [], "closed=0: never close over owed work")
        self.assertEqual(result.closed, ())

    def test_no_retirement_does_not_over_block_a_normal_dispatch(self):
        """Control: an absent retirement authority must not block ordinary lanes."""
        result = execute_dispatch(
            authorization=self._auth(self.wk), fence=self.outbox, send=self._send
        )
        self.assertTrue(result.sent, "a normal dispatch must still send")
        self.assertEqual(len(self.sends), 1)

    def test_unreadable_retirement_authority_blocks_the_send(self):
        """An authority we cannot read is not an authority that says 'no retirement'."""
        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("claude", "%2"),))
        self.fence.seal_path.write_text("deadbeef")  # identity mismatch
        result = execute_dispatch(
            authorization=self._auth(self.wk), fence=self.outbox, send=self._send
        )
        self.assertFalse(result.sent)
        self.assertEqual(self.sends, [])


if __name__ == "__main__":
    unittest.main()
