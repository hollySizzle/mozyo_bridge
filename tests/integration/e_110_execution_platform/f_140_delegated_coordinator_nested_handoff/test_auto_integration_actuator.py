"""Auto-integration actuator composition tests (Redmine #13686).

Wires the two pure #13686 state machines to a recording fake Git port and pins what the
composition — not the decision — is responsible for:

- the config -> policy translations, including that the ``mode`` vocabulary is shared so an
  unrecognized value stays unrecognized rather than being translated into an actionable one;
- that the use case performs **only** the side effect the decision authorized: a refused
  action calls nothing at all, and a fast-forward never applies a merge;
- that a resumed run does not repeat a merge, a push, or a delete, and that a resumed
  merge-commit run pushes the commit the earlier apply produced rather than the source head;
- that a rejected non-force push stops the run rather than escalating to a force;
- that the asynchronous CI gate is recorded pending rather than waited on;
- the Git / non-Git, already-integrated, patch-equivalent, conflict, dirty, and recovery-lane
  paths the acceptance enumerates, driven end to end through the use case;
- that the cleanup use case never substitutes a stronger operation for a refused one.

Hermetic: the Git port is a fake that records its calls. No real ``git``, no network.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    AutoIntegrationGitOperations,
    AutoIntegrationUseCase,
    ManagedProcessOperations,
    MergeResult,
    PushResult,
    cleanup_policy_from_config,
    integration_policy_from_config,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    MODE_AUTO,
    MODE_DISABLED,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_PENDING,
    STATE_ALREADY_INTEGRATED,
    STATE_AWAITING_CI,
    STATE_DISABLED,
    STATE_INTEGRATED,
    STATE_INTEGRATION_BLOCKED,
    STATE_NOT_APPLICABLE,
    STATE_PATCH_EQUIVALENT,
    STEP_INTEGRATION_APPLY,
    STEP_INTEGRATION_CI,
    STEP_PUSH,
    AutoIntegrationPolicy,
    CoordinatorConfirmation,
    IntegrationActionRecord,
    IntegrationCiEvidence,
    IntegrationPreflight,
    IntegrationWorktree,
    StepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    STATE_CLEANUP_BLOCKED,
    STATE_RETIRED,
    STEP_LOCAL_BRANCH_DELETE,
    STEP_PROCESS_RETIRE,
    STEP_WORKTREE_REMOVE,
    CleanupActionRecord,
    CleanupPreflight,
    RetirementCleanupPolicy,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (
    AutoIntegrationConfig,
)

SOURCE = "a" * 40
TARGET = "b" * 40
MERGE_HEAD = "d" * 40


@dataclass
class FakeGitOperations:
    """A recording :class:`AutoIntegrationGitOperations` with configurable results."""

    merge_result: MergeResult = field(
        default_factory=lambda: MergeResult(conflicted=False, integration_head=MERGE_HEAD)
    )
    push_result: PushResult = field(default_factory=lambda: PushResult(accepted=True))
    worktree_removed: bool = True
    branch_deleted: bool = True
    calls: List[Tuple[str, dict]] = field(default_factory=list)

    def apply_merge(
        self, *, source_head: str, target_ref: str, integration_worktree: str
    ) -> MergeResult:
        self.calls.append(
            (
                "apply_merge",
                {
                    "source_head": source_head,
                    "target_ref": target_ref,
                    "integration_worktree": integration_worktree,
                },
            )
        )
        return self.merge_result

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        self.calls.append(
            ("push_non_force", {"source_head": source_head, "target_ref": target_ref})
        )
        return self.push_result

    def remove_worktree(self, *, worktree_path: str) -> bool:
        self.calls.append(("remove_worktree", {"worktree_path": worktree_path}))
        return self.worktree_removed

    def delete_local_branch(self, *, branch: str, expected_tip: str) -> bool:
        self.calls.append(
            ("delete_local_branch", {"branch": branch, "expected_tip": expected_tip})
        )
        return self.branch_deleted

    @property
    def performed(self) -> List[str]:
        return [name for name, _ in self.calls]


@dataclass
class FakeProcessOperations:
    released: bool = True
    calls: List[dict] = field(default_factory=list)

    def release_process(self, *, issue: str, lane_generation: int) -> bool:
        self.calls.append({"issue": issue, "lane_generation": lane_generation})
        return self.released


def _record(**overrides: object) -> IntegrationActionRecord:
    fields: dict = {
        "issue": "13686",
        "lane_generation": 3,
        "source_head": SOURCE,
        "target_ref": "main",
        "expected_target_head": TARGET,
        "review_generation": "j#96337",
    }
    fields.update(overrides)
    return IntegrationActionRecord(**fields)  # type: ignore[arg-type]


def _clean(**overrides: object) -> IntegrationPreflight:
    fields: dict = {
        "is_git_workspace": True,
        "observed_target_head": TARGET,
        "fast_forward_possible": True,
        "source_worktree_dirty": False,
        "worktree_is_foreign": False,
        "unpushed_unique_commits": False,
        "source_head_matches_review": True,
        "source_origin_reachable": True,
        "review_generation_admissible": True,
        "target_identity_known": True,
        "source_ci_green": True,
        "callbacks_drained": True,
        "owner_gates_resolved": True,
        "integration_ci": IntegrationCiEvidence(
            integration_head=SOURCE, workflow="required-ci", run="run-1", conclusion="success"
        ),
    }
    fields.update(overrides)
    return IntegrationPreflight(**fields)  # type: ignore[arg-type]


def _dedicated(**overrides: object) -> IntegrationWorktree:
    """A verified dedicated integration worktree (registered, clean, not the lane's)."""
    fields: dict = {
        "path": "<integration-worktree>",
        "registered": True,
        "is_lane_worktree": False,
        "clean": True,
        "checked_out_branch": "main",
    }
    fields.update(overrides)
    return IntegrationWorktree(**fields)  # type: ignore[arg-type]


def _use_case(operations: FakeGitOperations, **kwargs: object) -> AutoIntegrationUseCase:
    defaults: dict = {
        "integration_policy": AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main"
        ),
    }
    defaults.update(kwargs)
    return AutoIntegrationUseCase(operations=operations, **defaults)  # type: ignore[arg-type]


