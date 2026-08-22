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

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-scoped SQLite file holding stall streaks and pending escalations.
STALL_ESCALATION_FILENAME = "stall-escalation.sqlite"

#: Schema version stamped into ``PRAGMA user_version``. Unrecognized -> fail closed.
STALL_ESCALATION_SCHEMA_VERSION = 1

_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1})

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
    last_reason       TEXT NOT NULL DEFAULT ''
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


def escalation_idempotency_key(
    *,
    workspace_id: str,
    lane_id: str,
    role: str,
    generation: str,
    stall_class: str,
    first_observed_at: str,
) -> str:
    """The stable key identifying ONE firing of ONE streak.

    Derived from the run's own identity rather than from the firing pass's clock, so a
    crash-and-retry of the same firing collides and a genuinely different run does not.
    ``first_observed_at`` is what separates two runs of the same class on the same slot and
    generation: the policy layer restarts ``first_observed_at`` on every restart, so two
    runs separated by a reset produce two keys while one run retried across a crash keeps
    producing the same one.

    Deliberately NOT derived from ``escalated_at``: that moves on every pass, which would
    make each retry look like a new escalation and write a duplicate Redmine journal — the
    exact failure the readback fence exists to prevent.
    """
    digest = hashlib.sha256(
        "\x1f".join(
            (workspace_id, lane_id, role, generation, stall_class, first_observed_at)
        ).encode("utf-8")
    ).hexdigest()
    return f"stallesc1_{digest[:32]}"


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

    @property
    def slot_label(self) -> str:
        return f"{self.workspace_id}/{self.lane_id}/{self.role}"

    @property
    def recorded(self) -> bool:
        """Whether the durable record (the Redmine journal) exists for this firing."""
        return bool(self.journal_id)

    @property
    def settled(self) -> bool:
        return bool(self.journal_id) and bool(self.woke_at)

    def telemetry(self) -> dict[str, object]:
        """Classification-token-only projection, safe to paste into a durable record."""
        payload: dict[str, object] = {
            "idempotency_key": self.idempotency_key,
            "slot": self.slot_label,
            "stall_class": self.stall_class,
            "prescription": self.prescription,
            "consecutive": self.consecutive,
            "first_observed_at": self.first_observed_at,
            "escalated_at": self.escalated_at,
            "recorded": self.recorded,
            "settled": self.settled,
            "attempts": self.attempts,
        }
        for key, value in (
            ("generation", self.generation),
            ("target", self.target),
            ("issue", self.issue),
            ("matched_id", self.matched_id),
            ("evidence_tier", self.evidence_tier),
            ("journal_id", self.journal_id),
            ("last_reason", self.last_reason),
        ):
            if value:
                payload[key] = value
        return payload


_STREAK_COLUMNS = (
    "workspace_id, lane_id, role, generation, target, stall_class, consecutive, "
    "first_observed_at, last_observed_at, escalated_at"
)

