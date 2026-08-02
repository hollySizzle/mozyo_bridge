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
    BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE,
    BLOCKED_INTEGRATION_CI_HEAD_MISMATCH,
    BLOCKED_INTEGRATION_LOST_FROM_TARGET,
    BLOCKED_LEDGER_UNTRUSTWORTHY,
    BLOCKED_MERGE_CONFLICT,
    BLOCKED_MODE_UNRECOGNIZED,
    BLOCKED_TARGET_NOT_CONFIGURED,
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
    BLOCKED_PUSH_HEAD_MISMATCH,
    BLOCKED_PUSH_OUTCOME_HEAD_MISSING,
    BLOCKED_SOURCE_CI_EVIDENCE_INCOMPLETE,
    BLOCKED_SOURCE_CI_HEAD_MISMATCH,
    CI_GATE_GREEN,
    CI_GATE_NOT_REACHED,
    CI_GATE_STATES,
    DISPOSITION_FAST_FORWARD,
    DISPOSITION_MERGE_COMMIT,
    EMPTY_TARGET_HEAD,
    INTEGRATION_MODES,
    MODE_AUTO,
    MODE_DISABLED,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    STATE_ALREADY_INTEGRATED,
    STATE_AWAITING_CI,
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
    PUSH_ACCEPTED,
    PUSH_REMOTE_MOVED,
    AutoIntegrationPolicy,
    IntegrationActionRecord,
    IntegrationCiEvidence,
    MERGE_MERGED,
    LEDGER_MERGE_STATUS_UNSOUND,
    LEDGER_MERGE_VERSION_MISSING,
    IntegrationPreflight,
    StepOutcome,
    build_integration_action_record,
    decide_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_journal import (
    render_integration_action_journal,
)

SOURCE = "a" * 40
RECORDER = "actuator:/lane|lane_br"
TARGET = "b" * 40
OTHER = "c" * 40
#: What a real apply records about the git that built the commit — required on a `done`
#: apply since j#96441 finding 4 (a version the record could not name was accepted before).
GIT_VERSION = "git version 2.50.1"


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
        "source_ci": IntegrationCiEvidence(
            integration_head=SOURCE, workflow="required-ci", run="src-1", conclusion="success"
        ),
        "callbacks_drained": True,
        "owner_gates_resolved": True,
        # Post-push: what this action landed is still on the target. Read only once a trusted
        # push receipt exists, and it is what replaces the pre-push expected-head comparison
        # at that point (R5 review j#96385 finding 2).
        "landed_head_on_target": True,
        # The CI evidence names the exact commit a fast-forward push lands (the source head),
        # plus the required check and the run — a bare "it was green" is no longer accepted.
        "integration_ci": IntegrationCiEvidence(
            integration_head=SOURCE, workflow="required-ci", run="run-1", conclusion="success"
        ),
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

    def test_an_integrated_decision_always_carries_a_green_exact_sha_gate(self) -> None:
        # R2 review j#96350 finding 1: `waived` is withdrawn, so `integrated` cannot mean
        # "no CI ran". There are only two CI-gate states left.
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)]
        decision = decide_integration(AUTO, record, _clean(), ledger=ledger)
        self.assertEqual(decision.state, STATE_INTEGRATED)
        self.assertEqual(decision.integration_ci, CI_GATE_GREEN)
        self.assertEqual(CI_GATE_STATES, {CI_GATE_GREEN, CI_GATE_NOT_REACHED})

