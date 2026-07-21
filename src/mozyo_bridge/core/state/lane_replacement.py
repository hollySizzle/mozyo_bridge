"""Receiver-replacement store: the third lifecycle axis (Redmine #13763).

The replacement axis owns its own guarded writes but deliberately shares the lane
row — and therefore the row's single ``revision`` — with disposition and process
release (:mod:`mozyo_bridge.core.state.lane_lifecycle`). That shared revision *is*
the race fence the quarantine contract asks for (j#78011: "supersession/hibernate
とのraceをCASでfail-closed"): a disposition transition bumps the revision and so
invalidates an in-flight replacement's ``expected_revision``, and a replacement bumps
it and so invalidates a stale hibernate / supersede.

Both CAS writes open their own ``BEGIN IMMEDIATE`` on the lifecycle store's
connection, re-read the locked row, and refuse rather than repair: the caller never
gains disposition or release mutation authority through this surface, only the narrow
request / outcome / get triple for one owner-approved receiver generation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_rows import (
    _locked_row,
    _rollback,
    _utc_now,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (
    CAS_ACTION_MISMATCH,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_OWNER_CONFLICT,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    REPLACEMENT_PENDING,
    REPLACEMENT_REPLACED,
    REPLACEMENT_REQUESTED,
    CasOutcome,
    DecisionPointer,
    LaneLifecycleKey,
    ReleasePin,
    encode_release_pins,
    norm,
    replacement_open_allowed,
    replacement_transition_allowed,
    validate_replacement_pins,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    TABLE as _TABLE,
    LaneLifecycleError,
)
from mozyo_bridge.core.state.lane_replacement_model import LaneReplacementRecord


class LaneReplacementStore:
    """Request / outcome / get for one owner-approved receiver replacement."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    @property
    def last_write_preparation(self):
        """The last mutation's explicit-write-gate result (Redmine #13844 R3-F2).

        Delegates to the wrapped lifecycle store so a replacement command can surface the
        pre-migration preflight + post migration outcome (peer-reader risk) in its typed outcome.
        """
        return self._lifecycle.last_write_preparation

    def get_replacement(
        self, key: LaneLifecycleKey
    ) -> Optional[LaneReplacementRecord]:
        row = self._lifecycle.get(key)
        return LaneReplacementRecord.from_lifecycle(row) if row is not None else None

    def request_replacement(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        action_id: str,
        pins: Iterable[ReleasePin],
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Open one owner-approved receiver replacement generation.

        The lane must remain the active owner and exactly one old receiver slot is
        pinned.  A previous terminal generation may be replaced by a new action,
        while ``requested`` / ``pending`` can only be resumed by that same action.
        """
        action = norm(action_id)
        if not action:
            raise ValueError("a replacement generation requires a non-empty action id")
        pinned = validate_replacement_pins(tuple(pins))
        stamp = now or _utc_now()
        conn = self._lifecycle._connect_write(key)  # Redmine #13844 R2: shared write gate
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if current.lane_disposition != DISPOSITION_ACTIVE:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if not decision.authorizes_binding(current.issue_id):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_OWNER_CONFLICT,
                    revision=current.revision,
                )
            if not replacement_open_allowed(current.replacement_state):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=(
                        CAS_ACTION_MISMATCH
                        if current.replacement_action_id
                        and current.replacement_action_id != action
                        else CAS_FORBIDDEN_TRANSITION
                    ),
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET replacement_state = ?, "
                "replacement_action_id = ?, replacement_pins = ?, revision = ?, "
                "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                "updated_at = ? WHERE repo_workspace_id = ? AND lane_id = ? "
                "AND revision = ?",
                (
                    REPLACEMENT_REQUESTED,
                    action,
                    encode_release_pins(pinned),
                    revision,
                    decision.source,
                    decision.issue_id,
                    decision.journal_id,
                    stamp,
                    key.repo_workspace_id,
                    key.lane_id,
                    current.revision,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane replacement request failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def record_replacement_outcome(
        self,
        key: LaneLifecycleKey,
        *,
        action_id: str,
        expected_revision: int,
        target: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Record ``pending`` or ``replaced`` for the exact action generation."""
        if target not in (REPLACEMENT_PENDING, REPLACEMENT_REPLACED):
            raise ValueError(
                f"a replacement outcome is pending or replaced, not {target!r}"
            )
        action = norm(action_id)
        stamp = now or _utc_now()
        conn = self._lifecycle._connect_write(key)  # Redmine #13844 R2: shared write gate
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.replacement_action_id != action:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ACTION_MISMATCH,
                    revision=current.revision,
                )
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if current.lane_disposition != DISPOSITION_ACTIVE:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if not replacement_transition_allowed(current.replacement_state, target):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET replacement_state = ?, revision = ?, "
                "updated_at = ? WHERE repo_workspace_id = ? AND lane_id = ? "
                "AND revision = ? AND replacement_action_id = ?",
                (
                    target,
                    revision,
                    stamp,
                    key.repo_workspace_id,
                    key.lane_id,
                    current.revision,
                    action,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane replacement outcome failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneReplacementStore",)
