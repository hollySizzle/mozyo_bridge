"""Capture and recheck non-destructive legacy restore absence authority (#15227)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_inventory_identity import (
    terminal_inventory_complete,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_ATTESTED,
    GENERATION_PENDING,
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    StartupTransactionFence,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_legacy_absence_authority import (  # noqa: E501
    OfflineRolloutLegacyAbsenceAuthority,
    OfflineRolloutLegacyAbsencePin,
    decode_legacy_absence_authority,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_restore_intent import (  # noqa: E501
    OfflineRolloutRestoreIntent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_inventory import (  # noqa: E501
    agent_pane_rows_exact,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    parse_pane_bound_receipt,
)


def _ok(**receipt: object) -> PhaseExecutionResult:
    return PhaseExecutionResult(True, receipt=receipt)


def _fail(reason: str, detail: str = "") -> PhaseExecutionResult:
    return PhaseExecutionResult(False, reason=reason, detail=detail[:1000])


def _completed_receipt(
    *,
    home: Path,
    pin: OfflineRolloutLegacyAbsencePin,
    expected_providers: tuple[str, ...],
):
    """Read the exact historical pair action and this pin's terminal-bound receipt."""

    action = StartupTransactionFence(home=home).read(pin.startup_action_id)
    participant = action.participant_for(pin.provider) if action is not None else None
    receipt = parse_pane_bound_receipt(
        getattr(participant, "receipt", "") if participant is not None else ""
    )
    unit = getattr(action, "unit", None)
    participants = tuple(getattr(action, "participants", ()) or ())
    if not (
        action is not None
        and getattr(action, "phase", "") == PHASE_COMPLETED_SUCCESS
        and unit is not None
        and getattr(unit, "workspace_id", "") == pin.workspace_id
        and getattr(unit, "lane_id", "") == pin.lane_id
        and tuple(getattr(unit, "providers", ()) or ())
        == tuple(sorted(expected_providers))
        and len(participants) == len(expected_providers)
        and {getattr(row, "role", "") for row in participants}
        == set(expected_providers)
        and participant is not None
        and getattr(participant, "closed", True) is False
        and getattr(participant, "assigned_name", "") == pin.assigned_name
        and getattr(participant, "locator", "") == pin.old_locator
        and receipt is not None
        and receipt.native_name == native_name_for(pin.assigned_name)
        and bool(receipt.terminal_id)
    ):
        return None
    return receipt


def _attestation_exact(*, home: Path, pin, terminal_id: str) -> bool:
    record = HerdrIdentityAttestationStore(home=home).read(pin.assigned_name)
    joined = evaluate_attestation(
        record,
        live_locator=pin.old_locator,
        live_terminal_id=terminal_id,
        expected_workspace_id=pin.workspace_id,
        expected_role=pin.provider,
        expected_lane=pin.lane_id,
    )
    return bool(
        record is not None
        and type(getattr(record, "schema_version", None)) is int
        and record.schema_version == HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
        and joined.ok
    )


def _current_generation_exact(
    *, home: Path, pin: OfflineRolloutLegacyAbsencePin, terminal_id: str
) -> bool:
    generation = HerdrLaunchGenerationStore(home=home).read(pin.assigned_name)
    return bool(
        generation is not None
        and getattr(generation, "phase", "") == GENERATION_ATTESTED
        and getattr(generation, "startup_action_id", "") == pin.startup_action_id
        and getattr(generation, "workspace_id", "") == pin.workspace_id
        and getattr(generation, "role", "") == pin.provider
        and getattr(generation, "lane_id", "") == pin.lane_id
        and getattr(generation, "locator", "") == pin.old_locator
        and getattr(generation, "terminal_id", "") == terminal_id
        and verified_terminal_generation_token(
            home,
            assigned_name=pin.assigned_name,
            workspace_id=pin.workspace_id,
            role=pin.provider,
            lane_id=pin.lane_id,
            locator=pin.old_locator,
            terminal_id=terminal_id,
        )
        == pin.startup_action_id
    )


