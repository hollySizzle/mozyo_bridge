"""State-lattice operations for the operator startup gate (Redmine #13812/#13813).

The :mod:`.operator_startup_gate` module owns the durable **record types** — the
dataclasses, the field guards, and the pasteable-safety screening. This sibling owns the
**state machine** over those records: the per-state ``(approval, resume)`` invariants
(:func:`validate_state_invariants`, which the record's ``__post_init__`` delegates to),
the forward-only transition builders that continue the same ``gate_id`` /
``action_generation`` (the append-only "同じ gate_id/action_generation を継ぐ" chain,
j#78409), and the two pasteable record renderers.

Splitting the state machine from the record types keeps each module a cohesive, focused
home (module-health gate, #12321) without weakening the invariant guarantee — the record
still fails closed at construction, it just imports its validator from here. The
dependency is one-way at import time (this module imports the record types from
:mod:`.operator_startup_gate`; the record's ``__post_init__`` imports this validator
lazily, at instantiation, so there is no import cycle).
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    FENCE_DELIVERED,
    FENCE_NOT_RESERVED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    STATE_CONSUMED,
    STATE_OPERATOR_REPORTED_DONE,
    STATE_OWNER_APPROVED,
    STATE_REQUIRED,
    STATE_SUPERSEDED,
    STATE_VERIFIED_CLEAR,
    TERMINAL_STATES,
    GateApproval,
    GateResume,
    OperatorStartupGate,
    OperatorStartupGateError,
)

#: The forward-only transition edges of the lattice (source -> allowed successors),
#: excluding ``superseded`` which may branch off any non-terminal state. Used by the
#: transition builders to reject a backward / skipping edge.
_FORWARD_EDGES: dict[str, frozenset[str]] = {
    STATE_REQUIRED: frozenset({STATE_OWNER_APPROVED}),
    STATE_OWNER_APPROVED: frozenset({STATE_OPERATOR_REPORTED_DONE}),
    STATE_OPERATOR_REPORTED_DONE: frozenset({STATE_VERIFIED_CLEAR}),
    STATE_VERIFIED_CLEAR: frozenset({STATE_CONSUMED}),
    STATE_CONSUMED: frozenset(),
    STATE_SUPERSEDED: frozenset(),
}


def validate_state_invariants(
    state: str, approval: Optional[GateApproval], resume: GateResume
) -> None:
    """Enforce the per-state ``(approval, resume)`` invariants of the lattice.

    Makes a contradictory durable record impossible to construct (the review j#79003
    Finding 2 discipline, extended from ``required`` across the whole lattice): e.g. a
    ``consumed`` gate that never reserved a fence, or a ``verified_clear`` gate that
    claims a delivered send. The rung definitions are documented on the ``STATE_*``
    block in :mod:`.operator_startup_gate`; this function is their executable form and is
    called from :meth:`OperatorStartupGate.__post_init__`.
    """
    default_resume = GateResume()
    approval_present = approval is not None

    if state == STATE_REQUIRED:
        if approval_present:
            raise OperatorStartupGateError(
                f"a {STATE_REQUIRED!r} operator startup gate must not carry an owner "
                f"approval; an approval is granted only on the transition out of "
                f"{STATE_REQUIRED!r}"
            )
        if resume != default_resume:
            raise OperatorStartupGateError(
                f"a {STATE_REQUIRED!r} operator startup gate must carry the default "
                f"resume (nothing observed clear, {FENCE_NOT_RESERVED!r}, no consumed "
                f"delivery)"
            )
        return

    if state == STATE_SUPERSEDED:
        # The invalidation branch may hang off ANY non-terminal state (including a
        # still-`required` gate a newer generation supersedes), so it retains whatever
        # approval / resume it was invalidated from as audit history. The resume fields
        # are already shape-screened by GateResume; no rung constraint applies.
        return

    # Every remaining state (owner_approved .. consumed) is on the approved resume path.
    if not approval_present:
        raise OperatorStartupGateError(
            f"a {state!r} operator startup gate requires an owner approval (the owner "
            f"has authorized the one-target / one-generation operator UI action)"
        )

    if state in (STATE_OWNER_APPROVED, STATE_OPERATOR_REPORTED_DONE):
        # Approved, but the agent has not re-observed startup-clear and the outbox
        # fence is untouched: the resume evidence must still be all-default.
        if resume != default_resume:
            raise OperatorStartupGateError(
                f"a {state!r} operator startup gate must carry the default resume "
                f"(nothing observed clear, {FENCE_NOT_RESERVED!r}, no consumed "
                f"delivery); resume evidence appears only from {STATE_VERIFIED_CLEAR!r}"
            )
        return

    if state == STATE_VERIFIED_CLEAR:
        # Startup-clear positively re-observed and the fence reserved, but the send's
        # turn-start is NOT confirmed delivered (reserve / uncertain rung -> reconcile).
        if resume.startup_clear_observed_at is None:
            raise OperatorStartupGateError(
                f"a {STATE_VERIFIED_CLEAR!r} operator startup gate requires "
                f"resume.startup_clear_observed_at (the agent re-observed startup-clear)"
            )
        if resume.dispatch_fence_state not in (FENCE_RESERVED, FENCE_UNCERTAIN):
            raise OperatorStartupGateError(
                f"a {STATE_VERIFIED_CLEAR!r} operator startup gate must carry a "
                f"{FENCE_RESERVED!r} or {FENCE_UNCERTAIN!r} fence (a send was reserved "
                f"but not confirmed delivered), got "
                f"{resume.dispatch_fence_state!r}"
            )
        if resume.consumed_delivery_record is not None:
            raise OperatorStartupGateError(
                f"a {STATE_VERIFIED_CLEAR!r} operator startup gate must not carry a "
                f"consumed delivery record; a confirmed delivery is the {STATE_CONSUMED!r} "
                f"rung"
            )
        return

    if state == STATE_CONSUMED:
        # The original request was re-issued exactly once and its turn-start confirmed.
        if resume.startup_clear_observed_at is None:
            raise OperatorStartupGateError(
                f"a {STATE_CONSUMED!r} operator startup gate requires "
                f"resume.startup_clear_observed_at"
            )
        if resume.dispatch_fence_state != FENCE_DELIVERED:
            raise OperatorStartupGateError(
                f"a {STATE_CONSUMED!r} operator startup gate must carry a "
                f"{FENCE_DELIVERED!r} fence, got {resume.dispatch_fence_state!r}"
            )
        if resume.consumed_delivery_record is None:
            raise OperatorStartupGateError(
                f"a {STATE_CONSUMED!r} operator startup gate requires a "
                f"consumed_delivery_record (the delivered request pointer)"
            )
        return


# ---------------------------------------------------------------------------
# Forward-only transition builders (#13813). Each returns a NEW frozen gate that
# continues the same ``gate_id`` / ``action_generation`` / ``target`` / ``classification``
# / ``original_request`` — the append-only "同じ gate_id/action_generation を継ぐ"
# transition (j#78409). They reject a backward / skipping edge (:data:`_FORWARD_EDGES`)
# so an out-of-order transition cannot forge a durable record. ``supersede_gate`` is the
# one exception: it may branch off any non-terminal state.
# ---------------------------------------------------------------------------
def _transition(
    gate: OperatorStartupGate,
    *,
    to_state: str,
    approval: Optional[GateApproval],
    resume: GateResume,
) -> OperatorStartupGate:
    """Build the successor gate, rejecting a non-forward edge (fail-closed)."""
    allowed = _FORWARD_EDGES.get(gate.state, frozenset())
    if to_state not in allowed:
        raise OperatorStartupGateError(
            f"operator startup gate transition {gate.state!r} -> {to_state!r} is not a "
            f"forward edge; allowed successors of {gate.state!r}: {sorted(allowed)}"
        )
    return OperatorStartupGate(
        gate_id=gate.gate_id,
        action_generation=gate.action_generation,
        state=to_state,
        original_request=gate.original_request,
        target=gate.target,
        classification=gate.classification,
        approval=approval,
        resume=resume,
    )


def approve_gate(gate: OperatorStartupGate, *, approval: GateApproval) -> OperatorStartupGate:
    """``required`` -> ``owner_approved``: attach the owner's UI-action approval."""
    return _transition(
        gate, to_state=STATE_OWNER_APPROVED, approval=approval, resume=GateResume()
    )


