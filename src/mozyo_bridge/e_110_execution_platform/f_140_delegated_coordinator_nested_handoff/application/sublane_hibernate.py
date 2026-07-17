"""`mozyo-bridge sublane hibernate` — release an open lane's processes (Redmine #13682).

A lane whose issue is still **open** but parked on a long dependency wait keeps its
gateway/worker pair resident, consuming lane capacity and cockpit legibility for nothing
(the measured case: #13441, dependency-parked with two idle panes). Hibernate is the
explicit disposition that winds those processes down *without* touching the work: the
issue stays open, the worktree / branch / unpublished commits / lane metadata / durable
callback route are all preserved, and only the managed panes close (Design Answer j#76629
``accepted_with_correction``, Implementation Request j#77485).

It is deliberately **not** retire (``issue closed`` is retire's precondition; hibernate's
is ``issue open``) and deliberately not an idle-timeout kill (a bare idle pane is not a
hibernate signal — ``workflow.md`` stall discipline). Layered on the #13689 lifecycle
substrate, exactly mirroring ``sublane supersede`` minus the ownership handover:

1. **preflight (fail-closed)** — the lane's identity is known and *actively* owns the
   issue, an **affirmative park basis** holds, and every durable idle gate the operator
   asserts from the Redmine record holds: no coordinator callback is owed, no review /
   owner-approval / integration is pending, no composer input is pending, and no work is
   in flight. There are two park bases (Redmine #13967 item 1): the original **dependency
   park** (the issue is explicitly parked/blocked on a wait) and the standardized **early
   hibernate** (a same-lane review-approved + staging-integrated + required-CI-green
   feature lane whose TestPyPI / installed dogfood + close are delegated to the dedicated
   release issue — so the lane hibernates without waiting for ticket close or installed
   dogfood; unpushed commits fail closed here, since an early hibernate presupposes
   integrated work). A dirty worktree does **not** block (hibernate preserves the
   worktree) but its uncommitted diff / resume next-action must be captured in a boundary
   journal first — asserted via ``worktree_clean`` OR ``boundary_recorded`` (Design Answer
   Q2). Any unmet gate blocks with a reason and mutates nothing; each flag defaults to the
   unsatisfied (safe-failing) value.
2. **commit point** — :meth:`LaneLifecycleStore.transition_disposition` CAS-moves the lane
   ``active -> hibernated``. After this the lane draws zero active capacity (W4 roster
   join) and an explicit send to it is a zero-send (W3 gate, ``lane_hibernated``).
3. **process release (tombstone-free)** — the shared :func:`drive_process_release`
   opens a release generation pinning the lane's live slots and closes its managed panes
   through the existing :func:`execute_herdr_retire_close` primitive (never a worktree
   remove, branch delete, or metadata tombstone). A partial close is re-drivable.

Boundary (j#77485): this does not close the issue, remove a worktree, delete a branch,
merge, publish, or auto-kill on idle. Default is preflight only; ``--execute`` performs
the hibernate. Resume back to ``active`` is the sibling ``sublane resume`` (never here).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
    execute_herdr_retire_close,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    ReleaseOutcome,
    drive_process_release,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

# Blocked-reason vocabulary (fail-closed preflight).
BLOCK_ORIGINAL_IDENTITY = "original_identity_unknown"
BLOCK_NOT_PARKED = "issue_not_explicitly_parked"
BLOCK_CALLBACK_DEBT = "callback_debt_outstanding"
BLOCK_REVIEW_PENDING = "review_pending"
BLOCK_OWNER_PENDING = "owner_approval_pending"
BLOCK_INTEGRATION_PENDING = "integration_pending"
BLOCK_PENDING_PROMPT = "pending_composer_input"
BLOCK_WORKING = "work_in_flight"
BLOCK_UNRECORDED_BOUNDARY = "dirty_worktree_without_boundary_journal"
BLOCK_INVENTORY_UNREADABLE = "inventory_unreadable"
# Early-hibernate (Redmine #13967 item 1) unpushed fence: an early-hibernate basis
# presupposes the commits are integrated to staging, so an early-hibernate attempt whose
# commits are not pushed / origin-reachable fails closed (unlike a dependency park, which
# deliberately preserves unpublished commits).
BLOCK_UNPUSHED_COMMITS = "unpushed_commits"

# Park-basis vocabulary (Redmine #13967 item 1): the two affirmative preconditions that
# justify winding a lane's processes down. `dependency` is the original #13682 park (issue
# explicitly parked/blocked on a wait). `early_hibernate` is the standardized new basis: a
# same-lane review-approved + staging-integrated + required-CI-green feature lane whose
# TestPyPI / installed dogfood + close are delegated to the dedicated release issue via a
# durable park/delegation record (so the lane hibernates without waiting for ticket close
# or installed dogfood). `none` = no affirmative basis (fail-closed).
PARK_BASIS_DEPENDENCY = "dependency"
PARK_BASIS_EARLY_HIBERNATE = "early_hibernate"
PARK_BASIS_NONE = "none"


# ---------------------------------------------------------------------------
# Operator-asserted durable-record invariants (the "lane is idle & parked" gate).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HibernateAssertions:
    """Durable-record facts no live probe can infer, asserted from the Redmine record.

    Every flag defaults to the unsatisfied (safe-failing) value, so a caller that omits
    one fails closed. The lane must have an affirmative park basis (dependency park OR
    early hibernate — see :attr:`park_basis`) with no outstanding coordinator obligation
    before its processes are released:

    - :attr:`explicitly_parked` — the issue is open AND explicitly parked/blocked (the
      *dependency* park basis; hibernate is never an idle-timeout kill, j#77485).
    - :attr:`callbacks_drained` / :attr:`no_review_pending` /
      :attr:`no_owner_approval_pending` / :attr:`no_integration_pending` — no coordinator
      callback, review, owner approval, or integration is due on this lane.
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
    # + required-CI-green feature lane whose dogfood/close is delegated to the dedicated
    # release issue. Every flag defaults False (fail-closed). The generic safety gates
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
        if self.explicitly_parked:
            return base and self.no_owner_approval_pending
        if self.early_hibernate_qualified:
            return base
        return False

    @property
    def owner_gate_applies(self) -> bool:
        """True when a pending owner close approval should block this hibernate.

        Only the dependency-park basis gates on owner approval. An early-hibernate
        attempt (or a no-basis request) does not — its owner approval is deferred to the
        coordinator's normal close path.
        """
        return not self.early_hibernate_attempted

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
        the TestPyPI / installed dogfood + close are delegated to the dedicated release
        issue (a durable park/delegation record), and the commits are pushed /
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
        if self.explicitly_parked:
            return PARK_BASIS_DEPENDENCY
        if self.early_hibernate_qualified:
            return PARK_BASIS_EARLY_HIBERNATE
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


# ---------------------------------------------------------------------------
# Pure preflight decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HibernatePreflight:
    """The fail-closed inputs + verdict of a hibernate preflight (pure)."""

    original_identity_known: bool
    park_satisfied: bool
    obligations_satisfied: bool
    lane_idle: bool
    boundary_ok: bool
    inventory_readable: bool = True
    assertions: HibernateAssertions = field(default_factory=HibernateAssertions)

    @property
    def may_hibernate(self) -> bool:
        return (
            self.original_identity_known
            and self.park_satisfied
            and self.obligations_satisfied
            and self.lane_idle
            and self.boundary_ok
            and self.inventory_readable
        )

    @property
    def park_basis(self) -> str:
        return self.assertions.park_basis

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.original_identity_known:
            reasons.append(BLOCK_ORIGINAL_IDENTITY)
        if not self.park_satisfied:
            reasons.append(BLOCK_NOT_PARKED)
        # An intended early hibernate (Redmine #13967 item 1) whose commits are not pushed
        # fails closed on the specific unpushed fence — surfaced even when the generic
        # `not parked` already fired, so the operator sees the concrete unmet gate.
        if self.assertions.early_hibernate_attempted and not self.assertions.commits_pushed:
            reasons.append(BLOCK_UNPUSHED_COMMITS)
        # Each obligation names itself so the operator sees exactly which gate is unmet.
        if not self.assertions.callbacks_drained:
            reasons.append(BLOCK_CALLBACK_DEBT)
        if not self.assertions.no_review_pending:
            reasons.append(BLOCK_REVIEW_PENDING)
        # Owner close approval only gates the dependency-park basis (Redmine #13967 F1):
        # an early hibernate is expected to run while owner approval is still outstanding
        # (deferred to the coordinator's normal close path), so it is not a blocker there.
        if self.assertions.owner_gate_applies and not self.assertions.no_owner_approval_pending:
            reasons.append(BLOCK_OWNER_PENDING)
        if not self.assertions.no_integration_pending:
            reasons.append(BLOCK_INTEGRATION_PENDING)
        if not self.assertions.no_pending_prompt:
            reasons.append(BLOCK_PENDING_PROMPT)
        if not self.assertions.not_working:
            reasons.append(BLOCK_WORKING)
        if not self.boundary_ok:
            reasons.append(BLOCK_UNRECORDED_BOUNDARY)
        if not self.inventory_readable:
            reasons.append(BLOCK_INVENTORY_UNREADABLE)
        return tuple(reasons)

    def as_payload(self) -> dict[str, Any]:
        return {
            "may_hibernate": self.may_hibernate,
            "original_identity_known": self.original_identity_known,
            "park_satisfied": self.park_satisfied,
            "park_basis": self.park_basis,
            "obligations_satisfied": self.obligations_satisfied,
            "lane_idle": self.lane_idle,
            "boundary_ok": self.boundary_ok,
            "inventory_readable": self.inventory_readable,
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class HibernateOutcome:
    """The full result: preflight verdict, the disposition commit, and the release."""

    executed: bool
    preflight: HibernatePreflight
    issue: str
    lane: str
    already_hibernated: bool = False
    redrive_blocked: bool = False
    transition: Optional[CasOutcome] = None
    release: Optional[ReleaseOutcome] = None
    detail: str = ""

    @property
    def is_blocked(self) -> bool:
        if self.already_hibernated:
            # A re-drive on an already-hibernated lane still fails closed when its
            # current preservation gate is unmet or the inventory is unreadable (R1-F2):
            # never a silent zero-close reported as success.
            return self.redrive_blocked
        if not self.preflight.may_hibernate:
            return True
        # A commit that was attempted but not applied (a lost CAS race) is a block.
        if self.executed and self.transition is not None and not self.transition.applied:
            return True
        return False

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "issue": self.issue,
            "lane": self.lane,
            "already_hibernated": self.already_hibernated,
            "redrive_blocked": self.redrive_blocked,
            "is_blocked": self.is_blocked,
            "preflight": self.preflight.as_payload(),
            "transition": (
                {"applied": self.transition.applied, "reason": self.transition.reason,
                 "revision": self.transition.revision}
                if self.transition is not None
                else None
            ),
            "release": self.release.as_payload() if self.release is not None else None,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Injected IO port + live adapter.
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneHibernateOps(Protocol):
    """Every side effect the hibernate use case needs, injected so tests drive fakes."""

    def workspace_id(self) -> str: ...

    def read_inventory(self) -> tuple[Sequence[Mapping[str, object]], bool]: ...

    def execute_close(self, plan: HerdrRetireClosePlan) -> HerdrRetireCloseResult: ...


@dataclass
class LiveSublaneHibernateOps:
    """Live adapter: project workspace segment + live herdr inventory + guarded close."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS

    def workspace_id(self) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            herdr_workspace_segment,
        )

        try:
            return herdr_workspace_segment(self.repo_root)
        except (OSError, ValueError):
            return ""

    def read_inventory(self) -> tuple[Sequence[Mapping[str, object]], bool]:
        """``(rows, readable)`` — an **unreadable** inventory is not folded to empty (R1-F1).

        A live-inventory read that could not run (``list_herdr_agent_rows`` raised) returns
        ``((), False)`` — the caller must fail closed rather than mistake "could not verify
        the panes" for "the panes are gone". A successful read (even genuinely empty)
        returns ``(rows, True)``.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        try:
            return list(list_herdr_agent_rows(self.env)), True
        except Exception:  # noqa: BLE001 — inventory unreadable -> fail closed (NOT empty)
            return (), False

    def execute_close(self, plan: HerdrRetireClosePlan) -> HerdrRetireCloseResult:
        return execute_herdr_retire_close(
            plan, env=self.env, runner=self.runner, timeout=self.timeout
        )


# ---------------------------------------------------------------------------
# Use case.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HibernateRequest:
    issue: str
    lane: str
    journal: str
    assertions: HibernateAssertions


@dataclass
class SublaneHibernateUseCase:
    """Preflight + disposition CAS (active -> hibernated) + tombstone-free release."""

    ops: SublaneHibernateOps
    store: LaneLifecycleStore

    def _decision(self, request: HibernateRequest) -> Optional[DecisionPointer]:
        try:
            return DecisionPointer(
                source="redmine",
                issue_id=_norm(request.issue),
                journal_id=_norm(request.journal),
            )
        except DecisionPointerError:
            return None

    def run(self, request: HibernateRequest, *, execute: bool) -> HibernateOutcome:
        issue = _norm(request.issue)
        lane = _norm(request.lane)
        workspace_id = _norm(self.ops.workspace_id())

        # A malformed identity / anchor can address nothing — fail closed before any read.
        decision = self._decision(request)
        if not issue or not lane or not workspace_id or decision is None:
            preflight = HibernatePreflight(
                original_identity_known=False,
                park_satisfied=request.assertions.park_satisfied,
                obligations_satisfied=request.assertions.obligations_satisfied,
                lane_idle=request.assertions.lane_idle,
                boundary_ok=request.assertions.boundary_ok,
                assertions=request.assertions,
            )
            return HibernateOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                detail="incomplete hibernate identity or decision anchor",
            )

        key = LaneLifecycleKey(workspace_id, lane)

        try:
            rec = self.store.get(key)
        except (LaneLifecycleError, OSError):
            preflight = HibernatePreflight(
                original_identity_known=False,
                park_satisfied=request.assertions.park_satisfied,
                obligations_satisfied=request.assertions.obligations_satisfied,
                lane_idle=request.assertions.lane_idle,
                boundary_ok=request.assertions.boundary_ok,
                assertions=request.assertions,
            )
            return HibernateOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                detail="lifecycle store unreadable; fail closed",
            )

        # Read the live inventory ONCE, keeping readability explicit (R1-F1). An
        # unreadable inventory is never folded to "empty"; the same snapshot is reused for
        # the release close so nothing is re-read between the gate and the actuation.
        rows, inventory_readable = self.ops.read_inventory()

        # Idempotent resume: the lane is already hibernated. Skip the commit (its CAS
        # guard would refuse anyway) and re-drive the release, which is itself idempotent
        # (a pane close is, unlike a send). But the re-drive re-checks the lane's CURRENT
        # preservation gate and the inventory readability (R1-F2): a partial close from a
        # prior run finishes only while the lane is still parked / idle / boundary-recorded;
        # a lane that has since started working, gained a pending prompt, or owes a callback
        # is NEVER closed by a stale retry, and an unreadable inventory blocks the re-drive.
        already_hibernated = (
            rec is not None
            and rec.lane_disposition == DISPOSITION_HIBERNATED
            and rec.issue_id == issue
        )
        if already_hibernated:
            redrive_ok = (
                inventory_readable and request.assertions.preservation_satisfied
            )
            release = None
            if execute and redrive_ok:
                release = self._drive_release(key, lane, workspace_id, rows)
            preflight = HibernatePreflight(
                original_identity_known=True,  # the hibernated lane is known
                park_satisfied=request.assertions.park_satisfied,
                obligations_satisfied=request.assertions.obligations_satisfied,
                lane_idle=request.assertions.lane_idle,
                boundary_ok=request.assertions.boundary_ok,
                inventory_readable=inventory_readable,
                assertions=request.assertions,
            )
            return HibernateOutcome(
                executed=execute and redrive_ok,
                preflight=preflight,
                issue=issue,
                lane=lane,
                already_hibernated=True,
                redrive_blocked=execute and not redrive_ok,
                release=release,
                detail=(
                    "lane already hibernated; release re-drive blocked (preservation gate "
                    "unmet or inventory unreadable)"
                    if execute and not redrive_ok
                    else "lane already hibernated; resumed release"
                ),
            )

        original_identity_known = (
            rec is not None
            and rec.lane_disposition == DISPOSITION_ACTIVE
            and rec.issue_id == issue
        )
        preflight = HibernatePreflight(
            original_identity_known=original_identity_known,
            park_satisfied=request.assertions.park_satisfied,
            obligations_satisfied=request.assertions.obligations_satisfied,
            lane_idle=request.assertions.lane_idle,
            boundary_ok=request.assertions.boundary_ok,
            inventory_readable=inventory_readable,
            assertions=request.assertions,
        )
        if not preflight.may_hibernate or not execute:
            return HibernateOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                detail=(
                    "preflight only (no --execute)"
                    if preflight.may_hibernate
                    else "fail-closed: hibernate blocked"
                ),
            )

        # Commit point: CAS active -> hibernated, guarded on the lane's exact state +
        # revision and the durable decision anchor. Nothing is closed until this lands, and
        # may_hibernate already required a readable inventory — so the CAS is never reached
        # on an unverifiable one (zero-mutation on unreadable, R1-F1).
        assert rec is not None  # guaranteed by original_identity_known
        # Redmine #13844 R3: hibernate is a schema-needing mutation. Its write opens through the
        # universal `_connect_write` gate, which emits the PRE-migration peer-reader advisory to
        # stderr BEFORE the shared store is migrated (no per-command emit needed here).
        transition = self.store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=decision,
        )
        if not transition.applied:
            return HibernateOutcome(
                executed=True,
                preflight=preflight,
                issue=issue,
                lane=lane,
                transition=transition,
                detail=f"hibernate commit refused ({transition.reason})",
            )

        release = self._drive_release(key, lane, workspace_id, rows)
        return HibernateOutcome(
            executed=True,
            preflight=preflight,
            issue=issue,
            lane=lane,
            transition=transition,
            release=release,
            detail="lane hibernated; managed processes released",
        )

    def _drive_release(
        self,
        key: LaneLifecycleKey,
        lane: str,
        workspace_id: str,
        rows: Sequence[Mapping[str, object]],
    ) -> ReleaseOutcome:
        """Open (or resume) the release generation and close the lane's managed slots.

        Delegates to the shared tombstone-free driver: it never removes a worktree,
        deletes a branch, or writes a metadata tombstone, and a partial close leaves the
        generation open and re-drivable (Redmine #13682). ``rows`` is the readability-vetted
        inventory snapshot the caller already read (R1-F1), so an empty ``rows`` means a
        *confirmed*-empty inventory, never an unreadable one.
        """
        return drive_process_release(
            store=self.store,
            ops=self.ops,
            key=key,
            lane_id=lane,
            workspace_id=workspace_id,
            action_id=f"hibernate:{key.lane_id}",
            rows=rows,
        )


