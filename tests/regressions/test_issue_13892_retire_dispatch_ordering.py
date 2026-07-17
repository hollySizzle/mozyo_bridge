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


class RelaunchAfterCompletedTest(RetireDispatchOrderingTest):
    """j#80594 R4-F2 — a completed attempt must not block a RELAUNCHED pair forever.

    My R3-F1 dispatch guard treated any pending/completed attempt naming the slot as
    "retiring". Because herdr assigned names are deterministic, a relaunched pair takes the
    same name, so an old completion cancelled every future dispatch to the new pair — the very
    "never reuse an old completion for a relaunched pair" rule the retire side enforces,
    violated on the dispatch side. The guard is now locator-correlated.
    """

    def _complete_at(self, pins):
        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            a = txn.reserve(pinned=pins)
            txn.mark_completed(attempt_id=a.attempt_id, closed=pins)

    def test_relaunched_pair_at_a_new_locator_is_dispatchable(self):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution as de

        self._complete_at((("codex", "%old1"), ("claude", "%old2")))
        original = de._live_locator_for
        de._live_locator_for = lambda n: "%new9"
        self.addCleanup(setattr, de, "_live_locator_for", original)
        result = execute_dispatch(
            authorization=self._auth(self.wk), fence=self.outbox, send=self._send
        )
        self.assertTrue(result.sent, "an old completion must not strand a relaunched pair")
        self.assertEqual(len(self.sends), 1)

    def test_stale_dispatch_to_the_closed_pane_is_refused(self):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution as de

        self._complete_at((("codex", "%old1"), ("claude", "%old2")))
        original = de._live_locator_for
        de._live_locator_for = lambda n: "%old2"  # the very pane the attempt closed
        self.addCleanup(setattr, de, "_live_locator_for", original)
        result = execute_dispatch(
            authorization=self._auth(self.wk), fence=self.outbox, send=self._send
        )
        self.assertFalse(result.sent)
        self.assertEqual(self.sends, [])

    def test_unobservable_locator_with_a_completed_attempt_fails_closed(self):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution as de

        self._complete_at((("codex", "%old1"), ("claude", "%old2")))
        original = de._live_locator_for
        de._live_locator_for = lambda n: ""
        self.addCleanup(setattr, de, "_live_locator_for", original)
        result = execute_dispatch(
            authorization=self._auth(self.wk), fence=self.outbox, send=self._send
        )
        self.assertFalse(result.sent, "ambiguous -> fail closed")

    def test_pending_blocks_regardless_of_locator(self):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution as de

        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("claude", "%2"),))
        original = de._live_locator_for
        de._live_locator_for = lambda n: "%totally_other"
        self.addCleanup(setattr, de, "_live_locator_for", original)
        result = execute_dispatch(
            authorization=self._auth(self.wk), fence=self.outbox, send=self._send
        )
        self.assertFalse(result.sent, "a close in flight blocks whatever the locator is")

    def test_the_guard_is_shared_by_every_reserve_then_send_edge(self):
        """j#80594 R4-F3(c): `execute_dispatch` was the ONLY edge wired, while I reported
        that all outbox reserve->send edges checked. The guard is now one exported seam and
        each edge calls it; this pins that the other edges actually import it."""
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            callback_sweep,
            operator_startup_resume,
        )

        for mod in (callback_sweep, operator_startup_resume):
            self.assertIn(
                "target_is_retiring",
                inspect.getsource(mod),
                f"{mod.__name__} reserves on the outbox and sends; it must ask the same guard",
            )
