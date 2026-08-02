"""The #13686 actuator's durable step ledger (Redmine #14825, acceptance item 4).

#13686 shipped :class:`~...application.auto_integration_ports.InMemoryLedgerStore` and said
plainly what it was not: "its lifetime is one actuator instance, so a resume across processes
finds nothing and the run starts over". For the integration machine that is not merely a missing
optimisation — the CI gate is asynchronous, so the run that pushes and the run that concludes
``integrated`` are DIFFERENT PROCESSES by construction. Without a ledger that outlives a process
there is no continuation, and the feature cannot complete.

This is that store. Three properties, each of which #13686 named as owed to this issue.

**The writer identity belongs to the store.** ``StepOutcome.recorded_by`` arrives from the caller
and is ignored: :meth:`SqliteLedgerStore.append` stamps :attr:`~SqliteLedgerStore.writer_id`,
which the store minted into its own file the first time that file was created. A caller cannot
choose it, cannot rewrite it, and cannot construct an entry that carries it without going through
``append``. #13686's per-instance receipt could not do this and said so: "a durable store with an
authenticated writer identity is the real answer".

*What that boundary actually is, stated rather than implied.* The identity is the STORE's, not a
per-process token — it has to be, because the whole point is that a later process trusts what an
earlier one wrote. So the trust boundary is the ledger FILE (its filesystem permissions), and the
guarantee is: an entry carrying this writer id was written through this store's append API. It is
not a proof of which process wrote it, and nothing here should be read as one. What it does close
is the defect it was asked to close (R4 review j#96379 finding 1): a provenance derived from
public constructor values that a caller could reproduce *without* the store.

**A mutation and its receipt are two writes, and a crash can land between them.** So they are not
two independent writes: :meth:`begin_step` records an INTENT before the actuator performs the
side effect, and ``append`` closes that intent and records the outcome in ONE transaction. A run
that finds an unresolved intent knows a mutation may have run and that its outcome is unknown —
which is not the same as knowing it did not run, and must never resolve to "retry the push". The
actuator refuses to mutate while one is open (:meth:`unresolved_intents`), and an operator
resolves it by reading what actually landed.

**A ``done`` step is recorded once per action.** A partial unique index makes a second ``done``
row for the same ``(action_key, step)`` a database error rather than a second entry the ledger
would then report as a duplicate step. The idempotency contract is enforced by the store, not
only checked by the reader afterwards.

The store is append-only: there is no update and no delete on the outcome table, and resolving an
intent writes to the intent table alone.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Tuple

from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    OUTCOME_DONE,
    StepOutcome,
)

#: The ledger file, alongside the other home-scoped durable stores.
AUTO_INTEGRATION_LEDGER_FILENAME = "auto_integration_ledger.sqlite3"

#: Bumped only for an incompatible layout. A file written by a newer schema is left untouched
#: rather than opened optimistically (the same downgrade-safe posture as the herdr ledger).
AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION = 1

_WRITER_SQL = """
CREATE TABLE IF NOT EXISTS auto_integration_writer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    writer_id TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_OUTCOME_SQL = """
CREATE TABLE IF NOT EXISTS auto_integration_ledger (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    action_key TEXT NOT NULL,
    step TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    head TEXT NOT NULL DEFAULT '',
    git_version TEXT NOT NULL DEFAULT '',
    merge_status TEXT NOT NULL DEFAULT '',
    push_status TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL
)
"""

