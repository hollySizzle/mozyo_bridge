"""Single standard `workflow step` state machine (Redmine #12755).

`mozyo-bridge workflow step` is the one standard command an AI / operator runs to
advance one safe workflow step. The as-is surface
(``project-gateway consult`` / ``child-intake`` / ``handoff send`` /
``handoff ticketless-callback`` / ``handoff q-enter`` / ``delegate-*`` /
``%pane`` debug delivery) leaks route, pane, rail, and role-transition decisions
to the caller. The design (``vibes/docs/logics/workflow-step-command-design.md``)
fixes a single entrypoint: the caller runs the same command and mozyo-bridge
resolves the next safe action from current lane identity + durable gate + route
identity, or fails closed with the next responsible owner and a fixed reason.

This module is the **pure, fail-closed** state machine. It owns the decision; it
performs no tmux mutation and no delivery. It:

- classifies the current lane role from the already-discovered self candidate's
  identity (role / confidence / project scope / ``@mozyo_lane_kind`` stamp) — never
  from caller guesswork (:func:`classify_workflow_lane`);
- resolves the one-step-down delegation route with the existing #12699 / #12748
  resolvers (:func:`resolve_relative_route` / :func:`resolve_child_intake_route`),
  so no divergent identity model grows;
- maps the lane + route (+ optional already-available Redmine anchor / pending
  callback) onto a structured :class:`WorkflowStepOutcome` whose ``state`` /
  ``next_action`` / ``execution`` / ``reason`` / ``next_owner`` / ``primitive`` /
  ``durable_anchor`` are replayable and always name the next owner.

Scope boundaries the state machine **must not** cross (design `## 禁止される自動実行`):
it never creates a domain/design answer, never selects/creates a Redmine issue,
never dispatches a grandchild worker without an existing anchor, and never does
owner-approval / release / credential / destructive work. Those are surfaced as
fail-closed states with the responsible next owner, not auto-performed.

The CLI layer (:mod:`...application.cli_workflow`) gathers the runtime inputs
(self pane, discovered candidates, dry-run flag), calls :func:`resolve_workflow_step`,
and — only for a non-dry-run executable forward leg — dispatches the named
internal primitive. Pane id stays a self-fence / cache, never a route authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    CONFIDENCE_STRONG,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.child_intake_route import (
    STATUS_CHILD_AMBIGUOUS,
    STATUS_CHILD_MISSING,
    STATUS_CHILD_RESOLVED,
    STATUS_SAME_LANE,
    resolve_child_intake_route,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    TARGET_KIND_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    TARGET_KIND_WORKER,
    classify_target_kind,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_WORKER,
    cockpit_visible_from_candidate,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)

# The pending-callback leg derives the `handoff ticketless-callback` rail's required
# fields from the lane's already-determined classification (#12703). The mapping +
# fail-closed error live in a sibling leaf so this module stays under the
# module-health line cap; re-exported below so callers import them from here.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_callback import (
    WorkflowStepError,
    callback_rail_fields,
)

# The delegated-coordinator-tree display kinds (the `@mozyo_lane_kind` projection
# cache, #12466). Imported as the lane-role disambiguator: a delegated coordinator
# and a project gateway are the *same* live identity (strong project-scoped Codex),
# so only this stamp tells the child (delegated_coordinator) lane apart from the
# parent (project_gateway) lane.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_projection import (
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)


# ---------------------------------------------------------------------------
# Fixed vocabulary (machine-readable; kept literal regardless of UI language).
# ---------------------------------------------------------------------------

# ``execution`` — what the step did / would do.
EXECUTION_EXECUTED = "executed"
EXECUTION_READY = "ready"
EXECUTION_DRY_RUN = "dry_run"
EXECUTION_BLOCKED = "blocked"
EXECUTION_NO_OP = "no_op"

# ``next_owner`` — who acts next (design output contract).
OWNER_WORKFLOW = "workflow"
OWNER_CALLER = "caller"
OWNER_PARENT = "parent"
OWNER_CHILD = "child"
OWNER_GRANDCHILD = "grandchild"
OWNER_OPERATOR = "operator"
OWNER_OWNER = "owner"

# ``primitive`` — the internal/compatibility primitive the step resolves to.
PRIMITIVE_CONSULT = "project_gateway_consult"
PRIMITIVE_CHILD_INTAKE = "project_gateway_child_intake"
PRIMITIVE_HANDOFF_SEND = "handoff_send"
PRIMITIVE_TICKETLESS_CALLBACK = "handoff_ticketless_callback"
PRIMITIVE_NONE = "none"

# ``state`` — the resolved workflow state token.
STATE_GRANDPARENT_CONSULTATION = "grandparent_consultation"
STATE_PARENT_WORK_INTAKE = "parent_work_intake"
STATE_CHILD_WORKER_DISPATCH = "child_worker_dispatch"
STATE_GRANDCHILD_REDMINE_WORK = "grandchild_redmine_work"
STATE_PENDING_CALLBACK = "pending_callback"
STATE_LANE_UNRESOLVED = "lane_unresolved"

# ``reason`` — fixed reason tokens (one per terminal outcome).
REASON_CONSULTATION_READY = "consultation_ready"
REASON_GATEWAY_MISSING = "gateway_missing"
REASON_GATEWAY_AMBIGUOUS = "gateway_target_ambiguous"
REASON_GATEWAY_NOT_COCKPIT_VISIBLE = "gateway_not_cockpit_visible"
REASON_WORK_INTAKE_READY = "work_intake_ready"
REASON_CHILD_MISSING = "child_missing"
REASON_CHILD_AMBIGUOUS = "child_ambiguous"
REASON_SAME_LANE_CHILD_ROUTE = "same_lane_child_route"
REASON_WORKER_DISPATCH_READY = "worker_dispatch_ready"
REASON_WORKER_MISSING = "worker_missing"
REASON_WORKER_AMBIGUOUS = "worker_ambiguous"
REASON_ANCHOR_REQUIRED = "anchor_required"
REASON_REDMINE_WORK_READY = "redmine_work_ready"
REASON_WORKER_RUNS_WITHOUT_ANCHOR = "worker_runs_without_anchor"
REASON_CALLBACK_READY = "callback_ready"
REASON_SELF_LANE_UNRESOLVED = "self_lane_unresolved"
REASON_LANE_ROLE_UNRESOLVED = "lane_role_unresolved"
REASON_UNSAFE_PROVIDER_BINDING = "unsafe_provider_binding"


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowAnchor:
    """An already-available Redmine issue/journal anchor for the current lane.

    The state machine never *creates or selects* an anchor (design
    `## 禁止される自動実行`). This carries an anchor the child coordinator has
    already determined out of band, so the *executable* anchored worker-dispatch
    branch can be expressed when — and only when — the anchor exists. The standard
    arg-free CLI surface supplies ``None`` here, so the child lane fails closed with
    :data:`REASON_ANCHOR_REQUIRED`; an anchor is the child's decision, surfaced via
    the ``handoff send`` primitive, not invented by ``workflow step``.
    """

    issue: str
    journal: str = ""

    def pointer(self) -> str:
        if self.journal:
            return f"redmine:issue={self.issue}:journal={self.journal}"
        return f"redmine:issue={self.issue}"


@dataclass(frozen=True)
class PendingCallback:
    """A consultation/work-intake callback state that is *already determined*.

    The project gateway / child coordinator may have reached a workflow state
    (``blocked`` / ``anchor_required`` / ``no_dispatch`` / ``consultation_result`` /
    ``review_ready``) that must be returned to the caller lane via the no-anchor
    callback rail rather than stopping at a local pane answer (the #12737 return
    contract). ``classification`` is the determined state; ``callback_to_role`` is
    the caller lane to return to. The state machine only *routes* an
    already-determined callback — it never decides a domain/design answer.
    """

    classification: str
    callback_to_role: str = ROLE_GRANDPARENT_COORDINATOR
    detail: str = ""


@dataclass(frozen=True)
class WorkflowLane:
    """The resolved current-lane identity the step machine acts from.

    Derived purely from the discovered self candidate (:func:`classify_workflow_lane`):
    ``caller_role`` is one of the relative roles (``grandparent_coordinator`` /
    ``project_gateway`` / ``delegated_coordinator`` / ``implementation_worker``) or
    ``None`` when the lane cannot be classified safely. ``provider_safe`` is false
    when the self candidate's role binding is weak / ambiguous, so an unsafe binding
    fails closed instead of routing on a guessed role.
    """

    self_pane: str
    caller_role: Optional[str]
    repo_root: str
    project_scope: str
    provider_safe: bool
    detail: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "self_pane": self.self_pane,
            "caller_role": self.caller_role,
            "repo_root": self.repo_root,
            "project_scope": self.project_scope,
            "provider_safe": self.provider_safe,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowStepOutcome:
    """The replayable result of one ``workflow step`` (design `## State machine の出力`).

    The contract surface is ``state`` / ``next_action`` / ``execution`` / ``reason``
    / ``next_owner`` / ``primitive`` / ``durable_anchor``: the result is replayable
    and always names the next owner. The ``caller_role`` / ``target_pane`` /
    ``repo_root`` / ``project_scope`` / ``self_pane`` fields are the execution wiring
    the CLI uses to dispatch the named internal primitive (a resolved ``target_pane``
    is only ever a delivery handle the resolver produced — never a route authority
    the operator typed).
    """

    state: str
    next_action: str
    execution: str
    reason: str
    next_owner: str
    primitive: str = PRIMITIVE_NONE
    durable_anchor: str = "none"
    caller_role: str = ""
    target_pane: str = ""
    repo_root: str = ""
    project_scope: str = ""
    self_pane: str = ""
    # Determined-callback execution wiring (only set on the pending-callback leg):
    # ``callback_classification`` is the lane's determined result class, and
    # ``callback_to_role`` is the caller lane role the callback returns to. The CLI
    # derives the full ticketless-callback rail fields from these (see
    # :func:`callback_rail_fields`).
    callback_classification: str = ""
    callback_to_role: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the step is forward progress (executed / ready / no_op / dry_run).

        ``blocked`` is the only not-ok execution: the route could not be safely
        advanced and the named ``next_owner`` must act.
        """
        return self.execution != EXECUTION_BLOCKED

    @property
    def executable(self) -> bool:
        """True when a non-dry-run CLI run may dispatch the named primitive.

        Every ``ready`` leg the design lets ``workflow step`` perform is executable:
        the no-anchor consultation / work-intake forwards, the determined ticketless
        callback, and the anchored worker dispatch (allowed only because the route
        reached ``ready`` — the child lane reaches it solely when an already-available
        Redmine anchor was supplied and a unique grandchild worker resolved). The
        grandchild Redmine-work ``no_op`` and every ``blocked`` outcome are not
        executable (they are the worker's / owner's / operator's action).
        """
        return self.execution == EXECUTION_READY and self.primitive in (
            PRIMITIVE_CONSULT,
            PRIMITIVE_CHILD_INTAKE,
            PRIMITIVE_TICKETLESS_CALLBACK,
            PRIMITIVE_HANDOFF_SEND,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "next_action": self.next_action,
            "execution": self.execution,
            "reason": self.reason,
            "next_owner": self.next_owner,
            "primitive": self.primitive,
            "durable_anchor": self.durable_anchor,
            "caller_role": self.caller_role,
            "target_pane": self.target_pane,
            "repo_root": self.repo_root,
            "project_scope": self.project_scope,
            "self_pane": self.self_pane,
            "callback_classification": self.callback_classification,
            "callback_to_role": self.callback_to_role,
            "ok": self.ok,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Lane-role classification.
# ---------------------------------------------------------------------------

# Fixed detail prefix marking "the self pane is not in the discovered inventory",
# so :func:`_blocked_lane` can tell a not-discovered lane (self_lane_unresolved)
# apart from a discovered-but-weak provider binding (unsafe_provider_binding).
_SELF_NOT_DISCOVERED_PREFIX = "self pane not discovered:"


def classify_workflow_lane(
    self_candidate: Optional[TargetCandidate], *, self_pane: str
) -> WorkflowLane:
    """Classify the current lane's role from its discovered identity (pure, #12755).

    The lane role is derived from already-resolved facts, never caller guesswork
    (design `## State machine の入力`):

    - No self candidate resolved (``self_pane`` not in the inventory) -> a lane with
      ``caller_role=None`` / ``provider_safe=False`` -> the step fails closed
      ``self_lane_unresolved``.
    - A Claude pane / an ``@mozyo_lane_kind=implementation`` stamp ->
      ``implementation_worker`` (the grandchild).
    - A weak / ambiguous Codex role binds nothing -> ``provider_safe=False`` -> the
      step fails closed ``unsafe_provider_binding`` (never routes on a guessed role).
    - A strong Codex with ``@mozyo_lane_kind=delegated_coordinator`` -> the child
      ``delegated_coordinator`` (the stamp is the only thing that tells it apart from
      a project gateway — both are a strong project-scoped Codex).
    - A strong Codex with an adopted ``project_scope`` -> the parent
      ``project_gateway``.
    - A strong Codex with no project scope -> the grandparent
      ``grandparent_coordinator`` (the department root).
    """
    if self_candidate is None:
        return WorkflowLane(
            self_pane=self_pane,
            caller_role=None,
            repo_root="",
            project_scope="",
            provider_safe=False,
            detail=(
                f"{_SELF_NOT_DISCOVERED_PREFIX} the current pane is not in the "
                "discovered agent inventory; cannot resolve the lane role (run from "
                "inside the lane's tmux pane)"
            ),
        )

    repo_root = (self_candidate.repo_root or "").strip()
    project_scope = (self_candidate.project_scope or "").strip()
    lane_kind = (self_candidate.lane_kind or "").strip()

    # A Claude implementation lane (or an explicit implementation stamp) is the
    # grandchild worker regardless of confidence — it is never a coordinator.
    if self_candidate.role == AGENT_KIND_CLAUDE or lane_kind == LANE_KIND_IMPLEMENTATION:
        return WorkflowLane(
            self_pane=self_pane,
            caller_role=ROLE_IMPLEMENTATION_WORKER,
            repo_root=repo_root,
            project_scope=project_scope,
            provider_safe=True,
            detail="implementation worker lane (grandchild)",
        )

    # Anything not a strong, unambiguous Codex cannot bind a coordinator route.
    if (
        self_candidate.role != AGENT_KIND_CODEX
        or self_candidate.confidence != CONFIDENCE_STRONG
        or self_candidate.ambiguous
    ):
        return WorkflowLane(
            self_pane=self_pane,
            caller_role=None,
            repo_root=repo_root,
            project_scope=project_scope,
            provider_safe=False,
            detail=(
                "the current lane's provider binding is weak / ambiguous "
                f"(role={self_candidate.role!r}, confidence={self_candidate.confidence!r}, "
                f"ambiguous={self_candidate.ambiguous}); fail closed rather than "
                "route on a guessed role"
            ),
        )

    if lane_kind == LANE_KIND_DELEGATED_COORDINATOR:
        caller_role = ROLE_DELEGATED_COORDINATOR
        detail = "delegated coordinator lane (child)"
    elif project_scope:
        caller_role = ROLE_PROJECT_GATEWAY
        detail = "project gateway lane (parent)"
    else:
        caller_role = ROLE_GRANDPARENT_COORDINATOR
        detail = "department-root coordinator lane (grandparent)"

    return WorkflowLane(
        self_pane=self_pane,
        caller_role=caller_role,
        repo_root=repo_root,
        project_scope=project_scope,
        provider_safe=True,
        detail=detail,
    )


def _self_candidate(
    candidates: Iterable[TargetCandidate], self_pane: str
) -> Optional[TargetCandidate]:
    for cand in candidates:
        if cand.pane_id == self_pane:
            return cand
    return None


# ---------------------------------------------------------------------------
# State machine.
# ---------------------------------------------------------------------------


def _blocked_lane(lane: WorkflowLane) -> WorkflowStepOutcome:
    """Fail-closed outcome when the lane role cannot be resolved safely.

    The self candidate not being in the inventory (``SELF_NOT_DISCOVERED``) is a
    distinct, more actionable reason than a discovered-but-weak provider binding
    (``unsafe_provider_binding``); :func:`classify_workflow_lane` marks the former
    with a fixed detail prefix so the two stay distinguishable.
    """
    if lane.detail.startswith(_SELF_NOT_DISCOVERED_PREFIX):
        reason = REASON_SELF_LANE_UNRESOLVED
    else:
        reason = REASON_UNSAFE_PROVIDER_BINDING
    return WorkflowStepOutcome(
        state=STATE_LANE_UNRESOLVED,
        next_action=(
            "resolve the current lane identity before stepping: run from inside the "
            "lane's tmux pane with a strong, unambiguous role binding "
            "(mozyo-bridge agents targets to inspect)"
        ),
        execution=EXECUTION_BLOCKED,
        reason=reason,
        next_owner=OWNER_OPERATOR,
        primitive=PRIMITIVE_NONE,
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
        detail=lane.detail,
    )


def _callback_to_role_for_lane(lane: WorkflowLane, pending: PendingCallback) -> str:
    """The caller lane role a callback returns to (derived from the current lane).

    A project gateway returns up to the grandparent coordinator; a delegated
    coordinator returns up to the project gateway. The ticketless callback
    ``read_contract`` only resolves ``grandparent_coordinator`` / ``project_gateway``
    (#12700 / #12706), so this maps to one of those. An explicit
    ``pending.callback_to_role`` that is a valid read-contract token wins.
    """
    if pending.callback_to_role in (ROLE_GRANDPARENT_COORDINATOR, ROLE_PROJECT_GATEWAY):
        # The caller supplied a concrete read-contract role; only override the
        # default placeholder, not an explicit project_gateway return.
        if not (
            pending.callback_to_role == ROLE_GRANDPARENT_COORDINATOR
            and lane.caller_role == ROLE_DELEGATED_COORDINATOR
        ):
            return pending.callback_to_role
    if lane.caller_role == ROLE_DELEGATED_COORDINATOR:
        return ROLE_PROJECT_GATEWAY
    return ROLE_GRANDPARENT_COORDINATOR


def _callback_outcome(
    lane: WorkflowLane, pending: PendingCallback
) -> WorkflowStepOutcome:
    """Route an already-determined callback back to the caller lane (#12737).

    The classification must be one the no-anchor callback rail carries
    (:func:`callback_rail_fields`); an off-rail classification fails closed rather
    than fabricating a callback. ``callback_to_role`` is derived from the current
    lane (a gateway returns to the grandparent, a delegated coordinator to the
    gateway) and is the rail's ``read_contract``.
    """
    callback_to_role = _callback_to_role_for_lane(lane, pending)
    try:
        callback_rail_fields(pending.classification)
    except WorkflowStepError as exc:
        return WorkflowStepOutcome(
            state=STATE_PENDING_CALLBACK,
            next_action=str(exc),
            execution=EXECUTION_BLOCKED,
            reason=REASON_CALLBACK_READY,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            caller_role=lane.caller_role or "",
            self_pane=lane.self_pane,
            repo_root=lane.repo_root,
            project_scope=lane.project_scope,
            detail=str(exc),
        )
    return WorkflowStepOutcome(
        state=STATE_PENDING_CALLBACK,
        next_action=(
            "return the determined consultation/work-intake result to the caller "
            f"lane ({callback_to_role}) via the no-anchor callback rail "
            "(handoff ticketless-callback); do not stop at a local pane answer"
        ),
        execution=EXECUTION_READY,
        reason=REASON_CALLBACK_READY,
        next_owner=OWNER_CALLER,
        primitive=PRIMITIVE_TICKETLESS_CALLBACK,
        durable_anchor="none",
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
        callback_classification=pending.classification,
        callback_to_role=callback_to_role,
        detail=pending.detail
        or f"pending callback classification={pending.classification!r}",
    )


def _grandparent_outcome(
    lane: WorkflowLane,
    candidates: list[TargetCandidate],
) -> WorkflowStepOutcome:
    """Grandparent -> parent: forward a no-anchor ticketless consultation (#12740).

    The grandparent (department root) has no project scope of its own, so the
    consultation target is *the project gateway present in the inventory*, not a
    scope derived from the grandparent lane. Resolve the unique project-gateway
    candidate (a strong project-scoped Codex, :func:`classify_target_kind`),
    excluding the grandparent's own lane:

    - exactly one cockpit-visible gateway -> ``consultation_ready`` (forward consult);
    - exactly one gateway but a detached normal window -> ``gateway_not_cockpit_visible``
      (real lane, not green-path route evidence — #12699);
    - zero -> ``gateway_missing``; more than one -> ``gateway_target_ambiguous``.

    A specific consultation could narrow a multi-gateway inventory to one, but
    ``workflow step`` carries no consultation payload, so multiple gateways fail
    closed rather than guess which project the consultation belongs to.
    """
    base = dict(
        state=STATE_GRANDPARENT_CONSULTATION,
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
    )
    gateways = [
        cand
        for cand in candidates
        if cand.pane_id != lane.self_pane
        and classify_target_kind(cand) == TARGET_KIND_PROJECT_GATEWAY
    ]

    if not gateways:
        return WorkflowStepOutcome(
            next_action=(
                "no project gateway is live to consult; start a cockpit-visible "
                "project gateway Unit (cd <project workdir> && mozyo-bridge cockpit) "
                "or inspect with `mozyo-bridge project-gateway adopt`"
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_GATEWAY_MISSING,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            detail="no project_gateway candidate in the inventory",
            **base,
        )

    if len(gateways) > 1:
        return WorkflowStepOutcome(
            next_action=(
                "multiple project gateways are live; `workflow step` carries no "
                "consultation payload to choose one. Disambiguate by project "
                "(mozyo-bridge project-gateway resolve --project <scope>) before "
                "forwarding"
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_GATEWAY_AMBIGUOUS,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            detail=(
                "ambiguous project_gateway candidates: "
                + ", ".join(c.pane_id for c in gateways)
            ),
            **base,
        )

    gateway = gateways[0]
    # The gateway's own project scope is the consultation target scope (the
    # grandparent has none); carry it so the CLI can `project-gateway consult
    # --target-project <scope>`.
    target_scope = (gateway.project_scope or "").strip()
    if not cockpit_visible_from_candidate(gateway):
        # A matching gateway lane exists but it is a detached normal window: real
        # lane, not a cockpit-visible Unit. Not green-path route evidence (#12699).
        return WorkflowStepOutcome(
            next_action=(
                "the resolved project gateway is a detached normal window, not a "
                "cockpit-visible Unit; bring it up as a cockpit Unit before treating "
                "the route as green (cd <project workdir> && mozyo-bridge cockpit)"
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_GATEWAY_NOT_COCKPIT_VISIBLE,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            detail=f"gateway {gateway.pane_id} is not cockpit-visible",
            **{**base, "project_scope": target_scope},
        )

    return WorkflowStepOutcome(
        next_action=(
            "forward the ticketless consultation to the resolved project gateway "
            f"(project-gateway consult --target-project {target_scope}); the gateway "
            "owns the domain/design decision and the Redmine anchor"
        ),
        execution=EXECUTION_READY,
        reason=REASON_CONSULTATION_READY,
        next_owner=OWNER_PARENT,
        primitive=PRIMITIVE_CONSULT,
        target_pane=gateway.pane_id,
        detail=f"unique cockpit-visible project gateway {gateway.pane_id}",
        **{
            **base,
            "project_scope": target_scope,
            "repo_root": (gateway.repo_root or "").strip() or lane.repo_root,
        },
    )


def _parent_outcome(
    lane: WorkflowLane,
    candidates: list[TargetCandidate],
    *,
    session: Optional[str],
) -> WorkflowStepOutcome:
    """Parent -> child: forward a no-anchor ticketless work-intake (#12748).

    Uses the same-lane-fenced child resolver so the child route can never resolve
    back to the parent's own lane; fails closed on same-lane / missing / ambiguous.
    """
    route = resolve_child_intake_route(
        candidates,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
        caller_pane=lane.self_pane,
        session=session,
    )
    base = dict(
        state=STATE_PARENT_WORK_INTAKE,
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
    )

    if route.status == STATUS_CHILD_RESOLVED and route.selected is not None:
        return WorkflowStepOutcome(
            next_action=(
                "forward the ticketless work-intake to the resolved child "
                "coordinator (project-gateway child-intake); the child owns the "
                "Redmine anchor create/select/blocked decision — do not answer the "
                "domain/design here"
            ),
            execution=EXECUTION_READY,
            reason=REASON_WORK_INTAKE_READY,
            next_owner=OWNER_CHILD,
            primitive=PRIMITIVE_CHILD_INTAKE,
            target_pane=route.selected.pane_id,
            detail=route.detail,
            **base,
        )

    reason = {
        STATUS_SAME_LANE: REASON_SAME_LANE_CHILD_ROUTE,
        STATUS_CHILD_MISSING: REASON_CHILD_MISSING,
        STATUS_CHILD_AMBIGUOUS: REASON_CHILD_AMBIGUOUS,
    }.get(route.status, REASON_CHILD_MISSING)
    return WorkflowStepOutcome(
        next_action=(
            "resolve a distinct child coordinator before intake: "
            + (route.detail or f"child route {route.status}")
        ),
        execution=EXECUTION_BLOCKED,
        reason=reason,
        next_owner=OWNER_OPERATOR,
        primitive=PRIMITIVE_NONE,
        detail=route.detail,
        **base,
    )


def _resolve_unique_worker(
    candidates: list[TargetCandidate], lane: WorkflowLane
) -> tuple[str, Optional[TargetCandidate]]:
    """Resolve the unique grandchild implementation worker for the child lane.

    A worker is a Claude implementation lane (:data:`TARGET_KIND_WORKER`) in the
    child's own ``repo_root``, excluding the child's own pane. When the worker
    carries a project scope it must match the child's. Returns ``("worker_resolved",
    cand)`` for exactly one match, ``("worker_missing", None)`` for none, and
    ``("worker_ambiguous", None)`` for more than one — never a guess. The worker is
    an existing lane (it is dispatched against the anchor, never launched here).
    """
    workers = [
        cand
        for cand in candidates
        if cand.pane_id != lane.self_pane
        and classify_target_kind(cand) == TARGET_KIND_WORKER
        and (cand.repo_root or "").strip() == lane.repo_root
        and (
            not (cand.project_scope or "").strip()
            or (cand.project_scope or "").strip() == lane.project_scope
        )
    ]
    if not workers:
        return "worker_missing", None
    if len(workers) > 1:
        return "worker_ambiguous", None
    return "worker_resolved", workers[0]


def _child_outcome(
    lane: WorkflowLane,
    candidates: list[TargetCandidate],
    *,
    anchor: Optional[WorkflowAnchor],
) -> WorkflowStepOutcome:
    """Child -> grandchild: anchor-gated worker dispatch (never invents an anchor)."""
    base = dict(
        state=STATE_CHILD_WORKER_DISPATCH,
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
    )
    if anchor is None:
        # The worker-dispatch Redmine-anchor requirement is never relaxed. The
        # child owns the create/select/blocked decision; ``workflow step`` does not
        # select or create an issue (design `## 禁止される自動実行`).
        return WorkflowStepOutcome(
            next_action=(
                "decide the Redmine anchor for this work (create / select an issue "
                "+ journal), then dispatch the worker with `handoff send --source "
                "redmine --kind implementation_request`; the worker is anchor-gated"
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_ANCHOR_REQUIRED,
            next_owner=OWNER_CHILD,
            primitive=PRIMITIVE_NONE,
            durable_anchor="none",
            **base,
        )

    # An anchor is already available: resolve the grandchild worker and execute the
    # anchored dispatch. Fail closed (do not launch a worker, do not guess) when no
    # unique worker lane exists — that is the child's decision (launch one, then
    # step again), not ``workflow step``'s.
    status, worker = _resolve_unique_worker(candidates, lane)
    if status != "worker_resolved" or worker is None:
        reason = (
            REASON_WORKER_AMBIGUOUS
            if status == "worker_ambiguous"
            else REASON_WORKER_MISSING
        )
        next_action = (
            "multiple grandchild implementation workers match; `workflow step` will "
            "not guess. Disambiguate (narrow with --session) or dispatch explicitly "
            "with `handoff send`"
            if status == "worker_ambiguous"
            else (
                "no grandchild implementation worker lane is live for this anchor; "
                "launch the worker lane, then step again (the worker is dispatched "
                "against the anchor, never launched by workflow step)"
            )
        )
        return WorkflowStepOutcome(
            next_action=next_action,
            execution=EXECUTION_BLOCKED,
            reason=reason,
            next_owner=OWNER_CHILD,
            primitive=PRIMITIVE_NONE,
            durable_anchor=anchor.pointer(),
            **base,
        )

    return WorkflowStepOutcome(
        next_action=(
            "dispatch the implementation worker on the standard Redmine-anchored "
            f"rail for {anchor.pointer()} (handoff send --to claude --source redmine "
            "--kind implementation_request); the anchor and worker are already "
            "available so the route is forward-executable"
        ),
        execution=EXECUTION_READY,
        reason=REASON_WORKER_DISPATCH_READY,
        next_owner=OWNER_GRANDCHILD,
        primitive=PRIMITIVE_HANDOFF_SEND,
        target_pane=worker.pane_id,
        durable_anchor=anchor.pointer(),
        **base,
    )


def _grandchild_outcome(
    lane: WorkflowLane, *, anchor: Optional[WorkflowAnchor]
) -> WorkflowStepOutcome:
    """Grandchild: execute Redmine-governed work; fail closed without an anchor."""
    base = dict(
        state=STATE_GRANDCHILD_REDMINE_WORK,
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
    )
    if anchor is None:
        return WorkflowStepOutcome(
            next_action=(
                "do not implement: this worker has no Redmine anchor to read. Return "
                "a blocked callback to the child coordinator (worker runs only "
                "against a Redmine anchor)"
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_WORKER_RUNS_WITHOUT_ANCHOR,
            next_owner=OWNER_CHILD,
            primitive=PRIMITIVE_NONE,
            durable_anchor="none",
            **base,
        )
    # The worker reads its anchor and advances the implementation itself; this is
    # not a transport ``workflow step`` performs (it never authors the domain work).
    return WorkflowStepOutcome(
        next_action=(
            f"read the Redmine anchor {anchor.pointer()} and its required docs, then "
            "implement / verify and record implementation_done / review_request on "
            "the durable record"
        ),
        execution=EXECUTION_NO_OP,
        reason=REASON_REDMINE_WORK_READY,
        next_owner=OWNER_GRANDCHILD,
        primitive=PRIMITIVE_NONE,
        durable_anchor=anchor.pointer(),
        **base,
    )


def resolve_workflow_step(
    candidates: Iterable[TargetCandidate],
    *,
    self_pane: str,
    anchor: Optional[WorkflowAnchor] = None,
    pending_callback: Optional[PendingCallback] = None,
    session: Optional[str] = None,
) -> WorkflowStepOutcome:
    """Resolve the single next safe workflow step from the current lane (pure, #12755).

    The entry point of the ``workflow step`` state machine. It classifies the
    current lane from the discovered self candidate, then either resolves a safe
    forward action (consultation forward / work-intake forward / determined
    callback / anchored worker dispatch / grandchild Redmine-work) or fails closed
    with a fixed reason and the responsible next owner. It performs no tmux mutation
    and no delivery — the CLI layer dispatches the named ``primitive`` for an
    executable forward leg.

    Precedence: an already-determined ``pending_callback`` is routed first (the
    #12737 return contract takes priority over forwarding a new step); otherwise the
    lane role selects the one-step-down transition. An unresolved / unsafe lane fails
    closed before any route resolution.
    """
    candidates = list(candidates)
    lane = classify_workflow_lane(_self_candidate(candidates, self_pane), self_pane=self_pane)

    if lane.caller_role is None or not lane.provider_safe:
        return _blocked_lane(lane)

    if pending_callback is not None:
        return _callback_outcome(lane, pending_callback)

    if lane.caller_role == ROLE_GRANDPARENT_COORDINATOR:
        return _grandparent_outcome(lane, candidates)
    if lane.caller_role == ROLE_PROJECT_GATEWAY:
        return _parent_outcome(lane, candidates, session=session)
    if lane.caller_role == ROLE_DELEGATED_COORDINATOR:
        return _child_outcome(lane, candidates, anchor=anchor)
    if lane.caller_role == ROLE_IMPLEMENTATION_WORKER:
        return _grandchild_outcome(lane, anchor=anchor)

    # Unreachable: classify_workflow_lane only returns the four roles or None.
    return _blocked_lane(lane)


__all__ = (
    "EXECUTION_EXECUTED",
    "EXECUTION_READY",
    "EXECUTION_DRY_RUN",
    "EXECUTION_BLOCKED",
    "EXECUTION_NO_OP",
    "OWNER_WORKFLOW",
    "OWNER_CALLER",
    "OWNER_PARENT",
    "OWNER_CHILD",
    "OWNER_GRANDCHILD",
    "OWNER_OPERATOR",
    "OWNER_OWNER",
    "PRIMITIVE_CONSULT",
    "PRIMITIVE_CHILD_INTAKE",
    "PRIMITIVE_HANDOFF_SEND",
    "PRIMITIVE_TICKETLESS_CALLBACK",
    "PRIMITIVE_NONE",
    "STATE_GRANDPARENT_CONSULTATION",
    "STATE_PARENT_WORK_INTAKE",
    "STATE_CHILD_WORKER_DISPATCH",
    "STATE_GRANDCHILD_REDMINE_WORK",
    "STATE_PENDING_CALLBACK",
    "STATE_LANE_UNRESOLVED",
    "REASON_CONSULTATION_READY",
    "REASON_GATEWAY_MISSING",
    "REASON_GATEWAY_AMBIGUOUS",
    "REASON_GATEWAY_NOT_COCKPIT_VISIBLE",
    "REASON_WORK_INTAKE_READY",
    "REASON_CHILD_MISSING",
    "REASON_CHILD_AMBIGUOUS",
    "REASON_SAME_LANE_CHILD_ROUTE",
    "REASON_WORKER_DISPATCH_READY",
    "REASON_WORKER_MISSING",
    "REASON_WORKER_AMBIGUOUS",
    "REASON_ANCHOR_REQUIRED",
    "REASON_REDMINE_WORK_READY",
    "REASON_WORKER_RUNS_WITHOUT_ANCHOR",
    "REASON_CALLBACK_READY",
    "REASON_SELF_LANE_UNRESOLVED",
    "REASON_LANE_ROLE_UNRESOLVED",
    "REASON_UNSAFE_PROVIDER_BINDING",
    "WorkflowStepError",
    "callback_rail_fields",
    "WorkflowAnchor",
    "PendingCallback",
    "WorkflowLane",
    "WorkflowStepOutcome",
    "classify_workflow_lane",
    "resolve_workflow_step",
)
