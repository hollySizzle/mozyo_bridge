"""Local durability under the stall watcher's escalation gate (Redmine #15855).

**This store is not the durable escalation record.** #15855 j#110121 settled that the
workflow truth for a stall escalation is a Redmine ``## Gate: blocked`` journal written
through the canonical gate writer; what lives here is only what a Redmine journal cannot
hold between ticks:

- ``stall_watch_streak`` — one row per **slot**, the live run of same-class observations.
  #15843's watcher is stateless by design, so the run length has to survive the gap
  between passes; nothing else about it is durable-record material.
- ``stall_escalation_pending`` — threshold events that have not yet been written to
  Redmine. A pass may write at most one external mutation and callback delivery holds
  first priority (#14219 T3 / j#87188), so an escalation that fires on a busy pass has to
  wait somewhere. It waits here, visibly, until a pass writes it.

The consequence worth stating plainly: **a row here with no ``journal_id`` is a stall that
the durable record does not yet know about.** That is a real, recoverable state, not a
failure — and it is a column rather than an inference precisely so a status surface can
show how old the oldest one is (the starvation-visibility requirement of j#110121-6).

Identity, not locators
----------------------
The streak primary key is ``(workspace_id, lane_id, role)`` — the durable slot — and the
terminal ``generation`` is a stored, *bound* column rather than part of the key. Keying on
generation would strand a row per relaunch; binding it lets the policy restart a run when
the process behind the slot is replaced. The pane ``target`` is stored for evidence and is
**not** part of any key: herdr locators are recycled and rebound, so a locator-keyed run
either counts against a stranger or restarts for a unit that never moved.

Content hygiene is inherited, not re-decided (``stall-watcher-screen-diff.md``
`## 出力の hygiene`): **no column here holds pane text.** Every stored value is a fixed
classification token, an identity, a count, or a timestamp, which is what makes a row safe
to render verbatim into a Redmine journal.

Store discipline mirrors the sibling home-scoped stores (``supervisor_wake`` /
``supervisor_lease``): construction touches no filesystem, reads on an absent DB are empty
(the normal pre-write state), writes go through ``BEGIN IMMEDIATE``, and an unrecognized
``PRAGMA user_version`` **fails closed** rather than rewriting a table a newer build owns.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.stall_discovery_contract import (
    DISCOVERY_BAD_COUNT,
    DISCOVERY_BAD_REASON_TOKEN,
    DISCOVERY_BAD_TIMESTAMP,
    DISCOVERY_DROP_REASONS,
    DISCOVERY_FOREIGN_REASON,
    DISCOVERY_INCONSISTENT,
    DISCOVERY_MALFORMED,
    DISCOVERY_UNREADABLE,
    TIMESTAMP_MAX_LENGTH,
    TIMESTAMP_UNREADABLE,
    StallDiscoveryContractError,
    checked_timestamp,
    discovery_reject_token,
    timestamp_sort_key,
    rendered_timestamp,
    unreadable_discovery,
    validate_discovery,
)
from mozyo_bridge.core.state.stall_pending_contract import (
    PENDING_OK,
    ROW_SEAL_FIELDS,
    StallPendingContractError,
    UNCLASSIFIED_REASON,
    canonical_journal_id,
    canonical_numeric_id_sql,
    checked_reason,
    escalation_idempotency_key,
    pending_row_integrity,
    pending_row_seal,
    pending_telemetry,
    row_seal_for,
    validate_pending_fields,
)
from mozyo_bridge.core.state.stall_pending_transition import (
    apply_sealed_transition,
    plan_attempt,
    plan_recorded,
    plan_woken,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-scoped SQLite file holding stall streaks and pending escalations.
STALL_ESCALATION_FILENAME = "stall-escalation.sqlite"

#: Schema version stamped into ``PRAGMA user_version``. Unrecognized -> fail closed.
#:
#: v2 adds ``stall_escalation_pending.row_seal``. v1 stays RECOGNIZED and is migrated in
#: place on the first read-write connection: a v1 row simply has no seal, so it reads as a
#: state-binding mismatch and is quarantined — preserved and operator-visible, never
#: actuated. That is the same disposition every other pre-contract row gets, and it is why
#: the migration does not need to invent seals for rows whose history it cannot verify.
STALL_ESCALATION_SCHEMA_VERSION = 2

_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1, 2})

_STREAK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stall_watch_streak (
    workspace_id      TEXT NOT NULL,
    lane_id           TEXT NOT NULL,
    role              TEXT NOT NULL,
    generation        TEXT NOT NULL DEFAULT '',
    target            TEXT NOT NULL DEFAULT '',
    stall_class       TEXT NOT NULL,
    consecutive       INTEGER NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at  TEXT NOT NULL,
    escalated_at      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, lane_id, role)
)
"""

