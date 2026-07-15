"""Lane declaration / incarnation service (Redmine #13810).

The common **declaration** surface both #13810 and #13809 share, and the explicit
generation **re-incarnation** CAS — kept in its own module (the sibling
:mod:`mozyo_bridge.core.state.lane_replacement` precedent) so the core CAS store stays a
cohesive, under-threshold unit while everything still writes the ONE shared
``lane_lifecycle_records`` row.

- :meth:`LaneDeclarationStore.declare_lane` declares a fresh ``active`` lane at
  generation 1 for **either** binding kind — an issue lane (owner ``issue_id``) or a
  project-gateway lane (owner ``project_scope`` + a provider-bound declared slot set). It
  is idempotent on an exact duplicate (the #13809 live-adopt requirement) and fail-closed
  on an owner conflict / a differing re-declaration / an unreadable store.
- :meth:`LaneDeclarationStore.open_next_generation` re-incarnates a **retired** lane as
  its next generation (a ``retired -> active`` disposition edge is forbidden; this is the
  only sanctioned re-open, owner decision j#78405), bumping ``lane_generation`` and
  resetting the release / replacement axes so a stale generation's approvals cannot act.

Like :class:`LaneReplacementStore`, this composes a :class:`LaneLifecycleStore` for the
container guard + autocommit connection and drives its own ``BEGIN IMMEDIATE`` CAS on the
shared row via the low-level helpers in
:mod:`mozyo_bridge.core.state.lane_lifecycle_rows`. It never gains disposition / release /
replacement mutation authority through this surface — only declaration and incarnation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    BINDING_KIND_PROJECT_GATEWAY,
    BINDING_KINDS,
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_OWNER_CONFLICT,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_RETIRED,
    RELEASE_NOT_REQUESTED,
    REPLACEMENT_NOT_REQUESTED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleKey,
    ProcessGenerationPin,
    encode_declared_slots,
    norm,
    rehydrate_allowed,
    replacement_settled,
    validate_declared_slots,
)
from mozyo_bridge.core.state.lane_lifecycle_rows import (
    _active_owner,
    _active_project_owner,
    _insert_active_row,
    _locked_row,
    _rollback,
    _utc_now,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    TABLE as _TABLE,
    LaneLifecycleError,
)


class LaneDeclarationStore:
    """Declaration + generation-reopen CAS for the shared lane-lifecycle row."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    def declare_lane(
        self,
        key: LaneLifecycleKey,
        *,
        decision: DecisionPointer,
        binding_kind: str = BINDING_KIND_ISSUE,
        issue_id: str = "",
        project_scope: str = "",
        declared_slots: Sequence[ProcessGenerationPin] = (),
        worktree_identity: str = "",
        now: Optional[str] = None,
    ) -> CasOutcome:
        """The common declaration / backfill service for BOTH binding kinds (Redmine #13810).

        The single fail-closed surface #13810 and #13809 share: it declares a fresh
        ``active`` lane at generation 1, whether the binding is an **issue**
        (``binding_kind='issue'`` + optional ``issue_id``, empty for an unbound lane) or a
        **project gateway** (``binding_kind='project_gateway'`` + a canonical full
        ``project_scope`` and a non-empty declared slot set). The full scope is never
        inferred from the derived lane id (j#78386 §6); the caller supplies it.

        Idempotent by declaration identity (the #13809 live-adopt requirement): re-declaring
        the **exact** same active lane — same binding kind, same issue / scope, same worktree
        identity, same declared slot snapshot — is a no-op success (``applied=True``), so
        adopting a live pair twice never conflicts and never adds a process. A row at the same
        key whose binding / worktree / slots **differ**, or which is not ``active``, is
        :data:`CAS_ALREADY_DECLARED` (a divergent re-declare is never silently accepted) — a
        re-declare never silently overwrites an existing authority row (the
        tombstone-reviving ``lane_metadata.upsert`` anti-pattern). An issue / project scope
        already actively owned by *another* lane is :data:`CAS_OWNER_CONFLICT` (the storage
        index, not a later check, makes double ownership impossible). Every refusal is
        zero-write.

        Bulk / implicit backfill is out of scope: this declares one exact lane from one
        durable decision. A legacy rowless lane is re-declared explicitly, never guessed.
        """
        kind = norm(binding_kind)
        if kind not in BINDING_KINDS:
            raise ValueError(f"unknown lane binding kind {binding_kind!r}")
        issue = norm(issue_id)
        scope = norm(project_scope)
        worktree = norm(worktree_identity)
        # An unusable declared slot (missing identity/evidence) or a duplicate slot fails
        # here, never stored (the ProcessGenerationPin R1-F4 discipline).
        pinned = validate_declared_slots(tuple(declared_slots))
        encoded_slots = encode_declared_slots(pinned)
        if kind == BINDING_KIND_ISSUE:
            if scope:
                raise ValueError("an issue lane owns no project scope")
            # A bound issue lane's decision must be filed on that same issue; an unbound
            # lane accepts any complete anchor (R2-F1, as `declare_active`).
            if not decision.authorizes_binding(issue):
                raise DecisionPointerError(
                    f"decision is anchored to issue {decision.issue_id!r} but the lane "
                    f"is being bound to {issue!r}"
                )
        else:  # BINDING_KIND_PROJECT_GATEWAY
            if issue:
                raise ValueError("a project-gateway lane owns a scope, not an issue")
            if not scope:
                raise ValueError(
                    "a project-gateway lane requires a canonical full project scope"
                )
            if not pinned:
                raise ValueError(
                    "a project-gateway declaration requires its provider-bound slot set"
                )
        stamp = now or _utc_now()
        conn = self._lifecycle._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _locked_row(conn, key)
            if existing is not None:
                # Exact duplicate of an ACTIVE declaration -> idempotent no-op success.
                # Anything else at this key (different binding / slots, or a non-active
                # disposition) is a real conflict a re-declare must not overwrite.
                if (
                    existing.lane_disposition == DISPOSITION_ACTIVE
                    and norm(existing.binding_kind) == kind
                    and existing.issue_id == issue
                    and existing.project_scope == scope
                    and existing.worktree_identity == worktree
                    and existing.declared_slots == encoded_slots
                ):
                    conn.execute("ROLLBACK")
                    return CasOutcome(
                        applied=True,
                        reason=CAS_APPLIED,
                        revision=existing.revision,
                    )
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=existing.revision,
                )
            if kind == BINDING_KIND_ISSUE:
                if issue and _active_owner(conn, key.repo_workspace_id, issue):
                    conn.execute("ROLLBACK")
                    return CasOutcome(applied=False, reason=CAS_OWNER_CONFLICT)
            else:
                if _active_project_owner(conn, key.repo_workspace_id, scope):
                    conn.execute("ROLLBACK")
                    return CasOutcome(applied=False, reason=CAS_OWNER_CONFLICT)
            try:
                _insert_active_row(
                    conn,
                    key=key,
                    issue=issue,
                    decision=decision,
                    revision=1,
                    stamp=stamp,
                    worktree=worktree,
                    binding_kind=kind,
                    project_scope=scope,
                    lane_generation=1,
                    declared_slots=encoded_slots,
                )
            except sqlite3.IntegrityError:
                # The owner index is the backstop the pre-checks above should have caught.
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_OWNER_CONFLICT)
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=1)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane declaration failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def backfill_active_binding(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        issue_id: str,
        worktree_identity: str,
        declared_slots: Sequence[ProcessGenerationPin] = (),
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Fill the MISSING binding of an existing ``active`` issue owner row (Redmine #13809).

        The bounded residual companion to :meth:`declare_lane`, for the measured live-adopt
        gap (j#78944 / j#78945): a **pre-#13754 legacy** owner row is already ``active`` and
        already owns its issue, but its ``worktree_identity`` is empty — so ``declare_lane``
        treats the live adopt as a *divergent* re-declare (worktree differs) and refuses
        zero-write, and ``retire --execute`` stays permanently blocked on
        ``worktree_binding_unverified``. This surface fills that one empty field (and the
        empty ``declared_slots`` snapshot) via an exact ``expected_revision`` CAS, so the
        gate-verified live worktree + typed pins land on the row the lane already owns.

        Deliberately **not** a relaxation of ``declare_lane``'s "a divergent re-declare must
        not overwrite" (the tombstone-reviving ``lane_metadata.upsert`` anti-pattern the
        component exists to prevent). It writes ONLY when every check holds:

        - the row exists, is ``active``, is an ``issue`` binding, owns **this exact** issue,
          and owns no project scope — otherwise :data:`CAS_UNEXPECTED_STATE` (a non-active
          disposition, a different / project-gateway binding, or a *different issue* is
          zero-write, never coerced);
        - its ``worktree_identity`` is **empty** (the gap). A row already bound to a
          worktree is never overwritten: an exact match (same worktree + same slots) is an
          idempotent no-op success, and any *non-empty* divergence is :data:`CAS_ALREADY_DECLARED`
          zero-write. A non-empty ``declared_slots`` that differs is likewise refused — the
          missing-field surface fills a gap, it never edits an existing snapshot;
        - the caller's ``expected_revision`` still matches — a concurrent write that moved
          the row loses :data:`CAS_STALE_REVISION` rather than clobbering the newer state.

        The lane's disposition / generation / release / replacement / decision anchor are
        untouched; only the empty binding fields are filled. ``issue_id`` and
        ``worktree_identity`` are required non-empty (this surface only backfills a bound
        issue lane's worktree identity, never guesses one).
        """
        issue = norm(issue_id)
        worktree = norm(worktree_identity)
        if not issue:
            raise ValueError(
                "a binding backfill requires the exact issue the row must already own"
            )
        if not worktree:
            raise ValueError(
                "a binding backfill requires a non-empty canonical worktree identity"
            )
        pinned = validate_declared_slots(tuple(declared_slots))
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
                # A concurrent declare / transition moved the row; never clobber it.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False, reason=CAS_STALE_REVISION, revision=current.revision
                )
            if (
                current.lane_disposition != DISPOSITION_ACTIVE
                or norm(current.binding_kind) != BINDING_KIND_ISSUE
                or current.issue_id != issue
                or current.project_scope
            ):
                # Only an active issue lane that already owns THIS exact issue is a backfill
                # target: a non-active disposition, a project-gateway binding, or a different
                # issue is a genuinely different row, refused zero-write (never coerced).
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if current.worktree_identity:
                # The row is already bound. Fill nothing: an exact match is an idempotent
                # no-op, any non-empty divergence is a conflict declare_lane already refuses.
                if (
                    current.worktree_identity == worktree
                    and current.declared_slots == encoded_slots
                ):
                    conn.execute("ROLLBACK")
                    return CasOutcome(
                        applied=True, reason=CAS_APPLIED, revision=current.revision
                    )
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )
            if current.declared_slots and current.declared_slots != encoded_slots:
                # The worktree is missing but a divergent slot snapshot already exists: this
                # is not a clean legacy gap, so it is not silently overwritten either.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET worktree_identity = ?, declared_slots = ?, "
                "revision = ?, updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    worktree,
                    encoded_slots,
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
                f"lane binding backfill failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def open_next_generation(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        expected_generation: int,
        decision: DecisionPointer,
        declared_slots: Sequence[ProcessGenerationPin] = (),
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Re-incarnate a **retired** lane as its next generation (Redmine #13810).

        A retired generation is terminal; a lane is never revived by a ``retired -> active``
        disposition edge (that edge is forbidden, and an implicit revive is exactly what the
        owner decision j#78405 forbids). Re-running the same semantic route is this explicit,
        CAS-guarded step instead: the row's ``lane_generation`` bumps to +1, its disposition
        returns to ``active``, and its release / replacement axes reset — so any approval,
        pin, or action id anchored to the *previous* generation is stale and cannot act on
        the new one.

        Guarded on the exact ``expected_revision`` AND ``expected_generation``: a caller
        holding a stale view of either loses (:data:`CAS_STALE_REVISION` /
        :data:`CAS_GENERATION_MISMATCH`) rather than opening a second incarnation. The lane's
        binding (kind, issue, project scope, worktree) is preserved — this is the same lane,
        re-incarnated, not a re-declaration — while ``declared_slots`` records the new
        generation's observed slot set. If the issue / scope was taken by another active lane
        while this one was retired, the re-open is refused :data:`CAS_OWNER_CONFLICT`.
        """
        pinned = validate_declared_slots(tuple(declared_slots))
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
            if current.lane_generation != expected_generation:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
            if current.lane_disposition != DISPOSITION_RETIRED:
                # Only a retired generation is re-openable; a live lane is not re-incarnated.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if not replacement_settled(current.replacement_state):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if not rehydrate_allowed(current.process_release):
                # Reopening is a return-to-active path, so it obeys the same in-flight
                # release fence as ``transition_disposition``'s rehydrate (Redmine #13810
                # R1-F2): a ``requested`` / ``partial`` generation means an actuator may be
                # closing this lane's pinned slots right now. Silently resetting it to
                # ``not_requested`` (as the write below does) would abandon that in-flight
                # release; only a finished one (never opened, or fully ``released``) may be
                # cleared, so a still-open release fails closed zero-write.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if not decision.authorizes_binding(current.issue_id):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            # While this lane was retired another lane may have taken its issue / scope.
            if norm(current.binding_kind) == BINDING_KIND_ISSUE and current.issue_id:
                owner = _active_owner(conn, key.repo_workspace_id, current.issue_id)
                if owner is not None and owner != key.lane_id:
                    conn.execute("ROLLBACK")
                    return CasOutcome(
                        applied=False,
                        reason=CAS_OWNER_CONFLICT,
                        revision=current.revision,
                    )
            elif (
                norm(current.binding_kind) == BINDING_KIND_PROJECT_GATEWAY
                and current.project_scope
            ):
                owner = _active_project_owner(
                    conn, key.repo_workspace_id, current.project_scope
                )
                if owner is not None and owner != key.lane_id:
                    conn.execute("ROLLBACK")
                    return CasOutcome(
                        applied=False,
                        reason=CAS_OWNER_CONFLICT,
                        revision=current.revision,
                    )
            if norm(current.binding_kind) == BINDING_KIND_PROJECT_GATEWAY and not pinned:
                # A project-gateway lane always owns a provider-bound slot set; the new
                # generation must declare it too (Redmine #13810 R1-F3). Only ``declare_lane``
                # enforced this at create time — the reopen must re-check the kind-specific
                # requirement rather than accept an empty snapshot for the new incarnation.
                conn.execute("ROLLBACK")
                raise ValueError(
                    "a project-gateway generation reopen requires its provider-bound slot set"
                )
            revision = current.revision + 1
            generation = current.lane_generation + 1
            try:
                conn.execute(
                    f"UPDATE {_TABLE} SET lane_disposition = ?, lane_generation = ?, "
                    "process_release = ?, release_action_id = ?, release_pins = ?, "
                    "replacement_state = ?, replacement_action_id = ?, "
                    "replacement_pins = ?, declared_slots = ?, revision = ?, "
                    "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                    "updated_at = ? "
                    "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                    (
                        DISPOSITION_ACTIVE,
                        generation,
                        RELEASE_NOT_REQUESTED,
                        "",
                        "",
                        REPLACEMENT_NOT_REQUESTED,
                        "",
                        "",
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
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_OWNER_CONFLICT,
                    revision=current.revision,
                )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane generation reopen failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneDeclarationStore",)
