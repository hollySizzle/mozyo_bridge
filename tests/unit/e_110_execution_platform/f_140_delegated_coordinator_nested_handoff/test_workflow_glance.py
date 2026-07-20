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
    GATE_BLOCKED,
    GATE_CLOSE,
    GATE_REVIEW as _GATE_REVIEW,
    GATE_START,
    REVIEW_CHANGES_REQUESTED,
    REVIEW_PENDING,
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

    def test_bounded_dash_qualifiers_preserve_exact_gate_matching(self):
        live_implementation_requests = (
            "## Gate: Implementation Request — R6 partial-effect recovery correction",
            "## Gate: Implementation Request — receiver-side recovery idempotency",
            "## Gate: Implementation Request – scratch pair startup health",
        )
        for notes in live_implementation_requests:
            with self.subTest(notes=notes):
                facts = fold_issue_gate_facts([_j("100", notes)])
                self.assertEqual(facts.latest_gate, GATE_START)

        review = fold_issue_gate_facts(
            [_j("101", "## Gate: review — R10 approved\n- 結論: 承認")]
        )
        self.assertEqual(review.latest_gate, _GATE_REVIEW)
        self.assertEqual(review.review_conclusion, REVIEW_APPROVED)

        blocked = fold_issue_gate_facts([_j("102", "## Gate: blocked – credential unavailable")])
        self.assertEqual(blocked.latest_gate, GATE_BLOCKED)
        closed = fold_issue_gate_facts([_j("103", "## Gate: close — installed dogfood green")])
        self.assertEqual(closed.latest_gate, GATE_CLOSE)

    def test_bounded_dash_qualifier_does_not_enable_prefix_guessing_or_collisions(self):
        for notes in (
            "## Gate: Implementation Requester — not a governed lifecycle token",
            "## Gate: Review Finding Verdict — accepted",
            "## Gate: Design Consultation Answer – option A",
            "## Gate: review—R10",  # no bounded separator spaces
        ):
            with self.subTest(notes=notes):
                self.assertIsNone(fold_issue_gate_facts([_j("100", notes)]))

    def test_combined_heading_can_carry_a_qualified_final_part(self):
        facts = fold_issue_gate_facts(
            [
                _j(
                    "100",
                    "## Gate: Implementation Done + Review Request — R6\n"
                    "- commit: `abc1234`",
                )
            ]
        )
        self.assertEqual(facts.latest_gate, GATE_REVIEW_REQUEST)
        self.assertTrue(facts.commit_bearing)

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

    # -- Redmine #13952: the reviewer's durable vocabulary ------------------------------

    def test_suffixed_review_gate_heading_is_the_review_gate(self):
        # #13910 j#81021 verbatim: the same-lane reviewer's durable changes-requested review.
        # It carries no 結論: field — the bounded qualifier IS the conclusion.
        facts = fold_issue_gate_facts([_j("81021", "## Review Gate — 要修正\n\n- finding_1: ...")])
        self.assertEqual(facts.latest_gate, _GATE_REVIEW)
        self.assertEqual(facts.review_conclusion, REVIEW_CHANGES_REQUESTED)
        self.assertEqual(facts.latest_gate_journal, "81021")

    def test_qualified_review_result_heading_is_the_review_gate(self):
        # #13910 j#81029 verbatim: the prefixed shape with the reviewer's `Review Result`
        # wording and an English conclusion token in the qualifier.
        facts = fold_issue_gate_facts(
            [_j("81029", "## Gate: Review Result — changes_requested\n\n- target_commit: `7f67ae6b`")]
        )
        self.assertEqual(facts.latest_gate, _GATE_REVIEW)
        self.assertEqual(facts.review_conclusion, REVIEW_CHANGES_REQUESTED)

    def test_review_outcomes_project_to_their_lane_state_and_next_owner(self):
        # The four outcomes the coordinator reads off a durable review, pinned end-to-end
        # (grammar -> signal -> lane state -> next owner). `re-review required` is not a
        # separate outcome: it is 要修正 plus the template's own 再review要否 field.
        cases = (
            ("approved", "## Gate: Review\n- 結論: 承認", "owner_waiting", OWNER_COORDINATOR),
            ("changes_requested", "## Gate: Review\n- 結論: 要修正", "implementing", OWNER_WORKER),
            ("blocker", "## Gate: Review\n- 結論: blocker (remote_verification 不能)", "blocked", OWNER_COORDINATOR),
            (
                "re-review required",
                "## Gate: Review\n- 再review要否: 要\n- 結論: 要修正",
                "implementing",
                OWNER_WORKER,
            ),
        )
        for label, notes, expected_state, expected_owner in cases:
            with self.subTest(outcome=label):
                facts = fold_issue_gate_facts([_j("81021", notes)])
                self.assertEqual(facts.latest_gate, _GATE_REVIEW)
                row = fold_glance_row(
                    IssueGlanceSnapshot(
                        issue_id="13910",
                        signal=lane_signal_from_gate_facts("13910", facts),
                        latest_gate_journal=facts.latest_gate_journal,
                        delivery=DeliveryObservation(),
                    )
                )
                self.assertEqual(row.workflow_state, expected_state)
                self.assertEqual(row.next_owner, expected_owner)

    def test_concluded_blocker_review_is_a_blocker_not_an_audit_still_owed(self):
        # A review that concluded `blocker` has *happened*; folding it to pending would read
        # as "audit owed" and dispatch past a lane that cannot proceed.
        facts = fold_issue_gate_facts([_j("100", "## Gate: Review\n- 結論: blocker")])
        self.assertTrue(facts.blocker_recorded)
        self.assertEqual(classify_lane_state(lane_signal_from_gate_facts("7", facts)), "blocked")

    def test_review_qualifier_without_a_vocabulary_token_stays_pending(self):
        # A topic-only qualifier asserts no conclusion: the audit is still owed (fail-closed),
        # never guessed from the surrounding words.
        facts = fold_issue_gate_facts([_j("100", "## Gate: Review — R6 partial recovery")])
        self.assertEqual(facts.latest_gate, _GATE_REVIEW)
        self.assertEqual(facts.review_conclusion, REVIEW_PENDING)
        self.assertEqual(classify_lane_state(lane_signal_from_gate_facts("7", facts)), "review_waiting")

    def test_explicit_conclusion_field_outranks_the_heading_qualifier(self):
        # The canonical field is the contract; the qualifier only stands in when it is absent.
        facts = fold_issue_gate_facts([_j("100", "## Gate: Review Result — changes_requested\n- 結論: 承認")])
        self.assertEqual(facts.review_conclusion, REVIEW_APPROVED)

    def test_conclusion_is_exact_not_substring_so_prose_and_negations_stay_pending(self):
        # #13952 j#81089 F1: a substring match promoted prose and reversed negations. The
        # conclusion is now an anchored exact-match, so none of these classify — the audit is
        # still owed (fail-closed), never a fabricated approved / changes_requested.
        for notes in (
            "## Gate: Review — needs owner clarification",
            "## Gate: Review — approved wording pending",
            "## Gate: Review\n- 結論: not approved",
            "## Gate: Review\n- 結論: changes not requested",
        ):
            with self.subTest(notes=notes):
                facts = fold_issue_gate_facts([_j("100", notes)])
                self.assertEqual(facts.latest_gate, _GATE_REVIEW)
                self.assertEqual(facts.review_conclusion, REVIEW_PENDING)
                self.assertFalse(facts.blocker_recorded)
                self.assertEqual(
                    classify_lane_state(lane_signal_from_gate_facts("7", facts)), "review_waiting"
                )

    def test_conclusion_tolerates_only_a_trailing_parenthetical_qualifier(self):
        # The one structural qualifier allowed: a governed reviewer's `要修正 (再review 要)` /
        # `blocker (…)` still reads because the trailing `(...)` is stripped before the
        # exact-match — while `要修正 、詳細は…` (prose, no parenthetical) stays pending.
        approved = fold_issue_gate_facts([_j("100", "## Gate: Review\n- 結論: 要修正 (再review 要)")])
        self.assertEqual(approved.review_conclusion, REVIEW_CHANGES_REQUESTED)
        blocker = fold_issue_gate_facts([_j("100", "## Gate: Review\n- 結論: blocker (remote_verification 不能)")])
        self.assertTrue(blocker.blocker_recorded)
        prose = fold_issue_gate_facts([_j("100", "## Gate: Review\n- 結論: 要修正 、詳細は本文")])
        self.assertEqual(prose.review_conclusion, REVIEW_PENDING)

    def test_round_qualifier_before_a_dash_does_not_lose_the_gate(self):
        # #13910 j#81068 (evidence j#81073 / correction j#81076): the round parenthetical sits
        # BEFORE the dash, so the title-level trailing-paren normalization never reached it and
        # the review_request anchor was lost. The same normalization now applies to the
        # dash-split left token.
        facts = fold_issue_gate_facts([_j("81068", "## Gate: Review Request (R3) — correction completed")])
        self.assertEqual(facts.latest_gate, GATE_REVIEW_REQUEST)
        self.assertEqual(facts.latest_gate_journal, "81068")

    def test_round_qualifier_normalization_is_not_a_broad_alias(self):
        # The correction re-applies an existing normalization; it must not become prefix
        # matching or a `review request (r3)` alias. Both of these normalize to non-entries.
        for notes in (
            "## Gate: Review Request candidate (R3) — ...",
            "## Gate: Review Request R3 — ...",
        ):
            with self.subTest(notes=notes):
                self.assertIsNone(fold_issue_gate_facts([_j("100", notes)]))

    def test_suffixed_shape_stays_fail_closed_against_prose_and_collisions(self):
        # The suffixed shape is the widest surface added here, so it carries the widest
        # negative set: trailing prose breaks the shape; a non-entry token breaks the
        # allowlist; an excluded collision stays excluded in this shape too.
        for notes in (
            "## Review Gate approval を待つ",  # trailing prose -> not the governed shape
            "## Sublane 完了 guardrail",
            "## Review Finding Verdict Gate",  # collision exclusion holds in the suffixed shape
            "## Gate Schema",
            "## そのうち Gate を通す",
            "## Progress Log — worker alive",
        ):
            with self.subTest(notes=notes):
                self.assertIsNone(fold_issue_gate_facts([_j("100", notes)]))


