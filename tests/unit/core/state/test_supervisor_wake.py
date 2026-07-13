"""Supervisor wake store tests (Redmine #13683 Phase A, R1-F2).

The durable local-wake queue: coalesced (same workspace+issue collapses), atomically drained
(single-consumer-safe), best-effort producer, fail-closed schema.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.supervisor_wake import (
    SUPERVISOR_WAKE_SCHEMA_VERSION,
    SupervisorWakeError,
    SupervisorWakeStore,
    WakeHint,
)


class SupervisorWakeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = SupervisorWakeStore(path=self.dir / "supervisor-wake.sqlite")

    def test_enqueue_and_drain(self) -> None:
        self.assertTrue(self.store.enqueue("wsA", "13683"))
        self.assertTrue(self.store.enqueue("wsB", "13684"))
        drained = self.store.drain()
        self.assertEqual(set(h.as_tuple() for h in drained), {("wsA", "13683"), ("wsB", "13684")})
        # Drain consumes: a second drain is empty (single-consumer-safe).
        self.assertEqual(self.store.drain(), ())

    def test_enqueue_coalesces_same_pair(self) -> None:
        self.store.enqueue("wsA", "13683", now="2026-07-13T00:00:00+00:00")
        self.store.enqueue("wsA", "13683", now="2026-07-13T00:00:05+00:00")
        drained = self.store.drain()
        self.assertEqual(len(drained), 1)  # coalesced to one row
        self.assertEqual(drained[0], WakeHint("wsA", "13683"))

    def test_blank_workspace_or_issue_is_noop(self) -> None:
        self.assertFalse(self.store.enqueue("", "13683"))
        self.assertFalse(self.store.enqueue("wsA", "  "))
        self.assertEqual(self.store.pending(), ())

    def test_drain_by_workspace(self) -> None:
        self.store.enqueue("wsA", "1")
        self.store.enqueue("wsB", "2")
        drained_a = self.store.drain(workspace_id="wsA")
        self.assertEqual([h.as_tuple() for h in drained_a], [("wsA", "1")])
        # wsB is untouched.
        self.assertEqual([h.as_tuple() for h in self.store.pending()], [("wsB", "2")])

    def test_pending_is_read_only(self) -> None:
        self.store.enqueue("wsA", "13683")
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(len(self.store.pending()), 1)  # not consumed
        self.assertEqual(len(self.store.drain()), 1)

    def test_absent_db_drains_empty(self) -> None:
        fresh = SupervisorWakeStore(path=self.dir / "never.sqlite")
        self.assertEqual(fresh.drain(), ())
        self.assertEqual(fresh.pending(), ())

    def test_foreign_schema_fails_closed(self) -> None:
        self.store.enqueue("wsA", "1")
        conn = sqlite3.connect(self.dir / "supervisor-wake.sqlite")
        conn.execute(f"PRAGMA user_version = {SUPERVISOR_WAKE_SCHEMA_VERSION + 42}")
        conn.commit()
        conn.close()
        with self.assertRaises(SupervisorWakeError):
            self.store.enqueue("wsB", "2")
        with self.assertRaises(SupervisorWakeError):
            self.store.pending()


if __name__ == "__main__":
    unittest.main()
