"""Redmine #13892 R6-F2 — the production correlation seam must carry the ROW's own identity.

Review j#80644 reproduced two false discharges against the **production** composition
(`dispatch_outbox_obligations(..., correlate=LiveSessionRetireOps._durable_disposition)`), not
the pure correlator, which was already right:

1. a row whose ``action_id`` named a foreign action discharged, because the seam received only
   ``(issue, journal)`` and rebuilt the rest **from the AUTHORIZE** it was meant to check the
   row against — so the identity comparison compared that AUTHORIZE with itself, a tautology
   that could never fail;
2. two valid AUTHORIZE markers at one dispatch journal discharged, because the index was a
   ``{journal: auth}`` dict comprehension whose last-write-wins turned a real ambiguity into a
   confident answer.

Both are fail-open safety defects: a false discharge retires a pair that still owes work.

These drive the real ``DispatchOutboxFence`` and the real production entry point. The pure
correlator is NOT called directly here — its correctness was never the bug.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence, FenceKey
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
    build_dispatch_authorization_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
    render_dispatch_disposition_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
    LiveSessionRetireOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.scratch_pair_obligations import (  # noqa: E501
    dispatch_outbox_obligations,
)

ISSUE, WS, LANE = "13999", "wsabc", "dogfood13892"
NAME = "mzb1_wsabc_claude_dogfood13892"
DISPATCH_J, REVIEW_J, DISP_J = "100", "200", "300"


class ProductionRowIdentityTest(unittest.TestCase):
    """The real fence + the real production correlate seam. No pure-function shortcuts."""

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.ops = LiveSessionRetireOps(repo_root=Path("."))

    def _deliver_row(self, *, action_id: str):
        """A REAL delivered row in a real fence, carrying ``action_id`` as its own identity."""
        fence = DispatchOutboxFence(home=self.home)
        fence.bootstrap()
        key = FenceKey(WS, LANE, ISSUE, DISPATCH_J, action_id, NAME)
        fence.reserve(key)
        fence.mark_delivered(key, detail="delivered")

    def _history(self, *, auth_actions, disposition_action):
        entries = [
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id=DISPATCH_J,
                notes="\n".join(
                    build_dispatch_authorization_marker(
                        action_id=a, source_gate="start", issue=ISSUE, workspace_id=WS,
                        lane_id=LANE, target_assigned_name=NAME,
                    )
                    for a in auth_actions
                ),
            ),
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id=REVIEW_J,
                notes="[mozyo:workflow-event:gate=review_request]",
            ),
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id=DISP_J,
                notes=render_dispatch_disposition_marker(
                    action_id=disposition_action, dispatch_journal=DISPATCH_J,
                    workspace_id=WS, lane_id=LANE, target_assigned_name=NAME,
                    terminal_journal=REVIEW_J,
                ),
            ),
        ]

        class Src:
            def read_entries(self, issue_id):
                return entries

        self.ops._redmine_source = lambda: Src()

    def _obligations(self):
        """The PRODUCTION entry point, wired to the production correlate seam."""
        return dispatch_outbox_obligations(
            workspace_id=WS,
            assigned_names=(NAME,),
            correlate=self.ops._durable_disposition,
        )

    def test_a_matching_identity_discharges(self):
        """Over-block control: the honest case must still retire, or the fix is a new defect."""
        self._deliver_row(action_id="ACT-1")
        self._history(auth_actions=("ACT-1",), disposition_action="ACT-1")
        self.assertEqual(self._obligations(), (), "a fully correlated round must discharge")

    def test_a_row_naming_a_foreign_action_does_not_discharge(self):
        """R6-F2 probe 1: the row's OWN action_id must be what is checked."""
        self._deliver_row(action_id="ROW-ACTION")
        self._history(auth_actions=("AUTH-ACTION",), disposition_action="AUTH-ACTION")
        found = self._obligations()
        self.assertEqual(
            len(found), 1,
            "the row's action_id names a different round than the AUTHORIZE/disposition; "
            "discharging it is the false discharge j#80644 reproduced",
        )
        self.assertTrue(found[0].blocks)
        self.assertEqual(found[0].action_id, "ROW-ACTION", "the row's identity must survive")

    def test_two_valid_authorizes_at_one_journal_do_not_discharge(self):
        """R6-F2 probe 2: cardinality is the answer, not something to resolve away."""
        self._deliver_row(action_id="ACT-2")
        self._history(auth_actions=("ACT-1", "ACT-2"), disposition_action="ACT-2")
        found = self._obligations()
        self.assertEqual(
            len(found), 1,
            "two valid AUTHORIZE markers at one journal is a genuine ambiguity; last-write-wins "
            "turned it into a discharge",
        )
        self.assertTrue(found[0].blocks)

    def test_a_row_with_no_action_id_never_correlates(self):
        """A blank causal identity names no round, so it can never be proven discharged."""
        self._deliver_row(action_id="")
        self._history(auth_actions=("ACT-1",), disposition_action="ACT-1")
        found = self._obligations()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].blocks)


class RowIdentitySurvivesTheStoreTest(unittest.TestCase):
    """The store must return the identity at all — the fix's load-bearing precondition."""

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)

    def test_the_read_carries_workspace_and_lane(self):
        fence = DispatchOutboxFence(home=self.home)
        fence.bootstrap()
        fence.reserve(FenceKey(WS, LANE, ISSUE, DISPATCH_J, "act1", NAME))
        rows = fence.obligations_for_targets(workspace_id=WS, target_assigned_names=(NAME,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].workspace_id, WS)
        self.assertEqual(rows[0].lane_id, LANE)
        self.assertEqual(rows[0].action_id, "act1")


if __name__ == "__main__":
    unittest.main()
