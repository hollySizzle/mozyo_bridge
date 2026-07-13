"""Gateway-route enforcement gate wiring for ``orchestrate_handoff`` (Redmine #12918).

``orchestrate_handoff`` (``application/commands.py``) is an already-oversized
module under the module-health gate, so the gateway-route enforcement *gate* — the
glue that resolves the sender lane Unit, runs the pure
:func:`...domain.gateway_route_enforcement.decide_gateway_route` policy, and fails
closed / records the exception — lives here instead of growing that module with
more inline body. ``commands.py`` keeps only a single :func:`enforce_gateway_route`
call.

The pure decision + all prose live in the f_140 domain module; this is the only
side-effecting seam (it emits the structured outcome and may ``die``). The caller
passes its private ``_emit_outcome`` as ``emit`` so the blocked outcome is emitted
through the exact same record path as every other ``orchestrate_handoff`` gate.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Callable, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    current_pane_lane_unit,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
    load_workflow_binding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_route_enforcement import (
    GatewayRouteRequest,
    decide_gateway_route,
    render_block_die_message,
    render_exception_advisory,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    PROVIDER_CLAUDE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ROLE_IMPLEMENTER,
)
from mozyo_bridge.shared.errors import die


def _resolve_target_disposition(preflight_target: Any) -> Optional[str]:
    """The target lane's lifecycle disposition, or ``None`` (byte-invariant; #13681 W3).

    Fail-closed to ``None`` on every uncertainty — a target with no resolvable
    ``(workspace_id, lane_id)`` unit, an unreadable store, or a lane with no lifecycle
    row (owner-unbound). ``None`` and ``active`` both leave the gate byte-identical to
    its pre-#13681 behaviour; only a resolved non-active disposition zero-sends. The
    lookup keys on the SAME ``(project workspace segment, lane_label)`` unit the create
    (W1) and supersede (W2) writes use — the mzb1 decode that populates
    ``preflight_target`` yields exactly that project workspace segment and lane label.
    """
    workspace = getattr(preflight_target, "workspace_id", None)
    lane = getattr(preflight_target, "lane_id", None)
    if not workspace or not lane:
        return None
    try:
        from mozyo_bridge.core.state.lane_lifecycle import (
            LaneLifecycleError,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        record = LaneLifecycleStore().get(
            LaneLifecycleKey(str(workspace), str(lane))
        )
    except (LaneLifecycleError, OSError, ValueError):
        return None
    return record.lane_disposition if record is not None else None


def enforce_gateway_route(
    args: argparse.Namespace,
    *,
    kind: str | None,
    receiver: str,
    preflight_target: Any,
    source: str | None,
    mode: str | None,
    anchor: Any,
    target: str | None,
    record_format: str,
    record_command: str | None,
    emit: Callable[..., None],
    sender_lane_unit: Optional[Tuple[Optional[str], Optional[str]]] = None,
) -> None:
    """Apply the #12918 gateway-route gate; fail closed on a cross-lane worker send.

    Resolves the sender's lane Unit, runs the pure policy, and on a block emits the
    structured ``gateway_route_blocked`` outcome through ``emit`` and ``die``s before
    any text is typed. On an explicit durable exception it prints the advisory to
    stderr and returns so the send proceeds. Returns ``None`` (and raises nothing) for
    an allowed route.

    Sender-lane resolution (Redmine #13261 increment 4): by default the Unit comes
    from the live tmux inventory (``current_pane_lane_unit()`` — ``TMUX_PANE``; a
    sender the inventory does not carry leaves the gate skipped, mirroring the
    cross-session gate). Under the herdr backend there is no tmux to read: the caller
    passes the env-derived ``sender_lane_unit`` (from the launch-time
    ``MOZYO_WORKSPACE_ID`` / ``MOZYO_LANE_ID`` SenderIdentity), so the gate **enforces
    on the env sender lane and makes zero tmux calls**. The tmux path is byte-identical
    when ``sender_lane_unit`` is ``None``.
    """
    if sender_lane_unit is not None:
        sender_ws, sender_lane = sender_lane_unit
    else:
        sender_ws, sender_lane = current_pane_lane_unit()
    # Role-based worker discrimination (Redmine #13174): resolve the implementer
    # (worker) role's runtime provider from the repo-local binding (#12673/#13157;
    # default -> claude, byte-identical) so the authority decision keys on the role,
    # not the literal `claude` receiver token. A broken config fails closed through
    # the loader's RepoLocalConfigError.
    binding, _warnings = load_workflow_binding()
    worker_provider = binding.provider_for(ROLE_IMPLEMENTER) or PROVIDER_CLAUDE
    decision = decide_gateway_route(
        GatewayRouteRequest(
            kind=kind,
            receiver=receiver,
            sender_identity_known=sender_lane is not None,
            sender_workspace_id=sender_ws,
            sender_lane_id=sender_lane,
            target_workspace_id=preflight_target.workspace_id,
            target_lane_id=preflight_target.lane_id,
            target_role=preflight_target.role,
            allow_direct_worker=bool(getattr(args, "allow_direct_worker", False)),
            worker_provider=worker_provider,
            # Redmine #13681 W3: zero-send a governed delivery to a lane the lifecycle
            # authority marks non-active (superseded / hibernated / retired). Resolved
            # fail-closed to None (byte-invariant) when no disposition is knowable.
            target_lane_disposition=_resolve_target_disposition(preflight_target),
        )
    )
    if decision.is_blocked:
        emit(
            make_outcome(
                status="blocked",
                reason="gateway_route_blocked",
                receiver=receiver,
                target=target,
                anchor=anchor,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
            ),
            record_format=record_format,
            command=record_command,
        )
        die(render_block_die_message(decision, preflight_target.lane_id))
        raise AssertionError("unreachable")
    if decision.is_exception:
        print(render_exception_advisory(decision, preflight_target.lane_id), file=sys.stderr)


__all__ = ("enforce_gateway_route",)
