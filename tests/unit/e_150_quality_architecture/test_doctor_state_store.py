"""Fake-port / verdict-policy specifications for the doctor state-store
inspector boundary (#12893).

These exercise the ``doctor_state_store`` verdict policy directly with a
synthetic :class:`StateStoreReads` port — without a real SQLite file, without a
real home directory. They pin the per-component status vocabulary, the legacy
dict shape, the section roll-up (``missing`` ranks as ``ok``), and the
conditional ``state_schema_components`` read sequence the single-DB inspection
depends on. The end-to-end read-only invariant over real SQLite stays pinned by
``tests/integration/.../test_state_store_inspector.py``; this file pins the
policy in isolation.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from mozyo_bridge.application.doctor_state_store import (
    STATE_STORE_SINGLE_DB_CONTAINER_VERSION,
    STATE_STORE_SINGLE_DB_FILENAME,
    LiveStateStoreReads,
    StateStoreReads,
    StateStoreSectionUseCase,
    evaluate_state_store_section,
    inspect_legacy_component,
    inspect_single_db,
    state_store_next_actions,
    state_store_section_status,
)
from mozyo_bridge.state_store import (
    RECOVERY_AUTHORITATIVE,
    RECOVERY_REBUILDABLE,
)

HOME = Path("/home/.mozyo_bridge")

# A registry (authoritative identity) and an otel (rebuildable cache) legacy
# spec, mirroring the shared state_store registry shape the policy consumes.
REGISTRY_SPEC: dict[str, Any] = {
    "component": "registry",
    "filename": "registry.sqlite",
    "schema_version": 1,
    "tables": ["workspaces"],
    "recovery_policy": RECOVERY_AUTHORITATIVE,
    "repair_action": "restore_backup",
}
OTEL_SPEC: dict[str, Any] = {
    "component": "otel",
    "filename": "otel-events.sqlite",
    "schema_version": 1,
    "tables": ["otel_events"],
    "recovery_policy": RECOVERY_REBUILDABLE,
    "repair_action": "rebuild",
}


def _probe(
    *,
    opened: bool = True,
    user_version: int | None = 1,
    tables: list[str] | None = None,
    integrity: str = "ok",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "opened": opened,
        "user_version": user_version,
        "tables": [] if tables is None else tables,
        "integrity": integrity,
        "error": error,
    }


class FakeStateStoreReads:
    """Synthetic read port keyed by path basename.

    ``existing`` is the set of file basenames that "exist"; ``probes`` and
    ``schema_components`` map basename -> the value the corresponding read
    returns. Every call is recorded so a test can assert the conditional
    ``read_state_schema_components`` read happens only when the policy reaches
    it.
    """

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        probes: dict[str, dict[str, Any]] | None = None,
        schema_components: dict[str, list[dict[str, Any]] | None] | None = None,
    ) -> None:
        self.existing = set() if existing is None else existing
        self.probes = {} if probes is None else probes
        self.schema_components = {} if schema_components is None else schema_components
        self.exists_calls: list[str] = []
        self.probe_calls: list[str] = []
        self.schema_calls: list[str] = []

    def exists(self, path: Path) -> bool:
        self.exists_calls.append(path.name)
        return path.name in self.existing

    def probe_sqlite_ro(self, path: Path) -> dict[str, Any]:
        self.probe_calls.append(path.name)
        return self.probes[path.name]

    def read_state_schema_components(
        self, path: Path
    ) -> list[dict[str, Any]] | None:
        self.schema_calls.append(path.name)
        return self.schema_components.get(path.name)


class InspectLegacyComponentTest(unittest.TestCase):
    def test_absent_file_is_missing_and_never_probes(self) -> None:
        reads = FakeStateStoreReads(existing=set())
        entry = inspect_legacy_component(REGISTRY_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "missing")
        self.assertFalse(entry["exists"])
        self.assertEqual(entry["readability"], "absent")
        self.assertEqual(entry["next_action"], "leave_untouched")
        # strictly read-only: an absent file is never opened.
        self.assertEqual(reads.probe_calls, [])

    def test_readable_at_expected_shape_is_ok(self) -> None:
        reads = FakeStateStoreReads(
            existing={"registry.sqlite"},
            probes={"registry.sqlite": _probe(tables=["workspaces"])},
        )
        entry = inspect_legacy_component(REGISTRY_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["next_action"], "leave_untouched")
        self.assertEqual(entry["readability"], "readable")

    def test_unopenable_file_is_error_with_repair_action(self) -> None:
        reads = FakeStateStoreReads(
            existing={"registry.sqlite"},
            probes={
                "registry.sqlite": _probe(
                    opened=False, user_version=None, integrity="unknown", error="boom"
                )
            },
        )
        entry = inspect_legacy_component(REGISTRY_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["readability"], "unreadable")
        self.assertEqual(entry["next_action"], "restore_backup")
        self.assertIn("boom", entry["notes"][0])

    def test_corrupt_integrity_is_error(self) -> None:
        reads = FakeStateStoreReads(
            existing={"otel-events.sqlite"},
            probes={"otel-events.sqlite": _probe(integrity="error")},
        )
        entry = inspect_legacy_component(OTEL_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["readability"], "partial")
        self.assertEqual(entry["next_action"], "rebuild")

    def test_newer_authoritative_schema_is_invalid_left_untouched(self) -> None:
        reads = FakeStateStoreReads(
            existing={"registry.sqlite"},
            probes={"registry.sqlite": _probe(user_version=999, tables=["workspaces"])},
        )
        entry = inspect_legacy_component(REGISTRY_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "invalid")
        self.assertEqual(entry["next_action"], "leave_untouched")

    def test_newer_cache_schema_is_warning_downgrade_safe(self) -> None:
        reads = FakeStateStoreReads(
            existing={"otel-events.sqlite"},
            probes={"otel-events.sqlite": _probe(user_version=999, tables=["otel_events"])},
        )
        entry = inspect_legacy_component(OTEL_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "warning")
        self.assertEqual(entry["next_action"], "leave_untouched")

    def test_older_cache_schema_is_warning_rebuildable(self) -> None:
        reads = FakeStateStoreReads(
            existing={"otel-events.sqlite"},
            probes={"otel-events.sqlite": _probe(user_version=0, tables=["otel_events"])},
        )
        entry = inspect_legacy_component(OTEL_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "warning")
        self.assertEqual(entry["next_action"], "rebuild")

    def test_missing_expected_table_is_invalid(self) -> None:
        reads = FakeStateStoreReads(
            existing={"registry.sqlite"},
            probes={"registry.sqlite": _probe(tables=[])},
        )
        entry = inspect_legacy_component(REGISTRY_SPEC, HOME, reads)
        self.assertEqual(entry["status"], "invalid")
        self.assertEqual(entry["readability"], "partial")
        # authoritative identity is left untouched even with a missing table.
        self.assertEqual(entry["next_action"], "leave_untouched")


class InspectSingleDbTest(unittest.TestCase):
    def _path_name(self) -> str:
        return STATE_STORE_SINGLE_DB_FILENAME

    def test_absent_single_db_is_missing_and_never_probes(self) -> None:
        reads = FakeStateStoreReads(existing=set())
        entry = inspect_single_db(HOME, reads)
        self.assertEqual(entry["status"], "missing")
        self.assertEqual(reads.probe_calls, [])
        self.assertEqual(reads.schema_calls, [])

    def test_newer_container_is_invalid_before_metadata_read(self) -> None:
        name = self._path_name()
        reads = FakeStateStoreReads(
            existing={name},
            probes={
                name: _probe(
                    user_version=STATE_STORE_SINGLE_DB_CONTAINER_VERSION + 998,
                    tables=["state_schema_components"],
                )
            },
        )
        entry = inspect_single_db(HOME, reads)
        self.assertEqual(entry["status"], "invalid")
        self.assertEqual(entry["next_action"], "leave_untouched")
        # container check fails first: metadata is never read.
        self.assertEqual(reads.schema_calls, [])

    def test_supported_container_without_metadata_table_is_invalid(self) -> None:
        name = self._path_name()
        reads = FakeStateStoreReads(
            existing={name},
            probes={
                name: _probe(
                    user_version=STATE_STORE_SINGLE_DB_CONTAINER_VERSION,
                    tables=["something_else"],
                )
            },
        )
        entry = inspect_single_db(HOME, reads)
        self.assertEqual(entry["status"], "invalid")
        self.assertEqual(entry["next_action"], "leave_untouched")
        self.assertEqual(reads.schema_calls, [])

    def test_malformed_metadata_table_is_invalid_not_partial(self) -> None:
        name = self._path_name()
        reads = FakeStateStoreReads(
            existing={name},
            probes={
                name: _probe(
                    user_version=STATE_STORE_SINGLE_DB_CONTAINER_VERSION,
                    tables=["state_schema_components"],
                )
            },
            schema_components={name: None},
        )
        entry = inspect_single_db(HOME, reads)
        self.assertEqual(entry["status"], "invalid")
        self.assertEqual(entry["next_action"], "leave_untouched")
        self.assertEqual(entry["readability"], "partial")
        self.assertEqual(entry["components"], [])
        # the metadata read is reached only once the container/table checks pass.
        self.assertEqual(reads.schema_calls, [name])

    def test_partial_components_is_warning_migrate_dry_run(self) -> None:
        name = self._path_name()
        reads = FakeStateStoreReads(
            existing={name},
            probes={
                name: _probe(
                    user_version=STATE_STORE_SINGLE_DB_CONTAINER_VERSION,
                    tables=["state_schema_components"],
                )
            },
            schema_components={
                name: [
                    {
                        "component": "registry",
                        "schema_version": 1,
                        "recovery_policy": "authoritative_identity",
                        "migrated_from": "registry.sqlite",
                    }
                ]
            },
        )
        entry = inspect_single_db(HOME, reads)
        self.assertEqual(entry["status"], "warning")
        self.assertEqual(entry["readability"], "partial")
        self.assertEqual(entry["next_action"], "migrate_dry_run")

    def test_empty_metadata_is_partial_not_invalid(self) -> None:
        name = self._path_name()
        reads = FakeStateStoreReads(
            existing={name},
            probes={
                name: _probe(
                    user_version=STATE_STORE_SINGLE_DB_CONTAINER_VERSION,
                    tables=["state_schema_components"],
                )
            },
            schema_components={name: []},
        )
        entry = inspect_single_db(HOME, reads)
        self.assertEqual(entry["status"], "warning")
        self.assertEqual(entry["next_action"], "migrate_dry_run")
        self.assertEqual(entry["components"], [])


class SectionRollUpTest(unittest.TestCase):
    def test_missing_ranks_as_ok(self) -> None:
        components = [
            {"status": "missing", "notes": [], "next_action": "leave_untouched", "component": "a"},
            {"status": "ok", "notes": [], "next_action": "leave_untouched", "component": "b"},
        ]
        self.assertEqual(state_store_section_status(components), "ok")
        self.assertEqual(state_store_next_actions(components), [])

    def test_worst_component_drives_section_status(self) -> None:
        components = [
            {"status": "ok", "notes": [], "next_action": "leave_untouched", "component": "a"},
            {"status": "warning", "notes": ["w"], "next_action": "rebuild", "component": "b"},
            {"status": "error", "notes": ["e"], "next_action": "restore_backup", "component": "c"},
        ]
        self.assertEqual(state_store_section_status(components), "error")

    def test_next_actions_only_for_actionable_components(self) -> None:
        components = [
            {"status": "ok", "notes": [], "next_action": "leave_untouched", "component": "a"},
            {"status": "warning", "notes": ["needs rebuild"], "next_action": "rebuild", "component": "b"},
        ]
        actions = state_store_next_actions(components)
        self.assertEqual(actions, ["b: rebuild (needs rebuild)"])


class EvaluateSectionAndUseCaseTest(unittest.TestCase):
    def test_evaluate_assembles_legacy_dict_shape(self) -> None:
        reads = FakeStateStoreReads(existing=set())
        section = evaluate_state_store_section(HOME, reads)
        self.assertEqual(set(section), {"status", "home", "components", "next_action"})
        self.assertEqual(section["home"], str(HOME))
        # absent everything -> section ok, no actions.
        self.assertEqual(section["status"], "ok")
        self.assertEqual(section["next_action"], [])
        # one entry per legacy component plus the single DB.
        kinds = [c["kind"] for c in section["components"]]
        self.assertIn("single_db", kinds)
        self.assertEqual(kinds.count("single_db"), 1)

    def test_use_case_delegates_to_policy(self) -> None:
        reads = FakeStateStoreReads(existing=set())
        section = StateStoreSectionUseCase(reads).execute(HOME)
        self.assertEqual(section["status"], "ok")
        self.assertEqual(section["home"], str(HOME))

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveStateStoreReads(), StateStoreReads)


if __name__ == "__main__":
    unittest.main()
