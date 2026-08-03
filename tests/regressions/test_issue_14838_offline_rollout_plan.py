from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.workspace_registry import WorkspaceRecord
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_toctou import (  # noqa: E501
    WorktreeMutationFingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_offline_rollout import (  # noqa: E501
    register_herdr_offline_rollout_parser,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_plan import (  # noqa: E501
    StoreSnapshot,
    build_offline_rollout_plan,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    HerdrObservedAgent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_snapshot import (  # noqa: E501
    capture_offline_rollout_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)


class OfflineRolloutSnapshotRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.other = self.root / "other"
        self.top_name = encode_assigned_name("ws_main", "codex", "default")
        self.peer_name = encode_assigned_name("ws_other", "claude", "lane_1")
        self.records = (
            WorkspaceRecord(
                "ws_main",
                str(self.repo),
                str(self.repo),
                "mozyo_bridge",
                "main",
                None,
                None,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                None,
            ),
            WorkspaceRecord(
                "ws_other",
                str(self.other),
                str(self.other),
                "unrelated",
                "other",
                None,
                None,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
        self.view = HerdrInventoryView(
            backend_selected=True,
            ok=True,
            workspace_segment="ws_main",
            agents=(
                HerdrObservedAgent(
                    name=self.top_name,
                    managed=True,
                    workspace_id="ws_main",
                    lane_id="default",
                    role="codex",
                    runtime_state="working",
                    raw_status="working",
                ),
                HerdrObservedAgent(
                    name=self.peer_name,
                    managed=True,
                    workspace_id="ws_other",
                    lane_id="lane_1",
                    role="claude",
                    runtime_state="idle",
                    raw_status="idle",
                ),
            ),
        )

    def _kwargs(self) -> dict:
        return {
            "repo_root": self.repo,
            "home": self.home,
            "candidate_version": "0.15.0a2",
            "candidate_source_sha": "d" * 40,
            "env": {
                "MOZYO_WORKSPACE_ID": "ws_main",
                "MOZYO_AGENT_ROLE": "codex",
                "MOZYO_LANE_ID": "default",
            },
            "inventory_reader": lambda repo_root, env: self.view,
            "registry_health_reader": lambda home: {"status": "ok"},
            "workspace_reader": lambda *, home: self.records,
            "worktree_reader": lambda path, timeout: WorktreeMutationFingerprint(
                readable=True,
                dirty=path == self.other,
                untracked=False,
                digest=("a" if path == self.repo else "b") * 64,
            ),
            "store_reader": lambda home: (
                StoreSnapshot("attestation", "recognized", 1),
                StoreSnapshot("lane_lifecycle", "recognized", 9),
                StoreSnapshot("startup_transaction", "recognized", 1),
            ),
            "supervisor_reader": lambda *, mozyo_home: {
                "agents": [
                    {
                        "label": "jp.giken.mozyo-bridge.reconcile",
                        "installed": True,
                        "loaded": True,
                        "pid": 42,
                        "home_pin": "ok",
                        "executable_matches": True,
                        "credential_readiness": "ready",
                    }
                ]
            },
        }

    def test_live_adapter_captures_all_workspaces_and_redacts_paths(self) -> None:
        captured = capture_offline_rollout_snapshot(**self._kwargs())
        result = build_offline_rollout_plan(captured)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.plan["workspaces"]), 2)
        self.assertEqual(result.plan["workspaces"][1]["scope"], "unrelated_project")
        payload = str(result.as_payload())
        self.assertNotIn(str(self.root), payload)

    def test_inventory_drift_refuses_without_a_plan(self) -> None:
        calls = []

        def drifting(repo_root, env):
            calls.append(1)
            if len(calls) == 1:
                return self.view
            return HerdrInventoryView(
                backend_selected=True,
                ok=True,
                workspace_segment="ws_main",
                agents=self.view.agents[:1],
            )

        kwargs = self._kwargs()
        kwargs["inventory_reader"] = drifting
        result = capture_offline_rollout_snapshot(**kwargs)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "snapshot_drift")
        self.assertIsNone(result.plan)

    def test_unhealthy_registry_refuses_before_inventory(self) -> None:
        kwargs = self._kwargs()
        kwargs["registry_health_reader"] = lambda home: {"status": "unreadable"}
        result = capture_offline_rollout_snapshot(**kwargs)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "workspace_registry_unreadable")

    def test_parser_exposes_only_plan_in_phase_a(self) -> None:
        parser = argparse.ArgumentParser()
        herdr_sub = parser.add_subparsers(dest="command", required=True)
        register_herdr_offline_rollout_parser(
            herdr_sub,
            add_repo_option=lambda target: target.add_argument("--repo"),
        )
        parsed = parser.parse_args(
            [
                "offline-rollout",
                "plan",
                "--candidate-version",
                "0.15.0a2",
                "--json",
            ]
        )
        self.assertEqual(parsed.offline_rollout_command, "plan")
        with self.assertRaises(SystemExit):
            parser.parse_args(["offline-rollout", "run"])


if __name__ == "__main__":
    unittest.main()
