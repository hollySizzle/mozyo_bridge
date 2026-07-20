"""Redmine #13952 — the producer template and the glance grammar share ONE contract.

The drift that motivated this issue: the packaged ``implementation_gateway`` role profile
told a same-lane reviewer to record a ``Review Result``, while the ``workflow glance`` grammar
only recognized ``review`` + an explicit ``結論:`` field. A durable, correct review
(``## Review Gate — 要修正``) was therefore invisible to the coordinator until someone
hand-added a pointer journal (#13910 j#81021 / j#81029 / j#81031).

The fix pins both sides to the literals the consumer exports
(:data:`CANONICAL_REVIEW_HEADING` / :data:`CANONICAL_REVIEW_CONCLUSION_LABEL` /
:data:`CANONICAL_REVIEW_CONCLUSION_TOKENS`). This regression drives the *packaged producer
template itself* — not a hand-written fixture — through the *consumer grammar*, so the two
cannot re-fork into separate literal allowlists without failing here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain import (
    role_profile as rp,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (
    CANONICAL_REVIEW_CONCLUSION_LABEL,
    CANONICAL_REVIEW_CONCLUSION_TOKENS,
    CANONICAL_REVIEW_HEADING,
    REVIEW_OUTCOME_BLOCKER,
    fold_issue_gate_facts,
    lane_signal_from_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    render_gate_note,
    render_workflow_event_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_REVIEW,
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    classify_lane_state,
)

#: The role whose durable output is a review (the same-lane reviewer of #13952).
_REVIEWER_ROLE = "implementation_gateway"

#: The role whose durable output carries the review-request / implementation-done anchors.
_WORKER_ROLE = "implementation_worker"


class ProducerTemplateMandatesTheConsumerContract(unittest.TestCase):
    """The packaged producer template names exactly the literals the grammar recognizes."""

    def setUp(self) -> None:
        self.templates = rp.ROLE_PROFILE_TEMPLATES

    def test_reviewer_template_pins_the_canonical_review_heading_and_label(self) -> None:
        body = self.templates[_REVIEWER_ROLE]
        self.assertIn(CANONICAL_REVIEW_HEADING, body)
        self.assertIn(CANONICAL_REVIEW_CONCLUSION_LABEL, body)

    def test_reviewer_template_offers_every_canonical_conclusion_token(self) -> None:
        # The Japanese / literal tokens a reviewer is told to write must each be a token the
        # grammar classifies — otherwise the producer could emit a conclusion the consumer
        # drops to ``pending``.
        body = self.templates[_REVIEWER_ROLE]
        for token in ("承認", "要修正", "blocker"):
            self.assertIn(token, body)
            self.assertIn(token, CANONICAL_REVIEW_CONCLUSION_TOKENS)

    def test_worker_template_pins_canonical_gate_tokens_not_reworded(self) -> None:
        # #13910 j#81068: a worker who reworded the gate token (``Review Request (R3)``) lost
        # the anchor. The template fixes the canonical literals.
        body = self.templates[_WORKER_ROLE]
        self.assertIn("## Gate: Implementation Done", body)
        self.assertIn("## Gate: Review Request", body)


class TemplateMandatedJournalsFoldThroughTheGrammar(unittest.TestCase):
    """A journal written per the packaged template projects the intended coordinator state."""

    def _fold(self, notes: str):
        facts = fold_issue_gate_facts([("81021", notes)])
        self.assertIsNotNone(facts, f"grammar failed to recognize template-shaped {notes!r}")
        return facts

    def test_each_canonical_conclusion_projects_its_outcome(self) -> None:
        # Build exactly what the reviewer template mandates: the canonical heading + the
        # canonical label + each canonical Japanese/literal token.
        expectations = {
            "承認": (REVIEW_APPROVED, "owner_waiting"),
            "要修正": (REVIEW_CHANGES_REQUESTED, "implementing"),
            "blocker": (None, "blocked"),  # blocker folds to blocker_recorded, not a conclusion
        }
        for token, (expected_conclusion, expected_state) in expectations.items():
            with self.subTest(token=token):
                notes = f"{CANONICAL_REVIEW_HEADING}\n- {CANONICAL_REVIEW_CONCLUSION_LABEL}: {token}"
                facts = self._fold(notes)
                self.assertEqual(facts.latest_gate, GATE_REVIEW)
                if expected_conclusion is not None:
                    self.assertEqual(facts.review_conclusion, expected_conclusion)
                else:
                    self.assertTrue(facts.blocker_recorded)
                state = classify_lane_state(lane_signal_from_gate_facts("13910", facts))
                self.assertEqual(state, expected_state)

    def test_blocker_token_maps_to_the_blocker_outcome_constant(self) -> None:
        # Guard the closed vocabulary: ``blocker`` is the out-of-conclusion outcome, so the
        # review vocabulary never grows a fourth REVIEW_CONCLUSIONS member.
        self.assertEqual(CANONICAL_REVIEW_CONCLUSION_TOKENS["blocker"], REVIEW_OUTCOME_BLOCKER)


class StructuredMarkerProducerConsumerContract(unittest.TestCase):
    """Redmine #13952 R3: the marker PRODUCER and the glance CONSUMER share ONE token grammar.

    The R2 fix pinned the heading / ``結論`` field literals. R3 adds the second drift the same
    issue kept hitting: a durable review recorded with the canonical structured marker
    (``[mozyo:workflow-event:gate=review_result:conclusion=…]``) but a reworded heading or a
    Markdown-emphasized / English-labelled body conclusion was dropped to ``pending`` — the
    coordinator saw "auditor review owed" for a review that had already concluded
    changes_requested (installed 0.12.2, j#83324: #13811 j#83313 / #13951 j#83311). The consumer
    now reads the marker as the unambiguous authority, and it reads it through the SAME
    :func:`render_workflow_event_marker` producer contract the watcher uses — so the two cannot
    re-fork. This drives the *producer renderer's own output* through the *glance grammar*.
    """

    def test_rendered_review_result_marker_folds_to_its_conclusion(self) -> None:
        # Each conclusion the producer can render round-trips through the consumer to the
        # coordinator state it means — pinned end to end (producer -> marker -> grammar -> state).
        cases = {
            "changes_requested": (REVIEW_CHANGES_REQUESTED, "implementing"),
            "approved": (REVIEW_APPROVED, "owner_waiting"),
        }
        for token, (expected_conclusion, expected_state) in cases.items():
            with self.subTest(conclusion=token):
                marker = render_workflow_event_marker(
                    "review_result",
                    conclusion=token,
                    target_head="a" * 40,
                    review_request_journal="83188",
                )
                facts = fold_issue_gate_facts([("83311", f"## Gate: Review\n{marker}")])
                self.assertIsNotNone(facts)
                self.assertEqual(facts.latest_gate, GATE_REVIEW)
                self.assertEqual(facts.review_conclusion, expected_conclusion)
                state = classify_lane_state(lane_signal_from_gate_facts("13952", facts))
                self.assertEqual(state, expected_state)

    def test_rendered_gate_note_survives_a_reworded_heading_and_bold_body(self) -> None:
        # The exact live shapes: the producer's gate note (prose body + embedded marker) folds
        # to worker even when the human body carries a reworded heading and a bold conclusion
        # value — the token, not the prose, is authoritative.
        body = (
            "## Gate: Review — project-gateway hibernate exact-generation fence (T1 R2)\n"
            "- conclusion: **changes_requested**"
        )
        note = render_gate_note(
            "review_result",
            body=body,
            conclusion="changes_requested",
            target_head="a" * 40,
            review_request_journal="83236",
        )
        facts = fold_issue_gate_facts([("83313", note)])
        self.assertEqual(facts.latest_gate, GATE_REVIEW)
        self.assertEqual(facts.review_conclusion, REVIEW_CHANGES_REQUESTED)
        self.assertEqual(
            classify_lane_state(lane_signal_from_gate_facts("13952", facts)), "implementing"
        )


if __name__ == "__main__":
    unittest.main()
