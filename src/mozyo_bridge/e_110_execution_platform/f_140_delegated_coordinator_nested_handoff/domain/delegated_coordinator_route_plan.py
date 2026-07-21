"""Delegated-coordinator route planner (Redmine #12491 / #12474 classical tests).

The #12474 minimal-context smoke proved that the part-unit delegation primitives
are individually sound — the grandchild dispatch decision (#12458), the
launch/adopt selector (#12457), the realization stamp + realize-or-blocked gate
(#12473), the projection metadata (#12465), and the role-profile templates
(#12388) all have thick unit coverage. What was missing, and what the smoke kept
re-discovering as the "#12460 failure shape", is a **planner / orchestrator
contract**: proof that the delegated coordinator's runtime assembles those
primitives in the correct order —

    read-boundary gate -> dispatch decision -> launch/adopt -> stamp ->
    realization gate -> send to grandchild gateway, **or** blocked

— so a same-lane worker handoff can never be silently treated as display
acceptance when policy required a grandchild lane, and a contaminated / insufficient
Redmine read can never feed the route decision (#12474 j#64147 / j#64152 / j#64160
/ j#64172 / j#64185, and the classical-test plan at #12474 j#64217).

This module is that planner. It is **pure** — it composes the sibling pure
domains and adds no I/O — and it is the subject-under-test the #12491 scenario /
regression tests drive through a fake executor. The plan it returns is an ordered,
replayable step list plus a ``proceed`` / ``blocked`` verdict and the resolved
three-hop role-profile chain; the side-effecting executor (the CLI / a fake) reads
the plan and performs (or refuses) the stamps and sends.

Composition order and the invariants it preserves:

1. **Read-boundary gate first** (:mod:`redmine_read_boundary`). A non-``allowed``
   read (contaminated or insufficient) yields a ``blocked`` plan whose
   ``dispatch_decision`` is ``None`` — the route decision is never even computed.
2. **Role-profile chain** (:mod:`role_profile`). The fixed three-hop chain
   ``delegated_coordinator`` -> ``implementation_gateway`` ->
   ``implementation_worker`` is resolved and bound to the hops; an incomplete
   chain is a caller error (``role profile omitted -> plan invalid``).
3. **Dispatch decision** (:mod:`grandchild_dispatch`). Either the grandchild
   dispatch decision or an explicit no-dispatch; a policy / selection
   ``fail_closed`` yields a ``blocked`` plan with the inherited reason.
4. **Realization gate** (:mod:`grandchild_stamp`). When a grandchild is required,
   a realized depth-2 lane must be found, else the plan is ``blocked`` with
   ``grandchild_required_but_not_realized`` — the same-lane worker fallback is
   never acceptance. When no grandchild is required, the same-lane worker is the
   legitimate terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_dispatch import (
    DEFAULT_DELEGATED_COORDINATOR_DEPTH,
    DelegationPolicy,
    GrandchildDispatchDecision,
    resolve_grandchild_dispatch,
    resolve_no_dispatch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (
    GrandchildTargetIdentity,
    InventoryUnit,
    RealizationGateResult,
    evaluate_grandchild_realization_gate,
    resolve_realized_grandchild_binding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt import (
    DelegationCandidate,
    repo_identity_matches,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_read_boundary import ReadBoundaryVerdict
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
    RoleProfileError,
    RoleProfileResolution,
    resolve_role_profile,
)

# --- plan step vocabulary (Redmine #12474 j#64217 ordered plan steps) ----------

#: The read-boundary gate; always the first step (it can block before the route).
STEP_READ_BOUNDARY = "read_boundary"
#: The grandchild dispatch (or no-dispatch) decision.
STEP_DISPATCH_DECISION = "dispatch_decision"
#: A grandchild lane must be launched or adopted before the worker handoff.
STEP_LAUNCH_OR_ADOPT = "launch_or_adopt_required"
#: The realized grandchild lane's live ``KIND``/``DEPTH``/``PARENT`` must be stamped.
STEP_STAMP = "stamp_required"
#: The realize-or-blocked gate over the dispatch requirement + realization evidence.
STEP_REALIZATION_GATE = "realization_gate"
#: Terminal: hand the work to the realized grandchild's Codex gateway.
STEP_SEND_TO_GRANDCHILD_GATEWAY = "send_to_grandchild_gateway"
#: Terminal: hand the work to the delegated coordinator's own same-lane worker
#: (legitimate only when no grandchild was required).
STEP_SEND_SAME_LANE_WORKER = "send_same_lane_worker"
#: Terminal: the route is blocked; record blocked replayably, perform no send.
STEP_BLOCKED = "blocked"

# --- plan verdict -------------------------------------------------------------

#: The plan may proceed to its terminal send step.
PLAN_PROCEED = "proceed"
#: The plan is blocked; ``blocked_reason`` names why and no send may happen.
PLAN_BLOCKED = "blocked"

# --- the fixed three-hop role-profile chain (Redmine #12388 / #12474 j#64217) --

#: Hop names, in route order, paired with the role profile each hop carries.
ROUTE_HOPS: tuple[tuple[str, str], ...] = (
    ("parent_to_child", ROLE_DELEGATED_COORDINATOR),
    ("child_to_gateway", ROLE_IMPLEMENTATION_GATEWAY),
    ("gateway_to_worker", ROLE_IMPLEMENTATION_WORKER),
)
#: The canonical role-profile chain tokens, in route order. A plan that omits any
#: hop's role profile is invalid (Redmine #12474 j#64217: "role profile omitted ->
#: plan invalid").
DEFAULT_ROLE_PROFILE_CHAIN: tuple[str, ...] = tuple(role for _hop, role in ROUTE_HOPS)


class RoutePlanError(ValueError):
    """A route plan request is structurally invalid (caller error, fail closed).

    Raised for *caller* mistakes — a missing durable anchor, an incomplete /
    reordered role-profile chain, or an unknown role profile. A *policy* / route
    fail-closed (disabled, ambiguous candidate, grandchild-required-but-not-realized,
    contaminated read) is **not** an error: it is a first-class
    :class:`DelegatedCoordinatorRoutePlan` with ``verdict == PLAN_BLOCKED`` so the
    caller records it durably rather than crashing.
    """


@dataclass(frozen=True)
class RoutePlanRequest:
    """The durable inputs a delegated coordinator plans a route from.

    Every field is a fact read from the durable record (never pane proximity):
    the ``durable_anchor`` pointer, the ``read_boundary`` verdict for the
    inference read, the delegation ``policy`` and launch/adopt ``mode``, the
    discovery ``candidates`` for the grandchild Codex gateway, the canonical
    ``target_repo_identity`` gate, the ``delegated_coordinator_unit`` the
    grandchild must descend from, and the ``realized_units`` — the typed
    :class:`InventoryUnit` rows re-resolved from the live inventory (a bare
    positional tuple is coerced but, lacking a resolved codex gateway, can never
    realize) the realization gate reads. ``grandchild_target``, when
    set, is the exact dispatch-selected/created/adopted grandchild identity the
    realization gate binds to; when unset, an adopt dispatch's selected candidate
    supplies it (Redmine #13571 / #12454 j#75444 F1). ``no_dispatch_reason``,
    when set, plans an explicit keep-in-lane (no-dispatch) route instead of a
    grandchild dispatch. ``role_profile_fields`` supplies per-role
    ``<placeholder>`` values for the chain; ``role_profile_chain`` may be
    overridden but must remain the complete canonical chain.
    """

    durable_anchor: str
    read_boundary: ReadBoundaryVerdict
    policy: DelegationPolicy
    launch_adopt_mode: str
    candidates: Sequence[DelegationCandidate]
    target_repo_identity: Optional[str]
    delegated_coordinator_unit: str
    realized_units: Sequence[InventoryUnit] = ()
    grandchild_target: Optional[GrandchildTargetIdentity] = None
    current_depth: int = DEFAULT_DELEGATED_COORDINATOR_DEPTH
    active_grandchild_lanes: int = 0
    excluded_lane_ids: tuple[str, ...] = ()
    child_project: Optional[str] = None
    no_dispatch_reason: Optional[str] = None
    role_profile_chain: tuple[str, ...] = DEFAULT_ROLE_PROFILE_CHAIN
    role_profile_fields: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class DelegatedCoordinatorRoutePlan:
    """The ordered, replayable delegated-coordinator route plan.

    ``steps`` is the ordered execution plan; ``terminal_step`` is its last step
    (:data:`STEP_SEND_TO_GRANDCHILD_GATEWAY` / :data:`STEP_SEND_SAME_LANE_WORKER`
    / :data:`STEP_BLOCKED`). ``verdict`` is :data:`PLAN_PROCEED` or
    :data:`PLAN_BLOCKED`; ``blocked_reason`` is set only when blocked.
    ``role_profile_resolutions`` is the resolved three-hop chain bound to
    :data:`ROUTE_HOPS`. ``dispatch_decision`` is ``None`` only when the
    read-boundary gate blocked before the route decision; ``realization_gate`` is
    set whenever a dispatch decision was reached.
    """

    verdict: str
    steps: tuple[str, ...]
    terminal_step: str
    blocked_reason: Optional[str]
    grandchild_required: Optional[bool]
    read_boundary: ReadBoundaryVerdict
    role_profile_chain: tuple[str, ...]
    role_profile_resolutions: tuple[RoleProfileResolution, ...]
    dispatch_decision: Optional[GrandchildDispatchDecision] = None
    realization_gate: Optional[RealizationGateResult] = None

    @property
    def is_blocked(self) -> bool:
        return self.verdict == PLAN_BLOCKED

    @property
    def is_proceed(self) -> bool:
        return self.verdict == PLAN_PROCEED

    @property
    def sends_to_grandchild_gateway(self) -> bool:
        return self.terminal_step == STEP_SEND_TO_GRANDCHILD_GATEWAY

    @property
    def sends_to_same_lane_worker(self) -> bool:
        return self.terminal_step == STEP_SEND_SAME_LANE_WORKER

    def role_profile_for_hop(self, hop: str) -> RoleProfileResolution:
        """Return the resolved role profile bound to a :data:`ROUTE_HOPS` hop.

        Raises :class:`RoutePlanError` for an unknown hop name so a typo never
        silently returns the wrong hop's profile.
        """
        for (hop_name, _role), resolution in zip(ROUTE_HOPS, self.role_profile_resolutions):
            if hop_name == hop:
                return resolution
        raise RoutePlanError(
            f"unknown route hop {hop!r}; expected one of "
            f"{[name for name, _ in ROUTE_HOPS]}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "steps": list(self.steps),
            "terminal_step": self.terminal_step,
            "blocked_reason": self.blocked_reason,
            "grandchild_required": self.grandchild_required,
            "read_boundary": self.read_boundary.to_dict(),
            "role_profile_chain": list(self.role_profile_chain),
            "role_profiles": [r.to_structured_dict() for r in self.role_profile_resolutions],
            "dispatch_decision": (
                self.dispatch_decision.to_dict() if self.dispatch_decision else None
            ),
            "realization_gate": (
                {
                    "verdict": self.realization_gate.verdict,
                    "reason": self.realization_gate.reason,
                    "grandchild_required": self.realization_gate.grandchild_required,
                    "realized_grandchild_unit": self.realization_gate.realized_grandchild_unit,
                }
                if self.realization_gate
                else None
            ),
        }


def _resolve_role_profile_chain(request: RoutePlanRequest) -> tuple[RoleProfileResolution, ...]:
    """Resolve the fixed three-hop role-profile chain, failing closed on omission.

    The chain must be exactly the canonical :data:`DEFAULT_ROLE_PROFILE_CHAIN`, in
    order — an omitted / reordered / unknown role is a caller error ("role profile
    omitted -> plan invalid", Redmine #12474 j#64217). Each role's
    ``<durable_anchor>`` placeholder is auto-filled from the request anchor so the
    resolved contract a receiver reads always points at the durable anchor; any
    additional per-role fields come from ``role_profile_fields``.
    """
    if tuple(request.role_profile_chain) != DEFAULT_ROLE_PROFILE_CHAIN:
        raise RoutePlanError(
            "the delegated-coordinator route requires the complete role-profile "
            f"chain {list(DEFAULT_ROLE_PROFILE_CHAIN)} in order; got "
            f"{list(request.role_profile_chain)} (role profile omitted -> plan "
            "invalid)."
        )
    resolutions: list[RoleProfileResolution] = []
    for role in request.role_profile_chain:
        fields = dict(request.role_profile_fields.get(role, {}))
        fields.setdefault("durable_anchor", request.durable_anchor)
        try:
            resolutions.append(resolve_role_profile(role, fields))
        except RoleProfileError as exc:  # pragma: no cover - chain is canonical
            raise RoutePlanError(f"role profile {role!r} could not be resolved: {exc}") from exc
    return tuple(resolutions)


def _identities_agree(
    a: GrandchildTargetIdentity, b: GrandchildTargetIdentity
) -> bool:
    """Whether two grandchild identities name the same exact lane (F1).

    Compares the routing-authoritative facts: unit id, declared parent, and
    canonical repo (normalized). A display-only difference never matters because
    those facts are not compared; a difference in unit / parent / repo means the
    two identities are NOT the same lane.
    """
    return (
        a.unit_id == b.unit_id
        and a.delegation_parent == b.delegation_parent
        and repo_identity_matches(a.repo_identity, b.repo_identity)
    )


def _effective_grandchild_target(
    request: RoutePlanRequest, dispatch: GrandchildDispatchDecision
) -> Optional[GrandchildTargetIdentity]:
    """Resolve the exact grandchild identity the realization gate must bind to.

    Authority order (Redmine #13571 / #12454 j#75444 F1): for an **adopt**
    dispatch the selected Codex gateway candidate is authoritative — the dispatch
    literally selected that lane, so its ``<workspace_id>/<lane_id>`` unit and the
    canonical ``--target-repo`` identity are the exact target. An explicit
    ``request.grandchild_target`` may accompany it but must NAME THE SAME LANE;
    an explicit target that disagrees with the dispatch selection is a conflict
    and yields ``None`` (the gate then fails closed rather than letting an
    unrelated sibling's display evidence open the gate). An explicit target is
    authoritative only for a **launch** dispatch (no selected candidate — the
    runtime supplies the created lane's post-launch identity). A launch with no
    explicit target yields ``None`` -> ``unbound`` -> blocked.
    """
    selected = dispatch.selected
    if selected is not None:
        derived = GrandchildTargetIdentity(
            unit_id=f"{selected.workspace_id or ''}/{selected.lane_id or ''}",
            delegation_parent=request.delegated_coordinator_unit,
            repo_identity=request.target_repo_identity or selected.repo_root,
        )
        if request.grandchild_target is not None and not _identities_agree(
            request.grandchild_target, derived
        ):
            # Explicit target disagrees with the dispatch-selected lane: fail
            # closed instead of overriding the dispatch selection (F1).
            return None
        return derived
    # Launch: no selected candidate; the explicit post-launch identity (if any)
    # is authoritative — but it must name a genuinely NEW lane. A launch target
    # that collides with a pre-launch discovery candidate is an existing lane
    # smuggled in under a launch label (an adopt masquerading as a launch), so
    # fail closed rather than bind to it (Redmine #13571 j#75473 F5).
    explicit = request.grandchild_target
    if explicit is not None:
        for cand in request.candidates:
            cand_unit = f"{cand.workspace_id or ''}/{cand.lane_id or ''}"
            if cand_unit == explicit.unit_id:
                return None
    return explicit


def plan_delegated_coordinator_route(
    request: RoutePlanRequest,
) -> DelegatedCoordinatorRoutePlan:
    """Plan the ordered delegated-coordinator route, fail-closed at each gate.

    See the module docstring for the composition order. Returns a
    :class:`DelegatedCoordinatorRoutePlan` for every reachable outcome (proceed or
    blocked); raises :class:`RoutePlanError` only for caller mistakes (missing
    anchor, incomplete role-profile chain). Pure and deterministic over its inputs.
    """
    if not (request.durable_anchor or "").strip():
        raise RoutePlanError(
            "a route plan requires a non-empty durable_anchor (read it from the "
            "source-of-truth system before planning a route)."
        )

    chain = _resolve_role_profile_chain(request)
    steps: list[str] = [STEP_READ_BOUNDARY]

    def _blocked(
        *,
        reason: str,
        grandchild_required: Optional[bool],
        dispatch: Optional[GrandchildDispatchDecision],
        gate: Optional[RealizationGateResult],
    ) -> DelegatedCoordinatorRoutePlan:
        steps.append(STEP_BLOCKED)
        return DelegatedCoordinatorRoutePlan(
            verdict=PLAN_BLOCKED,
            steps=tuple(steps),
            terminal_step=STEP_BLOCKED,
            blocked_reason=reason,
            grandchild_required=grandchild_required,
            read_boundary=request.read_boundary,
            role_profile_chain=tuple(request.role_profile_chain),
            role_profile_resolutions=chain,
            dispatch_decision=dispatch,
            realization_gate=gate,
        )

    # 1. Read-boundary gate: a non-allowed read stops the route BEFORE any
    #    dispatch decision is computed (dispatch_decision stays None).
    if not request.read_boundary.is_allowed:
        return _blocked(
            reason=f"read_boundary_{request.read_boundary.classification}",
            grandchild_required=None,
            dispatch=None,
            gate=None,
        )

    # 2. Dispatch decision (grandchild dispatch, or an explicit no-dispatch).
    if (request.no_dispatch_reason or "").strip():
        dispatch = resolve_no_dispatch(
            policy=request.policy,
            no_dispatch_reason=request.no_dispatch_reason,  # type: ignore[arg-type]
            current_depth=request.current_depth,
            child_project=request.child_project,
        )
    else:
        dispatch = resolve_grandchild_dispatch(
            policy=request.policy,
            mode=request.launch_adopt_mode,
            candidates=request.candidates,
            target_repo_identity=request.target_repo_identity,
            current_depth=request.current_depth,
            active_grandchild_lanes=request.active_grandchild_lanes,
            excluded_lane_ids=request.excluded_lane_ids,
            child_project=request.child_project,
        )
    steps.append(STEP_DISPATCH_DECISION)

    if dispatch.is_fail_closed:
        return _blocked(
            reason=dispatch.reason or "dispatch_fail_closed",
            grandchild_required=False,
            dispatch=dispatch,
            gate=None,
        )

    grandchild_required = dispatch.is_dispatch

    # 3a. No grandchild required: the same-lane worker is the legitimate terminal.
    if not grandchild_required:
        gate = evaluate_grandchild_realization_gate(
            grandchild_required=False, realized_grandchild_unit=None
        )
        steps.append(STEP_REALIZATION_GATE)
        steps.append(STEP_SEND_SAME_LANE_WORKER)
        return DelegatedCoordinatorRoutePlan(
            verdict=PLAN_PROCEED,
            steps=tuple(steps),
            terminal_step=STEP_SEND_SAME_LANE_WORKER,
            blocked_reason=None,
            grandchild_required=False,
            read_boundary=request.read_boundary,
            role_profile_chain=tuple(request.role_profile_chain),
            role_profile_resolutions=chain,
            dispatch_decision=dispatch,
            realization_gate=gate,
        )

    # 3b. Grandchild required: launch/adopt + stamp must precede the worker
    #     handoff, and the realize-or-blocked gate decides proceed vs blocked.
    steps.append(STEP_LAUNCH_OR_ADOPT)
    steps.append(STEP_STAMP)
    # Bind the realization gate to the EXACT dispatch-selected grandchild identity
    # (never the first depth-2 sibling): a stale/unrelated sibling must not be
    # treated as realized (Redmine #13571 / #12454 j#75444 F1). The binding
    # re-verifies role/depth/parent/repo against the live inventory and fails
    # closed (missing/mismatch/ambiguous/unbound) to a blocked gate.
    target = _effective_grandchild_target(request, dispatch)
    binding = resolve_realized_grandchild_binding(
        request.realized_units,
        target=target,
        delegated_coordinator_unit=request.delegated_coordinator_unit,
    )
    gate = evaluate_grandchild_realization_gate(
        grandchild_required=True, realized_grandchild_unit=binding.matched_unit
    )
    steps.append(STEP_REALIZATION_GATE)

    if gate.is_blocked:
        # The same-lane worker fallback: a grandchild was required but none is
        # realized/stamped. Blocked, never acceptance (Redmine #12460 / #12474).
        return _blocked(
            reason=gate.reason,
            grandchild_required=True,
            dispatch=dispatch,
            gate=gate,
        )

    steps.append(STEP_SEND_TO_GRANDCHILD_GATEWAY)
    return DelegatedCoordinatorRoutePlan(
        verdict=PLAN_PROCEED,
        steps=tuple(steps),
        terminal_step=STEP_SEND_TO_GRANDCHILD_GATEWAY,
        blocked_reason=None,
        grandchild_required=True,
        read_boundary=request.read_boundary,
        role_profile_chain=tuple(request.role_profile_chain),
        role_profile_resolutions=chain,
        dispatch_decision=dispatch,
        realization_gate=gate,
    )


__all__ = (
    "STEP_READ_BOUNDARY",
    "STEP_DISPATCH_DECISION",
    "STEP_LAUNCH_OR_ADOPT",
    "STEP_STAMP",
    "STEP_REALIZATION_GATE",
    "STEP_SEND_TO_GRANDCHILD_GATEWAY",
    "STEP_SEND_SAME_LANE_WORKER",
    "STEP_BLOCKED",
    "PLAN_PROCEED",
    "PLAN_BLOCKED",
    "ROUTE_HOPS",
    "DEFAULT_ROLE_PROFILE_CHAIN",
    "RoutePlanError",
    "RoutePlanRequest",
    "DelegatedCoordinatorRoutePlan",
    "plan_delegated_coordinator_route",
)
