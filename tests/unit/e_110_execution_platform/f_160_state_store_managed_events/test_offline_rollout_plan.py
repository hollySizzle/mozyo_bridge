from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_plan import (  # noqa: E501
    AgentSnapshot,
    OfflineRolloutCapture,
    StoreSnapshot,
    SupervisorAgentSnapshot,
    TopIdentitySnapshot,
    WorkspaceSnapshot,
    build_offline_rollout_plan,
)


def _capture() -> OfflineRolloutCapture:
    top = "mzb1_ws_a__codex__default"
    peer = "mzb1_ws_b__claude__lane_1"
    return OfflineRolloutCapture(
        current_workspace_id="ws_a",
        current_project_name="mozyo_bridge",
        candidate_version="0.15.0a2",
        candidate_source_sha="a" * 40,
        candidate_source_ref="refs/heads/main",
        candidate_workflow_run_id="30821934713",
        candidate_wheel_sha256="d" * 64,
        candidate_sdist_sha256="e" * 64,
        workspaces=(
            WorkspaceSnapshot(
                "ws_b",
                "other",
                "unrelated_project",
                (peer,),
                True,
                True,
                False,
                "b" * 64,
            ),
            WorkspaceSnapshot(
                "ws_a",
                "mozyo_bridge",
                "target_project",
                (top,),
                True,
                False,
                False,
                "c" * 64,
            ),
        ),
        agents=(
            AgentSnapshot(peer, "ws_b", "lane_1", "claude", "idle"),
            AgentSnapshot(top, "ws_a", "default", "codex", "working"),
        ),
        unmanaged_assigned_names=(),
        top_identity=TopIdentitySnapshot("ws_a", "default", "codex", top),
        stores=(
            StoreSnapshot(
                "startup_transaction",
                "recognized",
                1,
                content_digest="1" * 64,
                migration_plan_digest="2" * 64,
            ),
            StoreSnapshot("attestation", "recognized", 1, content_digest="3" * 64),
            StoreSnapshot("lane_lifecycle", "recognized", 9, content_digest="4" * 64),
        ),
        supervisors=(
            SupervisorAgentSnapshot(
                "org.mozyo-bridge.callback-supervisor",
                True,
                True,
                123,
                "ok",
                True,
                "ready",
            ),
            SupervisorAgentSnapshot(
                "org.mozyo-bridge.callback-supervisor.drain",
                True,
                True,
                124,
                "ok",
                True,
                "ready",
            ),
        ),
    )


