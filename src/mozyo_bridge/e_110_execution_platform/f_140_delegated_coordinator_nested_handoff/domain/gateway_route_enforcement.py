"""Gateway-route enforcement for the handoff command surface (Redmine #12918).

The governed development route is fixed:

    coordinator Codex -> sublane Codex gateway -> same-lane Claude worker.

Until #12918 that route was enforced only by the ``AGENTS.md`` / workflow-doc
operating convention. In practice a coordinator repeatedly *dispatched
implementation_request directly to each sublane Claude worker* instead of
entering through that sublane's Codex gateway (#12670 j#68733: "coordinator
dispatched #12642 and #12669 implementation_request directly to each sublane
Claude"). The durable implementation_request journals stayed valid anchors, but
the direct Claude pane delivery skipped the gateway that owns lane coordination,
review/callback routing, and the coordinator callback. This module is the pure,
machine-checkable gate that makes that bypass **fail closed** at the command
surface instead of relying on the convention.

What it governs (and, deliberately, what it does not):

- Only **implementation-shaped work and review_result** are governed
  (:data:`GATEWAY_GOVERNED_KINDS`). Every other handoff kind — ``design_consultation``,
  ``review_request`` (the worker's *outbound* callback, not a coordinator->worker
  delivery), ``reply``, ``implementation_done``, ``custom`` — is left untouched, so
  read-only / design-consultation / summary uses of a main-lane Claude are never
  blocked (acceptance: "preserve allowed main-lane Claude read-only / design
  consultation / summary workflows").
- A governed kind addressed **to the Codex gateway** (``receiver == codex``) is the
  governed route itself and is always allowed — that is exactly
  ``coordinator -> sublane Codex gateway``.
- A governed kind addressed **to a Claude worker** (``receiver == claude``) is allowed
  ONLY when it is the legitimate terminal hop ``same-lane gateway -> same-lane
  worker`` — the sender and the resolved target share one Unit
  ``(workspace_id, lane_id)``. A *cross-lane* Claude-worker delivery (the coordinator
  reaching into a sublane worker that lives in a different lane than the sender) is
  the recorded failure mode and fails closed.
- The block can be released only by an **explicit durable exception**
  (``allow_direct_worker``), which is reported as a distinct
  :data:`ROUTE_EXCEPTION` verdict so it is recorded apart from the normal route
  (acceptance: "explicit durable exception ... recorded distinctly from the normal
  route").

Why the **Unit (workspace_id, lane_id)** is the discriminator, not the tmux
session: the cross-session ``--to claude`` gate (Redmine #10332) already blocks a
coordinator typing into a *different session's* Claude. The #12918 gap is the
*same-session, different-lane* cockpit case — the coordinator's own window and a
sublane worker's window share one tmux session but belong to different lane Units
(``@mozyo_lane_id``, Redmine #11820). So the gate keys on the lane Unit, which is
the public-safe stable identity (``route_identity_ledger``: a pane id is a cache,
never the route authority), and a lane id is safe to echo into a durable record.

Non-cockpit safety: when the resolved target carries **no lane metadata**
(``target_lane_id`` empty) it is not a recognizable managed sublane worker, so the
gate does not fire — a plain two-window non-cockpit ``gateway -> worker`` dispatch
keeps working and is left to the existing session/agent gates. The gate only
*adds* a block for the precise cockpit cross-lane bypass shape.

This module is **pure**: value objects + total functions over plain strings. It
opens no tmux, reads no Redmine, sends nothing. Resolving the sender / target lane
Unit from the live panes and threading the decision into the ``handoff`` command
(emitting the structured outcome and failing closed) is the caller's concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# The handoff-kind vocabulary the gate selects from lives one layer down in the
# handoff-routing package; importing it (rather than re-declaring the strings)
# keeps the governed set from drifting out of ``KIND_LABELS``. f_140 already
# depends on f_130 (role_profile), so this import direction introduces no cycle.
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    KIND_LABELS,
)

# Provider / receiver tokens. ``claude`` is the worker surface a governed kind may
# only reach via its same-lane gateway; reused from the role/provider binding so
# this module and the binding cannot drift on the literal. Any non-``claude``
# receiver (the ``codex`` gateway) is the governed route head.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
)

#: Implementation-shaped kinds whose delivery is route-governed (#12918 objective:
#: "implementation-shaped work and review_result"). ``review_request`` is NOT here:
#: it is the worker's outbound review-request callback, not a coordinator->worker
#: delivery, and governing it would block the legitimate same-lane callback.
GATEWAY_GOVERNED_KINDS: frozenset[str] = frozenset(
    {"implementation_request", "review_result"}
)

# Defense against a future rename of the handoff vocabulary silently emptying the
# governed set: every governed kind must be a real handoff kind.
assert GATEWAY_GOVERNED_KINDS <= KIND_LABELS, (
    "GATEWAY_GOVERNED_KINDS must be a subset of handoff.KIND_LABELS; "
    f"unknown: {sorted(GATEWAY_GOVERNED_KINDS - KIND_LABELS)}"
)

# ---------------------------------------------------------------------------
# Verdict tokens. Exactly one is carried by a decision; each block/exception is a
# distinct, durable-record-safe diagnostic.
# ---------------------------------------------------------------------------
#: The delivery satisfies the governed route (or the kind is not governed).
ROUTE_ALLOWED: str = "route_allowed"
#: A governed kind would be delivered directly to a cross-lane Claude worker,
#: bypassing that lane's Codex gateway. Fails closed (acceptance #1).
ROUTE_BLOCKED: str = "gateway_route_blocked"
#: The cross-lane worker delivery was admitted *only* because an explicit durable
#: exception was supplied. Allowed, but recorded distinctly from the normal route.
ROUTE_EXCEPTION: str = "gateway_route_exception"

#: The single ``blocked_reason`` token a :data:`ROUTE_BLOCKED` decision carries.
BLOCKED_DIRECT_WORKER_BYPASS: str = "coordinator_to_sublane_worker_bypass"
#: A governed delivery addressed to a provider that is NEITHER the binding-resolved
#: gateway nor the worker (Redmine #13569 R1-F3). With the receiver vocabulary opened to
#: every registered provider (2A), "not the worker" no longer implies "the gateway", so a
#: cross-boundary governed send to an arbitrary third provider must fail closed rather than
#: be mistaken for the always-allowed gateway route head.
BLOCKED_NON_GATEWAY_RECEIVER: str = "governed_route_non_gateway_receiver"

#: The backward-compatible lane id a missing / empty ``@mozyo_lane_id`` resolves to
#: (Redmine #11820, mirrored from ``agent_discovery._normalize_lane_display``). A
#: non-cockpit pane carries no lane option and normalizes to this lane, so a plain
#: ``default`` -> ``default`` dispatch reads as same-lane and is never blocked.
DEFAULT_LANE: str = "default"


def _norm(value: object) -> str:
    """Trim a raw token to a comparable string (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


def _norm_lane(value: object) -> str:
    """Normalize a lane token, mapping missing / empty to :data:`DEFAULT_LANE`."""
    return _norm(value) or DEFAULT_LANE


@dataclass(frozen=True)
class GatewayRouteRequest:
    """The facts a handoff delivery is route-checked against (pure inputs).

    ``kind`` / ``receiver`` are the handoff intent. The lane Unit of the sender and
    the resolved target are ``(*_workspace_id, *_lane_id)``; a lane id is the
    public-safe stable identity the gate keys on (never a pane id). A missing lane
    normalizes to :data:`DEFAULT_LANE`.

    ``sender_identity_known`` records whether the sender pane's lane Unit could be
    resolved at all (i.e. the command ran from a managed pane the live inventory
    knows). When it is ``False`` — run outside tmux, or from a pane the inventory
    does not carry — the gate **cannot prove** a cross-lane bypass and stays out of
    the way, mirroring the cross-session ``--to claude`` gate which is skipped when
    the sender session is unknown. ``allow_direct_worker`` is the explicit
    durable-exception flag.

    ``worker_provider`` is the runtime provider bound to the **implementer (worker)
    role** for this repo (Redmine #13174). The authority decision — "is this delivery
    addressed to a worker (and thus the terminal ``gateway -> worker`` hop that must be
    same-lane), or to the gateway (the always-allowed route head)?" — keys on the role,
    not a literal ``claude``. The caller resolves it from the repo-local
    :class:`RoleProviderBinding` and threads it in; ``None`` (or empty) falls back to
    :data:`PROVIDER_CLAUDE`, so the default binding is byte-identical to the pre-#13174
    gate. The ``receiver`` token itself stays a delivery attribute — the gate only
    compares it against the role-resolved worker provider.
    """

    kind: Optional[str]
    receiver: Optional[str]
    sender_identity_known: bool = False
    sender_workspace_id: Optional[str] = None
    sender_lane_id: Optional[str] = None
    target_workspace_id: Optional[str] = None
    target_lane_id: Optional[str] = None
    target_role: Optional[str] = None
    allow_direct_worker: bool = False
    worker_provider: Optional[str] = None
    #: The runtime provider bound to the **coordinator (gateway) role** for this repo
    #: (Redmine #13569 R1-F3). The governed route head (coordinator -> sublane gateway) is
    #: the delivery whose receiver IS this provider; a governed delivery to any other
    #: non-worker provider is not a legitimate gateway route and fails closed. The caller
    #: resolves it from the repo-local :class:`RoleProviderBinding`; ``None`` (or empty)
    #: falls back to :data:`PROVIDER_CODEX`, byte-identical to the pre-#13569 default.
    gateway_provider: Optional[str] = None


@dataclass(frozen=True)
class GatewayRouteDecision:
    """The structured result of :func:`decide_gateway_route` (durable-record safe).

    ``verdict`` is one of :data:`ROUTE_ALLOWED` / :data:`ROUTE_BLOCKED` /
    :data:`ROUTE_EXCEPTION`. ``governed`` records whether the kind was in
    :data:`GATEWAY_GOVERNED_KINDS` at all (a non-governed kind is always allowed and
    ungoverned). ``resolved_receiver`` echoes the receiver the delivery resolves to;
    ``blocked_reason`` and ``suggested_safe_route`` are set only when the route did
    not pass cleanly. ``same_unit`` is the lane-Unit comparison result (``None`` when
    it was not computed — non-governed, or a Codex-gateway delivery). Carries only
    fixed tokens and the (public-safe) lane id, never a pane id.
    """

    verdict: str
    governed: bool
    kind: Optional[str]
    resolved_receiver: Optional[str]
    blocked_reason: Optional[str]
    suggested_safe_route: Optional[str]
    same_unit: Optional[bool]
    exception_applied: bool

    @property
    def is_blocked(self) -> bool:
        return self.verdict == ROUTE_BLOCKED

    @property
    def is_exception(self) -> bool:
        return self.verdict == ROUTE_EXCEPTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "governed": self.governed,
            "kind": self.kind,
            "resolved_receiver": self.resolved_receiver,
            "blocked_reason": self.blocked_reason,
            "suggested_safe_route": self.suggested_safe_route,
            "same_unit": self.same_unit,
            "exception_applied": self.exception_applied,
        }