#: Two full 40-hex commit heads (Review Generation Marker Contract v2 identity).
_HEAD = "6109b1573ec192cf67e596e24831b6524f4c40bf"
_HEAD2 = "aa6a150f74329732e99af78ea193e27a78dc01f4"
_REQ_J = "90"  # a review_request journal id (< the review_result journal id 100)
_RES_J = "100"


def _request_journal(*, head=_HEAD, jid=_REQ_J, heading="## Gate: Review Request"):
    """A canonical review_request journal (carries the round's pinned head)."""
    return _j(jid, f"{heading}\n[mozyo:workflow-event:gate=review_request:head={head}]")


def _result_journal(
    conclusion=None, *, head=_HEAD, req=_REQ_J, jid=_RES_J, heading="## Gate: Review", body="", blocker=False
):
    """A review_result journal marker (defaults to a shape that is canonical against `_request_journal`)."""
    fields = ["gate=review_result"]
    if conclusion is not None:
        fields.append(f"conclusion={conclusion}")
    if blocker:
        fields.append("blocker=1")
    if head is not None:
        fields.append(f"head={head}")
    if req is not None:
        fields.append(f"req={req}")
    marker = "[mozyo:workflow-event:" + ":".join(fields) + "]"
    note = heading + (("\n" + body) if body else "") + "\n" + marker
    return _j(jid, note)


