"""Home-scoped desired-presentation current-table store (Redmine #12304).

Implements the runtime follow-up the schema boundary in
``vibes/docs/logics/unit-presentation-state-db.md`` deferred to a later task: a
home-scoped SQLite DB that holds the *desired presentation current state*
(``cockpit_group_membership`` + ``projection_preferences``) and a small seed /
migration path from the static repo-local ``.mozyo-bridge/config.yaml``
presentation block into those tables.

What this store **is** (the doc's "presentation state DB"):

- ``cockpit_group_membership`` — which Project Group a Unit is *desired* to be
  displayed under, plus display ``position`` / ``pinned`` / ``hidden``.
- ``projection_preferences`` — a Unit's *preferred* projection
  (``cockpit_pane`` / ``normal_window`` / …).
- ``presentation_seed_provenance`` — which static config version last seeded the
  tables, so a migration is auditable. (#12304 acceptance: "record source config
  version".)

What this store is **not**, kept enforced by the schema + the seed contract:

- It is **not** a workspace identity store. ``registry.sqlite`` stays the only
  workspace identity source; this DB never invents ``workspace_id`` / canonical
  session.
- It is **not** liveness, a routing target, or a handoff/approval/close
  authority. There is deliberately no pane / session / route / approval / review
  / close column anywhere here, and the seed refuses to write one. Action
  permission stays an action-time live preflight; workflow completion stays
  Redmine-only.
- It does **not** derive durable membership from live tmux geometry. The seed
  reads only explicit, public-safe ``unit_overrides`` declared in config; it
  never reads a pane tree to decide membership (the doc's "live geometry
  boundary").

Boundaries that make the migration safe (#12304 受入条件):

- **Idempotent.** Seeding is a *content-comparing* upsert: a row whose desired
  content already matches is left byte-for-byte untouched (its ``updated_at`` is
  not even rewritten), so re-running the same seed is a true no-op.
- **Non-destructive.** The seed only inserts / updates; it *never* deletes. A
  membership the operator pinned by hand, or a row whose config override was
  later removed, survives a re-seed. No destructive auto-reconcile here — that
  stays a future preview/confirm command (the doc's live-geometry reconcile).
- **Schema mismatch fails closed, never silently wipes.** Unlike the regenerable
  ``inventory.sqlite`` cache, this is operator-managed *desired* state, so an
  unrecognized ``user_version`` raises :class:`PresentationStateError` rather
  than dropping the tables.

Read-model display policy (#12304: "rebuild / stale / desired-but-missing 表示
方針"): :func:`classify_membership` folds the desired rows against an observed
set into ``present`` / ``stale`` / ``desired_but_missing`` *display* statuses. It
is a pure projection — it resolves no routing and decides no side effect; a
``desired_but_missing`` row is *shown*, never silently dropped or rerouted, and
a stale observation never reads as present.

Conventions mirror the sibling home-scoped stores
(:mod:`mozyo_bridge.workspace_registry`, :mod:`mozyo_bridge.managed_events`,
:mod:`mozyo_bridge.session_inventory`): a ``*_FILENAME`` constant, a
``*_path(home=None)`` helper resolving through
:func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a ``PRAGMA user_version``
schema guard, frozen ``*Row`` dataclasses with ``as_payload()``, and ISO-second
UTC timestamps.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

if TYPE_CHECKING:  # avoid importing the domain config at runtime; only typed here
    from mozyo_bridge.domain.presentation_grouping import PresentationGroupingConfig

#: The home-scoped SQLite file holding desired presentation current state. A
#: separate DB from ``registry.sqlite`` (identity) and ``inventory.sqlite``
#: (cache), exactly as the schema-boundary doc requires.
PRESENTATION_STATE_FILENAME = "presentation.sqlite"

#: Schema version stamped into ``PRAGMA user_version``. Bump only with a
#: migration; an unrecognized version fails closed (operator-managed desired
#: state is never silently dropped).
PRESENTATION_STATE_SCHEMA_VERSION = 1

#: The provenance ``source`` recorded for a seed from the repo-local config file.
SOURCE_REPO_LOCAL_CONFIG = "repo_local_config"

#: Read-model *display* statuses for a desired membership row folded against the
#: observed set (:func:`classify_membership`). Display-only — never routing.
STATUS_PRESENT = "present"
STATUS_STALE = "stale"
STATUS_DESIRED_BUT_MISSING = "desired_but_missing"

_MEMBERSHIP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cockpit_group_membership (
    group_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    position INTEGER,
    width_weight REAL,
    pinned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    source_event_id INTEGER,
    PRIMARY KEY (group_id, unit_id)
)
"""

