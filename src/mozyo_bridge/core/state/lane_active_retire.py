"""Active live-zero terminal retire CAS (Redmine #14242).

The fourth bounded terminalizer, for the shape the other three refuse. An **ACTIVE bound**
lifecycle row whose managed pair is already positively gone — the lane's issue is closed, its
work integrated, its panes released outside the lifecycle store (a crash, an external close, a
host restart, or the #14222 US-close drain) — can be terminalized by none of them:

- ``retire --execute`` (Redmine #13754) plans a close, finds nothing to close, and a zero-close
  only counts as a retire when the durable row ALREADY says ``retired``. An active row with no
  live pair therefore fails closed on ``zero_close_unproven`` forever (live evidence #14222
  j#85208-j#85209: preflight ``retire_ok``, ``closed: []``, ``durable_retirement: ""``).
- ``retire_released_hibernated_bound`` (Redmine #13845) requires ``hibernated`` AND
  ``process_release == released``. An active row is neither, so it is refused
  ``CAS_UNEXPECTED_STATE`` (surfacing as ``not_hibernated_bound_state``).
- ``retire_released_hibernated_legacy`` (#13841) and ``retire_reconciled_hibernated_legacy``
  (#13842) both require an **empty** ``worktree_identity`` — the legacy signature — and
  ``hibernated``.

**The release axis cannot be this surface's proof.** #13845 pairs a live-zero read with a
durable ``process_release == released`` record: two independent witnesses that the pair is
gone. An ACTIVE row has ``process_release == not_requested`` by construction — nothing ever
requested a release — so that second witness does not exist here. The live-inventory zero read
is therefore the ONLY liveness authority, which *raises* the bar on the caller: it must prove
positive absence across the expected slots AND the foreign slots AND locator-less rows AND
duplicates before calling this CAS, and must re-read at action time. This store states the
durable half of the signature; it cannot and does not verify liveness.

Like its three siblings this composes a :class:`LaneLifecycleStore` for the container guard and
drives its own CAS on the shared row. It deliberately does **not** widen
:meth:`...lane_bound_retire.LaneBoundRetireStore.retire_released_hibernated_bound` to also
accept ``active``: that guard is a safety contract reviewed against #13845's own evidence, and
relaxing its disposition predicate would let an active row terminalize on the *hibernated*
surface's release proof — which, per the paragraph above, an active row can never actually
supply. Each surface states its full signature literally and refuses everything else zero-write
(``managed-state-model.md`` "row-shape CAS は各 surface が literal に自 signature を述べる").
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
    DISPOSITION_ACTIVE,
    DISPOSITION_RETIRED,
    RELEASE_NOT_REQUESTED,
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

#: The release states an ACTIVE live-zero row may hold. ``not_requested`` is the ordinary shape
#: (nothing ever asked for a release); ``released`` is admitted because a completed release that
#: never advanced the disposition leaves the same terminalizable row. ``requested`` / ``partial``
#: are deliberately absent: an actuator may be closing panes right now, so the caller's live-zero
#: read could be observing a half-finished release rather than a settled absence.
_ADMISSIBLE_RELEASE_STATES = frozenset({RELEASE_NOT_REQUESTED, RELEASE_RELEASED})


class LaneActiveRetireStore:
    """Bounded ``active -> retired`` CAS for a live-zero BOUND lane (Redmine #14242)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    @property
    def last_write_preparation(self):
        """The last mutation's explicit-write-gate result (Redmine #13844 R3-F2).

        Delegates to the wrapped lifecycle store so the retire command can surface the
        pre-migration preflight + post-migration outcome (peer-reader risk) in its typed
        outcome, exactly as the #13841 / #13842 / #13845 siblings do.
        """
        return self._lifecycle.last_write_preparation

    def retire_active_live_zero(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        issue_id: str,
        worktree_identity: str,
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Terminalize an ACTIVE bound row whose pair is proven gone, or fail closed (#14242).

        Writes the single ``active -> retired`` disposition edge ONLY when every part of the
        exact active-bound signature holds — otherwise zero-write:

        - the row exists (:data:`CAS_NOT_FOUND`) and its ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION`). The revision is the race fence: the caller measured the
          live-zero inventory against THIS revision, so any concurrent declare / transition /
          generation open that moved the row invalidates that measurement and loses here rather
          than clobbering the newer state;
        - it is ``active`` — a ``hibernated`` row belongs to #13845 / #13841 / #13842, and a
          ``superseded`` / already ``retired`` row is not this surface's target — is an
          ``issue`` binding, owns **this exact** issue, and owns no project scope
          (:data:`CAS_UNEXPECTED_STATE`);
        - its ``worktree_identity`` is **non-empty AND equal to** the caller's attested token.
          An empty binding is the #13841 legacy signature and terminalizes there, never here; a
          non-empty mismatch means the caller's ``--worktree`` belongs to a different lane, so
          it is refused rather than coerced. Re-checked HERE under the row lock: the command's
          action-time attestation is a diagnostic, this is the authority;
        - its process release is ``not_requested`` or ``released``
          (:data:`_ADMISSIBLE_RELEASE_STATES`) — never ``requested`` / ``partial``, which mean a
          release is in flight and the caller's zero read may be mid-actuation
          (:data:`CAS_FORBIDDEN_TRANSITION`); and no receiver replacement is in flight
          (:func:`replacement_settled`, same reason);
        - the ``decision`` anchor names this issue.

        **Liveness is NOT verified here and cannot be.** Unlike #13845 there is no durable
        release proof to pair with — an active row never requested one. The caller must
        establish positive absence from a fresh inventory read (expected slots, foreign slots,
        locator-less rows, duplicates) and pass the revision it measured against, so this CAS's
        stale-revision fence converts a concurrent relaunch into a refusal. A caller that skips
        that read can terminalize a live lane; that contract is stated on the calling surface.

        Deliberately NOT :meth:`LaneLifecycleStore.transition_disposition`: that generic edge
        accepts any ``active -> retired`` regardless of binding / release / worktree shape.

        The disposition is the only field written (plus the decision anchor + revision). The
        row's ``worktree_identity``, ``declared_slots`` pins, ``lane_generation``,
        ``process_release``, ``replacement_*`` and ``reconcile_phase`` are all **preserved**, so
        a terminalized row keeps its forensic binding and stays distinguishable from a #13842
        reconcile-owed close. A duplicate replay is handled by the caller reading the
        already-``retired`` row (an idempotent success re-verified against a live-zero read), so
        this CAS stays strictly ``active -> retired``.
        """
        issue = norm(issue_id)
        if not issue:
            raise ValueError(
                "an active live-zero retire requires the exact issue the row must already own"
            )
        want_worktree = norm(worktree_identity)
        if not want_worktree:
            raise ValueError(
                "an active live-zero retire requires the canonical worktree token the row's "
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
                # The caller's live-zero measurement was taken against a different revision.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False, reason=CAS_STALE_REVISION, revision=current.revision
                )
            if (
                current.lane_disposition != DISPOSITION_ACTIVE
                or norm(current.binding_kind) != BINDING_KIND_ISSUE
                or current.issue_id != issue
                or current.project_scope
                or norm(current.worktree_identity) != want_worktree
            ):
                # Not the exact active-bound signature: a hibernated row (the #13845 / #13841 /
                # #13842 target, never this one's), a superseded / already retired row, a
                # project-gateway binding, a different issue, an EMPTY worktree binding, or a
                # binding naming a DIFFERENT worktree. Refused zero-write, never coerced.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if current.process_release not in _ADMISSIBLE_RELEASE_STATES:
                # requested / partial: a release actuator may be closing panes right now, so the
                # caller's zero read cannot be trusted as a settled absence.
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
                # active -> retired is a legal edge; this is the backstop, never reached under
                # the disposition guard above.
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
                f"active live-zero retire failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneActiveRetireStore",)
