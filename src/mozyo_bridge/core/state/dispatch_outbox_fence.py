"""Home-scoped atomic idempotency / outbox fence for worker auto-dispatch (Redmine #13489).

The design contract's requirement 3 (``vibes/docs/logics/workflow-step-command-design.md``
``### Increment 2 dispatch 再有効化 contract``; j#74922 Q3 / j#74996 / j#75001): a single
``workflow step`` may perform **exactly one** exact-target send, fenced so a repeat / crash /
concurrent caller can never produce a duplicate send.

The fence is the **authority** — the append-only herdr delivery ledger, lane metadata, and the
workflow runtime store are recovery *evidence* and never substitute for it. It is a
home-scoped SQLite store with a UNIQUE key over
``(workspace_id, lane_id, issue, journal, action_id, target_assigned_name)`` and a
``BEGIN IMMEDIATE`` reserve-before-send:

- :meth:`DispatchOutboxFence.reserve` takes the write lock immediately and, for a fresh key,
  writes a :data:`FENCE_RESERVED` row and reports the caller *won* the reserve (proceed to the
  one send). For an existing key it reports the caller must **not** send (never-send) and
  echoes the prior state; a re-entry on a still-:data:`FENCE_RESERVED` row (a crash window
  where the prior send's fate is unknown) is transitioned to :data:`FENCE_UNCERTAIN` and
  surfaced for operator reconcile — it is **not** auto-retried.
- concurrency: ``BEGIN IMMEDIATE`` serializes two callers of the same key; the loser sees the
  winner's row and never sends. The UNIQUE constraint is the backstop.
- **store identity (mid-review j#75047 F1).** A reserve never auto-creates or silently accepts
  a fresh store: the mechanism would then let a **deleted / replaced** DB re-send an already
  delivered action. The store carries a random ``store_nonce`` pinned in a **DB-external
  sidecar** file. :meth:`bootstrap` is *initial only* — it creates the DB + sidecar together,
  and **refuses** (fail closed) when a sidecar already exists but the DB is missing / at the
  wrong nonce (a loss / replacement), directing the operator to the deliberate
  :meth:`recover`. Every reserve / update requires the DB and sidecar to co-exist at the same
  nonce; a missing / empty-swap / foreign / nonce-mismatched store fails closed. Recovery is
  operator-gated: :meth:`recover` mints a new nonce + a fresh DB, and a re-attempt of a lost
  action is only authorized upstream by a reconcile + a **new** ``action_id``.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.shared.paths import mozyo_bridge_home

DISPATCH_OUTBOX_FENCE_FILENAME = "dispatch-outbox-fence.sqlite"
DISPATCH_OUTBOX_FENCE_SIDECAR_SUFFIX = ".anchor"
DISPATCH_OUTBOX_FENCE_SCHEMA_VERSION = 1

# The closed fence-state vocabulary (design requirement 3).
FENCE_RESERVED = "reserved"  # write-locked before the send; the send's fate is not yet known
FENCE_DELIVERED = "delivered"  # the send's turn-start delivery was positively confirmed
FENCE_UNCERTAIN = "uncertain"  # the send outcome is unknown (crash / timeout) -> operator reconcile
FENCE_CANCELLED = "cancelled"  # a durable supersede was confirmed *before* the send
FENCE_ABSENT = "absent"  # sentinel: no row existed for the key (not persisted)

FENCE_STATES = frozenset({FENCE_RESERVED, FENCE_DELIVERED, FENCE_UNCERTAIN, FENCE_CANCELLED})

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dispatch_outbox (
    workspace_id         TEXT NOT NULL,
    lane_id              TEXT NOT NULL,
    issue                TEXT NOT NULL,
    journal              TEXT NOT NULL,
    action_id            TEXT NOT NULL,
    target_assigned_name TEXT NOT NULL,
    state                TEXT NOT NULL,
    detail               TEXT NOT NULL DEFAULT '',
    reserved_at          TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE(workspace_id, lane_id, issue, journal, action_id, target_assigned_name)
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"


class DispatchOutboxFenceError(RuntimeError):
    """The outbox fence DB could not be opened at the expected schema (fail-closed).

    Raised for a corrupt file or an unrecognized ``user_version`` — a structural problem the
    fence must not paper over. The caller treats it as "do not send": the idempotency authority
    is unavailable, so a send could duplicate.
    """


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (sibling-store convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dispatch_outbox_fence_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``dispatch-outbox-fence.sqlite`` path under the mozyo-bridge home.

    ``home`` overrides the root (tests pass a temp dir); otherwise
    :func:`mozyo_bridge.shared.paths.mozyo_bridge_home` resolves
    ``MOZYO_BRIDGE_HOME`` / ``~/.mozyo_bridge``.
    """
    return (home or mozyo_bridge_home()) / DISPATCH_OUTBOX_FENCE_FILENAME


