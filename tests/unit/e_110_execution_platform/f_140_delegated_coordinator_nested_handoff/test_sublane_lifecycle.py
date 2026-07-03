"""Pure sublane lifecycle projection / planning tests (Redmine #12955).

Pins the pure core of the ``mozyo-bridge sublane`` lifecycle MVP:

- :func:`project_sublanes` — folds a tmux pane inventory into one lane view per sublane
  (default lane skipped, gateway/worker picked by role, issue parsed, branch from the
  caller lookup, coarse state), plus the #13086 host-window identity (shared with the
  ``agents list`` / ``agents targets`` discovery vocabulary) and the machine-readable
  stale / retire hints (pane missing / window split / duplicate issue lane /
  unresolved worktree / branch integrated — advisory, never fabricated from unknowns);
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
    STALE_HINT_BRANCH_INTEGRATED,
    STALE_HINT_DUPLICATE_ISSUE_LANE,
    STALE_HINT_GATEWAY_PANE_MISSING,
    STALE_HINT_WINDOW_SPLIT,
    STALE_HINT_WORKER_PANE_MISSING,
    STALE_HINT_WORKTREE_UNRESOLVED,
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


class HostWindowProjectionTests(unittest.TestCase):
    """#13086: lane host-window identity from the shared pane-location vocabulary."""

    def test_shared_window_yields_host_window_and_name(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1", lane_label="issue_100_a",
                 location="cockpit:3.1", window_name="mozyo_bridge"),
            _row(id="%2", agent_role="claude", lane_id="l1", lane_label="issue_100_a",
                 location="cockpit:3.2", window_name="mozyo_bridge"),
        ]
        v = project_sublanes(rows)[0]
        self.assertEqual(v.host_window, "cockpit:3")
        self.assertEqual(v.host_window_name, "mozyo_bridge")
        self.assertEqual(v.windows, ("cockpit:3",))
        self.assertNotIn(STALE_HINT_WINDOW_SPLIT, v.stale_hints)

    def test_pane_carries_window_identity_fields(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1", lane_label="issue_100_a",
                 location="cockpit:3.1", window_name="mozyo_bridge"),
        ]
        pane = project_sublanes(rows)[0].panes[0]
        self.assertEqual(pane.session, "cockpit")
        self.assertEqual(pane.window_index, "3")
        self.assertEqual(pane.window_name, "mozyo_bridge")
        self.assertEqual(pane.window, "cockpit:3")
        payload = pane.as_payload()
        self.assertEqual(payload["window"], "cockpit:3")
        self.assertEqual(payload["session"], "cockpit")

    def test_split_windows_yield_no_host_and_split_hint(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1", lane_label="issue_100_a",
                 location="cockpit:3.1", window_name="mozyo_bridge"),
            _row(id="%2", agent_role="claude", lane_id="l1", lane_label="issue_100_a",
                 location="cockpit:5.0", window_name="stray"),
        ]
        v = project_sublanes(rows)[0]
        self.assertIsNone(v.host_window)
        self.assertIsNone(v.host_window_name)
        self.assertEqual(v.windows, ("cockpit:3", "cockpit:5"))
        self.assertIn(STALE_HINT_WINDOW_SPLIT, v.stale_hints)

    def test_unknown_location_yields_no_windows_and_no_split_hint(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1", lane_label="issue_100_a"),
            _row(id="%2", agent_role="claude", lane_id="l1", lane_label="issue_100_a"),
        ]
        v = project_sublanes(rows)[0]
        self.assertIsNone(v.host_window)
        self.assertEqual(v.windows, ())
        self.assertNotIn(STALE_HINT_WINDOW_SPLIT, v.stale_hints)

    def test_window_identity_agrees_with_agents_discovery(self):
        # Acceptance (#13086): `agents targets` and `sublane list` must not
        # contradict each other on pane/window identity. Both fold the same
        # pane row through the same parse_location vocabulary.
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
            discover_agents,
        )

        row = _row(id="%9", agent_role="codex", lane_id="l1",
                   lane_label="issue_100_a", location="mozyo-cockpit:4.2",
                   window_name="mozyo_bridge")
        record = discover_agents([row])[0]
        pane = project_sublanes([row])[0].panes[0]
        self.assertEqual(
            (pane.session, pane.window_index, pane.window_name),
            (record.session, record.window_index, record.window_name),
        )


