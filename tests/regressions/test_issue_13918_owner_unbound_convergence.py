"""Redmine #13918 — owner-approved convergence for rowless historical pairs."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.scratch_retirement_fence import ScratchRetirementFence
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
    REASON_COMPOSER_DISCARD_APPROVAL_INVALID,
    REASON_COMPOSER_DISCARD_ISSUE_MISMATCH,
    REASON_HISTORICAL_BRANCH_MISMATCH,
    REASON_HISTORICAL_WORKTREE_DIRTY,
    REASON_HISTORICAL_WORKTREE_UNREADABLE,
    REASON_WORK_OBLIGATION_PRESENT,
    run_session_retire,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_cli import (  # noqa: E501
    register_herdr_session_retire_parser,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
    LiveSessionRetireOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.scratch_pair_obligations import (  # noqa: E501
    OWED,
    PairObligation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.scratch_pair_retire import (  # noqa: E501
    REASON_AGENT_NOT_IDLE,
    REASON_DUPLICATE_INVENTORY,
    REASON_FOREIGN_INVENTORY_PRESENT,
    REASON_LANE_RECORD_PRESENT,
    REASON_PENDING_COMPOSER,
    STATE_BLOCKED,
    STATE_GREEN,
)
from tests.regressions.test_issue_13892_scratch_pair_public_retire import FakeOps
from tests.support.herdr_workspace_fixtures import anchored_repo_root


LANE = "issue_13918_owner_unbound_convergence"
APPROVAL = "13918:80789"
GATEWAY = "codex"
WORKER = "claude"


class HistoricalOps(FakeOps):
    """The #13892 seam plus action-time Git facts introduced by #13918."""

    def __init__(self, rows, *, worktree=(True, True, LANE), **kwargs):
        super().__init__(rows, **kwargs)
        self._worktree = worktree
        self.worktree_calls = 0

    def worktree_facts(self):
        self.worktree_calls += 1
        return self._worktree


class OwnerApprovedConvergenceTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = anchored_repo_root(self)
        self.ws = herdr_workspace_segment(self.repo_root)

    def _name(self, role, lane=LANE):
        return encode_assigned_name(self.ws, role, lane)

    def _rows(self, lane=LANE):
        return [
            {"name": self._name(GATEWAY, lane), "pane": "%1", "agent": GATEWAY},
            {"name": self._name(WORKER, lane), "pane": "%2", "agent": WORKER},
        ]

    def _args(self, *, approval=None, execute=True, lane=LANE):
        return argparse.Namespace(
            lane=lane,
            execute=execute,
            json=False,
            repo=None,
            pending_composer_discard_approval=approval,
        )

    def _run(self, ops, *, approval=None, execute=True, lane=LANE):
        if ops.fence is None:
            home = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, home, True)
            ops.fence = ScratchRetirementFence(home=home)
        return run_session_retire(
            self._args(approval=approval, execute=execute, lane=lane),
            self.repo_root,
            ops=ops,
        )

    def test_default_pending_composer_refusal_is_unchanged(self):
        ops = HistoricalOps(self._rows(), composer=(True, True))
        result = self._run(ops)
        self.assertEqual(result.reason, REASON_PENDING_COMPOSER)
        self.assertEqual(ops.close_calls, [])
        self.assertEqual(ops.worktree_calls, 0, "default mode needs no Git authority")

    def test_malformed_approval_is_zero_close(self):
        for token in ("13918", "13918:0", "0:80789", "13918:80789:1", " 13918:80789 "):
            with self.subTest(token=token):
                ops = HistoricalOps(self._rows(), composer=(True, True))
                result = self._run(ops, approval=token)
                self.assertEqual(result.reason, REASON_COMPOSER_DISCARD_APPROVAL_INVALID)
                self.assertEqual(ops.close_calls, [])
                self.assertEqual(ops.worktree_calls, 0)

    def test_historical_lane_requires_matching_issue(self):
        ops = HistoricalOps(self._rows(), composer=(True, True))
        result = self._run(ops, approval="13883:80484")
        self.assertEqual(result.reason, REASON_COMPOSER_DISCARD_ISSUE_MISMATCH)
        self.assertEqual(ops.close_calls, [])
        self.assertEqual(ops.worktree_calls, 0)

    def test_historical_lane_requires_readable_clean_matching_worktree(self):
        cases = (
            ((False, False, ""), REASON_HISTORICAL_WORKTREE_UNREADABLE),
            ((True, False, LANE), REASON_HISTORICAL_WORKTREE_DIRTY),
            ((True, True, "other"), REASON_HISTORICAL_BRANCH_MISMATCH),
            ((True, True, ""), REASON_HISTORICAL_BRANCH_MISMATCH),
        )
        for facts, reason in cases:
            with self.subTest(facts=facts):
                ops = HistoricalOps(
                    self._rows(), composer=(True, True), worktree=facts
                )
                result = self._run(ops, approval=APPROVAL)
                self.assertEqual(result.reason, reason)
                self.assertEqual(ops.close_calls, [])
                self.assertEqual(ops.worktree_calls, 1)

    def test_preflight_reports_explicit_discard_and_writes_nothing(self):
        ops = HistoricalOps(self._rows(), composer=(True, True))
        result = self._run(ops, approval=APPROVAL, execute=False)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertFalse(result.executed)
        self.assertIn(APPROVAL, result.detail)
        self.assertEqual(ops.close_calls, [])
        self.assertEqual(ops.recorded, [])

    def test_execute_closes_pair_and_audits_approval_pointer(self):
        ops = HistoricalOps(self._rows(), composer=(True, True))
        result = self._run(ops, approval=APPROVAL)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertEqual(len(ops.close_calls), 1)
        self.assertEqual(
            ops.recorded[0]["pending_composer_discard_approval"], APPROVAL
        )
        self.assertIn(APPROVAL, result.detail)

    def test_approval_only_overrides_pending_composer(self):
        foreign = encode_assigned_name(self.ws, "gemini", LANE)
        owed = PairObligation(
            source="dispatch_outbox",
            verdict=OWED,
            target=self._name(WORKER),
            state="reserved",
            issue="13918",
            journal="80719",
        )
        cases = (
            (
                HistoricalOps(self._rows(), composer=(True, True), runtime="busy"),
                REASON_AGENT_NOT_IDLE,
            ),
            (
                HistoricalOps(
                    self._rows()
                    + [{"name": self._name(WORKER), "pane": "%3", "agent": WORKER}],
                    composer=(True, True),
                ),
                REASON_DUPLICATE_INVENTORY,
            ),
            (
                HistoricalOps(
                    self._rows()
                    + [{"name": foreign, "pane": "%9", "agent": "gemini"}],
                    composer=(True, True),
                ),
                REASON_FOREIGN_INVENTORY_PRESENT,
            ),
            (
                HistoricalOps(
                    self._rows(), composer=(True, True), record_absent=False
                ),
                REASON_LANE_RECORD_PRESENT,
            ),
            (
                HistoricalOps(
                    self._rows(), composer=(True, True), obligations=(owed,)
                ),
                REASON_WORK_OBLIGATION_PRESENT,
            ),
        )
        for ops, reason in cases:
            with self.subTest(reason=reason):
                result = self._run(ops, approval=APPROVAL)
                self.assertEqual(result.state, STATE_BLOCKED)
                self.assertEqual(result.reason, reason)
                self.assertEqual(ops.close_calls, [])

    def test_partial_close_retry_keeps_approval_and_finishes(self):
        ops = HistoricalOps(
            self._rows(), composer=(True, True), fail_roles=(WORKER,)
        )
        first = self._run(ops, approval=APPROVAL)
        self.assertEqual(first.state, STATE_BLOCKED)
        self.assertEqual(first.closed, ((GATEWAY, "%1"),))
        ops._fail_roles = ()
        second = self._run(ops, approval=APPROVAL)
        self.assertEqual(second.state, STATE_GREEN, second.detail)
        self.assertEqual(ops.close_calls[-1], ((WORKER, "%2"),))
        self.assertEqual(
            ops.recorded[0]["pending_composer_discard_approval"], APPROVAL
        )

    def test_non_issue_scratch_pair_skips_git_gate(self):
        lane = "dogfood13918"

        class NoGitAuthorityOps(HistoricalOps):
            def worktree_facts(self):
                raise AssertionError("non-issue scratch pairs do not have Git authority")

        ops = NoGitAuthorityOps(self._rows(lane), composer=(True, True))
        result = self._run(ops, approval=APPROVAL, lane=lane)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertEqual(len(ops.close_calls), 1)

    def test_public_parser_accepts_only_the_pointer_as_data(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register_herdr_session_retire_parser(sub)
        args = parser.parse_args(
            [
                "session-retire",
                "--lane",
                LANE,
                "--pending-composer-discard-approval",
                APPROVAL,
            ]
        )
        self.assertEqual(args.pending_composer_discard_approval, APPROVAL)
        self.assertFalse(hasattr(args, "pane"))
        self.assertFalse(hasattr(args, "locator"))
        self.assertFalse(hasattr(args, "force"))


class LiveWorktreeFactsTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        init = subprocess.run(
            ["git", "init", "-q", "-b", LANE],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if init.returncode != 0:
            self.skipTest("git init -b is unavailable")

    def test_reads_branch_and_dirty_state_from_repo_root(self):
        ops = LiveSessionRetireOps(repo_root=self.repo)
        self.assertEqual(ops.worktree_facts(), (True, True, LANE))
        (self.repo / "unsaved.txt").write_text("pending\n", encoding="utf-8")
        self.assertEqual(ops.worktree_facts(), (True, False, LANE))

    def test_non_repository_is_unreadable(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        self.assertEqual(
            LiveSessionRetireOps(repo_root=other).worktree_facts(),
            (False, False, ""),
        )


if __name__ == "__main__":
    unittest.main()
