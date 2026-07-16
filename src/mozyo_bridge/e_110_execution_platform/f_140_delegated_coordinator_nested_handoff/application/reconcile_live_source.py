"""Reconcile live-source seams (Redmine #13758 review R4-F1).

The source live-smoke edge feed for the reconciler: the EXPECTED OWNER's live runtime read
from the herdr inventory. Kept apart from the supervisor composition root (which is at the
module-health line budget) so the matching logic is test-pinned against production-shape
observed-agent records without a live inventory. The exact dispatch anchor (R4-F3) is read
from the raw handoff markers in :mod:`...domain.redmine_journal_source`.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional


def match_lane_worker_runtime(
    agents: "Iterable[object]",
    *,
    workspace_id: str,
    lane_id: str,
    provider: str,
) -> str:
    """The runtime of the agent matching ``(workspace_id, lane_id, provider)`` (pure, fail-closed).

    The matching half of :func:`lane_worker_runtime`, split out so it is test-pinned against
    production-shape observed-agent records without a live herdr inventory. Requires EXACTLY ONE
    matching agent (Redmine #13758 review R5-F2): ZERO matches OR TWO-OR-MORE (a duplicate /
    replacement overlap) return ``""`` — an ambiguous inventory must never resolve to one
    agent's runtime by iteration order and fabricate an edge (the acceptance's
    route-ambiguity / original-recovery zero-send). ``""`` = no edge.
    """
    wsid, lane, prov = (
        str(workspace_id or "").strip(),
        str(lane_id or "").strip(),
        str(provider or "").strip(),
    )
    if not (wsid and lane and prov):
        return ""
    matches = [
        str(getattr(agent, "runtime_state", "") or "").strip()
        for agent in agents or ()
        if str(getattr(agent, "workspace_id", "") or "").strip() == wsid
        and str(getattr(agent, "lane_id", "") or "").strip() == lane
        and str(getattr(agent, "role", "") or "").strip() == prov
    ]
    return matches[0] if len(matches) == 1 else ""  # exactly one, else fail-closed blank


def _live_observed_agents() -> "list":
    """Project the live ``herdr agent list`` inventory to observed-agent records (best-effort)."""
    import os

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (
        project_observed_agents,
    )

    return list(project_observed_agents(list(list_herdr_agent_rows(os.environ))))


def lane_worker_runtime(
    workspace_id: str,
    lane_id: str,
    expected_owner_role: str,
    *,
    agents_fn: Optional[Callable[[], "Iterable[object]"]] = None,
) -> str:
    """The EXPECTED OWNER's live runtime state for a lane from the herdr inventory (best-effort).

    The source live-smoke edge feed (Redmine #13758 review R4-F1): reads the live
    ``herdr agent list`` inventory (or the injected ``agents_fn`` in tests), and returns the
    runtime of the agent whose ``(workspace_id, lane_id, role)`` matches this lane and the
    expected owner's provider (a worker await -> the ``claude`` slot, a gateway await -> the
    ``codex`` slot; :func:`...reconcile_delivery_route.provider_for_role`). Fail-closed to ``""``
    on any unavailability / no match — a blank runtime is no edge (never a fabricated one). Live
    running agents are the source-smoke surface; the installed-artifact E2E dogfood is #13492.
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_delivery_route import (
            provider_for_role,
        )

        agents = (agents_fn or _live_observed_agents)()
        return match_lane_worker_runtime(
            agents,
            workspace_id=workspace_id,
            lane_id=lane_id,
            provider=provider_for_role(expected_owner_role),
        )
    except Exception:  # noqa: BLE001 - an unreadable inventory is fail-closed no-edge
        return ""


__all__ = (
    "match_lane_worker_runtime",
    "lane_worker_runtime",
)
