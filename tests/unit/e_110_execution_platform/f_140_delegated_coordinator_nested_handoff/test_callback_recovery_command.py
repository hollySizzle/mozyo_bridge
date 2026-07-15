"""Recovery-plan command assembly tests (Redmine #13520 j#75276 / review F4).

The read-only surface probes git (injected), the outbox backlog, the workspace anchors, and the
sender env, and returns the pure reconciler plan. No mutation, fail-safe probes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_recovery_command import (
    build_observation,
    recovery_plan_from_observation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_reconciler import (
    RECOVERY_FAIL_CLOSED,
    RECOVERY_NEEDS_RECOVERY,
    RECOVERY_READY,
    STEP_REATTEST_SENDER,
    STEP_RELAUNCH_STALE_SLOT,
    RuntimeSlot,
)

WS = "e1487dcb"


def _clean_git(argv):
    return 0, ""  # worktree present, clean


def _dirty_git(argv):
    return 0, " M src/x.py\n"


def _absent_git(argv):
    return 128, ""  # not a git worktree


class BuildObservationTest(unittest.TestCase):
    def test_healthy_env_and_clean_git_is_ready(self):
        obs = build_observation(
            workspace_id_expected=WS, workspace_id_registry=WS, redmine_anchor_readable=True,
            repo_root="/repo", outbox_present=True, outbox_pending=0, outbox_uncertain=0,
            env={"MOZYO_WORKSPACE_ID": WS, "MOZYO_AGENT_ROLE": "claude"},
            git_runner=_clean_git,
        )
        self.assertTrue(obs.git_worktree_present)
        self.assertFalse(obs.git_dirty)
        self.assertTrue(obs.sender_env_present)
        self.assertEqual(recovery_plan_from_observation(obs).status, RECOVERY_READY)

    def test_missing_sender_env_and_dirty_git_needs_recovery(self):
        obs = build_observation(
            workspace_id_expected=WS, workspace_id_registry=WS, redmine_anchor_readable=True,
            repo_root="/repo", outbox_present=True, outbox_pending=1, outbox_uncertain=0,
            env={},  # launch-time MOZYO_* absent (reboot)
            git_runner=_dirty_git,
        )
        self.assertTrue(obs.git_dirty)
        self.assertFalse(obs.sender_env_present)
        plan = recovery_plan_from_observation(obs)
        self.assertEqual(plan.status, RECOVERY_NEEDS_RECOVERY)
        self.assertIn(STEP_REATTEST_SENDER, [s.kind for s in plan.steps])

    def test_absent_worktree_fails_closed(self):
        obs = build_observation(
            workspace_id_expected=WS, workspace_id_registry=WS, redmine_anchor_readable=True,
            repo_root="/repo", outbox_present=True, outbox_pending=0, outbox_uncertain=0,
            env={"MOZYO_WORKSPACE_ID": WS, "MOZYO_AGENT_ROLE": "claude"},
            git_runner=_absent_git,
        )
        self.assertFalse(obs.git_worktree_present)
        self.assertEqual(recovery_plan_from_observation(obs).status, RECOVERY_FAIL_CLOSED)

    def test_git_probe_failure_is_fail_safe_absent(self):
        def boom(argv):
            raise OSError("no git")

        obs = build_observation(
            workspace_id_expected=WS, workspace_id_registry=WS, redmine_anchor_readable=True,
            repo_root="/repo", outbox_present=True, outbox_pending=0, outbox_uncertain=0,
            env={"MOZYO_WORKSPACE_ID": WS, "MOZYO_AGENT_ROLE": "claude"}, git_runner=boom,
        )
        self.assertFalse(obs.git_worktree_present)  # fail-safe: unobservable worktree -> fail-closed

    def test_injected_slot_probe_drives_stale_relaunch_step(self):
        obs = build_observation(
            workspace_id_expected=WS, workspace_id_registry=WS, redmine_anchor_readable=True,
            repo_root="/repo", outbox_present=True, outbox_pending=0, outbox_uncertain=0,
            env={"MOZYO_WORKSPACE_ID": WS, "MOZYO_AGENT_ROLE": "claude"}, git_runner=_clean_git,
            slot_probe=lambda: [RuntimeSlot("mzb1_ws_codex_lane", "stale_named_slot")],
        )
        plan = recovery_plan_from_observation(obs)
        self.assertIn(STEP_RELAUNCH_STALE_SLOT, [s.kind for s in plan.steps])


if __name__ == "__main__":
    unittest.main()
