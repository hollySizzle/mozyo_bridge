"""Home-scoped route+generation lifecycle store for herdr coordinator forwards (Redmine #13583).

Increment 3 correction (Design Answer j#76528, R1-F1): a herdr coordinator one-step forward — the
department-root → project-gateway consultation, or the project-gateway → child work-intake — must be
**at-most-once per logical generation**, where a generation spans from the send until the receiver's
ticketless callback is positively returned. A repeat while a generation is reserved / delivered /
uncertain is a duplicate zero-send; only after the generation is ``completed`` (by the correlated
callback) may the next ``workflow step`` mint a new generation and send once.

This store is the **authority** for that lifecycle. It is keyed on the **stable route identity**
``(workspace_id, from_lane_id, from_role, to_role, project_scope)`` — the target's live assigned
name is an action-time attestation and is deliberately NOT part of the key, so a target rename can
never advance a generation (j#76528 point 1). Each route holds **exactly one** active generation
row carrying an opaque ``forward_action_id`` and its ``state``:

- :meth:`reserve` mints a fresh ``forward_action_id`` and writes a :data:`FORWARD_RESERVED` row for
  a fresh / ``completed`` route (the caller won the single send); a still-``reserved`` re-entry
  (crash window) transitions to :data:`FORWARD_UNCERTAIN` (never auto-retried); a
  ``reserved`` / ``delivered`` / ``uncertain`` generation is never-send (the active generation);
- :meth:`mark_delivered` / :meth:`mark_uncertain` record the send outcome, guarded by the exact
  ``forward_action_id`` so a stale writer never clobbers a newer generation;
- :meth:`complete` CAS-transitions the exact ``delivered`` generation to :data:`FORWARD_COMPLETED`
  — the correlated-callback completion hook. A stale / mismatched / already-advanced id no-ops, so a
  duplicate of an old callback can never close a newer active generation (j#76528 point 4 / 5).

Store identity mirrors the sibling fences: a DB-external ``store_nonce`` sidecar fails a deleted /
replaced store **closed** (a lost store can never re-send a delivered forward). :meth:`bootstrap` is
initial-only and :meth:`recover` is operator-gated — the **execution path never auto-creates the
store** (R1-F2): a missing / corrupt / replaced / total-loss store is a do-not-send condition, not a
silent re-create. The anchored-worker ``DispatchOutboxFence`` / ``callback_outbox`` are neither
reused nor disguised (this is ticketless — no Redmine ``(issue, journal)`` anchor).
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
#: Schema v2: route+generation lifecycle (v1 was the target-keyed fence, Redmine #13583 R1-F1).
FORWARD_OUTBOX_FENCE_SCHEMA_VERSION = 2

# The closed generation-state vocabulary (Design Answer j#76528).
FORWARD_RESERVED = "reserved"  # minted + write-locked before the send; the send's fate is unknown
FORWARD_DELIVERED = "delivered"  # the forward send was positively confirmed; awaiting the callback
FORWARD_UNCERTAIN = "uncertain"  # the send outcome is unknown (crash / timeout) -> operator reconcile
FORWARD_COMPLETED = "completed"  # the correlated callback returned -> the next step may re-mint
FORWARD_ABSENT = "absent"  # sentinel: no row existed for the route (not persisted)

FORWARD_STATES = frozenset(
    {FORWARD_RESERVED, FORWARD_DELIVERED, FORWARD_UNCERTAIN, FORWARD_COMPLETED}
)
#: The states that hold the active generation (a repeat is a duplicate zero-send).
_ACTIVE_STATES = frozenset({FORWARD_RESERVED, FORWARD_DELIVERED, FORWARD_UNCERTAIN})

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS forward_generation (
    workspace_id       TEXT NOT NULL,
    from_lane_id       TEXT NOT NULL,
    from_role          TEXT NOT NULL,
    to_role            TEXT NOT NULL,
    project_scope      TEXT NOT NULL,
    forward_action_id  TEXT NOT NULL,
    state              TEXT NOT NULL,
    detail             TEXT NOT NULL DEFAULT '',
    reserved_at        TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(workspace_id, from_lane_id, from_role, to_role, project_scope)
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
    """The forward store could not be opened at the expected schema (fail-closed = do-not-send)."""


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (sibling-store convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def forward_outbox_fence_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``forward-outbox-fence.sqlite`` path under the mozyo-bridge home."""
    return (home or mozyo_bridge_home()) / FORWARD_OUTBOX_FENCE_FILENAME