class R15ReviewFinding4Test(unittest.TestCase):
    """A `done` apply must name the git that built the commit (j#96441 finding 4).

    The field was added in R14, filled in R15 by a probe that could fail and still report
    success, and read by nobody. Third time in this shape: prose, then a field no consumer
    read, then a field allowed to be empty.
    """

    def test_an_apply_without_a_recorded_version_does_not_authorize_the_push(self) -> None:
        record = _record()
        decision = decide_integration(
            AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch="main", ff_only=False),
            record,
            _clean(fast_forward_possible=False),
            ledger=[
                StepOutcome(
                    record.action_key,
                    STEP_INTEGRATION_APPLY,
                    OUTCOME_DONE,
                    head=OTHER,
                    recorded_by=RECORDER,
                    merge_status=MERGE_MERGED,
                    git_version="   ",
                )
            ],
            trusted_recorder=RECORDER,
        )
        self.assertTrue(decision.is_blocked)
        self.assertIsNone(decision.next_step)
        self.assertIn(LEDGER_MERGE_VERSION_MISSING, decision.reason)


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
                source_ci=None,
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
            ({"source_ci": None}, BLOCKED_SOURCE_CI_NOT_GREEN),
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

    def test_no_config_field_can_turn_a_ci_gate_off(self) -> None:
        # R2 review j#96350 finding 1: `require_source_ci` / `require_integration_ci` are gone.
        # j#77124, j#96335's target flow, and j#96337's fail-closed list all require green CI.
        for field_name in ("require_source_ci", "require_integration_ci"):
            self.assertFalse(hasattr(AUTO, field_name), field_name)

    def test_absent_source_ci_evidence_blocks(self) -> None:
        decision = decide_integration(AUTO, _record(), _clean(source_ci=None))
        self.assertEqual(decision.blocked_reasons, (BLOCKED_SOURCE_CI_NOT_GREEN,))


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
            policy,
            _record(),
            _clean(
                fast_forward_possible=False,
            ),
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
        pending_ci = _clean(integration_ci=None)

        first = decide_integration(AUTO, record, pending_ci)
        self.assertEqual((first.state, first.next_step), (STATE_PUSH_WAITING, STEP_PUSH))

        pushed = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)]
        second = decide_integration(AUTO, record, pending_ci, ledger=pushed)
        self.assertEqual(
            (second.state, second.next_step), (STATE_AWAITING_CI, STEP_INTEGRATION_CI)
        )

        third = decide_integration(AUTO, record, _clean(), ledger=pushed)
        self.assertEqual(third.state, STATE_INTEGRATED)
        self.assertIsNone(third.next_step)
        self.assertTrue(third.integrated)

    def test_settled_but_red_integration_ci_is_its_own_reason(self) -> None:
        # "The run settled" and "the run was green" are separate facts: a settled non-green
        # verdict is a failure, not a still-pending gate.
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)]
        red = IntegrationCiEvidence(
            integration_head=SOURCE, workflow="required-ci", run="run-1", conclusion="failure"
        )
        decision = decide_integration(
            AUTO, record, _clean(integration_ci=red), ledger=ledger
        )
        self.assertEqual(decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(decision.blocked_reasons, (BLOCKED_INTEGRATION_CI_FAILED,))
        # The reason names the run, so the record can be checked afterwards.
        self.assertIn("run-1", decision.reason)


class R12ReviewFinding2Test(unittest.TestCase):
    """A recorded apply is believed only when it recorded that it MERGED.

    j#96417 finding 2 asked for the typed status to reach the durable record; R12 added the
    field, and nothing read it. Measured on the pure decision: an apply recorded ``done`` with
    ``unrecognized_status``, trusted provenance and a full head reached ``push_waiting`` and
    authorized the push (j#96422 finding 2). Storing a fact and gating on it are two different
    pieces of work, and only the first had been done.
    """

    def _merge_policy(self) -> AutoIntegrationPolicy:
        return AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )

    def _applied(self, record, status: str) -> list:
        return [
            StepOutcome(
                record.action_key,
                STEP_INTEGRATION_APPLY,
                OUTCOME_DONE,
                head=OTHER,
                recorded_by=RECORDER,
                merge_status=status,
                git_version=GIT_VERSION,
            )
        ]

    def test_only_a_merged_apply_authorizes_the_push(self) -> None:
        record = _record()
        for status in (
            "unrecognized_status",
            "content_conflict",
            "merge_error",
            "primitive_unsupported",
            "probe_error",
            "",
        ):
            decision = decide_integration(
                self._merge_policy(),
                record,
                _clean(fast_forward_possible=False),
                ledger=self._applied(record, status),
                trusted_recorder=RECORDER,
            )
            self.assertTrue(decision.is_blocked, status)
            self.assertIsNone(decision.next_step, status)
            self.assertIn(LEDGER_MERGE_STATUS_UNSOUND, decision.reason, status)

    def test_a_merged_apply_still_reaches_the_push(self) -> None:
        record = _record()
        decision = decide_integration(
            self._merge_policy(),
            record,
            _clean(fast_forward_possible=False),
            ledger=self._applied(record, MERGE_MERGED),
            trusted_recorder=RECORDER,
        )
        self.assertEqual(decision.next_step, STEP_PUSH)
        self.assertFalse(decision.is_blocked)

    def test_a_merge_status_on_a_step_that_cannot_produce_one_is_refused(self) -> None:
        # A record about something that did not happen is refused rather than ignored.
        record = _record()
        decision = decide_integration(
            self._merge_policy(),
            record,
            _clean(fast_forward_possible=False),
            ledger=self._applied(record, MERGE_MERGED)
            + [
                StepOutcome(
                    record.action_key,
                    STEP_PUSH,
                    OUTCOME_DONE,
                    head=OTHER,
                    recorded_by=RECORDER,
                    merge_status=MERGE_MERGED,
                    git_version=GIT_VERSION, push_status=PUSH_ACCEPTED)
            ],
            trusted_recorder=RECORDER,
        )
        self.assertTrue(decision.is_blocked)
        self.assertIn(LEDGER_MERGE_STATUS_UNSOUND, decision.reason)


