"""Owner-token attempt lease for the callback sweep (Redmine #13889 review R5-F1).

The sweep needs an authority it can **hold across slow I/O** — a Redmine read, a record write,
another read — so that a concurrent sweep cannot publish a duplicate record or race the send.

:class:`...dispatch_outbox_fence.DispatchOutboxFence` cannot be that authority, and the attempt to
make it one was rejected (R5-F1). Its contract assumes a reservation is **instantaneous**:
``reserve`` treats an existing ``reserved`` row as *crash residue* and transitions it to
``uncertain``. That is correct for its own callers, which reserve and send in one breath. But a
sweep that holds a reservation across seconds of I/O gets its live row rewritten to ``uncertain``
by any concurrent sweep — the owner can then no longer stand down, and the anchor is blocked
permanently. Adding a ``release`` did not fix that: ``state == reserved`` **does not prove the send
never happened**, because the row carries no owner identity.

So this is a separate, lane-local authority with the property the fence deliberately lacks:
**every row names its owner**. That single addition is what makes the difference:

- a **loser is passive** (``LEASE_HELD``): it observes that someone else owns the attempt and
  changes *nothing*, so an active owner is never corrupted and never mistaken for a crash;
- **release is owner-conditional**: only the holder can drop its own lease, so "stand down" is a
  claim only the party that knows it did not send can make;
- an **expired** lease is reclaimable, so a genuinely crashed owner does not block the anchor
  forever — the failure mode the fence avoids by declaring the crash window ``uncertain``, solved
  here by a deadline instead, because here a crashed owner has provably not sent (the send happens
  under the *fence*, after the lease work is done).

The division of labour is deliberate. This lease serializes the **attempt** (reads + record
publication). The dispatch outbox fence still serializes the **send**, in exactly the short,
native way its contract supports. Neither is asked to do the other's job.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

CALLBACK_SWEEP_LEASE_FILENAME = "callback-sweep-lease.sqlite"
CALLBACK_SWEEP_LEASE_SIDECAR_SUFFIX = ".anchor"
CALLBACK_SWEEP_LEASE_SCHEMA_VERSION = 1

#: This caller now owns the attempt and must release it when done.
LEASE_ACQUIRED = "acquired"
#: Another owner holds a live lease. The caller does NOTHING — it does not mutate the row, does not
#: reclassify the owner as crashed, and does not send. Passivity is the whole point (R5-F1).
LEASE_HELD = "held"
#: A previous owner's lease passed its deadline and was reclaimed by this caller.
LEASE_RECLAIMED = "reclaimed"

#: How long an attempt may hold the lease before another sweep may reclaim it. Generous relative to
#: a sweep (a few Redmine round-trips) so a slow-but-live owner is never stolen from; short enough
#: that a crashed owner does not strand the anchor. A crashed owner cannot have sent: the send
#: happens under the outbox fence *after* the leased work.
DEFAULT_LEASE_TTL_SECONDS = 120.0

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sweep_lease (
    workspace_id TEXT NOT NULL,
    lane_id      TEXT NOT NULL,
    issue        TEXT NOT NULL,
    anchor       TEXT NOT NULL,
    owner_token  TEXT NOT NULL,
    expires_at   REAL NOT NULL,
    acquired_at  REAL NOT NULL,
    UNIQUE(workspace_id, lane_id, issue, anchor)
)
"""


class CallbackSweepLeaseError(RuntimeError):
    """The lease store is unusable, so the attempt must not proceed (fail-closed)."""


@dataclass(frozen=True)
class LeaseKey:
    """The attempt identity: one lease per (workspace, lane, issue, dispatch anchor)."""

    workspace_id: str
    lane_id: str
    issue: str
    anchor: str

    def as_row(self) -> tuple[str, str, str, str]:
        return (self.workspace_id, self.lane_id, self.issue, self.anchor)


@dataclass(frozen=True)
class LeaseResult:
    """The outcome of an acquire. ``token`` is set only when this caller owns the attempt."""

    status: str
    token: str = ""
    detail: str = ""
    #: The store identity this grant was issued under (review R6-F2). An owner that later finds a
    #: DIFFERENT nonce is holding a lease from a store that no longer exists, so it must stand down.
    store_nonce: str = ""

    @property
    def owned(self) -> bool:
        return self.status in (LEASE_ACQUIRED, LEASE_RECLAIMED)


def callback_sweep_lease_path(home: Optional[Path] = None) -> Path:
    return (home or mozyo_bridge_home()) / CALLBACK_SWEEP_LEASE_FILENAME


