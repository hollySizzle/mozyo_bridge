"""Home-scoped single state store: facade, dry-run planner, write migration (#12305).

The follow-up implementation the design split out of #12257-#12261 (recorded in
``vibes/docs/logics/managed-state-model.md`` ``### home-scoped single SQLite 統合
方針`` / ``### migration / doctor / integrity check 方針``): a single home-scoped
SQLite — ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/state.sqlite`` — that the legacy
per-kind files (``registry.sqlite`` / ``managed-events.sqlite`` /
``inventory.sqlite`` / ``otel-events.sqlite``) migrate into, component by
component, behind a ``state_schema_components`` metadata table.

What this module owns (and what makes it the single source of truth):

- the **container layout** — the single-DB filename, the container
  ``PRAGMA user_version``, and the ``state_schema_components`` schema;
- the **component registry** (:data:`COMPONENTS`) — for each component its legacy
  filename, legacy schema version, recovery policy, the doctor next-action token a
  damaged legacy store should suggest, and the legacy-table -> target-namespaced-
  table map. The read-only inspector in :mod:`mozyo_bridge.application.doctor`
  imports this registry and these constants rather than re-declaring them, so the
  inspector (#12273) and the migrator (#12305) can never drift apart.

Migration boundaries pinned by the doc (and by #12305 受入条件):

- **Read-only inspector first.** The component-status inspector
  (:func:`mozyo_bridge.application.doctor.collect_state_store`) and the
  :meth:`StateStore.plan_migration` dry-run both create and write nothing; only
  :meth:`StateStore.migrate` ever writes, and only when explicitly asked.
- **Copy / import, never in-place mutate.** Legacy files are opened read-only and
  copied into the new DB's namespace; a legacy file is never written or deleted by
  migration. It stays as rollback / downgrade input (legacy cleanup / retirement is
  a deliberately separate, separately-gated later stage — see
  :class:`CleanupPlan`).
- **Backup-first.** A write migration copies every legacy file it will read, plus
  any existing ``state.sqlite``, into a timestamped ``backups/state-<ts>/`` under
  home *before* the first write. A backup failure aborts the migration; it never
  proceeds unbacked.
- **Idempotent by component.** A component already recorded in
  ``state_schema_components`` is skipped, so re-running a completed migration writes
  nothing. A component whose import did not finish (no metadata row) is rebuilt from
  the legacy source on re-run.
- **Downgrade-aware / non-destructive.** An older build that meets a newer container
  ``user_version`` reports it unsupported and leaves the DB untouched. An
  authoritative legacy store at an unknown schema version is left untouched (never
  rewritten); caches degrade rather than fail.
- **Owner / namespace boundary.** Each component migrator copies only its own
  legacy file into its own table namespace. ``state_schema_components`` is
  metadata, not workflow truth — no completion / approval / liveness column lives
  here, and no method JOINs across namespaces to invent a "current truth".

Conventions mirror the sibling home-scoped stores
(:mod:`mozyo_bridge.workspace_registry`, :mod:`mozyo_bridge.presentation_state`):
a ``*_FILENAME`` constant, a ``*_path(home=None)`` helper resolving through
:func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a ``PRAGMA user_version``
schema guard, frozen dataclasses with ``as_payload()``, and ISO-second UTC
timestamps.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.managed_events import (
    MANAGED_EVENTS_FILENAME,
    MANAGED_EVENTS_SCHEMA_VERSION,
)
from mozyo_bridge.core.state.otel_store import (
    OTEL_STORE_FILENAME,
    OTEL_STORE_SCHEMA_VERSION,
)
from mozyo_bridge.core.state.session_inventory import (
    INVENTORY_FILENAME,
    INVENTORY_SCHEMA_VERSION,
)
from mozyo_bridge.core.state.workspace_registry import (
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-scoped single state DB. A consolidation target for the legacy
#: per-kind files; ``state.sqlite`` is absent until a migration runs.
STATE_STORE_FILENAME = "state.sqlite"

#: Container ``PRAGMA user_version``: identifies the single-DB layout and the
#: presence of ``state_schema_components`` (managed-state-model.md ``### schema
#: version / migration`` two-level model). Bump only with a container migration;
#: a newer container is reported unsupported and left untouched (downgrade-safe).
STATE_CONTAINER_VERSION = 1

#: Sub-directory under home holding pre-write backups, one timestamped dir per
#: write migration / destructive cleanup (``backups/state-<ts>/``).
BACKUPS_DIRNAME = "backups"

# recovery-policy vocabulary (managed-state-model.md ``### recovery policy vocabulary``)
RECOVERY_AUTHORITATIVE = "authoritative"
RECOVERY_APPEND_ONLY = "append_only_lossy"
RECOVERY_REBUILDABLE = "rebuildable_cache"


@dataclass(frozen=True)
class ComponentSpec:
    """One legacy component's identity, recovery policy, and migration mapping.

    The single source of truth shared by the read-only inspector (#12273) and the
    migrator (#12305). ``table_map`` pairs each legacy source table with its
    target table in the single DB's namespace (``workspaces`` ->
    ``registry_workspaces``); a pair that keeps the same name (``managed_events``)
    simply maps a name to itself.
    """

    component: str
    legacy_filename: str
    legacy_schema_version: int
    recovery_policy: str
    repair_action: str
    table_map: tuple[tuple[str, str], ...]

    @property
    def legacy_tables(self) -> tuple[str, ...]:
        return tuple(legacy for legacy, _ in self.table_map)

    @property
    def target_tables(self) -> tuple[str, ...]:
        return tuple(target for _, target in self.table_map)


#: Per-component registry, in the legacy-import order the doc lists
#: (managed-state-model.md ``### legacy import``). Owner module / namespace and
#: recovery policy follow ``### state kind ownership / recovery matrix``.
COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        component="registry",
        legacy_filename=REGISTRY_FILENAME,
        legacy_schema_version=REGISTRY_SCHEMA_VERSION,
        recovery_policy=RECOVERY_AUTHORITATIVE,
        repair_action="re_register",
        table_map=(
            ("workspaces", "registry_workspaces"),
            ("workspace_activity", "registry_workspace_activity"),
        ),
    ),
    ComponentSpec(
        component="managed_events",
        legacy_filename=MANAGED_EVENTS_FILENAME,
        legacy_schema_version=MANAGED_EVENTS_SCHEMA_VERSION,
        recovery_policy=RECOVERY_APPEND_ONLY,
        repair_action="restore_backup",
        table_map=(("managed_events", "managed_events"),),
    ),
    ComponentSpec(
        component="inventory",
        legacy_filename=INVENTORY_FILENAME,
        legacy_schema_version=INVENTORY_SCHEMA_VERSION,
        recovery_policy=RECOVERY_REBUILDABLE,
        repair_action="reload",
        table_map=(
            ("panes", "inventory_panes"),
            ("inventory_meta", "inventory_meta"),
        ),
    ),
    ComponentSpec(
        component="otel",
        legacy_filename=OTEL_STORE_FILENAME,
        legacy_schema_version=OTEL_STORE_SCHEMA_VERSION,
        recovery_policy=RECOVERY_REBUILDABLE,
        repair_action="restart_receiver",
        table_map=(
            ("otel_events", "otel_events"),
            ("otel_meta", "otel_meta"),
        ),
    ),
)

#: Component name -> spec, and the set the single DB is expected to absorb.
COMPONENTS_BY_NAME: dict[str, ComponentSpec] = {c.component: c for c in COMPONENTS}
COMPONENT_NAMES: tuple[str, ...] = tuple(c.component for c in COMPONENTS)

# Planner / migration per-component action vocabulary. Deliberately small and
# orthogonal to the inspector's status vocabulary: a plan describes what a write
# migration *would* (or did) do, never workflow truth.
ACTION_MIGRATE = "migrate"  # legacy present, valid, not yet recorded -> import
ACTION_SKIP_COMPLETE = "skip_complete"  # already in state_schema_components
ACTION_SKIP_ABSENT = "skip_absent"  # legacy file missing -> nothing to import
ACTION_BLOCKED_CORRUPT = "blocked_corrupt"  # unreadable / integrity error
ACTION_BLOCKED_UNSUPPORTED = "blocked_unsupported"  # schema version mismatch
ACTION_BLOCKED_INCOMPLETE = "blocked_incomplete"  # correct version/integrity but expected table(s) missing

_STATE_SCHEMA_COMPONENTS_SQL = """
CREATE TABLE IF NOT EXISTS state_schema_components (
    component TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    owner TEXT NOT NULL,
    recovery_policy TEXT NOT NULL,
    migrated_from TEXT,
    updated_at TEXT NOT NULL
)
"""


class StateStoreError(RuntimeError):
    """A structural problem the state store must not paper over.

    Raised for a downgrade hazard (an unsupported container ``user_version``) or a
    backup failure that must abort a write migration — the fail-closed cases the
    doc requires. Routine states (an absent file, an absent legacy component, a
    corrupt legacy cache) are *reported*, not raised.
    """


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (sibling-store convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup_stamp(now: str) -> str:
    """Compact filesystem-safe stamp (``20260621T130000Z``) for a backup dir."""
    parsed = datetime.fromisoformat(now)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def state_store_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``state.sqlite`` path under the mozyo-bridge home.

    ``home`` overrides the root (tests pass a temp dir); otherwise the shared
    :func:`mozyo_bridge.shared.paths.mozyo_bridge_home` resolves
    ``MOZYO_BRIDGE_HOME`` / ``~/.mozyo_bridge``.
    """
    return (home or mozyo_bridge_home()) / STATE_STORE_FILENAME


@dataclass(frozen=True)
class ComponentSchemaRow:
    """A recorded ``state_schema_components`` row (a migrated component)."""

    component: str
    schema_version: int
    owner: str
    recovery_policy: str
    migrated_from: Optional[str]
    updated_at: str

    def as_payload(self) -> dict:
        return {
            "component": self.component,
            "schema_version": self.schema_version,
            "owner": self.owner,
            "recovery_policy": self.recovery_policy,
            "migrated_from": self.migrated_from,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ComponentPlan:
    """The planned (or performed) migration action for one component."""

    component: str
    action: str
    recovery_policy: str
    legacy_path: str
    legacy_present: bool
    legacy_schema_version: Optional[int]
    target_tables: tuple[str, ...]
    source_rows: Optional[int]
    reason: str

    def as_payload(self) -> dict:
        return {
            "component": self.component,
            "action": self.action,
            "recovery_policy": self.recovery_policy,
            "legacy_path": self.legacy_path,
            "legacy_present": self.legacy_present,
            "legacy_schema_version": self.legacy_schema_version,
            "target_tables": list(self.target_tables),
            "source_rows": self.source_rows,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MigrationPlan:
    """A read-only migration plan (dry-run) or the record of a performed one."""

    home: str
    db_path: str
    container_version: int
    db_present: bool
    components: tuple[ComponentPlan, ...]
    backup_dir: Optional[str] = None
    backup_files: tuple[str, ...] = ()
    performed: bool = False

    @property
    def migratable(self) -> tuple[ComponentPlan, ...]:
        return tuple(c for c in self.components if c.action == ACTION_MIGRATE)

    def as_payload(self) -> dict:
        return {
            "home": self.home,
            "db_path": self.db_path,
            "container_version": self.container_version,
            "db_present": self.db_present,
            "performed": self.performed,
            "backup_dir": self.backup_dir,
            "backup_files": list(self.backup_files),
            "components": [c.as_payload() for c in self.components],
            "migratable": [c.component for c in self.migratable],
        }


@dataclass(frozen=True)
class CleanupComponentPlan:
    """Per-component legacy-retirement plan (the destructive, separately-gated stage)."""

    component: str
    legacy_path: str
    legacy_present: bool
    migrated: bool
    eligible: bool
    reason: str

    def as_payload(self) -> dict:
        return {
            "component": self.component,
            "legacy_path": self.legacy_path,
            "legacy_present": self.legacy_present,
            "migrated": self.migrated,
            "eligible": self.eligible,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CleanupPlan:
    """Plan / result of retiring migrated legacy files (destructive, gated)."""

    home: str
    db_path: str
    components: tuple[CleanupComponentPlan, ...]
    performed: bool = False
    backup_dir: Optional[str] = None
    removed: tuple[str, ...] = ()

    @property
    def eligible(self) -> tuple[CleanupComponentPlan, ...]:
        return tuple(c for c in self.components if c.eligible)

    def as_payload(self) -> dict:
        return {
            "home": self.home,
            "db_path": self.db_path,
            "performed": self.performed,
            "backup_dir": self.backup_dir,
            "removed": list(self.removed),
            "components": [c.as_payload() for c in self.components],
            "eligible": [c.component for c in self.eligible],
        }


def _probe_legacy(
    path: Path, expected_version: int, expected_tables: tuple[str, ...]
) -> tuple[str, Optional[int], Optional[str]]:
    """Read-only probe of a legacy file: ``(state, user_version, error)``.

    ``state`` is ``absent`` / ``ok`` / ``corrupt`` / ``unsupported`` /
    ``incomplete``. Opens the file through a ``mode=ro`` URI (never creating it),
    runs ``PRAGMA user_version`` + ``integrity_check``, then checks that every
    ``expected_tables`` entry is present. Classification:

    - ``corrupt`` — unreadable or ``integrity_check`` not ``ok``;
    - ``unsupported`` — version mismatch (the migrator leaves it untouched);
    - ``incomplete`` — correct version, integrity ``ok``, but an expected table is
      missing. The doc's import rule requires the *row-shape* (expected tables) to
      be present before a component counts as complete; importing a shape-missing
      file would record a complete component and make an incompletely-migrated
      authoritative legacy file cleanup-eligible (data-loss risk, #12305 review
      j#62394). ``error`` carries the missing table names;
    - ``ok`` — correct version, integrity ``ok``, all expected tables present.
    """
    if not path.exists():
        return "absent", None, None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        return "corrupt", None, str(exc)
    try:
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            return "corrupt", None, str(exc)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            return "corrupt", version, str(exc)
        if check is None or check[0] != "ok":
            return "corrupt", version, "integrity_check did not return ok"
        if version != expected_version:
            return "unsupported", version, None
        try:
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.DatabaseError as exc:
            return "corrupt", version, str(exc)
        missing = [t for t in expected_tables if t not in present]
        if missing:
            return "incomplete", version, ", ".join(missing)
        return "ok", version, None
    finally:
        conn.close()


def _legacy_row_count(path: Path, tables: tuple[str, ...]) -> int:
    """Sum the row counts of ``tables`` in a legacy file, read-only."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        total = 0
        present = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in tables:
            if table in present:
                total += int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        return total
    finally:
        conn.close()


def _build_create_sql(target_table: str, columns: list[tuple]) -> str:
    """Compose a plain ``CREATE TABLE`` for ``target_table`` from PRAGMA table_info.

    Reproduces each column's name / type / NOT NULL / DEFAULT and the primary key,
    but deliberately drops foreign-key clauses, UNIQUE constraints, and indexes:
    the migrated copy is a namespaced projection of the legacy table, and the doc
    forbids requiring a hard cross-component FK (``### namespace / table
    ownership``). The target is schema-qualified ``main."<table>"`` so it can never
    be confused with a same-named attached ``legacy`` table. ``columns`` rows are
    ``(cid, name, type, notnull, dflt, pk)``.
    """
    col_defs: list[str] = []
    pk: list[tuple[int, str]] = []
    for cid, name, ctype, notnull, dflt, pk_index in sorted(columns, key=lambda r: r[0]):
        piece = f'"{name}"'
        if ctype:
            piece += f" {ctype}"
        if notnull:
            piece += " NOT NULL"
        if dflt is not None:
            piece += f" DEFAULT {dflt}"
        col_defs.append(piece)
        if pk_index:
            pk.append((pk_index, name))
    if pk:
        pk_cols = ", ".join(f'"{name}"' for _, name in sorted(pk))
        col_defs.append(f"PRIMARY KEY ({pk_cols})")
    return f'CREATE TABLE main."{target_table}" (\n    ' + ",\n    ".join(col_defs) + "\n)"


class StateStore:
    """Read/write access to the home-scoped single state DB and its migration.

    Construction never touches the filesystem; ``state.sqlite`` is created lazily
    only by :meth:`migrate` (the sole writer). :meth:`plan_migration` and
    :meth:`read_components` are read-only and create nothing.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.home = (home or mozyo_bridge_home()) if path is None else None
        self.path = Path(path) if path is not None else state_store_path(home)

    def _resolved_home(self) -> Path:
        return self.home if self.home is not None else self.path.parent

    # -- connections -------------------------------------------------------

    def _connect_rw(self) -> sqlite3.Connection:
        """Open a read-write connection, creating / validating the container.

        ``PRAGMA user_version`` is the migration guard, mirroring the sibling
        stores. Version ``0`` is a fresh file — create ``state_schema_components``
        and stamp the container version. A newer, unrecognized container fails
        closed via :class:`StateStoreError` rather than being rewritten, so a
        downgraded build never destroys a newer single DB.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute(_STATE_SCHEMA_COMPONENTS_SQL)
            conn.execute(f"PRAGMA user_version = {STATE_CONTAINER_VERSION}")
            conn.commit()
        elif version != STATE_CONTAINER_VERSION:
            conn.close()
            raise StateStoreError(
                f"state store {self.path} has unsupported container version "
                f"{version}; this build understands {STATE_CONTAINER_VERSION}. "
                f"The single DB is left untouched (downgrade-safe); migrate with a "
                f"newer build or move the file aside deliberately."
            )
        return conn

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        """Open a read-only connection if the DB exists; ``None`` when absent.

        A *missing* file is the normal pre-migration state (returns ``None``). An
        existing file with an unsupported container version fails closed.
        """
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            conn.close()
            raise StateStoreError(f"state store {self.path} is unreadable: {exc}") from exc
        if version != STATE_CONTAINER_VERSION:
            conn.close()
            raise StateStoreError(
                f"state store {self.path} has unsupported container version "
                f"{version}; this build understands {STATE_CONTAINER_VERSION}."
            )
        return conn

    # -- reads -------------------------------------------------------------

    def read_components(self) -> tuple[ComponentSchemaRow, ...]:
        """Return the recorded ``state_schema_components`` rows (migrated components).

        Empty when the DB is absent or no component has migrated yet. Read-only;
        fails closed on an unsupported container.
        """
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            try:
                rows = conn.execute(
                    "SELECT component, schema_version, owner, recovery_policy, "
                    "migrated_from, updated_at FROM state_schema_components "
                    "ORDER BY component"
                ).fetchall()
            except sqlite3.DatabaseError:
                return ()
        finally:
            conn.close()
        return tuple(
            ComponentSchemaRow(
                component=r[0],
                schema_version=r[1],
                owner=r[2],
                recovery_policy=r[3],
                migrated_from=r[4],
                updated_at=r[5],
            )
            for r in rows
        )

    def _recorded_names(self) -> set[str]:
        return {row.component for row in self.read_components()}

    # -- planning (read-only) ---------------------------------------------

    def plan_migration(
        self, *, components: Optional[tuple[str, ...]] = None
    ) -> MigrationPlan:
        """Compute a read-only migration plan. Creates nothing, writes nothing.

        For each requested component (default: all), probe the legacy file and the
        recorded ``state_schema_components`` to classify the action a write
        migration would take (:data:`ACTION_MIGRATE` / ``skip_complete`` /
        ``skip_absent`` / ``blocked_corrupt`` / ``blocked_unsupported`` /
        ``blocked_incomplete``) and count the source rows. The single DB and home
        dir are never created here.
        """
        home = self._resolved_home()
        selected = self._select_components(components)
        recorded = self._recorded_names()
        plans: list[ComponentPlan] = []
        for spec in selected:
            legacy_path = home / spec.legacy_filename
            plans.append(self._plan_component(spec, legacy_path, recorded))
        return MigrationPlan(
            home=str(home),
            db_path=str(self.path),
            container_version=STATE_CONTAINER_VERSION,
            db_present=self.path.exists(),
            components=tuple(plans),
        )

    def _select_components(
        self, components: Optional[tuple[str, ...]]
    ) -> tuple[ComponentSpec, ...]:
        if not components:
            return COMPONENTS
        unknown = [c for c in components if c not in COMPONENTS_BY_NAME]
        if unknown:
            raise StateStoreError(
                f"unknown state component(s): {', '.join(unknown)}; "
                f"known: {', '.join(COMPONENT_NAMES)}"
            )
        return tuple(COMPONENTS_BY_NAME[c] for c in components)

    def _plan_component(
        self, spec: ComponentSpec, legacy_path: Path, recorded: set[str]
    ) -> ComponentPlan:
        if spec.component in recorded:
            return ComponentPlan(
                component=spec.component,
                action=ACTION_SKIP_COMPLETE,
                recovery_policy=spec.recovery_policy,
                legacy_path=str(legacy_path),
                legacy_present=legacy_path.exists(),
                legacy_schema_version=None,
                target_tables=spec.target_tables,
                source_rows=None,
                reason="already recorded in state_schema_components; idempotent skip",
            )
        state, version, error = _probe_legacy(
            legacy_path, spec.legacy_schema_version, spec.legacy_tables
        )
        if state == "absent":
            return ComponentPlan(
                component=spec.component,
                action=ACTION_SKIP_ABSENT,
                recovery_policy=spec.recovery_policy,
                legacy_path=str(legacy_path),
                legacy_present=False,
                legacy_schema_version=None,
                target_tables=spec.target_tables,
                source_rows=None,
                reason="legacy file absent; nothing to import",
            )
        if state == "corrupt":
            return ComponentPlan(
                component=spec.component,
                action=ACTION_BLOCKED_CORRUPT,
                recovery_policy=spec.recovery_policy,
                legacy_path=str(legacy_path),
                legacy_present=True,
                legacy_schema_version=version,
                target_tables=spec.target_tables,
                source_rows=None,
                reason=f"legacy file unreadable/corrupt ({error}); "
                f"resolve with `{spec.repair_action}` before migrating",
            )
        if state == "unsupported":
            return ComponentPlan(
                component=spec.component,
                action=ACTION_BLOCKED_UNSUPPORTED,
                recovery_policy=spec.recovery_policy,
                legacy_path=str(legacy_path),
                legacy_present=True,
                legacy_schema_version=version,
                target_tables=spec.target_tables,
                source_rows=None,
                reason=f"legacy schema_version {version} != expected "
                f"{spec.legacy_schema_version}; left untouched (downgrade-safe)",
            )
        if state == "incomplete":
            # Correct version + integrity but the expected row-shape is missing.
            # Do NOT migrate: importing would record a complete component and make
            # an incompletely-migrated authoritative legacy file cleanup-eligible
            # (#12305 review j#62394 data-loss finding).
            return ComponentPlan(
                component=spec.component,
                action=ACTION_BLOCKED_INCOMPLETE,
                recovery_policy=spec.recovery_policy,
                legacy_path=str(legacy_path),
                legacy_present=True,
                legacy_schema_version=version,
                target_tables=spec.target_tables,
                source_rows=None,
                reason=f"legacy file is missing expected table(s) ({error}); "
                f"not migrated (would be an incomplete/partial import); "
                f"resolve with `{spec.repair_action}` before migrating",
            )
        return ComponentPlan(
            component=spec.component,
            action=ACTION_MIGRATE,
            recovery_policy=spec.recovery_policy,
            legacy_path=str(legacy_path),
            legacy_present=True,
            legacy_schema_version=version,
            target_tables=spec.target_tables,
            source_rows=_legacy_row_count(legacy_path, spec.legacy_tables),
            reason="legacy present and valid; will import into namespace",
        )

    # -- backup ------------------------------------------------------------

    def _backup(self, files: list[Path], stamp: str) -> tuple[Path, list[str]]:
        """Copy ``files`` into ``backups/state-<stamp>/`` under home, fail-closed.

        Raises :class:`StateStoreError` on any copy failure so a write migration
        never proceeds with an incomplete backup.
        """
        backup_dir = self._resolved_home() / BACKUPS_DIRNAME / f"state-{stamp}"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            copied: list[str] = []
            for src in files:
                if src.exists():
                    shutil.copy2(src, backup_dir / src.name)
                    copied.append(src.name)
        except OSError as exc:
            raise StateStoreError(
                f"backup to {backup_dir} failed ({exc}); migration aborted "
                f"(nothing was written)"
            ) from exc
        return backup_dir, copied

    # -- write migration ---------------------------------------------------

    def migrate(
        self,
        *,
        components: Optional[tuple[str, ...]] = None,
        backup: bool = True,
        now: Optional[str] = None,
    ) -> MigrationPlan:
        """Backup-first, idempotent, non-destructive write migration.

        Plans the requested components, backs up every legacy file it will read
        plus any existing ``state.sqlite`` (unless ``backup=False``), then imports
        each :data:`ACTION_MIGRATE` component into its target namespace and records
        a ``state_schema_components`` row. Already-recorded components are skipped
        (idempotent). Legacy files are only ever read, never written or deleted
        (non-destructive). A blocked component (corrupt / unsupported) is left for
        the operator and not imported. Returns the performed plan (``performed=True``).
        """
        stamp = now or _utc_now()
        plan = self.plan_migration(components=components)
        migratable = plan.migratable
        if not migratable:
            # Nothing to import: do not create the DB or a backup dir for a no-op.
            return MigrationPlan(
                home=plan.home,
                db_path=plan.db_path,
                container_version=plan.container_version,
                db_present=self.path.exists(),
                components=plan.components,
                performed=True,
            )

        backup_dir: Optional[str] = None
        backup_files: tuple[str, ...] = ()
        if backup:
            to_back_up = [Path(c.legacy_path) for c in migratable]
            if self.path.exists():
                to_back_up.append(self.path)
            backup_path, copied = self._backup(to_back_up, _backup_stamp(stamp))
            backup_dir = str(backup_path)
            backup_files = tuple(copied)

        conn = self._connect_rw()
        try:
            for cplan in migratable:
                # Each component commits its own copies + metadata atomically
                # (inside _import_component), so a partial migration is resumable.
                spec = COMPONENTS_BY_NAME[cplan.component]
                self._import_component(conn, spec, Path(cplan.legacy_path), now=stamp)
        finally:
            conn.close()

        # Re-plan post-write so the returned components reflect the new recorded
        # state (the just-migrated ones now read as skip_complete).
        final = self.plan_migration(components=components)
        return MigrationPlan(
            home=final.home,
            db_path=final.db_path,
            container_version=final.container_version,
            db_present=self.path.exists(),
            components=final.components,
            backup_dir=backup_dir,
            backup_files=backup_files,
            performed=True,
        )

    def _import_component(
        self, conn: sqlite3.Connection, spec: ComponentSpec, legacy_path: Path, *, now: str
    ) -> None:
        """Copy one component's legacy tables into its namespace; record metadata.

        Owner-namespace only: drops and rebuilds *this* component's target tables
        (safe — they are copies; the legacy source is untouched) then copies rows,
        so a re-run after a partial failure rebuilds cleanly. ATTACHes the legacy
        file as a source it only ever SELECTs from, so the legacy file stays
        byte-identical (verified by the non-destructive test).
        """
        conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy_path),))
        try:
            legacy_tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM legacy.sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = [t for t, _ in spec.table_map if t not in legacy_tables]
            if missing:
                # Defense-in-depth: the planner already classifies a shape-missing
                # legacy file as ``blocked_incomplete`` and never routes it here, so
                # this is a TOCTOU guard. Fail closed rather than record a complete
                # component for a partial import (#12305 review j#62394).
                raise StateStoreError(
                    f"legacy file {legacy_path} for component '{spec.component}' is "
                    f"missing expected table(s): {', '.join(missing)}; refusing to "
                    f"record an incomplete migration"
                )
            for legacy_table, target_table in spec.table_map:
                # All target operations are schema-qualified ``main.`` — an
                # unqualified ``DROP``/``CREATE`` would resolve to a same-named
                # attached ``legacy`` table when ``main`` has none yet, dropping the
                # legacy data and breaking non-destructiveness.
                conn.execute(f'DROP TABLE IF EXISTS main."{target_table}"')
                # Parameterized table-valued form (schema as 2nd arg): unambiguous
                # even when the target table shadows the legacy table name, unlike
                # the schema-qualified ``PRAGMA legacy.table_info(name)`` form.
                columns = conn.execute(
                    "SELECT cid, name, type, \"notnull\", dflt_value, pk "
                    "FROM pragma_table_info(?, 'legacy')",
                    (legacy_table,),
                ).fetchall()
                conn.execute(_build_create_sql(target_table, columns))
                col_list = ", ".join(f'"{c[1]}"' for c in sorted(columns, key=lambda r: r[0]))
                conn.execute(
                    f'INSERT INTO main."{target_table}" ({col_list}) '
                    f'SELECT {col_list} FROM legacy."{legacy_table}"'
                )
            conn.execute(
                "INSERT INTO state_schema_components "
                "(component, schema_version, owner, recovery_policy, migrated_from, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(component) DO UPDATE SET "
                "schema_version = excluded.schema_version, "
                "owner = excluded.owner, recovery_policy = excluded.recovery_policy, "
                "migrated_from = excluded.migrated_from, updated_at = excluded.updated_at",
                (
                    spec.component,
                    spec.legacy_schema_version,
                    spec.component,
                    spec.recovery_policy,
                    spec.legacy_filename,
                    now,
                ),
            )
            # Commit the component's copies + metadata atomically *before* DETACH:
            # SQLite refuses to DETACH a database referenced by an open transaction.
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE legacy")

    # -- legacy cleanup (destructive; separately gated) -------------------

    def plan_cleanup(
        self, *, components: Optional[tuple[str, ...]] = None
    ) -> CleanupPlan:
        """Plan which migrated legacy files are eligible for retirement. Read-only.

        A legacy file is *eligible* only when its component is recorded complete in
        ``state_schema_components`` (so the data lives in the single DB) and the
        legacy file is still present. This is the read-only half of the
        deliberately separate, destructive cleanup stage; :meth:`cleanup` performs
        the removal and demands an explicit gate.
        """
        home = self._resolved_home()
        selected = self._select_components(components)
        recorded = self._recorded_names()
        plans: list[CleanupComponentPlan] = []
        for spec in selected:
            legacy_path = home / spec.legacy_filename
            present = legacy_path.exists()
            migrated = spec.component in recorded
            eligible = present and migrated
            if not migrated:
                reason = "not migrated; retiring the legacy file would lose data"
            elif not present:
                reason = "legacy file already absent"
            else:
                reason = "migrated into the single DB; safe to retire after backup"
            plans.append(
                CleanupComponentPlan(
                    component=spec.component,
                    legacy_path=str(legacy_path),
                    legacy_present=present,
                    migrated=migrated,
                    eligible=eligible,
                    reason=reason,
                )
            )
        return CleanupPlan(
            home=str(home), db_path=str(self.path), components=tuple(plans)
        )

    def cleanup(
        self,
        *,
        components: Optional[tuple[str, ...]] = None,
        confirm_destroy: bool = False,
        backup: bool = True,
        now: Optional[str] = None,
    ) -> CleanupPlan:
        """Retire migrated legacy files. DESTRUCTIVE — requires ``confirm_destroy``.

        The separately-gated destructive stage (managed-state-model.md
        ``### corruption quarantine / component repair`` / ``### implementation
        order`` step 5). Without ``confirm_destroy=True`` it is a no-op that returns
        the read-only :meth:`plan_cleanup` — the gate is explicit, never implied by
        a migrate flag. With it, only *eligible* (migrated + present) legacy files
        are backed up and then deleted; a not-yet-migrated legacy file is never
        removed, so retirement can never lose un-migrated data.
        """
        plan = self.plan_cleanup(components=components)
        if not confirm_destroy:
            return plan
        eligible = plan.eligible
        if not eligible:
            return CleanupPlan(
                home=plan.home,
                db_path=plan.db_path,
                components=plan.components,
                performed=True,
            )
        stamp = now or _utc_now()
        backup_dir: Optional[str] = None
        if backup:
            backup_path, _ = self._backup(
                [Path(c.legacy_path) for c in eligible], _backup_stamp(stamp)
            )
            backup_dir = str(backup_path)
        removed: list[str] = []
        for cplan in eligible:
            Path(cplan.legacy_path).unlink()
            removed.append(Path(cplan.legacy_path).name)
        return CleanupPlan(
            home=plan.home,
            db_path=plan.db_path,
            components=plan.components,
            performed=True,
            backup_dir=backup_dir,
            removed=tuple(removed),
        )


__all__ = (
    "STATE_STORE_FILENAME",
    "STATE_CONTAINER_VERSION",
    "BACKUPS_DIRNAME",
    "RECOVERY_AUTHORITATIVE",
    "RECOVERY_APPEND_ONLY",
    "RECOVERY_REBUILDABLE",
    "ACTION_MIGRATE",
    "ACTION_SKIP_COMPLETE",
    "ACTION_SKIP_ABSENT",
    "ACTION_BLOCKED_CORRUPT",
    "ACTION_BLOCKED_UNSUPPORTED",
    "ACTION_BLOCKED_INCOMPLETE",
    "ComponentSpec",
    "COMPONENTS",
    "COMPONENTS_BY_NAME",
    "COMPONENT_NAMES",
    "ComponentSchemaRow",
    "ComponentPlan",
    "MigrationPlan",
    "CleanupComponentPlan",
    "CleanupPlan",
    "StateStoreError",
    "state_store_path",
    "StateStore",
)
