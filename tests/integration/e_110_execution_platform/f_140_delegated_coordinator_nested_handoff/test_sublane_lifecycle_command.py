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
    SublaneListOutcome,
    SublaneListUseCase,
    SublaneRetireUseCase,
    format_create_text,
    format_list_text,
    format_retire_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    INTEGRATION_BLOCKED,
    RETIRE_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    CREATE_BLOCKED,
    CREATE_PLANNED,
    SUBLANE_STATE_ACTIVE,
    SublaneCreateRequest,
    SublaneLaneView,
)

# Redmine #13368: synthetic host-local worktree path (never a real home path).
_WT = "/workspace/parent/mozyo_bridge_issue_13368_record_path_redaction"
_WT_LABEL = "mozyo_bridge_issue_13368_record_path_redaction"


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
        integrated=None,
        workspace_root=None,
    ):
        self._rows = rows or []
        self._git = git
        self._worktree_exists = worktree_exists
        self._dirty = dirty
        self._branches = branches or {}
        # (branch, integration_branch) -> Optional[bool] ancestry answers (#13086).
        self._integrated = integrated or {}
        # #13432 optional capability: the workspace root a non-git omitted --worktree
        # defaults to. When None the fake omits the capability entirely (older-adapter
        # parity — the getattr discovery falls back to leaving the worktree blank).
        self._workspace_root = workspace_root
        self.branch_calls = []
        self.integrated_calls = []
        if workspace_root is not None:
            self.canonical_workspace_root = lambda: workspace_root

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

    def branch_integrated(self, branch, integration_branch):
        self.integrated_calls.append((branch, integration_branch))
        return self._integrated.get((branch, integration_branch))


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

    def test_unresolved_worktree_yields_stale_hint(self):
        # #13086: a recorded worktree the port cannot resolve a branch for is
        # stale retire material (removed / moved / never created).
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1",
                 lane_label="issue_1_a", repo_root_stamp="/wt/gone"),
        ]
        outcome = SublaneListUseCase(FakeOps(rows=rows)).run()
        self.assertIn("worktree_unresolved", outcome.lanes[0].stale_hints)
        self.assertIsNone(outcome.lanes[0].branch)

    def test_integration_branch_probe_is_opt_in(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1",
                 lane_label="issue_1_a", repo_root_stamp="/wt/a"),
        ]
        ops = FakeOps(rows=rows, branches={"/wt/a": "issue_1_a"},
                      integrated={("issue_1_a", "main"): True})
        # Omitted -> never probed, never hinted.
        outcome = SublaneListUseCase(ops).run()
        self.assertEqual(ops.integrated_calls, [])
        self.assertNotIn("branch_integrated:main", outcome.lanes[0].stale_hints)
        # Named -> probed through the port and hinted with the branch name.
        outcome = SublaneListUseCase(ops).run(integration_branch="main")
        self.assertIn(("issue_1_a", "main"), ops.integrated_calls)
        self.assertIn("branch_integrated:main", outcome.lanes[0].stale_hints)

    def test_unknown_ancestry_answer_never_fabricates_hint(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1",
                 lane_label="issue_1_a", repo_root_stamp="/wt/a"),
        ]
        ops = FakeOps(rows=rows, branches={"/wt/a": "issue_1_a"},
                      integrated={})  # probe answers None (unknown)
        outcome = SublaneListUseCase(ops).run(integration_branch="main")
        self.assertFalse(
            [h for h in outcome.lanes[0].stale_hints
             if h.startswith("branch_integrated")]
        )

    def test_host_window_projected_from_pane_locations(self):
        rows = [
            _row(id="%1", agent_role="codex", lane_id="l1", lane_label="issue_1_a",
                 location="cockpit:3.1", window_name="mozyo_bridge"),
            _row(id="%2", agent_role="claude", lane_id="l1", lane_label="issue_1_a",
                 location="cockpit:3.2", window_name="mozyo_bridge"),
        ]
        outcome = SublaneListUseCase(FakeOps(rows=rows)).run()
        lane = outcome.lanes[0]
        self.assertEqual(lane.host_window, "cockpit:3")
        self.assertEqual(lane.host_window_name, "mozyo_bridge")
        payload = outcome.as_payload()["sublanes"][0]
        self.assertEqual(payload["host_window"], "cockpit:3")
        self.assertEqual(payload["stale_hints"], [])


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

    def test_git_missing_field_fails_closed(self):
        # #13432: a Git workspace keeps the full identity requirement, so a missing
        # worktree still fails closed (the probe order changed, the contract did not).
        ops = FakeOps(git=True)
        outcome = SublaneCreateUseCase(ops).run(_req(worktree_path=""))
        self.assertEqual(outcome.plan.status, CREATE_BLOCKED)
        self.assertIn("missing_field:worktree_path", outcome.plan.blocked_reasons)

    def test_non_git_omitted_branch_and_worktree_plans(self):
        # #13432: --branch/--worktree are optional in a non-git workspace; the plan still
        # resolves (skip_no_git) with the lane / dispatch steps present.
        ops = FakeOps(git=False, workspace_root="/ws")
        outcome = SublaneCreateUseCase(ops).run(_req(branch="", worktree_path=""))
        self.assertEqual(outcome.plan.status, CREATE_PLANNED)
        self.assertEqual(outcome.plan.launch_action, "skip_no_git")
        self.assertEqual(len(outcome.plan.steps), 4)

    def test_non_git_missing_lane_identity_still_fails_closed(self):
        # #13432: only the Git worktree identity relaxes; issue / lane_label are required
        # in every workspace.
        ops = FakeOps(git=False, workspace_root="/ws")
        outcome = SublaneCreateUseCase(ops).run(
            _req(lane_label="", branch="", worktree_path="")
        )
        self.assertEqual(outcome.plan.status, CREATE_BLOCKED)
        self.assertIn("missing_field:lane_label", outcome.plan.blocked_reasons)


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