def report_operator_done(gate: OperatorStartupGate) -> OperatorStartupGate:
    """``owner_approved`` -> ``operator_reported_done``: the operator says they cleared it.

    Carries the approval forward unchanged; resume is still all-default (the agent has
    not re-verified startup-clear). This is the resume orchestrator's precondition state.
    """
    return _transition(
        gate,
        to_state=STATE_OPERATOR_REPORTED_DONE,
        approval=gate.approval,
        resume=GateResume(),
    )


def verify_clear_gate(
    gate: OperatorStartupGate,
    *,
    startup_clear_observed_at: str,
    dispatch_fence_state: str,
    consumed_delivery_record: Optional[str] = None,
) -> OperatorStartupGate:
    """``operator_reported_done`` -> ``verified_clear``: startup-clear observed + fence reserved.

    ``dispatch_fence_state`` must be :data:`FENCE_RESERVED` or :data:`FENCE_UNCERTAIN`
    (a send was reserved but its turn-start is not confirmed delivered); a confirmed
    delivery is the :func:`consume_gate` rung. ``consumed_delivery_record`` stays unset.
    """
    return _transition(
        gate,
        to_state=STATE_VERIFIED_CLEAR,
        approval=gate.approval,
        resume=GateResume(
            startup_clear_observed_at=startup_clear_observed_at,
            dispatch_fence_state=dispatch_fence_state,
            consumed_delivery_record=consumed_delivery_record,
        ),
    )


