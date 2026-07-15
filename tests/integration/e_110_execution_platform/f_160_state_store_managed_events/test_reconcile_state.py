"""Integration tests for the reconcile-state native component (Redmine #13758).

Pins the CAS store over ``state.sqlite``:

- native self-registration (``migrated_from`` NULL, recovery ``rebuildable_cache``);
- ``open_cycle`` INSERT-if-absent, and — the accumulator invariant — a returning wake for
  the same dispatch never resets the failure counter (acceptance §7);
- ``advance`` exact-``expected_revision`` CAS (stale / not-found refusals);
- the downgrade guard: a foreign / newer component version is left byte-unchanged (fail
  closed), and a table without metadata is not silently adopted.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.reconcile_state import (
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    ReconcileStateError,
    ReconcileStateKey,
    ReconcileStateStore,
)
from mozyo_bridge.core.state.reconcile_state_schema import (
    RECONCILE_STATE_COMPONENT,
    RECONCILE_STATE_RECOVERY_POLICY,
    READONLY_COMPONENT_RECOGNIZED,
    READONLY_COMPONENT_UNSUPPORTED,
    readonly_component_status,
)

KEY = ReconcileStateKey(workspace_id="ws1", lane_id="lane-a", dispatch_anchor="13758:79337")


class SelfRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(_TmpDir()))
        self.path = self.tmp / "state.sqlite"
        self.store = ReconcileStateStore(path=self.path)

    def test_ensure_schema_registers_native_component(self):
        self.store.ensure_schema()
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT schema_version, owner, recovery_policy, migrated_from "
                "FROM state_schema_components WHERE component = ?",
                (RECONCILE_STATE_COMPONENT,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 1)
            self.assertEqual(row[2], RECONCILE_STATE_RECOVERY_POLICY)  # rebuildable_cache
            self.assertIsNone(row[3])  # migrated_from NULL (native)
            self.assertEqual(readonly_component_status(conn), READONLY_COMPONENT_RECOGNIZED)
        finally:
            conn.close()

    def test_table_without_metadata_is_not_adopted(self):
        # A raw table with no component row is a partial / unknown state -> fail closed.
        from mozyo_bridge.core.state.state_store import connect_state_container_rw

        conn = connect_state_container_rw(self.path)
        conn.execute(
            "CREATE TABLE reconcile_state_records (workspace_id TEXT, lane_id TEXT, "
            "dispatch_anchor TEXT, PRIMARY KEY (workspace_id, lane_id, dispatch_anchor))"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(ReconcileStateError):
            self.store.ensure_schema()

    def test_newer_component_version_fails_closed_untouched(self):
        self.store.ensure_schema()
        conn = sqlite3.connect(self.path)
        conn.execute(
            "UPDATE state_schema_components SET schema_version = 99 WHERE component = ?",
            (RECONCILE_STATE_COMPONENT,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(ReconcileStateError):
            self.store.ensure_schema()
        # And the read-side agrees.
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                readonly_component_status(conn), READONLY_COMPONENT_UNSUPPORTED
            )
        finally:
            conn.close()


class OpenCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(_TmpDir()))
        self.store = ReconcileStateStore(path=self.tmp / "state.sqlite")

    def test_open_cycle_inserts_fresh_record(self):
        out = self.store.open_cycle(
            KEY,
            lane_generation=2,
            issue_id="13758",
            expected_gate="implementation_done",
            expected_next_owner="implementation_worker",
            phase="turn_ended_gate_pending",
        )
        self.assertTrue(out.applied)
        self.assertEqual(out.revision, 1)
        rec = self.store.get(KEY)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.reconcile_failure_count, 0)
        self.assertEqual(rec.lane_generation, 2)
        self.assertEqual(rec.expected_next_owner, "implementation_worker")
        self.assertEqual(rec.phase, "turn_ended_gate_pending")

    def test_reopen_never_resets_the_failure_counter(self):
        # §7: a duplicate wake for the same dispatch must not reset the accumulator.
        self.store.open_cycle(KEY, phase="turn_ended_gate_pending")
        self.store.advance(
            KEY, expected_revision=1, next_phase="self_heal_attempt_1", next_failure_count=1
        )
        again = self.store.open_cycle(KEY, phase="turn_ended_gate_pending")
        self.assertFalse(again.applied)
        self.assertEqual(again.reason, CAS_UNEXPECTED_STATE)
        rec = self.store.get(KEY)
        self.assertEqual(rec.reconcile_failure_count, 1)  # preserved, not reset to 0
        self.assertEqual(rec.phase, "self_heal_attempt_1")

    def test_open_cycle_rejects_bool_generation(self):
        with self.assertRaises(ValueError):
            self.store.open_cycle(KEY, lane_generation=True)


class AdvanceCasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(_TmpDir()))
        self.store = ReconcileStateStore(path=self.tmp / "state.sqlite")
        self.store.open_cycle(KEY, phase="turn_ended_gate_pending")

    def test_advance_bumps_revision(self):
        out = self.store.advance(
            KEY, expected_revision=1, next_phase="self_heal_attempt_1", next_failure_count=1
        )
        self.assertTrue(out.applied)
        self.assertEqual(out.revision, 2)
        self.assertEqual(self.store.get(KEY).revision, 2)

    def test_stale_revision_is_refused(self):
        self.store.advance(
            KEY, expected_revision=1, next_phase="self_heal_attempt_1", next_failure_count=1
        )
        stale = self.store.advance(
            KEY, expected_revision=1, next_phase="self_heal_attempt_2", next_failure_count=2
        )
        self.assertFalse(stale.applied)
        self.assertEqual(stale.reason, CAS_STALE_REVISION)
        self.assertEqual(self.store.get(KEY).reconcile_failure_count, 1)  # unchanged

    def test_advance_missing_row_is_not_found(self):
        other = ReconcileStateKey(
            workspace_id="ws1", lane_id="lane-z", dispatch_anchor="1:1"
        )
        out = self.store.advance(
            other, expected_revision=1, next_phase="closed", next_failure_count=0
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_advance_persists_escalated_flag_and_disposition(self):
        self.store.advance(
            KEY,
            expected_revision=1,
            next_phase="coordinator_escalation",
            next_failure_count=3,
            escalated=True,
            last_disposition="reconcile_three_strike",
            callback_outbox_state="pending",
        )
        rec = self.store.get(KEY)
        self.assertTrue(rec.escalated)
        self.assertEqual(rec.last_disposition, "reconcile_three_strike")
        self.assertEqual(rec.callback_outbox_state, "pending")

    def test_advance_rejects_bool_revision(self):
        with self.assertRaises(ValueError):
            self.store.advance(
                KEY, expected_revision=True, next_phase="closed", next_failure_count=0
            )


class _TmpDir:
    """A tiny ``enterContext``-compatible temp dir (no external deps)."""

    def __enter__(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        return self._td.name

    def __exit__(self, *exc):
        self._td.cleanup()
        return False


if __name__ == "__main__":
    unittest.main()
