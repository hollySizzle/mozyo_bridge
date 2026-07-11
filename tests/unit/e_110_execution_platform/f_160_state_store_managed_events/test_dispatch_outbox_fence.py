"""Dispatch outbox idempotency fence tests (Redmine #13489 increment 2).

Pins the reserve-before-send fence: a fresh key wins exactly once, a repeat / crash re-entry
never wins (and a reserved re-entry surfaces uncertain), the closed state vocabulary, and a
corrupt / unrecognized-version store fails closed (do-not-send).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    FENCE_ABSENT,
    FENCE_CANCELLED,
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    DispatchOutboxFence,
    DispatchOutboxFenceError,
    FenceKey,
    dispatch_outbox_fence_path,
)


def _key(**over) -> FenceKey:
    fields = dict(
        workspace_id="ws1",
        lane_id="issue_13489",
        issue="13489",
        journal="75010",
        action_id="act-1",
        target_assigned_name="mzb1_ws1_claude_issue_13489",
    )
    fields.update(over)
    return FenceKey(**fields)


class ReserveTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fresh_key_wins_once(self):
        r = self.fence.reserve(_key())
        self.assertTrue(r.won)
        self.assertEqual(r.prior_state, FENCE_ABSENT)
        self.assertEqual(r.current_state, FENCE_RESERVED)

    def test_repeat_reserved_key_never_wins_and_surfaces_uncertain(self):
        self.fence.reserve(_key())
        r2 = self.fence.reserve(_key())
        self.assertFalse(r2.won)
        self.assertEqual(r2.prior_state, FENCE_RESERVED)
        self.assertEqual(r2.current_state, FENCE_UNCERTAIN)
        self.assertTrue(r2.needs_reconcile)
        # A third attempt still never-wins (now uncertain).
        r3 = self.fence.reserve(_key())
        self.assertFalse(r3.won)
        self.assertEqual(r3.prior_state, FENCE_UNCERTAIN)

    def test_delivered_key_never_wins(self):
        self.fence.reserve(_key())
        self.assertTrue(self.fence.mark_delivered(_key()))
        r = self.fence.reserve(_key())
        self.assertFalse(r.won)
        self.assertEqual(r.prior_state, FENCE_DELIVERED)

    def test_cancelled_key_never_wins(self):
        self.fence.reserve(_key())
        self.assertTrue(self.fence.mark_cancelled(_key()))
        r = self.fence.reserve(_key())
        self.assertFalse(r.won)
        self.assertEqual(r.prior_state, FENCE_CANCELLED)

    def test_distinct_action_id_is_a_new_key(self):
        # A reconcile + new action_id is a distinct key -> a fresh win (design: one send).
        self.fence.reserve(_key())
        self.fence.mark_uncertain(_key())
        r = self.fence.reserve(_key(action_id="act-2"))
        self.assertTrue(r.won)

    def test_state_of(self):
        self.assertEqual(self.fence.state_of(_key()), FENCE_ABSENT)
        self.fence.reserve(_key())
        self.assertEqual(self.fence.state_of(_key()), FENCE_RESERVED)
        self.fence.mark_delivered(_key())
        self.assertEqual(self.fence.state_of(_key()), FENCE_DELIVERED)

    def test_concurrent_reserves_single_winner(self):
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            # Each thread its own store handle over the same DB path.
            fence = DispatchOutboxFence(home=self.home)
            results.append(fence.reserve(_key()).won)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for won in results if won), 1, results)


class CorruptStoreTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_corrupt_file_fails_closed(self):
        path = dispatch_outbox_fence_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(DispatchOutboxFenceError):
            DispatchOutboxFence(home=self.home).reserve(_key())

    def test_unrecognized_schema_version_fails_closed(self):
        path = dispatch_outbox_fence_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        with self.assertRaises(DispatchOutboxFenceError):
            DispatchOutboxFence(home=self.home).reserve(_key())

    def test_replacement_with_delivered_key_never_sends(self):
        # A store "replaced" with one that already has the key delivered -> never-send.
        fence = DispatchOutboxFence(home=self.home)
        fence.reserve(_key())
        fence.mark_delivered(_key())
        # Simulate a fresh caller against the same (replaced) DB.
        r = DispatchOutboxFence(home=self.home).reserve(_key())
        self.assertFalse(r.won)
        self.assertEqual(r.prior_state, FENCE_DELIVERED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