_MEMBERSHIP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_cockpit_group_position
  ON cockpit_group_membership(group_id, position)
"""

_PROJECTION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS projection_preferences (
    unit_id TEXT PRIMARY KEY,
    preferred_projection TEXT NOT NULL,
    fallback_projection TEXT,
    updated_at TEXT NOT NULL,
    source_event_id INTEGER
)
"""

_PROVENANCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS presentation_seed_provenance (
    source TEXT PRIMARY KEY,
    source_config_version INTEGER NOT NULL,
    grouping_version INTEGER,
    seeded_at TEXT NOT NULL,
    membership_rows INTEGER NOT NULL DEFAULT 0,
    projection_rows INTEGER NOT NULL DEFAULT 0,
    note TEXT
)
"""


class PresentationStateError(RuntimeError):
    """The presentation-state DB could not be opened at the expected schema.

    Raised for a structural / version problem the store must not paper over — an
    unrecognized ``user_version`` most importantly. Desired presentation state is
    operator-managed, so the store fails closed rather than dropping tables the
    way the regenerable inventory cache does.
    """


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (sibling-store convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def presentation_state_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``presentation.sqlite`` path under the mozyo-bridge home.

    ``home`` overrides the root (tests pass a temp dir); otherwise the shared
    :func:`mozyo_bridge.shared.paths.mozyo_bridge_home` resolves
    ``MOZYO_BRIDGE_HOME`` / ``~/.mozyo_bridge``.
    """
    return (home or mozyo_bridge_home()) / PRESENTATION_STATE_FILENAME


def unit_id_for(
    workspace_id: str, lane_id: str = "default", host_id: str = "local"
) -> str:
    """Derive the stable, public-safe Unit key from its identity facts.

    A Unit is identified by ``(host_id, workspace_id, lane_id)`` (the doc's
    ``unit_desired_state`` unique identity). This composes them into a single
    portable ``unit_id`` join key so the membership / projection tables can be
    keyed without copying any private path or live pane id. ``workspace_id`` is
    the registry identity (public-safe by contract); ``host_id`` defaults to the
    local host. The derivation is deterministic, so re-deriving the same identity
    always yields the same key — the basis of the seed's idempotency.
    """
    return f"{host_id}:{workspace_id}:{lane_id}"


@dataclass(frozen=True)
class GroupMembershipRow:
    """A desired cockpit-group membership row (display-only)."""

    group_id: str
    unit_id: str
    position: Optional[int] = None
    width_weight: Optional[float] = None
    pinned: bool = False
    hidden: bool = False
    updated_at: Optional[str] = None
    source_event_id: Optional[int] = None

    def as_payload(self) -> dict:
        return {
            "group_id": self.group_id,
            "unit_id": self.unit_id,
            "position": self.position,
            "width_weight": self.width_weight,
            "pinned": self.pinned,
            "hidden": self.hidden,
            "updated_at": self.updated_at,
            "source_event_id": self.source_event_id,
        }

    def _content_key(self) -> tuple:
        """The desired content, excluding ``updated_at`` / identity.

        Two rows with the same identity and the same content key are
        semantically equal, so the seed can skip rewriting ``updated_at`` — the
        basis of true (timestamp-stable) idempotency.
        """
        return (
            self.position,
            self.width_weight,
            bool(self.pinned),
            bool(self.hidden),
            self.source_event_id,
        )