# ---------------------------------------------------------------------------
# Text rendering + thin CLI handler.
# ---------------------------------------------------------------------------


def format_hibernate_text(outcome: HibernateOutcome) -> str:
    lines = [
        f"sublane hibernate: {outcome.lane} (issue {outcome.issue})",
        f"  may_hibernate: {outcome.preflight.may_hibernate} executed: {outcome.executed}",
    ]
    if outcome.already_hibernated:
        lines.append("  lane already hibernated (idempotent resume)")
    if outcome.is_blocked:
        lines.append(
            "  -> fail-closed blocked: " + ", ".join(outcome.preflight.blocked_reasons)
        )
        if outcome.transition is not None and not outcome.transition.applied:
            lines.append(f"  commit refused: {outcome.transition.reason}")
        return "\n".join(lines)
    if outcome.transition is not None:
        lines.append(
            f"  commit: applied={outcome.transition.applied} "
            f"reason={outcome.transition.reason}"
        )
    if outcome.release is not None:
        rel = outcome.release
        lines.append(f"  release: {rel.process_release} ({rel.detail})")
        for role, locator in rel.closed:
            lines.append(f"    - closed {role} {locator}")
        for role, locator, detail in rel.failed:
            lines.append(f"    ! close failed {role} {locator}: {detail}")
    if not outcome.executed and outcome.preflight.may_hibernate:
        lines.append("  (preflight only; re-run with --execute to hibernate the lane)")
    return "\n".join(lines)


