"""Bound replacement orchestration while the selected attestation store is v1.

The shared identity-attestation store must not be migrated while older installed
launchers are still live.  This narrow adapter therefore combines a normal v1
self-attestation with the separate action-binding authority introduced for #13933.
It accepts only an exact immutable replacement identity and a durable startup
participant receipt; a foreign live slot is never adopted or retroactively bound.

The caller owns the main attestation store's shared generation lock for this whole
operation.  ``launch`` must consequently use the session-start composition entry
that assumes that lock is already held.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    HerdrIdentityReplacementBindingStore,
    ReplacementActionBinding,
    ReplacementActionBindingError,
    replacement_action_is_bound,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_COMPLETED_SUCCESS,
    StartupTransactionFence,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (  # noqa: E501
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SessionStartResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    Runner,
)


def prepare_actuator_lane_session(
    *,
    worktree_path: str,
    config_repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    runner: Optional[Runner],
    timeout: float,
    replacement_action_id: str,
    action_nonce: str = "",
    startup_fence: StartupTransactionFence | None = None,
    admission_lock_held: bool = False,
) -> SessionStartResult:
    """Compose one actuator launch, optionally beneath its caller-held store lock."""
    from mozyo_bridge.application.repo_local_config_loader import (
        load_repo_local_config,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        _prepare_session_locked,
        prepare_session,
    )

    repo_config = load_repo_local_config(config_repo_root)
    session_start = _prepare_session_locked if admission_lock_held else prepare_session
    return session_start(
        repo_root=Path(worktree_path),
        providers=list(providers),
        lane_id=lane_id,
        env=env,
        runner=runner,
        timeout=timeout,
        lane_placement=repo_config.lane_placement,
        claude_permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
        agent_launch=repo_config.agent_launch,
        replacement_action_id=replacement_action_id,
        action_nonce=action_nonce,
        startup_fence=startup_fence,
    )


# Stable, value-free reasons surfaced through the coordinator's typed heal error.
V1_BINDING_CONTEXT_MISSING = "replacement_binding_context_missing"
V1_BINDING_AUTHORITY_CONFLICT = "replacement_binding_authority_conflict"
V1_BINDING_STORE_UNUSABLE = "replacement_binding_store_unusable"
V1_BINDING_STARTUP_DEBT = "replacement_binding_startup_debt"
V1_BINDING_LAUNCH_UNHEALTHY = "replacement_binding_launch_unhealthy"
V1_BINDING_ATTESTATION_MISMATCH = "replacement_binding_attestation_mismatch"
V1_BINDING_MAINTENANCE_BUSY = "replacement_binding_maintenance_busy"


class V1ReplacementBindingFailure(RuntimeError):
    """A stable fail-closed reason from the v1 replacement binding adapter.

    ``startup_result`` carries the nested :class:`SessionStartResult` of an *unhealthy fresh
    launch* so the execution-platform caller can project a locator-free startup observation
    (typed action id / per-role health / rollback debt) and surface the explicit public
    rollback pointer, without this adapter layer ever importing the execution platform
    (Redmine #13948 R3). It is populated only for :data:`V1_BINDING_LAUNCH_UNHEALTHY`; the
    raw result never leaves this process un-projected — the sole caller
    (:meth:`HerdrSublaneActuatorOps.heal_lane_column`) projects it at the catch site.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        startup_result: "SessionStartResult | None" = None,
    ):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.startup_result = startup_result


def _stop(
    reason: str, detail: str, *, startup_result: "SessionStartResult | None" = None
) -> None:
    raise V1ReplacementBindingFailure(reason, detail, startup_result=startup_result)


def _binding_identity_matches(
    intent: ReplacementActionBinding,
    *,
    action_id: str,
    assigned_name: str,
    workspace_id: str,
    provider: str,
    lane_id: str,
    old_locator: str,
    managed_pair: tuple[str, ...],
) -> bool:
    if (
        intent.action_id != action_id
        or intent.assigned_name != assigned_name
        or intent.workspace_id != workspace_id
        or intent.role != provider
        or intent.lane_id != lane_id
        or intent.old_locator != old_locator
    ):
        return False
    try:
        expected_startup = startup_action_id(
            StartupUnit(workspace_id, lane_id, managed_pair), intent.startup_nonce
        )
    except ValueError:
        return False
    return intent.startup_action_id == expected_startup


