"""Pure herdr-native one-step forward-route matrix for the resolved coordinator roles (Redmine #13583).

Increment 1 gave the herdr default-lane pair a durable workflow-role authority: a
:class:`~...domain.workflow_role_authority.WorkflowRoleResolution` names the lane's canonical
role (``grandparent_coordinator`` / ``project_gateway``) but performed **no** send — the resolved
outcome was ``no_op`` / ``primitive=none`` (``herdr_role_resolved_forward_pending``).

Increment 3 (Design Answer j#76417, Opt A) wires the **one-step forward SEND** onto that resolved
role. This module is the *pure* route matrix at the centre of it: it maps a resolved role to a
:class:`ForwardRoutePlan` — the single one-step-down transition the lane may take, named with a
**direction-specific** primitive + reason so the two legs are never conflated (safety contract
point 1) — and it decides, purely, whether a resolved target + fence state permit the single send
or must fail closed to a **zero-send** with a fixed reason (points 2 / 4).

It is pure: value objects + total functions over plain tokens. It opens no file, reads no env,
scans no inventory, and performs no send. The application adapter
(:mod:`...application.herdr_forward_send`) supplies the live-resolved target status and the
durable fence state; the cli leg fires the one send. The forward semantics themselves are the
department-root → project-gateway consultation and the project-gateway → child work-intake the
``vibes/docs/logics/workflow-step-command-design.md`` §祖父・親・子・孫 swimlane fixes:

- ``grandparent_coordinator`` → the **single live** cockpit-visible ``project_gateway`` in the
  herdr inventory (0 / 2+ / drift fail closed): a ticketless *consultation* forward;
- ``project_gateway`` → the child ``delegated_coordinator`` resolved with a **same-lane
  self-fence** (same-lane / missing / ambiguous fail closed): a ticketless *work-intake* forward.

The message payloads reuse the existing ticketless contracts
(:class:`...f_130_handoff_routing.domain.ticketless_consultation.TicketlessConsultation` /
``ticketless_work_intake.TicketlessWorkIntake``); this module only names *which* forward and
*whether* it may fire — it does not build the payload or perform the send.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
)

# ---------------------------------------------------------------------------
# Direction tokens (machine-readable; kept literal regardless of UI language). The two legs are
# distinct so a caller / test / durable record never conflates the grandparent consultation with
# the project-gateway work-intake (safety contract point 1).
# ---------------------------------------------------------------------------
FORWARD_GRANDPARENT_TO_GATEWAY = "grandparent_to_project_gateway"
FORWARD_GATEWAY_TO_CHILD = "project_gateway_to_delegated_coordinator"

# ---------------------------------------------------------------------------
# Direction-specific forward primitives + ready reasons. Separate tokens per leg (point 1) so the
# resolution-only outcome and the executed leg are never a single generic "forward" token.
# ---------------------------------------------------------------------------
PRIMITIVE_HERDR_FORWARD_CONSULT = "herdr_forward_consultation"
PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE = "herdr_forward_child_intake"

REASON_HERDR_FORWARD_CONSULT_READY = "herdr_forward_consultation_ready"
REASON_HERDR_FORWARD_CHILD_INTAKE_READY = "herdr_forward_child_intake_ready"

# ---------------------------------------------------------------------------
# Target-selection mode: how the application adapter resolves the live target for a leg.
# ---------------------------------------------------------------------------
#: grandparent -> the single live project-gateway lane in the herdr inventory (0 / 2+ fail closed).
SELECT_SINGLE_LIVE_GATEWAY = "single_live_project_gateway"
#: project_gateway -> the child delegated_coordinator resolved with a same-lane self-fence.
SELECT_CHILD_WITH_SELF_FENCE = "child_with_self_fence"

# ---------------------------------------------------------------------------
# Ticketless payload kind reused for each leg (the message envelope, not built here).
# ---------------------------------------------------------------------------
TICKETLESS_CONSULTATION = "ticketless_consultation"
TICKETLESS_WORK_INTAKE = "ticketless_work_intake"

# ---------------------------------------------------------------------------
# Fixed zero-send reasons (safety contract points 2 / 4). Every path that resolves a target or a
# fence state but must not send names one of these — a caller never guesses a target or re-sends.
# ---------------------------------------------------------------------------
#: The live inventory has no project-gateway / child target for this leg (0 live matches).
REASON_HERDR_FORWARD_TARGET_MISSING = "herdr_forward_target_missing"
#: The live inventory has 2+ candidate targets (drift / duplicate identity) — never guess one.
REASON_HERDR_FORWARD_TARGET_AMBIGUOUS = "herdr_forward_target_ambiguous"
#: The single candidate target has no usable live locator (present but unaddressable).
REASON_HERDR_FORWARD_TARGET_LOCATOR_MISSING = "herdr_forward_target_locator_missing"
#: The resolved target is the sender's own lane (self / same-lane route) — the self-fence tripped.
REASON_HERDR_FORWARD_SELF_ROUTE = "herdr_forward_self_route"
#: A logical forward for this (role, target) is already pending / inflight / delivered-uncertain —
#: a duplicate must not re-send (the durable fence holds it).
REASON_HERDR_FORWARD_DUPLICATE = "herdr_forward_duplicate"
#: The durable duplicate fence is unavailable (do-not-send: a send could duplicate).
REASON_HERDR_FORWARD_FENCE_UNAVAILABLE = "herdr_forward_fence_unavailable"

# The ready reason for a plan, keyed by direction.
_READY_REASON = {
    FORWARD_GRANDPARENT_TO_GATEWAY: REASON_HERDR_FORWARD_CONSULT_READY,
    FORWARD_GATEWAY_TO_CHILD: REASON_HERDR_FORWARD_CHILD_INTAKE_READY,
}


@dataclass(frozen=True)
class ForwardRoutePlan:
    """The pure one-step-down forward plan for a resolved coordinator role (value object).

    ``direction`` is the leg (:data:`FORWARD_GRANDPARENT_TO_GATEWAY` /
    :data:`FORWARD_GATEWAY_TO_CHILD`); ``from_role`` / ``to_role`` are the canonical transition
    roles; ``primitive`` / ``ready_reason`` are the direction-specific tokens; ``select_mode`` tells
    the adapter how to resolve the live target; ``ticketless_kind`` names the reused payload
    envelope. ``project_scope`` is the gateway's declared scope for the child-intake leg (``""`` for
    the grandparent consultation, whose target scope is the resolved gateway's, not the caller's).
    """

    direction: str
    from_role: str
    to_role: str
    primitive: str
    ready_reason: str
    select_mode: str
    ticketless_kind: str
    project_scope: str = ""


def plan_forward_route(role: str, project_scope: str = "") -> Optional[ForwardRoutePlan]:
    """The one-step forward plan for a resolved canonical role, or ``None`` if it has none (pure).

    - ``grandparent_coordinator`` -> a consultation forward to the single live project gateway.
    - ``project_gateway`` -> a work-intake forward to the child delegated_coordinator (self-fenced).
    - any other / empty role -> ``None`` (the caller keeps the resolution-only outcome; only the
      two resolved coordinator roles have a herdr-native one-step forward).

    Pure and total: no IO, no target resolution — it only names the leg and how to resolve it.
    """
    token = (role or "").strip()
    if token == ROLE_GRANDPARENT_COORDINATOR:
        return ForwardRoutePlan(
            direction=FORWARD_GRANDPARENT_TO_GATEWAY,
            from_role=ROLE_GRANDPARENT_COORDINATOR,
            to_role=ROLE_PROJECT_GATEWAY,
            primitive=PRIMITIVE_HERDR_FORWARD_CONSULT,
            ready_reason=_READY_REASON[FORWARD_GRANDPARENT_TO_GATEWAY],
            select_mode=SELECT_SINGLE_LIVE_GATEWAY,
            ticketless_kind=TICKETLESS_CONSULTATION,
            project_scope="",
        )
    if token == ROLE_PROJECT_GATEWAY:
        return ForwardRoutePlan(
            direction=FORWARD_GATEWAY_TO_CHILD,
            from_role=ROLE_PROJECT_GATEWAY,
            to_role=ROLE_DELEGATED_COORDINATOR,
            primitive=PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE,
            ready_reason=_READY_REASON[FORWARD_GATEWAY_TO_CHILD],
            select_mode=SELECT_CHILD_WITH_SELF_FENCE,
            ticketless_kind=TICKETLESS_WORK_INTAKE,
            project_scope=(project_scope or "").strip(),
        )
    return None


# ---------------------------------------------------------------------------
# The pure send / zero-send decision. Given the *already-resolved* live-target status and the
# durable fence state, decide whether the single send may fire. Never resolves a target or reads a
# fence itself — the adapter supplies both, so this stays pure and exhaustively testable.
# ---------------------------------------------------------------------------

# Target-resolution status tokens the adapter maps from the live inventory resolution.
TARGET_OK = "ok"  # exactly one live target with a usable locator
TARGET_MISSING = "missing"  # zero live targets
TARGET_AMBIGUOUS = "ambiguous"  # 2+ live targets (drift / duplicate)
TARGET_LOCATOR_MISSING = "locator_missing"  # one target, no usable live locator
TARGET_SELF = "self"  # the resolved target is the sender's own lane (self / same-lane)

# Fence-state tokens the adapter maps from the durable duplicate fence (mirrors the outbox fence).
FENCE_OPEN = "open"  # no prior forward for this key -> the single send may proceed
FENCE_HELD = "held"  # a prior forward is pending / inflight / delivered / uncertain -> zero-send
FENCE_UNAVAILABLE = "unavailable"  # the fence could not be consulted -> do-not-send (fail closed)

SEND = "send"
ZERO_SEND = "zero_send"

_TARGET_ZERO_SEND_REASON = {
    TARGET_MISSING: REASON_HERDR_FORWARD_TARGET_MISSING,
    TARGET_AMBIGUOUS: REASON_HERDR_FORWARD_TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING: REASON_HERDR_FORWARD_TARGET_LOCATOR_MISSING,
    TARGET_SELF: REASON_HERDR_FORWARD_SELF_ROUTE,
}


@dataclass(frozen=True)
class ForwardSendDecision:
    """Whether the one-step forward may send, or a fixed zero-send reason (value object).

    ``decision`` is :data:`SEND` (proceed to exactly one send) or :data:`ZERO_SEND` (fail closed,
    perform no send). ``reason`` is the fixed zero-send reason token for a :data:`ZERO_SEND`
    (``""`` for :data:`SEND`).
    """

    decision: str
    reason: str = ""
    detail: str = ""

    @property
    def sends(self) -> bool:
        return self.decision == SEND


def decide_forward_send(target_status: str, fence_state: str) -> ForwardSendDecision:
    """Decide send / zero-send from the resolved target status + the durable fence state (pure).

    Target first (a bad target never sends regardless of the fence), then the fence (a held /
    unavailable fence blocks even a good target). Every non-send path names a fixed reason:

    - target missing / ambiguous / locator-missing / self -> the matching target zero-send reason;
    - fence held (a duplicate logical forward already pending / inflight / uncertain) ->
      :data:`REASON_HERDR_FORWARD_DUPLICATE`;
    - fence unavailable -> :data:`REASON_HERDR_FORWARD_FENCE_UNAVAILABLE` (do-not-send);
    - only an ``ok`` target on an ``open`` fence -> :data:`SEND`.
    """
    status = (target_status or "").strip()
    if status != TARGET_OK:
        reason = _TARGET_ZERO_SEND_REASON.get(status, REASON_HERDR_FORWARD_TARGET_MISSING)
        return ForwardSendDecision(
            decision=ZERO_SEND, reason=reason, detail=f"target status {status!r}"
        )
    fence = (fence_state or "").strip()
    if fence == FENCE_OPEN:
        return ForwardSendDecision(decision=SEND, detail="target resolved, fence open")
    if fence == FENCE_HELD:
        return ForwardSendDecision(
            decision=ZERO_SEND,
            reason=REASON_HERDR_FORWARD_DUPLICATE,
            detail="a logical forward for this key is already pending / inflight / uncertain",
        )
    return ForwardSendDecision(
        decision=ZERO_SEND,
        reason=REASON_HERDR_FORWARD_FENCE_UNAVAILABLE,
        detail=f"duplicate fence state {fence!r}; fail closed rather than risk a duplicate send",
    )


__all__ = (
    "FORWARD_GRANDPARENT_TO_GATEWAY",
    "FORWARD_GATEWAY_TO_CHILD",
    "PRIMITIVE_HERDR_FORWARD_CONSULT",
    "PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE",
    "REASON_HERDR_FORWARD_CONSULT_READY",
    "REASON_HERDR_FORWARD_CHILD_INTAKE_READY",
    "SELECT_SINGLE_LIVE_GATEWAY",
    "SELECT_CHILD_WITH_SELF_FENCE",
    "TICKETLESS_CONSULTATION",
    "TICKETLESS_WORK_INTAKE",
    "REASON_HERDR_FORWARD_TARGET_MISSING",
    "REASON_HERDR_FORWARD_TARGET_AMBIGUOUS",
    "REASON_HERDR_FORWARD_TARGET_LOCATOR_MISSING",
    "REASON_HERDR_FORWARD_SELF_ROUTE",
    "REASON_HERDR_FORWARD_DUPLICATE",
    "REASON_HERDR_FORWARD_FENCE_UNAVAILABLE",
    "ForwardRoutePlan",
    "plan_forward_route",
    "TARGET_OK",
    "TARGET_MISSING",
    "TARGET_AMBIGUOUS",
    "TARGET_LOCATOR_MISSING",
    "TARGET_SELF",
    "FENCE_OPEN",
    "FENCE_HELD",
    "FENCE_UNAVAILABLE",
    "SEND",
    "ZERO_SEND",
    "ForwardSendDecision",
    "decide_forward_send",
)