class PortConformanceTest(unittest.TestCase):
    def test_the_fake_satisfies_the_declared_ports(self) -> None:
        self.assertIsInstance(FakeGitOperations(), AutoIntegrationGitOperations)
        self.assertIsInstance(FakeProcessOperations(), ManagedProcessOperations)


class ConfigTranslationTest(unittest.TestCase):
    def test_integration_fields_map_through(self) -> None:
        config = AutoIntegrationConfig(
            mode=MODE_AUTO,
            integration_branch="release",
            ff_only=False,
            require_source_ci=False,
            require_integration_ci=False,
        )
        policy = integration_policy_from_config(config)
        self.assertEqual(policy.mode, MODE_AUTO)
        self.assertEqual(policy.integration_branch, "release")
        self.assertFalse(policy.ff_only)
        self.assertFalse(policy.require_source_ci)
        self.assertFalse(policy.require_integration_ci)

    def test_cleanup_fields_map_through_with_remote_delete_off(self) -> None:
        policy = cleanup_policy_from_config(AutoIntegrationConfig.default())
        self.assertTrue(policy.remove_worktree)
        self.assertTrue(policy.delete_local_branch)
        # R1 review j#96344 finding 1: there is no remote-delete knob to be False; the
        # operation is gone, so no config can ask for it.
        self.assertFalse(hasattr(policy, "delete_remote_branch"))

    def test_the_default_config_translates_to_a_disabled_actuator(self) -> None:
        policy = integration_policy_from_config(AutoIntegrationConfig.default())
        self.assertEqual(policy.mode, MODE_DISABLED)

    def test_the_mode_vocabulary_is_shared_so_an_unknown_value_stays_unknown(self) -> None:
        # Translating an unrecognized mode into an actionable one is the failure this pins:
        # the literal must survive the boundary so the decision can fail closed on it. (The
        # loader rejects such a value outright; this covers a record constructed in code.)
        translated = integration_policy_from_config(
            AutoIntegrationConfig(mode="not_a_mode")
        )
        self.assertEqual(translated.mode, "not_a_mode")


