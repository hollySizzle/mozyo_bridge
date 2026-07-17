"""Receiver-side admission authority for recovery actions (Redmine #13910, design j#80984).

The sweep is at-most-once per dispatch anchor, but its last live read cannot be atomic with the
transport call, so a recovery can arrive at a receiver that has already advanced (#13889 R5-F3).
This store answers the one question the receiver must answer before its first state-changing
effect: **has this exact recovery action already been admitted here?**

Per design answer j#80984 (reconciled authoritative by j#80986) it is deliberately **none of** the
existing state kinds:

- not **workflow truth** — Redmine gates / owner approval stay outside this DB;
- not a **send fence** — :mod:`.dispatch_outbox_fence` reserves a key around one *instantaneous*
  send and reads a lingering ``reserved`` row as crash residue. A receiver's recovery round is not
  a send: its effect is an agent acting over many turns, so folding this key into that fence would
  mean either holding a ``reserved`` row across the round (corrupting a live owner) or re-admitting
  a crashed one (duplicating the effect). j#80984 Disposition 1 rejects that reuse. The pattern is
  borrowed; the store is not (the same conclusion #13892 reached at j#80526);
- not a **completion authority** — see below.

It is an **operational action-idempotency authority**: it records *admission*, nothing else.

**Why there is no ``completed`` state** (j#80984 Disposition 2). A receipt row cannot witness the
round's completion: the effect is performed by an agent across turns, and its completion truth is a
Redmine gate. A ``completed`` column here could only ever hold a *claim* about work this store never
observed — the exact ACK-as-completion conflation
``vibes/docs/logics/ack-completion-receiver-state.md`` prohibits. So the vocabulary is closed at
:data:`RECEIPT_CLAIMED`, and "did the round finish?" is answered where it is true: in the journal.

**Why a claim is never reclaimed** (j#80984 Disposition 4, safety-first). No TTL, no
presumed-dead sweep, no ``recover()`` that silently frees keys. A crash after the claim and before
the effect leaves the row ``claimed`` forever, and the recovery is **not** re-admitted. That is a
real liveness cost and it is stated rather than hidden: retry is an explicit coordinator act — a
NEW durable recovery action anchor recorded with ``retry_of=<prior key>``, whose new journal id
yields a new key and therefore a new row. Nothing here re-issues an action automatically; the
sender's own fence does not either (once ``delivered``, that anchor never re-sends), so a
reclaimable claim would not restore liveness — it would only trade this store's safety away for a
duplicate effect.

**Why store loss fails closed, and why a seal is what makes that true** (review j#81021 F1). A
missing / replaced / nonce-mismatched store is not "nothing was claimed" — it is "this authority
cannot tell you", and admitting on that reading is exactly how a deleted DB re-actuates an
already-admitted recovery. But saying so in a docstring does not make it so: the first cut of this
module *claimed* a lost store fails closed while :meth:`bootstrap` still treated "DB and sidecar
both gone" as a genuine first install — so deleting the pair and bootstrapping re-admitted every
recovery it had recorded. A store cannot tell a fresh install from a total loss by looking at
itself, because in both cases there is nothing to look at.

The separation therefore lives **outside** the pair, in a first-init seal
(:data:`CALLBACK_RECOVERY_RECEIPT_SEAL_SUFFIX`) — the mechanism
:mod:`.callback_publication_fence` arrived at for the identical defect (its R12-F1 / R13-F1), and
the same reasoning applies verbatim here. Two rules carry it:

- the seal is written **before** the store it seals, so the crash window lands on an
  ``initializing`` seal (a store that has never granted, safe to re-mint) rather than on an
  operational store with no seal;
- the whole lifecycle decision runs **under an OS advisory lock, re-reading state after the lock is
  held**. Bootstrap had no exclusion at all: eight concurrent bootstraps produced two winners and
  six raw ``sqlite3.OperationalError``s, which is both a race and a fail-*open* leak — the caller
  never saw a domain error it could interpret as "do not actuate".

Deliberately NOT carried over from the publication fence: its legacy seal spellings. Those exist
because that fence shipped before its seal did. This store has never shipped without one, so there
is no build whose word means something different — inventing a compatibility state for a history
that does not exist would be dead code pretending to be caution.

**Known limit, stated rather than hidden.** A store whose seal is deleted along with the pair, or
one copied from another machine, is indistinguishable from a fresh install. That is not decidable
from local state; it is the same residual the sibling authority records (j#80408).

**Why the row keeps the whole key, not only its digest.** A digest hit is presumed to be the same
action, but a presumption is not a proof. The stored fields are compared against the presented ones,
so a digest collision or a tampered row surfaces as :attr:`ClaimResult.conflict` (fail-closed)
rather than being silently absorbed as a "duplicate" — the failure mode where a *different* recovery
is dropped on the floor and nobody can tell.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

CALLBACK_RECOVERY_RECEIPT_FILENAME = "callback-recovery-receipt.sqlite"
#: The DB-external identity artifact. Sibling convention (:mod:`.dispatch_outbox_fence`): a store
#: whose DB is replaced under a surviving sidecar is a LOSS, not a fresh start.
CALLBACK_RECOVERY_RECEIPT_SIDECAR_SUFFIX = ".anchor"
#: First-init seal: proof this authority has operated here, so a both-absent pair reads as a LOSS
#: rather than a fresh install (review j#81021 F1; the mechanism of `.callback_publication_fence`).
CALLBACK_RECOVERY_RECEIPT_SEAL_SUFFIX = ".sealed"
#: The advisory-lock file for the lifecycle decision. Separate from the DB so the lock survives a
#: DB replacement, and so taking it never creates the authority itself.
CALLBACK_RECOVERY_RECEIPT_LOCK_SUFFIX = ".lock"
CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION = 1

#: SQLite writes rows to these alongside the DB. A live journal / WAL beside a deleted DB IS a
#: claim, not an absence — so presence here counts as "a store is here" (publication fence R17-F2).
_SQLITE_AUXILIARY_SUFFIXES = ("-journal", "-wal", "-shm")

# --- seal lifecycle ---------------------------------------------------------
#: A store is being minted; it has never admitted anything, so re-minting it is safe.
SEAL_INITIALIZING = "initializing"
#: This authority has admitted here. A missing store is now a LOSS and must be restored.
SEAL_OPERATIONAL = "operational"
#: The seal exists but cannot be trusted (unknown content / version / unreadable). NEVER re-mint.
SEAL_INVALID = "invalid_or_unreadable"
#: Sentinel: the seal genuinely does not exist.
SEAL_ABSENT = "absent"

_SEAL_FORMAT = "mozyo-callback-recovery-receipt-seal"
_SEAL_VERSION_TOKEN = "v1"

#: The recovery round was admitted here. TERMINAL: never reclaimed, never superseded, and never
#: promoted to a completion (this store does not observe completions).
RECEIPT_CLAIMED = "claimed"
#: Sentinel: no row exists for the key. Not persisted.
RECEIPT_ABSENT = "absent"

RECEIPT_STATES = frozenset({RECEIPT_CLAIMED})

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS recovery_receipt (
    key_digest               TEXT NOT NULL,
    action_digest            TEXT NOT NULL,
    schema_version           INTEGER NOT NULL,
    recovery_action_journal  TEXT NOT NULL,
    original_dispatch_anchor TEXT NOT NULL,
    workspace_id             TEXT NOT NULL,
    lane_id                  TEXT NOT NULL,
    lane_generation          TEXT NOT NULL,
    route_identity           TEXT NOT NULL,
    receiver_identity        TEXT NOT NULL,
    action_kind              TEXT NOT NULL,
    retry_of                 TEXT NOT NULL,
    state                    TEXT NOT NULL,
    detail                   TEXT NOT NULL DEFAULT '',
    claimed_at               TEXT NOT NULL,
    seq                      INTEGER NOT NULL,
    UNIQUE(key_digest)
)
"""