_PENDING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stall_escalation_pending (
    idempotency_key   TEXT NOT NULL PRIMARY KEY,
    workspace_id      TEXT NOT NULL,
    lane_id           TEXT NOT NULL,
    role              TEXT NOT NULL,
    generation        TEXT NOT NULL DEFAULT '',
    target            TEXT NOT NULL DEFAULT '',
    issue             TEXT NOT NULL DEFAULT '',
    stall_class       TEXT NOT NULL,
    prescription      TEXT NOT NULL,
    matched_id        TEXT NOT NULL DEFAULT '',
    evidence_tier     TEXT NOT NULL DEFAULT '',
    consecutive       INTEGER NOT NULL,
    first_observed_at TEXT NOT NULL,
    escalated_at      TEXT NOT NULL,
    journal_id        TEXT NOT NULL DEFAULT '',
    written_at        TEXT NOT NULL DEFAULT '',
    woke_at           TEXT NOT NULL DEFAULT '',
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT NOT NULL DEFAULT '',
    last_reason       TEXT NOT NULL DEFAULT '',
    row_seal          TEXT NOT NULL DEFAULT ''
)
"""


#: The last discovery pass's COUNTS (never identities). An operator asking "is the watcher
#: covering my cockpit" must be answerable at any moment, not only just after a sweep — and
#: `--status` must not answer it by reading panes, which would turn a read-only status
#: command into a screen reader. So the leg persists its coverage summary and status projects
#: it with the instant it was taken (review j#110146 finding_1).
#:
#: ``dropped`` is a JSON object of fixed reason token -> count. Both halves are closed
#: vocabulary, so the row stays as safe to render as every other column here.
_DISCOVERY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stall_watch_discovery (
    workspace_id TEXT NOT NULL PRIMARY KEY,
    observed_at  TEXT NOT NULL,
    candidates   INTEGER NOT NULL DEFAULT 0,
    watched      INTEGER NOT NULL DEFAULT 0,
    out_of_reach INTEGER NOT NULL DEFAULT 0,
    dropped      TEXT NOT NULL DEFAULT '{}'
)
"""

