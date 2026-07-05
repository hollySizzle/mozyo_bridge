"""Pure sublane dispatch admission gate (Redmine #13290).

The advisory Post-Dispatch Fill Loop policy (#12855,
:mod:`...domain.workflow_fill_decision`) and the Redmine-aware admission preflight
(#12856, :mod:`...domain.sublane_admission`) already answer "given this lane set,
dispatch another sublane or stop — and for which concrete reason?". But until now
*nothing consulted them at the actuator*: the real dispatch path
(``sublane create --execute`` / ``sublane dispatch-worker --execute``) created and
dispatched a lane without ever asking the fill decision, so a coordinator could
dispatch straight past an unread ``review_request`` / ``owner_waiting`` /
``callback_delivery_failed``. The advisory command always exits 0; it can be
skipped or ignored. This module closes that gap by turning the *advisory* fill
decision into an *enforceable* fail-closed gate for the live dispatch path — while
keeping the decision authority itself single (it never re-implements the
:data:`FILL_*` vocabulary; it delegates to :func:`evaluate_fill_decision`).

The gate is deliberately **caller-armed**, not auto-discovered:

- if the caller supplies **no** fill-decision context (``fill_inputs is None``), the
  gate is *not armed* and the dispatch proceeds unchanged — the #12973 / #12988
  live-actuation contract is byte-for-byte back-compatible for callers that do not
  declare a lane set (the actuator discovers nothing; #13290 non-goal);
- if the caller supplies fill context, the gate evaluates the single existing
  authority. A :data:`FILL_DISPATCH_NEXT` result proceeds. Any concrete stop
  (``stop_no_ready_work`` / ``stop_overlap`` / ``stop_coordinator_blocking`` /
  ``stop_soft_profile_full`` / ``stop_owner_or_release_gate``) **fails closed** — the
  dispatch is refused — unless the coordinator passes an explicit override reason;
- an explicit override (a non-blank reason) lets a stop-classified dispatch proceed,
  and the override reason is carried out so the actuator records it (reason + the
  durable anchor already on the outcome) in the durable record. This is the
  formalization of the exact override construct the #13290 dispatch itself dogfooded
  (owner_waiting lanes overridden with an owner_intent anchor).

Scope boundaries (issue #13290) the gate **must not** cross:

- it discovers nothing — every :class:`FillDecisionInputs` field is supplied by the
  caller from the durable record / read model, never read live here;
- it adds no second decision vocabulary — the concrete stop reason always comes from
  :func:`evaluate_fill_decision`;
- it never weakens the advisory CLI — ``workflow fill-decision`` / ``admission`` stay
  exit-0 and untouched. This gate is only consulted on the live dispatch path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FillDecisionInputs,
    FillDecisionOutcome,
    evaluate_fill_decision,
)

# ---------------------------------------------------------------------------
# Gate result vocabulary (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------

#: No fill-decision context was supplied; the gate is not armed and the dispatch
#: proceeds unchanged (back-compat with the #12973 / #12988 contract).
FILL_GATE_NOT_ARMED = "not_armed"
#: The armed fill decision is ``dispatch_next``; the dispatch proceeds.
FILL_GATE_DISPATCH = "dispatch"
#: The armed fill decision is a concrete stop and no override was supplied; the
#: dispatch fails closed.
FILL_GATE_STOP_BLOCKED = "stop_blocked"
#: The armed fill decision is a concrete stop but the coordinator supplied an
#: explicit override reason; the dispatch proceeds and the override is recorded.
FILL_GATE_STOP_OVERRIDDEN = "stop_overridden"

FILL_GATE_RESULTS = frozenset(
    {
        FILL_GATE_NOT_ARMED,
        FILL_GATE_DISPATCH,
        FILL_GATE_STOP_BLOCKED,
        FILL_GATE_STOP_OVERRIDDEN,
    }
)

#: Fail-closed blocked-reason token: the armed fill decision resolved to a concrete
#: stop and no explicit override was supplied, so the live dispatch is refused. The
#: concrete stop reason (``stop_*``) is carried alongside on the outcome.
REASON_FILL_STOP = "fill_decision_stop"


@dataclass(frozen=True)
class DispatchAdmissionDecision:
    """The replayable result of consulting the fill decision for a live dispatch.

    ``gate`` is one of :data:`FILL_GATE_RESULTS`. ``fill`` is the underlying #12855
    :class:`FillDecisionOutcome` (``None`` only when the gate is not armed).
    ``override_reason`` is the explicit reason that let a stop-classified dispatch
    proceed (only set when ``gate`` is :data:`FILL_GATE_STOP_OVERRIDDEN`). ``reason``
    is a short human explanation of the gate decision.
    """

    gate: str
    reason: str
    fill: Optional[FillDecisionOutcome] = None
    override_reason: Optional[str] = None

    @property
    def is_blocked(self) -> bool:
        """True iff the live dispatch must fail closed on this decision."""
        return self.gate == FILL_GATE_STOP_BLOCKED

    @property
    def overridden(self) -> bool:
        """True iff a concrete stop was proceeded past via an explicit override."""
        return self.gate == FILL_GATE_STOP_OVERRIDDEN

    @property
    def armed(self) -> bool:
        """True iff fill-decision context was supplied (the gate was consulted)."""
        return self.gate != FILL_GATE_NOT_ARMED

    @property
    def fill_decision(self) -> Optional[str]:
        """The concrete :data:`FILL_*` token when armed, else ``None``."""
        return self.fill.fill_decision if self.fill is not None else None

    def as_payload(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "reason": self.reason,
            "armed": self.armed,
            "is_blocked": self.is_blocked,
            "overridden": self.overridden,
            "fill_decision": self.fill_decision,
            "override_reason": self.override_reason,
            "fill": self.fill.as_payload() if self.fill is not None else None,
        }


def evaluate_dispatch_admission(
    fill_inputs: Optional[FillDecisionInputs],
    *,
    override_reason: Optional[str] = None,
) -> DispatchAdmissionDecision:
    """Consult the fill decision for a live dispatch (pure, fail-closed, #13290).

    Precedence:

    1. ``fill_inputs is None`` -> :data:`FILL_GATE_NOT_ARMED`: no context was
       declared, so the gate is not armed and the dispatch proceeds unchanged. An
       override reason alone can never block or unblock anything — there is nothing
       to override — so it is ignored here.
    2. the single-authority :func:`evaluate_fill_decision` resolves to
       ``dispatch_next`` -> :data:`FILL_GATE_DISPATCH`: proceed.
    3. it resolves to a concrete stop and ``override_reason`` is blank / absent ->
       :data:`FILL_GATE_STOP_BLOCKED`: fail closed.
    4. it resolves to a concrete stop and a non-blank ``override_reason`` was
       supplied -> :data:`FILL_GATE_STOP_OVERRIDDEN`: proceed, carrying the override
       reason so the actuator records it (reason + durable anchor) in the durable
       record.
    """
    if fill_inputs is None:
        return DispatchAdmissionDecision(
            gate=FILL_GATE_NOT_ARMED,
            reason="no fill-decision context supplied; the dispatch admission gate is "
            "not armed and the dispatch proceeds unchanged",
        )

    fill = evaluate_fill_decision(fill_inputs)
    if fill.should_dispatch:
        return DispatchAdmissionDecision(
            gate=FILL_GATE_DISPATCH,
            reason="fill decision is dispatch_next; the dispatch admission gate "
            "permits the dispatch",
            fill=fill,
        )

    override = (override_reason or "").strip()
    if not override:
        return DispatchAdmissionDecision(
            gate=FILL_GATE_STOP_BLOCKED,
            reason=f"fill decision is a stop ({fill.fill_decision}): {fill.reason}. "
            "Refusing the live dispatch (fail-closed); drain the stop reason, or "
            "proceed intentionally with --override-fill-stop REASON recorded to the "
            "durable anchor",
            fill=fill,
        )

    return DispatchAdmissionDecision(
        gate=FILL_GATE_STOP_OVERRIDDEN,
        reason=f"fill decision is a stop ({fill.fill_decision}) but the coordinator "
        f"supplied an explicit override reason ({override!r}); proceeding and "
        "recording the override to the durable anchor",
        fill=fill,
        override_reason=override,
    )


__all__ = (
    "FILL_GATE_NOT_ARMED",
    "FILL_GATE_DISPATCH",
    "FILL_GATE_STOP_BLOCKED",
    "FILL_GATE_STOP_OVERRIDDEN",
    "FILL_GATE_RESULTS",
    "REASON_FILL_STOP",
    "DispatchAdmissionDecision",
    "evaluate_dispatch_admission",
)
