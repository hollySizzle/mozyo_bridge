"""Reconcile-state NON-CREATING read contract tests (Redmine #15747).

``ReconcileStateStore.records_readonly`` is the read the glance family uses instead of
``records()`` (whose ``_connect`` runs the read-write schema ensure and mints
``state.sqlite`` in a fresh home — the #15711 j#108206 measured side effect). These tests
pin the pure decision surface of the new read:

- an absent state file yields the typed empty ``()`` and touches NOTHING on the
  filesystem — no file, and not even the missing parent directories;
- a recognized container with no ``reconcile_state`` component yet yields ``()`` and
  leaves the container byte-identical;
- a recognized component's rows read back exactly as ``records()`` reads them (parity);
- an unknown / newer component version, and a non-SQLite file, yield ``None`` (fail
  closed — the same downgrade guard as the write path, never a silent rebuild).
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.reconcile_state import (
    ReconcileStateKey,
    ReconcileStateStore,
)
from mozyo_bridge.core.state.state_store import connect_state_container_rw


def _key(anchor: str = "j#100") -> ReconcileStateKey:
    return ReconcileStateKey(
        workspace_id="ws-15747", lane_id="issue_15747_lane", dispatch_anchor=anchor
    )


class AbsentStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_absent_file_reads_as_typed_empty(self):
        store = ReconcileStateStore(path=self.root / "state.sqlite")
        self.assertEqual(store.records_readonly(), ())

    def test_absent_file_creates_nothing(self):
        path = self.root / "state.sqlite"
        store = ReconcileStateStore(path=path)
        store.records_readonly()
        self.assertEqual(sorted(self.root.rglob("*")), [])

    def test_absent_parent_directories_are_not_created(self):
        # The defect's home shape: a fresh home whose directories do not exist yet.
        path = self.root / "home" / "nested" / "state.sqlite"
        store = ReconcileStateStore(path=path)
        self.assertEqual(store.records_readonly(), ())
        self.assertFalse((self.root / "home").exists())


class ExistingContainerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.sqlite"

    def _digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_container_without_component_reads_empty_and_writes_nothing(self):
        # A container another component created, with no reconcile_state row yet.
        connect_state_container_rw(self.path).close()
        before = self._digest()
        store = ReconcileStateStore(path=self.path)
        self.assertEqual(store.records_readonly(), ())
        self.assertEqual(self._digest(), before)

    def test_recognized_component_rows_match_the_rw_read(self):
        store = ReconcileStateStore(path=self.path)
        store.open_cycle(_key("j#100"), issue_id="15747", expected_gate="review_request")
        store.open_cycle(_key("j#200"), issue_id="15748")
        self.assertEqual(store.records_readonly(), store.records())

    def test_unknown_newer_component_version_fails_closed_to_none(self):
        store = ReconcileStateStore(path=self.path)
        store.open_cycle(_key())
        conn = connect_state_container_rw(self.path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 99 "
                "WHERE component = 'reconcile_state'"
            )
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(store.records_readonly())

    def test_non_sqlite_file_fails_closed_to_none(self):
        self.path.write_text("not a sqlite database", encoding="utf-8")
        store = ReconcileStateStore(path=self.path)
        self.assertIsNone(store.records_readonly())

    def test_schema_check_and_fetch_share_one_snapshot(self):
        # Review j#108779 finding_readonlyschemasnapshotrace + round-2
        # finding_regressiontestallowstwosnapshotrace: the interleaving writer must
        # change BOTH the schema version AND a row sentinel in one atomic commit —
        # a metadata-only upgrade left the rows identical, so the pre-fix
        # two-snapshot read still passed (a dead pin). With the sentinel, the old
        # implementation fetches the post-upgrade row under a pre-upgrade verdict
        # and fails; the fixed single-snapshot read never sees the sentinel.
        import sqlite3
        from unittest import mock

        from mozyo_bridge.core.state import reconcile_state as module

        store = ReconcileStateStore(path=self.path)
        store.open_cycle(_key("j#100"), issue_id="15747")
        baseline = store.records()
        sentinel = "SENTINEL-UPGRADED-99"
        real_status = module.readonly_component_status
        upgrade_error: list[Exception] = []

        def interleaving_status(conn):
            status = real_status(conn)
            writer = sqlite3.connect(str(self.path), timeout=0.05)
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "UPDATE state_schema_components SET schema_version = 99 "
                    "WHERE component = 'reconcile_state'"
                )
                writer.execute(
                    "UPDATE reconcile_state_records SET issue_id = ?", (sentinel,)
                )
                writer.commit()
            except sqlite3.OperationalError as exc:
                upgrade_error.append(exc)
            finally:
                writer.close()
            return status

        with mock.patch.object(
            module, "readonly_component_status", interleaving_status
        ):
            result = store.records_readonly()

        if upgrade_error:
            # The reader's shared lock held the writer out: that is only the
            # single-snapshot behavior when the refusal is a lock/busy refusal.
            message = str(upgrade_error[0]).lower()
            self.assertTrue(
                "locked" in message or "busy" in message,
                f"unexpected writer refusal: {upgrade_error[0]!r}",
            )
            self.assertEqual(result, baseline)
        else:
            # The upgrade committed (WAL): the read must stay on its pre-upgrade
            # snapshot (baseline rows) or fail closed — a sentinel row under a
            # recognized verdict is exactly the two-snapshot race.
            self.assertIn(result, (baseline, None))
        if result:
            self.assertFalse(
                any(record.issue_id == sentinel for record in result),
                "post-upgrade sentinel rows leaked into a recognized-verdict read",
            )
        if not upgrade_error:
            # A FRESH read after the committed upgrade honors the downgrade guard.
            self.assertIsNone(store.records_readonly())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
