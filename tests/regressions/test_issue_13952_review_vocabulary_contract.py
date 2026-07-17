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

ROOT = Path(__file__).resolve().parents[1]
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


if __name__ == "__main__":
    unittest.main()
