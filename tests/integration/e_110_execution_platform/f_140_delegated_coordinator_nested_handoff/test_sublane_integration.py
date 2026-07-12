"""Sublane Git worktree / retire-merge use-case composition tests (Redmine #12604).

Drives :class:`SublaneIntegrationUseCase` against a fake :class:`SublaneGitOperations`
port (the established #12557 executor-style fake), covering the acceptance scenarios at
the composition seam:

- launch in a Git workspace creates the worktree via the port; a non-Git workspace
  skips creation;
- retire attempts the merge only after every non-merge gate passes (a dirty worktree or
  a missing invariant blocks *before* any merge runs — the merge side effect is never
  reached);
- a merge conflict from the port re-decides to ``integration_blocked``;
- ``merge_on_retire: false`` skips the merge call entirely (the opt-out);
- :func:`policy_from_config` maps the governance config knob onto the domain policy;
- the live adapter's gated merge fails closed with a Design-Consultation pointer.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
    RetireInvariants,
    SublaneGitOperations,
    SublaneIntegrationUseCase,
    policy_from_config,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    BLOCKED_DIRTY_WORKTREE,
    INTEGRATION_STALE_REVIEW_GENERATION,
    BLOCKED_MERGE_CONFLICT,
    INTEGRATION_BLOCKED,
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_SKIP_NO_GIT,
    RETIRE_OK,
    SublaneIntegrationPolicy,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    SublaneIntegrationConfig,
)


class FakeGitOperations:
    """A scriptable :class:`SublaneGitOperations` recording the calls made to it."""

    def __init__(
        self,
        *,
        git: bool = True,
        worktree_exists: bool = False,
        dirty: bool = False,
        branch_resolved: bool = True,
        conflict: bool = False,
    ) -> None:
        self._git = git
        self._worktree_exists = worktree_exists
        self._dirty = dirty
        self._branch_resolved = branch_resolved
        self._conflict = conflict
        self.created: tuple[str, str] | None = None
        self.merge_called = False

    def is_git_workspace(self) -> bool:
        return self._git

    def worktree_exists(self, branch: str) -> bool:
        return self._worktree_exists

    def create_worktree(self, *, branch: str, worktree_path: str) -> None:
        self.created = (branch, worktree_path)

    def worktree_dirty(self) -> bool:
        return self._dirty

    def integration_branch_resolved(self, branch):  # type: ignore[no-untyped-def]
        return self._branch_resolved

    def merge_to_integration_branch(self, branch) -> bool:  # type: ignore[no-untyped-def]
        self.merge_called = True
        return self._conflict


def _ok_invariants() -> RetireInvariants:
    return RetireInvariants(
        target_identity_known=True,
        verification_passed=True,
        issue_closed=True,
        owner_approval_present=True,
        callbacks_drained=True,
        durable_record_recorded=True,
        latest_generation_admissible=True,  # R4-F3: fail-closed default -> must assert explicitly
    )


class FakePortIsProtocolTest(unittest.TestCase):
    def test_fake_satisfies_port_protocol(self) -> None:
        self.assertIsInstance(FakeGitOperations(), SublaneGitOperations)


class PolicyFromConfigTest(unittest.TestCase):
    def test_maps_each_field(self) -> None:
        config = SublaneIntegrationConfig(
            manage_worktree=False, integration_branch="main", merge_on_retire=False
        )
        policy = policy_from_config(config)
        self.assertEqual(
            policy,
            SublaneIntegrationPolicy(
                manage_worktree=False, integration_branch="main", merge_on_retire=False
            ),
        )

    def test_default_config_maps_to_default_policy(self) -> None:
        self.assertEqual(
            policy_from_config(SublaneIntegrationConfig.default()),
            SublaneIntegrationPolicy.default(),
        )


class LaunchUseCaseTest(unittest.TestCase):
    def test_git_workspace_creates_worktree_via_port(self) -> None:
        ops = FakeGitOperations(git=True)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        decision = use_case.plan_launch(branch="issue_12604", worktree_path="/tmp/wt")
        self.assertEqual(decision.action, LAUNCH_CREATE_WORKTREE)
        self.assertEqual(ops.created, ("issue_12604", "/tmp/wt"))

    def test_non_git_workspace_skips_and_creates_nothing(self) -> None:
        ops = FakeGitOperations(git=False)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        decision = use_case.plan_launch(branch="issue_12604", worktree_path="/tmp/wt")
        self.assertEqual(decision.action, LAUNCH_SKIP_NO_GIT)
        self.assertIsNone(ops.created)


class RetireUseCaseTest(unittest.TestCase):
    def test_clean_git_retire_attempts_and_succeeds(self) -> None:
        ops = FakeGitOperations(git=True)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        decision = use_case.evaluate_retire(invariants=_ok_invariants())
        self.assertEqual(decision.state, RETIRE_OK)
        self.assertTrue(decision.merge_performed)
        self.assertTrue(ops.merge_called)

    def test_stale_review_generation_blocks_integration_before_merge(self) -> None:
        # #13518 review R2-F7: an inadmissible latest review generation (a stale approval / an
        # unresolved blocking finding) fences integration BEFORE any git probe or merge.
        ops = FakeGitOperations(git=True)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        invariants = RetireInvariants(
            target_identity_known=True, verification_passed=True, issue_closed=True,
            owner_approval_present=True, callbacks_drained=True, durable_record_recorded=True,
            latest_generation_admissible=False,
        )
        decision = use_case.evaluate_retire(invariants=invariants)
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(INTEGRATION_STALE_REVIEW_GENERATION, decision.blocked_reasons)
        self.assertFalse(ops.merge_called)  # no merge on a stale generation

    def test_omitted_generation_invariant_blocks_fail_closed(self) -> None:
        # #13518 R4-F3: the programmatic use case's RetireInvariants defaults the generation fence to
        # UNSATISFIED (fail-closed) — a caller that omits it (every other invariant satisfied) is
        # blocked, never default-admitted. No merge is attempted on the stale generation.
        ops = FakeGitOperations(git=True)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        invariants = RetireInvariants(
            target_identity_known=True, verification_passed=True, issue_closed=True,
            owner_approval_present=True, callbacks_drained=True, durable_record_recorded=True,
            # latest_generation_admissible omitted -> fail-closed default
        )
        decision = use_case.evaluate_retire(invariants=invariants)
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(INTEGRATION_STALE_REVIEW_GENERATION, decision.blocked_reasons)
        self.assertFalse(ops.merge_called)

    def test_explicit_admissible_generation_retires_ok(self) -> None:
        # With the generation invariant explicitly asserted True (and all others), retire is OK.
        ops = FakeGitOperations(git=True)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        self.assertEqual(use_case.evaluate_retire(invariants=_ok_invariants()).state, RETIRE_OK)

    def test_dirty_worktree_blocks_before_merge(self) -> None:
        ops = FakeGitOperations(git=True, dirty=True)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        decision = use_case.evaluate_retire(invariants=_ok_invariants())
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_DIRTY_WORKTREE, decision.blocked_reasons)
        # The merge side effect must never run when a gate already blocks.
        self.assertFalse(ops.merge_called)

    def test_missing_invariant_blocks_before_merge(self) -> None:
        ops = FakeGitOperations(git=True)
        invariants = RetireInvariants(
            target_identity_known=True,
            verification_passed=True,
            issue_closed=True,
            owner_approval_present=False,  # owner approval missing
            callbacks_drained=True,
            durable_record_recorded=True,
        )
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        decision = use_case.evaluate_retire(invariants=invariants)
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertFalse(ops.merge_called)

    def test_merge_conflict_redecides_to_blocked(self) -> None:
        ops = FakeGitOperations(git=True, conflict=True)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        decision = use_case.evaluate_retire(invariants=_ok_invariants())
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertEqual(decision.primary_reason, BLOCKED_MERGE_CONFLICT)
        self.assertTrue(ops.merge_called)

    def test_merge_opt_out_skips_merge_call(self) -> None:
        ops = FakeGitOperations(git=True, conflict=True, branch_resolved=False)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy(merge_on_retire=False)
        )
        decision = use_case.evaluate_retire(invariants=_ok_invariants())
        self.assertEqual(decision.state, RETIRE_OK)
        self.assertFalse(ops.merge_called)

    def test_non_git_retire_does_not_call_merge(self) -> None:
        ops = FakeGitOperations(git=False)
        use_case = SublaneIntegrationUseCase(
            operations=ops, policy=SublaneIntegrationPolicy.default()
        )
        decision = use_case.evaluate_retire(invariants=_ok_invariants())
        self.assertEqual(decision.state, RETIRE_OK)
        self.assertFalse(ops.merge_called)


class LiveAdapterGatingTest(unittest.TestCase):
    def test_live_merge_is_gated_fail_closed(self) -> None:
        adapter = LiveSublaneGitOperations(repo_root=Path("/nonexistent"))
        with self.assertRaises(NotImplementedError):
            adapter.merge_to_integration_branch("main")

    def test_live_adapter_satisfies_port(self) -> None:
        self.assertIsInstance(
            LiveSublaneGitOperations(repo_root=Path(".")), SublaneGitOperations
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
