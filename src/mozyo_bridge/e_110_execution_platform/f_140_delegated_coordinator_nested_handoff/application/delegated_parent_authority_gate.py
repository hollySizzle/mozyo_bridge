"""Live composition of the delegated_coordinator parent-authority admission (#15146).

The application half of :mod:`...domain.delegated_parent_authority`: it reads the
repo-local role-binding declaration and the lane-lifecycle owner rows, and hands the
pure decision everything it needs. Called from BOTH ``sublane create`` entry points —
the plan-only surface and the ``--dry-run`` / ``--execute`` actuator — with the same
inputs, because a plan that says "fine" while execute refuses is exactly the
plan/execute drift #14224 was filed over.

Every read failure verifies nothing: an unresolvable workspace scope or an unreadable
lifecycle store refuses (typed), never waves the geometry through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_parent_authority import (  # noqa: E501
    DELEGATED_COORDINATOR_LANE_KIND,
    PARENT_SCOPE_UNRESOLVED,
    ParentAuthorityVerdict,
    decide_delegated_parent_authority,
    parent_authority_refusal_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
    load_parsed_role_bindings,
)

__all__ = (
    "delegated_parent_authority_refusal",
)


def delegated_parent_authority_refusal(
    repo_root: Path, lane_kind: str
) -> Optional[str]:
    """The refusal text for creating ``lane_kind`` at ``repo_root``, or ``None``.

    ``None`` for every lane kind other than ``delegated_coordinator`` — the
    admission is the geometry assertion's own cost, and no other kind asserts a
    parent — and for a delegated_coordinator whose parent gateway is both declared
    and verified.
    """
    if (lane_kind or "").strip() != DELEGATED_COORDINATOR_LANE_KIND:
        return None
    verdict = _decide(repo_root)
    if verdict.ok:
        return None
    return parent_authority_refusal_text(verdict)


def _decide(repo_root: Path) -> ParentAuthorityVerdict:
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )

    parsed = load_parsed_role_bindings(repo_root)
    scope = ""
    try:
        scope = repo_scope_workspace_id(Path(repo_root)) or ""
    except Exception:  # noqa: BLE001 - unresolvable scope verifies nothing
        scope = ""
    if not scope:
        return ParentAuthorityVerdict(
            False,
            reason=PARENT_SCOPE_UNRESOLVED,
            detail=(
                "this repo's workspace identity did not resolve, so the parent "
                "gateway's owner row cannot be scoped or verified"
            ),
        )

    def owner_row_active(lane_id: str) -> bool:
        try:
            from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
            from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey

            record = LaneLifecycleStore().get(LaneLifecycleKey(scope, lane_id))
        except Exception:  # noqa: BLE001 - an unreadable authority verifies nothing
            return False
        return record is not None and record.lane_disposition == "active"

    return decide_delegated_parent_authority(parsed, owner_row_active=owner_row_active)