_PENDING_COLUMNS = (
    "idempotency_key, workspace_id, lane_id, role, generation, target, issue, "
    "stall_class, prescription, matched_id, evidence_tier, consecutive, "
    "first_observed_at, escalated_at, journal_id, written_at, woke_at, attempts, "
    "last_attempt_at, last_reason"
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
        return conn

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

        def _work(conn):
            cursor = conn.execute(
                f"INSERT INTO stall_escalation_pending ({_PENDING_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    pending.idempotency_key,
                    pending.workspace_id,
                    pending.lane_id,
                    pending.role,
                    pending.generation,
                    pending.target,
                    pending.issue,
                    pending.stall_class,
                    pending.prescription,
                    pending.matched_id,
                    pending.evidence_tier,
                    int(pending.consecutive),
                    pending.first_observed_at,
                    pending.escalated_at,
                    pending.journal_id,
                    pending.written_at,
                    pending.woke_at,
                    int(pending.attempts),
                    pending.last_attempt_at,
                    pending.last_reason,
                ),
            )
            return cursor.rowcount > 0

        return bool(self._mutate("pending enqueue", _work))

    def unrecorded_pending(self, workspace_id: str = "") -> tuple[PendingEscalation, ...]:
        """Firings with no Redmine journal yet, oldest firing first.

        Oldest-first is the fairness order: whichever escalation has waited longest takes
        the next available write slot, so a busy cockpit cannot let a newer stall
        repeatedly overtake an older one.
        """
        return self._read_pending("journal_id=''", workspace_id)

    def unwoken_pending(self, workspace_id: str = "") -> tuple[PendingEscalation, ...]:
        """Firings whose journal exists but whose coordinator wake has not been enqueued."""
        return self._read_pending("journal_id<>'' AND woke_at=''", workspace_id)

    def open_pending(self, workspace_id: str = "") -> tuple[PendingEscalation, ...]:
        """Every firing that is not fully settled (unwritten or unwoken)."""
        return self._read_pending("journal_id='' OR woke_at=''", workspace_id)

    def _read_pending(self, predicate: str, workspace_id: str) -> tuple[PendingEscalation, ...]:
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            if not self._table_present(conn, "stall_escalation_pending"):
                return ()
            sql = (
                f"SELECT {_PENDING_COLUMNS} FROM stall_escalation_pending "
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
        return tuple(
            PendingEscalation(
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
                consecutive=int(r[11]),
                first_observed_at=str(r[12]),
                escalated_at=str(r[13]),
                journal_id=str(r[14] or ""),
                written_at=str(r[15] or ""),
                woke_at=str(r[16] or ""),
                attempts=int(r[17]),
                last_attempt_at=str(r[18] or ""),
                last_reason=str(r[19] or ""),
            )
            for r in rows
        )

    def record_attempt(self, idempotency_key: str, reason: str, *, now: Optional[str] = None) -> bool:
        """Count one refused / failed write attempt against a firing, with its reason.

        A repeatedly-refused write must not look like an escalation nobody has reached yet;
        ``attempts`` and ``last_reason`` are what a status surface reads to tell the two
        apart.
        """
        if not self.path.exists():
            return False

        def _work(conn):
            cursor = conn.execute(
                "UPDATE stall_escalation_pending SET attempts=attempts+1, "
                "last_attempt_at=?, last_reason=? WHERE idempotency_key=? AND journal_id=''",
                (now or _utc_now_iso(), str(reason or ""), idempotency_key),
            )
            return cursor.rowcount > 0

        return bool(self._mutate("pending attempt", _work))

    def mark_recorded(
        self, idempotency_key: str, journal_id: str, *, now: Optional[str] = None
    ) -> bool:
        """Bind a firing to the Redmine journal that now carries it (readback fence).

        ``journal_id`` must be non-empty: an "it probably wrote" with no readback is exactly
        the uncertain state that would let the next pass write a duplicate, so a blank id is
        refused and the firing stays unrecorded and retryable.
        """
        jid = str(journal_id or "").strip()
        if not jid or not self.path.exists():
            return False

        def _work(conn):
            cursor = conn.execute(
                "UPDATE stall_escalation_pending SET journal_id=?, written_at=?, "
                "attempts=attempts+1, last_attempt_at=?, last_reason='' "
                "WHERE idempotency_key=? AND journal_id=''",
                (jid, now or _utc_now_iso(), now or _utc_now_iso(), idempotency_key),
            )
            return cursor.rowcount > 0

        return bool(self._mutate("pending record", _work))

    def mark_woken(self, idempotency_key: str, *, now: Optional[str] = None) -> bool:
        """Record that the coordinator wake for a **recorded** firing was enqueued.

        Guarded on ``journal_id<>''`` in SQL rather than by the caller: waking a coordinator
        to read a journal that does not exist is the one ordering this whole rail is built
        to prevent, so the store refuses it instead of trusting call order.
        """
        if not self.path.exists():
            return False

        def _work(conn):
            cursor = conn.execute(
                "UPDATE stall_escalation_pending SET woke_at=? "
                "WHERE idempotency_key=? AND journal_id<>'' AND woke_at=''",
                (now or _utc_now_iso(), idempotency_key),
            )
            return cursor.rowcount > 0

        return bool(self._mutate("pending wake", _work))


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
        return str(row[0]) if row else ""

    def mark_pass(self, workspace_id: str, *, now: Optional[str] = None) -> None:
        """Record that a stall-watch phase ran for this workspace."""
        ws = str(workspace_id or "").strip()
        if not ws:
            return
        stamp = now or _utc_now_iso()
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
        payload = json.dumps(
            {str(k): int(v) for k, v in sorted((dropped or {}).items())},
            sort_keys=True,
        )
        stamp = now or _utc_now_iso()
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
        try:
            dropped = json.loads(str(row[4]) or "{}")
        except ValueError:
            dropped = {}
        return {
            "observed_at": str(row[0]),
            "candidates": int(row[1]),
            "watched": int(row[2]),
            "out_of_reach": int(row[3]),
            "dropped": dropped if isinstance(dropped, dict) else {},
        }


__all__ = (
    "STALL_ESCALATION_FILENAME",
    "STALL_ESCALATION_SCHEMA_VERSION",
    "PendingEscalation",
    "StallEscalationStore",
    "StallEscalationStoreError",
    "StreakRow",
    "escalation_idempotency_key",
    "stall_escalation_path",
)
