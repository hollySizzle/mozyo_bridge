"""`sublane hibernate` durable-record invariants (the "lane is idle & parked" gate).

The operator-asserted value object for :mod:`sublane_hibernate`, split out to keep the use
case module under the module-health ceiling (Redmine #13682 / #13967 / #13811). This is a
pure domain value: :class:`HibernateAssertions` carries the durable-record facts no live
probe can infer (each defaulting to the safe-failing value) and derives the park-basis /
obligation / preservation gates from them. No IO, no lifecycle store — the use case reads
these verdicts and layers the identity / inventory / project-gateway fences on top.
"""

from __future__ import annotations

from dataclasses import dataclass

# Park-basis vocabulary (Redmine #13967 item 1): the two affirmative preconditions that
# justify winding a lane's processes down. `dependency` is the original #13682 park (issue
# explicitly parked/blocked on a wait). `early_hibernate` is the standardized new basis: a
# same-lane review-approved + staging-integrated + required-CI-green feature lane whose
# TestPyPI / installed dogfood execution/evidence is delegated to the dedicated release
# issue via a durable park/delegation record (close authority + owner close approval stay
# with the coordinator's normal path, NOT delegated), so the lane hibernates without
# waiting for ticket close or installed dogfood. `none` = no affirmative basis (fail-closed).
PARK_BASIS_DEPENDENCY = "dependency"
PARK_BASIS_EARLY_HIBERNATE = "early_hibernate"
PARK_BASIS_NONE = "none"


