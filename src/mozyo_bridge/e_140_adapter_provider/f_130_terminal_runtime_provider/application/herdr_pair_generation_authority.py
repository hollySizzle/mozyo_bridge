"""Fresh terminal-generation authority for one managed pair geometry effect."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import (
    HerdrNativeIdentityBindingStore,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_PROVIDERS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_terminal_identity import (  # noqa: E501
    terminal_identity_of_live_slot,
    terminal_identity_snapshot_complete,
)


PairGenerationFingerprint = tuple[tuple[str, str, str, str, str, str], ...]


def pair_generation_fingerprint(
    locators: Sequence[str],
    *,
    expected_workspace_id: str,
    expected_lane_id: str,
    expected_anchor_assigned_name: str,
    expected_anchor_provider: str,
    expected_anchor_locator: str,
    expected_anchor_terminal_id: str,
    expected_anchor_action_id: str,
    home: Path,
    binary: str,
    runner,
    timeout: float,
) -> Optional[PairGenerationFingerprint]:
    """Join an exact pair to one fresh canonical inventory and current authority.

    The returned tuple contains no raw terminal identity.  It is suitable only for
    an in-memory equality check immediately before/after a geometry effect.
    """

    wanted = tuple(locators)
    expected = (
        expected_workspace_id,
        expected_lane_id,
        expected_anchor_assigned_name,
        expected_anchor_provider,
        expected_anchor_locator,
        expected_anchor_terminal_id,
        expected_anchor_action_id,
    )
    if (
        len(wanted) != 2
        or len(set(wanted)) != 2
        or any(type(value) is not str or not value for value in expected)
        or expected_anchor_locator not in wanted
    ):
        return None
    try:
        rows: Sequence[Mapping[str, object]] = tuple(
            _list_rows(
                binary,
                runner,
                timeout,
                binding_store=HerdrNativeIdentityBindingStore(home=Path(home)),
            )
        )
    except Exception:  # noqa: BLE001 - unreadable inventory is never authority
        return None
    if not terminal_identity_snapshot_complete(rows):
        return None
    attestation_store = HerdrIdentityAttestationStore(home=Path(home))

    entries: list[tuple[str, str, str, str, str, str]] = []
    pair_axes: list[tuple[str, str]] = []
    for locator in wanted:
        matches = [row for row in rows if _agent_locator(row) == locator]
        if len(matches) != 1:
            return None
        raw_name = matches[0].get(AGENT_KEY_NAME)
        if type(raw_name) is not str:
            return None
        decoded = decode_assigned_name(raw_name)
        if not decoded.ok or decoded.identity is None:
            return None
        identity = decoded.identity
        if identity.assigned_name != raw_name:
            return None
        terminal_id = terminal_identity_of_live_slot(raw_name, locator, rows)
        if terminal_id is None:
            return None
        try:
            attestation = attestation_store.read(raw_name)
        except Exception:  # noqa: BLE001 - unreadable attestation is non-authority
            return None
        if not evaluate_attestation(
            attestation,
            live_locator=locator,
            live_terminal_id=terminal_id,
            expected_workspace_id=identity.workspace_id,
            expected_role=identity.role,
            expected_lane=identity.lane_id,
        ).ok:
            return None
        token = verified_terminal_generation_token(
            home,
            assigned_name=raw_name,
            workspace_id=identity.workspace_id,
            role=identity.role,
            lane_id=identity.lane_id,
            locator=locator,
            terminal_id=terminal_id,
        )
        if not token:
            return None
        if locator == expected_anchor_locator and (
            identity.workspace_id != expected_workspace_id
            or identity.lane_id != expected_lane_id
            or identity.role != expected_anchor_provider
            or raw_name != expected_anchor_assigned_name
            or terminal_id != expected_anchor_terminal_id
            or token != expected_anchor_action_id
        ):
            return None
        pair_axes.append((identity.workspace_id, identity.lane_id))
        entries.append(
            (
                identity.workspace_id,
                identity.lane_id,
                identity.role,
                raw_name,
                locator,
                token,
            )
        )
    if (
        set(pair_axes) != {(expected_workspace_id, expected_lane_id)}
        or {entry[2] for entry in entries} != set(LANE_PLACEMENT_PROVIDERS)
    ):
        return None
    return tuple(entries)


__all__ = ("PairGenerationFingerprint", "pair_generation_fingerprint")
