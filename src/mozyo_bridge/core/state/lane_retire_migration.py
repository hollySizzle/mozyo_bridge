"""Hibernated legacy-lane retire migration CAS (Redmine #13841).

The bounded companion the #13754 guarded close and the #13809 active-row backfill both
leave uncovered. A **hibernated / released legacy** lifecycle row — the coordinator
hibernated the lane, its process release completed durably (``process_release`` reached
``released``), and its live pair is gone — but whose ``worktree_identity`` is EMPTY (a
pre-#13754 row that never recorded one) can be retired by neither path:

- ``retire --execute`` (Redmine #13754) attests the caller's ``--worktree`` against the
  recorded worktree binding before any close; an empty binding fails closed on
  ``worktree_binding_unverified`` forever, and there is no live pair to close anyway.
- ``backfill_active_binding`` (Redmine #13809) fills the missing binding fields of an
  **active** owner row only — a hibernated row is ``CAS_UNEXPECTED_STATE`` there.

Re-launching a fresh pair only to retire it again is exactly the needless actuation the
ticket forbids. This surface instead moves such a row **directly** to the #13689 terminal
``retired`` disposition via one bounded ``BEGIN IMMEDIATE`` CAS — metadata only, no process
launch / close / resume, no worktree / branch removal.

Like :class:`...lane_declaration.LaneDeclarationStore` and
:class:`...lane_replacement.LaneReplacementStore`, this composes a
:class:`LaneLifecycleStore` for the container guard + autocommit connection and drives its
own CAS on the shared ``lane_lifecycle_records`` row through the low-level helpers in
:mod:`mozyo_bridge.core.state.lane_lifecycle_rows`. It writes ONLY the one disposition
edge, and ONLY for the exact legacy signature; every other shape is refused zero-write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    RELEASE_RELEASED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleKey,
    disposition_transition_allowed,
    norm,
    replacement_settled,
)
from mozyo_bridge.core.state.lane_lifecycle_rows import (
    _locked_row,
    _rollback,
    _utc_now,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    TABLE as _TABLE,
    LaneLifecycleError,
)


class LaneRetireMigrationStore:
    """Bounded ``hibernated -> retired`` CAS for a released legacy lane (Redmine #13841)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    def retire_released_hibernated_legacy(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        issue_id: str,
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Retire a hibernated / released legacy owner row directly, or fail closed (#13841).

        Writes the single ``hibernated -> retired`` disposition edge ONLY when every part
        of the exact legacy signature holds — otherwise zero-write, so a live / active
        pair, an unproven release, a non-empty (already #13754-bound) worktree, a different
        issue / binding, or a concurrent write never migrates:

        - the row exists (:data:`CAS_NOT_FOUND`) and its ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION` — a concurrent declare / transition that moved the row
          loses rather than clobbering the newer state);
        - it is ``hibernated`` (an ``active`` lane still holds its work; a ``superseded`` /
          already ``retired`` row is not this migration's target), is an ``issue`` binding,
          owns **this exact** issue, and owns no project scope
          (:data:`CAS_UNEXPECTED_STATE`);
        - its ``worktree_identity`` is **empty** — the defining legacy signature. A
          non-empty binding is a #13754-bound row that retires through the normal guarded
          close, never here (:data:`CAS_UNEXPECTED_STATE`); this surface never overwrites or
          bypasses an established worktree binding;
        - its process release is durably ``released`` — the "already released" proof. A
          ``not_requested`` / ``requested`` / ``partial`` release is unproven or in flight
          (an actuator may still be closing panes), so it fails closed
          (:data:`CAS_FORBIDDEN_TRANSITION`); and no receiver replacement is in flight
          (:func:`replacement_settled`, same reason);
        - the ``decision`` anchor names this issue (a bound row is only decided by a record
          filed on its own issue).

        Deliberately NOT :meth:`LaneLifecycleStore.transition_disposition`: that generic
        edge would accept any ``hibernated -> retired`` regardless of the release / worktree
        / binding shape. This surface is the narrow, legacy-only migration — the release
        proof and the empty-worktree signature are part of the guard, not the caller's
        promise. ``released`` records that a release *command* completed, not that the slots
        are gone (``lane_lifecycle`` boundary): the caller pairs this durable proof with a
        live-inventory zero read (Redmine #13841), so a stale record can never migrate a lane
        whose pair is actually live.

        The disposition is the only field written (plus the decision anchor + revision);
        the process-release, replacement, and binding fields are untouched. A duplicate
        replay is handled by the caller reading the already-``retired`` row (an idempotent
        success), so this CAS stays strictly ``hibernated -> retired``.
        """
        issue = norm(issue_id)
        if not issue:
            raise ValueError(
                "a hibernated legacy retire migration requires the exact issue the row "
                "must already own"
            )
        if not decision.authorizes_binding(issue):
            raise DecisionPointerError(
                f"decision is anchored to issue {decision.issue_id!r} but the migration "
                f"targets a lane bound to {issue!r}"
            )
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
                    applied=False, reason=CAS_STALE_REVISION, revision=current.revision
                )
            if (
                current.lane_disposition != DISPOSITION_HIBERNATED
                or norm(current.binding_kind) != BINDING_KIND_ISSUE
                or current.issue_id != issue
                or current.project_scope
                or current.worktree_identity
            ):
                # Not the exact legacy signature: an active / superseded / retired row, a
                # project-gateway binding, a different issue, or an already-#13754-bound
                # (non-empty worktree) row. Refused zero-write, never coerced.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if current.process_release != RELEASE_RELEASED:
                # The release is unproven (never requested) or still in flight
                # (requested / partial — an actuator may be closing panes right now).
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if not replacement_settled(current.replacement_state):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if not disposition_transition_allowed(
                current.lane_disposition, DISPOSITION_RETIRED
            ):
                # hibernated -> retired is a legal edge; this is the backstop, never
                # reached under the disposition guard above.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET lane_disposition = ?, revision = ?, "
                "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                "updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    DISPOSITION_RETIRED,
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
                f"hibernated legacy retire migration failed ({type(exc).__name__}); "
                "fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneRetireMigrationStore",)
