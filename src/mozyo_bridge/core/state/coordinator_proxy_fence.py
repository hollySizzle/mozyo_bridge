"""Home-scoped exactly-once fence for external-client coordinator delegations (Redmine #14546).

``workflow proxy`` lets an **external** coordinator client — an operator shell or API caller that
is not itself an attested lane agent — hand one *already durably resolved* high-level action to the
live attested default coordinator. "Exactly once" is the whole point: the caller has no runtime of
its own, so a retry, a crash mid-send, or a second operator running the same command must not
deliver the action twice.

This store is the authority for that. It is keyed on the **route identity**
``(workspace_id, lane_id, role, action)`` — deliberately NOT on the target's live assigned name,
which is an action-time attestation, so a coordinator restart / rename can never advance a
generation. Each route holds exactly one generation row, and that row records the **durable
decision it delegated** (``issue`` + ``journal``). That extra pair is what makes this fence
different from its siblings, and it is required by the failure it guards:

- a repeat of the SAME ``(issue, journal)`` is a duplicate **even after the generation completed** —
  a durable decision is delegated once, full stop. A fence that re-opened on completion would let
  "run the command again" re-deliver a decision the coordinator already acted on;
- a delegation whose ``journal`` is OLDER than the one already delegated on this route is **stale**:
  the durable record moved on, and shipping a superseded decision is the same defect as shipping a
  duplicate. Redmine journal ids are monotonic per issue, so the comparison is a numeric one, and a
  non-numeric journal fails closed rather than sorting as a string.

Store identity mirrors the sibling fences (``forward_outbox_fence`` / the dispatch fence): a
DB-external ``store_nonce`` sidecar fails a deleted / replaced store **closed**, and :meth:`bootstrap`
/ :meth:`recover` are operator-only — the execution path never auto-creates the store, because a
silent re-create after a loss would let an already-delivered delegation be sent again.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

COORDINATOR_PROXY_FENCE_FILENAME = "coordinator-proxy-fence.sqlite"
COORDINATOR_PROXY_FENCE_SIDECAR_SUFFIX = ".anchor"
COORDINATOR_PROXY_FENCE_SCHEMA_VERSION = 1

# The closed generation-state vocabulary (mirrors the sibling forward fence).
PROXY_RESERVED = "reserved"  # minted + write-locked before the send; the send's fate is unknown
PROXY_DELIVERED = "delivered"  # the delegation was positively delivered to the coordinator
PROXY_UNCERTAIN = "uncertain"  # the send outcome is unknown (crash / timeout) -> operator reconcile
PROXY_COMPLETED = "completed"  # the coordinator acknowledged; the route may take a NEWER decision
PROXY_ABANDONED = "abandoned"  # an operator proved the send never left; the route may move on
PROXY_ABSENT = "absent"  # sentinel: no row existed for the route (not persisted)

PROXY_STATES = frozenset(
    {PROXY_RESERVED, PROXY_DELIVERED, PROXY_UNCERTAIN, PROXY_COMPLETED, PROXY_ABANDONED}
)
#: The states that hold an IN-FLIGHT generation (a repeat is a duplicate zero-send). ``delivered``
#: left this set with Design Answer j#90329 contract 1: a positively recorded delivery is terminal,
#: not in flight, because the proxy's job ends at delivery.
_ACTIVE_STATES = frozenset({PROXY_RESERVED, PROXY_UNCERTAIN})

#: The TERMINAL states a strictly newer canonical decision may mint past (Design Answer j#90329
#: contract 1). ``delivered`` is now one of them: a positively recorded delivery IS the proxy's
#: terminal success, because the proxy delivers a decision and does not — and cannot — prove the
#: coordinator acted on it. ``completed`` remains readable as a LEGACY terminal written by the
#: withdrawn acknowledgement path; nothing produces it any more. ``abandoned`` is the operator's
#: proven-not-sent disposition, which deliberately re-opens retry.
_TERMINAL_STATES = frozenset({PROXY_DELIVERED, PROXY_COMPLETED, PROXY_ABANDONED})

# Reserve verdicts (why a reserve did or did not win). Machine-readable.
RESERVE_WON = "won"
RESERVE_DUPLICATE = "duplicate"  # same (issue, journal) already delegated, or a generation in flight
RESERVE_STALE = "stale"  # this journal is older than the one already delegated on this route
RESERVE_NEEDS_RECONCILE = "needs_reconcile"  # a prior reserve never resolved (crash window)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS proxy_generation (
    workspace_id     TEXT NOT NULL,
    lane_id          TEXT NOT NULL,
    role             TEXT NOT NULL,
    action           TEXT NOT NULL,
    proxy_action_id  TEXT NOT NULL,
    issue            TEXT NOT NULL,
    journal          TEXT NOT NULL,
    state            TEXT NOT NULL,
    detail           TEXT NOT NULL DEFAULT '',
    reserved_at      TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(workspace_id, lane_id, role, action)
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"


class CoordinatorProxyFenceError(RuntimeError):
    """The proxy store could not be opened at the expected schema (fail-closed = do-not-send)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def coordinator_proxy_fence_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``coordinator-proxy-fence.sqlite`` path under the mozyo-bridge home."""
    return (home or mozyo_bridge_home()) / COORDINATOR_PROXY_FENCE_FILENAME


def mint_proxy_action_id() -> str:
    """Mint an opaque, unguessable proxy action id (never a role / approval / anchor authority)."""
    return "pxy_" + secrets.token_hex(16)


def journal_ordinal(journal: object) -> Optional[int]:
    """The numeric ordinal of a Redmine journal id, or ``None`` when it is not numeric (pure).

    Redmine journal ids are monotonically increasing integers, which is what makes "older than the
    delegated one" decidable. A non-numeric / empty value returns ``None`` so the caller fails
    closed instead of falling back to a lexicographic comparison (``"9" > "10"`` as strings).
    """
    token = str(journal or "").strip()
    if not token.isdigit():
        return None
    return int(token)


@dataclass(frozen=True)
class ProxyRouteKey:
    """The UNIQUE route identity a proxy generation series is keyed on (target-name-free)."""

    workspace_id: str
    lane_id: str
    role: str
    action: str

    def as_row(self) -> tuple[str, str, str, str]:
        return (self.workspace_id, self.lane_id, self.role, self.action)


@dataclass(frozen=True)
class ProxyReserveResult:
    """The outcome of a :meth:`CoordinatorProxyFence.reserve` attempt.

    ``won`` is True only when this call minted + wrote a fresh :data:`PROXY_RESERVED` generation
    (the single caller cleared to deliver). ``verdict`` names why on a loss
    (:data:`RESERVE_DUPLICATE` / :data:`RESERVE_STALE` / :data:`RESERVE_NEEDS_RECONCILE`).
    ``prior_issue`` / ``prior_journal`` describe the decision already on the route, so the caller
    can report *which* decision blocks this one instead of an opaque refusal.
    """

    won: bool
    verdict: str
    action_id: str = ""
    prior_state: str = PROXY_ABSENT
    prior_issue: str = ""
    prior_journal: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ProxyGeneration:
    """The route's current generation, or absent."""

    action_id: str
    state: str
    issue: str = ""
    journal: str = ""

    @property
    def absent(self) -> bool:
        return self.state == PROXY_ABSENT


