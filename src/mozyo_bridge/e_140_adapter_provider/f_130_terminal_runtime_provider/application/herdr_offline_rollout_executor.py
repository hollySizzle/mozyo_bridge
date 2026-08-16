"""Sealed, read-back host adapter for the shared-home Herdr offline rollout (#14838)."""

from __future__ import annotations

import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
    adopt_legacy_lanes,
    prepare_store_migration_proofs,
    store_phase_authority,
    verify_migrated_store,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    OFFLINE_ROLLOUT_APPROVAL_GATE,
    approval_fields,
    canonical_bytes,
    parse_approval_note,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_runner import (  # noqa: E501
    RUNNER_ENV,
    bounded_result as _bounded,
    file_sha256 as _sha256,
    reports_exact_version as _reports_exact_version,
    run_command as _run,
    sanitized_runtime_env as _sanitized_runtime_env,
    validate_provider_launch_bindings,
)


_RUN_TIMEOUT = 120.0
_INSTALL_TIMEOUT = 600.0

def _ok(**receipt) -> PhaseExecutionResult:
    return PhaseExecutionResult(ok=True, receipt=receipt)


def _fail(reason: str, detail: str = "") -> PhaseExecutionResult:
    return PhaseExecutionResult(ok=False, reason=reason, detail=detail[:1000])