@dataclass(frozen=True)
class ProjectionPreferenceRow:
    """A desired projection preference for one Unit (display-only)."""

    unit_id: str
    preferred_projection: str
    fallback_projection: Optional[str] = None
    updated_at: Optional[str] = None
    source_event_id: Optional[int] = None

    def as_payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "preferred_projection": self.preferred_projection,
            "fallback_projection": self.fallback_projection,
            "updated_at": self.updated_at,
            "source_event_id": self.source_event_id,
        }

    def _content_key(self) -> tuple:
        return (
            self.preferred_projection,
            self.fallback_projection,
            self.source_event_id,
        )


@dataclass(frozen=True)
class SeedProvenance:
    """The last config seed recorded into the presentation-state DB."""

    source: str
    source_config_version: int
    grouping_version: Optional[int]
    seeded_at: str
    membership_rows: int
    projection_rows: int
    note: Optional[str] = None

    def as_payload(self) -> dict:
        return {
            "source": self.source,
            "source_config_version": self.source_config_version,
            "grouping_version": self.grouping_version,
            "seeded_at": self.seeded_at,
            "membership_rows": self.membership_rows,
            "projection_rows": self.projection_rows,
            "note": self.note,
        }


@dataclass(frozen=True)
class SeedResult:
    """The outcome of a config -> current-table seed (idempotency-aware)."""

    source: str
    source_config_version: int
    membership_inserted: int = 0
    membership_updated: int = 0
    membership_unchanged: int = 0
    projection_inserted: int = 0
    projection_updated: int = 0
    projection_unchanged: int = 0
    skipped_overrides: int = 0

    @property
    def changed(self) -> int:
        """Rows actually written (insert + content update). 0 means a no-op."""
        return (
            self.membership_inserted
            + self.membership_updated
            + self.projection_inserted
            + self.projection_updated
        )

    def as_payload(self) -> dict:
        return {
            "source": self.source,
            "source_config_version": self.source_config_version,
            "membership_inserted": self.membership_inserted,
            "membership_updated": self.membership_updated,
            "membership_unchanged": self.membership_unchanged,
            "projection_inserted": self.projection_inserted,
            "projection_updated": self.projection_updated,
            "projection_unchanged": self.projection_unchanged,
            "skipped_overrides": self.skipped_overrides,
            "changed": self.changed,
        }


@dataclass(frozen=True)
class MembershipProjection:
    """A desired membership row folded against the observed set (display-only)."""

    row: GroupMembershipRow
    status: str
    observed_at: Optional[str] = None

    def as_payload(self) -> dict:
        return {
            "membership": self.row.as_payload(),
            "status": self.status,
            "observed_at": self.observed_at,
        }


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_membership(
    rows: "tuple[GroupMembershipRow, ...]",
    observed: "Mapping[str, Optional[str]]",
    *,
    now: Optional[str] = None,
    stale_after_seconds: Optional[float] = None,
) -> "tuple[MembershipProjection, ...]":
    """Fold desired membership rows against an observed set into display statuses.

    ``observed`` maps an observed ``unit_id`` to the ISO timestamp it was last
    observed at (``None`` when the freshness is unknown). The pure projection
    pinned by #12304's acceptance:

    - a desired row whose Unit is **not** in ``observed`` is
      ``desired_but_missing`` — it is *shown* in the read model, never silently
      dropped or rerouted (the doc's "current table と live tmux が矛盾した場合
      … desired but missing として表示する");
    - a desired row whose observation is older than ``stale_after_seconds`` (when
      both a threshold and an ``observed_at`` are given) is ``stale`` — an aged
      observation never reads as present;
    - otherwise the row is ``present``.

    This resolves no routing and decides no side effect: handoff / action
    permission keeps doing its own live preflight regardless of any status here.
    The output order follows ``rows``.
    """
    now_dt = _parse_iso(now) or datetime.now(timezone.utc)
    projections: list[MembershipProjection] = []
    for row in rows:
        if row.unit_id not in observed:
            projections.append(
                MembershipProjection(
                    row=row, status=STATUS_DESIRED_BUT_MISSING, observed_at=None
                )
            )
            continue
        observed_at = observed[row.unit_id]
        status = STATUS_PRESENT
        if stale_after_seconds is not None:
            observed_dt = _parse_iso(observed_at)
            if observed_dt is None or (
                (now_dt - observed_dt).total_seconds() > stale_after_seconds
            ):
                status = STATUS_STALE
        projections.append(
            MembershipProjection(row=row, status=status, observed_at=observed_at)
        )
    return tuple(projections)


