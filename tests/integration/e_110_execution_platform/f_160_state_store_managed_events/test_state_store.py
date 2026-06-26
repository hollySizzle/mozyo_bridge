"""Home-scoped state store facade / migration tests (Redmine #12305).

Pins the #12305 acceptance: the dry-run planner and the write migration's
backup-first / idempotent / downgrade-aware / non-destructive invariants, the
component owner / namespace boundary (legacy tables land in their namespaced
target tables, never a cross-namespace JOIN), and the separately-gated
destructive cleanup. Design anchor: `vibes/docs/logics/managed-state-model.md`
(`### home-scoped single SQLite 統合方針`, `### migration / doctor / integrity
check 方針`).

Legacy fixtures are built with the *real* sibling writers so the test tracks each
store's true schema version, not a hand-rolled copy.
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

# Self-contained src bootstrap so isolated discovery (unittest discover
# scoped to this subpackage or a single file) imports mozyo_bridge without
# relying on a sibling test inserting src first (Redmine #12490 j#64426).
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from mozyo_bridge.application.commands_state import (
    cmd_state_cleanup,
    cmd_state_inspect,
    cmd_state_migrate,
)
from mozyo_bridge.managed_events import record_managed_event
from mozyo_bridge.otel_store import OtelEvent, OtelEventStore
from mozyo_bridge.session_inventory import save_snapshot
from mozyo_bridge.state_store import (
    ACTION_BLOCKED_CORRUPT,
    ACTION_BLOCKED_INCOMPLETE,
    ACTION_BLOCKED_UNSUPPORTED,
    ACTION_MIGRATE,
    ACTION_SKIP_ABSENT,
    ACTION_SKIP_COMPLETE,
    COMPONENT_NAMES,
    STATE_CONTAINER_VERSION,
    STATE_STORE_FILENAME,
    StateStore,
    StateStoreError,
    state_store_path,
)
from mozyo_bridge.workspace_registry import register_workspace

LEGACY_FILES = {
    "registry": "registry.sqlite",
    "managed_events": "managed-events.sqlite",
    "inventory": "inventory.sqlite",
    "otel": "otel-events.sqlite",
}


def _write_valid_legacy(home: Path) -> None:
    """Create every legacy file at its current schema using the real writers."""
    repo = home / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    register_workspace(repo, home=home)
    save_snapshot([], home=home)
    record_managed_event(command="mozyo", event_kind="created", home=home)
    otel = OtelEventStore(home=home)
    otel.insert_events([OtelEvent(signal="logs", event_name="probe")])
    otel.close()


def _table_count(path: Path, table: str) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if table not in present:
            return -1
        return int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
    finally:
        conn.close()


def _file_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _by_component(plan) -> dict:
    return {c.component: c for c in plan.components}


class StateStorePlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_plan_absent_home_all_skip_absent_creates_nothing(self) -> None:
        store = StateStore(home=self.home)
        plan = store.plan_migration()
        by = _by_component(plan)
        self.assertEqual(set(by), set(COMPONENT_NAMES))
        for name in COMPONENT_NAMES:
            self.assertEqual(by[name].action, ACTION_SKIP_ABSENT, name)
            self.assertIsNone(by[name].source_rows, name)
        self.assertEqual(plan.migratable, ())
        self.assertFalse(plan.db_present)
        # strictly read-only: neither the home dir nor the single DB is created.
        self.assertFalse(self.home.exists())

    def test_plan_valid_legacy_all_migrate_with_row_counts(self) -> None:
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        plan = store.plan_migration()
        by = _by_component(plan)
        for name in COMPONENT_NAMES:
            self.assertEqual(by[name].action, ACTION_MIGRATE, name)
            self.assertTrue(by[name].legacy_present, name)
            self.assertIsInstance(by[name].source_rows, int, name)
        # planning never creates the single DB.
        self.assertFalse((self.home / STATE_STORE_FILENAME).exists())

    def test_plan_corrupt_legacy_is_blocked_not_migrate(self) -> None:
        _write_valid_legacy(self.home)
        (self.home / LEGACY_FILES["managed_events"]).write_bytes(b"not a database")
        plan = StateStore(home=self.home).plan_migration()
        by = _by_component(plan)
        self.assertEqual(by["managed_events"].action, ACTION_BLOCKED_CORRUPT)
        # other components are unaffected.
        self.assertEqual(by["registry"].action, ACTION_MIGRATE)

    def test_plan_unsupported_legacy_left_untouched(self) -> None:
        _write_valid_legacy(self.home)
        path = self.home / LEGACY_FILES["registry"]
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        plan = StateStore(home=self.home).plan_migration()
        by = _by_component(plan)
        self.assertEqual(by["registry"].action, ACTION_BLOCKED_UNSUPPORTED)
        self.assertEqual(by["registry"].legacy_schema_version, 999)

    def test_plan_unknown_component_raises(self) -> None:
        with self.assertRaises(StateStoreError):
            StateStore(home=self.home).plan_migration(components=("nope",))


class StateStoreMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_migrate_writes_namespaced_tables_and_records_components(self) -> None:
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        result = store.migrate()
        self.assertTrue(result.performed)
        db = self.home / STATE_STORE_FILENAME
        self.assertTrue(db.exists())
        # container stamped, metadata complete.
        self.assertEqual(
            sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            .execute("PRAGMA user_version")
            .fetchone()[0],
            STATE_CONTAINER_VERSION,
        )
        recorded = {row.component for row in store.read_components()}
        self.assertEqual(recorded, set(COMPONENT_NAMES))
        # legacy rows landed in the *namespaced* target tables.
        self.assertEqual(
            _table_count(db, "registry_workspaces"),
            _table_count(self.home / LEGACY_FILES["registry"], "workspaces"),
        )
        self.assertGreaterEqual(_table_count(db, "registry_workspaces"), 1)
        self.assertNotEqual(_table_count(db, "registry_workspace_activity"), -1)
        self.assertNotEqual(_table_count(db, "inventory_panes"), -1)
        self.assertEqual(
            _table_count(db, "managed_events"),
            _table_count(self.home / LEGACY_FILES["managed_events"], "managed_events"),
        )
        self.assertEqual(
            _table_count(db, "otel_events"),
            _table_count(self.home / LEGACY_FILES["otel"], "otel_events"),
        )
        # the unprefixed legacy identity table name must NOT leak into the single DB.
        self.assertEqual(_table_count(db, "workspaces"), -1)

    def test_migrate_is_backup_first(self) -> None:
        _write_valid_legacy(self.home)
        result = StateStore(home=self.home).migrate(now="2026-06-21T13:00:00+00:00")
        self.assertIsNotNone(result.backup_dir)
        backup_dir = Path(result.backup_dir)
        self.assertTrue(backup_dir.exists())
        self.assertEqual(backup_dir.parent.name, "backups")
        # every migrated legacy file is copied into the backup before the write.
        for legacy in LEGACY_FILES.values():
            self.assertTrue((backup_dir / legacy).exists(), legacy)

    def test_migrate_is_non_destructive_to_legacy(self) -> None:
        _write_valid_legacy(self.home)
        before = {f: _file_bytes(self.home / f) for f in LEGACY_FILES.values()}
        StateStore(home=self.home).migrate()
        # legacy files are read, never written or deleted.
        for f, content in before.items():
            self.assertTrue((self.home / f).exists(), f)
            self.assertEqual(_file_bytes(self.home / f), content, f)

    def test_migrate_is_idempotent(self) -> None:
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        store.migrate(now="2026-06-21T13:00:00+00:00")
        db_after_first = _file_bytes(self.home / STATE_STORE_FILENAME)
        backups_after_first = sorted(
            p.name for p in (self.home / "backups").iterdir()
        )
        # second run: every component already recorded -> skip_complete, no write.
        second = store.migrate(now="2026-06-21T14:00:00+00:00")
        by = _by_component(second)
        for name in COMPONENT_NAMES:
            self.assertEqual(by[name].action, ACTION_SKIP_COMPLETE, name)
        self.assertTrue(second.performed)
        self.assertIsNone(second.backup_dir)  # no-op makes no new backup
        # the single DB is byte-identical and no second backup dir was created.
        self.assertEqual(
            _file_bytes(self.home / STATE_STORE_FILENAME), db_after_first
        )
        self.assertEqual(
            sorted(p.name for p in (self.home / "backups").iterdir()),
            backups_after_first,
        )

    def test_migrate_resumes_partial_component_selection(self) -> None:
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        first = store.migrate(components=("registry",))
        self.assertEqual({r.component for r in store.read_components()}, {"registry"})
        self.assertEqual(_by_component(first)["registry"].action, ACTION_SKIP_COMPLETE)
        # a later full migrate skips the done one and imports the rest.
        store.migrate()
        self.assertEqual(
            {r.component for r in store.read_components()}, set(COMPONENT_NAMES)
        )

    def test_migrate_skips_blocked_component(self) -> None:
        _write_valid_legacy(self.home)
        (self.home / LEGACY_FILES["otel"]).write_bytes(b"not a database")
        store = StateStore(home=self.home)
        store.migrate()
        recorded = {r.component for r in store.read_components()}
        self.assertEqual(recorded, {"registry", "managed_events", "inventory"})
        # the corrupt legacy file is left exactly as-is (never imported, never wiped).
        self.assertEqual(
            (self.home / LEGACY_FILES["otel"]).read_bytes(), b"not a database"
        )

    def test_migrate_downgrade_safe_on_newer_container(self) -> None:
        # An older build meeting a newer single DB must fail closed and not write.
        self.home.mkdir(parents=True, exist_ok=True)
        db = self.home / STATE_STORE_FILENAME
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA user_version = 999")
        conn.execute(
            "CREATE TABLE state_schema_components (component TEXT PRIMARY KEY, "
            "schema_version INTEGER NOT NULL, owner TEXT NOT NULL, "
            "recovery_policy TEXT NOT NULL, migrated_from TEXT, updated_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        before = _file_bytes(db)
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        with self.assertRaises(StateStoreError):
            store.plan_migration()
        with self.assertRaises(StateStoreError):
            store.migrate()
        # the newer DB is left byte-identical.
        self.assertEqual(_file_bytes(db), before)

    def test_no_op_migrate_on_absent_legacy_creates_no_db(self) -> None:
        store = StateStore(home=self.home)
        result = store.migrate()
        self.assertTrue(result.performed)
        self.assertIsNone(result.backup_dir)
        self.assertFalse((self.home / STATE_STORE_FILENAME).exists())


class StateStoreCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cleanup_not_eligible_before_migration(self) -> None:
        _write_valid_legacy(self.home)
        plan = StateStore(home=self.home).plan_cleanup()
        for c in plan.components:
            self.assertFalse(c.eligible, c.component)

    def test_cleanup_without_confirm_deletes_nothing(self) -> None:
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        store.migrate()
        result = store.cleanup()  # no confirm_destroy
        self.assertFalse(result.performed)
        # all eligible, but nothing removed.
        self.assertEqual(set(c.component for c in result.eligible), set(COMPONENT_NAMES))
        for legacy in LEGACY_FILES.values():
            self.assertTrue((self.home / legacy).exists(), legacy)

    def test_cleanup_with_confirm_backs_up_and_removes_migrated(self) -> None:
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        store.migrate()
        result = store.cleanup(confirm_destroy=True, now="2026-06-21T15:00:00+00:00")
        self.assertTrue(result.performed)
        self.assertIsNotNone(result.backup_dir)
        backup_dir = Path(result.backup_dir)
        for legacy in LEGACY_FILES.values():
            # the migrated legacy file is retired, but only after a backup copy.
            self.assertFalse((self.home / legacy).exists(), legacy)
            self.assertTrue((backup_dir / legacy).exists(), legacy)

    def test_cleanup_preserves_unmigrated_legacy(self) -> None:
        _write_valid_legacy(self.home)
        store = StateStore(home=self.home)
        store.migrate(components=("registry",))
        store.cleanup(confirm_destroy=True)
        # only the migrated component's legacy file is removed.
        self.assertFalse((self.home / LEGACY_FILES["registry"]).exists())
        self.assertTrue((self.home / LEGACY_FILES["otel"]).exists())
        self.assertTrue((self.home / LEGACY_FILES["managed_events"]).exists())


class StateStorePartialLegacyTest(unittest.TestCase):
    """A legacy DB with the right version + integrity but a missing expected table
    must NOT migrate, must NOT be recorded complete, and must NOT become
    cleanup-eligible (Redmine #12305 review j#62394 data-loss finding)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_registry_missing_activity(self) -> None:
        """registry.sqlite at v1, integrity ok, but without ``workspace_activity``."""
        register_workspace(self.home / "repo", home=self.home)
        path = self.home / LEGACY_FILES["registry"]
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE workspace_activity")
        conn.commit()
        conn.close()

    def test_plan_marks_missing_table_blocked_incomplete(self) -> None:
        (self.home / "repo").mkdir(parents=True, exist_ok=True)
        self._make_registry_missing_activity()
        plan = StateStore(home=self.home).plan_migration(components=("registry",))
        registry = _by_component(plan)["registry"]
        self.assertEqual(registry.action, ACTION_BLOCKED_INCOMPLETE)
        self.assertIn("workspace_activity", registry.reason)
        self.assertEqual(plan.migratable, ())

    def test_migrate_does_not_record_incomplete_component(self) -> None:
        (self.home / "repo").mkdir(parents=True, exist_ok=True)
        self._make_registry_missing_activity()
        store = StateStore(home=self.home)
        result = store.migrate(components=("registry",))
        self.assertTrue(result.performed)
        # nothing migratable -> no DB, no recorded component, no backup.
        self.assertEqual({r.component for r in store.read_components()}, set())
        self.assertIsNone(result.backup_dir)
        self.assertFalse((self.home / STATE_STORE_FILENAME).exists())

    def test_incomplete_legacy_not_cleanup_eligible(self) -> None:
        (self.home / "repo").mkdir(parents=True, exist_ok=True)
        self._make_registry_missing_activity()
        # other components are valid and present.
        save_snapshot([], home=self.home)
        record_managed_event(command="mozyo", event_kind="created", home=self.home)
        store = StateStore(home=self.home)
        store.migrate()  # registry blocked_incomplete; others migrate
        self.assertNotIn("registry", {r.component for r in store.read_components()})
        cleanup = store.plan_cleanup()
        registry = {c.component: c for c in cleanup.components}["registry"]
        self.assertFalse(registry.eligible)
        # a confirmed destructive cleanup must leave the incomplete legacy file.
        store.cleanup(confirm_destroy=True)
        self.assertTrue((self.home / LEGACY_FILES["registry"]).exists())


class StateStoreDoctorRoundTripTest(unittest.TestCase):
    """After a full migration the doctor inspector reads the single DB as ok."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_migrated_single_db_reads_ok_to_inspector(self) -> None:
        from mozyo_bridge.application.doctor import collect_state_store

        _write_valid_legacy(self.home)
        StateStore(home=self.home).migrate()
        report = collect_state_store(home=self.home)
        single = {c["component"]: c for c in report["components"]}["single_db"]
        self.assertEqual(single["status"], "ok")
        self.assertEqual(single["next_action"], "inspect")


class StateStoreCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self, **kw) -> argparse.Namespace:
        base = {
            "home": str(self.home),
            "as_json": True,
            "components": None,
            "write": False,
            "confirm_destroy": False,
        }
        base.update(kw)
        return argparse.Namespace(**base)

    def test_inspect_returns_zero(self) -> None:
        _write_valid_legacy(self.home)
        self.assertEqual(cmd_state_inspect(self._args()), 0)

    def test_migrate_dry_run_does_not_write(self) -> None:
        _write_valid_legacy(self.home)
        self.assertEqual(cmd_state_migrate(self._args(write=False)), 0)
        self.assertFalse(state_store_path(self.home).exists())

    def test_migrate_write_creates_db(self) -> None:
        _write_valid_legacy(self.home)
        self.assertEqual(cmd_state_migrate(self._args(write=True)), 0)
        self.assertTrue(state_store_path(self.home).exists())

    def test_cleanup_requires_both_gates(self) -> None:
        _write_valid_legacy(self.home)
        self.assertEqual(cmd_state_migrate(self._args(write=True)), 0)
        # --write alone (no --confirm-destroy) deletes nothing.
        self.assertEqual(cmd_state_cleanup(self._args(write=True)), 0)
        self.assertTrue((self.home / LEGACY_FILES["registry"]).exists())
        # both gates retire the migrated legacy files.
        self.assertEqual(
            cmd_state_cleanup(self._args(write=True, confirm_destroy=True)), 0
        )
        self.assertFalse((self.home / LEGACY_FILES["registry"]).exists())

    def test_migrate_downgrade_fails_closed_nonzero(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        db = state_store_path(self.home)
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        self.assertEqual(cmd_state_migrate(self._args(write=True)), 1)


if __name__ == "__main__":
    unittest.main()
