"""herdr-native `workflow step` resolution adapter (Redmine #13489).

Bridges the impure runtime inputs a herdr session carries — launch-time sender env, the
repo anchor, the workspace registry, and the live ``herdr agent list`` inventory — into the
pure herdr classifier + resolver
(:mod:`...domain.workflow_step_herdr`). It is the herdr counterpart of the tmux wiring in
:func:`...application.cli_workflow.cmd_workflow_step` (``current_pane`` + tmux inventory ->
:func:`...domain.workflow_step.resolve_workflow_step`), and it produces the SAME replayable
:class:`~...domain.workflow_step.WorkflowStepOutcome` envelope so ``workflow step`` reads the
same under either backend.

Increment 1 (Redmine #13489 j#74685 design_boundary) is **resolution-only**: it resolves the
current lane's herdr-native identity + role and names the role-appropriate next action /
owner / herdr surface, failing closed on an unattested identity, an unknown provider, or a
gateway with no live same-lane worker. It performs no sublane lifecycle mutation and no
delivery — the policy-permitted one-step auto-execution of ``sublane create/start/dispatch``
(and the fail-closed destructive drain/retire boundary) is increment 2, gated behind the
mandatory task-level design mid-review.

Only the terminal-runtime provider seam (env, anchor, registry, inventory) is impure here;
the routing decision itself stays in the pure domain module. Inventory is read **only** for
a sublane gateway lane (to test its same-lane worker's liveness); the worker and coordinator
lanes resolve from env + registry alone, so a down herdr inventory never blocks them.
"""

from __future__ import annotations

import argparse
import os
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_LANE_UNRESOLVED,
    WorkflowStepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    HERDR_PROVIDER_CLAUDE,
    REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    classify_herdr_workflow_lane,
    resolve_herdr_workflow_step,
)


def _anchor_workspace_id(repo_root) -> Optional[str]:
    """The sender's own workspace segment for the anchor↔env gate (mirrors herdr_send_entry).

    Under the #13377 shared-project-workspace model a lane agent runs in a linked worktree
    whose segment resolves to the MAIN checkout's workspace identity — matching the
    ``MOZYO_WORKSPACE_ID=<project-ws>`` its launch injected; a standalone / main checkout
    resolves to its registry workspace_id. Legacy ``wt_<hash>`` lane attestation (pre-#13377
    lanes still live during the transition) is accepted exactly when the env carries the
    worktree's deterministically re-derived token, never an arbitrary env value.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        _norm,
        derive_lane_workspace_token,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
        MOZYO_WORKSPACE_ID_ENV,
    )

    anchor_ws = herdr_workspace_segment(repo_root) or None
    env_ws = _norm(os.environ.get(MOZYO_WORKSPACE_ID_ENV))
    if env_ws and env_ws != (anchor_ws or ""):
        try:
            legacy_token = derive_lane_workspace_token(str(repo_root))
        except (OSError, ValueError):
            legacy_token = ""
        if legacy_token and env_ws == legacy_token:
            anchor_ws = legacy_token
    return anchor_ws


def _project_scope_for(workspace_id: str) -> str:
    """The project scope for a workspace id (registry ``project_name``; ``""`` when absent).

    Distinguishes a default-lane codex coordinator (``project_gateway`` when a project scope
    resolves) from the department-root ``grandparent_coordinator``. Fail-safe to ``""`` — an
    unresolvable registry never fabricates a scope (the lane then classifies as grandparent,
    the more conservative coordinator role).
    """
    from mozyo_bridge.core.state.workspace_registry import load_workspace_by_id

    try:
        record = load_workspace_by_id(workspace_id)
    except Exception:  # noqa: BLE001 - a registry read must never break a live resolution
        return ""
    if record is None:
        return ""
    return (getattr(record, "project_name", "") or "").strip()


def _same_lane_worker_liveness(
    workspace_id: str, lane_id: str, *, env: Mapping[str, str]
) -> Optional[bool]:
    """Whether a live ``claude`` worker slot exists in the gateway's own lane unit.

    ``True`` / ``False`` from the live ``herdr agent list`` inventory; ``None`` when the
    inventory is unavailable (a down herdr — the gateway then blocks conservatively rather
    than claim a worker is present). Pure over the decode of each row's mzb1 name; the
    locator must be present for a slot to count as live.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
        WORKER_ROLE,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
        _agent_locator,
        _norm_lane,
        decode_assigned_name,
    )

    try:
        rows = list_herdr_agent_rows(env)
    except HerdrSessionStartError:
        return None
    want_lane = _norm_lane(lane_id)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id or identity.role != WORKER_ROLE:
            continue
        if _norm_lane(identity.lane_id) != want_lane:
            continue
        if _agent_locator(row):
            return True
    return False


def resolve_herdr_step_outcome(args: argparse.Namespace) -> WorkflowStepOutcome:
    """Resolve the herdr-native ``workflow step`` outcome for the current lane (Redmine #13489).

    Resolves the sender identity from launch env + the repo anchor (fail-closed on an
    unattested identity), derives the project scope from the workspace registry, classifies
    the workflow lane role, and — only for a sublane gateway lane — reads the live inventory
    for its same-lane worker's liveness, then delegates to the pure resolver. Never mutates a
    lane or delivers anything (increment 1 is resolution-only).
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
        resolve_sender_identity,
    )

    repo_root = repo_root_from_args(args)
    anchor_ws = _anchor_workspace_id(repo_root)
    sender_res = resolve_sender_identity(os.environ, anchor_workspace_id=anchor_ws)
    if not sender_res.ok or sender_res.identity is None:
        # An unattested herdr identity is not a workflow-step origin. Name the herdr-native
        # cause and the one sanctioned lane-dispatch route (mirrors herdr_send_entry).
        return WorkflowStepOutcome(
            state=STATE_LANE_UNRESOLVED,
            next_action=(
                "resolve the herdr lane identity before stepping: this shell carries no "
                "attested launch-time lane-sender identity (MOZYO_WORKSPACE_ID / "
                "MOZYO_AGENT_ROLE). Run workflow step from inside an attested herdr lane "
                "agent, or dispatch lanes through the coordinator (coordinator -> "
                "target-lane Codex gateway -> same-lane Claude worker). See "
                "vibes/docs/specs/herdr-native-identity.md."
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            repo_root=str(repo_root),
            detail=f"sender identity unresolved ({sender_res.reason}): {sender_res.detail}",
        )

    sender = sender_res.identity
    project_scope = ""
    if sender.role != HERDR_PROVIDER_CLAUDE:
        # Only a codex lane's coordinator/gateway classification consults the project scope;
        # a claude worker is a worker regardless, so skip the registry read for it.
        project_scope = _project_scope_for(sender.workspace_id)

    lane = classify_herdr_workflow_lane(
        provider=sender.role,
        lane_id=sender.lane_id,
        project_scope=project_scope,
        repo_root=str(repo_root),
    )

    same_lane_worker_live: Optional[bool] = None
    if lane.caller_role == ROLE_DELEGATED_COORDINATOR:
        same_lane_worker_live = _same_lane_worker_liveness(
            sender.workspace_id, sender.lane_id, env=os.environ
        )

    return resolve_herdr_workflow_step(lane, same_lane_worker_live=same_lane_worker_live)


__all__ = ("resolve_herdr_step_outcome",)
