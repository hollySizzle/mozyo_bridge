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

import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    decode_assigned_name,
)
from mozyo_bridge.core.state.dispatch_outbox_fence import (
    FENCE_ABSENT,
    FENCE_CANCELLED,
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


def _live_locator_for(assigned_name: str) -> str:
    """The live locator of a named slot, or ``""`` when it cannot be observed. (fail-soft)

    Fail-soft on purpose: an unreadable inventory yields ``""``, which
    :meth:`ScratchRetirementFence.blocking_attempt_for_target` treats as ambiguous and blocks.
    The safety decision stays in the authority; this only supplies the fact.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        AGENT_KEY_NAME,
        _agent_locator,
    )

    want = _norm(assigned_name)
    try:
        rows = list_herdr_agent_rows(os.environ)
    except Exception:  # noqa: BLE001 - unreadable -> "" -> the authority fails closed
        return ""
    matches = [
        r for r in rows
        if isinstance(r, Mapping) and _norm(r.get(AGENT_KEY_NAME)) == want
    ]
    if len(matches) != 1:
        return ""  # absent or ambiguous -> let the authority decide conservatively
    return _norm(_agent_locator(matches[0]))


def target_is_retiring(target_assigned_name: str) -> tuple[bool, str]:
    """Is this exact target inside a retirement transaction? (Redmine #13892 R3-F1 / R4-F3)

    The shared guard for EVERY outbox reserve -> send edge. Exported rather than private
    because `execute_dispatch` is not the only such edge: the callback sweep, the operator
    startup resume and the hibernated pair redispatch all reserve on the same
    `DispatchOutboxFence` with a `target_assigned_name` and then send. Wiring only one of
    them (and claiming all were covered) was review j#80594 R4-F3.

    Reads the retirement authority for the unit this target belongs to. Returns
    ``(True, reason)`` when a ``pending`` or ``completed`` attempt names the target's slot —
    the send must not land in panes that are being (or have been) retired.

    Fail-closed on an unreadable authority: a send we cannot prove is safe is not sent. A
    genuinely absent authority means no retirement was ever recorded, so the send proceeds
    (this is the ordinary case for every non-scratch lane, and it must not be over-blocked).
    """
    from mozyo_bridge.core.state.scratch_retirement_fence import (
        ScratchRetirementFence,
        ScratchRetirementFenceError,
    )

    target = _norm(target_assigned_name)
    if not target:
        return (False, "")
    decode = decode_assigned_name(target)
    if not decode.ok or decode.identity is None:
        # Not a managed mzb1 slot: the retirement authority is keyed on decoded units, so it
        # structurally cannot hold an attempt for this target.
        return (False, "")
    identity = decode.identity
    # The live locator distinguishes "the pane a completed attempt closed" from "a pair
    # relaunched at the same deterministic name" (review j#80594 R4-F2). Reading it is
    # fail-soft: an unknown locator makes a completed attempt ambiguous, which blocks.
    live_locator = _live_locator_for(target)
    try:
        fence = ScratchRetirementFence()
        # A scratch unit's digest is over the pair's full assigned-name set, so a single
        # target cannot rebuild it. Ask the authority for any attempt that forbids this send.
        attempt = fence.blocking_attempt_for_target(
            workspace_id=identity.workspace_id,
            lane_id=identity.lane_id,
            target_assigned_name=target,
            live_locator=live_locator,
        )
    except ScratchRetirementFenceError as exc:
        return (
            True,
            f"the retirement authority is unreadable ({exc}); refusing to send into a target "
            "whose retirement state cannot be established",
        )
    if attempt is None:
        return (False, "")
    return (
        True,
        f"the target {target} is inside a {attempt.state} retirement attempt "
        f"(revision {attempt.revision}, live locator {live_locator or '<unobservable>'}); "
        "sending would race a close or land in a pane that was closed",
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

    # We won the reserve. Before the send, check whether this exact target is being retired
    # (Redmine #13892 review j#80523 R3-F1, design j#80526). The retire side publishes its
    # `pending` intent BEFORE it reads obligations, and this read happens AFTER our reserve, so
    # one of the two always sees the other:
    #
    #   - our reserve lands first  -> the retire's obligation read sees it and closes nothing;
    #   - the retire's pending lands first -> we see it here and send nothing.
    #
    # Without this half, holding the retire lock published nothing to us: a dispatch could
    # reserve and send into panes that were about to be closed.
    retiring, detail = target_is_retiring(authorization.target_assigned_name)
    if retiring:
        fence.mark_cancelled(key, detail=detail, now=now)
        return DispatchExecutionResult(
            result=DISPATCH_SKIPPED,
            fence_state=FENCE_CANCELLED,
            detail=f"zero-send: {detail}",
            sent=False,
        )

    # We won the reserve and the target is not being retired: perform exactly one send attempt.
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
