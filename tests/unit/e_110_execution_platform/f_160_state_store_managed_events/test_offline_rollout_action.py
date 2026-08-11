from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
    delegate_offline_rollout,
    run_offline_rollout_action,
    status_offline_rollout_action,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    ACTION_COMPLETED,
    OfflineRolloutActionError,
    approval_fields,
    approval_manifest,
    approval_matches,
    canonical_digest,
    deterministic_action_id,
    parse_approval_note,
    render_approval_note,
    validate_action,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.infrastructure.offline_rollout_action_store import (  # noqa: E501
    OfflineRolloutActionStore,
    OfflineRolloutActionStoreError,
)


def _plan() -> dict:
    top = "mzb1_ws__codex__default"
    supervisor_label = "org.mozyo-bridge.callback-supervisor"
    return {
        "schema_version": 3,
        "candidate_artifact": {
            "distribution": "testpypi",
            "version": "0.15.0a4",
            "source_sha": "a" * 40,
            "source_ref": "refs/heads/main",
            "workflow_run_id": "40000000000",
            "wheel_sha256": "b" * 64,
            "sdist_sha256": "c" * 64,
            "exact_pin_ready": True,
        },
        "current_workspace_id": "ws",
        "current_project_name": "project",
        "top_identity": {
            "workspace_id": "ws",
            "lane_id": "default",
            "provider": "codex",
            "assigned_name": top,
        },
        "workspaces": [
            {
                "workspace_id": "other",
                "project_name": "other-project",
                "scope": "unrelated_project",
                "assigned_names": [],
                "wip": {
                    "readable": True,
                    "dirty": True,
                    "untracked": False,
                    "digest": "d" * 64,
                },
            },
            {
                "workspace_id": "ws",
                "project_name": "project",
                "scope": "target_project",
                "assigned_names": [top],
                "wip": {
                    "readable": True,
                    "dirty": False,
                    "untracked": False,
                    "digest": "e" * 64,
                },
            },
        ],
        "agents": [
            {
                "assigned_name": top,
                "workspace_id": "ws",
                "lane_id": "default",
                "provider": "codex",
                "runtime_state": "working",
            }
        ],
        "legacy_recoveries": [],
        "stores": {
            "attestation": {
                "state": "recognized",
                "version": 1,
                "target_version": 3,
                "upgrade_required": False,
                "content_digest": "1" * 64,
                "migration_plan_digest": "",
            },
            "lane_lifecycle": {
                "state": "recognized",
                "version": 10,
                "target_version": 10,
                "upgrade_required": False,
                "content_digest": "2" * 64,
                "migration_plan_digest": "",
            },
            "startup_transaction": {
                "state": "recognized",
                "version": 1,
                "target_version": 2,
                "upgrade_required": False,
                "content_digest": "3" * 64,
                "migration_plan_digest": "4" * 64,
            },
        },
        "supervisors": [
            {
                "label": supervisor_label,
                "installed": True,
                "loaded": True,
                "pid": 123,
                "home_pin": "ok",
                "executable_matches": True,
                "credential_readiness": "ready",
                "backend": "systemd_user",
                "legacy_drain": "not_applicable",
            }
        ],
        "stop_order": [top],
        "restore_order": [top],
        "schema_transitions": [
            {"store": "attestation", "from_version": 1, "to_version": 3},
            {"store": "lane_lifecycle", "from_version": 10, "to_version": 10},
            {"store": "startup_transaction", "from_version": 1, "to_version": 2},
        ],
        "phase_order": [
            {
                "phase": "supervisor_stop",
                "supervisor_labels": [supervisor_label],
                "required_readback": "current_stopped_and_legacy_absent",
            },
            {"phase": "non_top_workspace_stop", "assigned_names": []},
            {"phase": "top_workspace_stop", "assigned_names": [top]},
            {"phase": "consumer_zero", "required_readback": "zero"},
            {"phase": "verified_backup", "stores": ["attestation"]},
            {"phase": "migrate_attestation", "target_version": 3},
            {"phase": "migrate_lane_lifecycle", "target_version": 10},
            {"phase": "migrate_startup_transaction", "target_version": 2},
            {"phase": "exact_runtime_install"},
            {"phase": "legacy_lane_epoch_adoption", "targets": []},
            {"phase": "top_restore_action_bootstrap", "assigned_names": [top]},
            {"phase": "remaining_workspace_restore", "assigned_names": []},
            {"phase": "supervisor_pair_install", "supervisor_labels": [supervisor_label]},
            {"phase": "supervisor_pair_readback", "supervisor_labels": [supervisor_label]},
            {"phase": "final_verify"},
        ],
    }


