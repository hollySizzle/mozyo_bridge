"""Home-scoped atomic idempotency fence for herdr coordinator one-step forwards (Redmine #13583).

Increment 3 (Design Answer j#76417, safety-contract point 4): a single ``workflow step`` on a
resolved coordinator lane may perform **exactly one** ticketless forward send — the
department-root → project-gateway consultation, or the project-gateway → child work-intake — fenced
so a repeat / crash / concurrent caller can never produce a duplicate forward.

This is a **dedicated** fence, carved off from the anchored-worker
:class:`~mozyo_bridge.core.state.dispatch_outbox_fence.DispatchOutboxFence` exactly the way the
callback outbox was (a separate bounded context, the same reserve-before-send pattern): a herdr
forward is **ticketless**, so its identity must not be disguised as a Redmine
``(issue, journal)`` anchor (safety-contract point 3). The UNIQUE key is the forward's own
identity — ``(workspace_id, from_lane_id, from_role, to_role, project_scope,
target_assigned_name)`` — never a synthetic Redmine anchor.

The mechanics mirror the dispatch fence exactly (the guarantee is identical): a home-scoped SQLite
store with a ``BEGIN IMMEDIATE`` reserve-before-send, a DB-external ``store_nonce`` sidecar that
fails a **deleted / replaced** store closed (so a lost store can never re-send a delivered
forward), initial-only :meth:`bootstrap`, and operator-gated :meth:`recover`. States are the closed
``reserved / delivered / uncertain / cancelled`` set; a re-entry on a still-``reserved`` row (crash
window) is surfaced ``uncertain`` for operator reconcile, never auto-retried.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

FORWARD_OUTBOX_FENCE_FILENAME = "forward-outbox-fence.sqlite"
FORWARD_OUTBOX_FENCE_SIDECAR_SUFFIX = ".anchor"
FORWARD_OUTBOX_FENCE_SCHEMA_VERSION = 1

# The closed fence-state vocabulary (mirrors the dispatch fence; identical guarantee).
FORWARD_RESERVED = "reserved"  # write-locked before the send; the send's fate is not yet known
FORWARD_DELIVERED = "delivered"  # the forward send was positively confirmed
FORWARD_UNCERTAIN = "uncertain"  # the send outcome is unknown (crash / timeout) -> operator reconcile
FORWARD_CANCELLED = "cancelled"  # a durable supersede was confirmed *before* the send
FORWARD_ABSENT = "absent"  # sentinel: no row existed for the key (not persisted)

FORWARD_STATES = frozenset(
    {FORWARD_RESERVED, FORWARD_DELIVERED, FORWARD_UNCERTAIN, FORWARD_CANCELLED}
)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS forward_outbox (
    workspace_id         TEXT NOT NULL,
    from_lane_id         TEXT NOT NULL,
    from_role            TEXT NOT NULL,
    to_role              TEXT NOT NULL,
    project_scope        TEXT NOT NULL,
    target_assigned_name TEXT NOT NULL,
    state                TEXT NOT NULL,
    detail               TEXT NOT NULL DEFAULT '',
    reserved_at          TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE(workspace_id, from_lane_id, from_role, to_role, project_scope, target_assigned_name)
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"


class ForwardOutboxFenceError(RuntimeError):
    """The forward outbox fence DB could not be opened at the expected schema (fail-closed).

    The caller treats it as "do not send": the idempotency authority is unavailable, so a send
    could duplicate a forward.
    """


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (sibling-store convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def forward_outbox_fence_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``forward-outbox-fence.sqlite`` path under the mozyo-bridge home."""
    return (home or mozyo_bridge_home()) / FORWARD_OUTBOX_FENCE_FILENAME


@dataclass(frozen=True)
class ForwardFenceKey:
    """The UNIQUE forward-fence key: the forward's own (anchor-free) identity.

    ``from_lane_id`` / ``from_role`` are the sender lane + its resolved workflow role; ``to_role``
    is the forward target role; ``project_scope`` is the gateway's declared scope for a child-intake
    (``""`` for the grandparent consultation); ``target_assigned_name`` is the resolved live
    target's canonical mzb1 assigned name. No Redmine ``issue`` / ``journal`` — a forward is
    ticketless (safety-contract point 3).
    """

    workspace_id: str
    from_lane_id: str
    from_role: str
    to_role: str
    project_scope: str
    target_assigned_name: str

    def as_row(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.workspace_id,
            self.from_lane_id,
            self.from_role,
            self.to_role,
            self.project_scope,
            self.target_assigned_name,
        )


