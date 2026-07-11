"""Fenced one-step worker dispatch execution (Redmine #13489 increment 2).

The design contract's execution boundary (``### Increment 2 dispatch 再有効化 contract``): a
single ``workflow step`` may perform, at most, **reserve + exactly one exact-target send
attempt + one outcome write**. This module is that boundary — it takes an already-decided
:class:`~...domain.dispatch_authority.DispatchDecision` (an ``AUTHORIZE``) and drives the
:class:`~...core.state.dispatch_outbox_fence.DispatchOutboxFence` around one injected send:

- **reserve first.** Only the caller that wins a fresh :data:`FENCE_RESERVED` row sends. A key
  already reserved / delivered / uncertain / cancelled is **never-send** (zero additional send).
- **exactly one send.** The ``send`` seam is invoked at most once, only after a winning reserve.
- **fail-closed outcome write.** A positive ACK -> :data:`FENCE_DELIVERED`; a raised / unknown
  send outcome -> :data:`FENCE_UNCERTAIN` (the send may have landed — **never auto-retried**;
  operator reconcile + a new ``action_id`` is the only re-attempt). A corrupt / unavailable
  fence fails closed with **no send**.

The ``send`` seam returns a :class:`SendOutcome`; the real CLI wiring builds it from the herdr
worker dispatcher, and the required regressions inject a counting fake so the fence semantics
are proven without any live delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    FENCE_ABSENT,
    DispatchOutboxFence,
    DispatchOutboxFenceError,
    FenceKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    DispatchAuthorization,
)

# Execution result tokens (machine-readable; literal regardless of UI language).
DISPATCH_DELIVERED = "dispatch_delivered"  # reserved + sent + positive turn-start
DISPATCH_UNCERTAIN = "dispatch_uncertain"  # reserved + sent, but the outcome is unknown
DISPATCH_SKIPPED = "dispatch_skipped"  # never-send: the key was already fenced
DISPATCH_FENCE_UNAVAILABLE = "dispatch_fence_unavailable"  # corrupt/unreadable fence -> no send

# Structured turn-start observation tokens (mid-review j#75047 F2): ``delivered`` is confirmed
# ONLY by a positive turn-start ``started``, never by a bare delivery ACK. The ACK/delivery/
# completion layers are distinct (``ack-completion-receiver-state.md``): a submit-completion ACK
# proves the message landed in the composer, not that the receiver's turn actually started.
TURN_START_STARTED = "started"  # the receiver's turn positively started -> delivered
TURN_START_ACK_ONLY = "ack_only"  # a delivery ACK only, no turn-start confirmation -> uncertain
TURN_START_NOT_STARTED = "not_started"  # observed, but the turn did not start -> uncertain
TURN_START_TIMEOUT = "timeout"  # the turn-start observation timed out -> uncertain
TURN_START_UNKNOWN = "unknown"  # the turn-start could not be observed -> uncertain


@dataclass(frozen=True)
class SendOutcome:
    """The result of the single send attempt: a **structured turn-start** observation.

    ``turn_start`` is one of the :data:`TURN_START_*` tokens. Only :data:`TURN_START_STARTED`
    confirms :data:`FENCE_DELIVERED` (mid-review j#75047 F2); every other token — an ACK-only
    submit-completion, a not-started / timeout / unknown observation, or a raised exception the
    orchestrator catches — leaves the fence :data:`FENCE_UNCERTAIN` for operator reconcile. No
    raw LLM wait is introduced; the seam returns whatever structured turn-start the transport
    surfaced.
    """

    turn_start: str
    detail: str = ""

    @property
    def started(self) -> bool:
        return self.turn_start == TURN_START_STARTED


@dataclass(frozen=True)
class DispatchExecutionResult:
    """The replayable result of one fenced dispatch execution."""

    result: str
    fence_state: str
    detail: str = ""
    sent: bool = False

    @property
    def ok(self) -> bool:
        """A skipped never-send and a delivered send are both non-error outcomes."""
        return self.result in (DISPATCH_DELIVERED, DISPATCH_SKIPPED)


def fence_key_for(authorization: DispatchAuthorization) -> FenceKey:
    """The UNIQUE fence key for an authorization (its recording journal + action id + target)."""
    return FenceKey(
        workspace_id=authorization.workspace_id,
        lane_id=authorization.lane_id,
        issue=authorization.issue,
        journal=authorization.journal,
        action_id=authorization.action_id,
        target_assigned_name=authorization.target_assigned_name,
    )


def execute_dispatch(
    *,
    authorization: DispatchAuthorization,
    fence: DispatchOutboxFence,
    send: Callable[[], SendOutcome],
    now: Optional[str] = None,
) -> DispatchExecutionResult:
    """Reserve, send at most once, and write the outcome (the one-step dispatch boundary).

    ``authorization`` must be the AUTHORIZE decision's authorization (valid, non-superseded,
    exact-target awaiting_input — decided upstream). ``fence`` is the home-scoped idempotency
    authority; ``send`` is the single exact-target send seam (invoked at most once, only after a
    winning reserve). Returns the fenced result; never raises for a corrupt fence (fail-closed
    :data:`DISPATCH_FENCE_UNAVAILABLE`, no send).
    """
    key = fence_key_for(authorization)
    try:
        reservation = fence.reserve(key, now=now)
    except DispatchOutboxFenceError as exc:
        return DispatchExecutionResult(
            result=DISPATCH_FENCE_UNAVAILABLE,
            fence_state=FENCE_ABSENT,
            detail=f"idempotency fence unavailable; no send ({exc})",
            sent=False,
        )

    if not reservation.won:
        # Never-send: the key was already reserved / delivered / uncertain / cancelled.
        return DispatchExecutionResult(
            result=DISPATCH_SKIPPED,
            fence_state=reservation.current_state,
            detail=(
                f"never-send: key already fenced ({reservation.prior_state}); "
                + reservation.detail
            ),
            sent=False,
        )

    # We won the reserve: perform exactly one send attempt.
    try:
        outcome = send()
    except Exception as exc:  # noqa: BLE001 - the send may have landed; mark uncertain, never retry
        fence.mark_uncertain(
            key, detail=f"send raised {type(exc).__name__}; outcome unknown", now=now
        )
        return DispatchExecutionResult(
            result=DISPATCH_UNCERTAIN,
            fence_state="uncertain",
            detail=f"send attempt raised {type(exc).__name__}; outcome unknown -> reconcile",
            sent=True,
        )

    if outcome.started:
        fence.mark_delivered(key, detail=outcome.detail or "turn-start confirmed", now=now)
        return DispatchExecutionResult(
            result=DISPATCH_DELIVERED,
            fence_state="delivered",
            detail=outcome.detail or "reserved, sent, turn-start confirmed",
            sent=True,
        )
    # Any non-``started`` turn-start (ack-only / not-started / timeout / unknown) -> uncertain:
    # the send may have landed but the receiver's turn is not confirmed to have started.
    fence.mark_uncertain(
        key,
        detail=outcome.detail or f"turn-start {outcome.turn_start}; not confirmed started",
        now=now,
    )
    return DispatchExecutionResult(
        result=DISPATCH_UNCERTAIN,
        fence_state="uncertain",
        detail=(
            outcome.detail
            or f"turn-start {outcome.turn_start} (not started); outcome uncertain -> reconcile"
        ),
        sent=True,
    )


__all__ = (
    "DISPATCH_DELIVERED",
    "DISPATCH_UNCERTAIN",
    "DISPATCH_SKIPPED",
    "DISPATCH_FENCE_UNAVAILABLE",
    "TURN_START_STARTED",
    "TURN_START_ACK_ONLY",
    "TURN_START_NOT_STARTED",
    "TURN_START_TIMEOUT",
    "TURN_START_UNKNOWN",
    "SendOutcome",
    "DispatchExecutionResult",
    "fence_key_for",
    "execute_dispatch",
)
