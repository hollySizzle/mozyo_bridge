"""Pure live-lane slot resolution shared by herdr actuator read and heal paths."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    GATEWAY_ROLE,
    WORKER_ROLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _tab_id_of_row,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm_lane,
    decode_assigned_name,
    derive_lane_workspace_token,
)


def pair_colocation(
    slots: Mapping[str, tuple[str, str]],
    pair: tuple[str, str] | None = None,
) -> Optional[bool]:
    """Whether both managed slots share one live placement container (#13705)."""
    gateway_role, worker_role = (
        pair if pair is not None else (GATEWAY_ROLE, WORKER_ROLE)
    )
    gateway = slots.get(gateway_role)
    worker = slots.get(worker_role)
    if gateway is None or worker is None:
        return None
    return gateway[1] == worker[1]


def lane_slots(
    workspace_id: str,
    lane_id: str,
    rows: Sequence[Mapping[str, object]],
    managed: tuple[str, ...] | None = None,
) -> dict[str, tuple[str, str]]:
    """Map managed role to its exact live locator and placement key."""
    managed_pair = (
        managed if managed is not None else (GATEWAY_ROLE, WORKER_ROLE)
    )
    want_lane = _norm_lane(lane_id)
    slots: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            continue
        identity = decoded.identity
        if (
            identity.workspace_id != workspace_id
            or _norm_lane(identity.lane_id) != want_lane
            or identity.role not in managed_pair
        ):
            continue
        locator = _agent_locator(row)
        if locator and identity.role not in slots:
            slots[identity.role] = (
                locator,
                (_workspace_prefix(locator), _tab_id_of_row(row)),
            )
    return slots


def resolve_lane_slots(
    worktree_path: str,
    lane_label: str,
    rows: Sequence[Mapping[str, object]],
    managed: tuple[str, ...] | None = None,
) -> tuple[str, str, dict[str, tuple[str, str]]]:
    """Resolve shared-model first, then the legacy worktree/default-lane unit."""
    try:
        resolved = Path(worktree_path).expanduser().resolve()
        workspace_id = herdr_workspace_segment(resolved)
    except (OSError, ValueError):
        return "", "", {}
    lane_id = _norm_lane(lane_label)
    slots = lane_slots(workspace_id, lane_id, rows, managed) if workspace_id else {}
    if not slots:
        legacy_workspace = derive_lane_workspace_token(str(resolved))
        legacy_slots = lane_slots(legacy_workspace, DEFAULT_LANE, rows, managed)
        if legacy_slots:
            workspace_id, lane_id, slots = (
                legacy_workspace,
                DEFAULT_LANE,
                legacy_slots,
            )
    return workspace_id, lane_id, slots
