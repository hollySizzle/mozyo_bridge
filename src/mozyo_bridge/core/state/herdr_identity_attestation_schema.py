"""Attestation-store schema / compatibility authority (Redmine #13882).

The startup self-attestation store (:mod:`.herdr_identity_attestation`) is the one
managed-state file a **shared ``MOZYO_BRIDGE_HOME`` hands to launchers of different
vintages at once**: a managed launch injects ``--env MOZYO_BRIDGE_HOME=<store_home>``
into the child (``herdr_launch_argv``), so whichever runtime the operator happens to
have installed writes into the *same* file the source runtime reads. That makes it a
**mixed-runtime store**, and it is why this module's policy deliberately diverges from
its sibling ``lane_lifecycle_schema`` even though it borrows that module's structure.

The failure this module closes (#13882, live evidence): a shared home holding the
pre-0.12 **v1** shape is opened by a **v2** runtime. The old exact-version write guard
raised, the best-effort writer swallowed the error (an agent boot must never be blocked
by a store failure), and the pair booted **live but unattested** — every downstream
verify then failed closed with no public recovery. The launcher-capability probe
(#13847) could not see it: it joins the launcher's *advertised* schema against the
*source runtime's required* schema — both **code** — and never opens the selected store.

**Read-compatible / write-conservative** (``managed-state-model.md``
``#### attestation store: read-compatible / write-conservative (#13882)``):

- **Reads never migrate.** :func:`readonly_compatible_select` projects an older shape up
  to the current column vocabulary, padding absent columns with their *migration default
  literal*, inside one pinned read transaction (the #13844 ``BEGIN`` discipline).
- **Writes never migrate either** — the divergence from ``lane_lifecycle``. There, every
  mutation migrates through one write gate, because that store's readers all ship in the
  same runtime. Here an auto-migration on the shared home would leave every *older
  installed* launcher hitting its own exact-version guard, silently dropping its
  attestation and booting live-but-unattested: the very defect #13882 exists to kill,
  merely inverted onto the old runtimes. So a v1 store stays v1 until an operator runs
  the explicit backup-first migration command; a normal launch instead writes the
  **v1-shaped** row (:func:`writable_projection`).
- **Nothing is ever fabricated.** The v1 shape lacks only ``replacement_action_id``, a
  field introduced with the replacement transaction (#13806). A v1 row therefore
  *truthfully* carries an empty action id — padding it with ``''`` is a proven
  backward-compatible projection, not a guess. The converse is refused: a **replacement**
  launch (non-empty ``action_id``) can never be written v1-shaped, because dropping that
  field would silently lose the binding a replacement recovery matches on.

Compatibility is judged only by the **shape / capability table**
(:data:`_ALLOWED_SHAPES_BY_VERSION` / :data:`_COLUMN_DEFS`), never a guessed version
comparison — a recognized version whose on-disk columns disagree is partial / corrupt and
fails closed rather than being silently repaired.

Pure + sqlite only: it imports stdlib and the shared backup constants, so the dependency
never points core -> provider.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.state_store import BACKUPS_DIRNAME, StateStoreError

_TABLE = "herdr_identity_attestations"

#: The shape this build writes for a fresh store and reads as native.
HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION = 2

#: The store shapes this build can read (and, per the write policy above, write
#: *conservatively* without migrating). v1 = pre-#13806, v2 = additive
#: ``replacement_action_id``. A version outside this set is unsupported and fails closed
#: with the file byte-untouched.
RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2})

#: Recovery policy (``managed-state-model.md`` ``### recovery policy vocabulary``): a
#: rebuildable projection — losing it degrades to fail-closed (adopt refuses, doctor
#: non-green) and the next launch's self-attestation re-derives it.
HERDR_IDENTITY_ATTESTATION_RECOVERY_POLICY = "rebuildable_cache"

# --- Shape / capability table (the ONLY compatibility authority). ---------------------
_V1_COLUMNS = frozenset(
    {
        "assigned_name",
        "workspace_id",
        "role",
        "lane_id",
        "locator",
        "verdict",
        "detail",
        "observed_at",
    }
)
#: v2 (#13806 tranche D R2-F2) is purely additive over v1.
_V2_ADDS = frozenset({"replacement_action_id"})
_SHAPE_V1 = _V1_COLUMNS
_SHAPE_V2 = _V1_COLUMNS | _V2_ADDS

_ALLOWED_SHAPES_BY_VERSION: dict[int, tuple[frozenset, ...]] = {
    1: (_SHAPE_V1,),
    2: (_SHAPE_V2,),
}

#: The current column vocabulary, in the order every read projects and every native write
#: uses. Kept as an ordered tuple so a projection and a native SELECT decode identically.
COLUMNS_V2 = (
    "assigned_name",
    "workspace_id",
    "role",
    "lane_id",
    "locator",
    "verdict",
    "detail",
    "observed_at",
    "replacement_action_id",
)
#: The v1 write vocabulary — the same order minus the additive column.
COLUMNS_V1 = tuple(c for c in COLUMNS_V2 if c not in _V2_ADDS)

#: ``column -> migration default literal`` for columns absent from an older shape. A
#: column mapped to ``None`` has no proven default and makes a projection fail closed
#: rather than invent a value. ``replacement_action_id`` defaults to the empty string
#: because a v1 row was written by a runtime with no replacement concept at all — ``''``
#: is that row's true value, not a fabrication.
_COLUMN_DEFAULTS: dict[str, Optional[str]] = {"replacement_action_id": "''"}

#: The DDL each additive column is migrated in with (must agree with ``_TABLE_SQL_V2``).
_COLUMN_MIGRATION_DDL: dict[str, str] = {
    "replacement_action_id": "TEXT NOT NULL DEFAULT ''",
}

_TABLE_SQL_V2 = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    assigned_name TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    role TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    locator TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    replacement_action_id TEXT NOT NULL DEFAULT ''
)
"""

