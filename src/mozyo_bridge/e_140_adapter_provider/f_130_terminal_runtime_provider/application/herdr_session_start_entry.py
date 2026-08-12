"""Public admission wrapper for the Herdr session-start use case (#15227)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    AttestationStoreLockBusy,
    attestation_store_lock,
)
from mozyo_bridge.core.state.herdr_session_start_gate import (
    SessionStartGateError,
    acquire_session_start_gate,
    release_session_start_gate,
    require_session_start_gate,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_cli_capabilities import (  # noqa: E501
    require_herdr_cli_capabilities,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
    STORE_MAINTENANCE_IN_PROGRESS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_bound_launch import (  # noqa: E501
    ActionPrivateLaunchShimSet,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    HerdrLauncherIncompatibleError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_role_grouped_space import (  # noqa: E501
    require_registered_top_workspace,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
    apply_workspace_alias,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_preflight import (  # noqa: E501
    validate_session_request,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    DEFAULT_COORDINATOR_PLACEMENT_MODE,
    ROLE_GROUPED_SPACE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
    AGENT_PROVIDERS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
    LAUNCH_CAUSE_GENERIC_FRESH,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


def prepare_session(
    *,
    repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    pair_order: Optional[Sequence[str]] = None,
    runner: Optional[Runner] = None,
    launcher_runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    dry_run: bool = False,
    claude_permission_mode_default: Optional[str] = None,
    agent_launch=None,
    lane_placement=None,
    launch_context=None,
    coordinator_placement_mode: str = DEFAULT_COORDINATOR_PLACEMENT_MODE,
    coordinator_top_workspace_id: str = "",
    attestation_reader: Optional[Callable] = None,
    replacement_action_id: str = "",
    probe=None,
    startup_fence=None,
    action_nonce: str = "",
    expected_workspace_id: str = "",
    expected_worktree=None,
    launch_cause: str = LAUNCH_CAUSE_GENERIC_FRESH,
    _restore_effect_fence: Optional[Callable[[], None]] = None,
    _restore_startup_overlap_guard: bool = False,
    _restore_container=None,
    _restore_pane_owner=None,
    _session_gate_lease=None,
):
    """Validate first, then hold SH authority through every non-dry effect."""

    from . import herdr_session_start as use_case

    repo_root, alias_id = apply_workspace_alias(repo_root)
    call = dict(
        alias_expected_workspace_id=expected_workspace_id or alias_id,
        workspace_effect_expected_id=expected_workspace_id,
        repo_root=repo_root,
        providers=providers,
        lane_id=lane_id,
        env=env,
        pair_order=pair_order,
        runner=runner,
        launcher_runner=launcher_runner,
        timeout=timeout,
        dry_run=dry_run,
        claude_permission_mode_default=claude_permission_mode_default,
        agent_launch=agent_launch,
        lane_placement=lane_placement,
        launch_context=launch_context,
        coordinator_placement_mode=coordinator_placement_mode,
        coordinator_top_workspace_id=coordinator_top_workspace_id,
        attestation_reader=attestation_reader,
        replacement_action_id=replacement_action_id,
        probe=probe,
        startup_fence=startup_fence,
        action_nonce=action_nonce,
        expected_worktree=expected_worktree,
        launch_cause=launch_cause,
        _restore_effect_fence=_restore_effect_fence,
        _restore_startup_overlap_guard=_restore_startup_overlap_guard,
        _restore_container=_restore_container,
        _restore_pane_owner=_restore_pane_owner,
        _session_gate_lease=_session_gate_lease,
    )
    validate_session_request(
        providers=providers,
        lane_id=lane_id,
        coordinator_placement_mode=coordinator_placement_mode,
        coordinator_top_workspace_id=coordinator_top_workspace_id,
        claude_permission_mode_default=claude_permission_mode_default,
        env=env,
        launch_context=launch_context,
        pair_order=pair_order,
        error_type=use_case.HerdrSessionStartError,
    )
    for provider in providers:
        if provider not in AGENT_PROVIDERS:
            raise use_case.HerdrSessionStartError(
                f"unknown provider {provider!r}; expected one of "
                f"{sorted(AGENT_PROVIDERS)}"
            )
    if coordinator_placement_mode == ROLE_GROUPED_SPACE:
        require_registered_top_workspace(
            coordinator_top_workspace_id, home=mozyo_bridge_home()
        )
    binary = use_case._resolve_binary_or_die(env)
    capability_runner = runner or subprocess.run
    require_herdr_cli_capabilities(
        binary,
        runner=capability_runner,
        timeout=timeout,
        env=env,
        error_type=use_case.HerdrSessionStartError,
    )
    lease = _session_gate_lease
    owns_lease = False
    if not dry_run:
        try:
            if lease is None:
                lease = acquire_session_start_gate(
                    mozyo_bridge_home(), exclusive=False
                )
                owns_lease = True
            else:
                require_session_start_gate(
                    lease, home=mozyo_bridge_home(), exclusive=False
                )
            call["_session_gate_lease"] = lease
        except SessionStartGateError as exc:
            raise use_case.HerdrSessionStartError(str(exc)) from exc
    try:
        with ActionPrivateLaunchShimSet() as launch_shims:
            call["_launch_shims"] = launch_shims
            call["_capabilities_observed"] = True
            if dry_run:
                return use_case._prepare_session_locked(**call)
            with attestation_store_lock(
                mozyo_bridge_home(), exclusive=False, blocking=False
            ):
                return use_case._prepare_session_locked(**call)
    except AttestationStoreLockBusy as exc:
        raise HerdrLauncherIncompatibleError(
            f"managed-launch admission refused: the selected attestation store is "
            f"being maintained right now ({exc}), so this launch would attest into "
            f"a store that is being rebuilt underneath it. No workspace / tab / "
            f"agent was created. Re-run once the maintenance command finishes.",
            reason=STORE_MAINTENANCE_IN_PROGRESS,
        ) from exc
    finally:
        if owns_lease:
            try:
                release_session_start_gate(lease)
            except SessionStartGateError as exc:
                raise use_case.HerdrSessionStartError(str(exc)) from exc


__all__ = ("prepare_session",)