class OfflineRolloutPlanTests(unittest.TestCase):
    def test_canonical_digest_and_orders_are_input_order_independent(self) -> None:
        first = build_offline_rollout_plan(_capture())
        source = _capture()
        reversed_capture = replace(
            source,
            workspaces=tuple(reversed(source.workspaces)),
            agents=tuple(reversed(source.agents)),
            stores=tuple(reversed(source.stores)),
        )
        second = build_offline_rollout_plan(reversed_capture)

        self.assertTrue(first.ok)
        self.assertEqual(first.plan_digest, second.plan_digest)
        self.assertEqual(first.plan, second.plan)
        self.assertEqual(
            first.plan["stop_order"],
            ["mzb1_ws_b__claude__lane_1", "mzb1_ws_a__codex__default"],
        )
        self.assertEqual(
            first.plan["restore_order"],
            ["mzb1_ws_a__codex__default", "mzb1_ws_b__claude__lane_1"],
        )
        self.assertEqual(first.plan["stores"]["attestation"]["target_version"], 3)
        self.assertEqual(first.plan["stores"]["lane_lifecycle"]["target_version"], 10)
        self.assertEqual(first.plan["stores"]["startup_transaction"]["target_version"], 2)
        self.assertEqual(
            first.plan["stores"]["startup_transaction"]["migration_plan_digest"],
            "2" * 64,
        )
        phases = [row["phase"] for row in first.plan["phase_order"]]
        self.assertEqual(
            phases,
            [
                "supervisor_stop",
                "non_top_workspace_stop",
                "top_workspace_stop",
                "consumer_zero",
                "verified_backup",
                "migrate_attestation",
                "migrate_lane_lifecycle",
                "migrate_startup_transaction",
                "exact_runtime_install",
                "top_restore_action_bootstrap",
                "remaining_workspace_restore",
                "supervisor_pair_install",
                "supervisor_pair_readback",
                "final_verify",
            ],
        )

    def test_payload_is_path_free_and_json_serializable(self) -> None:
        result = build_offline_rollout_plan(_capture())
        encoded = json.dumps(result.as_payload(), sort_keys=True)
        self.assertNotIn("canonical_path", encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertIn("unrelated_project", encoded)

    def test_duplicate_assigned_name_refuses(self) -> None:
        source = _capture()
        duplicate = replace(source.agents[1], workspace_id="ws_b")
        result = build_offline_rollout_plan(
            replace(source, agents=(source.agents[0], duplicate, source.agents[1]))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "duplicate_assigned_name")

    def test_unmanaged_agent_refuses(self) -> None:
        result = build_offline_rollout_plan(
            replace(_capture(), unmanaged_assigned_names=("foreign",))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unmanaged_agent_present")

    def test_unreadable_wip_refuses(self) -> None:
        source = _capture()
        workspaces = (replace(source.workspaces[0], wip_readable=False), source.workspaces[1])
        result = build_offline_rollout_plan(replace(source, workspaces=workspaces))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "wip_unreadable")

    def test_unknown_store_refuses(self) -> None:
        source = _capture()
        stores = (
            replace(source.stores[0], state="unsupported"),
            source.stores[1],
            source.stores[2],
        )
        result = build_offline_rollout_plan(replace(source, stores=stores))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "store_unreadable")

    def test_recognized_store_without_content_pin_refuses(self) -> None:
        source = _capture()
        stores = (
            replace(source.stores[0], content_digest=""),
            source.stores[1],
            source.stores[2],
        )
        result = build_offline_rollout_plan(replace(source, stores=stores))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "store_unreadable")

    def test_top_must_match_exactly_one_live_managed_row(self) -> None:
        source = _capture()
        result = build_offline_rollout_plan(
            replace(
                source,
                top_identity=replace(source.top_identity, assigned_name="missing"),
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "top_identity_unresolved")

    def test_only_a_complete_artifact_receipt_is_pin_ready(self) -> None:
        ready = build_offline_rollout_plan(_capture())
        self.assertTrue(ready.ok)
        self.assertTrue(ready.plan["candidate_artifact"]["exact_pin_ready"])
        self.assertEqual(
            ready.plan["candidate_artifact"],
            {
                "distribution": "testpypi",
                "version": "0.15.0a2",
                "source_sha": "a" * 40,
                "source_ref": "refs/heads/main",
                "workflow_run_id": "30821934713",
                "wheel_sha256": "d" * 64,
                "sdist_sha256": "e" * 64,
                "exact_pin_ready": True,
            },
        )
        for field in (
            "candidate_source_sha",
            "candidate_source_ref",
            "candidate_workflow_run_id",
            "candidate_wheel_sha256",
            "candidate_sdist_sha256",
        ):
            with self.subTest(missing=field):
                result = build_offline_rollout_plan(replace(_capture(), **{field: ""}))
                self.assertTrue(result.ok)
                self.assertFalse(result.plan["candidate_artifact"]["exact_pin_ready"])

    def test_malformed_artifact_receipt_fields_refuse(self) -> None:
        cases = (
            ("candidate_source_ref", "origin/main", "candidate_source_ref_invalid"),
            ("candidate_source_ref", "refs/heads/.hidden", "candidate_source_ref_invalid"),
            ("candidate_source_ref", "refs/heads/main.lock", "candidate_source_ref_invalid"),
            ("candidate_workflow_run_id", "0", "candidate_workflow_run_id_invalid"),
            ("candidate_workflow_run_id", "01", "candidate_workflow_run_id_invalid"),
            ("candidate_wheel_sha256", "A" * 64, "candidate_wheel_sha256_invalid"),
            ("candidate_sdist_sha256", "f" * 63, "candidate_sdist_sha256_invalid"),
        )
        for field, value, detail in cases:
            with self.subTest(field=field):
                result = build_offline_rollout_plan(
                    replace(_capture(), **{field: value})
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, "invalid_capture")
                self.assertEqual(result.detail, detail)

    def test_falsey_non_string_artifact_receipt_fields_refuse(self) -> None:
        fields = {
            "candidate_source_sha": "candidate_source_sha_invalid",
            "candidate_source_ref": "candidate_source_ref_invalid",
            "candidate_workflow_run_id": "candidate_workflow_run_id_invalid",
            "candidate_wheel_sha256": "candidate_wheel_sha256_invalid",
            "candidate_sdist_sha256": "candidate_sdist_sha256_invalid",
        }
        for field, detail in fields.items():
            for value in (None, 0, False, [], {}):
                with self.subTest(field=field, value_type=type(value).__name__):
                    result = build_offline_rollout_plan(
                        replace(_capture(), **{field: value})
                    )
                    self.assertFalse(result.ok)
                    self.assertEqual(result.reason, "invalid_capture")
                    self.assertEqual(result.detail, detail)

    def test_artifact_receipt_is_part_of_the_plan_digest(self) -> None:
        first = build_offline_rollout_plan(_capture())
        changed = build_offline_rollout_plan(
            replace(_capture(), candidate_workflow_run_id="30821934714")
        )
        self.assertNotEqual(first.plan_digest, changed.plan_digest)

    def test_owned_supervisor_pair_is_exactly_required(self) -> None:
        source = _capture()
        missing = build_offline_rollout_plan(
            replace(source, supervisors=source.supervisors[:1])
        )
        foreign = build_offline_rollout_plan(
            replace(
                source,
                supervisors=(
                    source.supervisors[0],
                    replace(source.supervisors[1], label="foreign"),
                ),
            )
        )
        self.assertEqual(missing.reason, "supervisor_set_invalid")
        self.assertEqual(foreign.reason, "supervisor_set_invalid")

    def test_public_plan_copy_cannot_mutate_digest_authority(self) -> None:
        result = build_offline_rollout_plan(_capture())
        mutated = result.plan
        mutated["schema_transitions"][0]["to_version"] = 999

        fresh = result.plan
        self.assertNotEqual(fresh["schema_transitions"][0]["to_version"], 999)
        canonical = json.dumps(
            fresh,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(result.plan_digest, hashlib.sha256(canonical).hexdigest())


if __name__ == "__main__":
    unittest.main()
