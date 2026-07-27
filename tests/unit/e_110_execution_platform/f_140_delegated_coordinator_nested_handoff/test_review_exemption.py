"""Unit tests for the durable ``codex_direct_edit`` review exemption (#14539).

Pins :mod:`...domain.review_exemption` — the pure authority fact behind both halves of the
issue (the glance projection and the terminal-retire admission):

- the closed state vocabulary and the fail-closed direction (only a COMPLETE, valid gate with
  ``follow_up_review: false`` yields ``exempt``; everything else keeps the review owed);
- structural qualification before any field is read, so a bare exemption marker or a stray
  ``follow_up_review:`` line in an unrelated note is never authority;
- latest-wins with supersede-by-EXISTING, so a malformed newer gate shadows an older valid one;
- ``allowed_paths`` glob preservation (``**`` is a path, not Markdown emphasis);
- the terminal-retire evaluator's three conjunctive facts and their distinct reasons.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_exemption import (
    CANONICAL_DIRECT_EDIT_ROLE,
    EXEMPTION_EXEMPT,
    EXEMPTION_INVALID,
    EXEMPTION_NONE,
    EXEMPTION_REVIEW_REQUIRED,
    REASON_CLOSE_NOT_RECORDED,
    REASON_EXEMPTION_INVALID,
    REASON_FOLLOW_UP_REVIEW_REQUIRED,
    REASON_INTEGRATION_NOT_COMPLETE,
    REASON_NO_EXEMPTION_RECORDED,
    REVIEW_EXEMPTION_STATES,
    ReviewExemptionFacts,
    evaluate_exemption_integration_admissible,
    fold_review_exemption,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (
    REASON_OK,
)


def gate(
    *,
    role: str = CANONICAL_DIRECT_EDIT_ROLE,
    direct_edit: str = "true",
    allowed_paths: str = "vibes/docs/rules/**, .mozyo-bridge/docs/catalog.yaml",
    reason: str = "coordinator-owned policy docs",
    follow_up_review: str = "false",
    heading: str = "## Gate: codex_direct_edit",
) -> str:
    """The governed gate journal body, with one field overridable per test."""
    lines = [heading]
    if role is not None:
        lines.append(f"- role: {role}")
    if direct_edit is not None:
        lines.append(f"- direct_edit: {direct_edit}")
    if allowed_paths is not None:
        lines.append(f"- allowed_paths: {allowed_paths}")
    if reason is not None:
        lines.append(f"- reason: {reason}")
    if follow_up_review is not None:
        lines.append(f"- follow_up_review: {follow_up_review}")
    return "\n".join(lines) + "\n"


class ReviewExemptionFoldTests(unittest.TestCase):
    def test_valid_gate_with_follow_up_review_false_is_exempt(self):
        facts = fold_review_exemption([("101", gate())])
        self.assertEqual(facts.state, EXEMPTION_EXEMPT)
        self.assertTrue(facts.in_force)
        self.assertTrue(facts.recorded)
        self.assertEqual(facts.journal, "101")
        self.assertEqual(facts.reason, "coordinator-owned policy docs")

    def test_owner_required_follow_up_review_is_not_an_exemption(self):
        """Acceptance 4: ``follow_up_review: true`` keeps every existing fence armed."""
        facts = fold_review_exemption([("101", gate(follow_up_review="true"))])
        self.assertEqual(facts.state, EXEMPTION_REVIEW_REQUIRED)
        self.assertFalse(facts.in_force)

    def test_no_gate_journal_folds_to_none(self):
        facts = fold_review_exemption(
            [("100", "## Gate: Implementation Done\n- commit: abc1234")]
        )
        self.assertEqual(facts.state, EXEMPTION_NONE)
        self.assertFalse(facts.recorded)
        self.assertFalse(facts.in_force)

    def test_allowed_paths_preserves_glob_stars(self):
        """``**`` is a path glob, not Markdown emphasis — stripping it narrows the gate scope."""
        facts = fold_review_exemption(
            [("101", gate(allowed_paths="`src/**`, tests/**, docs/a.md"))]
        )
        self.assertEqual(facts.allowed_paths, ("src/**", "tests/**", "docs/a.md"))

    def test_marker_qualifies_the_journal_but_is_not_authority_on_its_own(self):
        """The implementation request's safety clause, literally: a bare marker never exempts."""
        facts = fold_review_exemption(
            [("101", "[mozyo:workflow-event:gate=codex_direct_edit]\nsome prose")]
        )
        self.assertEqual(facts.state, EXEMPTION_INVALID)
        self.assertFalse(facts.in_force)

    def test_marker_qualified_journal_with_complete_fields_is_exempt(self):
        body = "[mozyo:workflow-event:gate=codex_direct_edit]\n" + gate(heading="## Direct edit")
        self.assertEqual(fold_review_exemption([("101", body)]).state, EXEMPTION_EXEMPT)

    def test_stray_field_lines_without_structural_qualification_contribute_nothing(self):
        body = "## Progress Log\n- follow_up_review: false\n- direct_edit: true\n"
        self.assertEqual(fold_review_exemption([("101", body)]).state, EXEMPTION_NONE)

    def test_unfilled_template_follow_up_review_line_fails_closed(self):
        body = gate(follow_up_review="false (既定) | true (owner が独立 review を明示要求した場合)")
        self.assertEqual(fold_review_exemption([("101", body)]).state, EXEMPTION_INVALID)

    def test_each_missing_required_field_fails_closed(self):
        for field in ("role", "direct_edit", "allowed_paths", "reason", "follow_up_review"):
            with self.subTest(missing=field):
                facts = fold_review_exemption([("101", gate(**{field: None}))])
                self.assertEqual(facts.state, EXEMPTION_INVALID)
                self.assertFalse(facts.in_force)

    def test_non_canonical_role_fails_closed(self):
        self.assertEqual(
            fold_review_exemption([("101", gate(role="implementer"))]).state,
            EXEMPTION_INVALID,
        )

    def test_direct_edit_false_fails_closed(self):
        self.assertEqual(
            fold_review_exemption([("101", gate(direct_edit="false"))]).state,
            EXEMPTION_INVALID,
        )

    def test_latest_gate_wins(self):
        journals = [
            ("101", gate(follow_up_review="true")),
            ("205", gate(follow_up_review="false")),
        ]
        facts = fold_review_exemption(journals)
        self.assertEqual(facts.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.journal, "205")

    def test_a_newer_malformed_gate_shadows_an_older_valid_one(self):
        """Supersede-by-EXISTING (#13490 j#85365 F1 / #13952 F3), applied to the exemption.

        Skipping the malformed newer gate would leave the STALE older ``follow_up_review: false``
        exempting work the current record no longer covers.
        """
        journals = [("101", gate()), ("205", gate(reason=None))]
        facts = fold_review_exemption(journals)
        self.assertEqual(facts.state, EXEMPTION_INVALID)
        self.assertEqual(facts.journal, "205")
        self.assertFalse(facts.in_force)

    def test_unparseable_journal_ids_are_skipped(self):
        facts = fold_review_exemption([("not-a-journal", gate()), ("101", gate())])
        self.assertEqual(facts.journal, "101")

    def test_empty_history_is_total(self):
        self.assertEqual(fold_review_exemption([]).state, EXEMPTION_NONE)
        self.assertEqual(fold_review_exemption(()).state, EXEMPTION_NONE)

    def test_validated_coerces_out_of_vocabulary_state_to_invalid(self):
        coerced = ReviewExemptionFacts(state="approved").validated()
        self.assertEqual(coerced.state, EXEMPTION_INVALID)
        self.assertFalse(coerced.in_force)

    def test_every_declared_state_is_in_the_closed_vocabulary(self):
        for state in (
            EXEMPTION_NONE,
            EXEMPTION_EXEMPT,
            EXEMPTION_REVIEW_REQUIRED,
            EXEMPTION_INVALID,
        ):
            self.assertIn(state, REVIEW_EXEMPTION_STATES)