def consume_gate(
    gate: OperatorStartupGate, *, consumed_delivery_record: str
) -> OperatorStartupGate:
    """``verified_clear`` -> ``consumed``: the request was re-issued exactly once (delivered).

    Carries the verified-clear timestamp forward, flips the fence pointer to
    :data:`FENCE_DELIVERED`, and records the consumed delivery pointer. Terminal.
    """
    return _transition(
        gate,
        to_state=STATE_CONSUMED,
        approval=gate.approval,
        resume=GateResume(
            startup_clear_observed_at=gate.resume.startup_clear_observed_at,
            dispatch_fence_state=FENCE_DELIVERED,
            consumed_delivery_record=consumed_delivery_record,
        ),
    )


def supersede_gate(gate: OperatorStartupGate) -> OperatorStartupGate:
    """``<any non-terminal>`` -> ``superseded``: invalidate the gate (newer gen / supersede).

    Retains the current approval / resume as audit history. A gate already terminal
    (``consumed`` / ``superseded``) cannot be superseded (fail-closed).
    """
    if gate.state in TERMINAL_STATES:
        raise OperatorStartupGateError(
            f"operator startup gate in terminal state {gate.state!r} cannot be "
            f"superseded"
        )
    return OperatorStartupGate(
        gate_id=gate.gate_id,
        action_generation=gate.action_generation,
        state=STATE_SUPERSEDED,
        original_request=gate.original_request,
        target=gate.target,
        classification=gate.classification,
        approval=gate.approval,
        resume=gate.resume,
    )


def operator_startup_gate_record_lines(gate: OperatorStartupGate) -> list[str]:
    """Render the pasteable durable-record projection lines for a ``required`` gate.

    Follows the #13760 ``startup_admission_record_lines`` precedent: fixed tokens and
    a verdict only — no free text, no pane content, no absolute paths — so it is safe
    in a pasteable delivery record / Redmine journal. It names the exact target by its
    stable tokens and the opaque repo digest, the referenced blocker id, the approval
    scope, and the resume anchor, and states plainly that clearing the screen is an
    operator UI action this gate never performs. Use
    :func:`operator_startup_resume_record_lines` for a resume-advanced gate.
    """
    # A required gate carries no approval; the line is phrased for that state (the owner
    # has not acted). #13813's resume renderer handles the approved / advanced states.
    approval = "awaiting owner approval"
    return [
        (
            f"- operator_action_required (startup gate {gate.gate_id}, "
            f"action_generation={gate.action_generation}, state={gate.state}): the "
            f"{gate.target.provider_id} receiver is showing the "
            f"{gate.classification.blocker_id} startup screen, which cannot accept a "
            f"handoff body."
        ),
        (
            f"  target: workspace={gate.target.workspace_id} "
            f"repo={gate.target.repo_identity_digest} "
            f"execution_root={gate.target.execution_root} lane={gate.target.lane_id} "
            f"role={gate.target.target_role} name={gate.target.target_assigned_name} "
            f"agent_generation={gate.target.agent_generation}"
        ),
        (
            f"  classification: profile_version={gate.classification.profile_version} "
            f"classifier_version={gate.classification.classifier_version} "
            f"observed_at={gate.classification.observed_at}"
        ),
        (
            f"  approval: {approval}. original_request: "
            f"#{gate.original_request.issue} j#{gate.original_request.journal} "
            f"(delivery_id={gate.original_request.delivery_id})."
        ),
        (
            "  Clearing the screen is an operator action in the provider's own UI. "
            "This projection is read-only: it never answers the prompt, sends a key, "
            "or reserves the dispatch outbox. Resume is PENDING an operator UI action — "
            "the startup-clear re-observation, the outbox fence reserve, and the "
            "exactly-once re-issue of the original request are driven by the resume "
            "tranche (#13813) only AFTER the operator clears the screen. Do NOT "
            "re-issue the request by hand from this projection: a manual re-send "
            "bypasses the outbox fence and risks a lost or duplicate request (Redmine "
            "#13812 projection / #13760 detection / #13813 resume)."
        ),
    ]