def _same_lane_unit(request: GatewayRouteRequest) -> bool:
    """True when sender and target share one lane Unit ``(workspace_id, lane_id)``.

    Lanes are compared after normalizing a missing lane to :data:`DEFAULT_LANE`, so
    two non-cockpit ``default`` panes read as the same lane (a plain gateway ->
    worker dispatch is not a bypass). The workspace id must match when both are
    known; an unknown workspace on either side does not by itself break the match
    (the lane id already pins the Unit, and a workspace-id mismatch is independently
    caught by the cross-session / ``--target-repo`` gates).
    """
    if _norm_lane(request.sender_lane_id) != _norm_lane(request.target_lane_id):
        return False
    sender_ws = _norm(request.sender_workspace_id)
    target_ws = _norm(request.target_workspace_id)
    if sender_ws and target_ws and sender_ws != target_ws:
        return False
    return True


def _suggested_safe_route(request: GatewayRouteRequest) -> str:
    """Public-safe pointer at the governed route for a blocked delivery.

    Names the target lane (a stable, pasteable identity) and the Codex-gateway hop
    the delivery must take instead of the direct worker send. Contains no pane id.
    """
    target_lane = _norm(request.target_lane_id) or "<target_lane>"
    kind = _norm(request.kind) or "<kind>"
    return (
        f"route the {kind} through lane {target_lane!r}'s Codex gateway: send "
        f"`--to codex` to that lane's gateway pane (e.g. `--target <session>:codex "
        f"--target-repo <lane_workspace_root>`), and let the gateway perform the "
        f"same-lane Claude worker handoff. The direct coordinator-to-worker send "
        f"skips the gateway that owns lane coordination, review/callback routing, "
        f"and the coordinator callback."
    )