class ZeroSideEffectTest(unittest.TestCase):
    def test_a_refused_action_calls_nothing(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations).run_integration(
            _record(), _clean(source_worktree_dirty=True)
        )
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.outcomes, [])

    def test_a_disabled_actuator_calls_nothing(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(
            operations, integration_policy=AutoIntegrationPolicy.default()
        ).run_integration(_record(), _clean())
        self.assertEqual(report.final_decision.state, STATE_DISABLED)
        self.assertEqual(operations.performed, [])

    def test_a_non_git_workspace_calls_nothing(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations).run_integration(
            _record(), _clean(is_git_workspace=False)
        )
        self.assertEqual(report.final_decision.state, STATE_NOT_APPLICABLE)
        self.assertEqual(operations.performed, [])

    def test_already_integrated_performs_no_merge_and_no_push(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations).run_integration(
            _record(), _clean(already_integrated=True, fast_forward_possible=False)
        )
        self.assertEqual(report.final_decision.state, STATE_ALREADY_INTEGRATED)
        self.assertEqual(operations.performed, [])
        self.assertTrue(report.final_decision.integrated)

    def test_patch_equivalent_performs_no_merge_and_no_push(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations).run_integration(
            _record(),
            _clean(patch_equivalent_evidence=True, fast_forward_possible=False),
        )
        self.assertEqual(report.final_decision.state, STATE_PATCH_EQUIVALENT)
        self.assertEqual(operations.performed, [])


class FastForwardRunTest(unittest.TestCase):
    def test_a_fast_forward_pushes_the_source_head_and_never_merges(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations).run_integration(
            _record(), _clean(integration_ci=None)
        )
        self.assertEqual(operations.performed, ["push_non_force"])
        self.assertEqual(
            operations.calls[0][1],
            {"source_head": SOURCE, "target_ref": "main"},
        )
        # It stops at the asynchronous CI gate rather than waiting on it.
        self.assertEqual(report.final_decision.state, STATE_AWAITING_CI)
        self.assertEqual(
            [(o.step, o.outcome) for o in report.outcomes],
            [(STEP_PUSH, OUTCOME_DONE), (STEP_INTEGRATION_CI, OUTCOME_PENDING)],
        )

    def test_a_settled_green_ci_completes_the_run_without_re_pushing(self) -> None:
        record = _record()
        operations = FakeGitOperations()
        ledger = [
            StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE),
            StepOutcome(record.action_key, STEP_INTEGRATION_CI, OUTCOME_DONE),
        ]
        report = _use_case(operations).run_integration(
            record, _clean(), ledger=ledger
        )
        self.assertEqual(report.final_decision.state, STATE_INTEGRATED)
        self.assertEqual(operations.performed, [])

    def test_a_rejected_push_stops_the_run_and_never_escalates(self) -> None:
        operations = FakeGitOperations(
            push_result=PushResult(accepted=False, rejected=True, detail="stale target")
        )
        report = _use_case(operations).run_integration(_record(), _clean())
        self.assertEqual(operations.performed, ["push_non_force"])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)
        self.assertIn("stale target", report.outcomes[-1].detail)


class MergeCommitRunTest(unittest.TestCase):
    def _policy(self) -> AutoIntegrationPolicy:
        return AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )

    def test_a_merge_disposition_applies_then_pushes_the_merge_commit(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations, integration_policy=self._policy()).run_integration(
            _record(),
            _clean(
                fast_forward_possible=False,
                integration_ci=None,
                integration_worktree=_dedicated(),
            ),
        )
        self.assertEqual(operations.performed, ["apply_merge", "push_non_force"])
        self.assertEqual(
            operations.calls[0][1]["integration_worktree"], "<integration-worktree>"
        )
        # The pushed head is the commit the merge produced, not the source head.
        self.assertEqual(operations.calls[1][1]["source_head"], MERGE_HEAD)
        self.assertEqual(report.integration_head, MERGE_HEAD)

    def test_a_conflict_stops_before_any_push(self) -> None:
        operations = FakeGitOperations(
            merge_result=MergeResult(conflicted=True, detail="conflict in a.py")
        )
        report = _use_case(operations, integration_policy=self._policy()).run_integration(
            _record(),
            _clean(fast_forward_possible=False, integration_worktree=_dedicated()),
        )
        self.assertEqual(operations.performed, ["apply_merge"])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)
        self.assertIn("conflict in a.py", report.outcomes[-1].detail)

    def test_the_lane_s_own_worktree_never_reaches_the_merge(self) -> None:
        # R1 review j#96344 finding 3: R1 forwarded ANY non-empty path, so passing the lane's
        # own checkout made the actuator do the one thing j#77124 forbids. The refusal is now
        # the decision's, before any step is authorized — the port is never called.
        operations = FakeGitOperations()
        report = _use_case(operations, integration_policy=self._policy()).run_integration(
            _record(),
            _clean(
                fast_forward_possible=False,
                integration_worktree=_dedicated(is_lane_worktree=True),
            ),
        )
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.outcomes, [])
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)

    def test_a_missing_dedicated_worktree_refuses_rather_than_using_the_lane(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations, integration_policy=self._policy()).run_integration(
            _record(), _clean(fast_forward_possible=False)
        )
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)

    def test_a_resumed_run_pushes_the_recorded_merge_commit_not_the_source(self) -> None:
        # The in-memory head is gone on a resume; without reading it back from the ledger
        # the push would silently integrate a different commit than the apply created.
        record = _record()
        operations = FakeGitOperations()
        ledger = [
            StepOutcome(
                record.action_key,
                STEP_INTEGRATION_APPLY,
                OUTCOME_DONE,
                head=MERGE_HEAD,
            )
        ]
        _use_case(operations, integration_policy=self._policy()).run_integration(
            record,
            _clean(fast_forward_possible=False, integration_ci=None,
                   integration_worktree=_dedicated()),
            ledger=ledger,
        )
        self.assertEqual(operations.performed, ["push_non_force"])
        self.assertEqual(operations.calls[0][1]["source_head"], MERGE_HEAD)


