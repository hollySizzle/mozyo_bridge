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
import hashlib
import json
import re
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.startup_transaction_fence_lock import FenceLock as _FenceLock
from mozyo_bridge.shared.paths import mozyo_bridge_home

STARTUP_TRANSACTION_FENCE_FILENAME = "startup-transaction-fence.sqlite"
STARTUP_TRANSACTION_FENCE_SEAL_SUFFIX = ".seal"
STARTUP_TRANSACTION_FENCE_LOCK_SUFFIX = ".lock"
STARTUP_TRANSACTION_FENCE_TEMP_SUFFIX = ".tmp"
#: The version a NORMAL runtime still creates. A fresh store is v1 so an older peer can
#: still read it; mixed-runtime homes are supported for the whole legacy period (j#96936).
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


from mozyo_bridge.core.state.startup_action_capability import (  # noqa: F401
    CAPABILITIES,
    CAPABILITY_IDENTITY_RECEIPT,
    CAPABILITY_LEGACY,
    IDENTITY_MANIFEST_PROTOCOL,
    REASON_RECEIPT_REQUIREMENT_UNAVAILABLE,
    IdentityManifest,
    IdentityManifestSlot,
    StartupTransactionBusy,
    StartupTransactionError,
    _IDENTITY_MANIFEST_COLUMNS,
    _IDENTITY_MANIFEST_SQL,
    _IDENTITY_MANIFEST_TABLE,
    action_capability,
    read_identity_manifest as _read_identity_manifest,
    resolve_reserve_identity,
    resolve_reserve_identity as _resolve_reserve_identity,
    reserve_or_replay as _reserve_or_replay,
    REASON_OFFLINE_UPGRADE_REQUIRED,
    STARTUP_TRANSACTION_FENCE_SUPPORTED_VERSIONS,
    require_v2_for_tagged_reserve as _require_v2_for_tagged_reserve,
    verify_supported_version as _verify_supported_version,
    verify_v2_manifest_shape as _verify_v2_manifest_shape,
    requires_identity_receipt,
    startup_action_id,
    startup_action_id_matching,
)


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