def decide_gateway_route(request: GatewayRouteRequest) -> GatewayRouteDecision:
    """Decide whether a handoff delivery satisfies the governed gateway route.

    Pure and total. See the module docstring for the policy; in short:

    1. a non-governed kind -> :data:`ROUTE_ALLOWED`, ``governed=False`` (read-only /
       design / summary / reply / implementation_done are never gated);
    2. a governed kind to the Codex gateway (``receiver == codex``) ->
       :data:`ROUTE_ALLOWED` (this *is* ``coordinator -> sublane Codex gateway``);
    3. a governed kind to a Claude worker when the sender lane Unit is unknown ->
       :data:`ROUTE_ALLOWED` (cannot prove a cross-lane bypass; mirrors the
       cross-session gate being skipped when the sender session is unknown);
    4. a governed kind to a Claude worker that shares the sender's lane Unit ->
       :data:`ROUTE_ALLOWED` (the legitimate ``gateway -> same-lane worker``);
    5. a governed kind to a *cross-lane* Claude worker -> :data:`ROUTE_BLOCKED`,
       unless ``allow_direct_worker`` releases it as a :data:`ROUTE_EXCEPTION`.
    """
    kind = _norm(request.kind)
    receiver = _norm(request.receiver)
    # Role-based worker discrimination (Redmine #13174): the "worker" hop is the
    # implementer role's runtime provider, resolved by the caller from the binding.
    # Default (unset) -> claude, so the pre-#13174 behavior is byte-identical.
    worker_provider = _norm(request.worker_provider) or PROVIDER_CLAUDE
    # The gateway route head is the coordinator (gateway) role's provider (Redmine #13569
    # R1-F3). Default (unset) -> codex, byte-identical. Since 2A opened the receiver
    # vocabulary to every registered provider, "not the worker" no longer implies "the
    # gateway", so the governed head is checked against the EXACT gateway provider.
    gateway_provider = _norm(request.gateway_provider) or PROVIDER_CODEX

    def _allowed(*, governed: bool, same_unit: Optional[bool]) -> GatewayRouteDecision:
        return GatewayRouteDecision(
            verdict=ROUTE_ALLOWED,
            governed=governed,
            kind=request.kind,
            resolved_receiver=request.receiver,
            blocked_reason=None,
            suggested_safe_route=None,
            same_unit=same_unit,
            exception_applied=False,
        )

    if kind not in GATEWAY_GOVERNED_KINDS:
        return _allowed(governed=False, same_unit=None)

    # Governed kind. The route head is the delivery to the EXACT gateway provider
    # (coordinator -> sublane gateway), always allowed.
    if receiver == gateway_provider:
        return _allowed(governed=True, same_unit=None)

    # Not the gateway. The only other legitimate governed receiver is the worker
    # provider (the terminal gateway -> worker hop). A governed delivery to a provider
    # that is NEITHER the gateway nor the worker is not a legitimate route and fails
    # closed (Redmine #13569 R1-F3) — a third provider must never be mistaken for the
    # always-allowed gateway head now that the receiver vocabulary is open.
    if receiver != worker_provider:
        return GatewayRouteDecision(
            verdict=ROUTE_BLOCKED,
            governed=True,
            kind=request.kind,
            resolved_receiver=request.receiver,
            blocked_reason=BLOCKED_NON_GATEWAY_RECEIVER,
            suggested_safe_route=_suggested_safe_route(request),
            same_unit=None,
            exception_applied=False,
        )

    # Governed kind addressed to a worker. When the sender's own lane Unit
    # could not be resolved the gate cannot prove a cross-lane delivery and stays
    # out of the way (same posture as the cross-session gate skipping outside tmux).
    if not request.sender_identity_known:
        return _allowed(governed=True, same_unit=None)

    # Sender Unit known: a same-lane terminal hop is the legitimate
    # gateway -> worker delivery; a cross-lane worker is the recorded bypass and
    # fails closed unless an explicit durable exception releases it.
    if _same_lane_unit(request):
        return _allowed(governed=True, same_unit=True)

    if request.allow_direct_worker:
        return GatewayRouteDecision(
            verdict=ROUTE_EXCEPTION,
            governed=True,
            kind=request.kind,
            resolved_receiver=request.receiver,
            blocked_reason=None,
            suggested_safe_route=_suggested_safe_route(request),
            same_unit=False,
            exception_applied=True,
        )
    return GatewayRouteDecision(
        verdict=ROUTE_BLOCKED,
        governed=True,
        kind=request.kind,
        resolved_receiver=request.receiver,
        blocked_reason=BLOCKED_DIRECT_WORKER_BYPASS,
        suggested_safe_route=_suggested_safe_route(request),
        same_unit=False,
        exception_applied=False,
    )