class RecoveryLaneTest(unittest.TestCase):
    def test_a_foreign_lane_worktree_refuses_before_any_operation(self) -> None:
        # The recovery / original pair case: the checkout under the action is not this
        # lane's, so nothing runs — not even a read the port would otherwise be asked for.
        operations = FakeGitOperations()
        report = _use_case(operations).run_integration(
            _record(), _clean(worktree_is_foreign=True)
        )
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_a_target_that_moved_refuses_rather_than_racing(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations).run_integration(
            _record(), _clean(observed_target_head="e" * 40)
        )
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])


class CleanupCompositionTest(unittest.TestCase):
    def _record(self, **overrides: object) -> CleanupActionRecord:
        fields: dict = {
            "issue": "13686",
            "lane_generation": 3,
            "branch": "issue_13686_lane",
            "worktree_path": "<lane-worktree>",
            "recorded_source_head": SOURCE,
            "integration_action_key": _record().action_key,
        }
        fields.update(overrides)
        return CleanupActionRecord(**fields)  # type: ignore[arg-type]

    def _clean(self, **overrides: object) -> CleanupPreflight:
        fields: dict = {
            "is_git_workspace": True,
            "authorizing_action_key": _record().action_key,
            "issue_closed": True,
            "integration_confirmed": True,
            "integration_ci_settled_green": True,
            "callbacks_drained": True,
            "owner_gates_resolved": True,
            "worktree_is_foreign": False,
            "worktree_clean": True,
            "worktree_path_registered": True,
            "branch_checked_out_elsewhere": False,
            "unpushed_unique_commits": False,
            "branch_reachable_from_target": True,
            "branch_tip": SOURCE,
        }
        fields.update(overrides)
        return CleanupPreflight(**fields)  # type: ignore[arg-type]

    def test_a_full_cleanup_runs_the_three_default_steps_in_order(self) -> None:
        operations = FakeGitOperations()
        processes = FakeProcessOperations()
        report = _use_case(operations, processes=processes).run_cleanup(
            self._record(), self._clean()
        )
        self.assertEqual(report.final_decision.state, STATE_RETIRED)
        self.assertEqual(
            operations.performed, ["remove_worktree", "delete_local_branch"]
        )
        self.assertEqual(processes.calls, [{"issue": "13686", "lane_generation": 3}])
        self.assertEqual(
            [o.step for o in report.outcomes],
            [STEP_PROCESS_RETIRE, STEP_WORKTREE_REMOVE, STEP_LOCAL_BRANCH_DELETE],
        )
        # The delete is a compare-and-swap against the recorded tip.
        self.assertEqual(
            operations.calls[1][1], {"branch": "issue_13686_lane", "expected_tip": SOURCE}
        )

    def test_the_port_cannot_delete_a_remote_ref_at_all(self) -> None:
        # R1 review j#96344 finding 1: the operation is removed from the interface rather
        # than guarded, so no policy, config, or code path can reach it.
        self.assertFalse(hasattr(FakeGitOperations(), "delete_remote_branch"))
        operations = FakeGitOperations()
        _use_case(operations, processes=FakeProcessOperations()).run_cleanup(
            self._record(), self._clean()
        )
        self.assertNotIn("delete_remote_branch", operations.performed)

    def test_a_dirty_worktree_stops_before_the_branch_delete(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(
            self._record(), self._clean(worktree_clean=False)
        )
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_a_failed_removal_is_blocked_and_not_retried_with_force(self) -> None:
        operations = FakeGitOperations(worktree_removed=False)
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(
            self._record(), self._clean()
        )
        self.assertEqual(operations.performed, ["remove_worktree"])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)
        self.assertIn("nothing forced", report.outcomes[-1].detail)

    def test_a_resumed_cleanup_does_not_delete_twice(self) -> None:
        record = self._record()
        operations = FakeGitOperations()
        ledger = [
            StepOutcome(record.action_key, STEP_PROCESS_RETIRE, OUTCOME_DONE),
            StepOutcome(record.action_key, STEP_WORKTREE_REMOVE, OUTCOME_DONE),
        ]
        _use_case(operations, processes=FakeProcessOperations()).run_cleanup(
            record, self._clean(), ledger=ledger
        )
        self.assertEqual(operations.performed, ["delete_local_branch"])

    def test_a_non_git_lane_releases_the_process_and_touches_no_git(self) -> None:
        operations = FakeGitOperations()
        processes = FakeProcessOperations()
        report = _use_case(operations, processes=processes).run_cleanup(
            self._record(), self._clean(is_git_workspace=False)
        )
        self.assertEqual(report.final_decision.state, STATE_RETIRED)
        self.assertEqual(operations.performed, [])
        self.assertEqual(len(processes.calls), 1)

    def test_a_missing_process_port_is_blocked_not_silently_skipped(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(operations).run_cleanup(self._record(), self._clean())
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
