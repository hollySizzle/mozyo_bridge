"""Redmine #13892 R6-F3 — the hibernated redispatch reserve -> send edge must be guarded.

``redispatch_to_gateway`` is a real reserve -> send edge: it reserves on the same
``DispatchOutboxFence`` with a ``target_assigned_name`` and then sends an implementation_request
to that slot's live locator. It was the ONE such edge left unguarded, while j#80636 claimed
"all 5 edges" were wired — and ``target_is_retiring``'s own docstring already named this call
site as one of its callers. The docstring was the counter-evidence.

These drive the REAL ``redispatch_to_gateway`` entry with a real fence and count real sends.
Both directions are pinned: a retiring target sends 0, and an ordinary lane (no retirement
authority at all) still sends 1 — an over-block here would re-create the permanent-stuck this
ticket exists to remove.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    dispatch_outbox_fence_path,
)
from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    slot_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_hibernated_pair_recovery_live as live,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    REDISPATCH_DELIVERED,
    REDISPATCH_TARGET_RETIRING,
    REDISPATCH_UNCERTAIN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

from tests.regressions.test_issue_13847_pair_recovery_live import _ops, _row, _LANE, _WS


class RedispatchRetirementGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

        self.gw = encode_assigned_name(_WS, "codex", _LANE)
        self.worker = encode_assigned_name(_WS, "claude", _LANE)
        self.outbox = DispatchOutboxFence(path=dispatch_outbox_fence_path(self.home))
        self.outbox.bootstrap()
        self.ops = _ops(self.tmp.name, fence=self.outbox)
        self.retirement = ScratchRetirementFence(home=self.home)
        self.unit = RetirementUnit(_WS, _LANE, slot_digest([self.gw, self.worker]))
        self.sends = []

    def _redispatch(self, *, action_id="recover-pair:13847:x:3:2"):
        def _dispatch(_self, **kw):
            self.sends.append(kw)
            return 0

        with patch.object(
            live, "list_herdr_agent_rows", return_value=[_row(self.gw, "wZ:p3G")]
        ), patch.object(
            live.HerdrSublaneActuatorOps, "dispatch_implementation_request", _dispatch
        ):
            return self.ops.redispatch_to_gateway(
                action_id=action_id, gateway_assigned_name=self.gw, issue="13847",
                lane=_LANE, journal="79612", workspace_id=_WS,
            )

    def test_a_retiring_target_stops_the_send(self):
        with self.retirement.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("codex", "wZ:p3G"), ("claude", "wZ:p3C")))
            result = self._redispatch()
        self.assertEqual(self.sends, [], "sent=0: never redispatch into a retiring pair")
        self.assertEqual(result, REDISPATCH_TARGET_RETIRING)

    def test_a_stopped_send_leaves_no_reserved_row_blocking_the_retirement(self):
        """The reserve must be cancelled, not abandoned reserved.

        A row left ``reserved`` reads as "a send's fate is unresolved", which blocks the very
        retirement this guard just deferred to — a deadlock where each side waits for the
        other. Cancelled is a positive "this will never send".
        """
        with self.retirement.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("codex", "wZ:p3G"), ("claude", "wZ:p3C")))
            self._redispatch()
        rows = self.outbox.obligations_for_targets(
            workspace_id=_WS, target_assigned_names=(self.gw,)
        )
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].non_terminal, "a cancelled row is not an unresolved send")
        self.assertFalse(
            rows[0].needs_gate_correlation, "cancelled is positively not owed"
        )

    def test_no_retirement_does_not_over_block_an_ordinary_redispatch(self):
        """Control: the ordinary case for every non-scratch lane must still send."""
        result = self._redispatch()
        self.assertEqual(len(self.sends), 1, "an absent authority must not block a send")
        self.assertEqual(result, REDISPATCH_DELIVERED)

    def test_an_unreadable_authority_stops_the_send(self):
        with self.retirement.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("codex", "wZ:p3G"), ("claude", "wZ:p3C")))
        self.retirement.seal_path.write_text("deadbeef")  # identity mismatch
        result = self._redispatch()
        self.assertEqual(
            self.sends, [], "a send we cannot prove is safe is not sent"
        )
        self.assertEqual(result, REDISPATCH_TARGET_RETIRING)

    def test_the_guard_runs_before_the_locator_is_resolved(self):
        """No live gateway AND retiring: the retirement answer wins, and nothing sends.

        Ordering matters: resolving the locator first would report `uncertain` (a reconcile
        condition an operator must chase) for a pair that is simply being retired.
        """
        with patch.object(live, "list_herdr_agent_rows", return_value=[]):
            with self.retirement.transaction(self.unit, live_pair_present=True) as txn:
                txn.reserve(pinned=(("codex", "wZ:p3G"), ("claude", "wZ:p3C")))
                result = self.ops.redispatch_to_gateway(
                    action_id="a", gateway_assigned_name=self.gw, issue="13847",
                    lane=_LANE, journal="79612", workspace_id=_WS,
                )
        self.assertEqual(result, REDISPATCH_TARGET_RETIRING)
        self.assertNotEqual(result, REDISPATCH_UNCERTAIN)


if __name__ == "__main__":
    unittest.main()
