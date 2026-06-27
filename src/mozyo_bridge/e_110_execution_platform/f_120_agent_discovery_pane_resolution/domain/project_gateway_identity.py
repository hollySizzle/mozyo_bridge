"""Live project-gateway lane identity + launch-or-adopt policy (Redmine #12708).

#12668 (``project_gateway.resolve_project_gateway``) answers *"which live pane is
the project gateway?"* over the discovered :class:`TargetCandidate` list. It does
not answer the two questions the GK3500 exploratory smoke (#12698) surfaced:

1. **launch-or-adopt** — when classification lands a request on
   ``giken-cloud-drive-management`` but *no* live lane carries that project's
   gateway identity, the resolver returns ``gateway_missing`` and stops. The
   grandparent coordinator had no semantic way to say *"adopt the existing
   gateway, or launch one"* without copying a ``%pane``.
2. **a visible gateway identity** — a project-scoped Codex lane and the
   department-root / default Codex lane both project ``role=codex``; nothing on
   the projection surface said *which* one is the Cloud Drive **project gateway**
   versus the GK3500 department root.

This module is the **live lane identity** layer the issue asks for, kept separate
from ``project.yaml`` metadata (the prohibition: no fixed pane id in project
metadata; project metadata and live runtime lane state stay separated). It is
pure — it derives a :class:`GatewayLaneIdentity` *route registry* record from the
already-adopted :class:`ProjectScope` (#12658) + the Git ``repo_root`` authority,
classifies any live candidate's gateway ``target_kind``, and decides
launch-vs-adopt over the live candidate list by reusing #12668's resolver. The
declarative policy fields (``lane_kind=parent`` / ``launch_policy`` /
``callback_to=grandparent``) are the design-doc vocabulary from the issue's
``project_gateway:`` example; rendering operator-facing actions and any
side-effecting launch stay in the CLI / cockpit layers, so this has no tmux / git
/ I/O and is fully unit-testable.

Design invariants (consistent with #12668, never weakened here):

- A project gateway is a **strong, non-ambiguous Codex with an adopted project
  scope** — the same identity #12668's resolver binds. This layer *names* and
  *projects* that derived kind; it does not introduce a second pane marker the
  resolver would ignore, and it never treats an ``active`` pane or a copied
  ``%pane`` as authority.
- ``repo_root`` stays the Git authority. The identity carries the project scope
  layered *under* the workspace, never as a substitute (Redmine #12658).
- launch-or-adopt fails closed exactly where the resolver does: ``gateway_missing``
  is the only status that yields a *launch* (start a gateway); ``ambiguous`` and
  ``selector_gap`` are ``blocked`` (refuse — operator must disambiguate / fix the
  route), never a silent pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
    ProjectScope,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    CONFIDENCE_STRONG,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    STATUS_FOUND,
    STATUS_GATEWAY_AMBIGUOUS,
    STATUS_GATEWAY_MISSING,
    STATUS_SELECTOR_GAP,
    TARGET_KIND_PROJECT_GATEWAY,
    GatewayResolution,
    ProjectGatewayRoute,
    resolve_project_gateway,
    start_project_gateway_command,
)

# The lane's role class within the ticketless department-root -> project-gateway
# -> worker hierarchy (issue #12708 `project_gateway:` example). A project
# gateway is the *parent* coordinator: it receives the consultation from the
# grandparent (department root) and decides whether a child implementation lane
# is needed. Display / governance vocabulary, never a routing key.
LANE_KIND_PARENT = "parent"

# The launch/adopt policy for a project gateway lane. The resolver picks adopt
# (a live gateway exists) or launch (none exists); this names that the gateway is
# allowed to be brought up on demand rather than requiring a pre-existing pane.
LAUNCH_POLICY_LAUNCH_OR_ADOPT = "launch_or_adopt"

# Where the gateway returns its `consultation_result` / `no_dispatch` / `blocked`
# / `anchor_required` transition (ticketless-project-gateway-runtime-ux.md
# "Transition / Callback Matrix", 親 -> 祖父 row).
CALLBACK_TO_GRANDPARENT = "grandparent"

# Projected gateway target kinds for `agents targets` (the visible distinction the
# issue requires between the Cloud Drive project gateway and the GK3500 root). A
# pane's kind is *derived* from its already-stamped identity (#12658 project scope
# + #11822 role provenance), never a new pane option:
#
# - PROJECT_GATEWAY: a strong Codex carrying an adopted project scope -- the
#   project's gateway (the unit a `resolve_project_gateway` route can bind).
# - WORKSPACE_ROOT: a strong Codex with no project scope -- the department-root /
#   default coordinator (NOT a project gateway; this is the GK3500 root the smoke
#   mistook for a gateway).
# - WORKER: a Claude implementation lane -- reached only after the gateway creates
#   a Redmine anchor, never a gateway itself.
# - UNKNOWN: a weak / ambiguous / unknown-role pane that cannot bind a route.
TARGET_KIND_WORKSPACE_ROOT = "workspace_root"
TARGET_KIND_WORKER = "worker"
TARGET_KIND_UNKNOWN = "unknown"

# Launch-or-adopt actions. `adopt` reuses a live gateway, `launch` starts one,
# `blocked` fails closed (ambiguous / under-specified route) -- never a guess.
ACTION_ADOPT = "adopt"
ACTION_LAUNCH = "launch"
ACTION_BLOCKED = "blocked"


def classify_target_kind(candidate: TargetCandidate) -> str:
    """Classify a live candidate's gateway ``target_kind`` (pure, projection-only).

    Derived purely from the candidate's already-resolved identity — role +
    role-confidence + adopted project scope — so it stays consistent with what
    :func:`resolve_project_gateway` would bind and introduces no second marker.

    - A Claude pane is a :data:`TARGET_KIND_WORKER` regardless of confidence (the
      implementation lane is never a gateway).
    - A weak or ambiguous role can bind nothing → :data:`TARGET_KIND_UNKNOWN`.
    - A strong Codex with an adopted ``project_scope`` is the
      :data:`TARGET_KIND_PROJECT_GATEWAY`; without one it is the department-root /
      default :data:`TARGET_KIND_WORKSPACE_ROOT`.
    """
    if candidate.role == AGENT_KIND_CLAUDE:
        return TARGET_KIND_WORKER
    if candidate.role != AGENT_KIND_CODEX:
        return TARGET_KIND_UNKNOWN
    if candidate.confidence != CONFIDENCE_STRONG or candidate.ambiguous:
        return TARGET_KIND_UNKNOWN
    if (candidate.project_scope or "").strip():
        return TARGET_KIND_PROJECT_GATEWAY
    return TARGET_KIND_WORKSPACE_ROOT


def gateway_projection(candidate: TargetCandidate) -> dict[str, object]:
    """Display-only gateway identity record for ``agents targets`` (#12708).

    Mirrors the additive ``delegation`` / ``attention`` projections: a small,
    routing-free record that makes the project-gateway distinction visible on the
    target table without folding it into the canonical ``TargetRecord`` routing
    projection (``TargetCandidate.to_dict``). ``is_project_gateway`` is the single
    boolean a grandparent reads to tell the Cloud Drive gateway apart from the
    department root; the project identity fields echo the #12658 stamp.
    """
    kind = classify_target_kind(candidate)
    return {
        "target_kind": kind,
        "is_project_gateway": kind == TARGET_KIND_PROJECT_GATEWAY,
        "project_scope": (candidate.project_scope or "").strip() or None,
        "project_label": (candidate.project_label or "").strip() or None,
    }


@dataclass(frozen=True)
class GatewayLaneIdentity:
    """The runtime lane identity / route registry record for a project gateway.

    The issue's ``project_gateway:`` example, derived from project metadata (an
    adopted :class:`ProjectScope` + the Git ``repo_root``) rather than stored in
    ``project.yaml`` — project metadata and live lane state stay separated, and no
    pane id is ever fixed here. ``repo_root`` is the Git authority; ``project_*``
    is the routing/presentation scope under it; ``role`` / ``target_kind`` /
    ``lane_kind`` / ``launch_policy`` / ``callback_to`` are the declarative
    contract a live gateway lane is expected to satisfy and stamp.
    """

    project_scope: str
    project_label: str
    project_path: str
    repo_root: str
    workspace: Optional[str] = None
    role: str = AGENT_KIND_CODEX
    target_kind: str = TARGET_KIND_PROJECT_GATEWAY
    lane_kind: str = LANE_KIND_PARENT
    launch_policy: str = LAUNCH_POLICY_LAUNCH_OR_ADOPT
    callback_to: str = CALLBACK_TO_GRANDPARENT

    def as_route(self, *, session: Optional[str] = None) -> ProjectGatewayRoute:
        """Build the #12668 semantic resolution route for this identity.

        The route carries only the identity fields the pure resolver matches on
        (``repo_root`` + ``project_scope`` + ``role``); ``session`` is the
        optional narrowing filter. The declarative policy fields stay on the
        identity — they describe the lane, they are not resolver inputs.
        """
        return ProjectGatewayRoute(
            repo_root=self.repo_root,
            project_scope=self.project_scope,
            role=self.role,
            session=session,
            target_kind=self.target_kind,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "project_scope": self.project_scope,
            "project_label": self.project_label,
            "project_path": self.project_path,
            "repo_root": self.repo_root,
            "workspace": self.workspace,
            "role": self.role,
            "target_kind": self.target_kind,
            "lane_kind": self.lane_kind,
            "launch_policy": self.launch_policy,
            "callback_to": self.callback_to,
        }


def gateway_lane_identity_from_scope(
    scope: ProjectScope, *, repo_root: str
) -> GatewayLaneIdentity:
    """Derive a :class:`GatewayLaneIdentity` from an adopted project scope (#12658).

    The project metadata is the source of the *project* identity (scope / label /
    path / parent workspace); ``repo_root`` is the live Git worktree authority
    supplied by the caller. The role-class / policy fields take their gateway
    defaults. No pane id, no live state — the route registry record is derived,
    never stored in ``project.yaml``.
    """
    return GatewayLaneIdentity(
        project_scope=scope.scope,
        project_label=scope.label,
        project_path=scope.path,
        repo_root=repo_root,
        workspace=scope.parent_workspace,
    )


@dataclass(frozen=True)
class LaunchOrAdoptDecision:
    """The classified launch-or-adopt outcome for a project gateway lane (#12708).

    ``action`` is one of :data:`ACTION_ADOPT` / :data:`ACTION_LAUNCH` /
    :data:`ACTION_BLOCKED`. ``adopted`` is the live gateway pane to reuse (only on
    adopt); ``launch_command`` is the concrete ``start_project_gateway`` action
    (only on launch). ``resolution`` is the underlying #12668 resolution so the
    fail-closed near-misses stay inspectable on a blocked outcome.
    """

    action: str
    identity: GatewayLaneIdentity
    resolution: GatewayResolution
    adopted: Optional[TargetCandidate] = None
    launch_command: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when a gateway is reachable (adopt) or launchable (launch).

        ``blocked`` is the only not-ok action; adopt and launch are both forward
        progress for the grandparent -> parent transition.
        """
        return self.action in (ACTION_ADOPT, ACTION_LAUNCH)

    def as_payload(self) -> dict[str, object]:
        return {
            "action": self.action,
            "identity": self.identity.as_payload(),
            "resolution": self.resolution.as_payload(),
            "adopted": self.adopted.to_dict() if self.adopted is not None else None,
            "launch_command": self.launch_command,
            "detail": self.detail,
        }


def resolve_launch_or_adopt(
    candidates: Iterable[TargetCandidate],
    identity: GatewayLaneIdentity,
    *,
    session: Optional[str] = None,
) -> LaunchOrAdoptDecision:
    """Decide adopt / launch / blocked for a project gateway lane (pure, #12708).

    Reuses #12668's :func:`resolve_project_gateway` over the live candidate list so
    the launch-or-adopt policy never grows a divergent identity model, then maps
    its fail-closed status to the lane action:

    - ``found`` → :data:`ACTION_ADOPT` (reuse the single resolved gateway pane).
    - ``gateway_missing`` → :data:`ACTION_LAUNCH` (no live gateway; name the
      concrete ``start_project_gateway`` command for the project workdir).
    - ``gateway_target_ambiguous`` / ``selector_gap`` → :data:`ACTION_BLOCKED`
      (refuse — the operator must narrow the ambiguity or complete the route;
      never a silent pick).

    Selection is by identity only; ``active`` / pane id are never consulted (the
    resolver's invariant carries through).
    """
    route = identity.as_route(session=session)
    resolution = resolve_project_gateway(candidates, route)

    if resolution.status == STATUS_FOUND and resolution.selected is not None:
        return LaunchOrAdoptDecision(
            action=ACTION_ADOPT,
            identity=identity,
            resolution=resolution,
            adopted=resolution.selected,
            detail=(
                "adopt the live project gateway lane "
                f"{resolution.selected.pane_id} (resolved by identity, not pane id)"
            ),
        )

    if resolution.status == STATUS_GATEWAY_MISSING:
        command = start_project_gateway_command(
            route, project_path=identity.project_path or None
        )
        return LaunchOrAdoptDecision(
            action=ACTION_LAUNCH,
            identity=identity,
            resolution=resolution,
            launch_command=command,
            detail=(
                "no live project gateway lane; launch one in the project workdir "
                "(separate window/session is the normal path)"
            ),
        )

    # gateway_target_ambiguous / selector_gap -> fail closed.
    if resolution.status == STATUS_GATEWAY_AMBIGUOUS:
        detail = (
            "multiple live project gateway lanes match; refuse to adopt or launch. "
            "Narrow with a session/cockpit-group filter or retire the duplicate."
        )
    elif resolution.status == STATUS_SELECTOR_GAP:
        detail = (
            "the gateway route is under-specified; complete repo_root + "
            "project_scope before launch-or-adopt. A pane id is a debug escape "
            "hatch, not the route."
        )
    else:  # pragma: no cover - defensive; resolver only emits the four statuses
        detail = f"unhandled resolution status {resolution.status!r}; fail closed"
    return LaunchOrAdoptDecision(
        action=ACTION_BLOCKED,
        identity=identity,
        resolution=resolution,
        detail=detail,
    )
