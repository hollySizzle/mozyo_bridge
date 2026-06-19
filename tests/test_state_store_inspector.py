"""Read-only state store inspector / doctor tests (Redmine #12273).

Pins the component status vocabulary, JSON/text output shape, and — most
importantly — the read-only invariant: the inspector detects legacy SQLite and
the future single DB side-by-side without ever creating, writing, migrating, or
repairing anything. Design anchors: `vibes/docs/logics/managed-state-model.md`
and `vibes/docs/logics/runtime-observability-boundary.md`.
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.application.doctor import (
    STATE_STORE_SINGLE_DB_FILENAME,
    collect_state_store,
    doctor_state_store_section,
    format_doctor_text,
    run_doctor,
)
from mozyo_bridge.managed_events import record_managed_event
from mozyo_bridge.otel_store import OtelEvent, OtelEventStore
from mozyo_bridge.session_inventory import save_snapshot
from mozyo_bridge.workspace_registry import register_workspace

LEGACY_COMPONENTS = ("registry", "managed_events", "inventory", "otel")
ALL_COMPONENTS = LEGACY_COMPONENTS + ("single_db",)
LEGACY_FILES = {
    "registry": "registry.sqlite",
    "managed_events": "managed-events.sqlite",
    "inventory": "inventory.sqlite",
    "otel": "otel-events.sqlite",
}


def _by_component(result: dict) -> dict:
    return {c["component"]: c for c in result["components"]}


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


class StateStoreInspectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- absent home --------------------------------------------------------
    def test_absent_home_all_missing_section_ok_and_no_creation(self) -> None:
        result = collect_state_store(home=self.home)
        self.assertEqual(result["status"], "ok")
        by = _by_component(result)
        self.assertEqual(set(by), set(ALL_COMPONENTS))
        for name in ALL_COMPONENTS:
            self.assertEqual(by[name]["status"], "missing", name)
            self.assertFalse(by[name]["exists"], name)
            self.assertEqual(by[name]["readability"], "absent", name)
            self.assertEqual(by[name]["next_action"], "leave_untouched", name)
        # missing is normal: it must not produce section-level next actions.
        self.assertEqual(result["next_action"], [])
        # strictly read-only: the home directory must not be created.
        self.assertFalse(self.home.exists())

    # --- valid legacy DBs ---------------------------------------------------
    def test_valid_legacy_dbs_report_ok(self) -> None:
        _write_valid_legacy(self.home)
        result = collect_state_store(home=self.home)
        by = _by_component(result)
        for name in LEGACY_COMPONENTS:
            self.assertEqual(by[name]["status"], "ok", (name, by[name]))
            self.assertTrue(by[name]["exists"], name)
            self.assertEqual(by[name]["readability"], "readable", name)
            self.assertEqual(by[name]["integrity"], "ok", name)
            self.assertIsInstance(by[name]["schema_version"], int)
        # single DB still absent (no migration has run) → section stays ok.
        self.assertEqual(by["single_db"]["status"], "missing")
        self.assertEqual(result["status"], "ok")

    # --- corrupt DB ---------------------------------------------------------
    def test_corrupt_db_reports_error(self) -> None:
        _write_valid_legacy(self.home)
        (self.home / LEGACY_FILES["managed_events"]).write_bytes(
            b"this is not a sqlite database"
        )
        result = collect_state_store(home=self.home)
        by = _by_component(result)
        self.assertEqual(by["managed_events"]["status"], "error")
        self.assertIn(by["managed_events"]["readability"], ("unreadable", "partial"))
        self.assertEqual(by["managed_events"]["integrity"], "error")
        # append_only_lossy history → restore_backup, never auto-repair here.
        self.assertEqual(by["managed_events"]["next_action"], "restore_backup")
        self.assertEqual(result["status"], "error")

    # --- newer schema on authoritative vs cache -----------------------------
    def test_newer_authoritative_schema_is_invalid_left_untouched(self) -> None:
        _write_valid_legacy(self.home)
        path = self.home / LEGACY_FILES["registry"]
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        result = collect_state_store(home=self.home)
        by = _by_component(result)
        self.assertEqual(by["registry"]["status"], "invalid")
        self.assertEqual(by["registry"]["next_action"], "leave_untouched")
        self.assertEqual(result["status"], "invalid")

    def test_newer_cache_schema_is_warning_downgrade_safe(self) -> None:
        _write_valid_legacy(self.home)
        path = self.home / LEGACY_FILES["otel"]
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        result = collect_state_store(home=self.home)
        by = _by_component(result)
        self.assertEqual(by["otel"]["status"], "warning")
        # newer cache is left untouched so a downgraded CLI never destroys it.
        self.assertEqual(by["otel"]["next_action"], "leave_untouched")
        self.assertEqual(result["status"], "warning")

    def test_invalid_schema_when_expected_table_missing(self) -> None:
        # A registry file at the right user_version but with the identity table
        # dropped is an invalid shape, not merely ok.
        _write_valid_legacy(self.home)
        path = self.home / LEGACY_FILES["registry"]
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE workspaces")
        conn.commit()
        conn.close()
        result = collect_state_store(home=self.home)
        by = _by_component(result)
        self.assertEqual(by["registry"]["status"], "invalid")

    # --- future single DB ---------------------------------------------------
    def _make_single_db(self, components: list[str]) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / STATE_STORE_SINGLE_DB_FILENAME
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE state_schema_components ("
            "component TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, "
            "owner TEXT NOT NULL, recovery_policy TEXT NOT NULL, "
            "migrated_from TEXT, updated_at TEXT NOT NULL)"
        )
        for component in components:
            conn.execute(
                "INSERT INTO state_schema_components VALUES (?,?,?,?,?,?)",
                (component, 1, component, "rebuildable_cache", f"{component}.sqlite", "t"),
            )
        conn.commit()
        conn.close()
        return path

    def test_single_db_partial_component_metadata_is_warning(self) -> None:
        self._make_single_db(["registry"])
        result = collect_state_store(home=self.home)
        by = _by_component(result)
        sdb = by["single_db"]
        self.assertTrue(sdb["exists"])
        self.assertEqual(sdb["status"], "warning")
        self.assertEqual(sdb["readability"], "partial")
        self.assertEqual(sdb["next_action"], "migrate_dry_run")
        present = {c["component"] for c in sdb["components"]}
        self.assertEqual(present, {"registry"})
        self.assertEqual(result["status"], "warning")

    def test_single_db_complete_components_is_ok(self) -> None:
        self._make_single_db(list(LEGACY_COMPONENTS))
        result = collect_state_store(home=self.home)
        sdb = _by_component(result)["single_db"]
        self.assertEqual(sdb["status"], "ok")
        self.assertEqual(sdb["next_action"], "inspect")

    def test_single_db_without_metadata_table_is_invalid(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / STATE_STORE_SINGLE_DB_FILENAME
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("CREATE TABLE something_else (id INTEGER)")
        conn.commit()
        conn.close()
        sdb = _by_component(collect_state_store(home=self.home))["single_db"]
        self.assertEqual(sdb["status"], "invalid")
        self.assertEqual(sdb["next_action"], "leave_untouched")

    # --- JSON output contract ----------------------------------------------
    def test_json_contract_fields_pinned(self) -> None:
        _write_valid_legacy(self.home)
        result = collect_state_store(home=self.home)
        self.assertEqual(set(result), {"status", "home", "components", "next_action"})
        required = {
            "component",
            "path",
            "kind",
            "exists",
            "schema_version",
            "status",
            "readability",
            "integrity",
            "tables",
            "next_action",
            "notes",
        }
        for component in result["components"]:
            self.assertTrue(required.issubset(component), set(required) - set(component))
            self.assertIn(component["status"], ("missing", "ok", "warning", "invalid", "error"))
            self.assertIn(component["kind"], ("legacy", "single_db"))
            self.assertIsInstance(component["tables"], list)
            self.assertIsInstance(component["notes"], list)

    # --- text formatting ----------------------------------------------------
    def test_text_format_renders_section_and_components(self) -> None:
        self._make_single_db(["registry"])
        result = run_doctor(argparse.Namespace(repo=str(self.home), home=str(self.home)))
        text = format_doctor_text(result)
        self.assertIn("state_store:", text)
        for name in ALL_COMPONENTS:
            self.assertRegex(text, rf"\n  {name}: ")
        # partial single DB surfaces a component-scoped next action line.
        self.assertIn("migrate_dry_run", text)

    # --- read-only: no creation, no mutation --------------------------------
    def test_read_only_creates_and_mutates_nothing(self) -> None:
        _write_valid_legacy(self.home)
        before = {
            p.name: p.stat().st_mtime_ns
            for p in self.home.iterdir()
            if p.is_file()
        }
        # Run twice; neither run may create the single DB nor touch any file.
        collect_state_store(home=self.home)
        collect_state_store(home=self.home)
        after = {
            p.name: p.stat().st_mtime_ns
            for p in self.home.iterdir()
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertFalse((self.home / STATE_STORE_SINGLE_DB_FILENAME).exists())
        # No sidecar -wal / -shm files were created by a read-only open either.
        names = {p.name for p in self.home.iterdir()}
        self.assertFalse(any(n.endswith(("-wal", "-shm")) for n in names), names)

    def test_doctor_section_resolves_home_from_args(self) -> None:
        _write_valid_legacy(self.home)
        section = doctor_state_store_section(
            argparse.Namespace(repo=str(self.home), home=str(self.home))
        )
        # doctor_home(args) resolves the path (symlink canonicalization on macOS).
        self.assertEqual(section["home"], str(self.home.resolve()))
        self.assertEqual(section["status"], "ok")


if __name__ == "__main__":
    unittest.main()