def operator_startup_resume_record_lines(gate: OperatorStartupGate) -> list[str]:
    """Render pasteable append-only lines for a resume-advanced gate (#13813).

    The resume-tranche counterpart of :func:`operator_startup_gate_record_lines`: fixed
    tokens and a verdict only — no pane content, no absolute path, no credential — safe
    in a Redmine journal. It names the exact target, the resume state, the fence pointer,
    and the consumed-delivery anchor, and states plainly that a delivered re-issue is
    **not** a completion: it never promotes the original request to implementation_done /
    review / close (the ACK / delivery / completion separation, ``ack-completion-receiver-state``).
    Requires a resume-advanced state (``verified_clear`` / ``consumed`` / ``superseded``);
    use :func:`operator_startup_gate_record_lines` for a ``required`` gate.
    """
    resume_states = (STATE_VERIFIED_CLEAR, STATE_CONSUMED, STATE_SUPERSEDED)
    if gate.state not in resume_states:
        raise OperatorStartupGateError(
            f"operator_startup_resume_record_lines expects a resume-advanced state "
            f"{list(resume_states)}, got {gate.state!r}; use "
            f"operator_startup_gate_record_lines for a {STATE_REQUIRED!r} gate"
        )
    consumed = gate.resume.consumed_delivery_record
    consumed_line = "none" if consumed is None else consumed
    return [
        (
            f"- operator_startup_resume (startup gate {gate.gate_id}, "
            f"action_generation={gate.action_generation}, state={gate.state}): resume of "
            f"the original request for the {gate.target.provider_id} receiver."
        ),
        (
            f"  target: workspace={gate.target.workspace_id} "
            f"repo={gate.target.repo_identity_digest} "
            f"execution_root={gate.target.execution_root} lane={gate.target.lane_id} "
            f"role={gate.target.target_role} name={gate.target.target_assigned_name} "
            f"agent_generation={gate.target.agent_generation}"
        ),
        (
            f"  resume: startup_clear_observed_at={gate.resume.startup_clear_observed_at} "
            f"dispatch_fence_state={gate.resume.dispatch_fence_state} "
            f"consumed_delivery_record={consumed_line}"
        ),
        (
            f"  original_request: #{gate.original_request.issue} "
            f"j#{gate.original_request.journal} "
            f"(delivery_id={gate.original_request.delivery_id})."
        ),
        (
            "  A delivered re-issue is NOT a completion: the exactly-once outbox fence "
            "is the sole authority, and this gate never promotes the request to "
            "implementation_done, review, or close. An uncertain / superseded outcome "
            "is fail-closed — operator reconcile, never a blind retry (Redmine #13813 "
            "resume / #13812 projection / #13760 detection)."
        ),
    ]


__all__ = (
    "validate_state_invariants",
    "approve_gate",
    "report_operator_done",
    "verify_clear_gate",
    "consume_gate",
    "supersede_gate",
    "operator_startup_gate_record_lines",
    "operator_startup_resume_record_lines",
)
