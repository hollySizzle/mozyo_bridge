"""Explicit supervisor home remains authoritative through retire actuation (#15066)."""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.lane_lifecycle import (
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
    resolve_retire_evidence_target,
)


class RetireHomeScopeRegressionTest(unittest.TestCase):
    def test_cli_carries_explicit_home_into_shared_retire_facade(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_lifecycle_command as command,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_application import (  # noqa: E501
            RETIRE_RESULT_UNCERTAIN,
            RetireApplicationResult,
        )

        state_home = Path("/isolated-state")
        target = SimpleNamespace(
            workspace="ws_1", lane="issue_15066_lane", lane_generation=2, revision=4
        )
        args = argparse.Namespace(
            repo="/repo",
            home=state_home,
            issue="15066",
            lane_label="issue_15066_lane",
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=False,
            retire_active_live_zero=False,
            retire_active_unbound_live_zero=False,
            retire_hibernated_unbound_live_zero=False,
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            json=True,
        )
        captured = []
        with patch.object(
            command, "resolve_retire_evidence_target", return_value=target
        ) as resolve, patch.object(
            command,
            "_resolve_latest_generation_admissible",
            return_value=SimpleNamespace(admissible=True, reason=""),
        ), patch.object(
            command,
            "run_retire_application",
            side_effect=lambda request: (
                captured.append(request)
                or RetireApplicationResult(state=RETIRE_RESULT_UNCERTAIN)
            ),
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(command.cmd_sublane_retire(args), 1)

        resolve.assert_called_once_with(args, Path("/repo"), home=state_home)
        self.assertEqual(captured[0].home, state_home)

    def test_linked_worktree_uses_the_explicit_home_for_identity_and_lane_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "main"
            lane = root / "lane"
            state_home = root / "state-home"
            unrelated_home = root / "unrelated-home"
            main.mkdir()
            state_home.mkdir()
            unrelated_home.mkdir()
            subprocess.run(["git", "init", "-q", str(main)], check=True)
            subprocess.run(
                ["git", "-C", str(main), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(main), "config", "user.name", "Test"], check=True
            )
            (main / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(main), "add", "tracked.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(main), "commit", "-qm", "base"], check=True
            )
            registered = register_workspace(main, home=state_home)
            workspace = registered.record.workspace_id
            registered.anchor_path.unlink()
            subprocess.run(
                ["git", "-C", str(main), "worktree", "add", "-qb", "lane", str(lane)],
                check=True,
            )

            key = LaneLifecycleKey(workspace, "issue_15066_lane")
            store = LaneLifecycleStore(home=state_home)
            store.declare_active(
                key,
                decision=DecisionPointer(
                    source="redmine", issue_id="15066", journal_id="100711"
                ),
                issue_id="15066",
                worktree_identity="wt_test",
            )
            row = store.get(key)
            self.assertIsNotNone(row)
            args = argparse.Namespace(lane_label=key.lane_id)
            with patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": str(unrelated_home)}, clear=False
            ):
                target = resolve_retire_evidence_target(
                    args, lane, home=state_home
                )
                wrong_home_target = resolve_retire_evidence_target(
                    args, lane, home=unrelated_home
                )

            self.assertIsNotNone(target)
            self.assertEqual(target.workspace, workspace)
            self.assertEqual(target.issue, "15066")
            self.assertEqual(target.lane_generation, row.lane_generation)
            self.assertEqual(target.revision, row.revision)
            self.assertIsNone(wrong_home_target)


if __name__ == "__main__":
    unittest.main()