class R21ReviewFinding1Test(unittest.TestCase):
    """A recorded push is believed only when it recorded that it was ACCEPTED.

    The merge lesson, learned a second time. R21 recorded the push's outcome as two booleans
    and a sentence, so a `git` that could not be spawned and a lost race produced the same
    durable record — and the sentence told the operator to re-form the action either way
    (j#96516 finding 1). The typed status now has to be there, has to be `accepted`, and has
    to be absent from every step that cannot push.
    """

    def _pushed(self, record, status: str) -> list:
        return [
            StepOutcome(
                record.action_key,
                STEP_PUSH,
                OUTCOME_DONE,
                head=SOURCE,
                recorded_by=RECORDER,
                push_status=status,
            )
        ]

    def test_only_an_accepted_push_is_believed(self) -> None:
        record = _record()
        for status in (
            PUSH_REMOTE_MOVED,
            "remote_refused",
            "operational_error",
            "invalid_input",
            "unrecognized_status",
            "",
        ):
            decision = decide_integration(
                AUTO, record, _clean(), ledger=self._pushed(record, status),
                trusted_recorder=RECORDER,
            )
            self.assertTrue(decision.is_blocked, status)
            self.assertIn(BLOCKED_LEDGER_UNTRUSTWORTHY, decision.blocked_reasons, status)

    def test_an_accepted_push_reaches_the_ci_gate(self) -> None:
        record = _record()
        decision = decide_integration(
            AUTO, record, _clean(), ledger=self._pushed(record, PUSH_ACCEPTED),
            trusted_recorder=RECORDER,
        )
        self.assertFalse(decision.is_blocked, decision.reason)

    def test_a_step_that_cannot_push_may_not_carry_a_push_status(self) -> None:
        record = _record()
        ledger = [
            StepOutcome(
                record.action_key,
                STEP_INTEGRATION_APPLY,
                OUTCOME_DONE,
                head=OTHER,
                recorded_by=RECORDER,
                merge_status=MERGE_MERGED,
                git_version=GIT_VERSION,
                push_status=PUSH_ACCEPTED,
            )
        ]
        decision = decide_integration(
            AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch="main", ff_only=False),
            record,
            _clean(fast_forward_possible=False),
            ledger=ledger,
            trusted_recorder=RECORDER,
        )
        self.assertTrue(decision.is_blocked)
        self.assertIn(BLOCKED_LEDGER_UNTRUSTWORTHY, decision.blocked_reasons)

    def test_the_status_survives_the_durable_round_trip_and_unknowns_fail_closed(self) -> None:
        entry = StepOutcome(
            "k", STEP_PUSH, OUTCOME_DONE, head=SOURCE, push_status=PUSH_ACCEPTED
        )
        self.assertEqual(entry.as_payload()["push_status"], PUSH_ACCEPTED)
        self.assertEqual(
            StepOutcome.from_payload(entry.as_payload()).push_status, PUSH_ACCEPTED
        )
        # A record cannot introduce a new outcome by writing one.
        payload = dict(entry.as_payload(), push_status="landed_probably")
        self.assertEqual(StepOutcome.from_payload(payload).push_status, "unrecognized_status")


