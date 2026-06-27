"""Semantic project-gateway target resolver (Redmine #12668).

The department-root -> project-gateway route must be expressible without a
volatile pane id (``vibes/docs/logics/ticketless-project-gateway-runtime-ux.md``
"Semantic Targeting Requirement"). This module is the ``resolve_project_gateway``
function from that design doc's swimlane: a **pure** resolver over the already
discovered :class:`TargetCandidate` list (the same ``discover_agents`` ->
``fold_agents_by_pane`` -> :func:`build_target_candidates` pipeline that backs
``agents targets`` / ``handoff`` preflight, so the two never grow divergent
identity models).

It is the project-gateway specialization of the #12663 semantic target selector:
same fail-closed shape (resolve exactly one target by ``role`` + ``repo_root`` +
optional ``project_scope`` + optional ``session``/cockpit group, refuse on
zero/multiple), narrowed to ``target_kind = project_gateway`` (a project-scoped
Codex unit). When #12663 lands the generic selector, this resolver should be
re-expressed on top of it rather than duplicated.

Design invariants (all enforced here, never by the caller):

- The resolver **never** treats an ``active`` pane or a copied ``%pane`` as
  authority. Selection is by identity only.
- ``repo_root`` stays the Git authority. A candidate whose ``project_scope``
  matches but whose ``repo_root`` differs is rejected (``repo_root_mismatch``),
  never selected -- project scope is layered *under* workspace identity, never a
  substitute for it (Redmine #12658).
- A candidate in the right repo but not inside the expected adopted project
  scope is rejected (``project_scope_mismatch``) when a project gate is
  requested.
- Only a *strong, non-ambiguous* role binds. A weak (process-inferred) or
  ambiguous role is never auto-targeted (``weak_or_ambiguous_role``), the same
  fail-closed posture :meth:`PreflightTarget.binds_receiver` already takes.
- Separate window/session placement is the **normal** path: with no explicit
  ``session`` the resolver matches a gateway in any session/window. A
  ``session`` is an optional narrowing filter (session or cockpit group), not a
  same-session requirement.

The result is a fully classified :class:`GatewayResolution`; rendering the
operator-facing next action (start command, ambiguity disambiguators) is the CLI
layer's job, so this stays pure and unit-testable with no tmux / git / I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CODEX,
    CONFIDENCE_STRONG,
    TargetCandidate,
)

# The route's ``target_kind`` (design doc "Semantic Targeting Requirement").
# A project gateway is a project-scoped coordinator (Codex) unit; the literal is
# part of the stable design vocabulary even though the resolver narrows by role +
# project_scope rather than by a stored pane "kind".
TARGET_KIND_PROJECT_GATEWAY = "project_gateway"

# Resolution status. These literals are the stable classification the design doc
# names (``gateway_missing`` / ``gateway_target_ambiguous`` / ``selector_gap``);
# do not rename without updating the doc + scenario tests.
STATUS_FOUND = "found"
STATUS_GATEWAY_MISSING = "gateway_missing"
STATUS_GATEWAY_AMBIGUOUS = "gateway_target_ambiguous"
STATUS_SELECTOR_GAP = "selector_gap"

# Per-candidate rejection reasons (diagnostics + ambiguity disambiguation). One
# of these is attached to every candidate the resolver declined, so a blocked
# route can explain *why* each near miss was not the gateway.
REASON_ROLE_MISMATCH = "role_mismatch"
REASON_WEAK_OR_AMBIGUOUS_ROLE = "weak_or_ambiguous_role"
REASON_REPO_ROOT_MISMATCH = "repo_root_mismatch"
REASON_PROJECT_SCOPE_MISMATCH = "project_scope_mismatch"
REASON_SESSION_MISMATCH = "session_mismatch"


def _normalize_root(value: Optional[str]) -> str:
    """Canonicalize a repo root for comparison (``~`` / symlinks / trailing /).

    Pure and tolerant: an unreadable path falls back to its stripped string so a
    mismatch fails closed rather than raising. Empty stays empty.
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        return text.rstrip("/")


@dataclass(frozen=True)
class ProjectGatewayRoute:
    """The semantic route inputs for resolving a project gateway target.

    ``repo_root`` is the Git worktree root (workspace authority). ``project_scope``
    is the adopted monorepo project id (Redmine #12658). ``role`` defaults to the
    project-gateway role (``codex``). ``session`` is an *optional* session or
    cockpit-group narrowing filter -- omit it to resolve across separate
    windows/sessions, which is the normal department-root -> project-gateway path.
    """

    repo_root: str
    project_scope: str
    role: str = AGENT_KIND_CODEX
    session: Optional[str] = None
    target_kind: str = TARGET_KIND_PROJECT_GATEWAY

    def as_payload(self) -> dict[str, object]:
        return {
            "repo_root": self.repo_root,
            "project_scope": self.project_scope,
            "role": self.role,
            "session": self.session,
            "target_kind": self.target_kind,
        }


@dataclass(frozen=True)
class GatewayNearMiss:
    """A candidate the resolver declined, with the literal reason it was not the gateway."""

    candidate: TargetCandidate
    reason: str

    def as_payload(self) -> dict[str, object]:
        return {"reason": self.reason, "candidate": self.candidate.to_dict()}


