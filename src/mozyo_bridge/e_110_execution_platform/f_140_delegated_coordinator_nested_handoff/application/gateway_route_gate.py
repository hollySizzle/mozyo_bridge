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
from typing import Any, Callable

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    current_pane_lane_unit,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_route_enforcement import (
    GatewayRouteRequest,
    decide_gateway_route,
    render_block_die_message,
    render_exception_advisory,
)
from mozyo_bridge.shared.errors import die


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
) -> None:
    """Apply the #12918 gateway-route gate; fail closed on a cross-lane worker send.

    Resolves the sender's lane Unit from the live inventory (``TMUX_PANE``; a sender
    the inventory does not carry leaves the gate skipped, mirroring the
    cross-session gate's skip when the sender session is unknown), runs the pure
    policy, and on a block emits the structured ``gateway_route_blocked`` outcome
    through ``emit`` and ``die``s before any text is typed. On an explicit durable
    exception it prints the advisory to stderr and returns so the send proceeds.
    Returns ``None`` (and raises nothing) for an allowed route.
    """
    sender_ws, sender_lane = current_pane_lane_unit()
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
