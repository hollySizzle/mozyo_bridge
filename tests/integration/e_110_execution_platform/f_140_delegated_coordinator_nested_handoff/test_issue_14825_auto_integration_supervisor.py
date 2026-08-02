"""Scheduled owner / trigger / crash recovery for durable auto-integration (#14825)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (  # noqa: E501
    AutoIntegrationUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
    CI_STATE_PENDING,
    CI_STATE_SUCCESS,
    CiVerdict,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    ACTION_AWAITING_CI,
    ACTION_INTEGRATED,
    DurableIntegrationAction,
    SqliteLedgerStore,
    _open_ledger_writer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_ops import (  # noqa: E501
    REACHABILITY_NOT_REACHABLE,
    REACHABILITY_REACHABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ports import (  # noqa: E501
    CleanupAuthority,
    IntegrationAuthority,
    MergeResult,
    PushResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_supervisor import (  # noqa: E501
    SUPERVISION_AWAITING_CI,
    SUPERVISION_INTEGRATED,
    SUPERVISION_REFUSED,
    AutoIntegrationSupervisionOutcome,
    AutoIntegrationSupervisorLeg,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
    build_supervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    AutoIntegrationPolicy,
    STEP_PUSH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    IntegrationActionRecord,
    IntegrationCiEvidence,
    LaneWorktree,
)


ISSUE = "14825"
WORKSPACE = "ws-1"
LANE = "lane-14825"
BRANCH = "issue_14825"
SOURCE = "a" * 40
TARGET = "b" * 40


def _record() -> IntegrationActionRecord:
    return IntegrationActionRecord(
        issue=ISSUE,
        lane_generation=1,
        source_head=SOURCE,
        target_ref="main",
        expected_target_head=TARGET,
        review_generation="r3",
    )


def _action(root: Path) -> DurableIntegrationAction:
    record = _record()
    return DurableIntegrationAction(
        action_key=record.action_key,
        issue=ISSUE,
        workspace=WORKSPACE,
        lane=LANE,
        lane_generation=1,
        branch=BRANCH,
        worktree=str(root),
        repo_root=str(root),
        source_head=SOURCE,
        target_ref="main",
        expected_target_head=TARGET,
        review_generation="r3",
    )


@dataclass
class _Git:
    repo_root: Path
    target_head: str = TARGET
    pushes: int = 0

    def is_git_workspace(self):
        return True

    def describe_lane_worktree(self, *, path):
        return LaneWorktree(path=path, registered=True, clean=True, checked_out_branch=BRANCH)

    def resolve_head(self, ref):
        return self.target_head

    def remote_branch_tip(self, branch):
        return self.target_head

    def is_ancestor(self, *, ancestor, descendant):
        return ancestor == descendant or (ancestor == TARGET and descendant == SOURCE)

    def worktree_dirty(self, *, worktree_path=""):
        return False

    def commit_on_remote(self, commit, *, branch):
        return branch == BRANCH or commit == self.target_head

    def reachability(self, commit, *, branch):
        return (
            REACHABILITY_REACHABLE
            if self.commit_on_remote(commit, branch=branch)
            else REACHABILITY_NOT_REACHABLE
        )

    def apply_merge(self, *, source_head, target_ref, expected_target_head):
        return MergeResult(status="merged", integration_head="c" * 40, git_version="git test")

    def push_non_force(self, *, source_head, target_ref):
        self.pushes += 1
        self.target_head = source_head
        return PushResult(status="accepted")


@dataclass
class _Authority:
    integration_ci: IntegrationCiEvidence | None = None

    def read_integration_authority(self, *, record):
        return IntegrationAuthority(
            review_generation_admissible=True,
            reviewed_head=SOURCE,
            target_identity_known=True,
            callbacks_drained=True,
            owner_gates_resolved=True,
            source_ci=IntegrationCiEvidence(
                integration_head=SOURCE,
                workflow="required-ci",
                run="source-run",
                conclusion="success",
            ),
        )

    def read_integration_ci(self, *, record, integration_head):
        return self.integration_ci

    def required_ci_workflow(self, *, record):
        return "required-ci"

    def read_cleanup_authority(self, *, record):
        return CleanupAuthority()


@dataclass
class _Ci:
    state: str = CI_STATE_PENDING

    def verdict_for(self, commit, *, workflow="", attested_run="", branch=""):
        return CiVerdict(
            self.state,
            "fixed",
            run=attested_run or "target-run",
            workflow=workflow,
            commit=commit,
            branch=branch,
            conclusion="success" if self.state == CI_STATE_SUCCESS else self.state,
        )


class AutoIntegrationSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.root = self.home / "repo"
        self.root.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.git = _Git(repo_root=self.root)
        self.authority = _Authority()
        self.ci = _Ci()
        self.writer = _open_ledger_writer(home=self.home)
        self.writer.register_action(_action(self.root))

    def _compose(self, action, root):
        return AutoIntegrationUseCase(
            operations=self.git,
            integration_policy=AutoIntegrationPolicy(
                mode="auto", integration_branch="main", ff_only=True
            ),
            authority=self.authority,
            ledger=SqliteLedgerStore(home=self.home),
            _ledger_writer=_open_ledger_writer(home=self.home),
            lane_worktree=str(self.root),
            lane_branch=BRANCH,
            lane_issue=ISSUE,
            lane_generation=1,
        )

    def _leg(self, *, root=None):
        resolved = self.root if root is None else root
        return AutoIntegrationSupervisorLeg(
            workspace_root_fn=lambda _workspace: resolved,
            callback_outbox=object(),
            lifecycle_store=LaneLifecycleStore(home=self.home),
            home=self.home,
            compose_fn=self._compose,
            ci_reader_fn=lambda _root: self.ci,
        )

    def test_pending_then_terminal_wakes_push_once_and_converge(self) -> None:
        first = self._leg()(WORKSPACE, ISSUE)
        self.assertTrue(first.mutated)
        self.assertEqual(first.actions[0].status, SUPERVISION_AWAITING_CI)
        self.assertEqual(self.git.pushes, 1)
        durable = SqliteLedgerStore(home=self.home).action(_record().action_key)
        self.assertEqual(durable.state, ACTION_AWAITING_CI)
        self.assertEqual(durable.ci_workflow, "required-ci")

        self.ci.state = CI_STATE_SUCCESS
        self.authority.integration_ci = IntegrationCiEvidence(
            integration_head=SOURCE,
            workflow="required-ci",
            run="integration-run",
            conclusion="success",
        )
        second = self._leg()(WORKSPACE, ISSUE)
        self.assertFalse(second.mutated)
        self.assertEqual(second.actions[0].status, SUPERVISION_INTEGRATED)
        self.assertEqual(self.git.pushes, 1)
        self.assertEqual(
            SqliteLedgerStore(home=self.home).action(_record().action_key).state,
            ACTION_INTEGRATED,
        )

        third = self._leg()(WORKSPACE, ISSUE)
        self.assertEqual(third.actions, ())
        self.assertEqual(self.git.pushes, 1)
        self.assertEqual(self.writer.action_event_count(action_key=_record().action_key), 3)

    def test_a_crash_after_the_push_is_reconciled_without_a_second_push(self) -> None:
        self.git.target_head = SOURCE
        self.writer.begin_step(action_key=_record().action_key, step=STEP_PUSH)
        outcome = self._leg()(WORKSPACE, ISSUE)
        self.assertEqual(outcome.actions[0].status, SUPERVISION_AWAITING_CI)
        self.assertFalse(outcome.mutated)
        self.assertEqual(self.git.pushes, 0)
        self.assertEqual(
            SqliteLedgerStore(home=self.home).unresolved_intents(
                action_key=_record().action_key
            ),
            (),
        )

    def test_a_registry_root_mismatch_refuses_without_composing_or_pushing(self) -> None:
        other = self.home / "other"
        other.mkdir()
        outcome = self._leg(root=other)(WORKSPACE, ISSUE)
        self.assertEqual(outcome.actions[0].status, SUPERVISION_REFUSED)
        self.assertEqual(self.git.pushes, 0)

    def test_the_production_workspace_supervisor_wires_the_owner_leg(self) -> None:
        supervisor = build_supervisor(
            holder="test-owner",
            home=self.home,
            store_path=self.home / "workflow-runtime.sqlite3",
        )
        self.assertTrue(callable(supervisor._auto_integration_leg_fn))

    def test_a_scheduled_bounded_sweep_invokes_the_owner_under_the_issue_lease(self) -> None:
        calls = []

        def auto_leg(workspace, issue, source):
            calls.append((workspace, issue, source))
            return AutoIntegrationSupervisionOutcome(workspace=workspace, issue=issue)

        supervisor = WorkspaceCallbackSupervisor(
            holder="scheduled-owner",
            lease_store=SupervisorLeaseStore(path=self.home / "lease.sqlite3"),
            store=WorkflowRuntimeStore(path=self.home / "workflow.sqlite3"),
            outbox=CallbackOutbox(path=self.home / "workflow.sqlite3"),
            workspaces_fn=lambda: [
                SupervisedWorkspace(workspace_id=WORKSPACE, canonical_path=str(self.root))
            ],
            roster_fn=lambda _workspace: ((ISSUE,), ""),
            redmine_source_fn=lambda _workspace: None,
            sender_fn=lambda _workspace: (lambda _row: "delivered"),
            auto_integration_leg_fn=auto_leg,
        )
        report = supervisor.run_once()
        self.assertEqual(report.workspaces[0].supervised_issues, (ISSUE,))
        self.assertEqual(calls, [(WORKSPACE, ISSUE, None)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