@dataclass(frozen=True)
class GatewayResolution:
    """The classified outcome of :func:`resolve_project_gateway`.

    ``status`` is one of the ``STATUS_*`` literals. ``selected`` is the single
    bound gateway target only when ``status == found``. ``matched`` carries every
    fully-matching candidate (length 1 on found, >= 2 on ambiguous). ``near_misses``
    explains each declined candidate so a blocked route can guide the operator.
    """

    status: str
    route: ProjectGatewayRoute
    selected: Optional[TargetCandidate] = None
    matched: tuple[TargetCandidate, ...] = ()
    near_misses: tuple[GatewayNearMiss, ...] = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_FOUND

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "route": self.route.as_payload(),
            "selected": self.selected.to_dict() if self.selected is not None else None,
            "matched": [c.to_dict() for c in self.matched],
            "near_misses": [nm.as_payload() for nm in self.near_misses],
            "detail": self.detail,
        }


def _candidate_in_session(candidate: TargetCandidate, session: str) -> bool:
    """True when the candidate is a member of ``session`` (canonical session match).

    ``TargetCandidate`` projects the folded pane's canonical session; the
    cockpit session doubles as the display/cockpit group, so an exact name match
    covers both "session" and "cockpit group" route inputs.
    """
    return candidate.session == session


def resolve_project_gateway(
    candidates: Iterable[TargetCandidate],
    route: ProjectGatewayRoute,
) -> GatewayResolution:
    """Resolve exactly one project-gateway target from ``candidates`` (pure, #12668).

    Returns a :class:`GatewayResolution` whose ``status`` is ``found`` with a
    single ``selected`` target, or one of the fail-closed classifications
    (``gateway_missing`` / ``gateway_target_ambiguous`` / ``selector_gap``). The
    filter chain, in order, so the most specific near-miss reason wins:

    1. role must equal ``route.role`` (else ``role_mismatch``)
    2. role must be strong + non-ambiguous (else ``weak_or_ambiguous_role``)
    3. optional ``route.session`` membership (else ``session_mismatch``)
    4. ``repo_root`` must match (else ``repo_root_mismatch`` -- project scope is
       never a substitute for the Git authority)
    5. ``project_scope`` must match (else ``project_scope_mismatch`` -- right repo
       but outside the expected project workdir)

    Selection is by identity only; ``active`` / pane id are never consulted.
    """
    required_missing = []
    if not (route.repo_root or "").strip():
        required_missing.append("repo_root")
    if not (route.project_scope or "").strip():
        required_missing.append("project_scope")
    if not (route.role or "").strip():
        required_missing.append("role")
    if required_missing:
        return GatewayResolution(
            status=STATUS_SELECTOR_GAP,
            route=route,
            detail=(
                "route is under-specified; the semantic project-gateway route "
                "requires " + ", ".join(required_missing) + ". A pane id is a "
                "debug escape hatch, not the normal route."
            ),
        )

    expected_root = _normalize_root(route.repo_root)
    matched: list[TargetCandidate] = []
    near: list[GatewayNearMiss] = []

    for candidate in candidates:
        if candidate.role != route.role:
            near.append(GatewayNearMiss(candidate, REASON_ROLE_MISMATCH))
            continue
        if candidate.confidence != CONFIDENCE_STRONG or candidate.ambiguous:
            near.append(GatewayNearMiss(candidate, REASON_WEAK_OR_AMBIGUOUS_ROLE))
            continue
        if route.session and not _candidate_in_session(candidate, route.session):
            near.append(GatewayNearMiss(candidate, REASON_SESSION_MISMATCH))
            continue
        if _normalize_root(candidate.repo_root) != expected_root:
            near.append(GatewayNearMiss(candidate, REASON_REPO_ROOT_MISMATCH))
            continue
        if (candidate.project_scope or "") != route.project_scope:
            near.append(GatewayNearMiss(candidate, REASON_PROJECT_SCOPE_MISMATCH))
            continue
        matched.append(candidate)

    near_t = tuple(near)
    if len(matched) == 1:
        return GatewayResolution(
            status=STATUS_FOUND,
            route=route,
            selected=matched[0],
            matched=(matched[0],),
            near_misses=near_t,
            detail="resolved exactly one project gateway target",
        )
    if len(matched) >= 2:
        return GatewayResolution(
            status=STATUS_GATEWAY_AMBIGUOUS,
            route=route,
            matched=tuple(matched),
            near_misses=near_t,
            detail=(
                f"{len(matched)} project gateway targets match the route; refuse "
                "to send. Narrow with --session, or disambiguate the candidates."
            ),
        )
    return GatewayResolution(
        status=STATUS_GATEWAY_MISSING,
        route=route,
        near_misses=near_t,
        detail=(
            "no project gateway target matches the route; start one in the "
            "project workdir (separate window/session is fine)."
        ),
    )


def start_project_gateway_command(route: ProjectGatewayRoute, *, project_path: str | None = None) -> str:
    """Concrete ``start_project_gateway`` action for a ``gateway_missing`` outcome.

    Names the standard startup command (Redmine #11803 cockpit launch) run from
    the project workdir, which launches the project-scoped Codex/Claude panes and
    stamps the #12658 separated ``@mozyo_repo_root`` + ``@mozyo_project_scope`` /
    ``_path`` / ``_label`` identity. ``project_path`` is the repo-relative project
    directory when known; otherwise the operator substitutes the workdir for the
    adopted ``project_scope``. Separate window/session projection is expected and
    is not a defect.
    """
    workdir = project_path or f"<workdir of project {route.project_scope}>"
    return (
        f"cd {route.repo_root}/{workdir} && mozyo-bridge cockpit --repo {route.repo_root}  "
        f"# start_project_gateway(project_scope={route.project_scope}, "
        "projection=separate_window_or_session)"
    )
