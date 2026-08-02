"""Public guarded gateway refresh surface (Redmine #14203).

The recovery #14203 found missing: a managed sublane's same-lane implementation_gateway ends a
provider turn immediately after a confirmed callback delivery, no expected durable gate lands,
the runtime keeps reporting a live settled ``turn_ended`` — and no public surface can refresh
exactly that gateway process (``sublane recover-stale`` protects the gateway by design;
``recover-pair`` requires a hibernated lane; ``hibernate`` is fail-closed on in-flight work;
raw backend operations are forbidden). This use case is that surface: ``sublane
recover-gateway``.

The default is a **read-only preflight**: classify the provider turn
(:func:`...domain.gateway_turn_recovery.classify_gateway_turn` — the durable journal is the
authority; an unconfirmed delivery / turn start is NEVER a failure) and the refresh target
(:func:`...domain.gateway_turn_recovery.decide_gateway_refresh` — ordered fail-closed gates
protecting the worker / default coordinator / foreign slot). ``--execute`` actuates ONLY with
a positive owner approval (a durable Redmine :class:`DecisionPointer` + the exact
``refresh-gateway:<…>`` action id + the immutable approved generation) AND the action-time
re-verification that the target is still the exact failed gateway.

The actuation is **atomic + resumable** (the #13806 tranche A/B machinery): it plans a
*non-self* replacement transaction whose sole participant is the gateway, drives it through
:meth:`...ReplacementActuatorUseCase.drive_worker_recovery` (the coordinator-alive non-self
topology — guarded exact-generation close → same-slot fresh launch → action-bound
attestation; the worktree, branch, worker slot, and durable route are untouched), and only
after the fresh gateway is attested drives the resume continuation exactly once through the
shared :func:`...replacement_continuation_drain.drive_continuation_once` authority. The
resume re-delivers the EXISTING durable anchor via the callback recovery rail — it never
regenerates an Implementation Request / Review Request (``ops.resume_once`` routes through
the ``sublane callback-recovery`` machinery, which additionally enforces at most one
notification per dispatch anchor and zero-sends on a landed gate / superseded round).

Every failure / partial refresh is durably recorded: the replacement transaction row holds
the replay fence (a re-run resumes; a crash between close and launch is admitted as a
post-close resume ONLY on the expected ``identity_unknown`` + a committed-close transaction),
and the typed outcome names the exact fence that stopped it. No blind resend, no raw backend
operation, no generic kill is reachable from this surface.
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_completion import (  # noqa: E501
    build_update_evidence_completion,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner_composition import (  # noqa: E501
    plan_participants_with_evidence,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    ContinuationPointerError,
    DecisionPointerError,
    ParticipantPinError,
    norm,
    participant_authority_matches,
    participant_with_stored_evidence,
    stored_evidence_is_foreign,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_UNKNOWN,
    launch_authority_current,
    launch_authority_runbook,
    normalize_launch_authority_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    GatewayRefreshObservation,
    GatewayTurnObservation,
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_LAUNCH_AUTHORITY,
    REFRESH_BLOCK_UNKNOWN,
    RESUMABLE_GATES,
    RESUME_VIA_CALLBACK_RECOVERY,
    classify_gateway_turn,
    decide_gateway_refresh,
    gateway_refresh_action_id,
    normalize_turn_failure_reason,
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

# -- refresh status vocabulary (closed) -----------------------------------------

#: Preflight only — no ``--execute`` was requested (read-only classification).
REFRESH_STATUS_PREFLIGHT = "preflight"
#: ``--execute`` refused before any actuation (a typed preflight blocker or an incomplete
#: owner approval) — zero close.
REFRESH_STATUS_REFUSED = "refused"
#: The guarded actuation ran and every leg completed: the gateway is refreshed (fresh slot
#: attested) AND the existing durable anchor's resume was driven to confirmed exactly once.
REFRESH_STATUS_COMPLETED = "completed"
#: The actuation ran but a leg stopped fail-closed; the durable transaction holds the replay
#: fence — a re-run resumes. The stopping leg's closed token is carried in the outcome.
REFRESH_STATUS_STOPPED = "stopped"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class GatewayRefreshRequest:
    """One approved gateway refresh request (the exact target + the owner approval)."""

    issue: str
    lane: str
    role: str
    provider: str
    assigned_name: str
    locator: str
    #: The Redmine journal id of the positive owner approval (``--execute`` only).
    journal: str = ""
    #: The exact ``refresh-gateway:<…>`` action id the approval names — re-derived and
    #: matched, never trusted verbatim.
    action_id: str = ""
    #: The immutable approved generation counter (>= 1). The transaction's authority token.
    action_generation: int = 0
    #: The LIVE GATEWAY INVENTORY row revision pinned at approval time (the
    #: ``generation_matches`` preflight axis; a distinct authority from the lane lifecycle
    #: below — the #13806 revision-authority-split lesson).
    gateway_revision: str = ""
    #: The LANE LIFECYCLE ``(revision, generation)`` pinned at approval time — the evidence
    #: the close-boundary preservation fence re-verifies against the live lifecycle store.
    lane_revision: str = ""
    lane_generation: str = ""
    #: The issue carrying the durable ANCHOR + approval journals — a SEPARATE authority
    #: from :attr:`issue` (review j#87364 F1): :attr:`issue` is the lane's OWNING issue (the
    #: destructive authorization boundary the issue-lane fence compares), while a child issue
    #: worked ON that lane (the #14203-on-#13490 topology) carries the anchors. Empty falls
    #: back to :attr:`issue` (a lane whose own issue carries the work).
    anchor_issue: str = ""
    #: The EXISTING durable anchor the fresh gateway must resume — the undelivered gate's
    #: Redmine journal id. A SEPARATE authority from :attr:`journal` (the approval): the
    #: resume re-delivers this anchor exactly once and never regenerates a gate.
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
        return f"refresh-gateway:{norm(self.action_id)}:g{int(self.action_generation)}"


@dataclass(frozen=True)
class GatewayRefreshOutcome:
    """The typed outcome the coordinator renders / gates on."""

    issue: str
    lane: str
    role: str
    #: The provider-turn classification (a closed ``TURN_CLASS_*`` token).
    turn_class: str
    #: The secret-safe turn-failure reason (a closed ``TURN_REASON_*`` token; ``unknown``
    #: whenever no structured evidence was injected — never inferred).
    turn_reason: str
    #: The refresh preflight verdict (a closed ``REFRESH_*`` token).
    verdict: str
    status: str
    executed: bool = False
    refresh_status: str = ""
    resume_status: str = ""
    closed_old_gateway: bool = False
    fresh_slot_attested: bool = False
    phase: str = ""
    revision: int = 0
    detail: str = ""
    turn_observation: Optional[dict[str, object]] = None
    observation: Optional[dict[str, bool]] = None
    preservation_reasons: tuple[str, ...] = ()
    #: Whether this --execute was admitted as a POST-CLOSE resume (the #13806 correction:
    #: close committed, launch owed — the pinned old gateway is expectedly absent).
    post_close_resume: bool = False
    #: The lane LAUNCH-authority axis as a closed
    #: :data:`...lane_launch_authority.LAUNCH_AUTHORITY_REASONS` token, and its secret-safe
    #: operator runbook (review j#88477 F2). Emitted as TYPED fields on EVERY outcome —
    #: preflight, refused, stopped, completed — so an operator / automation can select a
    #: recovery action without parsing :attr:`detail` prose. Both are axis-level facts: never
    #: a path, token value, branch, or identity.
    launch_authority_reason: str = LAUNCH_AUTHORITY_UNKNOWN
    launch_authority_runbook: str = ""
    #: WHY the action-bound launch leg fenced, as the closed token the fence itself raised
    #: (Redmine #14480). ``""`` whenever no launch fence fired — the launch succeeded, or the
    #: run stopped somewhere else entirely — so a consumer never has to read "field absent" as
    #: "launch was fine". A launch that failed with no typed reason reports
    #: :data:`...replacement_launch_failure.LAUNCH_FAILURE_UNTYPED`, which is a different
    #: statement from ``""``. Value-free by construction: an axis / fence name, never a path,
    #: locator, credential, or exception prose. :attr:`detail` keeps the compatibility
    #: ``launch:<reason>`` rendering for readers that predate this field.
    launch_failure_reason: str = LAUNCH_FAILURE_NONE

    @property
    def is_blocked(self) -> bool:
        if not self.executed:
            return False
        return self.status != REFRESH_STATUS_COMPLETED

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
            "closed_old_gateway": self.closed_old_gateway,
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
            # Redmine #14480: emitted on EVERY outcome (like the authority axis above), so
            # automation reads a value rather than inferring from a missing key. ``""`` is
            # rendered as ``None`` — "no launch fence fired" — matching the runbook field's
            # existing empty-to-null convention.
            "launch_failure_reason": self.launch_failure_reason or None,
        }


@runtime_checkable
class GatewayRecoveryOps(Protocol):
    """The injected observe + resume effects (faked in tests; live wiring is a follow-up)."""

    def observe_turn(self, request: GatewayRefreshRequest) -> GatewayTurnObservation:
        """Observe the delivered callback's provider turn (read-only, all-positive-fact).

        Resolves the callback outcome (positively ``sent``?), the turn-start observation,
        the FRESH settled runtime state, and a FRESH anchored+ordered durable re-read of
        whether the expected gate landed after the resume anchor. Every unreadable axis
        stays ``False`` (fail-closed classification).
        """
        ...

    def observe_target(self, request: GatewayRefreshRequest) -> GatewayRefreshObservation:
        """Observe the live pinned gateway slot (read-only, all-positive-fact)."""
        ...

    def lane_authority_reason(self, request: GatewayRefreshRequest) -> str:
        """WHICH lane launch-authority axis fails right now? (read-only)

        A closed :data:`...lane_launch_authority.LAUNCH_AUTHORITY_REASONS` token naming the
        first failing axis (``ok`` when every axis holds). This is the SINGLE evaluator behind
        both the pre-close preflight axis and :meth:`resume_lane_authority` — a preflight
        backed by a second implementation drifts away from the effect it predicts, which is
        how #14462 j#88463 closed a gateway it could never relaunch (Redmine #14475).
        """
        ...

    def resume_lane_authority(self, request: GatewayRefreshRequest) -> bool:
        """Is the lane's ambient authority EXACT and current, right now? (read-only)

        Re-joined immediately before each owed effect (the launch and the resume send):
        the LIVE lane lifecycle ``(revision, generation)`` must equal the approval's pinned
        evidence, the worktree token / branch must be exact, and the lane's WORKER slot must
        still hold its pinned identity (the refresh preserves it byte-for-byte). Fail-closed.

        The boolean projection of :meth:`lane_authority_reason`.
        """
        ...

    def replacement_store_admission(self, key, pin) -> Optional[str]:
        """May this replacement produce effects at all? (read-only, Redmine #14756 j#96848)

        A reason token refuses the whole action with zero close, zero participant CAS and
        zero launch; ``None`` permits. It is a REQUIRED member rather than an optional one
        because the fence it arms is invisible when absent: an ops implementation that simply
        omitted it would leave the actuator un-gated and every test would still pass. Making
        it part of the protocol turns "this path was not gated" from a silence into an error.
        """
        ...

    def gateway_name_free_of_live_process(self, request: GatewayRefreshRequest) -> bool:
        """Is the gateway's assigned name free of ANY live process? (read-only)

        The pre-launch collision fence: at that point the old gateway is closed and the
        fresh one not yet launched, so ANY live process at the gateway's assigned name is
        foreign. The lane WORKER lives at a different assigned name and never trips this.
        Fail-closed: an unreadable inventory returns ``False``.
        """
        ...

    def resume_rail_ready(self, request: GatewayRefreshRequest) -> bool:
        """Can THIS execution context deliver the anchor resume? (read-only, pre-close)

        Verified BEFORE the destructive close (review j#87364 F2) so a context that cannot
        resume — e.g. a shell without the attested launch-time sender identity — is a typed
        up-front refusal, never a post-close ``stopped`` discovery. Fail-closed.
        """
        ...

    def resume_confirmed(self, continuation: ContinuationPointer) -> bool:
        """Has the resume's durable effect already landed? (fresh read, never a snapshot)

        True when the existing anchor was already re-delivered (a durable callback outcome /
        recovery record for this anchor) OR the anchor's expected response landed — either
        way a further send would be a duplicate.
        """
        ...

    def resume_once(self, continuation: ContinuationPointer) -> str:
        """Resume the EXISTING durable anchor once via the callback recovery rail.

        Routes through the ``sublane callback-recovery`` machinery (which itself enforces
        at most one notification per dispatch anchor, historical / superseded / landed-gate
        zero-sends) — never a raw send, never a regenerated gate. Returns
        :data:`...fresh_coordinator_drain.DRAIN_SEND_OK` or an error token.
        """
        ...


class GatewayRefreshUseCase:
    """Read-only preflight + owner-approved atomic refresh of a failed lane gateway."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        actuation_port: ExactGenerationActuatorPort,
        ops: GatewayRecoveryOps,
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

    def _lane_authority_reason(self, request: GatewayRefreshRequest) -> str:
        """The lane launch-authority axis (fail-closed, normalized).

        Read through the same evaluator the launch leg's authority fence uses, so the
        preflight verdict and the action-time effect cannot be backed by different logic. An
        ops adapter that raises (or predates the #14475 protocol) is ``unknown``, which
        refuses — never a fabricated green axis.

        MAY be called more than once per run (review j#88485 / j#88498): always once before
        the verdict, to make the axis a pre-close preflight fence, and AGAIN only at an
        actuator or continuation refusal, so the reported reason names the ACTION-TIME state
        rather than the preflight-time observation the run no longer reflects. A run that
        completes re-reads nothing — it acted under the axis it reported. Each call is a fresh
        read; the result is never cached across the close boundary, which is exactly what
        would report ``ok`` for a lane whose authority moved mid-action (#14462 j#88463).
        """
        try:
            raw = self._ops.lane_authority_reason(request)
        except Exception:  # noqa: BLE001 - an unreadable authority is never "current"
            return LAUNCH_AUTHORITY_UNKNOWN
        return normalize_launch_authority_reason(raw)

    def run(self, request: GatewayRefreshRequest, *, execute: bool) -> GatewayRefreshOutcome:
        turn_obs = self._ops.observe_turn(request)
        turn_class = classify_gateway_turn(turn_obs)
        turn_reason = normalize_turn_failure_reason(
            turn_obs.reason_token or request.reason_token
        )
        # Redmine #14475: the lane LAUNCH authority is a preflight axis, joined from the ONE
        # evaluator before any verdict — so the read-only preflight reports the same fence the
        # post-close launch leg would hit, instead of reporting ``actionable`` for a lane that
        # can never be relaunched (#14462 j#88463).
        authority_reason = self._lane_authority_reason(request)
        observation = self._ops.observe_target(request).with_launch_authority(
            launch_authority_current(authority_reason)
        )
        verdict = decide_gateway_refresh(observation, turn_class)
        authority_detail = (
            f" (lane launch authority: {authority_reason}; "
            f"{launch_authority_runbook(authority_reason)})"
            if verdict == REFRESH_BLOCK_LAUNCH_AUTHORITY
            else ""
        )
        if not execute:
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=REFRESH_STATUS_PREFLIGHT,
                turn_observation=turn_obs, observation=observation,
                detail=(
                    "preflight only; --execute requires a positive owner approval"
                    + authority_detail
                ),
                authority_reason=authority_reason,
            )
        if verdict == REFRESH_BLOCK_LAUNCH_AUTHORITY:
            # The one blocker that must never fall through to the post-close resume admission:
            # it is a CURRENT-state fence on the lane, not the expected post-close
            # ``identity_unknown`` signal, so it stands and closes nothing.
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=REFRESH_STATUS_REFUSED, executed=True,
                turn_observation=turn_obs, observation=observation,
                detail=(
                    f"target not actionable ({verdict}); zero close" + authority_detail
                ),
                authority_reason=authority_reason,
            )
        if verdict != REFRESH_ACTIONABLE:
            resumed = self._post_close_resume(
                request, turn_class, turn_reason, verdict, turn_obs, observation,
                authority_reason,
            )
            if resumed is not None:
                return resumed
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=REFRESH_STATUS_REFUSED, executed=True,
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
        request: GatewayRefreshRequest,
        turn_class: str,
        turn_reason: str,
        verdict: str,
        turn_obs: GatewayTurnObservation,
        observation: GatewayRefreshObservation,
        authority_reason: str,
    ) -> Optional[GatewayRefreshOutcome]:
        """Admit + drive a post-close replay, or ``None`` when it is not a resume.

        Admission is closed to the ONE expected post-close signal — an ``identity_unknown``
        preflight (the exact old gateway was closed and its pinned locator no longer
        resolves). Every other blocker is a real current-state fence that stands. It is a
        resume ONLY when a durable transaction for this EXACT approved refresh already
        committed the gateway's close (its participant is past ``close_owed``). The effect
        authorities are re-joined action-time inside the actuator / continuation drive, never
        snapshotted here.
        """
        if norm(verdict) != REFRESH_BLOCK_UNKNOWN:
            return None
        try:
            expected_action = gateway_refresh_action_id(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, locator=request.locator,
                revision=request.gateway_revision,
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
        request: GatewayRefreshRequest,
        turn_class: str,
        turn_reason: str,
        verdict: str,
        turn_obs: GatewayTurnObservation,
        observation: GatewayRefreshObservation,
        authority_reason: str,
    ) -> GatewayRefreshOutcome:
        def refused(detail: str) -> GatewayRefreshOutcome:
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=REFRESH_STATUS_REFUSED, executed=True,
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
            expected_action = gateway_refresh_action_id(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, locator=request.locator,
                revision=request.gateway_revision,
            )
        except ValueError:
            return refused(
                "refresh inputs do not identify one exact gateway generation (a non-empty "
                "gateway inventory row revision is required)"
            )
        if norm(request.action_id) != expected_action:
            return refused("action id does not match the exact approved gateway")
        if not isinstance(request.action_generation, int) or isinstance(
            request.action_generation, bool
        ) or request.action_generation < 1:
            return refused("approved generation is not a positive exact integer")
        if not norm(request.lane_revision) or not norm(request.lane_generation):
            return refused(
                "lane lifecycle revision / generation evidence is required for a "
                "destructive gateway refresh; zero close"
            )
        # Review j#87364 F2: the resume rail's capability is verified BEFORE any write /
        # close — a context that cannot deliver the anchor resume (e.g. a sender-identity-
        # less shell) is refused up front, never discovered as a post-close ``stopped``.
        if not self._ops.resume_rail_ready(request):
            return refused(
                "the anchor-resume rail is not available from this execution context "
                "(resume_rail_unavailable); run from an attested pane context — zero close"
            )
        # The resume continuation: the EXISTING durable anchor (a journal DISTINCT from the
        # approval) + a closed resumable gate kind + the ONE fixed resume action. A refresh
        # whose continuation cannot name the exact anchor to resume never closes anything.
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
            gateway = ParticipantPin(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, old_locator=request.locator,
                is_self=False, lane_revision=request.lane_revision,
                lane_generation=request.lane_generation,
            )
        except ParticipantPinError:
            return refused("approved gateway pin is incomplete")
        try:
            key = ReplacementTransactionKey(self._workspace_id, expected_action)
        except ValueError:
            return refused("workspace / action identity is incomplete")
        gen = request.action_generation

        # 1b. A FRESH transaction pins its update evidence through the ONE planner
        # authority (j#97093 decision 2); a PROGRESSED one resumes on the manifest it
        # already pinned (j#97121). After the close, the current launch generation may have
        # rotated and the bound evidence may have been consumed -- both legitimate, both
        # AFTER this transaction recorded what it was acting on -- so re-reading them here
        # would let ordinary external progress refuse a replay nobody re-authorised.
        existing = self._store.get(key)
        if existing is None:
            planning = plan_participants_with_evidence(
                [gateway], home=self._store.path.parent,
                workspace_id=self._workspace_id, lane_id=gateway.lane_id,
            )
            if planning.refused:
                # Zero-effect: before the plan, before any actuation.
                return refused(f"update evidence planning refused ({planning.refusal})")
            gateway = planning.participants[0]

            # 2. Plan the non-self refresh transaction.
            plan = self._store.plan_transaction(
                key, action_generation=gen, decision=decision, continuation=continuation,
                participants=[gateway],
            )
            if not plan.applied and plan.reason != CAS_ALREADY_DECLARED:
                return self._outcome(
                    request, turn_class, turn_reason, verdict,
                    status=REFRESH_STATUS_STOPPED, executed=True,
                    turn_observation=turn_obs, observation=observation,
                    detail=f"transaction plan refused ({plan.reason})",
                    authority_reason=authority_reason,
                )
        else:
            # The request-built pin wearing the stored triplet -- and nothing else from the
            # stored row. The whole-participant comparison below is what decides whether
            # this really is the same action; adopting the evidence does not weaken it.
            stored_pin = existing.find_participant(gateway.identity)
            if stored_evidence_is_foreign(stored_pin, workspace_id=self._workspace_id):
                return refused(
                    "the stored update evidence names another workspace; zero actuation"
                )
            gateway = participant_with_stored_evidence(gateway, stored_pin)
        current = self._store.get(key)
        if current is None:
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=REFRESH_STATUS_STOPPED, executed=True,
                turn_observation=turn_obs, observation=observation,
                detail="transaction row vanished after plan",
                authority_reason=authority_reason,
            )
        # A pre-existing row at this key must be THIS exact approved generation + decision +
        # continuation AND the same single pinned gateway — otherwise a different authority is
        # already acting on this slot. Zero actuation (no supersede path in v1: a stuck
        # zero-effect row is an operator diagnosis, never silently re-anchored).
        stored = current.find_participant(gateway.identity)
        if (
            current.action_generation != gen
            or current.decision != decision
            or current.continuation != continuation
            or len(current.participants) != 1
            or not participant_authority_matches(stored, gateway)
        ):
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=REFRESH_STATUS_REFUSED, executed=True,
                turn_observation=turn_obs, observation=observation,
                phase=current.phase, revision=current.revision,
                detail=(
                    "a different refresh authority is already in flight for this gateway; "
                    "zero actuation"
                ),
                authority_reason=authority_reason,
            )

        # 3. Drive the guarded close → launch → attest (the tranche B actuator). The launch
        #    authority is re-joined action-time immediately before the launch effect: the
        #    exact lane authority AND the gateway name free of any live (foreign) process.
        actuator = ReplacementActuatorUseCase(
            self._store, self._actuation_port, clock=self._clock,
            lease_ttl_seconds=self._ttl,
            preservation_policy=assess_worker_recovery_preservation,
            launch_authority=lambda _pin: (
                self._ops.resume_lane_authority(request)
                and self._ops.gateway_name_free_of_live_process(request)
            ),
            store_admission=self._ops.replacement_store_admission,
            # Bound to the SAME explicit home the planner read from, so what a plan armed
            # and what a completion discharges cannot address different stores.
            evidence_completion=build_update_evidence_completion(self._store.path.parent),
        )
        recov = actuator.drive_worker_recovery(
            key, holder=request.holder, expected_action_generation=gen,
        )
        after = self._store.get(key)
        gateway_pin = after.find_participant(gateway.identity) if after else None
        if recov.status != ACTUATION_RECOVERED:
            # Redmine #14480: the actuator collapses every launch-leg failure into the
            # hardcoded ``detail="launch"``, so the reason the port's fence actually raised is
            # read from the port as a typed value and carried BOTH as its own closed field and
            # (compatibly) inside the stop detail. Read once, here, so the typed field and the
            # rendered detail can never disagree about what stopped the launch.
            launch_reason = port_launch_failure_reason(self._actuation_port)
            stop_detail = launch_failure_detail(
                status=recov.status,
                detail=recov.detail,
                preservation_reasons=recov.preservation_reasons,
                reason=launch_reason,
            )
            return self._outcome(
                request, turn_class, turn_reason, verdict,
                status=REFRESH_STATUS_STOPPED, executed=True,
                turn_observation=turn_obs, observation=observation,
                refresh_status=recov.status,
                closed_old_gateway=self._closed_old_gateway(gateway_pin),
                phase=after.phase if after else "", revision=after.revision if after else 0,
                preservation_reasons=tuple(recov.preservation_reasons),
                launch_failure_reason=launch_reason,
                detail=(
                    f"gateway refresh stopped ({recov.status}"
                    + (f": {stop_detail}" if norm(stop_detail) else "")
                    + "); re-run resumes"
                ),
                # Review j#88485: the actuator re-joins the lane authority AFTER the close, so
                # a stop here may be the authority MOVING mid-action (the exact #14462 j#88463
                # transition). Reporting the preflight-time reason would name a state that is
                # no longer true. Re-read the canonical evaluator and report the action-time
                # axis — which stays ``ok`` when the stop had some other cause (e.g. the
                # gateway's assigned name occupied by a foreign live process).
                authority_reason=self._lane_authority_reason(request),
            )

        # 4. Fresh gateway attested — drive the resume continuation exactly once through the
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
            REFRESH_STATUS_COMPLETED
            if resume == CONTINUATION_CONFIRMED
            else REFRESH_STATUS_STOPPED
        )
        return self._outcome(
            request, turn_class, turn_reason, verdict, status=status, executed=True,
            turn_observation=turn_obs, observation=observation,
            refresh_status=recov.status, resume_status=resume,
            closed_old_gateway=self._closed_old_gateway(gateway_pin),
            fresh_slot_attested=True,
            phase=final.phase if final else "", revision=final.revision if final else 0,
            detail=(
                "gateway refreshed and existing anchor resumed exactly once"
                if resume == CONTINUATION_CONFIRMED
                else f"gateway refreshed; resume {resume} (no blind resend; re-run resumes)"
            ),
            # The resume leg re-joins the same authority immediately before the send, so a
            # stopped continuation gets the action-time axis too (review j#88485). A confirmed
            # resume re-reads to the same ``ok`` it acted under.
            authority_reason=(
                authority_reason
                if resume == CONTINUATION_CONFIRMED
                else self._lane_authority_reason(request)
            ),
        )

    @staticmethod
    def _closed_old_gateway(gateway_pin) -> bool:
        # The old exact gateway was closed once the participant moved off close_owed.
        return gateway_pin is not None and gateway_pin.phase not in ("close_owed", "")

    # -- rendering -----------------------------------------------------------

    def _outcome(
        self,
        request: GatewayRefreshRequest,
        turn_class: str,
        turn_reason: str,
        verdict: str,
        *,
        status: str,
        executed: bool = False,
        turn_observation: Optional[GatewayTurnObservation] = None,
        observation: Optional[GatewayRefreshObservation] = None,
        refresh_status: str = "",
        resume_status: str = "",
        closed_old_gateway: bool = False,
        fresh_slot_attested: bool = False,
        phase: str = "",
        revision: int = 0,
        detail: str = "",
        preservation_reasons: tuple[str, ...] = (),
        authority_reason: str = LAUNCH_AUTHORITY_UNKNOWN,
        launch_failure_reason: str = LAUNCH_FAILURE_NONE,
    ) -> GatewayRefreshOutcome:
        # Review j#88477 F2: the closed axis token + its runbook are TYPED fields on every
        # outcome the surface can return, not prose inside ``detail``.
        reason = normalize_launch_authority_reason(authority_reason)
        return GatewayRefreshOutcome(
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
            closed_old_gateway=closed_old_gateway,
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
    "REFRESH_STATUS_PREFLIGHT",
    "REFRESH_STATUS_REFUSED",
    "REFRESH_STATUS_COMPLETED",
    "REFRESH_STATUS_STOPPED",
    "GatewayRefreshRequest",
    "GatewayRefreshOutcome",
    "GatewayRecoveryOps",
    "GatewayRefreshUseCase",
)