class CallbackSweepLease:
    """Owner-token attempt leases, serialized by ``BEGIN IMMEDIATE`` (home-scoped SQLite)."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path else callback_sweep_lease_path(home)

    @property
    def sidecar_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + CALLBACK_SWEEP_LEASE_SIDECAR_SUFFIX)

    def _read_sidecar_nonce(self) -> Optional[str]:
        try:
            value = self.sidecar_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    @staticmethod
    def _db_nonce(conn: sqlite3.Connection) -> Optional[str]:
        try:
            row = conn.execute(
                "SELECT value FROM store_meta WHERE key = ?", (_STORE_NONCE_KEY,)
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        return str(row[0]) if row else None

    def _create_fresh(self, nonce: str) -> None:
        """Create the DB and its sidecar together at ``nonce`` (the only creation path)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (_STORE_NONCE_KEY, nonce),
            )
            conn.execute(f"PRAGMA user_version = {CALLBACK_SWEEP_LEASE_SCHEMA_VERSION}")
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    def is_bootstrapped(self) -> bool:
        """True when the DB and its sidecar co-exist at the SAME nonce (read-only)."""
        sidecar = self._read_sidecar_nonce()
        if sidecar is None or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(self.path, isolation_level=None)
        except sqlite3.DatabaseError:
            return False
        try:
            conn.execute(_META_TABLE_SQL)
            return self._db_nonce(conn) == sidecar
        except sqlite3.DatabaseError:
            return False
        finally:
            conn.close()

    def bootstrap(self) -> None:
        """Initial-only creation of the lease store + its DB-external identity (review R7-F2).

        Mirrors :meth:`...dispatch_outbox_fence.DispatchOutboxFence.bootstrap` exactly, because the
        hazard is exactly the same and an earlier revision copied only half of it:

        - **both** the DB and the sidecar absent (a genuine first bootstrap) -> mint a random
          ``store_nonce`` and create the DB + sidecar together at that nonce;
        - DB and sidecar co-exist at the same nonce -> idempotent no-op;
        - **any** other state -- sidecar present but DB missing, **DB present but the sidecar
          missing**, or a nonce mismatch -> **fail closed**.

        The sidecar-only-loss case is why "half the contract" is not a partial fix but a hole: the
        production composition root calls ``bootstrap()`` on EVERY ``--execute``, so silently
        re-minting a nonce onto a live DB invalidates the grant of an owner that is still working
        and hands the anchor to someone else. A single-sided store is a loss or a replacement, and
        it must go through the deliberate, operator-gated :meth:`recover` -- never a silent
        re-create.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        db_exists = self.path.exists()
        if sidecar_nonce is None and not db_exists:
            self._create_fresh(secrets.token_hex(16))  # both absent: the only genuine first init
            return
        if self.is_bootstrapped():
            return  # DB + sidecar at the same nonce: already bootstrapped
        raise CallbackSweepLeaseError(
            f"callback sweep lease {self.path} is in an inconsistent state (only one of the DB / "
            f"sidecar exists, or their nonces differ): a store loss or replacement. Refusing to "
            f"silently re-create, which would invalidate a live owner's grant and hand the same "
            f"anchor to a second owner. Use recover() for a deliberate, operator-gated recovery."
        )

    def recover(self) -> None:
        """Deliberate, operator-gated loss recovery: mint a NEW nonce and a fresh store.

        The sanctioned way OUT of the fail-closed state :meth:`bootstrap` raises — a guard with no
        release path is a permanent stall, not a safety property. An operator invokes this after
        confirming no sweep is mid-attempt; every lingering grant is invalidated by the new nonce,
        which is the point.
        """
        self._create_fresh(secrets.token_hex(16))

    def _connect(self) -> sqlite3.Connection:
        """Open an EXISTING, identity-matched store, or fail closed (mirrors the outbox fence).

        Never creates the container. A deleted / replaced / recreated store is the #13889 R6-F2
        split-brain: the old owner keeps its grant against a store that is gone while a new process
        hands the same anchor to somebody else. The DB and its DB-external sidecar must co-exist at
        the SAME nonce; a missing file, an empty swap-in, or a nonce mismatch all fail closed.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None:
            raise CallbackSweepLeaseError(
                f"callback sweep lease {self.path} has no identity sidecar (never bootstrapped / "
                f"lost); fail closed rather than run an unserialized attempt"
            )
        if not self.path.exists():
            raise CallbackSweepLeaseError(
                f"callback sweep lease {self.path} is missing while its sidecar remains (store "
                f"loss); fail closed rather than auto-create and hand out a duplicate lease"
            )
        try:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            if self._db_nonce(conn) != sidecar_nonce:
                conn.close()
                raise CallbackSweepLeaseError(
                    f"callback sweep lease {self.path} nonce does not match its sidecar "
                    f"(replaced / recreated store); fail closed"
                )
            return conn
        except CallbackSweepLeaseError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise CallbackSweepLeaseError(
                f"callback sweep lease store {self.path} is unusable ({type(exc).__name__}); "
                f"fail closed rather than run an unserialized attempt"
            ) from exc

    def acquire(
        self, key: LeaseKey, *, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        now: Optional[float] = None,
    ) -> LeaseResult:
        """Take the attempt lease, or report that a live owner holds it (passively).

        A live owner's row is **never** mutated by a loser — the defect that made the shared fence
        unusable here (R5-F1). An expired row is reclaimed with a fresh token, which also
        invalidates the dead owner's release.
        """
        stamp = float(now if now is not None else time.time())
        token = secrets.token_hex(16)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_token, expires_at FROM sweep_lease WHERE workspace_id=? AND "
                "lane_id=? AND issue=? AND anchor=?",
                key.as_row(),
            ).fetchone()
            if row is not None and float(row[1]) > stamp:
                conn.execute("ROLLBACK")
                return LeaseResult(
                    status=LEASE_HELD,
                    detail="another sweep owns this attempt; standing down without touching it",
                )
            reclaimed = row is not None
            conn.execute(
                "INSERT INTO sweep_lease (workspace_id, lane_id, issue, anchor, owner_token, "
                "expires_at, acquired_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, lane_id, issue, anchor) DO UPDATE SET "
                "owner_token=excluded.owner_token, expires_at=excluded.expires_at, "
                "acquired_at=excluded.acquired_at",
                (*key.as_row(), token, stamp + float(ttl_seconds), stamp),
            )
            nonce = self._db_nonce(conn) or ""
            conn.execute("COMMIT")
            return LeaseResult(
                status=LEASE_RECLAIMED if reclaimed else LEASE_ACQUIRED,
                token=token,
                store_nonce=nonce,
                detail="reclaimed an expired lease" if reclaimed else "acquired a fresh lease",
            )
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CallbackSweepLeaseError(
                f"callback sweep lease acquire failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def release(self, key: LeaseKey, token: str) -> bool:
        """Drop the lease, but only if ``token`` still owns it. Returns True when dropped.

        Owner-conditional by construction: a caller that has been superseded (its lease expired and
        was reclaimed) cannot delete the new owner's lease.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM sweep_lease WHERE workspace_id=? AND lane_id=? AND issue=? AND "
                "anchor=? AND owner_token=?",
                (*key.as_row(), str(token)),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CallbackSweepLeaseError(
                f"callback sweep lease release failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def owns(self, key: LeaseKey, token: str, *, store_nonce: str = "") -> bool:
        """True only if ``token`` STILL owns a live lease on ``key`` in the SAME store (R6-F1/F2).

        The check a caller must make immediately before every durable publication and before the
        send. Acquiring is not enough: the TTL can expire while the owner is merely *slow* rather
        than dead -- a few Redmine round-trips is all it takes -- and a new owner then reclaims the
        anchor. Both would publish, producing exactly the duplicate durable record this issue
        exists to remove. "The owner is dead so it cannot have sent" only ever covered the SEND
        (which the outbox fence gates); it never covered the publication, which only the lease
        gates.

        Fail-closed by construction: an unreadable / lost / replaced store raises out of
        :meth:`_connect`, an expired lease reads as not-owned, and a mismatched ``store_nonce``
        means the grant came from a store that no longer exists.
        """
        conn = self._connect()
        try:
            if store_nonce and self._db_nonce(conn) != str(store_nonce):
                return False
            row = conn.execute(
                "SELECT owner_token, expires_at FROM sweep_lease WHERE workspace_id=? AND "
                "lane_id=? AND issue=? AND anchor=?",
                key.as_row(),
            ).fetchone()
        finally:
            conn.close()
        if row is None or float(row[1]) <= time.time():
            return False
        return str(row[0]) == str(token)

    def owner_of(self, key: LeaseKey) -> str:
        """The current owner token, or ``""`` when unheld / expired (read-only, for tests+debug)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT owner_token, expires_at FROM sweep_lease WHERE workspace_id=? AND "
                "lane_id=? AND issue=? AND anchor=?",
                key.as_row(),
            ).fetchone()
        finally:
            conn.close()
        if row is None or float(row[1]) <= time.time():
            return ""
        return str(row[0])


__all__ = (
    "CALLBACK_SWEEP_LEASE_FILENAME",
    "LEASE_ACQUIRED",
    "LEASE_HELD",
    "LEASE_RECLAIMED",
    "DEFAULT_LEASE_TTL_SECONDS",
    "CallbackSweepLeaseError",
    "LeaseKey",
    "LeaseResult",
    "CallbackSweepLease",
    "callback_sweep_lease_path",
    "CALLBACK_SWEEP_LEASE_SIDECAR_SUFFIX",
)