def _bind_startup_receipt(
    *,
    home: Path,
    binding_store: HerdrIdentityReplacementBindingStore,
    startup_fence: StartupTransactionFence,
    intent: ReplacementActionBinding,
    live_locator: str,
    managed_pair: tuple[str, ...],
) -> None:
    """Finish or replay one exact reserve -> startup receipt -> v1-row binding."""
    if intent.phase == "bound":
        try:
            record = HerdrIdentityAttestationStore(home=home).read(
                intent.assigned_name
            )
        except Exception:  # noqa: BLE001 - fixed typed public reason below
            _stop(
                V1_BINDING_ATTESTATION_MISMATCH,
                "the v1 startup attestation is unreadable",
            )
        if not replacement_action_is_bound(
            record,
            action_id=intent.action_id,
            live_locator=live_locator,
            expected_workspace_id=intent.workspace_id,
            expected_role=intent.role,
            expected_lane=intent.lane_id,
            expected_assigned_name=intent.assigned_name,
            expected_old_locator=intent.old_locator,
            home=home,
        ):
            _stop(
                V1_BINDING_ATTESTATION_MISMATCH,
                "the bound action no longer matches the live v1 attestation generation",
            )
        return

    try:
        startup = startup_fence.read(intent.startup_action_id)
    except Exception:  # noqa: BLE001 - authority read is fail-closed
        _stop(
            V1_BINDING_STARTUP_DEBT,
            "the startup transaction receipt is unreadable",
        )
    if (
        startup is None
        or startup.phase != PHASE_COMPLETED_SUCCESS
        or startup.action_id != intent.startup_action_id
        or startup.unit.workspace_id != intent.workspace_id
        or startup.unit.lane_id != intent.lane_id
        or tuple(startup.unit.providers) != tuple(sorted(set(managed_pair)))
    ):
        _stop(
            V1_BINDING_STARTUP_DEBT,
            "the reserved launch has not reached exact durable startup success",
        )
    participant = startup.participant_for(intent.role)
    if (
        participant is None
        or participant.closed
        or participant.assigned_name != intent.assigned_name
        or participant.locator != live_locator
        or not participant.receipt
    ):
        _stop(
            V1_BINDING_AUTHORITY_CONFLICT,
            "the startup participant receipt does not match the live replacement slot",
        )
    try:
        record = HerdrIdentityAttestationStore(home=home).read(intent.assigned_name)
    except Exception:  # noqa: BLE001 - fixed typed public reason below
        _stop(
            V1_BINDING_ATTESTATION_MISMATCH,
            "the v1 startup attestation is unreadable",
        )
    join = evaluate_attestation(
        record,
        live_locator=live_locator,
        expected_workspace_id=intent.workspace_id,
        expected_role=intent.role,
        expected_lane=intent.lane_id,
    )
    if not join.ok or record is None or record.replacement_action_id:
        _stop(
            V1_BINDING_ATTESTATION_MISMATCH,
            "the startup receipt does not have an exact normal-v1 attestation row",
        )
    try:
        binding_store.bind(
            intent,
            attestation=record,
            receipt_startup_action_id=startup.action_id,
            receipt_role=participant.role,
            receipt_assigned_name=participant.assigned_name,
            receipt_locator=participant.locator,
            receipt_present=bool(participant.receipt),
        )
    except ReplacementActionBindingError:
        _stop(
            V1_BINDING_AUTHORITY_CONFLICT,
            "the replacement action binding compare-and-set was refused",
        )


