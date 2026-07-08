"""Lane metadata record component tests (Redmine #13356, design j#73386 Q2).

Pins the native ``state.sqlite`` component: upsert / tombstone / fail-open reads,
the shared container guard, the ``state_schema_components`` registration
(``operator_current_state``, no ``migrated_from``), coexistence with the #12305
legacy-import migrator, and the never-raises best-effort command-boundary
wrappers.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_metadata import (  # noqa: E402
    LANE_METADATA_COMPONENT,
    LANE_METADATA_RECOVERY_POLICY,
    LANE_STATUS_ACTIVE,
    LANE_STATUS_RETIRED,
    LaneMetadataRecord,
    LaneMetadataStore,
    lane_metadata_path,
    load_lane_records,
    record_lane_created,
    record_lane_retired,
)
from mozyo_bridge.core.state.state_store import (  # noqa: E402
    STATE_CONTAINER_VERSION,
    STATE_STORE_FILENAME,
    StateStore,
)


class LaneMetadataStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _store(self) -> LaneMetadataStore:
        return LaneMetadataStore(home=self.home)

    def test_lives_in_the_consolidated_state_db(self) -> None:
        self.assertEqual(
            lane_metadata_path(self.home), self.home / STATE_STORE_FILENAME
        )

    def test_upsert_creates_container_and_component_row(self) -> None:
        record = self._store().upsert(
            LaneMetadataRecord(
                lane_workspace_token="wt_abc",
                repo_workspace_id="wsMain",
                issue_id="13356",
                lane_label="issue_13356_cockpit_aggregate",
                branch="issue_13356_cockpit_aggregate",
                worktree_path="/work/lane",
            )
        )
        self.assertEqual(record.status, LANE_STATUS_ACTIVE)
        self.assertTrue(record.created_at)
        self.assertTrue(record.updated_at)
        db = lane_metadata_path(self.home)
        self.assertTrue(db.exists())
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                STATE_CONTAINER_VERSION,
            )
            row = conn.execute(
                "SELECT schema_version, owner, recovery_policy, migrated_from "
                "FROM state_schema_components WHERE component = ?",
                (LANE_METADATA_COMPONENT,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], LANE_METADATA_COMPONENT)
        self.assertEqual(row[2], LANE_METADATA_RECOVERY_POLICY)
        # Native post-consolidation component: there is no legacy file.
        self.assertIsNone(row[3])

    def test_load_all_round_trips_and_get(self) -> None:
        self._store().upsert(
            LaneMetadataRecord(
                lane_workspace_token="wt_abc",
                issue_id="13356",
                lane_label="issue_13356_cockpit_aggregate",
            )
        )
        records = self._store().load_all()
        self.assertIn("wt_abc", records)
        got = self._store().get("wt_abc")
        self.assertIsNotNone(got)
        self.assertEqual(got.issue_id, "13356")
        self.assertEqual(got.lane_label, "issue_13356_cockpit_aggregate")

    def test_tombstone_and_revive(self) -> None:
        store = self._store()
        store.upsert(LaneMetadataRecord(lane_workspace_token="wt_abc"))
        self.assertTrue(store.mark_retired("wt_abc"))
        got = store.get("wt_abc")
        self.assertEqual(got.status, LANE_STATUS_RETIRED)
        self.assertTrue(got.retired_at)
        # Tombstones stay resolvable by default; a live-only reader filters.
        self.assertEqual(store.load_all(include_retired=False), {})
        self.assertIn("wt_abc", store.load_all())
        # Re-creating the same lane token revives the record.
        store.upsert(LaneMetadataRecord(lane_workspace_token="wt_abc"))
        revived = store.get("wt_abc")
        self.assertEqual(revived.status, LANE_STATUS_ACTIVE)
        self.assertIsNone(revived.retired_at)

    def test_mark_retired_without_record_is_false(self) -> None:
        self.assertFalse(self._store().mark_retired("wt_never_recorded"))
        self.assertFalse(self._store().mark_retired(""))

    def test_reads_fail_open(self) -> None:
        # Absent DB.
        self.assertEqual(self._store().load_all(), {})
        # Corrupt file.
        db = lane_metadata_path(self.home)
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"not a sqlite file")
        self.assertEqual(self._store().load_all(), {})
        self.assertEqual(load_lane_records(home=self.home), {})

    def test_upsert_rejects_empty_token(self) -> None:
        with self.assertRaises(ValueError):
            self._store().upsert(LaneMetadataRecord(lane_workspace_token=""))

    def test_best_effort_wrappers_never_raise(self) -> None:
        # A home path that is a *file* makes every sqlite open fail; the
        # command-boundary wrappers must swallow it (the actuation never breaks).
        bogus_home = self.home / "not-a-dir"
        bogus_home.write_text("file, not dir")
        self.assertIsNone(
            record_lane_created(
                lane_workspace_token="wt_abc", home=bogus_home
            )
        )
        self.assertFalse(record_lane_retired("wt_abc", home=bogus_home))
        self.assertEqual(load_lane_records(home=bogus_home), {})
        # And the empty-token guard is swallowed too (never raises).
        self.assertIsNone(record_lane_created(lane_workspace_token="", home=self.home))

    def test_coexists_with_legacy_import_migrator(self) -> None:
        # A native component row must not confuse the #12305 legacy planner: the
        # migrator plans only its registered legacy components and read_components
        # simply includes the native row.
        record_lane_created(lane_workspace_token="wt_abc", home=self.home)
        store = StateStore(home=self.home)
        plan = store.plan_migration()
        self.assertEqual({c.component for c in plan.migratable}, set())
        recorded = {row.component for row in store.read_components()}
        self.assertIn(LANE_METADATA_COMPONENT, recorded)

    def test_worktree_path_is_local_only_payload_field(self) -> None:
        # The payload carries worktree_path for LOCAL surfaces; the privacy rule
        # (never into a Redmine journal) is the caller's, pinned in the docstring.
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc", worktree_path="/work/lane"
        )
        self.assertEqual(record.as_payload()["worktree_path"], "/work/lane")

    # -- v2 lane_id (Redmine #13377 shared project workspace model) -------------

    def test_lane_id_round_trips_and_indexes_by_unit(self) -> None:
        from mozyo_bridge.core.state.lane_metadata import lane_records_by_unit

        self._store().upsert(
            LaneMetadataRecord(
                lane_workspace_token="wt_abc",
                repo_workspace_id="wsMain",
                lane_label="issue_13377_shared",
                lane_id="issue_13377_shared",
            )
        )
        records = self._store().load_all()
        self.assertEqual(records["wt_abc"].lane_id, "issue_13377_shared")
        by_unit = lane_records_by_unit(records)
        self.assertIn(("wsMain", "issue_13377_shared"), by_unit)
        # A legacy record (no lane_id) never fabricates a unit key.
        self._store().upsert(
            LaneMetadataRecord(
                lane_workspace_token="wt_legacy", repo_workspace_id="wsMain"
            )
        )
        by_unit = lane_records_by_unit(self._store().load_all())
        self.assertEqual(
            sorted(by_unit), [("wsMain", "issue_13377_shared")]
        )

    def _create_v1_table(self) -> Path:
        """A pre-#13377 container whose lane table lacks the ``lane_id`` column."""
        db = lane_metadata_path(self.home)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            with conn:
                conn.execute(f"PRAGMA user_version = {STATE_CONTAINER_VERSION}")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS state_schema_components ("
                    "component TEXT PRIMARY KEY, schema_version INTEGER, owner TEXT, "
                    "recovery_policy TEXT, migrated_from TEXT, updated_at TEXT)"
                )
                conn.execute(
                    "CREATE TABLE lane_metadata_records ("
                    "lane_workspace_token TEXT PRIMARY KEY, repo_workspace_id TEXT, "
                    "issue_id TEXT, lane_label TEXT, branch TEXT, worktree_path TEXT, "
                    "source_backend TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL, retired_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO lane_metadata_records VALUES "
                    "('wt_old', 'wsMain', '13331', 'issue_13331_x', 'issue_13331_x', "
                    "'/work/lane', 'herdr', 'active', 't', 't', NULL)"
                )
        finally:
            conn.close()
        return db

    def test_v1_table_is_readable_without_migration(self) -> None:
        # A read-only consumer (no write since the upgrade) still resolves legacy
        # records — lane_id defaults to "" (the legacy marker), nothing is lost.
        self._create_v1_table()
        records = self._store().load_all()
        self.assertIn("wt_old", records)
        self.assertEqual(records["wt_old"].lane_id, "")
        self.assertEqual(records["wt_old"].lane_label, "issue_13331_x")

    def test_write_migrates_v1_table_in_place(self) -> None:
        self._create_v1_table()
        self._store().upsert(
            LaneMetadataRecord(
                lane_workspace_token="wt_new",
                repo_workspace_id="wsMain",
                lane_id="issue_13377_shared",
            )
        )
        records = self._store().load_all()
        # The legacy row survives the additive ALTER; the new row carries lane_id.
        self.assertEqual(records["wt_old"].lane_id, "")
        self.assertEqual(records["wt_new"].lane_id, "issue_13377_shared")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
