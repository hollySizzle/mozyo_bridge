"""Pure sublane lifecycle projection / planning tests (Redmine #12955).

Pins the pure core of the ``mozyo-bridge sublane`` lifecycle MVP:

- :func:`project_sublanes` — folds a tmux pane inventory into one lane view per sublane
  (default lane skipped, gateway/worker picked by role, issue parsed, branch from the
  caller lookup, coarse state);
- :func:`plan_sublane_create` — the fail-closed launch plan (missing identity, blocked
  launch, create vs reuse vs skip);
- :func:`preflight_sublane_retire` — the fail-closed retire preflight (blocked => empty
  runbook; ok => destructive runbook with no remote-branch deletion; non-Git skips the
  worktree/branch steps).

Pure decisions only — no IO, no git, no use case (those are the integration tests).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    INTEGRATION_BLOCKED,
    LAUNCH_BLOCKED,
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_REUSE_WORKTREE,
    LAUNCH_SKIP_NO_GIT,
    RETIRE_OK,
    RetireDecision,
    WorktreeLaunchDecision,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    CREATE_BLOCKED,
    CREATE_PLANNED,
    SUBLANE_STATE_ACTIVE,
    SUBLANE_STATE_GATEWAY_ONLY,
    SUBLANE_STATE_WORKER_ONLY,
    SublaneCreateRequest,
    parse_issue_from_lane_label,
    plan_sublane_create,
    preflight_sublane_retire,
    project_sublanes,
)


def _row(**kw):
    base = {
        "id": "",
        "location": "",
        "command": "",
        "cwd": "",
        "window_name": "",
        "pane_active": "1",
        "agent_role": "",
        "workspace_id": "ws",
        "lane_id": "",
        "lane_label": "",
        "repo_root_stamp": "",
    }
    base.update(kw)
    return base


class ParseIssueTests(unittest.TestCase):
    def test_parses_issue_from_conventional_label(self):
        self.assertEqual(
            parse_issue_from_lane_label("issue_12955_sublane_lifecycle_command"),
            "12955",
        )

    def test_returns_none_for_unconventional_label(self):
        self.assertIsNone(parse_issue_from_lane_label("scratch-lane"))
        self.assertIsNone(parse_issue_from_lane_label(""))


class ProjectSublanesTests(unittest.TestCase):
    def test_groups_by_lane_and_picks_gateway_worker(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1",
                 lane_label="issue_100_a", repo_root_stamp="/wt/a"),
            _row(id="%2", agent_role="claude", lane_id="l1",
                 lane_label="issue_100_a", repo_root_stamp="/wt/a"),
        ]
        views = project_sublanes(rows, branches={"l1": "issue_100_a"})
        self.assertEqual(len(views), 1)
        v = views[0]
        self.assertEqual(v.lane_id, "l1")
        self.assertEqual(v.issue, "100")
        self.assertEqual(v.gateway_pane, "%1")
        self.assertEqual(v.worker_pane, "%2")
        self.assertEqual(v.branch, "issue_100_a")
        self.assertEqual(v.repo_root, "/wt/a")
        self.assertEqual(v.state, SUBLANE_STATE_ACTIVE)

    def test_skips_default_lane(self):
        rows = [
            _row(id="%1", agent_role="claude", lane_id="default"),
            _row(id="%2", agent_role="claude", lane_id=""),  # empty normalizes to default
        ]
        self.assertEqual(project_sublanes(rows), [])

    def test_skips_main_lane_with_hashed_lane_id(self):
        # Regression (#12955 j#69954): the live main / coordinator lane carries a hashed
        # non-default lane id and only its label reads "main"; it must not appear as a
        # sublane alongside real ones.
        rows = [
            _row(id="%2", agent_role="codex", lane_id="lane-124611ffed3c",
                 lane_label="main"),
            _row(id="%3", agent_role="claude", lane_id="lane-124611ffed3c",
                 lane_label="main"),
            _row(id="%93", agent_role="codex", lane_id="lane-12955",
                 lane_label="issue_12955_x"),
            _row(id="%99", agent_role="claude", lane_id="lane-12955",
                 lane_label="issue_12955_x"),
        ]
        views = project_sublanes(rows)
        self.assertEqual([v.lane_label for v in views], ["issue_12955_x"])

    def test_skips_main_lane_by_kind(self):
        rows = [
            _row(id="%2", agent_role="codex", lane_id="lane-abc",
                 lane_label="", lane_kind="main"),
        ]
        self.assertEqual(project_sublanes(rows), [])

    def test_state_gateway_only_and_worker_only(self):
        gw = project_sublanes([_row(id="%1", agent_role="codex", lane_id="l1")])
        self.assertEqual(gw[0].state, SUBLANE_STATE_GATEWAY_ONLY)
        self.assertIsNone(gw[0].worker_pane)
        wk = project_sublanes([_row(id="%2", agent_role="claude", lane_id="l2")])
        self.assertEqual(wk[0].state, SUBLANE_STATE_WORKER_ONLY)

    def test_branch_none_when_lookup_missing(self):
        views = project_sublanes([_row(id="%1", agent_role="codex", lane_id="l1")])
        self.assertIsNone(views[0].branch)

    def test_repo_root_falls_back_to_cwd(self):
        rows = [_row(id="%1", agent_role="claude", lane_id="l1", cwd="/wt/x")]
        self.assertEqual(project_sublanes(rows)[0].repo_root, "/wt/x")


def _req(**kw):
    base = dict(
        issue="12955",
        lane_label="issue_12955_x",
        branch="issue_12955_x",
        worktree_path="/wt/12955",
        journal="69879",
        upstream_coordinator="%2",
    )
    base.update(kw)
    return SublaneCreateRequest(**base)


class PlanCreateTests(unittest.TestCase):
    def _launch(self, action):
        return WorktreeLaunchDecision(action=action, reason="r")

    def test_planned_create_emits_worktree_add_and_dispatch(self):
        plan = plan_sublane_create(_req(), self._launch(LAUNCH_CREATE_WORKTREE))
        self.assertEqual(plan.status, CREATE_PLANNED)
        self.assertEqual(plan.steps[0].command, "git worktree add /wt/12955 -b issue_12955_x")
        # dispatch step carries the issue + role profile + lane
        dispatch = plan.steps[-1]
        self.assertIn("--issue 12955", dispatch.command)
        self.assertIn("implementation_gateway", dispatch.command)
        self.assertIn("lane=issue_12955_x", dispatch.command)

    def test_reuse_worktree_has_no_add_command(self):
        plan = plan_sublane_create(_req(), self._launch(LAUNCH_REUSE_WORKTREE))
        self.assertEqual(plan.status, CREATE_PLANNED)
        self.assertIsNone(plan.steps[0].command)
        self.assertEqual(plan.steps[0].title, "reuse worktree")

    def test_skip_no_git_still_plans_panes(self):
        plan = plan_sublane_create(_req(), self._launch(LAUNCH_SKIP_NO_GIT))
        self.assertEqual(plan.status, CREATE_PLANNED)
        self.assertEqual(plan.steps[0].title, "skip worktree")
        self.assertEqual(len(plan.steps), 4)

    def test_missing_identity_fails_closed_with_no_steps(self):
        plan = plan_sublane_create(
            _req(worktree_path=""), self._launch(LAUNCH_CREATE_WORKTREE)
        )
        self.assertEqual(plan.status, CREATE_BLOCKED)
        self.assertEqual(plan.steps, ())
        self.assertIn("missing_field:worktree_path", plan.blocked_reasons)

    def test_blocked_launch_fails_closed(self):
        plan = plan_sublane_create(_req(), self._launch(LAUNCH_BLOCKED))
        self.assertEqual(plan.status, CREATE_BLOCKED)
        self.assertEqual(plan.steps, ())
        self.assertIn(LAUNCH_BLOCKED, plan.blocked_reasons)


class PreflightRetireTests(unittest.TestCase):
    def _blocked(self):
        return RetireDecision(
            state=INTEGRATION_BLOCKED,
            blocked_reasons=("dirty_worktree",),
            primary_reason="dirty_worktree",
        )

    def _ok(self):
        return RetireDecision(state=RETIRE_OK)

    def test_blocked_has_empty_runbook(self):
        pre = preflight_sublane_retire(
            self._blocked(), issue="12955", lane_label="issue_12955_x",
            worktree_path="/wt", branch="b",
        )
        self.assertFalse(pre.may_retire)
        self.assertEqual(pre.runbook, ())
        self.assertIn("integration_blocked", pre.journal)

    def test_ok_runbook_lists_destructive_steps_but_no_remote_delete(self):
        pre = preflight_sublane_retire(
            self._ok(), issue="12955", lane_label="issue_12955_x",
            worktree_path="/wt/12955", branch="issue_12955_x",
        )
        self.assertTrue(pre.may_retire)
        commands = "\n".join(s.command or "" for s in pre.runbook)
        self.assertIn("git worktree remove /wt/12955", commands)
        self.assertIn("git branch -d issue_12955_x", commands)
        # never a remote-branch deletion
        self.assertNotIn("push", commands)
        self.assertNotIn("branch -D", commands)
        self.assertNotIn("origin", commands)

    def test_non_git_ok_skips_worktree_and_branch_steps(self):
        pre = preflight_sublane_retire(
            self._ok(), issue="12955", lane_label="issue_12955_x",
            worktree_path="/wt/12955", branch="b", is_git_workspace=False,
        )
        titles = [s.title for s in pre.runbook]
        self.assertNotIn("remove worktree", titles)
        self.assertNotIn("delete local branch", titles)


if __name__ == "__main__":
    unittest.main()
