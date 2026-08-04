"""Host adapter for the external shared-home Herdr offline rollout (#14838).

All destructive targets come from the sealed private action.  Every phase measures
its goal state after the effect and is safe to re-enter: absence is accepted only for
an exact planned name, replacement locator/name drift is refused, and migrations are
forward-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    OFFLINE_ROLLOUT_APPROVAL_GATE,
    approval_fields,
    canonical_bytes,
    parse_approval_note,
)


RUNNER_ENV = "MOZYO_OFFLINE_ROLLOUT_RUNNER_ACTION_ID"
_RUN_TIMEOUT = 120.0
_INSTALL_TIMEOUT = 600.0


def _ok(**receipt) -> PhaseExecutionResult:
    return PhaseExecutionResult(ok=True, receipt=receipt)


def _fail(reason: str, detail: str = "") -> PhaseExecutionResult:
    return PhaseExecutionResult(ok=False, reason=reason, detail=detail[:1000])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _run(argv: Sequence[str], *, timeout: float, env=None, cwd=None):
    return subprocess.run(
        list(argv),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )


def _bounded(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stderr or "") or (result.stdout or "")).strip()[:1000]


def _sanitized_runtime_env(env: Mapping[str, str]) -> dict[str, str]:
    """Keep source-tree injection from impersonating the installed candidate."""
    clean = dict(env)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        clean.pop(name, None)
    return clean


def _reports_exact_version(stdout: object, expected: object) -> bool:
    """Accept one exact CLI version token, never a substring such as a4 in a41."""
    if not isinstance(stdout, str) or not isinstance(expected, str) or not expected:
        return False
    return stdout.splitlines() == [f"mozyo-bridge {expected}"]


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
        from mozyo_bridge.core.state.workspace_registry import list_workspaces
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
            read_herdr_inventory,
        )

        if self.repo_root is None:
            return _fail("repo_root_required")
        try:
            records = tuple(list_workspaces(home=self.home))
            inventory = read_herdr_inventory(self.repo_root, env=self.env)
        except Exception as exc:  # noqa: BLE001
            return _fail("private_binding_capture_failed", type(exc).__name__)
        if not inventory.ok or inventory.invalid_row_count != 0:
            return _fail("inventory_unreadable", inventory.reason or "")
        wanted_workspaces = {
            row["workspace_id"] for row in plan.get("workspaces", ())
        }
        by_workspace = {record.workspace_id: record for record in records}
        if set(by_workspace) != wanted_workspaces:
            return _fail("workspace_set_drift")
        wanted_agents = {row["assigned_name"] for row in plan.get("agents", ())}
        current_agents = {agent.name for agent in inventory.managed_agents}
        if current_agents != wanted_agents or inventory.unmanaged_agents:
            return _fail("agent_set_drift")
        agents = []
        for agent in inventory.managed_agents:
            if not agent.locator:
                return _fail("agent_locator_unreadable", agent.name)
            agents.append(
                {
                    "assigned_name": agent.name,
                    "workspace_id": agent.workspace_id,
                    "lane_id": agent.lane_id,
                    "provider": agent.role,
                    "locator": agent.locator,
                }
            )
        target_cli = shutil.which("mozyo-bridge", path=self.env.get("PATH"))
        pipx = shutil.which("pipx", path=self.env.get("PATH"))
        if not target_cli or not pipx:
            return _fail("runtime_installer_unavailable")
        try:
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
                resolve_herdr_binary,
            )

            herdr_binary = resolve_herdr_binary(self.env).path
        except Exception as exc:  # noqa: BLE001
            return _fail("herdr_binary_unavailable", type(exc).__name__)
        return _ok(
            workspace_paths={
                workspace_id: by_workspace[workspace_id].canonical_path
                for workspace_id in sorted(wanted_workspaces)
            },
            agents=sorted(agents, key=lambda row: row["assigned_name"]),
            # Keep the stable pipx app symlink, not its current venv target: --force
            # replaces that venv during the cutover and the old resolved path vanishes.
            target_cli=str(Path(target_cli).absolute()),
            pipx=str(Path(pipx).resolve()),
            herdr_binary=str(Path(herdr_binary).resolve()),
        )

    # -- immutable external runner -------------------------------------------------

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

    def execute_phase(self, *, phase, action, action_directory, replaying=False):
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
            "exact_runtime_install": self._exact_runtime_install,
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
            if name in {
                "migrate_attestation",
                "migrate_lane_lifecycle",
                "migrate_startup_transaction",
            }:
                return handler(
                    phase, action, action_directory, replaying=bool(replaying)
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

    def _supervisor_stop(self, _phase, _action, _directory):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd import (  # noqa: E501
            service_status_pair,
            uninstall_pair,
        )

        result = uninstall_pair()
        status = service_status_pair(mozyo_home=self.home)
        stopped = all(
            not row.get("installed") and not row.get("loaded")
            for row in status.get("agents", ())
        )
        if not result.get("performed") or not stopped:
            return _fail("supervisor_stop_unverified", str(result.get("reason") or ""))
        return _ok(supervisors_stopped=True)

    def _ensure_wip_snapshots(
        self, action, action_directory, *, workspace_ids=None
    ):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
            read_live_worktree_fingerprint,
        )

        root = action_directory / "wip"
        root.mkdir(mode=0o700, exist_ok=True)
        paths = self._bindings(action)["workspace_paths"]
        selected = None if workspace_ids is None else set(workspace_ids)
        for row in action["plan"]["workspaces"]:
            workspace_id = row["workspace_id"]
            if selected is not None and workspace_id not in selected:
                continue
            # Registry IDs are authority tokens, not trusted path segments.  Hashing keeps every
            # snapshot inside the sealed action even if an old/imported registry row contains
            # separators or dot components.
            target = root / hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
            manifest_path = target / "manifest.json"
            current = read_live_worktree_fingerprint(Path(paths[workspace_id]), 30.0)
            if not current.readable or current.digest != row["wip"]["digest"]:
                return _fail("wip_drift", workspace_id)
            if not row["wip"]["dirty"] and not row["wip"]["untracked"]:
                continue
            if manifest_path.is_file():
                recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    recorded.get("workspace_id") != workspace_id
                    or recorded.get("wip_digest") != current.digest
                ):
                    return _fail("wip_snapshot_mismatch", workspace_id)
                files = recorded.get("files")
                expected_files = {
                    "worktree.patch",
                    "index.patch",
                    "untracked.list",
                    "git-index",
                    "untracked.tar",
                }
                if (
                    not isinstance(files, Mapping)
                    or set(files) != expected_files
                    or any(
                    not (target / name).is_file()
                    or _sha256(target / name) != digest
                    for name, digest in files.items()
                    )
                ):
                    return _fail("wip_snapshot_readback_failed", workspace_id)
                continue
            target.mkdir(mode=0o700, exist_ok=False)
            repo = Path(paths[workspace_id])
            commands = {
                "worktree.patch": ["git", "diff", "HEAD", "--binary", "--no-ext-diff"],
                "index.patch": ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
                "untracked.list": [
                    "git", "ls-files", "--others", "--exclude-standard", "-z"
                ],
            }
            outputs = {}
            for filename, argv in commands.items():
                result = subprocess.run(
                    argv, cwd=repo, capture_output=True, check=False, timeout=60.0
                )
                if result.returncode != 0:
                    return _fail("wip_snapshot_failed", workspace_id)
                path = target / filename
                path.write_bytes(result.stdout)
                path.chmod(0o600)
                outputs[filename] = _sha256(path)
            index_path_result = _run(
                ["git", "rev-parse", "--git-path", "index"], timeout=30.0, cwd=repo
            )
            if index_path_result.returncode != 0:
                return _fail("wip_index_unreadable", workspace_id)
            index_path = Path(index_path_result.stdout.strip())
            if not index_path.is_absolute():
                index_path = repo / index_path
            index_copy = target / "git-index"
            index_copy.write_bytes(index_path.read_bytes())
            index_copy.chmod(0o600)
            outputs["git-index"] = _sha256(index_copy)
            untracked = (target / "untracked.list").read_bytes().split(b"\0")
            archive = target / "untracked.tar"
            with tarfile.open(archive, "w", dereference=False) as tar:
                for raw in untracked:
                    if not raw:
                        continue
                    relative = os.fsdecode(raw)
                    full = repo / relative
                    tar.add(full, arcname=relative, recursive=False)
            archive.chmod(0o600)
            outputs["untracked.tar"] = _sha256(archive)
            manifest = {
                "workspace_id": workspace_id,
                "wip_digest": current.digest,
                "files": outputs,
            }
            manifest_path.write_bytes(canonical_bytes(manifest) + b"\n")
            manifest_path.chmod(0o600)
        return _ok(wip_snapshots_verified=True)

    def _stop_agents(self, phase, action, action_directory):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            HerdrRetireClosePlan,
            execute_herdr_retire_close,
        )

        names = tuple(phase.get("assigned_names", ()))
        bindings = {
            row["assigned_name"]: row for row in self._bindings(action)["agents"]
        }
        settled = self._wait_for_settled_empty(action, names, bindings)
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
        for name in names:
            view = self._inventory(action)
            if not view.ok or view.invalid_row_count != 0 or view.unmanaged_agents:
                return _fail("inventory_unreadable")
            matches = [agent for agent in view.managed_agents if agent.name == name]
            if not matches:
                continue
            if len(matches) != 1 or matches[0].locator != bindings[name]["locator"]:
                return _fail("agent_generation_drift", name)
            result = execute_herdr_retire_close(
                HerdrRetireClosePlan(
                    workspace_id=bindings[name]["workspace_id"],
                    lane_id=bindings[name]["lane_id"],
                    close_targets=((bindings[name]["provider"], bindings[name]["locator"]),),
                    foreign_names=(),
                ),
                env=self.env,
            )
            if result.failed:
                return _fail("agent_close_failed", name)
        view = self._inventory(action)
        live = {agent.name for agent in view.managed_agents} if view.ok else set(names)
        if any(name in live for name in names):
            return _fail("agent_stop_unverified")
        return _ok(stopped_assigned_names=sorted(names), wip_preserved=True)

    def _wait_for_settled_empty(self, action, names, bindings):
        """Wait boundedly for every planned target to become idle with no composer debt."""
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
            LiveSessionRetireOps,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
            RUNTIME_AWAITING_INPUT,
            RUNTIME_TURN_ENDED,
        )

        if not names:
            return _ok(targets_settled=True)
        deadline = time.monotonic() + 600.0
        paths = self._bindings(action)["workspace_paths"]
        while True:
            view = self._inventory(action)
            if not view.ok or view.invalid_row_count != 0 or view.unmanaged_agents:
                return _fail("inventory_unreadable")
            current = {agent.name: agent for agent in view.managed_agents}
            pending = []
            for name in names:
                agent = current.get(name)
                if agent is None:
                    continue
                binding = bindings.get(name)
                if binding is None or agent.locator != binding["locator"]:
                    return _fail("agent_generation_drift", name)
                if agent.runtime_state not in (
                    RUNTIME_AWAITING_INPUT,
                    RUNTIME_TURN_ENDED,
                ):
                    pending.append(name)
                    continue
                observer = LiveSessionRetireOps(
                    repo_root=Path(paths[binding["workspace_id"]]), env=self.env
                )
                readable, has_pending = observer.observe_composer(agent.locator)
                if not readable or has_pending is not False:
                    pending.append(name)
            if not pending:
                return _ok(targets_settled=True)
            if time.monotonic() >= deadline:
                return _fail("agents_not_settled", ",".join(sorted(pending)))
            time.sleep(2.0)

    def _consumer_zero(self, _phase, action, _directory):
        view = self._inventory(action)
        if (
            not view.ok
            or view.invalid_row_count != 0
            or view.unmanaged_agents
            or view.managed_agents
            or view.raw_row_count != 0
        ):
            return _fail("consumer_zero_unverified")
        return _ok(consumer_count=0)

    def _fresh_store_records(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_snapshot import (  # noqa: E501
            _store_snapshots,
        )

        return {row.name: row.to_record() for row in _store_snapshots(self.home)}

    def _require_store_phase_authority(
        self, action, *, store_name, phase_name, replaying
    ):
        """Re-check the approved store immediately before its migration effect.

        A byte-exact predecessor is the normal first attempt.  The target version is
        accepted only while replaying this action's durably active phase after its
        verified-backup phase; this is the narrow crash window between the migration
        commit and its action receipt.  No general "already current" shortcut exists.
        """
        observed = self._fresh_store_records().get(store_name)
        planned = action["plan"]["stores"][store_name]
        if observed == planned:
            return _ok(store_authority="planned_predecessor")
        if (
            isinstance(observed, Mapping)
            and observed.get("state") == "recognized"
            and observed.get("version") == planned.get("target_version")
            and replaying is True
            and action.get("active_phase") == phase_name
            and "verified_backup" in action.get("completed_phases", ())
        ):
            return _ok(store_authority="active_phase_replay")
        return _fail(f"{store_name}_plan_drift")

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
        backup_root.mkdir(mode=0o700, exist_ok=True)
        attestation = backup_attestation_store(herdr_identity_attestation_path(self.home))
        state = backup_state_container(lane_lifecycle_path(self.home))
        startup = backup_root / "startup-preflight"
        if not startup.exists():
            fence = StartupTransactionFence(home=self.home)
            with fence._hold():  # noqa: SLF001 - migration authority's external lock
                with fence._connection("ro") as conn:  # noqa: SLF001
                    staging = startup.with_name(startup.name + ".staging")
                    stage_recovery_artifact(fence, conn, staging)
                    publish_recovery_artifact(staging, startup)
        startup_digest = artifact_digest_of(startup)
        if not startup_digest:
            return _fail("startup_backup_readback_failed")
        return _ok(
            attestation_backup=bool(attestation),
            state_backup=bool(state),
            startup_backup=True,
            startup_backup_digest=startup_digest,
        )

    def _migrate_attestation(
        self, _phase, action, _directory, *, replaying=False
    ):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            herdr_identity_attestation_path,
        )
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
            migrate_attestation_store,
            probe_store_schema,
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
        result = migrate_attestation_store(path)
        observed = probe_store_schema(path)
        if observed.version != HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION:
            return _fail("attestation_migration_unverified")
        return _ok(outcome=result.outcome, to_version=observed.version)

    def _migrate_lane_lifecycle(
        self, _phase, action, _directory, *, replaying=False
    ):
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            probe_lane_lifecycle_schema,
        )
        from mozyo_bridge.core.state.lane_lifecycle_schema import (
            LANE_LIFECYCLE_SCHEMA_VERSION,
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
        outcome = ensure_lane_lifecycle_schema(lane_lifecycle_path(self.home))
        observed = probe_lane_lifecycle_schema(home=self.home)
        if observed.version != LANE_LIFECYCLE_SCHEMA_VERSION:
            return _fail("lane_lifecycle_migration_unverified")
        return _ok(action=outcome.action, to_version=observed.version)

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
        result = migrate_startup_store_v1_to_v2(
            fence,
            backup_path=artifact,
            expected_plan_digest=expected,
            completion_receipt=completion,
        )
        if result.schema_version != 2:
            return _fail("startup_migration_unverified")
        return _ok(
            outcome=result.outcome,
            to_version=result.schema_version,
            artifact_digest=result.artifact_digest or completion.artifact_digest,
        )

    def _exact_runtime_install(self, _phase, action, _directory):
        bindings = self._bindings(action)
        runner = bindings["runner"]
        wheel = Path(runner["wheel"])
        artifact = action["plan"]["candidate_artifact"]
        if _sha256(wheel) != artifact["wheel_sha256"]:
            return _fail("candidate_wheel_digest_mismatch")
        clean_env = _sanitized_runtime_env(self.env)
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

    def _restore_agents(self, phase, action, _directory):
        names = set(phase.get("assigned_names", ()))
        bindings = self._bindings(action)
        agents = [row for row in bindings["agents"] if row["assigned_name"] in names]
        groups = {}
        for row in agents:
            groups.setdefault((row["workspace_id"], row["lane_id"]), []).append(row)
        action_ids = []
        for (workspace_id, lane_id), rows in sorted(groups.items()):
            argv = [
                bindings["target_cli"],
                "herdr",
                "session-start",
                "--repo",
                bindings["workspace_paths"][workspace_id],
                "--json",
            ]
            if lane_id != "default":
                argv += ["--lane", lane_id]
            for row in sorted(rows, key=lambda item: item["provider"]):
                argv += ["--agent", row["provider"]]
            result = _run(
                argv,
                timeout=_INSTALL_TIMEOUT,
                env=_sanitized_runtime_env(self.env),
            )
            if result.returncode != 0:
                return _fail("workspace_restore_failed", _bounded(result))
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                return _fail("workspace_restore_payload_invalid")
            if payload.get("ok") is not True:
                return _fail("workspace_restore_unhealthy")
            if payload.get("action_id"):
                action_ids.append(payload["action_id"])
        verified = self._verify_live_names(action, names)
        if not verified.ok:
            return verified
        return _ok(restored_assigned_names=sorted(names), startup_action_ids=action_ids)

    def _verify_live_names(self, action, names):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
            VERDICT_PRESENT,
        )

        view = self._inventory(action)
        if not view.ok or view.invalid_row_count != 0 or view.unmanaged_agents:
            return _fail("restore_inventory_unreadable")
        live = {agent.name: agent for agent in view.managed_agents}
        store = HerdrIdentityAttestationStore(home=self.home)
        for name in names:
            agent = live.get(name)
            record = store.read(name)
            if (
                agent is None
                or not agent.locator
                or record is None
                or record.verdict != VERDICT_PRESENT
                or record.locator != agent.locator
            ):
                return _fail("restore_attestation_unverified", name)
        return _ok(live_names_verified=True)

    def _supervisor_install(self, _phase, action, _directory):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd import (  # noqa: E501
            install_pair,
        )

        target_cli = self._bindings(action)["target_cli"]

        def which(name):
            return target_cli if name == "mozyo-bridge" else shutil.which(name)

        result = install_pair(mozyo_home=self.home, which=which)
        if not result.get("performed"):
            return _fail("supervisor_install_failed", str(result.get("reason") or ""))
        return _ok(supervisors_installed=True)

    def _supervisor_readback(self, _phase, action, _directory):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd import (  # noqa: E501
            CREDENTIAL_READY,
            HOME_PIN_OK,
            service_status_pair,
        )

        target_cli = self._bindings(action)["target_cli"]

        def which(name):
            return target_cli if name == "mozyo-bridge" else shutil.which(name)

        status = service_status_pair(mozyo_home=self.home, which=which)
        agents = status.get("agents", ())
        healthy = len(agents) == 2 and all(
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
        live = self._verify_live_names(action, expected)
        if not live.ok:
            return live
        stores = self._fresh_store_records()
        targets = {"attestation": 3, "lane_lifecycle": 10, "startup_transaction": 2}
        if any(stores[name]["version"] != version for name, version in targets.items()):
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