class IdempotencyTest(unittest.TestCase):
    def test_a_done_step_is_not_re_run(self) -> None:
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)]
        decision = decide_integration(AUTO, record, _clean(), ledger=ledger)
        self.assertEqual(decision.state, STATE_INTEGRATED)
        self.assertIsNone(decision.next_step)

    def test_a_ledger_under_a_drifted_key_satisfies_nothing(self) -> None:
        # The lane advanced, so the action is a different action. The old push does not
        # count, and the new one is decided from scratch.
        stale = _record(source_head=OTHER)
        ledger = [StepOutcome(stale.action_key, STEP_PUSH, OUTCOME_DONE, push_status=PUSH_ACCEPTED)]
        decision = decide_integration(AUTO, _record(), _clean(), ledger=ledger)
        self.assertEqual(decision.state, STATE_PUSH_WAITING)
        self.assertEqual(decision.next_step, STEP_PUSH)

    def test_a_blocked_step_outcome_does_not_count_as_progress(self) -> None:
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_BLOCKED, head=SOURCE, recorded_by=RECORDER)]
        decision = decide_integration(AUTO, record, _clean(), ledger=ledger)
        self.assertEqual(decision.next_step, STEP_PUSH)

    def test_a_resumed_merge_run_does_not_re_apply_the_merge(self) -> None:
        record = _record()
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        world = _clean(
            fast_forward_possible=False
        )
        ledger = [
            StepOutcome(
                record.action_key, STEP_INTEGRATION_APPLY, OUTCOME_DONE,
                head=OTHER, recorded_by=RECORDER, merge_status=MERGE_MERGED,
                git_version=GIT_VERSION)
        ]
        decision = decide_integration(policy, record, world, ledger=ledger)
        self.assertEqual(decision.next_step, STEP_PUSH)



