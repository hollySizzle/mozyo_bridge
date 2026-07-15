"""Hibernated live-contradiction reconcile binding CAS (Redmine #13842).

The bounded companion to the #13841 metadata-only retire migration, for the case the
migration deliberately refuses: a **hibernated / released legacy** lifecycle row — the
coordinator hibernated the lane, its process release completed durably
(``process_release`` reached ``released``), its ``worktree_identity`` is EMPTY (a
pre-#13754 row that never recorded one) — whose exact managed pair is nonetheless observed
**live** in the action-time Herdr inventory (#13756 j#79188). Three existing contracts leave
it with no convergence path:

- the #13841 live-zero migration correctly refuses zero-write on ``live_pair_present`` (its
  guard requires an empty live inventory);
- the #13754 guarded ``retire --execute`` fails closed on ``worktree_binding_unverified``
  (an empty worktree binding can never be attested);
- the #13809 ``backfill_active_binding`` fills an **active** owner row only — a hibernated
  row is ``CAS_UNEXPECTED_STATE`` there.

This surface is the first half of the reconcile: it **re-establishes** the missing worktree
+ process (``declared_slots``) binding on the hibernated legacy row via one bounded
``BEGIN IMMEDIATE`` CAS, so the existing #13754 guarded close can then attest and close the
exact live pair and record the terminal ``retired`` disposition. It is the hibernated
analogue of :meth:`LaneDeclarationStore.backfill_active_binding`:

- ``backfill_active_binding`` fills the binding of an **active** owner row;
- this fills the binding of a **hibernated + released** owner row.

It mutates NO disposition (the row stays ``hibernated``; the subsequent guarded close moves
it to ``retired``), launches / closes / resumes no process, and removes no worktree / branch.
Like the sibling stores it composes a :class:`LaneLifecycleStore` for the container guard +
autocommit connection and drives its own CAS on the shared ``lane_lifecycle_records`` row
through the low-level helpers in :mod:`mozyo_bridge.core.state.lane_lifecycle_rows`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_HIBERNATED,
    RELEASE_RELEASED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleKey,
    ProcessGenerationPin,
    encode_declared_slots,
    norm,
    replacement_settled,
    validate_declared_slots,
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


class LaneReconcileBindingStore:
    """Bounded worktree + declared-slots rebind for a released hibernated legacy row (#13842)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    def rebind_released_hibernated_legacy(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        issue_id: str,
        worktree_identity: str,
        declared_slots: Sequence[ProcessGenerationPin],
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Fill the MISSING binding of a hibernated / released legacy owner row, or fail closed.

        Writes the empty ``worktree_identity`` + ``declared_slots`` binding fields **plus the
        reconcile's own ``decision`` anchor** (Redmine #13842 review j#79244 F1), and ONLY when
        every part of the exact reconcilable signature holds — so an active / superseded /
        retired row, an unproven / in-flight release, a receiver replacement in flight, a
        different issue / binding, an already-bound-to-a-*different*-token row, or a concurrent
        write never rebinds:

        The ``decision`` anchor is the reconcile-specific **provenance** the caller's owed-state
        resume keys on: a #13809 ``backfill_active_binding`` row carries the SAME
        ``worktree_identity`` + ``declared_slots`` shape but its decision names the declare /
        hibernate journal, NOT this reconcile's, so recording the reconcile decision here — in
        the SAME atomic CAS that establishes the binding, leaving no window between the rebind
        and its provenance — lets a positive-absence resume tell "this reconcile rebound and
        owes a retirement" apart from "a pre-existing bound row whose pair happens to be gone"
        (review j#79244 F1). It is deliberately NOT the #13809 backfill's "fill a gap, do not
        touch authority" posture: the reconcile rebind IS a deliberate owner-authorized step of
        a retire flow, so it re-anchors the decision to the record that authorized it.

        - the row exists (:data:`CAS_NOT_FOUND`) and its ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION` — a concurrent declare / transition that moved the row
          loses rather than clobbering the newer state);
        - it is ``hibernated`` (an ``active`` lane still holds its work and backfills through
          #13809; a ``superseded`` / ``retired`` row is not this reconcile's target), is an
          ``issue`` binding, owns **this exact** issue, and owns no project scope
          (:data:`CAS_UNEXPECTED_STATE`);
        - its process release is durably ``released`` and no receiver replacement is in flight
          (:data:`CAS_FORBIDDEN_TRANSITION`) — the "already released" proof the caller pairs
          with a live-inventory read; a ``not_requested`` / ``requested`` / ``partial`` release
          is unproven or in flight (an actuator may still be closing panes);
        - its ``worktree_identity`` is **empty or already equal** to the incoming token, AND
          its ``declared_slots`` snapshot is **empty or already equal** to the incoming set.
          An established binding is never overwritten: a *non-empty different* worktree, or a
          *non-empty different* slot snapshot (a recycled generation whose live locators
          differ), is :data:`CAS_ALREADY_DECLARED` zero-write. Both fields already exactly
          present is an idempotent no-op success (the replayable reconcile flow re-runs this
          without a second write); otherwise the empty field(s) are filled.

        Deliberately mutates NO disposition, release, replacement, or generation — the row
        stays ``hibernated`` and the subsequent revision-guarded retire CAS moves it to
        ``retired``. It DOES re-anchor the decision (provenance, above). ``issue_id`` and
        ``worktree_identity`` are required non-empty (this surface only rebinds a bound issue
        lane's binding, never guesses one), and the ``declared_slots`` set records the exact
        live pair the reconcile committed to closing.
        """
        issue = norm(issue_id)
        worktree = norm(worktree_identity)
        if not issue:
            raise ValueError(
                "a hibernated legacy reconcile rebind requires the exact issue the row "
                "must already own"
            )
        if not worktree:
            raise ValueError(
                "a hibernated legacy reconcile rebind requires a non-empty canonical "
                "worktree identity"
            )
        if not decision.authorizes_binding(issue):
            raise DecisionPointerError(
                f"decision is anchored to issue {decision.issue_id!r} but the reconcile "
                f"rebind targets a lane bound to {issue!r}"
            )
        # An unusable declared slot (missing identity / evidence) or a duplicate slot fails
        # here, never stored (the ProcessGenerationPin discipline). The reconcile always
        # supplies the exact live pair, so an empty set is a caller error.
        pinned = validate_declared_slots(tuple(declared_slots))
        if not pinned:
            raise ValueError(
                "a hibernated legacy reconcile rebind requires the observed live pair's "
                "declared slot set (the process binding it re-establishes)"
            )
        encoded_slots = encode_declared_slots(pinned)
        stamp = now or _utc_now()
        conn = self._lifecycle._connect()
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
            ):
                # Not the exact reconcilable signature: an active / superseded / retired row,
                # a project-gateway binding, or a different issue. Refused zero-write.
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
            if current.worktree_identity and current.worktree_identity != worktree:
                # An established worktree binding is never overwritten by a different one —
                # this surface fills a gap, it never edits an existing binding.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )
            if current.declared_slots and current.declared_slots != encoded_slots:
                # A non-empty slot snapshot that differs is a divergent (recycled) generation —
                # its live locators differ — and is never silently overwritten either.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )
            if (
                current.worktree_identity == worktree
                and current.declared_slots == encoded_slots
                and current.decision_source == decision.source
                and current.decision_issue_id == decision.issue_id
                and current.decision_journal == decision.journal_id
            ):
                # Nothing to change: the binding AND the reconcile decision anchor are already
                # exactly present -> idempotent no-op. The replayable reconcile flow (a crash
                # after the rebind commit) re-runs this and resumes without a second write.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=True, reason=CAS_APPLIED, revision=current.revision
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET worktree_identity = ?, declared_slots = ?, "
                "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                "revision = ?, updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    worktree,
                    encoded_slots,
                    decision.source,
                    decision.issue_id,
                    decision.journal_id,
                    revision,
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
                f"hibernated legacy reconcile rebind failed ({type(exc).__name__}); "
                "fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneReconcileBindingStore",)
