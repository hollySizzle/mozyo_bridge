"""The session-start action's own side-effect transaction authority (Redmine #13948).

A ``herdr session-start`` that starts one role and fails the other leaves a partial pair
nobody owns: the run has no durable handle on what it started, so a later command cannot
tell *this* action's Codex from a Codex somebody else launched a minute ago. #13882
j#80951 / j#80968 paid for that twice — the operator had to hand-approve a composer
discard to converge two panes the tool itself had created seconds earlier.

This is that missing handle: an **immutable startup action identity**, reserved *before*
the first side effect, recording each launch as a participant as it happens. It is what
makes an explicit rollback able to say "these exact panes are mine to undo" — and,
equally, what makes it refuse everything else.

Why a new authority rather than #13892's ``scratch_retirement_fence`` (Answer j#80989 Q3):
that store's unit, table and completion mean *retirement*. Opening a launch rollback as a
retirement attempt over the same unit would let a stale retirement completion be read as
proof about a live launch — the exact "old completion applied to a new pair" confusion its
own ``relaunch 誤認防止`` rule exists to prevent. The **patterns** are borrowed wholesale,
because they were bought with review cycles:

- reserve-before-effect (a side effect must never precede its durable record);
- an OS advisory lock (exclusive, non-blocking) held across the external close, because
  ``BEGIN IMMEDIATE`` cannot span a subprocess;
- contention is refused, never queued and never stolen;
- artifacts are three-valued — absent / present / damaged — never two;
- completion-write failure withholds success rather than fabricating it.

The one deliberate divergence: **reserve may bootstrap, rollback may not.** A reserve is
minting a *new* identity, so creating the store forgets nothing. A rollback asked to act
against an absent store has no proof of anything and must fail closed — bootstrapping
there would silently re-create a lost authority and then close panes on the strength of it.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.shared.paths import mozyo_bridge_home

STARTUP_TRANSACTION_FENCE_FILENAME = "startup-transaction-fence.sqlite"
STARTUP_TRANSACTION_FENCE_SEAL_SUFFIX = ".seal"
STARTUP_TRANSACTION_FENCE_LOCK_SUFFIX = ".lock"
STARTUP_TRANSACTION_FENCE_TEMP_SUFFIX = ".tmp"
STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION = 1

#: Reserved, nothing started yet. The only phase at which zero side effects exist.
PHASE_PLANNED = "planned"
#: At least one `agent start` has been issued for this action.
PHASE_LAUNCHING = "launching"
#: Every launch is done; the bounded health probe is running.
PHASE_HEALTH_CHECK = "health_check"
#: The probe said not-all-healthy. This action's fresh launches are owed a compensation,
#: which only the explicit public rollback rail may perform (Answer j#80991).
PHASE_ROLLBACK_OWED = "rollback_owed"
#: The probe said all-healthy; the success record is not durable yet.
PHASE_SUCCESS_OWED = "success_owed"
#: Terminal: an explicit rollback proved this action's participants absent.
PHASE_COMPLETED_ROLLED_BACK = "completed_rolled_back"
#: Terminal: the action came up healthy and said so durably.
PHASE_COMPLETED_SUCCESS = "completed_success"

PHASES: frozenset[str] = frozenset(
    {
        PHASE_PLANNED,
        PHASE_LAUNCHING,
        PHASE_HEALTH_CHECK,
        PHASE_ROLLBACK_OWED,
        PHASE_SUCCESS_OWED,
        PHASE_COMPLETED_ROLLED_BACK,
        PHASE_COMPLETED_SUCCESS,
    }
)

#: Phases after which nothing more is owed. A terminal action is replay-safe: asking to
#: roll it back again is answered from the record, never by closing something again.
TERMINAL_PHASES: frozenset[str] = frozenset(
    {PHASE_COMPLETED_ROLLED_BACK, PHASE_COMPLETED_SUCCESS}
)

STORE_ABSENT = "absent"
STORE_PRESENT = "present"
STORE_DAMAGED = "damaged"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS startup_actions (
    action_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    providers TEXT NOT NULL,
    phase TEXT NOT NULL,
    revision INTEGER NOT NULL,
    participants TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""
_STORE_NONCE_KEY = "store_nonce"

#: The exact key set of a persisted participant (review j#81202 R6-F1). A read-back
#: participant must carry exactly these — no missing key defaulted, no extra key ignored.
_PARTICIPANT_KEYS = frozenset({"role", "assigned_name", "locator", "receipt", "closed"})

#: The table/column shape that IS part of schema version 1 (review j#81092 R3-F1). A store
#: at the right `user_version` but missing any of these is a partial schema and fails
#: closed, rather than raising `no such table` / `no such column` out of a read.
_EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "startup_actions": (
        "action_id",
        "workspace_id",
        "lane_id",
        "providers",
        "phase",
        "revision",
        "participants",
        "reserved_at",
        "updated_at",
    ),
    "store_meta": ("key", "value"),
}


class StartupTransactionError(RuntimeError):
    """The startup transaction authority is unusable / was asked for something invalid."""


class StartupTransactionBusy(StartupTransactionError):
    """Another startup transaction holds this authority. Never wait, never steal."""


def _norm(value: object) -> str:
    return str(value or "").strip()


def _close_quietly(conn) -> None:
    """Close a connection during error cleanup, swallowing a secondary close failure.

    Used only on the failure path (an exception is already propagating): a close error
    here must not mask the original fault. The success path closes through
    :meth:`StartupTransactionFence._connection`, which DOES surface a close failure.
    """
    if conn is None:
        return
    try:
        conn.close()
    except (sqlite3.DatabaseError, OSError):
        pass


def _close_os_fd_quietly(fd) -> None:
    """Close an OS fd during error cleanup, swallowing a secondary close failure.

    The lock's acquire-failure path (review j#81202 R6-F2): a close error while an acquire
    failure is already propagating must not mask it with a raw OSError.
    """
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def startup_transaction_fence_path(home: Optional[Path] = None) -> Path:
    return Path(home or mozyo_bridge_home()) / STARTUP_TRANSACTION_FENCE_FILENAME


def canonical_providers(providers: Sequence[str]) -> tuple[str, ...]:
    """The requested provider set, canonicalised. Order is not identity; membership is."""
    return tuple(sorted({_norm(p) for p in providers if _norm(p)}))


@dataclass(frozen=True)
class StartupUnit:
    """What one session-start action is scoped to (Answer j#80989 Q3).

    The requested provider *set* is part of the unit: a run asked for (claude, codex) is
    not the same action as a run asked for (codex), even in the same lane — and a rollback
    must never generalise from one to the other.
    """

    workspace_id: str
    lane_id: str
    providers: tuple[str, ...]

    def canonical(self) -> "StartupUnit":
        return StartupUnit(
            workspace_id=_norm(self.workspace_id),
            lane_id=_norm(self.lane_id),
            providers=canonical_providers(self.providers),
        )


def startup_action_id(unit: StartupUnit, nonce: str) -> str:
    """The immutable identity of one session-start invocation.

    The unit alone is NOT an identity: the same operator re-running the same command in the
    same lane is a *different* action, and letting the second inherit the first's record is
    how an old completion gets applied to a live pair. The ``nonce`` is what separates
    them; it is supplied by the caller (and injected by tests) rather than minted here, so
    this stays pure and the invocation stays the single place a new identity is born.
    """
    canonical = unit.canonical()
    values = (
        canonical.workspace_id,
        canonical.lane_id,
        ",".join(canonical.providers),
        _norm(nonce),
    )
    if not all(values):
        raise ValueError(
            "a startup action identity requires an exact workspace, lane, requested "
            "provider set, and nonce"
        )
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return "startup-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Participant:
    """One launch this action actually performed, as the launcher observed it.

    ``receipt`` is the launcher's own evidence (the landed workspace / tab it verified
    before trusting the locator). It is kept because a rollback must be able to show that
    the pane it is about to close is the pane THIS action started — not merely one whose
    durable name matches.
    """

    role: str
    assigned_name: str
    locator: str
    receipt: str = ""
    closed: bool = False

    def as_payload(self) -> dict:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "receipt": self.receipt,
            "closed": self.closed,
        }

    @staticmethod
    def from_payload(raw: dict) -> "Participant":
        return Participant(
            role=_norm(raw.get("role")),
            assigned_name=_norm(raw.get("assigned_name")),
            locator=_norm(raw.get("locator")),
            receipt=_norm(raw.get("receipt")),
            closed=bool(raw.get("closed")),
        )

    @staticmethod
    def strict_from_payload(raw: object, action_id: str) -> "Participant":
        """Decode a participant that was READ BACK from the authority (fail-closed).

        Distinct from :meth:`from_payload`, which is the lenient path for a payload this
        process just built. A participant read from disk is an authority record, so every
        field must be the type the schema promised (review j#81166 R5-F1): a missing key,
        a non-string role/name/locator/receipt, or a non-boolean ``closed`` is a corrupt
        authority, not a value to coerce. ``closed="false"`` becoming ``True`` was the
        exact coercion that let a corrupt row read as "already closed" and vanish a
        rollback debt into a terminal completion.
        """
        if not isinstance(raw, dict):
            raise StartupTransactionError(
                f"startup action {action_id!r} has a non-object participant "
                f"({type(raw).__name__}); the authority row is malformed"
            )
        # EXACT key set — no missing, no extra (review j#81202 R6-F1). Defaulting a missing
        # receipt to "" or a missing closed to False was still coercion: it turned a
        # participant the schema never fully recorded into a plausible one, and an extra key
        # is a shape this version does not write. A read-back participant is byte-exact or
        # it is a malformed authority.
        if set(raw) != _PARTICIPANT_KEYS:
            raise StartupTransactionError(
                f"startup action {action_id!r} participant keys {sorted(raw)} are not the "
                f"exact set {sorted(_PARTICIPANT_KEYS)}; the authority row is malformed"
            )
        for key in ("role", "assigned_name", "locator", "receipt"):
            value = raw[key]
            if not isinstance(value, str):
                raise StartupTransactionError(
                    f"startup action {action_id!r} participant {key} is not a string "
                    f"({value!r}); the authority row is malformed"
                )
        for key in ("role", "assigned_name", "locator"):
            value = raw[key]
            # An identity field is a canonical token: non-empty AND already stripped. A
            # whitespace-wrapped value is a corrupt authority, not a value to normalize —
            # stripping it to match a live pane was the R6-F1 coercion. `strip()` here is a
            # VALIDATION comparison, never a mutation: the stored bytes are used verbatim.
            if not value or value != value.strip():
                raise StartupTransactionError(
                    f"startup action {action_id!r} participant {key} is empty or has "
                    f"surrounding whitespace ({value!r}); the authority row is malformed"
                )
        if not isinstance(raw["closed"], bool):
            raise StartupTransactionError(
                f"startup action {action_id!r} participant closed is not a boolean "
                f"({raw['closed']!r}); refusing to coerce a corrupt flag into a verdict"
            )
        # The read-back identity bytes are preserved verbatim — no _norm strip. A
        # whitespace-wrapped locator is a DIFFERENT authority value, and stripping it to
        # match a live pane (R6-F1) is exactly the coercion this contract forbids.
        return Participant(
            role=raw["role"],
            assigned_name=raw["assigned_name"],
            locator=raw["locator"],
            receipt=raw["receipt"],
            closed=raw["closed"],
        )


@dataclass(frozen=True)
class StartupAction:
    """The durable state of one session-start invocation."""

    action_id: str
    unit: StartupUnit
    phase: str
    revision: int = 1
    participants: tuple[Participant, ...] = ()
    reserved_at: str = ""
    updated_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def participant_for(self, role: str) -> Optional[Participant]:
        for participant in self.participants:
            if participant.role == _norm(role):
                return participant
        return None

    def as_payload(self) -> dict:
        return {
            "action_id": self.action_id,
            "workspace_id": self.unit.workspace_id,
            "lane_id": self.unit.lane_id,
            "providers": list(self.unit.providers),
            "phase": self.phase,
            "revision": self.revision,
            "participants": [p.as_payload() for p in self.participants],
            "reserved_at": self.reserved_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class StoreShape:
    """Absent / present / damaged — never collapsed to a boolean."""

    state: str
    present_artifacts: tuple[str, ...] = ()

    @property
    def absent(self) -> bool:
        return self.state == STORE_ABSENT


class StartupTransactionFence:
    """The home-scoped startup-action authority. Construction touches no filesystem."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = path or startup_transaction_fence_path(home)
        # Re-entrancy is per INSTANCE and is not a weakening of the exclusion. A rollback
        # holds the lock across its external close and then records what it proved; those
        # inner writes are the same holder, and flock — which keys on the open file
        # description, not the process — would otherwise refuse this fence its own lock and
        # report `busy` to itself. A *different* holder (another instance, another process)
        # still gets a hard refusal, which is the property that matters.
        self._lock_fd: Optional[int] = None
        self._lock_depth = 0

    @property
    def seal_path(self) -> Path:
        return self.path.with_name(self.path.name + STARTUP_TRANSACTION_FENCE_SEAL_SUFFIX)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + STARTUP_TRANSACTION_FENCE_LOCK_SUFFIX)

    @property
    def temp_path(self) -> Path:
        return self.path.with_name(self.path.name + STARTUP_TRANSACTION_FENCE_TEMP_SUFFIX)

    def _artifact_paths(self) -> tuple[tuple[str, Path], ...]:
        # The sidecars and the bootstrap temp are evidence too: a crash can leave one with
        # the main DB gone, and an inventory blind to that would call the wreckage "absent"
        # and bootstrap over a lost authority (#13892 j#80526 / review j#80523 R3-F5).
        # The lock file is excluded — taking a lock is not evidence of an action.
        return (
            ("db", self.path),
            ("wal", self.path.with_name(self.path.name + "-wal")),
            ("shm", self.path.with_name(self.path.name + "-shm")),
            ("journal", self.path.with_name(self.path.name + "-journal")),
            ("seal", self.seal_path),
            ("temp", self.temp_path),
        )

    @staticmethod
    def _artifact_present(path: Path) -> bool:
        """Probe one artifact with a raw ``lstat``, three-valued (review j#81202 R6-F3.1).

        ``os.path.lexists`` is NOT usable here: it swallows the ``lstat`` ``OSError``
        internally and returns ``False``, so a permission-denied artifact reads as absent —
        and an absent store bootstraps / an absent action is "unknown", both of which act
        on a store we could not actually read. ``lstat`` directly lets not-found
        (``FileNotFoundError`` / ``NotADirectoryError`` → genuinely absent) be told apart
        from unreadable (any other ``OSError`` → the store is there but unprobeable), which
        the caller raises as a damaged authority.
        """
        try:
            os.lstat(path)
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False

    def store_shape(self) -> StoreShape:
        """Classify the artifact set (absent / present / damaged), fail-closed on unreadable.

        The probe distinguishes genuinely-absent from unreadable via a raw ``lstat``
        (review j#81202 R6-F3.1): this runs BEFORE the connect/lock guards on every read
        and reserve, and an unprobeable artifact must never read as absent (which would
        bootstrap over it, or answer "action unknown" for a store that is really there).
        """
        try:
            present = tuple(
                name for name, p in self._artifact_paths() if self._artifact_present(p)
            )
        except OSError as exc:
            raise StartupTransactionError(
                f"the startup transaction authority {self.path} artifacts could not be "
                f"probed ({exc}); fail closed rather than read an unprobeable store as "
                "absent"
            ) from exc
        if not present:
            return StoreShape(state=STORE_ABSENT)
        row_bearing = {"db", "wal", "shm", "journal"} & set(present)
        if not row_bearing or "temp" in present or "seal" not in present:
            # A half-built / half-deleted set: something WAS here. Never guess which half.
            return StoreShape(state=STORE_DAMAGED, present_artifacts=present)
        return StoreShape(state=STORE_PRESENT, present_artifacts=present)

    # -- lifecycle ---------------------------------------------------------

    def _create_fresh(self, nonce: str) -> None:
        """Stage in a temp, rename in, seal LAST (so an interrupted build reads damaged)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.temp_path
        if temp.exists():
            temp.unlink()
        conn = sqlite3.connect(temp, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (_STORE_NONCE_KEY, nonce),
            )
            conn.execute(
                f"PRAGMA user_version = {STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION}"
            )
        finally:
            conn.close()
        os.replace(temp, self.path)
        self.seal_path.write_text(nonce, encoding="utf-8")

    def _read_seal_nonce(self) -> Optional[str]:
        """The seal's nonce, or ``None`` when it cannot be read as one.

        ``ValueError`` is caught alongside ``OSError`` deliberately: a seal holding
        non-UTF-8 bytes raises ``UnicodeDecodeError``, which is a ``ValueError`` and would
        otherwise escape an ``OSError``-only guard.
        """
        try:
            value = self.seal_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        return value or None

    @staticmethod
    def _db_nonce(conn: sqlite3.Connection) -> Optional[str]:
        row = conn.execute(
            "SELECT value FROM store_meta WHERE key = ?", (_STORE_NONCE_KEY,)
        ).fetchone()
        if row is None:
            return None
        value = row[0]
        # The nonce is text, and it is compared to a text seal — NOT coerced (review j#81202
        # R6-F3.2). `str(b"abc")` is `"b'abc'"`, which a seal literally holding `b'abc'`
        # would then MATCH, letting a BLOB-nonce store pass its own identity check. A
        # non-text nonce is a corrupt authority, surfaced by the caller's guard as
        # unreadable rather than silently made to match. (sqlite3.DatabaseError from the
        # query itself is normalized by the caller's `_open` / `_verify` guard.)
        if not isinstance(value, str):
            raise StartupTransactionError(
                "the startup transaction store nonce is not text "
                f"({type(value).__name__}); the authority identity is corrupt"
            )
        return value

    def _verify_shape(self, conn: sqlite3.Connection) -> None:
        """The table/column shape IS part of the schema (review j#81092 R3-F1).

        A store at the right ``user_version`` but missing the ``startup_actions`` table (or
        a column of it) is a partial schema, which `managed-state-model.md` requires to
        fail closed byte-unchanged — not to raise ``no such table`` out of a read. Checking
        the shape here, under the same normalized guard as the version/seal, is what turns
        a partial store into a structured `rollback_authority_unavailable` instead of a raw
        ``OperationalError`` escaping the public rail.
        """
        for table, expected in _EXPECTED_COLUMNS.items():
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            actual = {str(row[1]) for row in rows}
            if not actual:
                raise StartupTransactionError(
                    f"the startup transaction authority {self.path} is at the right schema "
                    f"version but is missing the {table!r} table (partial schema); fail "
                    "closed rather than read an incomplete authority"
                )
            missing = set(expected) - actual
            if missing:
                raise StartupTransactionError(
                    f"the startup transaction authority {self.path} {table!r} table is "
                    f"missing columns {sorted(missing)} (partial schema); fail closed"
                )

    def _verify(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        """Prove an open connection is a complete, identity-matched authority (fail-closed).

        Three checks, all normalized to :class:`StartupTransactionError` by the callers'
        shared guard: the schema *version*, the table/column *shape* (R3-F1), and the
        seal/DB-nonce *identity* (R1-F7). The schema check alone is not an identity check —
        a store swapped for another valid-schema store passed it — and neither is enough
        without the shape, because a right-version store can still be missing its tables.
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION:
            raise StartupTransactionError(
                f"startup transaction store schema {version!r} is not this runtime's "
                f"{STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION}; fail closed rather than "
                "read an unknown shape"
            )
        self._verify_shape(conn)
        seal = self._read_seal_nonce()
        if seal is None:
            raise StartupTransactionError(
                f"the startup transaction authority {self.path} has no readable "
                "identity seal; the actions it holds cannot be trusted"
            )
        if self._db_nonce(conn) != seal:
            raise StartupTransactionError(
                f"the startup transaction authority {self.path} does not match its "
                "identity seal (store replacement); fail closed rather than close "
                "panes on the strength of another store's record"
            )
        return conn

    def _open(self, uri: str) -> sqlite3.Connection:
        """Open + verify a connection, normalizing EVERY unreadable shape (fail-closed).

        The one funnel for both read and write connections. Normalizing here — not just at
        `PRAGMA user_version` — is the R3-F1 lesson: the same authority-unreadable face has
        to cover the shape read and (via the callers) the row read and decode too, or a
        partial store escapes the public rail's "never raises" contract as a raw
        ``OperationalError`` / ``JSONDecodeError``. `mode` is caller-chosen and always
        existing-only (`ro` / `rw`, never `rwc`): a read must never *create* the authority
        it is checking (R3-F1), and a write only ever runs after `reserve` has bootstrapped.
        """
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            conn.execute("PRAGMA busy_timeout = 2000")
            return self._verify(conn)
        except StartupTransactionError:
            _close_quietly(conn)
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            _close_quietly(conn)
            raise StartupTransactionError(
                f"the startup transaction authority {self.path} is unreadable ({exc}); "
                "fail closed rather than treat an unreadable store as an empty one"
            ) from exc

    @contextlib.contextmanager
    def _connection(self, mode: str):
        """Open a connection, yield it, and GUARANTEE a normalized close (R6-F3.3).

        The single funnel that fixes the whole "connection close" surface at once instead
        of one call site at a time: every read / reserve / write goes through here, so a
        ``close()`` that raises is normalized in ONE place rather than leaking raw from
        each ``finally``. A ``close`` failure never overwrites the body's own exception
        (that is the real fault); it is only surfaced when the body itself succeeded.
        """
        conn = self._connect_ro() if mode == "ro" else self._connect_rw()
        body_failed = False
        try:
            yield conn
        except BaseException:
            body_failed = True
            raise
        finally:
            try:
                conn.close()
            except (sqlite3.DatabaseError, OSError) as exc:
                if not body_failed:
                    raise StartupTransactionError(
                        f"the startup transaction authority {self.path} connection could "
                        f"not be closed ({exc}); fail closed"
                    ) from exc

    def _connect_ro(self) -> sqlite3.Connection:
        """A strict read-only, existing-only connection (never fabricates the store)."""
        return self._open(f"file:{self.path}?mode=ro")

    def _connect_rw(self) -> sqlite3.Connection:
        """A read-write, existing-only connection (a write runs only after reserve)."""
        return self._open(f"file:{self.path}?mode=rw")

    def _hold(self):
        """Take the exclusive, non-blocking advisory lock (contention refuses, never waits)."""
        return _FenceLock(self)

    # -- reads -------------------------------------------------------------

    def read(self, action_id: str) -> Optional[StartupAction]:
        """Read one action. ``None`` = no such record. Raises when the store is unusable."""
        shape = self.store_shape()
        if shape.absent:
            return None
        if shape.state == STORE_DAMAGED:
            raise StartupTransactionError(
                "the startup transaction store is damaged (a partial artifact set); "
                "refusing to read an authority whose shape cannot be trusted"
            )
        with self._connection("ro") as conn:
            try:
                row = conn.execute(
                    "SELECT action_id, workspace_id, lane_id, providers, phase, revision,"
                    " participants, reserved_at, updated_at FROM startup_actions"
                    " WHERE action_id = ?",
                    (_norm(action_id),),
                ).fetchone()
                # The row read AND its decode are inside the guard (R3-F1 / R4-F1): a query
                # against a partial schema raises OperationalError, and a malformed cell
                # raises StartupTransactionError from `_row_to_action` — the latter passes
                # through untouched, the former normalizes here. The connection close is
                # guaranteed and normalized by `_connection` (R6-F3.3).
                from mozyo_bridge.core.state.startup_transaction_row import (
                    _row_to_action,
                )

                return _row_to_action(row) if row else None
            except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                raise StartupTransactionError(
                    f"the startup transaction authority {self.path} could not be read "
                    f"({exc}); fail closed rather than treat it as empty"
                ) from exc

    # -- writes ------------------------------------------------------------

    def reserve(self, unit: StartupUnit, nonce: str) -> StartupAction:
        """Mint + persist a new action BEFORE its first side effect (bootstraps if absent).

        Bootstrapping here is safe precisely because the identity is new: there is no prior
        record for this action to forget. (A rollback against an absent store is the
        opposite case and refuses — see :meth:`read` callers.)
        """
        canonical = unit.canonical()
        action_id = startup_action_id(canonical, nonce)
        now = _utc_now()
        with self._hold():
            shape = self.store_shape()
            if shape.state == STORE_DAMAGED:
                raise StartupTransactionError(
                    "the startup transaction store is damaged (a partial artifact set); "
                    "refusing to reserve an action against it — nothing was started"
                )
            try:
                if shape.absent:
                    self._create_fresh(hashlib.sha256(now.encode("utf-8")).hexdigest())
            except (sqlite3.DatabaseError, OSError) as exc:
                # A bootstrap write that fails is a reserve that did not happen — surface it
                # structured, before any side effect, exactly like every other write path
                # (review j#81122 R4-F2).
                raise StartupTransactionError(
                    f"the startup transaction authority {self.path} could not be created "
                    f"({exc}); nothing was started"
                ) from exc
            with self._connection("rw") as conn:
                try:
                    existing = conn.execute(
                        "SELECT phase FROM startup_actions WHERE action_id = ?",
                        (action_id,),
                    ).fetchone()
                    if existing is not None:
                        raise StartupTransactionError(
                            f"startup action {action_id!r} already exists (phase "
                            f"{existing[0]!r}); a nonce must never be reused — refusing to "
                            "reserve over a recorded action"
                        )
                    conn.execute(
                        "INSERT INTO startup_actions (action_id, workspace_id, lane_id,"
                        " providers, phase, revision, participants, reserved_at,"
                        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            action_id,
                            canonical.workspace_id,
                            canonical.lane_id,
                            ",".join(canonical.providers),
                            PHASE_PLANNED,
                            1,
                            json.dumps([]),
                            now,
                            now,
                        ),
                    )
                except StartupTransactionError:
                    raise
                except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                    # The SELECT/INSERT are normalized (R4-F2); the connection close is
                    # guaranteed and normalized by `_connection` (R6-F3.3). reserve is the
                    # reserve-before-effect anchor — it must fail closed, not leak internals.
                    raise StartupTransactionError(
                        f"the startup transaction authority {self.path} could not record "
                        f"the reserve ({exc}); nothing was started"
                    ) from exc
        return StartupAction(
            action_id=action_id,
            unit=canonical,
            phase=PHASE_PLANNED,
            participants=(),
            reserved_at=now,
            updated_at=now,
        )

    def record_participant(self, action_id: str, participant: Participant) -> StartupAction:
        """Append a launch this action performed. Called immediately after each start."""
        with self._hold():
            action = self._require(action_id)
            if action.terminal:
                raise StartupTransactionError(
                    f"startup action {action_id!r} is {action.phase!r}; refusing to add a "
                    "participant to a completed action"
                )
            if action.participant_for(participant.role) is not None:
                raise StartupTransactionError(
                    f"startup action {action_id!r} already has a {participant.role!r} "
                    "participant; one action starts a role at most once"
                )
            merged = action.participants + (participant,)
            self._write(action_id, phase=PHASE_LAUNCHING, participants=merged)
            return self._require(action_id)

    def set_phase(self, action_id: str, phase: str) -> StartupAction:
        """Advance the action's phase. Terminal phases are write-once."""
        if phase not in PHASES:
            raise StartupTransactionError(f"unknown startup action phase {phase!r}")
        with self._hold():
            action = self._require(action_id)
            if action.terminal:
                raise StartupTransactionError(
                    f"startup action {action_id!r} is already {action.phase!r}; a terminal "
                    "phase is written once and never revised"
                )
            self._write(action_id, phase=phase, participants=action.participants)
            return self._require(action_id)

    def mark_closed(self, action_id: str, role: str) -> StartupAction:
        """Record that a participant's pane was proven closed by a rollback."""
        with self._hold():
            action = self._require(action_id)
            updated = tuple(
                Participant(
                    role=p.role,
                    assigned_name=p.assigned_name,
                    locator=p.locator,
                    receipt=p.receipt,
                    closed=True if p.role == _norm(role) else p.closed,
                )
                for p in action.participants
            )
            self._write(action_id, phase=action.phase, participants=updated)
            return self._require(action_id)

    # -- internals ---------------------------------------------------------

    def _require(self, action_id: str) -> StartupAction:
        action = self.read(action_id)
        if action is None:
            raise StartupTransactionError(
                f"no startup action {action_id!r} in this store; refusing to act without "
                "the record that proves what was started"
            )
        return action

    def _write(self, action_id: str, *, phase: str, participants) -> None:
        with self._connection("rw") as conn:
            try:
                conn.execute(
                    "UPDATE startup_actions SET phase = ?, participants = ?, updated_at ="
                    " ?, revision = revision + 1 WHERE action_id = ?",
                    (
                        phase,
                        json.dumps([p.as_payload() for p in participants]),
                        _utc_now(),
                        _norm(action_id),
                    ),
                )
            except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                # Write normalized here; connection close guaranteed + normalized by
                # `_connection` (R6-F3.3).
                raise StartupTransactionError(
                    f"the startup transaction authority {self.path} could not be written "
                    f"({exc}); fail closed"
                ) from exc