def mint_forward_action_id() -> str:
    """Mint an opaque, unguessable forward action id (never a role / approval / anchor authority)."""
    return "fwd_" + secrets.token_hex(16)


@dataclass(frozen=True)
class ForwardRouteKey:
    """The UNIQUE route identity a forward generation series is keyed on (target-name-free).

    The target's live assigned name is an **action-time attestation**, not part of this key
    (j#76528 point 1) — so a target rename can never advance a generation.
    """

    workspace_id: str
    from_lane_id: str
    from_role: str
    to_role: str
    project_scope: str

    def as_row(self) -> tuple[str, str, str, str, str]:
        return (
            self.workspace_id,
            self.from_lane_id,
            self.from_role,
            self.to_role,
            self.project_scope,
        )


@dataclass(frozen=True)
class ReserveResult:
    """The outcome of a :meth:`ForwardOutboxFence.reserve` attempt.

    ``won`` is True only when this call minted + wrote a fresh :data:`FORWARD_RESERVED` generation
    (the single caller cleared to send). ``action_id`` is the minted id on ``won`` (``""`` else).
    ``prior_state`` is the route's state before this call.
    """

    won: bool
    action_id: str
    prior_state: str
    current_state: str
    needs_reconcile: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ActiveGeneration:
    """The route's current generation (id + state), or absent."""

    action_id: str
    state: str

    @property
    def absent(self) -> bool:
        return self.state == FORWARD_ABSENT


