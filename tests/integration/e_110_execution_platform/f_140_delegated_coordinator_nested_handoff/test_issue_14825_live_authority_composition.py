"""The live composition of the #13686 actuator (Redmine #14825, items 3 / 4 / 5 / 7).

Everything here runs against the REAL durable stores — a real SQLite ledger file and a real
:class:`~mozyo_bridge.core.state.lane_lifecycle.LaneLifecycleStore` — because the four properties
under test are properties of storage, and a fake store would be asserting that the fake behaves.
j#96408's fifth condition says so in as many words: the zero-release cases are to be pinned
against a real lifecycle store, not a fake.

- **item 7** — the managed-process release resolves its target from ``issue`` + ``lane_generation``
  alone, and a stale generation / a foreign lane / an ambiguous or absent row releases nothing;
- **item 4** — the ledger owns its writer identity, ignores the payload's claim about it, survives
  a process boundary, refuses a duplicate ``done``, and can hold "a mutation ran and we do not
  know how it ended";
- **item 5** — the cleanup's authorization comes from the durable lifecycle, so a record can no
  longer authorize itself;
- **item 3** — the asynchronous CI continuation re-enters the same action, and a duplicate trigger
  changes nothing.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (  # noqa: E501
    AutoIntegrationUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
    CONTINUATION_CI_UNSETTLED,
    CONTINUATION_INTEGRATED,
    CONTINUATION_NOT_AWAITING_CI,
    AsyncCiContinuation,
    ledger_authorizing_action_reader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    AutoIntegrationLedgerError,
    SqliteLedgerStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ports import (  # noqa: E501
    CleanupAuthority,
    IntegrationAuthority,
    MergeResult,
    PushResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_process_ops import (  # noqa: E501
    REFUSE_AMBIGUOUS,
    REFUSE_FOREIGN_LANE,
    REFUSE_INVENTORY_UNREADABLE,
    REFUSE_NO_ROW,
    LiveManagedProcessOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    AutoIntegrationPolicy,
    STEP_INTEGRATION_CI,
    STEP_PUSH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    PUSH_ACCEPTED,
    IntegrationActionRecord,
    IntegrationCiEvidence,
    LaneWorktree,
    StepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (  # noqa: E501
    BLOCKED_ACTION_KEY_MISMATCH,
    STATE_RETIRED,
    CleanupActionRecord,
)

ISSUE = "14825"
WS = "ws-1"
LANE = "lane-14825"
GEN = 1
SOURCE = "a" * 40
TARGET = "b" * 40
MERGED = "c" * 40
LANE_BRANCH = "issue_14825"
LANE_WORKTREE = "/tmp/lane-14825"
TARGET_REF = "main"


# ---------------------------------------------------------------------------
# item 7: the live managed-process release, against a real lifecycle store.
# ---------------------------------------------------------------------------


@dataclass
class FakeInventoryOps:
    """The herdr seam. Only the INVENTORY is faked; the lifecycle store is real.

    An empty-but-readable inventory is a real production state (the panes are already gone), and
    it is the one that lets these tests exercise the release path without a terminal.
    """

    rows: Sequence[Mapping[str, object]] = ()
    readable: bool = True
    closed: List[HerdrRetireClosePlan] = field(default_factory=list)

    def read_inventory(self) -> Tuple[Sequence[Mapping[str, object]], bool]:
        return (tuple(self.rows), self.readable)

    def live_rows(self) -> Sequence[Mapping[str, object]]:
        return tuple(self.rows)

    def execute_close(self, plan: HerdrRetireClosePlan) -> HerdrRetireCloseResult:
        self.closed.append(plan)
        return HerdrRetireCloseResult(
            workspace_id=plan.workspace_id, lane_id=plan.lane_id
        )


class LiveManagedProcessReleaseTest(unittest.TestCase):
    """j#96408's five conditions, against a real ``LaneLifecycleStore``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = LaneLifecycleStore(home=self.home)

    def _declare(
        self, *, workspace: str = WS, lane: str = LANE, issue: str = ISSUE
    ) -> LaneLifecycleKey:
        key = LaneLifecycleKey(workspace, lane)
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id="96589")
        self.store.declare_active(key, decision=decision, issue_id=issue)
        return key

    def _leave_active(self, key: LaneLifecycleKey) -> None:
        """A lane still holding its work is never a release target; move it out of ``active``."""
        record = self.store.get(key)
        self.store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=record.revision,
            target=DISPOSITION_HIBERNATED,
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id="96589"
            ),
        )

    def _ops(self, **over) -> LiveManagedProcessOperations:
        fields = dict(
            store=self.store,
            ops=over.pop("inventory", FakeInventoryOps()),
            lane_workspace=WS,
            lane_id=LANE,
        )
        fields.update(over)
        return LiveManagedProcessOperations(**fields)  # type: ignore[arg-type]

    def test_the_lanes_own_current_generation_releases(self) -> None:
        key = self._declare()
        self._leave_active(key)
        record = self.store.get(key)
        outcome = self._ops().describe_release(
            issue=ISSUE, lane_generation=record.lane_generation
        )
        self.assertTrue(outcome.released, outcome.detail)
        self.assertEqual(outcome.resolved_lane, LANE)

    def test_a_generation_that_is_not_the_rows_resolves_to_no_row_at_all(self) -> None:
        # The staleness mechanism, stated as the test that proves it: a lifecycle row carries the
        # lane's CURRENT generation, so a superseded generation matches nothing. There is no
        # separate staleness probe to get wrong.
        key = self._declare()
        self._leave_active(key)
        current = self.store.get(key).lane_generation
        outcome = self._ops().describe_release(
            issue=ISSUE, lane_generation=current + 1
        )
        self.assertFalse(outcome.released)
        self.assertEqual(outcome.refusal, REFUSE_NO_ROW)

    def test_an_issue_no_row_owns_releases_nothing(self) -> None:
        self._declare()
        outcome = self._ops().describe_release(issue="99999", lane_generation=GEN)
        self.assertFalse(outcome.released)
        self.assertEqual(outcome.refusal, REFUSE_NO_ROW)

    def test_two_rows_owning_one_issue_and_generation_release_nothing(self) -> None:
        # Two workspaces claiming the same issue. The match runs across every workspace BEFORE
        # the ownership check precisely so this is seen as the ambiguity it is: filtering to our
        # own lane first would leave one clean match and release it.
        self._declare()
        self._declare(workspace="ws-2", lane="lane-other")
        outcome = self._ops().describe_release(issue=ISSUE, lane_generation=GEN)
        self.assertFalse(outcome.released)
        self.assertEqual(outcome.refusal, REFUSE_AMBIGUOUS)

    def test_a_foreign_lanes_row_releases_nothing(self) -> None:
        key = self._declare(lane="somebody-elses-lane")
        self._leave_active(key)
        outcome = self._ops().describe_release(issue=ISSUE, lane_generation=GEN)
        self.assertFalse(outcome.released)
        self.assertEqual(outcome.refusal, REFUSE_FOREIGN_LANE)

    def test_an_unreadable_inventory_is_not_an_empty_one(self) -> None:
        key = self._declare()
        self._leave_active(key)
        outcome = self._ops(
            inventory=FakeInventoryOps(readable=False)
        ).describe_release(issue=ISSUE, lane_generation=GEN)
        self.assertFalse(outcome.released)
        self.assertEqual(outcome.refusal, REFUSE_INVENTORY_UNREADABLE)

    def test_a_lane_still_active_is_never_released(self) -> None:
        self._declare()
        outcome = self._ops().describe_release(issue=ISSUE, lane_generation=GEN)
        self.assertFalse(outcome.released)

    def test_a_non_positive_generation_resolves_nothing(self) -> None:
        self._declare()
        for generation in (0, -1, True):
            with self.subTest(generation=generation):
                self.assertFalse(
                    self._ops().release_process(
                        issue=ISSUE, lane_generation=generation  # type: ignore[arg-type]
                    )
                )


