"""Unit tests for the durable ``codex_direct_edit`` review exemption (#14539).

Pins :mod:`...domain.review_exemption` — the pure authority fact behind both halves of the
issue (the glance projection and the terminal-retire admission):

- the closed state vocabulary and the fail-closed direction (only a COMPLETE, valid gate with
  ``follow_up_review: false`` yields ``exempt``; everything else keeps the review owed);
- structural qualification before any field is read, so a bare exemption marker or a stray
  ``follow_up_review:`` line in an unrelated note is never authority;
- latest-wins with supersede-by-EXISTING, so a malformed newer gate shadows an older valid one;
- ``allowed_paths`` glob preservation (``**`` is a path, not Markdown emphasis);
- **path coverage** (review j#90137 F1): the preset's exemption condition is a gate covering the
  target commit's WHOLE changed-path set, so a non-empty ``allowed_paths`` is not enough;
- the terminal-retire evaluator's conjunctive facts and their distinct reasons, including the
  supersession fact the retire shares with the glance (review j#90137 F3).
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
    EXEMPTION_PATH_COVERAGE_UNPROVEN,
    EXEMPTION_REVIEW_REQUIRED,
    REASON_CLOSE_NOT_RECORDED,
    REASON_EXEMPTION_INVALID,
    REASON_EXEMPTION_SUPERSEDED,
    REASON_FOLLOW_UP_REVIEW_REQUIRED,
    REASON_INTEGRATION_NOT_COMPLETE,
    REASON_NO_EXEMPTION_RECORDED,
    REASON_PATH_COVERAGE_UNPROVEN,
    REVIEW_EXEMPTION_STATES,
    ReviewExemptionFacts,
    evaluate_exemption_integration_admissible,
    fold_declared_change_scope,
    fold_review_exemption,
    uncovered_paths,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (
    REASON_OK,
)


HEAD = "4f2b9c1a3d5e6f708192a3b4c5d6e7f809a1b2c3"

#: The paths :func:`scope` declares as the target commit's changed set.
SCOPE_PATHS = ("vibes/docs/rules/agent-workflow.md", ".mozyo-bridge/docs/catalog.yaml")


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


def scope(*paths: str, commit: str = HEAD) -> str:
    """A governed ``implementation_done`` journal declaring the target commit + changed paths.

    Written in the CONTINUATION-list shape the templates actually produce (``- changed_paths:``
    then indented items) — the shape this issue's own review_request j#89842 used.
    """
    items = paths or SCOPE_PATHS
    lines = ["## Gate: Implementation Done", f"- commit: {commit}", "- changed_paths:"]
    lines.extend(f"  - `{p}`" for p in items)
    return "\n".join(lines) + "\n"


def exempt_history(**over):
    """A gate journal plus the change-scope evidence its coverage is checked against."""
    return [("101", gate(**over)), ("102", scope())]


class ReviewExemptionFoldTests(unittest.TestCase):
    def test_valid_gate_with_follow_up_review_false_is_exempt(self):
        facts = fold_review_exemption(exempt_history())
        self.assertEqual(facts.state, EXEMPTION_EXEMPT)
        self.assertTrue(facts.in_force)
        self.assertTrue(facts.recorded)
        self.assertEqual(facts.journal, "101")
        self.assertEqual(facts.reason, "coordinator-owned policy docs")

    def test_owner_required_follow_up_review_is_not_an_exemption(self):
        """Acceptance 4: ``follow_up_review: true`` keeps every existing fence armed."""
        facts = fold_review_exemption(exempt_history(follow_up_review="true"))
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
        self.assertEqual(
            fold_review_exemption([("101", body), ("102", scope())]).state, EXEMPTION_EXEMPT
        )

    def test_stray_field_lines_without_structural_qualification_contribute_nothing(self):
        body = "## Progress Log\n- follow_up_review: false\n- direct_edit: true\n"
        self.assertEqual(fold_review_exemption([("101", body)]).state, EXEMPTION_NONE)

    def test_unfilled_template_follow_up_review_line_fails_closed(self):
        body = gate(follow_up_review="false (既定) | true (owner が独立 review を明示要求した場合)")
        self.assertEqual(
            fold_review_exemption([("101", body), ("102", scope())]).state, EXEMPTION_INVALID
        )

    def test_each_missing_required_field_fails_closed(self):
        for field in ("role", "direct_edit", "allowed_paths", "reason", "follow_up_review"):
            with self.subTest(missing=field):
                facts = fold_review_exemption(exempt_history(**{field: None}))
                self.assertEqual(facts.state, EXEMPTION_INVALID)
                self.assertFalse(facts.in_force)

    def test_non_canonical_role_fails_closed(self):
        self.assertEqual(
            fold_review_exemption(exempt_history(role="implementer")).state,
            EXEMPTION_INVALID,
        )

    def test_direct_edit_false_fails_closed(self):
        self.assertEqual(
            fold_review_exemption(exempt_history(direct_edit="false")).state,
            EXEMPTION_INVALID,
        )

    def test_latest_gate_wins(self):
        journals = [
            ("101", gate(follow_up_review="true")),
            ("102", scope()),
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
        journals = [("101", gate()), ("102", scope()), ("205", gate(reason=None))]
        facts = fold_review_exemption(journals)
        self.assertEqual(facts.state, EXEMPTION_INVALID)
        self.assertEqual(facts.journal, "205")
        self.assertFalse(facts.in_force)

    def test_unparseable_journal_ids_are_skipped(self):
        facts = fold_review_exemption(
            [("not-a-journal", gate()), ("101", gate()), ("102", scope())]
        )
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
            EXEMPTION_PATH_COVERAGE_UNPROVEN,
        ):
            self.assertIn(state, REVIEW_EXEMPTION_STATES)


class PathCoverageTests(unittest.TestCase):
    """Review j#90137 F1: a non-empty ``allowed_paths`` is NOT the preset's exemption condition.

    The preset requires a gate covering "対象 commit の全 changed path". A gate that names one
    unrelated file must not exempt a commit that touched the runtime package.
    """

    def test_a_gate_that_does_not_cover_the_changed_paths_is_not_an_exemption(self):
        facts = fold_review_exemption([("101", gate(allowed_paths="README.md")), ("102", scope())])
        self.assertEqual(facts.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertFalse(facts.in_force)
        self.assertEqual(facts.uncovered, SCOPE_PATHS)
        self.assertEqual(facts.covered_commit, HEAD)

    def test_partial_coverage_is_not_coverage(self):
        facts = fold_review_exemption(
            [("101", gate(allowed_paths="vibes/docs/rules/**")), ("102", scope())]
        )
        self.assertEqual(facts.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertEqual(facts.uncovered, (".mozyo-bridge/docs/catalog.yaml",))

    def test_full_coverage_is_an_exemption(self):
        facts = fold_review_exemption(exempt_history())
        self.assertEqual(facts.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.uncovered, ())
        self.assertEqual(facts.covered_commit, HEAD)

    def test_no_declared_change_scope_fails_closed(self):
        """"We could not check coverage" must never read as "covered"."""
        facts = fold_review_exemption([("101", gate())])
        self.assertEqual(facts.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertFalse(facts.in_force)

    def test_changed_paths_without_a_commit_is_not_a_proven_scope(self):
        body = "## Gate: Implementation Done\n- changed_paths:\n  - vibes/docs/rules/x.md\n"
        self.assertFalse(fold_declared_change_scope([("102", body)]).proven)
        self.assertEqual(
            fold_review_exemption([("101", gate()), ("102", body)]).state,
            EXEMPTION_PATH_COVERAGE_UNPROVEN,
        )

    def test_single_segment_glob_does_not_cover_nested_paths(self):
        """``src/*`` must not act as a recursive glob (a whole-string fnmatch would)."""
        self.assertEqual(uncovered_paths(("src/a/b.py",), ("src/*",)), ("src/a/b.py",))
        self.assertEqual(uncovered_paths(("src/a.py",), ("src/*",)), ())

    def test_double_star_covers_any_depth_including_zero(self):
        self.assertEqual(uncovered_paths(("src/a/b/c.py", "src"), ("src/**",)), ())

    def test_empty_allowed_paths_covers_nothing(self):
        self.assertEqual(uncovered_paths(("a.py",), ()), ("a.py",))

    def test_change_scope_is_latest_wins(self):
        first = scope("only/one.md", commit="1111111111111111111111111111111111111111")
        second = scope("other/two.md")
        resolved = fold_declared_change_scope([("102", first), ("300", second)])
        self.assertEqual(resolved.paths, ("other/two.md",))
        self.assertEqual(resolved.commit, HEAD)
        self.assertEqual(resolved.journal, "300")


class ChangeScopeSupersedesByExistingTests(unittest.TestCase):
    """Review j#90244 R2-F1: a scope declaration supersedes by EXISTING, not by being valid.

    Skipping a newer empty / commit-less declaration leaves a STALE older scope as "latest", so
    the gate keeps being checked against the PREVIOUS commit's path set. This is the same
    invariant the module already applies to the gate itself (#13490 j#85365 F1 / #13952 F3).
    """

    OLD_COMMIT = "1111111111111111111111111111111111111111"
    OLD_SCOPE = scope("vibes/docs/rules/agent-workflow.md", commit=OLD_COMMIT)

    def _fold(self, newest_notes):
        journals = [("100", self.OLD_SCOPE), ("101", gate()), ("200", newest_notes)]
        return fold_declared_change_scope(journals), fold_review_exemption(journals)

    def test_a_newer_empty_declaration_shadows_an_older_proven_scope(self):
        newer = f"## Gate: Implementation Done\n- commit: {HEAD}\n- changed_paths:\n"
        resolved, exemption = self._fold(newer)
        self.assertEqual(resolved.journal, "200")
        self.assertEqual(resolved.paths, ())
        self.assertFalse(resolved.proven)
        self.assertEqual(exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertFalse(exemption.in_force)

    def test_a_newer_declaration_without_a_commit_shadows_too(self):
        newer = "## Gate: Implementation Done\n- changed_paths:\n  - vibes/docs/rules/x.md\n"
        resolved, exemption = self._fold(newer)
        self.assertEqual(resolved.journal, "200")
        self.assertEqual(resolved.commit, "")
        self.assertFalse(resolved.proven)
        self.assertEqual(exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)

    def test_a_newer_valid_declaration_is_adopted(self):
        """Negative control: the shadow rule does not make every newer journal unprovable."""
        resolved, exemption = self._fold(scope("vibes/docs/rules/other.md"))
        self.assertEqual(resolved.journal, "200")
        self.assertEqual(resolved.commit, HEAD)
        self.assertTrue(resolved.proven)
        self.assertEqual(exemption.state, EXEMPTION_EXEMPT)

    def test_a_journal_with_no_declaration_leaves_the_older_scope_standing(self):
        """Absence is not a declaration — only a journal that CARRIES the field supersedes."""
        resolved, exemption = self._fold(f"## Gate: Close\n- commit: {HEAD}\n")
        self.assertEqual(resolved.journal, "100")
        self.assertEqual(resolved.commit, self.OLD_COMMIT)
        self.assertTrue(resolved.proven)
        self.assertEqual(exemption.state, EXEMPTION_EXEMPT)


class GovernedPathListShapeTests(unittest.TestCase):
    """Both governed shapes are read: inline, and the template's continuation list."""

    def test_continuation_list_allowed_paths_is_read(self):
        body = (
            "## Gate: codex_direct_edit\n"
            "- role: 実装者\n"
            "- direct_edit: true\n"
            "- allowed_paths:\n"
            "  - `vibes/docs/rules/**`\n"
            "  - `.mozyo-bridge/docs/catalog.yaml` (既存)\n"
            "- reason: r\n"
            "- follow_up_review: false\n"
        )
        facts = fold_review_exemption([("101", body), ("102", scope())])
        self.assertEqual(
            facts.allowed_paths,
            ("vibes/docs/rules/**", ".mozyo-bridge/docs/catalog.yaml"),
        )
        self.assertEqual(facts.state, EXEMPTION_EXEMPT)

    def test_the_scan_stops_at_the_next_sibling_field(self):
        """A following field must not leak into the list."""
        body = (
            "## Gate: codex_direct_edit\n"
            "- role: 実装者\n"
            "- direct_edit: true\n"
            "- allowed_paths:\n"
            "  - vibes/docs/rules/**\n"
            "- reason: r\n"
            "- follow_up_review: false\n"
        )
        self.assertEqual(
            fold_review_exemption([("101", body)]).allowed_paths, ("vibes/docs/rules/**",)
        )

    def test_an_unfilled_list_field_yields_no_entries(self):
        self.assertEqual(
            fold_review_exemption([("101", gate(allowed_paths=""))]).state, EXEMPTION_INVALID
        )


class ExemptionIntegrationAdmissibilityTests(unittest.TestCase):
    """The terminal-retire fence: every conjunct, each with its own distinct reason."""

    EXEMPT = ReviewExemptionFacts(state=EXEMPTION_EXEMPT, journal="101")

    def _evaluate(self, exemption=None, **over):
        kwargs = dict(currently_in_force=True, close_recorded=True, integration_complete=True)
        kwargs.update(over)
        return evaluate_exemption_integration_admissible(
            self.EXEMPT if exemption is None else exemption, **kwargs
        )

    def test_all_facts_admit(self):
        result = self._evaluate()
        self.assertTrue(result.admissible)
        self.assertEqual(result.reason, REASON_OK)

    def test_missing_close_blocks(self):
        result = self._evaluate(close_recorded=False)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_CLOSE_NOT_RECORDED)

    def test_incomplete_integration_blocks(self):
        result = self._evaluate(integration_complete=False)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_INTEGRATION_NOT_COMPLETE)

    def test_no_exemption_blocks(self):
        result = self._evaluate(ReviewExemptionFacts())
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_NO_EXEMPTION_RECORDED)

    def test_invalid_exemption_blocks(self):
        result = self._evaluate(ReviewExemptionFacts(state=EXEMPTION_INVALID, journal="101"))
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_EXEMPTION_INVALID)

    def test_unproven_path_coverage_blocks(self):
        """Review j#90137 F1, at the retire fence."""
        result = self._evaluate(
            ReviewExemptionFacts(state=EXEMPTION_PATH_COVERAGE_UNPROVEN, journal="101")
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_PATH_COVERAGE_UNPROVEN)

    def test_owner_required_follow_up_review_blocks_this_route(self):
        """Acceptance 4: an owner-required review never passes through the exemption route."""
        result = self._evaluate(
            ReviewExemptionFacts(state=EXEMPTION_REVIEW_REQUIRED, journal="101")
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_FOLLOW_UP_REVIEW_REQUIRED)

    def test_a_superseded_exemption_blocks(self):
        """Review j#90137 F3: the retire consumes the SAME supersession-aware fact as the glance.

        A valid, covering exemption that a NEWER review round superseded must not admit — reading
        only the gate state here is exactly what let the retire and the glance disagree.
        """
        result = self._evaluate(currently_in_force=False)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_EXEMPTION_SUPERSEDED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