def launch_or_resume_v1_replacement(
    *,
    home: Path,
    action_id: str,
    assigned_name: str,
    old_locator: str,
    target_provider: str | None,
    workspace_id: str,
    lane_id: str,
    managed_pair: tuple[str, ...],
    rows: Sequence[Mapping[str, object]],
    existing: Mapping[str, tuple[str, str]],
    launch: Callable[[str, StartupTransactionFence], SessionStartResult],
) -> None:
    """Launch or resume one exact v1-bound replacement under a caller-held lock."""
    action_id = (action_id or "").strip()
    provider = (target_provider or "").strip()
    assigned_name = (assigned_name or "").strip()
    old_locator = (old_locator or "").strip()
    lane_id = (lane_id or "").strip()
    if (
        not action_id
        or provider not in managed_pair
        or not assigned_name
        or not old_locator
        or not workspace_id
        or not lane_id
        or assigned_name != encode_assigned_name(workspace_id, provider, lane_id)
    ):
        _stop(
            V1_BINDING_CONTEXT_MISSING,
            "an exact action/provider/assigned-name/workspace/lane binding is required",
        )

    named_rows = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
    ]
    if len(named_rows) > 1:
        _stop(
            V1_BINDING_AUTHORITY_CONFLICT,
            "the replacement assigned name is duplicated in live inventory",
        )
    live_locator = existing.get(provider, ("", ""))[0]
    if named_rows and _agent_locator(named_rows[0]) != live_locator:
        _stop(
            V1_BINDING_AUTHORITY_CONFLICT,
            "the resolved target slot disagrees with exact-name live inventory",
        )

    binding_store = HerdrIdentityReplacementBindingStore(home=home)
    startup_fence = StartupTransactionFence(home=home)
    try:
        intent = binding_store.read(action_id, assigned_name)
    except ReplacementActionBindingError:
        _stop(
            V1_BINDING_STORE_UNUSABLE,
            "the replacement action binding store is absent, unsafe, or unreadable",
        )
    if intent is not None and not _binding_identity_matches(
        intent,
        action_id=action_id,
        assigned_name=assigned_name,
        workspace_id=workspace_id,
        provider=provider,
        lane_id=lane_id,
        old_locator=old_locator,
        managed_pair=managed_pair,
    ):
        _stop(
            V1_BINDING_AUTHORITY_CONFLICT,
            "the existing replacement binding belongs to another identity or generation",
        )

    if live_locator:
        if intent is None:
            _stop(
                V1_BINDING_AUTHORITY_CONFLICT,
                "a live replacement slot has no reserve-before-launch binding",
            )
        _bind_startup_receipt(
            home=home,
            binding_store=binding_store,
            startup_fence=startup_fence,
            intent=intent,
            live_locator=live_locator,
            managed_pair=managed_pair,
        )
        return

    nonce = binding_store.new_startup_nonce()
    startup_id = startup_action_id(
        StartupUnit(workspace_id, lane_id, managed_pair), nonce
    )
    if intent is None:
        try:
            intent = binding_store.reserve(
                action_id=action_id,
                assigned_name=assigned_name,
                workspace_id=workspace_id,
                role=provider,
                lane_id=lane_id,
                old_locator=old_locator,
                startup_nonce=nonce,
                startup_action_id=startup_id,
            )
        except ReplacementActionBindingError:
            _stop(
                V1_BINDING_STORE_UNUSABLE,
                "the replacement binding could not be reserved before launch",
            )
    else:
        try:
            prior = startup_fence.read(intent.startup_action_id)
        except Exception:  # noqa: BLE001 - fail-closed authority read
            prior = None
        if prior is None or prior.phase != PHASE_COMPLETED_ROLLED_BACK:
            _stop(
                V1_BINDING_STARTUP_DEBT,
                "the prior startup attempt is not durably rolled back",
            )
        try:
            intent = binding_store.replace_rolled_back_reservation(
                intent, startup_nonce=nonce, startup_action_id=startup_id
            )
        except ReplacementActionBindingError:
            _stop(
                V1_BINDING_AUTHORITY_CONFLICT,
                "the rolled-back binding attempt changed before replay",
            )

    result = launch(intent.startup_nonce, startup_fence)
    launched = [
        slot
        for slot in result.slots
        if slot.provider == provider and slot.assigned_name == assigned_name
    ]
    if (
        result.action_id != intent.startup_action_id
        or len(launched) != 1
        or launched[0].outcome != "launched"
        or not launched[0].locator
    ):
        _stop(
            V1_BINDING_AUTHORITY_CONFLICT,
            "the startup result did not record one exact fresh replacement participant",
        )
    if not result.ok:
        # Carry the nested result so the caller can project the typed action id / per-role
        # health / rollback debt and surface the explicit public rollback pointer for the
        # SAME startup action (Redmine #13948 R3). ``result.action_id`` is exactly the
        # startup-transaction id ``herdr session-rollback --action-id`` acts under.
        _stop(
            V1_BINDING_LAUNCH_UNHEALTHY,
            "the fresh replacement participant did not reach bounded startup health",
            startup_result=result,
        )
    _bind_startup_receipt(
        home=home,
        binding_store=binding_store,
        startup_fence=startup_fence,
        intent=intent,
        live_locator=launched[0].locator,
        managed_pair=managed_pair,
    )