@dataclass(frozen=True)
class FenceKey:
    """The UNIQUE fence key: the full ``(workspace, lane, issue, journal, action, target)`` tuple."""

    workspace_id: str
    lane_id: str
    issue: str
    journal: str
    action_id: str
    target_assigned_name: str

    def as_row(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.workspace_id,
            self.lane_id,
            self.issue,
            self.journal,
            self.action_id,
            self.target_assigned_name,
        )


@dataclass(frozen=True)
class TargetObligation:
    """One fence row aimed at a slot, WITH its causal identity (Redmine #13892 R2-F2).

    ``state`` alone cannot decide whether work is owed: a ``delivered`` row is a delivery ACK,
    and only its ``issue`` / ``journal`` can be correlated against the durable gate that says
    whether the handed-off work finished. Dropping that identity (as the first cut did) leaves
    a caller structurally unable to tell an owed obligation from a discharged one — so it is
    carried here rather than filtered away at the store.
    """

    target_assigned_name: str
    state: str
    issue: str = ""
    journal: str = ""
    action_id: str = ""
    workspace_id: str = ""
    lane_id: str = ""

    @property
    def non_terminal(self) -> bool:
        """Owed on the fence's own terms: the send's fate is unresolved."""
        return self.state in (FENCE_RESERVED, FENCE_UNCERTAIN)

    @property
    def needs_gate_correlation(self) -> bool:
        """Delivered: handed over, but whether the WORK is discharged lives in Redmine."""
        return self.state == FENCE_DELIVERED

    def as_payload(self) -> dict:
        return {
            "target": self.target_assigned_name,
            "state": self.state,
            "issue": self.issue,
            "journal": self.journal,
            "action_id": self.action_id,
        }


@dataclass(frozen=True)
class ReserveResult:
    """The outcome of a :meth:`DispatchOutboxFence.reserve` attempt.

    ``won`` is True only when this call wrote a fresh :data:`FENCE_RESERVED` row — the single
    caller cleared to perform the one send. ``prior_state`` is the state the key was in before
    this call (:data:`FENCE_ABSENT` when ``won``). ``current_state`` is the state after the
    call. ``needs_reconcile`` is True when the call surfaced a :data:`FENCE_UNCERTAIN` /
    re-entered :data:`FENCE_RESERVED` situation an operator must reconcile.
    """

    won: bool
    prior_state: str
    current_state: str
    needs_reconcile: bool = False
    detail: str = ""