class FakeOps:
    def __init__(self, *, fail_once: str = ""):
        self.fail_once = fail_once
        self.phases = []
        self.phase_replays = []
        self.launches = 0

    def verify_owner_approval(self, **kwargs):
        return PhaseExecutionResult(True, receipt={"verified": True})

    def capture_private_bindings(self, **kwargs):
        return PhaseExecutionResult(
            True,
            receipt={
                "workspace_paths": {"ws": "/private/ws", "other": "/private/other"},
                "agents": [],
                "target_cli": "/private/bin/mozyo-bridge",
                "pipx": "/private/bin/pipx",
            },
        )

    def prepare_external_runner(self, **kwargs):
        return PhaseExecutionResult(
            True, receipt={"cli": "/private/runner", "wheel": "/private/wheel"}
        )

    def launch_external_runner(self, **kwargs):
        self.launches += 1
        return PhaseExecutionResult(True, receipt={"launchd_bootstrapped": True})

    def attest_external_runner(self, **kwargs):
        return PhaseExecutionResult(True, receipt={"external": True})

    def execute_phase(self, *, phase, replaying, **kwargs):
        name = phase["phase"]
        self.phases.append(name)
        self.phase_replays.append((name, replaying))
        if self.fail_once == name:
            self.fail_once = ""
            return PhaseExecutionResult(
                False,
                reason="injected_failure",
                detail="/private/action/internal-error",
            )
        return PhaseExecutionResult(True, receipt={"phase": name, "verified": True})


class OfflineRolloutActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.plan = _plan()
        self.digest = canonical_digest(self.plan)

    def test_old_schema_or_missing_supervisor_stop_evidence_is_not_executable(self) -> None:
        cases = (
            ("v1", lambda plan: plan.__setitem__("schema_version", 1), "plan_schema_unsupported"),
            ("v2", lambda plan: plan.__setitem__("schema_version", 2), "plan_schema_unsupported"),
            (
                "missing_legacy",
                lambda plan: plan["supervisors"][0].pop("legacy_drain"),
                "plan_supervisor_evidence_invalid",
            ),
            (
                "wrong_phase_label",
                lambda plan: plan["phase_order"][0].__setitem__(
                    "supervisor_labels", ["foreign"]
                ),
                "plan_supervisor_evidence_invalid",
            ),
            (
                "old_readback",
                lambda plan: plan["phase_order"][0].__setitem__(
                    "required_readback", "all_not_installed_and_not_loaded"
                ),
                "supervisor_readback_contract_invalid",
            ),
            (
                "foreign_legacy",
                lambda plan: plan["supervisors"][0].update(
                    backend="launchd", legacy_drain="foreign"
                ),
                "plan_supervisor_evidence_invalid",
            ),
            (
                "unreadable_legacy",
                lambda plan: plan["supervisors"][0].update(
                    backend="launchd", legacy_drain="unreadable"
                ),
                "plan_supervisor_evidence_invalid",
            ),
            (
                "absent_legacy",
                lambda plan: plan["supervisors"][0].update(
                    backend="launchd", legacy_drain="absent"
                ),
                "plan_supervisor_evidence_invalid",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(name=name):
                plan = json.loads(json.dumps(self.plan))
                mutate(plan)
                with self.assertRaisesRegex(OfflineRolloutActionError, expected):
                    approval_manifest(plan, canonical_digest(plan))

    def test_approval_note_is_exact_and_enumerates_high_blast_facts(self) -> None:
        manifest = approval_manifest(self.plan, self.digest)
        note = render_approval_note(manifest, "14838")
        expected = approval_fields(manifest, "14838")
        self.assertTrue(approval_matches(note, self.plan, self.digest, "14838"))
        self.assertEqual(parse_approval_note(note), expected)
        self.assertEqual(manifest["workspace_ids"], ["other", "ws"])
        self.assertEqual(manifest["unrelated_workspace_ids"], ["other"])
        self.assertTrue(manifest["global_stop"])
        self.assertTrue(manifest["forward_only"])
        changed = note.replace(expected["action_digest"], "0" * 64)
        for refused in (
            f"`{note}`",
            f"```\n{note}\n```",
            note + "\n" + note,
            changed,
            note.replace("decision=approved", "decision=declined:decision=approved"),
        ):
            self.assertFalse(
                approval_matches(refused, self.plan, self.digest, "14838")
            )
        self.assertFalse(approval_matches(note, self.plan, self.digest, "14839"))

    def test_malformed_pointer_and_plan_digest_fail_closed(self) -> None:
        ops = FakeOps()
        result = delegate_offline_rollout(
            plan=self.plan,
            plan_digest="0" * 64,
            owner_approval="14838:1",
            home=self.home,
            repo_root=self.repo,
            execute=False,
            ops=ops,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "plan_digest_mismatch")
        result = delegate_offline_rollout(
            plan=self.plan,
            plan_digest=self.digest,
            owner_approval=" 14838:1",
            home=self.home,
            repo_root=self.repo,
            execute=False,
            ops=ops,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "owner_approval_invalid")

    def _delegate(self, ops: FakeOps):
        result = delegate_offline_rollout(
            plan=self.plan,
            plan_digest=self.digest,
            owner_approval="14838:97999",
            home=self.home,
            repo_root=self.repo,
            execute=True,
            ops=ops,
        )
        self.assertTrue(result.ok, result.as_payload())
        self.assertEqual(
            result.payload["action_id"],
            deterministic_action_id(self.digest, "14838:97999"),
        )
        return str(result.payload["action_id"])

    def test_delegate_run_status_and_replay_complete_every_phase_once(self) -> None:
        ops = FakeOps()
        action_id = self._delegate(ops)
        running = run_offline_rollout_action(
            action_id=action_id, home=self.home, ops=ops
        )
        self.assertTrue(running.ok, running.as_payload())
        self.assertEqual(running.state, ACTION_COMPLETED)
        expected = [row["phase"] for row in self.plan["phase_order"]]
        self.assertEqual(ops.phases, expected)
        replay = run_offline_rollout_action(
            action_id=action_id, home=self.home, ops=ops
        )
        self.assertTrue(replay.ok)
        self.assertEqual(ops.phases, expected)
        status = status_offline_rollout_action(action_id=action_id, home=self.home)
        encoded = json.dumps(status.as_payload())
        self.assertNotIn("/private/", encoded)
        self.assertEqual(status.payload["completed_phases"], expected)

    def test_duplicate_delegate_of_one_plan_cannot_launch_a_second_runner(self) -> None:
        ops = FakeOps()
        action_id = self._delegate(ops)
        duplicate = delegate_offline_rollout(
            plan=self.plan,
            plan_digest=self.digest,
            owner_approval="14838:97999",
            home=self.home,
            repo_root=self.repo,
            execute=True,
            ops=ops,
        )
        self.assertFalse(duplicate.ok)
        self.assertIn(duplicate.reason, {"action_already_exists", "action_busy"})
        self.assertEqual(ops.launches, 1)
        self.assertTrue(OfflineRolloutActionStore(self.home).load(action_id))

    def test_blocked_phase_resumes_forward_without_repeating_completed_prefix(self) -> None:
        ops = FakeOps(fail_once="migrate_attestation")
        action_id = self._delegate(ops)
        first = run_offline_rollout_action(action_id=action_id, home=self.home, ops=ops)
        self.assertFalse(first.ok)
        self.assertEqual(first.reason, "injected_failure")
        self.assertNotIn("/private/", json.dumps(first.as_payload()))
        prefix = [row["phase"] for row in self.plan["phase_order"][:5]]
        self.assertEqual(first.payload["completed_phases"], prefix)
        second = run_offline_rollout_action(action_id=action_id, home=self.home, ops=ops)
        self.assertTrue(second.ok)
        self.assertEqual(ops.phases.count("verified_backup"), 1)
        self.assertEqual(ops.phases.count("migrate_attestation"), 2)
        attempts = [
            replaying
            for phase, replaying in ops.phase_replays
            if phase == "migrate_attestation"
        ]
        self.assertEqual(attempts, [False, True])
        status = status_offline_rollout_action(action_id=action_id, home=self.home)
        self.assertEqual(status.payload["active_phase"], "")

    def test_unverified_supervisor_stop_never_runs_the_next_offline_phase(self) -> None:
        ops = FakeOps(fail_once="supervisor_stop")
        action_id = self._delegate(ops)

        result = run_offline_rollout_action(action_id=action_id, home=self.home, ops=ops)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "injected_failure")
        self.assertEqual(ops.phases, ["supervisor_stop"])
        self.assertEqual(result.payload["completed_phases"], [])

    def test_active_phase_is_persisted_before_effect_and_tamper_is_refused(self) -> None:
        ops = FakeOps(fail_once="migrate_attestation")
        action_id = self._delegate(ops)
        result = run_offline_rollout_action(action_id=action_id, home=self.home, ops=ops)
        self.assertFalse(result.ok)
        action = OfflineRolloutActionStore(self.home).load(action_id)
        self.assertEqual(action["active_phase"], "migrate_attestation")
        self.assertIn("/private/", action["last_detail"])
        public = status_offline_rollout_action(action_id=action_id, home=self.home)
        self.assertNotIn("/private/", json.dumps(public.as_payload()))
        self.assertTrue(public.payload["private_detail_recorded"])
        tampered = dict(action)
        tampered["active_phase"] = "migrate_lane_lifecycle"
        with self.assertRaises(OfflineRolloutActionError):
            validate_action(tampered)

    def test_sealed_store_refuses_tamper_and_uses_private_modes(self) -> None:
        action_id = self._delegate(FakeOps())
        store = OfflineRolloutActionStore(self.home)
        directory = store.action_directory(action_id)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        record = directory / "action.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        raw = record.read_text(encoding="utf-8").replace("delegated", "tampered")
        record.write_text(raw, encoding="utf-8")
        record.chmod(0o600)
        with self.assertRaises(OfflineRolloutActionStoreError):
            store.load(action_id)


if __name__ == "__main__":
    unittest.main()
