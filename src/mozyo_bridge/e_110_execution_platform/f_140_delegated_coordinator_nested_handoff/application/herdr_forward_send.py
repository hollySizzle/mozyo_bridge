"""herdr-native one-step forward SEND adapter for the resolved coordinator lane (Redmine #13583).

Increment 3 (Design Answer j#76417, Opt A). When ``workflow step`` resolves a herdr default-lane
pair to a durable coordinator role (:mod:`...domain.workflow_role_authority`), this adapter performs
the **one** ticketless forward the resolved role's one-step-down transition names — the
department-root → project-gateway *consultation*, or the project-gateway → child *work-intake* —
fenced so a repeat / crash / concurrent caller can never duplicate it.

It is the impure side of the pure route matrix (:mod:`...domain.workflow_forward_route`): the matrix
names *which* forward and decides send / zero-send from a resolved target status + fence state; this
adapter resolves the **live** target from the herdr inventory (fail closed on missing / duplicate /
drift / self), consults the **dedicated** duplicate fence
(:class:`...core.state.forward_outbox_fence.ForwardOutboxFence`), and — only on ``ok`` target + open
fence — reserves the fence and performs **exactly one** send through an injected
:class:`ForwardSendPort`, then records the fence outcome. Every negative path performs **zero**
sends.

Safety contract (j#76417):

- point 1: the two legs are distinct (direction-specific plan / primitive / reason).
- point 2: the target is resolved to **exactly one** live herdr assigned-name slot; missing /
  duplicate / drift / locator-missing / self / same-lane are zero-send with a fixed reason.
- point 3: the fence identity is the forward's own anchor-free key — never a synthetic Redmine
  anchor; the message payload reuses the existing ticketless
  :class:`...ticketless_consultation.TicketlessConsultation` /
  ``ticketless_work_intake.TicketlessWorkIntake`` contract but the send authority is a dedicated
  record, not an anchored ``DispatchAuthorization``.
- point 4: a logical forward that is already reserved / delivered / uncertain is a duplicate
  zero-send; an unknown send outcome is marked ``uncertain`` (operator reconcile), never blind
  retried.
- point 6: a dry-run resolves the route + decision only — it writes no fence row and performs no
  send (the resolution-only outcome the pure resolver already emits stays byte-invariant).

The concrete :class:`OrchestrateHandoffForwardSendPort` reuses the gated no-anchor
:func:`orchestrate_handoff` ticketless path with the herdr-resolved target (the same programmatic
payload injection ``project-gateway consult`` / ``child-intake`` do, but with the herdr target /
lane pin instead of the tmux pane resolution). Tests inject a counting fake port so the send-count
(positive = 1, every negative = 0, repeat = 0) is asserted without a live herdr / Redmine.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Protocol, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (
    FENCE_HELD,
    FENCE_OPEN,
    FENCE_UNAVAILABLE,
    SELECT_CHILD_WITH_SELF_FENCE,
    SELECT_SINGLE_LIVE_GATEWAY,
    TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING,
    TARGET_MISSING,
    TARGET_OK,
    TARGET_SELF,
    TICKETLESS_CONSULTATION,
    ForwardRoutePlan,
    ForwardSendDecision,
    ZERO_SEND,
    decide_forward_send,
    plan_forward_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    WorkflowRoleResolution,
)

# The herdr default lane stand-in (mirrors ``herdr_identity.DEFAULT_LANE`` / the pure
# ``workflow_step_herdr.HERDR_DEFAULT_LANE``); a coordinator pair sits here, never a child.
_DEFAULT_LANE = "default"


@dataclass(frozen=True)
class ForwardTargetResolution:
    """The live-resolved forward target, or a fail-closed status (value object).

    ``status`` is one of the pure ``TARGET_*`` tokens. On :data:`TARGET_OK` the target is a single
    live slot: ``assigned_name`` (canonical mzb1 name), ``locator`` (transient live locator), and
    ``lane_id`` are set. Every other status carries no usable target.
    """

    status: str
    assigned_name: str = ""
    locator: str = ""
    lane_id: str = ""
    detail: str = ""


def _decoded_rows(rows, *, decode, locator_of):
    """Yield ``(workspace_id, provider, lane_id, assigned_name, locator)`` for each decodable row."""
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            AGENT_KEY_NAME,
        )

        name = row.get(AGENT_KEY_NAME)
        decode_result = decode(name)
        if not getattr(decode_result, "ok", False) or decode_result.identity is None:
            continue
        identity = decode_result.identity
        yield (
            identity.workspace_id,
            identity.role,
            (identity.lane_id or "").strip() or _DEFAULT_LANE,
            str(name),
            locator_of(row) or "",
        )


def resolve_forward_target(
    plan: ForwardRoutePlan,
    *,
    workspace_id: str,
    sender_lane_id: str,
    target_provider: str,
    gateway_lane_ids: frozenset,
    rows: Sequence[Mapping],
    decode,
    locator_of,
) -> ForwardTargetResolution:
    """Resolve the single live forward target from the herdr inventory (pure over ``rows``).

    - :data:`SELECT_SINGLE_LIVE_GATEWAY` (grandparent → gateway): the live slots whose
      ``(workspace_id, provider, lane_id)`` match this workspace, the target provider, and one of
      the **bound** project-gateway lane ids. Exactly one live lane -> :data:`TARGET_OK`; zero ->
      :data:`TARGET_MISSING`; 2+ -> :data:`TARGET_AMBIGUOUS`; one lane with no usable locator ->
      :data:`TARGET_LOCATOR_MISSING`.
    - :data:`SELECT_CHILD_WITH_SELF_FENCE` (gateway → child): the live target-provider **non-default**
      slots that are NOT bound gateways. A same-lane self-fence excludes the sender's own lane: if
      the only candidate is the sender lane -> :data:`TARGET_SELF`; otherwise the single remaining
      lane resolves (missing / ambiguous / locator-missing as above).

    Pure over the injected ``rows`` + ``decode`` / ``locator_of`` — no IO, so it is exhaustively
    testable. ``decode`` returns a ``HerdrNameDecode`` (``.ok`` / ``.identity``); ``locator_of``
    returns a row's transient live locator (``""`` when absent).
    """
    ws = (workspace_id or "").strip()
    want_provider = (target_provider or "").strip()
    sender_lane = (sender_lane_id or "").strip() or _DEFAULT_LANE

    # lane_id -> (assigned_name, best_locator); a duplicate lane keeps a usable locator if any row
    # has one, but 2+ *distinct* candidate lanes is the ambiguity we fail closed on.
    candidates: dict = {}
    for cand_ws, provider, lane_id, assigned_name, locator in _decoded_rows(
        rows, decode=decode, locator_of=locator_of
    ):
        if cand_ws != ws or provider != want_provider:
            continue
        if plan.select_mode == SELECT_SINGLE_LIVE_GATEWAY:
            if lane_id not in gateway_lane_ids:
                continue
        else:  # SELECT_CHILD_WITH_SELF_FENCE
            if lane_id == _DEFAULT_LANE or lane_id in gateway_lane_ids:
                continue
        prev = candidates.get(lane_id)
        best_locator = locator or (prev[1] if prev else "")
        candidates[lane_id] = (assigned_name, best_locator)

    if plan.select_mode == SELECT_CHILD_WITH_SELF_FENCE:
        if candidates and set(candidates) == {sender_lane}:
            return ForwardTargetResolution(
                status=TARGET_SELF,
                detail="the only live child candidate is the sender's own lane (same-lane)",
            )
        candidates.pop(sender_lane, None)  # same-lane self-fence: never route to self

    if not candidates:
        return ForwardTargetResolution(
            status=TARGET_MISSING, detail="no live target slot for this forward leg"
        )
    if len(candidates) >= 2:
        return ForwardTargetResolution(
            status=TARGET_AMBIGUOUS,
            detail=f"{len(candidates)} live candidate lanes {sorted(candidates)}; never guess",
        )
    lane_id, (assigned_name, locator) = next(iter(candidates.items()))
    if not locator:
        return ForwardTargetResolution(
            status=TARGET_LOCATOR_MISSING,
            assigned_name=assigned_name,
            lane_id=lane_id,
            detail="the single live target has no usable locator to address",
        )
    return ForwardTargetResolution(
        status=TARGET_OK,
        assigned_name=assigned_name,
        locator=locator,
        lane_id=lane_id,
        detail=f"single live target lane {lane_id!r}",
    )


# ---------------------------------------------------------------------------
# The injected send port + its outcome. The port performs the ONE ticketless forward send to the
# already-resolved live target; the adapter guarantees it is called at most once (fence-gated).
# ---------------------------------------------------------------------------

SEND_DELIVERED = "delivered"
SEND_UNCERTAIN = "uncertain"
SEND_FAILED = "failed"


@dataclass(frozen=True)
class ForwardSendOutcome:
    """The outcome of the single forward send (value object).

    ``result`` is :data:`SEND_DELIVERED` (positively delivered), :data:`SEND_UNCERTAIN` (outcome
    unknown -> the fence records uncertain, never blind-retried), or :data:`SEND_FAILED` (a
    fail-closed send that never landed -> the fence records uncertain so a reconcile precedes any
    re-send). ``rc`` is the underlying exit code for the CLI envelope.
    """

    result: str
    rc: int = 0
    detail: str = ""


class ForwardSendPort(Protocol):
    """The one-step forward send seam (injected so tests count sends without a live herdr).

    ``action_id`` is the reserved generation's opaque ``forward_action_id``: the port injects it into
    the outbound ticketless payload so the returning callback can echo it and complete the exact
    forward generation (Redmine #13583 R1-F1).
    """

    def send(
        self,
        plan: ForwardRoutePlan,
        target: ForwardTargetResolution,
        action_id: str,
        *,
        args: argparse.Namespace,
    ) -> ForwardSendOutcome:
        ...


@dataclass(frozen=True)
class ForwardExecutionResult:
    """The result of an attempted one-step forward (value object).

    ``sent`` is True only when the single send fired. ``decision`` / ``target_status`` /
    ``fence_state`` record why; ``reason`` is the fixed zero-send reason on a non-send. ``send`` is
    the :class:`ForwardSendOutcome` when a send fired (else ``None``).
    """

    sent: bool
    decision: str
    target_status: str
    fence_state: str
    reason: str = ""
    detail: str = ""
    send: Optional[ForwardSendOutcome] = None


def execute_herdr_forward(
    resolution: WorkflowRoleResolution,
    *,
    args: argparse.Namespace,
    workspace_id: str,
    sender_lane_id: str,
    target_provider: str,
    gateway_lane_ids: frozenset,
    rows: Sequence[Mapping],
    decode,
    locator_of,
    fence,
    send_port: ForwardSendPort,
) -> ForwardExecutionResult:
    """Resolve, fence, and perform the single one-step forward for a resolved role (fail-closed).

    Sequence (Design Answer j#76528): (1) the durable store must be usable — an un-bootstrapped /
    lost store is a do-not-send ``herdr_forward_fence_unavailable`` (R1-F2: the execution path never
    auto-creates it); (2) a route whose generation is already **active** (reserved / delivered /
    uncertain) is a duplicate zero-send **before** the target is resolved (a duplicate must not even
    read the inventory); (3) resolve the live target — a missing / duplicate / drift / self target is
    a zero-send that consumes **no** generation; (4) reserve the route, minting a fresh opaque
    ``forward_action_id`` (a lost reserve race never sends); (5) perform **exactly one** send through
    ``send_port`` with the minted action id, and record ``delivered`` / ``uncertain`` guarded by that
    id. Every non-send path performs zero sends. ``fence`` / ``send_port`` / ``rows`` / ``decode`` /
    ``locator_of`` are injected so the whole choreography is testable without a live herdr.
    """
    from mozyo_bridge.core.state.forward_outbox_fence import (
        ForwardOutboxFenceError,
        ForwardRouteKey,
    )

    plan = plan_forward_route(resolution.role, resolution.project_scope)
    if plan is None:  # defensive: only the two resolved coordinator roles reach here
        return ForwardExecutionResult(
            sent=False, decision=ZERO_SEND, target_status="", fence_state="",
            reason="herdr_forward_no_route",
            detail=f"role {resolution.role!r} has no one-step forward",
        )

    route = ForwardRouteKey(
        workspace_id=workspace_id,
        from_lane_id=(sender_lane_id or "").strip() or _DEFAULT_LANE,
        from_role=plan.from_role,
        to_role=plan.to_role,
        project_scope=plan.project_scope,
    )

    # (1) the store must be usable; the execution path never bootstraps it (R1-F2).
    if not fence.is_bootstrapped():
        return ForwardExecutionResult(
            sent=False, decision=ZERO_SEND, target_status="", fence_state=FENCE_UNAVAILABLE,
            reason="herdr_forward_fence_unavailable",
            detail="forward store is not bootstrapped (missing / lost); run `workflow forward-fence "
            "--bootstrap` (or --recover after reconcile). The execution path never auto-creates it.",
        )

    # (2) an already-active generation is a duplicate zero-send before any target/inventory read.
    try:
        if fence.is_active(route):
            return ForwardExecutionResult(
                sent=False, decision=ZERO_SEND, target_status="", fence_state=FENCE_HELD,
                reason="herdr_forward_duplicate",
                detail="a forward generation for this route is already reserved / delivered / "
                "uncertain; the next send waits for the correlated callback to complete it",
            )
    except ForwardOutboxFenceError as exc:
        return ForwardExecutionResult(
            sent=False, decision=ZERO_SEND, target_status="", fence_state=FENCE_UNAVAILABLE,
            reason="herdr_forward_fence_unavailable", detail=f"forward store unreadable: {exc}",
        )

    # (3) resolve the live target; a bad target consumes no generation.
    target = resolve_forward_target(
        plan, workspace_id=workspace_id, sender_lane_id=sender_lane_id,
        target_provider=target_provider, gateway_lane_ids=gateway_lane_ids, rows=rows,
        decode=decode, locator_of=locator_of,
    )
    decision: ForwardSendDecision = decide_forward_send(target.status, FENCE_OPEN)
    if not decision.sends:
        return ForwardExecutionResult(
            sent=False, decision=decision.decision, target_status=target.status,
            fence_state=FENCE_OPEN, reason=decision.reason,
            detail=f"{target.detail}; {decision.detail}",
        )

    # (4) reserve the route (mint the generation); a lost race never sends.
    try:
        reserve = fence.reserve(route)
    except ForwardOutboxFenceError as exc:
        return ForwardExecutionResult(
            sent=False, decision=ZERO_SEND, target_status=target.status,
            fence_state=FENCE_UNAVAILABLE, reason="herdr_forward_fence_unavailable",
            detail=f"fence reserve failed: {exc}",
        )
    if not reserve.won:
        return ForwardExecutionResult(
            sent=False, decision=ZERO_SEND, target_status=target.status, fence_state=FENCE_HELD,
            reason="herdr_forward_duplicate",
            detail=f"fence not won (prior {reserve.prior_state}); {reserve.detail}",
        )

    # (4b) the retirement gate (Redmine #13892 R5-F2). This is a real reserve -> send edge
    #      against a resolved slot, so it carries the same guard `execute_dispatch` has: the
    #      retire side publishes `pending` before reading obligations, and this read happens
    #      after our reserve, so whichever side publishes first, the other does nothing. Wiring
    #      only the DispatchOutboxFence edges and reporting that all send edges checked was the
    #      defect (review j#80620 R5-F2).
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (  # noqa: E501
        target_is_retiring,
    )

    _retiring, _retire_detail = target_is_retiring(getattr(target, "assigned_name", ""))
    if _retiring:
        fence.mark_uncertain(route, reserve.action_id, detail=_retire_detail)
        return ForwardExecutionResult(
            sent=False, decision=ZERO_SEND, target_status=target.status,
            fence_state=FENCE_HELD, reason="herdr_forward_target_retiring",
            detail=f"zero-send: {_retire_detail}",
        )

    # (5) exactly one send with the minted action id; record the outcome guarded by that id.
    outcome = send_port.send(plan, target, reserve.action_id, args=args)
    if outcome.result == SEND_DELIVERED:
        fence.mark_delivered(route, reserve.action_id, detail=outcome.detail)
    else:
        # A failed / unknown send is uncertain: never auto-retried; a reconcile precedes any re-send.
        fence.mark_uncertain(route, reserve.action_id, detail=outcome.detail or f"send {outcome.result}")
    return ForwardExecutionResult(
        sent=True, decision=decision.decision, target_status=target.status,
        fence_state=FENCE_OPEN, detail=f"{target.detail}; action_id={reserve.action_id}",
        send=outcome,
    )


class OrchestrateHandoffForwardSendPort:
    """The concrete forward send: the gated no-anchor ticketless send to the herdr target.

    Reuses the EXACT ticketless payload contract ``project-gateway consult`` / ``child-intake``
    inject (safety-contract point 5) — the consultation / work-intake fields, the transition-role
    boundary, and the callback return contract — but delivers to the **herdr-resolved** target
    (``args.target`` = the live locator, ``args.target_lane`` = the target's stable lane identity so
    the herdr rail's ``derive_target_lane`` tier-1 explicit lane wins and the send never re-derives
    to the sender's lane) instead of the tmux pane resolution. The send is wrapped in stdout capture
    + SystemExit→rc containment (the same j#71597 pattern the worker dispatch uses) so the
    ``workflow step`` envelope stays the single structured surface. rc 0 -> delivered; any non-zero
    / fail-closed -> a failed send the caller records ``uncertain`` (never blind-retried).
    """

    def __init__(self, *, repo_root: str, receiver_provider: str) -> None:
        self._repo_root = repo_root
        self._receiver_provider = receiver_provider

    def send(
        self,
        plan: ForwardRoutePlan,
        target: ForwardTargetResolution,
        action_id: str,
        *,
        args: argparse.Namespace,
    ) -> ForwardSendOutcome:
        import contextlib
        import io

        from mozyo_bridge.application.commands import orchestrate_handoff
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
            ROLE_DELEGATED_COORDINATOR,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
            ROLE_GRANDPARENT_COORDINATOR,
            ROLE_PROJECT_GATEWAY,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
            CALLBACK_METHODS,
            CONSULTATION_PROJECT_DOMAIN,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
            WORK_SHAPE_DOMAIN_DESIGN,
        )

        # Copy the workflow-step args so orchestrate_handoff's many getattr()-with-default reads
        # inherit the base (record format, repo), then set the forward-specific send fields. The
        # base args is never mutated (the workflow step still reports its own outcome).
        send_args = argparse.Namespace(**vars(args))
        # R1-F3: the receiver kind is the action-time provider_binding provider for the target role,
        # not a hard-coded codex; the target resolution used the same provider (consistent authority).
        send_args.to = self._receiver_provider
        send_args.target = target.locator
        send_args.target_lane = target.lane_id
        send_args.target_repo = self._repo_root
        send_args.repo = self._repo_root  # pin the herdr effective-backend + workspace root
        send_args.mode = "queue-enter"
        send_args.summary = None
        send_args.callback_methods = list(CALLBACK_METHODS)
        # R1-F1: inject the minted forward generation id so the returning callback echoes it.
        send_args.forward_action_id = action_id
        if plan.ticketless_kind == TICKETLESS_CONSULTATION:
            send_args.transition_role = ROLE_GRANDPARENT_COORDINATOR
            send_args.workflow_contract = ROLE_GRANDPARENT_COORDINATOR
            send_args.consultation_kind = CONSULTATION_PROJECT_DOMAIN
            send_args.callback_to_role = ROLE_GRANDPARENT_COORDINATOR
            send_args.read_contract = ROLE_PROJECT_GATEWAY
            ticketless_kwargs = {"ticketless_consultation": True}
        else:
            send_args.work_shape = WORK_SHAPE_DOMAIN_DESIGN
            send_args.callback_to_role = ROLE_PROJECT_GATEWAY
            send_args.read_contract = ROLE_DELEGATED_COORDINATOR
            ticketless_kwargs = {"ticketless_work_intake": True}

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = int(
                    orchestrate_handoff(
                        send_args,
                        default_kind="design_consultation",
                        ticketless=True,
                        **ticketless_kwargs,
                    )
                    or 0
                )
        except SystemExit as exc:  # die() fail-closed leg -> a non-delivered send
            code = exc.code
            rc = code if isinstance(code, int) and code != 0 else 1
        captured = buf.getvalue().strip()
        if rc == 0:
            return ForwardSendOutcome(result=SEND_DELIVERED, rc=0, detail="ticketless forward sent")
        return ForwardSendOutcome(
            result=SEND_FAILED, rc=rc, detail=f"forward send fail-closed (rc={rc}): {captured[:200]}"
        )


__all__ = (
    "ForwardTargetResolution",
    "resolve_forward_target",
    "SEND_DELIVERED",
    "SEND_UNCERTAIN",
    "SEND_FAILED",
    "ForwardSendOutcome",
    "ForwardSendPort",
    "ForwardExecutionResult",
    "execute_herdr_forward",
    "OrchestrateHandoffForwardSendPort",
)
