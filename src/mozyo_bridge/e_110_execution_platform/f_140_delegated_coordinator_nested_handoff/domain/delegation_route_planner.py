"""Delegated coordinator planner / actuator-seam integration (Redmine #12550).

#12548 (j#64716) found the external-parent delegation path stops at *read-only
recommendation* — the resolved child candidate (#12549) and the role-profile
vocabulary (#12388) both exist, but nothing turns a
:class:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config.ChildCandidateResolution`
into a concrete, fail-closed, executable route toward a delegated coordinator and
(when the work shape needs it) a grandchild implementation lane. This module is
that missing *plan* layer.

It is the pure-plan half of the split mandated by
``vibes/docs/logics/delegated-coordinator-smoke-test-frame.md`` (``## 推奨実装方針``):

- **plan** (this module): from the #12549 resolver result, the requested work
  shape, and a read-only ``agents targets`` candidate listing, produce the
  ordered command/record sequence and the role-profile chain a full
  ``parent -> delegated coordinator -> grandchild gateway -> worker`` route needs.
- **executor** (a deliberately deferred follow-up): worktree / window / lane
  creation, stamp, and ``handoff send`` against live tmux / Redmine / cockpit.

Per #12550 Required behavior #7 this seam ships **pure** — it never opens tmux,
never sends a handoff, never writes Redmine, never mutates cockpit membership.
Side-effecting steps are emitted as an explicit, typed command plan
(:class:`PlannedStep`) carrying the boundary between what a future actuator may
auto-run and what must stay operator-confirmed, rather than being executed here.

Disposition vocabulary is kept *oracle-compatible* with the #12547 acceptance
oracle (``tests/test_delegated_coordinator_acceptance_oracle.py``): a plan whose
realized route would classify as ``failed_acceptance`` / ``insufficient`` /
``blocked`` under that oracle is never emitted as an executable PASS-eligible
plan. The shared reason strings (``child_candidate_missing`` /
``child_candidate_ambiguous`` / ``cross_project_claude_direct_send`` /
``grandchild_required_but_not_realized`` / ``grandchild_window_not_launched`` /
``read_only_recommendation_only``) are reused verbatim so the planner and the
oracle cannot drift; a cross-check test pins the mapping.

Enforced invariants:

- **Read-only discovery.** ``agents targets`` / candidate listing is consumed as
  read-only input through :class:`RealizationCandidateView` (match-classification
  tokens only, never a pane id / host path). Candidate *listing alone never*
  produces a PASS-eligible plan — only a constructed adopt/launch realization
  plus a resolved candidate and a Codex-gateway route does (Required behavior #5).
- **Codex gateway routing preserved.** A cross-project / cross-lane Claude direct
  send is never planned: such a route fails closed at classification time and the
  step builder additionally raises :class:`DelegationRoutePlanError` as
  defense-in-depth (Required behavior #4).
- **Fail-closed realization.** A required child window or grandchild lane that is
  not actually adoptable/launchable, an ambiguous candidate set, or a
  grandchild-required route that would silently fall back to the same-lane worker
  all fail closed rather than producing an executable plan (Required behavior #6).
- **Route-identity targets, not stale pane ids.** Planned steps reference logical
  route-identity tokens (``child_gateway`` / ``grandchild_gateway`` /
  ``same_lane_worker``) that a future actuator re-resolves to live panes, never a
  baked pane id. A durable route-identity ledger is intentionally *not* built
  here (it is recorded as a follow-up); this seam only needs stable logical
  targets.

The module is pure (dataclasses + small helpers) and imports only from the
sibling pure-domain modules :mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config`
and :mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile`, so it composes without a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config import (
    CHILD_CANDIDATE_AMBIGUOUS,
    CHILD_CANDIDATE_MISSING,
    STATUS_AMBIGUOUS,
    STATUS_MISSING,
    STATUS_RESOLVED,
    ChildCandidateResolution,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
    RoleProfileResolution,
    resolve_role_profile,
)

# ---------------------------------------------------------------------------
# Plan-readiness disposition (mirrors the #12547 oracle's verdict buckets).
#
# These are the only plan outcomes; each maps to exactly one oracle
# classification so a route the oracle would reject is never an executable plan:
#   PLAN_EXECUTABLE / PLAN_OPERATOR_CONFIRM -> oracle PASS (PASS-eligible)
#   PLAN_INSUFFICIENT                       -> oracle insufficient
#   PLAN_BLOCKED                            -> oracle blocked
#   PLAN_FAILED                             -> oracle failed_acceptance
# ---------------------------------------------------------------------------
PLAN_EXECUTABLE: str = "executable_handoff_plan"
PLAN_OPERATOR_CONFIRM: str = "operator_confirmed_command_plan"
PLAN_INSUFFICIENT: str = "read_only_recommendation"
PLAN_BLOCKED: str = "blocked"
PLAN_FAILED: str = "failed_acceptance"

#: PASS-eligible dispositions: a route a (future) actuator may carry forward.
#: ``insufficient`` / ``blocked`` / ``failed`` are deliberately excluded so
#: "the planner produced output" is never confused with "the route may proceed".
PASS_ELIGIBLE_DISPOSITIONS: frozenset[str] = frozenset(
    {PLAN_EXECUTABLE, PLAN_OPERATOR_CONFIRM}
)

# ---------------------------------------------------------------------------
# Per-step execution boundary (Required behavior #7). This seam emits the plan
# only; it executes nothing. The mode records, for a future executor, which
# steps are auto-runnable vs. which must stay operator-confirmed.
# ---------------------------------------------------------------------------
#: A durable-record step (e.g. record the parent delegation decision / callback
#: outcome in Redmine). Recorded by the coordinator, not auto-run by the planner.
EXEC_DURABLE_RECORD: str = "durable_record"
#: An adopt / handoff step against an already-live, discovered pane. Lower risk;
#: a future actuator may auto-run it once executor support lands.
EXEC_AUTO: str = "auto"
#: A step that creates new topology (launch a window/lane) or otherwise mutates
#: shared state in a way that is not safe to auto-run: emitted as an explicit
#: command for the operator to confirm.
EXEC_OPERATOR_CONFIRMED: str = "operator_confirmed"

# ---------------------------------------------------------------------------
# Lane realization decision derived from read-only discovery. Aligned with the
# smoke-test-frame's grandchild realization vocabulary so the oracle cross-check
# stays mechanical.
# ---------------------------------------------------------------------------
#: Exactly one matching live candidate -> adopt it (no new topology).
REALIZE_ADOPT: str = "adopt"
#: No matching candidate, but a lane may be launched -> create new topology.
REALIZE_LAUNCH: str = "launch"
#: Grandchild required, no grandchild lane realizable, but a same-lane worker is
#: available: the naive route would silently fall back to same-lane. Blocked.
REALIZE_SAME_LANE_FALLBACK: str = "same_lane_fallback"
#: More than one matching candidate -> fail closed (never fabricate one).
REALIZE_AMBIGUOUS: str = "ambiguous"
#: No candidate and no way to launch / fall back -> the window is not launched.
REALIZE_NOT_LAUNCHED: str = "not_launched"
#: Grandchild not part of the requested work shape.
REALIZE_NOT_APPLICABLE: str = "not_applicable"

# ---------------------------------------------------------------------------
# Requested output mode (Required behavior #2: read-only recommendation must be
# expressible and must classify as insufficient, never PASS).
# ---------------------------------------------------------------------------
#: Caller wants a constructed, route-realizing plan (the normal path).
OUTPUT_EXECUTE: str = "execute"
#: Caller explicitly wants a read-only recommendation only -> insufficient.
OUTPUT_RECOMMEND_ONLY: str = "recommend_only"

# ---------------------------------------------------------------------------
# Route target roles. A parent -> child handoff must target a Codex gateway; a
# direct Claude target across project/lane is a hard invariant violation.
# ---------------------------------------------------------------------------
ROUTE_CODEX_GATEWAY: str = "codex_gateway"
ROUTE_CLAUDE_DIRECT: str = "claude_direct"

# ---------------------------------------------------------------------------
# Planned-step kinds, in the order the command planner must emit them
# (acceptance doc ``## Classical Test Obligations``: parent decision -> child
# handoff -> grandchild stamp -> worker handoff -> callback record).
# ---------------------------------------------------------------------------
STEP_PARENT_DECISION: str = "parent_decision"
STEP_CHILD_HANDOFF: str = "child_handoff"
STEP_GRANDCHILD_STAMP: str = "grandchild_stamp"
STEP_WORKER_HANDOFF: str = "worker_handoff"
STEP_CALLBACK_RECORD: str = "callback_record"

#: Logical route-identity targets (re-resolved to live panes by a future
#: actuator). Never a pane id, so the plan carries no stale topology.
TARGET_CHILD_GATEWAY: str = "child_gateway"
TARGET_GRANDCHILD_GATEWAY: str = "grandchild_gateway"
TARGET_SAME_LANE_WORKER: str = "same_lane_worker"
TARGET_PARENT_COORDINATOR: str = "parent_coordinator"

#: Diagnostic emitted for the only fully clean, route-realizing plan.
PLAN_ROUTE_REALIZABLE: str = "route_realizable"


class DelegationRoutePlanError(ValueError):
    """A route-planning input is malformed or violates a build-time invariant.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    delegation/role-profile domain errors. Structured *runtime* outcomes
    (missing / ambiguous candidate, an unrealizable lane) are returned as a
    :class:`RoutePlan` disposition, not raised; only a malformed *input* (wrong
    type, an internally inconsistent resolution) or an attempt to construct a
    forbidden cross-boundary Claude send raises.
    """


@dataclass(frozen=True)
class RealizationCandidateView:
    """Public-safe, read-only view of one ``agents targets`` discovery candidate.

    Carries only the match-classification tokens the planner needs to decide
    adopt / launch / ambiguous — never a pane id, host path, or cockpit
    composition (Required behavior #8). :attr:`candidate_ref` is an optional
    opaque, public-safe handle for audit echo only (e.g. a logical lane label),
    never a private topology value.

    A candidate *matches* a required lane when repo, lane, and role all match;
    folding raw discovery rows into these tokens is the discovery layer's job, so
    this planner only counts matches and never inspects live topology.
    """

    repo_match: bool
    lane_match: bool
    role_match: bool
    candidate_ref: str = ""

    @property
    def matches(self) -> bool:
        """True only when repo, lane, and role all match the required lane."""
        return self.repo_match and self.lane_match and self.role_match


@dataclass(frozen=True)
class RouteRequest:
    """The work shape + route-identity context a route plan is built from.

    Every field is a durable-record-safe token: logical route-identity targets
    and public-safe identifiers, never a pane id / host path / private project
    name. The resolver result is supplied separately to
    :func:`plan_delegation_route`.
    """

    #: Durable anchor pointer (e.g. ``redmine:#12550 j#64780``) threaded into the
    #: role-profile templates so the receiver reads the contract from one place.
    durable_anchor: str
    #: Requested child project id (echoed by the resolver result; cross-checked).
    child_project: str
    #: Whether a grandchild implementation lane is part of the requested work
    #: shape. ``False`` means the delegated coordinator handles the work itself.
    grandchild_required: bool = False
    #: Route target role of the parent -> child handoff. Must be the Codex
    #: gateway; a direct Claude target fails closed.
    route_target_role: str = ROUTE_CODEX_GATEWAY
    #: True when the parent -> child handoff crosses a project boundary (the
    #: normal external-parent case). A cross-project Claude direct send is never
    #: planned regardless of this flag; it is recorded for audit / role fields.
    cross_project: bool = True
    #: Requested output mode. ``recommend_only`` yields an insufficient plan.
    output_mode: str = OUTPUT_EXECUTE
    #: Optional capability the route is for (echoed into the decision record).
    capability: Optional[str] = None
    #: The runtime providers the gateway / worker route heads target (Redmine #13569
    #: Increment 2B). Defaults are the built-in binding (gateway ``codex`` / worker
    #: ``claude``), byte-identical; the caller passes the values resolved from the
    #: repo-local ``RoleProviderBinding`` so a rebound worker/gateway provider follows
    #: without a literal edit, while the "gateway-via, worker-never-cross-boundary-direct"
    #: invariant keys on the resolved worker provider rather than the literal ``claude``.
    gateway_provider: str = "codex"
    worker_provider: str = "claude"
    # --- role-profile template fields (all optional; unresolved -> reported) ---
    parent_project: str = ""
    parent_issue: str = ""
    redmine_project: str = ""
    parent_callback_target: str = ""
    upstream_coordinator: str = ""
    gateway_callback_target: str = ""
    lane: str = ""


@dataclass(frozen=True)
class PlannedStep:
    """One ordered step of a route plan: a command or durable-record to emit.

    The step is *described*, not executed. :attr:`execution_mode` records the
    boundary a future actuator must honor; :attr:`route_target` is a logical
    route-identity token (never a pane id). :attr:`role_profile`, when present, is
    the resolved role contract carried by a handoff step.
    """

    kind: str
    execution_mode: str
    description: str
    route_target: str = ""
    role_profile: Optional[RoleProfileResolution] = None


@dataclass(frozen=True)
class RoutePlan:
    """Typed planner output: a disposition plus the ordered step sequence.

    :attr:`disposition` is one of the ``PLAN_*`` tokens and maps 1:1 to a #12547
    oracle classification. :attr:`diagnostic` reuses the oracle's reason strings
    for the shared failure modes. :attr:`steps` is the parent-decision ->
    child-handoff -> grandchild-stamp -> worker-handoff -> callback ordering (the
    grandchild/worker steps are present only when grandchild work is realized).
    :attr:`role_profile_chain` is the role tokens the route delivers, top-down.
    """

    disposition: str
    diagnostic: str
    requested_child_project: str
    steps: tuple[PlannedStep, ...] = ()
    role_profile_chain: tuple[str, ...] = ()
    child_realization: str = REALIZE_NOT_APPLICABLE
    grandchild_realization: str = REALIZE_NOT_APPLICABLE

    @property
    def is_pass_eligible(self) -> bool:
        """True only for a constructed, route-realizing plan (never for listing).

        Candidate *listing alone* never reaches a PASS-eligible disposition, so
        this property is the single guard a caller checks before treating a plan
        as a route the actuator may carry forward (Required behavior #5).
        """
        return self.disposition in PASS_ELIGIBLE_DISPOSITIONS

    @property
    def requires_operator_confirmation(self) -> bool:
        """True when any planned step is operator-confirmed (a launch / mutation)."""
        return any(
            step.execution_mode == EXEC_OPERATOR_CONFIRMED for step in self.steps
        )


def decide_child_realization(
    candidates: "Sequence[RealizationCandidateView]", *, can_launch: bool
) -> str:
    """Decide how the child delegated-coordinator window is realized.

    Pure over the read-only candidate listing: exactly one match -> adopt; more
    than one -> ambiguous (fail closed); none -> launch when launchable, else
    not_launched. The child window is always part of the route, so there is no
    ``not_applicable`` here.
    """
    match_count = _match_count(candidates)
    if match_count == 1:
        return REALIZE_ADOPT
    if match_count > 1:
        return REALIZE_AMBIGUOUS
    return REALIZE_LAUNCH if can_launch else REALIZE_NOT_LAUNCHED


def decide_grandchild_realization(
    candidates: "Sequence[RealizationCandidateView]",
    *,
    required: bool,
    can_launch: bool,
    same_lane_worker_available: bool,
) -> str:
    """Decide how a (possibly required) grandchild lane is realized.

    Not required -> ``not_applicable``. Required: exactly one match -> adopt; more
    than one -> ambiguous; none + launchable -> launch; none + not launchable but
    a same-lane worker is available -> ``same_lane_fallback`` (the route would
    silently degrade, so it is blocked, never a PASS); otherwise ``not_launched``.
    """
    if not required:
        return REALIZE_NOT_APPLICABLE
    match_count = _match_count(candidates)
    if match_count == 1:
        return REALIZE_ADOPT
    if match_count > 1:
        return REALIZE_AMBIGUOUS
    if can_launch:
        return REALIZE_LAUNCH
    if same_lane_worker_available:
        return REALIZE_SAME_LANE_FALLBACK
    return REALIZE_NOT_LAUNCHED


def _match_count(candidates: "Sequence[RealizationCandidateView]") -> int:
    """Count fully-matching candidates, failing closed on a malformed listing."""
    if isinstance(candidates, (str, bytes, Mapping)) or not isinstance(
        candidates, Sequence
    ):
        raise DelegationRoutePlanError(
            "discovery candidate listing must be a sequence of "
            f"RealizationCandidateView, got {type(candidates).__name__}"
        )
    count = 0
    for candidate in candidates:
        if not isinstance(candidate, RealizationCandidateView):
            raise DelegationRoutePlanError(
                "discovery candidate must be a RealizationCandidateView, got "
                f"{type(candidate).__name__}"
            )
        if candidate.matches:
            count += 1
    return count


def plan_delegation_route(
    resolution: ChildCandidateResolution,
    request: RouteRequest,
    *,
    child_candidates: "Sequence[RealizationCandidateView]" = (),
    grandchild_candidates: "Sequence[RealizationCandidateView]" = (),
    child_can_launch: bool = True,
    grandchild_can_launch: bool = True,
    same_lane_worker_available: bool = False,
) -> RoutePlan:
    """Plan a fail-closed delegation route from a resolved child candidate.

    Consumes the #12549 :class:`ChildCandidateResolution` and a read-only
    ``agents targets`` candidate listing (folded to
    :class:`RealizationCandidateView`), and returns a typed :class:`RoutePlan`
    whose disposition is oracle-compatible with #12547.

    Precedence (first match wins), mirroring the oracle so mixed inputs are
    deterministic and a rejected route is never an executable plan:

    1. **failed_acceptance** — resolver missing / ambiguous candidate, or a
       cross-project/cross-lane Claude direct route target.
    2. **blocked** — grandchild required but the route would fall back to the
       same-lane worker (``grandchild_required_but_not_realized``).
    3. **failed_acceptance** — the child window or a required grandchild lane is
       not adoptable/launchable, or an ambiguous candidate set.
    4. **insufficient** — the caller asked for a read-only recommendation only.
    5. **executable / operator-confirmed** — a clean route. Operator-confirmed
       when any required lane must be *launched* (new topology); executable when
       every required lane is *adopted* from a live candidate.

    Raises :class:`DelegationRoutePlanError` only on a malformed input (wrong
    type, a ``resolved`` status with no candidate, a child-project mismatch) — a
    programming error, distinct from the structured fail-closed dispositions.
    """
    _validate_inputs(resolution, request)

    # 1. Resolver fail-closed outcomes -> failed_acceptance (oracle reasons).
    if resolution.status == STATUS_MISSING:
        return _terminal_plan(PLAN_FAILED, CHILD_CANDIDATE_MISSING, request)
    if resolution.status == STATUS_AMBIGUOUS:
        return _terminal_plan(PLAN_FAILED, CHILD_CANDIDATE_AMBIGUOUS, request)

    # Route-target invariant: a cross-project/cross-lane Claude direct send is a
    # hard invariant violation, never planned.
    if request.route_target_role == ROUTE_CLAUDE_DIRECT:
        return _terminal_plan(
            PLAN_FAILED, "cross_project_claude_direct_send", request
        )

    child_real = decide_child_realization(
        child_candidates, can_launch=child_can_launch
    )
    grandchild_real = decide_grandchild_realization(
        grandchild_candidates,
        required=request.grandchild_required,
        can_launch=grandchild_can_launch,
        same_lane_worker_available=same_lane_worker_available,
    )

    # 2. Grandchild required but same-lane fallback -> blocked (outranks the
    #    failed-acceptance window checks, per oracle precedence).
    if grandchild_real == REALIZE_SAME_LANE_FALLBACK:
        return _terminal_plan(
            PLAN_BLOCKED,
            "grandchild_required_but_not_realized",
            request,
            child_realization=child_real,
            grandchild_realization=grandchild_real,
        )

    # 3. Hard realization failures -> failed_acceptance.
    if child_real in (REALIZE_NOT_LAUNCHED, REALIZE_AMBIGUOUS):
        diagnostic = (
            "child_window_ambiguous"
            if child_real == REALIZE_AMBIGUOUS
            else "child_window_not_launched"
        )
        return _terminal_plan(
            PLAN_FAILED,
            diagnostic,
            request,
            child_realization=child_real,
            grandchild_realization=grandchild_real,
        )
    if grandchild_real in (REALIZE_NOT_LAUNCHED, REALIZE_AMBIGUOUS):
        diagnostic = (
            "grandchild_window_ambiguous"
            if grandchild_real == REALIZE_AMBIGUOUS
            else "grandchild_window_not_launched"
        )
        return _terminal_plan(
            PLAN_FAILED,
            diagnostic,
            request,
            child_realization=child_real,
            grandchild_realization=grandchild_real,
        )

    # 4. Read-only recommendation requested -> insufficient (never PASS).
    if request.output_mode == OUTPUT_RECOMMEND_ONLY:
        return _terminal_plan(
            PLAN_INSUFFICIENT,
            "read_only_recommendation_only",
            request,
            child_realization=child_real,
            grandchild_realization=grandchild_real,
        )

    # 5. Clean route: build the ordered step sequence and the role chain.
    steps, chain = _build_steps(request, child_real, grandchild_real)
    disposition = (
        PLAN_OPERATOR_CONFIRM
        if _requires_launch(child_real, grandchild_real)
        else PLAN_EXECUTABLE
    )
    return RoutePlan(
        disposition=disposition,
        diagnostic=PLAN_ROUTE_REALIZABLE,
        requested_child_project=request.child_project,
        steps=steps,
        role_profile_chain=chain,
        child_realization=child_real,
        grandchild_realization=grandchild_real,
    )


def _validate_inputs(
    resolution: ChildCandidateResolution, request: RouteRequest
) -> None:
    """Fail closed on a malformed resolver result or request (programming error)."""
    if not isinstance(resolution, ChildCandidateResolution):
        raise DelegationRoutePlanError(
            "plan_delegation_route requires a ChildCandidateResolution, got "
            f"{type(resolution).__name__}"
        )
    if not isinstance(request, RouteRequest):
        raise DelegationRoutePlanError(
            "plan_delegation_route requires a RouteRequest, got "
            f"{type(request).__name__}"
        )
    if request.output_mode not in (OUTPUT_EXECUTE, OUTPUT_RECOMMEND_ONLY):
        raise DelegationRoutePlanError(
            f"unknown output_mode {request.output_mode!r}; expected "
            f"{OUTPUT_EXECUTE!r} or {OUTPUT_RECOMMEND_ONLY!r}"
        )
    if request.route_target_role not in (ROUTE_CODEX_GATEWAY, ROUTE_CLAUDE_DIRECT):
        raise DelegationRoutePlanError(
            f"unknown route_target_role {request.route_target_role!r}; expected "
            f"{ROUTE_CODEX_GATEWAY!r} or {ROUTE_CLAUDE_DIRECT!r}"
        )
    # ``ChildCandidateResolution`` is a plain dataclass and does not itself
    # enforce its status vocabulary, so an unknown / internally inconsistent
    # status must fail closed here before any route is built — otherwise it would
    # fall through the missing/ambiguous branches into realization and could
    # surface as a PASS-eligible plan (Required behavior #1: malformed resolver
    # results stay fail-closed and oracle-compatible).
    if resolution.status not in (STATUS_RESOLVED, STATUS_MISSING, STATUS_AMBIGUOUS):
        raise DelegationRoutePlanError(
            f"unknown ChildCandidateResolution status {resolution.status!r}; "
            f"expected {STATUS_RESOLVED!r}, {STATUS_MISSING!r}, or "
            f"{STATUS_AMBIGUOUS!r}"
        )
    # A ``resolved`` status must carry exactly its single candidate, and the
    # candidate / request must agree on the child project. An inconsistent
    # resolution is a malformed input, not a fail-closed runtime outcome.
    if resolution.status == STATUS_RESOLVED:
        if resolution.candidate is None:
            raise DelegationRoutePlanError(
                "resolved ChildCandidateResolution must carry a candidate"
            )
        if resolution.candidate.child_project != request.child_project:
            raise DelegationRoutePlanError(
                "route request child_project "
                f"{request.child_project!r} does not match resolved candidate "
                f"{resolution.candidate.child_project!r}"
            )


def _terminal_plan(
    disposition: str,
    diagnostic: str,
    request: RouteRequest,
    *,
    child_realization: str = REALIZE_NOT_APPLICABLE,
    grandchild_realization: str = REALIZE_NOT_APPLICABLE,
) -> RoutePlan:
    """A no-steps plan for every non-realizing disposition (fail-closed / insufficient)."""
    return RoutePlan(
        disposition=disposition,
        diagnostic=diagnostic,
        requested_child_project=request.child_project,
        steps=(),
        role_profile_chain=(),
        child_realization=child_realization,
        grandchild_realization=grandchild_realization,
    )


def _requires_launch(child_real: str, grandchild_real: str) -> bool:
    """True when any realized lane must be launched (new topology)."""
    return REALIZE_LAUNCH in (child_real, grandchild_real)


def _build_steps(
    request: RouteRequest, child_real: str, grandchild_real: str
) -> "tuple[tuple[PlannedStep, ...], tuple[str, ...]]":
    """Build the ordered step sequence and the role-profile chain for a clean route.

    Order is fixed by the acceptance doc's classical-test obligation: parent
    decision -> child handoff -> (grandchild stamp -> worker handoff) -> callback.
    """
    steps: list[PlannedStep] = []
    chain: list[str] = []

    # 1. Parent delegation decision -> durable record (grounds the route).
    steps.append(
        PlannedStep(
            kind=STEP_PARENT_DECISION,
            execution_mode=EXEC_DURABLE_RECORD,
            description=(
                "record parent delegation decision for child project "
                f"{request.child_project!r}"
                + (
                    f" (capability {request.capability!r})"
                    if request.capability
                    else ""
                )
            ),
            route_target=TARGET_PARENT_COORDINATOR,
        )
    )

    # 2. Parent -> child Codex gateway handoff, delegated_coordinator profile.
    child_profile = resolve_role_profile(
        ROLE_DELEGATED_COORDINATOR,
        {
            "parent_project": request.parent_project,
            "child_project": request.child_project,
            "parent_callback_target": request.parent_callback_target,
            "parent_issue": request.parent_issue,
            "redmine_project": request.redmine_project,
        },
    )
    steps.append(
        _handoff_step(
            kind=STEP_CHILD_HANDOFF,
            to_role=request.gateway_provider,
            cross_boundary=request.cross_project,
            description=(
                f"handoff to child Codex gateway ({_realization_verb(child_real)} "
                "child delegated coordinator window)"
            ),
            route_target=TARGET_CHILD_GATEWAY,
            role_profile=child_profile,
            realization=child_real,
            worker_provider=request.worker_provider,
        )
    )
    chain.append(ROLE_DELEGATED_COORDINATOR)

    # 3 & 4. Grandchild stamp + worker handoff, only when grandchild work runs.
    if grandchild_real in (REALIZE_ADOPT, REALIZE_LAUNCH):
        steps.append(
            PlannedStep(
                kind=STEP_GRANDCHILD_STAMP,
                execution_mode=_realization_mode(grandchild_real),
                description=(
                    f"{_realization_verb(grandchild_real)} grandchild lane and "
                    "stamp KIND/DEPTH/PARENT projection"
                ),
                route_target=TARGET_GRANDCHILD_GATEWAY,
            )
        )
        gateway_profile = resolve_role_profile(
            ROLE_IMPLEMENTATION_GATEWAY,
            {
                "lane": request.lane,
                "durable_anchor": request.durable_anchor,
                "upstream_coordinator": request.upstream_coordinator,
            },
        )
        steps.append(
            _handoff_step(
                kind=STEP_WORKER_HANDOFF,
                to_role=request.gateway_provider,
                cross_boundary=True,
                description="handoff to grandchild Codex gateway",
                route_target=TARGET_GRANDCHILD_GATEWAY,
                role_profile=gateway_profile,
                realization=grandchild_real,
                worker_provider=request.worker_provider,
            )
        )
        chain.append(ROLE_IMPLEMENTATION_GATEWAY)

        worker_profile = resolve_role_profile(
            ROLE_IMPLEMENTATION_WORKER,
            {
                "lane": request.lane,
                "durable_anchor": request.durable_anchor,
                "gateway_callback_target": request.gateway_callback_target,
            },
        )
        # Gateway -> same-lane worker is the ONLY Claude-targeted handoff allowed,
        # and only because it never crosses project/lane.
        steps.append(
            _handoff_step(
                kind=STEP_WORKER_HANDOFF,
                to_role=request.worker_provider,
                cross_boundary=False,
                description="grandchild gateway routes to same-lane Claude worker",
                route_target=TARGET_SAME_LANE_WORKER,
                role_profile=worker_profile,
                realization=REALIZE_ADOPT,
                worker_provider=request.worker_provider,
            )
        )
        chain.append(ROLE_IMPLEMENTATION_WORKER)

    # 5. Callback record -> durable record (parent callback target preserved).
    steps.append(
        PlannedStep(
            kind=STEP_CALLBACK_RECORD,
            execution_mode=EXEC_DURABLE_RECORD,
            description="record callback outcome to parent coordinator route",
            route_target=TARGET_PARENT_COORDINATOR,
        )
    )

    return tuple(steps), tuple(chain)


def _handoff_step(
    *,
    kind: str,
    to_role: str,
    cross_boundary: bool,
    description: str,
    route_target: str,
    role_profile: RoleProfileResolution,
    realization: str,
    worker_provider: str = "claude",
) -> PlannedStep:
    """Build a handoff step, failing closed on a forbidden cross-boundary worker send.

    Defense-in-depth for Required behavior #4: even if classification were bypassed,
    constructing a cross-project/cross-lane direct-to-worker step raises rather than
    emitting it. The only permitted worker target is a same-lane (non-cross-boundary)
    worker handoff. The forbidden target keys on the binding-resolved ``worker_provider``
    (Redmine #13569 j#76969 correction 3), not the literal ``claude`` — a rebound worker
    provider does not weaken the gateway-via invariant. Default ``claude`` is
    byte-identical.
    """
    if to_role == worker_provider and cross_boundary:
        raise DelegationRoutePlanError(
            "cross-project/cross-lane worker direct send may never be planned; "
            "route via the gateway"
        )
    return PlannedStep(
        kind=kind,
        execution_mode=_realization_mode(realization),
        description=description,
        route_target=route_target,
        role_profile=role_profile,
    )


def _realization_mode(realization: str) -> str:
    """Map a realization decision to its per-step execution boundary."""
    return EXEC_OPERATOR_CONFIRMED if realization == REALIZE_LAUNCH else EXEC_AUTO


def _realization_verb(realization: str) -> str:
    """Human-readable verb for a realization decision (for step descriptions)."""
    if realization == REALIZE_ADOPT:
        return "adopt"
    if realization == REALIZE_LAUNCH:
        return "launch"
    return realization


__all__ = (
    "PLAN_EXECUTABLE",
    "PLAN_OPERATOR_CONFIRM",
    "PLAN_INSUFFICIENT",
    "PLAN_BLOCKED",
    "PLAN_FAILED",
    "PASS_ELIGIBLE_DISPOSITIONS",
    "EXEC_DURABLE_RECORD",
    "EXEC_AUTO",
    "EXEC_OPERATOR_CONFIRMED",
    "REALIZE_ADOPT",
    "REALIZE_LAUNCH",
    "REALIZE_SAME_LANE_FALLBACK",
    "REALIZE_AMBIGUOUS",
    "REALIZE_NOT_LAUNCHED",
    "REALIZE_NOT_APPLICABLE",
    "OUTPUT_EXECUTE",
    "OUTPUT_RECOMMEND_ONLY",
    "ROUTE_CODEX_GATEWAY",
    "ROUTE_CLAUDE_DIRECT",
    "STEP_PARENT_DECISION",
    "STEP_CHILD_HANDOFF",
    "STEP_GRANDCHILD_STAMP",
    "STEP_WORKER_HANDOFF",
    "STEP_CALLBACK_RECORD",
    "TARGET_CHILD_GATEWAY",
    "TARGET_GRANDCHILD_GATEWAY",
    "TARGET_SAME_LANE_WORKER",
    "TARGET_PARENT_COORDINATOR",
    "PLAN_ROUTE_REALIZABLE",
    "DelegationRoutePlanError",
    "RealizationCandidateView",
    "RouteRequest",
    "PlannedStep",
    "RoutePlan",
    "decide_child_realization",
    "decide_grandchild_realization",
    "plan_delegation_route",
)
