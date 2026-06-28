"""Same-lane-guarded parent -> child intake route resolution (Redmine #12748).

`#12699` (:mod:`...domain.relative_route`) resolves the one-step-down delegation
target from the current Unit's role — for a parent ``project_gateway`` that is the
child ``delegated_coordinator``. But it resolves the child by the *same*
coordinator-class identity the parent itself carries (live discovery cannot tell a
delegated coordinator from a project gateway — both are a strong project-scoped
Codex), so with only the parent's own lane live it would happily *adopt the parent
as its own child*. That is exactly the failure the GK3500 ``parent -> child``
rerun must avoid: the runtime-ux ``親 -> 子`` row's fail condition *"route が
parent 自身へ戻る"* (the route resolves back to the parent's own lane).

This module is the pure, fail-closed resolver for the **parent -> child intake
route** with the same-lane guard the issue requires. The caller (the parent
project gateway) declares its own lane via ``caller_pane`` — used ONLY as a
negative self-fence, never as the routing authority for the target — and the
resolver:

- resolves the child by semantic identity (``repo_root`` + ``project_scope`` +
  the coordinator-class kind), reusing #12708's :func:`resolve_launch_or_adopt`,
  over the candidate list with the caller's own lane **excluded**;
- classifies :data:`STATUS_SAME_LANE` (blocked) when the only coordinator-class
  lane that matches is the caller itself — the child route resolved back to the
  parent — instead of adopting the parent as its own child;
- classifies :data:`STATUS_CHILD_MISSING` (blocked; launch a distinct child),
  :data:`STATUS_CHILD_AMBIGUOUS` (blocked; multiple distinct child lanes), or
  :data:`STATUS_CHILD_RESOLVED` (a single distinct child lane to adopt).

Design invariants (carried from #12668 / #12708 / #12699, never weakened):

- The target child is resolved by **semantic identity**, never an ``active`` /
  copied ``%pane``. ``caller_pane`` is the caller's own lane id, used only to
  fence out the parent so the child cannot be the parent — it is not the route
  authority and never addresses the target.
- The intake itself is **no-anchor**: the parent does not mint a Redmine anchor
  and the route never returns ``anchor_required`` (that is the child's decision).
  Worker dispatch stays anchor-gated downstream.
- Fail closed everywhere a unique distinct child cannot be identified
  (same-lane / missing / ambiguous), never a silent pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CODEX,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    TARGET_KIND_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    ACTION_ADOPT,
    ACTION_LAUNCH,
    GatewayLaneIdentity,
    LaunchOrAdoptDecision,
    resolve_launch_or_adopt,
)


class ChildIntakeRouteError(ValueError):
    """A parent -> child intake route input is malformed (missing caller lane).

    Fail-closed like the sibling gateway / relative-route domain errors: the
    same-lane guard needs the caller's own lane identity, so an absent
    ``caller_pane`` raises rather than resolving without the self-fence (which
    could adopt the parent as its own child).
    """


# Resolution statuses for the parent -> child intake route. Only
# `child_resolved` is a forward (deliverable) outcome; the other three are
# fail-closed blocks with distinct, actionable reasons.
STATUS_CHILD_RESOLVED = "child_resolved"
STATUS_CHILD_MISSING = "child_missing"
STATUS_CHILD_AMBIGUOUS = "child_ambiguous"
STATUS_SAME_LANE = "same_lane"


@dataclass(frozen=True)
class ChildIntakeRoute:
    """The classified parent -> child intake route outcome (#12748).

    ``status`` is one of the ``STATUS_*`` tokens. ``selected`` is the distinct
    child coordinator lane to deliver the work-intake to (only on
    :data:`STATUS_CHILD_RESOLVED`). ``decision`` is the underlying #12708
    launch-or-adopt decision over the candidates with the caller's own lane
    excluded, so the fail-closed near-misses stay inspectable. ``self_is_gateway``
    records whether the caller's own lane matched the coordinator-class identity
    (used to tell :data:`STATUS_SAME_LANE` from :data:`STATUS_CHILD_MISSING`).
    """

    status: str
    repo_root: str
    project_scope: str
    caller_pane: str
    decision: LaunchOrAdoptDecision
    self_is_gateway: bool
    selected: Optional[TargetCandidate] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True only when a single distinct child lane was resolved (deliverable)."""
        return self.status == STATUS_CHILD_RESOLVED

    @property
    def anchor_required(self) -> bool:
        """Always False: the intake is no-anchor; the child owns the anchor decision.

        The parent never returns ``anchor_required`` merely because no Redmine
        anchor exists — that is the child coordinator's create/select/blocked
        decision (Redmine #12748 required behavior).
        """
        return False

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "repo_root": self.repo_root,
            "project_scope": self.project_scope,
            "caller_pane": self.caller_pane,
            "self_is_gateway": self.self_is_gateway,
            "ok": self.ok,
            "anchor_required": self.anchor_required,
            "selected": self.selected.to_dict() if self.selected is not None else None,
            "decision": self.decision.as_payload(),
            "detail": self.detail,
        }


def _child_identity(repo_root: str, project_scope: str) -> GatewayLaneIdentity:
    """The coordinator-class identity for the child (#12699 shared-kind note).

    Live discovery cannot distinguish a delegated coordinator from a project
    gateway — both are a strong project-scoped Codex — so the child is resolved by
    the same :data:`TARGET_KIND_PROJECT_GATEWAY` coordinator-class identity over
    the same ``repo_root`` + ``project_scope``. The same-lane guard, not a separate
    kind marker, is what keeps the child distinct from the parent.
    """
    return GatewayLaneIdentity(
        project_scope=project_scope,
        project_label=project_scope,
        project_path="",
        repo_root=repo_root,
        role=AGENT_KIND_CODEX,
        target_kind=TARGET_KIND_PROJECT_GATEWAY,
    )


