from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
    delegate_offline_rollout,
    run_offline_rollout_action,
    status_offline_rollout_action,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    ACTION_COMPLETED,
    HISTORICAL_V3_EXECUTION_PHASES,
    OfflineRolloutActionError,
    approval_fields,
    approval_manifest,
    approval_matches,
    canonical_bytes,
    canonical_digest,
    deterministic_action_id,
    new_action,
    parse_approval_note,
    render_approval_note,
    validate_action,
    verify_plan,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_restore_intent import (  # noqa: E501
    RESTORE_PHASES,
    build_restore_intent,
    decode_restore_intent,
    restore_phase_receipt,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.infrastructure.offline_rollout_action_store import (  # noqa: E501
    OfflineRolloutActionStore,
    OfflineRolloutActionStoreError,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_ROLLBACK_OWED,
    StartupTransactionFence,
    StartupUnit,
)


def _plan() -> dict:
    top = "mzb1_ws__codex__default"
    supervisor_label = "org.mozyo-bridge.callback-supervisor"
    return {
        "schema_version": 4,
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
                "target_version": 4,
                "upgrade_required": False,
                "content_digest": "1" * 64,
                "migration_plan_digest": "",
            },
            "lane_lifecycle": {
                "state": "recognized",
                "version": 10,
                "target_version": 12,
                "upgrade_required": True,
                "content_digest": "2" * 64,
                "migration_plan_digest": "",
            },
            "launch_generation": {
                "state": "recognized",
                "version": 1,
                "target_version": 2,
                "upgrade_required": True,
                "content_digest": "5" * 64,
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
            {"store": "attestation", "from_version": 1, "to_version": 4},
            {"store": "lane_lifecycle", "from_version": 10, "to_version": 12},
            {"store": "launch_generation", "from_version": 1, "to_version": 2},
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
            {"phase": "migrate_attestation", "target_version": 4},
            {"phase": "migrate_lane_lifecycle", "target_version": 12},
            {"phase": "migrate_startup_transaction", "target_version": 2},
            {"phase": "rebuild_launch_generation", "target_version": 2},
            {"phase": "exact_runtime_install"},
            {"phase": "legacy_lane_epoch_adoption", "targets": []},
            {"phase": "top_restore_action_bootstrap", "assigned_names": [top]},
            {"phase": "remaining_workspace_restore", "assigned_names": []},
            {"phase": "supervisor_pair_install", "supervisor_labels": [supervisor_label]},
            {"phase": "supervisor_pair_readback", "supervisor_labels": [supervisor_label]},
            {"phase": "final_verify"},
        ],
    }


def _legacy_recovery_plan() -> dict:
    plan = _plan()
    names = (
        "mzb1_other__claude__lane_1",
        "mzb1_other__codex__lane_1",
    )
    plan["legacy_recoveries"] = [
        {
            "issue_id": "15227",
            "journal_id": "103900",
            "workspace_id": "other",
            "lane_id": "lane-1",
            "lane_generation": 7,
            "expected_revision": 11,
            "from_epoch": 0,
            "to_epoch": 1,
            "worktree": {
                "identity": "legacy-worktree-identity",
                "wip": {
                    "readable": True,
                    "dirty": False,
                    "untracked": False,
                    "digest": "f" * 64,
                },
            },
            "agents": [
                {"provider": "claude", "assigned_name": names[0]},
                {"provider": "codex", "assigned_name": names[1]},
            ],
        }
    ]
    plan["restore_order"] = [plan["top_identity"]["assigned_name"], *names]
    plan["stores"]["launch_generation"].update(
        version=2, upgrade_required=False
    )
    for transition in plan["schema_transitions"]:
        if transition["store"] == "launch_generation":
            transition["from_version"] = 2
    for phase in plan["phase_order"]:
        if phase["phase"] == "legacy_lane_epoch_adoption":
            phase["targets"] = [
                {
                    "issue_id": "15227",
                    "workspace_id": "other",
                    "lane_id": "lane-1",
                }
            ]
        elif phase["phase"] == "remaining_workspace_restore":
            phase["assigned_names"] = list(names)
    return plan