class R1ReviewFindingRegressionTest(unittest.TestCase):
    """Each of R1 review j#96344's findings, pinned shut with the input that reproduced it."""

    def test_f2_a_green_run_on_another_commit_is_not_this_action_s_ci(self) -> None:
        # The R1 gate was `integration_ci_green: bool`, so ANY green run satisfied it. The
        # evidence now names the commit it is about and is matched against the head the push
        # landed — the central preset's `### Hibernate Evidence Marker Contract` rule that an
        # unrelated green run must not satisfy a required-CI claim.
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)]
        elsewhere = IntegrationCiEvidence(
            integration_head=OTHER, workflow="required-ci", run="run-9", conclusion="success"
        )
        decision = decide_integration(
            AUTO, record, _clean(integration_ci=elsewhere), ledger=ledger
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_INTEGRATION_CI_HEAD_MISMATCH,))
        self.assertIn(OTHER, decision.reason)
        self.assertIn(SOURCE, decision.reason)

    def test_f2_ci_evidence_that_cannot_be_checked_is_its_own_reason(self) -> None:
        # "We cannot tell what this run was" is not "the run failed": the operator's next
        # action differs, so the tokens differ.
        record = _record()
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)]
        for overrides in (
            {"workflow": ""},
            {"run": ""},
            {"integration_head": "not-a-sha"},
            {"conclusion": ""},
        ):
            fields = {
                "integration_head": SOURCE,
                "workflow": "required-ci",
                "run": "run-1",
                "conclusion": "success",
            }
            fields.update(overrides)
            decision = decide_integration(
                AUTO,
                record,
                _clean(integration_ci=IntegrationCiEvidence(**fields)),  # type: ignore[arg-type]
                ledger=ledger,
            )
            self.assertEqual(
                decision.blocked_reasons,
                (BLOCKED_INTEGRATION_CI_EVIDENCE_INCOMPLETE,),
                overrides,
            )

    def test_f2_a_merge_run_is_matched_against_the_merge_commit_not_the_source(self) -> None:
        # For a merge disposition the pushed head is the merge commit, so CI about the SOURCE
        # head is about a commit that was never integrated.
        record = _record()
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        merge_head = "f" * 40
        ledger = [
            StepOutcome(
                record.action_key, STEP_INTEGRATION_APPLY, OUTCOME_DONE,
                head=merge_head, recorded_by=RECORDER, merge_status=MERGE_MERGED,
                git_version=GIT_VERSION),
            StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=merge_head, recorded_by=RECORDER, push_status=PUSH_ACCEPTED),
        ]
        about_source = IntegrationCiEvidence(
            integration_head=SOURCE, workflow="required-ci", run="r", conclusion="success"
        )
        blocked = decide_integration(
            policy,
            record,
            _clean(
                fast_forward_possible=False,
                integration_ci=about_source,
            ),
            ledger=ledger,
        )
        self.assertEqual(blocked.blocked_reasons, (BLOCKED_INTEGRATION_CI_HEAD_MISMATCH,))

        about_merge = IntegrationCiEvidence(
            integration_head=merge_head, workflow="required-ci", run="r", conclusion="success"
        )
        ok = decide_integration(
            policy,
            record,
            _clean(
                fast_forward_possible=False,
                integration_ci=about_merge,
            ),
            ledger=ledger,
        )
        self.assertEqual(ok.state, STATE_INTEGRATED)
        self.assertEqual(ok.integration_ci, CI_GATE_GREEN)

    def test_f3_no_merge_is_ever_performed_in_a_checkout(self) -> None:
        """j#77124's rule outlived the mechanism that was supposed to enforce it.

        R1 asserted "the lane never checks out the target branch" in a docstring and enforced
        nothing (j#96344 finding 3); R2 made a dedicated checkout's identity a measured, gated
        fact. Review j#96406 finding 1 then reproduced a foreign lane's checkout being swapped
        onto that path between the gate and the merge — so the gate is gone along with the
        checkout it guarded, and what is pinned instead is that the decision carries no
        checkout at all and its apply step names none.
        """
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        decision = decide_integration(
            policy, _record(), _clean(fast_forward_possible=False)
        )
        self.assertEqual(decision.next_step, STEP_INTEGRATION_APPLY)
        self.assertEqual(decision.blocked_reasons, ())
        # No preflight field and no blocked reason may reintroduce one, and the decision says
        # what it now does rather than naming a place it does it in.
        self.assertFalse(hasattr(_clean(), "integration_worktree"))
        self.assertIn("as objects", decision.reason)
        self.assertNotIn("dedicated", decision.reason.lower())

    def test_f3_a_fast_forward_still_applies_nothing(self) -> None:
        # A fast-forward creates no commit, so there is nothing to build and the apply step
        # is skipped entirely.
        decision = decide_integration(AUTO, _record(), _clean())
        self.assertEqual(decision.next_step, STEP_PUSH)

    def test_f4_the_configured_branch_constrains_the_action_s_target(self) -> None:
        # A config field no decision reads is not a constraint. `target_identity_known` asks
        # whether the ref is a known integration branch at all; this asks whether it is the
        # one the operator pointed THIS actuator at.
        decision = decide_integration(
            AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch="release"),
            _record(target_ref="main"),
            _clean(),
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_TARGET_NOT_CONFIGURED,))
        self.assertIsNone(decision.next_step)

    def test_f4_ref_spellings_are_normalized_before_comparison(self) -> None:
        # `refs/heads/main` and `main` are the same target; comparing raw strings would
        # defeat the check that uses them.
        decision = decide_integration(
            AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch="refs/heads/main"),
            _record(target_ref="main"),
            _clean(),
        )
        self.assertEqual(decision.state, STATE_PUSH_WAITING)

    def test_f4_an_unconfigured_branch_imposes_no_constraint(self) -> None:
        # `None` is documented as runtime resolution, so it constrains nothing.
        decision = decide_integration(
            AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch=None),
            _record(target_ref="anything"),
            _clean(),
        )
        self.assertEqual(decision.state, STATE_PUSH_WAITING)

    def test_f4_the_builder_cannot_produce_the_mismatch(self) -> None:
        policy = AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch="refs/heads/main")
        record = build_integration_action_record(
            configured_branch=policy.integration_branch or "",
            issue="13686",
            lane_generation=3,
            source_head=SOURCE,
            expected_target_head=TARGET,
            review_generation="j#96337",
        )
        self.assertEqual(record.target_ref, "main")
        self.assertEqual(
            decide_integration(policy, record, _clean()).state, STATE_PUSH_WAITING
        )