def render_block_die_message(decision: GatewayRouteDecision, lane_id: object) -> str:
    """The fail-closed CLI ``die`` message for a blocked governed delivery (pure).

    Kept here (not inline in ``orchestrate_handoff``) so the command surface holds
    only the thin wiring and the gateway-route prose lives with its policy. Names
    the governed route, the (public-safe) target lane, the suggested safe route,
    and the explicit durable exception.
    """
    lane = _norm(lane_id) or "<unknown>"
    return (
        f"gateway route enforcement (Redmine #12918): a {_norm(decision.kind)!r} "
        f"addressed directly to the Claude worker in lane {lane!r} bypasses that "
        "lane's Codex gateway. The governed route is coordinator -> sublane Codex "
        "gateway -> same-lane Claude worker. "
        f"{decision.suggested_safe_route} If a bypass is genuinely required, re-run "
        "with the explicit durable exception `--allow-direct-worker` (recorded "
        "distinctly as a gateway_route_exception)."
    )


def render_exception_advisory(decision: GatewayRouteDecision, lane_id: object) -> str:
    """The stderr advisory recording an admitted explicit-exception delivery (pure).

    Emitted when ``--allow-direct-worker`` releases the block so the cross-lane
    worker delivery is recorded distinctly from the normal governed route.
    """
    lane = _norm(lane_id) or "<unknown>"
    return (
        "gateway route enforcement (Redmine #12918): explicit durable exception "
        f"applied — {_norm(decision.kind)!r} delivered directly to the cross-lane "
        f"Claude worker in lane {lane!r} via `--allow-direct-worker`, bypassing the "
        "lane's Codex gateway. Record this exception distinctly from the normal "
        "governed route."
    )


__all__ = (
    "GATEWAY_GOVERNED_KINDS",
    "ROUTE_ALLOWED",
    "ROUTE_BLOCKED",
    "ROUTE_EXCEPTION",
    "BLOCKED_DIRECT_WORKER_BYPASS",
    "BLOCKED_NON_GATEWAY_RECEIVER",
    "GatewayRouteRequest",
    "GatewayRouteDecision",
    "decide_gateway_route",
    "render_block_die_message",
    "render_exception_advisory",
)