_INTENT_SQL = """
CREATE TABLE IF NOT EXISTS auto_integration_intent (
    id INTEGER PRIMARY KEY,
    opened_at TEXT NOT NULL,
    action_key TEXT NOT NULL,
    step TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    resolved_at TEXT
)
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_auto_integration_ledger_action "
    "ON auto_integration_ledger(action_key, id)",
    # The idempotency contract, enforced where the write happens: one `done` per step per action.
    # A second one is refused by the database, so "the ledger records a duplicate step" cannot be
    # produced by this store at all — a reader that finds one is looking at a foreign file.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_integration_ledger_done "
    "ON auto_integration_ledger(action_key, step) WHERE outcome = 'done'",
    "CREATE INDEX IF NOT EXISTS idx_auto_integration_intent_open "
    "ON auto_integration_intent(action_key, resolved_at)",
)

_COLUMNS = (
    "recorded_at, action_key, step, outcome, detail, head, git_version, "
    "merge_status, push_status, recorded_by"
)


class AutoIntegrationLedgerError(RuntimeError):
    """The ledger could not be opened or written. Fail-closed: never "nothing has run"."""


@dataclass(frozen=True)
class StepIntent:
    """A step this store was told was about to run, and whose outcome it never received.

    Its existence is the honest statement of an unknown: the side effect may have happened. A
    consumer must not read it as "the step did not run" — that reading is what turns a crash
    between a push and its receipt into a second push.
    """

    action_key: str
    step: str
    opened_at: str
    intent_id: int = 0
    recorded_by: str = ""


def auto_integration_ledger_path(home: Optional[Path] = None) -> Path:
    return (home or mozyo_bridge_home()) / AUTO_INTEGRATION_LEDGER_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SqliteLedgerStore:
    """A durable, append-only :class:`...auto_integration_ports.LedgerStore`.

    Satisfies the port's contract — whole :class:`StepOutcome` records including their
    provenance, and entries only ever from :meth:`append` — and adds the two things a
    cross-process, asynchronously-gated action needs: a writer identity the store owns, and an
    intent record that survives a crash between a mutation and its receipt.
    """

    def __init__(
        self, path: Optional[Path] = None, *, home: Optional[Path] = None
    ) -> None:
        self.path = path or auto_integration_ledger_path(home)
        self._writer_id = ""

    # -- identity ---------------------------------------------------------

    @property
    def writer_id(self) -> str:
        """This store's own writer identity, minted into its file at creation.

        Read through the connection rather than generated per instance, so two processes opening
        the same ledger agree about which entries are the actuator's — which is the whole
        mechanism a resume across the asynchronous CI gate rests on.
        """
        if not self._writer_id:
            conn = self._connect()
            try:
                self._writer_id = self._read_writer(conn)
            finally:
                conn.close()
        return self._writer_id

    # -- port ---------------------------------------------------------------

    def read(self, *, action_key: str) -> Sequence[StepOutcome]:
        """Every entry recorded under exactly ``action_key``, oldest first.

        A missing file is an empty ledger (nothing has been recorded yet), which is the correct
        fail-closed reading: the run starts over rather than trusting anything. An UNREADABLE
        file is a different fact and raises — "we cannot read what ran" must not present as
        "nothing ran", because the next thing the caller would do is push again.
        """
        if not self.path.exists():
            return ()
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM auto_integration_ledger "
                "WHERE action_key = ? ORDER BY id",
                (str(action_key),),
            ).fetchall()
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be read "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return tuple(_row_to_outcome(row) for row in rows)

    def append(self, outcome: StepOutcome) -> None:
        """Record ``outcome``, stamped with THIS store's writer identity.

        ``outcome.recorded_by`` is not read. A payload's own claim about who wrote it is exactly
        the self-report this store exists to replace, and accepting it "when it happens to match"
        would make the check depend on the value being checked.

        Any intent open for the same ``(action_key, step)`` is resolved in the same transaction,
        so the receipt and the closing of the intent cannot come apart the way the mutation and
        the receipt can.
        """
        conn = self._connect()
        try:
            writer = self._read_writer(conn)
            with conn:
                conn.execute(
                    f"INSERT INTO auto_integration_ledger ({_COLUMNS}) "
                    f"VALUES ({', '.join('?' * 10)})",
                    (
                        _utc_now(),
                        str(outcome.action_key),
                        str(outcome.step),
                        str(outcome.outcome),
                        str(outcome.detail),
                        str(outcome.head),
                        str(outcome.git_version),
                        str(outcome.merge_status),
                        str(outcome.push_status),
                        writer,
                    ),
                )
                conn.execute(
                    "UPDATE auto_integration_intent SET resolved_at = ? "
                    "WHERE action_key = ? AND step = ? AND resolved_at IS NULL",
                    (_utc_now(), str(outcome.action_key), str(outcome.step)),
                )
        except sqlite3.IntegrityError as exc:
            raise AutoIntegrationLedgerError(
                f"step {outcome.step!r} is already recorded {OUTCOME_DONE} for this action; "
                "refusing to record it twice (the idempotency key is the action key)"
            ) from exc
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be written "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()

    def completed_action_keys(self, *, prefix: str, step: str) -> Tuple[str, ...]:
        """Action keys under ``prefix`` whose ``step`` this store recorded ``done``, in order.

        The lifecycle authority a post-close cleanup is authorized BY. #13686's cleanup compared
        the authorization it was offered against the record that offered it — a comparison whose
        two sides were the same field, so it could not fail (Redmine #14825 item 5). The
        independent side is here: the ledger says which integration action actually ran to
        completion, and it says so in rows this store stamped, not in the record asking to act.

        ``prefix`` matches the action key's leading identity fields exactly (they are ordered, so
        a prefix IS an identity constraint); ``_`` and ``%`` in it are escaped so a caller's
        value cannot widen the match into a pattern.
        """
        if not self.path.exists():
            return ()
        pattern = _like_prefix(prefix)
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                "SELECT DISTINCT action_key FROM auto_integration_ledger "
                "WHERE step = ? AND outcome = ? AND action_key LIKE ? ESCAPE '\\' "
                "ORDER BY action_key",
                (str(step), OUTCOME_DONE, pattern),
            ).fetchall()
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be read "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return tuple(str(row[0]) for row in rows)

    # -- crash boundary ----------------------------------------------------

    def begin_step(self, *, action_key: str, step: str) -> StepIntent:
        """Record that ``step`` is ABOUT to run, before the side effect happens.

        The window this closes is the one between a ``git push`` landing and its receipt being
        written. Without an intent the next run sees no push entry and offers the push again; with
        one it sees a step whose outcome is unknown and stops. "We do not know" is a state the
        durable record has to be able to hold, or it will be rounded to the convenient one.
        """
        conn = self._connect()
        try:
            writer = self._read_writer(conn)
            opened_at = _utc_now()
            with conn:
                cursor = conn.execute(
                    "INSERT INTO auto_integration_intent "
                    "(opened_at, action_key, step, recorded_by) VALUES (?, ?, ?, ?)",
                    (opened_at, str(action_key), str(step), writer),
                )
                intent_id = cursor.lastrowid
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not record a step intent "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return StepIntent(
            action_key=str(action_key),
            step=str(step),
            opened_at=opened_at,
            intent_id=intent_id,
            recorded_by=writer,
        )

    def unresolved_intents(self, *, action_key: str) -> Tuple[StepIntent, ...]:
        """Steps whose side effect may have run and whose outcome never arrived."""
        if not self.path.exists():
            return ()
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                "SELECT id, opened_at, action_key, step, recorded_by FROM "
                "auto_integration_intent WHERE action_key = ? AND resolved_at IS NULL "
                "ORDER BY id",
                (str(action_key),),
            ).fetchall()
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be read "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return tuple(
            StepIntent(
                intent_id=int(row[0]),
                opened_at=str(row[1]),
                action_key=str(row[2]),
                step=str(row[3]),
                recorded_by=str(row[4]),
            )
            for row in rows
        )

    # -- connection --------------------------------------------------------

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self._check_version(conn)
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with conn:
                conn.execute(_WRITER_SQL)
                conn.execute(_OUTCOME_SQL)
                conn.execute(_INTENT_SQL)
                for sql in _INDEX_SQL:
                    conn.execute(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO auto_integration_writer "
                    "(id, writer_id, created_at) VALUES (1, ?, ?)",
                    (f"ledger:{secrets.token_hex(16)}", _utc_now()),
                )
                conn.execute(
                    f"PRAGMA user_version = {AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION}"
                )
            return conn
        self._check_version(conn, opened=version)
        return conn

    def _check_version(
        self, conn: sqlite3.Connection, *, opened: Optional[int] = None
    ) -> None:
        version = (
            opened
            if opened is not None
            else conn.execute("PRAGMA user_version").fetchone()[0]
        )
        if version != AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION:
            conn.close()
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} has schema version {version}; this "
                f"mozyo-bridge supports {AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION}. The file is "
                "left untouched (downgrade-safe)."
            )

    @staticmethod
    def _read_writer(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT writer_id FROM auto_integration_writer WHERE id = 1"
        ).fetchone()
        if not row or not str(row[0] or "").strip():
            raise AutoIntegrationLedgerError(
                "the auto-integration ledger carries no writer identity; refusing to attribute "
                "entries to an identity that was never minted"
            )
        return str(row[0])


def _like_prefix(prefix: str) -> str:
    """``prefix`` as a LIKE pattern matching it literally, then anything.

    SQL ``LIKE`` treats ``%`` and ``_`` as wildcards, and an action key is caller data (a
    ``target_ref`` may legitimately contain either). Escaping them keeps a prefix a prefix
    rather than letting one widen into a pattern that matches a DIFFERENT action.
    """
    escaped = (
        str(prefix).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"{escaped}%"


def _row_to_outcome(row: Sequence[object]) -> StepOutcome:
    return StepOutcome(
        action_key=str(row[1]),
        step=str(row[2]),
        outcome=str(row[3]),
        detail=str(row[4]),
        head=str(row[5]),
        git_version=str(row[6]),
        merge_status=str(row[7]),
        push_status=str(row[8]),
        recorded_by=str(row[9]),
    )


__all__ = (
    "AUTO_INTEGRATION_LEDGER_FILENAME",
    "AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION",
    "AutoIntegrationLedgerError",
    "SqliteLedgerStore",
    "StepIntent",
    "auto_integration_ledger_path",
)