class _FenceLock:
    """The exclusive, non-blocking advisory lock, held across an external close.

    ``BEGIN IMMEDIATE`` only spans statements, so it cannot cover a ``herdr pane close``
    subprocess. This can (#13892's pattern). Contention is a refusal — never a wait, never
    a steal: two rollbacks racing the same panes is exactly what must not happen.
    """

    def __init__(self, fence: StartupTransactionFence) -> None:
        self._fence = fence
        self._nested = False

    def __enter__(self) -> "_FenceLock":
        fence = self._fence
        if fence._lock_depth > 0:
            # Already held by this exact holder: nest without re-acquiring.
            fence._lock_depth += 1
            self._nested = True
            return self
        # Acquire lifecycle (review j#81202 R6-F2). mkdir / open / flock are all inside the
        # guard, and the failure-path `os.close(fd)` is done through `_close_os_fd_quietly`
        # so a SECONDARY close failure during cleanup cannot mask the acquire failure with a
        # raw OSError. flock contention (a live fd + EAGAIN/EACCES) is `StartupTransactionBusy`;
        # a failed open — where fd is None — is authority-unavailable, never mistaken for it.
        fd = None
        try:
            lock = fence.lock_path
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            contention = (
                fd is not None
                and getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN)
            )
            _close_os_fd_quietly(fd)
            if contention:
                raise StartupTransactionBusy(
                    "another startup transaction holds this authority; refusing to wait "
                    "or steal it — nothing was started or closed"
                ) from exc
            raise StartupTransactionError(
                f"could not take the startup transaction lock ({exc}); fail closed"
            ) from exc
        fence._lock_fd = fd
        fence._lock_depth = 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        fence = self._fence
        if self._nested:
            fence._lock_depth -= 1
            return
        if fence._lock_fd is None:
            return
        fd = fence._lock_fd
        fence._lock_fd = None
        fence._lock_depth = 0
        # Release lifecycle (review j#81202 R6-F2): unlock and close are BOTH normalized,
        # the fd is always closed, and a release failure is surfaced only when the body
        # succeeded (`exc_type is None`). A body exception is the real fault and must not be
        # overwritten by a secondary unlock/close error — so on the body-failure path the
        # release error is swallowed and the body exception propagates unchanged.
        release_error = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as unlock_exc:
            release_error = unlock_exc
        try:
            os.close(fd)
        except OSError as close_exc:
            release_error = release_error or close_exc
        if release_error is not None and exc_type is None:
            raise StartupTransactionError(
                f"could not release the startup transaction lock ({release_error}); "
                "fail closed"
            ) from release_error


__all__ = (
    "PHASES",
    "PHASE_COMPLETED_ROLLED_BACK",
    "PHASE_COMPLETED_SUCCESS",
    "PHASE_HEALTH_CHECK",
    "PHASE_LAUNCHING",
    "PHASE_PLANNED",
    "PHASE_ROLLBACK_OWED",
    "PHASE_SUCCESS_OWED",
    "STARTUP_TRANSACTION_FENCE_FILENAME",
    "STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION",
    "STORE_ABSENT",
    "STORE_DAMAGED",
    "STORE_PRESENT",
    "TERMINAL_PHASES",
    "Participant",
    "StartupAction",
    "StartupTransactionBusy",
    "StartupTransactionError",
    "StartupTransactionFence",
    "StartupUnit",
    "StoreShape",
    "canonical_providers",
    "startup_action_id",
    "startup_transaction_fence_path",
)
