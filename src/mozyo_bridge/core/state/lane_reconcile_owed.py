"""Reconcile owed-close provenance ledger (Redmine #13842 review j#79346 R5).

The collision-proof, reconcile-specific durable provenance the hibernated live-contradiction
reconcile needs to **resume its own owed close** after a crash — WITHOUT the shared
``lane_lifecycle`` schema being able to tell a reconcile-retired row apart from an ordinary
#13809 / #13810-bound retired row (review j#79320 R4), and WITHOUT delegating the recovery to
the ordinary #13754 guarded close, which is name-based, reads no ``declared_slots`` generation
pins, and gates on no idle / composer / attestation — so it would close a recycled newer
generation (review j#79346 R5).

The retire-first reconcile flow has exactly one crash window: the ``hibernated -> retired`` CAS
committed but the external pane close did not finish. To resume that window in the SAME
reconcile authority — at the EXACT generation it verified, under the SAME full conjunct
(idle / turn-ended / no pending composer / attestation / uniqueness / no foreign, plus a
whole-unit post-close measure) — the reconcile records an owed entry **before** the retire CAS,
so an entry is always present whenever a row is reconcile-retired. The entry pins the exact
``(lane_generation, retired_revision)`` the reconcile CAS produces, so:

- the retired-branch resume fires ONLY when an entry exists AND its
  ``(lane_generation, retired_revision)`` matches the live retired row — an ordinary bound
  retired row has no entry and is never resumed (R4 preserved, collision-proof);
- a reopened + re-retired lane (a bumped ``lane_generation``) never matches a stale entry;
- a failed retire CAS (a rehydrate race) leaves at most a stale, inert entry (the row is not
  retired at the pinned revision), re-written idempotently on the next attempt.

This lives in its OWN home-scoped durable component (like ``herdr_identity_attestation``), NOT
as a ``lane_lifecycle`` schema column: it is deliberately kept off the shared authority schema.
Both files live under ``$MOZYO_BRIDGE_HOME``, so a normal crash preserves them together and the
resume is robust; a partial loss of only this file degrades to fail-closed (the resume simply
does not fire — a withheld close, never a wrong one). Conventions mirror the sibling
home-scoped stores: a ``*_FILENAME`` constant, a ``*_path(home=None)`` helper through
:func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a ``PRAGMA user_version`` guard, a frozen
dataclass, ISO-second UTC timestamps, a fail-open read, and a record that RAISES on failure so
the caller fails closed BEFORE it retires (an unrecorded retire would be an unresumable owed
close).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

LANE_RECONCILE_OWED_FILENAME = "lane-reconcile-owed.sqlite"
LANE_RECONCILE_OWED_SCHEMA_VERSION = 1

#: Recovery policy (managed-state-model.md ``### recovery policy vocabulary``): a fail-closed
#: rebuildable projection. Losing the file degrades the reconcile's owed-close resume to
#: fail-closed (the retired-branch withholds instead of resuming); it never authorizes a close.
LANE_RECONCILE_OWED_RECOVERY_POLICY = "rebuildable_cache"


def lane_reconcile_owed_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / LANE_RECONCILE_OWED_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LaneReconcileOwedError(RuntimeError):
    """The owed-close ledger is unusable (schema mismatch / unwritable); fail closed."""


@dataclass(frozen=True)
class ReconcileOwedRecord:
    """One lane's owed reconcile close — the provenance a retired-branch resume keys on.

    ``(lane_generation, retired_revision)`` pin the EXACT generation + post-retire revision the
    reconcile's ``hibernated -> retired`` CAS produced. A resume fires only when a live retired
    row matches BOTH, so an ordinary bound retired row (no entry) or a reopened generation (a
    bumped ``lane_generation``) is never mistaken for this reconcile's owed close.
    """

    workspace_id: str
    lane_id: str
    lane_generation: int
    retired_revision: int
    observed_at: Optional[str] = None


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lane_reconcile_owed (
    repo_workspace_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    lane_generation INTEGER NOT NULL,
    retired_revision INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (repo_workspace_id, lane_id)
)
"""

_COLUMNS = "repo_workspace_id, lane_id, lane_generation, retired_revision, observed_at"