class ForwardOutboxFence:
    """Read/write access to the home-scoped forward route+generation lifecycle store."""

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

    # -- bootstrap / recover (operator-only; the execution path never auto-creates, R1-F2) --

    def bootstrap(self) -> None:
        """Initial-only creation of the store + its DB-external identity (operator action).

        The **only** initial-creation path. A reserve never auto-creates a missing store (that would
        resurrect a deleted / replaced store and let an already-``delivered`` forward re-send — R1-F2
        / R1-F1). Both absent -> mint + create; co-existing at the same nonce -> idempotent no-op; any
        single-sided / mismatched state -> fail closed (use :meth:`recover`).
        """
        sidecar_nonce = self._read_sidecar_nonce()
        db_exists = self.path.exists()
        if sidecar_nonce is None and not db_exists:
            self._create_fresh(secrets.token_hex(16))
            return
        if self.is_bootstrapped():
            return
        raise ForwardOutboxFenceError(
            f"forward store {self.path} is in an inconsistent state (only one of the DB / sidecar "
            f"exists, or their nonces differ): a store loss or replacement. Refusing to silently "
            f"re-create. Use recover() for a deliberate, operator-gated loss recovery."
        )

    def recover(self) -> None:
        """Deliberate operator loss-recovery: mint a NEW nonce and a fresh DB."""
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
                f"forward store {self.path} has no identity sidecar (never bootstrapped / lost); "
                f"fail closed rather than risk a duplicate send"
            )
        if not self.path.exists():
            raise ForwardOutboxFenceError(
                f"forward store {self.path} DB is missing while its sidecar remains (store loss); "
                f"fail closed rather than auto-create and risk a duplicate send"
            )
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != FORWARD_OUTBOX_FENCE_SCHEMA_VERSION:
                raise ForwardOutboxFenceError(
                    f"forward store {self.path} is not a bootstrapped store at version "
                    f"{FORWARD_OUTBOX_FENCE_SCHEMA_VERSION} (found {version}: empty / replaced / "
                    f"foreign / v1 store); fail closed rather than risk a duplicate send"
                )
            if self._db_nonce(conn) != sidecar_nonce:
                raise ForwardOutboxFenceError(
                    f"forward store {self.path} nonce does not match its sidecar (replaced / "
                    f"foreign store); fail closed rather than risk a duplicate send"
                )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise ForwardOutboxFenceError(
                f"forward store {self.path} is unreadable ({type(exc).__name__}); fail closed"
            ) from exc
        except ForwardOutboxFenceError:
            conn.close()
            raise
        return conn

    # -- reserve -----------------------------------------------------------

    def reserve(self, route: ForwardRouteKey, *, now: Optional[str] = None) -> ReserveResult:
        """Mint + reserve a fresh generation for the route, or report never-send (fail-closed).

        Fresh route or a :data:`FORWARD_COMPLETED` prior generation -> mint a new
        ``forward_action_id``, write / replace a :data:`FORWARD_RESERVED` row, ``won=True``. A
        still-:data:`FORWARD_RESERVED` re-entry (crash window) transitions to
        :data:`FORWARD_UNCERTAIN`, ``won=False``, ``needs_reconcile``. A
        :data:`FORWARD_DELIVERED` / :data:`FORWARD_UNCERTAIN` generation is the active generation ->
        ``won=False`` (never-send). Raises :class:`ForwardOutboxFenceError` (do-not-send) on a
        corrupt / uninitialized store.
        """
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT forward_action_id, state FROM forward_generation WHERE workspace_id=? AND "
                "from_lane_id=? AND from_role=? AND to_role=? AND project_scope=?",
                route.as_row(),
            ).fetchone()
            if row is None:
                action_id = mint_forward_action_id()
                conn.execute(
                    "INSERT INTO forward_generation (workspace_id, from_lane_id, from_role, "
                    "to_role, project_scope, forward_action_id, state, detail, reserved_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*route.as_row(), action_id, FORWARD_RESERVED, "", stamp, stamp),
                )
                conn.execute("COMMIT")
                return ReserveResult(
                    won=True, action_id=action_id, prior_state=FORWARD_ABSENT,
                    current_state=FORWARD_RESERVED, detail="minted the first generation for this route",
                )
            prior_action, prior = str(row[0]), str(row[1])
            if prior == FORWARD_COMPLETED:
                action_id = mint_forward_action_id()
                conn.execute(
                    "UPDATE forward_generation SET forward_action_id=?, state=?, detail=?, "
                    "reserved_at=?, updated_at=? WHERE workspace_id=? AND from_lane_id=? AND "
                    "from_role=? AND to_role=? AND project_scope=?",
                    (action_id, FORWARD_RESERVED, "", stamp, stamp, *route.as_row()),
                )
                conn.execute("COMMIT")
                return ReserveResult(
                    won=True, action_id=action_id, prior_state=FORWARD_COMPLETED,
                    current_state=FORWARD_RESERVED, detail="minted a new generation after completion",
                )
            if prior == FORWARD_RESERVED:
                conn.execute(
                    "UPDATE forward_generation SET state=?, detail=?, updated_at=? WHERE "
                    "workspace_id=? AND from_lane_id=? AND from_role=? AND to_role=? AND "
                    "project_scope=?",
                    (
                        FORWARD_UNCERTAIN,
                        "re-entered a reserved generation (crash window); prior send outcome unknown",
                        stamp,
                        *route.as_row(),
                    ),
                )
                conn.execute("COMMIT")
                return ReserveResult(
                    won=False, action_id="", prior_state=FORWARD_RESERVED,
                    current_state=FORWARD_UNCERTAIN, needs_reconcile=True,
                    detail="prior reserve unresolved; marked uncertain for operator reconcile",
                )
            conn.execute("ROLLBACK")
            return ReserveResult(
                won=False, action_id=prior_action, prior_state=prior, current_state=prior,
                needs_reconcile=(prior == FORWARD_UNCERTAIN),
                detail=f"generation already {prior}; never-send until it completes",
            )
        except ForwardOutboxFenceError:
            raise
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ForwardOutboxFenceError(
                f"forward store reserve failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- outcome / completion writes ---------------------------------------

    def _guarded_set(
        self, route: ForwardRouteKey, action_id: str, from_states, to_state: str, detail: str,
        *, now: Optional[str],
    ) -> bool:
        """CAS the route's generation to ``to_state`` only when its id + state match (stale-safe)."""
        stamp = now or _utc_now()
        placeholders = ",".join("?" for _ in from_states)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE forward_generation SET state=?, detail=?, updated_at=? WHERE workspace_id=? "
                "AND from_lane_id=? AND from_role=? AND to_role=? AND project_scope=? AND "
                f"forward_action_id=? AND state IN ({placeholders})",
                (to_state, detail, stamp, *route.as_row(), action_id, *from_states),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ForwardOutboxFenceError(
                f"forward store update failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def mark_delivered(self, route: ForwardRouteKey, action_id: str, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved generation's send as delivered (guarded by the exact action id)."""
        return self._guarded_set(
            route, action_id, (FORWARD_RESERVED,), FORWARD_DELIVERED,
            detail or "forward delivered; awaiting correlated callback", now=now,
        )

    def mark_uncertain(self, route: ForwardRouteKey, action_id: str, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved generation's send outcome as unknown (crash / timeout) -> reconcile."""
        return self._guarded_set(
            route, action_id, (FORWARD_RESERVED,), FORWARD_UNCERTAIN,
            detail or "forward outcome uncertain", now=now,
        )

    def complete(self, route: ForwardRouteKey, action_id: str, *, detail: str = "", now: Optional[str] = None) -> bool:
        """CAS the EXACT delivered generation to completed — the correlated-callback hook (j#76528).

        Advances **only** when the route's current generation is :data:`FORWARD_DELIVERED` AND its
        ``forward_action_id`` equals ``action_id``. A stale / mismatched / already-completed /
        reserved id no-ops (returns False), so a duplicate of an old callback can never close a newer
        active generation. Never advances from ``uncertain`` (that needs an explicit reconcile).
        """
        return self._guarded_set(
            route, action_id, (FORWARD_DELIVERED,), FORWARD_COMPLETED,
            detail or "correlated callback positively delivered; generation completed", now=now,
        )

    def complete_by_correlation(
        self,
        action_id: str,
        *,
        workspace_id: str,
        from_role: str,
        detail: str = "",
        now: Optional[str] = None,
    ) -> bool:
        """Complete a delivered generation by the callback's echoed id + reverse route (j#76528 §4).

        The correlated-callback completion hook: a callback returning to the forward's caller echoes
        the opaque ``forward_action_id`` and carries the caller's ``read_contract`` — which is the
        forward's ``from_role`` (the role the callback returns *to*). This CAS-advances the EXACT
        :data:`FORWARD_DELIVERED` generation whose ``forward_action_id`` **and**
        ``(workspace_id, from_role)`` match. The opaque, globally-unique ``action_id`` already pins
        the exact generation (so ``to_role`` / ``from_lane_id`` / ``project_scope`` are not required
        in the match); the ``from_role`` is the route cross-check that rejects a callback echoing a
        valid id on a **drifted** contract. A mismatched id / route, or a non-delivered /
        already-advanced generation, no-ops (returns False) — a stale / duplicate callback can never
        close a newer active generation. Returns ``False`` (never raises) on a fail-closed store: a
        missing completion is safe (the generation stays delivered until a real correlated callback
        arrives).
        """
        aid = (action_id or "").strip()
        if not aid:
            return False
        try:
            conn = self._connect()
        except ForwardOutboxFenceError:
            return False
        stamp = now or _utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE forward_generation SET state=?, detail=?, updated_at=? WHERE workspace_id=? "
                "AND from_role=? AND forward_action_id=? AND state=?",
                (
                    FORWARD_COMPLETED,
                    detail or "correlated callback positively delivered; generation completed",
                    stamp,
                    (workspace_id or "").strip(),
                    (from_role or "").strip(),
                    aid,
                    FORWARD_DELIVERED,
                ),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            return False
        finally:
            conn.close()

    # -- reads -------------------------------------------------------------

    def active(self, route: ForwardRouteKey) -> ActiveGeneration:
        """The route's current generation (id + state), or absent (fail-soft diagnostic)."""
        if not self.is_bootstrapped():
            return ActiveGeneration(action_id="", state=FORWARD_ABSENT)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT forward_action_id, state FROM forward_generation WHERE workspace_id=? AND "
                "from_lane_id=? AND from_role=? AND to_role=? AND project_scope=?",
                route.as_row(),
            ).fetchone()
            if row is None:
                return ActiveGeneration(action_id="", state=FORWARD_ABSENT)
            return ActiveGeneration(action_id=str(row[0]), state=str(row[1]))
        finally:
            conn.close()

    def is_active(self, route: ForwardRouteKey) -> bool:
        """True when the route currently holds a reserved / delivered / uncertain generation."""
        return self.active(route).state in _ACTIVE_STATES

    def _genuinely_uninitialized(self) -> bool:
        """True only when BOTH artifacts are truly absent. (tri-state, #13892 R5-F3)

        `_read_sidecar_nonce() is None` is a fail-soft predicate: it covers an EMPTY or
        UNREADABLE sidecar as well as a missing one, so `nonce is None and not path.exists()`
        read a DB-absent + empty-sidecar-residue store as "nothing was ever reserved here" —
        turning damage into a silent "no obligations owed". This is the identical defect the
        sibling dispatch fence was corrected for (review j#80523 R2-F1); it was re-introduced
        here by writing the same fail-soft check again.

        Uses `lexists`, so a broken symlink still counts as evidence something was placed here.
        """
        import os

        return not os.path.lexists(self.sidecar_path) and not os.path.lexists(self.path)

    def rows_for_sender(
        self, *, workspace_id: str, from_lane_id: str
    ) -> tuple[tuple[str, str, str], ...]:
        """``(from_role, to_role, state)`` for every generation this lane SENT. (read-only)

        Redmine #13892 R4-F3: a destructive action against a lane must know what that lane
        still owes, not only what is owed to it. A forward generation stays active from the
        send until its correlated callback returns, so closing the sender mid-generation
        strands it. :meth:`is_active` cannot answer this — it needs the full route key
        (including ``to_role`` / ``project_scope``), which a caller that only knows *which
        panes it is about to close* cannot build.

        Fails closed on a damaged / identity-mismatched store; a never-bootstrapped store has
        provably no rows and returns empty (the ordinary case, which must not be over-blocked).
        """
        if self._genuinely_uninitialized():
            return ()  # both artifacts absent: nothing was ever reserved here
        # Any other shape must prove itself through `_connect`, which fails closed on a
        # missing / empty / foreign / nonce-mismatched store.
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT from_role, to_role, state FROM forward_generation "
                "WHERE workspace_id=? AND from_lane_id=? ORDER BY from_role, to_role",
                (workspace_id, from_lane_id),
            ).fetchall()
        finally:
            conn.close()
        return tuple((str(r[0]), str(r[1]), str(r[2])) for r in rows)


__all__ = (
    "FORWARD_OUTBOX_FENCE_FILENAME",
    "FORWARD_OUTBOX_FENCE_SIDECAR_SUFFIX",
    "FORWARD_OUTBOX_FENCE_SCHEMA_VERSION",
    "FORWARD_RESERVED",
    "FORWARD_DELIVERED",
    "FORWARD_UNCERTAIN",
    "FORWARD_COMPLETED",
    "FORWARD_ABSENT",
    "FORWARD_STATES",
    "ForwardOutboxFenceError",
    "forward_outbox_fence_path",
    "mint_forward_action_id",
    "ForwardRouteKey",
    "ReserveResult",
    "ActiveGeneration",
    "ForwardOutboxFence",
)