#: Indexed because every claim asks "has this ACTION been admitted, under any journal?" — the
#: question review j#81021 F2 showed the key digest alone cannot answer.
_ACTION_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS recovery_receipt_action "
    "ON recovery_receipt (action_digest, seq)"
)

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"

#: The identity columns compared against a presented key on a digest hit (canonical order).
_IDENTITY_COLUMNS = (
    "schema_version",
    "recovery_action_journal",
    "original_dispatch_anchor",
    "workspace_id",
    "lane_id",
    "lane_generation",
    "route_identity",
    "receiver_identity",
    "action_kind",
    "retry_of",
)


class CallbackRecoveryReceiptError(RuntimeError):
    """The receipt authority is unavailable / not itself (fail-closed -> do NOT actuate).

    Raised for a missing, empty, foreign, corrupt, or nonce-mismatched store. The caller must read
    it as "this authority cannot tell me whether the action was admitted", never as "it was not".
    """


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (sibling-store convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def callback_recovery_receipt_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``callback-recovery-receipt.sqlite`` path under the mozyo-bridge home."""
    return (home or mozyo_bridge_home()) / CALLBACK_RECOVERY_RECEIPT_FILENAME


@dataclass(frozen=True)
class ClaimResult:
    """The outcome of a :meth:`CallbackRecoveryReceipt.claim`.

    ``won`` is True only when THIS call wrote a fresh :data:`RECEIPT_CLAIMED` row — the single
    caller cleared to perform the recovery round's first effect. ``prior_state`` is the state
    before the call (:data:`RECEIPT_ABSENT` when ``won``). ``conflict`` is True when the digest
    matched but the stored identity did not — a collision or a tampered row, which is fail-closed
    and NOT a duplicate.
    """

    won: bool
    prior_state: str
    conflict: bool = False
    detail: str = ""


class CallbackRecoveryReceipt:
    """Read/write access to the home-scoped recovery admission authority.

    Construction never touches the filesystem. Unlike a lazily-created cache, the store is never
    auto-created on the write path: :meth:`bootstrap` is the only creation surface, so a deleted
    store fails closed instead of re-admitting every action it had already recorded.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else callback_recovery_receipt_path(home)
        self.sidecar_path = self.path.with_name(
            self.path.name + CALLBACK_RECOVERY_RECEIPT_SIDECAR_SUFFIX
        )
        self.seal_path = self.path.with_name(
            self.path.name + CALLBACK_RECOVERY_RECEIPT_SEAL_SUFFIX
        )
        self.lock_path = self.path.with_name(
            self.path.name + CALLBACK_RECOVERY_RECEIPT_LOCK_SUFFIX
        )

    # -- lifecycle seal (DB-external; the fresh-install / total-loss discriminator) ----------

    @contextlib.contextmanager
    def _lifecycle_lock(self):
        """Serialize the bootstrap decision across processes; released by the OS on crash.

        Review j#81021 F1. The claim path has always been exclusive (``BEGIN IMMEDIATE`` + a UNIQUE
        key), but the procedure that *builds* the store had no exclusion at all: eight concurrent
        bootstraps produced two winners and six raw ``sqlite3.OperationalError``s. Two processes
        both reading "nothing here" and both minting is how one re-mints the other's operational
        store.

        ``flock`` dies with the process, so a crashed initializer never wedges the lifecycle — the
        one place a reclaimable lock is correct, because it guards a *decision*, not a claim.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def seal_state(self) -> str:
        """The seal's state (pure read; fail-soft to :data:`SEAL_INVALID`, never to absent).

        An unreadable or unrecognized seal is :data:`SEAL_INVALID`, NOT :data:`SEAL_ABSENT`:
        "something is here and I cannot read it" is not evidence that nothing ever ran here, and
        collapsing the two is what would let a re-mint through.
        """
        try:
            text = self.seal_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SEAL_ABSENT
        except OSError:
            return SEAL_INVALID
        parts = (text.splitlines() or [""])[0].split()
        if (
            len(parts) == 3
            and parts[0] == _SEAL_FORMAT
            and parts[1] == _SEAL_VERSION_TOKEN
            and parts[2] in (SEAL_INITIALIZING, SEAL_OPERATIONAL)
        ):
            return parts[2]
        return SEAL_INVALID

    def _write_seal(self, state: str) -> None:
        """Replace the seal in one step (atomic write; exclusion lives in :meth:`_lifecycle_lock`)."""
        self.seal_path.parent.mkdir(parents=True, exist_ok=True)
        note = {
            SEAL_INITIALIZING: "a store is being minted here; it has never admitted a recovery, "
                               "so re-minting it is safe",
            SEAL_OPERATIONAL: "this authority has admitted here; a missing store is a LOSS, never "
                              "a fresh install, and must be restored rather than re-created",
        }[state]
        # Process-unique temp: a fixed name would itself be a collision between concurrent writers.
        tmp = self.seal_path.with_suffix(f"{self.seal_path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(
            f"{_SEAL_FORMAT} {_SEAL_VERSION_TOKEN} {state}\nsealed at {_utc_now()}\n{note}\n",
            encoding="utf-8",
        )
        tmp.replace(self.seal_path)

    def store_artifacts(self) -> list:
        """Every directory entry here that could hold, or hide, a claim.

        The DB is not the only file that carries rows: SQLite keeps an in-flight transaction's rows
        in its rollback journal / WAL until it commits, so a live journal beside a deleted DB is a
        claim rather than an absence. ``lexists`` because a broken symlink is a directory entry
        someone put here on purpose, and ``exists()`` would follow it and report nothing at all.
        """
        candidates = [self.path, self.sidecar_path]
        candidates += [
            self.path.with_name(self.path.name + suffix)
            for suffix in _SQLITE_AUXILIARY_SUFFIXES
        ]
        return [c for c in candidates if os.path.lexists(c)]

    def has_store(self) -> bool:
        """True when ANY store artifact exists here — usable or not.

        Deliberately not the same question as "is the pair healthy". A torn store is unusable but
        NOT empty: its DB still holds whatever was claimed through it. "I cannot open this" is not
        "there is nothing here", and re-minting on the strength of that confusion destroys live
        claims and re-grants their identities.
        """
        return bool(self.store_artifacts())

    def has_operated(self) -> bool:
        """True once this authority has been sealed operational here, store or no store."""
        return self.seal_state() == SEAL_OPERATIONAL

    # -- store identity (DB-external sidecar) ------------------------------

    def _read_sidecar_nonce(self) -> Optional[str]:
        """The nonce pinned in the DB-external sidecar, or ``None`` when absent / unreadable."""
        try:
            value = self.sidecar_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        return value or None

    @staticmethod
    def _db_nonce(conn: sqlite3.Connection) -> Optional[str]:
        """The ``store_nonce`` stamped inside the DB, or ``None`` (fail-soft)."""
        try:
            row = conn.execute(
                "SELECT value FROM store_meta WHERE key = ?", (_STORE_NONCE_KEY,)
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        return str(row[0]) if row is not None else None

    def _create_fresh(self, nonce: str) -> None:
        """Create the DB fresh, stamp the schema version + store nonce, write the sidecar."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(_TABLE_SQL)
            conn.execute(_ACTION_INDEX_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (_STORE_NONCE_KEY, nonce),
            )
            conn.execute(f"PRAGMA user_version = {CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION}")
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    # -- bootstrap ---------------------------------------------------------

    def bootstrap(self) -> None:
        """Operator-only init / adoption. NEVER called from the claim path.

        Review j#81021 F1. Every branch answers one question: **could this store already have
        admitted a recovery that a re-mint would forget?** The whole decision runs under the
        lifecycle lock, on state re-read *after* the lock is held — reading first and acting later
        is what lets two processes both decide "fresh" and the loser re-mint the winner's store.

        Decided on ARTIFACT PRESENCE, never on usability:

        ==================================  ========================================================
        state                               action
        ==================================  ========================================================
        invalid seal                        REFUSE. Something is here and cannot be read; that is
                                            not evidence it never ran.
        healthy pair (any seal)             ADOPT in place, keeping the nonce and every row; a
                                            half-written seal is promoted to operational.
        any artifact present, unusable      REFUSE. Unusable is not empty: a torn store's DB,
                                            journal or WAL still holds its claims. Restore it --
                                            deleting is not a recovery.
        no store + operational seal         REFUSE. It admitted here and its store is gone: a LOSS.
                                            This is the branch whose absence let a deleted pair
                                            re-admit every recovery it had recorded.
        no store + initializing seal        mint. The claim path demands an operational seal, so no
                                            admission can have been granted under it, however many
                                            times the crash was recorded.
        no store + absent seal              mint. First init -- see the limit in the module note.
        ==================================  ========================================================

        There is deliberately no ``recover()`` sibling: re-minting would free every claim this
        store holds and re-admit every recovery already actuated. A lost store is restored, not
        re-created.
        """
        with self._lifecycle_lock():
            seal = self.seal_state()                 # re-read UNDER the lock: earlier reads are stale
            pair_ok = self._pair_is_healthy()
            artifacts = self.has_store()

            if seal == SEAL_INVALID:
                raise CallbackRecoveryReceiptError(
                    f"callback recovery receipt seal {self.seal_path} exists but cannot be read as "
                    f"a seal. Something operated here and its record is unreadable; that is not "
                    f"evidence it never ran. Refusing to re-mint — restore or inspect the store."
                )
            if pair_ok:
                if seal != SEAL_OPERATIONAL:
                    # Adopt in place: the rows and nonce are untouched, only the seal advances.
                    self._write_seal(SEAL_OPERATIONAL)
                return
            if artifacts:
                raise CallbackRecoveryReceiptError(
                    f"callback recovery receipt store {self.path} has artifacts "
                    f"({[p.name for p in self.store_artifacts()]}) but they do not work together "
                    f"(torn / replaced / nonce-mismatched). Unusable is NOT empty — those files "
                    f"may still hold admitted claims. Restore the store; deleting it is not a "
                    f"recovery."
                )
            if seal == SEAL_OPERATIONAL:
                raise CallbackRecoveryReceiptError(
                    f"callback recovery receipt store {self.path} is GONE but its seal says this "
                    f"authority has admitted here: a total loss, not a fresh install. Re-minting "
                    f"would re-admit every recovery already actuated. Restore the store from the "
                    f"durable record; this authority has no re-mint."
                )
            # Seal FIRST, store second: a crash between them leaves `initializing` (never granted,
            # safe to re-mint) rather than an operational store with no seal (which the branch
            # above could never diagnose as a loss).
            self._write_seal(SEAL_INITIALIZING)
            self._create_fresh(secrets.token_hex(16))
            self._write_seal(SEAL_OPERATIONAL)

    def _pair_is_healthy(self) -> bool:
        """True when DB + sidecar co-exist at the same nonce and schema version (fail-soft).

        Says nothing about the seal — that is :meth:`is_bootstrapped`'s job. Kept separate because
        conflating "the files work together" with "this authority is ready" is what would let the
        claim path run against a store bootstrap still considers mintable.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return False
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION:
                return False
            return self._db_nonce(conn) == sidecar_nonce
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return False
        finally:
            conn.close()

    def is_bootstrapped(self) -> bool:
        """True when the store is usable AND sealed operational — readiness includes the seal.

        A seal written but never consulted is decoration. Readiness has to mean both, or the claim
        path would happily admit against a store whose seal still says ``initializing`` — which
        bootstrap is entitled to re-mint underneath it.
        """
        return self._pair_is_healthy() and self.seal_state() == SEAL_OPERATIONAL

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open an **existing, identity-matched, SEALED** connection, or fail closed.

        The seal check lives here, not only in the composition root: a check at the door is not a
        guard on the safe. Without it, a claim would be granted against a store whose seal says
        ``initializing`` — a store bootstrap is still entitled to re-mint underneath the claim it
        just issued.
        """
        seal = self.seal_state()
        if seal != SEAL_OPERATIONAL:
            raise CallbackRecoveryReceiptError(
                f"callback recovery receipt store {self.path} is not sealed operational "
                f"(seal={seal}); fail closed rather than admit against a store whose lifecycle is "
                f"unresolved"
            )
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None:
            raise CallbackRecoveryReceiptError(
                f"callback recovery receipt store {self.path} has no identity sidecar (never "
                f"bootstrapped / lost); fail closed rather than admit a recovery that may already "
                f"have been actuated"
            )
        if not self.path.exists():
            raise CallbackRecoveryReceiptError(
                f"callback recovery receipt store {self.path} DB is missing while its sidecar "
                f"remains (store loss); fail closed rather than auto-create and re-admit every "
                f"recovery it recorded"
            )
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION:
                raise CallbackRecoveryReceiptError(
                    f"callback recovery receipt store {self.path} is not a bootstrapped store at "
                    f"version {CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION} (found {version}: empty / "
                    f"replaced / foreign); fail closed"
                )
            if self._db_nonce(conn) != sidecar_nonce:
                raise CallbackRecoveryReceiptError(
                    f"callback recovery receipt store {self.path} nonce does not match its sidecar "
                    f"(replaced / foreign store); fail closed"
                )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise CallbackRecoveryReceiptError(
                f"callback recovery receipt store {self.path} is unreadable "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        except CallbackRecoveryReceiptError:
            conn.close()
            raise
        return conn

    # -- claim -------------------------------------------------------------

    def claim(self, key: object, *, now: Optional[str] = None) -> ClaimResult:
        """Atomically admit ``key`` for ONE recovery round, or report never-actuate (fail-closed).

        Takes the write lock (``BEGIN IMMEDIATE``) before reading, so two concurrent receivers of
        the same key serialize and exactly one wins — the loser sees the winner's row and does not
        actuate.

        **Two questions, not one** (review j#81021 F2). The key digest answers "has this exact
        publication been admitted?"; the action digest answers "has this *recovery* been admitted,
        under any journal?". Asking only the first was the hole: re-publishing the same recovery at
        a different journal id produced a different key, and both copies were admitted — walking
        straight around a claim that is never reclaimed. So a second publication of an
        already-admitted action must **prove** it is an authorized retry by naming the exact key it
        retries; anything else is a conflict.

        A pre-existing row is NEVER reclaimed, whatever its age (j#80984 Disposition 4). On a key
        digest hit the stored identity is compared with the presented key; a mismatch is reported as
        ``conflict`` rather than as a duplicate, because dropping a *different* action as a
        duplicate is a silent loss.

        Raises :class:`CallbackRecoveryReceiptError` (do-not-actuate) on a corrupt / lost store or
        any transaction failure.
        """
        digest = str(key.digest())
        action = str(key.action_digest())
        fields = dict(key.as_fields())
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT state, {', '.join(_IDENTITY_COLUMNS)} FROM recovery_receipt "
                f"WHERE key_digest = ?",
                (digest,),
            ).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                stored = {name: str(row[i + 1]) for i, name in enumerate(_IDENTITY_COLUMNS)}
                presented = {name: str(fields[name]) for name in _IDENTITY_COLUMNS}
                if stored != presented:
                    differing = sorted(k for k in presented if stored[k] != presented[k])
                    return ClaimResult(
                        won=False,
                        prior_state=str(row[0]),
                        conflict=True,
                        detail=(
                            f"a receipt row exists at this key digest but its identity differs in "
                            f"{differing!r}: a digest collision or a tampered row. Failing closed — "
                            f"this is NOT a duplicate, and admitting or silently dropping it would "
                            f"lose a distinct recovery action"
                        ),
                    )
                return ClaimResult(
                    won=False,
                    prior_state=str(row[0]),
                    detail=(
                        f"this exact recovery action was already admitted here at "
                        f"j#{stored['recovery_action_journal']}; a duplicate delivery is a durable "
                        f"no-op and is never re-admitted (claims are not reclaimed)"
                    ),
                )
            # This publication is new. Has the ACTION already been admitted under another journal?
            prior = conn.execute(
                "SELECT key_digest, recovery_action_journal, state FROM recovery_receipt "
                "WHERE action_digest = ? ORDER BY seq DESC LIMIT 1",
                (action,),
            ).fetchone()
            if prior is not None:
                prior_digest, prior_journal, prior_state = (
                    str(prior[0]), str(prior[1]), str(prior[2])
                )
                # Only a retry that names the LATEST admitted key continues the chain. Comparing
                # against any older link would let a stale linkage be replayed forever.
                if str(fields["retry_of"]) != prior_digest:
                    conn.execute("COMMIT")
                    declared = str(fields["retry_of"])
                    return ClaimResult(
                        won=False,
                        prior_state=prior_state,
                        conflict=True,
                        detail=(
                            f"this recovery was already admitted here at j#{prior_journal}, and "
                            f"this publication (j#{fields['recovery_action_journal']}) is a "
                            f"different journal for the SAME action "
                            f"(retry_of={declared}, expected {prior_digest}). A new journal id is "
                            f"not authorization: an accidental duplicate publication or a copied "
                            f"note would otherwise actuate the same recovery twice. An authorized "
                            f"retry must name the exact prior key it retries"
                        ),
                    )
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM recovery_receipt"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO recovery_receipt (key_digest, action_digest, schema_version, "
                "recovery_action_journal, original_dispatch_anchor, workspace_id, lane_id, "
                "lane_generation, route_identity, receiver_identity, action_kind, retry_of, "
                "state, detail, claimed_at, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest,
                    action,
                    int(fields["schema_version"]),
                    fields["recovery_action_journal"],
                    fields["original_dispatch_anchor"],
                    fields["workspace_id"],
                    fields["lane_id"],
                    fields["lane_generation"],
                    fields["route_identity"],
                    fields["receiver_identity"],
                    fields["action_kind"],
                    fields["retry_of"],
                    RECEIPT_CLAIMED,
                    "admitted for one recovery round",
                    stamp,
                    int(seq),
                ),
            )
            conn.execute("COMMIT")
        except CallbackRecoveryReceiptError:
            raise
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CallbackRecoveryReceiptError(
                f"callback recovery receipt claim failed ({type(exc).__name__}: {exc}); fail "
                f"closed rather than actuate without a durable admission"
            ) from exc
        finally:
            conn.close()
        return ClaimResult(
            won=True,
            prior_state=RECEIPT_ABSENT,
            detail="admitted: this receiver may perform this recovery round's first effect once",
        )

    def peek(self, key: object) -> str:
        """The recorded state for ``key`` (read-only; :data:`RECEIPT_ABSENT` when unclaimed).

        Diagnostics only. It is NOT an admission test: between a peek and an effect another
        receiver can claim, so only :meth:`claim` authorizes actuation.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state FROM recovery_receipt WHERE key_digest = ?", (str(key.digest()),)
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise CallbackRecoveryReceiptError(
                f"callback recovery receipt read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return str(row[0]) if row is not None else RECEIPT_ABSENT


__all__ = (
    "CALLBACK_RECOVERY_RECEIPT_FILENAME",
    "CALLBACK_RECOVERY_RECEIPT_SIDECAR_SUFFIX",
    "CALLBACK_RECOVERY_RECEIPT_SEAL_SUFFIX",
    "CALLBACK_RECOVERY_RECEIPT_LOCK_SUFFIX",
    "SEAL_INITIALIZING",
    "SEAL_OPERATIONAL",
    "SEAL_INVALID",
    "SEAL_ABSENT",
    "CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION",
    "RECEIPT_CLAIMED",
    "RECEIPT_ABSENT",
    "RECEIPT_STATES",
    "CallbackRecoveryReceiptError",
    "ClaimResult",
    "CallbackRecoveryReceipt",
    "callback_recovery_receipt_path",
)
