"""The coordinator's audit-failure terminal decision, as mozyo-owned desired state (#15166).

Three review rounds established what this store exists to replace. The route terminalizes a
no-change verification lane whose failure was recorded by an independent audit rather than a
``## Gate: review``, and its hard question is never the lane's own facts — those are all
measurable — but the BINDING between "this audit failure" and "that successor's acceptance".
Each attempt to derive that binding from durable records was refuted by measurement:

- mutual acknowledgement between the two issues (review j#101880 finding 1): one unauthenticatable
  writer can place both halves, because every role posts under one source-system account (ruling
  #14219 j#86718);
- the successor's approved review examining the lane's exact head (review j#101909 finding 1): on
  a zero-change lane the lane head IS the integration head, so every unrelated approved issue on
  that base shares it;
- an enumeration hard-coded in the package (review j#102074 finding 1 / scope decision j#102081):
  it closed the hole but made every future lane of the same shape a product change, which is an
  individual migration rather than a supported rail.

The binding is a coordinator JUDGEMENT, and ``managed-state-model.md`` already says where a
judgement made at a mozyo command boundary lives. Its ``state_kinds`` table is the authority for
this module's existence:

- ``desired_state`` — "mozyo が command 境界で作成/採用/mark/rename しようとした構成・意図" — whose
  authority is *mozyo-owned persisted state*. A decision recorded here is authoritative for WHAT
  WAS DECIDED, within that classification and no further;
- ``side_effect_permission`` — whose authority is the mozyo command implementation, and whose
  meaning is spelled out as "persisted desired state + durable workflow gate + action-time live
  preflight を照合した結果". That conjunction is exactly the retire's fence: this record supplies
  the first term, the Redmine journals the second, and the live probes the third.

**What a record here does and does not establish, stated rather than implied.** It does NOT
authenticate a human, and nothing in this workspace can — that gap is unchanged. What it
establishes is that the decision was taken through the governed command boundary and written to a
surface a Redmine journal author cannot reach: no sequence of journal writes produces a row here.
That is the difference from all three refuted attempts, and it is the whole of the claim.

**Single use is not a state machine here; it is the lifecycle revision.** A decision is bound to
the lane's exact ``lane_generation`` AND ``revision`` at decision time, and every retire that
mutates the lane row advances that revision through the existing CAS. A decision therefore
authorizes at most one mutation and cannot be replayed against the world it left behind — using
the lifecycle generation the design direction (j#102092) names as one of its canonical sources,
rather than inventing a second consumption ledger that could disagree with it.

Store identity mirrors the sibling fences: a DB-external nonce sidecar makes a deleted / replaced
/ foreign store fail CLOSED. Unlike them there is no ``bootstrap`` / ``recover`` ceremony, because
the asymmetry here is simpler and safer: :meth:`record` — the coordinator's own action — creates
the store, and every read path refuses when it is absent. A lost store therefore cannot silently
admit anything; it can only require the coordinator to decide again.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION = 1
AUDIT_FAILURE_TERMINAL_DECISION_SIDECAR_SUFFIX = ".nonce"
_STORE_NONCE_KEY = "store_nonce"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_failure_terminal_decision (
    workspace_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    lane_generation INTEGER NOT NULL,
    lane_revision INTEGER NOT NULL,
    issue TEXT NOT NULL,
    audit_journal TEXT NOT NULL,
    successor_issue TEXT NOT NULL,
    successor_review_journal TEXT NOT NULL,
    head TEXT NOT NULL,
    integration_branch TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, lane_id)
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


class AuditFailureTerminalDecisionError(RuntimeError):
    """The decision store is absent, replaced, or unreadable; callers must fail closed."""


def audit_failure_terminal_decision_path(home: Optional[Path] = None) -> Path:
    """Resolve the decision store's path under the mozyo-bridge home (same shape as the fences)."""
    return (Path(home) if home is not None else mozyo_bridge_home()) / (
        "audit-failure-terminal-decision.sqlite"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint_decision_id() -> str:
    """A fresh opaque decision id. Minted by the STORE, never supplied by a caller."""
    return f"aft_{secrets.token_hex(16)}"


@dataclass(frozen=True)
class DecisionRoute:
    """The route one decision belongs to: a lane in a workspace.

    Deliberately NOT keyed on the issue. The retire resolves the route from the lane lifecycle row
    it is retiring, so the lookup cannot be pointed at another lane's decision by naming a
    different issue — the issue is one of the BOUND fields the decision must then match, not part
    of the key that selects it.
    """

    workspace_id: str
    lane_id: str

    def as_row(self) -> tuple[str, str]:
        return (self.workspace_id.strip(), self.lane_id.strip())


@dataclass(frozen=True)
class TerminalDecision:
    """One recorded coordinator decision, with every identity it is bound to.

    ``decision_id`` is the store's own minted handle. Every other field is an identity the retire
    re-measures from an independent source at action time — the declaration marker, the lane
    lifecycle row, the committed config, and the live checkout — so a decision authorizes exactly
    the world it was taken about and nothing that has drifted since.
    """

    workspace_id: str
    lane_id: str
    decision_id: str
    lane_generation: int
    lane_revision: int
    issue: str
    audit_journal: str
    successor_issue: str
    successor_review_journal: str
    head: str
    integration_branch: str
    recorded_at: str = ""

    @property
    def route(self) -> DecisionRoute:
        return DecisionRoute(self.workspace_id, self.lane_id)

    def as_payload(self) -> dict:
        """A credential-free projection for operator output and durable records."""
        return {
            "decision_id": self.decision_id,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "lane_generation": self.lane_generation,
            "lane_revision": self.lane_revision,
            "issue": self.issue,
            "audit_journal": self.audit_journal,
            "successor_issue": self.successor_issue,
            "successor_review_journal": self.successor_review_journal,
            "head": self.head,
            "integration_branch": self.integration_branch,
            "recorded_at": self.recorded_at,
        }


def _validation_errors(decision: TerminalDecision) -> "tuple[str, ...]":
    """Why this decision cannot identify a terminal (empty iff usable; pure).

    Checked at WRITE time so a record that could never match anything is never stored: a decision
    the retire can only ever refuse is an operator trap, not a fence.
    """
    problems: list[str] = []
    for field in (
        "workspace_id",
        "lane_id",
        "issue",
        "audit_journal",
        "successor_issue",
        "successor_review_journal",
        "head",
        "integration_branch",
    ):
        if not str(getattr(decision, field, "") or "").strip():
            problems.append(f"{field} is empty")
    for field in ("lane_generation", "lane_revision"):
        value = getattr(decision, field, None)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            problems.append(f"{field} must be a positive integer")
    if decision.issue.strip() == decision.successor_issue.strip():
        problems.append("issue and successor_issue must differ")
    head = str(decision.head or "").strip()
    if head and (len(head) not in (40, 64) or any(c not in "0123456789abcdef" for c in head)):
        problems.append("head must be a full lowercase 40/64-hex commit SHA")
    return tuple(problems)


class AuditFailureTerminalDecisionStore:
    """Read / write the coordinator's audit-failure terminal decisions (home-scoped).

    :meth:`record` is the coordinator's action and is the ONLY writer; it creates the store on
    first use. :meth:`read` never creates anything and raises when the store is absent, replaced or
    unreadable, so a retire whose decision surface is gone refuses rather than proceeding on the
    records alone.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = (
            Path(path) if path is not None else audit_failure_terminal_decision_path(home)
        )
        self.sidecar_path = self.path.with_name(
            self.path.name + AUDIT_FAILURE_TERMINAL_DECISION_SIDECAR_SUFFIX
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
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (_STORE_NONCE_KEY, nonce),
            )
            conn.execute(
                f"PRAGMA user_version = {AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION}"
            )
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    def is_initialized(self) -> bool:
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return False
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION:
                return False
            return self._db_nonce(conn) == sidecar_nonce
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return False
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Open an existing, identity-matched connection, or fail closed.

        Never creates. A missing sidecar, a missing DB beside a surviving sidecar, a foreign schema
        version and a nonce mismatch are each a store loss or replacement, and each refuses: an
        admission resting on a decision surface that is not demonstrably the one the coordinator
        wrote to is not an admission.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} has no identity sidecar (never recorded / lost); "
                "fail closed rather than admit a terminal with no recorded decision"
            )
        if not self.path.exists():
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} DB is missing while its sidecar remains (store loss); "
                "fail closed rather than auto-create"
            )
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION:
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.path} is not at schema version "
                    f"{AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION} (found {version}: empty / "
                    "replaced / foreign store); fail closed"
                )
            if self._db_nonce(conn) != sidecar_nonce:
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.path} nonce does not match its sidecar (replaced / "
                    "foreign store); fail closed"
                )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is unreadable ({type(exc).__name__}); fail closed"
            ) from exc
        except AuditFailureTerminalDecisionError:
            conn.close()
            raise
        return conn

    # -- the coordinator's action -----------------------------------------

    def record(self, decision: TerminalDecision, *, now: Optional[str] = None) -> TerminalDecision:
        """Record ONE coordinator decision for a route, minting its id (the writer path).

        Latest-wins per route, deliberately: a lane whose head or generation moved after a decision
        needs the coordinator to decide again about the world that now exists, and the natural way
        to express that is to record the new decision. What latest-wins does NOT do is widen
        anything — the replacement is still bound to its own exact identities, and the retire still
        re-measures every one of them.

        Raises :class:`AuditFailureTerminalDecisionError` on a decision that could never match
        (empty identity, non-positive generation / revision, self-successor, malformed head), so an
        unusable record is never stored.
        """
        problems = _validation_errors(decision)
        if problems:
            raise AuditFailureTerminalDecisionError(
                "refusing to record an audit-failure terminal decision that can never match: "
                + "; ".join(problems)
            )
        if not self.is_initialized():
            if self.path.exists() or self._read_sidecar_nonce() is not None:
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.path} exists but its identity does not verify "
                    "(replaced / foreign / half-written store); refusing to write into it"
                )
            self._create_fresh(secrets.token_hex(16))
        stamp = now or _utc_now()
        recorded = TerminalDecision(
            workspace_id=decision.workspace_id.strip(),
            lane_id=decision.lane_id.strip(),
            decision_id=mint_decision_id(),
            lane_generation=decision.lane_generation,
            lane_revision=decision.lane_revision,
            issue=decision.issue.strip(),
            audit_journal=decision.audit_journal.strip(),
            successor_issue=decision.successor_issue.strip(),
            successor_review_journal=decision.successor_review_journal.strip(),
            head=decision.head.strip().lower(),
            integration_branch=decision.integration_branch.strip(),
            recorded_at=stamp,
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO audit_failure_terminal_decision (workspace_id, lane_id, "
                "decision_id, lane_generation, lane_revision, issue, audit_journal, "
                "successor_issue, successor_review_journal, head, integration_branch, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recorded.workspace_id,
                    recorded.lane_id,
                    recorded.decision_id,
                    recorded.lane_generation,
                    recorded.lane_revision,
                    recorded.issue,
                    recorded.audit_journal,
                    recorded.successor_issue,
                    recorded.successor_review_journal,
                    recorded.head,
                    recorded.integration_branch,
                    recorded.recorded_at,
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return recorded

    # -- the retire's read -------------------------------------------------

    def read(self, route: DecisionRoute) -> Optional[TerminalDecision]:
        """The decision recorded for ``route``, or ``None`` when the route has none.

        Raises rather than returning ``None`` when the STORE itself cannot be trusted (see
        :meth:`_connect`): "this lane has no decision" and "the decision surface is gone" are
        different operational problems and the caller reports them differently, but both refuse.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT decision_id, lane_generation, lane_revision, issue, audit_journal, "
                "successor_issue, successor_review_journal, head, integration_branch, recorded_at "
                "FROM audit_failure_terminal_decision WHERE workspace_id=? AND lane_id=?",
                route.as_row(),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        if row is None:
            return None
        workspace_id, lane_id = route.as_row()
        return TerminalDecision(
            workspace_id=workspace_id,
            lane_id=lane_id,
            decision_id=str(row[0]),
            lane_generation=int(row[1]),
            lane_revision=int(row[2]),
            issue=str(row[3]),
            audit_journal=str(row[4]),
            successor_issue=str(row[5]),
            successor_review_journal=str(row[6]),
            head=str(row[7]),
            integration_branch=str(row[8]),
            recorded_at=str(row[9]),
        )


__all__ = (
    "AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION",
    "AUDIT_FAILURE_TERMINAL_DECISION_SIDECAR_SUFFIX",
    "AuditFailureTerminalDecisionError",
    "AuditFailureTerminalDecisionStore",
    "DecisionRoute",
    "TerminalDecision",
    "audit_failure_terminal_decision_path",
    "mint_decision_id",
)
