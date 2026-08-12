"""Regression surface for the external offline-rollout execution rail (#14838)."""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import io
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_offline_rollout import (  # noqa: E501
    cmd_herdr_offline_rollout_run,
    register_herdr_offline_rollout_parser,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_executor import (  # noqa: E501
    RUNNER_ENV,
    LiveOfflineRolloutExecutionPort,
    _reports_exact_version,
    _sanitized_runtime_env,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild import (  # noqa: E501
    backup_launch_generation,
    rebuild_launch_generation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_supervisor_stop import (  # noqa: E501
    supervisor_stop_refusal,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_runner import (  # noqa: E501
    OfflineRolloutRunnerBindingError,
    capture_provider_launch_bindings,
    validate_provider_launch_bindings,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    OFFLINE_ROLLOUT_APPROVAL_GATE,
    render_approval_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    check_issuer_resolution,
    contract_ruling_pointer,
    contract_writer_role,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
    config_policy_pointer,
    resolve_journal_issuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    read_live_worktree_fingerprint,
)


class OfflineRolloutExecutionRegressionTests(unittest.TestCase):
    SUPERVISOR_LABEL = "org.mozyo-bridge.callback-supervisor"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"

    def tearDown(self) -> None:
        # Several legacy SQLite migration primitives expose connection lifetimes to GC.  Force
        # collection inside this test boundary so a later warning-sensitive test cannot inherit
        # our fixture's delayed ResourceWarning (the leak itself predates this execution rail).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()

    def test_supervisor_stop_requires_complete_current_and_legacy_evidence(self) -> None:
        stopped_status = {
            "backend": "launchd",
            "agents": [
                {
                    "label": self.SUPERVISOR_LABEL,
                    "installed": False,
                    "loaded": False,
                    "legacy_drain": "absent",
                }
            ],
        }
        complete = {
            "backend": "launchd",
            "label": self.SUPERVISOR_LABEL,
            "performed": True,
            "effect_state": "complete",
            "legacy_drain": "owned",
            "legacy_drain_removed": True,
            "legacy_drain_reason": "",
        }
        self.assertEqual(
            supervisor_stop_refusal(
                complete, stopped_status, expected_label=self.SUPERVISOR_LABEL
            ),
            "",
        )
        cases = (
            (
                {
                    **complete,
                    "legacy_drain": "absent",
                    "legacy_drain_removed": False,
                },
                stopped_status,
                "supervisor_legacy_stop_unverified",
            ),
            (
                {
                    **complete,
                    "effect_state": "partial",
                    "legacy_drain": "owned",
                    "legacy_drain_reason": "legacy_drain_state_unreadable",
                },
                stopped_status,
                "supervisor_uninstall_incomplete",
            ),
            (
                {
                    **complete,
                    "effect_state": "partial",
                    "legacy_drain": "owned",
                    "legacy_drain_reason": "legacy_drain_removal_failed",
                },
                stopped_status,
                "supervisor_uninstall_incomplete",
            ),
            (
                complete,
                {
                    "backend": "launchd",
                    "agents": [
                        {"label": self.SUPERVISOR_LABEL, "legacy_drain": "absent"}
                    ],
                },
                "supervisor_current_stop_unverified",
            ),
            (
                complete,
                {
                    "backend": "launchd",
                    "agents": [
                        {
                            "label": self.SUPERVISOR_LABEL,
                            "installed": False,
                            "loaded": False,
                            "legacy_drain": "owned",
                        }
                    ],
                },
                "supervisor_legacy_stop_unverified",
            ),
        )
        for result, status, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    supervisor_stop_refusal(
                        result, status, expected_label=self.SUPERVISOR_LABEL
                    ),
                    expected,
                )

        wrong_row = {
            **stopped_status,
            "agents": [{**stopped_status["agents"][0], "label": "foreign"}],
        }
        self.assertEqual(
            supervisor_stop_refusal(
                complete, wrong_row, expected_label=self.SUPERVISOR_LABEL
            ),
            "supervisor_stop_label_drift",
        )

    def test_live_stop_phase_refuses_before_following_offline_phase_on_legacy_failure(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_service_backend,
        )

        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        status = {
            "backend": "launchd",
            "agents": [
                {
                    "label": self.SUPERVISOR_LABEL,
                    "installed": False,
                    "loaded": False,
                    "legacy_drain": "owned",
                }
            ],
        }
        for reason in (
            "legacy_drain_state_unreadable",
            "legacy_drain_removal_failed",
        ):
            result = {
                "backend": "launchd",
                "label": self.SUPERVISOR_LABEL,
                "performed": True,
                "effect_state": "partial",
                "reason": "",
                "legacy_drain": "owned",
                "legacy_drain_reason": reason,
            }
            with (
                patch.object(supervisor_service_backend, "uninstall", return_value=result),
                patch.object(supervisor_service_backend, "service_status", return_value=status),
            ):
                outcome = port._supervisor_stop(  # noqa: SLF001
                    {"supervisor_labels": [self.SUPERVISOR_LABEL]}, {}, self.home
                )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.reason, "supervisor_stop_unverified")
            self.assertEqual(outcome.detail, "supervisor_uninstall_incomplete")

        absent_without_manager_stop = {
            "backend": "launchd",
            "label": self.SUPERVISOR_LABEL,
            "performed": True,
            "effect_state": "complete",
            "reason": "",
            "legacy_drain": "absent",
            "legacy_drain_removed": False,
            "legacy_drain_reason": "",
        }
        stopped_status = {
            "backend": "launchd",
            "agents": [
                {
                    "label": self.SUPERVISOR_LABEL,
                    "installed": False,
                    "loaded": False,
                    "legacy_drain": "absent",
                }
            ],
        }
        with (
            patch.object(
                supervisor_service_backend,
                "uninstall",
                return_value=absent_without_manager_stop,
            ),
            patch.object(
                supervisor_service_backend,
                "service_status",
                return_value=stopped_status,
            ),
        ):
            outcome = port._supervisor_stop(  # noqa: SLF001
                {"supervisor_labels": [self.SUPERVISOR_LABEL]}, {}, self.home
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "supervisor_stop_unverified")
        self.assertEqual(outcome.detail, "supervisor_legacy_stop_unverified")

    def test_run_requires_execute_before_any_action_store_read(self) -> None:
        args = argparse.Namespace(
            execute=False,
            action_id="offline_" + "a" * 32,
            home=str(self.home),
            json=False,
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cmd_herdr_offline_rollout_run(args)
        self.assertEqual(code, 1)
        self.assertIn("--execute is required", stderr.getvalue())
        self.assertFalse(self.home.exists())

    def test_only_exact_unmanaged_launchd_runner_token_is_admitted(self) -> None:
        action_id = "offline_" + "b" * 32
        port = LiveOfflineRolloutExecutionPort(
            home=self.home, env={RUNNER_ENV: action_id}
        )
        binding = {
            "private_bindings": {
                "runner": {"launchd_label": port._runner_label(action_id)}  # noqa: SLF001
            }
        }
        completed = subprocess.CompletedProcess([], 0, stdout="job = ready\n", stderr="")
        with (
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_executor.sys.platform",
                "darwin",
            ),
            patch(
                "mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events."
                "infrastructure.offline_rollout_action_store.OfflineRolloutActionStore.load",
                return_value=binding,
            ),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_executor._run",
                return_value=completed,
            ),
        ):
            admitted = port.attest_external_runner(action_id=action_id)
        self.assertTrue(admitted.ok)
        wrong = LiveOfflineRolloutExecutionPort(
            home=self.home, env={RUNNER_ENV: "offline_" + "c" * 32}
        ).attest_external_runner(action_id=action_id)
        self.assertFalse(wrong.ok)
        managed = LiveOfflineRolloutExecutionPort(
            home=self.home,
            env={
                RUNNER_ENV: action_id,
                "MOZYO_AGENT_ROLE": "codex",
                "MOZYO_WORKSPACE_ID": "ws",
            },
        ).attest_external_runner(action_id=action_id)
        self.assertFalse(managed.ok)
        self.assertEqual(managed.reason, "runner_is_managed_consumer")

    def test_candidate_provenance_is_exact_and_source_env_is_removed(self) -> None:
        self.assertTrue(_reports_exact_version("mozyo-bridge 0.15.0a4\n", "0.15.0a4"))
        for output in (
            "mozyo-bridge 0.15.0a41\n",
            "mozyo-bridge 0.15.0\n",
            "prefix mozyo-bridge 0.15.0a4\n",
            "mozyo-bridge 0.15.0a4\nextra\n",
        ):
            self.assertFalse(_reports_exact_version(output, "0.15.0a4"))
        clean = _sanitized_runtime_env(
            {
                "PATH": "/bin",
                "PYTHONPATH": "/source",
                "PYTHONHOME": "/source/home",
                "VIRTUAL_ENV": "/source/venv",
            }
        )
        self.assertEqual(clean, {"PATH": "/bin"})

    def test_provider_executables_are_sealed_before_stop_and_exactly_revalidated(self) -> None:
        binary_dir = Path(self.temp.name) / "provider-bin"
        binary_dir.mkdir()
        for name in ("claude", "codex"):
            path = binary_dir / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        agents = (
            {"provider": "codex"},
            {"provider": "claude"},
            {"provider": "codex"},
        )

        captured = capture_provider_launch_bindings(
            agents=agents, env={"PATH": str(binary_dir)}
        )
        self.assertEqual(
            {key: value["argv0"] for key, value in captured.items()},
            {
                "MOZYO_AGENT_CLAUDE_BINARY": str(binary_dir / "claude"),
                "MOZYO_AGENT_CODEX_BINARY": str(binary_dir / "codex"),
            },
        )
        self.assertEqual(
            validate_provider_launch_bindings(agents=agents, bindings=captured),
            {key: value["argv0"] for key, value in captured.items()},
        )
        for invalid in (
            {"MOZYO_AGENT_CODEX_BINARY": captured["MOZYO_AGENT_CODEX_BINARY"]},
            {
                **captured,
                "MOZYO_AGENT_FOREIGN_BINARY": captured["MOZYO_AGENT_CODEX_BINARY"],
            },
            {
                **captured,
                "MOZYO_AGENT_CODEX_BINARY": {
                    **captured["MOZYO_AGENT_CODEX_BINARY"],
                    "argv0": "codex",
                },
            },
        ):
            with self.assertRaises(OfflineRolloutRunnerBindingError):
                validate_provider_launch_bindings(agents=agents, bindings=invalid)

        codex_alias = binary_dir / "codex"
        codex_target_b = binary_dir / "codex-target-b"
        codex_target_b.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex_target_b.chmod(0o755)
        codex_alias.unlink()
        codex_alias.symlink_to(codex_target_b)
        with self.assertRaisesRegex(
            OfflineRolloutRunnerBindingError, "provider_executable_drift"
        ):
            validate_provider_launch_bindings(agents=agents, bindings=captured)

    def test_launchd_runner_receives_only_the_sealed_provider_bindings(self) -> None:
        action_id = "offline_" + "e" * 32
        action_directory = Path(self.temp.name) / "action"
        cli = action_directory / "runner" / "venv" / "bin" / "mozyo-bridge"
        cli.parent.mkdir(parents=True)
        cli.write_text("runner\n", encoding="utf-8")
        herdr = Path(self.temp.name) / "herdr"
        herdr.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        herdr.chmod(0o755)
        binary_dir = Path(self.temp.name) / "provider-bin"
        binary_dir.mkdir()
        for name in ("claude", "codex"):
            path = binary_dir / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        agents = ({"provider": "claude"}, {"provider": "codex"})
        provider_bindings = capture_provider_launch_bindings(
            agents=agents, env={"PATH": str(binary_dir)}
        )
        provider_environment = {
            key: value["argv0"] for key, value in provider_bindings.items()
        }
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        action = {
            "private_bindings": {
                "agents": list(agents),
                "herdr_binary": str(herdr),
                "provider_executable_bindings": provider_bindings,
                "runner": {"launchd_label": port._runner_label(action_id)},  # noqa: SLF001
            }
        }
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        load_path = (
            "mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events."
            "infrastructure.offline_rollout_action_store.OfflineRolloutActionStore.load"
        )
        with (
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_executor.sys.platform",
                "darwin",
            ),
            patch(load_path, return_value=action),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_executor._run",
                return_value=completed,
            ) as invoked,
        ):
            launched = port.launch_external_runner(
                action_id=action_id, action_directory=action_directory
            )
        self.assertTrue(launched.ok, launched)
        runner_env = plistlib.loads(
            (action_directory / "runner.plist").read_bytes()
        )["EnvironmentVariables"]
        self.assertEqual(
            {key: runner_env[key] for key in provider_environment},
            provider_environment,
        )
        self.assertNotIn("PATH", runner_env)
        self.assertEqual(invoked.call_count, 2)

        invalid = {
            "private_bindings": {
                **action["private_bindings"],
                "provider_executable_bindings": {
                    "MOZYO_AGENT_CODEX_BINARY": provider_bindings[
                        "MOZYO_AGENT_CODEX_BINARY"
                    ]
                },
            }
        }
        with (
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_executor.sys.platform",
                "darwin",
            ),
            patch(load_path, return_value=invalid),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_executor._run"
            ) as refused_run,
        ):
            refused = port.launch_external_runner(
                action_id=action_id, action_directory=action_directory
            )
        self.assertFalse(refused.ok)
        self.assertEqual(refused.reason, "agent_provider_binary_binding_invalid")
        refused_run.assert_not_called()

    def test_global_window_adopts_one_exact_legacy_lane_and_replay_is_idempotent(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            DISPOSITION_ACTIVE,
            DISPOSITION_HIBERNATED,
            DecisionPointer,
            LaneLifecycleKey,
        )

        issue = "13842"
        journal = "79411"
        decision = DecisionPointer("redmine", issue, journal)
        key = LaneLifecycleKey("ws_recovery", "issue_13842_recovery")
        lifecycle = LaneLifecycleStore(home=self.home)
        declared = lifecycle.declare_active(key, decision=decision, issue_id=issue)
        moved = lifecycle.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=declared.revision,
            target=DISPOSITION_HIBERNATED,
            decision=decision,
        )
        self.assertTrue(moved.applied, moved.reason)
        with sqlite3.connect(lifecycle.path) as conn:
            conn.execute(
                "UPDATE lane_lifecycle_records "
                "SET process_release = 'released', lane_epoch = '0' "
                "WHERE repo_workspace_id = ? AND lane_id = ?",
                key.as_row(),
            )
        legacy = lifecycle.get(key)
        target = {
            "issue_id": issue,
            "journal_id": journal,
            "workspace_id": key.repo_workspace_id,
            "lane_id": key.lane_id,
            "expected_revision": legacy.revision,
            "from_epoch": 0,
            "to_epoch": 1,
            "agents": [],
        }
        phase = {"phase": "legacy_lane_epoch_adoption", "targets": []}
        action = {"plan": {"legacy_recoveries": [target]}}
        port = LiveOfflineRolloutExecutionPort(home=self.home)

        class AdmittedFence:
            @staticmethod
            def before_effect(_action, _phase):
                return PhaseExecutionResult(True)

        with patch.object(port, "_phase_fence", return_value=AdmittedFence()):
            first = port.execute_phase(
                phase=phase,
                action=action,
                action_directory=self.home / "private",
                replaying=False,
            )
        self.assertTrue(first.ok, first)
        adopted = lifecycle.get(key)
        self.assertEqual(adopted.lane_epoch, "1")
        self.assertEqual(adopted.revision, legacy.revision + 1)
        with patch.object(port, "_phase_fence", return_value=AdmittedFence()):
            replay = port.execute_phase(
                phase=phase,
                action=action,
                action_directory=self.home / "private",
                replaying=True,
            )
        self.assertTrue(replay.ok, replay)
        self.assertEqual(lifecycle.get(key).revision, legacy.revision + 1)

    def test_legacy_recovery_launch_uses_bound_lane_worktree_not_workspace_root(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_restore import (  # noqa: E501
            OfflineRolloutRestoreExecutor,
        )

        bindings = {
            "workspace_paths": {"ws": "/private/workspace-root"},
            "legacy_recovery_worktree_paths": {
                "legacy:13842": "/private/lane-worktree"
            },
        }
        group = SimpleNamespace(
            recovery_issue_id="13842", workspace_id="ws"
        )
        repo = OfflineRolloutRestoreExecutor._group_repo(bindings, group)  # noqa: SLF001
        self.assertEqual(repo, Path("/private/lane-worktree"))
        self.assertNotEqual(repo, Path("/private/workspace-root"))

    def test_owner_approval_gate_uses_anchored_coordinator_policy(self) -> None:
        manifest = {"plan_digest": "a" * 64, "global_stop": True}
        marker = render_approval_note(manifest, "14838")
        issuer = resolve_journal_issuer(
            "99", marker, policy_pointer=config_policy_pointer("d" * 40)
        )
        self.assertEqual(contract_writer_role(OFFLINE_ROLLOUT_APPROVAL_GATE), "coordinator")
        self.assertIn("redmine:#14838:j#97993", contract_ruling_pointer(OFFLINE_ROLLOUT_APPROVAL_GATE))
        self.assertIsNone(check_issuer_resolution(OFFLINE_ROLLOUT_APPROVAL_GATE, issuer))
        self.assertIn("evidence:redmine:j#99", issuer.authority_anchor)

    def test_live_owner_verifier_requires_exact_marker_and_resolved_policy(self) -> None:
        manifest = {"plan_digest": "a" * 64, "global_stop": True}
        marker = render_approval_note(manifest, "14838")
        entry = RedmineJournalEntry("14838", "99", marker, author_id="same-user")

        class Source:
            def read_entries(self, issue):
                return [entry] if issue == "14838" else []

        port = LiveOfflineRolloutExecutionPort(
            home=self.home, repo_root=Path(self.temp.name), env={}
        )
        with (
            patch(
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                "application.live_redmine_journal_source.LiveRedmineJournalSource.from_environment",
                return_value=Source(),
            ),
            patch(
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                "application.hibernate_lane_topology.committed_config_policy_pointer",
                return_value=config_policy_pointer("d" * 40),
            ),
        ):
            verified = port.verify_owner_approval(
                issue="14838", journal="99", manifest=manifest
            )
        self.assertTrue(verified.ok, verified)

        quoted = RedmineJournalEntry("14838", "99", f"`{marker}`", author_id="same-user")
        entry = quoted
        with (
            patch(
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                "application.live_redmine_journal_source.LiveRedmineJournalSource.from_environment",
                return_value=Source(),
            ),
            patch(
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                "application.hibernate_lane_topology.committed_config_policy_pointer",
                return_value=config_policy_pointer("d" * 40),
            ),
        ):
            refused = port.verify_owner_approval(
                issue="14838", journal="99", manifest=manifest
            )
        self.assertFalse(refused.ok)

    def test_cli_has_no_unarmed_implicit_execution(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="root", required=True)
        register_herdr_offline_rollout_parser(
            sub, add_repo_option=lambda target: target.add_argument("--repo")
        )
        delegated = parser.parse_args(
            [
                "offline-rollout",
                "delegate",
                "--candidate-version",
                "0.15.0a4",
                "--plan-digest",
                "d" * 64,
                "--owner-approval",
                "14838:99999",
            ]
        )
        self.assertFalse(delegated.execute)
        run = parser.parse_args(
            ["offline-rollout", "run", "--action-id", "offline_" + "d" * 32]
        )
        self.assertFalse(run.execute)
        armed = parser.parse_args(
            [
                "offline-rollout",
                "run",
                "--action-id",
                "offline_" + "d" * 32,
                "--execute",
            ]
        )
        self.assertTrue(armed.execute)

    def test_private_wip_snapshot_preserves_index_tracked_and_untracked_bytes(self) -> None:
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        (repo / "tracked.txt").write_text("working\n", encoding="utf-8")
        (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True)
        (repo / "untracked.bin").write_bytes(b"\x00private-wip\xff")
        fingerprint = read_live_worktree_fingerprint(repo, 10.0)
        self.assertTrue(fingerprint.readable)

        action_directory = Path(self.temp.name) / "action"
        action_directory.mkdir(mode=0o700)
        action = {
            "plan": {
                "workspaces": [
                    {
                        "workspace_id": "ws",
                        "wip": {
                            "dirty": True,
                            "untracked": True,
                            "digest": fingerprint.digest,
                        },
                    }
                ]
            },
            "private_bindings": {"workspace_paths": {"ws": str(repo)}},
        }
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        result = port._ensure_wip_snapshots(action, action_directory)  # noqa: SLF001
        self.assertTrue(result.ok, result)
        snapshot = action_directory / "wip" / hashlib.sha256(b"ws").hexdigest()
        for name in (
            "worktree.patch",
            "index.patch",
            "git-index",
            "untracked.list",
            "untracked.tar",
            "manifest.json",
        ):
            self.assertTrue((snapshot / name).is_file(), name)
        self.assertIn(b"private-wip", (snapshot / "untracked.tar").read_bytes())

        (repo / "tracked.txt").write_text("drifted\n", encoding="utf-8")
        drifted = port._ensure_wip_snapshots(action, action_directory)  # noqa: SLF001
        self.assertFalse(drifted.ok)
        self.assertEqual(drifted.reason, "wip_drift")

    def test_store_migration_requires_exact_plan_or_active_phase_replay(self) -> None:
        planned = {
            "state": "recognized",
            "version": 1,
            "target_version": 3,
            "upgrade_required": True,
            "content_digest": "a" * 64,
            "migration_plan_digest": "",
        }
        action = {
            "plan": {"stores": {"attestation": planned}},
            "completed_phases": ["verified_backup"],
            "active_phase": "migrate_attestation",
            "phase_receipts": {
                "verified_backup": {
                    "migration_post_digests": {"attestation": "b" * 64}
                }
            },
        }
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        with patch.object(port, "_fresh_store_records", return_value={"attestation": planned}):
            first = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="attestation",
                phase_name="migrate_attestation",
                replaying=False,
            )
        self.assertTrue(first.ok)
        self.assertEqual(first.receipt["store_authority"], "planned_predecessor")

        target = dict(planned, version=3, upgrade_required=False, content_digest="b" * 64)
        with patch.object(port, "_fresh_store_records", return_value={"attestation": target}):
            replay = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="attestation",
                phase_name="migrate_attestation",
                replaying=True,
            )
        self.assertTrue(replay.ok)
        self.assertEqual(
            replay.receipt["store_authority"], "exact_post_digest_replay"
        )

        drifted_target = dict(target, content_digest="c" * 64)
        with patch.object(
            port,
            "_fresh_store_records",
            return_value={"attestation": drifted_target},
        ):
            drifted_replay = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="attestation",
                phase_name="migrate_attestation",
                replaying=True,
            )
        self.assertFalse(drifted_replay.ok)
        self.assertEqual(drifted_replay.reason, "attestation_plan_drift")

        with patch.object(port, "_fresh_store_records", return_value={"attestation": target}):
            refused = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="attestation",
                phase_name="migrate_attestation",
                replaying=False,
            )
        self.assertFalse(refused.ok)
        self.assertEqual(refused.reason, "attestation_plan_drift")

    def test_generation_rebuild_refuses_bool_and_float_planned_versions(self) -> None:
        backup_root = Path(self.temp.name) / "backup-numeric"
        backup_root.mkdir()
        for version in (True, 1.0, 2.0):
            planned = {
                "state": "recognized", "version": version,
                "target_version": 2, "upgrade_required": True,
                "content_digest": "a" * 64, "migration_plan_digest": "",
            }
            observed = dict(planned, version=int(version))
            with self.subTest(version=version):
                backed = backup_launch_generation(
                    home=self.home, backup_root=backup_root, planned=planned,
                    observe=lambda: observed,
                )
                self.assertFalse(backed.ok)
                self.assertEqual(backed.reason, "launch_generation_plan_drift")
                rebuilt = rebuild_launch_generation(
                    home=self.home, backup_root=backup_root, planned=planned,
                    observe=lambda: observed, backup_receipt={}, replaying=True,
                )
                self.assertFalse(rebuilt.ok)
                self.assertEqual(rebuilt.reason, "launch_generation_plan_drift")

    def test_startup_replay_authority_names_deferred_completion_check(self) -> None:
        planned = {
            "state": "recognized",
            "version": 1,
            "target_version": 2,
            "upgrade_required": True,
            "content_digest": "a" * 64,
            "migration_plan_digest": "b" * 64,
        }
        target = dict(planned, version=2, upgrade_required=False)
        action = {
            "plan": {"stores": {"startup_transaction": planned}},
            "completed_phases": ["verified_backup"],
            "active_phase": "migrate_startup_transaction",
        }
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        with patch.object(
            port,
            "_fresh_store_records",
            return_value={"startup_transaction": target},
        ):
            authority = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="startup_transaction",
                phase_name="migrate_startup_transaction",
                replaying=True,
            )
        self.assertTrue(authority.ok)
        self.assertEqual(
            authority.receipt["store_authority"],
            "startup_completion_check_deferred_to_primitive",
        )

    def test_real_private_home_migrates_four_stores_and_replays(self) -> None:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            herdr_identity_attestation_path,
        )
        from mozyo_bridge.core.state.lane_lifecycle_schema import (
            ensure_lane_lifecycle_schema,
            lane_lifecycle_path,
        )
        from mozyo_bridge.core.state.startup_transaction_fence import (
            StartupTransactionFence,
            StartupUnit,
        )
        from mozyo_bridge.core.state.herdr_launch_generation import (
            HerdrLaunchGenerationStore,
            herdr_launch_generation_path,
        )

        attestation = herdr_identity_attestation_path(self.home)
        attestation.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(attestation) as conn:
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                "CREATE TABLE herdr_identity_attestations ("
                "assigned_name TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                "role TEXT NOT NULL, lane_id TEXT NOT NULL, locator TEXT NOT NULL, "
                "verdict TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', "
                "observed_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO herdr_identity_attestations VALUES (?,?,?,?,?,?,?,?)",
                ("mzb1_ws_codex_default", "ws", "codex", "default", "w1:p1", "present", "", "t0"),
            )
        ensure_lane_lifecycle_schema(lane_lifecycle_path(self.home))
        generation_path = herdr_launch_generation_path(self.home)
        with sqlite3.connect(generation_path) as conn:
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                "CREATE TABLE herdr_launch_generations ("
                "assigned_name TEXT NOT NULL PRIMARY KEY, startup_action_id TEXT NOT NULL, "
                "phase TEXT NOT NULL, workspace_id TEXT NOT NULL, role TEXT NOT NULL, "
                "lane_id TEXT NOT NULL, locator TEXT NOT NULL DEFAULT '', "
                "verdict TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL DEFAULT '', "
                "reserved_at TEXT NOT NULL, attested_at TEXT NOT NULL DEFAULT '')"
            )
        generation_path.chmod(0o600)
        startup = StartupTransactionFence(home=self.home)
        startup.reserve(StartupUnit("ws", "default", ("codex",)), "nonce")

        action_directory = Path(self.temp.name) / "action"
        action_directory.mkdir(mode=0o700)
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        edge_patch = patch.object(
            port,
            "_require_effect_edge",
            return_value=PhaseExecutionResult(True),
        )
        edge_patch.start()
        self.addCleanup(edge_patch.stop)
        original = port._fresh_store_records()  # noqa: SLF001
        self.assertEqual(
            {name: row["version"] for name, row in original.items()},
            {"attestation": 1, "lane_lifecycle": 11,
             "launch_generation": 1, "startup_transaction": 1},
        )
        action = {
            "action_id": "offline_" + "e" * 32,
            "plan": {"stores": original},
            "completed_phases": [],
            "active_phase": "verified_backup",
        }
        backup = port._verified_backup({}, action, action_directory)  # noqa: SLF001
        self.assertTrue(backup.ok, backup)
        self.assertTrue(backup.receipt["startup_backup_digest"])
        self.assertTrue(backup.receipt["launch_generation_backup"])
        self.assertEqual(
            set(backup.receipt["migration_post_digests"]),
            {"attestation", "lane_lifecycle"},
        )

        action["completed_phases"] = ["verified_backup"]
        action["phase_receipts"] = {"verified_backup": dict(backup.receipt)}
        action["active_phase"] = "migrate_attestation"
        attested = port._migrate_attestation({}, action, action_directory)  # noqa: SLF001
        self.assertTrue(attested.ok, attested)
        action["active_phase"] = "migrate_lane_lifecycle"
        lifecycle = port._migrate_lane_lifecycle({}, action, action_directory)  # noqa: SLF001
        self.assertTrue(lifecycle.ok, lifecycle)
        action["active_phase"] = "migrate_startup_transaction"
        startup_result = port._migrate_startup_transaction(  # noqa: SLF001
            {}, action, action_directory
        )
        self.assertTrue(startup_result.ok, startup_result)
        action["active_phase"] = "rebuild_launch_generation"
        generation_result = port._rebuild_launch_generation(  # noqa: SLF001
            {}, action, action_directory
        )
        self.assertTrue(generation_result.ok, generation_result)
        migrated = port._fresh_store_records()  # noqa: SLF001
        self.assertEqual(
            {name: row["version"] for name, row in migrated.items()},
            {"attestation": 4, "lane_lifecycle": 11,
             "launch_generation": None, "startup_transaction": 2},
        )
        HerdrLaunchGenerationStore(home=self.home).reserve_pending(
            assigned_name="mzb1_ws_codex_default", startup_action_id="startup_action",
            workspace_id="ws", role="codex", lane_id="default",
        )
        self.assertEqual(port._fresh_store_records()["launch_generation"]["version"], 2)

        # Crash-window replay: active intent remains while the effect is already durable.
        action["active_phase"] = "migrate_attestation"
        self.assertTrue(  # noqa: SLF001
            port._migrate_attestation(
                {}, action, action_directory, replaying=True
            ).ok
        )
        action["active_phase"] = "migrate_startup_transaction"
        replay = port._migrate_startup_transaction(  # noqa: SLF001
            {}, action, action_directory, replaying=True
        )
        self.assertTrue(replay.ok, replay)


if __name__ == "__main__":
    unittest.main()
