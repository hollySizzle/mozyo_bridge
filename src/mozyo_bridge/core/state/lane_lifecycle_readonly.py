"""Lane lifecycle — read-only, version-compatible, NON-MIGRATING access (Redmine #13844).

The *read* half of the read-compatible / write-migrating split. Parallel repo lanes each
run a source CLI of a different schema generation, but the lifecycle authority is
home-scoped and shared. A newer-schema source CLI that forward-migrates the shared store on
a mere READ (status / handoff / review / callback / drain routing) fail-closes every
concurrent older-schema reader: the older reader can no longer read the authority, so a
``standard`` handoff stops with ``gateway_route_blocked`` and the transport rail stalls
permanently (#13813 j#79382).

This module is the fix: every routing / notification read of the lifecycle authority goes
through :class:`LaneLifecycleReader` (or :func:`load_lane_lifecycle_readonly`), which

- **never migrates**: it opens the store read-only (``mode=ro``) — no ``ensure`` DDL, no
  ``ALTER``, no version re-stamp, no backup — so a shared v5 store stays v5 while a v6
  source CLI reads it, and the concurrent v5 reader keeps working;
- **reads older known additive shapes** by padding the columns the store lacks with their
  in-memory migration defaults (:func:`readonly_compatible_select`), so a newer build reads
  an older store faithfully without touching a byte;
- **fails closed on anything unknown / newer / partial / malformed** exactly like the write
  guard, so an old reader never downgrades or misreads a newer store — and it names the
  specific NEWER sub-case :data:`READER_UPGRADE_REQUIRED` so the caller can route to the
  current compatible facade rather than a raw downgrade (item 5).

Only a genuinely mutating use case (a CAS write in :mod:`...lane_lifecycle`) takes the
migrating :func:`ensure_lane_lifecycle_schema` path. This module depends only on the leaf
schema / rows / model layers (no import cycle with the CAS store).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    DISPOSITION_ACTIVE,
    LaneLifecycleKey,
    LaneLifecycleRecord,
    OWNER_ABSENT,
    OWNER_AMBIGUOUS,
    OWNER_RESOLVED,
    OWNER_UNKNOWN,
    OwnerResolution,
    norm,
)
from mozyo_bridge.core.state.lane_lifecycle_rows import _record
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    READER_UPGRADE_REQUIRED,
    READONLY_COMPONENT_ABSENT,
    READONLY_COMPONENT_RECOGNIZED,
    SCHEMA_MIGRATED,
    TABLE as _TABLE,
    LaneLifecycleError,
    LifecycleSchemaOutcome,
    lane_lifecycle_path,
    readonly_compatible_select,
    readonly_component_status,
    reader_upgrade_required,
)


class LaneLifecycleReaderUpgradeRequired(LaneLifecycleError):
    """The store is a NEWER schema than this build can read — route, don't downgrade.

    The typed :data:`READER_UPGRADE_REQUIRED` sub-case of :class:`LaneLifecycleError` (Redmine
    #13844 design 5): a concurrent newer-schema lane migrated the shared home store, so THIS
    reader is stale. A caller distinguishes this from a generic unreadable/corrupt store and
    routes the operation to the current compatible high-level facade (never a raw DB
    downgrade). Because it subclasses :class:`LaneLifecycleError`, every existing
    ``except LaneLifecycleError`` fail-closed path still catches it — only a caller that WANTS
    the distinction (the handoff gate) inspects the concrete type.
    """


class _ReadClosed(Exception):
    """Internal: the store cannot be read (unsupported / partial / malformed) — fail closed.

    Carries a human message and whether the specific cause is a NEWER schema than this build
    understands (``upgrade_required``), so a caller can route to the current facade instead of
    downgrading (Redmine #13844 item 5).
    """

    def __init__(self, message: str, *, upgrade_required: bool) -> None:
        super().__init__(message)
        self.upgrade_required = upgrade_required


def _open_and_project(path: Path) -> Optional[tuple[sqlite3.Connection, str]]:
    """Open the store read-only and return ``(conn, select_columns)``, or ``None`` if absent.

    - ``None`` — the store file, its lifecycle component, or its table is absent: a genuinely
      empty authority (no rows), never a read failure. The caller yields "no rows" / "no owner"
      and creates nothing.
    - ``(conn, select)`` — a recognized, KNOWN-shape store the caller may read with the
      version-compatible padded ``select`` (older additive columns filled with their default
      literal). The caller owns closing ``conn``.

    Raises :class:`_ReadClosed` (fail closed) for an unknown / newer / partial / malformed /
    unreadable store. It never migrates: only ``mode=ro`` + ``PRAGMA``/``sqlite_master`` reads.
    """
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        # Redmine #13844 F3: a read landing while a PEER lane runs an explicit backup-first
        # migration (which holds the write lock across its ``BEGIN IMMEDIATE`` section) must
        # WAIT for that commit, not fail-closed on a transient "database is locked". Without a
        # busy timeout a concurrent migration would spuriously break a read; with it the reader
        # briefly blocks, then reads the settled (pre- or post-migration) committed shape.
        conn.execute("PRAGMA busy_timeout = 2000")
    except sqlite3.DatabaseError as exc:
        raise _ReadClosed(
            f"lane lifecycle store {path} is unreadable ({type(exc).__name__})",
            upgrade_required=False,
        ) from exc
    try:
        status = readonly_component_status(conn)
        if status == READONLY_COMPONENT_ABSENT:
            conn.close()
            return None
        if status != READONLY_COMPONENT_RECOGNIZED:
            upgrade = reader_upgrade_required(conn)
            conn.close()
            raise _ReadClosed(
                f"lane lifecycle store {path} carries a schema this build cannot read"
                + (f" ({READER_UPGRADE_REQUIRED})" if upgrade else " (unsupported)"),
                upgrade_required=upgrade,
            )
        select = readonly_compatible_select(conn)
        if select is None:
            conn.close()
            raise _ReadClosed(
                f"lane lifecycle store {path} does not match a known schema signature "
                "(partial / corrupt authority shape)",
                upgrade_required=False,
            )
    except _ReadClosed:
        raise
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise _ReadClosed(
            f"lane lifecycle read failed ({type(exc).__name__})",
            upgrade_required=False,
        ) from exc
    return conn, select


class LaneLifecycleReader:
    """Read-only, version-compatible, NON-MIGRATING lifecycle access (Redmine #13844).

    A drop-in for the read half of :class:`...lane_lifecycle.LaneLifecycleStore`
    (:meth:`get` / :meth:`records` / :meth:`resolve_owner`), with the SAME fail-closed
    contract — it raises :class:`LaneLifecycleError` when the store is unreadable / unsupported
    — but it NEVER runs the schema ensure/migration. Status / handoff / review / callback /
    drain routing reads the authority through this so a newer source CLI does not migrate the
    shared home store out from under a concurrent older reader.

    An **absent** store (no file / no component / no table) reads as an empty authority — no
    row (:meth:`get` → ``None``), no rows (:meth:`records` → ``()``), no owner
    (:meth:`resolve_owner` → :data:`OWNER_ABSENT`) — matching what the migrating store would
    observe after creating an empty table, but without creating anything.
    """

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self.path = path if path is not None else lane_lifecycle_path(home)

    def _fail_closed(self, exc: _ReadClosed) -> LaneLifecycleError:
        # A NEWER-schema store fails closed with the TYPED subclass so the caller can route to
        # the current facade (Redmine #13844 design 5); every other cause (malformed / partial /
        # unreadable) stays the generic error. Both are LaneLifecycleError, so existing
        # fail-closed catches are unaffected.
        if exc.upgrade_required:
            return LaneLifecycleReaderUpgradeRequired(str(exc))
        return LaneLifecycleError(str(exc))

    def get(self, key: LaneLifecycleKey) -> Optional[LaneLifecycleRecord]:
        """The lane's row via a non-migrating compatible read, or ``None`` when it has none.

        Raises :class:`LaneLifecycleError` when the store cannot be read (fail closed) — never
        migrates, never assumes "no row" for an unreadable store.
        """
        try:
            opened = _open_and_project(self.path)
        except _ReadClosed as exc:
            raise self._fail_closed(exc) from exc
        if opened is None:
            return None
        conn, select = opened
        try:
            row = conn.execute(
                f"SELECT {select} FROM {_TABLE} "
                "WHERE repo_workspace_id = ? AND lane_id = ?",
                key.as_row(),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise LaneLifecycleError(
                f"lane lifecycle read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return _record(row) if row is not None else None

    def records(self) -> tuple[LaneLifecycleRecord, ...]:
        """Every row via a non-migrating compatible read. Raises when unreadable (fail closed)."""
        try:
            opened = _open_and_project(self.path)
        except _ReadClosed as exc:
            raise self._fail_closed(exc) from exc
        if opened is None:
            return ()
        conn, select = opened
        try:
            rows = conn.execute(
                f"SELECT {select} FROM {_TABLE} ORDER BY repo_workspace_id, lane_id"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise LaneLifecycleError(
                f"lane lifecycle read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return tuple(_record(row) for row in rows)

    def resolve_owner(self, repo_workspace_id: str, issue_id: str) -> OwnerResolution:
        """The issue's single active owning lane, via a non-migrating compatible read.

        Same resolution contract as the store's :meth:`resolve_owner`: exactly one active row
        resolves; zero (:data:`OWNER_ABSENT`), many (:data:`OWNER_AMBIGUOUS`), or an empty
        query resolves to no owner — a caller must not fall back to "the newest lane". An
        unreadable store raises :class:`LaneLifecycleError` (fail closed).
        """
        workspace = norm(repo_workspace_id)
        issue = norm(issue_id)
        if not workspace or not issue:
            return OwnerResolution(
                status=OWNER_ABSENT, detail="workspace or issue not supplied"
            )
        try:
            opened = _open_and_project(self.path)
        except _ReadClosed as exc:
            raise self._fail_closed(exc) from exc
        if opened is None:
            return OwnerResolution(status=OWNER_ABSENT, detail="no active owner")
        conn, _select = opened
        try:
            rows = conn.execute(
                f"SELECT lane_id FROM {_TABLE} WHERE repo_workspace_id = ? "
                "AND issue_id = ? AND lane_disposition = ?",
                (workspace, issue, DISPOSITION_ACTIVE),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise LaneLifecycleError(
                f"lane lifecycle read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        if not rows:
            return OwnerResolution(status=OWNER_ABSENT, detail="no active owner")
        if len(rows) > 1:
            return OwnerResolution(
                status=OWNER_AMBIGUOUS,
                detail=f"{len(rows)} active owners; the owner index is not holding",
            )
        return OwnerResolution(status=OWNER_RESOLVED, lane_id=str(rows[0][0]))


def load_lane_lifecycle_readonly(
    *, home: Path | None = None
) -> Optional[tuple[LaneLifecycleRecord, ...]]:
    """Every lifecycle row via a **non-creating, version-compatible** read (Redmine #13844).

    The read a read-only projection uses (``workflow glance --snapshot-json`` must not create
    ``state.sqlite`` just to fold a diagnostic). An absent store, or an existing store with no
    lifecycle component yet, yields ``()`` (no rows, nothing created). It honours the same
    downgrade guard as the write path — an unknown / newer / malformed / partial component
    schema yields ``None`` (fail closed) — but, unlike a bare ``SELECT`` of the current column
    set, it reads an OLDER known additive shape by padding the missing columns with their
    migration defaults, so a newer build reads an older shared store faithfully instead of
    failing on ``no such column`` (Redmine #13844 item 2).
    """
    try:
        return LaneLifecycleReader(home=home).records()
    except (LaneLifecycleError, OSError):
        return None


@dataclass(frozen=True)
class LifecycleMigrationPreflight:
    """The read-only compatibility preflight for a schema-changing WRITE (Redmine #13844 item 6).

    A forward migration of the shared home store is safe for the writer but fail-closes any
    concurrent OLDER-schema reader lane (its source CLI can no longer read the migrated
    authority). Before such a write an operator should see who would be affected — the OTHER
    active lanes sharing this home. This is computed read-only / version-compatibly (it never
    migrates to measure), and it cannot know each lane's reader version, so it reports the
    active peer lanes as *potentially* affected rather than asserting a version.
    """

    #: ``True`` when the store could not be read at all (fail closed) — treat as "cannot vouch
    #: for safety" rather than "no peers".
    unreadable: bool = False
    #: The recorded component schema version currently on the shared store (``None`` when the
    #: store is absent or unreadable).
    current_version: Optional[int] = None
    #: The active lane ids that share this home and are NOT the writer's own lane — each a
    #: potential older-schema reader a forward migration would fail-close.
    peer_active_lanes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_peers(self) -> bool:
        return bool(self.peer_active_lanes)


def lifecycle_migration_preflight(
    *,
    home: Path | None = None,
    path: Path | None = None,
    writer_workspace_id: Optional[str] = None,
    writer_lane_id: Optional[str] = None,
) -> LifecycleMigrationPreflight:
    """Which active peer lanes a schema-changing write would fail-close (Redmine #13844 item 6).

    Reads the shared authority read-only / version-compatibly and returns the active lanes
    other than ``(writer_workspace_id, writer_lane_id)``. An unreadable store yields
    ``unreadable=True`` (the caller must not treat "no peers" as "safe"). It performs no
    migration — the preflight for a migration must not itself trigger one. ``path`` addresses an
    explicit store file (what the write gate passes, from ``store.path``); ``home`` resolves the
    default location.
    """
    reader = LaneLifecycleReader(home=home, path=path)
    try:
        records = reader.records()
    except (LaneLifecycleError, OSError):
        return LifecycleMigrationPreflight(unreadable=True)
    writer_ws = norm(writer_workspace_id) if writer_workspace_id else ""
    writer_lane = norm(writer_lane_id) if writer_lane_id else ""
    peers = tuple(
        rec.lane_id
        for rec in records
        if rec.lane_disposition == DISPOSITION_ACTIVE
        and not (
            norm(rec.repo_workspace_id) == writer_ws and norm(rec.lane_id) == writer_lane
        )
    )
    version = _current_recorded_version(home=home, path=path)
    return LifecycleMigrationPreflight(
        current_version=version, peer_active_lanes=peers
    )


def _current_recorded_version(
    *, home: Path | None = None, path: Path | None = None
) -> Optional[int]:
    """The shared store's recorded component version, read-only (``None`` if absent/unreadable)."""
    path = path if path is not None else lane_lifecycle_path(home)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return None
    try:
        row = conn.execute(
            "SELECT typeof(schema_version), schema_version "
            "FROM state_schema_components WHERE component = 'lane_lifecycle'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    if row is None or row[0] != "integer" or not isinstance(row[1], int) or isinstance(
        row[1], bool
    ):
        return None
    return row[1]


@dataclass(frozen=True)
class LifecycleWritePreparation:
    """The typed result of the explicit schema-changing WRITE gate (Redmine #13844 design 3/6).

    A mutating use case that needs the current schema runs the explicit write gate
    (:meth:`...lane_lifecycle.LaneLifecycleStore.prepare_write`) BEFORE its CAS: it reads the
    peer compatibility preflight FIRST (on the pre-migration store), then runs the backup-first
    migration, and returns BOTH here so the migration is a visible, typed act — never an
    implicit side effect of opening the store. ``outcome`` is the
    :class:`LifecycleSchemaOutcome` (created / intact / migrated{from_version, backup_dir});
    ``preflight`` is the active peer lanes a forward migration would fail-close.
    """

    outcome: LifecycleSchemaOutcome
    preflight: LifecycleMigrationPreflight

    @property
    def migrated(self) -> bool:
        """This write actually forward-migrated the shared store (an authority-shape change)."""
        return self.outcome.action == SCHEMA_MIGRATED

    @property
    def peer_reader_risk(self) -> bool:
        """A migration happened AND active peer lanes may be older-schema readers of it."""
        return self.migrated and (
            self.preflight.unreadable or self.preflight.has_peers
        )


def format_lifecycle_migration_advisory(
    preparation: Optional[LifecycleWritePreparation],
) -> Optional[str]:
    """The operator advisory when a mutation forward-migrated the shared store with peers at risk.

    The single shared wording (Redmine #13844 R2) every schema-changing command surface uses so
    a forward migration is operator-visible — declaration/adopt AND disposition / supersede /
    release / retire / reconcile alike — not only the adopt path. Returns ``None`` when there was
    no migration or no peer at risk (nothing to warn about).
    """
    if preparation is None or not preparation.peer_reader_risk:
        return None
    peers = ", ".join(preparation.preflight.peer_active_lanes) or "(unreadable peer set)"
    return (
        "advisory (Redmine #13844): this operation forward-migrated the shared lifecycle "
        f"store {preparation.outcome.from_version} -> {preparation.outcome.to_version} "
        f"(backup {preparation.outcome.backup_dir}); active peer lanes that may run an older-"
        f"schema source CLI and now read-fail-closed: {peers}. Re-run those lanes' reads from "
        "the current facade; do not downgrade the store."
    )


def emit_lifecycle_migration_advisory(
    preparation: Optional[LifecycleWritePreparation],
    *,
    stream=None,
) -> bool:
    """Print the peer-reader-risk advisory (if any) to ``stream`` (default stderr). Returns whether
    an advisory was emitted, so a caller can also thread it into a structured outcome if wanted."""
    message = format_lifecycle_migration_advisory(preparation)
    if message is None:
        return False
    import sys

    print(message, file=stream if stream is not None else sys.stderr)
    return True


__all__ = (
    "LaneLifecycleReader",
    "LaneLifecycleReaderUpgradeRequired",
    "LifecycleMigrationPreflight",
    "LifecycleWritePreparation",
    "emit_lifecycle_migration_advisory",
    "format_lifecycle_migration_advisory",
    "lifecycle_migration_preflight",
    "load_lane_lifecycle_readonly",
)
