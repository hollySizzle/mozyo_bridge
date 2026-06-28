"""Unique downstream/upstream candidate resolvers for `workflow step` (Redmine #12755).

Split out of :mod:`...domain.workflow_step` so that module stays under the
module-health line cap. These are the semantic, fail-closed resolvers the state
machine uses to pick exactly one live lane for the anchored worker dispatch (one
step *down*) and the determined callback (one step *up*) — never an implicit
same-session label, never a guess.

Both return a ``(status, candidate)`` pair: ``"<x>_resolved"`` for exactly one
match, ``"<x>_missing"`` for none, ``"<x>_ambiguous"`` for more than one. The
caller resolver additionally returns ``"caller_not_applicable"`` for a lane with
no ticketless caller above it (a grandparent records the result; a worker replies
on the anchored rail).
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    TARGET_KIND_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    TARGET_KIND_WORKER,
    TARGET_KIND_WORKSPACE_ROOT,
    classify_target_kind,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
)


def _unique(callers: list[TargetCandidate], prefix: str) -> tuple[str, Optional[TargetCandidate]]:
    if not callers:
        return f"{prefix}_missing", None
    if len(callers) > 1:
        return f"{prefix}_ambiguous", None
    return f"{prefix}_resolved", callers[0]


def resolve_unique_worker(
    candidates: list[TargetCandidate],
    *,
    self_pane: str,
    repo_root: str,
    project_scope: str,
) -> tuple[str, Optional[TargetCandidate]]:
    """Resolve the unique grandchild implementation worker for the child lane.

    A worker is a Claude implementation lane (:data:`TARGET_KIND_WORKER`) in the
    child's own ``repo_root``, excluding the child's own pane. When the worker
    carries a project scope it must match the child's (a worker may be unscoped).
    The worker is an existing lane (dispatched against the anchor, never launched).
    """
    workers = [
        cand
        for cand in candidates
        if cand.pane_id != self_pane
        and classify_target_kind(cand) == TARGET_KIND_WORKER
        and (cand.repo_root or "").strip() == repo_root
        and (
            not (cand.project_scope or "").strip()
            or (cand.project_scope or "").strip() == project_scope
        )
    ]
    return _unique(workers, "worker")


def resolve_caller_target(
    candidates: list[TargetCandidate],
    *,
    self_pane: str,
    caller_role: Optional[str],
    repo_root: str,
    project_scope: str,
) -> tuple[str, Optional[TargetCandidate]]:
    """Resolve the unique caller lane a callback returns *up* to.

    A project gateway returns to the grandparent (a strong Codex with no project
    scope, :data:`TARGET_KIND_WORKSPACE_ROOT`); a delegated coordinator returns to
    the project gateway (same project scope). Resolved by semantic identity in the
    same ``repo_root``, excluding the caller's own lane. A grandparent (terminal
    recorder) and a worker (anchored reply rail) have no ticketless caller above
    them -> ``"caller_not_applicable"``.
    """
    if caller_role == ROLE_PROJECT_GATEWAY:
        caller_kind = TARGET_KIND_WORKSPACE_ROOT
        require_scope: Optional[str] = None
    elif caller_role == ROLE_DELEGATED_COORDINATOR:
        caller_kind = TARGET_KIND_PROJECT_GATEWAY
        require_scope = project_scope
    else:
        return "caller_not_applicable", None

    callers = [
        cand
        for cand in candidates
        if cand.pane_id != self_pane
        and classify_target_kind(cand) == caller_kind
        and (cand.repo_root or "").strip() == repo_root
        and (require_scope is None or (cand.project_scope or "").strip() == require_scope)
    ]
    return _unique(callers, "caller")


__all__ = ("resolve_unique_worker", "resolve_caller_target")