def _close_authority(
    plan: dict, *, startup_action_id: str = "startup-" + "a" * 64
) -> dict:
    return {
        "version": 2,
        "pins": [
            {
                "workspace_id": row["workspace_id"],
                "lane_id": row["lane_id"],
                "role": row["provider"],
                "assigned_name": row["assigned_name"],
                "locator": f"private:{index}",
                "startup_action_id": startup_action_id,
            }
            for index, row in enumerate(
                sorted(plan["agents"], key=lambda item: item["assigned_name"]), start=1
            )
        ],
    }


def _legacy_absence_authority(plan: dict) -> dict:
    return {
        "version": 1,
        "pins": [
            {
                "workspace_id": recovery["workspace_id"],
                "lane_id": recovery["lane_id"],
                "provider": row["provider"],
                "assigned_name": row["assigned_name"],
                "old_locator": f"w9:p{index}",
                "startup_action_id": "startup-" + "9" * 64,
            }
            for index, (recovery, row) in enumerate(
                sorted(
                    (
                        (recovery, row)
                        for recovery in plan["legacy_recoveries"]
                        for row in recovery["agents"]
                    ),
                    key=lambda value: value[1]["assigned_name"],
                ),
                start=1,
            )
        ],
    }


def _restore_intent(plan: dict) -> dict:
    counter = iter(range(1, 100))
    return build_restore_intent(
        plan, nonce_factory=lambda: f"{next(counter):032x}"
    ).as_payload()


def _pane_and_container_intents(restore_intent: dict) -> dict:
    panes = []
    containers = []
    for index, group in enumerate(restore_intent["groups"], start=1):
        workspace = f"w{index}"
        pane = f"{workspace}:p1"
        tab = f"{workspace}:t1"
        terminal = f"terminal:root:{index}"
        panes.append(
            {
                "locator": pane,
                "workspace_id": workspace,
                "tab_id": tab,
                "terminal_id": terminal,
            }
        )
        containers.append(
            {
                "expected_startup_action_id": group[
                    "expected_startup_action_id"
                ],
                "logical_workspace_id": group["workspace_id"],
                "lane_id": group["lane_id"],
                "workspace_id": workspace,
                "tab_id": tab,
                "pane_locator": pane,
                "terminal_id": terminal,
            }
        )
    return {
        "passive_pane_intent": {"version": 1, "panes": panes},
        "restore_container_intent": {"version": 1, "groups": containers},
    }


def _historical_v3_plan() -> dict:
    """The exact frozen pre-launch-generation plan written by schema v3."""

    plan = json.loads(json.dumps(_plan()))
    plan["schema_version"] = 3
    plan["stores"].pop("launch_generation")
    plan["stores"]["attestation"]["target_version"] = 3
    plan["stores"]["lane_lifecycle"].update(
        target_version=10, upgrade_required=False
    )
    plan["schema_transitions"] = [
        row
        for row in plan["schema_transitions"]
        if row["store"] != "launch_generation"
    ]
    for transition in plan["schema_transitions"]:
        if transition["store"] == "attestation":
            transition["to_version"] = 3
        elif transition["store"] == "lane_lifecycle":
            transition["to_version"] = 10
    plan["phase_order"] = [
        row
        for row in plan["phase_order"]
        if row["phase"] != "rebuild_launch_generation"
    ]
    for phase in plan["phase_order"]:
        if phase["phase"] == "migrate_attestation":
            phase["target_version"] = 3
        elif phase["phase"] == "migrate_lane_lifecycle":
            phase["target_version"] = 10
    assert tuple(row["phase"] for row in plan["phase_order"]) == (
        HISTORICAL_V3_EXECUTION_PHASES
    )
    return plan


