"""Pure post-close retirement / cleanup state machine tests (Redmine #13686).

Pins the second of the two machines j#77124 必須訂正1 required be kept apart from
integration:

- the authorization binding: a cleanup only runs under the exact integration action key
  that authorized it, and a different key refuses rather than being ignored;
- the always-enforced gates (issue closed, integration confirmed, CI settled green,
  callbacks drained, owner gates resolved, non-foreign worktree), which stop **every** step
  including the non-destructive process retire;
- the step order process_retire -> worktree_remove -> local_branch_delete ->
  remote_branch_delete, one step per call, with a complete stage table on every decision;
- the j#77124 必須訂正2 safety conditions: a worktree is removed only when clean and at its
  exact registered path (never forced), and the local branch delete is a compare-and-swap
  requiring no holding worktree, no unique unpushed commit, target reachability or patch
  equivalence, and an unchanged tip;
- the non-Git path, where the worktree / branch steps are explicit ``not_applicable`` and
  the process retire still runs;
- remote branch deletion staying off unless explicitly enabled;
- idempotent resume: a ``done`` step is not re-run, and a stale ledger satisfies nothing.

Pure decisions only — no IO, no git, no use case (those are the integration tests).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    BLOCKED_ACTION_KEY_MISMATCH,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_PENDING,
    StepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    BLOCKED_BRANCH_CHECKED_OUT,
    BLOCKED_BRANCH_TIP_DRIFT,
    BLOCKED_CI_UNSETTLED,
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_FOREIGN_WORKTREE,
    BLOCKED_INTEGRATION_UNCONFIRMED,
    BLOCKED_ISSUE_NOT_CLOSED,
    BLOCKED_NOT_INTEGRATED_REF,
    BLOCKED_UNPUSHED_COMMITS,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    BLOCKED_WORKTREE_PATH_UNREGISTERED,
    STATE_BRANCH_CLEANUP,
    STATE_CLEANUP_BLOCKED,
    STATE_CLEANUP_PREFLIGHT,
    STATE_PROCESS_RETIRING,
    STATE_RETIRED,
    STATE_WORKTREE_REMOVING,
    STEP_LOCAL_BRANCH_DELETE,
    STEP_PROCESS_RETIRE,
    STEP_REMOTE_BRANCH_DELETE,
    STEP_WORKTREE_REMOVE,
    CleanupActionRecord,
    CleanupPreflight,
    RetirementCleanupPolicy,
    decide_cleanup,
    render_cleanup_journal,
)

TIP = "a" * 40
MOVED = "c" * 40
AUTHORIZING_KEY = "issue=13686|lane_generation=3|source_head=" + TIP

DEFAULT_POLICY = RetirementCleanupPolicy.default()


def _record(**overrides: object) -> CleanupActionRecord:
    fields: dict = {
        "issue": "13686",
        "lane_generation": 3,
        "branch": "issue_13686_auto_integration_actuator_r1",
        "worktree_path": "<lane-worktree>",
        "recorded_source_head": TIP,
        "integration_action_key": AUTHORIZING_KEY,
    }
    fields.update(overrides)
    return CleanupActionRecord(**fields)  # type: ignore[arg-type]


def _clean(**overrides: object) -> CleanupPreflight:
    """A preflight in which every cleanup gate passes."""
    fields: dict = {
        "is_git_workspace": True,
        "authorizing_action_key": AUTHORIZING_KEY,
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
        "branch_patch_equivalent": False,
        "branch_tip": TIP,
    }
    fields.update(overrides)
    return CleanupPreflight(**fields)  # type: ignore[arg-type]


def _ledger(record: CleanupActionRecord, *steps: str) -> list[StepOutcome]:
    return [StepOutcome(record.action_key, step, OUTCOME_DONE) for step in steps]


class AuthorizationBindingTest(unittest.TestCase):
    def test_a_different_integration_action_key_refuses(self) -> None:
        # A destructive step must not inherit another action's authorization, so this is a
        # refusal rather than a silently ignored ledger entry.
        decision = decide_cleanup(
            DEFAULT_POLICY,
            _record(),
            _clean(authorizing_action_key="issue=99999|lane_generation=1"),
        )
        self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_ACTION_KEY_MISMATCH,))
        self.assertIsNone(decision.next_step)

    def test_a_missing_authorization_refuses(self) -> None:
        decision = decide_cleanup(
            DEFAULT_POLICY, _record(), _clean(authorizing_action_key="")
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_ACTION_KEY_MISMATCH,))


class AlwaysEnforcedGateTest(unittest.TestCase):
    def test_each_gate_blocks_on_its_own(self) -> None:
        cases = (
            ({"issue_closed": False}, BLOCKED_ISSUE_NOT_CLOSED),
            ({"integration_confirmed": False}, BLOCKED_INTEGRATION_UNCONFIRMED),
            ({"integration_ci_settled_green": False}, BLOCKED_CI_UNSETTLED),
            ({"callbacks_drained": False}, BLOCKED_UNRESOLVED_CALLBACK),
            ({"owner_gates_resolved": False}, BLOCKED_UNRESOLVED_OWNER_GATE),
            ({"worktree_is_foreign": True}, BLOCKED_FOREIGN_WORKTREE),
        )
        for overrides, reason in cases:
            decision = decide_cleanup(DEFAULT_POLICY, _record(), _clean(**overrides))
            self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED, overrides)
            self.assertEqual(decision.blocked_reasons, (reason,), overrides)
            self.assertIsNone(decision.next_step, overrides)

    def test_a_failing_gate_stops_even_the_non_destructive_process_retire(self) -> None:
        # These gates are what establish that the lane is finished; before they pass, not
        # even releasing the process is authorized.
        decision = decide_cleanup(DEFAULT_POLICY, _record(), _clean(issue_closed=False))
        self.assertIsNone(decision.next_step)
        self.assertEqual(decision.outcome_for(STEP_PROCESS_RETIRE), OUTCOME_PENDING)

    def test_an_omitted_preflight_field_is_blocked_not_admitted(self) -> None:
        decision = decide_cleanup(
            DEFAULT_POLICY,
            _record(),
            CleanupPreflight(
                is_git_workspace=True, authorizing_action_key=AUTHORIZING_KEY
            ),
        )
        self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED)
        self.assertIn(BLOCKED_ISSUE_NOT_CLOSED, decision.blocked_reasons)
        self.assertIn(BLOCKED_FOREIGN_WORKTREE, decision.blocked_reasons)

    def test_every_failing_gate_is_reported(self) -> None:
        decision = decide_cleanup(
            DEFAULT_POLICY,
            _record(),
            _clean(issue_closed=False, callbacks_drained=False),
        )
        self.assertEqual(
            set(decision.blocked_reasons),
            {BLOCKED_ISSUE_NOT_CLOSED, BLOCKED_UNRESOLVED_CALLBACK},
        )


class StepOrderTest(unittest.TestCase):
    def test_preflight_is_an_entry_phase_never_a_resting_state(self) -> None:
        for preflight in (_clean(), _clean(issue_closed=False), _clean(is_git_workspace=False)):
            self.assertNotEqual(
                decide_cleanup(DEFAULT_POLICY, _record(), preflight).state,
                STATE_CLEANUP_PREFLIGHT,
            )

    def test_full_run_walks_the_steps_one_at_a_time(self) -> None:
        record = _record()
        world = _clean()

        first = decide_cleanup(DEFAULT_POLICY, record, world)
        self.assertEqual(
            (first.state, first.next_step), (STATE_PROCESS_RETIRING, STEP_PROCESS_RETIRE)
        )

        second = decide_cleanup(
            DEFAULT_POLICY, record, world, ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(
            (second.state, second.next_step),
            (STATE_WORKTREE_REMOVING, STEP_WORKTREE_REMOVE),
        )

        third = decide_cleanup(
            DEFAULT_POLICY,
            record,
            world,
            ledger=_ledger(record, STEP_PROCESS_RETIRE, STEP_WORKTREE_REMOVE),
        )
        self.assertEqual(
            (third.state, third.next_step),
            (STATE_BRANCH_CLEANUP, STEP_LOCAL_BRANCH_DELETE),
        )

        fourth = decide_cleanup(
            DEFAULT_POLICY,
            record,
            world,
            ledger=_ledger(
                record,
                STEP_PROCESS_RETIRE,
                STEP_WORKTREE_REMOVE,
                STEP_LOCAL_BRANCH_DELETE,
            ),
        )
        self.assertEqual(fourth.state, STATE_RETIRED)
        self.assertIsNone(fourth.next_step)
        # Remote deletion stayed off, and says so rather than staying silent.
        self.assertEqual(
            fourth.outcome_for(STEP_REMOTE_BRANCH_DELETE), OUTCOME_NOT_APPLICABLE
        )

    def test_stage_table_is_complete_on_every_decision(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY, record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(
            [step for step, _ in decision.step_outcomes],
            [
                STEP_PROCESS_RETIRE,
                STEP_WORKTREE_REMOVE,
                STEP_LOCAL_BRANCH_DELETE,
                STEP_REMOTE_BRANCH_DELETE,
            ],
        )
        self.assertEqual(decision.outcome_for(STEP_PROCESS_RETIRE), OUTCOME_DONE)
        self.assertEqual(decision.outcome_for(STEP_WORKTREE_REMOVE), OUTCOME_PENDING)


class WorktreeRemovalSafetyTest(unittest.TestCase):
    def test_dirty_worktree_refuses_and_force_is_not_an_answer(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(worktree_clean=False),
            ledger=_ledger(record, STEP_PROCESS_RETIRE),
        )
        self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_DIRTY_WORKTREE,))
        self.assertEqual(decision.outcome_for(STEP_WORKTREE_REMOVE), OUTCOME_BLOCKED)
        self.assertIn("--force", decision.reason)

    def test_unregistered_path_refuses(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(worktree_path_registered=False),
            ledger=_ledger(record, STEP_PROCESS_RETIRE),
        )
        self.assertEqual(
            decision.blocked_reasons, (BLOCKED_WORKTREE_PATH_UNREGISTERED,)
        )

    def test_a_refused_removal_stops_the_branch_delete_that_would_follow(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(worktree_clean=False),
            ledger=_ledger(record, STEP_PROCESS_RETIRE),
        )
        self.assertEqual(
            decision.outcome_for(STEP_LOCAL_BRANCH_DELETE), OUTCOME_PENDING
        )
        self.assertIsNone(decision.next_step)

    def test_disabled_removal_is_not_applicable_not_silently_skipped(self) -> None:
        record = _record()
        policy = RetirementCleanupPolicy(remove_worktree=False)
        decision = decide_cleanup(
            policy, record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(
            decision.outcome_for(STEP_WORKTREE_REMOVE), OUTCOME_NOT_APPLICABLE
        )
        self.assertEqual(decision.next_step, STEP_LOCAL_BRANCH_DELETE)


class LocalBranchDeleteCasTest(unittest.TestCase):
    def _after_worktree(self, record: CleanupActionRecord) -> list[StepOutcome]:
        return _ledger(record, STEP_PROCESS_RETIRE, STEP_WORKTREE_REMOVE)

    def test_each_cas_condition_refuses_on_its_own(self) -> None:
        record = _record()
        cases = (
            ({"branch_checked_out_elsewhere": True}, BLOCKED_BRANCH_CHECKED_OUT),
            ({"unpushed_unique_commits": True}, BLOCKED_UNPUSHED_COMMITS),
            (
                {"branch_reachable_from_target": False},
                BLOCKED_NOT_INTEGRATED_REF,
            ),
            ({"branch_tip": MOVED}, BLOCKED_BRANCH_TIP_DRIFT),
        )
        for overrides, reason in cases:
            decision = decide_cleanup(
                DEFAULT_POLICY,
                record,
                _clean(**overrides),
                ledger=self._after_worktree(record),
            )
            self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED, overrides)
            self.assertEqual(decision.blocked_reasons, (reason,), overrides)
            self.assertIn("git branch -D", decision.reason)

    def test_patch_equivalence_satisfies_the_reachability_condition(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(branch_reachable_from_target=False, branch_patch_equivalent=True),
            ledger=self._after_worktree(record),
        )
        self.assertEqual(decision.next_step, STEP_LOCAL_BRANCH_DELETE)

    def test_a_branch_that_moved_since_the_action_survives(self) -> None:
        # The compare-and-swap is the whole safety of the delete: a moved tip is a different
        # branch than the one that was integrated.
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(branch_tip=MOVED),
            ledger=self._after_worktree(record),
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_BRANCH_TIP_DRIFT,))

    def test_disabled_delete_is_not_applicable(self) -> None:
        record = _record()
        policy = RetirementCleanupPolicy(delete_local_branch=False)
        decision = decide_cleanup(
            policy, record, _clean(), ledger=self._after_worktree(record)
        )
        self.assertEqual(decision.state, STATE_RETIRED)
        self.assertEqual(
            decision.outcome_for(STEP_LOCAL_BRANCH_DELETE), OUTCOME_NOT_APPLICABLE
        )


class RemoteBranchDeleteTest(unittest.TestCase):
    def test_remote_delete_is_off_by_default(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(),
            ledger=_ledger(
                record,
                STEP_PROCESS_RETIRE,
                STEP_WORKTREE_REMOVE,
                STEP_LOCAL_BRANCH_DELETE,
            ),
        )
        self.assertEqual(decision.state, STATE_RETIRED)
        self.assertEqual(
            decision.outcome_for(STEP_REMOTE_BRANCH_DELETE), OUTCOME_NOT_APPLICABLE
        )

    def test_enabling_it_adds_a_step_behind_every_local_condition(self) -> None:
        record = _record()
        policy = RetirementCleanupPolicy(delete_remote_branch=True)
        decision = decide_cleanup(
            policy,
            record,
            _clean(),
            ledger=_ledger(
                record,
                STEP_PROCESS_RETIRE,
                STEP_WORKTREE_REMOVE,
                STEP_LOCAL_BRANCH_DELETE,
            ),
        )
        self.assertEqual(decision.next_step, STEP_REMOTE_BRANCH_DELETE)
        # And it does not relax any of them: a refused local delete never reaches it.
        refused = decide_cleanup(
            policy,
            record,
            _clean(unpushed_unique_commits=True),
            ledger=_ledger(record, STEP_PROCESS_RETIRE, STEP_WORKTREE_REMOVE),
        )
        self.assertEqual(refused.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(
            refused.outcome_for(STEP_REMOTE_BRANCH_DELETE), OUTCOME_PENDING
        )


class NonGitWorkspaceTest(unittest.TestCase):
    def test_process_retire_runs_and_the_git_steps_are_not_applicable(self) -> None:
        record = _record()
        world = _clean(is_git_workspace=False)

        first = decide_cleanup(DEFAULT_POLICY, record, world)
        self.assertEqual(first.next_step, STEP_PROCESS_RETIRE)

        second = decide_cleanup(
            DEFAULT_POLICY, record, world, ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(second.state, STATE_RETIRED)
        self.assertIsNone(second.next_step)
        for step in (
            STEP_WORKTREE_REMOVE,
            STEP_LOCAL_BRANCH_DELETE,
            STEP_REMOTE_BRANCH_DELETE,
        ):
            self.assertEqual(second.outcome_for(step), OUTCOME_NOT_APPLICABLE, step)

    def test_a_non_git_lane_is_not_blocked_by_the_foreign_worktree_gate(self) -> None:
        # There is no worktree to be foreign; the gate must not fire on its fail-closed
        # default and strand every directory-scaffold lane.
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            CleanupPreflight(
                is_git_workspace=False,
                authorizing_action_key=AUTHORIZING_KEY,
                issue_closed=True,
                integration_confirmed=True,
                integration_ci_settled_green=True,
                callbacks_drained=True,
                owner_gates_resolved=True,
            ),
        )
        self.assertEqual(decision.next_step, STEP_PROCESS_RETIRE)


class IdempotencyTest(unittest.TestCase):
    def test_a_done_step_is_not_re_run(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(),
            ledger=_ledger(record, STEP_PROCESS_RETIRE, STEP_WORKTREE_REMOVE),
        )
        self.assertEqual(decision.next_step, STEP_LOCAL_BRANCH_DELETE)

    def test_a_ledger_under_a_drifted_key_satisfies_nothing(self) -> None:
        record = _record()
        stale = _record(recorded_source_head=MOVED)
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(),
            ledger=[StepOutcome(stale.action_key, STEP_PROCESS_RETIRE, OUTCOME_DONE)],
        )
        self.assertEqual(decision.next_step, STEP_PROCESS_RETIRE)

    def test_a_blocked_step_outcome_does_not_count_as_progress(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(),
            ledger=[StepOutcome(record.action_key, STEP_PROCESS_RETIRE, OUTCOME_BLOCKED)],
        )
        self.assertEqual(decision.next_step, STEP_PROCESS_RETIRE)


class JournalRendererTest(unittest.TestCase):
    def test_blocked_record_names_the_zero_side_effect(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY,
            record,
            _clean(worktree_clean=False),
            ledger=_ledger(record, STEP_PROCESS_RETIRE),
        )
        rendered = render_cleanup_journal(decision, record)
        self.assertIn("## cleanup_blocked", rendered)
        self.assertIn(BLOCKED_DIRTY_WORKTREE, rendered)
        self.assertIn("no worktree removed, no ref deleted", rendered)
        self.assertIn("`git branch -D`", rendered)

    def test_record_emits_the_full_stage_table(self) -> None:
        record = _record()
        decision = decide_cleanup(
            DEFAULT_POLICY, record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        rendered = render_cleanup_journal(decision, record)
        self.assertIn("## retirement cleanup decision", rendered)
        self.assertIn(f"- step.{STEP_PROCESS_RETIRE}: {OUTCOME_DONE}", rendered)
        self.assertIn(f"- step.{STEP_WORKTREE_REMOVE}: {OUTCOME_PENDING}", rendered)
        self.assertIn(f"- integration_action_key: {AUTHORIZING_KEY}", rendered)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
