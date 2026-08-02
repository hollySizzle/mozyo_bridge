"""Append-only durable action frames and continuation transitions (#14825).

This is the asynchronous-action half of ``auto_integration_ledger`` split into a cohesive leaf:
the step ledger still owns the SQLite container/writer capability, while these functions own the
immutable resume frame and its registered -> awaiting-CI -> terminal event stream.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    IntegrationActionRecord,
    is_full_sha,
)


ACTION_REGISTERED = "registered"
ACTION_AWAITING_CI = "awaiting_ci"
ACTION_INTEGRATED = "integrated"
ACTION_CI_FAILED = "ci_failed"
ACTION_STATES = frozenset(
    {ACTION_REGISTERED, ACTION_AWAITING_CI, ACTION_INTEGRATED, ACTION_CI_FAILED}
)
_ACTION_TERMINAL_STATES = frozenset({ACTION_INTEGRATED, ACTION_CI_FAILED})

ACTION_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS auto_integration_action (
        action_key TEXT PRIMARY KEY,
        issue TEXT NOT NULL,
        workspace TEXT NOT NULL,
        lane TEXT NOT NULL,
        lane_generation INTEGER NOT NULL,
        branch TEXT NOT NULL,
        worktree TEXT NOT NULL,
        repo_root TEXT NOT NULL,
        source_head TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        expected_target_head TEXT NOT NULL,
        review_generation TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auto_integration_action_event (
        id INTEGER PRIMARY KEY,
        action_key TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        state TEXT NOT NULL,
        landed_head TEXT NOT NULL DEFAULT '',
        ci_workflow TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(action_key) REFERENCES auto_integration_action(action_key)
    )
    """,
)

ACTION_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_auto_integration_action_scope "
    "ON auto_integration_action(workspace, issue)",
    "CREATE INDEX IF NOT EXISTS idx_auto_integration_action_event_latest "
    "ON auto_integration_action_event(action_key, id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_integration_action_awaiting "
    "ON auto_integration_action_event(action_key) WHERE state = 'awaiting_ci'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_integration_action_terminal "
    "ON auto_integration_action_event(action_key) "
    "WHERE state IN ('integrated', 'ci_failed')",
)


class ActionRegistryError(RuntimeError):
    """An action frame/transition was unreadable, invalid, or lost its compare-and-set."""


@dataclass(frozen=True)
class DurableIntegrationAction:
    """An exact integration action plus the lane/runtime frame needed to resume it."""

    action_key: str
    issue: str
    workspace: str
    lane: str
    lane_generation: int
    branch: str
    worktree: str
    repo_root: str
    source_head: str
    target_ref: str
    expected_target_head: str
    review_generation: str
    state: str = ACTION_REGISTERED
    landed_head: str = ""
    ci_workflow: str = ""

    @property
    def record(self) -> IntegrationActionRecord:
        return IntegrationActionRecord(
            issue=self.issue,
            lane_generation=self.lane_generation,
            source_head=self.source_head,
            target_ref=self.target_ref,
            expected_target_head=self.expected_target_head,
            review_generation=self.review_generation,
        )

    def validation_errors(self) -> Tuple[str, ...]:
        problems = list(self.record.validation_errors())
        if self.record.action_key != self.action_key:
            problems.append("action_key does not match the six-field integration record")
        for name in ("workspace", "lane", "branch", "worktree", "repo_root"):
            if not str(getattr(self, name) or "").strip():
                problems.append(f"{name} is empty")
        if self.state not in ACTION_STATES:
            problems.append(f"unrecognized action state {self.state!r}")
        return tuple(problems)