def _rollback_quietly(conn) -> None:
    """Roll back a co-commit that failed mid-transaction, swallowing a secondary failure.

    The primary fault is what the caller must see; a rollback that itself fails must not
    mask it. An unrolled-back BEGIN is closed by `_connection` anyway, which discards it.
    """
    if conn is None:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
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
    receipt: str = field(default="", repr=False)
    closed: bool = False

    def as_payload(self) -> dict:
        """Return the public projection; private pane receipts are deliberately omitted."""
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "closed": self.closed,
        }

    def as_authority_payload(self) -> dict:
        """Return the exact private on-disk authority shape."""
        return {**self.as_payload(), "receipt": self.receipt}

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

    def as_authority_payload(self) -> dict:
        """Return the private fingerprint/storage projection including receipts."""
        return {
            **self.as_payload(),
            "participants": [p.as_authority_payload() for p in self.participants],
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
        except FileNotFoundError:
            # ONLY a genuine not-found is absence (review j#81224 R7-F3). NotADirectoryError
            # (a path component is a file), PermissionError, and every other OSError mean
            # the namespace is unreadable — NOT that the artifact is absent — so they
            # propagate to store_shape's guard as a damaged/unreadable authority rather than
            # collapsing to "absent" (which would bootstrap over it, or answer unknown).
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
        """The seal's nonce as the writer wrote it, or ``None`` when unreadable.

        The bytes are NOT stripped (review j#81224 R7-F3): the seal is one half of the
        identity comparison, and normalizing it is the same coercion the DB-nonce side
        already forbids — a nonce wrapped in whitespace/newline would otherwise MATCH a
        clean stored nonce and let a corrupt authority pass its own identity check. The
        writer emits the exact nonce (`_create_fresh`), so an exact compare is what a
        genuine seal survives. ``ValueError`` is caught with ``OSError`` because a non-UTF-8
        seal raises ``UnicodeDecodeError`` (a ``ValueError``).
        """
        try:
            value = self.seal_path.read_text(encoding="utf-8")
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

        Four checks, normalized by the callers' shared guard: schema *version* (v1 or v2,
        j#96936), table/column *shape* (R3-F1), v2's required manifest table, and the
        seal/DB-nonce *identity* (R1-F7). Version alone is not identity — a store swapped
        for another valid-schema store passed it.
        """
        _verify_supported_version(conn)
        self._verify_shape(conn)
        _verify_v2_manifest_shape(conn)
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

    def read_identity_manifest(self, action_id: str) -> "Optional[IdentityManifest]":
        """The launch manifest a TAGGED action is content-bound to (fail-closed).

        Body in :func:`...startup_action_capability.read_identity_manifest`.
        """
        return _read_identity_manifest(self, action_id)

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
                # Audit j#96946 C5: query the RAW id. `_norm` stripped padding and matched
                # the canonical row, laundering an id the pure classifier refuses.
                row = conn.execute(
                    "SELECT action_id, workspace_id, lane_id, providers, phase, revision,"
                    " participants, reserved_at, updated_at FROM startup_actions"
                    " WHERE action_id = ?",
                    (action_id if isinstance(action_id, str) else "",),
                ).fetchone()
                # The row read AND its decode are inside the guard (R3-F1 / R4-F1): a query
                # against a partial schema raises OperationalError, and a malformed cell
                # raises StartupTransactionError from `_row_to_action` — the latter passes
                # through untouched, the former normalizes here. The connection close is
                # guaranteed and normalized by `_connection` (R6-F3.3).
                from mozyo_bridge.core.state.startup_transaction_row import (
                    _row_to_action,
                )

                action = _row_to_action(row) if row else None
            except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                raise StartupTransactionError(
                    f"the startup transaction authority {self.path} could not be read "
                    f"({exc}); fail closed rather than treat it as empty"
                ) from exc
        if action is not None:
            # Audit j#96928 F2: rollback / status / current-action all consume THIS read, so
            # a tagged action must prove its manifest obligation here or it can be spent
            # without anyone looking at it. Legacy short-circuits and is unchanged.
            self.read_identity_manifest(action.action_id)
        return action

    @staticmethod
    def _actions_from_connection(conn: sqlite3.Connection) -> tuple[StartupAction, ...]:
        """Strictly decode every startup action from one SQLite snapshot."""
        from mozyo_bridge.core.state.startup_transaction_row import _row_to_action

        rows = conn.execute(
            "SELECT action_id, workspace_id, lane_id, providers, phase, revision,"
            " participants, reserved_at, updated_at FROM startup_actions"
            " ORDER BY action_id"
        ).fetchall()
        return tuple(_row_to_action(row) for row in rows)

    def read_snapshot(self) -> tuple[StartupAction, ...]:
        """Read the complete action authority, never treating damage as an empty store.

        Genuine absence is the only empty result.  Every present row is strict-decoded and
        every tagged row re-proves its identity manifest before this snapshot is returned.
        The advisory lock makes the row set stable against every conforming writer while it
        is captured; callers still re-read at each external effect edge.
        """
        with self._hold():
            shape = self.store_shape()
            if shape.absent:
                return ()
            if shape.state == STORE_DAMAGED:
                raise StartupTransactionError(
                    "the startup transaction store is damaged (a partial artifact set); "
                    "refusing to read an incomplete action snapshot"
                )
            try:
                with self._connection("ro") as conn:
                    actions = self._actions_from_connection(conn)
                for action in actions:
                    self.read_identity_manifest(action.action_id)
                return actions
            except StartupTransactionError:
                raise
            except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                raise StartupTransactionError(
                    f"the startup transaction authority {self.path} action snapshot is "
                    f"unreadable ({exc}); fail closed rather than treat it as empty"
                ) from exc

    # -- writes ------------------------------------------------------------

    def reserve(
        self,
        unit: StartupUnit,
        nonce: str,
        *,
        manifest: "Optional[IdentityManifest]" = None,
        refuse_nonterminal_slot_overlap: bool = False,
    ) -> StartupAction:
        """Mint + persist a new action BEFORE its first side effect (bootstraps if absent).

        Bootstrapping here is safe precisely because the identity is new: there is no prior
        record for this action to forget. (A rollback against an absent store is the
        opposite case and refuses — see :meth:`read` callers.)

        Passing a ``manifest`` mints a **capability-tagged** action content-bound to it and
        co-commits both rows in one transaction inside this lock; it requires a v2 store
        (j#96917 / j#96936). Omitting it is the pre-#14741 path, byte for byte.
        """
        canonical = unit.canonical()
        action_id, manifest_digest, manifest_payload = _resolve_reserve_identity(
            canonical, nonce, manifest
        )
        now = _utc_now()
        replayed = ""
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
                    if manifest is not None:
                        _require_v2_for_tagged_reserve(conn)
                    if refuse_nonterminal_slot_overlap:
                        actions = self._actions_from_connection(conn)
                        for action in actions:
                            self.read_identity_manifest(action.action_id)
                            if (
                                not action.terminal
                                and action.action_id != action_id
                            ):
                                raise StartupTransactionError(
                                    "a foreign nonterminal startup action exists in this "
                                    "home; refusing the global offline restore reservation "
                                    "before any effect"
                                )
                    replayed = _reserve_or_replay(
                        conn,
                        action_id=action_id,
                        canonical=canonical,
                        phase=PHASE_PLANNED,
                        now=now,
                        manifest=manifest,
                        manifest_digest=manifest_digest,
                        manifest_payload=manifest_payload,
                        nonce=nonce,
                    )
                except StartupTransactionError:
                    _rollback_quietly(conn)
                    raise
                except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                    # Normalized here (R4-F2); the close is guaranteed by `_connection`
                    # (R6-F3.3). reserve is the reserve-before-effect anchor, so it fails
                    # closed rather than leaking internals.
                    _rollback_quietly(conn)
                    raise StartupTransactionError(
                        f"the startup transaction authority {self.path} could not record "
                        f"the reserve ({exc}); nothing was started"
                    ) from exc
        if replayed:
            return self._require(replayed)
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
            # A participant must be a role this action actually requested (review j#81224
            # R7-F2): the requested provider set is part of the action's identity, and a
            # participant outside it would let the rail close a pane this action never
            # started. Enforced on the WRITE side so a corrupt participant never lands.
            if participant.role not in action.unit.providers:
                raise StartupTransactionError(
                    f"startup action {action_id!r} participant role {participant.role!r} is "
                    f"not in the requested provider set {action.unit.providers}; refusing "
                    "to record a role this action did not request"
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
            if action.terminal:
                # A terminal action is answered from the record, never mutated (review
                # j#81224 R7-F1): a stale close arriving after a concurrent rollback
                # completed must not re-write a settled authority.
                raise StartupTransactionError(
                    f"startup action {action_id!r} is {action.phase!r}; refusing to record "
                    "a close against a completed action"
                )
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

    def repin_restored_participant_locator(
        self,
        action_id: str,
        role: str,
        *,
        assigned_name: str,
        expected_locator: str,
        new_locator: str,
    ) -> StartupAction:
        """CAS-move ONE participant's ``locator`` after a server restore (Redmine #15769).

        The participant-side half of the governed restored-pair re-attest (j#108766): a
        deliberate, BOUNDED, field-scoped exception to the "terminal phase is written
        once" rule — locator only, exact-CAS-guarded, ``completed_success`` /
        ``rollback_owed`` only, receipt byte-untouched; only the rebind rail calls it.
        Body + contract: :mod:`...core.state.startup_transaction_restored_repin`.
        """
        from mozyo_bridge.core.state.startup_transaction_restored_repin import (
            repin_restored_participant_locator as _repin,
        )

        return _repin(
            self, action_id, role, assigned_name=assigned_name,
            expected_locator=expected_locator, new_locator=new_locator,
        )

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
                        json.dumps([p.as_authority_payload() for p in participants]),
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
