"""`sublane hibernate` fail-closed preflight — pure verdict type (Redmine #13682 / #13843).

Split out of :mod:`sublane_hibernate` (module-health reduction, Redmine #14230, the same
reason :mod:`sublane_hibernate_toctou` was split out earlier): the preflight blocked-reason
vocabulary and :class:`HibernatePreflight` are a pure, self-contained decision type with no
IO of their own — they only compose :class:`~sublane_hibernate_assertions.HibernateAssertions`
(the durable-record-asserted gates) into one fail-closed verdict. Relocated, not rewritten:
every name, field, and behavior is unchanged from its prior home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_assertions import (  # noqa: E501
    HibernateAssertions,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_toctou import (  # noqa: E501
    BLOCK_COMPOSER_PENDING_REAL,
    BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN,
    BLOCK_WORKER_BUSY,
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
    # --- Dry-run / action-time parity (Redmine #15193) ---------------------------------
    # The gates above are all ASSERTED from the durable record; the release boundary
    # (T1) additionally LIVE-PROBES the composer and worker. That asymmetry is the
    # divergence #15193 names: an operator asserting `--not-working` / no pending prompt
    # got `may_hibernate=true` from a dry-run, then `--execute` blocked on
    # `composer_pending_real` — a blocker the dry-run had every means to report and did
    # not, because it never looked (#15110 j#102068, #15140 j#102064).
    #
    # These three carry a T0 live observation so the dry-run returns the SAME typed token
    # the boundary would. They default to the quiescent/readable values, so a caller that
    # supplies no probe keeps the previous behaviour exactly.
    #: A REAL (ghost-refined) pending composer input was positively observed at preflight.
    live_composer_pending: bool = False
    #: A live managed slot was positively observed mid-turn at preflight.
    live_worker_busy: bool = False
    #: The live activity probe itself was readable. An UNREADABLE probe deliberately does
    #: NOT block the dry-run: a dry-run is a diagnostic, an unreadable probe proves nothing,
    #: and failing it closed would deny the operator the very report they ran it for. The
    #: action-time boundary still fails closed on the same condition — the difference is
    #: reported explicitly by :attr:`unverified_axes` rather than left silent.
    live_activity_readable: bool = True
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
            and not self.live_composer_pending
            and not self.live_worker_busy
        )

    @property
    def unverified_axes(self) -> tuple[str, ...]:
        """Axes this preflight could NOT verify but the action-time boundary will (#15193).

        A green preflight carrying a non-empty ``unverified_axes`` means exactly: "no blocker
        was found, AND this axis was not provable here, so ``--execute`` may still block on
        it". That is the typed explanation of the dry-run / action-time difference the issue
        asks for — previously the same situation was reported as an unqualified green.
        """
        if not self.live_activity_readable:
            return (BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN,)
        return ()

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
        # Redmine #15193 dry-run parity: the SAME tokens the release boundary uses, so an
        # operator sees one vocabulary across preflight and action time rather than a green
        # dry-run followed by an unfamiliar `composer_pending_real`.
        if self.live_worker_busy:
            reasons.append(BLOCK_WORKER_BUSY)
        if self.live_composer_pending:
            reasons.append(BLOCK_COMPOSER_PENDING_REAL)
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
            "live_composer_pending": self.live_composer_pending,
            "live_worker_busy": self.live_worker_busy,
            "live_activity_readable": self.live_activity_readable,
            "unverified_axes": list(self.unverified_axes),
            "blocked_reasons": list(self.blocked_reasons),
        }


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
    "BLOCK_REVIEW_PENDING",
    "BLOCK_STALE_ACTION_GENERATION",
    "BLOCK_STALE_ACTION_IDENTITY",
    "BLOCK_STALE_ACTION_REVISION",
    "BLOCK_UNPUSHED_COMMITS",
    "BLOCK_WORKING",
    "HibernatePreflight",
)