class R3ReviewFindingRegressionTest(unittest.TestCase):
    """R3 review j#96368's findings, pinned with the inputs that reproduced them."""

    def test_f3_a_push_recorded_before_any_apply_is_not_believed(self) -> None:
        # R3: `completed_steps` mapped entries without looking at ORDER, so a ledger claiming
        # a push that never happened let the run apply a merge and then report `integrated`
        # having pushed nothing.
        record = _record()
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        decision = decide_integration(
            policy,
            record,
            _clean(
                fast_forward_possible=False
            ),
            ledger=[
                StepOutcome(
                    record.action_key, STEP_PUSH, OUTCOME_DONE, head=OTHER,
                    recorded_by=RECORDER, push_status=PUSH_ACCEPTED)
            ],
            trusted_recorder=RECORDER,
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_LEDGER_UNTRUSTWORTHY,))
        self.assertIsNone(decision.next_step)

    def test_f3_a_ledger_entry_this_actuator_did_not_write_does_not_count(self) -> None:
        # Provenance: an actuator counts only what it recorded. A caller-authored entry is
        # not evidence that a step ran.
        record = _record()
        decision = decide_integration(
            AUTO,
            record,
            _clean(),
            ledger=[
                StepOutcome(
                    record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE,
                    recorded_by="somebody-else", push_status=PUSH_ACCEPTED)
            ],
            trusted_recorder=RECORDER,
        )
        self.assertEqual(decision.next_step, STEP_PUSH)

    def test_f3_an_in_order_own_provenance_ledger_is_believed(self) -> None:
        record = _record()
        decision = decide_integration(
            AUTO,
            record,
            _clean(),
            ledger=[
                StepOutcome(
                    record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE,
                    recorded_by=RECORDER, push_status=PUSH_ACCEPTED)
            ],
            trusted_recorder=RECORDER,
        )
        self.assertEqual(decision.state, STATE_INTEGRATED)

    def test_f4_the_coordinator_confirmed_mode_is_not_offered(self) -> None:
        # R3 review j#96368 finding 4: the mode had no live resolver binding, so it was not
        # live-executable. Withdrawn until one exists (the reviewer's second option).
        self.assertEqual(INTEGRATION_MODES, {MODE_AUTO, MODE_DISABLED})
        decision = decide_integration(
            AutoIntegrationPolicy(mode="coordinator_confirmed", integration_branch="main"),
            _record(),
            _clean(),
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_MODE_UNRECOGNIZED,))


