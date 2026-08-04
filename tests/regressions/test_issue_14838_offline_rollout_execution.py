"""Regression surface for the external offline-rollout execution rail (#14838)."""

from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_offline_rollout import (  # noqa: E501
    cmd_herdr_offline_rollout_run,
    register_herdr_offline_rollout_parser,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_executor import (  # noqa: E501
    RUNNER_ENV,
    LiveOfflineRolloutExecutionPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    read_live_worktree_fingerprint,
)


class OfflineRolloutExecutionRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"

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
        admitted = LiveOfflineRolloutExecutionPort(
            home=self.home, env={RUNNER_ENV: action_id}
        ).attest_external_runner(action_id=action_id)
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
        snapshot = action_directory / "wip" / "ws"
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
        }
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        with patch.object(port, "_fresh_store_records", return_value={"attestation": planned}):
            first = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="attestation",
                phase_name="migrate_attestation",
            )
        self.assertTrue(first.ok)
        self.assertEqual(first.receipt["store_authority"], "planned_predecessor")

        target = dict(planned, version=3, upgrade_required=False, content_digest="b" * 64)
        with patch.object(port, "_fresh_store_records", return_value={"attestation": target}):
            replay = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="attestation",
                phase_name="migrate_attestation",
            )
        self.assertTrue(replay.ok)
        self.assertEqual(replay.receipt["store_authority"], "active_phase_replay")

        not_active = dict(action, active_phase="")
        with patch.object(port, "_fresh_store_records", return_value={"attestation": target}):
            refused = port._require_store_phase_authority(  # noqa: SLF001
                not_active,
                store_name="attestation",
                phase_name="migrate_attestation",
            )
        self.assertFalse(refused.ok)
        self.assertEqual(refused.reason, "attestation_plan_drift")

    def test_real_private_home_migrates_v1_v10_v1_to_v3_v10_v2_and_replays(self) -> None:
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
        startup = StartupTransactionFence(home=self.home)
        startup.reserve(StartupUnit("ws", "default", ("codex",)), "nonce")

        action_directory = Path(self.temp.name) / "action"
        action_directory.mkdir(mode=0o700)
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        original = port._fresh_store_records()  # noqa: SLF001
        self.assertEqual(
            {name: row["version"] for name, row in original.items()},
            {"attestation": 1, "lane_lifecycle": 10, "startup_transaction": 1},
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

        action["completed_phases"] = ["verified_backup"]
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
        migrated = port._fresh_store_records()  # noqa: SLF001
        self.assertEqual(
            {name: row["version"] for name, row in migrated.items()},
            {"attestation": 3, "lane_lifecycle": 10, "startup_transaction": 2},
        )

        # Crash-window replay: active intent remains while the effect is already durable.
        action["active_phase"] = "migrate_attestation"
        self.assertTrue(port._migrate_attestation({}, action, action_directory).ok)  # noqa: SLF001
        action["active_phase"] = "migrate_startup_transaction"
        replay = port._migrate_startup_transaction({}, action, action_directory)  # noqa: SLF001
        self.assertTrue(replay.ok, replay)


if __name__ == "__main__":
    unittest.main()
