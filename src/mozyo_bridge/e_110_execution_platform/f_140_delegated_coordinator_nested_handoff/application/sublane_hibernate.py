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
   integration is pending, no composer input is pending, and no work is in flight. There
   are two park bases (Redmine #13967 item 1): the original **dependency park** (the issue
   is explicitly parked/blocked on a wait) and the standardized **early hibernate** (a
   same-lane review-approved + staging-integrated + required-CI-green feature lane whose
   TestPyPI / installed dogfood execution/evidence is delegated to the dedicated release
   issue — so the lane hibernates without waiting for ticket close or installed dogfood;
   unpushed commits fail closed here, since an early hibernate presupposes integrated work).
   **Owner close approval pending is basis-dependent**: it blocks a dependency park, but is
   NOT a blocker for early hibernate — the source issue's close authority + owner close
   approval stay with the coordinator's normal path (NOT delegated), so an early hibernate
   runs while owner approval is still outstanding (the ``owner_waiting`` state it serves).
   A dirty worktree does **not** block (hibernate preserves the worktree) but its
   uncommitted diff / resume next-action must be captured in a boundary journal first —
   asserted via ``worktree_clean`` OR ``boundary_recorded`` (Design Answer Q2). Any unmet
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

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_binding import record_matches_binding
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_assertions import (  # noqa: E501
    PARK_BASIS_DEPENDENCY,
    PARK_BASIS_EARLY_HIBERNATE,
    PARK_BASIS_NONE,
    HibernateAssertions,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    RELEASE_NOT_REQUESTED,
    RELEASE_RELEASED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessPinError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
    execute_herdr_retire_close,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    ReleaseOutcome,
    declared_generation_attested,
    declared_generation_exactly_live,
    drive_process_release,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_toctou import (  # noqa: E501
    BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT,
    BLOCK_RELEASE_BOUNDARY_MUTATION,
    BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT,
    BLOCK_WORKTREE_UNREADABLE,
    WorktreeMutationFingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    LaneActivityObservation,
    fresh_release_disposition,
    post_release_residue,
    read_fingerprint,
    read_live_lane_activity,
    read_live_worktree_fingerprint,
    redrive_detail,
    revalidate_boundary,
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
# Project-gateway action-time fences (Redmine #13811; design #13780 j#78386 §1-3). Each
# gates ONLY a project-gateway lane (a lane bound by `project_scope`, not `issue_id`); an
# issue-owned lane leaves all three satisfied, so its path is byte-identical.
#: The live inventory does not carry the lane's EXACT declared generation — a recycled /
#: renamed / provider-rebound / undeclared-role / ambiguous live slot. Releasing from the
#: current live rows would close a newer generation than the one declared (§1-3 "exact pair
#: pins" / "newer generation -> zero-actuation"); fail closed instead.
BLOCK_PROJECT_GENERATION_MISMATCH = "project_generation_mismatch"
#: A live managed slot lacks an action-time, generation-matched startup self-attestation
#: (missing / stale locator-drift / conflict / unreadable). §2 requires re-reading the
#: startup attestation at action time; an unattested live target is zero-actuation.
BLOCK_PROJECT_UNATTESTED = "project_slot_unattested"
#: The approval's asserted `lane_generation` does not equal the row's current generation —
#: a stale approval from a superseded incarnation (a retire + `open_next_generation` bumps
#: it, §1 "generation を混ぜない"). A stale approval never re-binds to the current
#: generation; the operator must assert the approved generation and it must still hold.
BLOCK_STALE_ACTION_GENERATION = "stale_action_generation"
#: The approval's asserted `revision` does not equal the row's current revision — the
#: process authority advanced WITHIN the same generation (pin repair / replacement /
#: decision update) since the approval, so the approved action authority is stale (Redmine
#: #13811 R2 F2; design j#78386 §1-3 "approved lane_generation + revision + action_id").
#: The disposition CAS is bound to the approved revision, so a stale approval is a pre-CAS
#: zero-write / zero-close, never re-bound to the current revision / pins.
BLOCK_STALE_ACTION_REVISION = "stale_action_revision"
#: The already-hibernated redrive was invoked by an approval whose immutable action identity
#: (the journal-scoped release action id) does NOT match the release the row already opened —
#: a DIFFERENT hibernate cycle's approval trying to redrive this cycle's stored release
#: (Redmine #13811 R4 F2; design j#78386 §1-3 "approved ... action_id" exact-match on both
#: fresh execution and redrive). A stale cross-cycle approval never resumes another cycle's
#: release; it fails closed zero-close.
BLOCK_STALE_ACTION_IDENTITY = "stale_action_identity"
# Early-hibernate (Redmine #13967 item 1) unpushed fence: an early-hibernate basis
# presupposes the commits are integrated to staging, so an early-hibernate attempt whose
# commits are not pushed / origin-reachable fails closed (unlike a dependency park, which
# deliberately preserves unpublished commits).
BLOCK_UNPUSHED_COMMITS = "unpushed_commits"


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
    #: Project-gateway action-time fences (Redmine #13811). Each defaults ``True`` so an
    #: issue-owned lane (which never sets them) is unchanged; only a project-gateway lane
    #: with a matched binding evaluates them against its declared generation / attestation /
    #: approved generation.
    project_generation_matched: bool = True
    project_attestation_ok: bool = True
    action_generation_current: bool = True
    #: The approved ``revision`` still equals the row's current revision (Redmine #13811 R2
    #: F2). ONLY meaningful on the fresh (active -> hibernated) path — the already-hibernated
    #: redrive resumes the stored action authority, so it leaves this ``True`` (the row's
    #: revision has advanced past the approval by the hibernate itself). ``True`` for an
    #: issue lane.
    action_revision_current: bool = True
    #: The approval's immutable action identity matches the release the row already opened
    #: (Redmine #13811 R4 F2). ONLY meaningful on the already-hibernated REDRIVE path — a
    #: cross-cycle approval whose journal-scoped action id differs from the stored
    #: ``release_action_id`` fails closed. ``True`` on the fresh path (no stored release yet)
    #: and for an issue lane.
    action_identity_current: bool = True
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
            and self.project_generation_matched
            and self.project_attestation_ok
            and self.action_generation_current
            and self.action_revision_current
            and self.action_identity_current
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
        # Project-gateway action-time fences (Redmine #13811): each names itself so the
        # operator sees exactly which exact-generation guard blocked the release.
        if not self.action_generation_current:
            reasons.append(BLOCK_STALE_ACTION_GENERATION)
        if not self.action_revision_current:
            reasons.append(BLOCK_STALE_ACTION_REVISION)
        if not self.action_identity_current:
            reasons.append(BLOCK_STALE_ACTION_IDENTITY)
        if not self.project_generation_matched:
            reasons.append(BLOCK_PROJECT_GENERATION_MISMATCH)
        if not self.project_attestation_ok:
            reasons.append(BLOCK_PROJECT_UNATTESTED)
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
            "project_generation_matched": self.project_generation_matched,
            "project_attestation_ok": self.project_attestation_ok,
            "action_generation_current": self.action_generation_current,
            "action_revision_current": self.action_revision_current,
            "action_identity_current": self.action_identity_current,
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class HibernateOutcome:
    """The full result: preflight verdict, the disposition commit, and the release."""

    executed: bool
    preflight: HibernatePreflight
    issue: str
    lane: str
    project_scope: str = ""
    already_hibernated: bool = False
    redrive_blocked: bool = False
    transition: Optional[CasOutcome] = None
    release: Optional[ReleaseOutcome] = None
    detail: str = ""
    #: Redmine #13843: the release-boundary (T1) re-validation fired on the fresh path (zero
    #: transition / zero close). A redrive boundary block folds into :attr:`redrive_blocked`.
    boundary_blocked: bool = False
    #: The typed reasons the boundary re-validation blocked (rendered with the preflight ones).
    boundary_reasons: tuple[str, ...] = ()
    #: Redmine #13843: the post-release (T2) check / release admission block withheld a success
    #: — the lane stays hibernated (work preserved) and :attr:`recovery_detail` names the next
    #: action.
    success_withheld: bool = False
    recovery_detail: str = ""

    @property
    def is_blocked(self) -> bool:
        if self.already_hibernated:
            # A re-drive on an already-hibernated lane still fails closed when its
            # current preservation gate is unmet, the inventory is unreadable (R1-F2), or the
            # release-boundary re-validation fired (Redmine #13843): never a silent zero-close
            # reported as success.
            return self.redrive_blocked
        # Redmine #13843: the release-boundary re-validation blocked the fresh path (pre-CAS,
        # zero mutation).
        if self.boundary_blocked:
            return True
        if not self.preflight.may_hibernate:
            return True
        # A commit that was attempted but not applied (a lost CAS race) is a block.
        if self.executed and self.transition is not None and not self.transition.applied:
            return True
        return False

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        """The preflight blocked reasons plus any release-boundary (T1) reasons (#13843)."""
        return tuple(self.preflight.blocked_reasons) + tuple(self.boundary_reasons)

    @property
    def is_success(self) -> bool:
        """A clean, FULLY-actuated hibernate success (Redmine #13843 review F5).

        Requires the release to have COMPLETED — ``released`` (every slot closed) or
        ``not_requested`` (no live slot / dead processes); a ``partial`` / ``requested`` /
        refused release is an incomplete actuation (re-drive needed), not a clean success.
        """
        if not self.executed or self.is_blocked or self.success_withheld:
            return False
        if self.release is not None and self.release.process_release not in (
            RELEASE_RELEASED,
            RELEASE_NOT_REQUESTED,
        ):
            return False
        return True

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "issue": self.issue,
            "lane": self.lane,
            "project_scope": self.project_scope,
            "already_hibernated": self.already_hibernated,
            "redrive_blocked": self.redrive_blocked,
            "is_blocked": self.is_blocked,
            "is_success": self.is_success,
            "boundary_blocked": self.boundary_blocked,
            "boundary_reasons": list(self.boundary_reasons),
            "success_withheld": self.success_withheld,
            "recovery_detail": self.recovery_detail,
            "blocked_reasons": list(self.blocked_reasons),
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

    def read_attestation(
        self, assigned_name: str
    ) -> Optional[IdentityAttestationRecord]: ...

    def read_worktree_mutation(self) -> WorktreeMutationFingerprint: ...

    def read_lane_activity(
        self, workspace_id: str, lane: str, rows: Sequence[Mapping[str, object]]
    ) -> LaneActivityObservation: ...

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

    def read_attestation(
        self, assigned_name: str
    ) -> Optional[IdentityAttestationRecord]:
        """Read a slot's #13637 startup self-attestation for the action-time gate.

        Read-only over the shared attestation store, fail-open to ``None`` (absent /
        unreadable): the project-gateway attestation gate then fails CLOSED on a ``None``
        (an un-attestable live slot is never released, Redmine #13811 / #13882), so a cache
        loss never falsely attests a slot.
        """
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        try:
            return HerdrIdentityAttestationStore().read(assigned_name)
        except Exception:  # noqa: BLE001 — unreadable attestation -> None -> gate fails closed
            return None

    def read_worktree_mutation(self) -> WorktreeMutationFingerprint:
        """Live worktree fingerprint (#13843) — delegates to the tri-state fail-closed probe."""
        return read_live_worktree_fingerprint(self.repo_root, self.timeout)

    def read_lane_activity(
        self, workspace_id: str, lane: str, rows: Sequence[Mapping[str, object]]
    ) -> LaneActivityObservation:
        """Live worker-busy / pending-composer observation (#13843 F2), fail-closed."""
        return read_live_lane_activity(
            rows, workspace_id, lane, repo_root=self.repo_root, env=self.env,
            runner=self.runner, timeout=self.timeout,
        )

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
    #: A project-gateway lane's canonical full project scope (Redmine #13811). When
    #: non-empty the lane is identified by its ``project_gateway`` owner binding (scope +
    #: empty issue), not by ``issue`` — which then names only the durable decision anchor the
    #: ``--journal`` is filed on, exactly as for an issue lane. Empty for an issue lane (the
    #: byte-identical pre-#13811 issue-owned path).
    project_scope: str = ""
    #: The approved expected ``lane_generation`` the operator asserts from the durable
    #: Redmine approval (Redmine #13811 R1 F1 item 3; design j#78386 §1-2 stale-approval
    #: fence). For a project-gateway lane it MUST be supplied and MUST equal the row's
    #: current generation, so an approval from a superseded incarnation (retire +
    #: ``open_next_generation`` bumps the generation) cannot re-bind to the current one.
    #: Ignored for an issue lane.
    expected_lane_generation: str = ""
    #: The approved expected lifecycle ``revision`` the operator asserts from the durable
    #: approval (Redmine #13811 R2 F2; design j#78386 §1-3). For a project-gateway lane it
    #: MUST be supplied and the FRESH (active -> hibernated) disposition CAS is bound to it,
    #: so an approval whose process authority has since advanced within the same generation
    #: (pin repair / replacement / decision update) fails closed pre-CAS rather than
    #: re-binding to the current revision / pins. Ignored for an issue lane; the
    #: already-hibernated redrive resumes the row's STORED release action id / pins (the
    #: immutable action authority), so it does not re-check this advanced revision.
    expected_revision: str = ""


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
        project_scope = _norm(request.project_scope)
        journal = _norm(request.journal)
        workspace_id = _norm(self.ops.workspace_id())

        # The immutable per-approval action identity (Redmine #13811 R4 F2). For a
        # project-gateway lane the release action id is scoped to the approving journal, so a
        # DIFFERENT hibernate cycle's approval (a different journal) can never resume THIS
        # cycle's stored release on the already-hibernated redrive. An issue lane keeps the
        # byte-identical ``hibernate:<lane>`` id (no behaviour change).
        action_id = f"hibernate:{lane}:{journal}" if project_scope else f"hibernate:{lane}"

        # A malformed identity / anchor can address nothing — fail closed before any read.
        # The decision anchor is required (and issue-addressable) for BOTH binding kinds: a
        # project-gateway lane owns a scope, but the journal that authorizes this hibernate
        # is still filed on a real issue (R2-F1). ``project_scope`` selects WHICH lane the
        # anchor may act on; it never replaces the anchor.
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
                project_scope=project_scope,
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
                project_scope=project_scope,
                detail="lifecycle store unreadable; fail closed",
            )

        # Read the live inventory ONCE for the preflight gates, keeping readability explicit
        # (R1-F1). An unreadable inventory is never folded to "empty". The release close does
        # NOT reuse this stale snapshot: Redmine #13843 re-reads a FRESH inventory + worktree
        # fingerprint at the release boundary and blocks on any divergence (see
        # :func:`sublane_hibernate_boundary.revalidate_boundary`).
        rows, inventory_readable = self.ops.read_inventory()
        # Redmine #13843 preflight (T0) worktree fingerprint — the baseline the release-boundary
        # re-validation compares the fresh (T1) capture against.
        fingerprint_preflight = read_fingerprint(self.ops)

        # Project-gateway action-time exact-generation fences (Redmine #13811; design #13780
        # j#78386 §1-2). The release closes the lane's CURRENT live slots, so before any
        # mutation a project-gateway lane must prove (a) the operator's approved generation
        # still equals the row's current generation — no stale approval re-binds to a newer
        # incarnation; (b) those live slots ARE its exact declared generation — no recycled /
        # renamed / provider-rebound / ambiguous slot is closed from a stale declaration; and
        # (c) every live target carries an action-time, generation-matched startup
        # attestation. An issue-owned lane (empty ``project_scope``) skips all three: they
        # stay ``True`` and the issue path is byte-identical. Only evaluated on a readable
        # inventory that this exact project lane owns; a corrupt declared snapshot fails
        # closed (never coerced to "matched").
        (
            action_generation_current,
            project_generation_matched,
            project_attestation_ok,
        ) = self._project_gates(
            rec,
            rows,
            project_scope=project_scope,
            workspace_id=workspace_id,
            lane=lane,
            inventory_readable=inventory_readable,
            expected_lane_generation=_norm(request.expected_lane_generation),
        )

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
            and record_matches_binding(rec, issue_id=issue, project_scope=project_scope)
        )
        if already_hibernated:
            # Redmine #13811 R4 F2 / R5: a project-gateway redrive is honored only when THIS
            # approval owns the current cycle — a release already open must match the approval's
            # journal-scoped action id, and in the crash window where the CAS landed but the
            # release was not yet opened (empty action id) only the SAME durable decision the CAS
            # stored may open it now (else an old approval overwrites this cycle's pair).
            if not project_scope:
                action_identity_current = True
            elif _norm(rec.release_action_id):
                action_identity_current = _norm(rec.release_action_id) == action_id
            else:
                action_identity_current = decision is not None and (
                    _norm(rec.decision_source),
                    _norm(rec.decision_issue_id),
                    _norm(rec.decision_journal),
                ) == (
                    _norm(decision.source),
                    _norm(decision.issue_id),
                    _norm(decision.journal_id),
                )
            # The redrive also honors the project exact-generation / attestation / approval
            # gates: a partial release is resumed only while the lane's live slots are still
            # its declared, attested generation and the approval still names it — never
            # re-pinning a slot recycled between the partial close and the resume.
            redrive_ok = (
                inventory_readable
                and request.assertions.preservation_satisfied
                and action_generation_current
                and action_identity_current
                and project_generation_matched
                and project_attestation_ok
            )
            # Redmine #13843: the redrive re-drives a process release, so it takes the SAME
            # release-boundary TOCTOU fence as the fresh path — a fresh worktree fingerprint +
            # live-generation re-read before the (idempotent) close, and a post-release check
            # after it. A boundary divergence blocks the redrive (zero close), and a
            # post-release residue withholds the resumed success.
            release = None
            boundary_reasons: tuple[str, ...] = ()
            post_residue = False
            recovery_detail = ""
            if execute and redrive_ok:
                rows1, fingerprint_boundary, boundary_reasons = revalidate_boundary(
                    ops=self.ops,
                    store=self.store,
                    key=key,
                    rec0=rec,
                    rows0=rows,
                    fingerprint_preflight=fingerprint_preflight,
                    workspace_id=workspace_id,
                    lane=lane,
                    project_scope=project_scope,
                )
                if not boundary_reasons:
                    # Bind the resume to the T1-verified revision (Redmine #13843 review F3):
                    # an advance between T1 and the driver read closes nothing.
                    release = self._drive_release(
                        key, lane, workspace_id, rows1, action_id,
                        expected_revision=rec.revision,
                    )
                    if release.admission_blocked:
                        boundary_reasons = (BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT,)
                        release = None
                    else:
                        post = post_release_residue(
                            ops=self.ops, fingerprint_boundary=fingerprint_boundary
                        )
                        post_residue = post.residue_detected
                        recovery_detail = post.recovery_detail
            redrive_executed = execute and redrive_ok and not boundary_reasons
            redrive_blocked = execute and (not redrive_ok or bool(boundary_reasons))
            preflight = HibernatePreflight(
                original_identity_known=True,  # the hibernated lane is known
                park_satisfied=request.assertions.park_satisfied,
                obligations_satisfied=request.assertions.obligations_satisfied,
                lane_idle=request.assertions.lane_idle,
                boundary_ok=request.assertions.boundary_ok,
                inventory_readable=inventory_readable,
                project_generation_matched=project_generation_matched,
                project_attestation_ok=project_attestation_ok,
                action_generation_current=action_generation_current,
                action_identity_current=action_identity_current,
                assertions=request.assertions,
            )
            return HibernateOutcome(
                executed=redrive_executed,
                preflight=preflight,
                issue=issue,
                lane=lane,
                project_scope=project_scope,
                already_hibernated=True,
                redrive_blocked=redrive_blocked,
                boundary_reasons=boundary_reasons,
                release=release,
                success_withheld=post_residue,
                recovery_detail=recovery_detail,
                detail=redrive_detail(
                    redrive_ok=redrive_ok,
                    boundary_reasons=boundary_reasons,
                    post_residue=post_residue,
                ),
            )

        # Fresh-path stale-approval REVISION fence (Redmine #13811 R2 F2). For a
        # project-gateway lane the operator asserts the approved lifecycle revision; the fresh
        # active -> hibernated CAS is bound to it, so an approval whose process authority
        # advanced WITHIN the same generation (pin repair / replacement / decision update)
        # since the approval fails closed pre-CAS rather than re-binding to the current
        # revision / pins. An issue lane is unchanged (the CAS uses the current revision). The
        # already-hibernated redrive above does NOT apply this — it resumes the stored release
        # action id / pins (the row's revision has advanced past the approval by the hibernate
        # itself), so re-asserting the approval's revision there would wrongly block resume.
        expected_revision = _norm(request.expected_revision)
        cas_expected_revision = rec.revision if rec is not None else 0
        action_revision_current = True
        if project_scope and record_matches_binding(rec, project_scope=project_scope):
            assert rec is not None  # record_matches_binding is False for None
            action_revision_current = (
                expected_revision.isdigit() and int(expected_revision) == rec.revision
            )
            if action_revision_current:
                cas_expected_revision = int(expected_revision)

        original_identity_known = (
            rec is not None
            and rec.lane_disposition == DISPOSITION_ACTIVE
            and record_matches_binding(rec, issue_id=issue, project_scope=project_scope)
        )
        preflight = HibernatePreflight(
            original_identity_known=original_identity_known,
            park_satisfied=request.assertions.park_satisfied,
            obligations_satisfied=request.assertions.obligations_satisfied,
            lane_idle=request.assertions.lane_idle,
            boundary_ok=request.assertions.boundary_ok,
            inventory_readable=inventory_readable,
            project_generation_matched=project_generation_matched,
            project_attestation_ok=project_attestation_ok,
            action_generation_current=action_generation_current,
            action_revision_current=action_revision_current,
            assertions=request.assertions,
        )
        if not preflight.may_hibernate or not execute:
            return HibernateOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                project_scope=project_scope,
                detail=(
                    "preflight only (no --execute)"
                    if preflight.may_hibernate
                    else "fail-closed: hibernate blocked"
                ),
            )

        # Redmine #13843 release-boundary (T1) TOCTOU fence. Between the preflight snapshot
        # and the process release a worker can start a worktree mutation (the operator's
        # clean/idle assertions and the inventory read are NOT atomic with the pane close),
        # so re-read a FRESH worktree fingerprint + live inventory here and block on any
        # divergence from the preflight capture BEFORE the disposition CAS. A block is a typed
        # fail-closed with **lifecycle transition 0 / process close 0** — the lane stays active
        # and nothing is closed.
        rows1, fingerprint_boundary, boundary_reasons = revalidate_boundary(
            ops=self.ops,
            store=self.store,
            key=key,
            rec0=rec,
            rows0=rows,
            fingerprint_preflight=fingerprint_preflight,
            workspace_id=workspace_id,
            lane=lane,
            project_scope=project_scope,
        )
        if boundary_reasons:
            return HibernateOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                project_scope=project_scope,
                boundary_blocked=True,
                boundary_reasons=boundary_reasons,
                detail=(
                    "fail-closed: release-boundary re-validation blocked ("
                    + ", ".join(boundary_reasons)
                    + ")"
                ),
            )

        # Commit point: CAS active -> hibernated, guarded on the lane's exact state +
        # revision and the durable decision anchor. Nothing is closed until this lands, and
        # may_hibernate already required a readable inventory — so the CAS is never reached
        # on an unverifiable one (zero-mutation on unreadable, R1-F1). For a project-gateway
        # lane ``cas_expected_revision`` is the operator's APPROVED revision (Redmine #13811 R2
        # F2) — bound here so the atomic CAS itself refuses a same-generation revision drift,
        # not only the pre-CAS gate; for an issue lane it is the current revision (unchanged).
        assert rec is not None  # guaranteed by original_identity_known
        # Redmine #13844 R3: hibernate is a schema-needing mutation. Its write opens through the
        # universal `_connect_write` gate, which emits the PRE-migration peer-reader advisory to
        # stderr BEFORE the shared store is migrated (no per-command emit needed here).
        transition = self.store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=cas_expected_revision,
            target=DISPOSITION_HIBERNATED,
            decision=decision,
        )
        if not transition.applied:
            return HibernateOutcome(
                executed=True,
                preflight=preflight,
                issue=issue,
                lane=lane,
                project_scope=project_scope,
                transition=transition,
                detail=f"hibernate commit refused ({transition.reason})",
            )

        # Release on the FRESH boundary snapshot (rows1), bound to the exact revision the CAS
        # just committed (Redmine #13843 review F3): an authority advance between the CAS and the
        # driver read closes nothing (admission_blocked). Then the post-release (T2) check +
        # disposition resolution withhold the success on residue / admission block.
        release = self._drive_release(
            key, lane, workspace_id, rows1, action_id,
            expected_revision=transition.revision,
        )
        post = post_release_residue(
            ops=self.ops, fingerprint_boundary=fingerprint_boundary
        )
        withheld, recovery, detail = fresh_release_disposition(release, post)
        return HibernateOutcome(
            executed=True,
            preflight=preflight,
            issue=issue,
            lane=lane,
            project_scope=project_scope,
            transition=transition,
            release=release,
            success_withheld=withheld,
            recovery_detail=recovery,
            detail=detail,
        )

    def _project_gates(
        self,
        rec: Optional[Any],
        rows: Sequence[Mapping[str, object]],
        *,
        project_scope: str,
        workspace_id: str,
        lane: str,
        inventory_readable: bool,
        expected_lane_generation: str,
    ) -> tuple[bool, bool, bool]:
        """The three project-gateway action-time gates (Redmine #13811; j#78386 §1-2).

        Returns ``(action_generation_current, project_generation_matched,
        project_attestation_ok)``. All three are ``True`` for an issue-owned lane (empty
        ``project_scope``) or when the row is not the project lane the caller names / the
        inventory is unreadable — so the issue path is byte-identical and the project gates
        never *add* a spurious block on top of an identity / readability block that already
        fires. Evaluated only when ``project_scope`` is set AND the row's binding matches it
        AND the inventory is readable:

        - **action_generation_current** (§1 stale-approval fence): the operator's asserted
          approved ``lane_generation`` must be non-empty AND equal the row's current
          generation. A missing assertion or a superseded incarnation fails closed.
        - **project_generation_matched** (§1-2 exact-generation): the live inventory carries
          the exact declared generation (:func:`declared_generation_exactly_live`). A
          corrupt declared snapshot (:class:`ProcessPinError`) fails closed.
        - **project_attestation_ok** (§2): every live target carries an action-time,
          generation-matched startup attestation (:func:`declared_generation_attested`).
        """
        if not project_scope:
            return True, True, True
        if not inventory_readable or not record_matches_binding(
            rec, project_scope=project_scope
        ):
            # A non-matching / unreadable case already blocks on identity / readability; the
            # project gates stay satisfied so they do not double-report.
            return True, True, True
        assert rec is not None  # record_matches_binding is False for None
        action_generation_current = bool(expected_lane_generation) and (
            str(rec.lane_generation) == expected_lane_generation
        )
        try:
            project_generation_matched = declared_generation_exactly_live(
                rec.declared_pins, rows, workspace_id=workspace_id, lane_id=lane
            )
        except ProcessPinError:
            project_generation_matched = False
        project_attestation_ok = declared_generation_attested(
            rows, workspace_id, lane, self.ops.read_attestation
        )
        return (
            action_generation_current,
            project_generation_matched,
            project_attestation_ok,
        )

    def _drive_release(
        self,
        key: LaneLifecycleKey,
        lane: str,
        workspace_id: str,
        rows: Sequence[Mapping[str, object]],
        action_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> ReleaseOutcome:
        """Open (or resume) the release generation and close the lane's managed slots.

        Delegates to the shared tombstone-free driver: it never removes a worktree,
        deletes a branch, or writes a metadata tombstone, and a partial close leaves the
        generation open and re-drivable (Redmine #13682). ``rows`` is the readability-vetted
        inventory snapshot the caller already read (R1-F1), so an empty ``rows`` means a
        *confirmed*-empty inventory, never an unreadable one. ``action_id`` is the caller's
        immutable action identity. ``expected_revision`` binds the release to the T1-verified
        lifecycle authority (Redmine #13843 review F3): a driver read that no longer carries it
        closes nothing (admission_blocked).
        """
        return drive_process_release(
            store=self.store,
            ops=self.ops,
            key=key,
            lane_id=lane,
            workspace_id=workspace_id,
            action_id=action_id,
            rows=rows,
            expected_revision=expected_revision,
        )


__all__ = (
    "BLOCK_CALLBACK_DEBT",
    "BLOCK_INTEGRATION_PENDING",
    "BLOCK_INVENTORY_UNREADABLE",
    "BLOCK_NOT_PARKED",
    "BLOCK_ORIGINAL_IDENTITY",
    "BLOCK_OWNER_PENDING",
    "BLOCK_PENDING_PROMPT",
    "BLOCK_PROJECT_GENERATION_MISMATCH",
    "BLOCK_PROJECT_UNATTESTED",
    "BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT",
    "BLOCK_RELEASE_BOUNDARY_MUTATION",
    "BLOCK_REVIEW_PENDING",
    "BLOCK_STALE_ACTION_GENERATION",
    "BLOCK_STALE_ACTION_IDENTITY",
    "BLOCK_STALE_ACTION_REVISION",
    "BLOCK_UNRECORDED_BOUNDARY",
    "BLOCK_UNPUSHED_COMMITS",
    "BLOCK_WORKING",
    "BLOCK_WORKTREE_UNREADABLE",
    "WorktreeMutationFingerprint",
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
)
