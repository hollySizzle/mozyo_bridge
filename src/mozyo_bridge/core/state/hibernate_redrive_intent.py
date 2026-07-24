"""Durable auto-hibernate redrive-intent store (Redmine #14219 T2c review j#86776 R5-F3).

A crash between the auto-hibernate CAS (``active -> hibernated``) and its process release
leaves a hibernated row whose managed slots may still be live. The next pass must FINISH that
release — but the public ``already_hibernated`` redrive needs the hibernate basis
(:class:`HibernateAssertions`) to decide whether the lane may still be released, and the
lifecycle row is only a decision POINTER: it carries the decision anchor, disposition, and
release state, never the basis conjuncts / provenance the CAS proved. Fabricating
``review_approved=True`` (etc.) from the generic hibernated disposition — the pre-R5 wiring —
manufactured a basis the row never recorded (review j#86776 R5-F3).

This store is that missing durable memory: at fresh actuation, immediately after the request is
derived and BEFORE the irreversible CAS, the runner persists a typed :class:`RedriveIntent`
(the exact workspace / lane / generation / issue / decision journal / basis / action id and the
derived durable assertion flags). A later redrive requires the intent to EXIST and to match the
row's decision journal / issue / generation / action; it then constructs the redrive's DURABLE
basis flags from the intent (never fabricated) and re-observes the LIVE gates fresh. An absent
intent (a dependency-park row, a manually-created row, a pre-R5 crash) or any drift is a typed
zero-close — the redrive never runs. Reading it is a home-scoped read, never a provider
(Redmine) read (review j#86776 R5-F3: the redrive's provider read budget is 0).

Design mirrors the sibling home-scoped fences (:mod:`supervisor_lease`,
:mod:`workflow_runtime_store`): a ``*_FILENAME`` constant, a ``*_path(home=None)`` helper
resolving through :func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a ``PRAGMA user_version``
schema guard that fails closed on an unrecognized version, frozen dataclasses, and ISO-second
UTC timestamps. It is a **separate** home-scoped SQLite (not folded into ``state.sqlite``) — a
net-new runtime concern with its own lifecycle, exactly the "separate home-scoped file"
precedent the lease / outbox fences set.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-scoped SQLite file holding redrive intents. A separate DB from the supervisor lease
#: and the workflow runtime cache: the intent is a distinct crash-recovery concern.
HIBERNATE_REDRIVE_INTENT_FILENAME = "hibernate-redrive-intent.sqlite"

#: Schema version stamped into ``PRAGMA user_version``. An unrecognized version fails closed
#: (a downgraded build never silently drops or rewrites a newer intent table).
HIBERNATE_REDRIVE_INTENT_SCHEMA_VERSION = 1

_RECOGNIZED_SCHEMA_VERSIONS = frozenset({1})

_INTENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hibernate_redrive_intent (
    workspace_id     TEXT NOT NULL,
    lane_id          TEXT NOT NULL,
    lane_generation  INTEGER NOT NULL,
    issue_id         TEXT NOT NULL,
    decision_journal TEXT NOT NULL,
    basis            TEXT NOT NULL,
    action_id        TEXT NOT NULL,
    assertion_flags  TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    PRIMARY KEY (workspace_id, lane_id, lane_generation)
)
"""


class HibernateRedriveIntentError(RuntimeError):
    """The redrive-intent DB could not be opened / read at a recognized schema (fail-closed)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hibernate_redrive_intent_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``hibernate-redrive-intent.sqlite`` path under the mozyo-bridge home.

    ``home`` overrides the root (tests pass a temp dir); otherwise the shared
    :func:`mozyo_bridge.shared.paths.mozyo_bridge_home` resolves ``MOZYO_BRIDGE_HOME`` /
    ``~/.mozyo_bridge``.
    """
    return (home or mozyo_bridge_home()) / HIBERNATE_REDRIVE_INTENT_FILENAME


@dataclass(frozen=True)
class RedriveIntent:
    """One fresh actuation's durable intent, keyed by (workspace, lane, generation).

    ``assertion_flags`` is the FULL :class:`HibernateAssertions` kwargs mapping the fresh
    actuation derived (basis flags from the candidate's proven basis, the durable obligation
    flags as observed). A redrive transcribes the DURABLE subset from here and re-observes the
    LIVE gates fresh — it never fabricates a basis flag the row did not record.
    """

    workspace_id: str
    lane_id: str
    lane_generation: int
    issue_id: str
    decision_journal: str
    basis: str
    action_id: str
    assertion_flags: Mapping[str, bool] = field(default_factory=dict)
    recorded_at: str = ""

    def matches_row(
        self, *, issue_id: str, decision_journal: str, action_id: str
    ) -> bool:
        """Whether this intent is the same action authority the hibernated row records.

        The generation is the store key (an intent for another generation is simply not read),
        so the row match is the remaining triple: the issue the CAS bound, the decision journal
        it stored, and the action id the release resumes. Any drift means the intent describes a
        DIFFERENT cycle than the row and must not authorise this redrive (fail-closed).
        """
        return (
            str(self.issue_id).strip() == str(issue_id).strip()
            and str(self.decision_journal).strip() == str(decision_journal).strip()
            and str(self.action_id).strip() == str(action_id).strip()
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "lane_generation": self.lane_generation,
            "issue_id": self.issue_id,
            "decision_journal": self.decision_journal,
            "basis": self.basis,
            "action_id": self.action_id,
            "assertion_flags": dict(self.assertion_flags),
            "recorded_at": self.recorded_at,
        }