class CoordinatorProxyFence:
    """Read/write access to the home-scoped coordinator-proxy generation store."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else coordinator_proxy_fence_path(home)
        self.sidecar_path = self.path.with_name(
            self.path.name + COORDINATOR_PROXY_FENCE_SIDECAR_SUFFIX
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
            conn.execute(f"PRAGMA user_version = {COORDINATOR_PROXY_FENCE_SCHEMA_VERSION}")
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    # -- bootstrap / recover (operator-only; the execution path never auto-creates) --

    def bootstrap(self) -> None:
        """Initial-only creation of the store + its DB-external identity (operator action)."""
        sidecar_nonce = self._read_sidecar_nonce()
        db_exists = self.path.exists()
        if sidecar_nonce is None and not db_exists:
            self._create_fresh(secrets.token_hex(16))
            return
        if self.is_bootstrapped():
            return
        raise CoordinatorProxyFenceError(
            f"proxy store {self.path} is in an inconsistent state (only one of the DB / sidecar "
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
            if version != COORDINATOR_PROXY_FENCE_SCHEMA_VERSION:
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
            raise CoordinatorProxyFenceError(
                f"proxy store {self.path} has no identity sidecar (never bootstrapped / lost); "
                f"fail closed rather than risk a duplicate delegation"
            )
        if not self.path.exists():
            raise CoordinatorProxyFenceError(
                f"proxy store {self.path} DB is missing while its sidecar remains (store loss); "
                f"fail closed rather than auto-create and risk a duplicate delegation"
            )
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != COORDINATOR_PROXY_FENCE_SCHEMA_VERSION:
                raise CoordinatorProxyFenceError(
                    f"proxy store {self.path} is not a bootstrapped store at version "
                    f"{COORDINATOR_PROXY_FENCE_SCHEMA_VERSION} (found {version}: empty / replaced / "
                    f"foreign store); fail closed rather than risk a duplicate delegation"
                )
            if self._db_nonce(conn) != sidecar_nonce:
                raise CoordinatorProxyFenceError(
                    f"proxy store {self.path} nonce does not match its sidecar (replaced / "
                    f"foreign store); fail closed rather than risk a duplicate delegation"
                )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise CoordinatorProxyFenceError(
                f"proxy store {self.path} is unreadable ({type(exc).__name__}); fail closed"
            ) from exc
        except CoordinatorProxyFenceError:
            conn.close()
            raise
        return conn

    # -- reserve -----------------------------------------------------------

    def reserve(
        self, route: ProxyRouteKey, *, issue: str, journal: str, now: Optional[str] = None
    ) -> ProxyReserveResult:
        """Reserve the single delegation of ``(issue, journal)`` on ``route``, or refuse.

        Wins only when the route is fresh, or when its prior generation is terminal
        (:data:`PROXY_DELIVERED` / :data:`PROXY_ABANDONED` / legacy :data:`PROXY_COMPLETED`) for a
        **strictly older** journal on the same issue. Refuses with:

        - :data:`RESERVE_DUPLICATE` — a generation is in flight (reserved / uncertain), or ANY
          generation already delegated this exact ``(issue, journal)``. A decision is delegated
          once, whatever state its generation reached;
        - :data:`RESERVE_STALE` — the terminal generation delegated a NEWER journal, so this
          decision is superseded;
        - :data:`RESERVE_NEEDS_RECONCILE` — a prior reserve never resolved (crash window). The row
          is moved to :data:`PROXY_UNCERTAIN` and never auto-retried.

        A non-numeric journal (on either side) refuses as :data:`RESERVE_STALE` rather than guessing
        an order. Raises :class:`CoordinatorProxyFenceError` (do-not-send) on a corrupt store.
        """
        stamp = now or _utc_now()
        want_issue = (issue or "").strip()
        want_journal = (journal or "").strip()
        want_ordinal = journal_ordinal(want_journal)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT proxy_action_id, state, issue, journal FROM proxy_generation WHERE "
                "workspace_id=? AND lane_id=? AND role=? AND action=?",
                route.as_row(),
            ).fetchone()
            if row is None:
                if want_ordinal is None:
                    conn.execute("ROLLBACK")
                    return ProxyReserveResult(
                        won=False, verdict=RESERVE_STALE,
                        detail=f"journal {want_journal!r} is not a numeric Redmine journal id",
                    )
                action_id = mint_proxy_action_id()
                conn.execute(
                    "INSERT INTO proxy_generation (workspace_id, lane_id, role, action, "
                    "proxy_action_id, issue, journal, state, detail, reserved_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *route.as_row(), action_id, want_issue, want_journal,
                        PROXY_RESERVED, "", stamp, stamp,
                    ),
                )
                conn.execute("COMMIT")
                return ProxyReserveResult(
                    won=True, verdict=RESERVE_WON, action_id=action_id,
                    detail="minted the first delegation generation for this route",
                )

            prior_action, prior_state = str(row[0]), str(row[1])
            prior_issue, prior_journal = str(row[2]), str(row[3])

            if prior_state == PROXY_RESERVED:
                conn.execute(
                    "UPDATE proxy_generation SET state=?, detail=?, updated_at=? WHERE "
                    "workspace_id=? AND lane_id=? AND role=? AND action=?",
                    (
                        PROXY_UNCERTAIN,
                        "re-entered a reserved generation (crash window); prior delegation outcome "
                        "unknown",
                        stamp,
                        *route.as_row(),
                    ),
                )
                conn.execute("COMMIT")
                return ProxyReserveResult(
                    won=False, verdict=RESERVE_NEEDS_RECONCILE, prior_state=PROXY_RESERVED,
                    prior_issue=prior_issue, prior_journal=prior_journal,
                    detail="prior reserve unresolved; marked uncertain for operator reconcile",
                )

            # The SAME durable decision is never delegated twice, whatever state its generation
            # reached — delivered, completed (legacy), abandoned, uncertain or still reserved
            # (Design Answer j#90329 contract 1). This is checked before the state split so a
            # terminal state can never re-open the decision that produced it.
            if prior_issue == want_issue and prior_journal == want_journal:
                conn.execute("ROLLBACK")
                return ProxyReserveResult(
                    won=False, verdict=RESERVE_DUPLICATE, action_id=prior_action,
                    prior_state=prior_state, prior_issue=prior_issue, prior_journal=prior_journal,
                    detail=(
                        "this exact durable decision was already delegated on this route "
                        f"(generation {prior_state}); a decision is delegated once"
                    ),
                )

            if prior_state in _ACTIVE_STATES:
                conn.execute("ROLLBACK")
                return ProxyReserveResult(
                    won=False, verdict=RESERVE_DUPLICATE, action_id=prior_action,
                    prior_state=prior_state, prior_issue=prior_issue, prior_journal=prior_journal,
                    detail=(
                        f"a delegation generation is already {prior_state} for issue "
                        f"{prior_issue} journal {prior_journal}"
                    ),
                )

            if prior_state not in _TERMINAL_STATES:
                conn.execute("ROLLBACK")
                return ProxyReserveResult(
                    won=False, verdict=RESERVE_DUPLICATE, action_id=prior_action,
                    prior_state=prior_state, prior_issue=prior_issue, prior_journal=prior_journal,
                    detail=f"generation state {prior_state!r} is not terminal; never-send",
                )
            prior_ordinal = journal_ordinal(prior_journal)
            if want_ordinal is None or prior_ordinal is None or want_ordinal < prior_ordinal:
                conn.execute("ROLLBACK")
                return ProxyReserveResult(
                    won=False, verdict=RESERVE_STALE, action_id=prior_action,
                    prior_state=prior_state, prior_issue=prior_issue, prior_journal=prior_journal,
                    detail=(
                        f"journal {want_journal!r} does not supersede the already-delegated "
                        f"journal {prior_journal!r} on this route"
                    ),
                )
            action_id = mint_proxy_action_id()
            conn.execute(
                "UPDATE proxy_generation SET proxy_action_id=?, issue=?, journal=?, state=?, "
                "detail=?, reserved_at=?, updated_at=? WHERE workspace_id=? AND lane_id=? AND "
                "role=? AND action=?",
                (
                    action_id, want_issue, want_journal, PROXY_RESERVED, "", stamp, stamp,
                    *route.as_row(),
                ),
            )
            conn.execute("COMMIT")
            return ProxyReserveResult(
                won=True, verdict=RESERVE_WON, action_id=action_id, prior_state=prior_state,
                prior_issue=prior_issue, prior_journal=prior_journal,
                detail=f"minted a new generation for a superseding decision (prior {prior_state})",
            )
        except CoordinatorProxyFenceError:
            raise
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CoordinatorProxyFenceError(
                f"proxy store reserve failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    # -- outcome / completion writes ---------------------------------------

    def _guarded_set(
        self, route: ProxyRouteKey, action_id: str, from_states, to_state: str, detail: str,
        *, now: Optional[str], issue: str = "", journal: str = "",
    ) -> bool:
        """CAS the route's generation, joined to the EXACT stored decision anchor.

        ``issue`` / ``journal`` are matched against the row's stored values when supplied (Design
        Answer j#90329 contract 3). Without that join a caller could name a different anchor and
        still advance this route's generation — the transition would be about one decision while the
        row records another.
        """
        stamp = now or _utc_now()
        placeholders = ",".join("?" for _ in from_states)
        anchor_sql = ""
        anchor_params: tuple = ()
        if issue or journal:
            anchor_sql = " AND issue=? AND journal=?"
            anchor_params = ((issue or "").strip(), (journal or "").strip())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE proxy_generation SET state=?, detail=?, updated_at=? WHERE workspace_id=? "
                "AND lane_id=? AND role=? AND action=? AND proxy_action_id=? AND state IN "
                f"({placeholders}){anchor_sql}",
                (to_state, detail, stamp, *route.as_row(), action_id, *from_states, *anchor_params),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CoordinatorProxyFenceError(
                f"proxy store update failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def mark_delivered(
        self, route: ProxyRouteKey, action_id: str, *, detail: str = "", now: Optional[str] = None,
        issue: str = "", journal: str = "",
    ) -> bool:
        """Record the reserved generation's delegation as delivered — the proxy's TERMINAL success.

        The proxy delivers a decision; it does not, and cannot, prove the coordinator acted on it
        (Design Answer j#90329 contract 1). So a positively recorded delivery ends this decision's
        life on the route: the same ``(issue, journal)`` is duplicate forever, and only a strictly
        newer canonical decision mints the next generation.
        """
        return self._guarded_set(
            route, action_id, (PROXY_RESERVED,), PROXY_DELIVERED,
            detail or "delegation delivered to the live default coordinator", now=now,
            issue=issue, journal=journal,
        )

    def mark_abandoned(
        self, route: ProxyRouteKey, action_id: str, *, detail: str, now: Optional[str] = None,
        issue: str = "", journal: str = "",
    ) -> bool:
        """Record that an operator PROVED the send never left — the unwedging terminal (contract 4).

        It releases the route so the coordinator's NEXT canonical decision can be delegated; it does
        not resurrect the decision it names, which stays delegated-once like every other terminal.
        Admitted only against the exact generation and its stored anchor, and only from
        :data:`PROXY_UNCERTAIN`: a delivery that landed, or one whose fate is still being
        established, is not abandonable.
        """
        return self._guarded_set(
            route, action_id, (PROXY_UNCERTAIN,), PROXY_ABANDONED, detail, now=now,
            issue=issue, journal=journal,
        )

    def mark_uncertain(
        self, route: ProxyRouteKey, action_id: str, *, detail: str = "", now: Optional[str] = None,
        issue: str = "", journal: str = "",
    ) -> bool:
        """Record the reserved generation's outcome as unknown (crash / timeout) -> reconcile."""
        return self._guarded_set(
            route, action_id, (PROXY_RESERVED,), PROXY_UNCERTAIN,
            detail or "delegation outcome uncertain", now=now, issue=issue, journal=journal,
        )

    def confirm_delivered(
        self, route: ProxyRouteKey, action_id: str, *, detail: str, now: Optional[str] = None,
        issue: str = "", journal: str = "",
    ) -> bool:
        """Resolve an ``uncertain`` generation to ``delivered`` on proven evidence (contract 4).

        The reconcile's positive disposition: an operator established that the send DID land, so the
        generation reaches the proxy's terminal success and a strictly newer decision may follow.
        Advances only from :data:`PROXY_UNCERTAIN`, joined to the exact stored anchor.
        """
        return self._guarded_set(
            route, action_id, (PROXY_UNCERTAIN,), PROXY_DELIVERED, detail, now=now,
            issue=issue, journal=journal,
        )

    def complete(
        self, route: ProxyRouteKey, action_id: str, *, detail: str = "", now: Optional[str] = None
    ) -> bool:
        """LEGACY: the withdrawn acknowledgement transition (Design Answer j#90329 contract 2).

        Nothing in the product calls this any more. ``completed`` rows written by the withdrawn ack
        path stay readable as a terminal state, but completion is no longer an authority the proxy
        claims — delivery is. Kept only so an existing row's history is interpretable.

        Completing does NOT re-open the route for the same decision: :meth:`reserve` still refuses a
        repeat of the completed ``(issue, journal)`` as a duplicate. Only a strictly newer journal
        may mint the next generation.
        """
        return self._guarded_set(
            route, action_id, (PROXY_DELIVERED,), PROXY_COMPLETED,
            detail or "coordinator acknowledged the delegated action", now=now,
        )

    def complete_by_action_id(
        self, action_id: str, *, workspace_id: str, detail: str = "", now: Optional[str] = None
    ) -> bool:
        """Complete the EXACT delivered generation carrying ``action_id`` in ``workspace_id``.

        The acknowledgement hook the coordinator's ack surface drives (review j#89918 finding 1).
        The opaque, globally-unique ``proxy_action_id`` already pins one generation, so the route's
        lane / role / action are not required in the match; the ``workspace_id`` is the cross-check
        that rejects an id replayed against a different workspace.

        Advances **only** from :data:`PROXY_DELIVERED`. A stale / unknown id, a generation that is
        still ``reserved``, one already ``completed``, or an ``uncertain`` one (which needs an
        explicit reconcile) all no-op and return ``False`` — an acknowledgement can never close a
        generation that was not positively delivered, and never a newer one than it names. Returns
        ``False`` (never raises) on a fail-closed store: a missing completion is safe, because the
        generation simply stays delivered until a real acknowledgement arrives.
        """
        aid = (action_id or "").strip()
        if not aid:
            return False
        try:
            conn = self._connect()
        except CoordinatorProxyFenceError:
            return False
        stamp = now or _utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE proxy_generation SET state=?, detail=?, updated_at=? WHERE workspace_id=? "
                "AND proxy_action_id=? AND state=?",
                (
                    PROXY_COMPLETED,
                    detail or "coordinator acknowledged the delegated action",
                    stamp,
                    (workspace_id or "").strip(),
                    aid,
                    PROXY_DELIVERED,
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

    def active(self, route: ProxyRouteKey) -> ProxyGeneration:
        """The route's current generation, or absent (fail-soft diagnostic)."""
        if not self.is_bootstrapped():
            return ProxyGeneration(action_id="", state=PROXY_ABSENT)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT proxy_action_id, state, issue, journal FROM proxy_generation WHERE "
                "workspace_id=? AND lane_id=? AND role=? AND action=?",
                route.as_row(),
            ).fetchone()
            if row is None:
                return ProxyGeneration(action_id="", state=PROXY_ABSENT)
            return ProxyGeneration(
                action_id=str(row[0]), state=str(row[1]), issue=str(row[2]), journal=str(row[3])
            )
        finally:
            conn.close()

    def is_active(self, route: ProxyRouteKey) -> bool:
        """True when the route currently holds a reserved / delivered / uncertain generation."""
        return self.active(route).state in _ACTIVE_STATES


__all__ = (
    "COORDINATOR_PROXY_FENCE_FILENAME",
    "COORDINATOR_PROXY_FENCE_SIDECAR_SUFFIX",
    "COORDINATOR_PROXY_FENCE_SCHEMA_VERSION",
    "PROXY_RESERVED",
    "PROXY_DELIVERED",
    "PROXY_UNCERTAIN",
    "PROXY_COMPLETED",
    "PROXY_ABSENT",
    "PROXY_STATES",
    "RESERVE_WON",
    "RESERVE_DUPLICATE",
    "RESERVE_STALE",
    "RESERVE_NEEDS_RECONCILE",
    "CoordinatorProxyFenceError",
    "coordinator_proxy_fence_path",
    "mint_proxy_action_id",
    "journal_ordinal",
    "ProxyRouteKey",
    "ProxyReserveResult",
    "ProxyGeneration",
    "CoordinatorProxyFence",
)
