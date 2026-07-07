"""Doctor state-store inspector section boundary (#12893).

The ``doctor_state_store_section`` / ``collect_state_store`` collector historically
mixed two responsibilities in the ``doctor`` body: the *external read* (the
read-only SQLite probing of the legacy per-kind files and the future single DB —
``PRAGMA user_version`` / ``sqlite_master`` / ``PRAGMA integrity_check`` and the
``state_schema_components`` metadata read, all through a ``file:...?mode=ro`` URI
that never creates or writes), and the *verdict policy* that turns those probe
results into the per-component status dict and the section roll-up. This module
carves the collector slice out of the ``doctor`` body into an OOP-first boundary
(#12638 / #12892 follow-up):

- :class:`StateStoreReads` is the port for the *external read* and
  :class:`LiveStateStoreReads` the live adapter over the real read-only SQLite
  probes. The adapter owns every ``sqlite3`` call; the inspection domain never
  touches a database directly.
- :func:`inspect_legacy_component` and :func:`inspect_single_db` are the verdict
  policy. They derive a component dict from the read-view by calling the port,
  and re-assemble the legacy dict byte-for-byte (key order and note wording
  unchanged) so ``run_doctor`` aggregation, JSON output, and
  ``format_doctor_text`` rendering are unchanged.
- :func:`evaluate_state_store_section` assembles the whole section dict
  (``status`` / ``home`` / ``components`` / ``next_action``).
- :class:`StateStoreSectionUseCase` composes the port and the policy.

Unlike the OTel / tmux section boundaries this section's reads are *conditional*:
the ``state_schema_components`` metadata is only read after the container version
and metadata-table checks pass. A flat eager read-view would have to replicate
those verdict branches to know whether to read it, duplicating policy in the
adapter — so the inspection domain orchestrates the reads through the injected
port instead, keeping the read sequence (and the strict read-only invariant)
byte-identical to the legacy collector.

Read-only contract (Redmine #12273 j#61668 / j#61689): every probe opens SQLite
through a ``file:...?mode=ro`` URI which refuses to create the file; the surface
creates no home dir, no schema, no migration, no repair. ``ok`` means only
"readable at the expected shape for this state kind", never complete / approved /
action-allowed. An absent legacy file and an absent single DB are NORMAL states,
not failures, so ``missing`` ranks as ``ok`` in the roll-up. Any container or
schema version this build does not understand is reported unsupported and left
untouched (downgrade-safe), never ``ok`` (#12273 j#61689 Finding 1).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mozyo_bridge.state_store import (
    COMPONENTS as _STATE_COMPONENTS,
    RECOVERY_AUTHORITATIVE as _RECOVERY_AUTHORITATIVE,
    STATE_CONTAINER_VERSION as STATE_STORE_SINGLE_DB_CONTAINER_VERSION,
    STATE_STORE_FILENAME as STATE_STORE_SINGLE_DB_FILENAME,
)

# Legacy per-kind files, in ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}``.
# ``repair_action`` is the component-scoped next-action token a damaged store
# should suggest; it is advice, not something this read-only surface performs.
# Derived from the shared ``state_store`` registry so the inspector (#12273) and
# migrator (#12305) cannot drift.
_LEGACY_COMPONENTS: tuple[dict[str, Any], ...] = tuple(
    {
        "component": spec.component,
        "filename": spec.legacy_filename,
        "schema_version": spec.legacy_schema_version,
        "tables": spec.legacy_tables,
        "recovery_policy": spec.recovery_policy,
        "repair_action": spec.repair_action,
    }
    for spec in _STATE_COMPONENTS
)

# Component names the future single DB is expected to absorb from the legacy
# files (managed-state-model.md ``### legacy import``). A single DB that carries
# a strict subset is a partial migration, not a complete one.
_SINGLE_DB_EXPECTED_COMPONENTS = frozenset(
    spec["component"] for spec in _LEGACY_COMPONENTS
)

# Section roll-up rank. ``missing`` is treated as ``ok`` (0): an absent legacy
# file or absent single DB is a normal state, so it must not flip doctor red.
_STATE_STORE_STATUS_RANK = {"ok": 0, "missing": 0, "warning": 1, "invalid": 2, "error": 3}
_STATE_STORE_RANK_STATUS = {0: "ok", 1: "warning", 2: "invalid", 3: "error"}


@runtime_checkable
class StateStoreReads(Protocol):
    """Port: the read-only SQLite probes the state-store inspector depends on.

    Implementations own every database access. The verdict policy depends only
    on the returned probe dicts — it never opens a connection itself, so it is
    exercisable with synthetic probe results. Every method is strictly
    read-only: it must never create, write, migrate, or repair a store.
    """

    def exists(self, path: Path) -> bool:
        ...

    def probe_sqlite_ro(self, path: Path) -> dict[str, Any]:
        ...

    def read_state_schema_components(self, path: Path) -> list[dict[str, Any]] | None:
        ...


class LiveStateStoreReads:
    """Live adapter: the real read-only SQLite probes (#12273 invariants).

    Mirrors the legacy collector exactly. ``probe_sqlite_ro`` opens through a
    ``file:...?mode=ro`` URI (which errors rather than creating an absent file)
    and only issues ``PRAGMA user_version``, ``sqlite_master``, and
    ``PRAGMA integrity_check``; a non-SQLite or truncated file opens lazily but
    fails the first query, collapsing to ``integrity='error'`` so a corrupt
    store is reported, not raised. ``read_state_schema_components`` returns the
    parsed rows (possibly empty — a legitimate empty/partial migration) or
    ``None`` when the table cannot be read at all (malformed / unreadable
    schema), so the caller can distinguish a malformed metadata schema from a
    genuine partial migration (#12273 j#61689 Finding 1).
    """

    def exists(self, path: Path) -> bool:
        return path.exists()

    def probe_sqlite_ro(self, path: Path) -> dict[str, Any]:
        info: dict[str, Any] = {
            "opened": False,
            "user_version": None,
            "tables": [],
            "integrity": "unknown",
            "error": None,
        }
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.DatabaseError as exc:
            info["error"] = str(exc)
            return info
        try:
            info["opened"] = True
            try:
                row = conn.execute("PRAGMA user_version").fetchone()
                info["user_version"] = int(row[0]) if row is not None else None
            except (sqlite3.DatabaseError, TypeError, ValueError):
                info["integrity"] = "error"
            try:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                info["tables"] = [r[0] for r in rows]
            except sqlite3.DatabaseError:
                info["integrity"] = "error"
            try:
                check = conn.execute("PRAGMA integrity_check").fetchone()
                info["integrity"] = "ok" if check is not None and check[0] == "ok" else "error"
            except sqlite3.DatabaseError:
                info["integrity"] = "error"
        finally:
            conn.close()
        return info

    def read_state_schema_components(self, path: Path) -> list[dict[str, Any]] | None:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return None
        try:
            rows = conn.execute(
                "SELECT component, schema_version, recovery_policy, migrated_from "
                "FROM state_schema_components ORDER BY component"
            ).fetchall()
        except sqlite3.DatabaseError:
            return None
        finally:
            conn.close()
        return [
            {
                "component": component,
                "schema_version": schema_version,
                "recovery_policy": recovery_policy,
                "migrated_from": migrated_from,
            }
            for component, schema_version, recovery_policy, migrated_from in rows
        ]


def inspect_legacy_component(
    spec: dict[str, Any], home: Path, reads: StateStoreReads
) -> dict[str, Any]:
    path = home / spec["filename"]
    entry: dict[str, Any] = {
        "component": spec["component"],
        "path": str(path),
        "kind": "legacy",
        "recovery_policy": spec["recovery_policy"],
        "exists": False,
        "schema_version": None,
        "status": "missing",
        "readability": "absent",
        "integrity": "unknown",
        "tables": [],
        "next_action": "leave_untouched",
        "notes": [],
    }
    if not reads.exists(path):
        entry["notes"].append(
            "legacy file absent (normal before first use or after migration)"
        )
        return entry

    entry["exists"] = True
    probe = reads.probe_sqlite_ro(path)
    entry["schema_version"] = probe["user_version"]
    entry["tables"] = probe["tables"]

    if not probe["opened"]:
        entry["status"] = "error"
        entry["readability"] = "unreadable"
        entry["integrity"] = "error"
        entry["next_action"] = spec["repair_action"]
        entry["notes"].append(
            f"unreadable: {probe['error'] or 'cannot open read-only'}"
        )
        return entry

    entry["readability"] = "readable"
    entry["integrity"] = probe["integrity"]

    if probe["integrity"] != "ok":
        entry["status"] = "error"
        entry["readability"] = "partial"
        entry["next_action"] = spec["repair_action"]
        entry["notes"].append("PRAGMA integrity_check did not return ok (corrupt)")
        return entry

    expected_version = spec["schema_version"]
    actual_version = probe["user_version"]
    if actual_version != expected_version:
        newer = isinstance(actual_version, int) and actual_version > expected_version
        if spec["recovery_policy"] == _RECOVERY_AUTHORITATIVE:
            # Authoritative identity is fail-closed on unknown schema: report
            # unsupported and leave the DB untouched (downgrade must not rewrite
            # newer identity state).
            entry["status"] = "invalid"
            entry["next_action"] = "leave_untouched"
            entry["notes"].append(
                f"unsupported schema_version {actual_version} (this build expects "
                f"{expected_version}); authoritative store left untouched"
            )
        else:
            # Caches/history degrade rather than fail: a newer DB is left
            # untouched (downgrade-safe), an older shape is rebuildable.
            entry["status"] = "warning"
            entry["next_action"] = (
                "leave_untouched" if newer else spec["repair_action"]
            )
            entry["notes"].append(
                f"schema_version {actual_version} != expected {expected_version}; "
                + (
                    "newer DB left untouched (downgrade-safe)"
                    if newer
                    else "older cache/history shape; rebuildable"
                )
            )
        return entry

    missing_tables = [t for t in spec["tables"] if t not in probe["tables"]]
    if missing_tables:
        entry["status"] = "invalid"
        entry["readability"] = "partial"
        entry["next_action"] = (
            "leave_untouched"
            if spec["recovery_policy"] == _RECOVERY_AUTHORITATIVE
            else spec["repair_action"]
        )
        entry["notes"].append(
            "expected tables missing: " + ", ".join(missing_tables)
        )
        return entry

    entry["status"] = "ok"
    entry["next_action"] = "leave_untouched"
    entry["notes"].append("readable at expected schema; no migration in this build")
    return entry


def inspect_single_db(home: Path, reads: StateStoreReads) -> dict[str, Any]:
    path = home / STATE_STORE_SINGLE_DB_FILENAME
    entry: dict[str, Any] = {
        "component": "single_db",
        "path": str(path),
        "kind": "single_db",
        "recovery_policy": "mixed",
        "exists": False,
        "schema_version": None,
        "status": "missing",
        "readability": "absent",
        "integrity": "unknown",
        "tables": [],
        "components": [],
        "next_action": "leave_untouched",
        "notes": [],
    }
    if not reads.exists(path):
        entry["notes"].append(
            "future single DB absent (no migration has run; legacy files remain "
            "the source of state)"
        )
        return entry

    entry["exists"] = True
    probe = reads.probe_sqlite_ro(path)
    entry["schema_version"] = probe["user_version"]
    entry["tables"] = probe["tables"]

    if not probe["opened"]:
        entry["status"] = "error"
        entry["readability"] = "unreadable"
        entry["integrity"] = "error"
        entry["next_action"] = "restore_backup"
        entry["notes"].append(
            f"unreadable: {probe['error'] or 'cannot open read-only'}"
        )
        return entry

    entry["readability"] = "readable"
    entry["integrity"] = probe["integrity"]

    if probe["integrity"] != "ok":
        entry["status"] = "error"
        entry["readability"] = "partial"
        entry["next_action"] = "restore_backup"
        entry["notes"].append("PRAGMA integrity_check did not return ok (corrupt)")
        return entry

    # Validate the container layout version BEFORE trusting component metadata.
    # An unsupported container (typically a newer one this build does not
    # understand) must be reported unsupported and left untouched — never `ok`
    # even when component rows look complete (#12273 j#61689 Finding 1).
    container_version = probe["user_version"]
    if container_version != STATE_STORE_SINGLE_DB_CONTAINER_VERSION:
        newer = (
            isinstance(container_version, int)
            and container_version > STATE_STORE_SINGLE_DB_CONTAINER_VERSION
        )
        entry["status"] = "invalid"
        entry["next_action"] = "leave_untouched"
        entry["notes"].append(
            f"unsupported container schema version {container_version} (this build "
            f"understands {STATE_STORE_SINGLE_DB_CONTAINER_VERSION}); "
            + ("newer DB left untouched (downgrade-safe)" if newer else "left untouched")
        )
        return entry

    if "state_schema_components" not in probe["tables"]:
        # A supported container `user_version` without the component metadata
        # table is an unknown / unsupported layout. Leave it untouched.
        entry["status"] = "invalid"
        entry["next_action"] = "leave_untouched"
        entry["notes"].append(
            "container present but state_schema_components metadata table is "
            "missing; unsupported / unknown layout left untouched"
        )
        return entry

    components = reads.read_state_schema_components(path)
    if components is None:
        # The metadata table exists but its schema is malformed / unreadable.
        # This is NOT a partial migration: report it invalid and leave it
        # untouched rather than suggesting a dry-run migrate (#12273 j#61689
        # Finding 1) — a malformed schema is not a migratable subset.
        entry["status"] = "invalid"
        entry["readability"] = "partial"
        entry["next_action"] = "leave_untouched"
        entry["notes"].append(
            "state_schema_components present but unreadable / malformed schema; "
            "left untouched (not treated as partial migration)"
        )
        return entry

    entry["components"] = components
    present = {c["component"] for c in components}
    migrated_legacy = present & _SINGLE_DB_EXPECTED_COMPONENTS
    missing = sorted(_SINGLE_DB_EXPECTED_COMPONENTS - present)
    if missing and (migrated_legacy or not present):
        # A migration RAN and stopped short (some legacy components imported,
        # some not), or the container exists with an EMPTY metadata table (a
        # write that died before recording anything, #12273 j#61689) — the true
        # partial-migration hazards the doc forbids treating as complete
        # (managed-state-model.md ### legacy import).
        entry["status"] = "warning"
        entry["readability"] = "partial"
        entry["next_action"] = "migrate_dry_run"
        entry["notes"].append(
            "partial migration: state_schema_components carries "
            f"{sorted(present) or 'no'} component(s); not yet migrated: {missing}"
        )
    elif missing:
        # Native components only (Redmine #13356: a post-consolidation component
        # such as `lane_metadata` creates the container without any legacy
        # import). No migration has run and the legacy files remain the source
        # of their state — exactly the healthy pre-migration posture, not a
        # partial import; migrating stays an operator option, not a warning.
        entry["status"] = "ok"
        entry["next_action"] = "inspect"
        entry["notes"].append(
            "single DB carries native component(s) only "
            f"({sorted(present) or 'none'}); no legacy migration has run — "
            f"legacy files remain the source for: {missing}"
        )
    else:
        entry["status"] = "ok"
        entry["next_action"] = "inspect"
        entry["notes"].append(
            "all expected components present in state_schema_components"
        )
    return entry


def state_store_section_status(components: list[dict[str, Any]]) -> str:
    worst = 0
    for component in components:
        worst = max(worst, _STATE_STORE_STATUS_RANK.get(component["status"], 0))
    return _STATE_STORE_RANK_STATUS[worst]


def state_store_next_actions(components: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for component in components:
        if component["status"] in ("warning", "invalid", "error"):
            detail = component["notes"][0] if component["notes"] else component["status"]
            actions.append(
                f"{component['component']}: {component['next_action']} ({detail})"
            )
    return actions


def evaluate_state_store_section(home: Path, reads: StateStoreReads) -> dict[str, Any]:
    """Derive the legacy state-store section dict from the read port.

    Detects the legacy per-kind SQLite files and the future single DB
    side-by-side under ``home``, reporting per-component status and a
    next-action token. Reads nothing it does not need and writes nothing. The
    result is a component projection, not workflow truth or a side-effect
    permission. Re-assembles the legacy dict byte-for-byte (key order
    ``status`` / ``home`` / ``components`` / ``next_action``).
    """
    components = [
        inspect_legacy_component(spec, home, reads) for spec in _LEGACY_COMPONENTS
    ]
    components.append(inspect_single_db(home, reads))
    return {
        "status": state_store_section_status(components),
        "home": str(home),
        "components": components,
        "next_action": state_store_next_actions(components),
    }


class StateStoreSectionUseCase:
    """Use case: probe the state store read-only and apply the verdict policy.

    Returns the legacy ``collect_state_store`` dict shape byte-for-byte for an
    already-resolved ``home``.
    """

    def __init__(self, reads: StateStoreReads) -> None:
        self._reads = reads

    def execute(self, home: Path) -> dict[str, Any]:
        return evaluate_state_store_section(home, self._reads)