# ---------------------------------------------------------------------------
# item 4: the durable ledger.
# ---------------------------------------------------------------------------


class DurableLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SqliteLedgerStore(home=self.home)

    def test_the_payloads_claim_about_its_writer_is_ignored(self) -> None:
        self.ledger.append(
            StepOutcome(
                action_key="A",
                step=STEP_PUSH,
                outcome=OUTCOME_DONE,
                head=SOURCE,
                push_status="accepted",
                recorded_by="receipt:forged-by-a-caller",
            )
        )
        (entry,) = self.ledger.read(action_key="A")
        self.assertEqual(entry.recorded_by, self.ledger.writer_id)
        self.assertNotEqual(entry.recorded_by, "receipt:forged-by-a-caller")

    def test_a_second_store_on_the_same_file_is_the_same_writer(self) -> None:
        # What makes a resume across the asynchronous CI gate possible: the next process reads
        # the same identity back and therefore counts what the first one recorded.
        self.ledger.append(
            StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE)
        )
        resumed = SqliteLedgerStore(home=self.home)
        self.assertEqual(resumed.writer_id, self.ledger.writer_id)
        self.assertEqual(len(resumed.read(action_key="A")), 1)

    def test_a_different_ledger_file_is_a_different_writer(self) -> None:
        other = SqliteLedgerStore(home=Path(tempfile.mkdtemp()))
        self.assertNotEqual(other.writer_id, self.ledger.writer_id)

    def test_a_second_done_for_one_step_is_refused_by_the_store(self) -> None:
        done = StepOutcome(
            action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE
        )
        self.ledger.append(done)
        with self.assertRaises(AutoIntegrationLedgerError):
            self.ledger.append(done)

    def test_a_blocked_step_may_be_recorded_more_than_once(self) -> None:
        # Only `done` is once-per-action: a refusal is an observation, and re-running an action
        # that keeps failing must keep recording that it failed.
        for _ in range(2):
            self.ledger.append(
                StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_BLOCKED)
            )
        self.assertEqual(len(self.ledger.read(action_key="A")), 2)

    def test_an_intent_without_a_receipt_survives_as_an_unknown(self) -> None:
        self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        # The process dies here. A new one opens the same file.
        resumed = SqliteLedgerStore(home=self.home)
        (open_intent,) = resumed.unresolved_intents(action_key="A")
        self.assertEqual(open_intent.step, STEP_PUSH)
        self.assertEqual(resumed.read(action_key="A"), ())

    def test_the_receipt_closes_the_intent_in_one_transaction(self) -> None:
        self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        self.ledger.append(
            StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE)
        )
        self.assertEqual(self.ledger.unresolved_intents(action_key="A"), ())

    def test_entries_of_another_action_are_not_this_actions(self) -> None:
        self.ledger.append(
            StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE)
        )
        self.assertEqual(self.ledger.read(action_key="B"), ())

    def test_an_action_key_wildcard_cannot_widen_a_prefix_match(self) -> None:
        # `%` and `_` are legal in an action key (a target ref may carry either). If they reached
        # LIKE unescaped, one action's key would match another's rows.
        self.ledger.append(
            StepOutcome(
                action_key="issue=1|lane_generation=1|source_head=XY|rest",
                step=STEP_INTEGRATION_CI,
                outcome=OUTCOME_DONE,
                head=SOURCE,
            )
        )
        self.assertEqual(
            self.ledger.completed_action_keys(
                prefix="issue=1|lane_generation=1|source_head=_Y|",
                step=STEP_INTEGRATION_CI,
            ),
            (),
        )


