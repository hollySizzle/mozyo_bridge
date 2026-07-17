"""Redmine #13892 R2-F1 — the by-target obligation read must fail closed on store damage.

The first cut gated on ``is_bootstrapped()``, whose ``False`` is a fail-soft catch-all: it
covers a genuinely uninitialized store AND five damage shapes. Every one of them was reported
as "no obligations owed", so a damaged store silently became permission to close panes over
unknown owed work. These pin the tri-state judgment instead.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    DispatchOutboxFenceError,
    FenceKey,
    TargetObligation,
)

WS = "ws1"
TARGET = "mzb1_ws1_claude_lane1"


class OutboxObligationReadTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        self.fence = DispatchOutboxFence(home=self.home)

    def _read(self):
        return self.fence.obligations_for_targets(
            workspace_id=WS, target_assigned_names=(TARGET,)
        )

    def test_genuinely_uninitialized_is_the_only_positive_absence(self):
        self.assertEqual(self._read(), (), "both artifacts absent: nothing can be owed")

    def test_bootstrapped_and_empty_reports_no_obligations(self):
        self.fence.bootstrap()
        self.assertEqual(self._read(), ())

    def test_reserved_row_is_reported_with_causal_identity(self):
        self.fence.bootstrap()
        self.fence.reserve(FenceKey(WS, "lane1", "13999", "42", "act1", TARGET))
        rows = self._read()
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0], TargetObligation)
        self.assertTrue(rows[0].non_terminal)
        self.assertEqual(rows[0].issue, "13999")
        self.assertEqual(rows[0].journal, "42", "identity must survive the read (R2-F2)")

    def test_delivered_row_is_returned_not_filtered_away(self):
        """A delivery ACK is not task completion: the caller must see it and correlate."""
        self.fence.bootstrap()
        key = FenceKey(WS, "lane1", "13999", "42", "act1", TARGET)
        self.fence.reserve(key)
        self.fence.mark_delivered(key)
        rows = self._read()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].non_terminal)
        self.assertTrue(rows[0].needs_gate_correlation)

    # -- the five damage shapes that used to read as "no obligations" ------

    def _damage(self, fn):
        self.fence.bootstrap()
        fn()
        with self.assertRaises(DispatchOutboxFenceError):
            self._read()

    def test_db_lost_sidecar_remains_fails_closed(self):
        self._damage(lambda: self.fence.path.unlink())

    def test_sidecar_lost_db_remains_fails_closed(self):
        self._damage(lambda: self.fence.sidecar_path.unlink())

    def test_corrupt_db_fails_closed(self):
        self._damage(lambda: self.fence.path.write_bytes(b"not a sqlite file"))

    def test_nonce_mismatch_fails_closed(self):
        self._damage(lambda: self.fence.sidecar_path.write_text("deadbeef"))

    def test_unknown_schema_fails_closed(self):
        def bump():
            conn = sqlite3.connect(self.fence.path)
            conn.execute("PRAGMA user_version = 9999")
            conn.commit()
            conn.close()

        self._damage(bump)


if __name__ == "__main__":
    unittest.main()