@dataclass(frozen=True)
class ReserveResult:
    """The outcome of a :meth:`ForwardOutboxFence.reserve` attempt.

    ``won`` is True only when this call wrote a fresh :data:`FORWARD_RESERVED` row — the single
    caller cleared to perform the one forward send.
    """

    won: bool
    prior_state: str
    current_state: str
    needs_reconcile: bool = False
    detail: str = ""


class ForwardOutboxFence:
    """Read/write access to the home-scoped forward outbox fence DB (mirrors the dispatch fence)."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else forward_outbox_fence_path(home)
        self.sidecar_path = self.path.with_name(
            self.path.name + FORWARD_OUTBOX_FENCE_SIDECAR_SUFFIX
        )

    # -- store identity (DB-external sidecar) ------------------------------

    def _read_sidecar_nonce(self) -> Optional[str]:
        try:
            value = self.sidecar_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
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
        return str(row[0]) if row is not None else None

    def _create_fresh(self, nonce: str) -> None:
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
            conn.execute(f"PRAGMA user_version = {FORWARD_OUTBOX_FENCE_SCHEMA_VERSION}")
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    # -- bootstrap / recover -----------------------------------------------

    def bootstrap(self) -> None:
        """Initial-only creation of the fence store + its DB-external identity (mirrors dispatch F1).

        A reserve never auto-creates a missing store (auto-creation would resurrect a deleted /
        replaced store and let an already ``delivered`` forward re-send). Both absent -> mint a
        nonce + create; co-existing at the same nonce -> idempotent no-op; any single-sided /
        mismatched state -> fail closed (use :meth:`recover`).
        """
        sidecar_nonce = self._read_sidecar_nonce()
        db_exists = self.path.exists()
        if sidecar_nonce is None and not db_exists:
            self._create_fresh(secrets.token_hex(16))
            return
        if self.is_bootstrapped():
            return
        raise ForwardOutboxFenceError(
            f"forward outbox fence {self.path} is in an inconsistent state (only one of the DB / "
            f"sidecar exists, or their nonces differ): a store loss or replacement. Refusing to "
            f"silently re-create. Use recover() for a deliberate, operator-gated loss recovery."
        )

    def recover(self) -> None:
        """Deliberate operator loss-recovery: mint a NEW nonce and a fresh DB (mirrors dispatch F1)."""
        self._create_fresh(secrets.token_hex(16))

    def is_bootstrapped(self) -> bool:
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return False
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != FORWARD_OUTBOX_FENCE_SCHEMA_VERSION:
                return False
            return self._db_nonce(conn) == sidecar_nonce
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return False
        finally:
            conn.close()

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open an existing, identity-matched manual-transaction connection, or fail closed."""
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None:
            raise ForwardOutboxFenceError(
                f"forward outbox fence {self.path} has no identity sidecar (never bootstrapped / "
                f"lost); fail closed rather than risk a duplicate send"
            )
        if not self.path.exists():
            raise ForwardOutboxFenceError(
                f"forward outbox fence {self.path} DB is missing while its sidecar remains (store "
                f"loss); fail closed rather than auto-create and risk a duplicate send"
            )
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != FORWARD_OUTBOX_FENCE_SCHEMA_VERSION:
                raise ForwardOutboxFenceError(
                    f"forward outbox fence {self.path} is not a bootstrapped fence at version "
                    f"{FORWARD_OUTBOX_FENCE_SCHEMA_VERSION} (found {version}: empty / replaced / "
                    f"foreign store); fail closed rather than risk a duplicate send"
                )
            if self._db_nonce(conn) != sidecar_nonce:
                raise ForwardOutboxFenceError(
                    f"forward outbox fence {self.path} nonce does not match its sidecar (replaced "
                    f"/ foreign store); fail closed rather than risk a duplicate send"
                )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise ForwardOutboxFenceError(
                f"forward outbox fence {self.path} is unreadable ({type(exc).__name__}); fail "
                f"closed rather than risk a duplicate send"
            ) from exc
        except ForwardOutboxFenceError:
            conn.close()
            raise
        return conn

    # -- reserve -----------------------------------------------------------

    def reserve(self, key: ForwardFenceKey, *, now: Optional[str] = None) -> ReserveResult:
        """Atomically reserve the key for a single forward send, or report never-send (fail-closed).

        Fresh key -> writes a :data:`FORWARD_RESERVED` row, ``won=True``. Existing key ->
        ``won=False`` with the prior state; a still-:data:`FORWARD_RESERVED` row (crash window) is
        transitioned to :data:`FORWARD_UNCERTAIN` and flagged ``needs_reconcile`` (never
        auto-retried). Raises :class:`ForwardOutboxFenceError` (do-not-send) on a corrupt store.
        """
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM forward_outbox WHERE workspace_id=? AND from_lane_id=? AND "
                "from_role=? AND to_role=? AND project_scope=? AND target_assigned_name=?",
                key.as_row(),
            ).fetchone()
            if row is None:
                try:
                    conn.execute(
                        "INSERT INTO forward_outbox (workspace_id, from_lane_id, from_role, "
                        "to_role, project_scope, target_assigned_name, state, detail, reserved_at, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (*key.as_row(), FORWARD_RESERVED, "", stamp, stamp),
                    )
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK")
                    return ReserveResult(
                        won=False,
                        prior_state=FORWARD_RESERVED,
                        current_state=FORWARD_RESERVED,
                        needs_reconcile=False,
                        detail="lost a concurrent reserve race; the other caller sends",
                    )
                conn.execute("COMMIT")
                return ReserveResult(
                    won=True,
                    prior_state=FORWARD_ABSENT,
                    current_state=FORWARD_RESERVED,
                    detail="reserved a fresh key for the single forward send",
                )
            prior = str(row[0])
            if prior == FORWARD_RESERVED:
                conn.execute(
                    "UPDATE forward_outbox SET state=?, detail=?, updated_at=? WHERE "
                    "workspace_id=? AND from_lane_id=? AND from_role=? AND to_role=? AND "
                    "project_scope=? AND target_assigned_name=?",
                    (
                        FORWARD_UNCERTAIN,
                        "re-entered a reserved key (crash window); prior send outcome unknown",
                        stamp,
                        *key.as_row(),
                    ),
                )
                conn.execute("COMMIT")
                return ReserveResult(
                    won=False,
                    prior_state=FORWARD_RESERVED,
                    current_state=FORWARD_UNCERTAIN,
                    needs_reconcile=True,
                    detail="prior reserve unresolved; marked uncertain for operator reconcile",
                )
            conn.execute("ROLLBACK")
            return ReserveResult(
                won=False,
                prior_state=prior,
                current_state=prior,
                needs_reconcile=(prior == FORWARD_UNCERTAIN),
                detail=f"key already {prior}; never-send",
            )
        except ForwardOutboxFenceError:
            raise
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ForwardOutboxFenceError(
                f"forward outbox fence reserve failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- outcome writes ----------------------------------------------------

    def _set_state(self, key: ForwardFenceKey, state: str, detail: str, *, now: Optional[str]) -> bool:
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE forward_outbox SET state=?, detail=?, updated_at=? WHERE workspace_id=? "
                "AND from_lane_id=? AND from_role=? AND to_role=? AND project_scope=? AND "
                "target_assigned_name=?",
                (state, detail, stamp, *key.as_row()),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ForwardOutboxFenceError(
                f"forward outbox fence update failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def mark_delivered(self, key: ForwardFenceKey, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved key's forward send as positively delivered."""
        return self._set_state(key, FORWARD_DELIVERED, detail or "forward delivered", now=now)

    def mark_uncertain(self, key: ForwardFenceKey, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved key's forward outcome as unknown (crash / timeout) -> reconcile."""
        return self._set_state(key, FORWARD_UNCERTAIN, detail or "forward outcome uncertain", now=now)

    def mark_cancelled(self, key: ForwardFenceKey, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved key as cancelled (a durable supersede confirmed before the send)."""
        return self._set_state(key, FORWARD_CANCELLED, detail or "cancelled before send", now=now)

    # -- reads -------------------------------------------------------------

    def state_of(self, key: ForwardFenceKey) -> str:
        """The current fence state for the key, or :data:`FORWARD_ABSENT` (fail-soft diagnostic)."""
        if not self.is_bootstrapped():
            return FORWARD_ABSENT
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state FROM forward_outbox WHERE workspace_id=? AND from_lane_id=? AND "
                "from_role=? AND to_role=? AND project_scope=? AND target_assigned_name=?",
                key.as_row(),
            ).fetchone()
            return str(row[0]) if row is not None else FORWARD_ABSENT
        finally:
            conn.close()


__all__ = (
    "FORWARD_OUTBOX_FENCE_FILENAME",
    "FORWARD_OUTBOX_FENCE_SIDECAR_SUFFIX",
    "FORWARD_OUTBOX_FENCE_SCHEMA_VERSION",
    "FORWARD_RESERVED",
    "FORWARD_DELIVERED",
    "FORWARD_UNCERTAIN",
    "FORWARD_CANCELLED",
    "FORWARD_ABSENT",
    "FORWARD_STATES",
    "ForwardOutboxFenceError",
    "forward_outbox_fence_path",
    "ForwardFenceKey",
    "ReserveResult",
    "ForwardOutboxFence",
)
