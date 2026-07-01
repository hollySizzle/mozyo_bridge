"""Pure sublane live-actuation outcome / execution vocabulary (Redmine #12973).

#12955 delivered the ``mozyo-bridge sublane`` surface as *planning only*: ``create`` /
``start`` emit a fail-closed, replayable :class:`SublaneCreatePlan`, but never actuate the
``git worktree add`` / cockpit pane append / gateway dispatch a coordinator otherwise hand-
assembles for every max-5 sublane. #12973 adds the **creation-side live actuator** that
executes that plan (`sublane start --execute`), staying on the additive / boundary-approved
side of ``vibes/docs/logics/worktree-lifecycle-boundary.md`` (the #12604
:class:`LiveSublaneGitOperations.create_worktree` additive ``git worktree add`` is already
inside the boundary; the destructive retire-time merge / pane kill / worktree remove stays
gated and is untouched here).

This module is the **pure execution-state vocabulary + outcome value objects** for that
actuator. It holds no IO and orchestrates nothing: the application-layer use case
(:mod:`...application.sublane_actuator`) drives the injected port and assembles these VOs;
this module only names the machine-readable states and renders the durable-record snippet.

Three concerns, each pure:

- the per-step execution status vocabulary (:data:`STEP_EXECUTED` / :data:`STEP_READY` /
  :data:`STEP_SKIPPED` / :data:`STEP_BLOCKED`) and the overall actuation status
  (:data:`ACTUATE_EXECUTED` / :data:`ACTUATE_READY` / :data:`ACTUATE_BLOCKED`);
- the fail-closed blocked-reason tokens (:data:`REASON_*`) — a create-side actuator is
  *additive*, so its fail-closed set is missing identity, an unverified launch target, a
  missing durable anchor, a worktree collision (branch / path already taken), a pane-
  creation / stamp read-back failure, a **lane-identity mismatch** (the resolved lane's
  ``lane_label`` / ``issue`` does not match the request — a repo-root / basename collision
  or a stale lane, which would misdeliver to the wrong gateway), and a handoff-dispatch
  failure; a **dirty worktree fail-closed is a retire-side gate** (#12604
  :func:`decide_retire_integration`), because an additive create never clobbers an existing
  checkout — a collision surfaces as :data:`REASON_WORKTREE_CREATE_FAILED`, not silent data
  loss;
- the :class:`ActuationStep` / :class:`SublaneActuationOutcome` value objects and
  :func:`render_actuation_journal`, the replayable machine-readable record the coordinator
  posts to the Redmine durable anchor (the "Redmine durable record package" of the issue
  scope). Only issue id / lane label / state / launch action / gateway+worker pane / branch
  / worktree / dispatch target are emitted — runtime evidence the acceptance wants recorded
  (``sublane list --json`` shows the same lane), never a hidden ``%pane`` typed as normal UX.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Per-step execution status (literal; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

#: The step ran and its side effect landed (live ``--execute`` run).
STEP_EXECUTED = "executed"
#: Dry-run: the step *would* run; no side effect was performed (the default UX).
STEP_READY = "ready"
#: The step was intentionally not performed (adopt an existing lane / pane, a non-Git skip,
#: or ``--no-dispatch``); not a failure.
STEP_SKIPPED = "skipped"
#: The step failed closed; actuation stopped here and no later step ran.
STEP_BLOCKED = "blocked"

STEP_STATES = frozenset({STEP_EXECUTED, STEP_READY, STEP_SKIPPED, STEP_BLOCKED})

# ---------------------------------------------------------------------------
# Overall actuation status.
# ---------------------------------------------------------------------------

#: A live ``--execute`` run completed every required step.
ACTUATE_EXECUTED = "executed"
#: A dry-run resolved a complete, replayable plan (would execute); no side effect.
ACTUATE_READY = "ready"
#: Fail-closed: a required identity / target / anchor was missing, or a step failed. No
#: partial success is reported as ok.
ACTUATE_BLOCKED = "blocked"

ACTUATE_STATES = frozenset({ACTUATE_EXECUTED, ACTUATE_READY, ACTUATE_BLOCKED})

# ---------------------------------------------------------------------------
# Fail-closed blocked-reason tokens.
# ---------------------------------------------------------------------------

#: A required sublane identity field (issue / lane_label / branch / worktree) was blank.
REASON_MISSING_IDENTITY = "missing_identity"
#: The pure #12604 launch decision refused (unverified target / branch not resolved).
REASON_LAUNCH_BLOCKED = "launch_blocked"
#: A live dispatch was requested but no durable-anchor journal id was supplied — the
#: workflow-step contract fails closed rather than dispatch a worker without an anchor.
REASON_ANCHOR_REQUIRED = "anchor_required"
#: ``git worktree add`` failed — the branch already exists, the worktree path is taken, or
#: git refused. Covers the acceptance's "branch collision" and "worktree collision".
REASON_WORKTREE_CREATE_FAILED = "worktree_create_failed"
#: The cockpit lane column could not be appended, or the read-back did not show a live
#: gateway + worker pane pair for the lane.
REASON_PANE_CREATE_FAILED = "pane_create_failed"
#: The appended / adopted lane did not carry the expected identity stamps (repo-root /
#: lane) on read-back, so the lane could not be positively confirmed.
REASON_STAMP_FAILED = "stamp_failed"
#: The lane resolved for the worktree does not match the requested lane identity
#: (lane_label / issue) — a repo-root / basename collision or a stale / different lane.
#: Adopting or dispatching to it would misdeliver #<issue> to the wrong gateway, so the
#: ambiguous target fails closed before any adopt / dispatch.
REASON_LANE_MISMATCH = "lane_identity_mismatch"
#: The gateway ``implementation_request`` dispatch returned a non-zero / failed outcome.
REASON_HANDOFF_FAILED = "handoff_failed"

BLOCKED_REASONS = frozenset(
    {
        REASON_MISSING_IDENTITY,
        REASON_LAUNCH_BLOCKED,
        REASON_ANCHOR_REQUIRED,
        REASON_WORKTREE_CREATE_FAILED,
        REASON_PANE_CREATE_FAILED,
        REASON_STAMP_FAILED,
        REASON_LANE_MISMATCH,
        REASON_HANDOFF_FAILED,
    }
)

#: The dispatch outcome tokens recorded in the outcome / journal.
DISPATCH_SENT = "sent"
#: Dispatch was intentionally skipped (``--no-dispatch``).
DISPATCH_SKIPPED = "skipped"
#: Dispatch was not reached (dry-run, or actuation blocked before the dispatch step).
DISPATCH_NOT_ATTEMPTED = "not_attempted"


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActuationStep:
    """One ordered actuation step and the status of its (attempted) side effect.

    ``command`` is the concrete, replayable shell command when the step maps to one
    (``git worktree add`` / ``handoff send`` / ``cockpit append``); ``None`` for a read-
    back / confirm step whose exact form is runtime-resolved.
    """

    order: int
    title: str
    status: str
    detail: str
    command: Optional[str] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "order": self.order,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "command": self.command,
        }


@dataclass(frozen=True)
class SublaneActuationOutcome:
    """The machine-readable result of a ``sublane start`` plan / actuation.

    ``status`` is one of :data:`ACTUATE_STATES`. ``execute`` records whether live
    actuation was requested (``False`` = dry-run). The identity / evidence fields
    (``gateway_pane`` / ``worker_pane`` / ``worktree_path`` / ``branch`` /
    ``dispatch_target``) carry the runtime evidence the acceptance wants recorded — the
    same lane a subsequent ``sublane list --json`` shows. ``adopted`` is ``True`` when an
    already-live lane was reused rather than created. ``blocked_reasons`` is the fail-
    closed reason set (empty unless :data:`ACTUATE_BLOCKED`).
    """

    status: str
    execute: bool
    reason: str
    issue: str
    lane_label: str
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    launch_action: Optional[str] = None
    gateway_pane: Optional[str] = None
    worker_pane: Optional[str] = None
    dispatch_target: Optional[str] = None
    dispatch_result: str = DISPATCH_NOT_ATTEMPTED
    durable_anchor: Optional[str] = None
    adopted: bool = False
    steps: Tuple[ActuationStep, ...] = ()
    blocked_reasons: Tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return self.status == ACTUATE_BLOCKED

    @property
    def executed(self) -> bool:
        return self.status == ACTUATE_EXECUTED

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "execute": self.execute,
            "reason": self.reason,
            "issue": self.issue,
            "lane_label": self.lane_label,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "launch_action": self.launch_action,
            "gateway_pane": self.gateway_pane,
            "worker_pane": self.worker_pane,
            "dispatch_target": self.dispatch_target,
            "dispatch_result": self.dispatch_result,
            "durable_anchor": self.durable_anchor,
            "adopted": self.adopted,
            "steps": [s.as_payload() for s in self.steps],
            "blocked_reasons": list(self.blocked_reasons),
        }


def render_actuation_journal(outcome: SublaneActuationOutcome) -> str:
    """Render the actuation outcome as a replayable durable-record snippet (pure).

    This is the "Redmine durable record package" of the issue scope: a machine-readable
    pointer the coordinator posts to the durable anchor. It carries the lane identity and
    the resolved gateway / worker pane evidence (the acceptance wants the created / adopted
    lane confirmable as issue / gateway / worker / branch / state), the launch action, and
    the dispatch outcome. On a fail-closed run it records the blocked reasons and the next
    owner instead of a partial-success claim.
    """
    heading = (
        "## sublane actuation blocked"
        if outcome.is_blocked
        else (
            "## sublane actuated"
            if outcome.execute
            else "## sublane actuation plan (dry-run)"
        )
    )
    lines = [
        heading,
        "",
        f"- issue: #{outcome.issue}",
        f"- lane_label: {outcome.lane_label or '-'}",
        f"- state: {outcome.status}",
        f"- execute: {str(outcome.execute).lower()}",
        f"- adopted: {str(outcome.adopted).lower()}",
        f"- launch_action: {outcome.launch_action or '-'}",
        f"- branch: {outcome.branch or '-'}",
        f"- worktree: {outcome.worktree_path or '-'}",
        f"- gateway_pane: {outcome.gateway_pane or '-'}",
        f"- worker_pane: {outcome.worker_pane or '-'}",
        f"- dispatch_target: {outcome.dispatch_target or '-'}",
        f"- dispatch_result: {outcome.dispatch_result}",
        f"- durable_anchor: {outcome.durable_anchor or '-'}",
    ]
    if outcome.is_blocked:
        lines.append("- blocked_reasons: " + ", ".join(outcome.blocked_reasons))
        lines.append(
            "- next_action: coordinator callback (fail-closed; lane not fully actuated)"
        )
    else:
        lines.append(
            "- next_action: "
            + (
                "confirm with `sublane list --json`; gateway routes the "
                "implementation_request to the same-lane worker"
                if outcome.execute
                else "re-run with --execute to actuate the resolved plan"
            )
        )
    return "\n".join(lines)


__all__ = (
    "STEP_EXECUTED",
    "STEP_READY",
    "STEP_SKIPPED",
    "STEP_BLOCKED",
    "STEP_STATES",
    "ACTUATE_EXECUTED",
    "ACTUATE_READY",
    "ACTUATE_BLOCKED",
    "ACTUATE_STATES",
    "REASON_MISSING_IDENTITY",
    "REASON_LAUNCH_BLOCKED",
    "REASON_ANCHOR_REQUIRED",
    "REASON_WORKTREE_CREATE_FAILED",
    "REASON_PANE_CREATE_FAILED",
    "REASON_STAMP_FAILED",
    "REASON_LANE_MISMATCH",
    "REASON_HANDOFF_FAILED",
    "BLOCKED_REASONS",
    "DISPATCH_SENT",
    "DISPATCH_SKIPPED",
    "DISPATCH_NOT_ATTEMPTED",
    "ActuationStep",
    "SublaneActuationOutcome",
    "render_actuation_journal",
)