class R5ReviewFinding2Test(unittest.TestCase):
    """The target gate asks a different question before and after this action has pushed."""

    def test_pre_push_the_target_must_still_be_where_the_action_expected_it(self) -> None:
        decision = decide_integration(AUTO, _record(), _clean(observed_target_head=OTHER))
        self.assertEqual(decision.blocked_reasons, (BLOCKED_TARGET_DRIFT,))

    def test_post_push_the_expected_head_is_no_longer_the_question(self) -> None:
        # R5 review j#96385 finding 2: our own push moves the target off the pre-push
        # expectation by construction, so comparing them made every successful integration
        # look like drift and no run could ever complete.
        record = _record()
        ledger = [
            StepOutcome(
                record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE,
                recorded_by=RECORDER, push_status=PUSH_ACCEPTED)
        ]
        decision = decide_integration(
            AUTO,
            record,
            # The target now holds what we landed — which is NOT the expected head.
            _clean(observed_target_head=SOURCE, landed_head_on_target=True),
            ledger=ledger,
            trusted_recorder=RECORDER,
        )
        self.assertEqual(decision.state, STATE_INTEGRATED)

    def test_post_push_work_rewritten_off_the_target_is_its_own_reason(self) -> None:
        record = _record()
        ledger = [
            StepOutcome(
                record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE,
                recorded_by=RECORDER, push_status=PUSH_ACCEPTED)
        ]
        decision = decide_integration(
            AUTO,
            record,
            _clean(observed_target_head=OTHER, landed_head_on_target=False),
            ledger=ledger,
            trusted_recorder=RECORDER,
        )
        self.assertEqual(
            decision.blocked_reasons, (BLOCKED_INTEGRATION_LOST_FROM_TARGET,)
        )

    def test_already_integrated_is_only_a_no_op_before_we_push(self) -> None:
        # After our own push the source IS reachable from the target; terminating as
        # `already_integrated` there would skip the exact-SHA CI gate entirely.
        record = _record()
        ledger = [
            StepOutcome(
                record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE,
                recorded_by=RECORDER, push_status=PUSH_ACCEPTED)
        ]
        decision = decide_integration(
            AUTO,
            record,
            _clean(already_integrated=True, integration_ci=None),
            ledger=ledger,
            trusted_recorder=RECORDER,
        )
        self.assertEqual(decision.state, STATE_AWAITING_CI)