# ---------------------------------------------------------------------------
# items 3 / 5: the actuator over the durable ledger.
# ---------------------------------------------------------------------------


@dataclass
class StubGitOperations:
    """Just enough git for a fast-forward integration; every probe is deliberate."""

    target_head: str = TARGET
    pushed: List[dict] = field(default_factory=list)

    def is_git_workspace(self) -> bool:
        return True

    def apply_merge(self, *, source_head, target_ref, expected_target_head) -> MergeResult:
        return MergeResult(status="merged", integration_head=MERGED, git_version="git 2.50.1")

    def push_non_force(self, *, source_head, target_ref) -> PushResult:
        self.pushed.append({"source_head": source_head, "target_ref": target_ref})
        self.target_head = source_head
        return PushResult(status="accepted")

    def describe_lane_worktree(self, *, path) -> LaneWorktree:
        return LaneWorktree(
            path=path, registered=True, clean=True, checked_out_branch=LANE_BRANCH
        )

    def resolve_head(self, ref: str) -> str:
        return self.target_head

    def remote_branch_tip(self, branch: str) -> str:
        return self.target_head

    def is_ancestor(self, *, ancestor, descendant) -> bool:
        if ancestor == TARGET and descendant == SOURCE:
            return True
        return ancestor == descendant

    def worktree_dirty(self, *, worktree_path: str = "") -> bool:
        return False

    def commit_on_remote(self, commit: str, *, branch: str) -> bool:
        return True


@dataclass
class StubAuthority:
    """The durable reader, with the CI verdict switchable so the async gate can settle."""

    integration_ci: Optional[IntegrationCiEvidence] = None
    cleanup: CleanupAuthority = field(default_factory=CleanupAuthority)

    def read_integration_authority(self, *, record) -> IntegrationAuthority:
        return IntegrationAuthority(
            review_generation_admissible=True,
            reviewed_head=SOURCE,
            target_identity_known=True,
            callbacks_drained=True,
            owner_gates_resolved=True,
            source_ci=IntegrationCiEvidence(
                integration_head=SOURCE,
                workflow="required-ci",
                run="src-1",
                conclusion="success",
            ),
        )

    def read_integration_ci(self, *, record, integration_head):
        return self.integration_ci

    def read_cleanup_authority(self, *, record) -> CleanupAuthority:
        return self.cleanup


