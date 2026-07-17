"""Redmine #13892 R5-F2 — the REAL send edges of every covered obligation source.

I marked CallbackOutbox and ForwardOutboxFence covered (they are read), then wired the
retirement guard only into the DispatchOutboxFence edges and reported that all outbox
reserve->send edges checked. The actual edges — `CallbackOutboxProcessor.deliver` and
`execute_herdr_forward` — could still fire into panes a retire had already published a
`pending` intent for, so the bilateral `sent=0` was not closed.

These DRIVE the real edges against a real retirement fence. The prior test asserted on
`inspect.getsource` of two modules, which is not a guard: it passes on a docstring.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mozyo_bridge.core.state.scratch_retirement_fence as srf
from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    slot_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (  # noqa: E501
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS, LANE, ROLE = "wsabc", "dogfood13892", "claude"
NAME = encode_assigned_name(WS, ROLE, LANE)


class _NullSource:
    def read_entries(self, issue_id):
        return []


class CallbackDeliverEdgeTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        # The edge resolves its own fence from the home; point it at the temp one.
        original = srf.ScratchRetirementFence.__init__
        home = self.home
        srf.ScratchRetirementFence.__init__ = (
            lambda s, path=None, home=home: original(s, path, home=home)
        )
        self.addCleanup(setattr, srf.ScratchRetirementFence, "__init__", original)

        self.fence = ScratchRetirementFence(home=self.home)
        self.unit = RetirementUnit(
            WS, LANE, slot_digest([NAME, encode_assigned_name(WS, "codex", LANE)])
        )
        self.outbox = CallbackOutbox(path=self.home / "workflow-runtime.sqlite")
        self.key = CallbackOutboxKey(
            workspace_id=WS, source="redmine", issue="13999", journal="42",
            normalized_gate="review_request", callback_route="coordinator",
        )
        self.outbox.enqueue(
            self.key, target_lane=LANE, target_receiver=ROLE, target_generation="1"
        )
        self.sends = []
        self.proc = CallbackOutboxProcessor(
            self.outbox, _NullSource(), workspace_id=WS
        )

    def _sender(self, row):
        self.sends.append(row.key.issue)
        return "delivered"

    def test_a_pending_retirement_makes_the_callback_edge_zero_send(self):
        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("claude", "%2"),))
            report = self.proc.deliver(self._sender)
        self.assertEqual(self.sends, [], "sent=0: never deliver into a retiring pair")
        self.assertTrue(report.skipped_retiring, "the skip is surfaced, not silent")

    def test_no_retirement_does_not_over_block_a_normal_callback(self):
        """Control: an absent authority must not stop ordinary callback delivery."""
        report = self.proc.deliver(self._sender)
        self.assertEqual(len(self.sends), 1, "a normal callback must still deliver")
        self.assertFalse(report.skipped_retiring)

    def test_a_row_naming_no_rebuildable_target_still_delivers(self):
        """Control: a row with no target identity cannot be aimed at a retiring pair."""
        outbox = CallbackOutbox(path=self.home / "wr2.sqlite")
        key = CallbackOutboxKey(
            workspace_id=WS, source="redmine", issue="14000", journal="43",
            normalized_gate="review_request", callback_route="coordinator",
        )
        outbox.enqueue(key)  # no target_lane / target_receiver
        proc = CallbackOutboxProcessor(outbox, _NullSource(), workspace_id=WS)
        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("claude", "%2"),))
            proc.deliver(self._sender)
        self.assertEqual(len(self.sends), 1)

    def test_an_unreadable_authority_blocks_the_callback_edge(self):
        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("claude", "%2"),))
        self.fence.seal_path.write_text("deadbeef")  # identity mismatch
        self.proc.deliver(self._sender)
        self.assertEqual(self.sends, [], "an unreadable authority must not permit a send")


class SharedGuardSeamTest(unittest.TestCase):
    """The shared seam itself. The forward EDGE is driven in `test_issue_13892_r6_forward_edge`.

    The `inspect.getsource` order assert that used to live here was removed (review j#80644
    R6-F4): it passed on source text, so a deleted / bypassed / post-send guard would not have
    failed it. A seam test plus a source-string assert is not a driven edge, however the two
    are captioned.
    """

    def test_the_guard_returns_zero_send_for_a_retiring_target(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        home = Path(d)
        original = srf.ScratchRetirementFence.__init__
        srf.ScratchRetirementFence.__init__ = (
            lambda s, path=None, home=home: original(s, path, home=home)
        )
        self.addCleanup(setattr, srf.ScratchRetirementFence, "__init__", original)

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E501
            target_is_retiring,
        )

        fence = ScratchRetirementFence(home=home)
        unit = RetirementUnit(WS, LANE, slot_digest([NAME]))
        with fence.transaction(unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("claude", "%2"),))
            retiring, detail = target_is_retiring(NAME)
        self.assertTrue(retiring, "the shared guard the forward edge calls must refuse")
        self.assertTrue(detail)


if __name__ == "__main__":
    unittest.main()