def legacy_pin_positive_absence(
    *,
    home: Path,
    view,
    pane_rows,
    pin: OfflineRolloutLegacyAbsencePin,
    expected_providers: tuple[str, ...],
    require_current_generation: bool,
    require_assigned_name_absent: bool = True,
    allowed_replacement_action_id: str = "",
) -> bool:
    """Rejoin one legacy pin and prove its old name/locator/terminal stay absent."""

    try:
        receipt = _completed_receipt(
            home=home, pin=pin, expected_providers=expected_providers
        )
        if receipt is None:
            return False
        generation = HerdrLaunchGenerationStore(home=home).read(
            pin.assigned_name
        )
        current_exact = _current_generation_exact(
            home=home, pin=pin, terminal_id=receipt.terminal_id
        )
        replacement_exact = bool(
            generation is not None
            and allowed_replacement_action_id
            and getattr(generation, "startup_action_id", "")
            == allowed_replacement_action_id
            and getattr(generation, "phase", "")
            in {GENERATION_PENDING, GENERATION_ATTESTED}
            and getattr(generation, "workspace_id", "") == pin.workspace_id
            and getattr(generation, "role", "") == pin.provider
            and getattr(generation, "lane_id", "") == pin.lane_id
            and getattr(generation, "locator", "") != pin.old_locator
            and getattr(generation, "terminal_id", "") != receipt.terminal_id
        )
        if not current_exact and (
            require_current_generation or not replacement_exact
        ):
            return False
        # A restored process overwrites the per-name attestation.  Only after the
        # generation rebuild may its immutable completed receipt replace that projection;
        # the old locator and terminal must still be absent from the same live snapshot.
        attestation_exact = _attestation_exact(
            home=home, pin=pin, terminal_id=receipt.terminal_id
        )
        if not attestation_exact and (
            require_current_generation or require_assigned_name_absent
        ):
            return False
        return bool(
            not any(
                (require_assigned_name_absent and agent.name == pin.assigned_name)
                or agent.locator == pin.old_locator
                or agent.terminal_id == receipt.terminal_id
                for agent in view.agents
            )
            and not any(
                row.get("pane_id") == pin.old_locator
                or row.get("terminal_id") == receipt.terminal_id
                for row in pane_rows
            )
        )
    except Exception:  # noqa: BLE001 - every evidence read is fail-closed
        return False


def capture_legacy_absence_authority(
    *,
    home: Path,
    plan: Mapping[str, object],
    restore_intent: OfflineRolloutRestoreIntent,
    view,
    pane_rows,
) -> PhaseExecutionResult:
    """Capture pins for exactly restore identities absent from the live plan roster."""

    if (
        not terminal_inventory_complete(view)
        or tuple(view.unmanaged_agents)
        or not agent_pane_rows_exact(view.agents, pane_rows)
    ):
        return _fail("legacy_absence_inventory_unreadable")
    live_names = {
        row.get("assigned_name")
        for row in plan.get("agents", ())
        if isinstance(row, Mapping)
    }
    expected = [
        (group, agent)
        for group in restore_intent.groups
        for agent in group.agents
        if agent.assigned_name not in live_names
    ]
    pins = []
    for group, agent in sorted(expected, key=lambda row: row[1].assigned_name):
        try:
            generation = HerdrLaunchGenerationStore(home=home).read(
                agent.assigned_name
            )
            pin = OfflineRolloutLegacyAbsencePin(
                workspace_id=agent.workspace_id,
                lane_id=agent.lane_id,
                provider=agent.provider,
                assigned_name=agent.assigned_name,
                old_locator=getattr(generation, "locator", ""),
                startup_action_id=getattr(generation, "startup_action_id", ""),
            )
        except Exception:  # noqa: BLE001 - missing/malformed evidence is typed refusal
            return _fail("legacy_absence_authority_unverified", agent.assigned_name)
        if not legacy_pin_positive_absence(
            home=home,
            view=view,
            pane_rows=pane_rows,
            pin=pin,
            expected_providers=group.providers,
            require_current_generation=True,
        ):
            return _fail("legacy_absence_authority_unverified", agent.assigned_name)
        pins.append(pin)
    authority = OfflineRolloutLegacyAbsenceAuthority(tuple(pins))
    try:
        decode_legacy_absence_authority(
            {
                "restore_intent": restore_intent.as_payload(),
                "legacy_absence_authority": authority.as_payload(),
            },
            plan=plan,
            restore_intent=restore_intent,
        )
    except Exception:  # noqa: BLE001 - closed decoder is the final capture boundary
        return _fail("legacy_absence_authority_unverified")
    return _ok(legacy_absence_authority=authority.as_payload())


__all__ = (
    "capture_legacy_absence_authority",
    "legacy_pin_positive_absence",
)
