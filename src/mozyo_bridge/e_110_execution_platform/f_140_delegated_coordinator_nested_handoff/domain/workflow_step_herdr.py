"""herdr-native lane classification + step resolution for `workflow step` (Redmine #13489).

`mozyo-bridge workflow step` resolves the current lane role from the discovered self
candidate under the tmux backend (:func:`...workflow_step.classify_workflow_lane`, a tmux
``%pane`` matched against the tmux inventory). A **pure herdr session** has no ``TMUX_PANE``
and the pane is not in the tmux inventory, so that path folds to
``self_lane_unresolved`` and the #13446 preflight replaced the dead end with a fail-closed
``herdr_self_lane_unresolved`` that just points the operator at ``sublane create/start``.

This module is the herdr-native counterpart that #13489 puts in that preflight's place: it
classifies the current lane role from the **herdr-native identity** (launch-time sender env
``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID`` + the workspace-registry
project scope) instead of a tmux ``%pane``, and maps that role onto the SAME replayable
:class:`~...workflow_step.WorkflowStepOutcome` contract. It grows **no divergent identity
model** (design principle 4): the lane-role vocabulary is exactly the tmux state machine's
four roles, derived here from the documented herdr shared-project-workspace model (spec
``vibes/docs/specs/herdr-native-identity.md`` §1, and the ``sublane list`` fold in
:mod:`...application.sublane_herdr_projection`):

- ``claude`` provider (any lane) -> ``implementation_worker`` (the grandchild worker);
- ``codex`` provider + a **non-default** lane -> ``delegated_coordinator`` (the sublane
  gateway / child coordinator);
- ``codex`` provider + the **default** lane + a resolved project scope -> ``project_gateway``
  (the project coordinator / parent);
- ``codex`` provider + the **default** lane + no project scope -> ``grandparent_coordinator``
  (the department-root coordinator);
- anything else -> fail closed (``herdr_lane_role_unresolved``).

**Scope (increment 1, Redmine #13489 j#74685 design_boundary).** This is *resolution-only*:
it names the role-appropriate next action, next owner, and the herdr surface each lane uses
(the worker reads its own dispatched Redmine anchor; the gateway dispatches / monitors the
same-lane worker via ``sublane dispatch-worker``; the coordinator orchestrates the next
sublane via ``workflow admission`` / ``sublane create|start``). It performs **no** sublane
lifecycle mutation and **no** delivery — the policy-permitted one-step auto-execution of
``sublane create/start/dispatch`` (and the fail-closed destructive drain/retire boundary) is
increment 2, gated behind the mandatory task-level design mid-review. Everything here is
pure: value objects + total functions over plain strings / booleans, no subprocess / env /
registry read (the application adapter supplies those).
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_WORKER,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    EXECUTION_NO_OP,
    OWNER_CALLER,
    OWNER_CHILD,
    OWNER_GRANDCHILD,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_CHILD_WORKER_DISPATCH,
    STATE_GRANDCHILD_REDMINE_WORK,
    STATE_GRANDPARENT_CONSULTATION,
    STATE_LANE_UNRESOLVED,
    STATE_PARENT_WORK_INTAKE,
    WorkflowLane,
    WorkflowStepOutcome,
)

# The herdr provider tokens (mzb1 "role" field = runtime provider, not workflow role).
# Kept literal here to avoid importing the terminal-runtime domain into this execution-
# platform module; they mirror `herdr_target_resolution.PROVIDER_CLAUDE / PROVIDER_CODEX`.
HERDR_PROVIDER_CLAUDE = "claude"
HERDR_PROVIDER_CODEX = "codex"

# The normalized stand-in for an unset lane (mirrors `herdr_identity.DEFAULT_LANE`); a
# herdr coordinator pair sits in this lane, every sublane slot in a non-default lane.
HERDR_DEFAULT_LANE = "default"


# ---------------------------------------------------------------------------
# herdr-native reason vocabulary (machine-readable; kept literal regardless of UI language).
# Namespaced `herdr_*` so the mid-review (Redmine #13489 j#74685) and any regression can see
# the herdr resolution vocabulary distinctly from the tmux state machine's reasons.
# ---------------------------------------------------------------------------

#: The worker lane's own step: read its dispatched Redmine anchor and implement (no dispatch).
REASON_HERDR_WORKER_STEP_READY = "herdr_worker_step_ready"
#: The gateway lane's step: its same-lane worker slot is live, so dispatch / monitor it.
REASON_HERDR_WORKER_DISPATCH_READY = "herdr_worker_dispatch_ready"
#: The gateway lane's step is blocked: no live same-lane worker slot to dispatch to.
REASON_HERDR_WORKER_SLOT_MISSING = "herdr_worker_slot_missing"
#: The coordinator lane's step: orchestrate the next sublane via the coordinator surfaces.
REASON_HERDR_COORDINATOR_ORCHESTRATION = "herdr_coordinator_orchestration"
#: The current herdr lane could not be classified into a workflow role (unknown provider).
REASON_HERDR_LANE_ROLE_UNRESOLVED = "herdr_lane_role_unresolved"
#: The herdr-native sender identity itself could not be resolved (missing / mismatched env,
#: unreadable anchor). The application adapter maps the `resolve_sender_identity` failure
#: reason into this single fail-closed workflow-step reason (detail carries the specifics).
REASON_HERDR_SENDER_IDENTITY_UNRESOLVED = "herdr_sender_identity_unresolved"

HERDR_STEP_REASONS = frozenset(
    {
        REASON_HERDR_WORKER_STEP_READY,
        REASON_HERDR_WORKER_DISPATCH_READY,
        REASON_HERDR_WORKER_SLOT_MISSING,
        REASON_HERDR_COORDINATOR_ORCHESTRATION,
        REASON_HERDR_LANE_ROLE_UNRESOLVED,
        REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    }
)


def classify_herdr_workflow_lane(
    *,
    provider: str,
    lane_id: str,
    project_scope: str,
    repo_root: str,
    locator: str = "",
) -> WorkflowLane:
    """Classify the current herdr lane's workflow role from its herdr-native identity (pure).

    Mirrors :func:`...workflow_step.classify_workflow_lane` but derives the role from the
    herdr shared-project-workspace model instead of a tmux ``TargetCandidate``:

    - ``claude`` provider (any lane) -> ``implementation_worker`` — a Claude slot is always
      the grandchild worker, never a coordinator (matches the tmux rule);
    - ``codex`` provider + a **non-default** lane -> ``delegated_coordinator`` — a sublane
      lane's codex slot is its gateway / child coordinator;
    - ``codex`` provider + the **default** lane + a project scope -> ``project_gateway``;
    - ``codex`` provider + the **default** lane + no project scope ->
      ``grandparent_coordinator`` (the department root has no project scope of its own);
    - any other provider -> ``caller_role=None`` / ``provider_safe=False`` so the resolver
      fails closed rather than route on a guessed role.

    ``locator`` is the herdr transient locator (``agent list`` ``pane_id``) carried into the
    outcome's ``self_pane`` for diagnostics only — never a route authority (spec §2).
    """
    provider = (provider or "").strip()
    lane = (lane_id or "").strip() or HERDR_DEFAULT_LANE
    scope = (project_scope or "").strip()
    root = (repo_root or "").strip()

    if provider == HERDR_PROVIDER_CLAUDE:
        return WorkflowLane(
            self_pane=locator,
            caller_role=ROLE_IMPLEMENTATION_WORKER,
            repo_root=root,
            project_scope=scope,
            provider_safe=True,
            detail=f"herdr implementation worker lane (grandchild), lane={lane!r}",
        )

    if provider == HERDR_PROVIDER_CODEX:
        if lane != HERDR_DEFAULT_LANE:
            caller_role = ROLE_DELEGATED_COORDINATOR
            detail = f"herdr sublane gateway lane (child), lane={lane!r}"
        elif scope:
            caller_role = ROLE_PROJECT_GATEWAY
            detail = "herdr project gateway / coordinator lane (parent), default lane"
        else:
            caller_role = ROLE_GRANDPARENT_COORDINATOR
            detail = "herdr department-root coordinator lane (grandparent), default lane"
        return WorkflowLane(
            self_pane=locator,
            caller_role=caller_role,
            repo_root=root,
            project_scope=scope,
            provider_safe=True,
            detail=detail,
        )

    return WorkflowLane(
        self_pane=locator,
        caller_role=None,
        repo_root=root,
        project_scope=scope,
        provider_safe=False,
        detail=(
            f"herdr sender provider {provider!r} is not a known runtime provider "
            f"({HERDR_PROVIDER_CLAUDE!r} / {HERDR_PROVIDER_CODEX!r}); cannot classify the "
            "workflow lane role — fail closed rather than route on a guessed role"
        ),
    )


def resolve_herdr_workflow_step(
    lane: WorkflowLane,
    *,
    same_lane_worker_live: Optional[bool] = None,
) -> WorkflowStepOutcome:
    """Map a classified herdr lane onto a resolution-only :class:`WorkflowStepOutcome` (pure).

    Increment 1 (Redmine #13489 j#74685 design_boundary): resolution-only — it names the
    role-appropriate next action, next owner, and the herdr surface each lane uses, and
    performs no sublane mutation and no delivery (``primitive=none`` throughout; the
    policy-permitted one-step auto-execution is increment 2, gated behind the mandatory
    task-level design mid-review).

    - ``implementation_worker`` -> ``no_op`` (``herdr_worker_step_ready``): read the lane's
      own dispatched Redmine anchor and implement / record on the durable record. The worker
      reads its anchor from the durable record (not a ``workflow step`` flag), so this needs
      no anchor input and is the worker's own action, never a dispatch.
    - ``delegated_coordinator`` (sublane gateway): with a live same-lane worker slot ->
      ``no_op`` (``herdr_worker_dispatch_ready``) naming ``sublane dispatch-worker`` and the
      dispatch-vs-monitor decision the lane's Redmine gate settles (increment 2); with no
      live worker slot -> ``blocked`` (``herdr_worker_slot_missing``), next owner the child
      (launch the worker lane, then step again). ``same_lane_worker_live=None`` (inventory
      unavailable) also blocks, conservatively.
    - ``project_gateway`` / ``grandparent_coordinator`` (coordinator pair) -> ``no_op``
      (``herdr_coordinator_orchestration``): the coordinator resolves the next sublane
      action via ``workflow admission`` / ``sublane create|start --execute``. herdr-native
      coordinator orchestration inside ``workflow step`` is increment 2.
    - unclassified lane -> ``blocked`` (``herdr_lane_role_unresolved``), next owner operator.
    """
    base = dict(
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
        durable_anchor="none",
    )

    if lane.caller_role is None or not lane.provider_safe:
        return WorkflowStepOutcome(
            state=STATE_LANE_UNRESOLVED,
            next_action=(
                "resolve the current herdr lane identity before stepping: the launch-time "
                "sender env (MOZYO_AGENT_ROLE) must name a known provider (claude / codex). "
                "Run from inside an attested herdr lane agent."
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_HERDR_LANE_ROLE_UNRESOLVED,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            detail=lane.detail,
            **base,
        )

    if lane.caller_role == ROLE_IMPLEMENTATION_WORKER:
        return WorkflowStepOutcome(
            state=STATE_GRANDCHILD_REDMINE_WORK,
            next_action=(
                "read this worker lane's dispatched Redmine anchor (issue + journal) and its "
                "required docs, then implement / verify and record implementation_done / "
                "review_request on the durable record. This is the worker's own action — "
                "workflow step performs no dispatch here."
            ),
            execution=EXECUTION_NO_OP,
            reason=REASON_HERDR_WORKER_STEP_READY,
            next_owner=OWNER_GRANDCHILD,
            primitive=PRIMITIVE_NONE,
            detail=lane.detail,
            **base,
        )

    if lane.caller_role == ROLE_DELEGATED_COORDINATOR:
        if same_lane_worker_live:
            return WorkflowStepOutcome(
                state=STATE_CHILD_WORKER_DISPATCH,
                next_action=(
                    "this sublane gateway's same-lane worker slot is live: dispatch or "
                    "monitor it with `sublane dispatch-worker --execute` for this lane's "
                    "Redmine anchor. Whether to dispatch (worker idle) or monitor "
                    "(implementation in flight) is settled by the lane's Redmine gate — "
                    "herdr-native gate resolution + one-step auto-dispatch is increment 2."
                ),
                execution=EXECUTION_NO_OP,
                reason=REASON_HERDR_WORKER_DISPATCH_READY,
                next_owner=OWNER_CHILD,
                primitive=PRIMITIVE_NONE,
                detail=lane.detail,
                **base,
            )
        return WorkflowStepOutcome(
            state=STATE_CHILD_WORKER_DISPATCH,
            next_action=(
                "this sublane gateway has no live same-lane worker slot to dispatch to: "
                "launch the worker lane (`sublane start --execute`), then step again. "
                "workflow step never launches the worker itself (anchor / lifecycle is the "
                "child's decision)."
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_HERDR_WORKER_SLOT_MISSING,
            next_owner=OWNER_CHILD,
            primitive=PRIMITIVE_NONE,
            detail=(
                lane.detail
                + (
                    "; same-lane worker slot unavailable"
                    if same_lane_worker_live is None
                    else "; no live same-lane worker slot"
                )
            ),
            **base,
        )

    # project_gateway / grandparent_coordinator: the coordinator pair.
    coordinator_state = (
        STATE_PARENT_WORK_INTAKE
        if lane.caller_role == ROLE_PROJECT_GATEWAY
        else STATE_GRANDPARENT_CONSULTATION
    )
    return WorkflowStepOutcome(
        state=coordinator_state,
        next_action=(
            "coordinator lane: resolve the next sublane action from the ready queue + "
            "durable Redmine gate with `workflow admission` and dispatch via `sublane "
            "create|start --execute` (through the coordinator). herdr-native coordinator "
            "orchestration inside workflow step (one-step auto create/start/dispatch, "
            "fail-closed destructive drain/retire) is increment 2."
        ),
        execution=EXECUTION_NO_OP,
        reason=REASON_HERDR_COORDINATOR_ORCHESTRATION,
        next_owner=OWNER_CALLER,
        primitive=PRIMITIVE_NONE,
        detail=lane.detail,
        **base,
    )


__all__ = (
    "HERDR_PROVIDER_CLAUDE",
    "HERDR_PROVIDER_CODEX",
    "HERDR_DEFAULT_LANE",
    "REASON_HERDR_WORKER_STEP_READY",
    "REASON_HERDR_WORKER_DISPATCH_READY",
    "REASON_HERDR_WORKER_SLOT_MISSING",
    "REASON_HERDR_COORDINATOR_ORCHESTRATION",
    "REASON_HERDR_LANE_ROLE_UNRESOLVED",
    "REASON_HERDR_SENDER_IDENTITY_UNRESOLVED",
    "HERDR_STEP_REASONS",
    "classify_herdr_workflow_lane",
    "resolve_herdr_workflow_step",
)