class RecordPathRedactionTests(unittest.TestCase):
    """Redmine #13368: command-layer pasteable text carries no host-local abs path.

    Every ``format_*_text`` a coordinator pastes into a Redmine journal redacts the
    absolute worktree path to its portable sibling basename; the machine ``--json``
    payload (``as_payload``) keeps the absolute path for local use.
    """

    def _all_true(self):
        return RetireAssertions(
            issue_closed=True, owner_approval_present=True, callbacks_drained=True,
            verification_passed=True, durable_record_recorded=True,
            target_identity_known=True,
        )

    def test_list_text_shows_basename_json_keeps_abs(self):
        lane = SublaneLaneView(
            workspace_id="ws", lane_id="lane-1",
            lane_label="issue_13368_record_path_redaction", issue="13368",
            branch="issue_13368_record_path_redaction", repo_root=_WT,
            gateway_pane="%1", worker_pane="%2", state=SUBLANE_STATE_ACTIVE,
        )
        outcome = SublaneListOutcome(lanes=(lane,))
        text = format_list_text(outcome)
        self.assertNotIn(_WT, text)
        self.assertIn(f"worktree={_WT_LABEL}", text)
        # Machine payload retains the absolute repo root.
        self.assertEqual(outcome.as_payload()["sublanes"][0]["repo_root"], _WT)

    def test_create_plan_text_redacts_git_worktree_add_command(self):
        outcome = SublaneCreateUseCase(FakeOps(git=True)).run(
            SublaneCreateRequest(
                issue="13368",
                lane_label="issue_13368_record_path_redaction",
                branch="issue_13368_record_path_redaction",
                worktree_path=_WT,
            )
        )
        text = format_create_text(outcome, worktree_path=_WT)
        self.assertNotIn(_WT, text)
        self.assertIn(_WT_LABEL, text)
        # The plan JSON keeps the exact replayable command with the absolute path.
        commands = [s["command"] for s in outcome.as_payload()["steps"] if s["command"]]
        self.assertTrue(any(_WT in c for c in commands))

    def test_retire_runbook_text_redacts_but_json_keeps_abs(self):
        outcome = SublaneRetireUseCase(FakeOps(git=True, dirty=False)).run(
            issue="13368", lane_label="issue_13368_record_path_redaction",
            worktree_path=_WT, branch="issue_13368_record_path_redaction",
            integration_branch=None, assertions=self._all_true(),
        )
        text = format_retire_text(outcome, worktree_path=_WT)
        self.assertNotIn(_WT, text)
        self.assertIn(f"git worktree remove {_WT_LABEL}", text)
        # The runbook JSON keeps the exact replayable command with the absolute path.
        commands = [
            s["command"]
            for s in outcome.as_payload()["runbook"]
            if s["command"]
        ]
        self.assertTrue(any(_WT in c for c in commands))


if __name__ == "__main__":
    unittest.main()