def resolve_child_intake_route(
    candidates: Iterable[TargetCandidate],
    *,
    repo_root: str,
    project_scope: str,
    caller_pane: str,
    session: Optional[str] = None,
) -> ChildIntakeRoute:
    """Resolve the parent -> child intake route with the same-lane guard (pure, #12748).

    The caller is the parent project gateway; ``caller_pane`` is its own lane id,
    used only to fence the parent out of the child candidate set (never to address
    the target). The child is resolved by semantic identity over the *other*
    candidates:

    - a single distinct coordinator lane -> :data:`STATUS_CHILD_RESOLVED` (adopt);
    - none, and the caller's own lane is itself the coordinator gateway ->
      :data:`STATUS_SAME_LANE` (the route resolved back to the parent; blocked);
    - none, and the caller is not a coordinator lane -> :data:`STATUS_CHILD_MISSING`
      (no child lane exists; launch a distinct one);
    - multiple distinct coordinator lanes / under-specified ->
      :data:`STATUS_CHILD_AMBIGUOUS` (blocked; disambiguate).

    Fails closed via :class:`ChildIntakeRouteError` when ``caller_pane`` is empty
    (the same-lane guard cannot run without the caller's own lane identity).
    """
    caller_pane = (caller_pane or "").strip()
    if not caller_pane:
        raise ChildIntakeRouteError(
            "parent -> child intake route needs the caller's own lane id "
            "(caller_pane) to fence out the parent so the child cannot resolve "
            "back to the parent's own lane; got an empty caller_pane"
        )

    candidates = list(candidates)
    identity = _child_identity(repo_root, project_scope)

    # Does the caller's own lane match the coordinator-class identity? Resolved over
    # the caller's pane alone, so an ADOPT means the parent itself is the gateway
    # (used to tell same_lane from child_missing). This consults identity only, not
    # the active/copied pane as authority — caller_pane is the caller's self-id.
    self_only = [cand for cand in candidates if cand.pane_id == caller_pane]
    self_decision = resolve_launch_or_adopt(self_only, identity, session=session)
    self_is_gateway = self_decision.action == ACTION_ADOPT

    # Resolve the child over the candidates with the caller's own lane EXCLUDED, so
    # a single remaining coordinator lane is a genuinely distinct child.
    others = [cand for cand in candidates if cand.pane_id != caller_pane]
    decision = resolve_launch_or_adopt(others, identity, session=session)

    if decision.action == ACTION_ADOPT and decision.adopted is not None:
        return ChildIntakeRoute(
            status=STATUS_CHILD_RESOLVED,
            repo_root=repo_root,
            project_scope=project_scope,
            caller_pane=caller_pane,
            decision=decision,
            self_is_gateway=self_is_gateway,
            selected=decision.adopted,
            detail=(
                "adopt the distinct child coordinator lane "
                f"{decision.adopted.pane_id} (resolved by identity, not pane id; "
                "the parent's own lane was excluded)"
            ),
        )

    if decision.action == ACTION_LAUNCH:
        # No coordinator lane among the OTHER candidates. If the caller's own lane
        # is itself the coordinator gateway, the child route resolved back to the
        # parent — fail closed as same_lane (do NOT adopt the parent as its child).
        if self_is_gateway:
            return ChildIntakeRoute(
                status=STATUS_SAME_LANE,
                repo_root=repo_root,
                project_scope=project_scope,
                caller_pane=caller_pane,
                decision=decision,
                self_is_gateway=True,
                detail=(
                    "the only coordinator-class lane that matches is the caller's "
                    "own project gateway lane; the child route resolved back to the "
                    "parent. Refuse to adopt the parent as its own child — launch a "
                    "distinct child coordinator Unit (separate window/session) for "
                    "the project before handing off the work-intake"
                ),
            )
        # The caller is not itself a coordinator lane and no other one exists:
        # there is genuinely no child lane yet. Launch a distinct one.
        return ChildIntakeRoute(
            status=STATUS_CHILD_MISSING,
            repo_root=repo_root,
            project_scope=project_scope,
            caller_pane=caller_pane,
            decision=decision,
            self_is_gateway=False,
            detail=(
                "no live child coordinator lane for the project; launch one as a "
                "cockpit-visible Unit (separate window/session is the normal path) "
                + (decision.launch_command or "")
            ),
        )

    # ACTION_BLOCKED: multiple distinct coordinator lanes match, or the route is
    # under-specified. Fail closed; the underlying decision carries the near-misses.
    return ChildIntakeRoute(
        status=STATUS_CHILD_AMBIGUOUS,
        repo_root=repo_root,
        project_scope=project_scope,
        caller_pane=caller_pane,
        decision=decision,
        self_is_gateway=self_is_gateway,
        detail=(
            "multiple distinct child coordinator lanes match (or the route is "
            "under-specified); refuse to adopt or launch. Narrow with a "
            "session/cockpit-group filter or retire the duplicate child lane. "
            f"{decision.detail}"
        ),
    )


__all__ = (
    "ChildIntakeRouteError",
    "STATUS_CHILD_RESOLVED",
    "STATUS_CHILD_MISSING",
    "STATUS_CHILD_AMBIGUOUS",
    "STATUS_SAME_LANE",
    "ChildIntakeRoute",
    "resolve_child_intake_route",
)
