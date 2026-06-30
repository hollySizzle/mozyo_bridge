"""Enriched, command-result ``workflow.next_action`` (Redmine #12671).

The #12857 :mod:`...domain.workflow_runtime` slice folds a durable event log into per-lane
state and one overall :class:`~...domain.workflow_runtime.NextAction` carrying just
``action`` / ``owner_role`` / ``target_issue`` / ``reason``. The spine roadmap US #12671
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` ``### 設計思想`` /
``### ロードマップUS`` step 2) asks every *workflow-aware command result* to carry a richer
``workflow.next_action`` so an agent never re-derives "what do I do next" from free text:

- ``owner_role`` — the abstract workflow actor that owns the action (already on #12857's
  NextAction; never a runtime provider — provider binding is #12673);
- ``route_identity`` — the **public-safe** stable route pointer of the lane the action
  concerns (``route=… ws=… lane=… role=… pane_name=…``), *never* a pane id. A pane id is
  cache / evidence only (the spine's "pane id は cache/evidence であり authority ではない");
- ``anchor`` — the durable Redmine anchor (``issue:journal``) the next action hangs off, so
  the recommendation is replayable from the durable record;
- ``suggested_command`` — an **auxiliary** CLI hint. The spine is explicit that the
  structured fields are the source of truth and ``suggested_command`` is only a convenience;
- ``risk_level`` / ``requires_confirmation`` — so the half-automatic, explicit-execution
  posture (#12671 "自動 watcher ではなく、まず半自動・明示実行で duplicate / risk /
  fail-closed を固定する") can gate a dangerous action behind an explicit confirm;
- ``blocked_reason`` — a fail-closed diagnostic when the next action cannot be safely
  recommended (an unknown action token, or a lane-targeted action whose route identity did
  not resolve).

This module is **pure**: it maps an already-computed #12857
:class:`~...domain.workflow_runtime.WorkflowRuntimeState` plus two caller-supplied lookup
tables (issue -> public route pointer, issue -> durable anchor) onto the enriched
:class:`WorkflowNextAction`, and wraps the whole thing in the
:class:`WorkflowCommandResult` envelope a workflow-aware command returns
(``{"workflow": {"state": …, "next_action": …}}``). It opens no DB, scans no tmux, and
makes no routing decision of its own — the decision authority stays the #12856/#12855
admission/fill policy the #12857 state already embeds. Persistence and live route
resolution are the caller's concern (the DB store / the route-identity ledger).

Fail-closed posture, consistent with #12856 treating an unreadable lane as
coordinator-blocking rather than dispatchable:

- an action token with no risk policy entry is ``risk_level=critical``,
  ``requires_confirmation=True``, ``blocked_reason=unknown_action`` — never silently
  treated as a safe, low-risk step;
- a lane-targeted routing action (deliver/redeliver callback, perform review, integrate,
  close, retire, resolve blocker) whose ``route_identity`` did not resolve is forced to
  ``requires_confirmation=True`` with ``blocked_reason=route_identity_unresolved`` — the
  command must not recommend delivering to a lane whose live route is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ACTION_AGGREGATE_OWNER_APPROVAL,
    ACTION_AWAIT_IMPLEMENTATION,
    ACTION_CLOSE_ISSUE,
    ACTION_DELIVER_CALLBACK,
    ACTION_DISPATCH_NEXT_SUBLANE,
    ACTION_HOLD,
    ACTION_INTEGRATE,
    ACTION_NONE,
    ACTION_PERFORM_REVIEW,
    ACTION_REDELIVER_CALLBACK,
    ACTION_RESOLVE_BLOCKER,
    ACTION_RESOLVE_OWNER_OR_RELEASE_GATE,
    ACTION_RETIRE_LANE,
    WorkflowRuntimeState,
)

# ---------------------------------------------------------------------------
# Risk vocabulary. Ordered least -> most severe; literal regardless of UI language.
# ---------------------------------------------------------------------------
RISK_NONE = "none"
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"

#: Severity rank, used only to fail closed to the *more* severe of two risks.
_RISK_RANK: dict[str, int] = {
    RISK_NONE: 0,
    RISK_LOW: 1,
    RISK_MEDIUM: 2,
    RISK_HIGH: 3,
    RISK_CRITICAL: 4,
}

# ---------------------------------------------------------------------------
# Fail-closed diagnostics (the ``blocked_reason`` vocabulary). Empty string means
# "not blocked"; each token names exactly one reason the action is not safely
# recommendable as-is.
# ---------------------------------------------------------------------------
BLOCKED_NONE = ""
BLOCKED_UNKNOWN_ACTION = "unknown_action"
BLOCKED_ROUTE_IDENTITY_UNRESOLVED = "route_identity_unresolved"

# ---------------------------------------------------------------------------
# Per-action risk policy: action token -> (risk_level, requires_confirmation,
# suggested_command). The suggested command is auxiliary (the spine: structured
# fields are the source of truth); it points the operator at the standard explicit
# entrypoint for the owed action, never an auto-run. ``requires_confirmation`` is
# True for any action that mutates a lane / target branch / Redmine close state or
# resolves an owner/release/blocker gate, matching the half-automatic posture.
# ---------------------------------------------------------------------------
_ACTION_RISK: dict[str, tuple[str, bool, str]] = {
    # Positive pipeline occupancy / waiting: nothing to confirm, nothing at risk.
    ACTION_NONE: (RISK_NONE, False, ""),
    ACTION_HOLD: (RISK_NONE, False, ""),
    ACTION_AWAIT_IMPLEMENTATION: (RISK_NONE, False, ""),
    # Delivering a first callback is low-risk (a pointer hand-off).
    ACTION_DELIVER_CALLBACK: (RISK_LOW, False, "mozyo-bridge workflow step"),
    # A re-delivery follows a failed callback: confirm so a mis-route is not repeated.
    ACTION_REDELIVER_CALLBACK: (RISK_MEDIUM, True, "mozyo-bridge workflow step"),
    # Review reads + records a gate; auditor action, no destructive side effect.
    ACTION_PERFORM_REVIEW: (RISK_MEDIUM, False, "mozyo-bridge workflow step"),
    # Opening a new sublane consumes coordinator bandwidth: explicit by design.
    ACTION_DISPATCH_NEXT_SUBLANE: (RISK_MEDIUM, True, "mozyo-bridge workflow step"),
    # Resolving a blocker is a coordinator judgement; confirm before acting.
    ACTION_RESOLVE_BLOCKER: (RISK_MEDIUM, True, "mozyo-bridge workflow resume"),
    # Owner aggregation touches the close-approval boundary: high, always confirm.
    ACTION_AGGREGATE_OWNER_APPROVAL: (RISK_HIGH, True, "mozyo-bridge workflow resume"),
    # Integration merges/pushes to a target branch: release-adjacent, confirm.
    ACTION_INTEGRATE: (RISK_HIGH, True, "mozyo-bridge workflow resume"),
    # Close is a governance gate: high, confirm.
    ACTION_CLOSE_ISSUE: (RISK_HIGH, True, "mozyo-bridge workflow resume"),
    # Retirement kills panes / removes worktrees: destructive, confirm.
    ACTION_RETIRE_LANE: (RISK_HIGH, True, "mozyo-bridge workflow resume"),
    # Owner / release / credential / destructive gate: the most severe, always confirm.
    ACTION_RESOLVE_OWNER_OR_RELEASE_GATE: (
        RISK_CRITICAL,
        True,
        "mozyo-bridge workflow resume",
    ),
}

#: Actions that deliver to / act on a *specific* lane and therefore need a resolved
#: live route. (Dispatch picks a brand-new lane; owner/release-gate and hold/await
#: target no single lane — they are intentionally absent so a missing route there is
#: not a false block.) A routing action with an unresolved ``route_identity`` fails
#: closed (the command must not recommend delivering to an unknown live target).
_ROUTING_ACTIONS: frozenset[str] = frozenset(
    {
        ACTION_DELIVER_CALLBACK,
        ACTION_REDELIVER_CALLBACK,
        ACTION_PERFORM_REVIEW,
        ACTION_AGGREGATE_OWNER_APPROVAL,
        ACTION_INTEGRATE,
        ACTION_CLOSE_ISSUE,
        ACTION_RETIRE_LANE,
        ACTION_RESOLVE_BLOCKER,
    }
)


def risk_policy_for(action: str) -> tuple[str, bool, str, str]:
    """Map an action token to ``(risk_level, requires_confirmation, suggested, blocked)``.

    An unrecognized action fails closed to :data:`RISK_CRITICAL` /
    ``requires_confirmation=True`` / :data:`BLOCKED_UNKNOWN_ACTION` with an empty
    suggested command — a token outside the known vocabulary is never treated as a
    safe, low-risk step. A known action returns ``BLOCKED_NONE``.
    """
    entry = _ACTION_RISK.get(action)
    if entry is None:
        return RISK_CRITICAL, True, "", BLOCKED_UNKNOWN_ACTION
    risk, confirm, suggested = entry
    return risk, confirm, suggested, BLOCKED_NONE


def _escalate(risk: str, floor: str) -> str:
    """Return the more severe of two risk levels (fail-closed escalation, never down)."""
    return floor if _RISK_RANK.get(floor, 0) > _RISK_RANK.get(risk, 0) else risk


@dataclass(frozen=True)
class WorkflowNextAction:
    """The enriched, command-result ``workflow.next_action`` (advisory).

    ``action`` / ``owner_role`` / ``target_issue`` / ``reason`` are carried straight
    from the #12857 :class:`~...domain.workflow_runtime.NextAction` (the decision
    authority). ``route_identity`` is the **public-safe** stable route pointer of the
    target lane (no pane id); ``anchor`` is the durable Redmine pointer; the remaining
    fields are this module's risk / confirmation / fail-closed enrichment.
    """

    action: str
    owner_role: str
    target_issue: str
    route_identity: str
    anchor: str
    suggested_command: str
    risk_level: str
    requires_confirmation: bool
    blocked_reason: str
    reason: str

    @property
    def is_blocked(self) -> bool:
        """True when a fail-closed ``blocked_reason`` is set."""
        return bool(self.blocked_reason)

    def as_payload(self) -> dict[str, object]:
        return {
            "action": self.action,
            "owner_role": self.owner_role,
            "target_issue": self.target_issue,
            "route_identity": self.route_identity,
            "anchor": self.anchor,
            "suggested_command": self.suggested_command,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "blocked_reason": self.blocked_reason,
            "reason": self.reason,
        }


def derive_workflow_next_action(
    state: WorkflowRuntimeState,
    *,
    issue_route_pointers: Mapping[str, str] | None = None,
    issue_anchors: Mapping[str, str] | None = None,
) -> WorkflowNextAction:
    """Enrich the #12857 overall next action into a command-result ``next_action`` (pure).

    ``issue_route_pointers`` maps a lane's Redmine issue id to its **public-safe** route
    pointer (e.g. :meth:`RouteIdentity.public_pointer`; never a pane id). ``issue_anchors``
    maps a lane's issue id to its durable Redmine anchor (``issue:journal``). Both default
    to empty — a caller with no persisted route / anchor still gets a well-formed (if
    route-blocked) next action.

    The risk / confirmation / suggested-command come from :func:`risk_policy_for`; a
    lane-targeted routing action whose route pointer is missing is escalated to
    ``requires_confirmation=True`` with :data:`BLOCKED_ROUTE_IDENTITY_UNRESOLVED` and at
    least :data:`RISK_HIGH` (fail-closed: do not recommend delivering to an unknown live
    target).
    """
    routes = issue_route_pointers or {}
    anchors = issue_anchors or {}
    nxt = state.next_action

    risk, confirm, suggested, blocked = risk_policy_for(nxt.action)

    target = nxt.target_issue
    route_identity = routes.get(target, "") if target else ""
    anchor = anchors.get(target, "") if target else ""

    # Fail closed when a lane-targeted routing action has no resolved live route.
    if not blocked and nxt.action in _ROUTING_ACTIONS and not route_identity:
        blocked = BLOCKED_ROUTE_IDENTITY_UNRESOLVED
        confirm = True
        risk = _escalate(risk, RISK_HIGH)

    return WorkflowNextAction(
        action=nxt.action,
        owner_role=nxt.owner_role,
        target_issue=target,
        route_identity=route_identity,
        anchor=anchor,
        suggested_command=suggested,
        risk_level=risk,
        requires_confirmation=confirm,
        blocked_reason=blocked,
        reason=nxt.reason,
    )


@dataclass(frozen=True)
class WorkflowCommandResult:
    """The ``workflow.state`` + ``workflow.next_action`` envelope a command returns.

    :attr:`state` is the #12857 runtime state (the per-lane read model + admission
    outcome + replay anchors); :attr:`next_action` is this module's enriched action.
    :meth:`as_payload` nests both under a single ``workflow`` key — exactly the shape the
    spine wants every workflow-aware command result to carry — so the structured fields,
    not any free-text summary, are the source of truth.
    """

    state: WorkflowRuntimeState
    next_action: WorkflowNextAction

    def as_payload(self) -> dict[str, object]:
        state_payload = self.state.as_payload()
        return {
            "workflow": {
                "advisory": state_payload.get("advisory", True),
                "state": state_payload.get("state", {}),
                "next_action": self.next_action.as_payload(),
            }
        }


def render_command_result_journal(result: WorkflowCommandResult) -> str:
    """Render the enriched command result as a public-safe durable record (pure).

    Reuses the #12857 runtime journal (Bandwidth Record Template + per-lane read model)
    and appends the enriched next-action fields. Only issue ids, the public route pointer,
    durable anchors, and risk / confirmation tokens are emitted — never a pane id or a
    private path.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
        render_runtime_journal,
    )

    na = result.next_action
    lines = [
        render_runtime_journal(result.state),
        "",
        "## Workflow command-result next action",
        "",
        f"- action: {na.action}",
        f"- owner_role: {na.owner_role}",
        f"- target_issue: {na.target_issue or 'none'}",
        f"- route_identity: {na.route_identity or 'unresolved'}",
        f"- anchor: {na.anchor or 'none'}",
        f"- risk_level: {na.risk_level}",
        f"- requires_confirmation: {str(na.requires_confirmation).lower()}",
        f"- blocked_reason: {na.blocked_reason or 'none'}",
        f"- suggested_command: {na.suggested_command or 'none'}",
    ]
    return "\n".join(lines)


__all__ = (
    "RISK_NONE",
    "RISK_LOW",
    "RISK_MEDIUM",
    "RISK_HIGH",
    "RISK_CRITICAL",
    "BLOCKED_NONE",
    "BLOCKED_UNKNOWN_ACTION",
    "BLOCKED_ROUTE_IDENTITY_UNRESOLVED",
    "risk_policy_for",
    "WorkflowNextAction",
    "derive_workflow_next_action",
    "WorkflowCommandResult",
    "render_command_result_journal",
)
