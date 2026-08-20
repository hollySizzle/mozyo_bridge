"""``backfill_active_binding`` lane_kind contract (Redmine #15774).

A supersede-minted recovery row deliberately starts with an empty ``lane_kind``
(``supersede_and_activate`` never guesses geometry), and the create/adopt rails
refuse to rewrite an already-declared row — so before #15774 a recovery lane could
never regain its kind and every delegated child create was refused
(``sender_lane_not_delegated_coordinator``, #15693 j#108814). The backfill CAS now
fills an EMPTY ``lane_kind`` from the creating caller's own assertion, empty-to-value
only:

- an empty stored kind + a non-empty asserted kind -> filled;
- a non-empty stored kind + a DIFFERENT non-empty assertion -> zero-write refusal
  (the kind is part of the declaration identity, #13647 v7);
- an equal kind (or no assertion) -> the pre-#15774 behavior, byte-identical.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)

WS = "ws-15774"
ORIGINAL = "issue_15774_original"
RECOVERY = "issue_15774_recovery"
ISSUE = "15774"
KIND = "delegated_coordinator"


class BackfillLaneKindTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.store = LaneDeclarationStore(home=self.home)
        self.lifecycle = LaneLifecycleStore(home=self.home)
        self.decision = DecisionPointer(
            source="redmine", issue_id=ISSUE, journal_id="108814"
        )

    def _supersede_minted_row(self):
        """An active recovery row exactly as ``supersede_and_activate`` mints it."""
        declared = self.store.declare_lane(
            LaneLifecycleKey(WS, ORIGINAL),
            decision=self.decision,
            binding_kind="issue",
            issue_id=ISSUE,
            worktree_identity="wt_original_token",
            lane_kind=KIND,
        )
        self.assertTrue(declared.applied)
        outcome = self.lifecycle.supersede_and_activate(
            superseded=LaneLifecycleKey(WS, ORIGINAL),
            expected_revision=declared.revision,
            recovery=LaneLifecycleKey(WS, RECOVERY),
            decision=self.decision,
        )
        self.assertTrue(outcome.applied)
        row = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(row.lane_kind, "")
        return row

    def test_empty_kind_is_filled_from_the_callers_assertion(self) -> None:
        row = self._supersede_minted_row()
        outcome = self.store.backfill_active_binding(
            LaneLifecycleKey(WS, RECOVERY),
            expected_revision=row.revision,
            issue_id=ISSUE,
            worktree_identity="wt_recovery_token",
            lane_kind=KIND,
        )
        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.reason, CAS_APPLIED)
        filled = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(filled.lane_kind, KIND)
        self.assertEqual(filled.worktree_identity, "wt_recovery_token")

    def test_a_different_nonempty_kind_is_a_zero_write_refusal(self) -> None:
        row = self._supersede_minted_row()
        first = self.store.backfill_active_binding(
            LaneLifecycleKey(WS, RECOVERY),
            expected_revision=row.revision,
            issue_id=ISSUE,
            worktree_identity="wt_recovery_token",
            lane_kind=KIND,
        )
        self.assertTrue(first.applied)
        divergent = self.store.backfill_active_binding(
            LaneLifecycleKey(WS, RECOVERY),
            expected_revision=first.revision,
            issue_id=ISSUE,
            worktree_identity="wt_recovery_token",
            lane_kind="implementation",
        )
        self.assertFalse(divergent.applied)
        self.assertEqual(divergent.reason, CAS_ALREADY_DECLARED)
        unchanged = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(unchanged.lane_kind, KIND)

    def test_no_assertion_keeps_the_pre_15774_behavior(self) -> None:
        row = self._supersede_minted_row()
        outcome = self.store.backfill_active_binding(
            LaneLifecycleKey(WS, RECOVERY),
            expected_revision=row.revision,
            issue_id=ISSUE,
            worktree_identity="wt_recovery_token",
        )
        self.assertTrue(outcome.applied)
        filled = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(filled.lane_kind, "")

    def test_equal_kind_with_full_binding_is_an_idempotent_noop(self) -> None:
        row = self._supersede_minted_row()
        first = self.store.backfill_active_binding(
            LaneLifecycleKey(WS, RECOVERY),
            expected_revision=row.revision,
            issue_id=ISSUE,
            worktree_identity="wt_recovery_token",
            lane_kind=KIND,
        )
        self.assertTrue(first.applied)
        again = self.store.backfill_active_binding(
            LaneLifecycleKey(WS, RECOVERY),
            expected_revision=first.revision,
            issue_id=ISSUE,
            worktree_identity="wt_recovery_token",
            lane_kind=KIND,
        )
        self.assertTrue(again.applied)
        # No revision bump on a no-op: nothing was missing.
        self.assertEqual(again.revision, first.revision)

    def test_a_malformed_kind_token_is_refused_before_any_write(self) -> None:
        row = self._supersede_minted_row()
        with self.assertRaises(Exception):
            self.store.backfill_active_binding(
                LaneLifecycleKey(WS, RECOVERY),
                expected_revision=row.revision,
                issue_id=ISSUE,
                worktree_identity="wt_recovery_token",
                lane_kind=" delegated_coordinator ",
            )
        unchanged = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(unchanged.lane_kind, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