def _record() -> IntegrationActionRecord:
    return IntegrationActionRecord(
        issue=ISSUE,
        lane_generation=GEN,
        source_head=SOURCE,
        target_ref=TARGET_REF,
        expected_target_head=TARGET,
        review_generation="r1",
    )


class AsyncCiContinuationTest(unittest.TestCase):
    """item 3: the run that pushes and the run that concludes are different processes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _use_case(
        self, authority: StubAuthority, *, target_head: str = TARGET
    ) -> AutoIntegrationUseCase:
        # ``target_head`` is where the REMOTE is when this process starts. A continuation runs
        # after the push landed, so its fresh read of the target finds the landed commit — the
        # first run's stub cannot be reused, because that would model a remote that forgot the
        # push this very test performed.
        return AutoIntegrationUseCase(
            operations=StubGitOperations(target_head=target_head),
            integration_policy=AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=True
            ),
            authority=authority,
            ledger=SqliteLedgerStore(home=self.home),
            lane_worktree=LANE_WORKTREE,
            lane_branch=LANE_BRANCH,
            lane_issue=ISSUE,
            lane_generation=GEN,
        )

    def test_a_push_lands_and_the_ci_gate_is_left_pending(self) -> None:
        report = self._use_case(StubAuthority()).run_integration(_record())
        steps = {outcome.step: outcome.outcome for outcome in report.outcomes}
        self.assertEqual(steps.get(STEP_PUSH), OUTCOME_DONE)
        self.assertEqual(steps.get(STEP_INTEGRATION_CI), "pending")

    def test_a_second_process_resumes_the_action_and_does_not_push_again(self) -> None:
        first = self._use_case(StubAuthority())
        first.run_integration(_record())

        # A NEW use case over a NEW store object on the same file — the shape of a real
        # continuation, where nothing survives in memory.
        settled = StubAuthority(
            integration_ci=IntegrationCiEvidence(
                integration_head=SOURCE,
                workflow="required-ci",
                run="int-1",
                conclusion="success",
            )
        )
        second = self._use_case(settled, target_head=SOURCE)
        outcome = AsyncCiContinuation(use_case=second).resume(_record())

        self.assertEqual(outcome.status, CONTINUATION_INTEGRATED)
        self.assertEqual(second.operations.pushed, [])

    def test_a_continuation_before_ci_settles_records_nothing(self) -> None:
        first = self._use_case(StubAuthority())
        first.run_integration(_record())
        outcome = AsyncCiContinuation(
            use_case=self._use_case(StubAuthority(), target_head=SOURCE)
        ).resume(_record())
        self.assertEqual(outcome.status, CONTINUATION_CI_UNSETTLED)

    def test_a_duplicate_trigger_after_settling_changes_nothing(self) -> None:
        settled = StubAuthority(
            integration_ci=IntegrationCiEvidence(
                integration_head=SOURCE,
                workflow="required-ci",
                run="int-1",
                conclusion="success",
            )
        )
        self._use_case(StubAuthority()).run_integration(_record())
        AsyncCiContinuation(use_case=self._use_case(settled, target_head=SOURCE)).resume(
            _record()
        )

        third = self._use_case(settled, target_head=SOURCE)
        again = AsyncCiContinuation(use_case=third).resume(_record())
        self.assertEqual(again.status, CONTINUATION_INTEGRATED)
        self.assertEqual(third.operations.pushed, [])
        ledger = SqliteLedgerStore(home=self.home)
        done = [
            entry
            for entry in ledger.read(action_key=_record().action_key)
            if entry.outcome == OUTCOME_DONE
        ]
        self.assertEqual(sorted({entry.step for entry in done}), sorted(
            {entry.step for entry in done}
        ))
        self.assertEqual(len(done), len({entry.step for entry in done}))

    def test_an_action_with_no_push_receipt_has_no_ci_to_continue(self) -> None:
        outcome = AsyncCiContinuation(use_case=self._use_case(StubAuthority())).resume(
            _record()
        )
        self.assertEqual(outcome.status, CONTINUATION_NOT_AWAITING_CI)

    def test_a_crash_between_a_mutation_and_its_receipt_stops_the_next_run(self) -> None:
        # The window the intent exists for. The ledger holds "push may have run"; a resume that
        # read only outcomes would see no push entry and offer the push a second time.
        ledger = SqliteLedgerStore(home=self.home)
        ledger.begin_step(action_key=_record().action_key, step=STEP_PUSH)

        use_case = self._use_case(StubAuthority())
        report = use_case.run_integration(_record())
        self.assertEqual(use_case.operations.pushed, [])
        (blocked,) = report.outcomes
        self.assertEqual(blocked.outcome, OUTCOME_BLOCKED)
        self.assertEqual(blocked.step, STEP_PUSH)
        # And nothing was written: appending would have resolved an intent this run knows
        # nothing about.
        self.assertEqual(ledger.unresolved_intents(action_key=_record().action_key)[0].step, STEP_PUSH)


class CleanupAuthorizationTest(unittest.TestCase):
    """item 5: a cleanup record can no longer authorize itself."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SqliteLedgerStore(home=self.home)

    def _cleanup_record(self, key: str) -> CleanupActionRecord:
        return CleanupActionRecord(
            issue=ISSUE,
            lane_generation=GEN,
            branch=LANE_BRANCH,
            worktree_path=LANE_WORKTREE,
            recorded_source_head=SOURCE,
            integration_action_key=key,
        )

    def _use_case(self, authority: StubAuthority) -> AutoIntegrationUseCase:
        return AutoIntegrationUseCase(
            operations=StubGitOperations(),
            integration_policy=AutoIntegrationPolicy(mode="auto"),
            processes=_AlwaysReleases(),
            authority=authority,
            ledger=self.ledger,
            lane_worktree=LANE_WORKTREE,
            lane_branch=LANE_BRANCH,
            lane_issue=ISSUE,
            lane_generation=GEN,
        )

    def _satisfied(self, authorizing_key: str) -> StubAuthority:
        return StubAuthority(
            cleanup=CleanupAuthority(
                issue_closed=True,
                integration_confirmed=True,
                integration_ci_settled_green=True,
                callbacks_drained=True,
                owner_gates_resolved=True,
                authorizing_action_key=authorizing_key,
            )
        )

    def test_a_record_naming_an_action_the_authority_does_not_is_refused(self) -> None:
        report = self._use_case(self._satisfied("the-action-that-ran")).run_cleanup(
            self._cleanup_record("an-action-the-record-made-up")
        )
        self.assertEqual(
            report.final_decision.primary_reason, BLOCKED_ACTION_KEY_MISMATCH
        )

    def test_an_authority_that_names_no_action_authorizes_nothing(self) -> None:
        report = self._use_case(self._satisfied("")).run_cleanup(
            self._cleanup_record("whatever-the-record-says")
        )
        self.assertEqual(
            report.final_decision.primary_reason, BLOCKED_ACTION_KEY_MISMATCH
        )

    def test_the_two_agreeing_lets_the_one_step_run(self) -> None:
        report = self._use_case(self._satisfied("the-action-that-ran")).run_cleanup(
            self._cleanup_record("the-action-that-ran")
        )
        self.assertEqual(report.final_decision.state, STATE_RETIRED)

    def test_the_ledger_reader_names_the_action_whose_push_landed(self) -> None:
        record = _record()
        self.ledger.append(_landed_push(record.action_key))
        read = ledger_authorizing_action_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored")), record.action_key)

    def test_a_push_that_did_not_report_an_accepted_landing_authorizes_nothing(self) -> None:
        # A `done` push carrying no accepted status. The actuator does not write one, and this
        # reader is reading a FILE — where an invariant of the writer is not an invariant of the
        # bytes.
        record = _record()
        self.ledger.append(
            StepOutcome(
                action_key=record.action_key,
                step=STEP_PUSH,
                outcome=OUTCOME_DONE,
                head=SOURCE,
            )
        )
        read = ledger_authorizing_action_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored")), "")

    def test_an_action_that_never_pushed_authorizes_nothing(self) -> None:
        read = ledger_authorizing_action_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored")), "")

    def test_two_completed_integrations_for_one_head_are_ambiguous(self) -> None:
        base = _record()
        other = IntegrationActionRecord(
            issue=ISSUE,
            lane_generation=GEN,
            source_head=SOURCE,
            target_ref="release",
            expected_target_head=TARGET,
            review_generation="r1",
        )
        for key in (base.action_key, other.action_key):
            self.ledger.append(_landed_push(key))
        read = ledger_authorizing_action_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored")), "")


def _landed_push(action_key: str) -> StepOutcome:
    """The ledger fact that an action published its commit — an accepted, `done` push."""
    return StepOutcome(
        action_key=action_key,
        step=STEP_PUSH,
        outcome=OUTCOME_DONE,
        head=SOURCE,
        push_status=PUSH_ACCEPTED,
    )


@dataclass
class _AlwaysReleases:
    def release_process(self, *, issue: str, lane_generation: int) -> bool:
        return True


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
