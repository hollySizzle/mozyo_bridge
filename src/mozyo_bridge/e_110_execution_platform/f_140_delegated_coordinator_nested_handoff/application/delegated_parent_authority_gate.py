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
    CoordinatorParentProbe,
    ParentAuthorityVerdict,
    decide_delegated_parent_authority,
    parent_authority_refusal_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
    load_parsed_role_bindings,
)

__all__ = (
    "delegated_parent_authority_refusal",
    "delegated_parent_authority_verdict",
)


def delegated_parent_authority_verdict(
    repo_root: Path, lane_kind: str
) -> Optional[ParentAuthorityVerdict]:
    """The full admission verdict for creating ``lane_kind`` at ``repo_root``.

    ``None`` for every lane kind other than ``delegated_coordinator`` — the
    admission is the geometry assertion's own cost, and no other kind asserts a
    parent. For a delegated_coordinator the verdict carries ``parent_kind``
    (which branch decided; #15700) even on success, so the caller can surface
    the taken branch instead of admitting silently.
    """
    if (lane_kind or "").strip() != DELEGATED_COORDINATOR_LANE_KIND:
        return None
    return _decide(repo_root)


def delegated_parent_authority_refusal(
    repo_root: Path, lane_kind: str
) -> Optional[str]:
    """The refusal text for creating ``lane_kind`` at ``repo_root``, or ``None``.

    ``None`` for every lane kind other than ``delegated_coordinator`` — and for a
    delegated_coordinator whose parent verified (a declared + verified project
    gateway, or — with no gateway declared at all — the workspace's live attested
    default-lane coordinator, #15700).
    """
    verdict = delegated_parent_authority_verdict(repo_root, lane_kind)
    if verdict is None or verdict.ok:
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

    def coordinator_parent_probe() -> CoordinatorParentProbe:
        # The single-workspace branch's reads (#15700). Reuses the coordinator
        # proxy rail's own resolvers — the same exactly-one live-attested policy —
        # rather than a second implementation that could drift from it. Any read
        # failure folds to a blocked probe: an unreadable parent verifies nothing.
        try:
            import os

            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
                live_agent_rows,
                live_workspace_id,
                resolve_default_lane_authority,
                resolve_expected_provider,
                resolve_proxy_target,
            )
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
                AUTHORITY_RESOLVED,
                TARGET_MISSING,
            )

            status, role, _scope, reason = resolve_default_lane_authority(repo_root)
            provider = (
                resolve_expected_provider(repo_root, role)
                if status == AUTHORITY_RESOLVED and role
                else ""
            )
            live_status = ""
            live_detail = ""
            verified_target = ""
            if status == AUTHORITY_RESOLVED and provider:
                workspace_id = live_workspace_id(repo_root)
                if not workspace_id:
                    live_status = TARGET_MISSING
                    live_detail = (
                        "the herdr workspace anchor did not resolve, so no live "
                        "default-lane coordinator can be found"
                    )
                else:
                    target = resolve_proxy_target(
                        live_agent_rows(os.environ),
                        workspace_id=workspace_id,
                        provider=provider,
                    )
                    live_status = target.status
                    verified_target = target.assigned_name
                    live_detail = (
                        f"live={target.live} with_locator={target.with_locator}"
                        + (
                            f" attestation={target.attestation_state}"
                            f" ({target.attestation_reason})"
                            if target.attestation_state
                            else ""
                        )
                    )
            return CoordinatorParentProbe(
                authority_status=status,
                authority_reason=reason,
                role=role,
                provider=provider,
                live_status=live_status,
                live_detail=live_detail,
                verified_target=verified_target,
            )
        except Exception:  # noqa: BLE001 - an unreadable parent verifies nothing
            return CoordinatorParentProbe(
                authority_status="blocked",
                authority_reason="coordinator_parent_probe_unavailable",
            )

    def owner_row_active(lane_id: str, project_scope: str) -> bool:
        try:
            from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
            from mozyo_bridge.core.state.lane_lifecycle_model import (
                BINDING_KIND_PROJECT_GATEWAY,
                LaneLifecycleKey,
                stored_binding_kind_is,
            )

            record = LaneLifecycleStore().get(LaneLifecycleKey(scope, lane_id))
        except Exception:  # noqa: BLE001 - an unreadable authority verifies nothing
            return False
        if record is None or record.lane_disposition != "active":
            return False
        # The CANONICAL owner row, not merely an active occupant of the derived
        # key (review j#106254 finding_parentownerrowtype): only what
        # `sublane declare-project-gateway` writes — binding_kind
        # project_gateway, byte-exact via the model's own comparator, carrying
        # this binding's canonical scope — verifies the parent. A stale or
        # foreign issue-kind row at the same key verifies nothing.
        if not stored_binding_kind_is(
            getattr(record, "binding_kind", None), BINDING_KIND_PROJECT_GATEWAY
        ):
            return False
        return str(getattr(record, "project_scope", "") or "") == project_scope

    return decide_delegated_parent_authority(
        parsed,
        owner_row_active=owner_row_active,
        coordinator_parent_probe=coordinator_parent_probe,
    )