class PresentationStateStore:
    """Read/write access to the home-scoped desired-presentation current tables.

    The store opens (and, on first use, creates) ``presentation.sqlite`` lazily.
    Writes go through a read-write connection guarded by ``PRAGMA user_version``;
    reads use a read-only URI connection so a query can never create or migrate
    the file. All write methods are content-comparing upserts (idempotent) and
    never delete (non-destructive).
    """

    def __init__(
        self, path: Optional[Path] = None, *, home: Optional[Path] = None
    ) -> None:
        self.path = Path(path) if path is not None else presentation_state_path(home)

    # -- connections -------------------------------------------------------

    def _connect_rw(self) -> sqlite3.Connection:
        """Open a read-write connection, creating / validating the schema.

        Mirrors the sibling stores: ``PRAGMA user_version`` is the migration
        guard. Version ``0`` is a fresh file — create the tables and stamp the
        current version. Any other unrecognized version fails closed via
        :class:`PresentationStateError` rather than dropping operator-managed
        desired state.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute(_MEMBERSHIP_TABLE_SQL)
            conn.execute(_MEMBERSHIP_INDEX_SQL)
            conn.execute(_PROJECTION_TABLE_SQL)
            conn.execute(_PROVENANCE_TABLE_SQL)
            conn.execute(
                f"PRAGMA user_version = {PRESENTATION_STATE_SCHEMA_VERSION}"
            )
            conn.commit()
        elif version != PRESENTATION_STATE_SCHEMA_VERSION:
            conn.close()
            raise PresentationStateError(
                f"presentation-state DB {self.path} has unsupported schema "
                f"version {version}; this build understands "
                f"{PRESENTATION_STATE_SCHEMA_VERSION}. Desired presentation state "
                f"is operator-managed and is not auto-dropped; migrate or move "
                f"the file aside deliberately."
            )
        return conn

    def _read_rows(self, sql: str, params: tuple = ()) -> "list[tuple]":
        """Run a read-only query; return ``[]`` when the file does not exist yet."""
        if not self.path.exists():
            return []
        uri = f"file:{self.path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return list(conn.execute(sql, params).fetchall())
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()

    # -- reads -------------------------------------------------------------

    def list_group_membership(self) -> "tuple[GroupMembershipRow, ...]":
        rows = self._read_rows(
            "SELECT group_id, unit_id, position, width_weight, pinned, hidden, "
            "updated_at, source_event_id FROM cockpit_group_membership "
            "ORDER BY group_id, position IS NULL, position, unit_id"
        )
        return tuple(
            GroupMembershipRow(
                group_id=r[0],
                unit_id=r[1],
                position=r[2],
                width_weight=r[3],
                pinned=bool(r[4]),
                hidden=bool(r[5]),
                updated_at=r[6],
                source_event_id=r[7],
            )
            for r in rows
        )

    def list_projection_preferences(self) -> "tuple[ProjectionPreferenceRow, ...]":
        rows = self._read_rows(
            "SELECT unit_id, preferred_projection, fallback_projection, "
            "updated_at, source_event_id FROM projection_preferences "
            "ORDER BY unit_id"
        )
        return tuple(
            ProjectionPreferenceRow(
                unit_id=r[0],
                preferred_projection=r[1],
                fallback_projection=r[2],
                updated_at=r[3],
                source_event_id=r[4],
            )
            for r in rows
        )

    def get_provenance(
        self, source: str = SOURCE_REPO_LOCAL_CONFIG
    ) -> Optional[SeedProvenance]:
        rows = self._read_rows(
            "SELECT source, source_config_version, grouping_version, seeded_at, "
            "membership_rows, projection_rows, note "
            "FROM presentation_seed_provenance WHERE source = ?",
            (source,),
        )
        if not rows:
            return None
        r = rows[0]
        return SeedProvenance(
            source=r[0],
            source_config_version=r[1],
            grouping_version=r[2],
            seeded_at=r[3],
            membership_rows=r[4],
            projection_rows=r[5],
            note=r[6],
        )

    # -- writes (content-comparing upserts; never delete) -----------------

    def _upsert_membership(
        self, conn: sqlite3.Connection, row: GroupMembershipRow, *, now: str
    ) -> str:
        """Insert/update one membership row; return ``inserted``/``updated``/``unchanged``."""
        existing = conn.execute(
            "SELECT position, width_weight, pinned, hidden, source_event_id "
            "FROM cockpit_group_membership WHERE group_id = ? AND unit_id = ?",
            (row.group_id, row.unit_id),
        ).fetchone()
        if existing is not None:
            existing_key = (
                existing[0],
                existing[1],
                bool(existing[2]),
                bool(existing[3]),
                existing[4],
            )
            if existing_key == row._content_key():
                return "unchanged"
            conn.execute(
                "UPDATE cockpit_group_membership SET position = ?, width_weight = ?, "
                "pinned = ?, hidden = ?, updated_at = ?, source_event_id = ? "
                "WHERE group_id = ? AND unit_id = ?",
                (
                    row.position,
                    row.width_weight,
                    int(row.pinned),
                    int(row.hidden),
                    now,
                    row.source_event_id,
                    row.group_id,
                    row.unit_id,
                ),
            )
            return "updated"
        conn.execute(
            "INSERT INTO cockpit_group_membership (group_id, unit_id, position, "
            "width_weight, pinned, hidden, updated_at, source_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.group_id,
                row.unit_id,
                row.position,
                row.width_weight,
                int(row.pinned),
                int(row.hidden),
                now,
                row.source_event_id,
            ),
        )
        return "inserted"

    def _upsert_projection(
        self, conn: sqlite3.Connection, row: ProjectionPreferenceRow, *, now: str
    ) -> str:
        existing = conn.execute(
            "SELECT preferred_projection, fallback_projection, source_event_id "
            "FROM projection_preferences WHERE unit_id = ?",
            (row.unit_id,),
        ).fetchone()
        if existing is not None:
            if (existing[0], existing[1], existing[2]) == row._content_key():
                return "unchanged"
            conn.execute(
                "UPDATE projection_preferences SET preferred_projection = ?, "
                "fallback_projection = ?, updated_at = ?, source_event_id = ? "
                "WHERE unit_id = ?",
                (
                    row.preferred_projection,
                    row.fallback_projection,
                    now,
                    row.source_event_id,
                    row.unit_id,
                ),
            )
            return "updated"
        conn.execute(
            "INSERT INTO projection_preferences (unit_id, preferred_projection, "
            "fallback_projection, updated_at, source_event_id) VALUES (?, ?, ?, ?, ?)",
            (
                row.unit_id,
                row.preferred_projection,
                row.fallback_projection,
                now,
                row.source_event_id,
            ),
        )
        return "inserted"

    def _record_provenance(
        self, conn: sqlite3.Connection, provenance: SeedProvenance
    ) -> None:
        """Upsert the single provenance row for ``provenance.source``."""
        conn.execute(
            "INSERT INTO presentation_seed_provenance (source, source_config_version, "
            "grouping_version, seeded_at, membership_rows, projection_rows, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "source_config_version = excluded.source_config_version, "
            "grouping_version = excluded.grouping_version, "
            "seeded_at = excluded.seeded_at, "
            "membership_rows = excluded.membership_rows, "
            "projection_rows = excluded.projection_rows, "
            "note = excluded.note",
            (
                provenance.source,
                provenance.source_config_version,
                provenance.grouping_version,
                provenance.seeded_at,
                provenance.membership_rows,
                provenance.projection_rows,
                provenance.note,
            ),
        )

    def seed_from_grouping_config(
        self,
        config: "PresentationGroupingConfig",
        *,
        source_config_version: int,
        grouping_version: Optional[int] = None,
        source: str = SOURCE_REPO_LOCAL_CONFIG,
        host_id: str = "local",
        now: Optional[str] = None,
        dry_run: bool = False,
    ) -> SeedResult:
        """Idempotently, non-destructively seed current tables from static config.

        Reads **only** the config's explicit, public-safe ``unit_overrides`` —
        each names a concrete ``(workspace_id, lane_id)`` Unit, so seeding them
        never depends on a live observation or a pane tree (the doc's live
        geometry boundary). ``membership_rules`` are *deliberately not* seeded:
        a rule derives a group from launch-time facts and is evaluated at launch
        by :func:`~mozyo_bridge.domain.presentation_grouping.resolve_launch_placement`,
        not frozen into durable membership here.

        For each override:

        - ``preferred_group`` (+ ``position`` / ``pinned`` / ``hidden``) upserts a
          :class:`GroupMembershipRow`;
        - ``preferred_projection`` upserts a :class:`ProjectionPreferenceRow`.

        An override with neither is counted in ``skipped_overrides`` (nothing to
        seed). Every write is a content-comparing upsert, so a re-seed of an
        unchanged config writes nothing (no ``updated_at`` churn). Nothing is ever
        deleted: a row whose override was later removed, or one the operator added
        by hand, survives. Provenance (the source config version) is recorded only
        when something changed or no provenance exists yet, so a true no-op re-seed
        leaves the whole DB byte-identical.

        The handoff / approval / close / routing / pane authorities never appear:
        ``UnitOverride`` carries no such field, and the schema has no column for
        one, so this migration can never turn display preference into send /
        approval / liveness truth.

        ``dry_run`` computes the same :class:`SeedResult` but rolls the data
        writes back, so an operator can preview a migration (and its idempotency)
        without changing any row. (An absent DB file is still initialized with the
        empty schema; only the row writes are rolled back.)
        """
        stamp = now or _utc_now()
        result_counts = {
            "membership_inserted": 0,
            "membership_updated": 0,
            "membership_unchanged": 0,
            "projection_inserted": 0,
            "projection_updated": 0,
            "projection_unchanged": 0,
            "skipped_overrides": 0,
        }
        membership_total = 0
        projection_total = 0

        conn = self._connect_rw()
        try:
            for override in config.unit_overrides:
                seeded_anything = False
                unit_id = unit_id_for(
                    override.workspace_id,
                    override.lane_id,
                    override.host_id or host_id,
                )
                if override.preferred_group is not None:
                    membership_total += 1
                    seeded_anything = True
                    outcome = self._upsert_membership(
                        conn,
                        GroupMembershipRow(
                            group_id=override.preferred_group,
                            unit_id=unit_id,
                            position=override.position,
                            pinned=bool(override.pinned),
                            hidden=bool(override.hidden),
                        ),
                        now=stamp,
                    )
                    result_counts[f"membership_{outcome}"] += 1
                if override.preferred_projection is not None:
                    projection_total += 1
                    seeded_anything = True
                    outcome = self._upsert_projection(
                        conn,
                        ProjectionPreferenceRow(
                            unit_id=unit_id,
                            preferred_projection=override.preferred_projection,
                        ),
                        now=stamp,
                    )
                    result_counts[f"projection_{outcome}"] += 1
                if not seeded_anything:
                    result_counts["skipped_overrides"] += 1

            result = SeedResult(
                source=source,
                source_config_version=source_config_version,
                **result_counts,
            )
            existing_provenance = conn.execute(
                "SELECT 1 FROM presentation_seed_provenance WHERE source = ?",
                (source,),
            ).fetchone()
            if result.changed > 0 or existing_provenance is None:
                self._record_provenance(
                    conn,
                    SeedProvenance(
                        source=source,
                        source_config_version=source_config_version,
                        grouping_version=grouping_version,
                        seeded_at=stamp,
                        membership_rows=membership_total,
                        projection_rows=projection_total,
                    ),
                )
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        finally:
            conn.close()
        return result


__all__ = (
    "PRESENTATION_STATE_FILENAME",
    "PRESENTATION_STATE_SCHEMA_VERSION",
    "SOURCE_REPO_LOCAL_CONFIG",
    "STATUS_PRESENT",
    "STATUS_STALE",
    "STATUS_DESIRED_BUT_MISSING",
    "PresentationStateError",
    "presentation_state_path",
    "unit_id_for",
    "GroupMembershipRow",
    "ProjectionPreferenceRow",
    "SeedProvenance",
    "SeedResult",
    "MembershipProjection",
    "classify_membership",
    "PresentationStateStore",
)
