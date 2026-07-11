"""Callback outbox store tests (Redmine #13520 / US #13518, schema v2).

Pins the zero-wait callback delivery bounded context over ``workflow-runtime.sqlite`` (design
answer j#75098 Q3): a UNIQUE-fenced, ``BEGIN IMMEDIATE`` callback outbox that never duplicates
a delivery across a watcher restart / duplicate event / concurrent claim, plus the explicit
v1->v2 migration that preserves the existing runtime state.

Verification matrix (j#75098):

- v1 fixture -> v2 migration preserves events / routes / meta (data preservation);
- enqueue is idempotent on the UNIQUE key (duplicate event -> one row);
- claim is a single winner (a second claim sees nothing);
- delivered / known-not-sent bounded retry -> dead_letter / uncertain-no-retry;
- inflight recovery: pre-send -> pending (retry), post-send -> uncertain (no retry);
- cursor round-trips (efficiency filter, not authority);
- a foreign / downgraded version fails closed; a pre-migration v1 DB reads callbacks empty.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_DELIVERED,
    CALLBACK_INFLIGHT,
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
    WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION,
    WorkflowRuntimeStore,
    WorkflowRuntimeStoreError,
)


def _key(journal: str, gate: str = "implementation_done", route: str = "coordinator") -> CallbackOutboxKey:
    return CallbackOutboxKey(
        source="redmine",
        issue="13518",
        journal=journal,
        normalized_gate=gate,
        callback_route=route,
    )


class _OutboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "workflow-runtime.sqlite"
        self.outbox = CallbackOutbox(path=self.path)


class EnqueueIdempotencyTest(_OutboxTestCase):
    def test_fresh_enqueue_inserts_pending(self):
        result = self.outbox.enqueue(_key("75094"))
        self.assertTrue(result.inserted)
        self.assertEqual(result.current_state, CALLBACK_PENDING)
        rows = self.outbox.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, CALLBACK_PENDING)

    def test_duplicate_key_enqueues_no_new_row(self):
        self.outbox.enqueue(_key("75094"))
        again = self.outbox.enqueue(_key("75094"))
        self.assertFalse(again.inserted)
        self.assertEqual(len(self.outbox.read()), 1)

    def test_duplicate_does_not_reset_a_delivered_row(self):
        k = _key("75094")
        self.outbox.enqueue(k)
        self.outbox.claim_pending()
        self.outbox.mark_delivered(k)
        again = self.outbox.enqueue(k)
        self.assertFalse(again.inserted)
        self.assertEqual(again.current_state, CALLBACK_DELIVERED)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_distinct_gate_or_route_is_a_distinct_row(self):
        self.outbox.enqueue(_key("75094", gate="implementation_done"))
        self.outbox.enqueue(_key("75094", gate="review_request"))
        self.outbox.enqueue(_key("75094", route="delegated_coordinator"))
        self.assertEqual(len(self.outbox.read()), 3)

    def test_empty_key_field_fails_closed(self):
        with self.assertRaises(WorkflowRuntimeStoreError):
            self.outbox.enqueue(_key(""))


class ClaimSingleWinnerTest(_OutboxTestCase):
    def test_claim_moves_pending_to_inflight_and_second_claim_is_empty(self):
        self.outbox.enqueue(_key("75094"))
        self.outbox.enqueue(_key("75096", gate="review_request"))
        first = self.outbox.claim_pending()
        self.assertEqual({r.state for r in first}, {CALLBACK_INFLIGHT})
        self.assertEqual(len(first), 2)
        second = self.outbox.claim_pending()
        self.assertEqual(second, ())

    def test_claim_respects_limit_and_seq_order(self):
        for j in ("75094", "75095", "75096"):
            self.outbox.enqueue(_key(j))
        claimed = self.outbox.claim_pending(limit=2)
        self.assertEqual([r.journal for r in claimed], ["75094", "75095"])


class DeliveryTransitionsTest(_OutboxTestCase):
    def test_delivered(self):
        k = _key("75094")
        self.outbox.enqueue(k)
        self.outbox.claim_pending()
        self.assertTrue(self.outbox.mark_delivered(k))
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_known_not_sent_retries_bounded_then_dead_letters(self):
        k = _key("75094")
        self.outbox.enqueue(k, max_attempts=2)
        self.outbox.claim_pending()
        self.assertEqual(self.outbox.mark_retry_or_dead(k), CALLBACK_PENDING)
        self.outbox.claim_pending()
        self.assertEqual(self.outbox.mark_retry_or_dead(k), CALLBACK_DEAD_LETTER)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)

    def test_uncertain_is_terminal_and_never_reclaimed(self):
        k = _key("75094")
        self.outbox.enqueue(k)
        self.outbox.claim_pending()
        self.outbox.mark_uncertain(k)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_UNCERTAIN)
        self.assertEqual(self.outbox.claim_pending(), ())


class InflightRecoveryTest(_OutboxTestCase):
    def test_pre_send_crash_recovers_to_pending(self):
        k = _key("75094")
        self.outbox.enqueue(k)
        self.outbox.claim_pending()  # inflight, send_attempted=0
        recovered = self.outbox.recover_inflight()
        self.assertEqual([r.state for r in recovered], [CALLBACK_PENDING])
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_PENDING)

    def test_post_send_crash_recovers_to_uncertain(self):
        k = _key("75094")
        self.outbox.enqueue(k)
        self.outbox.claim_pending()
        self.outbox.mark_sending(k)  # crossed the send edge
        recovered = self.outbox.recover_inflight()
        self.assertEqual([r.state for r in recovered], [CALLBACK_UNCERTAIN])
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_UNCERTAIN)

    def test_recover_is_noop_without_inflight(self):
        self.outbox.enqueue(_key("75094"))
        self.assertEqual(self.outbox.recover_inflight(), ())


class CursorTest(_OutboxTestCase):
    def test_cursor_round_trips_and_advances_even_on_duplicate(self):
        k = _key("75094")
        self.outbox.enqueue(k, cursor_source="redmine", cursor="75094")
        self.assertEqual(self.outbox.read_cursor("redmine"), "75094")
        self.outbox.enqueue(k, cursor_source="redmine", cursor="75096")
        self.assertEqual(self.outbox.read_cursor("redmine"), "75096")

    def test_absent_cursor_is_none(self):
        self.assertIsNone(self.outbox.read_cursor("redmine"))


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "workflow-runtime.sqlite"

    def _write_v1_db_with_an_event(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE workflow_events (event_id TEXT PRIMARY KEY, issue TEXT NOT NULL, "
            "gate TEXT NOT NULL, review_conclusion TEXT NOT NULL, callback_state TEXT NOT NULL, "
            "commit_bearing INTEGER NOT NULL DEFAULT 0, integration_recorded INTEGER NOT NULL "
            "DEFAULT 0, issue_open INTEGER NOT NULL DEFAULT 1, blocker_recorded INTEGER NOT NULL "
            "DEFAULT 0, seq INTEGER NOT NULL, recorded_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE workflow_route_identities (route_id TEXT PRIMARY KEY, issue TEXT, "
            "workspace_id TEXT, lane_id TEXT, role TEXT, pane_name TEXT, last_seen_pane_id TEXT, "
            "observed_at TEXT, recorded_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE workflow_runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO workflow_events (event_id, issue, gate, review_conclusion, "
            "callback_state, seq, recorded_at) VALUES "
            "('redmine:13518:75094', '13518', 'implementation_done', 'pending', 'none', 0, 'x')"
        )
        conn.execute(
            "INSERT INTO workflow_runtime_meta (key, value, updated_at) "
            "VALUES ('capacity_remaining', '3', 'x')"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

    def test_v1_reads_still_work_before_migration(self):
        self._write_v1_db_with_an_event()
        store = WorkflowRuntimeStore(path=self.path)
        outbox = CallbackOutbox(path=self.path)
        # Legacy tables read fine on a v1 DB (backward-compatible read).
        self.assertEqual(len(store.read_events()), 1)
        self.assertEqual(store.read_meta().get("capacity_remaining"), "3")
        # The callback table does not exist yet on a v1 DB -> callbacks read empty.
        self.assertEqual(outbox.read(), ())
        self.assertIsNone(outbox.read_cursor("redmine"))

    def test_v1_to_v2_migration_preserves_existing_state(self):
        self._write_v1_db_with_an_event()
        store = WorkflowRuntimeStore(path=self.path)
        outbox = CallbackOutbox(path=self.path)
        # A callback write triggers the explicit v1 -> v2 migration.
        outbox.enqueue(_key("75200"))
        conn = sqlite3.connect(self.path)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        conn.close()
        self.assertEqual(version, WORKFLOW_RUNTIME_STORE_SCHEMA_VERSION)
        # The pre-existing event / meta survived (data preservation).
        self.assertEqual(len(store.read_events()), 1)
        self.assertEqual(store.read_events()[0].event_id, "redmine:13518:75094")
        self.assertEqual(store.read_meta().get("capacity_remaining"), "3")
        self.assertEqual(len(outbox.read()), 1)

    def test_foreign_future_version_fails_closed(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        outbox = CallbackOutbox(path=self.path)
        with self.assertRaises(WorkflowRuntimeStoreError):
            outbox.read()
        with self.assertRaises(WorkflowRuntimeStoreError):
            outbox.enqueue(_key("75094"))


if __name__ == "__main__":
    unittest.main()
