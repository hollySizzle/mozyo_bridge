from __future__ import annotations

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
            StoreSnapshot("startup_transaction", "recognized", 1),
            StoreSnapshot("attestation", "recognized", 1),
            StoreSnapshot("lane_lifecycle", "recognized", 9),
        ),
        supervisors=(
            SupervisorAgentSnapshot(
                "jp.giken.mozyo-bridge.reconcile",
                True,
                True,
                123,
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

    def test_candidate_without_source_sha_is_explicitly_not_pin_ready(self) -> None:
        result = build_offline_rollout_plan(
            replace(_capture(), candidate_source_sha="")
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.plan["candidate_artifact"]["exact_pin_ready"])


if __name__ == "__main__":
    unittest.main()