def _review(conclusion=None, **result_kw):
    """A canonical review pair: the review_request journal + a review_result journal answering it."""
    head = result_kw.pop("head", _HEAD)
    return [_request_journal(head=head), _result_journal(conclusion, head=head, **result_kw)]


class StructuredMarkerAuthorityTest(unittest.TestCase):
    """Redmine #13952 R3/R4: the generation-correlated ``gate=review_result`` marker authority.

    Live evidence (installed 0.12.2, j#83324): #13811 j#83313 and #13951 j#83311 both carried a
    full-head ``gate=review_result:conclusion=changes_requested`` marker but fell to
    ``review_waiting`` / "auditor review owed", while #13884 j#83307 projected ``implementing`` /
    worker — because the conclusion was read only from a plain ``結論:`` field / heading qualifier
    (a Markdown-emphasized value or an English ``conclusion:`` label was dropped to ``pending``).
    The structured marker is now the authority, but ONLY when it EXACT-CORRELATES to the review
    round it answers (its ``req`` = the correlated review_request journal, its full head = that
    request's head, explicit conclusion). A malformed / uncorrelated marker fails closed to
    ``pending`` yet still shadows an older review (reviews j#83388 F1/F2, j#83422 F3/F4).
    """

    def _fold(self, journals):
        facts = fold_issue_gate_facts(journals)
        if facts is None:
            return None, None, None
        row = fold_glance_row(
            IssueGlanceSnapshot(
                issue_id="13",
                signal=lane_signal_from_gate_facts("13", facts),
                latest_gate_journal=facts.latest_gate_journal,
            )
        )
        return facts, row.workflow_state, row.next_owner

    # -- the three live j#83324 shapes (each carries a canonical, correlated marker) -----

    def test_marker_recovers_conclusion_when_the_field_carries_markdown_emphasis(self):
        # #13951 j#83311 verbatim shape: the ``結論`` value is bold, so the exact-match field
        # read drops it — but the correlated marker is authoritative, so it folds to worker.
        h = "7e535672b01c5a188846a10d84511c68ec386e4b"
        journals = [
            _request_journal(head=h, jid="83188"),
            _result_journal(
                "changes_requested", head=h, req="83188", jid="83311",
                heading="## Gate: Review — public callback lease recovery rail R1",
                body="- 再review要否: true\n- 結論: **changes_requested**",
            ),
        ]
        facts, state, owner = self._fold(journals)
        self.assertEqual(facts.latest_gate, _GATE_REVIEW)
        self.assertEqual(facts.review_conclusion, REVIEW_CHANGES_REQUESTED)
        self.assertEqual((state, owner), ("implementing", OWNER_WORKER))

    def test_marker_recovers_conclusion_when_the_label_is_english_and_bold(self):
        # #13811 j#83313 verbatim shape: an English ``conclusion:`` label (not ``結論``) + bold
        # value + a topic-only heading qualifier — none read by the body grammar; the marker does.
        journals = [
            _request_journal(head=_HEAD2, jid="83236"),
            _result_journal(
                "changes_requested", head=_HEAD2, req="83236", jid="83313",
                heading="## Gate: Review — project-gateway hibernate exact-generation fence (T1 R2)",
                body="- review_request: j#83236\n- conclusion: **changes_requested**",
            ),
        ]
        facts, state, owner = self._fold(journals)
        self.assertEqual((facts.review_conclusion, state, owner), (REVIEW_CHANGES_REQUESTED, "implementing", OWNER_WORKER))

    def test_plain_field_shape_is_unchanged(self):
        # #13884 j#83307 verbatim shape: a plain ``結論: 要修正`` field already worked; the marker
        # agrees, so the projection is unchanged (no regression on the shape that was correct).
        facts, state, owner = self._fold(_review("changes_requested", body="- 結論: 要修正"))
        self.assertEqual((state, owner), ("implementing", OWNER_WORKER))

    # -- authority + robustness (canonical, correlated markers) --------------------------

    def test_marker_conclusion_outranks_a_disagreeing_field(self):
        # A correlated marker is the machine authority: it wins over the body field on disagreement.
        facts, state, _ = self._fold(_review("changes_requested", body="- 結論: 承認"))
        self.assertEqual((facts.review_conclusion, state), (REVIEW_CHANGES_REQUESTED, "implementing"))

    def test_marker_position_and_trailing_placement_do_not_lose_it(self):
        # The marker is read wherever it sits in the review_result note (mid-body or trailing).
        journals = [
            _request_journal(),
            _result_journal(
                "changes_requested",
                heading="## Gate: Review",
                body="### Findings\n- F1 ...\n- F2 ...",
            ),
        ]
        facts, state, _ = self._fold(journals)
        self.assertEqual((facts.review_conclusion, state), (REVIEW_CHANGES_REQUESTED, "implementing"))

    def test_approved_and_blocker_markers_project_their_outcomes(self):
        approved, state_a, owner_a = self._fold(_review("approved"))
        self.assertEqual((approved.review_conclusion, state_a, owner_a), ("approved", "owner_waiting", OWNER_COORDINATOR))
        blocker, state_b, owner_b = self._fold(_review("changes_requested", blocker=True))
        self.assertTrue(blocker.blocker_recorded)
        self.assertEqual((state_b, owner_b), ("blocked", OWNER_COORDINATOR))

    def test_canonical_marker_establishes_the_review_gate_without_a_gate_heading(self):
        # A reworded review_result heading still folds, because the correlated marker independently
        # establishes the review gate.
        facts, state, owner = self._fold(
            _review("changes_requested", heading="## Durable review note (reworded heading)")
        )
        self.assertEqual((facts.latest_gate, state, owner), (_GATE_REVIEW, "implementing", OWNER_WORKER))

    # -- fail-closed: body fallback + shape identity (reviews j#83388 F1/F2) --------------

    def test_malformed_conclusion_marker_does_not_fall_back_to_the_body(self):
        # F1: a review_result marker with an out-of-vocabulary conclusion must NOT let the body
        # ``結論: 承認`` promote the lane — the marker's presence forbids the fallback.
        facts, state, owner = self._fold(_review("bogus", body="- 結論: 承認"))
        self.assertEqual((facts.review_conclusion, state, owner), (REVIEW_PENDING, "review_waiting", OWNER_AUDITOR))

    def test_marker_missing_head_is_not_authoritative(self):
        # F2: no head -> shape identity fails -> shadow (pending) even with a correlated request.
        facts, state, _ = self._fold(_review("changes_requested", head=None))
        self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    def test_marker_abbreviated_or_upper_head_is_not_authoritative(self):
        # F2: a truncated / upper-case head is not a full commit head (v2 identity fails closed).
        for bad in ("abc123", _HEAD.upper(), _HEAD[:39], _HEAD + "0"):
            with self.subTest(head=bad):
                facts, state, _ = self._fold(_review("approved", head=bad))
                self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    def test_marker_missing_or_nonnumeric_req_is_not_authoritative(self):
        # F2/F4: the declared req must be a non-blank numeric id AND correlate to a real request.
        for bad in (None, "x", "j83188", ""):
            with self.subTest(req=bad):
                facts, state, _ = self._fold(
                    [_request_journal(), _result_journal("changes_requested", req=bad)]
                )
                self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    def test_canonical_marker_without_a_conclusion_is_audit_owed(self):
        # A correlated review whose marker speaks no conclusion (and no field) is fail-closed to
        # pending — the audit is still owed, never guessed.
        facts, state, owner = self._fold(_review(None))
        self.assertEqual((facts.review_conclusion, state, owner), (REVIEW_PENDING, "review_waiting", OWNER_AUDITOR))

    def test_conflicting_markers_on_one_journal_fail_closed(self):
        # Two review_result markers on one journal disagree -> ambiguous -> pending.
        journals = [
            _request_journal(),
            _j(
                _RES_J,
                "## Gate: Review\n"
                f"[mozyo:workflow-event:gate=review_result:conclusion=approved:head={_HEAD}:req={_REQ_J}]\n"
                f"[mozyo:workflow-event:gate=review_result:conclusion=changes_requested:head={_HEAD}:req={_REQ_J}]",
            ),
        ]
        facts, state, _ = self._fold(journals)
        self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    # -- fail-closed: review-generation correlation (review j#83422 F3/F4) ----------------

    def test_head_drift_from_the_request_is_not_authoritative(self):
        # F4: the review_result head must EQUAL the review_request head it answers.
        facts, state, _ = self._fold(
            [_request_journal(head=_HEAD2), _result_journal("approved", head=_HEAD, req=_REQ_J)]
        )
        self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    def test_nonexistent_or_zero_req_is_not_authoritative(self):
        # F4: a req pointing at no real review_request journal (or ``0``) correlates to nothing.
        for bad_req in ("999", "0"):
            with self.subTest(req=bad_req):
                facts, state, _ = self._fold(
                    [_request_journal(), _result_journal("approved", req=bad_req)]
                )
                self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    def test_uncorrelated_result_without_a_preceding_request_is_not_authoritative(self):
        # F4: a review_result with no preceding review_request is an uncorrelated outcome -> pending.
        facts, state, _ = self._fold([_result_journal("approved")])
        self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    def test_newer_malformed_review_shadows_an_older_approved(self):
        # F3: the durable-history regression. j90 request, j100 canonical approved, j101 a newer
        # reworded + malformed review. j101 must still count (shadow) so the old approved does NOT
        # re-surface as the latest authority.
        journals = [
            _request_journal(jid="90"),
            _result_journal("approved", head=_HEAD, req="90", jid="100"),
            _result_journal(
                "bogus", head=_HEAD, req="90", jid="101", heading="## Durable review reworded"
            ),
        ]
        facts, state, _ = self._fold(journals)
        self.assertEqual(facts.latest_gate_journal, "101")  # the newer bad review is the latest
        self.assertEqual((facts.review_conclusion, state), (REVIEW_PENDING, "review_waiting"))

    def test_newer_valid_review_supersedes_an_older_one(self):
        # The healthy counterpart: a newer CANONICAL review wins over an older canonical review.
        journals = [
            _request_journal(head=_HEAD, jid="90"),
            _result_journal("changes_requested", head=_HEAD, req="90", jid="100"),
            _request_journal(head=_HEAD2, jid="110"),
            _result_journal("approved", head=_HEAD2, req="110", jid="120"),
        ]
        facts, state, owner = self._fold(journals)
        self.assertEqual(facts.latest_gate_journal, "120")
        self.assertEqual((facts.review_conclusion, state, owner), ("approved", "owner_waiting", OWNER_COORDINATOR))

    # -- fail-closed: the handoff channel is a notification, not truth (review j#83467 F5) -

    def _handoff(self, kind, *, journal="100", jid="101"):
        # A delivery-notification note on the ``handoff`` channel (a pointer, never durable truth).
        marker = f"[mozyo:handoff:source=redmine:issue=13952:journal={journal}:kind={kind}:to=claude]"
        return _j(jid, f"## Handoff delivery record\n{marker}")

    def test_handoff_review_result_notification_does_not_shadow_the_truth(self):
        # F5a: a NEWER handoff kind=review_result delivery note must not become a review and shadow
        # the real approved result — the handoff channel is a pointer, not the durable record.
        journals = [
            _request_journal(jid="90"),
            _result_journal("approved", jid="100"),
            self._handoff("review_result", journal="100", jid="101"),
        ]
        facts, state, owner = self._fold(journals)
        self.assertEqual(facts.latest_gate_journal, "100")
        self.assertEqual((facts.review_conclusion, state, owner), ("approved", "owner_waiting", OWNER_COORDINATOR))

    def test_handoff_review_request_notification_does_not_break_correlation(self):
        # F5b: a handoff kind=review_request delivery note between the real request and result must
        # not be treated as a competing review_request that breaks the result's correlation.
        journals = [
            _request_journal(jid="90"),
            self._handoff("review_request", journal="90", jid="95"),
            _result_journal("approved", req="90", jid="100"),
        ]
        facts, state, owner = self._fold(journals)
        self.assertEqual((facts.review_conclusion, state, owner), ("approved", "owner_waiting", OWNER_COORDINATOR))

    # -- round supersession: a newer review_request marker (review j#83467 F6) ------------

    def test_newer_marker_only_review_request_supersedes_an_older_result(self):
        # F6: a newer canonical review_request under a reworded (non-gate) heading must still make
        # its journal a recognized review_request gate, so an older approved result goes stale.
        journals = [
            _request_journal(head=_HEAD, jid="90"),
            _result_journal("approved", head=_HEAD, req="90", jid="100"),
            _j("110", f"## Durable re-review note\n[mozyo:workflow-event:gate=review_request:head={_HEAD2}]"),
        ]
        facts, state, owner = self._fold(journals)
        self.assertEqual(facts.latest_gate_journal, "110")
        self.assertEqual((facts.latest_gate, state, owner), (GATE_REVIEW_REQUEST, "review_waiting", OWNER_AUDITOR))

    def test_newer_malformed_review_request_still_supersedes(self):
        # F6: even a head-less / malformed newer review_request means the round restarted — the old
        # result is stale, so it must not stay the authority (fail-closed toward re-review).
        journals = [
            _request_journal(head=_HEAD, jid="90"),
            _result_journal("approved", head=_HEAD, req="90", jid="100"),
            _j("110", "## Durable re-review\n[mozyo:workflow-event:gate=review_request]"),
        ]
        facts, state, _ = self._fold(journals)
        self.assertEqual(facts.latest_gate_journal, "110")
        self.assertEqual(state, "review_waiting")

    # -- non-review markers ---------------------------------------------------------------

    def test_review_finding_verdict_marker_is_not_an_audit_review(self):
        # The implementer's verdict marker is not gate-bearing: it must never become a review.
        facts = fold_issue_gate_facts(
            [_j("100", "## Gate: Review Finding Verdict — R1\n[mozyo:workflow-event:gate=review_finding_verdict]")]
        )
        self.assertIsNone(facts)

    def test_dispatch_marker_does_not_become_a_review(self):
        # An implementation_request dispatch marker (``kind=`` field) is not a review_result here.
        facts = fold_issue_gate_facts(
            [_j("100", "## Gate: Implementation Request\n[mozyo:workflow-event:kind=implementation_request:lane=x:lane_generation=1]")]
        )
        self.assertEqual(facts.latest_gate, GATE_START)  # from the heading, not a review


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