def cmd_sublane_hibernate(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = HibernateRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        assertions=HibernateAssertions(
            explicitly_parked=bool(getattr(args, "explicitly_parked", False)),
            callbacks_drained=bool(getattr(args, "callbacks_drained", False)),
            no_review_pending=bool(getattr(args, "no_review_pending", False)),
            no_owner_approval_pending=bool(
                getattr(args, "no_owner_approval_pending", False)
            ),
            no_integration_pending=bool(getattr(args, "no_integration_pending", False)),
            no_pending_prompt=bool(getattr(args, "no_pending_prompt", False)),
            not_working=bool(getattr(args, "not_working", False)),
            worktree_clean=bool(getattr(args, "worktree_clean", False)),
            boundary_recorded=bool(getattr(args, "boundary_recorded", False)),
            review_approved=bool(getattr(args, "review_approved", False)),
            staging_integrated=bool(getattr(args, "staging_integrated", False)),
            required_ci_green=bool(getattr(args, "required_ci_green", False)),
            dogfood_delegated=bool(getattr(args, "dogfood_delegated", False)),
            commits_pushed=bool(getattr(args, "commits_pushed", False)),
        ),
    )
    json_mode = bool(getattr(args, "json", False))
    ops = LiveSublaneHibernateOps(repo_root=repo_root, env=dict(os.environ))
    use_case = SublaneHibernateUseCase(ops=ops, store=LaneLifecycleStore())
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if json_mode:
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_hibernate_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_hibernate_parser(sublane_sub: Any) -> None:
    """Register ``sublane hibernate`` outside the at-ceiling core CLI module.

    Mirrors the sibling ``register_sublane_resume_parser`` placement (the core CLI module
    is at the module-health ceiling), keeping the hibernate parser next to its use case
    (Redmine #13967). Includes the early-hibernate park-basis flags (item 1).
    """
    from mozyo_bridge.application.cli_common import add_repo_option

    sublane_hibernate = sublane_sub.add_parser(
        "hibernate",
        help=(
            "Redmine #13682: release an OPEN lane's managed gateway/worker processes "
            "while preserving its worktree / branch / unpublished commits / lane metadata "
            "/ durable callback route (tombstone-free — never closes the issue, removes a "
            "worktree, or deletes a branch). Fail-closed preflight (lane actively owns the "
            "issue; an affirmative park basis — dependency park or early hibernate; no "
            "callback/review/owner/integration due; no pending composer; no work in "
            "flight; a dirty worktree needs a boundary journal). Not an idle-timeout kill. "
            "Default is preflight only; --execute performs the hibernate. Exits non-zero "
            "when blocked. Resume with `sublane resume`."
        ),
    )
    sublane_hibernate.add_argument(
        "--issue", required=True, help="Redmine issue id the lane owns (stays open)"
    )
    sublane_hibernate.add_argument(
        "--lane",
        required=True,
        help="Lane label to hibernate (e.g. issue_<id>_<slug>)",
    )
    sublane_hibernate.add_argument(
        "--journal",
        required=True,
        help="Redmine journal id that authorizes the hibernate (durable anchor)",
    )
    # Durable-record invariants the operator asserts from the Redmine record (each
    # defaults to unsatisfied so an omitted flag fails closed).
    for _opt, _dest, _help in (
        ("--explicitly-parked", "explicitly_parked",
         "The issue is open and explicitly parked/blocked (dependency park basis)."),
        ("--callbacks-drained", "callbacks_drained",
         "The lane owes no outstanding coordinator callback."),
        ("--no-review-pending", "no_review_pending",
         "The lane has no review awaiting a result."),
        ("--no-owner-approval-pending", "no_owner_approval_pending",
         "The lane has no owner close approval pending."),
        ("--no-integration-pending", "no_integration_pending",
         "The lane has no integration disposition pending."),
        ("--no-pending-prompt", "no_pending_prompt",
         "The lane has no composer input pending."),
        ("--not-working", "not_working", "The lane has no work in flight."),
        ("--worktree-clean", "worktree_clean",
         "The lane's worktree has no uncommitted diff (no boundary journal needed)."),
        ("--boundary-recorded", "boundary_recorded",
         "A boundary journal capturing the dirty worktree's diff / resume next-action "
         "is recorded (required when the worktree is not clean)."),
        # Early-hibernate park basis (Redmine #13967 item 1): the alternative to
        # --explicitly-parked for a review-approved + staging-integrated feature lane
        # whose dogfood/close is delegated to the dedicated release issue. All five must
        # hold to qualify (each defaults unsatisfied -> fail closed).
        ("--review-approved", "review_approved",
         "Early hibernate: the same-lane Review Gate is approved with no open findings."),
        ("--staging-integrated", "staging_integrated",
         "Early hibernate: the coordinator staging integration is recorded (merged / "
         "patch-equivalent to the staging branch)."),
        ("--required-ci-green", "required_ci_green",
         "Early hibernate: the required CI for the integrated commits is green."),
        ("--dogfood-delegated", "dogfood_delegated",
         "Early hibernate: TestPyPI / installed dogfood + close are delegated to the "
         "dedicated release issue via a durable park/delegation record."),
        ("--commits-pushed", "commits_pushed",
         "Early hibernate: the lane's commits are pushed / origin-reachable (unpushed "
         "fails closed — an early hibernate presupposes integrated work)."),
    ):
        sublane_hibernate.add_argument(
            _opt, dest=_dest, action="store_true", help=_help
        )
    sublane_hibernate.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help=(
            "Perform the hibernate: CAS the disposition (active->hibernated) and release "
            "the lane's managed processes. Without it this is preflight only (no mutation)."
        ),
    )
    add_repo_option(sublane_hibernate)
    sublane_hibernate.add_argument(
        "--json", action="store_true", help="Emit structured JSON output"
    )
    sublane_hibernate.set_defaults(func=cmd_sublane_hibernate)


__all__ = (
    "BLOCK_CALLBACK_DEBT",
    "BLOCK_INTEGRATION_PENDING",
    "BLOCK_NOT_PARKED",
    "BLOCK_ORIGINAL_IDENTITY",
    "BLOCK_OWNER_PENDING",
    "BLOCK_PENDING_PROMPT",
    "BLOCK_REVIEW_PENDING",
    "BLOCK_UNRECORDED_BOUNDARY",
    "BLOCK_UNPUSHED_COMMITS",
    "BLOCK_WORKING",
    "PARK_BASIS_DEPENDENCY",
    "PARK_BASIS_EARLY_HIBERNATE",
    "PARK_BASIS_NONE",
    "HibernateAssertions",
    "HibernateOutcome",
    "HibernatePreflight",
    "HibernateRequest",
    "LiveSublaneHibernateOps",
    "SublaneHibernateOps",
    "SublaneHibernateUseCase",
    "cmd_sublane_hibernate",
    "format_hibernate_text",
    "register_sublane_hibernate_parser",
)
