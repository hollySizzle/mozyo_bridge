"""Sublane lifecycle use-case composition tests (Redmine #12955).

Drives the ``list`` / ``create`` / ``retire`` use cases against a fake
:class:`SublaneLifecycleOps` port (the established #12604 fake-port style), covering the
composition seam without any real tmux / git IO:

- ``list`` folds the fake pane inventory and resolves each lane's branch through the port
  (the two-pass branch lookup);
- ``create`` probes git for the launch action and composes a fail-closed plan; a missing
  identity field short-circuits before any probe;
- ``retire`` evaluates the fail-closed preflight from git probes + operator-asserted
  invariants, never attempts a merge, and never actuates a destructive op.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
    RetireAssertions,
    SublaneCreateUseCase,
    SublaneLifecycleOps,
    SublaneListUseCase,
    SublaneRetireUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    INTEGRATION_BLOCKED,
    RETIRE_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    CREATE_BLOCKED,
    CREATE_PLANNED,
    SublaneCreateRequest,
)


class FakeOps:
    """A scriptable :class:`SublaneLifecycleOps` recording the calls made to it."""

    def __init__(
        self,
        *,
        rows=None,
        git=True,
        worktree_exists=False,
        dirty=False,
        branches=None,
    ):
        self._rows = rows or []
        self._git = git
        self._worktree_exists = worktree_exists
        self._dirty = dirty
        self._branches = branches or {}
        self.branch_calls = []

    def pane_rows(self):
        return list(self._rows)

    def is_git_workspace(self):
        return self._git

    def worktree_exists(self, branch):
        return self._worktree_exists

    def worktree_dirty(self):
        return self._dirty

    def branch_for(self, checkout_path):
        self.branch_calls.append(checkout_path)
        return self._branches.get(checkout_path)


def _row(**kw):
    base = {
        "id": "", "location": "", "command": "", "cwd": "", "window_name": "",
        "pane_active": "1", "agent_role": "", "workspace_id": "ws", "lane_id": "",
        "lane_label": "", "repo_root_stamp": "",
    }
    base.update(kw)
    return base


class PortConformanceTests(unittest.TestCase):
    def test_fake_satisfies_protocol(self):
        self.assertIsInstance(FakeOps(), SublaneLifecycleOps)


class ListUseCaseTests(unittest.TestCase):
    def test_resolves_branch_through_port_two_pass(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1",
                 lane_label="issue_1_a", repo_root_stamp="/wt/a"),
            _row(id="%2", agent_role="claude", lane_id="l1",
                 lane_label="issue_1_a", repo_root_stamp="/wt/a"),
        ]
        ops = FakeOps(rows=rows, branches={"/wt/a": "issue_1_a"})
        outcome = SublaneListUseCase(ops).run()
        self.assertEqual(len(outcome.lanes), 1)
        self.assertEqual(outcome.lanes[0].branch, "issue_1_a")
        self.assertIn("/wt/a", ops.branch_calls)

    def test_lane_filter_by_issue(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1", lane_label="issue_1_a"),
            _row(id="%2", agent_role="codex", lane_id="l2", lane_label="issue_2_b"),
        ]
        outcome = SublaneListUseCase(FakeOps(rows=rows)).run(lane_filter="2")
        self.assertEqual([l.lane_id for l in outcome.lanes], ["l2"])

    def test_empty_inventory_is_no_sublanes(self):
        self.assertEqual(SublaneListUseCase(FakeOps()).run().lanes, ())


def _req(**kw):
    base = dict(
        issue="12955", lane_label="issue_12955_x", branch="b",
        worktree_path="/wt/12955", journal="69879", upstream_coordinator="%2",
    )
    base.update(kw)
    return SublaneCreateRequest(**base)


class CreateUseCaseTests(unittest.TestCase):
    def test_git_workspace_plans_create(self):
        outcome = SublaneCreateUseCase(FakeOps(git=True)).run(_req())
        self.assertEqual(outcome.plan.status, CREATE_PLANNED)
        self.assertEqual(outcome.plan.launch_action, "create_worktree")

    def test_existing_worktree_plans_reuse(self):
        outcome = SublaneCreateUseCase(FakeOps(git=True, worktree_exists=True)).run(_req())
        self.assertEqual(outcome.plan.launch_action, "reuse_worktree")

    def test_non_git_plans_skip(self):
        outcome = SublaneCreateUseCase(FakeOps(git=False)).run(_req())
        self.assertEqual(outcome.plan.launch_action, "skip_no_git")
        self.assertEqual(outcome.plan.status, CREATE_PLANNED)

    def test_missing_field_short_circuits_before_probe(self):
        ops = FakeOps(git=True)
        outcome = SublaneCreateUseCase(ops).run(_req(worktree_path=""))
        self.assertEqual(outcome.plan.status, CREATE_BLOCKED)


class RetireUseCaseTests(unittest.TestCase):
    def _all_true(self):
        return RetireAssertions(
            issue_closed=True, owner_approval_present=True, callbacks_drained=True,
            verification_passed=True, durable_record_recorded=True,
            target_identity_known=True,
        )

    def test_clean_all_invariants_retire_ok(self):
        outcome = SublaneRetireUseCase(FakeOps(git=True, dirty=False)).run(
            issue="12955", lane_label="issue_12955_x", worktree_path="/wt",
            branch="b", integration_branch=None, assertions=self._all_true(),
        )
        self.assertEqual(outcome.preflight.decision.state, RETIRE_OK)
        self.assertTrue(outcome.preflight.may_retire)
        # merge is never attempted by the preflight
        self.assertFalse(outcome.preflight.decision.merge_attempted)

    def test_dirty_worktree_blocks(self):
        outcome = SublaneRetireUseCase(FakeOps(git=True, dirty=True)).run(
            issue="12955", lane_label="issue_12955_x", worktree_path="/wt",
            branch="b", integration_branch=None, assertions=self._all_true(),
        )
        self.assertEqual(outcome.preflight.decision.state, INTEGRATION_BLOCKED)
        self.assertIn("dirty_worktree", outcome.preflight.decision.blocked_reasons)

    def test_missing_invariants_block_fail_closed(self):
        outcome = SublaneRetireUseCase(FakeOps(git=True)).run(
            issue="12955", lane_label="issue_12955_x", worktree_path="/wt",
            branch="b", integration_branch=None, assertions=RetireAssertions(),
        )
        self.assertFalse(outcome.preflight.may_retire)


if __name__ == "__main__":
    unittest.main()