# --- Store status vocabulary (read side). --------------------------------------------
#: No store file / no table yet — a fresh home. Not an error.
STORE_ABSENT = "store_absent"
#: A recognized version whose on-disk shape matches its version's capability entry.
STORE_RECOGNIZED = "store_recognized"
#: A version outside :data:`RECOGNIZED_SCHEMA_VERSIONS`, or a shape that disagrees with
#: its recorded version (partial / corrupt / foreign).
STORE_UNSUPPORTED = "store_unsupported"
#: The file exists but could not be opened / queried (corrupt, permissions, not a DB).
#: Distinct from :data:`STORE_UNSUPPORTED`: nothing about its shape is even knowable.
STORE_UNREADABLE = "store_unreadable"

#: Sentinel for a ``user_version`` that is present but not an exact integer.
_VERSION_MALFORMED = -1


class HerdrIdentityAttestationSchemaError(RuntimeError):
    """A user-actionable schema / migration error (fail-closed, file untouched)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup_stamp(now: str) -> str:
    """Compact filesystem-safe stamp (``20260716T130000Z``) for a backup dir."""
    parsed = datetime.fromisoformat(now)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def recorded_version(conn: sqlite3.Connection) -> Optional[int]:
    """The store's ``PRAGMA user_version``, or ``None`` for a fresh (unstamped) file.

    Three distinct outcomes, mirroring ``lane_lifecycle_schema._recorded_version``:
    ``None`` (never stamped -> a fresh store), :data:`_VERSION_MALFORMED` (present but not
    an exact integer), or the integer. ``bool`` is excluded and a REAL like ``2.5`` is
    rejected rather than truncated — ``int(2.5) == 2`` would otherwise walk a corrupt
    store straight through a version guard (the #13689 trap).
    """
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.DatabaseError:
        return _VERSION_MALFORMED
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int):
        return _VERSION_MALFORMED
    if value == 0:
        return None
    return value


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)
    ).fetchone()
    return row is not None


def _present_columns(conn: sqlite3.Connection) -> frozenset:
    return frozenset(row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})"))


def shape_matches(conn: sqlite3.Connection, version: int) -> bool:
    """Whether the on-disk columns equal one allowed shape for ``version`` (set equality).

    Set **equality**, not containment: an extra column means the file is not the shape
    this build understands, so it is partial / corrupt / foreign and fails closed rather
    than being read through a version number that no longer describes it.
    """
    allowed = _ALLOWED_SHAPES_BY_VERSION.get(version)
    if not allowed:
        return False
    present = _present_columns(conn)
    return any(present == shape for shape in allowed)


def store_status(conn: sqlite3.Connection) -> str:
    """Classify an open store connection: absent / recognized / unsupported."""
    version = recorded_version(conn)
    if version is None:
        # Never stamped. A table without a version is a partial store, not a fresh one:
        # adopting it silently would guess a shape the writer never declared.
        return STORE_UNSUPPORTED if _table_exists(conn) else STORE_ABSENT
    if version == _VERSION_MALFORMED or version not in RECOGNIZED_SCHEMA_VERSIONS:
        return STORE_UNSUPPORTED
    if not _table_exists(conn):
        return STORE_UNSUPPORTED
    if not shape_matches(conn, version):
        return STORE_UNSUPPORTED
    return STORE_RECOGNIZED


def reader_upgrade_required(conn: sqlite3.Connection) -> bool:
    """Whether an unsupported store is unsupported *because this build is too old*.

    ``True`` only for a store strictly newer than anything recognized — the operator's
    action is "upgrade this runtime". A malformed / partial / corrupt store is ``False``:
    it fails closed without dishonestly claiming an upgrade would fix it.
    """
    version = recorded_version(conn)
    if version is None or version == _VERSION_MALFORMED:
        return False
    return version > max(RECOGNIZED_SCHEMA_VERSIONS)


def readonly_compatible_select(conn: sqlite3.Connection) -> Optional[str]:
    """A SELECT list projecting any recognized shape onto :data:`COLUMNS_V2`, or ``None``.

    Emits the full current column vocabulary in :data:`COLUMNS_V2` order, projecting a
    column absent from an older shape as its migration-default *literal* (``'' AS
    replacement_action_id`` for a v1 store) so the caller decodes a v1 row with exactly
    the same row-decoder as a native v2 row. Returns ``None`` — fail closed, never a
    partial read — when the store is not a recognized, shape-matched version, or when an
    absent column has no proven default.

    Reads never migrate: this constructs a projection over the file as it lies.
    """
    if store_status(conn) != STORE_RECOGNIZED:
        return None
    present = _present_columns(conn)
    parts: list[str] = []
    for column in COLUMNS_V2:
        if column in present:
            parts.append(column)
            continue
        default = _COLUMN_DEFAULTS.get(column)
        if default is None:
            return None
        parts.append(f"{default} AS {column}")
    return ", ".join(parts)


def writable_projection(version: int) -> Optional[tuple[str, ...]]:
    """The column vocabulary a write must use against a store at ``version``.

    ``COLUMNS_V2`` for a v2 store, ``COLUMNS_V1`` for a v1 store (the conservative,
    non-migrating write), or ``None`` for an unrecognized version. The caller is
    responsible for refusing a write whose payload cannot survive the returned
    projection — see :func:`write_drops_replacement_action_id`.
    """
    if version not in RECOGNIZED_SCHEMA_VERSIONS:
        return None
    return COLUMNS_V2 if version >= 2 else COLUMNS_V1


def write_drops_replacement_action_id(version: int, replacement_action_id: str) -> bool:
    """Whether writing ``replacement_action_id`` at ``version`` would silently drop it.

    The single predicate separating the two launch kinds of acceptance 2: a **normal**
    launch carries an empty action id, so the v1 projection loses nothing and is a proven
    backward-compatible path; a **replacement** launch carries a non-empty one, which the
    v1 shape has nowhere to put — the write must be refused visibly rather than land a row
    a replacement recovery would later fail to match.
    """
    if not (replacement_action_id or "").strip():
        return False
    projection = writable_projection(version)
    return projection is not None and "replacement_action_id" not in projection


@dataclass(frozen=True)
class StoreSchemaObservation:
    """The value-free facts an on-disk store probe carries (Redmine #13882 acceptance 1).

    ``state`` is one of :data:`STORE_ABSENT` / :data:`STORE_RECOGNIZED` /
    :data:`STORE_UNSUPPORTED` / :data:`STORE_UNREADABLE`. ``version`` is the recorded
    ``user_version`` when one could be read at all (``None`` for absent / unreadable).
    ``upgrade_required`` distinguishes "this runtime is too old for that store" from
    "that store is corrupt", so the refusal can name the operator's real next action
    rather than dishonestly suggesting an upgrade would fix a corrupt file.
    """

    state: str
    version: Optional[int]
    upgrade_required: bool = False

    @property
    def usable(self) -> bool:
        return self.state in (STORE_ABSENT, STORE_RECOGNIZED)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "version": self.version,
            "upgrade_required": self.upgrade_required,
        }


def probe_store_schema(path: Path) -> StoreSchemaObservation:
    """Read-only probe of the **selected** store's on-disk schema (never migrates).

    The missing input of the #13847 capability preflight, which joined the launcher's
    advertised schema against the source runtime's required schema — both *code* — and so
    never noticed a shared home holding a different shape on *disk*. Read-only and
    fail-closed: it creates nothing (an absent store is a legitimate fresh home), and an
    unopenable file reports :data:`STORE_UNREADABLE` rather than being folded into
    "absent" — an unreadable store is not an empty one.
    """
    if not path.exists():
        return StoreSchemaObservation(STORE_ABSENT, None)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return StoreSchemaObservation(STORE_UNREADABLE, None)
    try:
        conn.execute("PRAGMA busy_timeout = 2000")
        conn.execute("BEGIN")
        status = store_status(conn)
        version = recorded_version(conn)
        upgrade = reader_upgrade_required(conn)
    except sqlite3.DatabaseError:
        return StoreSchemaObservation(STORE_UNREADABLE, None)
    finally:
        conn.close()
    if version == _VERSION_MALFORMED:
        version = None
    return StoreSchemaObservation(status, version, upgrade)


def create_schema(conn: sqlite3.Connection) -> None:
    """Create a fresh store at the current version (used only when nothing exists)."""
    conn.execute(_TABLE_SQL_V2)
    conn.execute(f"PRAGMA user_version = {HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}")


#: SQLite sidecars that can carry committed state or forensic evidence beside the main DB
#: file. A raw quarantine must move the whole artifact set, not just the main file
#: (Redmine #13882 review j#80029 R2-F1(b)).
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _new_backup_dir(path: Path) -> Path:
    """Create the next free ``backups/<stem>-<ts>[-N]/`` directory (never overwriting)."""
    base = path.parent / BACKUPS_DIRNAME / f"{path.stem}-{_backup_stamp(_utc_now())}"
    backup_dir = base
    suffix = 1
    try:
        while backup_dir.exists():
            backup_dir = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        backup_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise StateStoreError(
            f"backup near {base} failed ({exc}); operation aborted (nothing was written)"
        ) from exc
    return backup_dir


def backup_attestation_store(path: Path) -> Optional[Path]:
    """Take a **logical** snapshot before a migration. Fail-closed; never falls back.

    Uses SQLite's backup API, not a file copy (review j#80000 finding 1): ``shutil.copy2``
    duplicates only the main DB file, so a WAL store leaves committed pages in ``-wal`` and
    the snapshot loses them — reproduced as a recovery point reading ``version=1, rows=0``
    while the live store held the row. A recovery point that is incomplete *and trusted* is
    worse than none. ``Connection.backup()`` is transaction-consistent and
    checkpoint-independent, so it carries every committed row whatever the journal mode.

    **Any** failure raises :class:`StateStoreError` (review j#80029 R2-F1). The first fix
    caught ``sqlite3.DatabaseError`` here and fell back to a byte copy, reasoning that such
    an error meant "not a database" — but that exception is raised just as readily when a
    *valid* database is busy or its I/O fails, and the type carries no way to tell the two
    apart. Fault-injecting a lock error into a valid WAL store's ``backup()`` made the
    migration report ``migration_applied`` while writing a ``rows=0`` recovery point: the
    original defect, regenerated through its own fix. Corruption is not something to infer
    from an exception type — it is decided **by the caller's intent**, before the call, from
    :func:`probe_store_schema`. A caller that wants raw byte preservation for an
    already-proven-unreadable store calls :func:`quarantine_attestation_store_artifacts`.

    An existing snapshot is never overwritten (a second-precision stamp can collide, so a
    taken directory takes a numeric suffix). Returns ``None`` when there is nothing to
    preserve yet.
    """
    if not path.exists():
        return None
    backup_dir = _new_backup_dir(path)
    target = backup_dir / path.name
    source: Optional[sqlite3.Connection] = None
    dest: Optional[sqlite3.Connection] = None
    try:
        # Read-only source: a snapshot must never mutate the store it preserves.
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        source.execute("PRAGMA busy_timeout = 2000")
        dest = sqlite3.connect(target)
        source.backup(dest)
        dest.commit()
    except (sqlite3.DatabaseError, OSError) as exc:
        raise StateStoreError(
            f"logical snapshot of {path} failed ({exc.__class__.__name__}: {exc}); "
            f"refusing to migrate without a complete recovery point (the store is left "
            f"untouched). A byte copy is NOT substituted here: it would silently drop any "
            f"WAL-committed rows and produce a recovery point that looks valid"
        ) from exc
    finally:
        for conn in (source, dest):
            if conn is not None:
                conn.close()
    return backup_dir


def quarantine_attestation_store_artifacts(path: Path) -> Optional[Path]:
    """Raw byte-preserve the whole store artifact set before ``rebuild`` rotates it away.

    The counterpart to :func:`backup_attestation_store`, split by **caller intent** rather
    than by exception type (review j#80029 R2-F1). Only ``rebuild`` calls this, and only
    after :func:`probe_store_schema` has already proven the store unreadable / unsupported:
    such a file has no logical snapshot to take, and its *bytes are the evidence* an
    operator may need to diagnose what happened, so a raw copy is the correct — and only —
    semantics here.

    Preserves the **whole artifact set**, not just the main file (R2-F1(b)): a crashed WAL
    writer leaves ``-wal`` / ``-shm`` beside a corrupt main DB, and copying only the main
    file stranded that evidence in place while the rebuild removed its sibling. Every
    sidecar that exists is captured alongside it.
    """
    if not path.exists():
        return None
    backup_dir = _new_backup_dir(path)
    try:
        shutil.copy2(path, backup_dir / path.name)
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, backup_dir / sidecar.name)
    except OSError as exc:
        raise StateStoreError(
            f"quarantine of {path} failed ({exc}); rebuild aborted (nothing was removed)"
        ) from exc
    return backup_dir


def remove_attestation_store_artifacts(path: Path) -> None:
    """Remove the store and every sidecar (only after a successful quarantine).

    Leaving a ``-wal`` behind while removing the main DB would let a later open resurrect
    a partial store from the orphaned sidecar, so the rotation must be whole-artifact too.
    """
    for candidate in (path, *(path.with_name(path.name + s) for s in _SIDECAR_SUFFIXES)):
        if candidate.exists():
            candidate.unlink()


# --- Migration outcome vocabulary (the explicit operator command only). ---------------
#: The store was already at the current version; no DDL ran.
MIGRATION_INTACT = "migration_intact"
#: An older recognized store was migrated additively, backup-first.
MIGRATION_APPLIED = "migration_applied"
#: There was no store to migrate.
MIGRATION_ABSENT = "migration_absent"


@dataclass(frozen=True)
class AttestationMigrationOutcome:
    """The auditable result of a migration attempt (structured, not only stderr)."""

    outcome: str
    from_version: Optional[int]
    to_version: int
    backup_dir: Optional[Path]

    @property
    def migrated(self) -> bool:
        return self.outcome == MIGRATION_APPLIED

    def as_payload(self) -> dict:
        return {
            "outcome": self.outcome,
            "from_version": self.from_version,
            "to_version": self.to_version,
            # A path is operator-facing evidence; the caller redacts for pasteable records.
            "backup_dir": str(self.backup_dir) if self.backup_dir else None,
            "migrated": self.migrated,
        }


def migrate_attestation_store(path: Path) -> AttestationMigrationOutcome:
    """Backup-first, additive, idempotent migration to the current shape.

    **Never** called from a launch (see the module docstring's write policy) — only from
    the explicit operator command, which additionally refuses while consumers are active.
    ``BEGIN IMMEDIATE`` takes the reserved lock *before* the version is read, so two
    migrators cannot both snapshot the pre-migration state; the backup is taken inside
    that lock before the first ``ALTER``, and any failure rolls back to the predecessor
    with the snapshot as the recovery point.
    """
    if not path.exists():
        return AttestationMigrationOutcome(
            MIGRATION_ABSENT, None, HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION, None
        )
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA busy_timeout = 2000")
        conn.execute("BEGIN IMMEDIATE")
        status = store_status(conn)
        version = recorded_version(conn)
        if status == STORE_ABSENT:
            create_schema(conn)
            conn.execute("COMMIT")
            return AttestationMigrationOutcome(
                MIGRATION_ABSENT, None, HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION, None
            )
        if status != STORE_RECOGNIZED:
            _rollback_quietly(conn)
            upgrade = reader_upgrade_required(conn)
            hint = (
                "it is newer than this build understands; use a newer runtime"
                if upgrade
                else "its recorded version and on-disk shape disagree (partial / corrupt "
                "/ foreign); restore from a backup"
            )
            raise HerdrIdentityAttestationSchemaError(
                f"herdr identity attestation store {path} cannot be migrated: {hint}. "
                f"The store is left untouched (fail-closed, no silent repair)."
            )
        if version == HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION:
            _rollback_quietly(conn)
            return AttestationMigrationOutcome(
                MIGRATION_INTACT,
                version,
                HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
                None,
            )
        try:
            backup_dir = backup_attestation_store(path)
        except StateStoreError as exc:
            _rollback_quietly(conn)
            raise HerdrIdentityAttestationSchemaError(
                f"herdr identity attestation migration to "
                f"v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION} aborted: {exc}. "
                f"The store is left untouched (backup-first)."
            ) from exc
        try:
            present = _present_columns(conn)
            for column in COLUMNS_V2:
                if column in present:
                    continue
                ddl = _COLUMN_MIGRATION_DDL.get(column)
                if ddl is None:
                    raise HerdrIdentityAttestationSchemaError(
                        f"herdr identity attestation migration cannot add {column!r}: "
                        f"no additive definition (the store is left untouched)"
                    )
                conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {column} {ddl}")
            conn.execute(
                f"PRAGMA user_version = {HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}"
            )
            conn.execute("COMMIT")
        except (sqlite3.DatabaseError, HerdrIdentityAttestationSchemaError):
            _rollback_quietly(conn)
            raise
        return AttestationMigrationOutcome(
            MIGRATION_APPLIED,
            version,
            HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
            backup_dir,
        )
    finally:
        conn.close()


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        pass


__all__ = (
    "COLUMNS_V1",
    "COLUMNS_V2",
    "HERDR_IDENTITY_ATTESTATION_RECOVERY_POLICY",
    "HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION",
    "MIGRATION_ABSENT",
    "MIGRATION_APPLIED",
    "MIGRATION_INTACT",
    "RECOGNIZED_SCHEMA_VERSIONS",
    "STORE_ABSENT",
    "STORE_RECOGNIZED",
    "STORE_UNREADABLE",
    "STORE_UNSUPPORTED",
    "AttestationMigrationOutcome",
    "HerdrIdentityAttestationSchemaError",
    "StoreSchemaObservation",
    "backup_attestation_store",
    "create_schema",
    "migrate_attestation_store",
    "probe_store_schema",
    "quarantine_attestation_store_artifacts",
    "remove_attestation_store_artifacts",
    "readonly_compatible_select",
    "reader_upgrade_required",
    "recorded_version",
    "shape_matches",
    "store_status",
    "writable_projection",
    "write_drops_replacement_action_id",
)