class HibernateRedriveIntentStore:
    """CAS access to the home-scoped redrive-intent DB.

    Construction never touches the filesystem; the DB is created lazily on the first write.
    Reads on an absent DB return ``None`` (the normal pre-write state); an existing DB with an
    unrecognized container version, or a corrupt row, fails closed rather than being trusted.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else hibernate_redrive_intent_path(home)

    def _connect_rw(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            conn.execute(_INTENT_TABLE_SQL)
            conn.execute(
                f"PRAGMA user_version = {HIBERNATE_REDRIVE_INTENT_SCHEMA_VERSION}"
            )
        elif version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise HibernateRedriveIntentError(
                f"redrive intent store {self.path} has unsupported schema version {version}; "
                f"this build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}. The DB is left "
                "untouched (downgrade-safe); use a newer build or move it aside."
            )
        else:
            conn.execute(_INTENT_TABLE_SQL)  # self-heal a table lost under a valid version
        return conn

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            conn.close()
            raise HibernateRedriveIntentError(
                f"redrive intent store {self.path} is unreadable: {exc}"
            ) from exc
        if version not in _RECOGNIZED_SCHEMA_VERSIONS:
            conn.close()
            raise HibernateRedriveIntentError(
                f"redrive intent store {self.path} has unsupported schema version {version}; "
                f"this build understands {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}."
            )
        return conn

    def record(self, intent: RedriveIntent, *, now: Optional[str] = None) -> None:
        """Persist (upsert) the fresh actuation's intent, keyed by (workspace, lane, generation).

        Idempotent: re-recording the same lane/generation overwrites, so a re-driven fresh pass
        never accumulates stale rows. ``assertion_flags`` is stored as a JSON object of booleans.
        """
        ws = str(intent.workspace_id or "").strip()
        lane = str(intent.lane_id or "").strip()
        if not ws or not lane:
            raise HibernateRedriveIntentError(
                f"redrive intent requires a non-empty workspace_id and lane_id; "
                f"got workspace_id={intent.workspace_id!r} lane_id={intent.lane_id!r}"
            )
        stamp = now or _utc_now_iso()
        flags_json = json.dumps(
            {str(k): bool(v) for k, v in dict(intent.assertion_flags).items()},
            sort_keys=True,
        )
        conn = self._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO hibernate_redrive_intent "
                "(workspace_id, lane_id, lane_generation, issue_id, decision_journal, "
                " basis, action_id, assertion_flags, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, lane_id, lane_generation) DO UPDATE SET "
                "issue_id=excluded.issue_id, decision_journal=excluded.decision_journal, "
                "basis=excluded.basis, action_id=excluded.action_id, "
                "assertion_flags=excluded.assertion_flags, recorded_at=excluded.recorded_at",
                (
                    ws,
                    lane,
                    int(intent.lane_generation),
                    str(intent.issue_id or "").strip(),
                    str(intent.decision_journal or "").strip(),
                    str(intent.basis or "").strip(),
                    str(intent.action_id or "").strip(),
                    flags_json,
                    stamp,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise HibernateRedriveIntentError(
                f"redrive intent record failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def get(
        self, workspace_id: str, lane_id: str, lane_generation: int
    ) -> Optional[RedriveIntent]:
        """The persisted intent for the exact (workspace, lane, generation), or ``None``.

        ``None`` when the DB is absent or no row exists (the normal "no intent" case — a
        dependency-park / manual / pre-R5 row). A corrupt ``assertion_flags`` blob or an
        unreadable DB raises :class:`HibernateRedriveIntentError` (fail-closed — the caller
        treats it as no usable intent, never as a satisfied basis).
        """
        ws = str(workspace_id or "").strip()
        lane = str(lane_id or "").strip()
        conn = self._connect_ro()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT workspace_id, lane_id, lane_generation, issue_id, decision_journal, "
                "basis, action_id, assertion_flags, recorded_at "
                "FROM hibernate_redrive_intent "
                "WHERE workspace_id=? AND lane_id=? AND lane_generation=?",
                (ws, lane, int(lane_generation)),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        try:
            flags = json.loads(row[7])
        except (json.JSONDecodeError, TypeError) as exc:
            raise HibernateRedriveIntentError(
                f"redrive intent for {ws}/{lane}@{lane_generation} has a corrupt "
                f"assertion_flags blob; fail closed ({exc})"
            ) from exc
        if not isinstance(flags, dict):
            raise HibernateRedriveIntentError(
                f"redrive intent for {ws}/{lane}@{lane_generation} has a non-object "
                "assertion_flags blob; fail closed"
            )
        return RedriveIntent(
            workspace_id=row[0],
            lane_id=row[1],
            lane_generation=int(row[2]),
            issue_id=row[3],
            decision_journal=row[4],
            basis=row[5],
            action_id=row[6],
            assertion_flags={str(k): bool(v) for k, v in flags.items()},
            recorded_at=row[8],
        )


__all__ = (
    "HIBERNATE_REDRIVE_INTENT_FILENAME",
    "HIBERNATE_REDRIVE_INTENT_SCHEMA_VERSION",
    "HibernateRedriveIntentError",
    "RedriveIntent",
    "HibernateRedriveIntentStore",
    "hibernate_redrive_intent_path",
)
