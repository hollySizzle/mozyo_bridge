"""Regression surface for the external offline-rollout execution rail (#14838)."""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import io
import sqlite3
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_offline_rollout import (  # noqa: E501
    cmd_herdr_offline_rollout_run,
    register_herdr_offline_rollout_parser,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_executor import (  # noqa: E501
    RUNNER_ENV,
    LiveOfflineRolloutExecutionPort,
    _reports_exact_version,
    _sanitized_runtime_env,
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
        self.assertEqual(replay.receipt["store_authority"], "active_phase_replay")

        with patch.object(port, "_fresh_store_records", return_value={"attestation": target}):
            refused = port._require_store_phase_authority(  # noqa: SLF001
                action,
                store_name="attestation",
                phase_name="migrate_attestation",
                replaying=False,
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
