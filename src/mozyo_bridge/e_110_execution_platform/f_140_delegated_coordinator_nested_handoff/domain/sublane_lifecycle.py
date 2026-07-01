"""Pure sublane lifecycle projection / planning core (Redmine #12955).

The MVP lifecycle surface under ``mozyo-bridge sublane`` (``create`` / ``start`` /
``list`` / ``status`` / ``retire``) removes the hand-assembled choreography a
coordinator otherwise repeats for every max-5 sublane: derive the worktree / branch /
lane identity, stand up a cockpit-visible gateway + worker pane, and — at end of life —
retire the lane's panes / worktree / local branch safely. This module is the **pure
decision + projection core** of that surface; it holds no IO and discovers nothing.

Three concerns, each pure over caller-supplied facts:

- :func:`project_sublanes` folds a tmux pane inventory (the ``pane_lines`` row dicts) into
  one :class:`SublaneLaneView` per non-default lane — issue id (parsed from the lane
  label), worktree / repo root, the gateway (``codex``) pane, the worker (``claude``)
  pane, branch (from a caller-resolved lookup), and a coarse :data:`SUBLANE_STATE_*`.
  This is the read-only ``list`` / ``status`` projection.

- :func:`plan_sublane_create` composes the already-decided #12604 worktree launch action
  (:func:`...sublane_integration_policy.decide_worktree_launch`) with the pane / role /
  dispatch steps into a replayable :class:`SublaneCreatePlan`. It **fails closed**: a
  missing identity field or a blocked launch decision yields a ``blocked`` plan with no
  steps, never a partial one. It emits the plan; it never actuates it.

- :func:`preflight_sublane_retire` composes the #12604 retire decision
  (:func:`...sublane_integration_policy.decide_retire_integration`) into a
  :class:`SublaneRetirePreflight` carrying the fail-closed verdict, the durable-record
  journal, and the retirement runbook. On :data:`INTEGRATION_BLOCKED` the runbook is
  empty (the lane is *not* retired); on ``retire_ok`` it lists the destructive commands
  the coordinator runs by hand.

Boundary (``vibes/docs/logics/worktree-lifecycle-boundary.md`` — *scope 境界 / Design
Consultation triggers*): the destructive / actuating half of the lifecycle
(``git worktree add/remove`` as a core CLI actuator, pane kill, local branch delete) is
gated behind a separate Design Consultation and is **not** performed here. This module
plans and explains only; it is squarely on the identity / discovery / safety /
planning side of that boundary. It never self-authorizes a close, a carve-out, or an
owner decision, and it never emits private paths or pane ids into a durable journal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    LAUNCH_BLOCKED,
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_REUSE_WORKTREE,
    RetireDecision,
    WorktreeLaunchDecision,
    render_integration_decision_journal,
)

# ---------------------------------------------------------------------------
# Roles + lane identity (literal; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

#: The sublane gateway role — the Codex pane the coordinator routes governed kinds to.
GATEWAY_ROLE = "codex"
#: The sublane worker role — the same-lane Claude implementer.
WORKER_ROLE = "claude"

#: The reserved non-sublane lane id (cockpit / unmanaged panes carry this).
DEFAULT_LANE = "default"

#: ``issue_<id>_<slug>`` lane-label convention (the existing dogfood naming). Only the
#: numeric issue id is extracted; the slug is display-only and never forced-generated
#: here (issue-number -> path/branch generation stays operator judgment per the boundary
#: doc runbook).
_ISSUE_LABEL_RE = re.compile(r"issue[_-](\d+)")


def parse_issue_from_lane_label(lane_label: str) -> Optional[str]:
    """Extract the numeric issue id from an ``issue_<id>_...`` lane label (pure).

    Returns ``None`` when the label carries no ``issue_<digits>`` token, so a lane whose
    label does not follow the convention simply shows no issue rather than a guessed one.
    """
    match = _ISSUE_LABEL_RE.search(lane_label or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# list / status: sublane inventory projection.
# ---------------------------------------------------------------------------

#: Both a gateway and a worker pane are live for the lane.
SUBLANE_STATE_ACTIVE = "active"
#: Only the gateway (Codex) pane is live — the worker was lost / not yet dispatched.
SUBLANE_STATE_GATEWAY_ONLY = "gateway_only"
#: Only the worker (Claude) pane is live — the gateway is missing.
SUBLANE_STATE_WORKER_ONLY = "worker_only"
#: Neither a gateway nor a worker pane is live (only other/unknown-role panes).
SUBLANE_STATE_DETACHED = "detached"

SUBLANE_STATES = frozenset(
    {
        SUBLANE_STATE_ACTIVE,
        SUBLANE_STATE_GATEWAY_ONLY,
        SUBLANE_STATE_WORKER_ONLY,
        SUBLANE_STATE_DETACHED,
    }
)


@dataclass(frozen=True)
class SublanePane:
    """One pane belonging to a sublane (a projection of a pane-inventory row)."""

    pane_id: str
    role: str
    active: bool
    command: str
    cwd: str

    def as_payload(self) -> dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "role": self.role,
            "active": self.active,
            "command": self.command,
            "cwd": self.cwd,
        }


@dataclass(frozen=True)
class SublaneLaneView:
    """The ``list`` / ``status`` projection of a single sublane lane."""

    workspace_id: str
    lane_id: str
    lane_label: str
    issue: Optional[str]
    branch: Optional[str]
    repo_root: Optional[str]
    gateway_pane: Optional[str]
    worker_pane: Optional[str]
    state: str
    panes: Tuple[SublanePane, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "lane_label": self.lane_label,
            "issue": self.issue,
            "branch": self.branch,
            "repo_root": self.repo_root,
            "gateway_pane": self.gateway_pane,
            "worker_pane": self.worker_pane,
            "state": self.state,
            "panes": [p.as_payload() for p in self.panes],
        }


def _lane_state(gateway_pane: Optional[str], worker_pane: Optional[str]) -> str:
    if gateway_pane and worker_pane:
        return SUBLANE_STATE_ACTIVE
    if gateway_pane:
        return SUBLANE_STATE_GATEWAY_ONLY
    if worker_pane:
        return SUBLANE_STATE_WORKER_ONLY
    return SUBLANE_STATE_DETACHED


def project_sublanes(
    pane_rows: Iterable[Mapping[str, str]],
    *,
    branches: Optional[Mapping[str, str]] = None,
) -> list[SublaneLaneView]:
    """Fold a tmux pane inventory into one :class:`SublaneLaneView` per sublane (pure).

    ``pane_rows`` are the ``pane_lines`` row dicts (keys ``id`` / ``agent_role`` /
    ``workspace_id`` / ``lane_id`` / ``lane_label`` / ``cwd`` / ``command`` /
    ``pane_active`` / ``repo_root_stamp`` …). Rows are grouped by ``(workspace_id,
    lane_id)``; the reserved :data:`DEFAULT_LANE` (cockpit / unmanaged panes) is skipped so
    only real sublanes appear. Within a lane the first ``codex`` pane is the gateway and
    the first ``claude`` pane the worker; extra same-role panes are still listed under
    ``panes`` but never silently promoted. ``branches`` is a caller-resolved
    ``lane_id -> branch`` lookup (the domain never runs git); an absent entry leaves
    ``branch`` ``None``. Lanes are returned sorted by ``(workspace_id, lane_id)`` for a
    stable display.
    """
    branches = branches or {}
    grouped: dict[Tuple[str, str], list[SublanePane]] = {}
    labels: dict[Tuple[str, str], str] = {}
    repo_roots: dict[Tuple[str, str], str] = {}

    for row in pane_rows:
        lane_id = (row.get("lane_id") or "").strip() or DEFAULT_LANE
        if lane_id == DEFAULT_LANE:
            continue
        workspace_id = (row.get("workspace_id") or "").strip()
        key = (workspace_id, lane_id)
        pane = SublanePane(
            pane_id=(row.get("id") or "").strip(),
            role=(row.get("agent_role") or "").strip(),
            active=(row.get("pane_active") or "").strip() == "1",
            command=(row.get("command") or "").strip(),
            cwd=(row.get("cwd") or "").strip(),
        )
        grouped.setdefault(key, []).append(pane)
        # Keep the first non-empty lane label seen for the lane.
        if not labels.get(key):
            labels[key] = (row.get("lane_label") or "").strip()
        # Prefer an explicit repo-root stamp; fall back to the pane cwd.
        if key not in repo_roots or not repo_roots[key]:
            repo_roots[key] = (
                (row.get("repo_root_stamp") or "").strip() or pane.cwd
            )

    views: list[SublaneLaneView] = []
    for key in sorted(grouped):
        workspace_id, lane_id = key
        panes = tuple(grouped[key])
        gateway = next((p.pane_id for p in panes if p.role == GATEWAY_ROLE), None)
        worker = next((p.pane_id for p in panes if p.role == WORKER_ROLE), None)
        lane_label = labels.get(key, "")
        views.append(
            SublaneLaneView(
                workspace_id=workspace_id,
                lane_id=lane_id,
                lane_label=lane_label,
                issue=parse_issue_from_lane_label(lane_label),
                branch=branches.get(lane_id),
                repo_root=repo_roots.get(key) or None,
                gateway_pane=gateway,
                worker_pane=worker,
                state=_lane_state(gateway, worker),
                panes=panes,
            )
        )
    return views


# ---------------------------------------------------------------------------
# create / start: fail-closed launch plan.
# ---------------------------------------------------------------------------

#: The plan is complete and replayable.
CREATE_PLANNED = "planned"
#: Fail-closed: a required identity field is missing, or the launch decision refused; no
#: steps are emitted.
CREATE_BLOCKED = "blocked"

CREATE_STATES = frozenset({CREATE_PLANNED, CREATE_BLOCKED})


@dataclass(frozen=True)
class SublaneCreateRequest:
    """The operator-supplied identity for a ``sublane create`` (never forced-generated).

    Every field is caller-supplied so the domain never fabricates a worktree path or
    branch from the issue number (the boundary doc keeps issue-number -> path/branch
    generation an operator decision). ``journal`` is the durable anchor the dispatch
    steps point at. ``upstream_coordinator`` is the coordinator pane the gateway calls
    back to; ``None`` renders a placeholder in the dispatch step.
    """

    issue: str
    lane_label: str
    branch: str
    worktree_path: str
    journal: Optional[str] = None
    upstream_coordinator: Optional[str] = None
    gateway_role: str = GATEWAY_ROLE
    worker_role: str = WORKER_ROLE

    def missing_fields(self) -> Tuple[str, ...]:
        """The required identity fields left blank (fail-closed trigger)."""
        missing = []
        if not (self.issue or "").strip():
            missing.append("issue")
        if not (self.lane_label or "").strip():
            missing.append("lane_label")
        if not (self.branch or "").strip():
            missing.append("branch")
        if not (self.worktree_path or "").strip():
            missing.append("worktree_path")
        return tuple(missing)


@dataclass(frozen=True)
class SublaneStep:
    """One ordered, replayable step of a create plan.

    ``command`` is the concrete shell command when the step is directly replayable
    (``git worktree add`` / ``handoff send``); ``None`` when the step is a runbook
    pointer (adopt the pane's role via ``init``) whose exact form is operator / cockpit
    dependent.
    """

    order: int
    title: str
    detail: str
    command: Optional[str] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "order": self.order,
            "title": self.title,
            "detail": self.detail,
            "command": self.command,
        }


@dataclass(frozen=True)
class SublaneCreatePlan:
    """The result of :func:`plan_sublane_create`."""

    status: str
    reason: str
    launch_action: Optional[str] = None
    steps: Tuple[SublaneStep, ...] = ()
    blocked_reasons: Tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return self.status == CREATE_BLOCKED

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "launch_action": self.launch_action,
            "steps": [s.as_payload() for s in self.steps],
            "blocked_reasons": list(self.blocked_reasons),
        }


def plan_sublane_create(
    request: SublaneCreateRequest, launch: WorktreeLaunchDecision
) -> SublaneCreatePlan:
    """Compose the #12604 launch decision with the pane / dispatch steps (pure).

    Fail-closed precedence:

    1. any required identity field is blank -> :data:`CREATE_BLOCKED` (``missing_field``
       reasons), no steps;
    2. the launch decision is :data:`LAUNCH_BLOCKED` -> :data:`CREATE_BLOCKED` carrying the
       decision reason, no steps (never plan against an unverified target);
    3. otherwise -> :data:`CREATE_PLANNED` with the ordered worktree + gateway + worker +
       dispatch steps. A :data:`LAUNCH_REUSE_WORKTREE` action renders the worktree step as
       a no-op reuse note rather than a ``git worktree add``.

    The steps are a *plan*: this function actuates nothing.
    """
    missing = request.missing_fields()
    if missing:
        return SublaneCreatePlan(
            status=CREATE_BLOCKED,
            reason="required sublane identity fields are missing; refusing to plan a "
            "sublane against an incomplete target",
            launch_action=launch.action,
            blocked_reasons=tuple(f"missing_field:{name}" for name in missing),
        )
    if launch.action == LAUNCH_BLOCKED:
        return SublaneCreatePlan(
            status=CREATE_BLOCKED,
            reason=launch.reason,
            launch_action=launch.action,
            blocked_reasons=(LAUNCH_BLOCKED,),
        )

    if launch.action == LAUNCH_CREATE_WORKTREE:
        worktree_step = SublaneStep(
            order=1,
            title="create worktree",
            detail="create the lane worktree / branch with plain git (operator recipe; "
            "not actuated by this command)",
            command=f"git worktree add {request.worktree_path} -b {request.branch}",
        )
    elif launch.action == LAUNCH_REUSE_WORKTREE:
        worktree_step = SublaneStep(
            order=1,
            title="reuse worktree",
            detail=f"a worktree for branch {request.branch!r} already exists; reuse it "
            "(never clobbered)",
            command=None,
        )
    else:
        # skip_no_git / skip_disabled: the sublane runs without a worktree.
        worktree_step = SublaneStep(
            order=1,
            title="skip worktree",
            detail=launch.reason,
            command=None,
        )

    steps = (
        worktree_step,
        SublaneStep(
            order=2,
            title="append gateway pane",
            detail=f"append a cockpit-visible {request.gateway_role} gateway pane for "
            f"lane {request.lane_label!r} and bind its role / workspace / lane / "
            "repo-root stamps",
            command=None,
        ),
        SublaneStep(
            order=3,
            title="append worker pane",
            detail=f"append a cockpit-visible {request.worker_role} worker pane for "
            f"lane {request.lane_label!r} and bind its role / workspace / lane / "
            "repo-root stamps",
            command=None,
        ),
        SublaneStep(
            order=4,
            title="dispatch implementation_request",
            detail="route the governed implementation_request to the gateway "
            "(coordinator -> sublane Codex gateway -> same-lane Claude worker); the "
            "durable Redmine journal is the anchor, the pane message a pointer",
            command=_dispatch_command(request),
        ),
    )
    return SublaneCreatePlan(
        status=CREATE_PLANNED,
        reason="sublane identity resolved; launch action "
        f"{launch.action!r}: {launch.reason}",
        launch_action=launch.action,
        steps=steps,
    )


def _dispatch_command(request: SublaneCreateRequest) -> str:
    """The replayable gateway ``handoff send`` command for the create plan (pure)."""
    journal = request.journal or "<journal>"
    coordinator = request.upstream_coordinator or "<coordinator-pane>"
    return (
        "mozyo-bridge handoff send --to codex --source redmine "
        f"--issue {request.issue} --journal {journal} "
        "--kind implementation_request --target <gateway-pane> --target-repo auto "
        "--mode queue-enter --role-profile implementation_gateway "
        f"--profile-field lane={request.lane_label} "
        f"--profile-field upstream_coordinator={coordinator}"
    )


# ---------------------------------------------------------------------------
# retire: fail-closed preflight + runbook.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SublaneRetirePreflight:
    """The result of :func:`preflight_sublane_retire`.

    ``decision`` is the #12604 :class:`RetireDecision` (authority). ``journal`` is the
    durable-record text (fail-closed record on block; integration-decision record on ok)
    rendered by :func:`render_integration_decision_journal`. ``runbook`` is the ordered
    destructive command list the coordinator runs *by hand* — empty on a blocked
    preflight (the lane is not retired) and on a non-Git lane's Git-specific steps.
    """

    decision: RetireDecision
    journal: str
    runbook: Tuple[SublaneStep, ...] = field(default=())

    @property
    def may_retire(self) -> bool:
        return self.decision.may_retire

    def as_payload(self) -> dict[str, object]:
        return {
            "decision": self.decision.as_payload(),
            "journal": self.journal,
            "runbook": [s.as_payload() for s in self.runbook],
        }


def preflight_sublane_retire(
    decision: RetireDecision,
    *,
    issue: str,
    lane_label: str,
    worktree_path: Optional[str] = None,
    branch: Optional[str] = None,
    integration_branch: Optional[str] = None,
    is_git_workspace: bool = True,
) -> SublaneRetirePreflight:
    """Compose the #12604 retire decision into a fail-closed preflight + runbook (pure).

    On :data:`INTEGRATION_BLOCKED` the runbook is empty — the lane is *not* retired and the
    coordinator is called back with the fail-closed ``journal``. On ``retire_ok`` the
    runbook lists the destructive commands (pane kill / ``git worktree remove`` / local
    branch delete) the coordinator executes by hand under the Sublane Retirement Drain;
    this command never actuates them (the destructive core-CLI actuator is gated behind a
    Design Consultation per ``worktree-lifecycle-boundary.md``). Remote branch deletion is
    never emitted.
    """
    journal = render_integration_decision_journal(
        decision, issue=issue, integration_branch=integration_branch
    )
    if not decision.may_retire:
        return SublaneRetirePreflight(decision=decision, journal=journal, runbook=())

    runbook: list[SublaneStep] = [
        SublaneStep(
            order=1,
            title="confirm clean worktree",
            detail="verify no in-scope dirty / untracked changes remain before removing "
            "the worktree",
            command="git status --short",
        ),
        SublaneStep(
            order=2,
            title="kill lane panes",
            detail=f"guarded-kill the gateway + worker panes for lane {lane_label!r} "
            "(coordinator authority; never a hidden kill)",
            command=None,
        ),
    ]
    if is_git_workspace and worktree_path:
        runbook.append(
            SublaneStep(
                order=3,
                title="remove worktree",
                detail="remove the lane worktree with plain git (operator recipe)",
                command=f"git worktree remove {worktree_path}",
            )
        )
    if is_git_workspace and branch:
        runbook.append(
            SublaneStep(
                order=len(runbook) + 1,
                title="delete local branch",
                detail="delete the merged local branch only; remote branches are never "
                "deleted",
                command=f"git branch -d {branch}",
            )
        )
    return SublaneRetirePreflight(
        decision=decision, journal=journal, runbook=tuple(runbook)
    )


__all__ = (
    "GATEWAY_ROLE",
    "WORKER_ROLE",
    "DEFAULT_LANE",
    "parse_issue_from_lane_label",
    "SUBLANE_STATE_ACTIVE",
    "SUBLANE_STATE_GATEWAY_ONLY",
    "SUBLANE_STATE_WORKER_ONLY",
    "SUBLANE_STATE_DETACHED",
    "SUBLANE_STATES",
    "SublanePane",
    "SublaneLaneView",
    "project_sublanes",
    "CREATE_PLANNED",
    "CREATE_BLOCKED",
    "CREATE_STATES",
    "SublaneCreateRequest",
    "SublaneStep",
    "SublaneCreatePlan",
    "plan_sublane_create",
    "SublaneRetirePreflight",
    "preflight_sublane_retire",
)