_WATERMARK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stall_watch_watermark (
    workspace_id TEXT NOT NULL PRIMARY KEY,
    last_pass_at TEXT NOT NULL
)
"""


class StallEscalationStoreError(RuntimeError):
    """The stall-escalation DB could not be opened at a recognized schema (fail-closed)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stall_escalation_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``stall-escalation.sqlite`` path under the mozyo-bridge home."""
    return (home or mozyo_bridge_home()) / STALL_ESCALATION_FILENAME




def _writable_only(rows) -> tuple:
    """Keep only rows that may drive an external effect."""
    return tuple(row for row in rows if row.externally_writable)


def _safe_reason(reason: object) -> str:
    """A declared reason token, or :data:`UNCLASSIFIED_REASON` when it is not one."""
    try:
        return checked_reason(reason)
    except StallPendingContractError:
        return UNCLASSIFIED_REASON


@dataclass(frozen=True)
class StreakRow:
    """The stored projection of one slot's run.

    Structurally parallel to the policy layer's ``StreakState`` and deliberately a
    *separate* type: the store must not import the policy module (a state store that could
    reach the rules would invite a rule to be written here), and the policy module must
    stay free of anything persistence-shaped. The application layer converts between them,
    which is the one place the mapping is visible and testable.
    """

    workspace_id: str
    lane_id: str
    role: str
    stall_class: str
    consecutive: int
    first_observed_at: str
    last_observed_at: str
    generation: str = ""
    target: str = ""
    escalated_at: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.workspace_id, self.lane_id, self.role)


@dataclass(frozen=True)
class PendingEscalation:
    """One threshold firing, and how far it has got toward the durable record.

    Three states, distinguished by columns rather than inferred:

    - ``journal_id`` empty — fired, not yet written to Redmine. The durable record does not
      know about this stall.
    - ``journal_id`` set, ``woke_at`` empty — written and read back; the coordinator has
      not yet been woken to read it.
    - both set — settled.

    ``attempts`` / ``last_reason`` exist so a repeatedly-refused write (a credential gap, a
    closed issue) is visible as a refusal with a reason instead of looking like an
    escalation nobody has got to yet.
    """

    idempotency_key: str
    workspace_id: str
    lane_id: str
    role: str
    stall_class: str
    prescription: str
    consecutive: int
    first_observed_at: str
    escalated_at: str
    generation: str = ""
    target: str = ""
    issue: str = ""
    matched_id: str = ""
    evidence_tier: str = ""
    journal_id: str = ""
    written_at: str = ""
    woke_at: str = ""
    attempts: int = 0
    last_attempt_at: str = ""
    last_reason: str = ""
    #: Tamper evidence over EVERY column the idempotency key does not already seal, derived
    #: by the store at each transition and never accepted from a caller. It is what makes a
    #: fully-forged SETTLED row visible at all (review j#110254 finding_stateauthority).
    #: NOT an existence proof — that is :func:`admit_wake`, which asks Redmine.
    row_seal: str = ""
    #: Whether this stored row still satisfies the closed contract. ``PENDING_OK`` rows are
    #: the only ones an external writer or a wake may ever see; anything else is preserved
    #: (the escalation really happened) but is never actuated.
    integrity: str = PENDING_OK

    @property
    def externally_writable(self) -> bool:
        return self.integrity == PENDING_OK

    @property
    def slot_label(self) -> str:
        return f"{self.workspace_id}/{self.lane_id}/{self.role}"

    @property
    def recorded(self) -> bool:
        """Whether the durable record (the Redmine journal) exists for this firing.

        A journal id that is not a canonical id is not a journal. ``bool(...)`` alone made
        ``journal_id='not-a-journal'`` read as recorded, which let a wake settle a firing
        against a journal nobody read back (review j#110218).
        """
        return canonical_journal_id(self.journal_id)

    @property
    def settled(self) -> bool:
        return self.recorded and bool(self.woke_at)

    def telemetry(self) -> dict[str, object]:
        """Classification-token-only projection, safe to paste into a durable record.

        Delegated to the contract module: "how a row renders" is the same concern as "what
        a valid row is", and keeping the two together is what stops a field from acquiring
        a checker on the way IN without acquiring one on the way OUT.
        """
        return pending_telemetry(self)


_STREAK_COLUMNS = (
    "workspace_id, lane_id, role, generation, target, stall_class, consecutive, "
    "first_observed_at, last_observed_at, escalated_at"
)

_PENDING_COLUMNS = (
    "idempotency_key, workspace_id, lane_id, role, generation, target, issue, "
    "stall_class, prescription, matched_id, evidence_tier, consecutive, "
    "first_observed_at, escalated_at, journal_id, written_at, woke_at, attempts, "
    "last_attempt_at, last_reason, row_seal"
)

#: The same list with the v2 column supplied as a literal, for a v1 file opened READ-ONLY
#: (a `--status` run can reach a store no read-write connection has migrated yet). The rows
#: then carry an empty seal, which is a state-binding mismatch — the fail-closed direction.
_PENDING_COLUMNS_PRE_V2 = _PENDING_COLUMNS.replace(
    "row_seal", "'' AS row_seal"
)


class StallEscalationStore:
    """Streak runs + pending escalations in the home-scoped stall-escalation DB."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else stall_escalation_path(home)

    # -- connections ---------------------------------------------------------------

    def _connect_rw(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            conn.execute(_STREAK_TABLE_SQL)
            conn.execute(_PENDING_TABLE_SQL)
            conn.execute(_WATERMARK_TABLE_SQL)
            conn.execute(_DISCOVERY_TABLE_SQL)
            conn.execute(f"PRAGMA user_version = {STALL_ESCALATION_SCHEMA_VERSION}")
        elif version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise StallEscalationStoreError(
                f"stall escalation store {self.path} has unsupported schema version {version}; "
                f"this build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. Left untouched."
            )
        else:
            # Self-heal a table lost under a valid version (the sibling stores' behaviour).
            conn.execute(_STREAK_TABLE_SQL)
            conn.execute(_PENDING_TABLE_SQL)
            conn.execute(_WATERMARK_TABLE_SQL)
            conn.execute(_DISCOVERY_TABLE_SQL)
            if version < STALL_ESCALATION_SCHEMA_VERSION:
                self._add_missing_pending_columns(conn)
                conn.execute(f"PRAGMA user_version = {STALL_ESCALATION_SCHEMA_VERSION}")
        return conn

    @classmethod
    def _pending_columns(cls, conn: sqlite3.Connection) -> str:
        """The column list this connection can serve (v1 files lack ``row_seal``)."""
        present = frozenset(
            str(row[1])
            for row in conn.execute("PRAGMA table_info(stall_escalation_pending)")
        )
        return _PENDING_COLUMNS if "row_seal" in present else _PENDING_COLUMNS_PRE_V2

    @classmethod
    def _add_missing_pending_columns(cls, conn: sqlite3.Connection) -> None:
        """Bring a pre-v2 pending table up to the current column set, additively."""
        if cls._pending_columns(conn) is _PENDING_COLUMNS_PRE_V2:
            conn.execute(
                "ALTER TABLE stall_escalation_pending "
                "ADD COLUMN row_seal TEXT NOT NULL DEFAULT ''"
            )

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            conn.close()
            raise StallEscalationStoreError(
                f"stall escalation store {self.path} is unreadable: {exc}"
            ) from exc
        if version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise StallEscalationStoreError(
                f"stall escalation store {self.path} has unsupported schema version {version}."
            )
        return conn

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass

    @staticmethod
    def _table_present(conn: sqlite3.Connection, name: str) -> bool:
        return (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def _mutate(self, action: str, work) -> object:
        conn = self._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = work(conn)
            conn.execute("COMMIT")
            return result
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise StallEscalationStoreError(
                f"stall escalation {action} failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- streak runs ---------------------------------------------------------------

    def read_streaks(self, workspace_id: str) -> dict[tuple[str, str, str], StreakRow]:
        """Every recorded run for a workspace, keyed by slot (empty when absent)."""
        ws = str(workspace_id or "").strip()
        if not ws:
            return {}
        conn = self._connect_ro()
        if conn is None:
            return {}
        try:
            if not self._table_present(conn, "stall_watch_streak"):
                return {}
            rows = conn.execute(
                f"SELECT {_STREAK_COLUMNS} FROM stall_watch_streak WHERE workspace_id=?",
                (ws,),
            ).fetchall()
        finally:
            conn.close()
        out: dict[tuple[str, str, str], StreakRow] = {}
        for r in rows:
            row = StreakRow(
                workspace_id=str(r[0]),
                lane_id=str(r[1]),
                role=str(r[2]),
                generation=str(r[3] or ""),
                target=str(r[4] or ""),
                stall_class=str(r[5]),
                consecutive=int(r[6]),
                first_observed_at=str(r[7]),
                last_observed_at=str(r[8]),
                escalated_at=str(r[9] or ""),
            )
            out[row.key] = row
        return out

    def write_streak(self, row: StreakRow) -> None:
        """Upsert one slot's run."""
        if not row.workspace_id or not row.lane_id or not row.role:
            return

        def _work(conn):
            conn.execute(
                f"INSERT INTO stall_watch_streak ({_STREAK_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, lane_id, role) DO UPDATE SET "
                "generation=excluded.generation, target=excluded.target, "
                "stall_class=excluded.stall_class, consecutive=excluded.consecutive, "
                "first_observed_at=excluded.first_observed_at, "
                "last_observed_at=excluded.last_observed_at, "
                "escalated_at=excluded.escalated_at",
                (
                    row.workspace_id,
                    row.lane_id,
                    row.role,
                    row.generation,
                    row.target,
                    row.stall_class,
                    int(row.consecutive),
                    row.first_observed_at,
                    row.last_observed_at,
                    row.escalated_at,
                ),
            )

        self._mutate("streak write", _work)

    def clear_streak(self, key: tuple[str, str, str]) -> None:
        """Delete one slot's run (the unit proved it is alive)."""
        ws, lane, role = (str(part or "").strip() for part in key)
        if not ws or not lane or not role or not self.path.exists():
            return
        self._mutate(
            "streak clear",
            lambda conn: conn.execute(
                "DELETE FROM stall_watch_streak WHERE workspace_id=? AND lane_id=? AND role=?",
                (ws, lane, role),
            ),
        )

    def forget_absent_slots(
        self, workspace_id: str, live_keys: Sequence[tuple[str, str, str]]
    ) -> int:
        """Drop runs for slots absent from the live inventory; returns how many.

        A unit retired while stuck would otherwise keep a HELD run forever (a held
        observation never advances *or* resets, so nothing else would remove it). The live
        inventory is the authority on existence, so absence from it — not a timeout — is
        what clears the row. Pending escalations are untouched: the stall happened, and
        dropping the record of it because the pane went away would erase the very fact the
        watcher exists to preserve.
        """
        ws = str(workspace_id or "").strip()
        if not ws or not self.path.exists():
            return 0
        live = {
            (str(a or "").strip(), str(b or "").strip(), str(c or "").strip())
            for a, b, c in live_keys
        }

        def _work(conn):
            rows = conn.execute(
                "SELECT workspace_id, lane_id, role FROM stall_watch_streak WHERE workspace_id=?",
                (ws,),
            ).fetchall()
            gone = [
                (str(r[0]), str(r[1]), str(r[2]))
                for r in rows
                if (str(r[0]), str(r[1]), str(r[2])) not in live
            ]
            for key in gone:
                conn.execute(
                    "DELETE FROM stall_watch_streak "
                    "WHERE workspace_id=? AND lane_id=? AND role=?",
                    key,
                )
            return len(gone)

        return int(self._mutate("streak sweep", _work))  # type: ignore[arg-type]

    # -- pending escalations -------------------------------------------------------

    def enqueue_pending(self, pending: PendingEscalation) -> bool:
        """Record one firing as pending; ``False`` when that firing is already known.

        The primary key is the firing's own idempotency key, so a crash between this write
        and the Redmine write, or a retried tick, collides here rather than producing a
        second durable journal.
        """
        if not pending.idempotency_key or not pending.workspace_id:
            return False
        # EVERY persisted column, including the persistence-state ones this store writes
        # itself. Round six found those five unvalidated immediately after round five
        # declared the row closed: they were skipped because they are "ours", which forgets
        # that a store is not a trust boundary (review j#110218).
        # DERIVED here, never taken from the caller. Note the ORDER: validate first, then
        # seal what will actually be STORED -- `checked_timestamp` folds instants to UTC, so
        # sealing the caller's values would make every row that needed normalizing read as
        # tampered the moment the store itself wrote it.
        fields = validate_pending_fields(replace(pending, row_seal=row_seal_for(pending)))
        fields["row_seal"] = pending_row_seal(
            idempotency_key=fields["idempotency_key"],
            values={name: fields[name] for name in ROW_SEAL_FIELDS},
        )
        first_observed_at = fields["first_observed_at"]
        escalated_at = fields["escalated_at"]

        def _work(conn):
            cursor = conn.execute(
                f"INSERT INTO stall_escalation_pending ({_PENDING_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    pending.idempotency_key,
                    fields["workspace_id"],
                    fields["lane_id"],
                    fields["role"],
                    fields["generation"],
                    fields["target"],
                    fields["issue"],
                    fields["stall_class"],
                    fields["prescription"],
                    fields["matched_id"],
                    fields["evidence_tier"],
                    fields["consecutive"],
                    first_observed_at,
                    escalated_at,
                    fields["journal_id"],
                    fields["written_at"],
                    fields["woke_at"],
                    fields["attempts"],
                    fields["last_attempt_at"],
                    fields["last_reason"],
                    fields["row_seal"],
                ),
            )
            return cursor.rowcount > 0

        return bool(self._mutate("pending enqueue", _work))

    def unrecorded_pending(self, workspace_id: str = "") -> tuple[PendingEscalation, ...]:
        """Firings with no Redmine journal yet, oldest firing first.

        Oldest-first is the fairness order: whichever escalation has waited longest takes
        the next available write slot, so a busy cockpit cannot let a newer stall
        repeatedly overtake an older one.

        This is the SUPPLY SIDE of the external Redmine write, so it is filtered to rows
        that still satisfy the contract. Use :meth:`quarantined_pending` to see the rest.
        """
        return _writable_only(self._read_pending("journal_id=''", workspace_id))

    def unwoken_pending(self, workspace_id: str = "") -> tuple[PendingEscalation, ...]:
        """Firings whose journal exists but whose coordinator wake has not been enqueued.

        Filtered like :meth:`unrecorded_pending`: a wake is an effect on the coordinator.
        """
        return _writable_only(self._read_pending("journal_id<>'' AND woke_at=''", workspace_id))

    def open_pending(self, workspace_id: str = "") -> tuple[PendingEscalation, ...]:
        """Every firing that is not fully settled (unwritten or unwoken).

        Deliberately UNfiltered: this is the inventory surface, and an inventory that hides
        the rows something went wrong with is the wrong inventory. Callers that actuate use
        :meth:`unrecorded_pending` / :meth:`unwoken_pending`.
        """
        return self._read_pending("journal_id='' OR woke_at=''", workspace_id)

    def quarantined_pending(self, workspace_id: str = "") -> tuple[PendingEscalation, ...]:
        """Every stored firing that failed the row contract, at any lifecycle stage.

        Non-empty here means a stored escalation was altered after it was written, which is
        an operator-visible condition in its own right — not a reason to go quiet.

        Deliberately NOT built on :meth:`open_pending`. A row whose ``journal_id`` and
        ``woke_at`` were both rewritten looks *settled* to the lifecycle predicate, so
        scoping the scan to open rows would let the most complete forgery be the one nobody
        can see (review j#110218).
        """
        return tuple(
            row for row in self._read_pending("1=1", workspace_id)
            if not row.externally_writable
        )

    def _read_pending(self, predicate: str, workspace_id: str) -> tuple[PendingEscalation, ...]:
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            if not self._table_present(conn, "stall_escalation_pending"):
                return ()
            sql = (
                f"SELECT {self._pending_columns(conn)} FROM stall_escalation_pending "
                f"WHERE ({predicate}) "
            )
            ws = str(workspace_id or "").strip()
            if ws:
                rows = conn.execute(
                    sql + "AND workspace_id=? ORDER BY escalated_at, idempotency_key", (ws,)
                ).fetchall()
            else:
                rows = conn.execute(
                    sql + "ORDER BY escalated_at, idempotency_key"
                ).fetchall()
        finally:
            conn.close()
        # The SQL ORDER BY is a deterministic BASE, not the ordering AUTHORITY: it sorts
        # the raw stored text, which a corrupted row can place anywhere. Oldest-first is a
        # fairness contract, so the order is re-derived here from validated instants
        # (review j#110192 finding_2).
        built = [(timestamp_sort_key(r[13]), str(r[0]), self._row_to_pending(r)) for r in rows]
        built.sort(key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in built)

    @staticmethod
    def _row_to_pending(r) -> PendingEscalation:
        """One stored row as a value object, classified against the whole-row contract.

        Nothing is dropped here. A corrupted row is still evidence that a stall fired, and
        deleting it would turn a tampering signal into silence. It is stamped instead, and
        the actuation surfaces filter on that stamp.
        """
        row = PendingEscalation(
            idempotency_key=str(r[0]),
            workspace_id=str(r[1]),
            lane_id=str(r[2]),
            role=str(r[3]),
            generation=str(r[4] or ""),
            target=str(r[5] or ""),
            issue=str(r[6] or ""),
            stall_class=str(r[7]),
            prescription=str(r[8]),
            matched_id=str(r[9] or ""),
            evidence_tier=str(r[10] or ""),
            # NOT `int(...)`. Coercing here made a non-numeric stored value raise a
            # ValueError out of every read surface -- including `quarantined_pending`,
            # whose entire job is to make corrupted rows visible (review j#110218). The
            # raw value is carried and the contract turns it into a typed verdict, so the
            # annotation is what a VALID row holds, not a promise about a tampered one.
            consecutive=r[11],
            # A row that predates this build (or was hand-edited) can still hold junk here.
            # The row is KEPT -- it is a real escalation and dropping it would lose the
            # stall report -- but nothing arbitrary is rendered from it.
            first_observed_at=rendered_timestamp(r[12], field="observed_at.first"),
            escalated_at=rendered_timestamp(r[13], field="observed_at.escalated"),
            journal_id=str(r[14] or ""),
            written_at=str(r[15] or ""),
            woke_at=str(r[16] or ""),
            attempts=r[17],
            last_attempt_at=str(r[18] or ""),
            last_reason=str(r[19] or ""),
            row_seal=str(r[20] or ""),
        )
        return replace(row, integrity=pending_row_integrity(row))

    def _transition(self, action: str, idempotency_key: str, plan, *, sql_guard: str = "") -> bool:
        """Run one sealed persistence-state transition (rules in ``stall_pending_transition``).

        The store supplies the connection, the column set and the row reader; it does not
        decide what a transition may do. That separation is why the integrity verdict the
        transition refuses on is the same verdict every read surface computes.
        """
        if not str(idempotency_key or "") or not self.path.exists():
            return False

        def _work(conn):
            return apply_sealed_transition(
                conn,
                idempotency_key=idempotency_key,
                select_sql=(
                    f"SELECT {self._pending_columns(conn)} FROM stall_escalation_pending "
                    "WHERE idempotency_key=?"
                ),
                row_reader=self._row_to_pending,
                plan=plan,
                sql_guard=sql_guard,
            )

        return bool(self._mutate(action, _work))

    def record_attempt(self, idempotency_key: str, reason: str, *, now: Optional[str] = None) -> bool:
        """Count one refused / failed write attempt against a firing, with its reason.

        A repeatedly-refused write must not look like an escalation nobody has reached yet;
        ``attempts`` and ``last_reason`` are what a status surface reads to tell the two
        apart. The reason is held to the closed vocabulary on the way IN, because it reaches
        the status surface: an unknown reason is recorded as a refusal to classify rather
        than passed through verbatim.
        """
        return self._transition(
            "pending attempt",
            idempotency_key,
            plan_attempt(
                reason=_safe_reason(reason),
                stamp=_utc_now_iso() if now is None else now,
            ),
        )

    def mark_recorded(
        self, idempotency_key: str, journal_id: str, *, now: Optional[str] = None
    ) -> bool:
        """Bind a firing to the Redmine journal that now carries it (readback fence).

        ``journal_id`` must satisfy the SAME grammar the row contract declares — not merely
        be non-empty. A blank or malformed id is refused and the firing stays unrecorded and
        retryable, which is the recoverable direction: the opposite stored a value that
        every later surface then had to treat as corruption (review j#110254).

        The check is not written here. It happens once, in the transition, against
        :data:`PENDING_FIELD_CHECKERS` — the same table the read side uses. Duplicating it
        here was provably equivalent (a mutation removing it changed nothing observable),
        and a guard nothing can measure is a guard that will drift.

        Also NOT ``.strip()``. Trimming was a repairing face of the same rule: ``" 110264"``
        was silently stored as ``"110264"``, so this face answered "yes" to an input every
        other face refused. The equivalence test found it; review had not.
        """
        return self._transition(
            "pending record",
            idempotency_key,
            plan_recorded(
                journal_id=str(journal_id or ""),
                stamp=_utc_now_iso() if now is None else now,
            ),
        )

    def mark_woken(self, idempotency_key: str, *, now: Optional[str] = None) -> bool:
        """Record that the coordinator wake for a **recorded** firing was enqueued.

        Fenced twice, in Python and in SQL, because waking a coordinator to read a journal
        that does not exist is the one ordering this rail is built to prevent. Both fences
        now come from the one grammar: the SQL predicate is generated by
        :func:`canonical_numeric_id_sql` instead of being hand-written, which is how the
        12-character bound went missing from it in the first place (review j#110254
        finding_checkerdrift).

        Note what neither fence can do: they check SHAPE. That the journal EXISTS is the
        caller's admission to make, against Redmine, before it ever gets here
        (:func:`admit_wake`).

        The two fences MASK each other under single-point mutation, which is what defence in
        depth looks like from a mutation harness. Rather than delete one to make a number go
        green, the pair is measured AS a pair (removing both is red) and the SQL predicate is
        separately tested against the Python grammar over a corpus.
        """
        return self._transition(
            "pending wake",
            idempotency_key,
            plan_woken(stamp=_utc_now_iso() if now is None else now),
            sql_guard=f"woke_at='' AND {canonical_numeric_id_sql('journal_id')}",
        )


    # -- cadence watermark ---------------------------------------------------------

    def last_pass_at(self, workspace_id: str) -> str:
        """When this workspace last ran a stall-watch phase (``""`` when never).

        The watcher's OWN cadence watermark. It deliberately does not share state with the
        supervisor's provider-reconciliation watermark even though both currently default to
        300s: they answer different questions (how often to read screens vs how often to
        read Redmine), and coupling them would make a change to one silently retune the
        other (#15855 j#110121-2).
        """
        ws = str(workspace_id or "").strip()
        if not ws:
            return ""
        conn = self._connect_ro()
        if conn is None:
            return ""
        try:
            if not self._table_present(conn, "stall_watch_watermark"):
                return ""
            row = conn.execute(
                "SELECT last_pass_at FROM stall_watch_watermark WHERE workspace_id=?",
                (ws,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return ""
        # An unparseable stored watermark renders as the closed token, which the cadence
        # reader already treats as "unreadable -> due" (`CADENCE_UNREADABLE_WATERMARK`).
        # So a corrupt row makes the watcher run again rather than emit arbitrary text.
        return rendered_timestamp(row[0], field="observed_at.last_pass")

    def mark_pass(self, workspace_id: str, *, now: Optional[str] = None) -> None:
        """Record that a stall-watch phase ran for this workspace."""
        ws = str(workspace_id or "").strip()
        if not ws:
            return
        stamp = checked_timestamp(
            _utc_now_iso() if now is None else now, field="observed_at.last_pass"
        )
        self._mutate(
            "watermark write",
            lambda conn: conn.execute(
                "INSERT INTO stall_watch_watermark (workspace_id, last_pass_at) "
                "VALUES (?, ?) ON CONFLICT(workspace_id) DO UPDATE SET "
                "last_pass_at=excluded.last_pass_at",
                (ws, stamp),
            ),
        )


    # -- discovery coverage --------------------------------------------------------

    def record_discovery(
        self,
        workspace_id: str,
        *,
        candidates: int,
        watched: int,
        out_of_reach: int,
        dropped: Optional[dict] = None,
        now: Optional[str] = None,
    ) -> None:
        """Persist the last pass's coverage COUNTS for this workspace.

        Counts only — no lane id, no locator, no reason beyond the fixed drop tokens. The
        row exists so a later ``--status`` can answer "what is this watcher NOT seeing"
        without re-running discovery, so it must be as safe to render as it is to store.
        """
        ws = str(workspace_id or "").strip()
        if not ws:
            return
        # Validate BEFORE the row exists. An off-vocabulary reason or a negative count that
        # reaches the table is already on its way to `--status`, so the contract is enforced
        # at the write seam rather than apologised for at the read one (review j#110169).
        candidates, watched, out_of_reach, checked = validate_discovery(
            candidates=candidates,
            watched=watched,
            out_of_reach=out_of_reach,
            dropped=dropped,
        )
        payload = json.dumps(checked, sort_keys=True)
        # The fifth column. Validated and normalized like every other value in the row --
        # "it is a timestamp" was an assumption nothing enforced (review j#110183).
        stamp = checked_timestamp(
            _utc_now_iso() if now is None else now, field="observed_at"
        )
        self._mutate(
            "discovery write",
            lambda conn: conn.execute(
                "INSERT INTO stall_watch_discovery (workspace_id, observed_at, candidates, "
                "watched, out_of_reach, dropped) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET observed_at=excluded.observed_at, "
                "candidates=excluded.candidates, watched=excluded.watched, "
                "out_of_reach=excluded.out_of_reach, dropped=excluded.dropped",
                (ws, stamp, int(candidates), int(watched), int(out_of_reach), payload),
            ),
        )

    def last_discovery(self, workspace_id: str) -> Optional[dict]:
        """The last recorded coverage summary, or ``None`` when the leg has never run.

        ``None`` is a real answer and must stay distinguishable from "zero units watched":
        a watcher that has not run yet and a watcher that ran and found nothing are
        different operator situations.
        """
        ws = str(workspace_id or "").strip()
        if not ws:
            return None
        conn = self._connect_ro()
        if conn is None:
            return None
        try:
            if not self._table_present(conn, "stall_watch_discovery"):
                return None
            row = conn.execute(
                "SELECT observed_at, candidates, watched, out_of_reach, dropped "
                "FROM stall_watch_discovery WHERE workspace_id=?",
                (ws,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        # The durable row is re-validated on the way OUT as well. A store is not a trust
        # boundary: an older build, a hand-edited DB or a partially-written row can all put
        # values here that this build's contract forbids, and the read path is what actually
        # feeds `--status` (review j#110169).
        try:
            dropped = json.loads(str(row[4]) or "{}")
        except ValueError:
            return unreadable_discovery(DISCOVERY_MALFORMED)
        try:
            candidates, watched, out_of_reach, checked = validate_discovery(
                candidates=row[1],
                watched=row[2],
                out_of_reach=row[3],
                dropped=dropped,
            )
            observed_at = checked_timestamp(row[0], field="observed_at")
        except StallDiscoveryContractError as exc:
            return unreadable_discovery(discovery_reject_token(exc))
        return {
            "observed_at": observed_at,
            "candidates": candidates,
            "watched": watched,
            "out_of_reach": out_of_reach,
            "dropped": checked,
        }


__all__ = (
    "DISCOVERY_BAD_COUNT",
    "DISCOVERY_BAD_TIMESTAMP",
    "DISCOVERY_BAD_REASON_TOKEN",
    "DISCOVERY_DROP_REASONS",
    "DISCOVERY_FOREIGN_REASON",
    "DISCOVERY_INCONSISTENT",
    "DISCOVERY_MALFORMED",
    "DISCOVERY_UNREADABLE",
    "STALL_ESCALATION_FILENAME",
    "TIMESTAMP_MAX_LENGTH",
    "TIMESTAMP_UNREADABLE",
    "STALL_ESCALATION_SCHEMA_VERSION",
    "PendingEscalation",
    "StallEscalationStore",
    "StallEscalationStoreError",
    "StallDiscoveryContractError",
    "StreakRow",
    "escalation_idempotency_key",
    "unreadable_discovery",
    "validate_discovery",
    "stall_escalation_path",
)