class FakeOps:
    def __init__(self, *, fail_once: str = ""):
        self.fail_once = fail_once
        self.phases = []
        self.phase_replays = []
        self.launches = 0

    def verify_owner_approval(self, **kwargs):
        return PhaseExecutionResult(True, receipt={"verified": True})

    def capture_private_bindings(self, **kwargs):
        restore_intent = _restore_intent(kwargs["plan"])
        return PhaseExecutionResult(
            True,
            receipt={
                "workspace_paths": {"ws": "/private/ws", "other": "/private/other"},
                "legacy_recovery_worktree_paths": {},
                "agents": [],
                "close_authority": _close_authority(kwargs["plan"]),
                "legacy_absence_authority": {"version": 1, "pins": []},
                "restore_intent": restore_intent,
                **_pane_and_container_intents(restore_intent),
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
        if name in RESTORE_PHASES:
            action = kwargs["action"]
            intent = decode_restore_intent(
                action["private_bindings"], plan=action["plan"]
            )
            return PhaseExecutionResult(
                True, receipt=restore_phase_receipt(intent, name)
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

    def test_plan_numeric_versions_require_exact_int_not_bool_or_float(self) -> None:
        mutations = (
            lambda plan: plan.__setitem__("schema_version", True),
            lambda plan: plan.__setitem__("schema_version", 4.0),
            lambda plan: plan["stores"]["lane_lifecycle"].__setitem__(
                "target_version", 11.0
            ),
            lambda plan: plan["stores"]["launch_generation"].__setitem__(
                "version", True
            ),
            lambda plan: plan["schema_transitions"][0].__setitem__(
                "from_version", 1.0
            ),
            lambda plan: next(
                phase for phase in plan["phase_order"]
                if phase["phase"] == "migrate_lane_lifecycle"
            ).__setitem__("target_version", 11.0),
        )
        for mutate in mutations:
            plan = json.loads(json.dumps(self.plan))
            mutate(plan)
            with self.assertRaises(OfflineRolloutActionError):
                verify_plan(plan, canonical_digest(plan))

    def test_action_envelope_is_closed_and_schema_version_is_exact_int(self) -> None:
        action = new_action(
            action_id="offline_" + "a" * 32,
            plan=self.plan,
            plan_digest=self.digest,
            approval_pointer="14838:97999",
            private_bindings={},
            now="2026-08-11T00:00:00+00:00",
        )
        mutations = (
            lambda row: row.__setitem__("schema_version", True),
            lambda row: row.__setitem__("schema_version", 1.0),
            lambda row: row.__setitem__("extra", "unsupported"),
            lambda row: row.pop("updated_at"),
        )
        for mutate in mutations:
            candidate = json.loads(json.dumps(action))
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(
                OfflineRolloutActionError
            ):
                validate_action(candidate)

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

    def test_gate_release_unverified_is_typed_and_completed_replay_folds(self) -> None:
        from mozyo_bridge.core.state import herdr_session_start_gate as gate

        ops = FakeOps()
        action_id = self._delegate(ops)
        real_release = gate.release_session_start_gate

        def close_then_report_unverified(lease):
            real_release(lease)
            raise gate.SessionStartGateError(
                "session_start_gate_release_unverified"
            )

        with patch.object(
            gate,
            "release_session_start_gate",
            side_effect=close_then_report_unverified,
        ):
            first = run_offline_rollout_action(
                action_id=action_id, home=self.home, ops=ops
            )

        self.assertFalse(first.ok)
        self.assertEqual(first.reason, "session_start_gate_release_unverified")
        completed_phases = list(ops.phases)
        replay = run_offline_rollout_action(
            action_id=action_id, home=self.home, ops=ops
        )
        self.assertTrue(replay.ok, replay.as_payload())
        self.assertEqual(ops.phases, completed_phases)

    def test_foreign_nonterminal_blocks_before_runner_or_first_phase(self) -> None:
        class NoTouchOps(FakeOps):
            def __init__(self):
                super().__init__()
                self.attest_calls = 0

            def attest_external_runner(self, **kwargs):
                self.attest_calls += 1
                raise AssertionError("foreign startup must block before runner readback")

        ops = NoTouchOps()
        action_id = self._delegate(ops)
        startup = StartupTransactionFence(home=self.home)
        first = startup.reserve(
            StartupUnit("unrelated", "default", ("claude",)),
            "foreign-planned",
        )
        planned = run_offline_rollout_action(
            action_id=action_id, home=self.home, ops=ops
        )
        self.assertFalse(planned.ok)
        self.assertEqual(planned.reason, "restore_action_residual")
        self.assertEqual(ops.attest_calls, 0)
        self.assertEqual(ops.phases, [])

        startup.set_phase(first.action_id, PHASE_HEALTH_CHECK)
        startup.set_phase(first.action_id, PHASE_COMPLETED_SUCCESS)
        second = startup.reserve(
            StartupUnit("different", "lane", ("codex",)),
            "foreign-rollback",
        )
        startup.set_phase(second.action_id, PHASE_HEALTH_CHECK)
        startup.set_phase(second.action_id, PHASE_ROLLBACK_OWED)
        rollback = run_offline_rollout_action(
            action_id=action_id, home=self.home, ops=ops
        )
        self.assertFalse(rollback.ok)
        self.assertEqual(rollback.reason, "restore_action_residual")
        self.assertEqual(ops.attest_calls, 0)
        self.assertEqual(ops.phases, [])

    def test_offline_exclusive_lease_reaches_restore_locked_body_without_reacquire(self) -> None:
        from contextlib import nullcontext
        from types import SimpleNamespace

        from mozyo_bridge.core.state.herdr_session_start_gate import (
            require_session_start_gate,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start as use_case,
            herdr_session_start_entry as entry,
            herdr_session_start_service as service,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_phase_fence import (  # noqa: E501
            GROUP_LAUNCH,
            RestoreGroupAdmission,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_restore import (  # noqa: E501
            OfflineRolloutRestoreExecutor,
        )

        class RestoreFence:
            @staticmethod
            def require_restore_phase_entry(_action, *, phase_name):
                return PhaseExecutionResult(True, receipt={"phase": phase_name})

            @staticmethod
            def before_restore_group(_action, *, phase_name, group_index):
                return RestoreGroupAdmission(True, disposition=GROUP_LAUNCH)

            @staticmethod
            def restore_pane_snapshot():
                return {}

            @staticmethod
            def after_restore_group(_action, *, phase_name, group_index):
                return PhaseExecutionResult(True, receipt={"group": group_index})

        class LeaseOps(FakeOps):
            def execute_phase(self, *, phase, action, action_directory, **kwargs):
                if phase["phase"] != "top_restore_action_bootstrap":
                    return super().execute_phase(
                        phase=phase,
                        action=action,
                        action_directory=action_directory,
                        **kwargs,
                    )
                self.phases.append(phase["phase"])
                self.phase_replays.append(
                    (phase["phase"], bool(kwargs.get("replaying")))
                )
                executor = OfflineRolloutRestoreExecutor(
                    home=self_home,
                    env={},
                    phase_fence=RestoreFence(),
                    session_gate_lease=kwargs.get("session_gate_lease"),
                )
                with (
                    patch.object(
                        executor, "_candidate_provenance", return_value=True
                    ),
                    patch.object(executor, "_launch_environment", return_value={}),
                ):
                    return executor.execute(
                        phase_name=phase["phase"],
                        action=action,
                        action_directory=action_directory,
                    )

        self_home = self.home
        ops = LeaseOps()
        action_id = self._delegate(ops)
        action = OfflineRolloutActionStore(self.home).load(action_id)
        expected_action_id = decode_restore_intent(
            action["private_bindings"], plan=action["plan"]
        ).groups[0].expected_startup_action_id
        observed_leases = []

        def locked_body(**kwargs):
            lease = kwargs["_session_gate_lease"]
            require_session_start_gate(
                lease, home=self.home, exclusive=True
            )
            observed_leases.append(lease)
            return SimpleNamespace(ok=True, action_id=expected_action_id)

        with (
            patch.object(
                service,
                "load_repo_local_config",
                return_value=SimpleNamespace(
                    agent_launch=None, lane_placement=None
                ),
            ),
            patch.object(
                service,
                "load_coordinator_placement_for_launch",
                return_value=SimpleNamespace(
                    mode="per_project_space", top_workspace_id=""
                ),
            ),
            patch.object(
                entry,
                "apply_workspace_alias",
                side_effect=lambda path: (path, ""),
            ),
            patch.object(entry, "validate_session_request"),
            patch.object(
                use_case, "_resolve_binary_or_die", return_value="/herdr"
            ),
            patch.object(entry, "require_herdr_cli_capabilities"),
            patch.object(entry, "mozyo_bridge_home", return_value=self.home),
            patch.object(
                entry,
                "acquire_session_start_gate",
                side_effect=AssertionError("offline restore must not reacquire SH"),
            ),
            patch.object(
                entry,
                "ActionPrivateLaunchShimSet",
                return_value=nullcontext(object()),
            ),
            patch.object(
                entry, "attestation_store_lock", return_value=nullcontext()
            ),
            patch.object(use_case, "_prepare_session_locked", side_effect=locked_body),
        ):
            result = run_offline_rollout_action(
                action_id=action_id, home=self.home, ops=ops
            )

        self.assertTrue(result.ok, result.as_payload())
        self.assertEqual(len(observed_leases), 1)
        self.assertTrue(observed_leases[0].exclusive)

    def test_generation_pin_is_private_0600_and_never_renders_publicly(self) -> None:
        sentinel = "startup-" + "f" * 64

        class TerminalOps(FakeOps):
            def capture_private_bindings(self, **kwargs):
                captured = super().capture_private_bindings(**kwargs)
                receipt = dict(captured.receipt)
                receipt.update({
                    "agents": [{
                        "assigned_name": "mzb1_ws_codex_default",
                        "workspace_id": "ws", "lane_id": "default",
                        "provider": "codex",
                    }],
                    "close_authority": _close_authority(
                        kwargs["plan"], startup_action_id=sentinel
                    ),
                })
                return PhaseExecutionResult(True, receipt=receipt)

        ops = TerminalOps()
        action_id = self._delegate(ops)
        store = OfflineRolloutActionStore(self.home)
        directory = store.action_directory(action_id)
        record = directory / "action.json"
        self.assertEqual(store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertIn(sentinel, json.dumps(store.load(action_id)))
        self.assertNotIn(sentinel, repr(PhaseExecutionResult(
            True, receipt={"terminal_id": sentinel}
        )))
        public = status_offline_rollout_action(action_id=action_id, home=self.home)
        restore_nonce = OfflineRolloutActionStore(self.home).load(action_id)[
            "private_bindings"
        ]["restore_intent"]["groups"][0]["action_nonce"]
        self.assertNotIn(sentinel, repr(public))
        self.assertNotIn(sentinel, json.dumps(public.as_payload(), sort_keys=True))
        self.assertNotIn(restore_nonce, repr(public))
        self.assertNotIn(
            restore_nonce, json.dumps(public.as_payload(), sort_keys=True)
        )

    def test_valid_legacy_absence_authority_delegates_without_close_authority(self) -> None:
        plan = _legacy_recovery_plan()
        legacy_names = {
            row["assigned_name"]
            for recovery in plan["legacy_recoveries"]
            for row in recovery["agents"]
        }

        class LegacyOps(FakeOps):
            def capture_private_bindings(self, **kwargs):
                captured = super().capture_private_bindings(**kwargs)
                receipt = dict(captured.receipt)
                receipt["legacy_absence_authority"] = (
                    _legacy_absence_authority(plan)
                )
                return PhaseExecutionResult(True, receipt=receipt)

        digest = canonical_digest(plan)
        result = delegate_offline_rollout(
            plan=plan,
            plan_digest=digest,
            owner_approval="15227:103900",
            home=self.home,
            repo_root=self.repo,
            execute=True,
            ops=LegacyOps(),
        )
        self.assertTrue(result.ok, result.as_payload())
        action = OfflineRolloutActionStore(self.home).load(result.payload["action_id"])
        close_names = {
            row["assigned_name"]
            for row in action["private_bindings"]["close_authority"]["pins"]
        }
        absence_names = {
            row["assigned_name"]
            for row in action["private_bindings"]["legacy_absence_authority"][
                "pins"
            ]
        }
        self.assertTrue(close_names.isdisjoint(legacy_names))
        self.assertEqual(absence_names, legacy_names)

    def test_delegate_refuses_missing_or_malformed_legacy_absence_authority(self) -> None:
        plan = _legacy_recovery_plan()
        digest = canonical_digest(plan)

        class InvalidLegacyOps(FakeOps):
            def __init__(self, authority):
                super().__init__()
                self.authority = authority

            def capture_private_bindings(self, **kwargs):
                captured = super().capture_private_bindings(**kwargs)
                receipt = dict(captured.receipt)
                if self.authority is None:
                    receipt.pop("legacy_absence_authority", None)
                else:
                    receipt["legacy_absence_authority"] = self.authority
                return PhaseExecutionResult(True, receipt=receipt)

        for authority, reason in (
            (None, "legacy_absence_authority_missing"),
            (
                {**_legacy_absence_authority(plan), "version": 2},
                "legacy_absence_authority_schema_unsupported",
            ),
        ):
            with self.subTest(reason=reason):
                result = delegate_offline_rollout(
                    plan=plan,
                    plan_digest=digest,
                    owner_approval="15227:103900",
                    home=self.home,
                    repo_root=self.repo,
                    execute=True,
                    ops=InvalidLegacyOps(authority),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)
        self.assertFalse((self.home / "offline-rollout-actions-v1").exists())

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

    def test_delegate_refuses_malformed_close_authority_before_action_create(self) -> None:
        class MalformedOps(FakeOps):
            def capture_private_bindings(self, **kwargs):
                captured = super().capture_private_bindings(**kwargs)
                receipt = dict(captured.receipt)
                receipt["close_authority"] = {
                    "version": 2,
                    "pins": [{"assigned_name": self_plan_name}],
                }
                return PhaseExecutionResult(True, receipt=receipt)

        self_plan_name = self.plan["agents"][0]["assigned_name"]
        result = delegate_offline_rollout(
            plan=self.plan,
            plan_digest=self.digest,
            owner_approval="14838:97999",
            home=self.home,
            repo_root=self.repo,
            execute=True,
            ops=MalformedOps(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "close_authority_pin_shape_invalid")
        self.assertFalse(
            (self.home / "offline-rollout-actions-v1").exists()
        )

    def test_delegate_refuses_malformed_restore_intent_before_action_create(self) -> None:
        class MalformedOps(FakeOps):
            def capture_private_bindings(self, **kwargs):
                captured = super().capture_private_bindings(**kwargs)
                receipt = dict(captured.receipt)
                receipt["restore_intent"] = {
                    **receipt["restore_intent"],
                    "private_extra": "must-not-be-accepted",
                }
                return PhaseExecutionResult(True, receipt=receipt)

        result = delegate_offline_rollout(
            plan=self.plan,
            plan_digest=self.digest,
            owner_approval="14838:97999",
            home=self.home,
            repo_root=self.repo,
            execute=True,
            ops=MalformedOps(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "restore_intent_shape_invalid")
        self.assertFalse((self.home / "offline-rollout-actions-v1").exists())

    def test_legacy_close_authority_status_is_readable_but_run_is_zero_effect(self) -> None:
        class NoTouchOps(FakeOps):
            def attest_external_runner(self, **kwargs):
                raise AssertionError("legacy action must block before runner readback")

            def execute_phase(self, **kwargs):
                raise AssertionError("legacy action must never execute a phase")

        variants = (
            ({}, "close_authority_missing"),
            (
                {"close_authority": {"version": 1, "pins": []}},
                "close_authority_schema_unsupported",
            ),
            (
                {
                    "close_authority": {
                        "version": 2,
                        "pins": [
                            {
                                "workspace_id": "ws",
                                "lane_id": "default",
                                "role": "codex",
                                "assigned_name": self.plan["agents"][0][
                                    "assigned_name"
                                ],
                                "locator": "private:1",
                            }
                        ],
                    }
                },
                "close_authority_pin_shape_invalid",
            ),
            (
                {
                    "close_authority": {
                        "version": 2,
                        "pins": [
                            {
                                **_close_authority(self.plan)["pins"][0],
                                "startup_action_id": "startup-malformed",
                            }
                        ],
                    }
                },
                "close_authority_pin_invalid",
            ),
        )
        store = OfflineRolloutActionStore(self.home)
        for index, (private, reason) in enumerate(variants, start=1):
            with self.subTest(reason=reason):
                action_id = "offline_" + str(index) * 32
                action = new_action(
                    action_id=action_id,
                    plan=self.plan,
                    plan_digest=self.digest,
                    approval_pointer="14838:97999",
                    private_bindings=private,
                    now="2026-08-12T00:00:00+00:00",
                )
                store.create(action)
                record = store.action_directory(action_id) / "action.json"
                before = record.read_bytes()
                status = status_offline_rollout_action(
                    action_id=action_id, home=self.home
                )
                self.assertTrue(status.ok)
                blocked = run_offline_rollout_action(
                    action_id=action_id, home=self.home, ops=NoTouchOps()
                )
                self.assertFalse(blocked.ok)
                self.assertEqual(blocked.reason, reason)
                self.assertEqual(record.read_bytes(), before)

    def test_legacy_restore_intent_status_only_and_receipt_tamper_is_zero_effect(self) -> None:
        class NoTouchOps(FakeOps):
            def attest_external_runner(self, **kwargs):
                raise AssertionError("private authority must block before runner readback")

            def execute_phase(self, **kwargs):
                raise AssertionError("private authority must block before any phase")

        variants = (
            ({}, "restore_intent_missing"),
            (
                {"restore_intent": {"version": 0, "groups": []}},
                "restore_intent_schema_unsupported",
            ),
        )
        store = OfflineRolloutActionStore(self.home)
        for index, (extra, reason) in enumerate(variants, start=5):
            with self.subTest(reason=reason):
                action_id = "offline_" + str(index) * 32
                private = {"close_authority": _close_authority(self.plan), **extra}
                action = new_action(
                    action_id=action_id,
                    plan=self.plan,
                    plan_digest=self.digest,
                    approval_pointer="14838:97999",
                    private_bindings=private,
                    now="2026-08-12T00:00:00+00:00",
                )
                store.create(action)
                record = store.action_directory(action_id) / "action.json"
                before = record.read_bytes()
                self.assertTrue(
                    status_offline_rollout_action(
                        action_id=action_id, home=self.home
                    ).ok
                )
                blocked = run_offline_rollout_action(
                    action_id=action_id, home=self.home, ops=NoTouchOps()
                )
                self.assertFalse(blocked.ok)
                self.assertEqual(blocked.reason, reason)
                self.assertEqual(record.read_bytes(), before)

        action_id = "offline_" + "7" * 32
        restore_intent = _restore_intent(self.plan)
        private = {
            "close_authority": _close_authority(self.plan),
            "legacy_absence_authority": {"version": 1, "pins": []},
            "restore_intent": restore_intent,
            **_pane_and_container_intents(restore_intent),
        }
        action = new_action(
            action_id=action_id,
            plan=self.plan,
            plan_digest=self.digest,
            approval_pointer="14838:97999",
            private_bindings=private,
            now="2026-08-12T00:00:00+00:00",
        )
        top_index = HISTORICAL_V3_EXECUTION_PHASES.index(
            "top_restore_action_bootstrap"
        )
        # v4 has one extra phase before restore.
        completed = [
            row["phase"]
            for row in self.plan["phase_order"][: top_index + 2]
        ]
        action["completed_phases"] = completed
        action["phase_receipts"] = {
            name: {"phase": name} for name in completed
        }
        action["state"] = "running"
        store.create(action)
        record = store.action_directory(action_id) / "action.json"
        before = record.read_bytes()
        self.assertTrue(
            status_offline_rollout_action(action_id=action_id, home=self.home).ok
        )
        blocked = run_offline_rollout_action(
            action_id=action_id, home=self.home, ops=NoTouchOps()
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.reason, "restore_receipt_shape_invalid")
        self.assertEqual(record.read_bytes(), before)

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

    def test_historical_v3_is_status_only_through_the_same_sealed_reader(self) -> None:
        sentinel = "/private/historical-v3/nonce-or-path"
        historical = _historical_v3_plan()
        historical_digest = canonical_digest(historical)
        action_id = "offline_" + "8" * 32
        action = new_action(
            action_id=action_id,
            plan=self.plan,
            plan_digest=self.digest,
            approval_pointer="14838:97999",
            private_bindings={},
            now="2026-08-12T00:00:00+00:00",
        )
        action.update(
            plan=historical,
            plan_digest=historical_digest,
            private_bindings={"historical_private": sentinel},
            state="delegated",
        )
        store = OfflineRolloutActionStore(self.home)
        directory = store.action_directory(action_id, create=True)
        record = directory / "action.json"
        store._write_path(record, action)  # noqa: SLF001 - frozen archival fixture

        status = status_offline_rollout_action(action_id=action_id, home=self.home)
        self.assertTrue(status.ok, status.as_payload())
        self.assertEqual(len(status.payload), 12)
        self.assertEqual(status.payload["next_phase"], "supervisor_stop")
        self.assertNotIn(sentinel, repr(status))
        self.assertNotIn(sentinel, json.dumps(status.as_payload(), sort_keys=True))
        with self.assertRaisesRegex(
            OfflineRolloutActionStoreError, "plan_schema_unsupported"
        ):
            store.load(action_id)

        class NoTouchOps:
            def attest_external_runner(self, **_kwargs):
                raise AssertionError("historical status record must not reach the port")

        before = record.read_bytes()
        blocked = run_offline_rollout_action(
            action_id=action_id, home=self.home, ops=NoTouchOps()
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.reason, "plan_schema_unsupported")
        self.assertEqual(record.read_bytes(), before)

        record.chmod(0o644)
        unsafe = status_offline_rollout_action(action_id=action_id, home=self.home)
        self.assertFalse(unsafe.ok)
        self.assertEqual(unsafe.reason, "action_record_permissions_unsafe")
        record.chmod(0o600)

        record.write_bytes(before + b"\n")
        record.chmod(0o600)
        noncanonical = status_offline_rollout_action(
            action_id=action_id, home=self.home
        )
        self.assertFalse(noncanonical.ok)
        self.assertEqual(noncanonical.reason, "action_record_noncanonical")

        envelope = json.loads(before)
        envelope["payload"]["private_bindings"]["historical_private"] = (
            "/private/tampered"
        )
        record.write_bytes(canonical_bytes(envelope) + b"\n")
        record.chmod(0o600)
        unsealed = status_offline_rollout_action(action_id=action_id, home=self.home)
        self.assertFalse(unsealed.ok)
        self.assertEqual(unsealed.reason, "action_record_seal_mismatch")

        envelope = json.loads(before)
        envelope["payload"]["plan"]["phase_order"][0]["phase"] = "foreign_phase"
        envelope["payload"]["plan_digest"] = canonical_digest(
            envelope["payload"]["plan"]
        )
        envelope["payload_sha256"] = canonical_digest(envelope["payload"])
        record.write_bytes(canonical_bytes(envelope) + b"\n")
        record.chmod(0o600)
        alternate = status_offline_rollout_action(
            action_id=action_id, home=self.home
        )
        self.assertFalse(alternate.ok)
        self.assertEqual(alternate.reason, "plan_phase_order_unsupported")

        envelope = json.loads(before)
        envelope["payload"]["plan"]["candidate_artifact"][
            "exact_pin_ready"
        ] = False
        envelope["payload"]["plan_digest"] = canonical_digest(
            envelope["payload"]["plan"]
        )
        envelope["payload_sha256"] = canonical_digest(envelope["payload"])
        record.write_bytes(canonical_bytes(envelope) + b"\n")
        record.chmod(0o600)
        malformed = status_offline_rollout_action(
            action_id=action_id, home=self.home
        )
        self.assertFalse(malformed.ok)
        self.assertEqual(malformed.reason, "artifact_pin_incomplete")

        target = directory / "historical-action.json"
        target.write_bytes(before)
        target.chmod(0o600)
        record.unlink()
        record.symlink_to(target)
        linked = status_offline_rollout_action(action_id=action_id, home=self.home)
        self.assertFalse(linked.ok)
        self.assertEqual(linked.reason, "action_record_unavailable")

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
