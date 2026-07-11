"""Callback gate classification tests (Redmine #13520 / US #13518, design answer j#75098 Q4).

Pins the fail-closed exact-journal classifier: the notification is a pointer, the source
journal's structured marker is the authority.

- a single gate marker on the exact journal -> classified, adopting that normalized gate;
- a notification kind that disagrees with the journal marker -> ``mismatch`` (journal wins),
  and the ``review_result`` -> ``review`` alias does not raise a false mismatch;
- no marker on the journal -> unclassified ``gate_marker_missing``;
- two distinct gates on one journal -> unclassified ``gate_marker_ambiguous``;
- a marker anchored to a different issue -> unclassified ``issue_journal_mismatch``;
- an unclassified journal never carries a normalized gate (nothing is delivered on a guess).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    CLASSIFY_CLASSIFIED,
    CLASSIFY_UNCLASSIFIED,
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
    UNCLASSIFIED_GATE_MARKER_AMBIGUOUS,
    UNCLASSIFIED_GATE_MARKER_MISSING,
    UNCLASSIFIED_ISSUE_JOURNAL_MISMATCH,
    classify_callback_gate,
    normalize_gate_name,
    send_outcome_for_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    build_marker,
)


class NormalizeGateNameTest(unittest.TestCase):
    def test_review_result_aliases_to_review(self):
        self.assertEqual(normalize_gate_name("review_result"), "review")

    def test_unlisted_gate_passes_through(self):
        self.assertEqual(normalize_gate_name("implementation_done"), "implementation_done")


class ClassifyCallbackGateTest(unittest.TestCase):
    def test_single_marker_is_classified_and_adopts_gate(self):
        markers = [build_marker("13518", "75094", "implementation_done")]
        c = classify_callback_gate(markers, "13518", "75094", notification_kind="implementation_done")
        self.assertEqual(c.disposition, CLASSIFY_CLASSIFIED)
        self.assertEqual(c.normalized_gate, "implementation_done")
        self.assertFalse(c.mismatch)

    def test_notification_disagreeing_with_journal_is_a_mismatch_journal_wins(self):
        markers = [build_marker("13518", "75094", "implementation_done")]
        c = classify_callback_gate(markers, "13518", "75094", notification_kind="review_request")
        self.assertEqual(c.disposition, CLASSIFY_CLASSIFIED)
        self.assertEqual(c.normalized_gate, "implementation_done")  # journal wins
        self.assertTrue(c.mismatch)

    def test_review_result_alias_is_not_a_false_mismatch(self):
        # Journal marker names review_result (normalizes to review); notification says the same.
        markers = [build_marker("13518", "75094", "review_result")]
        c = classify_callback_gate(markers, "13518", "75094", notification_kind="review_result")
        self.assertEqual(c.normalized_gate, "review")
        self.assertFalse(c.mismatch)

    def test_no_marker_on_journal_is_unclassified_missing(self):
        markers = [build_marker("13518", "75000", "implementation_done")]
        c = classify_callback_gate(markers, "13518", "75094")
        self.assertEqual(c.disposition, CLASSIFY_UNCLASSIFIED)
        self.assertEqual(c.reason, UNCLASSIFIED_GATE_MARKER_MISSING)
        self.assertEqual(c.normalized_gate, "")

    def test_two_distinct_gates_on_one_journal_is_ambiguous(self):
        markers = [
            build_marker("13518", "75094", "implementation_done"),
            build_marker("13518", "75094", "review_request"),
        ]
        c = classify_callback_gate(markers, "13518", "75094")
        self.assertEqual(c.disposition, CLASSIFY_UNCLASSIFIED)
        self.assertEqual(c.reason, UNCLASSIFIED_GATE_MARKER_AMBIGUOUS)

    def test_same_gate_twice_on_one_journal_is_not_ambiguous(self):
        markers = [
            build_marker("13518", "75094", "implementation_done"),
            build_marker("13518", "75094", "implementation_done"),
        ]
        c = classify_callback_gate(markers, "13518", "75094")
        self.assertEqual(c.disposition, CLASSIFY_CLASSIFIED)
        self.assertEqual(c.normalized_gate, "implementation_done")

    def test_marker_for_a_different_issue_is_issue_journal_mismatch(self):
        markers = [build_marker("99999", "75094", "implementation_done")]
        c = classify_callback_gate(markers, "13518", "75094")
        self.assertEqual(c.disposition, CLASSIFY_UNCLASSIFIED)
        self.assertEqual(c.reason, UNCLASSIFIED_ISSUE_JOURNAL_MISMATCH)

    def test_empty_markers_is_unclassified_missing(self):
        c = classify_callback_gate([], "13518", "75094")
        self.assertEqual(c.reason, UNCLASSIFIED_GATE_MARKER_MISSING)


class SendOutcomeForDeliveryTest(unittest.TestCase):
    """The conservative DeliveryOutcome -> send-outcome mapping (only positive turn-start -> delivered)."""

    def test_sent_ok_is_delivered(self):
        self.assertEqual(send_outcome_for_delivery("sent", "ok"), SEND_DELIVERED)

    def test_sent_queue_enter_is_delivered(self):
        self.assertEqual(send_outcome_for_delivery("sent", "queue_enter"), SEND_DELIVERED)

    def test_deterministic_pre_injection_blocks_are_not_sent(self):
        for reason in (
            "target_unavailable",
            "target_not_agent",
            "invalid_anchor",
            "invalid_args",
            "receiver_blocked",
            "turn_start_absent",
            "precondition_not_idle",
            "cross_session_claude",
            "target_repo_mismatch",
            "gateway_route_blocked",
            "main_lane_implementation_blocked",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(send_outcome_for_delivery("blocked", reason), SEND_NOT_SENT)

    def test_ambiguous_blocks_are_uncertain(self):
        for reason in ("marker_timeout", "turn_start_unconfirmed", "inject_failed"):
            with self.subTest(reason=reason):
                self.assertEqual(send_outcome_for_delivery("blocked", reason), SEND_UNCERTAIN)

    def test_unknown_status_or_reason_defaults_to_uncertain(self):
        self.assertEqual(send_outcome_for_delivery("pending_input", "ok"), SEND_UNCERTAIN)
        self.assertEqual(send_outcome_for_delivery("sent", "surprise"), SEND_UNCERTAIN)
        self.assertEqual(send_outcome_for_delivery("weird", "weird"), SEND_UNCERTAIN)


if __name__ == "__main__":
    unittest.main()
