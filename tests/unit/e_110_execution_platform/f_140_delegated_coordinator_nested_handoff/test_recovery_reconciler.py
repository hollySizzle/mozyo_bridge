"""Multi-authority reboot recovery reconciler tests (Redmine #13520 j#75276 / review F4).

Positive (authorities agree -> resume), negative (each contradiction -> fail-closed, never-clobber),
and the reboot scenario (stale slot + missing sender env + backlog -> safe idempotent recovery plan
with the dirty worktree preserved).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_reconciler import (
    BLOCK_AMBIGUOUS_LIVE_SLOT,
    BLOCK_ANCHOR_UNREADABLE,
    BLOCK_DB_CONTRADICTION,
    BLOCK_WORKSPACE_MISMATCH,
    BLOCK_WORKTREE_ABSENT,
    RECOVERY_FAIL_CLOSED,
    RECOVERY_NEEDS_RECOVERY,
    RECOVERY_READY,
    STEP_PRESERVE_DIRTY_WORKTREE,
    STEP_REATTEST_SENDER,
    STEP_RELAUNCH_STALE_SLOT,
    STEP_REPLAY_OUTBOX,
    STEP_RESTART_WATCHER,
    STEP_RESUME_EXACT_JOURNAL,
    AuthorityObservation,
    RuntimeSlot,
    build_recovery_plan,
)

WS = "e1487dcb"


def _healthy(**over) -> AuthorityObservation:
    base = dict(
        workspace_id_expected=WS,
        workspace_id_registry=WS,
        redmine_anchor_readable=True,
        git_worktree_present=True,
        git_dirty=False,
        outbox_present=True,
        outbox_pending=0,
        outbox_uncertain=0,
        outbox_workspace_id=WS,
        runtime_slots=(RuntimeSlot("mzb1_ws_codex_lane", "live"), RuntimeSlot("mzb1_ws_claude_lane", "live")),
        sender_env_present=True,
    )
    base.update(over)
    return AuthorityObservation(**base)


def _kinds(plan):
    return [s.kind for s in plan.steps]


class PositiveTest(unittest.TestCase):
    def test_all_authorities_agree_is_ready_to_resume(self):
        plan = build_recovery_plan(_healthy())
        self.assertEqual(plan.status, RECOVERY_READY)
        self.assertTrue(plan.ok)
        self.assertEqual(_kinds(plan), [])


class FailClosedTest(unittest.TestCase):
    def test_workspace_mismatch_fails_closed_never_clobber(self):
        plan = build_recovery_plan(_healthy(workspace_id_registry="different"))
        self.assertEqual(plan.status, RECOVERY_FAIL_CLOSED)
        self.assertIn(BLOCK_WORKSPACE_MISMATCH, plan.blockers)
        self.assertEqual(plan.steps, ())  # no apply steps across a contradiction
        self.assertFalse(plan.ok)
        self.assertTrue(any("never-clobber" in n for n in plan.notes))

    def test_unreadable_anchor_fails_closed(self):
        plan = build_recovery_plan(_healthy(redmine_anchor_readable=False))
        self.assertEqual(plan.status, RECOVERY_FAIL_CLOSED)
        self.assertIn(BLOCK_ANCHOR_UNREADABLE, plan.blockers)

    def test_absent_worktree_fails_closed(self):
        plan = build_recovery_plan(_healthy(git_worktree_present=False))
        self.assertIn(BLOCK_WORKTREE_ABSENT, plan.blockers)
        self.assertEqual(plan.status, RECOVERY_FAIL_CLOSED)

    def test_ambiguous_live_slot_fails_closed(self):
        plan = build_recovery_plan(
            _healthy(runtime_slots=(RuntimeSlot("mzb1_ws_codex_lane", "live", count=2),))
        )
        self.assertIn(BLOCK_AMBIGUOUS_LIVE_SLOT, plan.blockers)
        self.assertEqual(plan.status, RECOVERY_FAIL_CLOSED)

    def test_db_workspace_contradiction_fails_closed(self):
        # The state DB references a different workspace than the durable anchor: the DB is not the
        # authority, so a mismatch is a stop, never a silent adopt.
        plan = build_recovery_plan(_healthy(outbox_workspace_id="other_ws"))
        self.assertIn(BLOCK_DB_CONTRADICTION, plan.blockers)
        self.assertEqual(plan.status, RECOVERY_FAIL_CLOSED)

    def test_dirty_worktree_is_never_clobbered_even_when_fail_closed(self):
        plan = build_recovery_plan(_healthy(workspace_id_registry="x", git_dirty=True))
        self.assertEqual(plan.status, RECOVERY_FAIL_CLOSED)
        # No apply steps at all across a contradiction (so certainly no reset/stash mutation), and
        # the note explicitly affirms the dirty worktree is preserved.
        self.assertEqual(plan.steps, ())
        self.assertTrue(any("never-clobber" in n for n in plan.notes))


class RebootScenarioTest(unittest.TestCase):
    def test_reboot_residue_plus_missing_env_plus_backlog_is_safe_recovery(self):
        plan = build_recovery_plan(
            _healthy(
                git_dirty=True,
                sender_env_present=False,
                outbox_pending=2,
                outbox_uncertain=1,
                runtime_slots=(
                    RuntimeSlot("mzb1_ws_codex_lane", "stale_named_slot"),
                    RuntimeSlot("mzb1_ws_claude_lane", "live"),
                ),
            )
        )
        self.assertEqual(plan.status, RECOVERY_NEEDS_RECOVERY)
        kinds = _kinds(plan)
        self.assertIn(STEP_PRESERVE_DIRTY_WORKTREE, kinds)  # never-clobber
        self.assertIn(STEP_RELAUNCH_STALE_SLOT, kinds)
        self.assertIn(STEP_REATTEST_SENDER, kinds)
        self.assertIn(STEP_REPLAY_OUTBOX, kinds)
        self.assertIn(STEP_RESUME_EXACT_JOURNAL, kinds)
        self.assertIn(STEP_RESTART_WATCHER, kinds)

    def test_security_sensitive_steps_are_gated(self):
        plan = build_recovery_plan(
            _healthy(
                sender_env_present=False,
                runtime_slots=(RuntimeSlot("mzb1_ws_codex_lane", "stale_named_slot"),),
            )
        )
        relaunch = next(s for s in plan.steps if s.kind == STEP_RELAUNCH_STALE_SLOT)
        reattest = next(s for s in plan.steps if s.kind == STEP_REATTEST_SENDER)
        self.assertTrue(relaunch.requires_owner_approval)  # destructive close+relaunch is owner-gated
        self.assertTrue(reattest.requires_verified_reattestation)  # identity re-injection is verified/sanctioned

    def test_backlog_only_needs_replay_and_resume_not_relaunch(self):
        plan = build_recovery_plan(_healthy(outbox_pending=1))
        kinds = _kinds(plan)
        self.assertIn(STEP_REPLAY_OUTBOX, kinds)
        self.assertIn(STEP_RESUME_EXACT_JOURNAL, kinds)
        self.assertNotIn(STEP_RELAUNCH_STALE_SLOT, kinds)
        self.assertEqual(plan.status, RECOVERY_NEEDS_RECOVERY)


if __name__ == "__main__":
    unittest.main()
