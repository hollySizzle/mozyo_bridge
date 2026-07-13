"""Durable per-workspace supervisor lease (Redmine #13683 Phase A).

The workspace callback supervisor is a **user-scoped single owner** that enumerates the
workspace registry and drives each workspace's callback outbox / durable-event supply. Two
supervisor processes must never both drive the same workspace — a duplicate daemon would
double-deliver callbacks and race the cursor. This module is the fence that makes a duplicate
supervisor a **zero-delivery** no-op (j#77065 Phase A acceptance 1): a supervisor acquires a
durable lease per workspace before it touches that workspace's outbox, and a second supervisor
that cannot acquire the lease simply skips the workspace.

Design (mirrors the callback outbox's ``claim_token`` + ``claimed_at`` lease and the workspace
registry's fail-closed schema guard):

- a **separate home-scoped SQLite** (``supervisor-lease.sqlite``), not a table folded into the
  shared ``workflow-runtime.sqlite`` — the lease is a net-new runtime concern with its own
  lifecycle, and keeping it out of the shared schema avoids bumping a version several other
  lanes depend on (the same "separate home-scoped file" precedent the callback fences use).
- a **CAS acquire** under ``BEGIN IMMEDIATE`` (:meth:`SupervisorLeaseStore.acquire`): the lease
  for a workspace is granted iff it is unheld, already held by the **same** holder (idempotent
  re-acquire / renew), or the current holder's lease has **expired** (a crashed supervisor's
  lease is taken over after its TTL, so a dead owner never wedges a workspace forever). A
  *different, still-live* holder is refused — that is the duplicate-supervisor fence.
- an **explicit release** (:meth:`release`) and a **renew/heartbeat** (:meth:`renew`), both
  token-conditional on still owning the lease, so a taken-over previous owner can never release
  or renew the new owner's lease.

The TTL is compared over ISO-8601 UTC-second timestamps, which sort chronologically, so the
expiry check is a plain lexicographic ``expires_at <= now`` — the same comparison the callback
outbox uses for its claim lease. All timestamps are injectable (``now=``) so the fence is
deterministically testable without wall-clock races.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-scoped SQLite file holding supervisor leases. A separate DB from the workflow
#: runtime cache / callback outbox (``workflow-runtime.sqlite``) and the workspace identity
#: registry (``registry.sqlite``): the lease is a distinct runtime-ownership concern.
SUPERVISOR_LEASE_FILENAME = "supervisor-lease.sqlite"

#: Schema version stamped into ``PRAGMA user_version``. An unrecognized version fails closed
#: (a downgraded build never silently drops or rewrites a newer lease table).
SUPERVISOR_LEASE_SCHEMA_VERSION = 1

_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1})

#: Default lease TTL (seconds). A live supervisor renews well within this window on every
#: bounded pass; a crashed / hung supervisor's lease is treated as abandoned once ``now`` is
#: past ``expires_at``, so another supervisor can take the workspace over. Chosen comfortably
#: larger than one bounded reconciliation sweep so an active owner is never preempted mid-pass.
SUPERVISOR_LEASE_TTL_SECONDS = 300

_LEASE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_lease (
    workspace_id TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    renewed_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
)
"""


class SupervisorLeaseError(RuntimeError):
    """The supervisor-lease DB could not be opened at a recognized schema (fail-closed)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plus_seconds(now_iso: str, seconds: int) -> str:
    """``now_iso`` (ISO-8601 UTC second) advanced by ``seconds``, as an ISO-8601 UTC second.

    Parses the injected / generated ISO timestamp and re-emits it at second precision so the
    stored ``expires_at`` sorts lexicographically against a later ``now`` (both UTC second).
    """
    base = datetime.fromisoformat(now_iso)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=max(0, int(seconds)))).astimezone(
        timezone.utc
    ).isoformat(timespec="seconds")


def supervisor_lease_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``supervisor-lease.sqlite`` path under the mozyo-bridge home.

    ``home`` overrides the root (tests pass a temp dir); otherwise the shared
    :func:`mozyo_bridge.shared.paths.mozyo_bridge_home` resolves ``MOZYO_BRIDGE_HOME`` /
    ``~/.mozyo_bridge``.
    """
    return (home or mozyo_bridge_home()) / SUPERVISOR_LEASE_FILENAME