@dataclass(frozen=True)
class HibernateAssertions:
    """Durable-record facts no live probe can infer, asserted from the Redmine record.

    Every flag defaults to the unsatisfied (safe-failing) value, so a caller that omits
    one fails closed. The lane must have an affirmative park basis (dependency park OR
    early hibernate — see :attr:`park_basis`) with no outstanding coordinator obligation
    before its processes are released:

    - :attr:`explicitly_parked` — the issue is open AND explicitly parked/blocked (the
      *dependency* park basis; hibernate is never an idle-timeout kill, j#77485).
    - :attr:`callbacks_drained` / :attr:`no_review_pending` / :attr:`no_integration_pending`
      — no coordinator callback, review, or integration is due on this lane (required by
      every basis).
    - :attr:`no_owner_approval_pending` — no owner close approval is due. This is
      **basis-dependent** (see :attr:`obligations_satisfied`): required by a dependency
      park, but NOT by an early hibernate (whose close authority + owner approval stay with
      the coordinator's normal path, so it runs in the ``owner_waiting`` state).
    - :attr:`no_pending_prompt` — no composer input is pending.
    - :attr:`not_working` — no work is in flight (no running turn to interrupt).
    - :attr:`worktree_clean` / :attr:`boundary_recorded` — a dirty worktree does not block
      (it is preserved), but its uncommitted diff / resume next-action must first be
      captured in a boundary journal. One of the two must hold (Design Answer Q2).
    """

    explicitly_parked: bool = False
    callbacks_drained: bool = False
    no_review_pending: bool = False
    no_owner_approval_pending: bool = False
    no_integration_pending: bool = False
    no_pending_prompt: bool = False
    not_working: bool = False
    worktree_clean: bool = False
    boundary_recorded: bool = False
    # Early-hibernate park basis (Redmine #13967 item 1). The alternative affirmative
    # precondition to `explicitly_parked`: a same-lane review-approved + staging-integrated
    # + required-CI-green feature lane whose dogfood execution/evidence is delegated to the
    # dedicated release issue (close authority stays with the coordinator). Every flag
    # defaults False (fail-closed). The generic safety gates
    # above (callbacks_drained / no_review_pending / no_integration_pending / lane_idle /
    # boundary) are NOT weakened by this basis — in the early-hibernate case they are all
    # satisfied (review approved => no review owed; integrated => no integration pending).
    review_approved: bool = False
    staging_integrated: bool = False
    required_ci_green: bool = False
    dogfood_delegated: bool = False
    commits_pushed: bool = False

    @property
    def no_outstanding_obligation(self) -> bool:
        return (
            self.callbacks_drained
            and self.no_review_pending
            and self.no_owner_approval_pending
            and self.no_integration_pending
        )

    @property
    def obligations_satisfied(self) -> bool:
        """The obligation gate for the ASSERTED park basis (Redmine #13967 F1).

        The common obligations (no callback / review / integration owed) apply to every
        basis. Owner close approval is basis-dependent: the **dependency park** keeps the
        original #13682 requirement (`no_owner_approval_pending`), but **early hibernate**
        must NOT require it — hibernate is not close, the source issue stays open, and the
        owner close approval is collected later on the coordinator's normal path. The
        anchor's fail-closed list for early hibernate deliberately omits owner approval
        (Implementation Request j#81283 item 1), so requiring it here made early hibernate
        undischargeable in its target `owner_waiting` state. Fails closed: no affirmative
        basis -> False.
        """
        base = self.callbacks_drained and self.no_review_pending and self.no_integration_pending
        # Prefer the early-hibernate basis when it fully qualifies (Redmine #13967 R2-F4):
        # if a lane genuinely meets every early-hibernate precondition, dropping the owner
        # gate is correct even when `explicitly_parked` is ALSO set — otherwise an ambiguous
        # input silently falls back to the stricter dependency basis and re-blocks the
        # owner_waiting state early hibernate exists to serve. `park_basis` uses the same
        # ordering so the reported basis and the gate agree.
        if self.early_hibernate_qualified:
            return base
        if self.explicitly_parked:
            return base and self.no_owner_approval_pending
        return False

    @property
    def owner_gate_applies(self) -> bool:
        """True when a pending owner close approval should block this hibernate.

        Only the dependency-park basis gates on owner approval. A lane that fully qualifies
        for early hibernate (even if `explicitly_parked` is also set) does not — its owner
        approval is deferred to the coordinator's normal close path (Redmine #13967 R2-F4:
        early qualification wins over dependency).
        """
        return not self.early_hibernate_qualified and not self.early_hibernate_attempted

    @property
    def lane_idle(self) -> bool:
        return self.no_pending_prompt and self.not_working

    @property
    def boundary_ok(self) -> bool:
        """A clean worktree, or a recorded boundary journal for a dirty one."""
        return self.worktree_clean or self.boundary_recorded

    @property
    def early_hibernate_qualified(self) -> bool:
        """The early-hibernate basis holds (Redmine #13967 item 1).

        Every early-hibernate precondition is affirmed: the same-lane Review Gate is
        approved, the coordinator staging integration is recorded, required CI is green,
        the TestPyPI / installed dogfood execution/evidence is delegated to the dedicated
        release issue (a durable park/delegation record; close authority + owner close
        approval stay with the coordinator, NOT delegated), and the commits are pushed /
        origin-reachable (unpushed fails closed — an early hibernate presupposes the work
        is integrated, unlike a dependency park which preserves unpublished commits).
        """
        return (
            self.review_approved
            and self.staging_integrated
            and self.required_ci_green
            and self.dogfood_delegated
            and self.commits_pushed
        )

    @property
    def early_hibernate_attempted(self) -> bool:
        """True when an early-hibernate basis is being asserted (any early flag set) without
        a dependency park. Used to surface the specific unpushed fence rather than a generic
        ``not parked`` when the operator intends an early hibernate but has not pushed."""
        return (not self.explicitly_parked) and (
            self.review_approved
            or self.staging_integrated
            or self.required_ci_green
            or self.dogfood_delegated
        )

    @property
    def park_satisfied(self) -> bool:
        """An affirmative park basis holds: a dependency park OR an early hibernate."""
        return self.explicitly_parked or self.early_hibernate_qualified

    @property
    def park_basis(self) -> str:
        # Early qualification wins over dependency when both hold (Redmine #13967 R2-F4),
        # matching `obligations_satisfied` so the reported basis and the owner gate agree.
        if self.early_hibernate_qualified:
            return PARK_BASIS_EARLY_HIBERNATE
        if self.explicitly_parked:
            return PARK_BASIS_DEPENDENCY
        return PARK_BASIS_NONE

    @property
    def preservation_satisfied(self) -> bool:
        """Every work-preservation gate holds (identity aside).

        The gate both the initial hibernate and the already-hibernated release re-drive
        share (R1-F2): a lane whose processes are (re-)released must have an affirmative
        park basis (dependency park OR early hibernate), owe no coordinator obligation, be
        idle (no work in flight / no pending composer), and have its dirty diff captured. A
        partial-release retry re-checks this against the lane's *current* state — a lane
        that has since started working is never closed.
        """
        return (
            self.park_satisfied
            and self.obligations_satisfied
            and self.lane_idle
            and self.boundary_ok
        )


__all__ = (
    "PARK_BASIS_DEPENDENCY",
    "PARK_BASIS_EARLY_HIBERNATE",
    "PARK_BASIS_NONE",
    "HibernateAssertions",
)
