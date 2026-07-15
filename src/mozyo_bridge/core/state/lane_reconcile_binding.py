"""Hibernated live-contradiction reconcile retire+bind CAS (Redmine #13842).

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

This CAS is the **retire-first** heart of the reconcile (Redmine #13842 review j#79282 R2,
correction boundary option (b)): the caller verifies the exact live pair is unique / idle /
settled / attested, then this ONE bounded ``BEGIN IMMEDIATE`` CAS both **re-establishes the
missing worktree + process (``declared_slots``) binding AND moves the row hibernated ->
retired**, guarded on the exact revision the caller verified. Doing the terminal retire
*before* the external pane close (rather than after) is what makes the close race-free:

- a rehydrate / move that raced the caller's verification bumps the revision, so the CAS
  refuses (``stale_revision`` / ``unexpected_state``) — **zero-write / zero-close**, the pane
  close is never even attempted (review j#79282 R2: a terminal CAS that runs *after* the
  close cannot un-close a pair it already killed);
- once this CAS commits, the disposition is ``retired`` (**terminal** — no ``retired ->
  active`` edge exists), so the lane's revision / generation can never change again while the
  caller closes the pinned pair. The close therefore runs under an immutable generation
  (review j#79282 R2 option (b): "revision/generation stays unchanged until the close
  completes"), and it retires ONLY on a verified live pair, so there is no absence -> retire
  path a #13809 backfill row could collide with (review j#79282 R1).

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
    ProcessGenerationPin,
    disposition_transition_allowed,
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
    """Bounded ``hibernated -> retired`` + worktree/declared-slots bind CAS for a released
    hibernated legacy row (Redmine #13842, retire-first)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    def retire_reconciled_hibernated_legacy(
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
        """Retire a verified hibernated / released legacy row, binding its pair, or fail closed.

        The single retire-first CAS: it writes the ``worktree_identity`` + ``declared_slots``
        binding AND the reconcile's ``decision`` anchor AND moves the disposition
        ``hibernated -> retired``, all guarded on the exact ``expected_revision`` the caller
        verified. It applies ONLY when every part of the exact reconcilable signature holds — so
        an active / superseded / already-retired row, an unproven / in-flight release, a
        receiver replacement in flight, a different issue / binding, an already-bound-to-a-
        *different*-token row, or a concurrent write never retires:

        - the row exists (:data:`CAS_NOT_FOUND`) and its ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION` — a concurrent declare / transition that moved the row
          loses rather than clobbering the newer state; the caller reports this as a revision
          race and closes NOTHING, review j#79282 R2);
        - it is ``hibernated`` (an ``active`` lane still holds its work and backfills through
          #13809; a ``superseded`` / already ``retired`` row is not this CAS's target), is an
          ``issue`` binding, owns **this exact** issue, and owns no project scope
          (:data:`CAS_UNEXPECTED_STATE`);
        - its process release is durably ``released`` and no receiver replacement is in flight
          (:data:`CAS_FORBIDDEN_TRANSITION`) — the "already released" proof the caller pairs
          with a live-inventory read;
        - its ``worktree_identity`` AND its ``declared_slots`` are **both empty** — the defining
          **legacy** signature (Redmine #13842 review j#79320 R1). A row with ANY existing
          binding (a #13754 / #13809 / #13810-bound row) is refused :data:`CAS_UNEXPECTED_STATE`
          zero-write: a bound row is the ordinary #13754 guarded retire's domain, and this
          legacy-contradiction surface reconciles ONLY the empty-binding legacy row the ticket
          scopes it to (non-regression of the #13754 ordinary path).

        On success the row is ``retired`` (**terminal**): no ``retired -> active`` edge exists,
        so the caller then closes the exact pinned pair under a generation that can no longer
        change (review j#79282 R2 option (b)). ``issue_id`` / ``worktree_identity`` are required
        non-empty and ``declared_slots`` must be the observed live pair (the process binding).
        """
        issue = norm(issue_id)
        worktree = norm(worktree_identity)
        if not issue:
            raise ValueError(
                "a hibernated legacy reconcile retire requires the exact issue the row "
                "must already own"
            )
        if not worktree:
            raise ValueError(
                "a hibernated legacy reconcile retire requires a non-empty canonical "
                "worktree identity"
            )
        if not decision.authorizes_binding(issue):
            raise DecisionPointerError(
                f"decision is anchored to issue {decision.issue_id!r} but the reconcile "
                f"retire targets a lane bound to {issue!r}"
            )
        # An unusable declared slot (missing identity / evidence) or a duplicate slot fails
        # here, never stored (the ProcessGenerationPin discipline). The reconcile always
        # supplies the exact live pair, so an empty set is a caller error.
        pinned = validate_declared_slots(tuple(declared_slots))
        if not pinned:
            raise ValueError(
                "a hibernated legacy reconcile retire requires the observed live pair's "
                "declared slot set (the process binding it records)"
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
                # A concurrent declare / transition (a rehydrate raced the caller's verify)
                # moved the row; refuse, so the caller reports a revision race and closes
                # nothing (review j#79282 R2).
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
                or current.declared_slots
            ):
                # Not the exact EMPTY-binding legacy signature (Redmine #13842 review j#79320
                # R1): an active / superseded / retired row, a project-gateway binding, a
                # different issue, or an already-bound row (non-empty ``worktree_identity`` or
                # ``declared_slots``). A bound row is the #13754 ordinary guarded retire's domain
                # (non-regression) — this legacy-contradiction surface reconciles ONLY the
                # empty-binding legacy row the ticket scopes it to. Refused zero-write.
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
                # hibernated -> retired is a legal edge; the backstop, never reached under the
                # disposition guard above.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET lane_disposition = ?, worktree_identity = ?, "
                "declared_slots = ?, decision_source = ?, decision_issue_id = ?, "
                "decision_journal = ?, revision = ?, updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    DISPOSITION_RETIRED,
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
                f"hibernated legacy reconcile retire failed ({type(exc).__name__}); "
                "fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneReconcileBindingStore",)
