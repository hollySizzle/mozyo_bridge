"""Public guarded live-worker refresh surface (Redmine #14661).

The recovery gap #14661 names, measured on the #14658 lane (j#92366): a standard sublane
implementation **worker** settles back to a live ``turn_ended`` after a confirmed resume
delivery, produces no durable progress, and holds in-scope dirty worktree files that must
survive. ``sublane recover-stale`` refuses it (``not_stale`` — the process is genuinely live,
and that fence stays), ``sublane recover-gateway`` protects the worker by design,
``sublane callback-recovery`` reports ``no_progress_after_handoff`` without closing anything,
and ``sublane start --execute`` re-adopts the pair but is not exactly-once on a repeat. This
use case is the missing surface: ``sublane refresh-worker``.

The default is a **read-only preflight**: classify the worker's provider turn
(:func:`...domain.worker_turn_recovery.classify_worker_turn` — the durable journal is the
authority, and the classification is admissible only while bound to the exact anchor / lane
generation / participant revision) and the refresh target
(:func:`...domain.worker_turn_recovery.decide_worker_refresh` — ordered fail-closed gates
protecting the lane gateway / default coordinator / foreign slot, and refusing an unreadable
worktree). ``--execute`` actuates ONLY with a positive owner approval (a durable Redmine
:class:`DecisionPointer` + the exact ``refresh-worker:<…>`` action id + the immutable approved
generation) AND the action-time re-verification that the target is still the exact failed
worker.

The actuation is **atomic + resumable**, driven by the identical #13806 tranche A/B machinery
the other two recovery surfaces use — it plans a *non-self* replacement transaction whose sole
participant is the worker, drives it through
:meth:`...ReplacementActuatorUseCase.drive_worker_recovery` (guarded exact-generation close →
same-slot fresh launch → action-bound attestation), and only after the fresh worker is attested
drives the resume continuation exactly once through the shared
:func:`...replacement_continuation_drain.drive_continuation_once` authority. The worktree,
branch, gateway slot, and durable route are never touched: the close ends ONE process, so the
dirty working tree it was editing is preserved byte-for-byte and the fresh worker re-joins the
same checkout. The resume re-delivers the EXISTING durable anchor — it never regenerates an
Implementation Request / Review Request.

Every failure / partial refresh is durably recorded: the replacement transaction row holds the
replay fence (a re-run resumes; a crash between close and launch is admitted as a post-close
resume ONLY on the expected ``identity_unknown`` + a committed-close transaction), and the
typed outcome names the exact fence that stopped it. No blind resend, no raw backend operation,
no generic kill is reachable from this surface.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

from mozyo_bridge.core.state.replacement_preservation import (
    assess_worker_recovery_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import (
    CAS_ALREADY_DECLARED,
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    ContinuationPointerError,
    DecisionPointerError,
    ParticipantPinError,
    norm,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    DEFAULT_LEASE_TTL_SECONDS,
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (  # noqa: E501
    ExactGenerationActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_continuation_drain import (  # noqa: E501
    CONTINUATION_CONFIRMED,
    drive_continuation_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    RESUMABLE_GATES,
    RESUME_VIA_CALLBACK_RECOVERY,
    normalize_turn_failure_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_UNKNOWN,
    launch_authority_current,
    launch_authority_runbook,
    normalize_launch_authority_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_RECOVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_launch_failure import (  # noqa: E501
    LAUNCH_FAILURE_NONE,
    launch_failure_detail,
    normalize_launch_failure_reason,
    port_launch_failure_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    worker_close_committed,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    WORKER_REFRESH_ACTIONABLE,
    WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY,
    WORKER_REFRESH_BLOCK_UNKNOWN,
    WorkerRefreshObservation,
    WorkerTurnObservation,
    classify_worker_turn,
    decide_worker_refresh,
    worker_refresh_action_id,
)

# -- refresh status vocabulary (closed; the #14203 spelling, shared verbatim) ----

#: Preflight only — no ``--execute`` was requested (read-only classification).
WORKER_REFRESH_STATUS_PREFLIGHT = "preflight"
#: ``--execute`` refused before any actuation (a typed preflight blocker or an incomplete
#: owner approval) — zero close.
WORKER_REFRESH_STATUS_REFUSED = "refused"
#: The guarded actuation ran and every leg completed: the worker is refreshed (fresh slot
#: attested) AND the existing durable anchor's resume was driven to confirmed exactly once.
WORKER_REFRESH_STATUS_COMPLETED = "completed"
#: The actuation ran but a leg stopped fail-closed; the durable transaction holds the replay
#: fence — a re-run resumes. The stopping leg's closed token is carried in the outcome.
WORKER_REFRESH_STATUS_STOPPED = "stopped"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WorkerRefreshRequest:
    """One approved live-worker refresh request (the exact target + the owner approval)."""

    issue: str
    lane: str
    role: str
    provider: str
    assigned_name: str
    locator: str
    #: The Redmine journal id of the positive owner approval (``--execute`` only).
    journal: str = ""
    #: The exact ``refresh-worker:<…>`` action id the approval names — re-derived and matched,
    #: never trusted verbatim.
    action_id: str = ""
    #: The immutable approved generation counter (>= 1). The transaction's authority token.
    action_generation: int = 0
    #: The LIVE WORKER INVENTORY row revision pinned at approval time (the
    #: ``generation_matches`` preflight axis AND the third #14661 classification binding; a
    #: distinct authority from the lane lifecycle below — the #13806 revision-authority-split
    #: lesson). Unlike ``recover-stale``'s same-named field, an EMPTY pin never matches here:
    #: a destructive refresh may not ride an unpinned generation (the #14203 j#87364 F5 rule).
    worker_revision: str = ""
    #: The LANE LIFECYCLE ``(revision, generation)`` pinned at approval time — the evidence
    #: the close-boundary preservation fence re-verifies against the live lifecycle store, and
    #: the second #14661 classification binding.
    lane_revision: str = ""
    lane_generation: str = ""
    #: The issue carrying the durable ANCHOR + approval journals — a SEPARATE authority from
    #: :attr:`issue` (the #14203 j#87364 F1 split): :attr:`issue` is the lane's OWNING issue
    #: (the destructive authorization boundary the issue-lane fence compares), while a child
    #: issue worked ON that lane carries the anchors. Empty falls back to :attr:`issue`.
    anchor_issue: str = ""
    #: The EXISTING durable anchor the fresh worker must resume — the un-answered gate's
    #: Redmine journal id, and the first #14661 classification binding. A SEPARATE authority
    #: from :attr:`journal` (the approval): the resume re-delivers this anchor exactly once and
    #: never regenerates a gate.
    resume_anchor_journal: str = ""
    #: The durable gate kind the resume anchor carries (a closed
    #: :data:`...gateway_turn_recovery.RESUMABLE_GATES` member).
    resume_gate: str = ""
    #: Optional structured turn-failure reason-evidence token (normalized to the closed
    #: secret-safe set; anything unrecognized collapses to ``unknown`` — fail-closed).
    reason_token: str = ""

    @property
    def effective_anchor_issue(self) -> str:
        """The issue whose journals carry the approval + resume anchor (F1 authority split)."""
        return norm(self.anchor_issue) or norm(self.issue)

    @property
    def holder(self) -> str:
        """The stable, action-bound lease identity for this refresh (resume-safe)."""
        return f"refresh-worker:{norm(self.action_id)}:g{int(self.action_generation)}"


@dataclass(frozen=True)
class WorkerRefreshOutcome:
    """The typed outcome the coordinator renders / gates on."""

    issue: str
    lane: str
    role: str
    #: The provider-turn classification (a closed shared ``TURN_CLASS_*`` token).
    turn_class: str
    #: The secret-safe turn-failure reason (a closed ``TURN_REASON_*`` token; ``unknown``
    #: whenever no structured evidence was injected — never inferred).
    turn_reason: str
    #: The refresh preflight verdict (a closed ``WORKER_REFRESH_*`` token).
    verdict: str
    status: str
    executed: bool = False
    refresh_status: str = ""
    resume_status: str = ""
    closed_old_worker: bool = False
    fresh_slot_attested: bool = False
    phase: str = ""
    revision: int = 0
    detail: str = ""
    turn_observation: Optional[dict[str, object]] = None
    observation: Optional[dict[str, bool]] = None
    preservation_reasons: tuple[str, ...] = ()
    #: Whether this --execute was admitted as a POST-CLOSE resume (the #13806 correction:
    #: close committed, launch owed — the pinned old worker is expectedly absent).
    post_close_resume: bool = False
    #: The lane LAUNCH-authority axis as a closed
    #: :data:`...lane_launch_authority.LAUNCH_AUTHORITY_REASONS` token, and its secret-safe
    #: operator runbook. Emitted as TYPED fields on EVERY outcome — preflight, refused,
    #: stopped, completed — so an operator / automation can select a recovery action without
    #: parsing :attr:`detail` prose. Both are axis-level facts: never a path, token value,
    #: branch, or identity.
    launch_authority_reason: str = LAUNCH_AUTHORITY_UNKNOWN
    launch_authority_runbook: str = ""
    #: WHY the action-bound launch leg fenced, as the closed token the fence itself raised
    #: (Redmine #14480). ``""`` whenever no launch fence fired — the launch succeeded, or the
    #: run stopped somewhere else entirely — so a consumer never has to read "field absent" as
    #: "launch was fine". Value-free by construction: an axis / fence name, never a path,
    #: locator, credential, or exception prose.
    launch_failure_reason: str = LAUNCH_FAILURE_NONE

    @property
    def is_blocked(self) -> bool:
        if not self.executed:
            return False
        return self.status != WORKER_REFRESH_STATUS_COMPLETED

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "role": self.role,
            "turn_class": self.turn_class,
            "turn_reason": self.turn_reason,
            "verdict": self.verdict,
            "status": self.status,
            "executed": self.executed,
            "refresh_status": self.refresh_status or None,
            "resume_status": self.resume_status or None,
            "closed_old_worker": self.closed_old_worker,
            "fresh_slot_attested": self.fresh_slot_attested,
            "phase": self.phase or None,
            "revision": self.revision,
            "is_blocked": self.is_blocked,
            "detail": self.detail,
            "turn_observation": self.turn_observation,
            "observation": self.observation,
            "preservation_reasons": list(self.preservation_reasons),
            "post_close_resume": self.post_close_resume,
            "launch_authority_reason": self.launch_authority_reason,
            "launch_authority_runbook": self.launch_authority_runbook or None,
            "launch_failure_reason": self.launch_failure_reason or None,
        }


@runtime_checkable
class WorkerRefreshOps(Protocol):
    """The injected observe + resume effects (faked in tests; live wiring is the ``*_live``)."""

    def observe_turn(self, request: WorkerRefreshRequest) -> WorkerTurnObservation:
        """Observe the delivered anchor's WORKER provider turn (read-only, all-positive-fact).

        Resolves the delivery's callback outcome (positively ``sent`` to the pinned worker
        locator?), the turn-start observation, the FRESH settled runtime state, a FRESH
        anchored+ordered durable re-read of whether worker progress landed after the anchor,
        and the three #14661 identity bindings. Every unreadable axis stays ``False``
        (fail-closed classification).
        """
        ...

    def observe_target(self, request: WorkerRefreshRequest) -> WorkerRefreshObservation:
        """Observe the live pinned worker slot (read-only, all-positive-fact)."""
        ...

    def lane_authority_reason(self, request: WorkerRefreshRequest) -> str:
        """WHICH lane launch-authority axis fails right now? (read-only)

        A closed :data:`...lane_launch_authority.LAUNCH_AUTHORITY_REASONS` token naming the
        first failing axis (``ok`` when every axis holds). This is the SINGLE evaluator behind
        both the pre-close preflight axis and :meth:`resume_lane_authority` — a preflight
        backed by a second implementation drifts away from the effect it predicts (#14475).
        """
        ...

    def resume_lane_authority(self, request: WorkerRefreshRequest) -> bool:
        """Is the lane's ambient authority EXACT and current, right now? (read-only)

        Re-joined immediately before each owed effect (the launch and the resume send). The
        boolean projection of :meth:`lane_authority_reason`. Fail-closed.
        """
        ...

    def worker_name_free_of_live_process(self, request: WorkerRefreshRequest) -> bool:
        """Is the worker's assigned name free of ANY live process? (read-only)

        The pre-launch collision fence: at that point the old worker is closed and the fresh
        one not yet launched, so ANY live process at the worker's assigned name is foreign.
        The lane GATEWAY lives at a different assigned name and never trips this. Fail-closed:
        an unreadable inventory returns ``False``.
        """
        ...

    def resume_rail_ready(self, request: WorkerRefreshRequest) -> bool:
        """Can THIS execution context deliver the anchor resume? (read-only, pre-close)

        Verified BEFORE the destructive close (the #14203 j#87364 F2 lesson) so a context that
        cannot resume is a typed up-front refusal, never a post-close ``stopped`` discovery.
        Fail-closed.
        """
        ...

    def resume_confirmed(self, continuation: ContinuationPointer) -> bool:
        """Has the resume's durable effect already landed? (fresh read, never a snapshot)"""
        ...

    def resume_once(self, continuation: ContinuationPointer) -> str:
        """Resume the EXISTING durable anchor once to the FRESH worker.

        Routes through the governed handoff rail — never a raw send, never a regenerated gate.
        Returns :data:`...fresh_coordinator_drain.DRAIN_SEND_OK` or an error token.
        """
        ...


class WorkerRefreshUseCase:
    """Read-only preflight + owner-approved atomic refresh of a live turn-ended worker."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        actuation_port: ExactGenerationActuatorPort,
        ops: WorkerRefreshOps,
        *,
        workspace_id: str,
        clock=_utc_now,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._actuation_port = actuation_port
        self._ops = ops
        self._workspace_id = norm(workspace_id)
        self._clock = clock
        self._ttl = lease_ttl_seconds

    def _lane_authority_reason(self, request: WorkerRefreshRequest) -> str:
        """The lane launch-authority axis (fail-closed, normalized).

        Read through the same evaluator the launch leg's authority fence uses, so the
        preflight verdict and the action-time effect cannot be backed by different logic. An
        ops adapter that raises is ``unknown``, which refuses — never a fabricated green axis.

        MAY be called more than once per run (the #14203 j#88485 / j#88498 discipline): always
        once before the verdict, and AGAIN only at an actuator or continuation refusal, so the
        reported reason names the ACTION-TIME state rather than the preflight-time observation
        the run no longer reflects. Each call is a fresh read; the result is never cached
        across the close boundary.
        """
        try:
            raw = self._ops.lane_authority_reason(request)
        except Exception:  # noqa: BLE001 - an unreadable authority is never "current"
            return LAUNCH_AUTHORITY_UNKNOWN
        return normalize_launch_authority_reason(raw)

    def run(self, request: WorkerRefreshRequest, *, execute: bool) -> WorkerRefreshOutcome:
        turn_obs = self._ops.observe_turn(request)
        turn_class = classify_worker_turn(turn_obs)
        turn_reason = normalize_turn_failure_reason(
            turn_obs.reason_token or request.reason_token
        )
        authority_reason = self._lane_authority_reason(request)
        observation = self._ops.observe_target(request).with_launch_authority(
            launch_authority_current(authority_reason)
        )
        verdict = decide_worker_refresh(observation, turn_class)
        authority_detail = (
            f" (lane launch authority: {authority_reason}; "
            f"{launch_authority_runbook(authority_reason)})"
            if verdict == WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY
            else ""
        )
        if not execute:
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_PREFLIGHT,
                turn_observation=turn_obs, observation=observation,
                detail=(
                    "preflight only; --execute requires a positive owner approval"
                    + authority_detail
                ),
                authority_reason=authority_reason,
            )
        if verdict == WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY:
            # The one blocker that must never fall through to the post-close resume admission:
            # it is a CURRENT-state fence on the lane, not the expected post-close
            # ``identity_unknown`` signal, so it stands and closes nothing.
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_REFUSED, executed=True,
                turn_observation=turn_obs, observation=observation,
                detail=f"target not actionable ({verdict}); zero close" + authority_detail,
                authority_reason=authority_reason,
            )
        if verdict != WORKER_REFRESH_ACTIONABLE:
            resumed = self._post_close_resume(
                request, turn_class, turn_reason, verdict, turn_obs, observation,
                authority_reason,
            )
            if resumed is not None:
                return resumed
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_REFUSED, executed=True,
                turn_observation=turn_obs, observation=observation,
                detail=f"target not actionable ({verdict}); zero close",
                authority_reason=authority_reason,
            )
        return self._execute(
            request, turn_class, turn_reason, verdict, turn_obs, observation,
            authority_reason,
        )

    # -- post-close resume admission (the #13806 correction, mirrored) --------

    def _post_close_resume(
        self,
        request: WorkerRefreshRequest,
        turn_class: str,
        turn_reason: str,
        verdict: str,
        turn_obs: WorkerTurnObservation,
        observation: WorkerRefreshObservation,
        authority_reason: str,
    ) -> Optional[WorkerRefreshOutcome]:
        """Admit + drive a post-close replay, or ``None`` when it is not a resume.

        Admission is closed to the ONE expected post-close signal — an ``identity_unknown``
        preflight (the exact old worker was closed and its pinned locator no longer resolves).
        Every other blocker is a real current-state fence that stands. It is a resume ONLY when
        a durable transaction for this EXACT approved refresh already committed the worker's
        close (its participant is past ``close_owed``). The effect authorities are re-joined
        action-time inside the actuator / continuation drive, never snapshotted here.
        """
        if norm(verdict) != WORKER_REFRESH_BLOCK_UNKNOWN:
            return None
        try:
            expected_action = worker_refresh_action_id(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, locator=request.locator,
                revision=request.worker_revision,
            )
        except ValueError:
            return None
        if norm(request.action_id) != expected_action:
            return None
        try:
            key = ReplacementTransactionKey(self._workspace_id, expected_action)
        except ValueError:
            return None
        current = self._store.get(key)
        if current is None:
            return None
        if not isinstance(request.action_generation, int) or isinstance(
            request.action_generation, bool
        ) or current.action_generation != request.action_generation:
            return None
        identity = (
            norm(request.lane), norm(request.role), norm(request.provider),
            norm(request.assigned_name),
        )
        stored = current.find_participant(identity)
        if stored is None or not worker_close_committed(stored.phase):
            return None
        outcome = self._execute(
            request, turn_class, turn_reason, verdict, turn_obs, observation,
            authority_reason,
        )
        return replace(outcome, post_close_resume=True)

    # -- execute -------------------------------------------------------------

    def _execute(
        self,
        request: WorkerRefreshRequest,
        turn_class: str,
        turn_reason: str,
        verdict: str,
        turn_obs: WorkerTurnObservation,
        observation: WorkerRefreshObservation,
        authority_reason: str,
    ) -> WorkerRefreshOutcome:
        def refused(detail: str) -> WorkerRefreshOutcome:
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_REFUSED, executed=True,
                turn_observation=turn_obs, observation=observation, detail=detail,
                authority_reason=authority_reason,
            )

        # 1. Positive durable owner approval + exact action id + generation + evidence,
        #    before any write.
        try:
            decision = DecisionPointer(
                source="redmine", issue_id=request.effective_anchor_issue,
                journal_id=norm(request.journal),
            )
        except DecisionPointerError:
            return refused("approval journal is not a complete Redmine pointer")
        try:
            expected_action = worker_refresh_action_id(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, locator=request.locator,
                revision=request.worker_revision,
            )
        except ValueError:
            return refused(
                "refresh inputs do not identify one exact worker generation (a non-empty "
                "worker inventory row revision is required)"
            )
        if norm(request.action_id) != expected_action:
            return refused("action id does not match the exact approved worker")
        if not isinstance(request.action_generation, int) or isinstance(
            request.action_generation, bool
        ) or request.action_generation < 1:
            return refused("approved generation is not a positive exact integer")
        if not norm(request.lane_revision) or not norm(request.lane_generation):
            return refused(
                "lane lifecycle revision / generation evidence is required for a "
                "destructive worker refresh; zero close"
            )
        # The resume rail's capability is verified BEFORE any write / close — a context that
        # cannot deliver the anchor resume is refused up front, never discovered as a
        # post-close ``stopped`` (the #14203 j#87364 F2 lesson).
        if not self._ops.resume_rail_ready(request):
            return refused(
                "the anchor-resume rail is not available from this execution context "
                "(resume_rail_unavailable); run from an attested pane context — zero close"
            )
        # The resume continuation: the EXISTING durable anchor (a journal DISTINCT from the
        # approval) + a closed resumable gate kind + the ONE fixed resume action.
        if norm(request.resume_gate) not in RESUMABLE_GATES:
            return refused(
                f"resume gate {norm(request.resume_gate)!r} is not a resumable durable "
                "gate kind; zero close"
            )
        try:
            continuation = ContinuationPointer(
                source="redmine", issue_id=request.effective_anchor_issue,
                journal_id=norm(request.resume_anchor_journal),
                expected_gate=norm(request.resume_gate),
                next_semantic_action=RESUME_VIA_CALLBACK_RECOVERY,
            )
        except ContinuationPointerError:
            return refused("resume anchor pointer is incomplete; zero close")
        try:
            worker = ParticipantPin(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, old_locator=request.locator,
                is_self=False, lane_revision=request.lane_revision,
                lane_generation=request.lane_generation,
            )
        except ParticipantPinError:
            return refused("approved worker pin is incomplete")
        try:
            key = ReplacementTransactionKey(self._workspace_id, expected_action)
        except ValueError:
            return refused("workspace / action identity is incomplete")
        gen = request.action_generation

        # 2. Plan (or idempotently resume) the non-self refresh transaction.
        plan = self._store.plan_transaction(
            key, action_generation=gen, decision=decision, continuation=continuation,
            participants=[worker],
        )
        if not plan.applied and plan.reason != CAS_ALREADY_DECLARED:
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_STOPPED, executed=True,
                turn_observation=turn_obs, observation=observation,
                detail=f"transaction plan refused ({plan.reason})",
                authority_reason=authority_reason,
            )
        current = self._store.get(key)
        if current is None:
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_STOPPED, executed=True,
                turn_observation=turn_obs, observation=observation,
                detail="transaction row vanished after plan",
                authority_reason=authority_reason,
            )
        # A pre-existing row at this key must be THIS exact approved generation + decision +
        # continuation AND the same single pinned worker — otherwise a different authority is
        # already acting on this slot. Zero actuation (no supersede path: a stuck zero-effect
        # row is an operator diagnosis, never silently re-anchored).
        stored = current.find_participant(worker.identity)
        if (
            current.action_generation != gen
            or current.decision != decision
            or current.continuation != continuation
            or len(current.participants) != 1
            or stored is None
            or stored.old_locator != worker.old_locator
            or stored.lane_revision != worker.lane_revision
            or stored.lane_generation != worker.lane_generation
        ):
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_REFUSED, executed=True,
                turn_observation=turn_obs, observation=observation,
                phase=current.phase, revision=current.revision,
                detail=(
                    "a different refresh authority is already in flight for this worker; "
                    "zero actuation"
                ),
                authority_reason=authority_reason,
            )

        # 3. Drive the guarded close → launch → attest (the tranche B actuator). The launch
        #    authority is re-joined action-time immediately before the launch effect: the
        #    exact lane authority AND the worker name free of any live (foreign) process.
        actuator = ReplacementActuatorUseCase(
            self._store, self._actuation_port, clock=self._clock,
            lease_ttl_seconds=self._ttl,
            preservation_policy=assess_worker_recovery_preservation,
            launch_authority=lambda _pin: (
                self._ops.resume_lane_authority(request)
                and self._ops.worker_name_free_of_live_process(request)
            ),
        )
        recov = actuator.drive_worker_recovery(
            key, holder=request.holder, expected_action_generation=gen,
        )
        after = self._store.get(key)
        worker_pin = after.find_participant(worker.identity) if after else None
        if recov.status != ACTUATION_RECOVERED:
            # The actuator collapses every launch-leg failure into the hardcoded
            # ``detail="launch"``, so the reason the port's fence actually raised is read from
            # the port as a typed value and carried BOTH as its own closed field and
            # (compatibly) inside the stop detail (Redmine #14480). Read once, here, so the
            # typed field and the rendered detail can never disagree.
            launch_reason = port_launch_failure_reason(self._actuation_port)
            stop_detail = launch_failure_detail(
                status=recov.status,
                detail=recov.detail,
                preservation_reasons=recov.preservation_reasons,
                reason=launch_reason,
            )
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=WORKER_REFRESH_STATUS_STOPPED, executed=True,
                turn_observation=turn_obs, observation=observation,
                refresh_status=recov.status,
                closed_old_worker=self._closed_old_worker(worker_pin),
                phase=after.phase if after else "", revision=after.revision if after else 0,
                preservation_reasons=tuple(recov.preservation_reasons),
                launch_failure_reason=launch_reason,
                detail=(
                    f"worker refresh stopped ({recov.status}"
                    + (f": {stop_detail}" if norm(stop_detail) else "")
                    + "); re-run resumes"
                ),
                # The actuator re-joins the lane authority AFTER the close, so a stop here may
                # be the authority MOVING mid-action. Reporting the preflight-time reason
                # would name a state that is no longer true.
                authority_reason=self._lane_authority_reason(request),
            )

        # 4. Fresh worker attested — drive the resume continuation exactly once through the
        #    shared drain authority (idempotency-first; record attempted BEFORE the send;
        #    action-time authority re-join; typed zero-send revert; never a blind resend).
        resume = drive_continuation_once(
            self._store, self._clock, key, holder=request.holder, gen=gen,
            authority_fn=lambda: self._ops.resume_lane_authority(request),
            send_fn=lambda: self._ops.resume_once(continuation),
            confirmed_fn=lambda: self._ops.resume_confirmed(continuation),
        )
        final = self._store.get(key)
        status = (
            WORKER_REFRESH_STATUS_COMPLETED
            if resume == CONTINUATION_CONFIRMED
            else WORKER_REFRESH_STATUS_STOPPED
        )
        return self._outcome(
            request, turn_class, turn_reason, verdict, status=status, executed=True,
            turn_observation=turn_obs, observation=observation,
            refresh_status=recov.status, resume_status=resume,
            closed_old_worker=self._closed_old_worker(worker_pin),
            fresh_slot_attested=True,
            phase=final.phase if final else "", revision=final.revision if final else 0,
            detail=(
                "worker refreshed and existing anchor resumed exactly once"
                if resume == CONTINUATION_CONFIRMED
                else f"worker refreshed; resume {resume} (no blind resend; re-run resumes)"
            ),
            authority_reason=(
                authority_reason
                if resume == CONTINUATION_CONFIRMED
                else self._lane_authority_reason(request)
            ),
        )

    @staticmethod
    def _closed_old_worker(worker_pin) -> bool:
        # The old exact worker was closed once the participant moved off close_owed.
        return worker_pin is not None and worker_pin.phase not in ("close_owed", "")

    # -- rendering -----------------------------------------------------------

    def _outcome(
        self,
        request: WorkerRefreshRequest,
        turn_class: str,
        turn_reason: str,
        verdict: str,
        *,
        status: str,
        executed: bool = False,
        turn_observation: Optional[WorkerTurnObservation] = None,
        observation: Optional[WorkerRefreshObservation] = None,
        refresh_status: str = "",
        resume_status: str = "",
        closed_old_worker: bool = False,
        fresh_slot_attested: bool = False,
        phase: str = "",
        revision: int = 0,
        detail: str = "",
        preservation_reasons: tuple[str, ...] = (),
        authority_reason: str = LAUNCH_AUTHORITY_UNKNOWN,
        launch_failure_reason: str = LAUNCH_FAILURE_NONE,
    ) -> WorkerRefreshOutcome:
        # The closed axis token + its runbook are TYPED fields on every outcome the surface
        # can return, not prose inside ``detail`` (the #14475 j#88477 F2 discipline).
        reason = normalize_launch_authority_reason(authority_reason)
        return WorkerRefreshOutcome(
            issue=norm(request.issue),
            lane=norm(request.lane),
            role=norm(request.role),
            turn_class=turn_class,
            turn_reason=turn_reason,
            verdict=verdict,
            status=status,
            executed=executed,
            refresh_status=refresh_status,
            resume_status=resume_status,
            closed_old_worker=closed_old_worker,
            fresh_slot_attested=fresh_slot_attested,
            phase=phase,
            revision=revision,
            detail=detail,
            turn_observation=(
                turn_observation.as_payload() if turn_observation is not None else None
            ),
            observation=observation.as_payload() if observation is not None else None,
            preservation_reasons=preservation_reasons,
            launch_authority_reason=reason,
            launch_authority_runbook=launch_authority_runbook(reason),
            # Normalized at the composer so EVERY return path yields a well-shaped token,
            # including the ones that never touch the launch leg (they pass the default).
            launch_failure_reason=normalize_launch_failure_reason(launch_failure_reason),
        )


__all__ = (
    "WORKER_REFRESH_STATUS_PREFLIGHT",
    "WORKER_REFRESH_STATUS_REFUSED",
    "WORKER_REFRESH_STATUS_COMPLETED",
    "WORKER_REFRESH_STATUS_STOPPED",
    "WorkerRefreshRequest",
    "WorkerRefreshOutcome",
    "WorkerRefreshOps",
    "WorkerRefreshUseCase",
)
