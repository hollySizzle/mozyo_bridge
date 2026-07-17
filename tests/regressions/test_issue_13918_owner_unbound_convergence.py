"""Redmine #13918 — owner-approved convergence for rowless historical pairs."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    ScratchRetirementFenceError,
    slot_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
    REASON_COMPOSER_DISCARD_APPROVAL_INVALID,
    REASON_COMPOSER_DISCARD_APPROVAL_MISMATCH,
    REASON_COMPOSER_DISCARD_ISSUE_MISMATCH,
    REASON_COMPLETION_UNPROVEN,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.composer_discard_approval import (  # noqa: E501
    APPROVAL_DECISION,
    APPROVAL_EFFECT,
    APPROVAL_GATE,
    APPROVAL_SOURCE,
    APPROVAL_VERSION,
    ComposerDiscardApprovalError,
    ComposerDiscardApprovalEvidence,
    pin_digest,
    verify_composer_discard_approval,
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
APPROVAL = "13918:90001"
WRONG_GATE_APPROVAL = "13918:80789"
GATEWAY = "codex"
WORKER = "claude"


def _approval_note(
    *, issue, workspace_id, lane_id, pair_slot_digest, pinned, gate=APPROVAL_GATE
):
    fields = {
        "gate": gate,
        "version": APPROVAL_VERSION,
        "approval_source": APPROVAL_SOURCE,
        "decision": APPROVAL_DECISION,
        "effect": APPROVAL_EFFECT,
        "issue": issue,
        "workspace": workspace_id,
        "lane": lane_id,
        "slot_digest": pair_slot_digest,
        "pin_digest": pin_digest(pinned),
    }
    body = ":".join(f"{key}={value}" for key, value in fields.items())
    return f"[mozyo:workflow-event:{body}]"


class HistoricalOps(FakeOps):
    """The #13892 seam plus action-time Git facts introduced by #13918."""

    def __init__(
        self,
        rows,
        *,
        worktree=(True, True, LANE),
        approval_mode="valid",
        **kwargs,
    ):
        super().__init__(rows, **kwargs)
        self._worktree = worktree
        self._approval_mode = approval_mode
        self.worktree_calls = 0
        self.approval_calls = []

    def worktree_facts(self):
        self.worktree_calls += 1
        return self._worktree

    def composer_discard_approval(
        self,
        *,
        issue,
        journal,
        workspace_id,
        lane_id,
        slot_digest,
        pinned,
    ):
        self.approval_calls.append((issue, journal, workspace_id, lane_id, pinned))
        if self._approval_mode == "unreadable":
            raise ComposerDiscardApprovalError("live Redmine read failed")
        if self._approval_mode == "missing":
            entries = []
        else:
            note_workspace = (
                "foreign-workspace"
                if self._approval_mode == "foreign_target"
                else workspace_id
            )
            note_pins = (
                ((GATEWAY, "%old1"), (WORKER, "%old2"))
                if self._approval_mode == "stale_target"
                else pinned
            )
            gate = (
                "codex_direct_edit"
                if self._approval_mode == "wrong_gate"
                else APPROVAL_GATE
            )
            entries = [
                RedmineJournalEntry(
                    issue_id=issue,
                    journal_id=journal,
                    notes=_approval_note(
                        issue=issue,
                        workspace_id=note_workspace,
                        lane_id=lane_id,
                        pair_slot_digest=slot_digest,
                        pinned=note_pins,
                        gate=gate,
                    ),
                )
            ]
        return verify_composer_discard_approval(
            entries,
            issue=issue,
            journal=journal,
            workspace_id=workspace_id,
            lane_id=lane_id,
            slot_digest=slot_digest,
            pinned=pinned,
        )


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

    def test_live_approval_must_exist_be_owner_gate_and_target_this_exact_pair(self):
        cases = (
            ("missing", APPROVAL),
            ("unreadable", APPROVAL),
            ("wrong_gate", WRONG_GATE_APPROVAL),
            ("foreign_target", APPROVAL),
            ("stale_target", APPROVAL),
        )
        for mode, token in cases:
            with self.subTest(mode=mode):
                ops = HistoricalOps(
                    self._rows(), composer=(True, True), approval_mode=mode
                )
                result = self._run(ops, approval=token)
                self.assertEqual(
                    result.reason, REASON_COMPOSER_DISCARD_APPROVAL_INVALID
                )
                self.assertEqual(ops.close_calls, [])
                self.assertFalse(
                    ops.fence.path.exists(),
                    "an invalid live approval must fail before reserve/bootstrap",
                )

    def test_old_direct_edit_journal_80789_is_not_owner_approval(self):
        ops = HistoricalOps(
            self._rows(), composer=(True, True), approval_mode="wrong_gate"
        )
        result = self._run(ops, approval=WRONG_GATE_APPROVAL)
        self.assertEqual(result.reason, REASON_COMPOSER_DISCARD_APPROVAL_INVALID)
        self.assertIn("structured composer-discard owner approval", result.detail)
        self.assertEqual(ops.close_calls, [])

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

    def test_completion_failure_retry_requires_same_fresh_approval_evidence(self):
        ops = HistoricalOps(self._rows(), composer=(True, True))
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        fence = ScratchRetirementFence(home=home)
        ops.fence = fence
        real_transaction = fence.transaction

        class BreakComplete:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._txn = self._inner.__enter__()
                return self

            def __exit__(self, *args):
                return self._inner.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._txn, name)

            def mark_completed(self, **kwargs):
                raise ScratchRetirementFenceError("simulated completion failure")

        fence.transaction = lambda unit, **kw: BreakComplete(  # type: ignore[method-assign]
            real_transaction(unit, **kw)
        )
        first = self._run(ops, approval=APPROVAL)
        self.assertEqual(first.reason, REASON_COMPLETION_UNPROVEN)
        self.assertEqual(len(first.closed), 2)

        fence.transaction = real_transaction  # type: ignore[method-assign]
        without = HistoricalOps([], composer=(True, False))
        without.fence = fence
        no_approval = self._run(without)
        self.assertEqual(
            no_approval.reason, REASON_COMPOSER_DISCARD_APPROVAL_MISMATCH
        )

        different = HistoricalOps([], composer=(True, False))
        different.fence = fence
        other_approval = self._run(different, approval="13918:90002")
        self.assertEqual(
            other_approval.reason, REASON_COMPOSER_DISCARD_APPROVAL_MISMATCH
        )

        exact = HistoricalOps([], composer=(True, False))
        exact.fence = fence
        repaired = self._run(exact, approval=APPROVAL)
        self.assertEqual(repaired.state, STATE_GREEN, repaired.detail)
        self.assertEqual(exact.close_calls, [])
        self.assertEqual(
            exact.recorded[0]["pending_composer_discard_approval"], APPROVAL
        )

    def test_completed_fence_keeps_exact_approval_when_audit_append_fails(self):
        ops = HistoricalOps(self._rows(), composer=(True, True))
        ops.record_retirement = lambda **kwargs: "not_recorded:append_failed"
        result = self._run(ops, approval=APPROVAL)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertEqual(result.audit_record, "not_recorded:append_failed")

        unit = RetirementUnit(
            self.ws,
            LANE,
            slot_digest([self._name(GATEWAY), self._name(WORKER)]),
        )
        completed = ops.fence.peek(unit)
        self.assertIsNotNone(completed)
        self.assertTrue(completed.completed)
        evidence = ComposerDiscardApprovalEvidence.from_json(
            completed.approval_evidence
        )
        self.assertEqual(evidence.token, APPROVAL)
        self.assertEqual(evidence.workspace_id, self.ws)
        self.assertEqual(evidence.lane_id, LANE)

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

    def test_live_ops_fetches_the_exact_journal_fresh_on_every_verification(self):
        workspace = "e13918"
        lane = LANE
        names = [
            encode_assigned_name(workspace, GATEWAY, lane),
            encode_assigned_name(workspace, WORKER, lane),
        ]
        pair_slot_digest = slot_digest(names)
        pins = ((GATEWAY, "%1"), (WORKER, "%2"))
        note = _approval_note(
            issue="13918",
            workspace_id=workspace,
            lane_id=lane,
            pair_slot_digest=pair_slot_digest,
            pinned=pins,
        )

        class FreshSource:
            def __init__(self):
                self.reads = 0

            def read_entries(self, issue):
                self.reads += 1
                return [RedmineJournalEntry(issue, "90001", note)]

        source = FreshSource()

        class TestOps(LiveSessionRetireOps):
            def _redmine_source(self):
                return source

        ops = TestOps(repo_root=self.repo)
        for _ in range(2):
            evidence = ops.composer_discard_approval(
                issue="13918",
                journal="90001",
                workspace_id=workspace,
                lane_id=lane,
                slot_digest=pair_slot_digest,
                pinned=pins,
            )
            self.assertEqual(evidence.token, APPROVAL)
        self.assertEqual(source.reads, 2)


if __name__ == "__main__":
    unittest.main()
