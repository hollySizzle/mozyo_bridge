"""Pure post-close retirement / cleanup state machine tests (Redmine #13686).

Pins the second of the two machines j#77124 必須訂正1 required be kept apart from
integration:

- the authorization binding: a cleanup only runs under the exact integration action key
  that authorized it, and a different key refuses rather than being ignored;
- the gates (issue closed, integration confirmed, CI settled green, callbacks drained, owner
  gates resolved, the record naming our own lane), which stop the step even though what is
  left is non-destructive;
- that the machine has exactly **one** step, ``process_retire``, and a complete stage table
  on every decision;
- that it performs **no Git operation at all**. All three it once had are retired: the remote
  branch delete (review j#96344 finding 1), the local branch delete (j#96396 finding 1) and
  the worktree removal (j#96401 finding 1). What is pinned here is the absence — no step, no
  state, no preflight field and no policy through which any of them can come back without
  re-arguing the ruling;
- idempotent resume: a ``done`` step is not re-run, and a stale ledger satisfies nothing.

Pure decisions only — no IO, no git, no use case (those are the integration tests).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    BLOCKED_ACTION_KEY_MISMATCH,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_PENDING,
    StepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    retirement_cleanup_policy,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    BLOCKED_CI_UNSETTLED,
    BLOCKED_LANE_IDENTITY_MISMATCH,
    BLOCKED_INTEGRATION_UNCONFIRMED,
    BLOCKED_ISSUE_NOT_CLOSED,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    STATE_CLEANUP_BLOCKED,
    STATE_CLEANUP_PREFLIGHT,
    STATE_PROCESS_RETIRING,
    STATE_RETIRED,
    CLEANUP_STEPS,
    GIT_MUTATING_STEPS,
    REF_DELETING_STEPS,
    STEP_PROCESS_RETIRE,
    CleanupActionRecord,
    CleanupPreflight,
    decide_cleanup,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_journal import (
    render_cleanup_journal,
)

TIP = "a" * 40
MOVED = "c" * 40
AUTHORIZING_KEY = "issue=13686|lane_generation=3|source_head=" + TIP


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
        "authorizing_action_key": AUTHORIZING_KEY,
        "issue_closed": True,
        "integration_confirmed": True,
        "integration_ci_settled_green": True,
        "callbacks_drained": True,
        "owner_gates_resolved": True,
        "lane_is_foreign": False,
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
            _record(),
            _clean(authorizing_action_key="issue=99999|lane_generation=1"),
        )
        self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_ACTION_KEY_MISMATCH,))
        self.assertIsNone(decision.next_step)

    def test_a_missing_authorization_refuses(self) -> None:
        decision = decide_cleanup(
            _record(), _clean(authorizing_action_key="")
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
            ({"lane_is_foreign": True}, BLOCKED_LANE_IDENTITY_MISMATCH),
        )
        for overrides, reason in cases:
            decision = decide_cleanup(_record(), _clean(**overrides))
            self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED, overrides)
            self.assertEqual(decision.blocked_reasons, (reason,), overrides)
            self.assertIsNone(decision.next_step, overrides)

    def test_a_failing_gate_stops_the_process_retire(self) -> None:
        # These gates are what establish that the lane is finished AND that it is ours.
        # Releasing another lane's managed process is a cross-lane side effect exactly as
        # removing its checkout was, so being non-destructive earns the step nothing here.
        decision = decide_cleanup(_record(), _clean(issue_closed=False))
        self.assertIsNone(decision.next_step)
        self.assertEqual(decision.outcome_for(STEP_PROCESS_RETIRE), OUTCOME_PENDING)

    def test_an_omitted_preflight_field_is_blocked_not_admitted(self) -> None:
        decision = decide_cleanup(
            _record(),
            CleanupPreflight(authorizing_action_key=AUTHORIZING_KEY),
        )
        self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED)
        self.assertIn(BLOCKED_ISSUE_NOT_CLOSED, decision.blocked_reasons)
        self.assertIn(BLOCKED_LANE_IDENTITY_MISMATCH, decision.blocked_reasons)

    def test_every_failing_gate_is_reported(self) -> None:
        decision = decide_cleanup(
            _record(),
            _clean(issue_closed=False, callbacks_drained=False),
        )
        self.assertEqual(
            set(decision.blocked_reasons),
            {BLOCKED_ISSUE_NOT_CLOSED, BLOCKED_UNRESOLVED_CALLBACK},
        )


class StepOrderTest(unittest.TestCase):
    def test_preflight_is_an_entry_phase_never_a_resting_state(self) -> None:
        for preflight in (_clean(), _clean(issue_closed=False), _clean(lane_is_foreign=True)):
            self.assertNotEqual(
                decide_cleanup(_record(), preflight).state,
                STATE_CLEANUP_PREFLIGHT,
            )

    def test_full_run_walks_the_steps_one_at_a_time(self) -> None:
        record = _record()
        world = _clean()

        first = decide_cleanup(record, world)
        self.assertEqual(
            (first.state, first.next_step), (STATE_PROCESS_RETIRING, STEP_PROCESS_RETIRE)
        )

        # The process retire is the only step: nothing follows it, because the worktree
        # removal that used to (j#96401 finding 1) is gone, as are both ref deletes.
        second = decide_cleanup(
            record, world, ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(second.state, STATE_RETIRED)
        self.assertIsNone(second.next_step)

    def test_stage_table_is_complete_on_every_decision(self) -> None:
        record = _record()
        decision = decide_cleanup(
            record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(
            [step for step, _ in decision.step_outcomes], [STEP_PROCESS_RETIRE]
        )
        self.assertEqual(decision.outcome_for(STEP_PROCESS_RETIRE), OUTCOME_DONE)


class NoGitOperationTest(unittest.TestCase):
    """All three Git steps this machine once had are retired, not guarded.

    The remote branch delete went first (j#96344 finding 1), the local branch delete second
    (j#96396 finding 1), and the worktree removal last (j#96401 finding 1). Every one of them
    named its target by something another actor could re-point — a remote ref, a local ref, a
    path — and checked the property that mattered in a *separate* invocation from the one
    that acted. What is pinned here is the absence, on every surface a step could come back
    through: the step tuple, the state set, the blocked vocabulary, and the preflight fields.
    """

    def test_the_process_retire_is_the_only_step(self) -> None:
        self.assertEqual(CLEANUP_STEPS, (STEP_PROCESS_RETIRE,))

    def test_no_step_touches_git(self) -> None:
        self.assertEqual(GIT_MUTATING_STEPS, frozenset())
        self.assertEqual(REF_DELETING_STEPS, frozenset())
        joined = " ".join(CLEANUP_STEPS).lower()
        for token in ("delete", "remove", "remote", "worktree", "branch"):
            self.assertNotIn(token, joined, token)

    def test_the_surviving_step_is_the_one_whose_primitive_takes_its_identity(self) -> None:
        # Not a coincidence and worth pinning as intent: `release_process(issue,
        # lane_generation)` cannot be re-pointed between the decision and the call, which is
        # exactly what a path or a ref name could be.
        record = _record()
        decision = decide_cleanup(record, _clean())
        self.assertEqual(decision.next_step, STEP_PROCESS_RETIRE)
        self.assertIn(record.issue, decision.reason)
        self.assertIn(str(record.lane_generation), decision.reason)

    def test_no_state_survives_a_retired_step(self) -> None:
        # A leftover `worktree_removing` / `branch_cleanup` state would be a seam a later
        # change could hang the operation back on without re-arguing the ruling.
        for gone in ("worktree_removing", "branch_cleanup"):
            self.assertNotIn(gone, retirement_cleanup_policy.CLEANUP_STATES, gone)

    def test_no_preflight_field_promises_a_protection_nothing_evaluates(self) -> None:
        # Eight fields were the conditions of the retired steps: five branch-shaped (R7) and
        # three worktree-shaped (R8). A caller that could still set one would be buying a
        # protection no gate reads — the failure mode this issue hit three times.
        for gone in (
            "branch_checked_out_elsewhere",
            "unpushed_unique_commits",
            "branch_reachable_from_target",
            "branch_patch_equivalent",
            "branch_tip",
            "worktree_is_foreign",
            "worktree_clean",
            "worktree_path_registered",
            "is_git_workspace",
        ):
            with self.assertRaises(TypeError, msg=gone):
                CleanupPreflight(**{gone: True})

    def test_the_resting_record_says_where_the_retired_work_went(self) -> None:
        record = _record()
        decision = decide_cleanup(
            record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(decision.state, STATE_RETIRED)
        # Silence about a step callers used to get would read as "it happened".
        self.assertIn("operator", decision.reason)
        self.assertIn(record.worktree_path, decision.reason)
        self.assertIn(record.branch, decision.reason)

    def test_the_lane_identity_gate_still_guards_the_surviving_step(self) -> None:
        # The foreign-lane refusal outlived the steps it was introduced for: releasing
        # another lane's managed process is the same class of cross-lane side effect.
        decision = decide_cleanup(_record(), _clean(lane_is_foreign=True))
        self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_LANE_IDENTITY_MISMATCH,))
        self.assertIsNone(decision.next_step)


class R3LedgerFenceTest(unittest.TestCase):
    """R3 review j#96368 finding 3: order and provenance are checked before any step runs."""

    def test_a_step_this_machine_does_not_have_is_not_believed(self) -> None:
        # The order fence and the unknown-step fence are the same guard: a ledger naming a
        # step outside `CLEANUP_STEPS` — including one of the three retired ones — is refused
        # rather than ignored, so a stale record cannot resurrect a withdrawn step's `done`.
        record = _record()
        for foreign_step in ("worktree_remove", "local_branch_delete", "remote_branch_delete"):
            decision = decide_cleanup(
                record, _clean(), ledger=_ledger(record, foreign_step)
            )
            self.assertEqual(decision.state, STATE_CLEANUP_BLOCKED, foreign_step)
            self.assertIsNone(decision.next_step, foreign_step)

    def test_a_ledger_this_actuator_did_not_write_does_not_count(self) -> None:
        record = _record()
        decision = decide_cleanup(
            record,
            _clean(),
            ledger=[
                StepOutcome(
                    record.action_key, STEP_PROCESS_RETIRE, OUTCOME_DONE,
                    recorded_by="somebody-else",
                )
            ],
            trusted_recorder="actuator:/lane|lane_br",
        )
        self.assertEqual(decision.next_step, STEP_PROCESS_RETIRE)


class NonGitWorkspaceTest(unittest.TestCase):
    def test_the_machine_is_identical_in_a_non_git_workspace(self) -> None:
        # There used to be a `is_git_workspace` branch marking the Git steps `not_applicable`.
        # With no Git step left the distinction cannot change an outcome, so the field is gone
        # rather than kept and ignored — and this is what pins that the non-Git lane, which
        # only ever had the process retire, still gets exactly it.
        record = _record()
        first = decide_cleanup(record, _clean())
        self.assertEqual(first.next_step, STEP_PROCESS_RETIRE)

        second = decide_cleanup(
            record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(second.state, STATE_RETIRED)
        self.assertIsNone(second.next_step)
        self.assertEqual([step for step, _ in second.step_outcomes], [STEP_PROCESS_RETIRE])


class IdempotencyTest(unittest.TestCase):
    def test_a_done_step_is_not_re_run(self) -> None:
        record = _record()
        decision = decide_cleanup(
            record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        self.assertEqual(decision.state, STATE_RETIRED)
        self.assertIsNone(decision.next_step)

    def test_a_ledger_under_a_drifted_key_satisfies_nothing(self) -> None:
        record = _record()
        stale = _record(recorded_source_head=MOVED)
        decision = decide_cleanup(
            record,
            _clean(),
            ledger=[StepOutcome(stale.action_key, STEP_PROCESS_RETIRE, OUTCOME_DONE)],
        )
        self.assertEqual(decision.next_step, STEP_PROCESS_RETIRE)

    def test_a_blocked_step_outcome_does_not_count_as_progress(self) -> None:
        record = _record()
        decision = decide_cleanup(
            record,
            _clean(),
            ledger=[StepOutcome(record.action_key, STEP_PROCESS_RETIRE, OUTCOME_BLOCKED)],
        )
        self.assertEqual(decision.next_step, STEP_PROCESS_RETIRE)


class JournalRendererTest(unittest.TestCase):
    def test_blocked_record_names_the_zero_side_effect(self) -> None:
        record = _record()
        decision = decide_cleanup(record, _clean(lane_is_foreign=True))
        rendered = render_cleanup_journal(decision, record)
        self.assertIn("## cleanup_blocked", rendered)
        self.assertIn(BLOCKED_LANE_IDENTITY_MISMATCH, rendered)
        self.assertIn("no process released", rendered)
        self.assertIn("removes no checkout and deletes no ref at all", rendered)

    def test_record_emits_the_full_stage_table(self) -> None:
        record = _record()
        decision = decide_cleanup(
            record, _clean(), ledger=_ledger(record, STEP_PROCESS_RETIRE)
        )
        rendered = render_cleanup_journal(decision, record)
        self.assertIn("## retirement cleanup decision", rendered)
        self.assertIn(f"- step.{STEP_PROCESS_RETIRE}: {OUTCOME_DONE}", rendered)
        self.assertNotIn("worktree_remove", rendered)
        self.assertIn(f"- integration_action_key: {AUTHORIZING_KEY}", rendered)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
