"""Pure gated auto-integration state machine tests (Redmine #13686).

Pins the decision core of the coordinator-owned auto-integration actuator the owner
authorized in j#96335, under the design consultation answer j#77124:

- the mode gate (``auto`` / ``coordinator_confirmed`` / ``disabled``, and the fail-closed
  reading of an unrecognized mode);
- the non-Git path, which has no integration at all;
- every action-time gate recorded as a fail-closed ``integration_blocked`` — target drift,
  post-review source mutation, unreachable source, inadmissible review generation, source
  CI, unknown target, dirty / foreign worktree, unpushed commits, callback and owner gates;
- the two terminal no-op dispositions kept apart: ``already_integrated`` by target ancestry
  and ``patch_equivalent`` by explicit evidence, neither re-producing a merge;
- the ff-only refusal of a non-fast-forward, and the merge-commit disposition's conflict;
- the state order preflight -> (apply) -> push -> CI -> integrated, one step per call;
- the action key's six identity fields and the idempotency they buy: a ``done`` step is not
  re-run, and a stale ledger under a drifted key satisfies nothing.

Pure decisions only — no IO, no git, no use case (those are the integration tests).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    BLOCKED_ACTION_RECORD_INVALID,
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_FOREIGN_WORKTREE,
    BLOCKED_INTEGRATION_CI_FAILED,
    BLOCKED_MERGE_CONFLICT,
    BLOCKED_MODE_UNRECOGNIZED,
    BLOCKED_NON_FAST_FORWARD,
    BLOCKED_REVIEW_INADMISSIBLE,
    BLOCKED_SOURCE_CI_NOT_GREEN,
    BLOCKED_SOURCE_MUTATED,
    BLOCKED_SOURCE_UNREACHABLE,
    BLOCKED_TARGET_DRIFT,
    BLOCKED_UNKNOWN_TARGET,
    BLOCKED_UNPUSHED_COMMITS,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_UNRESOLVED_OWNER_GATE,
    DISPOSITION_FAST_FORWARD,
    DISPOSITION_MERGE_COMMIT,
    EMPTY_TARGET_HEAD,
    MODE_AUTO,
    MODE_COORDINATOR_CONFIRMED,
    MODE_DISABLED,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    STATE_ALREADY_INTEGRATED,
    STATE_AWAITING_CI,
    STATE_CONFIRMATION_REQUIRED,
    STATE_DISABLED,
    STATE_INTEGRATED,
    STATE_INTEGRATION_APPLY,
    STATE_INTEGRATION_BLOCKED,
    STATE_INTEGRATION_PREFLIGHT,
    STATE_NOT_APPLICABLE,
    STATE_PATCH_EQUIVALENT,
    STATE_PUSH_WAITING,
    STEP_INTEGRATION_APPLY,
    STEP_INTEGRATION_CI,
    STEP_PUSH,
    AutoIntegrationPolicy,
    IntegrationActionRecord,
    IntegrationPreflight,
    StepOutcome,
    decide_integration,
    render_integration_action_journal,
)

SOURCE = "a" * 40
TARGET = "b" * 40
OTHER = "c" * 40


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
    """A preflight in which every gate passes and a fast-forward is possible."""
    fields: dict = {
        "is_git_workspace": True,
        "observed_target_head": TARGET,
        "fast_forward_possible": True,
        "already_integrated": False,
        "patch_equivalent_evidence": False,
        "merge_conflict": False,
        "source_worktree_dirty": False,
        "worktree_is_foreign": False,
        "unpushed_unique_commits": False,
        "source_head_matches_review": True,
        "source_origin_reachable": True,
        "review_generation_admissible": True,
        "target_identity_known": True,
        "source_ci_green": True,
        "integration_ci_green": True,
        "callbacks_drained": True,
        "owner_gates_resolved": True,
        "coordinator_confirmed": False,
    }
    fields.update(overrides)
    return IntegrationPreflight(**fields)  # type: ignore[arg-type]


AUTO = AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch="main")


class ModeGateTest(unittest.TestCase):
    def test_disabled_is_the_behavior_preserving_default(self) -> None:
        self.assertEqual(AutoIntegrationPolicy.default().mode, MODE_DISABLED)
        decision = decide_integration(
            AutoIntegrationPolicy.default(), _record(), _clean()
        )
        self.assertEqual(decision.state, STATE_DISABLED)
        self.assertIsNone(decision.next_step)
        self.assertTrue(decision.is_terminal)
        self.assertFalse(decision.integrated)

    def test_unrecognized_mode_fails_closed_rather_than_defaulting_to_auto(self) -> None:
        # An unknown mode must not be read as the one value that could integrate without
        # intent. It stops, and it stops even on a fully clean preflight.
        decision = decide_integration(
            AutoIntegrationPolicy(mode="whatever"), _record(), _clean()
        )
        self.assertIsNone(decision.next_step)
        # ...and it is reported as broken, not as `disabled`: a misconfigured workspace must
        # not read in a durable record as one whose operator deliberately opted out.
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_MODE_UNRECOGNIZED,))
        self.assertNotEqual(decision.state, STATE_DISABLED)

    def test_the_integrated_reason_does_not_claim_a_disabled_ci_gate(self) -> None:
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE)]
        relaxed = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", require_integration_ci=False
        )
        decision = decide_integration(
            relaxed, record, _clean(integration_ci_green=False), ledger=ledger
        )
        self.assertEqual(decision.state, STATE_INTEGRATED)
        self.assertIn("disabled by config", decision.reason)
        required = decide_integration(AUTO, record, _clean(), ledger=ledger)
        self.assertIn("exact-SHA CI are both settled", required.reason)

    def test_coordinator_confirmed_waits_for_this_exact_action_key(self) -> None:
        policy = AutoIntegrationPolicy(
            mode=MODE_COORDINATOR_CONFIRMED, integration_branch="main"
        )
        decision = decide_integration(policy, _record(), _clean())
        self.assertEqual(decision.state, STATE_CONFIRMATION_REQUIRED)
        self.assertIsNone(decision.next_step)
        self.assertIn(_record().action_key, decision.reason)

    def test_coordinator_confirmation_does_not_relax_a_gate(self) -> None:
        # A confirmation authorizes actuation; it is not an override. A blocked action stays
        # blocked with the confirmation present.
        policy = AutoIntegrationPolicy(
            mode=MODE_COORDINATOR_CONFIRMED, integration_branch="main"
        )
        decision = decide_integration(
            policy,
            _record(),
            _clean(coordinator_confirmed=True, source_worktree_dirty=True),
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_DIRTY_WORKTREE, decision.blocked_reasons)

    def test_confirmed_action_proceeds_to_the_push(self) -> None:
        policy = AutoIntegrationPolicy(
            mode=MODE_COORDINATOR_CONFIRMED, integration_branch="main"
        )
        decision = decide_integration(
            policy, _record(), _clean(coordinator_confirmed=True)
        )
        self.assertEqual(decision.state, STATE_PUSH_WAITING)
        self.assertEqual(decision.next_step, STEP_PUSH)


class NonGitWorkspaceTest(unittest.TestCase):
    def test_non_git_workspace_has_no_integration(self) -> None:
        decision = decide_integration(
            AUTO, _record(), _clean(is_git_workspace=False)
        )
        self.assertEqual(decision.state, STATE_NOT_APPLICABLE)
        self.assertIsNone(decision.next_step)
        self.assertEqual(decision.blocked_reasons, ())
        self.assertFalse(decision.integrated)


class ActionRecordTest(unittest.TestCase):
    def test_action_key_covers_the_six_identity_fields(self) -> None:
        key = _record().action_key
        for expected in (
            "issue=13686",
            "lane_generation=3",
            f"source_head={SOURCE}",
            "target_ref=main",
            f"expected_target_head={TARGET}",
            "review_generation=j#96337",
        ):
            self.assertIn(expected, key)

    def test_any_identity_drift_produces_a_different_key(self) -> None:
        base = _record().action_key
        for overrides in (
            {"issue": "13687"},
            {"lane_generation": 4},
            {"source_head": OTHER},
            {"target_ref": "release"},
            {"expected_target_head": OTHER},
            {"review_generation": "j#99999"},
        ):
            self.assertNotEqual(_record(**overrides).action_key, base, overrides)

    def test_malformed_record_is_blocked_before_anything_else(self) -> None:
        for overrides in (
            {"issue": "   "},
            {"lane_generation": 0},
            {"lane_generation": True},
            {"source_head": "abc"},
            {"target_ref": ""},
            {"review_generation": ""},
            {"expected_target_head": "not-a-sha"},
        ):
            decision = decide_integration(AUTO, _record(**overrides), _clean())
            self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED, overrides)
            self.assertEqual(
                decision.blocked_reasons, (BLOCKED_ACTION_RECORD_INVALID,), overrides
            )

    def test_empty_target_head_sentinel_is_a_valid_record(self) -> None:
        # A target ref that does not exist yet is a stated fact, not an omitted field.
        decision = decide_integration(
            AUTO,
            _record(expected_target_head=EMPTY_TARGET_HEAD),
            _clean(observed_target_head=EMPTY_TARGET_HEAD),
        )
        self.assertEqual(decision.state, STATE_PUSH_WAITING)


class FailClosedGateTest(unittest.TestCase):
    def test_every_gate_is_reported_not_just_the_first(self) -> None:
        decision = decide_integration(
            AUTO,
            _record(),
            _clean(
                source_worktree_dirty=True,
                callbacks_drained=False,
                source_ci_green=False,
            ),
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(
            set(decision.blocked_reasons),
            {
                BLOCKED_DIRTY_WORKTREE,
                BLOCKED_UNRESOLVED_CALLBACK,
                BLOCKED_SOURCE_CI_NOT_GREEN,
            },
        )
        # The primary is the most fundamental of the set, not the first one evaluated.
        self.assertEqual(decision.primary_reason, BLOCKED_DIRTY_WORKTREE)

    def test_each_gate_blocks_on_its_own(self) -> None:
        cases = (
            ({"worktree_is_foreign": True}, BLOCKED_FOREIGN_WORKTREE),
            ({"target_identity_known": False}, BLOCKED_UNKNOWN_TARGET),
            ({"review_generation_admissible": False}, BLOCKED_REVIEW_INADMISSIBLE),
            ({"source_head_matches_review": False}, BLOCKED_SOURCE_MUTATED),
            ({"source_origin_reachable": False}, BLOCKED_SOURCE_UNREACHABLE),
            ({"unpushed_unique_commits": True}, BLOCKED_UNPUSHED_COMMITS),
            ({"source_worktree_dirty": True}, BLOCKED_DIRTY_WORKTREE),
            ({"source_ci_green": False}, BLOCKED_SOURCE_CI_NOT_GREEN),
            ({"owner_gates_resolved": False}, BLOCKED_UNRESOLVED_OWNER_GATE),
            ({"callbacks_drained": False}, BLOCKED_UNRESOLVED_CALLBACK),
            ({"observed_target_head": OTHER}, BLOCKED_TARGET_DRIFT),
        )
        for overrides, reason in cases:
            decision = decide_integration(AUTO, _record(), _clean(**overrides))
            self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED, overrides)
            self.assertEqual(decision.blocked_reasons, (reason,), overrides)
            self.assertIsNone(decision.next_step, overrides)

    def test_an_omitted_preflight_field_is_blocked_not_admitted(self) -> None:
        # Every safety-bearing field defaults to its unsatisfied value, so a caller that
        # supplies only "it is a Git workspace" gets a refusal rather than an integration.
        decision = decide_integration(
            AUTO, _record(), IntegrationPreflight(is_git_workspace=True)
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_FOREIGN_WORKTREE, decision.blocked_reasons)
        self.assertIn(BLOCKED_REVIEW_INADMISSIBLE, decision.blocked_reasons)

    def test_source_ci_gate_is_skippable_by_config_but_no_safety_gate_is(self) -> None:
        relaxed = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", require_source_ci=False
        )
        decision = decide_integration(relaxed, _record(), _clean(source_ci_green=False))
        self.assertEqual(decision.state, STATE_PUSH_WAITING)
        # The same config cannot make a dirty worktree acceptable.
        blocked = decide_integration(
            relaxed, _record(), _clean(source_ci_green=False, source_worktree_dirty=True)
        )
        self.assertEqual(blocked.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(blocked.blocked_reasons, (BLOCKED_DIRTY_WORKTREE,))


class TerminalDispositionTest(unittest.TestCase):
    def test_already_integrated_is_terminal_and_performs_nothing(self) -> None:
        decision = decide_integration(
            AUTO, _record(), _clean(already_integrated=True, fast_forward_possible=False)
        )
        self.assertEqual(decision.state, STATE_ALREADY_INTEGRATED)
        self.assertIsNone(decision.next_step)
        self.assertTrue(decision.integrated)

    def test_patch_equivalent_needs_explicit_evidence_and_stays_distinct(self) -> None:
        decision = decide_integration(
            AUTO,
            _record(),
            _clean(patch_equivalent_evidence=True, fast_forward_possible=False),
        )
        self.assertEqual(decision.state, STATE_PATCH_EQUIVALENT)
        self.assertIsNone(decision.next_step)
        self.assertTrue(decision.integrated)
        # Without the evidence the same world is a plain non-fast-forward refusal, never a
        # silent "it is probably already there".
        without = decide_integration(
            AUTO, _record(), _clean(fast_forward_possible=False)
        )
        self.assertEqual(without.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(without.blocked_reasons, (BLOCKED_NON_FAST_FORWARD,))

    def test_ancestry_outranks_patch_equivalence(self) -> None:
        decision = decide_integration(
            AUTO,
            _record(),
            _clean(already_integrated=True, patch_equivalent_evidence=True),
        )
        self.assertEqual(decision.state, STATE_ALREADY_INTEGRATED)

    def test_a_blocked_action_is_never_reported_already_integrated(self) -> None:
        decision = decide_integration(
            AUTO, _record(), _clean(already_integrated=True, worktree_is_foreign=True)
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertFalse(decision.integrated)


class DispositionTest(unittest.TestCase):
    def test_ff_only_refuses_a_non_fast_forward(self) -> None:
        decision = decide_integration(
            AUTO, _record(), _clean(fast_forward_possible=False)
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_NON_FAST_FORWARD,))

    def test_merge_commit_disposition_applies_in_a_dedicated_worktree(self) -> None:
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        decision = decide_integration(
            policy, _record(), _clean(fast_forward_possible=False)
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_APPLY)
        self.assertEqual(decision.next_step, STEP_INTEGRATION_APPLY)
        self.assertEqual(decision.disposition, DISPOSITION_MERGE_COMMIT)

    def test_merge_conflict_fails_closed_before_any_step(self) -> None:
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        decision = decide_integration(
            policy, _record(), _clean(fast_forward_possible=False, merge_conflict=True)
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_MERGE_CONFLICT,))
        self.assertIsNone(decision.next_step)

    def test_effective_disposition_is_fast_forward_when_one_is_possible(self) -> None:
        # `ff_only: false` only ADMITS a merge commit; it does not create one where a
        # fast-forward suffices, and the durable record must not claim one either.
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        decision = decide_integration(policy, _record(), _clean())
        self.assertEqual(decision.disposition, DISPOSITION_FAST_FORWARD)
        self.assertEqual(decision.next_step, STEP_PUSH)


class StateOrderTest(unittest.TestCase):
    def test_preflight_is_an_entry_phase_never_a_resting_state(self) -> None:
        # Pinned deliberately: turning `integration_preflight` into a returned state would
        # give a consumer a "still working" reading for a decision that has, in fact,
        # already resolved to blocked or to a later state.
        worlds = (
            _clean(),
            _clean(source_worktree_dirty=True),
            _clean(is_git_workspace=False),
            _clean(already_integrated=True),
            _clean(fast_forward_possible=False),
        )
        for preflight in worlds:
            self.assertNotEqual(
                decide_integration(AUTO, _record(), preflight).state,
                STATE_INTEGRATION_PREFLIGHT,
            )

    def test_fast_forward_run_goes_push_then_ci_then_integrated(self) -> None:
        record = _record()
        pending_ci = _clean(integration_ci_green=False)

        first = decide_integration(AUTO, record, pending_ci)
        self.assertEqual((first.state, first.next_step), (STATE_PUSH_WAITING, STEP_PUSH))

        pushed = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE)]
        second = decide_integration(AUTO, record, pending_ci, ledger=pushed)
        self.assertEqual(
            (second.state, second.next_step), (STATE_AWAITING_CI, STEP_INTEGRATION_CI)
        )

        third = decide_integration(AUTO, record, _clean(), ledger=pushed)
        self.assertEqual(third.state, STATE_INTEGRATED)
        self.assertIsNone(third.next_step)
        self.assertTrue(third.integrated)

    def test_settled_but_red_integration_ci_is_its_own_reason(self) -> None:
        # "The run settled" and "the run was green" are separate facts: a recorded CI step
        # with a non-green verdict is a failure, not a still-pending gate.
        record = _record()
        ledger = [
            StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE),
            StepOutcome(record.action_key, STEP_INTEGRATION_CI, OUTCOME_DONE),
        ]
        decision = decide_integration(
            AUTO, record, _clean(integration_ci_green=False), ledger=ledger
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_INTEGRATION_CI_FAILED,))

    def test_integration_ci_gate_is_skippable_by_config(self) -> None:
        record = _record()
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", require_integration_ci=False
        )
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE)]
        decision = decide_integration(
            policy, record, _clean(integration_ci_green=False), ledger=ledger
        )
        self.assertEqual(decision.state, STATE_INTEGRATED)


class IdempotencyTest(unittest.TestCase):
    def test_a_done_step_is_not_re_run(self) -> None:
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE)]
        decision = decide_integration(AUTO, record, _clean(), ledger=ledger)
        self.assertEqual(decision.state, STATE_INTEGRATED)
        self.assertIsNone(decision.next_step)

    def test_a_ledger_under_a_drifted_key_satisfies_nothing(self) -> None:
        # The lane advanced, so the action is a different action. The old push does not
        # count, and the new one is decided from scratch.
        stale = _record(source_head=OTHER)
        ledger = [StepOutcome(stale.action_key, STEP_PUSH, OUTCOME_DONE)]
        decision = decide_integration(AUTO, _record(), _clean(), ledger=ledger)
        self.assertEqual(decision.state, STATE_PUSH_WAITING)
        self.assertEqual(decision.next_step, STEP_PUSH)

    def test_a_blocked_step_outcome_does_not_count_as_progress(self) -> None:
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_BLOCKED)]
        decision = decide_integration(AUTO, record, _clean(), ledger=ledger)
        self.assertEqual(decision.next_step, STEP_PUSH)

    def test_a_resumed_merge_run_does_not_re_apply_the_merge(self) -> None:
        record = _record()
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        world = _clean(fast_forward_possible=False)
        ledger = [
            StepOutcome(
                record.action_key, STEP_INTEGRATION_APPLY, OUTCOME_DONE, head=OTHER
            )
        ]
        decision = decide_integration(policy, record, world, ledger=ledger)
        self.assertEqual(decision.next_step, STEP_PUSH)


class JournalRendererTest(unittest.TestCase):
    def test_blocked_record_names_every_reason_and_the_zero_side_effect(self) -> None:
        record = _record()
        decision = decide_integration(
            AUTO, record, _clean(source_worktree_dirty=True, callbacks_drained=False)
        )
        rendered = render_integration_action_journal(decision, record)
        self.assertIn("## integration_blocked", rendered)
        self.assertIn(f"- action_key: {record.action_key}", rendered)
        self.assertIn(BLOCKED_DIRTY_WORKTREE, rendered)
        self.assertIn(BLOCKED_UNRESOLVED_CALLBACK, rendered)
        self.assertIn("no force push, no rebase, no ref deleted", rendered)

    def test_integrated_record_separates_source_and_integration_heads(self) -> None:
        # A single head cannot prove a merge-commit integration, so both are emitted.
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE)]
        decision = decide_integration(AUTO, record, _clean(), ledger=ledger)
        rendered = render_integration_action_journal(
            decision, record, integration_head=OTHER
        )
        self.assertIn("## integration action decision", rendered)
        self.assertIn(f"- source_head: {SOURCE}", rendered)
        self.assertIn(f"- integration_head: {OTHER}", rendered)
        self.assertIn(f"- disposition: {DISPOSITION_FAST_FORWARD}", rendered)

    def test_an_in_progress_decision_is_not_rendered_as_a_refusal(self) -> None:
        # push_waiting / awaiting_ci / confirmation_required are neither blocked nor
        # integrated. Keying the heading on "integrated" put a refusal that never happened
        # into a durable record.
        record = _record()
        for preflight in (_clean(), _clean(integration_ci_green=False)):
            decision = decide_integration(
                AUTO,
                record,
                preflight,
                ledger=[StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE)]
                if not preflight.integration_ci_green
                else [],
            )
            rendered = render_integration_action_journal(decision, record)
            self.assertNotIn("## integration_blocked", rendered, decision.state)
            self.assertIn("## integration action decision", rendered, decision.state)
            self.assertIn(f"- next_step: {decision.next_step or 'none'}", rendered)

    def test_renderer_emits_no_private_path_or_pane_id(self) -> None:
        record = _record()
        rendered = render_integration_action_journal(
            decide_integration(AUTO, record, _clean()), record
        )
        self.assertNotIn("/Users/", rendered)
        self.assertNotIn("/private/tmp", rendered)
        self.assertNotIn("%", rendered)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