class ExemptionIntegrationAdmissibilityTests(unittest.TestCase):
    """The terminal-retire fence: exemption AND Close AND complete integration (all three)."""

    EXEMPT = ReviewExemptionFacts(state=EXEMPTION_EXEMPT, journal="101")

    def test_all_three_facts_admit(self):
        result = evaluate_exemption_integration_admissible(
            self.EXEMPT, close_recorded=True, integration_complete=True
        )
        self.assertTrue(result.admissible)
        self.assertEqual(result.reason, REASON_OK)

    def test_missing_close_blocks(self):
        result = evaluate_exemption_integration_admissible(
            self.EXEMPT, close_recorded=False, integration_complete=True
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_CLOSE_NOT_RECORDED)

    def test_incomplete_integration_blocks(self):
        result = evaluate_exemption_integration_admissible(
            self.EXEMPT, close_recorded=True, integration_complete=False
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_INTEGRATION_NOT_COMPLETE)

    def test_no_exemption_blocks(self):
        result = evaluate_exemption_integration_admissible(
            ReviewExemptionFacts(), close_recorded=True, integration_complete=True
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_NO_EXEMPTION_RECORDED)

    def test_invalid_exemption_blocks(self):
        result = evaluate_exemption_integration_admissible(
            ReviewExemptionFacts(state=EXEMPTION_INVALID, journal="101"),
            close_recorded=True,
            integration_complete=True,
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_EXEMPTION_INVALID)

    def test_owner_required_follow_up_review_blocks_this_route(self):
        """Acceptance 4: an owner-required review never passes through the exemption route."""
        result = evaluate_exemption_integration_admissible(
            ReviewExemptionFacts(state=EXEMPTION_REVIEW_REQUIRED, journal="101"),
            close_recorded=True,
            integration_complete=True,
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_FOLLOW_UP_REVIEW_REQUIRED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
