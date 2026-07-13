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
   issue, and every durable idle gate the operator asserts from the Redmine record holds:
   the issue is explicitly parked/blocked, no coordinator callback is owed, no review /
   owner-approval / integration is pending, no composer input is pending, and no work is
   in flight. A dirty worktree does **not** block (hibernate preserves the worktree) but
   its uncommitted diff / resume next-action must be captured in a boundary journal first
   — asserted via ``worktree_clean`` OR ``boundary_recorded`` (Design Answer Q2). Any unmet
   gate blocks with a reason and mutates nothing; each flag defaults to the unsatisfied
   (safe-failing) value.
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


# ---------------------------------------------------------------------------
# Operator-asserted durable-record invariants (the "lane is idle & parked" gate).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HibernateAssertions:
    """Durable-record facts no live probe can infer, asserted from the Redmine record.

    Every flag defaults to the unsatisfied (safe-failing) value, so a caller that omits
    one fails closed. The lane must be explicitly parked/blocked with no outstanding
    coordinator obligation before its processes are released:

    - :attr:`explicitly_parked` — the issue is open AND explicitly parked/blocked (the
      affirmative precondition; hibernate is never an idle-timeout kill, j#77485).
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

    @property
    def no_outstanding_obligation(self) -> bool:
        return (
            self.callbacks_drained
            and self.no_review_pending
            and self.no_owner_approval_pending
            and self.no_integration_pending
        )

    @property
    def lane_idle(self) -> bool:
        return self.no_pending_prompt and self.not_working

    @property
    def boundary_ok(self) -> bool:
        """A clean worktree, or a recorded boundary journal for a dirty one."""
        return self.worktree_clean or self.boundary_recorded

    @property
    def preservation_satisfied(self) -> bool:
        """Every work-preservation gate holds (identity aside).

        The gate both the initial hibernate and the already-hibernated release re-drive
        share (R1-F2): a lane whose processes are (re-)released must be explicitly parked,
        owe no coordinator obligation, be idle (no work in flight / no pending composer),
        and have its dirty diff captured. A partial-release retry re-checks this against the
        lane's *current* state — a lane that has since started working is never closed.
        """
        return (
            self.explicitly_parked
            and self.no_outstanding_obligation
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
    explicitly_parked: bool
    no_outstanding_obligation: bool
    lane_idle: bool
    boundary_ok: bool
    inventory_readable: bool = True
    assertions: HibernateAssertions = field(default_factory=HibernateAssertions)

    @property
    def may_hibernate(self) -> bool:
        return (
            self.original_identity_known
            and self.explicitly_parked
            and self.no_outstanding_obligation
            and self.lane_idle
            and self.boundary_ok
            and self.inventory_readable
        )

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.original_identity_known:
            reasons.append(BLOCK_ORIGINAL_IDENTITY)
        if not self.explicitly_parked:
            reasons.append(BLOCK_NOT_PARKED)
        # Each obligation names itself so the operator sees exactly which gate is unmet.
        if not self.assertions.callbacks_drained:
            reasons.append(BLOCK_CALLBACK_DEBT)
        if not self.assertions.no_review_pending:
            reasons.append(BLOCK_REVIEW_PENDING)
        if not self.assertions.no_owner_approval_pending:
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
            "explicitly_parked": self.explicitly_parked,
            "no_outstanding_obligation": self.no_outstanding_obligation,
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
                explicitly_parked=request.assertions.explicitly_parked,
                no_outstanding_obligation=request.assertions.no_outstanding_obligation,
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
                explicitly_parked=request.assertions.explicitly_parked,
                no_outstanding_obligation=request.assertions.no_outstanding_obligation,
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
                explicitly_parked=request.assertions.explicitly_parked,
                no_outstanding_obligation=request.assertions.no_outstanding_obligation,
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
            explicitly_parked=request.assertions.explicitly_parked,
            no_outstanding_obligation=request.assertions.no_outstanding_obligation,
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


__all__ = (
    "BLOCK_CALLBACK_DEBT",
    "BLOCK_INTEGRATION_PENDING",
    "BLOCK_NOT_PARKED",
    "BLOCK_ORIGINAL_IDENTITY",
    "BLOCK_OWNER_PENDING",
    "BLOCK_PENDING_PROMPT",
    "BLOCK_REVIEW_PENDING",
    "BLOCK_UNRECORDED_BOUNDARY",
    "BLOCK_WORKING",
    "HibernateAssertions",
    "HibernateOutcome",
    "HibernatePreflight",
    "HibernateRequest",
    "LiveSublaneHibernateOps",
    "SublaneHibernateOps",
    "SublaneHibernateUseCase",
    "cmd_sublane_hibernate",
    "format_hibernate_text",
)
