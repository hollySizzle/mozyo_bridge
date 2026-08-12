"""Sealed, replay-safe restore execution for one offline rollout (#15227).

Restore is intentionally in-process.  The already isolated one-shot runner imports this
module from the exact candidate wheel, while the provider wrapper is pinned to the sealed
production CLI.  No nonce crosses argv, stdout, or a provider environment.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path
from typing import Callable, Mapping

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (  # noqa: E501
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_restore_intent import (  # noqa: E501
    decode_restore_intent,
    restore_phase_receipt,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_container_intent import (  # noqa: E501
    build_container_intent,
    decode_container_intent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    MOZYO_BRIDGE_LAUNCHER_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_phase_fence import (  # noqa: E501
    GROUP_FOLDED,
    GROUP_LAUNCH,
    OfflineRolloutPhaseFence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_runner import (  # noqa: E501
    RUNNER_ENV,
    file_sha256,
    reports_exact_version,
    run_command,
    sanitized_runtime_env,
    validate_provider_launch_bindings,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_service import (  # noqa: E501
    prepare_configured_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_identity import (  # noqa: E501
    PrivateRestoreContainerBinding,
    PrivateWorktreeBinding,
)


_DISTRIBUTION = "mozyo-bridge"
_IDENTITY_ENV = ("MOZYO_WORKSPACE_ID", "MOZYO_AGENT_ROLE", "MOZYO_LANE_ID")
_PROVENANCE_TIMEOUT = 30.0


def _ok(**receipt: object) -> PhaseExecutionResult:
    return PhaseExecutionResult(True, receipt=receipt)


def _fail(reason: str) -> PhaseExecutionResult:
    return PhaseExecutionResult(False, reason=reason)


def capture_restore_container_intent(
    *, home: Path, plan: Mapping[str, object], private_bindings: Mapping[str, object]
):
    """Prove every post-close group has one immutable passive root before closing."""

    from mozyo_bridge.core.state.herdr_identity_attestation import (
        HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
        HerdrIdentityAttestationStore,
        evaluate_attestation,
    )
    from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
    from mozyo_bridge.core.state.startup_transaction_fence import (
        PHASE_COMPLETED_SUCCESS,
        StartupTransactionFence,
    )
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_close_authority import (  # noqa: E501
        decode_close_authority,
    )
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_legacy_absence_authority import (  # noqa: E501
        decode_legacy_absence_authority,
    )
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_pane_intent import (  # noqa: E501
        decode_pane_intent,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
        parse_pane_bound_receipt,
    )

    restore = decode_restore_intent(private_bindings, plan=plan)
    close_authority = decode_close_authority(
        private_bindings, plan=plan
    ).by_name()
    legacy_authority = decode_legacy_absence_authority(
        private_bindings, plan=plan, restore_intent=restore
    ).by_name()
    passive = decode_pane_intent(private_bindings).panes
    startup_store = StartupTransactionFence(home=home)
    attestation_store = HerdrIdentityAttestationStore(home=home)
    rows = []
    for group in restore.groups:
        containers = set()
        startup_action_ids = set()
        for expected in group.agents:
            close_pin = close_authority.get(expected.assigned_name)
            legacy_pin = legacy_authority.get(expected.assigned_name)
            if (close_pin is None) == (legacy_pin is None):
                raise ValueError("restore_container_binding_invalid")
            pin = close_pin if close_pin is not None else legacy_pin
            locator = getattr(pin, "locator", "") or getattr(
                pin, "old_locator", ""
            )
            startup = startup_store.read(pin.startup_action_id)
            participant = (
                startup.participant_for(expected.provider) if startup else None
            )
            receipt = parse_pane_bound_receipt(
                getattr(participant, "receipt", "") if participant else ""
            )
            unit = getattr(startup, "unit", None)
            attestation = attestation_store.read(expected.assigned_name)
            joined = evaluate_attestation(
                attestation,
                live_locator=locator,
                live_terminal_id=getattr(receipt, "terminal_id", ""),
                expected_workspace_id=expected.workspace_id,
                expected_role=expected.provider,
                expected_lane=expected.lane_id,
            )
            if (
                startup is None
                or startup.phase != PHASE_COMPLETED_SUCCESS
                or unit is None
                or unit.workspace_id != group.workspace_id
                or unit.lane_id != group.lane_id
                or unit.providers != tuple(sorted(group.providers))
                or participant is None
                or participant.closed is not False
                or participant.assigned_name != expected.assigned_name
                or participant.locator != locator
                or receipt is None
                or receipt.native_name != native_name_for(expected.assigned_name)
                or not receipt.terminal_id
                or attestation is None
                or attestation.schema_version
                != HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
                or not joined.ok
            ):
                raise ValueError("restore_container_binding_invalid")
            containers.add((receipt.workspace_id, receipt.tab_id))
            startup_action_ids.add(pin.startup_action_id)
        if len(containers) != 1 or len(startup_action_ids) != 1:
            raise ValueError("restore_container_binding_invalid")
        workspace_id, tab_id = containers.pop()
        matches = [
            pane
            for pane in passive
            if pane.workspace_id == workspace_id and pane.tab_id == tab_id
        ]
        if len(matches) != 1:
            raise ValueError("restore_container_binding_invalid")
        pane = matches[0]
        rows.append(
            {
                "expected_startup_action_id": group.expected_startup_action_id,
                "logical_workspace_id": group.workspace_id,
                "lane_id": group.lane_id,
                "workspace_id": pane.workspace_id,
                "tab_id": pane.tab_id,
                "pane_locator": pane.locator,
                "terminal_id": pane.terminal_id,
            }
        )
    return build_container_intent(rows, restore_intent=restore)


class _RestoreEffectGuard:
    """Private same-process pane allowance around one sealed restore group."""

    def __init__(self, *, fence, action, phase_name: str, group_index: int) -> None:
        self._fence = fence
        self._action = action
        self._phase_name = phase_name
        self._group_index = group_index
        self._baseline = fence.restore_pane_snapshot()
        self._transient: dict[str, tuple[str, str, str]] = {}

    def __repr__(self) -> str:
        return "_RestoreEffectGuard(<private>)"

    def __call__(self) -> None:
        result = self._fence.require_restore_effect(
            self._action,
            phase_name=self._phase_name,
            group_index=self._group_index,
            baseline_panes=self._baseline,
            transient_panes=self._transient,
        )
        if not result.ok:
            raise HerdrSessionStartError(result.reason)

    def before_completion(self) -> None:
        result = self._fence.require_restore_completion(
            self._action,
            phase_name=self._phase_name,
            group_index=self._group_index,
            baseline_panes=self._baseline,
            transient_panes=self._transient,
        )
        if not result.ok:
            raise HerdrSessionStartError(result.reason)

    def own_pane(
        self,
        locator: str,
        workspace_id: str,
        tab_id: str,
        terminal_id: str,
    ) -> None:
        if locator in self._baseline or locator in self._transient:
            raise HerdrSessionStartError("restore_transient_pane_identity_reused")
        if not all(
            type(value) is str and value and value == value.strip()
            for value in (locator, workspace_id, tab_id, terminal_id)
        ):
            raise HerdrSessionStartError("restore_transient_pane_unreadable")
        self._transient[locator] = (workspace_id, tab_id, terminal_id)

    def release_pane(self, locator: str) -> None:
        if locator not in self._transient:
            raise HerdrSessionStartError("restore_transient_pane_not_owned")
        del self._transient[locator]

    @property
    def settled(self) -> bool:
        return not self._transient


class OfflineRolloutRestoreExecutor:
    """Launch only an absent sealed group, or fold its exact completed action."""

    def __init__(
        self,
        *,
        home: Path,
        env: Mapping[str, str],
        phase_fence: OfflineRolloutPhaseFence,
        session_preparer: Callable = prepare_configured_session,
        session_gate_lease=None,
    ) -> None:
        self.home = Path(home).expanduser().resolve()
        self.env = dict(env)
        self.phase_fence = phase_fence
        self.session_preparer = session_preparer
        self.session_gate_lease = session_gate_lease

    @staticmethod
    def _bindings(action: Mapping[str, object]) -> Mapping[str, object]:
        bindings = action.get("private_bindings")
        if not isinstance(bindings, Mapping):
            raise ValueError("restore_bindings_invalid")
        return bindings

    @staticmethod
    def _group_repo(bindings: Mapping[str, object], group) -> Path:
        workspace_paths = bindings.get("workspace_paths")
        recovery_paths = bindings.get("legacy_recovery_worktree_paths")
        if not isinstance(workspace_paths, Mapping) or not isinstance(
            recovery_paths, Mapping
        ):
            raise ValueError("restore_repo_binding_invalid")
        raw = (
            recovery_paths.get(f"legacy:{group.recovery_issue_id}")
            if group.recovery_issue_id
            else workspace_paths.get(group.workspace_id)
        )
        if type(raw) is not str or not raw or not os.path.isabs(raw):
            raise ValueError("restore_repo_binding_invalid")
        return Path(raw).resolve()

    @staticmethod
    def _under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def _candidate_provenance(
        self, action: Mapping[str, object], action_directory: Path
    ) -> bool:
        """Prove this in-process service came from the sealed candidate runner."""

        try:
            bindings = self._bindings(action)
            runner = bindings.get("runner")
            artifact = action["plan"]["candidate_artifact"]
            if not isinstance(runner, Mapping) or not isinstance(artifact, Mapping):
                return False
            prefix = (Path(action_directory) / "runner" / "venv").resolve()
            cli = (prefix / "bin" / "mozyo-bridge").resolve()
            wheel = Path(str(runner.get("wheel") or "")).resolve()
            module = Path(__file__).resolve()
            service_module = Path(
                sys.modules[prepare_configured_session.__module__].__file__ or ""
            ).resolve()
            expected_digest = artifact.get("wheel_sha256")
            expected_version = artifact.get("version")
            target_cli = Path(str(bindings.get("target_cli") or ""))
            if not (
                Path(sys.prefix).resolve() == prefix
                and self._under(module, prefix)
                and self._under(service_module, prefix)
                and Path(str(runner.get("cli") or "")).resolve() == cli
                and cli.is_file()
                and os.access(cli, os.X_OK)
                and wheel.is_file()
                and target_cli.is_absolute()
                and target_cli.is_file()
                and os.access(target_cli, os.X_OK)
                and type(expected_digest) is str
                and file_sha256(wheel) == expected_digest
                and importlib.metadata.version(_DISTRIBUTION) == expected_version
            ):
                return False
            checked = run_command(
                [str(cli), "--version"],
                timeout=_PROVENANCE_TIMEOUT,
                env=sanitized_runtime_env(self.env),
            )
            installed = run_command(
                [str(target_cli), "--version"],
                timeout=_PROVENANCE_TIMEOUT,
                env=sanitized_runtime_env(self.env),
            )
            return (
                checked.returncode == 0
                and reports_exact_version(checked.stdout, expected_version)
                and installed.returncode == 0
                and reports_exact_version(installed.stdout, expected_version)
            )
        except Exception:  # noqa: BLE001 - unreadable provenance is a typed refusal
            return False

    def _launch_environment(self, action: Mapping[str, object]) -> dict[str, str]:
        bindings = self._bindings(action)
        agents = bindings.get("agents")
        if not isinstance(agents, list):
            raise ValueError("restore_agents_binding_invalid")
        provider_environment = validate_provider_launch_bindings(
            agents=agents,
            bindings=bindings.get("provider_executable_bindings"),
        )
        target_cli = bindings.get("target_cli")
        if (
            type(target_cli) is not str
            or not os.path.isabs(target_cli)
            or not Path(target_cli).is_file()
            or not os.access(target_cli, os.X_OK)
        ):
            raise ValueError("restore_target_cli_invalid")
        launch_env = sanitized_runtime_env(self.env)
        launch_env.pop(RUNNER_ENV, None)
        for name in _IDENTITY_ENV:
            launch_env.pop(name, None)
        launch_env.update(provider_environment)
        launch_env[MOZYO_BRIDGE_LAUNCHER_ENV] = target_cli
        return launch_env

    @staticmethod
    def _worktree_binding(action: Mapping[str, object], group):
        if not group.recovery_issue_id:
            return None
        matches = [
            row
            for row in action.get("plan", {}).get("legacy_recoveries", ())
            if isinstance(row, Mapping)
            and row.get("issue_id") == group.recovery_issue_id
            and row.get("workspace_id") == group.workspace_id
            and row.get("lane_id") == group.lane_id
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("worktree"), Mapping):
            raise ValueError("restore_worktree_binding_invalid")
        row = matches[0]
        return PrivateWorktreeBinding(
            workspace_id=group.workspace_id,
            lane_id=group.lane_id,
            lane_generation=row["lane_generation"],
            worktree_identity=row["worktree"]["identity"],
        )

    def _container_binding(self, action: Mapping[str, object], group):
        intent = decode_restore_intent(
            action.get("private_bindings"), plan=action.get("plan", {})
        )
        pane = decode_container_intent(
            action.get("private_bindings"), restore_intent=intent
        ).for_action(group.expected_startup_action_id)
        return PrivateRestoreContainerBinding(
            workspace_id=pane.workspace_id,
            tab_id=pane.tab_id,
            pane_locator=pane.pane_locator,
            terminal_id=pane.terminal_id,
        )

    def execute(
        self,
        *,
        phase_name: str,
        action: Mapping[str, object],
        action_directory: Path,
    ) -> PhaseExecutionResult:
        try:
            intent = decode_restore_intent(
                action.get("private_bindings"), plan=action.get("plan", {})
            )
            entry = self.phase_fence.require_restore_phase_entry(
                action, phase_name=phase_name
            )
            if not entry.ok:
                return entry
            group_indexes = [
                index
                for index, group in enumerate(intent.groups)
                if group.phase == phase_name
            ]
            bindings = self._bindings(action)
            for index in group_indexes:
                group = intent.groups[index]
                admission = self.phase_fence.before_restore_group(
                    action, phase_name=phase_name, group_index=index
                )
                if not admission.ok:
                    return _fail(admission.reason)
                if admission.disposition == GROUP_LAUNCH:
                    if not self._candidate_provenance(action, action_directory):
                        return _fail("restore_candidate_provenance_unverified")
                    launch_env = self._launch_environment(action)
                    # Provenance reads may take time.  Rejoin the exact partition at the
                    # actual effect edge and never launch if it changed in that window.
                    admission = self.phase_fence.before_restore_group(
                        action, phase_name=phase_name, group_index=index
                    )
                    if not admission.ok:
                        return _fail(admission.reason)
                    if admission.disposition == GROUP_LAUNCH:
                        effect_guard = _RestoreEffectGuard(
                            fence=self.phase_fence,
                            action=action,
                            phase_name=phase_name,
                            group_index=index,
                        )
                        result = self.session_preparer(
                            repo_root=self._group_repo(bindings, group),
                            agents=group.providers,
                            lane_id="" if group.lane_id == "default" else group.lane_id,
                            env=launch_env,
                            dry_run=False,
                            claude_permission_mode_default=(
                                COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT
                            ),
                            action_nonce=group.action_nonce,
                            expected_workspace_id=group.workspace_id,
                            expected_worktree=self._worktree_binding(action, group),
                            restore_container=self._container_binding(action, group),
                            restore_effect_fence=effect_guard,
                            restore_pane_owner=effect_guard,
                            restore_startup_overlap_guard=True,
                            session_gate_lease=self.session_gate_lease,
                        )
                        if result.ok is not True:
                            return _fail("workspace_restore_unhealthy")
                        if result.action_id != group.expected_startup_action_id:
                            return _fail("restore_action_id_mismatch")
                        if not effect_guard.settled:
                            return _fail("restore_transient_pane_residual")
                    elif admission.disposition != GROUP_FOLDED:
                        return _fail("restore_group_disposition_invalid")
                elif admission.disposition != GROUP_FOLDED:
                    return _fail("restore_group_disposition_invalid")
                verified = self.phase_fence.after_restore_group(
                    action, phase_name=phase_name, group_index=index
                )
                if not verified.ok:
                    return verified
            return _ok(**restore_phase_receipt(intent, phase_name))
        except Exception:  # noqa: BLE001 - private details never enter public status
            return _fail("workspace_restore_failed")


__all__ = ("OfflineRolloutRestoreExecutor", "capture_restore_container_intent")
