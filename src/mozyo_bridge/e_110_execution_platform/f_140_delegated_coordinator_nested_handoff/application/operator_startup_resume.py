"""Startup-clear exactly-once resume orchestrator (Redmine #13813).

The resume tranche of #13762 (Design Answer j#78409, Coordinator Verdict j#78412,
Task split #13762-B). #13760 detects a provider startup screen and refuses the send
(zero-send); #13812 projects that blocker as a durable ``operator_action_required``
gate pinned to an exact target. Once the operator clears the screen in the provider's
own UI, *this* module re-issues the original Implementation Request — **exactly once**.

The order is the whole safety of the tranche (j#78409 "UI解消後の resume", and its
correction "startup-clear admission は reserve の前"):

1. **Resumable precondition.** The durable gate handed in (rebuilt by the caller from
   the Redmine record via :meth:`OperatorStartupGate.from_record`) must be in the
   ``operator_reported_done`` state — the operator has reported clearing the screen. A
   ``required`` / ``owner_approved`` gate is pre-clear, a ``consumed`` / ``superseded``
   gate is terminal, and a ``verified_clear`` gate is a resume already in flight: all
   are **zero-read, zero-write** (the pane is not even read).
2. **Startup-clear admission (zero-write).** #13812's
   :func:`project_operator_startup_gate` re-reads the live pane once and re-checks the
   exact target identity + agent generation against the gate's pin. Resume proceeds
   **only** on a positive :data:`PROJECT_STARTUP_CLEAR` — a still-blocked screen, an
   unreadable pane, an unknown provider, an identity mismatch, a newer / stale
   generation, an ambiguous / unresolved target, or a gate-binding mismatch all return
   a zero-send :data:`RESUME_NOT_CLEAR`. Nothing has touched the outbox fence yet.
3. **Reserve, then send at most once.** Only after startup-clear is confirmed does the
   orchestrator reserve the shared :class:`DispatchOutboxFence` (the SOLE exactly-once
   authority — no second ledger; the deterministic ``delivery_id`` is a duplicate-
   detection *pointer*, never the atomic authority) and drive one high-level send. This
   mirrors #13489's :func:`execute_dispatch` boundary exactly:

   - a lost / already-fenced reserve (duplicate, concurrent caller, restart) is
     **never-send** (:data:`RESUME_SKIPPED`); a corrupt / missing / replaced fence is
     fail-closed with **no send** (:data:`RESUME_FENCE_UNAVAILABLE`);
   - a positive turn-start confirms delivery (:data:`RESUME_DELIVERED`) and advances the
     gate to ``consumed``; any raised / ack-only / not-started / timeout / unknown
     outcome leaves the fence ``uncertain`` (:data:`RESUME_UNCERTAIN`, gate
     ``verified_clear``) for **operator reconcile — never a blind retry**.

The send seam is injected (``send: () -> SendOutcome``), exactly as #13489's execution
boundary injects its send: the live CLI wiring binds it to the existing high-level
handoff rail (re-issuing the original ``implementation_request`` anchor), and the
regressions inject a counting fake so the fence semantics are proven without any live
delivery. No raw Herdr / tmux, no low-level key/Enter, is introduced here.

**ACK is not completion.** A :data:`RESUME_DELIVERED` outcome means the original request
was re-issued exactly once — it never promotes the request to implementation_done,
review, or close (:attr:`StartupResumeResult.promotes_workflow_completion` is always
False; the ACK / delivery / completion separation, ``ack-completion-receiver-state``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    FENCE_ABSENT,
    FENCE_CANCELLED,
    DispatchOutboxFence,
    DispatchOutboxFenceError,
    FenceKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
    SendOutcome,
    TURN_START_STARTED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (
    PROJECT_STARTUP_CLEAR,
    ObservedStartupTarget,
    project_operator_startup_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    STATE_OPERATOR_REPORTED_DONE,
    STATE_OWNER_APPROVED,
    STATE_REQUIRED,
    STATE_VERIFIED_CLEAR,
    TERMINAL_STATES,
    OperatorStartupGate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (
    consume_gate,
    verify_clear_gate,
)

# Resume result tokens (machine-readable; literal regardless of UI language).
#: startup-clear confirmed, fence reserved, one send delivered (turn-start started) ->
#: the gate advances to ``consumed``. The original request was re-issued exactly once.
RESUME_DELIVERED = "resume_delivered"
#: reserved + sent, but the send's turn-start is not confirmed (raised / ack-only /
#: not-started / timeout / unknown) -> the gate advances to ``verified_clear`` with an
#: ``uncertain`` fence; operator reconcile, never a blind retry.
RESUME_UNCERTAIN = "resume_uncertain"
#: never-send: the fence key was already reserved / delivered / uncertain / cancelled
#: (a duplicate resume re-run, a concurrent caller, or a restart). Zero additional send.
RESUME_SKIPPED = "resume_skipped"
#: the outbox fence is corrupt / missing / replaced -> fail-closed with NO send.
RESUME_FENCE_UNAVAILABLE = "resume_fence_unavailable"
#: the startup-clear admission did not return a positive clear (still blocked /
#: unreadable / unknown provider / identity mismatch / newer or stale generation /
#: ambiguous / unresolved / gate-binding mismatch). Zero-write, zero fence touch.
RESUME_NOT_CLEAR = "resume_not_clear"
#: the durable gate is not in the resumable ``operator_reported_done`` state (it is
#: pre-clear, terminal, or a resume already in flight). Zero-read, zero-write.
RESUME_NOT_RESUMABLE = "resume_not_resumable"
#: the durable gate-transition writer is not available (write opt-in unset / no trusted
#: base URL / no credential) at the pre-reserve preflight -> reserve/send 0 (j#79332 §5).
RESUME_RECORDER_UNAVAILABLE = "resume_recorder_unavailable"
#: the latest durable gate is a READABLE legacy (v1 / v2) record: it predates the v3
#: runtime_role / lane_revision contract, so it cannot be resumed without fabricating an
#: exact-revision approval. Fixed disposition, reserve/send 0 -> the operator must re-approve
#: a fresh v3 gate (Design Answer j#79405 §B). Never promoted to corrupt or to current-v3.
RESUME_LEGACY_REAPPROVAL_REQUIRED = "legacy_gate_reapproval_required"
#: the gate's repo-relative ``execution_root`` did not safely resolve under the action-time
#: repo root (escape / unresolved root) at the pre-reserve check -> reserve/send 0 (Design
#: Answer j#79405 §C); a re-issue must never land outside the pinned execution root.
RESUME_EXECUTION_ROOT_UNSAFE = "resume_execution_root_unsafe"

#: All recognized resume results.
RESUME_RESULTS: frozenset[str] = frozenset(
    {
        RESUME_DELIVERED,
        RESUME_UNCERTAIN,
        RESUME_SKIPPED,
        RESUME_FENCE_UNAVAILABLE,
        RESUME_NOT_CLEAR,
        RESUME_NOT_RESUMABLE,
        RESUME_RECORDER_UNAVAILABLE,
        RESUME_LEGACY_REAPPROVAL_REQUIRED,
        RESUME_EXECUTION_ROOT_UNSAFE,
    }
)


class StartupResumeError(ValueError):
    """The resume orchestrator was called with an incoherent argument (contract violation).

    Distinct from a fail-closed *result* (a legitimate, returned outcome): raised only for
    a caller error the orchestrator cannot sensibly turn into a resume disposition.
    """


@dataclass(frozen=True)
class StartupResumeResult:
    """The typed outcome of one resume attempt.

    ``result`` is the sole authority (a member of :data:`RESUME_RESULTS`). ``sent`` /
    ``reserved`` record whether the single send seam ran and whether the fence reserve
    was won. ``fence_state`` is the fence's post-call state (or :data:`FENCE_ABSENT` when
    the fence was never touched). ``needs_reconcile`` flags an ``uncertain`` outcome an
    operator must reconcile (never auto-retried). ``advanced_gate`` is the append-only
    successor gate (``verified_clear`` on uncertain, ``consumed`` on delivered) the caller
    records under the same ``gate_id`` / ``action_generation``; it is absent on every
    zero-write result. ``projection_disposition`` carries the #13812 disposition when
    ``result`` is :data:`RESUME_NOT_CLEAR`.
    """

    result: str
    sent: bool = False
    reserved: bool = False
    fence_state: str = FENCE_ABSENT
    needs_reconcile: bool = False
    advanced_gate: Optional[OperatorStartupGate] = None
    projection_disposition: Optional[str] = None
    #: True when a delivered send's durable gate-transition append could not be recorded
    #: (a post-send record failure); the send was still exactly-once via the fence, so this
    #: is an operator-reconcile flag (the durable gate journal is behind), never a re-send.
    record_failed: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if self.result not in RESUME_RESULTS:
            raise StartupResumeError(
                f"resume result {self.result!r} is not recognized; allowed: "
                f"{sorted(RESUME_RESULTS)}"
            )

    @property
    def ok(self) -> bool:
        """A delivered re-issue and a never-send skip are both non-error outcomes."""
        return self.result in (RESUME_DELIVERED, RESUME_SKIPPED)

    @property
    def promotes_workflow_completion(self) -> bool:
        """Always False: a delivered re-issue is a delivery, NOT a workflow completion.

        The executable form of the ACK / delivery / completion separation
        (``ack-completion-receiver-state``): even :data:`RESUME_DELIVERED` never advances
        implementation_done / review / close. Those gates live in the durable record and
        are never derived from a send outcome.
        """
        return False


def fence_key_for_gate(gate: OperatorStartupGate) -> FenceKey:
    """The UNIQUE outbox fence key for a gate's resume.

    Built from the gate's pinned target and the ORIGINAL request anchor: the workspace /
    lane / target-name identify *where* the send goes, and the original request's issue /
    journal / ``delivery_id`` (the deterministic q-enter logical payload id, used here as
    the fence ``action_id``) identify *which* request. The fence — not the ``delivery_id``
    — is the exactly-once authority (j#78409 correction); this is the same
    ``DispatchOutboxFence`` table #13489's worker dispatch uses, never a second ledger.
    """
    return FenceKey(
        workspace_id=gate.target.workspace_id,
        lane_id=gate.target.lane_id,
        issue=gate.original_request.issue,
        journal=gate.original_request.journal,
        action_id=gate.original_request.delivery_id,
        target_assigned_name=gate.target.target_assigned_name,
    )


def _not_resumable(gate: OperatorStartupGate) -> Optional[StartupResumeResult]:
    """Return a zero-write :data:`RESUME_NOT_RESUMABLE` unless the gate is resumable.

    The resumable entry state is ``operator_reported_done``. Any other state short-
    circuits before the pane is read: ``required`` / ``owner_approved`` are pre-clear
    (the operator has not reported clearing the screen), ``consumed`` / ``superseded``
    are terminal, and ``verified_clear`` is a resume already in flight (its fence pointer
    tells whether it needs reconcile). Returns ``None`` when the gate may proceed.
    """
    state = gate.state
    if state == STATE_OPERATOR_REPORTED_DONE:
        return None
    if state in (STATE_REQUIRED, STATE_OWNER_APPROVED):
        detail = (
            f"gate is {state!r} (pre-clear): the operator has not reported clearing the "
            f"startup screen, so there is nothing to resume"
        )
        needs_reconcile = False
        fence_state = FENCE_ABSENT
    elif state == STATE_VERIFIED_CLEAR:
        # A resume already reserved the fence; its pointer says whether it is awaiting
        # reconcile. Re-invoking must not retry — the fence remains the authority.
        fence_state = gate.resume.dispatch_fence_state
        needs_reconcile = fence_state == FENCE_UNCERTAIN
        detail = (
            f"gate is {STATE_VERIFIED_CLEAR!r} (resume already in flight; fence "
            f"{fence_state}): no additional send"
        )
    else:  # terminal: consumed / superseded
        detail = f"gate is terminal ({state!r}): the resume is already resolved"
        needs_reconcile = False
        fence_state = FENCE_ABSENT
    return StartupResumeResult(
        result=RESUME_NOT_RESUMABLE,
        sent=False,
        reserved=False,
        fence_state=fence_state,
        needs_reconcile=needs_reconcile,
        detail=detail,
    )


def _confirm_outcome_write(
    mark: Callable[..., bool], key: FenceKey, *, detail: str, now: Optional[str]
) -> bool:
    """Attempt a fence outcome write; return True iff it was DURABLY confirmed.

    ``mark`` is ``fence.mark_delivered`` (the delivered CONFIRMATION — an UPDATE that
    returns False when the reserved row vanished, so a missing row is never claimed
    delivered; review j#79268 Finding 2) or ``fence.record_uncertain`` (the fail-closed
    uncertain RE-ASSERTION — an upsert that re-creates the row so the fence keeps a
    never-send state even after a row loss; review j#79309 Finding 1). A whole-store loss
    raises :class:`DispatchOutboxFenceError`, caught here and reported as False (a re-run
    then fails closed on the fence's ``_connect``). Either way the caller must not claim a
    durably recorded delivered outcome from an unconfirmable write.
    """
    try:
        return bool(mark(key, detail=detail, now=now))
    except DispatchOutboxFenceError:
        return False


def resume_startup_gate(
    *,
    existing_gate: OperatorStartupGate,
    observed: ObservedStartupTarget,
    read_visible: Callable[[], object],
    fence: DispatchOutboxFence,
    send: Callable[[], SendOutcome],
    profile_version: str,
    classifier_version: str,
    observed_at: str,
    now: Optional[str] = None,
    registry: object = None,
) -> StartupResumeResult:
    """Resume the original request exactly once, gated on a positive startup-clear.

    ``existing_gate`` is the durable approved gate (rebuilt from the Redmine record).
    ``observed`` / ``read_visible`` are the action-time target resolution and the #13760
    read primitive, exactly as #13812's projection consumes them. ``fence`` is the shared
    exactly-once authority; ``send`` is the one high-level send seam (invoked at most
    once, only after startup-clear AND a winning reserve). ``profile_version`` /
    ``classifier_version`` / ``observed_at`` feed the projection; ``observed_at`` is also
    the ``startup_clear_observed_at`` stamp on an advanced gate (the domain never reads
    the clock). Never raises for a corrupt fence — fail-closed
    :data:`RESUME_FENCE_UNAVAILABLE`, no send.
    """
    # 1. Resumable precondition (zero-read, zero-write on a non-resumable gate).
    not_resumable = _not_resumable(existing_gate)
    if not_resumable is not None:
        return not_resumable

    # 2. Startup-clear admission (zero-write). Reuse #13812's projection verbatim, pinned
    #    to THIS gate: the gate-binding fence + stale判定 + startup classification all run,
    #    and resume proceeds only on a positive startup_clear. Any other disposition is a
    #    zero-send RESUME_NOT_CLEAR — the pane read happened, but nothing was written.
    projection = project_operator_startup_gate(
        observed=observed,
        read_visible=read_visible,
        original_request=existing_gate.original_request,
        gate_id=existing_gate.gate_id,
        action_generation=existing_gate.action_generation,
        profile_version=profile_version,
        classifier_version=classifier_version,
        observed_at=observed_at,
        existing_gate=existing_gate,
        registry=registry,
    )
    if projection.disposition != PROJECT_STARTUP_CLEAR:
        return StartupResumeResult(
            result=RESUME_NOT_CLEAR,
            sent=False,
            reserved=False,
            fence_state=FENCE_ABSENT,
            projection_disposition=projection.disposition,
            detail=(
                f"startup-clear admission returned {projection.disposition!r}; "
                f"no reserve, no send"
            ),
        )

    # 3. Startup-clear confirmed -> reserve the shared fence FIRST (exactly-once authority).
    key = fence_key_for_gate(existing_gate)
    try:
        reservation = fence.reserve(key, now=now)
    except DispatchOutboxFenceError as exc:
        return StartupResumeResult(
            result=RESUME_FENCE_UNAVAILABLE,
            sent=False,
            reserved=False,
            fence_state=FENCE_ABSENT,
            detail=f"idempotency fence unavailable; no send ({exc})",
        )

    # The target must not be inside a retirement (Redmine #13892 R4-F3): this edge reserves on
    # the same fence with a `target_assigned_name` and then sends, so it carries the same guard
    # as `execute_dispatch`.
    if reservation.won:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E501
            target_is_retiring,
        )

        _retiring, _retire_detail = target_is_retiring(key.target_assigned_name)
        if _retiring:
            fence.mark_cancelled(key, detail=_retire_detail, now=now)
            return StartupResumeResult(
                result=RESUME_SKIPPED,
                sent=False,
                reserved=True,
                fence_state=FENCE_CANCELLED,
                detail=f"zero-send: {_retire_detail}",
            )

    if not reservation.won:
        # Never-send: duplicate resume re-run / concurrent caller / restart. The gate is
        # already (or being) consumed; this call performs zero additional send.
        return StartupResumeResult(
            result=RESUME_SKIPPED,
            sent=False,
            reserved=False,
            fence_state=reservation.current_state,
            needs_reconcile=reservation.needs_reconcile,
            detail=(
                f"never-send: fence key already {reservation.prior_state}; "
                + reservation.detail
            ),
        )

    # 4. We won the reserve: perform exactly one send attempt.
    try:
        outcome = send()
    except Exception as exc:  # noqa: BLE001 - the send may have landed; mark uncertain, never retry
        # Re-assert the never-send fence state (upsert, survives a vanished row) so a re-run
        # — even one re-reading a stale durable gate — sees uncertain and never sends. A
        # raised outcome write must NOT propagate out of a path that already sent once.
        _confirm_outcome_write(
            fence.record_uncertain,
            key,
            detail=f"resume send raised {type(exc).__name__}; outcome unknown",
            now=now,
        )
        advanced = verify_clear_gate(
            existing_gate,
            startup_clear_observed_at=observed_at,
            dispatch_fence_state=FENCE_UNCERTAIN,
        )
        return StartupResumeResult(
            result=RESUME_UNCERTAIN,
            sent=True,
            reserved=True,
            fence_state=FENCE_UNCERTAIN,
            needs_reconcile=True,
            advanced_gate=advanced,
            detail=f"resume send raised {type(exc).__name__}; outcome unknown -> reconcile",
        )

    if outcome.turn_start == TURN_START_STARTED:
        # A delivered outcome counts ONLY if the fence DURABLY records it. If the
        # authoritative row went missing / was replaced / errored between the reserve and
        # this write, the sole exactly-once authority can no longer prove the request was
        # consumed — a stale re-run would reserve fresh and duplicate the send. So a
        # delivered send with an unconfirmable outcome write fails closed to uncertain
        # (operator reconcile), never `delivered` / `consumed` (Finding 2, review j#79268).
        if _confirm_outcome_write(
            fence.mark_delivered,
            key,
            detail=outcome.detail or "resume turn-start confirmed",
            now=now,
        ):
            # required -> ... -> verified_clear -> consumed, continuing the same gate. The
            # consumed pointer is the original request's delivery_id (a path-safe anchor).
            cleared = verify_clear_gate(
                existing_gate,
                startup_clear_observed_at=observed_at,
                dispatch_fence_state=FENCE_RESERVED,
            )
            consumed = consume_gate(
                cleared, consumed_delivery_record=existing_gate.original_request.delivery_id
            )
            return StartupResumeResult(
                result=RESUME_DELIVERED,
                sent=True,
                reserved=True,
                fence_state=FENCE_DELIVERED,
                advanced_gate=consumed,
                detail=outcome.detail or "reserved, sent, turn-start confirmed; re-issued exactly once",
            )
        # The send's turn-start was confirmed, but the fence outcome write could not be
        # durably recorded (row missing / replaced / store error). Fail closed to uncertain:
        # the delivery is real but unrecordable, so it must be reconciled, not reported
        # consumed. record_uncertain UPSERTS the never-send state — re-creating the row if it
        # vanished — so the FENCE (not the durable gate) refuses a blind re-reserve: a re-run,
        # even one re-reading a stale `operator_reported_done` gate, sees uncertain and sends
        # zero (Finding 1, review j#79309). Only a whole-store loss leaves it unrecordable,
        # and a re-run then fails closed on the fence's _connect anyway.
        _confirm_outcome_write(
            fence.record_uncertain,
            key,
            detail="delivered outcome unrecordable; reserved row missing/replaced/error",
            now=now,
        )
        advanced = verify_clear_gate(
            existing_gate,
            startup_clear_observed_at=observed_at,
            dispatch_fence_state=FENCE_UNCERTAIN,
        )
        return StartupResumeResult(
            result=RESUME_UNCERTAIN,
            sent=True,
            reserved=True,
            fence_state=FENCE_UNCERTAIN,
            needs_reconcile=True,
            advanced_gate=advanced,
            detail=(
                "send turn-start confirmed but the fence outcome write could not be "
                "durably recorded (row missing/replaced/error); reconcile -> not consumed"
            ),
        )

    # Any non-``started`` turn-start (ack-only / not-started / timeout / unknown) -> the
    # send may have landed but the receiver's turn is not confirmed: uncertain, reconcile.
    # record_uncertain upserts the never-send state so a re-run sees uncertain (Finding 1).
    _confirm_outcome_write(
        fence.record_uncertain,
        key,
        detail=outcome.detail or f"resume turn-start {outcome.turn_start}; not confirmed started",
        now=now,
    )
    advanced = verify_clear_gate(
        existing_gate,
        startup_clear_observed_at=observed_at,
        dispatch_fence_state=FENCE_UNCERTAIN,
    )
    return StartupResumeResult(
        result=RESUME_UNCERTAIN,
        sent=True,
        reserved=True,
        fence_state=FENCE_UNCERTAIN,
        needs_reconcile=True,
        advanced_gate=advanced,
        detail=(
            outcome.detail
            or f"resume turn-start {outcome.turn_start} (not started); uncertain -> reconcile"
        ),
    )


__all__ = (
    "RESUME_DELIVERED",
    "RESUME_UNCERTAIN",
    "RESUME_SKIPPED",
    "RESUME_FENCE_UNAVAILABLE",
    "RESUME_NOT_CLEAR",
    "RESUME_NOT_RESUMABLE",
    "RESUME_RECORDER_UNAVAILABLE",
    "RESUME_LEGACY_REAPPROVAL_REQUIRED",
    "RESUME_EXECUTION_ROOT_UNSAFE",
    "RESUME_RESULTS",
    "StartupResumeError",
    "StartupResumeResult",
    "fence_key_for_gate",
    "resume_startup_gate",
)
