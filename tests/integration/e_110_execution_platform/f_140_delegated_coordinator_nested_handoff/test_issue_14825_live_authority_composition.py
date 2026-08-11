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

import json
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, Tuple
from unittest import mock

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    RELEASE_NOT_REQUESTED,
    RELEASE_RELEASED,
)
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_DELIVERED,
    CALLBACK_INFLIGHT,
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
)
from mozyo_bridge.core.state.lane_release_observation import build_release_observation
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (  # noqa: E501
    AutoIntegrationUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
    CI_STATE_FAILURE,
    CI_STATE_PENDING,
    CI_STATE_SUCCESS,
    CI_STATE_UNAVAILABLE,
    CiVerdict,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
    CiSettlementTrigger,
    CONTINUATION_CI_UNSETTLED,
    CONTINUATION_CI_FAILED,
    CONTINUATION_INTEGRATED,
    CONTINUATION_NOT_AWAITING_CI,
    AsyncCiContinuation,
    ledger_authorizing_action_reader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    ACTION_AWAITING_CI,
    ACTION_INTEGRATED,
    ACTION_REGISTERED,
    AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION,
    AutoIntegrationAdmissionError,
    AutoIntegrationLedgerError,
    AutoIntegrationLedgerReader,
    DurableIntegrationAction,
    SqliteLedgerStore,
    _open_ledger_writer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_ops import (  # noqa: E501
    REACHABILITY_NOT_REACHABLE,
    REACHABILITY_REACHABLE,
    REACHABILITY_UNAVAILABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_authority import (  # noqa: E501
    LaneCallbackScope,
    live_cleanup_callback_scope,
    live_lane_callback_scope,
    unresolved_lane_callback_debt,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_reconcile import (  # noqa: E501
    RECONCILED_AMBIGUOUS,
    RECONCILED_LANDED,
    RECONCILED_NOTHING_STRANDED,
    RECONCILED_NOT_LANDED,
    StrandedActionReconciler,
    StrandedCleanupReconciler,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ports import (  # noqa: E501
    CleanupAuthority,
    IntegrationAuthority,
    MergeResult,
    PushResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_process_ops import (  # noqa: E501
    RELEASE_OBSERVATION_NOT_RELEASED,
    RELEASE_OBSERVATION_RELEASED,
    RELEASE_OBSERVATION_UNAVAILABLE,
    REFUSE_AMBIGUOUS,
    REFUSE_FOREIGN_LANE,
    REFUSE_INVENTORY_UNREADABLE,
    REFUSE_NO_ROW,
    REFUSE_TRANSITION,
    REFUSE_TRANSITION_AUTHORITY,
    LiveManagedProcessOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    AutoIntegrationPolicy,
    STEP_INTEGRATION_APPLY,
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
    STEP_PROCESS_RETIRE,
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


def _durable_ledger_pair(home: Path) -> Tuple[SqliteLedgerStore, SqliteLedgerStore]:
    """Return the public reader and the private production mutation capability."""
    writer = _open_ledger_writer(home=home)
    return SqliteLedgerStore(home=home), writer


def _durable_action(**overrides) -> DurableIntegrationAction:
    record = IntegrationActionRecord(
        issue=ISSUE,
        lane_generation=GEN,
        source_head=SOURCE,
        target_ref=TARGET_REF,
        expected_target_head=TARGET,
        review_generation="r1",
    )
    values = dict(
        action_key=record.action_key,
        issue=ISSUE,
        workspace=WS,
        lane=LANE,
        lane_generation=GEN,
        branch=LANE_BRANCH,
        worktree=LANE_WORKTREE,
        repo_root="/tmp/repo",
        source_head=SOURCE,
        target_ref=TARGET_REF,
        expected_target_head=TARGET,
        review_generation="r1",
    )
    values.update(overrides)
    return DurableIntegrationAction(**values)


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


class LaneCallbackDebtTest(unittest.TestCase):
    """The integration gate owns one issue/lane generation, not workspace history."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(home=self.home)
        self._journal = 100
        self.scope = LaneCallbackScope(
            workspace_id=WS,
            issue=ISSUE,
            lane=LANE,
            lane_generation=GEN,
            lane_revision=5,
        )

    def _enqueue(
        self,
        *,
        state: str,
        route: str = "coordinator",
        workspace: str = WS,
        issue: str = ISSUE,
        enqueue_generation: str = "",
        target_generation: str = "",
        target_lane: str = "",
    ) -> None:
        self._journal += 1
        self.outbox.enqueue(
            CallbackOutboxKey(
                source="redmine",
                issue=issue,
                journal=str(self._journal),
                normalized_gate="review_request",
                callback_route=route,
                workspace_id=workspace,
            ),
            initial_state=state,
            enqueue_lane_generation=enqueue_generation,
            target_generation=target_generation,
            target_lane=target_lane,
        )

    def _debt(self, outbox=None):
        return unresolved_lane_callback_debt(
            outbox or self.outbox,
            scope=self.scope,
        )

    def test_each_unresolved_state_in_the_current_generation_blocks(self) -> None:
        self._enqueue(state=CALLBACK_PENDING, enqueue_generation=str(GEN))
        self._enqueue(
            state=CALLBACK_INFLIGHT,
            route=f"lane_gateway:{LANE}",
            target_generation=str(self.scope.lane_revision),
            target_lane=LANE,
        )
        self._enqueue(
            state=CALLBACK_UNCERTAIN,
            route=f"review_return:{LANE}",
            target_generation=str(self.scope.lane_revision),
            target_lane=LANE,
        )
        self._enqueue(state=CALLBACK_DEAD_LETTER, enqueue_generation=str(GEN))
        self.assertEqual(self._debt(), 4)

    def test_foreign_issue_workspace_and_previous_generation_do_not_block(self) -> None:
        self._enqueue(
            state=CALLBACK_UNCERTAIN,
            issue="99999",
            enqueue_generation=str(GEN),
        )
        self._enqueue(
            state=CALLBACK_DEAD_LETTER,
            workspace="some-other-workspace",
            enqueue_generation=str(GEN),
        )
        self._enqueue(
            state=CALLBACK_UNCERTAIN,
            enqueue_generation=str(GEN + 1),
        )
        self._enqueue(
            state=CALLBACK_UNCERTAIN,
            route=f"review_return:{LANE}",
            target_generation=str(self.scope.lane_revision + 1),
            target_lane=LANE,
        )
        self.assertEqual(self._debt(), 0)

    def test_a_proven_different_target_lane_is_foreign(self) -> None:
        self._enqueue(
            state=CALLBACK_UNCERTAIN,
            route="review_return:old-lane",
            target_generation=str(self.scope.lane_revision),
            target_lane="old-lane",
        )
        self.assertEqual(self._debt(), 0)

    def test_a_delivered_current_row_is_drained(self) -> None:
        self._enqueue(state=CALLBACK_DELIVERED, enqueue_generation=str(GEN))
        self.assertEqual(self._debt(), 0)

    def test_same_issue_ambiguous_generation_or_route_stays_debt(self) -> None:
        self._enqueue(state=CALLBACK_PENDING)
        self._enqueue(state=CALLBACK_PENDING, enqueue_generation="01")
        self._enqueue(
            state=CALLBACK_PENDING,
            route=f"lane_gateway:{LANE}",
            target_lane=LANE,
        )
        self._enqueue(
            state=CALLBACK_PENDING,
            route=f"review_return:{LANE}",
            target_generation=str(self.scope.lane_revision),
            enqueue_generation=str(GEN),
            target_lane=LANE,
        )
        self._enqueue(
            state=CALLBACK_PENDING,
            route="unknown-route",
            target_generation=str(GEN),
        )
        self.assertEqual(self._debt(), 5)

    def test_stored_authority_is_byte_exact_and_legacy_workspace_blocks(self) -> None:
        self._enqueue(
            state=CALLBACK_PENDING,
            enqueue_generation=f" {GEN} ",
        )
        self._enqueue(
            state=CALLBACK_PENDING,
            route=" coordinator ",
            enqueue_generation=str(GEN),
        )
        self._enqueue(
            state=CALLBACK_PENDING,
            route="lane_gateway:",
            target_generation=str(self.scope.lane_revision),
        )
        self._enqueue(
            state=CALLBACK_PENDING,
            workspace="",
            enqueue_generation=str(GEN),
        )
        self._enqueue(
            state=CALLBACK_PENDING,
            workspace=f" {WS} ",
            issue=f" {ISSUE} ",
            enqueue_generation=str(GEN),
        )
        self.assertEqual(self._debt(), 5)

    def test_an_unreadable_outbox_is_not_drained(self) -> None:
        class Unreadable:
            def read(self, *, states):
                raise OSError("unreadable")

        self.assertIsNone(self._debt(Unreadable()))

    def test_an_outbox_without_a_read_result_is_not_drained(self) -> None:
        class NoResult:
            def read(self, *, states):
                return None

        self.assertIsNone(self._debt(NoResult()))

    def test_live_scope_reads_incarnation_and_revision_as_distinct_axes(self) -> None:
        class Owner:
            resolved = True
            lane_id = LANE

        class Record:
            repo_workspace_id = WS
            lane_id = LANE
            issue_id = ISSUE
            binding_kind = "issue"
            lane_disposition = "active"
            lane_generation = GEN
            revision = 9

        class Store:
            def resolve_owner(self, workspace, issue):
                return Owner()

            def get(self, key):
                return Record()

            def records(self):
                return (Record(),)

        scope = live_lane_callback_scope(
            Store(),
            workspace_id=WS,
            issue=ISSUE,
            lane=LANE,
            lane_generation=GEN,
        )
        self.assertEqual(scope, LaneCallbackScope(WS, ISSUE, LANE, GEN, 9))

    def test_live_scope_rechecks_the_fetched_row_is_still_active(self) -> None:
        class Owner:
            resolved = True
            lane_id = LANE

        class Record:
            repo_workspace_id = WS
            lane_id = LANE
            issue_id = ISSUE
            binding_kind = "issue"
            lane_disposition = "hibernated"
            lane_generation = GEN
            revision = 9

        class Store:
            def resolve_owner(self, workspace, issue):
                return Owner()

            def get(self, key):
                return Record()

            def records(self):
                return (Record(),)

        self.assertIsNone(
            live_lane_callback_scope(
                Store(),
                workspace_id=WS,
                issue=ISSUE,
                lane=LANE,
                lane_generation=GEN,
            )
        )

        cleanup_scope = live_cleanup_callback_scope(
            Store(),
            workspace_id=WS,
            issue=ISSUE,
            lane=LANE,
            lane_generation=GEN,
        )
        self.assertEqual(cleanup_scope, LaneCallbackScope(WS, ISSUE, LANE, GEN, 9))

    def test_cleanup_scope_refuses_stale_or_non_cleanup_dispositions(self) -> None:
        class Owner:
            resolved = True
            lane_id = LANE

        class Store:
            disposition = DISPOSITION_HIBERNATED
            generation = GEN

            def resolve_owner(self, workspace, issue):
                return Owner()

            def get(self, key):
                return type(
                    "Record",
                    (),
                    {
                        "repo_workspace_id": WS,
                        "lane_id": LANE,
                        "issue_id": ISSUE,
                        "binding_kind": "issue",
                        "lane_disposition": self.disposition,
                        "lane_generation": self.generation,
                        "revision": 9,
                    },
                )()

            def records(self):
                return (self.get(None),)

        store = Store()
        store.generation = GEN + 1
        self.assertIsNone(
            live_cleanup_callback_scope(
                store,
                workspace_id=WS,
                issue=ISSUE,
                lane=LANE,
                lane_generation=GEN,
            )
        )
        store.generation = GEN
        for disposition in (" hibernated", "retired", "future"):
            with self.subTest(disposition=disposition):
                store.disposition = disposition
                self.assertIsNone(
                    live_cleanup_callback_scope(
                        store,
                        workspace_id=WS,
                        issue=ISSUE,
                        lane=LANE,
                        lane_generation=GEN,
                    )
                )

    def test_cleanup_scope_requires_one_readable_exact_owner(self) -> None:
        def record(*, workspace=WS, lane=LANE, revision=9):
            return type(
                "Record",
                (),
                {
                    "repo_workspace_id": workspace,
                    "lane_id": lane,
                    "issue_id": ISSUE,
                    "binding_kind": "issue",
                    "lane_disposition": DISPOSITION_HIBERNATED,
                    "lane_generation": GEN,
                    "revision": revision,
                },
            )()

        class Store:
            rows = (record(),)

            def records(self):
                return self.rows

        store = Store()
        refused_rows = (
            (record(), record(workspace="other-workspace", lane="other-lane")),
            (record(workspace="other-workspace", lane="other-lane"),),
            (record(revision="09"),),
        )
        for rows in refused_rows:
            with self.subTest(rows=rows):
                store.rows = rows
                self.assertIsNone(
                    live_cleanup_callback_scope(
                        store,
                        workspace_id=WS,
                        issue=ISSUE,
                        lane=LANE,
                        lane_generation=GEN,
                    )
                )

        class Unreadable:
            def records(self):
                raise OSError("unreadable")

        self.assertIsNone(
            live_cleanup_callback_scope(
                Unreadable(),
                workspace_id=WS,
                issue=ISSUE,
                lane=LANE,
                lane_generation=GEN,
            )
        )

    def test_live_scope_refuses_a_different_active_owner(self) -> None:
        class Store:
            def resolve_owner(self, workspace, issue):
                return type("Owner", (), {"resolved": True, "lane_id": "other-lane"})()

        self.assertIsNone(
            live_lane_callback_scope(
                Store(),
                workspace_id=WS,
                issue=ISSUE,
                lane=LANE,
                lane_generation=GEN,
            )
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

    def test_production_cleanup_transitions_the_active_lane_then_releases(self) -> None:
        key = self._declare()
        inventory = FakeInventoryOps()
        observed = []

        def transition_decision(issue: str, lane_generation: int) -> DecisionPointer:
            observed.append((issue, lane_generation))
            return DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id="96790"
            )

        outcome = self._ops(
            inventory=inventory, transition_decision_fn=transition_decision
        ).describe_release(issue=ISSUE, lane_generation=GEN)

        self.assertTrue(outcome.released, outcome.detail)
        self.assertEqual(observed, [(ISSUE, GEN)])
        record = self.store.get(key)
        self.assertEqual(record.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(record.process_release, RELEASE_RELEASED)
        self.assertEqual(record.decision.journal_id, "96790")
        # A complete-empty inventory is positive absence, so release completes
        # without dispatching an empty close plan to the provider.
        self.assertEqual(inventory.closed, [])

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

    def test_an_active_lane_without_durable_transition_authority_is_never_released(self) -> None:
        key = self._declare()
        outcome = self._ops().describe_release(issue=ISSUE, lane_generation=GEN)
        self.assertFalse(outcome.released)
        self.assertEqual(outcome.refusal, REFUSE_TRANSITION_AUTHORITY)
        self.assertEqual(self.store.get(key).lane_disposition, DISPOSITION_ACTIVE)

    def test_lifecycle_drift_after_authority_read_closes_no_process(self) -> None:
        key = self._declare()
        inventory = FakeInventoryOps()

        def transition_decision(issue: str, lane_generation: int) -> DecisionPointer:
            self.assertEqual((issue, lane_generation), (ISSUE, GEN))
            observed = self.store.get(key)
            self.store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=observed.revision,
                target=DISPOSITION_HIBERNATED,
                decision=DecisionPointer(
                    source="redmine", issue_id=ISSUE, journal_id="96789"
                ),
            )
            return DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id="96790"
            )

        outcome = self._ops(
            inventory=inventory, transition_decision_fn=transition_decision
        ).describe_release(issue=ISSUE, lane_generation=GEN)

        self.assertFalse(outcome.released)
        self.assertEqual(outcome.refusal, REFUSE_TRANSITION)
        self.assertEqual(inventory.closed, [])
        record = self.store.get(key)
        self.assertEqual(record.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(record.process_release, RELEASE_NOT_REQUESTED)

    def test_a_non_positive_generation_resolves_nothing(self) -> None:
        self._declare()
        for generation in (0, -1, True):
            with self.subTest(generation=generation):
                self.assertFalse(
                    self._ops().release_process(
                        issue=ISSUE, lane_generation=generation  # type: ignore[arg-type]
                    )
                )

    def test_a_never_opened_release_is_observed_not_released(self) -> None:
        key = self._declare()
        self._leave_active(key)
        generation = self.store.get(key).lane_generation
        self.assertEqual(
            self._ops().observe_release(issue=ISSUE, lane_generation=generation),
            RELEASE_OBSERVATION_NOT_RELEASED,
        )

    def test_our_requested_but_unsettled_release_is_observed_unavailable(self) -> None:
        key = self._declare()
        self._leave_active(key)
        record = self.store.get(key)
        action = f"auto_integration_retire:{ISSUE}:{record.lane_generation}"
        self.store.request_release(
            key,
            expected_revision=record.revision,
            action_id=action,
            observation=build_release_observation(()),
        )
        self.assertEqual(
            self._ops().observe_release(
                issue=ISSUE, lane_generation=record.lane_generation
            ),
            RELEASE_OBSERVATION_UNAVAILABLE,
        )

    def test_a_foreign_completed_release_is_not_ours(self) -> None:
        key = self._declare()
        self._leave_active(key)
        record = self.store.get(key)
        opened = self.store.request_release(
            key,
            expected_revision=record.revision,
            action_id="foreign-release",
            observation=build_release_observation(()),
        )
        self.store.record_release_outcome(
            key,
            action_id="foreign-release",
            expected_revision=opened.revision,
            target=RELEASE_RELEASED,
        )
        self.assertEqual(
            self._ops().observe_release(
                issue=ISSUE, lane_generation=record.lane_generation
            ),
            RELEASE_OBSERVATION_UNAVAILABLE,
        )

    def test_our_completed_release_is_observed_released(self) -> None:
        key = self._declare()
        self._leave_active(key)
        record = self.store.get(key)
        action = f"auto_integration_retire:{ISSUE}:{record.lane_generation}"
        opened = self.store.request_release(
            key,
            expected_revision=record.revision,
            action_id=action,
            observation=build_release_observation(()),
        )
        self.store.record_release_outcome(
            key,
            action_id=action,
            expected_revision=opened.revision,
            target=RELEASE_RELEASED,
        )
        self.assertEqual(
            self._ops().observe_release(
                issue=ISSUE, lane_generation=record.lane_generation
            ),
            RELEASE_OBSERVATION_RELEASED,
        )


# ---------------------------------------------------------------------------
# item 4: the durable ledger.
# ---------------------------------------------------------------------------


class DurableLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _reader, self.ledger = _durable_ledger_pair(self.home)

    def test_the_payloads_claim_about_its_writer_is_ignored(self) -> None:
        _admitted_append(
            self.ledger,
            StepOutcome(
                action_key="A",
                step=STEP_PUSH,
                outcome=OUTCOME_DONE,
                head=SOURCE,
                push_status="accepted",
                recorded_by="receipt:forged-by-a-caller",
            ),
        )
        (entry,) = self.ledger.read(action_key="A")
        self.assertEqual(entry.recorded_by, self.ledger.writer_id)
        self.assertNotEqual(entry.recorded_by, "receipt:forged-by-a-caller")

    def test_a_second_store_on_the_same_file_is_the_same_writer(self) -> None:
        # What makes a resume across the asynchronous CI gate possible: the next process reads
        # the same identity back and therefore counts what the first one recorded.
        _admitted_append(
            self.ledger,
            StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE),
        )
        resumed = SqliteLedgerStore(home=self.home)
        self.assertEqual(resumed.writer_id, self.ledger.writer_id)
        self.assertEqual(len(resumed.read(action_key="A")), 1)

    def test_a_different_ledger_file_is_a_different_writer(self) -> None:
        other = _open_ledger_writer(home=Path(tempfile.mkdtemp()))
        self.assertNotEqual(other.writer_id, self.ledger.writer_id)

    def test_a_second_done_for_one_step_is_refused_by_the_store(self) -> None:
        done = StepOutcome(
            action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE
        )
        _admitted_append(self.ledger, done)
        with self.assertRaises(AutoIntegrationLedgerError):
            _admitted_append(self.ledger, done)

    def test_a_blocked_step_may_be_recorded_more_than_once(self) -> None:
        # Only `done` is once-per-action: a refusal is an observation, and re-running an action
        # that keeps failing must keep recording that it failed.
        for _ in range(2):
            _admitted_append(
                self.ledger,
                StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_BLOCKED),
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
        intent = self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        self.ledger.append(
            StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE),
            receipt=intent.receipt,
        )
        self.assertEqual(self.ledger.unresolved_intents(action_key="A"), ())

    def test_entries_of_another_action_are_not_this_actions(self) -> None:
        _admitted_append(
            self.ledger,
            StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_DONE, head=SOURCE),
        )
        self.assertEqual(self.ledger.read(action_key="B"), ())

    def test_an_action_key_wildcard_cannot_widen_a_prefix_match(self) -> None:
        # `%` and `_` are legal in an action key (a target ref may carry either). If they reached
        # LIKE unescaped, one action's key would match another's rows.
        _admitted_append(
            self.ledger,
            StepOutcome(
                action_key="issue=1|lane_generation=1|source_head=XY|rest",
                step=STEP_INTEGRATION_CI,
                outcome=OUTCOME_DONE,
                head=SOURCE,
            ),
        )
        self.assertEqual(
            self.ledger.completed_action_keys(
                prefix="issue=1|lane_generation=1|source_head=_Y|",
                step=STEP_INTEGRATION_CI,
            ),
            (),
        )

    def test_the_resume_frame_and_transitions_are_append_only_and_idempotent(self) -> None:
        action = _durable_action()
        self.ledger.register_action(action)
        self.ledger.register_action(action)
        registered = SqliteLedgerStore(home=self.home).action(action.action_key)
        self.assertEqual(registered.state, ACTION_REGISTERED)
        self.assertEqual(self.ledger.action_event_count(action_key=action.action_key), 1)

        self.ledger.mark_action_awaiting_ci(
            action_key=action.action_key,
            landed_head=SOURCE,
            ci_workflow="required-ci",
        )
        self.ledger.mark_action_awaiting_ci(
            action_key=action.action_key,
            landed_head=SOURCE,
            ci_workflow="required-ci",
        )
        awaiting = SqliteLedgerStore(home=self.home).action(action.action_key)
        self.assertEqual(awaiting.state, ACTION_AWAITING_CI)
        self.assertEqual(awaiting.ci_workflow, "required-ci")
        self.assertEqual(self.ledger.action_event_count(action_key=action.action_key), 2)

        self.ledger.mark_action_terminal(
            action_key=action.action_key,
            state=ACTION_INTEGRATED,
            landed_head=SOURCE,
        )
        self.ledger.mark_action_terminal(
            action_key=action.action_key,
            state=ACTION_INTEGRATED,
            landed_head=SOURCE,
        )
        terminal = SqliteLedgerStore(home=self.home).action(action.action_key)
        self.assertEqual(terminal.state, ACTION_INTEGRATED)
        self.assertEqual(terminal.ci_workflow, "required-ci")
        self.assertEqual(self.ledger.action_event_count(action_key=action.action_key), 3)
        self.assertEqual(SqliteLedgerStore(home=self.home).resumable_actions(), ())

    def test_an_action_key_cannot_be_redirected_to_another_runtime_frame(self) -> None:
        action = _durable_action()
        self.ledger.register_action(action)
        with self.assertRaises(AutoIntegrationLedgerError):
            self.ledger.register_action(_durable_action(repo_root="/tmp/other-repo"))
        self.assertEqual(self.ledger.action_event_count(action_key=action.action_key), 1)

    def test_the_unreleased_v2_store_migrates_additively_to_the_action_registry(self) -> None:
        path = self.ledger.path
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TABLE auto_integration_action_event")
            conn.execute("DROP TABLE auto_integration_action")
            conn.execute("PRAGMA user_version = 2")

        migrated = _open_ledger_writer(home=self.home)
        migrated.register_action(_durable_action())
        with sqlite3.connect(path) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(version, AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION)
        self.assertEqual(
            SqliteLedgerStore(home=self.home).action(_durable_action().action_key).state,
            ACTION_REGISTERED,
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

    def reachability(self, commit: str, *, branch: str) -> str:
        """The three-valued form. ``target_head=""`` models a remote that cannot be queried."""
        if branch == LANE_BRANCH:
            return REACHABILITY_REACHABLE
        if not self.target_head:
            return REACHABILITY_UNAVAILABLE
        return (
            REACHABILITY_REACHABLE
            if self.commit_on_remote(commit, branch=branch)
            else REACHABILITY_NOT_REACHABLE
        )

    def commit_on_remote(self, commit: str, *, branch: str) -> bool:
        """Reachability that reflects this stub's own state, per branch.

        The lane branch always carries the source (it is where the work was pushed). The TARGET
        carries only what this stub's ``target_head`` says it does — which the push mutates. A
        double that answered True unconditionally could not express "the interrupted push never
        landed" at all, and the recovery path's whole job is telling those apart (the R5 lesson
        in #13686: a fake that does not reflect its own mutation makes the check meaningless).
        """
        if branch == LANE_BRANCH:
            return True
        return commit == self.target_head or self.is_ancestor(
            ancestor=commit, descendant=self.target_head
        )


class DivergedGitOperations(StubGitOperations):
    """A merge-commit path that can tighten policy immediately after local apply."""

    def __init__(self, *, after_apply: Callable[[], None]) -> None:
        super().__init__()
        self.after_apply = after_apply

    def is_ancestor(self, *, ancestor, descendant) -> bool:
        if ancestor == TARGET and descendant == SOURCE:
            return False
        return ancestor == descendant

    def apply_merge(self, *, source_head, target_ref, expected_target_head) -> MergeResult:
        result = super().apply_merge(
            source_head=source_head,
            target_ref=target_ref,
            expected_target_head=expected_target_head,
        )
        self.after_apply()
        return result


@dataclass
class StubAuthority:
    """The durable reader, with the CI verdict switchable so the async gate can settle."""

    integration_ci: Optional[IntegrationCiEvidence] = None
    cleanup: CleanupAuthority = field(default_factory=CleanupAuthority)

    def read_integration_authority(self, *, record) -> IntegrationAuthority:
        return IntegrationAuthority(
            review_generation_admissible=True,
            review_generation=record.review_generation,
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


_STUB_REACHABILITY = StubGitOperations.reachability


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
        reader, writer = _durable_ledger_pair(self.home)
        return AutoIntegrationUseCase(
            operations=StubGitOperations(target_head=target_head),
            integration_policy=AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=True
            ),
            authority=authority,
            ledger=reader,
            _ledger_writer=writer,
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
        ledger = _open_ledger_writer(home=self.home)
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
        ledger = _open_ledger_writer(home=self.home)
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
        self.reader, self.ledger = _durable_ledger_pair(self.home)

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
            ledger=self.reader,
            _ledger_writer=self.ledger,
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
        _admitted_append(self.ledger, _landed_push(record.action_key))
        read = _authorizing_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored"), SOURCE), record.action_key)

    def test_a_push_that_did_not_report_an_accepted_landing_authorizes_nothing(self) -> None:
        # A `done` push carrying no accepted status. The actuator does not write one, and this
        # reader is reading a FILE — where an invariant of the writer is not an invariant of the
        # bytes.
        record = _record()
        _admitted_append(
            self.ledger,
            StepOutcome(
                action_key=record.action_key,
                step=STEP_PUSH,
                outcome=OUTCOME_DONE,
                head=SOURCE,
            ),
        )
        read = _authorizing_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored"), SOURCE), "")

    def test_an_action_that_never_pushed_authorizes_nothing(self) -> None:
        read = _authorizing_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored"), SOURCE), "")

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
            _admitted_append(self.ledger, _landed_push(key))
        read = _authorizing_reader(self.ledger)
        self.assertEqual(read(self._cleanup_record("ignored"), SOURCE), "")


class R2ReviewFindingRegressionTest(unittest.TestCase):
    """Review j#96650's findings, pinned with the inputs that reproduced them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.reader, self.ledger = _durable_ledger_pair(self.home)

    # -- finding 1: the reconciliation path was a cross-action forge -------

    def test_f1_a_reconciliation_may_not_record_a_different_action(self) -> None:
        # Reproduced: one open admission on a DECOY action wrote an accepted push for the REAL
        # action, and the authorization reader took it. The path added to close a forgery was
        # itself one.
        self.ledger.begin_step(action_key="DECOY", step=STEP_PUSH)
        with self.assertRaises(AutoIntegrationLedgerError) as raised:
            self.ledger.resolve_intent(
                intent_id=self.ledger.unresolved_intents(action_key="DECOY")[0].intent_id,
                action_key="DECOY",
                step=STEP_PUSH,
                resolution=_landed_push(_record().action_key),
                observation="fabricated",
            )
        self.assertIn("only record an outcome for the admission it closes", str(raised.exception))
        self.assertEqual(self.ledger.read(action_key=_record().action_key), ())

    def test_f1_a_reconciliation_records_only_a_settled_outcome(self) -> None:
        self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        with self.assertRaises(AutoIntegrationLedgerError):
            self.ledger.resolve_intent(
                intent_id=self.ledger.unresolved_intents(action_key="A")[0].intent_id,
                action_key="A",
                step=STEP_PUSH,
                resolution=StepOutcome(action_key="A", step=STEP_PUSH, outcome="pending"),
                observation="not a measurement",
            )

    def test_f1_a_forged_receipt_must_also_match_the_coordinators_record(self) -> None:
        # The other half: even a receipt written through a self-taken admission authorizes
        # nothing unless it names the commit the COORDINATOR's integration record says landed.
        record = _record()
        _admitted_append(self.ledger, _landed_push(record.action_key))
        read = _authorizing_reader(self.ledger)
        self.assertEqual(read(self._cleanup(), SOURCE), record.action_key)  # agrees
        self.assertEqual(read(self._cleanup(), MERGED), "")  # coordinator says another commit
        self.assertEqual(read(self._cleanup(), ""), "")  # no corroborating record at all

    def test_f1_the_authorization_reader_cannot_write(self) -> None:
        reader = AutoIntegrationLedgerReader(store=self.reader)
        for forbidden in ("append", "begin_step", "resolve_intent"):
            with self.subTest(method=forbidden):
                self.assertFalse(hasattr(reader, forbidden))

    def _cleanup(self) -> CleanupActionRecord:
        return CleanupActionRecord(
            issue=ISSUE,
            lane_generation=GEN,
            branch=LANE_BRANCH,
            worktree_path=LANE_WORKTREE,
            recorded_source_head=SOURCE,
            integration_action_key="ignored",
        )

    # -- finding 2: the reconciliation is bound to the observed admission --

    def test_f2_a_stale_reconciliation_cannot_close_a_later_admission(self) -> None:
        # Reproduced: run 1's reconciliation closed run 2's admission, leaving run 2's mutation
        # unrecorded and its receipt refused.
        self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        observed = self.ledger.unresolved_intents(action_key="A")[0]
        self.ledger.resolve_intent(
            intent_id=observed.intent_id,
            action_key="A",
            step=STEP_PUSH,
            resolution=StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_BLOCKED),
            observation="run 1 did not land",
        )
        run2 = self.ledger.begin_step(action_key="A", step=STEP_PUSH)

        with self.assertRaises(AutoIntegrationLedgerError) as raised:
            self.ledger.resolve_intent(
                intent_id=observed.intent_id,  # the STALE observation
                action_key="A",
                step=STEP_PUSH,
                resolution=StepOutcome(
                    action_key="A", step=STEP_PUSH, outcome=OUTCOME_BLOCKED
                ),
                observation="stale",
            )
        self.assertIn("is not open", str(raised.exception))
        # And run 2 can still record its own outcome, which is what R2 destroyed.
        self.ledger.append(
            StepOutcome(
                action_key="A",
                step=STEP_PUSH,
                outcome=OUTCOME_DONE,
                head=SOURCE,
                push_status=PUSH_ACCEPTED,
            ),
            receipt=run2.receipt,
        )

    def test_f2_an_unqueryable_remote_is_not_an_unpushed_commit(self) -> None:
        # R2 read the gate's boolean, which folds "could not look" into "not there", so a probe
        # that could not run re-authorized the push.
        record = _record()
        self.ledger.begin_step(action_key=record.action_key, step=STEP_PUSH)
        outcome = StrandedActionReconciler(
            use_case=self._use_case(target_head="")
        ).reconcile(record)
        self.assertEqual(outcome.status, RECONCILED_AMBIGUOUS)
        self.assertEqual(
            len(SqliteLedgerStore(home=self.home).unresolved_intents(
                action_key=record.action_key
            )),
            1,
        )

    def test_f2_a_port_without_the_typed_probe_is_ambiguous(self) -> None:
        record = _record()
        self.ledger.begin_step(action_key=record.action_key, step=STEP_PUSH)
        use_case = self._use_case(target_head=SOURCE)
        del type(use_case.operations).reachability
        try:
            outcome = StrandedActionReconciler(use_case=use_case).reconcile(record)
        finally:
            type(use_case.operations).reachability = _STUB_REACHABILITY
        self.assertEqual(outcome.status, RECONCILED_AMBIGUOUS)

    def test_f2_a_stranded_process_retire_is_now_measurable(self) -> None:
        cleanup = self._cleanup()
        self.ledger.begin_step(action_key=cleanup.action_key, step=STEP_PROCESS_RETIRE)
        for observation, expected in (
            (RELEASE_OBSERVATION_RELEASED, RECONCILED_LANDED),
            (RELEASE_OBSERVATION_NOT_RELEASED, RECONCILED_NOT_LANDED),
            (RELEASE_OBSERVATION_UNAVAILABLE, RECONCILED_AMBIGUOUS),
        ):
            with self.subTest(observation=observation):
                fresh = _open_ledger_writer(home=self.home)
                if not fresh.unresolved_intents(action_key=cleanup.action_key):
                    fresh.begin_step(
                        action_key=cleanup.action_key, step=STEP_PROCESS_RETIRE
                    )
                use_case = AutoIntegrationUseCase(
                    operations=StubGitOperations(),
                    integration_policy=AutoIntegrationPolicy(mode="auto"),
                    processes=_ObservableReleases(observation=observation),
                    authority=StubAuthority(),
                    ledger=SqliteLedgerStore(home=self.home),
                    _ledger_writer=fresh,
                    lane_worktree=LANE_WORKTREE,
                    lane_branch=LANE_BRANCH,
                    lane_issue=ISSUE,
                    lane_generation=GEN,
                )
                outcome = StrandedCleanupReconciler(use_case=use_case).reconcile(cleanup)
                self.assertEqual(outcome.status, expected)

    # -- finding 3: the trigger, and pending idempotency -------------------

    def test_f3_an_unsettled_continuation_records_nothing_at_all(self) -> None:
        # Measured before: 2 rows -> 3 rows, while the outcome said it recorded no progress.
        use_case = self._use_case(target_head=TARGET)
        use_case.run_integration(_record())
        before = len(SqliteLedgerStore(home=self.home).read(action_key=_record().action_key))
        AsyncCiContinuation(use_case=self._use_case(target_head=SOURCE)).resume(_record())
        after = len(SqliteLedgerStore(home=self.home).read(action_key=_record().action_key))
        self.assertEqual((before, after), (2, 2))

    def test_f3_the_trigger_does_not_fire_while_ci_is_unsettled(self) -> None:
        self._use_case(target_head=TARGET).run_integration(_record())
        trigger = CiSettlementTrigger(
            use_case=self._use_case(target_head=SOURCE),
            ci_reader=_FixedCi(CI_STATE_PENDING),
        )
        outcome = trigger.settle(_record())
        self.assertEqual(outcome.status, CONTINUATION_CI_UNSETTLED)

    def test_f3_an_unavailable_provider_is_not_a_trigger(self) -> None:
        self._use_case(target_head=TARGET).run_integration(_record())
        outcome = CiSettlementTrigger(
            use_case=self._use_case(target_head=SOURCE),
            ci_reader=_FixedCi(CI_STATE_UNAVAILABLE),
        ).settle(_record())
        self.assertEqual(outcome.status, CONTINUATION_CI_UNSETTLED)

    def test_f3_a_terminal_verdict_fires_the_trigger(self) -> None:
        self._use_case(target_head=TARGET).run_integration(_record())
        settled = StubAuthority(
            integration_ci=IntegrationCiEvidence(
                integration_head=SOURCE,
                workflow="required-ci",
                run="int-1",
                conclusion="success",
            )
        )
        use_case = self._use_case(target_head=SOURCE, authority=settled)
        outcome = CiSettlementTrigger(
            use_case=use_case, ci_reader=_FixedCi(CI_STATE_SUCCESS)
        ).settle(_record())
        self.assertEqual(outcome.status, CONTINUATION_INTEGRATED)
        self.assertEqual(use_case.operations.pushed, [])

    def test_f3_a_terminal_failure_does_not_reenter_the_action(self) -> None:
        self._use_case(target_head=TARGET).run_integration(_record())
        use_case = self._use_case(target_head=SOURCE)
        outcome = CiSettlementTrigger(
            use_case=use_case, ci_reader=_FixedCi(CI_STATE_FAILURE)
        ).settle(_record(), workflow="required-ci")
        self.assertEqual(outcome.status, CONTINUATION_CI_FAILED)
        self.assertEqual(use_case.operations.pushed, [])

    # -- finding 4: the policy is re-read before every decision ------------

    def test_f4_a_policy_tightened_after_construction_is_observed(self) -> None:
        # Constructed under `auto`; the repository is changed to `disabled` before the run.
        use_case = AutoIntegrationUseCase(
            operations=StubGitOperations(),
            integration_policy=AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=True
            ),
            policy_source=lambda: AutoIntegrationPolicy(
                mode="disabled", integration_branch=TARGET_REF, ff_only=True
            ),
            authority=StubAuthority(),
            ledger=self.reader,
            _ledger_writer=self.ledger,
            lane_worktree=LANE_WORKTREE,
            lane_branch=LANE_BRANCH,
            lane_issue=ISSUE,
            lane_generation=GEN,
        )
        use_case.run_integration(_record())
        self.assertEqual(use_case.operations.pushed, [])

    def _assert_policy_tightened_between_apply_and_push(
        self, *, tightened: AutoIntegrationPolicy
    ) -> None:
        applied = {"value": False}
        operations = DivergedGitOperations(
            after_apply=lambda: applied.__setitem__("value", True)
        )

        def policy_source() -> AutoIntegrationPolicy:
            if applied["value"]:
                return tightened
            return AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=False
            )

        use_case = AutoIntegrationUseCase(
            operations=operations,
            integration_policy=policy_source(),
            policy_source=policy_source,
            authority=StubAuthority(),
            ledger=self.reader,
            _ledger_writer=self.ledger,
            lane_worktree=LANE_WORKTREE,
            lane_branch=LANE_BRANCH,
            lane_issue=ISSUE,
            lane_generation=GEN,
        )
        report = use_case.run_integration(_record())

        self.assertTrue(applied["value"])
        self.assertEqual(operations.pushed, [])
        self.assertEqual(
            [(outcome.step, outcome.outcome) for outcome in report.outcomes],
            [(STEP_INTEGRATION_APPLY, OUTCOME_DONE)],
        )

    def test_f4_mode_tightened_between_apply_and_push_blocks_the_push(self) -> None:
        self._assert_policy_tightened_between_apply_and_push(
            tightened=AutoIntegrationPolicy(
                mode="disabled", integration_branch=TARGET_REF, ff_only=False
            )
        )

    def test_f4_ff_only_tightened_between_apply_and_push_blocks_the_push(self) -> None:
        self._assert_policy_tightened_between_apply_and_push(
            tightened=AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=True
            )
        )

    # -- finding 5: a head that went red after its attestation ------------

    def test_f5_an_attested_head_that_is_currently_red_has_no_ci_evidence(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
            classify_runs,
        )

        self.assertEqual(
            classify_runs(
                [
                    {
                        "conclusion": "success",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "workflowName": "required-ci",
                        "databaseId": 1,
                    },
                    {
                        "conclusion": "failure",
                        "createdAt": "2026-01-02T00:00:00Z",
                        "workflowName": "required-ci",
                        "databaseId": 2,
                    },
                ],
                workflow="required-ci",
                attested_run="1",
            ).state,
            CI_STATE_FAILURE,
        )
        self.assertEqual(classify_runs([{"conclusion": "success"}]).state, CI_STATE_SUCCESS)
        self.assertEqual(classify_runs([{"status": "in_progress"}]).state, CI_STATE_PENDING)
        self.assertEqual(classify_runs([]).state, CI_STATE_UNAVAILABLE)
        # An unrecognised conclusion is not a success.
        self.assertEqual(classify_runs([{"conclusion": "weird"}]).state, CI_STATE_FAILURE)

    def test_f5_only_the_latest_run_of_the_required_workflow_decides(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
            classify_runs,
        )

        base = {
            "workflowName": "required-ci",
            "databaseId": 10,
            "createdAt": "2026-01-01T00:00:00Z",
            # This is the run the durable marker attested as green.  If the provider now
            # reports it failed, the marker/provider conjunction must fail even when a later
            # run is green (covered by the exact-identity test below).
            "conclusion": "success",
        }
        latest_success = {
            "workflowName": "required-ci",
            "databaseId": 11,
            "createdAt": "2026-01-02T00:00:00Z",
            "conclusion": "success",
        }
        unrelated_failure = {
            "workflowName": "optional-ci",
            "databaseId": 12,
            "createdAt": "2026-01-03T00:00:00Z",
            "conclusion": "failure",
        }
        self.assertEqual(
            classify_runs(
                [base, latest_success, unrelated_failure],
                workflow="required-ci",
                attested_run="10",
            ).state,
            CI_STATE_SUCCESS,
        )
        latest_pending = dict(latest_success, databaseId=13, createdAt="2026-01-04T00:00:00Z", conclusion="")
        self.assertEqual(
            classify_runs(
                [base, latest_success, latest_pending],
                workflow="required-ci",
                attested_run="10",
            ).state,
            CI_STATE_PENDING,
        )
        self.assertEqual(
            classify_runs(
                [latest_success], workflow="required-ci", attested_run="absent"
            ).state,
            CI_STATE_UNAVAILABLE,
        )

    def test_f5_provider_identity_must_match_exact_commit_and_attested_conclusion(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
            classify_runs,
        )

        attested = {
            "workflowName": "required-ci",
            "databaseId": 10,
            "createdAt": "2026-01-01T00:00:00Z",
            "conclusion": "success",
            "headSha": SOURCE,
        }
        latest = {
            "workflowName": "required-ci",
            "databaseId": 11,
            "createdAt": "2026-01-02T00:00:00Z",
            "conclusion": "success",
            "headSha": SOURCE,
        }
        self.assertEqual(
            classify_runs(
                [attested, latest],
                workflow="required-ci",
                attested_run="10",
                commit=SOURCE,
            ).state,
            CI_STATE_SUCCESS,
        )
        self.assertEqual(
            classify_runs(
                [dict(attested, headSha=MERGED), dict(latest, headSha=MERGED)],
                workflow="required-ci",
                attested_run="10",
                commit=SOURCE,
            ).state,
            CI_STATE_UNAVAILABLE,
        )
        self.assertEqual(
            classify_runs(
                [dict(attested, conclusion="failure"), latest],
                workflow="required-ci",
                attested_run="10",
                commit=SOURCE,
            ).state,
            CI_STATE_FAILURE,
        )

    def test_f5_a_source_branch_quick_run_is_not_target_branch_integration_ci(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
            classify_runs,
        )

        source_quick = {
            "workflowName": "Test",
            "databaseId": 20,
            "createdAt": "2026-01-01T00:00:00Z",
            "conclusion": "success",
            "headSha": SOURCE,
            "headBranch": LANE_BRANCH,
        }
        self.assertEqual(
            classify_runs(
                [source_quick], workflow="Test", commit=SOURCE, branch="main"
            ).state,
            CI_STATE_UNAVAILABLE,
        )
        target_pending = dict(
            source_quick,
            databaseId=21,
            createdAt="2026-01-02T00:00:00Z",
            conclusion="",
            headBranch="main",
        )
        self.assertEqual(
            classify_runs(
                [source_quick, target_pending],
                workflow="Test",
                commit=SOURCE,
                branch="main",
            ).state,
            CI_STATE_PENDING,
        )

    def test_f5_the_live_reader_uses_the_supported_actions_runs_api(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            auto_integration_ci_source as ci_source,
        )

        response = {
            "workflow_runs": [
                {
                    "status": "completed",
                    "conclusion": "success",
                    "name": "Test",
                    "id": 101,
                    "created_at": "2026-01-02T00:00:00Z",
                    "head_sha": SOURCE,
                    "head_branch": "main",
                    "event": "push",
                }
            ]
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(response), stderr=""
        )
        with mock.patch.object(ci_source.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
            ci_source.subprocess, "run", return_value=completed
        ) as run:
            verdict = ci_source.GhCliCiStatusReader(repo_root=Path(".")).verdict_for(
                SOURCE,
                workflow="Test",
                attested_run="101",
                branch="main",
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["gh", "api", "repos/{owner}/{repo}/actions/runs"])
        self.assertNotIn("--commit", argv)
        self.assertIn(f"head_sha={SOURCE}", argv)
        self.assertEqual(verdict.state, CI_STATE_SUCCESS)
        self.assertEqual((verdict.run, verdict.branch), ("101", "main"))

    # -- helpers ----------------------------------------------------------

    def _use_case(self, *, target_head: str, authority=None) -> AutoIntegrationUseCase:
        reader, writer = _durable_ledger_pair(self.home)
        return AutoIntegrationUseCase(
            operations=StubGitOperations(target_head=target_head),
            integration_policy=AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=True
            ),
            authority=authority or StubAuthority(),
            ledger=reader,
            _ledger_writer=writer,
            lane_worktree=LANE_WORKTREE,
            lane_branch=LANE_BRANCH,
            lane_issue=ISSUE,
            lane_generation=GEN,
        )


@dataclass
class _FixedCi:
    """A CI source with a fixed verdict — the trigger's input, not a provider."""

    state: str

    def verdict_for(
        self,
        commit: str,
        *,
        workflow: str = "",
        attested_run: str = "",
        branch: str = "",
    ) -> CiVerdict:
        return CiVerdict(
            self.state,
            "fixed",
            run=attested_run or "target-run",
            workflow=workflow,
            commit=commit,
            branch=branch,
            conclusion="success" if self.state == CI_STATE_SUCCESS else self.state,
        )


@dataclass
class _ObservableReleases:
    """A managed-process port that can say whether ITS release completed."""

    observation: str

    def release_process(self, *, issue: str, lane_generation: int) -> bool:
        return True

    def observe_release(self, *, issue: str, lane_generation: int) -> str:
        return self.observation


def _landed_push(action_key: str) -> StepOutcome:
    """The ledger fact that an action published its commit — an accepted, `done` push."""
    return StepOutcome(
        action_key=action_key,
        step=STEP_PUSH,
        outcome=OUTCOME_DONE,
        head=SOURCE,
        push_status=PUSH_ACCEPTED,
    )


def _authorizing_reader(ledger: SqliteLedgerStore):
    """The authorization reader over the READ-ONLY capability, as production wires it."""
    public_store = SqliteLedgerStore(path=ledger.path)
    return ledger_authorizing_action_reader(AutoIntegrationLedgerReader(store=public_store))


def _admitted_append(ledger: SqliteLedgerStore, outcome: StepOutcome) -> None:
    """Record ``outcome`` the way a run does: claim the admission, then present its receipt.

    Every test that wants a row in the ledger goes through this. Review j#96611 finding 3 was
    that a row could be written WITHOUT it, and the tests that appended directly were part of
    how that went unnoticed — they asserted the store accepted what the store should refuse.
    """
    intent = ledger.begin_step(action_key=outcome.action_key, step=outcome.step)
    ledger.append(outcome, receipt=intent.receipt)


class R1ReviewFindingRegressionTest(unittest.TestCase):
    """Review j#96611's findings 3 / 4 / 5, pinned with the inputs that reproduced them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.reader, self.ledger = _durable_ledger_pair(self.home)

    # -- finding 3: an outcome needs the admission that produced it --------

    def test_f3_an_append_with_no_admission_is_refused(self) -> None:
        # The exact reproduction: a bare `done` push with `accepted`, no mutation anywhere. R1
        # wrote the row and stamped it with the store's writer id.
        with self.assertRaises(AutoIntegrationLedgerError) as raised:
            self.ledger.append(_landed_push("A"))
        self.assertIn("no open admission", str(raised.exception))
        self.assertEqual(self.ledger.read(action_key="A"), ())

    def test_f3_the_public_store_has_zero_mutation_authority(self) -> None:
        with self.assertRaises(AutoIntegrationLedgerError) as raised:
            self.reader.begin_step(action_key="A", step=STEP_PUSH)
        self.assertIn("read capability", str(raised.exception))
        self.assertEqual(self.reader.read(action_key="A"), ())

    def test_f3_an_append_on_somebody_elses_admission_is_refused(self) -> None:
        self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        with self.assertRaises(AutoIntegrationLedgerError) as raised:
            self.ledger.append(_landed_push("A"), receipt="receipt:guessed")
        self.assertIn("not the one this store minted", str(raised.exception))
        self.assertEqual(self.ledger.read(action_key="A"), ())

    def test_f3_a_forged_row_can_no_longer_authorize_a_cleanup(self) -> None:
        # The consequence the finding named: the forged row became a cleanup's authorizing
        # action. With no row, the reader names nothing, and an empty key matches no record's.
        record = _record()
        try:
            self.ledger.append(_landed_push(record.action_key))
        except AutoIntegrationLedgerError:
            pass
        read = _authorizing_reader(self.ledger)
        self.assertEqual(
            read(
                CleanupActionRecord(
                    issue=ISSUE,
                    lane_generation=GEN,
                    branch=LANE_BRANCH,
                    worktree_path=LANE_WORKTREE,
                    recorded_source_head=SOURCE,
                    integration_action_key="anything",
                ),
                SOURCE,
            ),
            "",
        )

    # -- finding 4: the admission is a compare-and-set --------------------

    def test_f4_a_second_run_is_refused_the_admission_before_it_mutates(self) -> None:
        # Reviewer's minimal reproduction: two stores on one file both opened an intent for the
        # same push, so both proceeded to mutate.
        other = _open_ledger_writer(home=self.home)
        self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        with self.assertRaises(AutoIntegrationAdmissionError):
            other.begin_step(action_key="A", step=STEP_PUSH)
        self.assertEqual(len(self.ledger.unresolved_intents(action_key="A")), 1)

    def test_f4_the_admission_is_reusable_once_it_is_closed(self) -> None:
        # Exclusive while held, not exclusive forever: a blocked step must be re-attemptable.
        _admitted_append(
            self.ledger,
            StepOutcome(action_key="A", step=STEP_PUSH, outcome=OUTCOME_BLOCKED),
        )
        self.ledger.begin_step(action_key="A", step=STEP_PUSH)
        self.assertEqual(len(self.ledger.unresolved_intents(action_key="A")), 1)

    def test_f4_a_refused_admission_stops_the_run_without_mutating(self) -> None:
        record = _record()
        self.ledger.begin_step(action_key=record.action_key, step=STEP_PUSH)
        use_case = AutoIntegrationUseCase(
            operations=StubGitOperations(),
            integration_policy=AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=True
            ),
            authority=StubAuthority(),
            ledger=self.reader,
            _ledger_writer=_open_ledger_writer(home=self.home),
            lane_worktree=LANE_WORKTREE,
            lane_branch=LANE_BRANCH,
            lane_issue=ISSUE,
            lane_generation=GEN,
        )
        report = use_case.run_integration(record)
        self.assertEqual(use_case.operations.pushed, [])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)

    # -- finding 5: recovery, with three outcomes -------------------------

    def _reconciler(self, *, target_head: str) -> StrandedActionReconciler:
        reader, writer = _durable_ledger_pair(self.home)
        return StrandedActionReconciler(
            use_case=AutoIntegrationUseCase(
                operations=StubGitOperations(target_head=target_head),
                integration_policy=AutoIntegrationPolicy(
                    mode="auto", integration_branch=TARGET_REF, ff_only=True
                ),
                authority=StubAuthority(),
                ledger=reader,
                _ledger_writer=writer,
                lane_worktree=LANE_WORKTREE,
                lane_branch=LANE_BRANCH,
                lane_issue=ISSUE,
                lane_generation=GEN,
            )
        )

    def test_f5_a_stranded_push_that_landed_is_recorded_as_landed(self) -> None:
        record = _record()
        self.ledger.begin_step(action_key=record.action_key, step=STEP_PUSH)
        # The remote carries the source head: the interrupted push did land.
        outcome = self._reconciler(target_head=SOURCE).reconcile(record)
        self.assertEqual(outcome.status, RECONCILED_LANDED)
        self.assertEqual(outcome.head, SOURCE)
        (row,) = SqliteLedgerStore(home=self.home).read(action_key=record.action_key)
        self.assertEqual(row.outcome, OUTCOME_DONE)
        self.assertEqual(row.push_status, PUSH_ACCEPTED)
        self.assertEqual(
            SqliteLedgerStore(home=self.home).unresolved_intents(
                action_key=record.action_key
            ),
            (),
        )

    def test_f5_a_stranded_push_that_did_not_land_is_re_runnable(self) -> None:
        record = _record()
        self.ledger.begin_step(action_key=record.action_key, step=STEP_PUSH)
        # The remote is still at the old target: the push never landed.
        outcome = self._reconciler(target_head=TARGET).reconcile(record)
        self.assertEqual(outcome.status, RECONCILED_NOT_LANDED)
        (row,) = SqliteLedgerStore(home=self.home).read(action_key=record.action_key)
        self.assertEqual(row.outcome, OUTCOME_BLOCKED)
        self.assertEqual(
            SqliteLedgerStore(home=self.home).unresolved_intents(
                action_key=record.action_key
            ),
            (),
        )

    def test_f5_an_unreadable_target_leaves_the_admission_open(self) -> None:
        # The third outcome, and the one that must NOT resolve: an unreadable remote is not an
        # unpushed commit, and guessing here is the defect being recovered from, one layer out.
        record = _record()
        self.ledger.begin_step(action_key=record.action_key, step=STEP_PUSH)
        outcome = self._reconciler(target_head="").reconcile(record)
        self.assertEqual(outcome.status, RECONCILED_AMBIGUOUS)
        self.assertFalse(outcome.resolved)
        self.assertEqual(
            len(
                SqliteLedgerStore(home=self.home).unresolved_intents(
                    action_key=record.action_key
                )
            ),
            1,
        )

    def test_f5_reconciliation_needs_a_stranded_admission_to_close(self) -> None:
        # Not a second way to invent a receipt: with nothing open there is nothing to close.
        outcome = self._reconciler(target_head=SOURCE).reconcile(_record())
        self.assertEqual(outcome.status, RECONCILED_NOTHING_STRANDED)
        self.assertEqual(SqliteLedgerStore(home=self.home).read(action_key=_record().action_key), ())

    def test_f5_after_recovery_the_action_continues(self) -> None:
        # The whole point of recovery over permanent block: the run proceeds afterwards.
        record = _record()
        self.ledger.begin_step(action_key=record.action_key, step=STEP_PUSH)
        self._reconciler(target_head=SOURCE).reconcile(record)
        use_case = AutoIntegrationUseCase(
            operations=StubGitOperations(target_head=SOURCE),
            integration_policy=AutoIntegrationPolicy(
                mode="auto", integration_branch=TARGET_REF, ff_only=True
            ),
            authority=StubAuthority(
                integration_ci=IntegrationCiEvidence(
                    integration_head=SOURCE,
                    workflow="required-ci",
                    run="int-1",
                    conclusion="success",
                )
            ),
            ledger=SqliteLedgerStore(home=self.home),
            _ledger_writer=_open_ledger_writer(home=self.home),
            lane_worktree=LANE_WORKTREE,
            lane_branch=LANE_BRANCH,
            lane_issue=ISSUE,
            lane_generation=GEN,
        )
        outcome = AsyncCiContinuation(use_case=use_case).resume(record)
        self.assertEqual(outcome.status, CONTINUATION_INTEGRATED)
        self.assertEqual(use_case.operations.pushed, [])


@dataclass
class _AlwaysReleases:
    def release_process(self, *, issue: str, lane_generation: int) -> bool:
        return True


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
