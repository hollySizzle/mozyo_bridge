"""Public stale standard-sublane worker recovery surface (Redmine #13806 tranche D).

The coordinator-facing entry the residual j#79435 found missing: ``herdr session-start`` only
*reports* a ``stale_named_slot`` read-only, and ``sublane quarantine`` handles only a pending
composer — neither recovers a standard-sublane worker whose process vanished after a turn,
leaving an Implementation Done / Review Request diff un-durable-ized. This use case is that
recovery, connected to the existing tranche A/B/C primitives (Implementation Request j#79485).

The default is a **read-only preflight** (:func:`...domain.stale_worker_recovery.decide_recovery`
over a live observation). ``--execute`` actuates ONLY with a positive owner approval (a durable
Redmine :class:`DecisionPointer` + the exact ``recover:<…>`` action id + the immutable approved
generation) AND an action-time re-verification that the target is still the exact stale worker.
Any productive-provider / tool-child, unknown, wrong-issue-lane, stale-generation, gateway /
foreign, or unreadable-worktree observation is a zero-close typed blocker.

The actuation is **atomic + resumable**: it plans (or resumes) a *non-self* replacement
transaction whose sole participant is the stale worker, drives it through the tranche B
actuator's :meth:`...ReplacementActuatorUseCase.drive_worker_recovery` (guarded close → same-slot
fresh launch → action-bound attestation, byte-preserving the worktree — never reset / stash /
recreate / delete), and only after the fresh receiver is attested redispatches the ORIGINAL
durable gate exactly once (``replacing_nonself -> draining_continuation -> completed``, reusing
the tranche C drain's "record attempted before the send, never blind-resend" discipline). It
never closes the lane gateway or any foreign slot, never touches the current coordinator, and
never promotes an ACK / queue-enter to task completion.

The live observation / actuation adapter is deliberately NOT shipped here (the tranche A/B/C
precedent — live process mutation is non-scope, j#79121 / j#79485 boundary): the ports are
injected and faked in tests, and the CLI wires a fail-closed staged seam until a follow-up
lands the live wiring.
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
    CAS_GENERATION_MISMATCH,
    CAS_LEASE_NOT_HELD,
    ContinuationPointerError,
    DecisionPointerError,
    ParticipantPinError,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
    norm,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    DEFAULT_LEASE_TTL_SECONDS,
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (  # noqa: E501
    ExactGenerationActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_RECOVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.session_replacement_reconcile import (  # noqa: E501
    drain_state_for,
    may_attempt_drain,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    RECOVER_ACTIONABLE,
    RECOVER_BLOCK_UNKNOWN,
    RecoveryObservation,
    decide_recovery,
    stale_worker_recovery_action_id,
    worker_close_committed,
)

# -- recovery / redispatch status vocabulary (closed) ---------------------------

#: The ONLY gate kind a worker recovery may redispatch (Redmine #13806 R3-F1). The governed
#: same-lane worker-forward rail (``handoff send --kind implementation_request``) delivers an
#: implementation_request to the worker, so the continuation pointer's ``expected_gate`` must be
#: exactly this — a pointer naming any other gate is a zero-send typed blocker (the send kind,
#: the redispatch marker kind, and the pointer's gate kind are thereby all one closed token).
RECOVERY_REDISPATCH_GATE = "implementation_request"

#: The ONLY continuation semantic action a worker recovery may drive (Redmine #13806 R4-F1).
#: The redispatch performs a fixed "dispatch the gate to the fresh worker exactly once" effect,
#: so the pointer's ``next_semantic_action`` must name exactly that — a pointer declaring any
#: other action would let the transaction header point at one action while a different fixed
#: effect runs. Fenced together with :data:`RECOVERY_REDISPATCH_GATE` before any close / send.
RECOVERY_REDISPATCH_ACTION = "dispatch_once"

#: Preflight only — no ``--execute`` was requested (read-only classification).
RECOVERY_PREFLIGHT = "preflight"
#: ``--execute`` refused before any actuation because the target is not actionable (a typed
#: preflight blocker) or the owner approval was incomplete — zero close.
RECOVERY_REFUSED = "refused"
#: The guarded actuation ran and every leg completed: the worker is replaced AND the original
#: gate was redispatched exactly once (the transaction is ``completed``).
RECOVERY_COMPLETED = "completed"
#: The actuation ran but a leg stopped fail-closed (lease lost / effect failed / attestation
#: pending / preservation / etc.); the durable transaction holds the replay fence — a re-run
#: resumes. The underlying actuation / redispatch status is carried in ``detail``.
RECOVERY_STOPPED = "stopped"

#: Redispatch leg status (a closed vocabulary), riding the tranche C drain discipline.
REDISPATCH_CONFIRMED = "confirmed"
REDISPATCH_UNCERTAIN = "uncertain"
REDISPATCH_SEND_FAILED = "send_failed"
REDISPATCH_LEASE_LOST = "lease_lost"
REDISPATCH_GENERATION_MISMATCH = "generation_mismatch"
REDISPATCH_NOT_FOUND = "not_found"
REDISPATCH_CONTINUATION_UNREADABLE = "continuation_unreadable"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RecoveryRequest:
    """One approved stale-worker recovery request (the exact target + the owner approval)."""

    issue: str
    lane: str
    role: str
    provider: str
    assigned_name: str
    locator: str
    #: The Redmine journal id of the positive owner approval (``--execute`` only).
    journal: str = ""
    #: The exact ``recover:<lane>:<role>:<provider>:<assigned_name>:<locator>`` action id the
    #: approval names — re-derived and matched, never trusted verbatim.
    action_id: str = ""
    #: The immutable approved generation counter (>= 1). The transaction's authority token.
    action_generation: int = 0
    #: The LIVE WORKER INVENTORY row revision pinned at approval time — a *distinct* authority
    #: from the lane lifecycle below (Redmine #13806 recover-stale revision-authority split).
    #: The preflight ``generation_matches`` gate compares this to the live worker row's own
    #: ``revision`` (a same-name recycle at a bumped row revision is a stale generation). Empty
    #: matches any present row revision (the row shape may not carry one). This is NOT the lane
    #: lifecycle revision: conflating the two left an installed binary unable to satisfy both
    #: fences with one field.
    worker_revision: str = ""
    #: The LANE LIFECYCLE ``(revision, generation)`` pinned at approval time — the evidence the
    #: close-boundary preservation fence re-verifies against the live lane lifecycle store. A
    #: separate authority from :attr:`worker_revision`; the two are compared to two different
    #: live sources and must be pinned independently.
    lane_revision: str = ""
    lane_generation: str = ""
    #: Owner-approved convergence of a stuck same-action transaction (Redmine #13806): when the
    #: durable transaction was pinned to mis-bound lane-lifecycle evidence by an earlier
    #: (installed) run, a corrected re-run trips the authority-conflict fence. With ``supersede``
    #: AND a strictly-greater :attr:`action_generation`, the recovery re-anchors that row to the
    #: corrected evidence at the new generation — but ONLY while it has actuated nothing
    #: (zero close / launch / send). Never a raw-DB edit; never past an actuated fence.
    supersede: bool = False
    #: The durable gate the coordinator must find + the one semantic action to redispatch once.
    expected_gate: str = ""
    next_semantic_action: str = ""
    #: The owner's durable RE-approval journal for a post-close resume — a SEPARATE authority
    #: from :attr:`journal` (Redmine #13806 post-close correction §5). ``journal`` is the
    #: transaction's immutable stored decision / continuation anchor (the same-action CAS
    #: identity): a resume must present that ORIGINAL journal to match the durable row. A fresh
    #: owner re-approval of the resume therefore cannot be forced through the same ``--journal``
    #: without tripping the divergence / supersede fence — so this distinct pointer carries it.
    #: When present it is validated as a complete Redmine pointer and recorded as the resume
    #: authority; it never overwrites the stored decision / continuation anchor. Empty falls back
    #: to the stored anchor (a same-journal resume), preserving the original single-anchor flow.
    resume_journal: str = ""

    @property
    def holder(self) -> str:
        """The stable, action-bound lease identity for this recovery (resume-safe)."""
        return f"recover:{norm(self.action_id)}:g{int(self.action_generation)}"


@dataclass(frozen=True)
class RecoveryOutcome:
    """The typed outcome the coordinator renders / gates on."""

    issue: str
    lane: str
    role: str
    verdict: str
    status: str
    executed: bool = False
    recovery_status: str = ""
    redispatch_status: str = ""
    closed_old_worker: bool = False
    fresh_slot_attested: bool = False
    phase: str = ""
    revision: int = 0
    detail: str = ""
    observation: Optional[dict[str, bool]] = None
    #: The closed preservation reason(s) that fenced a close (Redmine #13806): the exact
    #: :data:`PRESERVE_*` tokens (identity_mismatch / running_process / pending_approval), not a
    #: generic ``preservation_blocked`` — so the durable record names which fence stopped it and
    #: on which comparison axis (carried in ``detail``). Empty unless a preservation fence fired.
    preservation_reasons: tuple[str, ...] = ()
    #: Whether this --execute re-anchored a stuck same-action transaction to a new generation
    #: before driving (the owner-approved supersede convergence). Diagnostic only.
    converged_supersede: bool = False
    #: Whether this --execute was admitted as a POST-CLOSE resume (Redmine #13806 post-close
    #: correction): the fresh-recovery preflight blocked (the closed old worker is expectedly
    #: absent) but a durable transaction that already committed the close drove the owed launch /
    #: attest / redispatch. Diagnostic — ``verdict`` still carries the honest preflight blocker.
    post_close_resume: bool = False
    #: The resume RE-approval journal that governed a post-close resume, when one was supplied
    #: distinct from the stored anchor (§5). Empty for a fresh execute or a same-journal resume.
    resume_authorization: str = ""

    @property
    def is_blocked(self) -> bool:
        # A read-only preflight is never "blocked" — it is a report. An --execute is blocked
        # unless every leg completed (worker replaced AND gate redispatched exactly once).
        if not self.executed:
            return False
        return self.status != RECOVERY_COMPLETED

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "role": self.role,
            "verdict": self.verdict,
            "status": self.status,
            "executed": self.executed,
            "recovery_status": self.recovery_status or None,
            "redispatch_status": self.redispatch_status or None,
            "closed_old_worker": self.closed_old_worker,
            "fresh_slot_attested": self.fresh_slot_attested,
            "phase": self.phase or None,
            "revision": self.revision,
            "is_blocked": self.is_blocked,
            "detail": self.detail,
            "observation": self.observation,
            "preservation_reasons": list(self.preservation_reasons),
            "converged_supersede": self.converged_supersede,
            "post_close_resume": self.post_close_resume,
            "resume_authorization": self.resume_authorization or None,
        }


@runtime_checkable
class StaleWorkerRecoveryOps(Protocol):
    """The injected observe + redispatch effects (faked in tests; live wiring is a follow-up)."""

    def observe_target(self, request: RecoveryRequest) -> RecoveryObservation:
        """Observe the live pinned worker — the positive facts :func:`decide_recovery` reads.

        Read-only: it resolves the exact ``(workspace, lane, issue, provider, assigned_name,
        locator)`` slot against the live inventory + attestation and returns a
        :class:`RecoveryObservation` whose every field defaults to the unsafe side, so an
        unreadable / ambiguous inventory classifies as ``identity_unknown`` (never launched
        blind).
        """
        ...

    def redispatch_gate(self, continuation: ContinuationPointer) -> str:
        """Redispatch the ORIGINAL durable gate to the fresh worker (high-level, once).

        Re-reads the source journal (``continuation``) to reconstruct the Implementation
        Request / gate and sends it ONCE under the same-lane gateway ownership. Returns
        :data:`DRAIN_SEND_OK` / an error token. Landing is NOT implied by the send — the
        caller confirms it via :meth:`gate_redispatched`. Never promotes an ACK / queue-enter
        to task completion.
        """
        ...

    def gate_redispatched(self, continuation: ContinuationPointer) -> bool:
        """Has the original gate already landed on the fresh worker's durable inbox / gate?

        The idempotency check that lets a resume distinguish confirmed from still-needed
        without a blind resend.
        """
        ...

    def lane_lifecycle_current(self, request: RecoveryRequest) -> bool:
        """Does the LIVE lane lifecycle still match the approval's pinned generation? (read-only)

        The ambient lane authority a **post-close resume** must re-verify before it drives any
        owed launch / send (Redmine #13806 post-close correction §3). A post-close replay re-runs
        against a durable transaction whose close already committed, so it cannot rely on the
        pinned old worker's identity (that slot is gone). This re-reads the LIVE lane lifecycle
        ``(revision, generation)`` — old-slot-independent, and not confused by the fresh
        relaunched slot — and returns ``True`` only when it still equals the approval's pinned
        ``lane_revision`` / ``lane_generation``. A moved / newer lifecycle (the coordinator
        re-owned or advanced the lane) or an unreadable / absent lifecycle returns ``False`` so
        the resume stops with zero launch / send rather than relaunching into a lane the
        approval no longer governs. Never mutates the #13810 owner row — it only compares.
        """
        ...

    def lane_worktree_readable(self, request: RecoveryRequest) -> bool:
        """Is the lane's recovery worktree currently READABLE? (read-only, Redmine #13806 R3-F1)

        The second ambient fence a **post-close resume** re-verifies before any owed launch /
        send: byte preservation requires a *readable* worktree to relaunch the fresh worker into.
        A post-close replay observes the old worker as absent (``identity_unknown``), so the entry
        preflight's ``worktree_readable`` gate is short-circuited and never re-checked — this
        re-reads the lane's worktree directly (old-slot-independent) and returns ``True`` only when
        it resolves. Fail-closed: an unreadable worktree returns ``False`` so the resume stops zero
        launch / send. A **dirty (but readable)** worktree is NOT unreadable — it is byte-preserved
        and recovered, exactly as a fresh recovery does (the tranche D contract, IR j#79485 §4 /
        ``assess_worker_recovery_preservation``); this fence is readability only, never a dirty
        block.
        """
        ...

    def lane_free_of_foreign_live(self, request: RecoveryRequest) -> bool:
        """Is the lane free of a foreign PRODUCTIVE live process? (read-only, Redmine #13806 R3-F1)

        The third ambient fence a **post-close resume** re-verifies (Design Consultation Answer
        j#82708 block list): a *productive* live process (a busy provider / running tool-child)
        occupying the lane's assigned name is foreign work the relaunch must never collide with.
        Old-slot-independent: it scans for a productive live row at the lane's assigned name, not
        the (absent) pinned old locator. The freshly relaunched recovery worker is IDLE (awaiting
        its redispatch), not productive, so it never trips this fence — only genuine foreign work
        does. An IDLE stale residue at the name is exactly what recover-stale recovers and is NOT
        a foreign-live block. Returns ``True`` when the lane is free (safe to launch); ``False``
        (fail-closed) when a productive live process is present or the inventory is unreadable.
        """
        ...


class StaleWorkerRecoveryUseCase:
    """Read-only preflight + owner-approved atomic recovery of a stale sublane worker."""

    def __init__(
        self,
        store: ReplacementTransactionStore,
        actuation_port: ExactGenerationActuatorPort,
        ops: StaleWorkerRecoveryOps,
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

    def run(self, request: RecoveryRequest, *, execute: bool) -> RecoveryOutcome:
        observation = self._ops.observe_target(request)
        verdict = decide_recovery(observation)
        if not execute:
            return self._outcome(
                request, verdict, status=RECOVERY_PREFLIGHT, observation=observation,
                detail="preflight only; --execute requires a positive owner approval",
            )
        # --execute: the target must be exactly the stale worker the approval names.
        if verdict != RECOVER_ACTIONABLE:
            # A POST-CLOSE resume (Redmine #13806 close-success → launch-failure → replay): the
            # fresh-recovery preflight cannot resolve the pinned OLD worker because the recovery
            # already CLOSED it — that absence is the expected post-close state, not a real
            # blocker. Route the replay to the durable owed transaction ONLY when one that
            # already committed this worker's close exists for THIS exact approved recovery;
            # otherwise the block stands (a fresh unknown identity never plans / launches blind).
            resumed = self._post_close_resume(request, verdict, observation)
            if resumed is not None:
                return resumed
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail=f"target not actionable ({verdict}); zero close",
            )
        return self._execute(request, verdict, observation)

    # -- post-close resume admission -----------------------------------------

    def _post_close_resume(
        self, request: RecoveryRequest, verdict: str, observation: RecoveryObservation
    ) -> Optional[RecoveryOutcome]:
        """Admit + drive a post-close replay, or ``None`` when it is not a resume.

        Admission is closed to the ONE expected post-close signal — an ``identity_unknown``
        preflight (the exact old worker was closed and its pinned locator no longer resolves)
        — Redmine #13806 post-close correction R3-F1. Every OTHER blocker verdict means the old
        worker DID resolve and a genuine current-state fence fired (an unreadable / dirty
        worktree, a stale generation, a productive provider, a gateway / foreign slot, a wrong
        issue-lane, a competing authority); that block is real and must stand — a resume never
        bypasses it. It is then a *resume* — never a fresh plan, never a blind launch — ONLY
        when a durable transaction for this EXACT approved recovery already committed the
        worker's close (its participant is past ``close_owed``).

        Before the replay is handed to :meth:`_execute`, the ambient lane authority the fresh
        launch depends on is **re-verified** (R3-F1): the live lane lifecycle must still match
        the approval's pinned generation. A moved / newer / unreadable lifecycle stops the
        resume with zero launch / send rather than relaunching into a lane the approval no
        longer governs (the lease authority is re-verified inside the actuator before every
        effect; the worktree-readability fence is the ``identity_unknown``-only admission plus a
        launch that fails closed on an unreadable worktree). Returns ``None`` (the caller's
        block stands) for every non-resume case: a non-``identity_unknown`` block, no such
        transaction, a different generation, or a participant still at ``close_owed``.
        """
        if norm(verdict) != RECOVER_BLOCK_UNKNOWN:
            # Only the expected old-locator absence admits a resume. Any other blocker is a real
            # current-state fence (worktree unreadable / stale generation / gateway / etc.) that
            # the resume must not bypass (R3-F1). The caller's block stands.
            return None
        try:
            expected_action = stale_worker_recovery_action_id(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, locator=request.locator,
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
        # The stored transaction must be THIS exact approved generation — a different generation
        # is a foreign / superseding authority, never resumed past the block (the full pointer /
        # evidence signature is re-verified inside _execute; the generation is the coarse gate
        # that keeps a wrong-generation replay from being admitted as a resume at all).
        if not isinstance(request.action_generation, int) or isinstance(
            request.action_generation, bool
        ) or current.action_generation != request.action_generation:
            return None
        # The pinned worker must already have committed its close (past close_owed). A
        # close_owed / absent participant is a fresh recovery whose preflight block is real.
        worker_identity = (
            norm(request.lane), norm(request.role), norm(request.provider),
            norm(request.assigned_name),
        )
        stored_worker = current.find_participant(worker_identity)
        if stored_worker is None or not worker_close_committed(stored_worker.phase):
            return None
        # R3-F1 — re-verify the ambient lane authority before driving any owed launch / send.
        # The durable transaction says the close committed, but the live lane lifecycle may have
        # moved since (a coordinator re-owned / advanced the lane); relaunching a fresh worker
        # into a lane the approval no longer governs is exactly what IR §3 (newer lifecycle ->
        # zero-launch/zero-send) forbids. Each resume command re-checks it fresh (old-slot- and
        # fresh-slot-independent), so it fences the launch AND the redispatch, never trusting the
        # durable state alone. Fail-closed: an unreadable / absent / moved lifecycle stops here.
        if not self._ops.lane_lifecycle_current(request):
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, post_close_resume=True,
                phase=current.phase, revision=current.revision,
                detail=(
                    "live lane lifecycle no longer matches the approved generation "
                    "(moved / newer / unreadable); zero close / launch / send"
                ),
            )
        # R3-F1 — the fresh worker relaunches into the lane's worktree, so byte preservation
        # requires it to be READABLE. The entry preflight's worktree gate is short-circuited on a
        # post-close absence (identity_unknown), so re-verify readability here, old-slot-
        # independent. An UNREADABLE worktree fences the launch/send zero-effect (a dirty but
        # readable worktree is byte-preserved and recovered, NOT blocked — the tranche D
        # contract; this fence is readability only). A later re-run with a readable worktree
        # resumes from the same durable owed state.
        if not self._ops.lane_worktree_readable(request):
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, post_close_resume=True,
                phase=current.phase, revision=current.revision,
                detail=(
                    "lane recovery worktree is unreadable; byte preservation requires a "
                    "readable worktree; zero close / launch / send"
                ),
            )
        # R3-F1 (Design Consultation Answer j#82708) — a foreign PRODUCTIVE live process (a busy
        # provider / running tool-child) occupying the lane's assigned name is live work the
        # relaunch must never collide with. The entry preflight's not_productive gate is
        # short-circuited on a post-close absence, so re-verify it here, old-slot-independent. The
        # freshly relaunched recovery worker is idle (never productive), so this fences only
        # genuine foreign work — an idle stale residue is what the recovery recovers, not a block.
        if not self._ops.lane_free_of_foreign_live(request):
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, post_close_resume=True,
                phase=current.phase, revision=current.revision,
                detail=(
                    "a foreign productive live process occupies the lane; zero close / "
                    "launch / send"
                ),
            )
        # §5 — the resume RE-approval anchor is a SEPARATE authority from the stored decision /
        # continuation anchor. A supplied ``resume_journal`` must be a complete Redmine pointer
        # (fail-closed, zero effect on a malformed one) and is recorded as the resume authority;
        # it NEVER overwrites the stored anchor, so the same-action CAS (matched on the original
        # ``journal``) and a fresh durable re-approval coexist without tripping the divergence
        # fence. An empty ``resume_journal`` is a same-journal resume (the original single anchor).
        resume_authorization = ""
        if norm(request.resume_journal):
            try:
                DecisionPointer(
                    source="redmine", issue_id=norm(request.issue),
                    journal_id=norm(request.resume_journal),
                )
            except DecisionPointerError:
                return self._outcome(
                    request, verdict, status=RECOVERY_REFUSED, executed=True,
                    observation=observation, post_close_resume=True,
                    detail=(
                        "resume re-approval journal is not a complete Redmine pointer; "
                        "zero close / launch / send"
                    ),
                )
            resume_authorization = norm(request.resume_journal)
        outcome = self._execute(request, verdict, observation)
        return replace(
            outcome, post_close_resume=True, resume_authorization=resume_authorization
        )

    # -- execute -------------------------------------------------------------

    def _execute(
        self, request: RecoveryRequest, verdict: str, observation: RecoveryObservation
    ) -> RecoveryOutcome:
        # 1. Positive durable owner approval + exact action id + generation, before any write.
        try:
            decision = DecisionPointer(
                source="redmine",
                issue_id=norm(request.issue),
                journal_id=norm(request.journal),
            )
        except DecisionPointerError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="approval journal is not a complete Redmine pointer",
            )
        try:
            expected_action = stale_worker_recovery_action_id(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, locator=request.locator,
            )
        except ValueError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="recovery inputs do not identify one exact worker",
            )
        if norm(request.action_id) != expected_action:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="action id does not match the exact approved worker",
            )
        if not isinstance(request.action_generation, int) or isinstance(
            request.action_generation, bool
        ) or request.action_generation < 1:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="approved generation is not a positive exact integer",
            )
        # A DESTRUCTIVE worker recovery requires the exact lane lifecycle (revision,
        # generation) evidence the approval pinned (Redmine #13806 R1-F2 / j#79485 §2): the
        # ParticipantPin treats these as optional for the default companion / coordinator, but a
        # standard-sublane worker recovery must carry them so the durable manifest holds — and
        # each destructive effect / replay re-verifies against — the exact lifecycle generation.
        # A missing one is a typed zero-close blocker, never actuated on a bare boolean.
        if not norm(request.lane_revision) or not norm(request.lane_generation):
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail=(
                    "lane lifecycle revision / generation evidence is required for a "
                    "destructive worker recovery; zero close"
                ),
            )
        try:
            continuation = ContinuationPointer(
                source="redmine", issue_id=norm(request.issue),
                journal_id=norm(request.journal),
                expected_gate=norm(request.expected_gate),
                next_semantic_action=norm(request.next_semantic_action),
            )
        except ContinuationPointerError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail="redispatch continuation pointer is incomplete",
            )
        # The immutable continuation authority is the (gate kind, semantic action) PAIR: the
        # redispatch delivers an implementation_request to the fresh worker and drives the fixed
        # dispatch-once effect (the only kind/action the governed worker-forward rail performs),
        # so BOTH ``expected_gate`` and ``next_semantic_action`` must name exactly those
        # (Redmine #13806 R3-F1 / R4-F1). A pointer declaring a different gate OR action would let
        # the transaction header point at one thing while a fixed effect runs another — a
        # zero-close / zero-send typed blocker, never advanced to completed on a mismatch.
        if continuation.expected_gate != RECOVERY_REDISPATCH_GATE:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail=(
                    f"continuation gate {continuation.expected_gate!r} is not a redispatchable "
                    f"worker gate ({RECOVERY_REDISPATCH_GATE!r}); zero send"
                ),
            )
        if continuation.next_semantic_action != RECOVERY_REDISPATCH_ACTION:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation,
                detail=(
                    f"continuation action {continuation.next_semantic_action!r} is not the "
                    f"redispatchable worker action ({RECOVERY_REDISPATCH_ACTION!r}); zero send"
                ),
            )
        try:
            worker = ParticipantPin(
                lane_id=request.lane, role=request.role, provider=request.provider,
                assigned_name=request.assigned_name, old_locator=request.locator,
                is_self=False, lane_revision=request.lane_revision,
                lane_generation=request.lane_generation,
            )
        except ParticipantPinError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, detail="approved worker pin is incomplete",
            )
        try:
            key = ReplacementTransactionKey(self._workspace_id, expected_action)
        except ValueError:
            return self._outcome(
                request, verdict, status=RECOVERY_REFUSED, executed=True,
                observation=observation, detail="workspace / action identity is incomplete",
            )
        gen = request.action_generation

        # 2. Plan (or idempotently resume) the non-self worker-recovery transaction.
        plan = self._store.plan_transaction(
            key, action_generation=gen, decision=decision, continuation=continuation,
            participants=[worker],
        )
        if not plan.applied and plan.reason != CAS_ALREADY_DECLARED:
            return self._outcome(
                request, verdict, status=RECOVERY_STOPPED, executed=True,
                observation=observation, detail=f"transaction plan refused ({plan.reason})",
            )
        current = self._store.get(key)
        if current is None:
            return self._outcome(
                request, verdict, status=RECOVERY_STOPPED, executed=True,
                observation=observation, detail="transaction row vanished after plan",
            )
        # A pre-existing row at this key must be THIS exact approved generation + decision +
        # continuation AND the same single pinned worker (identity + evidence) — otherwise a
        # different authority is already acting on this worker (authority conflict). Zero
        # actuation. (A fresh plan trivially matches.) The transaction key
        # ``recover:<lane>:<role>:<provider>:<name>:<locator>`` does not include the lane
        # ``(revision, generation)`` evidence, so an approval that differs ONLY in those pins is
        # explicitly refused here rather than silently resuming the stored worker's evidence.
        stored_worker = current.find_participant(worker.identity)
        diverged = (
            current.action_generation != gen
            or current.decision != decision
            or current.continuation != continuation
            or len(current.participants) != 1
            or stored_worker is None
            or stored_worker.old_locator != worker.old_locator
            or stored_worker.lane_revision != worker.lane_revision
            or stored_worker.lane_generation != worker.lane_generation
        )
        converged_supersede = False
        if diverged:
            if not request.supersede:
                return self._outcome(
                    request, verdict, status=RECOVERY_REFUSED, executed=True,
                    observation=observation, phase=current.phase, revision=current.revision,
                    detail=(
                        "a different recovery authority is already in flight for this worker; "
                        "pass --supersede with a higher --action-generation to re-anchor a "
                        "zero-effect stuck transaction to the corrected evidence"
                    ),
                )
            # Owner-approved convergence (Redmine #13806): the stuck row was pinned to mis-bound
            # lane-lifecycle evidence by an earlier run and can never actuate. Re-anchor it to
            # THIS new generation + corrected evidence — but the CAS re-anchors ONLY while the
            # row has run zero close / launch / send and is the same exact action; a close /
            # launch / send / foreign / in-flight row is an immutable fence, zero-write.
            sup = self._store.supersede_transaction(
                key, new_action_generation=gen, decision=decision,
                continuation=continuation, participants=[worker],
            )
            if not sup.applied:
                return self._outcome(
                    request, verdict, status=RECOVERY_REFUSED, executed=True,
                    observation=observation, phase=current.phase, revision=current.revision,
                    detail=(
                        f"supersede refused ({sup.reason}); the stuck transaction is not a "
                        "zero-effect same-action row (a close / launch / send / foreign / "
                        "in-flight transaction keeps its immutable fence)"
                    ),
                )
            current = self._store.get(key)
            if current is None:
                return self._outcome(
                    request, verdict, status=RECOVERY_STOPPED, executed=True,
                    observation=observation,
                    detail="transaction row vanished after supersede",
                )
            converged_supersede = True

        # 3. Drive the guarded close → launch → attest (tranche B actuator, byte-preserving).
        actuator = ReplacementActuatorUseCase(
            self._store, self._actuation_port, clock=self._clock,
            lease_ttl_seconds=self._ttl,
            preservation_policy=assess_worker_recovery_preservation,
        )
        recov = actuator.drive_worker_recovery(
            key, holder=request.holder, expected_action_generation=gen,
        )
        after = self._store.get(key)
        worker_pin = after.find_participant(worker.identity) if after else None
        if recov.status != ACTUATION_RECOVERED:
            # The recovery stopped fail-closed; the durable transaction holds the replay fence.
            # Surface the CLOSED preservation reason(s) + comparison axis (Redmine #13806) so a
            # preservation_blocked names identity_mismatch / running_process / pending_approval
            # and the diverging axis, not a generic block.
            return self._outcome(
                request, verdict, status=RECOVERY_STOPPED, executed=True,
                observation=observation, recovery_status=recov.status,
                closed_old_worker=self._closed_old_worker(worker_pin),
                phase=after.phase if after else "", revision=after.revision if after else 0,
                preservation_reasons=tuple(recov.preservation_reasons),
                converged_supersede=converged_supersede,
                detail=(
                    f"worker recovery stopped ({recov.status}"
                    + (f": {recov.detail}" if norm(recov.detail) else "")
                    + "); re-run resumes"
                ),
            )

        # 4. Fresh receiver attested — redispatch the ORIGINAL gate exactly once.
        redis = self._redispatch(key, holder=request.holder, gen=gen)
        final = self._store.get(key)
        status = (
            RECOVERY_COMPLETED
            if redis == REDISPATCH_CONFIRMED
            else RECOVERY_STOPPED
        )
        return self._outcome(
            request, verdict, status=status, executed=True, observation=observation,
            recovery_status=recov.status, redispatch_status=redis,
            closed_old_worker=self._closed_old_worker(worker_pin),
            fresh_slot_attested=True,
            phase=final.phase if final else "", revision=final.revision if final else 0,
            converged_supersede=converged_supersede,
            detail=(
                "worker replaced and original gate redispatched exactly once"
                if redis == REDISPATCH_CONFIRMED
                else f"worker replaced; redispatch {redis} (no blind resend; re-run resumes)"
            ),
        )

    # -- redispatch leg (rides the tranche C drain discipline) ----------------

    def _redispatch(self, key, *, holder, gen) -> str:
        """Redispatch the original durable gate exactly once (``replacing_nonself -> completed``).

        Reuses the tranche C drain's "record ``attempted`` (the phase move into
        ``draining_continuation``) BEFORE the send, then confirm on the durable gate, and NEVER
        blind-resend after ``attempted``" discipline (:func:`drain_state_for` /
        :func:`may_attempt_drain`). The send is high-level (the whole Implementation Request /
        gate), under the same-lane gateway ownership; an ACK / queue-enter is not completion.
        """
        rec = self._store.get(key)
        if rec is None:
            return REDISPATCH_NOT_FOUND
        continuation = rec.continuation
        if continuation is None:
            return REDISPATCH_CONTINUATION_UNREADABLE
        # Idempotency FIRST: if the original gate has already landed (a prior send that we could
        # not confirm, or an out-of-band dispatch), advance to completion with ZERO send — even
        # from ``replacing_nonself`` (which ``drain_state_for`` treats as not-yet-attempted
        # regardless of the gate). This is what makes the redispatch exactly-once, never a
        # duplicate dispatch.
        if self._ops.gate_redispatched(continuation):
            return self._finalize_confirmed(key, holder=holder, gen=gen)
        # The gate is not confirmed; ride the drain state machine from the current phase.
        state = drain_state_for(rec.phase, gate_confirmed=False)
        if not may_attempt_drain(state):
            # attempted / uncertain and the gate is NOT confirmed — a send may be in flight.
            # Report uncertain; a later re-run re-checks the gate. Never blind-resend.
            return REDISPATCH_UNCERTAIN
        # not_attempted (phase replacing_nonself): record attempted (-> draining_continuation)
        # BEFORE the send, so a crash here resumes as uncertain rather than re-sending.
        attempt = self._store.transition_phase(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            target=PHASE_DRAINING_CONTINUATION, holder=holder, now=self._clock(),
        )
        terminal = self._redispatch_terminal(attempt)
        if terminal is not None:
            return terminal
        # Re-authenticate the lease immediately before the send (a live-holder CAS re-read on a
        # fresh clock) — a lost lease yields ZERO send.
        fresh = self._store.get(key)
        effect_now = self._clock()
        if (
            fresh is None
            or fresh.action_generation != gen
            or fresh.lease_holder != holder
            or not fresh.lease_is_live(effect_now)
        ):
            return REDISPATCH_LEASE_LOST
        if self._ops.redispatch_gate(continuation) != DRAIN_SEND_OK:
            # Send failed; the state stays attempted/draining_continuation. A re-run re-checks
            # the gate and only completes if it confirms — never a blind resend.
            return REDISPATCH_SEND_FAILED
        if not self._ops.gate_redispatched(continuation):
            return REDISPATCH_UNCERTAIN
        return self._finalize_confirmed(key, holder=holder, gen=gen)

    def _finalize_confirmed(self, key, *, holder, gen) -> str:
        """Advance a gate-confirmed transaction to ``completed`` with ZERO send (idempotent).

        Reached only when the original gate has landed (a confirmed send, an out-of-band
        dispatch, or an already-``completed`` resume). Advances ``replacing_nonself ->
        draining_continuation -> completed`` as needed and releases the lease — never issues a
        send, so it can never duplicate the dispatch.
        """
        rec = self._store.get(key)
        if rec is None:
            return REDISPATCH_NOT_FOUND
        if rec.phase == PHASE_REPLACING_NONSELF:
            attempt = self._store.transition_phase(
                key, expected_revision=rec.revision, expected_action_generation=gen,
                target=PHASE_DRAINING_CONTINUATION, holder=holder, now=self._clock(),
            )
            terminal = self._redispatch_terminal(attempt)
            if terminal is not None:
                return terminal
            rec = self._store.get(key)
        if rec is not None and rec.phase == PHASE_DRAINING_CONTINUATION:
            done = self._store.transition_phase(
                key, expected_revision=rec.revision, expected_action_generation=gen,
                target=PHASE_COMPLETED, holder=holder, now=self._clock(),
            )
            terminal = self._redispatch_terminal(done)
            if terminal is not None:
                return terminal
        self._release(key, gen, holder)
        return REDISPATCH_CONFIRMED

    def _redispatch_terminal(self, outcome) -> Optional[str]:
        if outcome.applied:
            return None
        if outcome.reason == CAS_LEASE_NOT_HELD:
            return REDISPATCH_LEASE_LOST
        if outcome.reason == CAS_GENERATION_MISMATCH:
            return REDISPATCH_GENERATION_MISMATCH
        # A benign stale revision (a concurrent read moved the row) — a re-run re-reads; report
        # uncertain rather than assume the send state.
        return REDISPATCH_UNCERTAIN

    def _release(self, key, gen, holder) -> None:
        rec = self._store.get(key)
        if rec is None or rec.lease_holder != holder:
            return
        self._store.release(
            key, expected_revision=rec.revision, expected_action_generation=gen,
            holder=holder, now=self._clock(),
        )

    @staticmethod
    def _closed_old_worker(worker_pin) -> bool:
        # The old exact worker was closed once the participant moved off close_owed.
        return worker_pin is not None and worker_pin.phase not in ("close_owed", "")

    # -- rendering -----------------------------------------------------------

    def _outcome(
        self,
        request: RecoveryRequest,
        verdict: str,
        *,
        status: str,
        executed: bool = False,
        observation: Optional[RecoveryObservation] = None,
        recovery_status: str = "",
        redispatch_status: str = "",
        closed_old_worker: bool = False,
        fresh_slot_attested: bool = False,
        phase: str = "",
        revision: int = 0,
        detail: str = "",
        preservation_reasons: tuple[str, ...] = (),
        converged_supersede: bool = False,
        post_close_resume: bool = False,
        resume_authorization: str = "",
    ) -> RecoveryOutcome:
        return RecoveryOutcome(
            issue=norm(request.issue),
            lane=norm(request.lane),
            role=norm(request.role),
            verdict=verdict,
            status=status,
            executed=executed,
            recovery_status=recovery_status,
            redispatch_status=redispatch_status,
            closed_old_worker=closed_old_worker,
            fresh_slot_attested=fresh_slot_attested,
            phase=phase,
            revision=revision,
            detail=detail,
            observation=observation.as_payload() if observation is not None else None,
            preservation_reasons=tuple(preservation_reasons),
            converged_supersede=converged_supersede,
            post_close_resume=post_close_resume,
            resume_authorization=norm(resume_authorization),
        )


__all__ = (
    "RECOVERY_PREFLIGHT",
    "RECOVERY_REFUSED",
    "RECOVERY_COMPLETED",
    "RECOVERY_STOPPED",
    "REDISPATCH_CONFIRMED",
    "REDISPATCH_UNCERTAIN",
    "REDISPATCH_SEND_FAILED",
    "REDISPATCH_LEASE_LOST",
    "REDISPATCH_GENERATION_MISMATCH",
    "REDISPATCH_NOT_FOUND",
    "REDISPATCH_CONTINUATION_UNREADABLE",
    "RecoveryRequest",
    "RecoveryOutcome",
    "StaleWorkerRecoveryOps",
    "StaleWorkerRecoveryUseCase",
)
