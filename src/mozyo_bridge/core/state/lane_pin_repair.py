"""Hibernated bound-lane declared-pin repair CAS (Redmine #13879).

The bounded companion the #13809 backfill, the #13841 legacy migration, the #13842 live
reconcile and the #13845 bound retire all leave uncovered. A **hibernated / released BOUND**
lifecycle row whose ``worktree_identity`` is non-empty but whose ``declared_slots`` snapshot
is **empty**, while the lane's exact managed pair is observed **live**, can be repaired by
none of them — so ``sublane recover-pair`` (#13847), which requires the declared pins, can
never start:

- ``backfill_active_binding`` (Redmine #13809) fills exactly this pins-only gap, but only on
  an **active** row; a hibernated row is refused ``CAS_UNEXPECTED_STATE`` there.
- ``retire_released_hibernated_legacy`` (Redmine #13841) requires an **empty**
  ``worktree_identity`` — the defining legacy signature — and terminalizes rather than repairs.
- ``retire_reconciled_hibernated_legacy`` (Redmine #13842) requires an empty
  ``worktree_identity`` **and** empty ``declared_slots``, and its actuation is a retire-first
  close: the opposite disposition of a lane whose work is still owed.
- ``retire_released_hibernated_bound`` (Redmine #13845) matches the bound signature but targets
  the **live-zero** case and also terminalizes.

The measured shape (Redmine #13879, live evidence #13846 j#79915: rev4 / gen1, worktree binding
present, declared pins absent, an exact pair observed live) therefore has no convergence path:
recover-pair fails ``hibernated_record_missing_pins`` forever.

This surface fills **only the empty ``declared_slots`` snapshot** through one bounded
``BEGIN IMMEDIATE`` CAS — metadata only, no process launch / close / resume / send, no worktree
or branch removal. It is deliberately NOT a relaxation of ``recover-pair``'s declared-pins
precondition (Redmine #13847 owns that contract): it repairs the metadata that precondition
reads, and leaves the precondition itself untouched.

Like :class:`...lane_bound_retire.LaneBoundRetireStore` (#13845) and
:class:`...lane_reconcile_binding.LaneReconcileBindingStore` (#13842), this composes a
:class:`LaneLifecycleStore` for the container guard + autocommit connection and drives its own
CAS on the shared ``lane_lifecycle_records`` row through the low-level helpers in
:mod:`mozyo_bridge.core.state.lane_lifecycle_rows`. It deliberately does NOT parameterize the
sibling CAS surfaces over their worktree / pins predicates: those guards are safety contracts
reviewed against their own ticket's evidence, and a shared "empty-or-matching" predicate is one
edit away from admitting the shape a sibling exists to refuse (#13845 review j#80187). Each
surface states its full signature literally and refuses everything else zero-write. The #13842
signature (worktree **empty**) and this one (worktree **non-empty AND matching**) are mutually
exclusive by construction, so no row is ever a target of both.
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
    CAS_GENERATION_MISMATCH,
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


class LanePinRepairStore:
    """Bounded declared-pin repair CAS for a hibernated / released BOUND lane (#13879)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    @property
    def last_write_preparation(self):
        """The last mutation's explicit-write-gate result (Redmine #13844 R3-F2).

        Delegates to the wrapped lifecycle store so the repair command can surface the
        pre-migration preflight + post migration outcome (peer-reader risk) in its typed
        outcome, exactly as the #13841 / #13842 / #13845 siblings do.
        """
        return self._lifecycle.last_write_preparation

    def repair_hibernated_bound_pins(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        expected_generation: int,
        issue_id: str,
        worktree_identity: str,
        declared_slots: Sequence[ProcessGenerationPin],
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Fill the EMPTY ``declared_slots`` of a hibernated / released BOUND row (#13879).

        Writes the typed pin snapshot ONLY when every part of the exact repair signature holds
        — otherwise zero-write, so an active / retired lane, an unproven release, an EMPTY
        (legacy, #13841 / #13842's) binding, a binding naming a DIFFERENT worktree, a different
        issue, an ALREADY-pinned row, or a concurrent write never has its pins rewritten:

        - the row exists (:data:`CAS_NOT_FOUND`) and its ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION` — a concurrent declare / transition that moved the row
          loses rather than clobbering the newer state);
        - its ``lane_generation`` still matches ``expected_generation``
          (:data:`CAS_GENERATION_MISMATCH`): the pins name an observed process generation, so a
          row re-incarnated since the caller observed the pair is a different generation and its
          empty snapshot is NOT this pair's to fill;
        - it is ``hibernated`` (an ``active`` lane repairs through the #13809 backfill; a
          ``superseded`` / ``retired`` row is terminal), is an ``issue`` binding, owns **this
          exact** issue, and owns no project scope (:data:`CAS_UNEXPECTED_STATE`);
        - its ``worktree_identity`` is **non-empty AND equal to** the caller's resolved token —
          the defining bound signature, and the inverse of #13841's / #13842's. An empty binding
          is a legacy row those surfaces own, never this one; a non-empty MISMATCH means the
          caller's ``--worktree`` belongs to a different lane, so it is refused rather than
          coerced (:data:`CAS_UNEXPECTED_STATE`). The token is re-checked HERE under the row lock
          and not merely at the command's action-time observation: the pre-check is a diagnostic,
          this is the authority (the #13845 j#80148 discipline);
        - its process release is durably ``released`` and no receiver replacement is in flight
          (:func:`replacement_settled`) — an in-flight release / replacement means an actuator may
          be mutating this lane's slots right now (:data:`CAS_FORBIDDEN_TRANSITION`);
        - the ``decision`` anchor names this issue (a bound row is only decided by a record filed
          on its own issue).

        **Replay is byte-equal-only idempotent** (Redmine #13879 acceptance 4): a row whose
        ``declared_slots`` already encode **exactly** the incoming set is an idempotent no-op
        success (``applied=True``, revision unchanged); a row whose snapshot is **non-empty and
        different** — a recycled generation whose live locators differ, or a foreign pin set — is
        :data:`CAS_ALREADY_DECLARED` zero-write. An established snapshot is never overwritten:
        this surface fills a gap, it never edits an existing pin set. ``declared_slots`` is
        required non-empty (an empty "repair" would write nothing and prove nothing).

        ``declared_slots`` is the ONLY row field this writes (plus the decision anchor + the
        revision bump). The row's ``lane_disposition``, ``lane_generation``, ``worktree_identity``,
        ``process_release``, ``replacement_*`` and ``reconcile_phase`` are all **preserved** — the
        repair is metadata-only and leaves the lane hibernated, so no process is launched, closed,
        resumed, or sent to, and ``recover-pair`` remains the surface that acts on the pins
        (Redmine #13879 acceptance 3). Leaving ``reconcile_phase`` empty keeps a repaired row
        distinguishable from a #13842 reconcile-owed close.
        """
        issue = norm(issue_id)
        if not issue:
            raise ValueError(
                "a hibernated bound pin repair requires the exact issue the row must already own"
            )
        want_worktree = norm(worktree_identity)
        if not want_worktree:
            raise ValueError(
                "a hibernated bound pin repair requires the canonical worktree token the row's "
                "binding must equal; an empty token is the #13841 / #13842 legacy signature, not "
                "this surface's"
            )
        # An unusable / duplicate pin fails here, never stored (the ProcessGenerationPin R1-F4
        # discipline shared with ``declare_lane``).
        pinned = validate_declared_slots(tuple(declared_slots))
        if not pinned:
            raise ValueError(
                "a pin repair requires the observed slot set to fill; an empty snapshot would "
                "repair nothing and leave recover-pair blocked on the same missing pins"
            )
        encoded_slots = encode_declared_slots(pinned)
        if not decision.authorizes_binding(issue):
            raise DecisionPointerError(
                f"decision is anchored to issue {decision.issue_id!r} but the repair targets "
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
            if current.lane_generation != expected_generation:
                # The pins name a process generation the caller observed against THIS row's
                # generation; a re-incarnation since then is a different generation whose empty
                # snapshot is not this pair's to fill.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
            if (
                current.lane_disposition != DISPOSITION_HIBERNATED
                or norm(current.binding_kind) != BINDING_KIND_ISSUE
                or current.issue_id != issue
                or current.project_scope
                or norm(current.worktree_identity) != want_worktree
            ):
                # Not the exact bound signature: an active row (the #13809 backfill's target), a
                # superseded / retired row, a project-gateway binding, a different issue, an EMPTY
                # worktree binding (the #13841 / #13842 legacy signature — their target, never
                # this one's), or a binding naming a DIFFERENT worktree (the caller's --worktree
                # belongs to another lane). Refused zero-write, never coerced.
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
            if current.declared_slots == encoded_slots:
                # Byte-equal replay -> idempotent no-op success (acceptance 4). Checked BEFORE the
                # non-empty refusal below so a re-run of the exact same repair is a success, not an
                # ``already_declared`` conflict.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=True, reason=CAS_APPLIED, revision=current.revision
                )
            if current.declared_slots:
                # A non-empty snapshot that differs is a divergent (recycled / foreign) generation
                # — its live locators or identities differ — and is never silently overwritten.
                # This surface fills an EMPTY snapshot; it never edits an established one.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET declared_slots = ?, revision = ?, "
                "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                "updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    encoded_slots,
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
                f"hibernated bound pin repair failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LanePinRepairStore",)