def register_action(store, action: DurableIntegrationAction) -> None:
    """Persist an immutable frame; exact concurrent/sequential replay is a no-op."""
    store._require_writer()
    problems = action.validation_errors()
    if problems:
        raise ActionRegistryError("invalid durable integration action: " + "; ".join(problems))
    identity = _identity(action)
    conn = store._connect()
    try:
        with conn:
            existing = conn.execute(
                "SELECT action_key, issue, workspace, lane, lane_generation, branch, "
                "worktree, repo_root, source_head, target_ref, expected_target_head, "
                "review_generation FROM auto_integration_action WHERE action_key = ?",
                (action.action_key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != identity:
                    raise ActionRegistryError(
                        "the action key is already registered with a different lane/runtime "
                        "frame; refusing to redirect its continuation"
                    )
                return
            now = _utc_now()
            conn.execute(
                "INSERT INTO auto_integration_action "
                "(action_key, issue, workspace, lane, lane_generation, branch, worktree, "
                "repo_root, source_head, target_ref, expected_target_head, "
                "review_generation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                identity + (now,),
            )
            conn.execute(
                "INSERT INTO auto_integration_action_event "
                "(action_key, recorded_at, state) VALUES (?, ?, ?)",
                (action.action_key, now, ACTION_REGISTERED),
            )
    except ActionRegistryError:
        raise
    except sqlite3.IntegrityError:
        current = read_action(store, action.action_key)
        if current is not None and _identity(current) == identity:
            return
        raise ActionRegistryError(
            "a different durable action frame already won this action key"
        ) from None
    except sqlite3.Error as exc:
        raise ActionRegistryError(
            "the durable auto-integration action could not be registered "
            f"({exc.__class__.__name__})"
        ) from exc
    finally:
        conn.close()


def mark_action_awaiting_ci(
    store, *, action_key: str, landed_head: str, ci_workflow: str
) -> None:
    store._require_writer()
    if not is_full_sha(landed_head) or not str(ci_workflow or "").strip():
        raise ActionRegistryError(
            "an awaiting-CI transition requires the exact landed SHA and required workflow"
        )
    _append_transition(
        store,
        action_key=action_key,
        state=ACTION_AWAITING_CI,
        landed_head=landed_head,
        ci_workflow=ci_workflow,
        allowed_from=(ACTION_REGISTERED,),
    )


def mark_action_terminal(
    store, *, action_key: str, state: str, landed_head: str, detail: str = ""
) -> None:
    if state not in _ACTION_TERMINAL_STATES:
        raise ActionRegistryError(
            f"terminal action state must be one of {sorted(_ACTION_TERMINAL_STATES)}, "
            f"not {state!r}"
        )
    store._require_writer()
    if not is_full_sha(landed_head):
        raise ActionRegistryError(
            "a terminal continuation transition requires the exact landed SHA"
        )
    _append_transition(
        store,
        action_key=action_key,
        state=state,
        landed_head=landed_head,
        ci_workflow="",
        detail=detail,
        allowed_from=(ACTION_AWAITING_CI,),
    )


def _append_transition(
    store,
    *,
    action_key: str,
    state: str,
    landed_head: str,
    ci_workflow: str,
    allowed_from: Tuple[str, ...],
    detail: str = "",
) -> None:
    conn = store._connect()
    try:
        with conn:
            current = conn.execute(
                "SELECT state, landed_head, ci_workflow FROM "
                "auto_integration_action_event WHERE action_key = ? ORDER BY id DESC LIMIT 1",
                (str(action_key),),
            ).fetchone()
            if current is None:
                raise ActionRegistryError(
                    "the action is not registered; a continuation state cannot be invented"
                )
            if str(current[0]) == state:
                if str(current[1]) == str(landed_head) and (
                    not ci_workflow or str(current[2]) == str(ci_workflow)
                ):
                    return
                raise ActionRegistryError(
                    "the action already carries this state with different exact evidence"
                )
            if str(current[0]) not in allowed_from:
                raise ActionRegistryError(
                    f"action transition {current[0]!r} -> {state!r} is not permitted"
                )
            conn.execute(
                "INSERT INTO auto_integration_action_event "
                "(action_key, recorded_at, state, landed_head, ci_workflow, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(action_key),
                    _utc_now(),
                    state,
                    str(landed_head),
                    str(ci_workflow or current[2] or ""),
                    str(detail),
                ),
            )
    except ActionRegistryError:
        raise
    except sqlite3.IntegrityError:
        current = read_action(store, action_key)
        if (
            current is not None
            and current.state == state
            and current.landed_head == landed_head
            and (not ci_workflow or current.ci_workflow == ci_workflow)
        ):
            return
        raise ActionRegistryError(
            "a different durable continuation transition already won this action"
        ) from None
    except sqlite3.Error as exc:
        raise ActionRegistryError(
            f"the durable action transition could not be recorded ({exc.__class__.__name__})"
        ) from exc
    finally:
        conn.close()


def read_action(store, action_key: str) -> Optional[DurableIntegrationAction]:
    if not store.path.exists():
        return None
    conn = store._connect(read_only=True)
    try:
        row = conn.execute(
            "SELECT a.action_key, a.issue, a.workspace, a.lane, a.lane_generation, "
            "a.branch, a.worktree, a.repo_root, a.source_head, a.target_ref, "
            "a.expected_target_head, a.review_generation, e.state, e.landed_head, "
            "e.ci_workflow FROM auto_integration_action a JOIN "
            "auto_integration_action_event e ON e.action_key = a.action_key "
            "WHERE a.action_key = ? AND e.id = (SELECT MAX(id) FROM "
            "auto_integration_action_event WHERE action_key = a.action_key)",
            (str(action_key),),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ActionRegistryError(
            f"the durable action could not be read ({exc.__class__.__name__})"
        ) from exc
    finally:
        conn.close()
    return _row_to_action(row) if row is not None else None


def resumable_actions(
    store, *, workspace: str = "", issue: str = ""
) -> Tuple[DurableIntegrationAction, ...]:
    if not store.path.exists():
        return ()
    clauses = ["e.state IN (?, ?)"]
    params: list[object] = [ACTION_REGISTERED, ACTION_AWAITING_CI]
    if str(workspace).strip():
        clauses.append("a.workspace = ?")
        params.append(str(workspace))
    if str(issue).strip():
        clauses.append("a.issue = ?")
        params.append(str(issue))
    conn = store._connect(read_only=True)
    try:
        rows = conn.execute(
            "SELECT a.action_key, a.issue, a.workspace, a.lane, a.lane_generation, "
            "a.branch, a.worktree, a.repo_root, a.source_head, a.target_ref, "
            "a.expected_target_head, a.review_generation, e.state, e.landed_head, "
            "e.ci_workflow FROM auto_integration_action a JOIN "
            "auto_integration_action_event e ON e.action_key = a.action_key "
            "WHERE e.id = (SELECT MAX(id) FROM auto_integration_action_event "
            "WHERE action_key = a.action_key) AND "
            + " AND ".join(clauses)
            + " ORDER BY a.created_at, a.action_key",
            tuple(params),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ActionRegistryError(
            f"durable continuation actions could not be read ({exc.__class__.__name__})"
        ) from exc
    finally:
        conn.close()
    return tuple(_row_to_action(row) for row in rows)


def action_event_count(store, *, action_key: str) -> int:
    if not store.path.exists():
        return 0
    conn = store._connect(read_only=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM auto_integration_action_event WHERE action_key = ?",
            (str(action_key),),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error as exc:
        raise ActionRegistryError(
            f"durable continuation events could not be read ({exc.__class__.__name__})"
        ) from exc
    finally:
        conn.close()


def _identity(action: DurableIntegrationAction) -> tuple[object, ...]:
    return (
        action.action_key,
        action.issue,
        action.workspace,
        action.lane,
        int(action.lane_generation),
        action.branch,
        action.worktree,
        action.repo_root,
        action.source_head,
        action.target_ref,
        action.expected_target_head,
        action.review_generation,
    )


def _row_to_action(row: Sequence[object]) -> DurableIntegrationAction:
    return DurableIntegrationAction(
        action_key=str(row[0]),
        issue=str(row[1]),
        workspace=str(row[2]),
        lane=str(row[3]),
        lane_generation=int(row[4]),
        branch=str(row[5]),
        worktree=str(row[6]),
        repo_root=str(row[7]),
        source_head=str(row[8]),
        target_ref=str(row[9]),
        expected_target_head=str(row[10]),
        review_generation=str(row[11]),
        state=str(row[12]),
        landed_head=str(row[13]),
        ci_workflow=str(row[14]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


__all__ = (
    "ACTION_AWAITING_CI",
    "ACTION_CI_FAILED",
    "ACTION_INTEGRATED",
    "ACTION_REGISTERED",
    "ACTION_STATES",
    "ActionRegistryError",
    "DurableIntegrationAction",
)