class DispatchOutboxFence:
    """Read/write access to the home-scoped dispatch outbox fence DB.

    Construction never touches the filesystem; the DB is created lazily on the first
    :meth:`reserve`. Every write path is manual-transaction (``isolation_level=None`` +
    explicit ``BEGIN IMMEDIATE``) so the reserve holds the write lock across the
    read-then-insert and two concurrent callers of the same key cannot both win.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else dispatch_outbox_fence_path(home)
        self.sidecar_path = self.path.with_name(
            self.path.name + DISPATCH_OUTBOX_FENCE_SIDECAR_SUFFIX
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
        """(Re)create the DB fresh, stamp the schema version + the store nonce, write sidecar."""
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
            conn.execute(f"PRAGMA user_version = {DISPATCH_OUTBOX_FENCE_SCHEMA_VERSION}")
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    # -- bootstrap / recover -----------------------------------------------

    def bootstrap(self) -> None:
        """Initial-only creation of the fence store + its DB-external identity (mid-review F1).

        The **only** initial-creation path. A reserve never auto-creates a missing store —
        auto-creation would resurrect a **deleted / replaced** store and let an already
        ``delivered`` action re-send. Behavior:

        - **both** the DB and the sidecar absent (a genuine first bootstrap) -> mint a random
          ``store_nonce``, create the DB + sidecar together at that nonce.
        - DB and sidecar co-exist at the same nonce -> idempotent no-op.
        - **any** other state — sidecar present but DB missing / mismatched, OR the DB present
          but the sidecar missing (a *sidecar-only* loss that would otherwise let a fresh
          bootstrap unlink a durable DB, mid-review j#75065 F1), OR a nonce mismatch -> **fail
          closed** (:class:`DispatchOutboxFenceError`): an inconsistent single-sided store is a
          loss / replacement and must go through the deliberate :meth:`recover`, never a silent
          re-create that would destroy or re-enable an already-delivered action.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        db_exists = self.path.exists()
        if sidecar_nonce is None and not db_exists:
            self._create_fresh(secrets.token_hex(16))  # both absent: the only genuine first init.
            return
        if self.is_bootstrapped():
            return  # DB + sidecar co-exist at the same nonce: already bootstrapped.
        raise DispatchOutboxFenceError(
            f"dispatch outbox fence {self.path} is in an inconsistent state (only one of the DB "
            f"/ sidecar exists, or their nonces differ): a store loss or replacement. Refusing "
            f"to silently re-create (which could destroy or re-enable a delivered action). Use "
            f"recover() for a deliberate, operator-gated loss recovery."
        )

    def recover(self) -> None:
        """Deliberate operator loss-recovery: mint a NEW nonce and a fresh DB (mid-review F1).

        The explicit surface an operator invokes AFTER reconciling the lost action in Redmine
        (superseding it + issuing a new ``action_id``). It replaces the (lost / corrupt) store
        with a fresh DB under a brand-new nonce, so any lingering old DB is invalidated. Distinct
        from :meth:`bootstrap` (initial only): this is intentional, and the upstream reconcile —
        not this store — is what stops the old action from re-sending.
        """
        self._create_fresh(secrets.token_hex(16))

    def is_bootstrapped(self) -> bool:
        """True when the DB and sidecar co-exist at the same nonce and schema version (fail-soft)."""
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return False
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != DISPATCH_OUTBOX_FENCE_SCHEMA_VERSION:
                return False
            return self._db_nonce(conn) == sidecar_nonce
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return False
        finally:
            conn.close()

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open an **existing, identity-matched** manual-transaction connection, or fail closed.

        A reserve never creates the container (mid-review j#75047 F1). The DB and its
        DB-external sidecar must co-exist at the **same** ``store_nonce``: a **missing** file
        (never bootstrapped / deleted), an **empty** ``user_version=0`` swap-in, a **foreign** /
        unrecognized-version file, and a **nonce-mismatched replacement** all fail closed via
        :class:`DispatchOutboxFenceError` — the caller must not send, because the idempotency
        authority is not the one this store was bootstrapped as, so a send could duplicate an
        already-delivered action. Recovery is operator-gated (:meth:`recover` + a new
        ``action_id`` from an upstream reconcile).
        """
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None:
            raise DispatchOutboxFenceError(
                f"dispatch outbox fence {self.path} has no identity sidecar (never bootstrapped "
                f"/ lost); fail closed rather than risk a duplicate send"
            )
        if not self.path.exists():
            raise DispatchOutboxFenceError(
                f"dispatch outbox fence {self.path} DB is missing while its sidecar remains "
                f"(store loss); fail closed rather than auto-create and risk a duplicate send"
            )
        # ``isolation_level=None`` -> autocommit; we drive BEGIN IMMEDIATE / COMMIT ourselves.
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != DISPATCH_OUTBOX_FENCE_SCHEMA_VERSION:
                raise DispatchOutboxFenceError(
                    f"dispatch outbox fence {self.path} is not a bootstrapped fence at version "
                    f"{DISPATCH_OUTBOX_FENCE_SCHEMA_VERSION} (found {version}: empty / replaced / "
                    f"foreign store); fail closed rather than risk a duplicate send"
                )
            if self._db_nonce(conn) != sidecar_nonce:
                raise DispatchOutboxFenceError(
                    f"dispatch outbox fence {self.path} nonce does not match its sidecar "
                    f"(replaced / foreign store); fail closed rather than risk a duplicate send"
                )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise DispatchOutboxFenceError(
                f"dispatch outbox fence {self.path} is unreadable ({type(exc).__name__}); "
                f"fail closed rather than risk a duplicate send"
            ) from exc
        except DispatchOutboxFenceError:
            conn.close()
            raise
        return conn

    # -- reserve -----------------------------------------------------------

    def reserve(self, key: FenceKey, *, now: Optional[str] = None) -> ReserveResult:
        """Atomically reserve the key for a single send, or report never-send (fail-closed).

        Takes the write lock (``BEGIN IMMEDIATE``) before reading, so a concurrent caller of the
        same key blocks and then sees this row. For a fresh key: writes a
        :data:`FENCE_RESERVED` row and returns ``won=True``. For an existing key: returns
        ``won=False`` with the prior state — a still-:data:`FENCE_RESERVED` row (crash window)
        is transitioned to :data:`FENCE_UNCERTAIN` and flagged ``needs_reconcile`` (never
        auto-retried); :data:`FENCE_DELIVERED` / :data:`FENCE_UNCERTAIN` / :data:`FENCE_CANCELLED`
        are returned as-is. Raises :class:`DispatchOutboxFenceError` (do-not-send) on a corrupt
        store or any transaction failure.
        """
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM dispatch_outbox WHERE workspace_id=? AND lane_id=? AND "
                "issue=? AND journal=? AND action_id=? AND target_assigned_name=?",
                key.as_row(),
            ).fetchone()
            if row is None:
                try:
                    conn.execute(
                        "INSERT INTO dispatch_outbox (workspace_id, lane_id, issue, journal, "
                        "action_id, target_assigned_name, state, detail, reserved_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (*key.as_row(), FENCE_RESERVED, "", stamp, stamp),
                    )
                except sqlite3.IntegrityError:
                    # Lost a concurrent INSERT race (UNIQUE backstop); the winner reserved it.
                    conn.execute("ROLLBACK")
                    return ReserveResult(
                        won=False,
                        prior_state=FENCE_RESERVED,
                        current_state=FENCE_RESERVED,
                        needs_reconcile=False,
                        detail="lost a concurrent reserve race; the other caller sends",
                    )
                conn.execute("COMMIT")
                return ReserveResult(
                    won=True,
                    prior_state=FENCE_ABSENT,
                    current_state=FENCE_RESERVED,
                    detail="reserved a fresh key for the single send",
                )
            prior = str(row[0])
            if prior == FENCE_RESERVED:
                # Crash window: a prior reserve exists but never resolved to delivered/uncertain.
                # The prior send's fate is unknown -> surface uncertain, never auto-retry.
                conn.execute(
                    "UPDATE dispatch_outbox SET state=?, detail=?, updated_at=? WHERE "
                    "workspace_id=? AND lane_id=? AND issue=? AND journal=? AND action_id=? "
                    "AND target_assigned_name=?",
                    (
                        FENCE_UNCERTAIN,
                        "re-entered a reserved key (crash window); prior send outcome unknown",
                        stamp,
                        *key.as_row(),
                    ),
                )
                conn.execute("COMMIT")
                return ReserveResult(
                    won=False,
                    prior_state=FENCE_RESERVED,
                    current_state=FENCE_UNCERTAIN,
                    needs_reconcile=True,
                    detail="prior reserve unresolved; marked uncertain for operator reconcile",
                )
            conn.execute("ROLLBACK")
            return ReserveResult(
                won=False,
                prior_state=prior,
                current_state=prior,
                needs_reconcile=(prior == FENCE_UNCERTAIN),
                detail=f"key already {prior}; never-send",
            )
        except DispatchOutboxFenceError:
            raise
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise DispatchOutboxFenceError(
                f"dispatch outbox fence reserve failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- outcome writes ----------------------------------------------------

    def _set_state(self, key: FenceKey, state: str, detail: str, *, now: Optional[str]) -> bool:
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE dispatch_outbox SET state=?, detail=?, updated_at=? WHERE workspace_id=? "
                "AND lane_id=? AND issue=? AND journal=? AND action_id=? AND target_assigned_name=?",
                (state, detail, stamp, *key.as_row()),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise DispatchOutboxFenceError(
                f"dispatch outbox fence update failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def mark_delivered(self, key: FenceKey, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved key's send as positively delivered (turn-start confirmed)."""
        return self._set_state(key, FENCE_DELIVERED, detail or "send delivered", now=now)

    def mark_uncertain(self, key: FenceKey, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved key's send outcome as unknown (crash / timeout) -> reconcile."""
        return self._set_state(key, FENCE_UNCERTAIN, detail or "send outcome uncertain", now=now)

    def mark_cancelled(self, key: FenceKey, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Record the reserved key as cancelled (a durable supersede confirmed before the send)."""
        return self._set_state(key, FENCE_CANCELLED, detail or "cancelled before send", now=now)

    def record_uncertain(self, key: FenceKey, *, detail: str = "", now: Optional[str] = None) -> bool:
        """Force the key to :data:`FENCE_UNCERTAIN`, INSERTING the row if it is missing.

        Unlike :meth:`mark_uncertain` — an UPDATE that no-ops (rowcount 0) when the row is
        gone — this **upserts**: it re-creates the row as ``uncertain`` when a prior reserve's
        row has VANISHED (a store-level single-row loss between the reserve and the outcome
        write). Re-asserting ``uncertain`` keeps the fence — the *sole* exactly-once authority
        — holding a never-send state for the key, so a later reserve (even one driven by a
        caller that re-read a stale durable pointer) sees ``uncertain`` and never sends. It can
        only ever move a key TOWARD the fail-closed uncertain terminal: a positively resolved
        row (:data:`FENCE_DELIVERED` / :data:`FENCE_CANCELLED`) is left untouched, so a real
        delivery is never downgraded. Raises :class:`DispatchOutboxFenceError` only when the
        WHOLE store is lost / corrupt (a re-run then fails closed on :meth:`_connect` anyway).
        """
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO dispatch_outbox (workspace_id, lane_id, issue, journal, action_id, "
                "target_assigned_name, state, detail, reserved_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, lane_id, issue, journal, action_id, target_assigned_name) "
                "DO UPDATE SET state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at "
                "WHERE dispatch_outbox.state NOT IN (?, ?)",
                (
                    *key.as_row(),
                    FENCE_UNCERTAIN,
                    detail or "reserved row missing at outcome write; forced uncertain",
                    stamp,
                    stamp,
                    FENCE_DELIVERED,
                    FENCE_CANCELLED,
                ),
            )
            conn.execute("COMMIT")
            return True
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise DispatchOutboxFenceError(
                f"dispatch outbox fence record_uncertain failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- reads -------------------------------------------------------------

    def _genuinely_uninitialized(self) -> bool:
        """True only when BOTH the DB and its sidecar are absent. (tri-state, #13892 R2-F1)

        The artifact shape has three cases, and only the first is an absence:

        - **both absent** — no bootstrap ever ran; nothing can be recorded here;
        - **both present** — the store must prove its identity through :meth:`_connect`;
        - **exactly one present** — a loss / replacement. NOT an absence: the missing half is
          evidence something was here.

        :meth:`is_bootstrapped` deliberately collapses every non-healthy case to ``False``
        (it is a fail-soft diagnostic), so a gate that reads its ``False`` as "nothing is
        recorded here" turns store damage into a silent permission. Gates use this instead.
        """
        return not self.sidecar_path.exists() and not self.path.exists()

    def obligations_for_targets(
        self, *, workspace_id: str, target_assigned_names: Sequence[str]
    ) -> tuple["TargetObligation", ...]:
        """EVERY fence row aimed at any of these assigned names. (read-only, #13892)

        Returns a :class:`TargetObligation` per row — target, state, and the full causal
        identity (issue / journal / action_id) — for the durable obligations a *destructive*
        action against those slots must not run over.

        **Every** state is returned, not only the non-terminal ones (review j#80523 R2-F2).
        ``reserved`` (a send took the write lock, fate unknown) and ``uncertain`` (outcome
        never resolved) are obviously owed. ``delivered`` is NOT self-evidently discharged:
        it is a *turn-start delivery ACK*, and the workflow contract is explicit that a
        delivery ACK is not task completion — the work it handed to that slot may still be
        owed in Redmine. Filtering ``delivered`` out here would silently make delivery stand
        in for completion, so the caller receives it WITH its issue / journal identity and
        must correlate it against the durable gate (and fail closed when it cannot).

        No runtime ``idle`` / ``turn_ended`` observation can rule any of these out: receiver
        state and durable obligation are different axes.

        :meth:`state_of` cannot answer this: it requires the full six-part
        :class:`FenceKey` — an action-time caller that only knows *which panes it is about to
        close* has no issue / journal / action_id to build one, so it could never enumerate
        what is owed to a target. Hence this bounded by-target read.

        Rows keep their **full causal identity** (issue / journal / action_id), not just the
        state: a caller cannot correlate a delivered send with its Redmine gate — nor name what
        is owed in a durable record — from a bare state string (review j#80523 R2-F2).

        Unlike :meth:`state_of` (a fail-soft per-key diagnostic) this is a **gate input**, so
        it fails closed. Only a *genuinely uninitialized* store (BOTH artifacts absent) returns
        empty; every other shape — DB-only, sidecar-only, nonce mismatch, schema mismatch,
        corrupt — raises :class:`DispatchOutboxFenceError` rather than reporting "no
        obligations". Not observing an obligation is never the same as there being none.
        """
        names = tuple(dict.fromkeys(n for n in target_assigned_names if n))
        if not names:
            return ()
        if self._genuinely_uninitialized():
            # BOTH artifacts absent: no bootstrap ever ran here, so no reserve can ever have
            # been recorded. This is the ONE shape that is a positive absence.
            #
            # `is_bootstrapped()` must NOT be used for this decision (review j#80523 R2-F1):
            # it is a fail-soft predicate whose False also covers DB-only, sidecar-only, nonce
            # mismatch, schema mismatch and corrupt — five damage shapes that were all being
            # reported as "no obligations owed" and closing panes over unknown owed work.
            return ()
        # Any other shape must prove itself through `_connect`, which fails closed on a
        # missing / empty / foreign / nonce-mismatched store.
        conn = self._connect()
        try:
            placeholders = ", ".join("?" * len(names))
            rows = conn.execute(
                "SELECT target_assigned_name, state, issue, journal, action_id, "
                "workspace_id, lane_id "
                "FROM dispatch_outbox "
                f"WHERE workspace_id=? AND target_assigned_name IN ({placeholders}) "
                "ORDER BY target_assigned_name, action_id",
                (workspace_id, *names),
            ).fetchall()
        finally:
            conn.close()
        return tuple(
            TargetObligation(
                target_assigned_name=str(r[0]),
                state=str(r[1]),
                issue=str(r[2]),
                journal=str(r[3]),
                action_id=str(r[4]),
                workspace_id=str(r[5]),
                lane_id=str(r[6]),
            )
            for r in rows
        )

    def state_of(self, key: FenceKey) -> str:
        """The current fence state for the key, or :data:`FENCE_ABSENT` (fail-soft diagnostic).

        A read-only diagnostic (not a send gate): an un-bootstrapped / missing store simply has
        no state for the key (:data:`FENCE_ABSENT`) rather than raising.
        """
        if not self.is_bootstrapped():
            return FENCE_ABSENT
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state FROM dispatch_outbox WHERE workspace_id=? AND lane_id=? AND "
                "issue=? AND journal=? AND action_id=? AND target_assigned_name=?",
                key.as_row(),
            ).fetchone()
            return str(row[0]) if row is not None else FENCE_ABSENT
        finally:
            conn.close()


__all__ = (
    "DISPATCH_OUTBOX_FENCE_FILENAME",
    "TargetObligation",
    "DISPATCH_OUTBOX_FENCE_SIDECAR_SUFFIX",
    "DISPATCH_OUTBOX_FENCE_SCHEMA_VERSION",
    "FENCE_RESERVED",
    "FENCE_DELIVERED",
    "FENCE_UNCERTAIN",
    "FENCE_CANCELLED",
    "FENCE_ABSENT",
    "FENCE_STATES",
    "DispatchOutboxFenceError",
    "dispatch_outbox_fence_path",
    "FenceKey",
    "ReserveResult",
    "DispatchOutboxFence",
)