class StaleHintTests(unittest.TestCase):
    """#13086: machine-readable retire decision material (advisory only)."""

    def _intact_rows(self, lane="l1", label="issue_100_a", window="cockpit:3"):
        return [
            _row(id="%1", agent_role="codex", lane_id=lane, lane_label=label,
                 location=f"{window}.1", window_name="w"),
            _row(id="%2", agent_role="claude", lane_id=lane, lane_label=label,
                 location=f"{window}.2", window_name="w"),
        ]

    def test_intact_lane_has_no_hints(self):
        v = project_sublanes(self._intact_rows())[0]
        self.assertEqual(v.stale_hints, ())

    def test_missing_worker_and_gateway_pane_hints(self):
        gw_only = project_sublanes(
            [_row(id="%1", agent_role="codex", lane_id="l1", lane_label="issue_1_a")]
        )[0]
        self.assertIn(STALE_HINT_WORKER_PANE_MISSING, gw_only.stale_hints)
        self.assertNotIn(STALE_HINT_GATEWAY_PANE_MISSING, gw_only.stale_hints)
        wk_only = project_sublanes(
            [_row(id="%2", agent_role="claude", lane_id="l2", lane_label="issue_2_b")]
        )[0]
        self.assertIn(STALE_HINT_GATEWAY_PANE_MISSING, wk_only.stale_hints)

    def test_duplicate_issue_lanes_flag_each_other(self):
        rows = self._intact_rows(lane="l1", label="issue_100_a") + self._intact_rows(
            lane="l2", label="issue_100_b", window="cockpit:4"
        )
        views = {v.lane_id: v for v in project_sublanes(rows)}
        self.assertIn(
            f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:issue_100_b",
            views["l1"].stale_hints,
        )
        self.assertIn(
            f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:issue_100_a",
            views["l2"].stale_hints,
        )

    def test_distinct_issues_are_not_duplicates(self):
        rows = self._intact_rows(lane="l1", label="issue_100_a") + self._intact_rows(
            lane="l2", label="issue_200_b", window="cockpit:4"
        )
        for v in project_sublanes(rows):
            self.assertFalse(
                [h for h in v.stale_hints
                 if h.startswith(STALE_HINT_DUPLICATE_ISSUE_LANE)]
            )

    def test_worktree_unresolved_hint_from_caller_lookup(self):
        v = project_sublanes(
            self._intact_rows(), unresolved_worktrees={"l1"}
        )[0]
        self.assertIn(STALE_HINT_WORKTREE_UNRESOLVED, v.stale_hints)

    def test_branch_integrated_hint_names_the_integration_branch(self):
        v = project_sublanes(
            self._intact_rows(), integrated_branches={"l1": "main"}
        )[0]
        self.assertIn(f"{STALE_HINT_BRANCH_INTEGRATED}:main", v.stale_hints)

    def test_unknown_lookups_never_fabricate_hints(self):
        v = project_sublanes(self._intact_rows())[0]
        self.assertNotIn(STALE_HINT_WORKTREE_UNRESOLVED, v.stale_hints)
        self.assertFalse(
            [h for h in v.stale_hints
             if h.startswith(STALE_HINT_BRANCH_INTEGRATED)]
        )

    def test_payload_carries_window_and_hints(self):
        payload = project_sublanes(
            self._intact_rows(), integrated_branches={"l1": "main"}
        )[0].as_payload()
        self.assertEqual(payload["host_window"], "cockpit:3")
        self.assertEqual(payload["host_window_name"], "w")
        self.assertEqual(payload["windows"], ["cockpit:3"])
        self.assertEqual(
            payload["stale_hints"], [f"{STALE_HINT_BRANCH_INTEGRATED}:main"]
        )


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


class PlanWorkUnitGateTests(unittest.TestCase):
    """#13002: the work-unit granularity gate on the plan-only surface."""

    def _launch(self, action=LAUNCH_CREATE_WORKTREE):
        return WorktreeLaunchDecision(action=action, reason="r")

    def test_default_request_is_user_story_and_plans(self):
        request = _req()
        self.assertEqual(request.work_unit, "user_story")
        plan = plan_sublane_create(request, self._launch())
        self.assertEqual(plan.status, CREATE_PLANNED)

    def test_leaf_issue_exception_unit_plans(self):
        plan = plan_sublane_create(_req(work_unit="leaf_issue"), self._launch())
        self.assertEqual(plan.status, CREATE_PLANNED)

    def test_epic_without_decision_anchor_fails_closed(self):
        plan = plan_sublane_create(_req(work_unit="epic"), self._launch())
        self.assertEqual(plan.status, CREATE_BLOCKED)
        self.assertEqual(plan.steps, ())
        self.assertIn("work_unit_explicit_decision_required", plan.blocked_reasons)

    def test_feature_without_decision_anchor_fails_closed(self):
        plan = plan_sublane_create(_req(work_unit="feature"), self._launch())
        self.assertEqual(plan.status, CREATE_BLOCKED)
        self.assertIn("work_unit_explicit_decision_required", plan.blocked_reasons)

    def test_epic_with_durable_decision_anchor_plans(self):
        plan = plan_sublane_create(
            _req(work_unit="epic", work_unit_decision_anchor="70719"),
            self._launch(),
        )
        self.assertEqual(plan.status, CREATE_PLANNED)

    def test_missing_identity_still_wins_over_work_unit_gate(self):
        plan = plan_sublane_create(
            _req(worktree_path="", work_unit="epic"), self._launch()
        )
        self.assertEqual(plan.status, CREATE_BLOCKED)
        self.assertIn("missing_field:worktree_path", plan.blocked_reasons)


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
