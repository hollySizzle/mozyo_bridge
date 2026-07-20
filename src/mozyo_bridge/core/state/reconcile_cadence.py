"""Provider-reconciliation cadence watermark (Redmine #14150).

The durable per-workspace watermark the bounded provider-reconciliation leg reads to decide whether a
workspace is DUE for a ticket-provider re-read, or should be DOWNGRADED to a local drain this pass. It
is *derived state* (a latency / load optimisation), never a work-record authority: losing it only
makes the next pass reconcile early, so it is a rebuildable cache — a missing / unreadable row reads as
"never reconciled -> due", which fails toward reconciling (the provider fallback is never suppressed by
a lost watermark).

A tiny native ``reconcile-cadence.sqlite`` component (home-scoped), separate from the workflow-runtime
DB so this optimisation never perturbs the callback-outbox schema. One row per workspace: the last
completed-reconcile timestamp + the count of consecutive empty passes (which feeds the empty-pass
backoff). It stores **no** secret / pane id / path — only a workspace id (already public) and counters.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

RECONCILE_CADENCE_FILENAME = "reconcile-cadence.sqlite"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reconcile_watermark (
    workspace_id       TEXT PRIMARY KEY,
    last_reconciled_at TEXT NOT NULL DEFAULT '',
    empty_passes       INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT NOT NULL DEFAULT ''
)
"""


def reconcile_cadence_path(home: Optional[Path] = None) -> Path:
    """Resolve the ``reconcile-cadence.sqlite`` path under the mozyo-bridge home."""
    return (home or mozyo_bridge_home()) / RECONCILE_CADENCE_FILENAME


@dataclass(frozen=True)
class ReconcileWatermark:
    """One workspace's reconcile watermark (blank ``last_reconciled_at`` == never reconciled)."""

    workspace_id: str
    last_reconciled_at: str = ""
    empty_passes: int = 0


class ReconcileCadenceStore:
    """Durable per-workspace reconcile watermark (rebuildable cache; fail-toward-reconciling)."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else reconcile_cadence_path(home)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        conn.execute(_TABLE_SQL)
        return conn

    def read(self, workspace_id: str) -> ReconcileWatermark:
        """Return the workspace watermark; a missing / unreadable row is 'never reconciled'."""
        wsid = str(workspace_id or "").strip()
        if not wsid or not self.path.exists():
            return ReconcileWatermark(workspace_id=wsid)
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return ReconcileWatermark(workspace_id=wsid)
        try:
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reconcile_watermark'"
            ).fetchone()
            if has is None:
                return ReconcileWatermark(workspace_id=wsid)
            row = conn.execute(
                "SELECT last_reconciled_at, empty_passes FROM reconcile_watermark "
                "WHERE workspace_id=?",
                (wsid,),
            ).fetchone()
        except sqlite3.DatabaseError:
            return ReconcileWatermark(workspace_id=wsid)
        finally:
            conn.close()
        if row is None:
            return ReconcileWatermark(workspace_id=wsid)
        return ReconcileWatermark(
            workspace_id=wsid,
            last_reconciled_at=str(row[0] or ""),
            empty_passes=int(row[1] or 0),
        )

    def mark(self, workspace_id: str, *, now: str, produced: bool) -> None:
        """Advance the watermark after a completed provider reconcile.

        ``produced`` (this pass supplied an event / delivered a callback) resets the consecutive-empty
        counter to 0; an empty pass increments it (feeding the exponential backoff). A write failure is
        swallowed — the watermark is a cache, so a lost write just reconciles early next pass.
        """
        wsid = str(workspace_id or "").strip()
        if not wsid:
            return
        stamp = str(now or "")
        try:
            conn = self._connect()
        except sqlite3.DatabaseError:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            prev = conn.execute(
                "SELECT empty_passes FROM reconcile_watermark WHERE workspace_id=?", (wsid,)
            ).fetchone()
            empties = 0 if produced else (int(prev[0]) + 1 if prev is not None else 1)
            conn.execute(
                "INSERT INTO reconcile_watermark (workspace_id, last_reconciled_at, empty_passes, "
                "updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(workspace_id) DO UPDATE SET "
                "last_reconciled_at=excluded.last_reconciled_at, empty_passes=excluded.empty_passes, "
                "updated_at=excluded.updated_at",
                (wsid, stamp, empties, stamp),
            )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
        finally:
            conn.close()


__all__ = (
    "RECONCILE_CADENCE_FILENAME",
    "reconcile_cadence_path",
    "ReconcileWatermark",
    "ReconcileCadenceStore",
)
