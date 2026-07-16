"""Hibernated bound-lane terminal retire CAS (Redmine #13845).

The bounded companion the #13841 legacy migration, the #13842 live reconcile, and the
#13754 guarded close all leave uncovered. A **hibernated / released BOUND** lifecycle row —
the coordinator hibernated the lane, its process release completed durably
(``process_release`` reached ``released``), its live pair is gone — but whose
``worktree_identity`` is **non-empty** (a #13754 / #13809 / #13810-bound row that DID record
its canonical worktree binding) can be terminalized by none of them:

- ``retire --execute`` (Redmine #13754) attests the binding and then plans a close, but a
  zero-close is only a retire when the durable row ALREADY says ``retired``; a hibernated
  bound row with no live pair fails closed on ``zero_close_unproven`` forever (live evidence
  #13810 j#79416: ``retire_ok`` preflight true, ``closed: []``, ``durable_retirement: ""``).
- ``retire_released_hibernated_legacy`` (Redmine #13841) requires an **empty**
  ``worktree_identity`` — the defining legacy signature — so a bound row is refused
  ``CAS_UNEXPECTED_STATE`` there.
- ``retire_reconciled_hibernated_legacy`` (Redmine #13842) requires an empty
  ``worktree_identity`` **and** empty ``declared_slots``, and targets the opposite liveness
  case (an exact pair observed live).

Re-launching a fresh pair only to close it again is exactly the needless actuation the ticket
forbids. This surface instead moves such a row **directly** to the #13689 terminal ``retired``
disposition via one bounded ``BEGIN IMMEDIATE`` CAS — metadata only, no process launch /
close / resume, no worktree / branch removal.

Like :class:`...lane_retire_migration.LaneRetireMigrationStore` (#13841) and
:class:`...lane_reconcile_binding.LaneReconcileBindingStore` (#13842), this composes a
:class:`LaneLifecycleStore` for the container guard + autocommit connection and drives its own
CAS on the shared ``lane_lifecycle_records`` row through the low-level helpers in
:mod:`mozyo_bridge.core.state.lane_lifecycle_rows`. It deliberately does NOT parameterize the
#13841 CAS over its worktree predicate: those guards are safety contracts reviewed against
their own ticket's evidence, and a shared "empty-or-matching" predicate is one edit away from
admitting the shape the sibling surface exists to refuse. Each surface states its full
signature literally and refuses everything else zero-write.
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


class LaneBoundRetireStore:
    """Bounded ``hibernated -> retired`` CAS for a released BOUND lane (Redmine #13845)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    @property
    def last_write_preparation(self):
        """The last mutation's explicit-write-gate result (Redmine #13844 R3-F2).

        Delegates to the wrapped lifecycle store so the retire command can surface the
        pre-migration preflight + post migration outcome (peer-reader risk) in its typed
        outcome, exactly as the #13841 / #13842 siblings do.
        """
        return self._lifecycle.last_write_preparation

    def retire_released_hibernated_bound(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        issue_id: str,
        worktree_identity: str,
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Terminalize a hibernated / released BOUND owner row, or fail closed (#13845).

        Writes the single ``hibernated -> retired`` disposition edge ONLY when every part of
        the exact bound signature holds — otherwise zero-write, so a live / active pair, an
        unproven release, an EMPTY (legacy, #13841's) binding, a binding naming a DIFFERENT
        worktree, a different issue, or a concurrent write never terminalizes:

        - the row exists (:data:`CAS_NOT_FOUND`) and its ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION` — a concurrent declare / transition / generation open
          that moved the row loses rather than clobbering the newer state);
        - it is ``hibernated`` (an ``active`` lane still holds its work; a ``superseded`` /
          already ``retired`` row is not this surface's target), is an ``issue`` binding, owns
          **this exact** issue, and owns no project scope (:data:`CAS_UNEXPECTED_STATE`);
        - its ``worktree_identity`` is **non-empty AND equal to** the caller's attested token
          — the defining bound signature, and the inverse of #13841's. An empty binding is a
          legacy row that terminalizes through ``--migrate-hibernated-legacy``, never here; a
          non-empty MISMATCH means the caller's ``--worktree`` belongs to a different lane, so
          it is refused rather than coerced (:data:`CAS_UNEXPECTED_STATE`). The token is
          re-checked HERE under the row lock and not merely at the command's action-time
          attestation: the pre-check is a diagnostic, this is the authority;
        - its process release is durably ``released`` — the "already released" proof. A
          ``not_requested`` / ``requested`` / ``partial`` release is unproven or in flight (an
          actuator may still be closing panes), so it fails closed
          (:data:`CAS_FORBIDDEN_TRANSITION`); and no receiver replacement is in flight
          (:func:`replacement_settled`, same reason);
        - the ``decision`` anchor names this issue (a bound row is only decided by a record
          filed on its own issue).

        Deliberately NOT :meth:`LaneLifecycleStore.transition_disposition`: that generic edge
        would accept any ``hibernated -> retired`` regardless of the release / worktree /
        binding shape. The release proof and the bound-worktree signature are part of the
        guard, not the caller's promise. ``released`` records that a release *command*
        completed, not that the slots are gone (``lane_lifecycle`` boundary): the caller pairs
        this durable proof with a live-inventory zero read (Redmine #13845), so a stale record
        can never terminalize a lane whose pair is actually live.

        The disposition is the only field written (plus the decision anchor + revision). The
        row's ``worktree_identity``, ``declared_slots`` pins, ``lane_generation``,
        ``process_release``, ``replacement_*`` and ``reconcile_phase`` are all **preserved** —
        the bound row's declared pins and worktree identity survive terminalization (Redmine
        #13845 acceptance), and leaving ``reconcile_phase`` empty is what keeps an ordinary
        terminal retire distinguishable from a #13842 reconcile-owed close (review j#79320 R4).
        A duplicate replay is handled by the caller reading the already-``retired`` row (an
        idempotent success re-verified against a live-zero read), so this CAS stays strictly
        ``hibernated -> retired``.
        """
        issue = norm(issue_id)
        if not issue:
            raise ValueError(
                "a hibernated bound retire requires the exact issue the row must already own"
            )
        want_worktree = norm(worktree_identity)
        if not want_worktree:
            raise ValueError(
                "a hibernated bound retire requires the canonical worktree token the row's "
                "binding must equal; an empty token is the #13841 legacy signature, not this "
                "surface's"
            )
        if not decision.authorizes_binding(issue):
            raise DecisionPointerError(
                f"decision is anchored to issue {decision.issue_id!r} but the retire targets "
                f"a lane bound to {issue!r}"
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
                or norm(current.worktree_identity) != want_worktree
            ):
                # Not the exact bound signature: an active / superseded / retired row, a
                # project-gateway binding, a different issue, an EMPTY worktree binding (the
                # #13841 legacy signature — that surface's target, never this one's), or a
                # binding naming a DIFFERENT worktree (the caller's --worktree belongs to
                # another lane). Refused zero-write, never coerced.
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
                f"hibernated bound retire failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneBoundRetireStore",)