@dataclass(frozen=True)
class SupervisorLease:
    """A persisted supervisor lease row (who owns a workspace, and until when)."""

    workspace_id: str
    holder: str
    acquired_at: str
    renewed_at: str
    expires_at: str

    def as_payload(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "holder": self.holder,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class LeaseResult:
    """The outcome of an :meth:`SupervisorLeaseStore.acquire` attempt.

    ``acquired`` is True only when this call now holds the lease (a fresh grant, an idempotent
    re-acquire by the same holder, or an expired-lease takeover). When False, ``holder`` echoes
    the **current, still-live** owner so a refused duplicate supervisor can report who owns the
    workspace instead of guessing. ``expires_at`` is the lease deadline (this holder's on a
    grant; the incumbent's on a refusal).
    """

    acquired: bool
    workspace_id: str
    holder: str
    expires_at: str
    #: Why the acquire resolved as it did (fixed vocabulary; literal regardless of UI language).
    reason: str

    def as_payload(self) -> dict[str, object]:
        return {
            "acquired": self.acquired,
            "workspace_id": self.workspace_id,
            "holder": self.holder,
            "expires_at": self.expires_at,
            "reason": self.reason,
        }


#: ``LeaseResult.reason`` vocabulary.
LEASE_GRANTED_FRESH = "granted_fresh"  # no prior holder
LEASE_GRANTED_SAME_HOLDER = "granted_same_holder"  # idempotent re-acquire / renew
LEASE_GRANTED_TAKEOVER = "granted_expired_takeover"  # prior holder's lease expired
LEASE_REFUSED_HELD = "refused_held_by_other"  # a different, still-live holder owns it


class SupervisorLeaseStore:
    """CAS access to the home-scoped supervisor lease DB (duplicate-supervisor fence).

    Construction never touches the filesystem; the DB is created lazily on the first write.
    Reads on an absent DB return "unheld" (the normal pre-write state); an existing DB with an
    unrecognized container version fails closed rather than being rewritten.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else supervisor_lease_path(home)

    # -- connections -------------------------------------------------------

    def _connect_rw(self) -> sqlite3.Connection:
        """Open a read-write connection, creating / validating the container (fail-closed)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            conn.execute(_LEASE_TABLE_SQL)
            conn.execute(f"PRAGMA user_version = {SUPERVISOR_LEASE_SCHEMA_VERSION}")
        elif version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise SupervisorLeaseError(
                f"supervisor lease store {self.path} has unsupported schema version "
                f"{version}; this build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. "
                "The DB is left untouched (downgrade-safe); use a newer build or move it aside."
            )
        else:
            conn.execute(_LEASE_TABLE_SQL)  # self-heal a table lost under a valid version
        return conn

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        """Open a read-only connection if the DB exists; ``None`` when absent (fail-closed on bad version)."""
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            conn.close()
            raise SupervisorLeaseError(
                f"supervisor lease store {self.path} is unreadable: {exc}"
            ) from exc
        if version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise SupervisorLeaseError(
                f"supervisor lease store {self.path} has unsupported schema version {version}; "
                f"this build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}."
            )
        return conn

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass

    @staticmethod
    def _row(r: tuple) -> SupervisorLease:
        return SupervisorLease(
            workspace_id=r[0], holder=r[1], acquired_at=r[2], renewed_at=r[3], expires_at=r[4]
        )

    # -- acquire / renew / release -----------------------------------------

    def acquire(
        self,
        workspace_id: str,
        holder: str,
        *,
        now: Optional[str] = None,
        ttl_seconds: int = SUPERVISOR_LEASE_TTL_SECONDS,
    ) -> LeaseResult:
        """CAS-acquire the workspace's supervisor lease (the duplicate-supervisor fence).

        Under ``BEGIN IMMEDIATE`` the lease is granted iff it is unheld, already held by the
        **same** ``holder`` (idempotent re-acquire — this also renews the deadline), or the
        current holder's lease has **expired** (``expires_at <= now`` — takeover of a crashed
        supervisor). A different, still-live holder is **refused** (:data:`LEASE_REFUSED_HELD`)
        and its current deadline is echoed, so a second supervisor that loses the race delivers
        nothing for that workspace. ``acquired_at`` is preserved across a same-holder renew and
        reset on a fresh grant / takeover.
        """
        ws = str(workspace_id or "").strip()
        who = str(holder or "").strip()
        if not ws or not who:
            raise SupervisorLeaseError(
                f"supervisor lease requires a non-empty workspace_id and holder; "
                f"got workspace_id={workspace_id!r} holder={holder!r}"
            )
        stamp = now or _utc_now_iso()
        expires = _plus_seconds(stamp, ttl_seconds)
        conn = self._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT workspace_id, holder, acquired_at, renewed_at, expires_at "
                "FROM supervisor_lease WHERE workspace_id=?",
                (ws,),
            ).fetchone()
            if existing is None:
                reason = LEASE_GRANTED_FRESH
                acquired_at = stamp
            else:
                cur = self._row(existing)
                if cur.holder == who:
                    reason = LEASE_GRANTED_SAME_HOLDER
                    acquired_at = cur.acquired_at  # preserve original acquisition time
                elif cur.expires_at <= stamp:
                    reason = LEASE_GRANTED_TAKEOVER
                    acquired_at = stamp
                else:
                    # A different holder still owns a live lease — refuse (zero-delivery fence).
                    conn.execute("ROLLBACK")
                    return LeaseResult(
                        acquired=False,
                        workspace_id=ws,
                        holder=cur.holder,
                        expires_at=cur.expires_at,
                        reason=LEASE_REFUSED_HELD,
                    )
            conn.execute(
                "INSERT INTO supervisor_lease "
                "(workspace_id, holder, acquired_at, renewed_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET holder=excluded.holder, "
                "acquired_at=excluded.acquired_at, renewed_at=excluded.renewed_at, "
                "expires_at=excluded.expires_at",
                (ws, who, acquired_at, stamp, expires),
            )
            conn.execute("COMMIT")
            return LeaseResult(
                acquired=True, workspace_id=ws, holder=who, expires_at=expires, reason=reason
            )
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise SupervisorLeaseError(
                f"supervisor lease acquire failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def renew(
        self,
        workspace_id: str,
        holder: str,
        *,
        now: Optional[str] = None,
        ttl_seconds: int = SUPERVISOR_LEASE_TTL_SECONDS,
    ) -> bool:
        """Extend the lease deadline; returns False if this ``holder`` no longer owns it.

        Token-conditional on ``holder`` (``AND holder=?``): a supervisor whose lease was taken
        over after expiry cannot silently re-extend the new owner's lease. A heartbeat that
        returns False is the signal to stop touching the workspace.
        """
        ws = str(workspace_id or "").strip()
        who = str(holder or "").strip()
        if not ws or not who:
            return False
        stamp = now or _utc_now_iso()
        expires = _plus_seconds(stamp, ttl_seconds)
        conn = self._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE supervisor_lease SET renewed_at=?, expires_at=? "
                "WHERE workspace_id=? AND holder=?",
                (stamp, expires, ws, who),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise SupervisorLeaseError(
                f"supervisor lease renew failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def release(self, workspace_id: str, holder: str) -> bool:
        """Release the lease iff this ``holder`` still owns it; returns whether a row was deleted.

        Token-conditional on ``holder`` so a taken-over previous owner's late release cannot
        evict the new owner. A bounded run-once supervisor releases each workspace at the end of
        its sweep so the next invocation can re-acquire; a long-lived daemon holds + renews.
        """
        ws = str(workspace_id or "").strip()
        who = str(holder or "").strip()
        if not ws or not who:
            return False
        conn = self._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM supervisor_lease WHERE workspace_id=? AND holder=?", (ws, who)
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            self._rollback(conn)
            raise SupervisorLeaseError(
                f"supervisor lease release failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- reads -------------------------------------------------------------

    def holder_of(self, workspace_id: str) -> Optional[SupervisorLease]:
        """Return the persisted lease for ``workspace_id``, or ``None`` if unheld (read-only)."""
        ws = str(workspace_id or "").strip()
        conn = self._connect_ro()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT workspace_id, holder, acquired_at, renewed_at, expires_at "
                "FROM supervisor_lease WHERE workspace_id=?",
                (ws,),
            ).fetchone()
        finally:
            conn.close()
        return self._row(row) if row is not None else None

    def leases(self) -> tuple[SupervisorLease, ...]:
        """Return all persisted leases in workspace order (read-only; empty if absent)."""
        conn = self._connect_ro()
        if conn is None:
            return ()
        try:
            rows = conn.execute(
                "SELECT workspace_id, holder, acquired_at, renewed_at, expires_at "
                "FROM supervisor_lease ORDER BY workspace_id"
            ).fetchall()
        finally:
            conn.close()
        return tuple(self._row(r) for r in rows)


__all__ = (
    "SUPERVISOR_LEASE_FILENAME",
    "SUPERVISOR_LEASE_SCHEMA_VERSION",
    "SUPERVISOR_LEASE_TTL_SECONDS",
    "LEASE_GRANTED_FRESH",
    "LEASE_GRANTED_SAME_HOLDER",
    "LEASE_GRANTED_TAKEOVER",
    "LEASE_REFUSED_HELD",
    "SupervisorLeaseError",
    "SupervisorLease",
    "LeaseResult",
    "SupervisorLeaseStore",
    "supervisor_lease_path",
)
