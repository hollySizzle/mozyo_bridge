"""One fresh-generation authority join shared by destructive Herdr close rails (#15227)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_PRESENT,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationStore,
    completed_generation_startup_token,
    verified_generation_token,
)
from mozyo_bridge.core.state.lane_lifecycle_model import ReleasePin
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_terminal_identity import (
    terminal_identity_of_live_slot,
    terminal_identity_of_row,
    terminal_identity_snapshot_complete,
)


def current_generation_release_pin(
    rows: Sequence[Mapping[str, object]],
    *,
    home: Path | None,
    workspace_id: str,
    lane_id: str,
    role: str,
    assigned_name: str,
    locator: str,
) -> Optional[ReleasePin]:
    """Return a nonsecret v2 close pin iff all current-generation proofs agree."""
    snapshot = tuple(rows)
    if not terminal_identity_snapshot_complete(snapshot):
        return None
    terminal_id = terminal_identity_of_live_slot(assigned_name, locator, snapshot)
    if terminal_id is None:
        return None
    try:
        record = HerdrIdentityAttestationStore(home=home).read(_norm(assigned_name))
    except Exception:  # noqa: BLE001 - unreadable authority is non-current
        return None
    if not evaluate_attestation(
        record,
        live_locator=locator,
        expected_workspace_id=workspace_id,
        expected_role=role,
        expected_lane=lane_id,
        live_terminal_id=terminal_id,
    ).ok:
        return None
    token = verified_generation_token(
        home,
        assigned_name=assigned_name,
        workspace_id=workspace_id,
        role=role,
        lane_id=lane_id,
        locator=locator,
        live_terminal_id=terminal_id,
        norm=_norm,
        norm_lane=_norm_lane,
    )
    if not token:
        return None
    return ReleasePin(
        role=role,
        assigned_name=assigned_name,
        locator=locator,
        startup_action_id=token,
    )


def current_generation_release_pins(
    rows: Sequence[Mapping[str, object]],
    *,
    home: Path | None,
    workspace_id: str,
    lane_id: str,
    targets: Sequence[tuple[str, str, str]],
) -> Optional[tuple[ReleasePin, ...]]:
    """Join a whole close batch to one immutable full inventory snapshot."""
    snapshot = tuple(rows)
    if not terminal_identity_snapshot_complete(snapshot):
        return None
    pins: list[ReleasePin] = []
    for role, assigned_name, locator in targets:
        pin = current_generation_release_pin(
            snapshot,
            home=home,
            workspace_id=workspace_id,
            lane_id=lane_id,
            role=role,
            assigned_name=assigned_name,
            locator=locator,
        )
        if pin is None:
            return None
        pins.append(pin)
    return tuple(pins)


def pinned_generations_are_current(
    pins: Sequence[ReleasePin],
    rows: Sequence[Mapping[str, object]],
    *,
    home: Path | None,
    workspace_id: str,
    lane_id: str,
) -> bool:
    """Rejoin durable v2 pins at the close edge; legacy/tokenless pins never match."""
    wanted = tuple(pins)
    if not wanted or any(not pin.current_generation_bound for pin in wanted):
        return False
    current = current_generation_release_pins(
        rows,
        home=home,
        workspace_id=workspace_id,
        lane_id=lane_id,
        targets=tuple((pin.role, pin.assigned_name, pin.locator) for pin in wanted),
    )
    return current == wanted


def pinned_generation_partition(
    pins: Sequence[ReleasePin],
    rows: Sequence[Mapping[str, object]],
    *,
    home: Path | None,
    workspace_id: str,
    lane_id: str,
) -> Optional[tuple[tuple[ReleasePin, ...], tuple[ReleasePin, ...]]]:
    """Classify v2 pins as exact-live or positively absent; any recycle is unknown."""
    snapshot = tuple(rows)
    wanted = tuple(pins)
    roles = tuple(pin.role for pin in wanted)
    names = tuple(pin.assigned_name for pin in wanted)
    locators = tuple(pin.locator for pin in wanted)
    if (
        not wanted
        or any(not pin.current_generation_bound for pin in wanted)
        or len(set(roles)) != len(roles)
        or len(set(names)) != len(names)
        or len(set(locators)) != len(locators)
        or any(
            pin.assigned_name
            != encode_assigned_name(workspace_id, pin.role, lane_id)
            for pin in wanted
        )
        or not terminal_identity_snapshot_complete(snapshot)
    ):
        return None
    live: list[ReleasePin] = []
    absent: list[ReleasePin] = []
    for pin in wanted:
        named = [row for row in snapshot if row.get(AGENT_KEY_NAME) == pin.assigned_name]
        located = [row for row in snapshot if _agent_locator(row) == pin.locator]
        if not named and not located:
            try:
                generation = HerdrLaunchGenerationStore(home=home).read(pin.assigned_name)
            except Exception:  # noqa: BLE001 - absence requires readable durable identity
                return None
            terminal_id = getattr(generation, "terminal_id", "")
            if (
                generation is None
                or generation.startup_action_id != pin.startup_action_id
                or generation.assigned_name != pin.assigned_name
                or generation.workspace_id != workspace_id
                or generation.role != pin.role
                or _norm_lane(generation.lane_id) != _norm_lane(lane_id)
                or generation.locator != pin.locator
                or generation.verdict != VERDICT_PRESENT
                or completed_generation_startup_token(
                    home, generation, norm=_norm, norm_lane=_norm_lane
                )
                != pin.startup_action_id
                or type(terminal_id) is not str
                or not terminal_id
                or terminal_id.strip() != terminal_id
                or any(
                    terminal_identity_of_row(row) == terminal_id
                    for row in snapshot
                )
            ):
                return None
            absent.append(pin)
            continue
        if len(named) != 1 or len(located) != 1 or named[0] is not located[0]:
            return None
        current = current_generation_release_pin(
            snapshot,
            home=home,
            workspace_id=workspace_id,
            lane_id=lane_id,
            role=pin.role,
            assigned_name=pin.assigned_name,
            locator=pin.locator,
        )
        if current != pin:
            return None
        live.append(pin)
    return tuple(live), tuple(absent)


def pinned_generations_absent(
    pins: Sequence[ReleasePin], rows: Sequence[Mapping[str, object]], *,
    home: Path | None, workspace_id: str, lane_id: str,
) -> bool:
    """Require terminal-bound positive absence for every pin in one full snapshot."""
    wanted = tuple(pins)
    partition = pinned_generation_partition(
        wanted, rows, home=home, workspace_id=workspace_id, lane_id=lane_id
    )
    return partition is not None and not partition[0] and partition[1] == wanted


__all__ = (
    "current_generation_release_pin",
    "current_generation_release_pins",
    "pinned_generations_are_current",
    "pinned_generation_partition",
    "pinned_generations_absent",
)
