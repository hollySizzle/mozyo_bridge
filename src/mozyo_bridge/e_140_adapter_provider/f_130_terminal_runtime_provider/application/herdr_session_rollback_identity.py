"""Terminal-bound identity proofs for public startup rollback (#15227)."""

from __future__ import annotations

from typing import Mapping, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_PRESENT,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_ATTESTED,
    GENERATION_PENDING,
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_contract import (
    ParticipantVerdict,
    PreparedPaneObservation,
    ROLLBACK_PREPARED_TERMINAL_MISMATCH,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (
    parse_pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    terminal_identity_of_live_slot,
    terminal_identity_of_row,
    terminal_identity_snapshot_complete,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_rollback import (
    ROLLBACK_ABSENT,
    ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE,
    ROLLBACK_DETAIL,
    ROLLBACK_ELIGIBLE,
    ROLLBACK_INVENTORY_UNREADABLE,
    ROLLBACK_OBLIGATION_UNREADABLE,
    ROLLBACK_WORK_OBLIGATION,
)

PREPARED_PANE_PRESENT = "present"
PREPARED_PANE_ABSENT = "absent"
PREPARED_PANE_UNREADABLE = "unreadable"
ROLLBACK_PREPARED_PANE_UNVERIFIABLE = "prepared_pane_unverifiable"
ROLLBACK_PREPARED_RECEIPT_INVALID = "prepared_pane_receipt_invalid"
ROLLBACK_PREPARED_NATIVE_MISMATCH = "prepared_pane_native_identity_mismatch"


def terminal_bound_action_target(store_home, action, participant, rows, locator) -> bool:
    """Join one live slot to this exact startup action without exposing its terminal id."""
    live_terminal_id = terminal_identity_of_live_slot(
        participant.assigned_name, locator, rows
    )
    try:
        receipt = parse_pane_bound_receipt(participant.receipt)
        attestation = HerdrIdentityAttestationStore(home=store_home).read(
            participant.assigned_name
        )
        generation = HerdrLaunchGenerationStore(home=store_home).read(
            participant.assigned_name
        )
    except Exception:  # noqa: BLE001
        return False
    attested = evaluate_attestation(
        attestation,
        live_locator=locator,
        live_terminal_id=live_terminal_id,
        expected_workspace_id=action.unit.workspace_id,
        expected_role=participant.role,
        expected_lane=action.unit.lane_id,
    )
    return bool(
        receipt is not None
        and receipt.native_name == native_name_for(participant.assigned_name)
        and receipt.terminal_id == live_terminal_id
        and attested.ok
        and generation is not None
        and _norm(getattr(generation, "phase", "")) == GENERATION_ATTESTED
        and _norm(getattr(generation, "verdict", "")) == VERDICT_PRESENT
        and _norm(getattr(generation, "startup_action_id", "")) == _norm(action.action_id)
        and _norm(getattr(generation, "assigned_name", ""))
        == _norm(participant.assigned_name)
        and _norm(getattr(generation, "workspace_id", ""))
        == _norm(action.unit.workspace_id)
        and _norm(getattr(generation, "role", "")) == _norm(participant.role)
        and _norm_lane(getattr(generation, "lane_id", ""))
        == _norm_lane(action.unit.lane_id)
        and _norm(getattr(generation, "locator", "")) == locator
        and getattr(generation, "terminal_id", "") == live_terminal_id
    )


def terminal_bound_action_target_absent(
    store_home, action, participant, rows, *, generation_override=None
) -> bool:
    """Prove one recorded normal participant's private generation terminal absent."""
    try:
        snapshot = tuple(rows)
        if not terminal_identity_snapshot_complete(snapshot):
            return False
        receipt = parse_pane_bound_receipt(participant.receipt)
        if receipt is None:
            return False
        generation = generation_override
        if generation is None:
            generation = HerdrLaunchGenerationStore(home=store_home).read(
                participant.assigned_name
            )
        attestation = HerdrIdentityAttestationStore(home=store_home).read(
            participant.assigned_name
        )
    except Exception:  # noqa: BLE001
        return False
    terminal_id = getattr(generation, "terminal_id", "")
    return bool(
        generation is not None
        and _norm(getattr(generation, "phase", "")) == GENERATION_ATTESTED
        and _norm(getattr(generation, "verdict", "")) == VERDICT_PRESENT
        and _norm(getattr(generation, "startup_action_id", "")) == _norm(action.action_id)
        and _norm(getattr(generation, "assigned_name", ""))
        == _norm(participant.assigned_name)
        and _norm(getattr(generation, "workspace_id", ""))
        == _norm(action.unit.workspace_id)
        and _norm(getattr(generation, "role", "")) == _norm(participant.role)
        and _norm_lane(getattr(generation, "lane_id", ""))
        == _norm_lane(action.unit.lane_id)
        and _norm(getattr(generation, "locator", "")) == _norm(participant.locator)
        and type(terminal_id) is str
        and terminal_id
        and terminal_id.strip() == terminal_id
        and receipt.native_name == native_name_for(participant.assigned_name)
        and receipt.terminal_id == terminal_id
        and evaluate_attestation(
            attestation,
            live_locator=participant.locator,
            live_terminal_id=terminal_id,
            expected_workspace_id=action.unit.workspace_id,
            expected_role=participant.role,
            expected_lane=action.unit.lane_id,
        ).ok
        and not any(
            row.get(AGENT_KEY_NAME) == participant.assigned_name
            or _agent_locator(row) == participant.locator
            or terminal_identity_of_row(row) == terminal_id
            for row in snapshot
        )
    )


def historical_agent_generation_state(store_home, action, participant, rows) -> str:
    """Return ``absent``, ``blocked``, or ``none`` for a no-live-name participant."""
    try:
        generation = HerdrLaunchGenerationStore(home=store_home).read(
            participant.assigned_name
        )
    except Exception:  # noqa: BLE001
        return "blocked"
    if generation is None:
        return "none"
    if (
        _norm(getattr(generation, "phase", "")) == GENERATION_PENDING
        and _norm(getattr(generation, "startup_action_id", "")) == _norm(action.action_id)
        and _norm(getattr(generation, "assigned_name", ""))
        == _norm(participant.assigned_name)
        and _norm(getattr(generation, "workspace_id", ""))
        == _norm(action.unit.workspace_id)
        and _norm(getattr(generation, "role", "")) == _norm(participant.role)
        and _norm_lane(getattr(generation, "lane_id", ""))
        == _norm_lane(action.unit.lane_id)
    ):
        return "none"
    if terminal_bound_action_target_absent(
        store_home, action, participant, rows, generation_override=generation
    ):
        return "absent"
    return "blocked"


def name_matches(participant, rows) -> list[Mapping[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, Mapping)
        and _norm(row.get(AGENT_KEY_NAME)) == _norm(participant.assigned_name)
    ]


def inventory_identity_complete(rows) -> bool:
    """Require one complete globally unique terminal-identity snapshot."""
    return terminal_identity_snapshot_complete(rows)


def prepared_pane_verdict(
    ops,
    participant,
    receipt,
    *,
    inventory_readable: bool,
    obligation_names: set,
    obligation_unreadable: bool,
    conditional_close_supported: bool,
    inventory_rows: Sequence[Mapping[str, object]],
) -> ParticipantVerdict:
    """Classify a receipt-bound pane whose logical agent row is absent."""
    if not inventory_readable:
        verdict = ROLLBACK_INVENTORY_UNREADABLE
        detail = ROLLBACK_DETAIL[verdict]
    elif obligation_unreadable:
        verdict = ROLLBACK_OBLIGATION_UNREADABLE
        detail = ROLLBACK_DETAIL[verdict]
    elif participant.assigned_name in obligation_names:
        verdict = ROLLBACK_WORK_OBLIGATION
        detail = ROLLBACK_DETAIL[verdict]
    else:
        if receipt.native_name != native_name_for(participant.assigned_name):
            return ParticipantVerdict(
                role=participant.role,
                assigned_name=participant.assigned_name,
                locator=participant.locator,
                verdict=ROLLBACK_PREPARED_NATIVE_MISMATCH,
                detail="the prepared pane receipt has a foreign native identity",
                closed=participant.closed,
                prepared_pane=True,
            )
        try:
            observation = ops.prepared_pane(
                locator=participant.locator,
                workspace_id=receipt.workspace_id,
                tab_id=receipt.tab_id,
                expected_terminal_id=receipt.terminal_id,
            )
        except Exception:  # noqa: BLE001
            observation = PreparedPaneObservation(
                state=PREPARED_PANE_UNREADABLE,
                detail="prepared pane inventory could not be read",
            )
        terminal_reclaimed = bool(
            receipt.terminal_id
            and any(
                terminal_identity_of_row(row) == receipt.terminal_id
                for row in inventory_rows
            )
        )
        pane_terminal_clear = (
            not receipt.terminal_id or observation.terminal_reclaimed is False
        )
        if (
            observation.state == PREPARED_PANE_ABSENT
            and not terminal_reclaimed
            and pane_terminal_clear
        ):
            verdict = ROLLBACK_ABSENT
            detail = (
                "the pane-bound locator is positively absent from the complete Herdr "
                "pane inventory; there is nothing to close"
            )
        elif (
            not receipt.terminal_id
            or terminal_reclaimed
            or observation.terminal_reclaimed is True
        ):
            verdict = ROLLBACK_PREPARED_TERMINAL_MISMATCH
            detail = "the pane-bound receipt has no terminal generation identity"
        elif (
            observation.state == PREPARED_PANE_PRESENT
            and observation.terminal_id != receipt.terminal_id
        ):
            verdict = ROLLBACK_PREPARED_TERMINAL_MISMATCH
            detail = "the live prepared pane terminal generation changed"
        elif (
            observation.state == PREPARED_PANE_PRESENT
            and observation.locator == participant.locator
            and observation.workspace_id == receipt.workspace_id
            and observation.tab_id == receipt.tab_id
            and observation.agent_absent is True
            and observation.shell_only is True
            and observation.input_empty is True
        ):
            verdict = (
                ROLLBACK_ELIGIBLE
                if conditional_close_supported
                else ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE
            )
            detail = ROLLBACK_DETAIL.get(
                verdict, "the exact prepared pane generation is conditionally closeable"
            )
        elif observation.state == PREPARED_PANE_PRESENT:
            verdict = ROLLBACK_PREPARED_PANE_UNVERIFIABLE
            detail = (
                "the prepared pane lacks an exact terminal-bound conditional-close proof"
            )
        else:
            verdict = ROLLBACK_PREPARED_PANE_UNVERIFIABLE
            detail = observation.detail or (
                "the action-recorded prepared pane could not be proven to have the same "
                "container, no agent, only its shell, and no input; refusing to close it"
            )
    return ParticipantVerdict(
        role=participant.role,
        assigned_name=participant.assigned_name,
        locator=participant.locator,
        verdict=verdict,
        detail=detail,
        closed=participant.closed,
        prepared_pane=True,
    )


__all__ = (
    "PREPARED_PANE_ABSENT",
    "PREPARED_PANE_PRESENT",
    "PREPARED_PANE_UNREADABLE",
    "ROLLBACK_PREPARED_NATIVE_MISMATCH",
    "ROLLBACK_PREPARED_PANE_UNVERIFIABLE",
    "ROLLBACK_PREPARED_RECEIPT_INVALID",
    "historical_agent_generation_state",
    "inventory_identity_complete",
    "name_matches",
    "prepared_pane_verdict",
    "terminal_bound_action_target",
    "terminal_bound_action_target_absent",
)
