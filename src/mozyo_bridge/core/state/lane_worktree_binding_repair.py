"""Hibernated pinned-lane worktree-binding repair CAS (Redmine #14475, review j#88490).

The exact MIRROR of the #13879 pin repair, for the gap #14475 measured: a **hibernated /
released BOUND-BY-PINS** lifecycle row whose ``declared_slots`` snapshot is non-empty but
whose ``worktree_identity`` is **EMPTY**. No existing surface converges it:

- ``backfill_active_binding`` (Redmine #13809) fills exactly this worktree gap, but only on an
  **active** row; a hibernated row is refused ``CAS_UNEXPECTED_STATE`` there;
- ``repair_hibernated_bound_pins`` (Redmine #13879) is the inverse signature — it requires a
  **non-empty, matching** ``worktree_identity`` and fills the EMPTY pins;
- ``retire_released_hibernated_legacy`` (#13841) and ``retire_reconciled_hibernated_legacy``
  (#13842) do match an empty binding, but they **terminalize** (retire) rather than repair —
  the opposite disposition for a lane whose work is still owed;
- ``retire_released_hibernated_bound`` (#13845) matches a bound row and also terminalizes.

So the #14462 shape — pins present, worktree unbound, work owed — could be *diagnosed* by the
#14475 pre-close fences and then **recovered by nothing**: ``sublane recover-pair`` blocks on
``lane_worktree_binding_unverified`` forever, and the documented "re-run the lane's own
declaration surface" runbook only ever applied to an ``active`` row.

This surface fills **only the empty ``worktree_identity``** through one bounded
``BEGIN IMMEDIATE`` CAS — metadata only, no process launch / close / resume / send, no worktree
or branch mutation. It does NOT relax ``recover-pair``'s worktree-binding precondition (#14475
owns that contract): it repairs the metadata that precondition reads.

Following the #13845 review j#80187 discipline the sibling stores state explicitly: this does
**not** parameterize an existing CAS over an "empty-or-matching" predicate — one edit away from
admitting the shape a sibling exists to refuse. It states its full signature literally and
refuses everything else zero-write. The #13879 signature (worktree **non-empty**, pins
**empty**) and this one (worktree **empty**, pins **non-empty**) are mutually exclusive by
construction, so no row is ever a target of both.
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
from mozyo_bridge.core.state.lane_worktree_binding_signature import (
    SIGNATURE_BINDING_KIND,
    SIGNATURE_INVALID_PINS,
    SIGNATURE_MISSING_PINS,
    SIGNATURE_NOT_HIBERNATED,
    SIGNATURE_PINS_NOT_CANONICAL,
    SIGNATURE_PROJECT_SCOPE,
    SIGNATURE_RELEASE_NOT_SETTLED,
    SIGNATURE_REPLACEMENT_IN_FLIGHT,
    SIGNATURE_WRONG_ISSUE,
    classify_repair_signature,
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


#: Signature axes the CAS reports as ``unexpected_state`` (a row that is a different shape).
_UNEXPECTED_STATE_SIGNATURES = frozenset(
    {
        SIGNATURE_NOT_HIBERNATED,
        SIGNATURE_BINDING_KIND,
        SIGNATURE_WRONG_ISSUE,
        SIGNATURE_PROJECT_SCOPE,
        SIGNATURE_MISSING_PINS,
        SIGNATURE_INVALID_PINS,
        SIGNATURE_PINS_NOT_CANONICAL,
    }
)
#: Signature axes the CAS reports as ``forbidden_transition`` (right shape, wrong moment).
_FORBIDDEN_TRANSITION_SIGNATURES = frozenset(
    {SIGNATURE_RELEASE_NOT_SETTLED, SIGNATURE_REPLACEMENT_IN_FLIGHT}
)


class LaneWorktreeBindingRepairStore:
    """Bounded worktree-binding repair CAS for a hibernated / released PINNED lane (#14475)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    @property
    def last_write_preparation(self):
        """The last mutation's explicit-write-gate result (Redmine #13844 R3-F2).

        Delegates to the wrapped lifecycle store so the repair command can surface the
        pre-migration preflight + post-migration outcome (peer-reader risk) in its typed
        outcome, exactly as the #13841 / #13842 / #13845 / #13879 siblings do.
        """
        return self._lifecycle.last_write_preparation

    def repair_hibernated_worktree_binding(
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
        """Fill the EMPTY ``worktree_identity`` of a hibernated / released PINNED row (#14475).

        Writes the canonical worktree token ONLY when every part of the exact repair signature
        holds — otherwise zero-write, so an active / retired lane, an unproven release, an
        already-bound row, a row whose pins differ from the caller's observation, a different
        issue, or a concurrent write never has its binding written:

        - the row exists (:data:`CAS_NOT_FOUND`) and its ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION` — a concurrent declare / transition that moved the row
          loses rather than clobbering the newer state);
        - its ``lane_generation`` still matches ``expected_generation``
          (:data:`CAS_GENERATION_MISMATCH`): a row re-incarnated since the caller observed it is
          a different generation, and its binding is not this observation's to write;
        - it is ``hibernated`` (an ``active`` lane binds through the #13809 backfill; a
          ``superseded`` / ``retired`` row is terminal), is an ``issue`` binding, owns **this
          exact** issue, and owns no project scope (:data:`CAS_UNEXPECTED_STATE`);
        - its ``declared_slots`` snapshot is **non-empty AND byte-equal to** the caller's
          validated set — the defining pinned signature, and the inverse of #13879's. An EMPTY
          snapshot is the #13879 / #13842 shape those surfaces own, never this one's; a
          snapshot that DIFFERS means the caller observed a different (recycled / foreign)
          generation, so it is refused rather than coerced. The pins are re-checked HERE under
          the row lock and not merely at the command's action-time observation: the pre-check is
          a diagnostic, this is the authority (the #13845 j#80148 discipline);
        - its process release is durably ``released`` and no receiver replacement is in flight
          (:func:`replacement_settled`) — an in-flight release / replacement means an actuator
          may be mutating this lane's slots right now (:data:`CAS_FORBIDDEN_TRANSITION`);
        - the ``decision`` anchor names this issue (a bound row is only decided by a record
          filed on its own issue).

        **Replay is byte-equal-only idempotent**: a row whose ``worktree_identity`` already
        equals the incoming token is an idempotent no-op success (``applied=True``, revision
        unchanged); a row already bound to a DIFFERENT worktree is :data:`CAS_ALREADY_DECLARED`
        zero-write. An established binding is never overwritten: this surface fills a gap, it
        never re-binds a lane. ``worktree_identity`` is required non-empty (an empty "repair"
        would write nothing and prove nothing), and so is ``declared_slots``.

        ``worktree_identity`` is the only lifecycle PAYLOAD field this writes; the statement is
        about payload, not about the row's audit metadata — the same UPDATE also stamps the
        decision anchor, the revision and ``updated_at``, as every CAS in this component does
        (review j#88496). The row's ``lane_disposition``, ``lane_generation``, ``declared_slots``,
        ``process_release``, ``replacement_*`` and ``reconcile_phase`` are all **preserved** —
        the repair is metadata-only and leaves the lane hibernated, so no process is launched,
        closed, resumed, or sent to, and ``recover-pair`` remains the surface that acts.
        """
        issue = norm(issue_id)
        if not issue:
            raise ValueError(
                "a hibernated worktree-binding repair requires the exact issue the row must "
                "already own"
            )
        want_worktree = norm(worktree_identity)
        if not want_worktree:
            raise ValueError(
                "a hibernated worktree-binding repair requires the canonical worktree token to "
                "record; an empty token would repair nothing and leave every guarded recovery "
                "blocked on the same unbound row"
            )
        # An unusable / duplicate pin fails here, never compared against (the
        # ProcessGenerationPin R1-F4 discipline shared with ``declare_lane``).
        pinned = validate_declared_slots(tuple(declared_slots))
        if not pinned:
            raise ValueError(
                "a hibernated worktree-binding repair requires the row's observed slot set to "
                "match against; an empty set is the #13879 / #13842 signature, not this "
                "surface's"
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
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
            # The row-signature axes are classified by the SHARED pure classifier (review
            # j#88526 F2), so the command's read-only preflight reaches the same verdict for
            # the same row. Writing them out separately here is what let a normalizing
            # preflight report a green the CAS then refused.
            signature = classify_repair_signature(current, issue_id=issue)
            if signature in _UNEXPECTED_STATE_SIGNATURES:
                # Not the exact pinned signature: an active row (the #13809 backfill's target),
                # a superseded / retired row, a project-gateway binding, a different issue, an
                # EMPTY pin snapshot (the #13879 / #13842 shape — their target, never this
                # one's), or a snapshot that is not the caller's canonical set.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if signature in _FORBIDDEN_TRANSITION_SIGNATURES:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            # The caller's validated set must additionally be THIS row's snapshot — the
            # classifier proves the stored bytes are canonical, this proves they are the same
            # pins the caller observed.
            if current.declared_slots != encoded_slots:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if norm(current.worktree_identity) == want_worktree:
                # Byte-equal replay -> idempotent no-op success. Checked BEFORE the non-empty
                # refusal below so a re-run of the exact same repair is a success, not a
                # conflict.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=True, reason=CAS_APPLIED, revision=current.revision
                )
            if current.worktree_identity:
                # An established binding naming a DIFFERENT worktree is never re-bound: this
                # surface fills a gap, it never moves a lane to another worktree.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET worktree_identity = ?, revision = ?, "
                "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                "updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    want_worktree,
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
                "hibernated worktree-binding repair failed "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneWorktreeBindingRepairStore",)
