"""Active UNBOUND live-zero terminal retire CAS (Redmine #14499).

The fifth bounded terminalizer, for the one lane shape the other four still refuse: an
**ACTIVE, issue-bound row whose ``worktree_identity`` is EMPTY** and whose managed pair is
already positively gone.

Live evidence #14456 j#87973, measured after that issue was closed: ``issue 14456``,
``disposition active``, ``process_release not_requested``, ``binding_kind issue``,
``generation 1``, ``revision 1``, ``worktree identity empty``, zero managed panes live. Every
existing rail declined it, each correctly:

- ``retire --execute`` (#13754) returned ``blocked / worktree_binding_unverified`` — it
  attests the caller's ``--worktree`` against the row's recorded binding, and a pre-#13754
  row has none to attest;
- ``--retire-active-live-zero`` (#14242) requires a **non-empty** binding equal to the
  attested token (its ``ValueError`` on an empty token is explicit: "an empty token is the
  #13841 legacy signature, not this surface's");
- ``--retire-hibernated-bound`` (#13845) requires ``hibernated`` + ``released`` + a
  non-empty binding;
- ``--migrate-hibernated-legacy`` (#13841) / ``--reconcile-hibernated-live`` (#13842) do
  take an empty binding, but only on a ``hibernated`` row.

**What replaces the worktree attestation.** The four bound surfaces fence identity on the
recorded worktree token: it proves the caller is aiming at *this* lane and not a sibling.
An unbound row cannot supply that, so this surface substitutes a **caller-declared
generation + revision fence**. The coordinator reads the row (through the public read-only
audit), sees generation *g* and revision *r*, and must pass both back; the CAS applies only
if the row is still exactly at ``(g, r)``. That is strictly narrower than #14242's revision
fence alone: a lane whose generation was re-opened between the read and the write — the
exact way a lane legitimately comes back to life — loses, rather than being terminalized on
a stale reading. It does not, and cannot, prove *which worktree* the lane ran in; nothing
durable records that for this row, so the surface asks for the two facts that ARE durable
instead of accepting a guess.

**Liveness is not verified here**, exactly as in #14242 and for the same reason: an active
row has no ``process_release`` witness to pair with, so the caller must establish positive
absence from a fresh inventory read AND hold the home's attestation-store lock EXCLUSIVE
across both the read and this call (the #13882 boundary-3 launch exclusion). A caller that
skips either can terminalize a live lane.

The row's ``lane_generation``, ``declared_slots``, ``process_release``, ``replacement_*``,
``reconcile_phase`` and (empty) ``worktree_identity`` are all preserved; the disposition,
decision anchor and revision are the only fields written.
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
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleKey,
    disposition_transition_allowed,
    norm,
    replacement_settled,
    stored_binding_kind_is,
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

#: The ONLY release state an ACTIVE row may legitimately hold, for the same reason
#: :mod:`...lane_active_retire` documents: ``request_release`` refuses an ``active`` row and
#: ``record_release_outcome`` refuses to attribute an outcome to it, so ``requested`` /
#: ``partial`` / ``released`` are all unreachable through the public transitions. A row
#: carrying one is a corrupted shape, not a residue, and is refused rather than terminalized.
_ADMISSIBLE_RELEASE_STATES = frozenset({RELEASE_NOT_REQUESTED})


class LaneActiveUnboundRetireStore:
    """Bounded ``active -> retired`` CAS for a live-zero UNBOUND lane (Redmine #14499)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    @property
    def last_write_preparation(self):
        """The last mutation's explicit-write-gate result (Redmine #13844 R3-F2).

        Delegates to the wrapped lifecycle store so the retire command can surface the
        pre-migration preflight + post-migration outcome exactly as its four siblings do.
        """
        return self._lifecycle.last_write_preparation

    def retire_active_unbound_live_zero(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        expected_generation: int,
        issue_id: str,
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Terminalize an ACTIVE UNBOUND row whose pair is proven gone, or fail closed.

        Writes the single ``active -> retired`` disposition edge ONLY when every part of the
        exact active-unbound signature holds — otherwise zero-write:

        - the row exists (:data:`CAS_NOT_FOUND`);
        - its ``revision`` still equals ``expected_revision`` **and** its ``lane_generation``
          still equals ``expected_generation`` (:data:`CAS_STALE_REVISION`). Both are
          required because they fail differently: the revision catches any concurrent
          lifecycle write, while the generation catches the specific case this surface has
          no worktree token to guard against — the lane being re-incarnated between the
          caller's read and this write. A generation mismatch at an unchanged revision is
          not reachable through the public transitions, and a revision mismatch alone would
          already refuse; checking both means the caller must have read THIS incarnation,
          not merely a row at this address;
        - it is ``active`` (a ``hibernated`` row is #13841 / #13842 / #13845's target, and a
          ``superseded`` / already ``retired`` row is nobody's), is an ``issue`` binding,
          owns **this exact** issue, and owns no project scope
          (:data:`CAS_UNEXPECTED_STATE`);
        - its ``worktree_identity`` is **EMPTY**. A bound row terminalizes on #14242's
          surface, where its recorded binding can be attested against the caller's
          ``--worktree``; accepting one here would let a caller skip that attestation and
          terminalize a lane it never proved it was aiming at;
        - its process release is exactly ``not_requested`` and no receiver replacement is in
          flight (:data:`CAS_FORBIDDEN_TRANSITION`);
        - the ``decision`` anchor names this issue.

        **Liveness is NOT verified here and cannot be** — see the module docstring. The
        caller must establish positive absence from a fresh inventory read and hold the
        home's attestation-store lock EXCLUSIVE across both that read and this call.

        Deliberately not a widening of
        :meth:`...lane_active_retire.LaneActiveRetireStore.retire_active_live_zero`: that
        guard requires a non-empty binding *equal to an attested token*, which is the whole
        of its identity proof. Relaxing it to also accept an empty one would silently drop
        that proof for every caller, including the bound ones. Each surface states its full
        signature literally and refuses everything else zero-write.
        """
        issue = norm(issue_id)
        if not issue:
            raise ValueError(
                "an active unbound live-zero retire requires the exact issue the row must "
                "already own"
            )
        if expected_generation < 1:
            raise ValueError(
                "an active unbound live-zero retire requires the positive lane generation "
                "the caller measured the live-zero read against; the generation fence "
                "stands in for the worktree attestation this row cannot supply"
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
            if (
                current.revision != expected_revision
                or current.lane_generation != expected_generation
            ):
                # The caller's live-zero measurement was taken against a different
                # incarnation or a different revision of this row.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False, reason=CAS_STALE_REVISION, revision=current.revision
                )
            if (
                current.lane_disposition != DISPOSITION_ACTIVE
                or not stored_binding_kind_is(current.binding_kind, BINDING_KIND_ISSUE)
                or current.issue_id != issue
                or current.project_scope
                or norm(current.worktree_identity)
            ):
                # Not the exact active-UNBOUND signature: a hibernated / superseded /
                # already-retired row, a project-gateway binding, a different issue, or a
                # row that DOES record a canonical worktree binding (which belongs to the
                # #14242 bound surface, where it can be attested). Refused zero-write.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if current.process_release not in _ADMISSIBLE_RELEASE_STATES:
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
                # active -> retired is a legal edge; the backstop, never reached above.
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
                f"active unbound live-zero retire failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


__all__ = ("LaneActiveUnboundRetireStore",)
