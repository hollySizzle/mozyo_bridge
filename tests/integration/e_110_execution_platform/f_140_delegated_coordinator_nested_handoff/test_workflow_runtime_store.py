"""Workflow runtime DB store tests (Redmine #12671).

Pins the home-scoped ``workflow-runtime.sqlite`` store that holds the mozyo-DB runtime
state (``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### 設計思想`:
"mozyo DB: workflow runtime state、pending delivery、route identity、duplicate
suppression"):

- event append / read round-trip preserves the durable facts and the apply order;
- re-appending the same ``event_id`` upserts in place (idempotent) and keeps its seq;
- route identities round-trip issue-tagged with ``last_seen_pane_id`` as a cache column;
- a route identity missing a stable field fails closed (a pane id is never the authority);
- advisory meta round-trips;
- an absent DB reads as empty; an unsupported container version fails closed.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workflow_runtime_store import (
    META_CAPACITY,
    WorkflowRuntimeStore,
    WorkflowRuntimeStoreError,
)


class _StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=self.path)


class EventRoundTripTest(_StoreTestCase):
    def test_append_and_read_preserves_order_and_facts(self):
        self.store.append_events(
            [
                {"event_id": "12671:1", "issue": "12671", "gate": "review_request", "commit_bearing": True},
                {"event_id": "12671:2", "issue": "12671", "gate": "review", "review_conclusion": "approved"},
            ]
        )
        rows = self.store.read_events()
        self.assertEqual([r.event_id for r in rows], ["12671:1", "12671:2"])
        self.assertTrue(rows[0].commit_bearing)
        self.assertEqual(rows[1].review_conclusion, "approved")
        self.assertTrue(rows[1].issue_open)  # default True when omitted

    def test_reappending_same_event_id_is_idempotent_upsert(self):
        self.store.append_events([{"event_id": "e1", "issue": "12671", "gate": "start"}])
        self.store.append_events([{"event_id": "e2", "issue": "12671", "gate": "review_request"}])
        # Re-append e1 with a changed fact; seq order must be preserved, no duplicate row.
        self.store.append_events([{"event_id": "e1", "issue": "12671", "gate": "progress"}])
        rows = self.store.read_events()
        self.assertEqual([r.event_id for r in rows], ["e1", "e2"])
        self.assertEqual(rows[0].gate, "progress")  # upserted in place

    def test_event_without_issue_fails_closed(self):
        with self.assertRaises(WorkflowRuntimeStoreError):
            self.store.append_events([{"event_id": "x", "issue": ""}])


class RouteRoundTripTest(_StoreTestCase):
    def test_route_identity_round_trips_with_pane_id_as_cache(self):
        self.store.put_route_identities(
            [
                {
                    "route_id": "r-12671",
                    "issue": "12671",
                    "workspace_id": "ws1",
                    "role": "codex",
                    "pane_name": "gw-12671",
                    "last_seen_pane_id": "%17",
                }
            ]
        )
        rows = self.store.read_route_identities()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.issue, "12671")
        self.assertEqual(row.lane_id, "default")  # defaulted
        self.assertEqual(row.last_seen_pane_id, "%17")  # cache column persisted
        # The record round-trips for the ledger's from_record (cache included).
        self.assertEqual(row.as_record()["last_seen_pane_id"], "%17")

    def test_route_identity_missing_stable_field_fails_closed(self):
        with self.assertRaises(WorkflowRuntimeStoreError):
            self.store.put_route_identities(
                [{"route_id": "r", "issue": "12671", "workspace_id": "", "role": "codex", "pane_name": "p"}]
            )

    def test_reput_route_upserts_in_place(self):
        base = {"route_id": "r", "issue": "12671", "workspace_id": "ws1", "role": "codex", "pane_name": "p"}
        self.store.put_route_identities([base])
        self.store.put_route_identities([{**base, "pane_name": "p2"}])
        rows = self.store.read_route_identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pane_name, "p2")


class MetaTest(_StoreTestCase):
    def test_meta_round_trips(self):
        self.store.set_meta({META_CAPACITY: 3, "owner_or_release_gate_active": True})
        meta = self.store.read_meta()
        self.assertEqual(meta[META_CAPACITY], "3")
        self.assertEqual(meta["owner_or_release_gate_active"], "True")


class AbsentAndVersionTest(_StoreTestCase):
    def test_absent_db_reads_empty(self):
        self.assertEqual(self.store.read_events(), ())
        self.assertEqual(self.store.read_route_identities(), ())
        self.assertEqual(self.store.read_meta(), {})

    def test_unsupported_version_fails_closed(self):
        # Create the DB, then stamp an unknown future container version.
        self.store.append_events([{"event_id": "e", "issue": "12671", "gate": "start"}])
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        with self.assertRaises(WorkflowRuntimeStoreError):
            self.store.read_events()
        with self.assertRaises(WorkflowRuntimeStoreError):
            self.store.append_events([{"event_id": "e2", "issue": "12671", "gate": "start"}])


class GenerationLeaseTest(_StoreTestCase):
    """#13518 review R3-F2: durable single-consumer review-generation lease (CAS)."""

    def test_acquire_is_cas_single_consumer(self):
        key = "13586:75719:deadbeef"
        self.assertTrue(self.store.acquire_generation_lease(key, "A"))
        self.assertTrue(self.store.acquire_generation_lease(key, "A"))  # idempotent for same holder
        self.assertFalse(self.store.acquire_generation_lease(key, "B"))  # different holder refused
        self.assertEqual(self.store.generation_lease_holder(key), "A")

    def test_blank_holder_never_acquires(self):
        self.assertFalse(self.store.acquire_generation_lease("k", ""))
        self.assertIsNone(self.store.generation_lease_holder("k"))

    def test_lease_is_durable_across_instances(self):
        key = "13586:75719:deadbeef"
        self.assertTrue(self.store.acquire_generation_lease(key, "A"))
        reopened = WorkflowRuntimeStore(path=self.path)
        self.assertEqual(reopened.generation_lease_holder(key), "A")
        self.assertFalse(reopened.acquire_generation_lease(key, "B"))

    def test_lease_rows_excluded_from_advisory_meta(self):
        self.store.acquire_generation_lease("13586:75719:deadbeef", "A")
        self.store.set_meta({"ready_independent": "true"})
        meta = self.store.read_meta()
        self.assertIn("ready_independent", meta)
        self.assertFalse(any(k.startswith("genlease:") for k in meta))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
