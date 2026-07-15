"""Lane lifecycle — low-level row helpers shared by the CAS stores (Redmine #13810).

The row-level DB plumbing that both the core CAS store
(:mod:`mozyo_bridge.core.state.lane_lifecycle`), the declaration / incarnation service
(:mod:`mozyo_bridge.core.state.lane_declaration`), and the replacement axis
(:mod:`mozyo_bridge.core.state.lane_replacement`) drive against the one shared
``lane_lifecycle_records`` row:

- :func:`_record` — decode a ``SELECT`` row (all columns) into a typed record;
- :func:`_insert_active_row` — the single fresh-``active``-row ``INSERT`` (one 21-column
  value tuple, so no call site can drift out of column order with the schema);
- :func:`_locked_row` — read the row inside an already-open ``BEGIN IMMEDIATE``;
- :func:`_active_owner` / :func:`_active_project_owner` — the in-lock owner pre-checks for
  the issue and project-gateway owner indexes;
- :func:`_rollback` / :func:`_utc_now` — the tiny shared utilities.

Extracting this leaf layer (Redmine #13810) keeps each store module a cohesive,
under-threshold unit while the component stays ONE table / ONE CAS row — the design's
"one component" boundary is about the data model, not the Python module (the sibling
:mod:`...lane_replacement` already owns its axis in its own module on the same row).
This module knows nothing about the stores; the dependency is one-directional
(model / schema -> rows -> stores), so there is no import cycle.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    BINDING_KIND_PROJECT_GATEWAY,
    DISPOSITION_ACTIVE,
    RELEASE_NOT_REQUESTED,
    REPLACEMENT_NOT_REQUESTED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleRecord,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    COLUMNS as _COLUMNS,
    TABLE as _TABLE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(row: Sequence[object]) -> LaneLifecycleRecord:
    return LaneLifecycleRecord(
        repo_workspace_id=str(row[0]),
        lane_id=str(row[1]),
        issue_id=str(row[2] or ""),
        lane_disposition=str(row[3]),
        process_release=str(row[4]),
        revision=int(row[5]),
        release_action_id=str(row[6] or ""),
        release_pins=str(row[7] or ""),
        replacement_state=str(row[8] or REPLACEMENT_NOT_REQUESTED),
        replacement_action_id=str(row[9] or ""),
        replacement_pins=str(row[10] or ""),
        decision_source=str(row[11] or ""),
        decision_issue_id=str(row[12] or ""),
        decision_journal=str(row[13] or ""),
        created_at=str(row[14]),
        updated_at=str(row[15]),
        worktree_identity=str(row[16] or ""),
        binding_kind=str(row[17] or BINDING_KIND_ISSUE),
        project_scope=str(row[18] or ""),
        lane_generation=int(row[19]),
        declared_slots=str(row[20] or ""),
    )


#: Placeholder tuple sized to :data:`_COLUMNS` so the fresh-insert helper can never
#: drift out of column count with the schema.
_INSERT_SQL = (
    f"INSERT INTO {_TABLE} ({_COLUMNS}) "
    f"VALUES ({', '.join(['?'] * len(_COLUMNS.split(',')))})"
)


def _insert_active_row(
    conn: sqlite3.Connection,
    *,
    key: LaneLifecycleKey,
    issue: str,
    decision: DecisionPointer,
    revision: int,
    stamp: str,
    worktree: str = "",
    binding_kind: str = BINDING_KIND_ISSUE,
    project_scope: str = "",
    lane_generation: int = 1,
    declared_slots: str = "",
) -> None:
    """INSERT a fresh ``active`` / ``not_requested`` lane row (one column vocabulary).

    The single place a brand-new lane row is written — used by ``declare_active`` and the
    recovery-lane create inside ``supersede_and_activate`` (core store), and by
    ``declare_lane`` (declaration service) — so the 21-column value tuple exists once and
    cannot drift per call site.
    """
    conn.execute(
        _INSERT_SQL,
        (
            key.repo_workspace_id,
            key.lane_id,
            issue,
            DISPOSITION_ACTIVE,
            RELEASE_NOT_REQUESTED,
            revision,
            "",
            "",
            REPLACEMENT_NOT_REQUESTED,
            "",
            "",
            decision.source,
            decision.issue_id,
            decision.journal_id,
            stamp,
            stamp,
            worktree,
            binding_kind,
            project_scope,
            lane_generation,
            declared_slots,
        ),
    )


def _locked_row(
    conn: sqlite3.Connection, key: LaneLifecycleKey
) -> Optional[LaneLifecycleRecord]:
    """Read the row inside the already-open ``BEGIN IMMEDIATE`` write lock."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM {_TABLE} WHERE repo_workspace_id = ? AND lane_id = ?",
        key.as_row(),
    ).fetchone()
    return _record(row) if row is not None else None


def _active_owner(
    conn: sqlite3.Connection, repo_workspace_id: str, issue_id: str
) -> Optional[str]:
    """The lane actively owning ``issue_id``, read inside the write lock.

    Callers pre-check with this rather than classifying a raised
    ``IntegrityError``: SQLite reports a unique violation by *column list*, not by
    index name, so the two constraints on this table (the lane primary key and the
    active-owner index) are not reliably distinguishable from the message text.
    Holding ``BEGIN IMMEDIATE`` makes the pre-check authoritative — no other writer
    can slip in between it and the write. The index remains the backstop.
    """
    row = conn.execute(
        f"SELECT lane_id FROM {_TABLE} WHERE repo_workspace_id = ? AND issue_id = ? "
        "AND lane_disposition = ?",
        (repo_workspace_id, issue_id, DISPOSITION_ACTIVE),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _active_project_owner(
    conn: sqlite3.Connection, repo_workspace_id: str, project_scope: str
) -> Optional[str]:
    """The project-gateway lane actively owning ``project_scope``, read in the lock.

    The project-scope twin of :func:`_active_owner` (Redmine #13810): the pre-check the
    declaration service uses so a second active owner of one project scope is refused
    before the storage engine's ``idx_lane_lifecycle_active_project_owner`` backstop would
    raise (SQLite reports a unique violation by column list, not by index name).
    """
    row = conn.execute(
        f"SELECT lane_id FROM {_TABLE} WHERE repo_workspace_id = ? AND project_scope = ? "
        "AND binding_kind = ? AND lane_disposition = ?",
        (repo_workspace_id, project_scope, BINDING_KIND_PROJECT_GATEWAY, DISPOSITION_ACTIVE),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        pass


__all__ = (
    "_utc_now",
    "_record",
    "_insert_active_row",
    "_locked_row",
    "_active_owner",
    "_active_project_owner",
    "_rollback",
)