def _connect_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 2000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(_TABLE_SQL)
        conn.execute(f"PRAGMA user_version = {LANE_RECONCILE_OWED_SCHEMA_VERSION}")
        conn.commit()
    elif version != LANE_RECONCILE_OWED_SCHEMA_VERSION:
        conn.close()
        raise LaneReconcileOwedError(
            f"lane reconcile owed ledger {path} has schema version {version}; this build "
            f"supports {LANE_RECONCILE_OWED_SCHEMA_VERSION}. The file is left untouched "
            "(downgrade-safe)."
        )
    return conn


class LaneReconcileOwedStore:
    """Snapshot-per-lane durable owed-close ledger (one row per lane unit)."""

    def __init__(self, path: Path | None = None, *, home: Path | None = None) -> None:
        self.path = path or lane_reconcile_owed_path(home)

    def record(
        self,
        *,
        workspace_id: str,
        lane_id: str,
        lane_generation: int,
        retired_revision: int,
        now: Optional[str] = None,
    ) -> ReconcileOwedRecord:
        """Record (snapshot-replace) the lane's owed reconcile close.

        Called by the reconcile **before** its ``hibernated -> retired`` CAS, so an entry is
        present whenever a row becomes reconcile-retired. RAISES on any store failure — the
        caller must fail closed and NOT retire, because an unrecorded retire is an unresumable
        owed close (review j#79346 R5).
        """
        observed_at = now or _utc_now()
        try:
            conn = _connect_rw(self.path)
        except sqlite3.DatabaseError as exc:
            raise LaneReconcileOwedError(
                f"lane reconcile owed ledger {self.path} is unwritable "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO lane_reconcile_owed ({_COLUMNS}) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(repo_workspace_id, lane_id) DO UPDATE SET "
                    "lane_generation = excluded.lane_generation, "
                    "retired_revision = excluded.retired_revision, "
                    "observed_at = excluded.observed_at",
                    (workspace_id, lane_id, lane_generation, retired_revision, observed_at),
                )
        except sqlite3.DatabaseError as exc:
            raise LaneReconcileOwedError(
                f"lane reconcile owed record failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return ReconcileOwedRecord(
            workspace_id=workspace_id,
            lane_id=lane_id,
            lane_generation=lane_generation,
            retired_revision=retired_revision,
            observed_at=observed_at,
        )

    def read(self, workspace_id: str, lane_id: str) -> Optional[ReconcileOwedRecord]:
        """The lane's owed-close entry, or ``None``. Read-only, fail-OPEN to ``None``.

        An absent file / unreadable store / schema drift yields ``None``, so the caller's
        retired-branch resume simply does not fire (a withheld close, never a wrong one).
        """
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != LANE_RECONCILE_OWED_SCHEMA_VERSION:
                    return None
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM lane_reconcile_owed "
                    "WHERE repo_workspace_id = ? AND lane_id = ?",
                    (workspace_id, lane_id),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return None
        if row is None:
            return None
        return ReconcileOwedRecord(
            workspace_id=row[0],
            lane_id=row[1],
            lane_generation=int(row[2]),
            retired_revision=int(row[3]),
            observed_at=row[4],
        )

    def clear(
        self, *, workspace_id: str, lane_id: str, retired_revision: int
    ) -> None:
        """Delete the lane's owed entry after its close completed. Best-effort (never raises).

        Guarded on ``retired_revision`` so a stale entry from a newer reconcile generation is
        never deleted by an older completion. A clear failure is harmless: the entry stays and a
        later resume re-measures the whole unit (a positive absence) and clears it then.
        """
        try:
            conn = _connect_rw(self.path)
        except (sqlite3.DatabaseError, LaneReconcileOwedError, OSError):
            return
        try:
            with conn:
                conn.execute(
                    "DELETE FROM lane_reconcile_owed WHERE repo_workspace_id = ? "
                    "AND lane_id = ? AND retired_revision = ?",
                    (workspace_id, lane_id, retired_revision),
                )
        except sqlite3.DatabaseError:
            pass
        finally:
            conn.close()


__all__ = (
    "LANE_RECONCILE_OWED_FILENAME",
    "LANE_RECONCILE_OWED_SCHEMA_VERSION",
    "LANE_RECONCILE_OWED_RECOVERY_POLICY",
    "LaneReconcileOwedError",
    "ReconcileOwedRecord",
    "LaneReconcileOwedStore",
    "lane_reconcile_owed_path",
)