class R2ReviewFindingRegressionTest(unittest.TestCase):
    """R2 review j#96350's findings, pinned with the inputs that reproduced them."""

    def test_f1_the_ci_waiver_and_its_config_knobs_are_gone(self) -> None:
        # R2 argued from j#96335's configuration list that an optional gate was authorized;
        # j#77124, j#96335's own target flow, and j#96337's fail-closed list all say
        # otherwise, and the waiver had no downstream semantics (cleanup requires a settled
        # green CI). Dispute j#96346 withdrawn in j#96351.
        self.assertNotIn("waived", CI_GATE_STATES)
        record = _record()
        decision = decide_integration(
            AUTO,
            record,
            _clean(integration_ci=None),
            ledger=[StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)],
        )
        self.assertEqual(decision.state, STATE_AWAITING_CI)
        self.assertEqual(decision.integration_ci, CI_GATE_NOT_REACHED)

    def test_f1_the_source_ci_gate_is_typed_and_bound_to_the_source_head(self) -> None:
        # The sibling gate kept a bare bool through R2. Same hole, same fix.
        elsewhere = IntegrationCiEvidence(
            integration_head=OTHER, workflow="required-ci", run="r", conclusion="success"
        )
        self.assertEqual(
            decide_integration(AUTO, _record(), _clean(source_ci=elsewhere)).blocked_reasons,
            (BLOCKED_SOURCE_CI_HEAD_MISMATCH,),
        )
        incomplete = IntegrationCiEvidence(
            integration_head=SOURCE, workflow="", run="r", conclusion="success"
        )
        self.assertEqual(
            decide_integration(AUTO, _record(), _clean(source_ci=incomplete)).blocked_reasons,
            (BLOCKED_SOURCE_CI_EVIDENCE_INCOMPLETE,),
        )
        red = IntegrationCiEvidence(
            integration_head=SOURCE, workflow="required-ci", run="r", conclusion="failure"
        )
        self.assertEqual(
            decide_integration(AUTO, _record(), _clean(source_ci=red)).blocked_reasons,
            (BLOCKED_SOURCE_CI_NOT_GREEN,),
        )

    def test_f2_a_push_outcome_without_a_head_does_not_fall_back(self) -> None:
        # R2: `landed_head = done[STEP_PUSH].head or record.source_head` turned "we failed to
        # record what landed" into "the source landed", and then gated a MERGE integration on
        # CI about the source commit. Exact reproduction input.
        record = _record()
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        merge_head = "f" * 40
        about_source = IntegrationCiEvidence(
            integration_head=SOURCE, workflow="required-ci", run="r", conclusion="success"
        )
        decision = decide_integration(
            policy,
            record,
            _clean(
                fast_forward_possible=False,
                integration_ci=about_source,
            ),
            ledger=[
                StepOutcome(
                record.action_key, STEP_INTEGRATION_APPLY, OUTCOME_DONE,
                head=merge_head, recorded_by=RECORDER, merge_status=MERGE_MERGED,
                git_version=GIT_VERSION),
                StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head="", recorded_by=RECORDER, push_status=PUSH_ACCEPTED),
            ],
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_PUSH_OUTCOME_HEAD_MISSING,))

    def test_f2_a_malformed_push_head_is_refused_too(self) -> None:
        record = _record()
        decision = decide_integration(
            AUTO,
            record,
            _clean(),
            ledger=[
                StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head="not-a-sha", recorded_by=RECORDER, push_status=PUSH_ACCEPTED)
            ],
        )
        self.assertEqual(decision.blocked_reasons, (BLOCKED_PUSH_OUTCOME_HEAD_MISSING,))

    def test_f2_the_recorded_head_must_be_what_the_disposition_should_have_landed(
        self,
    ) -> None:
        record = _record()
        ff = decide_integration(
            AUTO,
            record,
            _clean(),
            ledger=[StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=OTHER, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)],
        )
        self.assertEqual(ff.blocked_reasons, (BLOCKED_PUSH_HEAD_MISMATCH,))

        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        merge_head = "f" * 40
        merged = decide_integration(
            policy,
            record,
            _clean(
                fast_forward_possible=False
            ),
            ledger=[
                StepOutcome(
                record.action_key, STEP_INTEGRATION_APPLY, OUTCOME_DONE,
                head=merge_head, recorded_by=RECORDER, merge_status=MERGE_MERGED,
                git_version=GIT_VERSION),
                StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED),
            ],
        )
        self.assertEqual(merged.blocked_reasons, (BLOCKED_PUSH_HEAD_MISMATCH,))

    def test_f2_a_correctly_recorded_merge_push_reaches_integrated(self) -> None:
        record = _record()
        policy = AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )
        merge_head = "f" * 40
        decision = decide_integration(
            policy,
            record,
            _clean(
                fast_forward_possible=False,
                integration_ci=IntegrationCiEvidence(
                    integration_head=merge_head,
                    workflow="required-ci",
                    run="r",
                    conclusion="success",
                ),
            ),
            ledger=[
                StepOutcome(
                record.action_key, STEP_INTEGRATION_APPLY, OUTCOME_DONE,
                head=merge_head, recorded_by=RECORDER, merge_status=MERGE_MERGED,
                git_version=GIT_VERSION),
                StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=merge_head, recorded_by=RECORDER, push_status=PUSH_ACCEPTED),
            ],
        )
        self.assertEqual(decision.state, STATE_INTEGRATED)
        self.assertIn(merge_head, decision.reason)


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
        ledger = [StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)]
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
        for preflight in (_clean(), _clean(integration_ci=None)):
            decision = decide_integration(
                AUTO,
                record,
                preflight,
                ledger=[
                    StepOutcome(record.action_key, STEP_PUSH, OUTCOME_DONE, head=SOURCE, recorded_by=RECORDER, push_status=PUSH_ACCEPTED)
                ]
                if preflight.integration_ci is None
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
