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

**Why store loss fails closed.** A missing / replaced / nonce-mismatched store is not "nothing was
claimed" — it is "this authority cannot tell you", and admitting on that reading is precisely how a
deleted DB re-actuates an already-admitted recovery. Every path raises
:class:`CallbackRecoveryReceiptError`, which the caller treats as zero-actuation. The residual is
the same local total-loss indistinguishability every sibling authority carries; nothing more is
claimed.

**Why the row keeps the whole key, not only its digest.** A digest hit is presumed to be the same
action, but a presumption is not a proof. The stored fields are compared against the presented ones,
so a digest collision or a tampered row surfaces as :attr:`ClaimResult.conflict` (fail-closed)
rather than being silently absorbed as a "duplicate" — the failure mode where a *different* recovery
is dropped on the floor and nobody can tell.
"""

from __future__ import annotations

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
CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION = 1

#: The recovery round was admitted here. TERMINAL: never reclaimed, never superseded, and never
#: promoted to a completion (this store does not observe completions).
RECEIPT_CLAIMED = "claimed"
#: Sentinel: no row exists for the key. Not persisted.
RECEIPT_ABSENT = "absent"

RECEIPT_STATES = frozenset({RECEIPT_CLAIMED})

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS recovery_receipt (
    key_digest               TEXT NOT NULL,
    schema_version           INTEGER NOT NULL,
    recovery_action_journal  TEXT NOT NULL,
    original_dispatch_anchor TEXT NOT NULL,
    workspace_id             TEXT NOT NULL,
    lane_id                  TEXT NOT NULL,
    lane_generation          TEXT NOT NULL,
    route_identity           TEXT NOT NULL,
    receiver_identity        TEXT NOT NULL,
    action_kind              TEXT NOT NULL,
    state                    TEXT NOT NULL,
    detail                   TEXT NOT NULL DEFAULT '',
    claimed_at               TEXT NOT NULL,
    UNIQUE(key_digest)
)
"""

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
        """Initial-only creation of the receipt store + its DB-external identity.

        The **only** creation path, and deliberately without a :meth:`recover` sibling: this
        authority has no operator-gated re-mint, because re-minting it would free every claim it
        holds and re-admit every recovery already actuated. A lost store is restored (from backup /
        the durable journal), not re-created.

        - **both** DB and sidecar absent -> mint a nonce and create them together (first init);
        - both present at the same nonce -> idempotent no-op;
        - **any** single-sided or nonce-mismatched state -> fail closed. An inconsistent store is a
          loss or a replacement, and silently re-creating it is exactly how an already-admitted
          recovery becomes admissible again.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        db_exists = self.path.exists()
        if sidecar_nonce is None and not db_exists:
            self._create_fresh(secrets.token_hex(16))
            return
        if self.is_bootstrapped():
            return
        raise CallbackRecoveryReceiptError(
            f"callback recovery receipt store {self.path} is in an inconsistent state (only one of "
            f"the DB / sidecar exists, or their nonces differ): a store loss or replacement. "
            f"Refusing to re-create it, which would re-admit recoveries that were already "
            f"actuated. Restore the store; do not re-mint it."
        )

    def is_bootstrapped(self) -> bool:
        """True when DB + sidecar co-exist at the same nonce and schema version (fail-soft)."""
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

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open an **existing, identity-matched** manual-transaction connection, or fail closed."""
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

        A pre-existing row is NEVER reclaimed, whatever its age: this is the safety-first crash
        semantics of j#80984 Disposition 4. On a digest hit the stored identity is compared with the
        presented key; a mismatch is reported as ``conflict`` rather than as a duplicate, because
        dropping a *different* action as a duplicate is a silent loss.

        Raises :class:`CallbackRecoveryReceiptError` (do-not-actuate) on a corrupt / lost store or
        any transaction failure.
        """
        digest = str(key.digest())
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
                        f"{stored['recovery_action_journal']!r}; a duplicate delivery is a durable "
                        f"no-op and is never re-admitted (claims are not reclaimed)"
                    ),
                )
            conn.execute(
                "INSERT INTO recovery_receipt (key_digest, schema_version, "
                "recovery_action_journal, original_dispatch_anchor, workspace_id, lane_id, "
                "lane_generation, route_identity, receiver_identity, action_kind, state, detail, "
                "claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest,
                    int(fields["schema_version"]),
                    fields["recovery_action_journal"],
                    fields["original_dispatch_anchor"],
                    fields["workspace_id"],
                    fields["lane_id"],
                    fields["lane_generation"],
                    fields["route_identity"],
                    fields["receiver_identity"],
                    fields["action_kind"],
                    RECEIPT_CLAIMED,
                    "admitted for one recovery round",
                    stamp,
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
    "CALLBACK_RECOVERY_RECEIPT_SCHEMA_VERSION",
    "RECEIPT_CLAIMED",
    "RECEIPT_ABSENT",
    "RECEIPT_STATES",
    "CallbackRecoveryReceiptError",
    "ClaimResult",
    "CallbackRecoveryReceipt",
    "callback_recovery_receipt_path",
)