class LiveOfflineRolloutExecutionPort:
    def __init__(
        self,
        *,
        home: Path,
        repo_root: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ):
        self.home = Path(home).expanduser().resolve()
        self.repo_root = Path(repo_root).expanduser().resolve() if repo_root else None
        self.env = dict(os.environ if env is None else env)

    # -- owner authority / private target capture ---------------------------------

    def verify_owner_approval(self, *, issue: str, journal: str, manifest):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
            committed_config_policy_pointer,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            check_issuer_resolution,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            resolve_journal_issuer,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        if self.repo_root is None:
            return _fail("owner_approval_policy_unavailable")
        try:
            source = LiveRedmineJournalSource.from_environment()
            entries = [
                entry for entry in source.read_entries(issue) if entry.journal_id == journal
            ]
        except Exception as exc:  # noqa: BLE001 - unreadable authority is never approval
            return _fail("owner_approval_unreadable", type(exc).__name__)
        if len(entries) != 1:
            return _fail("owner_approval_unresolved")
        entry = entries[0]
        policy_pointer = committed_config_policy_pointer(self.repo_root)
        issuer = resolve_journal_issuer(
            journal_id=entry.journal_id,
            notes=entry.notes,
            policy_pointer=policy_pointer,
        )
        if check_issuer_resolution(OFFLINE_ROLLOUT_APPROVAL_GATE, issuer):
            return _fail("owner_approval_issuer_mismatch")
        try:
            observed = parse_approval_note(entry.notes)
        except Exception as exc:  # noqa: BLE001 - parser supplies the fail-closed contract
            return _fail("owner_approval_malformed", str(exc))
        expected = approval_fields(manifest, issue)
        if canonical_bytes(observed) != canonical_bytes(expected):
            return _fail("owner_approval_plan_mismatch")
        return _ok(approval_verified=True, issuer_role=issuer.role)

    def capture_private_bindings(self, *, plan):
        from .herdr_offline_rollout_binding_capture import capture_private_bindings

        return capture_private_bindings(self, plan=plan)

    def prepare_external_runner(self, *, action_id, action_directory, plan):
        artifact = plan["candidate_artifact"]
        runner_root = action_directory / "runner"
        artifacts = action_directory / "artifacts"
        clean_env = _sanitized_runtime_env(self.env)
        try:
            runner_root.mkdir(mode=0o700, exist_ok=False)
            artifacts.mkdir(mode=0o700, exist_ok=False)
            venv = runner_root / "venv"
            created = _run(
                [sys.executable, "-m", "venv", str(venv)],
                timeout=_RUN_TIMEOUT,
                env=clean_env,
            )
            if created.returncode != 0:
                return _fail("runner_venv_failed", _bounded(created))
            pip = venv / "bin" / "pip"
            downloaded = _run(
                [
                    str(pip),
                    "download",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--index-url",
                    "https://test.pypi.org/simple/",
                    "--dest",
                    str(artifacts),
                    f"mozyo-bridge=={artifact['version']}",
                ],
                timeout=_INSTALL_TIMEOUT,
                env=clean_env,
            )
            if downloaded.returncode != 0:
                return _fail("candidate_download_failed", _bounded(downloaded))
            wheels = tuple(artifacts.glob("*.whl"))
            if len(wheels) != 1 or _sha256(wheels[0]) != artifact["wheel_sha256"]:
                return _fail("candidate_wheel_digest_mismatch")
            installed = _run(
                [
                    str(pip),
                    "install",
                    "--disable-pip-version-check",
                    "--extra-index-url",
                    "https://pypi.org/simple/",
                    str(wheels[0]),
                ],
                timeout=_INSTALL_TIMEOUT,
                env=clean_env,
            )
            if installed.returncode != 0:
                return _fail("runner_install_failed", _bounded(installed))
            cli = venv / "bin" / "mozyo-bridge"
            checked = _run(
                [str(cli), "--version"], timeout=_RUN_TIMEOUT, env=clean_env
            )
            if checked.returncode != 0 or not _reports_exact_version(
                checked.stdout, artifact["version"]
            ):
                return _fail("runner_provenance_failed", _bounded(checked))
            capability = _run(
                [str(cli), "herdr", "offline-rollout", "run", "--help"],
                timeout=_RUN_TIMEOUT,
                env=clean_env,
            )
            if capability.returncode != 0:
                return _fail("runner_candidate_incompatible", _bounded(capability))
            return _ok(
                cli=str(cli),
                wheel=str(wheels[0]),
                wheel_sha256=_sha256(wheels[0]),
                launchd_label=self._runner_label(action_id),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _fail("runner_prepare_failed", type(exc).__name__)

    @staticmethod
    def _runner_label(action_id: str) -> str:
        return f"com.giken.mozyo-bridge.offline-rollout.{action_id}"

    def launch_external_runner(self, *, action_id, action_directory):
        if sys.platform != "darwin":
            return _fail("unsupported_platform")
        cli = action_directory / "runner" / "venv" / "bin" / "mozyo-bridge"
        if not cli.is_file():
            return _fail("runner_unavailable")
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.infrastructure.offline_rollout_action_store import (  # noqa: E501
            OfflineRolloutActionStore,
        )

        action = OfflineRolloutActionStore(self.home).load(action_id)
        herdr_binary = action["private_bindings"].get("herdr_binary", "")
        if not Path(herdr_binary).is_file():
            return _fail("herdr_binary_unavailable")
        try:
            provider_environment = validate_provider_launch_bindings(
                agents=action["private_bindings"].get("agents", ()),
                bindings=action["private_bindings"].get(
                    "provider_executable_bindings"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - typed public refusal below
            return _fail("agent_provider_binary_binding_invalid", type(exc).__name__)
        label = self._runner_label(action_id)
        if action["private_bindings"].get("runner", {}).get("launchd_label") != label:
            return _fail("runner_launch_binding_mismatch")
        plist_path = action_directory / "runner.plist"
        log_path = action_directory / "runner.log"
        plist = {
            "Label": label,
            "ProgramArguments": [
                str(cli),
                "herdr",
                "offline-rollout",
                "run",
                "--action-id",
                action_id,
                "--home",
                str(self.home),
                "--execute",
            ],
            "EnvironmentVariables": {
                RUNNER_ENV: action_id,
                "MOZYO_BRIDGE_HOME": str(self.home),
                "MOZYO_HERDR_BINARY": herdr_binary,
                **provider_environment,
            },
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Background",
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
        }
        try:
            plist_path.write_bytes(plistlib.dumps(plist, sort_keys=True))
            plist_path.chmod(0o600)
            target = f"gui/{os.getuid()}"
            _run(["/bin/launchctl", "bootout", f"{target}/{label}"], timeout=30.0)
            launched = _run(
                ["/bin/launchctl", "bootstrap", target, str(plist_path)], timeout=30.0
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _fail("runner_launch_failed", type(exc).__name__)
        if launched.returncode != 0:
            return _fail("runner_launch_failed", _bounded(launched))
        return _ok(label=label, launchd_bootstrapped=True)

    def attest_external_runner(self, *, action_id):
        if self.env.get(RUNNER_ENV) != action_id:
            return _fail("external_runner_token_mismatch")
        if self.env.get("MOZYO_AGENT_ROLE") or self.env.get("MOZYO_WORKSPACE_ID"):
            return _fail("runner_is_managed_consumer")
        if sys.platform != "darwin":
            return _fail("unsupported_platform")
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.infrastructure.offline_rollout_action_store import (  # noqa: E501
            OfflineRolloutActionStore,
        )

        try:
            action = OfflineRolloutActionStore(self.home).load(action_id)
            expected = action["private_bindings"]["runner"]["launchd_label"]
        except Exception as exc:  # noqa: BLE001 - no private binding means no runner authority
            return _fail("runner_launch_binding_unreadable", type(exc).__name__)
        if expected != self._runner_label(action_id):
            return _fail("runner_launch_binding_mismatch")
        observed = _run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{expected}"],
            timeout=30.0,
            env=_sanitized_runtime_env(self.env),
        )
        if observed.returncode != 0:
            return _fail("runner_launchd_job_unverified", _bounded(observed))
        return _ok(external_runner=True, launchd_label=expected)

    # -- phase execution -----------------------------------------------------------

    def execute_phase(
        self,
        *,
        phase,
        action,
        action_directory,
        replaying=False,
        session_gate_lease=None,
    ):
        name = str(phase.get("phase") or "")
        handlers = {
            "supervisor_stop": self._supervisor_stop,
            "non_top_workspace_stop": self._stop_agents,
            "top_workspace_stop": self._stop_agents,
            "consumer_zero": self._consumer_zero,
            "verified_backup": self._verified_backup,
            "migrate_attestation": self._migrate_attestation,
            "migrate_lane_lifecycle": self._migrate_lane_lifecycle,
            "migrate_startup_transaction": self._migrate_startup_transaction,
            "rebuild_launch_generation": self._rebuild_launch_generation,
            "exact_runtime_install": self._exact_runtime_install,
            "legacy_lane_epoch_adoption": self._legacy_lane_epoch_adoption,
            "top_restore_action_bootstrap": self._restore_agents,
            "remaining_workspace_restore": self._restore_agents,
            "supervisor_pair_install": self._supervisor_install,
            "supervisor_pair_readback": self._supervisor_readback,
            "final_verify": self._final_verify,
        }
        handler = handlers.get(name)
        if handler is None:
            return _fail("unknown_phase", name)
        try:
            from .herdr_offline_rollout_phase_fence import (
                POST_RESTORE_EFFECT_PHASES,
                PRE_RESTORE_EFFECT_PHASES,
            )

            if name in PRE_RESTORE_EFFECT_PHASES | POST_RESTORE_EFFECT_PHASES:
                fenced = self._phase_fence(action).before_effect(action, name)
                if not fenced.ok:
                    return fenced
            if name in {
                "non_top_workspace_stop",
                "top_workspace_stop",
                "migrate_attestation",
                "migrate_lane_lifecycle",
                "migrate_startup_transaction",
                "rebuild_launch_generation",
                "legacy_lane_epoch_adoption",
            }:
                return handler(
                    phase, action, action_directory, replaying=bool(replaying)
                )
            if name in {"top_restore_action_bootstrap", "remaining_workspace_restore"}:
                return self._restore_agents(
                    phase,
                    action,
                    action_directory,
                    session_gate_lease=session_gate_lease,
                )
            return handler(phase, action, action_directory)
        except subprocess.TimeoutExpired:
            return _fail("phase_timeout", name)
        except Exception as exc:  # noqa: BLE001 - phase never escapes untyped
            return _fail(f"{name}_failed", type(exc).__name__)

    @staticmethod
    def _bindings(action):
        return action["private_bindings"]

    def _inventory(self, action):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
            read_herdr_inventory,
        )

        paths = self._bindings(action)["workspace_paths"]
        repo = Path(paths[action["plan"]["current_workspace_id"]])
        return read_herdr_inventory(repo, env=self.env)

    def _pane_inventory(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_inventory import (  # noqa: E501
            strict_pane_rows,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            _invoke,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            COMMAND_TIMEOUT_SECONDS,
            resolve_herdr_binary,
        )

        binary = resolve_herdr_binary(self.env).path
        listed = _invoke(
            binary,
            ["pane", "list"],
            subprocess.run,
            COMMAND_TIMEOUT_SECONDS,
            env=self.env,
        )
        return strict_pane_rows(listed.stdout)

    def _phase_fence(self, action):
        from .herdr_offline_rollout_phase_fence import (
            OfflineRolloutPhaseFence,
            supervisor_positive_stopped,
        )

        return OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: self._inventory(action),
            pane_inventory_reader=self._pane_inventory,
            supervisor_stopped_reader=lambda: supervisor_positive_stopped(
                home=self.home, action=action
            ),
        )

    def _require_effect_edge(self, action, phase_name):
        """Repeat the fresh three-state join at the mutation-owning call site."""

        return self._phase_fence(action).before_effect(action, phase_name)

    def _supervisor_stop(self, phase, _action, _directory):
        # Routed through the platform-resolving backend rather than the launchd module directly
        # (#15192 retired the `*_pair` verbs): the backend normalizes either host adapter into the
        # same one-row `agents` roster this check reads, so the step stops being macOS-only.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_service_backend,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_supervisor_stop import (  # noqa: E501
            supervisor_stop_refusal,
        )

        result = supervisor_service_backend.uninstall()
        status = supervisor_service_backend.service_status(mozyo_home=self.home)
        labels = phase.get("supervisor_labels") if isinstance(phase, Mapping) else None
        expected_label = labels[0] if isinstance(labels, list) and len(labels) == 1 else None
        refusal = supervisor_stop_refusal(
            result, status, expected_label=expected_label
        )
        if refusal:
            return _fail("supervisor_stop_unverified", refusal)
        return _ok(supervisors_stopped=True)

    def _ensure_wip_snapshots(
        self, action, action_directory, *, workspace_ids=None
    ):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_wip import (  # noqa: E501
            ensure_wip_snapshots,
        )

        paths = self._bindings(action)["workspace_paths"]
        selected = None if workspace_ids is None else set(workspace_ids)
        records = [
            {"snapshot_id": row["workspace_id"], "wip": row["wip"]}
            for row in action["plan"]["workspaces"]
            if selected is None or row["workspace_id"] in selected
        ]
        return ensure_wip_snapshots(
            records=records, paths=paths, action_directory=action_directory
        )

    def _ensure_recovery_wip_snapshots(self, action, action_directory):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_wip import (  # noqa: E501
            ensure_wip_snapshots,
        )

        records = [
            {
                "snapshot_id": f"legacy:{row['issue_id']}",
                "wip": row["worktree"]["wip"],
            }
            for row in action["plan"].get("legacy_recoveries", ())
        ]
        return ensure_wip_snapshots(
            records=records,
            paths=self._bindings(action).get("legacy_recovery_worktree_paths", {}),
            action_directory=action_directory,
        )

    def _stop_agents(
        self, phase, action, action_directory, *, replaying=False
    ):
        from .herdr_offline_rollout_close import OfflineRolloutCloseExecutor

        names = tuple(phase.get("assigned_names", ()))
        closer = OfflineRolloutCloseExecutor(
            home=self.home,
            env=self.env,
            inventory_reader=lambda: self._inventory(action),
            pane_inventory_reader=self._pane_inventory,
            workspace_paths=self._bindings(action)["workspace_paths"],
        )
        settled = closer.wait_for_settled(
            action=action, names=names, replaying=bool(replaying)
        )
        if not settled.ok:
            return settled
        current_workspace = action["plan"]["current_workspace_id"]
        if phase.get("phase") == "top_workspace_stop":
            workspace_ids = {current_workspace}
        else:
            workspace_ids = {
                row["workspace_id"]
                for row in action["plan"]["workspaces"]
                if row["workspace_id"] != current_workspace
            }
        preserved = self._ensure_wip_snapshots(
            action, action_directory, workspace_ids=workspace_ids
        )
        if not preserved.ok:
            return preserved
        if phase.get("phase") == "top_workspace_stop":
            recovered_wip = self._ensure_recovery_wip_snapshots(action, action_directory)
            if not recovered_wip.ok:
                return recovered_wip
        stopped = closer.close_names(
            action=action, names=names, replaying=bool(replaying)
        )
        if not stopped.ok:
            return stopped
        return _ok(**dict(stopped.receipt), wip_preserved=True)

    def _consumer_zero(self, _phase, action, _directory):
        result = self._phase_fence(action).require_pre_restore(action)
        if not result.ok:
            return result
        return _ok(consumer_count=0, exact_original_absence=True)

    def _legacy_lane_epoch_adoption(
        self, _phase, action, _directory, *, replaying=False
    ):
        return adopt_legacy_lanes(
            home=self.home,
            targets=action["plan"].get("legacy_recoveries", ()),
            replaying=bool(replaying),
            effect_fence=lambda: self._require_effect_edge(
                action, "legacy_lane_epoch_adoption"
            ),
        )

    def _fresh_store_records(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_snapshot import (  # noqa: E501
            _store_snapshots,
        )

        return {row.name: row.to_record() for row in _store_snapshots(self.home)}

    def _require_store_phase_authority(
        self, action, *, store_name, phase_name, replaying
    ):
        observed = self._fresh_store_records().get(store_name)
        return store_phase_authority(
            action,
            observed,
            store_name=store_name,
            phase_name=phase_name,
            replaying=bool(replaying),
        )

    def _verified_backup(self, _phase, action, action_directory):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            herdr_identity_attestation_path,
        )
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            backup_attestation_store,
        )
        from mozyo_bridge.core.state.lane_lifecycle_backup import backup_state_container
        from mozyo_bridge.core.state.lane_lifecycle_schema import lane_lifecycle_path
        from mozyo_bridge.core.state.startup_store_migration import (
            artifact_digest_of,
            publish_recovery_artifact,
            stage_recovery_artifact,
        )
        from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence

        expected = action["plan"]["stores"]
        if self._fresh_store_records() != expected:
            return _fail("store_plan_drift")
        backup_root = action_directory / "backups"
        admitted = self._require_effect_edge(action, "verified_backup")
        if not admitted.ok:
            return admitted
        backup_root.mkdir(mode=0o700, exist_ok=True)
        admitted = self._require_effect_edge(action, "verified_backup")
        if not admitted.ok:
            return admitted
        attestation = backup_attestation_store(herdr_identity_attestation_path(self.home))
        admitted = self._require_effect_edge(action, "verified_backup")
        if not admitted.ok:
            return admitted
        state = backup_state_container(lane_lifecycle_path(self.home))
        startup = backup_root / "startup-preflight"
        if not startup.exists():
            fence = StartupTransactionFence(home=self.home)
            admitted = self._require_effect_edge(action, "verified_backup")
            if not admitted.ok:
                return admitted
            with fence._hold():  # noqa: SLF001 - migration authority's external lock
                with fence._connection("ro") as conn:  # noqa: SLF001
                    staging = startup.with_name(startup.name + ".staging")
                    stage_recovery_artifact(fence, conn, staging)
                    publish_recovery_artifact(staging, startup)
        startup_digest = artifact_digest_of(startup)
        if not startup_digest:
            return _fail("startup_backup_readback_failed")
        from .herdr_offline_rollout_generation_rebuild import backup_launch_generation
        admitted = self._require_effect_edge(action, "verified_backup")
        if not admitted.ok:
            return admitted
        launch = backup_launch_generation(
            home=self.home,
            backup_root=backup_root,
            planned=expected["launch_generation"],
            observe=lambda: self._fresh_store_records()["launch_generation"],
            effect_fence=lambda: self._require_effect_edge(
                action, "verified_backup"
            ),
        )
        if not launch.ok:
            return launch
        proofs = prepare_store_migration_proofs(
            action_directory=action_directory,
            store_paths={
                "attestation": herdr_identity_attestation_path(self.home),
                "lane_lifecycle": lane_lifecycle_path(self.home),
            },
            effect_fence=lambda: self._require_effect_edge(
                action, "verified_backup"
            ),
        )
        if not proofs.ok:
            return proofs
        return _ok(
            attestation_backup=bool(attestation),
            state_backup=bool(state),
            startup_backup=True,
            startup_backup_digest=startup_digest,
            **launch.receipt,
            migration_post_digests=proofs.receipt["migration_post_digests"],
        )

    def _migrate_attestation(
        self, _phase, action, _directory, *, replaying=False
    ):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            herdr_identity_attestation_path,
        )
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            migrate_attestation_store,
        )

        authority = self._require_store_phase_authority(
            action,
            store_name="attestation",
            phase_name="migrate_attestation",
            replaying=replaying,
        )
        if not authority.ok:
            return authority
        path = herdr_identity_attestation_path(self.home)
        admitted = self._require_effect_edge(action, "migrate_attestation")
        if not admitted.ok:
            return admitted
        result = migrate_attestation_store(path)
        verified = verify_migrated_store(
            action, "attestation", self._fresh_store_records().get("attestation")
        )
        if not verified.ok:
            return verified
        return _ok(outcome=result.outcome, to_version=result.to_version, **verified.receipt)

    def _migrate_lane_lifecycle(
        self, _phase, action, _directory, *, replaying=False
    ):
        from mozyo_bridge.core.state.lane_lifecycle_schema import (
            ensure_lane_lifecycle_schema,
            lane_lifecycle_path,
        )

        authority = self._require_store_phase_authority(
            action,
            store_name="lane_lifecycle",
            phase_name="migrate_lane_lifecycle",
            replaying=replaying,
        )
        if not authority.ok:
            return authority
        admitted = self._require_effect_edge(action, "migrate_lane_lifecycle")
        if not admitted.ok:
            return admitted
        outcome = ensure_lane_lifecycle_schema(lane_lifecycle_path(self.home))
        verified = verify_migrated_store(
            action, "lane_lifecycle", self._fresh_store_records().get("lane_lifecycle")
        )
        if not verified.ok:
            return verified
        return _ok(action=outcome.action, to_version=outcome.to_version, **verified.receipt)

    def _migrate_startup_transaction(
        self, _phase, action, action_directory, *, replaying=False
    ):
        authority = self._require_store_phase_authority(
            action,
            store_name="startup_transaction",
            phase_name="migrate_startup_transaction",
            replaying=replaying,
        )
        if not authority.ok:
            return authority
        from mozyo_bridge.core.state.startup_store_migration import (
            MigrationCompletionReceipt,
            artifact_digest_of,
            migrate_startup_store_v1_to_v2,
        )
        from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence

        expected = action["plan"]["stores"]["startup_transaction"][
            "migration_plan_digest"
        ]
        artifact = action_directory / "backups" / "startup-migration"
        fence = StartupTransactionFence(home=self.home)
        completion = None
        if artifact.is_dir():
            with fence._connection("ro") as conn:  # noqa: SLF001
                row = conn.execute(
                    "SELECT value FROM store_meta WHERE key='store_nonce'"
                ).fetchone()
            completion = MigrationCompletionReceipt(
                action_id=action["action_id"],
                plan_digest=expected,
                artifact_path=str(artifact),
                store_identity=str(row[0]) if row else "",
                artifact_digest=artifact_digest_of(artifact),
            )
        admitted = self._require_effect_edge(action, "migrate_startup_transaction")
        if not admitted.ok:
            return admitted
        result = migrate_startup_store_v1_to_v2(
            fence,
            backup_path=artifact,
            expected_plan_digest=expected,
            completion_receipt=completion,
        )
        if type(result.schema_version) is not int or result.schema_version != 2:
            return _fail("startup_migration_unverified")
        return _ok(
            outcome=result.outcome,
            to_version=result.schema_version,
            artifact_digest=result.artifact_digest or completion.artifact_digest,
        )
    def _rebuild_launch_generation(self, _phase, action, _directory, *, replaying=False):
        """Backup-first v1 cache reset; restore repopulates terminal-bound v2 rows."""
        from .herdr_offline_rollout_generation_rebuild import rebuild_launch_generation
        planned = action["plan"]["stores"]["launch_generation"]
        admitted = self._require_effect_edge(action, "rebuild_launch_generation")
        if not admitted.ok:
            return admitted
        return rebuild_launch_generation(
            home=self.home, backup_root=_directory / "backups", planned=planned,
            observe=lambda: self._fresh_store_records()["launch_generation"],
            backup_receipt=action.get("phase_receipts", {}).get("verified_backup", {}),
            replaying=replaying,
            effect_fence=lambda: self._require_effect_edge(
                action, "rebuild_launch_generation"
            ),
        )
    def _exact_runtime_install(self, _phase, action, _directory):
        bindings = self._bindings(action)
        runner = bindings["runner"]
        wheel = Path(runner["wheel"])
        artifact = action["plan"]["candidate_artifact"]
        if _sha256(wheel) != artifact["wheel_sha256"]:
            return _fail("candidate_wheel_digest_mismatch")
        clean_env = _sanitized_runtime_env(self.env)
        admitted = self._require_effect_edge(action, "exact_runtime_install")
        if not admitted.ok:
            return admitted
        installed = _run(
            [bindings["pipx"], "install", "--force", str(wheel)],
            timeout=_INSTALL_TIMEOUT,
            env=clean_env,
        )
        if installed.returncode != 0:
            return _fail("runtime_install_failed", _bounded(installed))
        checked = _run(
            [bindings["target_cli"], "--version"], timeout=30.0, env=clean_env
        )
        if checked.returncode != 0 or not _reports_exact_version(
            checked.stdout, artifact["version"]
        ):
            return _fail("runtime_install_unverified", _bounded(checked))
        return _ok(version=artifact["version"], wheel_sha256=artifact["wheel_sha256"])

    def _restore_agents(
        self, phase, action, _directory, *, session_gate_lease=None
    ):
        from .herdr_offline_rollout_restore import OfflineRolloutRestoreExecutor

        return OfflineRolloutRestoreExecutor(
            home=self.home,
            env=self.env,
            phase_fence=self._phase_fence(action),
            session_gate_lease=session_gate_lease,
        ).execute(
            phase_name=str(phase.get("phase") or ""),
            action=action,
            action_directory=_directory,
        )

    def _verify_live_names(self, action, names, *, exact_roster=False):
        from .herdr_offline_restore_verification import verify_restored_names
        view = self._inventory(action)
        ok, name = verify_restored_names(
            view=view, names=names, home=self.home, exact_roster=exact_roster)
        if not ok:
            return (
                _fail("restore_attestation_unverified", name)
                if name else _fail("restore_inventory_unreadable")
            )
        return _ok(live_names_verified=True)

    def _supervisor_install(self, _phase, action, _directory):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_service_backend,
        )

        target_cli = self._bindings(action)["target_cli"]

        def which(name):
            return target_cli if name == "mozyo-bridge" else shutil.which(name)

        admitted = self._require_effect_edge(action, "supervisor_pair_install")
        if not admitted.ok:
            return admitted
        result = supervisor_service_backend.install(mozyo_home=self.home, which=which)
        if not result.get("performed"):
            return _fail("supervisor_install_failed", str(result.get("reason") or ""))
        return _ok(supervisors_installed=True)

    def _supervisor_readback(self, _phase, action, _directory):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_service_backend,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd import (  # noqa: E501
            CREDENTIAL_READY,
            HOME_PIN_OK,
        )

        target_cli = self._bindings(action)["target_cli"]

        def which(name):
            return target_cli if name == "mozyo-bridge" else shutil.which(name)

        status = supervisor_service_backend.service_status(mozyo_home=self.home, which=which)
        agents = status.get("agents", ())
        # Every owned service must be healthy, and there must BE one: the count is the host's owned
        # roster size (one since #15192), never a hard-coded pair.
        healthy = bool(agents) and all(
            row.get("installed")
            and row.get("loaded")
            and row.get("home_pin") == HOME_PIN_OK
            and row.get("executable_matches")
            and row.get("credential_readiness") == CREDENTIAL_READY
            for row in agents
        )
        if not healthy:
            return _fail("supervisor_readback_failed")
        return _ok(supervisors_ready=True)

    def _final_verify(self, _phase, action, _directory):
        expected = {row["assigned_name"] for row in action["plan"]["agents"]}
        expected.update(
            agent["assigned_name"]
            for recovery in action["plan"].get("legacy_recoveries", ())
            for agent in recovery.get("agents", ())
        )
        live = self._verify_live_names(action, expected, exact_roster=True)
        if not live.ok:
            return live
        stores = self._fresh_store_records()
        targets = {
            "attestation": 4, "lane_lifecycle": 11,
            "launch_generation": 2, "startup_transaction": 2,
        }
        if any(stores[n]["state"] != "recognized" or type(stores[n]["version"]) is not int
               or stores[n]["version"] != v
               for n, v in targets.items()):
            return _fail("final_store_version_mismatch")
        supervisors = self._supervisor_readback({}, action, _directory)
        if not supervisors.ok:
            return supervisors
        checked = _run(
            [self._bindings(action)["target_cli"], "--version"],
            timeout=30.0,
            env=_sanitized_runtime_env(self.env),
        )
        version = action["plan"]["candidate_artifact"]["version"]
        if checked.returncode != 0 or not _reports_exact_version(
            checked.stdout, version
        ):
            return _fail("final_runtime_provenance_mismatch")
        return _ok(
            final_verified=True,
            agent_count=len(expected),
            runtime_version=version,
            schema_versions=targets,
        )


__all__ = ("LiveOfflineRolloutExecutionPort", "RUNNER_ENV")
