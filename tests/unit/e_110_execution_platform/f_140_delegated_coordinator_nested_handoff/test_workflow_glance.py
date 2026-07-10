"""Coordinator pipeline glance projection tests (Redmine #13435).

Pins the pure fold (:func:`fold_glance_row`) and the source adapters
(:mod:`...application.glance_snapshot_source`) that back ``workflow glance``:

- **the load-bearing invariant** — a delivery anomaly never rolls the durable workflow
  state back: a done-but-not-delivered lane still classifies as ``review_waiting``,
  flagged with the anomaly and re-owned to the coordinator (the visible stall the
  motivating session missed);
- the three motivating delivery scenarios as fixtures: #13408 j#74118 callback
  self-loop, #13425 j#73980 turn_start_unconfirmed, #13392 durable-journal-poll
  supersession (a later gate makes the earlier anomaly ``stale``, and the state is not
  wound back);
- vocabulary fail-closed folding (an out-of-vocabulary anomaly / runtime / source /
  receive value folds to the ``unknown`` / ``none`` catch-all, never guessed);
- the renderer + JSON payload (empty input, the ``stale`` / runtime-observation
  markers, the active-anomaly summary);
- the adapters: structured-snapshot parsing, the conservative herdr-ledger ->
  anomaly derivation, and the fail-open store enumeration.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_IMPLEMENTATION_DONE,
    GATE_PROGRESS,
    GATE_REVIEW,
    GATE_REVIEW_REQUEST,
    LaneSignal,
    REVIEW_APPROVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
    ANOMALY_CALLBACK_SELF_LOOP,
    ANOMALY_NONE,
    ANOMALY_STAGED_NOT_SUBMITTED,
    ANOMALY_TURN_START_UNCONFIRMED,
    ANOMALY_UNKNOWN,
    DELIVERY_SOURCE_HERDR_LEDGER,
    DELIVERY_SOURCE_NONE,
    DELIVERY_SOURCE_RUNTIME_OBSERVATION,
    OWNER_AUDITOR,
    OWNER_COORDINATOR,
    OWNER_WORKER,
    RECEIVE_CALLBACK,
    RUNTIME_AWAITING_INPUT,
    RUNTIME_UNKNOWN,
    DeliveryObservation,
    IssueGlanceSnapshot,
    fold_glance_row,
    fold_glance_rows,
    glance_payload,
    render_glance_table,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (
    GlanceIssueRecord,
    MappingGlanceRedmineSource,
    MappingGlanceSnapshotSource,
    active_lane_snapshots,
    anomaly_from_ledger_record,
    enumerate_active_lanes,
    store_active_lane_snapshots,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (
    fold_issue_gate_facts,
    lane_signal_from_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_CLOSE,
    GATE_REVIEW as _GATE_REVIEW,
    REVIEW_CHANGES_REQUESTED,
    classify_lane_state,
)


def _snap(issue, gate, *, journal="", delivery=None, review="pending", **kw):
    return IssueGlanceSnapshot(
        issue_id=issue,
        signal=LaneSignal(issue=issue, latest_gate=gate, review_conclusion=review, **kw),
        latest_gate_journal=journal,
        delivery=delivery or DeliveryObservation(),
    )


class FoldNonRollbackTest(unittest.TestCase):
    """A delivery anomaly is a separate dimension; it never rewinds workflow state."""

    def test_workflow_state_folds_from_durable_record_only(self):
        row = fold_glance_row(_snap("13435", GATE_REVIEW_REQUEST))
        self.assertEqual(row.workflow_state, "review_waiting")
        self.assertEqual(row.state_class, row.workflow_state)
        self.assertEqual(row.next_owner, OWNER_AUDITOR)
        self.assertEqual(row.delivery_anomaly, ANOMALY_NONE)
        self.assertFalse(row.has_active_anomaly)

    def test_done_but_not_delivered_reads_as_stall_without_rollback(self):
        # review_request recorded (workflow-wise done, audit owed) but the handoff staged a
        # marker and never submitted: the state stays review_waiting, the anomaly is live,
        # and the row is re-owned to the coordinator — the visible stall.
        delivery = DeliveryObservation(
            anomaly=ANOMALY_STAGED_NOT_SUBMITTED,
            source=DELIVERY_SOURCE_RUNTIME_OBSERVATION,
            observed_journal="74210",
            runtime_state=RUNTIME_AWAITING_INPUT,
            receive_method=RECEIVE_CALLBACK,
        )
        row = fold_glance_row(_snap("13435", GATE_REVIEW_REQUEST, journal="74210", delivery=delivery))
        self.assertEqual(row.workflow_state, "review_waiting")  # NOT rolled back to implementing
        self.assertEqual(row.delivery_anomaly, ANOMALY_STAGED_NOT_SUBMITTED)
        self.assertFalse(row.delivery_anomaly_stale)
        self.assertTrue(row.has_active_anomaly)
        self.assertEqual(row.next_owner, OWNER_COORDINATOR)
        self.assertIn("staged_not_submitted", row.next_action)
        self.assertEqual(row.runtime_state, RUNTIME_AWAITING_INPUT)
        self.assertEqual(row.delivery_source, DELIVERY_SOURCE_RUNTIME_OBSERVATION)

    def test_healthy_implementing_lane_owned_by_worker(self):
        row = fold_glance_row(_snap("13446", GATE_PROGRESS))
        self.assertEqual(row.workflow_state, "implementing")
        self.assertEqual(row.next_owner, OWNER_WORKER)


class MotivatingScenarioFixtureTest(unittest.TestCase):
    """The three delivery stalls that motivated the issue, as fixtures."""

    def test_13425_turn_start_unconfirmed_live_anomaly(self):
        # #13425 j#73980: the turn-start rail injected the notification but no working
        # transition was observed. impl_done recorded at the same journal -> live anomaly.
        delivery = DeliveryObservation(
            anomaly=ANOMALY_TURN_START_UNCONFIRMED,
            source=DELIVERY_SOURCE_HERDR_LEDGER,
            observed_journal="73980",
        )
        row = fold_glance_row(
            _snap("13425", GATE_IMPLEMENTATION_DONE, journal="73980", delivery=delivery)
        )
        self.assertEqual(row.workflow_state, "review_waiting")
        self.assertEqual(row.delivery_anomaly, ANOMALY_TURN_START_UNCONFIRMED)
        self.assertFalse(row.delivery_anomaly_stale)
        self.assertEqual(row.next_owner, OWNER_COORDINATOR)

    def test_13408_callback_self_loop_live_then_stale(self):
        # #13408 j#74118: coordinator callback resolved to the sender lane (self-loop).
        # (a) still the latest gate -> live anomaly.
        live = DeliveryObservation(
            anomaly=ANOMALY_CALLBACK_SELF_LOOP,
            source=DELIVERY_SOURCE_HERDR_LEDGER,
            observed_journal="74118",
        )
        row_live = fold_glance_row(
            _snap("13408", GATE_REVIEW, journal="74118", review=REVIEW_APPROVED, delivery=live)
        )
        self.assertEqual(row_live.workflow_state, "owner_waiting")
        self.assertTrue(row_live.has_active_anomaly)
        self.assertEqual(row_live.next_owner, OWNER_COORDINATOR)

        # (b) a later durable gate (j#74130) supersedes the self-loop observation ->
        # anomaly marked stale, state NOT wound back, owner from the durable state.
        row_stale = fold_glance_row(
            _snap("13408", GATE_REVIEW, journal="74130", review=REVIEW_APPROVED, delivery=live)
        )
        self.assertEqual(row_stale.workflow_state, "owner_waiting")
        self.assertEqual(row_stale.delivery_anomaly, ANOMALY_CALLBACK_SELF_LOOP)
        self.assertTrue(row_stale.delivery_anomaly_stale)
        self.assertFalse(row_stale.has_active_anomaly)
        self.assertEqual(row_stale.next_owner, OWNER_COORDINATOR)  # owner_waiting base owner

    def test_13392_durable_journal_poll_supersedes_anomaly(self):
        # #13392: a durable journal poll advanced the gate past an earlier delivery hiccup.
        # The anomaly observed at j#74067 is stale against the latest gate at j#74133; the
        # workflow state is the durable one and is not wound back to a stall.
        delivery = DeliveryObservation(
            anomaly=ANOMALY_TURN_START_UNCONFIRMED,
            source=DELIVERY_SOURCE_HERDR_LEDGER,
            observed_journal="74067",
        )
        row = fold_glance_row(
            _snap("13392", GATE_REVIEW_REQUEST, journal="74133", delivery=delivery)
        )
        self.assertEqual(row.workflow_state, "review_waiting")
        self.assertTrue(row.delivery_anomaly_stale)
        self.assertFalse(row.has_active_anomaly)
        self.assertEqual(row.next_owner, OWNER_AUDITOR)  # durable state owner, not coordinator


class VocabularyFailClosedTest(unittest.TestCase):
    def test_out_of_vocabulary_values_fold_to_catch_all(self):
        delivery = DeliveryObservation(
            anomaly="totally-made-up",
            source="nowhere",
            runtime_state="hyperspace",
            receive_method="carrier-pigeon",
            observed_journal="1",
        )
        row = fold_glance_row(_snap("1", GATE_PROGRESS, journal="1", delivery=delivery))
        self.assertEqual(row.delivery_anomaly, ANOMALY_UNKNOWN)
        self.assertEqual(row.delivery_source, DELIVERY_SOURCE_NONE)
        self.assertEqual(row.runtime_state, RUNTIME_UNKNOWN)
        self.assertEqual(row.receive_method, "unknown")

    def test_stale_requires_numeric_journals_else_live(self):
        # A non-numeric journal id cannot be compared -> the anomaly is treated as live.
        delivery = DeliveryObservation(
            anomaly=ANOMALY_TURN_START_UNCONFIRMED, observed_journal="abc"
        )
        row = fold_glance_row(_snap("1", GATE_REVIEW_REQUEST, journal="74133", delivery=delivery))
        self.assertFalse(row.delivery_anomaly_stale)
        self.assertTrue(row.has_active_anomaly)


class RendererTest(unittest.TestCase):
    def test_empty_renders_explanatory_line_not_bare_header(self):
        self.assertIn("no active lanes", render_glance_table([]))

    def test_table_marks_stale_and_runtime_observation(self):
        rows = fold_glance_rows(
            [
                _snap(
                    "13435",
                    GATE_REVIEW_REQUEST,
                    journal="74210",
                    delivery=DeliveryObservation(
                        anomaly=ANOMALY_STAGED_NOT_SUBMITTED,
                        source=DELIVERY_SOURCE_RUNTIME_OBSERVATION,
                        observed_journal="74210",
                    ),
                ),
                _snap(
                    "13392",
                    GATE_REVIEW_REQUEST,
                    journal="74133",
                    delivery=DeliveryObservation(
                        anomaly=ANOMALY_TURN_START_UNCONFIRMED, observed_journal="74067"
                    ),
                ),
            ]
        )
        table = render_glance_table(rows)
        self.assertIn("~staged_not_submitted", table)  # runtime-observed marker
        self.assertIn("(stale)", table)  # superseded anomaly marker
        self.assertIn("WORKFLOW_STATE", table)

    def test_payload_summarises_only_live_anomalies(self):
        rows = fold_glance_rows(
            [
                _snap(
                    "13435",
                    GATE_REVIEW_REQUEST,
                    journal="74210",
                    delivery=DeliveryObservation(
                        anomaly=ANOMALY_STAGED_NOT_SUBMITTED, observed_journal="74210"
                    ),
                ),
                _snap(
                    "13392",
                    GATE_REVIEW_REQUEST,
                    journal="74133",
                    delivery=DeliveryObservation(
                        anomaly=ANOMALY_TURN_START_UNCONFIRMED, observed_journal="74067"
                    ),
                ),
            ]
        )
        payload = glance_payload(rows)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["active_anomaly_issues"], ["13435"])  # stale one excluded


class MappingSnapshotSourceTest(unittest.TestCase):
    def test_issues_wrapper_and_bare_list_both_parse(self):
        entry = {"issue": "13435", "latest_gate": "review_request", "lane": "l1"}
        wrapped = MappingGlanceSnapshotSource({"issues": [entry]}).snapshots()
        bare = MappingGlanceSnapshotSource([entry]).snapshots()
        self.assertEqual(len(wrapped), 1)
        self.assertEqual(len(bare), 1)
        self.assertEqual(wrapped[0].issue_id, "13435")
        self.assertEqual(wrapped[0].signal.latest_gate, "review_request")
        self.assertEqual(wrapped[0].lane, "l1")

    def test_entry_without_issue_is_skipped(self):
        snaps = MappingGlanceSnapshotSource([{"latest_gate": "review"}, {"issue": "7"}]).snapshots()
        self.assertEqual([s.issue_id for s in snaps], ["7"])

    def test_out_of_vocabulary_gate_folds_to_none(self):
        snaps = MappingGlanceSnapshotSource([{"issue": "7", "latest_gate": "bogus"}]).snapshots()
        self.assertEqual(snaps[0].signal.latest_gate, "none")

    def test_delivery_submapping_parsed(self):
        snaps = MappingGlanceSnapshotSource(
            [
                {
                    "issue": "13435",
                    "latest_gate": "review_request",
                    "delivery": {
                        "anomaly": "staged_not_submitted",
                        "runtime_state": "awaiting_input",
                    },
                }
            ]
        ).snapshots()
        row = fold_glance_row(snaps[0])
        self.assertEqual(row.delivery_anomaly, ANOMALY_STAGED_NOT_SUBMITTED)
        self.assertEqual(row.runtime_state, RUNTIME_AWAITING_INPUT)


class _FakeLedgerRecord:
    def __init__(self, **kw):
        self.journal_id = kw.get("journal_id")
        self.disposition = kw.get("disposition")
        self.status = kw.get("status")
        self.turn_start_outcome = kw.get("turn_start_outcome")


class LedgerAnomalyDerivationTest(unittest.TestCase):
    def test_turn_start_delivered_not_started_maps_to_unconfirmed(self):
        rec = _FakeLedgerRecord(
            journal_id="73980", turn_start_outcome={"outcome": "delivered_not_started"}
        )
        obs = anomaly_from_ledger_record(rec)
        self.assertEqual(obs.anomaly, ANOMALY_TURN_START_UNCONFIRMED)
        self.assertEqual(obs.source, DELIVERY_SOURCE_HERDR_LEDGER)
        self.assertEqual(obs.observed_journal, "73980")

    def test_inject_failed_maps_to_staged_not_submitted(self):
        rec = _FakeLedgerRecord(turn_start_outcome={"outcome": "inject_failed"})
        self.assertEqual(anomaly_from_ledger_record(rec).anomaly, ANOMALY_STAGED_NOT_SUBMITTED)

    def test_disposition_token_used_verbatim(self):
        rec = _FakeLedgerRecord(disposition="callback_self_loop")
        self.assertEqual(anomaly_from_ledger_record(rec).anomaly, ANOMALY_CALLBACK_SELF_LOOP)

    def test_started_and_unknown_are_healthy(self):
        started = _FakeLedgerRecord(turn_start_outcome={"outcome": "started"})
        empty = _FakeLedgerRecord()
        self.assertEqual(anomaly_from_ledger_record(started).anomaly, ANOMALY_NONE)
        self.assertEqual(anomaly_from_ledger_record(empty).anomaly, ANOMALY_NONE)


class _FakeEventRow:
    def __init__(self, event_id, issue, gate, **kw):
        self.event_id = event_id
        self.issue = issue
        self.gate = gate
        self.review_conclusion = kw.get("review_conclusion", "pending")
        self.callback_state = kw.get("callback_state", "none")
        self.commit_bearing = kw.get("commit_bearing", False)
        self.integration_recorded = kw.get("integration_recorded", False)
        self.issue_open = kw.get("issue_open", True)
        self.blocker_recorded = kw.get("blocker_recorded", False)


class _FakeRouteRow:
    def __init__(self, issue, lane_id):
        self.issue = issue
        self.lane_id = lane_id


class _FakeStore:
    def __init__(self, events, routes=()):
        self._events = events
        self._routes = routes

    def read_events(self):
        return tuple(self._events)

    def read_route_identities(self):
        return tuple(self._routes)


class _RaisingStore:
    def read_events(self):
        raise RuntimeError("boom")

    def read_route_identities(self):
        raise RuntimeError("boom")


class StoreEnumerationTest(unittest.TestCase):
    def test_latest_event_per_issue_wins_and_lane_joined(self):
        events = [
            _FakeEventRow("redmine:13435:74100", "13435", GATE_PROGRESS),
            _FakeEventRow("redmine:13435:74210", "13435", GATE_REVIEW_REQUEST),
            _FakeEventRow("redmine:13446:74050", "13446", GATE_PROGRESS),
        ]
        routes = [_FakeRouteRow("13435", "issue_13435_pipeline_glance")]
        snaps = store_active_lane_snapshots(_FakeStore(events, routes))
        by_issue = {s.issue_id: s for s in snaps}
        self.assertEqual(by_issue["13435"].signal.latest_gate, GATE_REVIEW_REQUEST)
        self.assertEqual(by_issue["13435"].latest_gate_journal, "74210")
        self.assertEqual(by_issue["13435"].lane, "issue_13435_pipeline_glance")
        self.assertEqual([s.issue_id for s in snaps], ["13435", "13446"])  # first-seen order

    def test_ledger_join_supplies_delivery(self):
        events = [_FakeEventRow("redmine:13425:73980", "13425", GATE_IMPLEMENTATION_DONE)]

        class _Ledger:
            def records_for_issue(self, issue_id):
                return [
                    _FakeLedgerRecord(
                        journal_id="73980",
                        turn_start_outcome={"outcome": "delivered_not_started"},
                    )
                ]

        snaps = store_active_lane_snapshots(_FakeStore(events), ledger=_Ledger())
        row = fold_glance_row(snaps[0])
        self.assertEqual(row.delivery_anomaly, ANOMALY_TURN_START_UNCONFIRMED)
        self.assertTrue(row.has_active_anomaly)

    def test_store_read_failure_is_fail_open(self):
        self.assertEqual(store_active_lane_snapshots(_RaisingStore()), ())


def _j(journal_id, notes):
    return (journal_id, notes)


class JournalGrammarTest(unittest.TestCase):
    """The glance-only ``## Gate:`` template grammar (Redmine #13435 j#74307 Option C).

    Fixtures are the real #13435 journal heading variants: only line-anchored ``## Gate:``
    headings are read, combined headings split, collisions are excluded, and a review
    conclusion is taken only from an explicit ``結論:`` field.
    """

    def test_combined_and_collisions_and_conclusion(self):
        # The real #13435 sequence: Start, combined impl_done+review_request, audit review
        # (要修正), then the Review Finding Verdicts / Design Consultation Answer collisions.
        facts = fold_issue_gate_facts(
            [
                _j("74193", "## Gate: Start (Claude implementation_worker)\n- lane: x"),
                _j(
                    "74194",
                    "## Gate: Implementation Done + Review Request (worker)\n- **commit: `93ac924`**",
                ),
                _j("74295", "## Gate: review (Codex US-level audit)\n- 結論: 要修正"),
                _j("74298", "## Gate: Review Finding Verdicts (worker, 迎合禁止)\n- x"),
                _j("74299", "## Gate: Design Consultation Answer (Codex)\n- y"),
                _j("74316", "## Progress Log: Correction 実装再開\n- z"),  # not a gate
            ]
        )
        self.assertEqual(facts.latest_gate, _GATE_REVIEW)  # not the later verdict/consult
        self.assertEqual(facts.latest_gate_journal, "74295")
        self.assertEqual(facts.review_conclusion, REVIEW_CHANGES_REQUESTED)
        self.assertTrue(facts.commit_bearing)  # combined journal carried a commit
        sig = lane_signal_from_gate_facts("13435", facts, issue_open=True)
        self.assertEqual(classify_lane_state(sig), "implementing")  # 要修正 -> back to worker

    def test_review_request_alone_is_review_waiting(self):
        facts = fold_issue_gate_facts([_j("100", "## Gate: review_request\n- foo")])
        sig = lane_signal_from_gate_facts("7", facts)
        self.assertEqual(classify_lane_state(sig), "review_waiting")

    def test_approved_review_conclusion(self):
        facts = fold_issue_gate_facts([_j("100", "## Gate: review\n- 結論: 承認")])
        self.assertEqual(classify_lane_state(lane_signal_from_gate_facts("7", facts)), "owner_waiting")

    def test_dispatch_heading_is_implementing(self):
        facts = fold_issue_gate_facts(
            [_j("50", "## Gate: Implementation Request Dispatch (Codex coordinator)\n- x")]
        )
        self.assertEqual(classify_lane_state(lane_signal_from_gate_facts("7", facts)), "implementing")

    def test_review_finding_verdicts_is_not_an_audit_review(self):
        # A verdict journal alone must NOT classify as an audit review (collision guard).
        facts = fold_issue_gate_facts([_j("100", "## Gate: Review Finding Verdicts (worker)\n- 結論: 承認")])
        self.assertIsNone(facts)  # unrecognized -> unknown, never a fabricated review

    def test_integration_deferral_is_not_integration_complete(self):
        # The real governed heading (#13446 j#74290): a deferral must NOT set
        # integration_recorded, so a commit-bearing owner-approved lane stays
        # integration_waiting (re-audit j#74323 Finding 1), not close_waiting.
        facts = fold_issue_gate_facts(
            [
                _j("200", "## Gate: owner_close_approval\n- commit_hash: `deadbee`"),
                _j("201", "## Integration disposition: explicit_deferral (bounded current wave)\n- reason: later"),
            ]
        )
        self.assertFalse(facts.integration_recorded)  # deferral != integrated
        sig = lane_signal_from_gate_facts("7", facts, issue_open=True)
        self.assertEqual(classify_lane_state(sig), "integration_waiting")
        self.assertEqual(facts.latest_gate_journal, "200")  # deferral is not a gate journal

    def test_integration_completion_disposition_marks_recorded(self):
        facts = fold_issue_gate_facts(
            [
                _j("200", "## Gate: owner_close_approval\n- commit_hash: `deadbee`"),
                _j("201", "## Integration disposition: merged\n- into main"),
            ]
        )
        self.assertTrue(facts.integration_recorded)  # a completion disposition IS integrated
        sig = lane_signal_from_gate_facts("7", facts, issue_open=True)
        self.assertEqual(classify_lane_state(sig), "close_waiting")

    def test_closed_issue_does_not_fabricate_retire(self):
        # closed + review-approved with NO commit facts must NOT be asserted retire_ready
        # (re-audit j#74323 Finding 3): retirement needs positively-resolved integration.
        facts = fold_issue_gate_facts([_j("100", "## Gate: review\n- 結論: 承認")])
        sig = lane_signal_from_gate_facts("7", facts, issue_open=False)
        self.assertNotEqual(classify_lane_state(sig), "retire_ready")

    def test_closed_with_real_close_gate_unmerged_is_integration_waiting(self):
        # A real close gate carrying an unmerged commit is integration_waiting, not retire.
        facts = fold_issue_gate_facts([_j("100", "## Gate: close\n- commit_hash: `abc1234`")])
        sig = lane_signal_from_gate_facts("7", facts, issue_open=False)
        self.assertEqual(classify_lane_state(sig), "integration_waiting")

    def test_non_gate_headings_yield_no_facts(self):
        facts = fold_issue_gate_facts(
            [
                _j("1", "## Progress Log: hi"),
                _j("2", "## Handoff Delivery Record\n- sent"),
                _j("3", "## Correction: oops"),
            ]
        )
        self.assertIsNone(facts)


class _FakeRedmineSource:
    def __init__(self, records):
        self._records = records  # issue -> GlanceIssueRecord

    def read_issue(self, issue_id):
        if issue_id not in self._records:
            raise KeyError(issue_id)
        return self._records[issue_id]


class ActiveLaneSnapshotsTest(unittest.TestCase):
    """The default roster fold: Redmine grammar + advisory store + degraded/unknown."""

    def test_known_gate_folds_with_empty_store(self):
        src = _FakeRedmineSource(
            {
                "13425": GlanceIssueRecord(
                    issue_id="13425",
                    subject="impl lane",
                    issue_open=True,
                    journals=((("73980"), "## Gate: review_request\n- x"),),
                )
            }
        )
        collection = active_lane_snapshots([("13425", "lane_a")], redmine_source=src, store=None)
        rows = fold_glance_rows(collection.snapshots)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].workflow_state, "review_waiting")  # from Redmine, empty store
        self.assertEqual(rows[0].lane, "lane_a")
        self.assertFalse(collection.degraded)

    def test_unknown_template_is_degraded_row_not_dropped(self):
        src = _FakeRedmineSource(
            {
                "13480": GlanceIssueRecord(
                    issue_id="13480", journals=((("1"), "## Progress Log: hi"),)
                )
            }
        )
        collection = active_lane_snapshots([("13480", "lane_b")], redmine_source=src, store=None)
        rows = fold_glance_rows(collection.snapshots)
        self.assertEqual(len(rows), 1)  # NOT dropped
        self.assertEqual(rows[0].workflow_state, "unknown")
        self.assertTrue(collection.degraded)
        self.assertTrue(collection.notes)

    def test_source_unavailable_is_degraded_unknown(self):
        src = _FakeRedmineSource({})  # every read raises KeyError
        collection = active_lane_snapshots([("99999", "lane_c")], redmine_source=src, store=None)
        rows = fold_glance_rows(collection.snapshots)
        self.assertEqual(rows[0].workflow_state, "unknown")
        self.assertTrue(collection.degraded)

    def test_no_source_and_no_store_is_degraded_not_empty(self):
        collection = active_lane_snapshots([("13435", "lane_d")], redmine_source=None, store=None)
        rows = fold_glance_rows(collection.snapshots)
        self.assertEqual(len(rows), 1)  # a real active lane is always surfaced
        self.assertEqual(rows[0].workflow_state, "unknown")
        self.assertTrue(collection.degraded)

    def test_closed_issue_no_facts_is_degraded_unknown_not_retire(self):
        # re-audit j#74323 Finding 3: a closed issue with no recognized gate must NOT be
        # projected onto retire_ready; it is an explicit degraded unknown (verification owed).
        src = _FakeRedmineSource(
            {
                "9": GlanceIssueRecord(
                    issue_id="9", issue_open=False, journals=((("1"), "## Progress Log: hi"),)
                )
            }
        )
        collection = active_lane_snapshots([("9", "lane_e")], redmine_source=src, store=None)
        rows = fold_glance_rows(collection.snapshots)
        self.assertEqual(rows[0].workflow_state, "unknown")  # not retire_ready
        self.assertNotIn("retire", rows[0].next_action)
        self.assertTrue(collection.degraded)


class _BoomRepoRoot:
    def __fspath__(self):
        raise RuntimeError("boom")


class EnumerateActiveLanesTest(unittest.TestCase):
    """re-audit j#74323 Finding 2: a roster enumeration failure is not a silent healthy 0."""

    def test_enumeration_failure_is_signaled_not_silent_empty(self):
        lanes, error = enumerate_active_lanes(_BoomRepoRoot())
        self.assertEqual(lanes, ())
        self.assertIsNotNone(error)  # failure distinguished from success-empty
        self.assertIn("enumeration failed", error)


class LedgerAbsentIsNotCallbackFailureTest(unittest.TestCase):
    """Finding 2: a generic turn-start ``absent`` is not a callback delivery failure."""

    def test_absent_maps_to_turn_start_unconfirmed_not_callback(self):
        rec = _FakeLedgerRecord(journal_id="9", turn_start_outcome={"outcome": "absent"})
        obs = anomaly_from_ledger_record(rec)
        self.assertEqual(obs.anomaly, ANOMALY_TURN_START_UNCONFIRMED)  # not callback_delivery_failed

    def test_verbatim_callback_disposition_still_honoured(self):
        # callback_delivery_failed is still used when the record ITSELF evidences a callback.
        rec = _FakeLedgerRecord(disposition="callback_delivery_failed")
        self.assertEqual(anomaly_from_ledger_record(rec).anomaly, "callback_delivery_failed")


if __name__ == "__main__":
    unittest.main()
