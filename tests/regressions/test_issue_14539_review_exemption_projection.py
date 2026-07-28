"""Redmine #14539 — a valid review exemption must reach the runtime projection and the retire.

The central preset's `### Codex Direct Edit Gate` (policy 正本: integration head
``f6763eb1f8b71dac42d2cb156c8131711f6e9f0d``, #14530 j#89545) says a valid ``codex_direct_edit``
gate with ``follow_up_review: false`` promotes Codex to the implementation subject for its scope
and owes NO separate auditor review. Until this fix that policy existed only in prose, so two
runtime read-models still acted as if the review were owed:

1. ``workflow glance`` folded a superseded, pre-exemption ``review_request`` back into
   ``review_waiting`` — an audit policy says is not owed;
2. the terminal retire's latest-generation fence blocked with ``stale_review_generation``,
   because an exempt lane has no review generation to BE the approved latest. The only way past
   it was to assert ``--latest-generation-admissible`` — literally "the latest generation is
   approved with no unresolved blocking finding" — about a review that never happened.

This suite pins both fixes AND, just as importantly, pins that they do not erode the fences they
sit next to: an owner-required ``follow_up_review: true``, an invalid gate, and a review round
opened AFTER the exemption must each keep the ordinary review path and the ordinary fence.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
    _resolve_latest_generation_admissible,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (
    fold_integration_disposition,
    has_conflicting_disposition_declaration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (
    fold_issue_gate_facts,
    lane_signal_from_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_exemption import (
    EXEMPTION_EXEMPT,
    EXEMPTION_INVALID,
    EXEMPTION_PATH_COVERAGE_UNPROVEN,
    EXEMPTION_REVIEW_REQUIRED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    classify_lane_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    INTEGRATION_STALE_REVIEW_GENERATION,
    RetirePreflight,
    SublaneIntegrationPolicy,
    decide_retire_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_RETIRE_READY,
    LANE_STATE_REVIEW_WAITING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
    IssueGlanceSnapshot,
    fold_glance_row,
)

GATE_EXEMPT = """## Gate: codex_direct_edit
- role: 実装者
- direct_edit: true
- allowed_paths: vibes/docs/rules/**, vibes/docs/logics/**
- reason: coordinator-owned standalone policy docs
- follow_up_review: false
"""

GATE_REVIEW_REQUIRED = GATE_EXEMPT.replace(
    "- follow_up_review: false", "- follow_up_review: true"
)

# A gate journal that structurally qualifies but omits a required field.
GATE_MALFORMED = "\n".join(
    line for line in GATE_EXEMPT.splitlines() if not line.startswith("- reason:")
)

HEAD = "4f2b9c1a3d5e6f708192a3b4c5d6e7f809a1b2c3"

# A COMPLIANT governed review_request: the template mandates `changed_paths`, and a real one
# carries it (this issue's own RR j#89842 does). A change-bearing journal that declares a commit
# WITHOUT them declares an unproven scope and shadows — see ReviewJ90289Tests.
REVIEW_REQUEST = (
    f"## Gate: Review Request\n"
    f"- commit: {HEAD}\n"
    "- changed_paths:\n"
    "  - `vibes/docs/rules/agent-workflow.md`\n"
    "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
)
# The durable change scope every exemption's path coverage is checked against (review j#90137 F1).
# Both paths are inside GATE_EXEMPT's ``allowed_paths``.
IMPLEMENTATION_DONE = (
    f"## Gate: Implementation Done\n"
    f"- commit: {HEAD}\n"
    "- changed_paths:\n"
    "  - `vibes/docs/rules/agent-workflow.md`\n"
    "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
)
def close(commit: str = HEAD) -> str:
    """A governed Close gate naming the commit it closes.

    Parameterized by review j#91577 finding 2: the Close's commit must be the commit the
    exemption's coverage was proven for, so a fixture cannot leave the two unrelated (the
    previous constant always named ``HEAD``, which silently made a later fixture assert admission
    for a history whose Close named a different commit than its own proven scope).
    """
    return f"## Gate: Close\n- commit: {commit}\n"


CLOSE = close()

#: The commit that proved integration on the branch — deliberately NOT the reviewed source head,
#: because under patch_equivalent they differ and the fence must bind to the SOURCE head.
INTEGRATION_HEAD = "7c1d2e3f4a5b60718293a4b5c6d7e8f90a1b2c3d"

#: A legacy, lane-unbound disposition note. Valid for the glance projection forever; NOT automated
#: terminal-retire evidence (review j#91696 finding 2), which is its own pin below.
INTEGRATION_MERGED_LEGACY = "## Integration disposition\n- disposition: merged\n"


def integration_merged(source_head: str = HEAD) -> str:
    """A governed integration disposition carrying STRICT lane-enveloped evidence.

    Parameterized by the reviewed SOURCE head, because that is what the retire binds to the
    exemption's covered commit. The previous fixture was the legacy lane-unbound note, which
    names no commit at all — so every "this history admits" test asserted admission on evidence
    that could not say which work it integrated.
    """
    return (
        "## Integration disposition\n"
        "- disposition: merged\n"
        "[mozyo:workflow-event:gate=integration_disposition:workspace=ws:lane=r1:"
        f"lane_generation=1:head={source_head}:integration_head={INTEGRATION_HEAD}:"
        "integration_branch=main-next:disposition=merge]\n"
    )


INTEGRATION_MERGED = integration_merged()


def state_of(journals, *, issue_open=True):
    """The workflow state the real projection chain folds these journals into."""
    facts = fold_issue_gate_facts(journals)
    assert facts is not None, "the fixture must carry a recognized gate"
    return facts, classify_lane_state(
        lane_signal_from_gate_facts("14539", facts, issue_open=issue_open)
    )


class GlanceProjectionTests(unittest.TestCase):
    """Acceptance 1: the glance does not return an exempt issue to ``review_waiting``."""

    def test_exemption_supersedes_an_earlier_review_request(self):
        """The literal defect: a superseded past Review Request re-projected as review owed."""
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("100", REVIEW_REQUEST), ("101", GATE_EXEMPT)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertTrue(facts.review_exempt)
        self.assertNotEqual(state, LANE_STATE_REVIEW_WAITING)
        self.assertEqual(state, LANE_STATE_OWNER_WAITING)

    def test_implementation_done_after_an_exemption_owes_no_review(self):
        facts, state = state_of(
            [("101", GATE_EXEMPT), ("102", IMPLEMENTATION_DONE)]
        )
        self.assertTrue(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_OWNER_WAITING)

    def test_exemption_recorded_before_implementation_done_still_holds(self):
        """``implementation_done`` is not a review round, so the exemption's order is irrelevant."""
        _, state = state_of([("101", GATE_EXEMPT), ("900", IMPLEMENTATION_DONE)])
        self.assertEqual(state, LANE_STATE_OWNER_WAITING)

    def test_exempt_lane_with_pending_integration_is_integration_waiting_not_owner_waiting(self):
        """The exemption removes the REVIEW requirement only — integration still gates."""
        deferral = "## Integration disposition\n- disposition: explicit_deferral\n"
        _, state = state_of(
            [("101", GATE_EXEMPT), ("102", IMPLEMENTATION_DONE), ("103", deferral)]
        )
        self.assertEqual(state, LANE_STATE_INTEGRATION_WAITING)

    def test_exemption_never_fabricates_a_review_approval(self):
        """The preset forbids expressing an exemption as a Review Gate approval / self review."""
        facts, _ = state_of([("101", GATE_EXEMPT), ("102", IMPLEMENTATION_DONE)])
        signal = lane_signal_from_gate_facts("14539", facts)
        self.assertEqual(signal.review_conclusion, "pending")
        self.assertNotEqual(signal.latest_gate, "review")

    def test_glance_row_reports_no_review_owed(self):
        """Through the actual ``workflow glance`` row fold, not just the classifier."""
        facts, _ = state_of(
            [("99", IMPLEMENTATION_DONE), ("100", REVIEW_REQUEST), ("101", GATE_EXEMPT)]
        )
        row = fold_glance_row(
            IssueGlanceSnapshot(
                issue_id="14539",
                signal=lane_signal_from_gate_facts("14539", facts),
                latest_gate_journal=facts.latest_gate_journal,
            )
        )
        self.assertNotEqual(row.workflow_state, LANE_STATE_REVIEW_WAITING)
        self.assertNotIn("Review Gate owed", row.next_action)
        self.assertNotIn("auditor review owed", row.next_action)


class ExemptionFenceIsNotErodedTests(unittest.TestCase):
    """Acceptance 4 + the fail-closed direction: what must STILL read as review_waiting."""

    def test_owner_required_follow_up_review_keeps_the_review_owed(self):
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("100", REVIEW_REQUEST), ("101", GATE_REVIEW_REQUIRED)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_REVIEW_REQUIRED)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_malformed_gate_keeps_the_review_owed(self):
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("100", REVIEW_REQUEST), ("101", GATE_MALFORMED)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_INVALID)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_review_round_opened_after_the_exemption_re_owes_the_review(self):
        """Cross-generation: the exemption exempts what it precedes, not what supersedes it."""
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("101", GATE_EXEMPT), ("205", REVIEW_REQUEST)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_newer_review_round_re_owes_even_when_impl_done_is_latest(self):
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("101", GATE_EXEMPT), ("205", REVIEW_REQUEST),
             ("300", IMPLEMENTATION_DONE)]
        )
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_newer_malformed_gate_shadows_an_older_valid_exemption(self):
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("101", GATE_EXEMPT), ("205", GATE_MALFORMED),
             ("300", REVIEW_REQUEST)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_INVALID)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_an_issue_with_no_exemption_is_untouched(self):
        _, state = state_of([("100", REVIEW_REQUEST)])
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)


class TerminalRetireAdmissionTests(unittest.TestCase):
    """Acceptance 2/3: re-verify at action time; never require a false assert."""

    def _resolve(self, **over):
        base = dict(
            review_generation_json=None,
            review_exemption_json=None,
            latest_generation_admissible=False,
            issue="14539",
        )
        base.update(over)
        return _resolve_latest_generation_admissible(argparse.Namespace(**base))

    def _obs(self, tmp, journals):
        path = Path(tmp) / "exemption.json"
        path.write_text(
            json.dumps(
                {
                    "issue": "14539",
                    "journals": [
                        {"journal_id": j, "notes": n} for j, n in journals
                    ],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    EXEMPT_CLOSED_MERGED = [
        ("101", GATE_EXEMPT),
        ("102", IMPLEMENTATION_DONE),
        ("103", INTEGRATION_MERGED),
        ("104", CLOSE),
    ]

    def test_exempt_closed_and_merged_admits_without_the_false_assert(self):
        with tempfile.TemporaryDirectory() as t:
            obs = self._obs(t, self.EXEMPT_CLOSED_MERGED)
            self.assertTrue(
                self._resolve(
                    review_exemption_json=obs, latest_generation_admissible=False
                )
            )

    def test_missing_close_fails_closed(self):
        journals = [j for j in self.EXEMPT_CLOSED_MERGED if j[0] != "104"]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(review_exemption_json=self._obs(t, journals)))

    def test_missing_integration_fails_closed(self):
        journals = [j for j in self.EXEMPT_CLOSED_MERGED if j[0] != "103"]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(review_exemption_json=self._obs(t, journals)))

    def test_owner_required_follow_up_review_fails_closed(self):
        journals = [
            (j, GATE_REVIEW_REQUIRED if j == "101" else n)
            for j, n in self.EXEMPT_CLOSED_MERGED
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(review_exemption_json=self._obs(t, journals)))

    def test_malformed_gate_fails_closed(self):
        journals = [
            (j, GATE_MALFORMED if j == "101" else n) for j, n in self.EXEMPT_CLOSED_MERGED
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(review_exemption_json=self._obs(t, journals)))

    def test_no_exemption_in_the_record_fails_closed(self):
        journals = [j for j in self.EXEMPT_CLOSED_MERGED if j[0] != "101"]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(review_exemption_json=self._obs(t, journals)))

    def test_unreadable_and_malformed_observation_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as t:
            bad = Path(t) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertFalse(self._resolve(review_exemption_json=str(bad)))
            missing = Path(t) / "absent.json"
            self.assertFalse(self._resolve(review_exemption_json=str(missing)))

    def test_a_supplied_but_failing_measurement_never_falls_back_to_the_assert(self):
        """The pre-existing #13518 invariant, extended to the new measured route."""
        journals = [j for j in self.EXEMPT_CLOSED_MERGED if j[0] != "104"]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(
                self._resolve(
                    review_exemption_json=self._obs(t, journals),
                    latest_generation_admissible=True,
                )
            )

    def test_existing_assert_and_fail_closed_paths_are_unchanged(self):
        self.assertFalse(self._resolve())
        self.assertTrue(self._resolve(latest_generation_admissible=True))

    def test_admitted_exemption_clears_stale_review_generation_at_the_retire_decision(self):
        """End of the chain: the resolved admissibility removes the blocker the issue named."""
        policy = SublaneIntegrationPolicy(merge_on_retire=False)
        blocked = decide_retire_integration(
            policy, RetirePreflight(is_git_workspace=False, latest_generation_admissible=False)
        )
        self.assertIn(INTEGRATION_STALE_REVIEW_GENERATION, blocked.blocked_reasons)

        with tempfile.TemporaryDirectory() as t:
            admissible = self._resolve(
                review_exemption_json=self._obs(t, self.EXEMPT_CLOSED_MERGED)
            )
        allowed = decide_retire_integration(
            policy,
            RetirePreflight(
                is_git_workspace=False, latest_generation_admissible=admissible
            ),
        )
        self.assertTrue(allowed.may_retire)
        self.assertEqual(allowed.blocked_reasons, ())

    def test_a_closed_exempt_lane_projects_retire_ready(self):
        """The glance half of the same scenario agrees with the retire half."""
        _, state = state_of(self.EXEMPT_CLOSED_MERGED, issue_open=False)
        self.assertEqual(state, LANE_STATE_RETIRE_READY)


class ReviewJ90137Tests(unittest.TestCase):
    """The three defects review j#90137 found in R1, each pinned by its own reproduction."""

    def _resolve(self, obs_path, *, issue="14539", assertion=False):
        return _resolve_latest_generation_admissible(
            argparse.Namespace(
                review_generation_json=None,
                review_exemption_json=obs_path,
                latest_generation_admissible=assertion,
                issue=issue,
            )
        )

    def _obs(self, tmp, journals, *, issue="14539", name="o.json"):
        path = Path(tmp) / name
        path.write_text(
            json.dumps(
                {
                    "issue": issue,
                    "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    #: A gate naming ONE unrelated file, against a commit that changed the docs tree.
    GATE_NARROW = GATE_EXEMPT.replace(
        "- allowed_paths: vibes/docs/rules/**, vibes/docs/logics/**",
        "- allowed_paths: README.md",
    )

    def test_f1_a_gate_not_covering_the_changed_paths_does_not_exempt(self):
        """F1: R1 accepted any non-empty ``allowed_paths``, so a README-only gate exempted src/**."""
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("100", REVIEW_REQUEST), ("101", self.GATE_NARROW)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)
        self.assertEqual(
            facts.review_exemption.uncovered,
            (
                "vibes/docs/rules/agent-workflow.md",
                "vibes/docs/logics/coordinator-sublane-development-flow.md",
            ),
        )

    def test_f1_the_retire_route_also_refuses_an_uncovering_gate(self):
        journals = [
            ("99", IMPLEMENTATION_DONE),
            ("101", self.GATE_NARROW),
            ("103", INTEGRATION_MERGED),
            ("104", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, journals)))

    def test_f2_evidence_from_another_issue_never_unlocks_the_fence(self):
        """F2: R1 read the observation's journals without ever correlating its ``issue``."""
        journals = [
            ("99", IMPLEMENTATION_DONE),
            ("101", GATE_EXEMPT),
            ("103", INTEGRATION_MERGED),
            ("104", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            obs = self._obs(t, journals, issue="14539")
            # The very same observation admits for its own issue …
            self.assertTrue(self._resolve(obs, issue="14539"))
            # … and never for another one.
            self.assertFalse(self._resolve(obs, issue="99999"))

    def test_f2_a_missing_issue_on_either_side_fails_closed(self):
        journals = [
            ("99", IMPLEMENTATION_DONE),
            ("101", GATE_EXEMPT),
            ("103", INTEGRATION_MERGED),
            ("104", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, journals, issue=""), issue="14539"))
            self.assertFalse(self._resolve(self._obs(t, journals, name="b.json"), issue=""))

    def test_f3_the_retire_agrees_with_the_glance_about_supersession(self):
        """F3: R1's retire ignored a review round opened after the exemption; the glance did not."""
        journals = [
            ("99", IMPLEMENTATION_DONE),
            ("101", GATE_EXEMPT),
            ("102", REVIEW_REQUEST),
            ("103", INTEGRATION_MERGED),
            ("104", CLOSE),
        ]
        facts = fold_issue_gate_facts(journals)
        self.assertFalse(facts.review_exempt)
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, journals)))

    def test_f3_without_the_newer_round_the_same_history_admits(self):
        """The negative control: the ONLY difference is the superseding review round."""
        journals = [
            ("99", IMPLEMENTATION_DONE),
            ("101", GATE_EXEMPT),
            ("103", INTEGRATION_MERGED),
            ("104", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self._resolve(self._obs(t, journals)))


class ReviewJ90244Tests(unittest.TestCase):
    """R2-F1: a NEWER empty change-scope declaration must shadow an older proven one.

    R2 fixed the covered/uncovered paths but kept ``continue``-ing past a declaration that
    yielded no entries, so a stale older scope stayed "latest" and the gate went on being checked
    against the PREVIOUS commit's path set — an exemption granted for a commit whose changed set
    the record no longer proves.
    """

    OLD_COMMIT = "1111111111111111111111111111111111111111"
    #: A scope for an EARLIER commit, fully covered by GATE_EXEMPT's allowed_paths.
    OLD_SCOPE = (
        f"## Gate: Implementation Done\n"
        f"- commit: {OLD_COMMIT}\n"
        "- changed_paths:\n"
        "  - `vibes/docs/rules/agent-workflow.md`\n"
    )
    #: A NEW commit whose changed set the record declares but leaves empty.
    NEW_EMPTY_SCOPE = f"## Gate: Implementation Done\n- commit: {HEAD}\n- changed_paths:\n"

    def _resolve(self, obs_path, *, issue="14539"):
        return _resolve_latest_generation_admissible(
            argparse.Namespace(
                review_generation_json=None,
                review_exemption_json=obs_path,
                latest_generation_admissible=False,
                issue=issue,
            )
        )

    def _obs(self, tmp, journals, name="o.json"):
        path = Path(tmp) / name
        path.write_text(
            json.dumps(
                {
                    "issue": "14539",
                    "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def _history(self, newest):
        """The retire-shaped history: closed and merged, so only the SCOPE decides."""
        return [
            ("100", self.OLD_SCOPE),
            ("101", GATE_EXEMPT),
            ("200", newest),
            ("203", INTEGRATION_MERGED),
            ("204", CLOSE),
        ]

    def _open_history(self, newest):
        """The glance-shaped history: the latest gate is ``implementation_done``, so the lane's
        state turns on whether a review is owed (a Close gate would decide it regardless)."""
        return [("100", self.OLD_SCOPE), ("101", GATE_EXEMPT), ("200", newest)]

    def test_a_newer_empty_scope_withholds_the_exemption_in_the_glance(self):
        facts, state = state_of(self._open_history(self.NEW_EMPTY_SCOPE))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_the_same_open_history_with_a_proven_newer_scope_owes_no_review(self):
        """Negative control for the glance half: only the newest declaration's CONTENT differs."""
        proven = (
            f"## Gate: Implementation Done\n"
            f"- commit: {HEAD}\n"
            "- changed_paths:\n"
            "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
        )
        facts, state = state_of(self._open_history(proven))
        self.assertTrue(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_OWNER_WAITING)

    def test_a_newer_empty_scope_blocks_the_terminal_retire(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, self._history(self.NEW_EMPTY_SCOPE))))

    def test_the_same_history_with_a_proven_newer_scope_admits(self):
        """Negative control: only the newest declaration's CONTENT differs."""
        proven = (
            f"## Gate: Implementation Done\n"
            f"- commit: {HEAD}\n"
            "- changed_paths:\n"
            "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
        )
        facts, _ = state_of(self._history(proven))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self._resolve(self._obs(t, self._history(proven))))

    def test_a_newer_journal_that_declares_nothing_leaves_the_scope_standing(self):
        """Absence is not a declaration.

        A ``close`` gate carries a commit too, but it reports on work already scoped rather than
        announcing a new implementation, so it must not erase the scope of the work it closes.
        """
        facts, _ = state_of(self._history(f"## Gate: Close\n- commit: {HEAD}\n"))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.OLD_COMMIT)


class ReviewJ90289Tests(unittest.TestCase):
    """R3-F1: a change-bearing journal announcing a NEW target commit declares a change scope.

    R3 keyed "is this a scope declaration?" on the ``changed_paths`` FIELD being present, so a new
    ``## Gate: Implementation Done`` naming a different commit but omitting the field altogether
    declared nothing — the previous commit's scope stayed authoritative and the gate went on being
    checked against it. Announcing a new target commit as an implementation result IS a
    declaration; without paths it is an UNPROVEN one.
    """

    OLD_COMMIT = "1111111111111111111111111111111111111111"
    NEW_COMMIT = "2222222222222222222222222222222222222222"
    OLD_SCOPE = (
        f"## Gate: Implementation Done\n"
        f"- commit: {OLD_COMMIT}\n"
        "- changed_paths:\n"
        "  - `vibes/docs/rules/agent-workflow.md`\n"
    )

    def _resolve(self, obs_path, *, issue="14539"):
        return _resolve_latest_generation_admissible(
            argparse.Namespace(
                review_generation_json=None,
                review_exemption_json=obs_path,
                latest_generation_admissible=False,
                issue=issue,
            )
        )

    def _obs(self, tmp, journals):
        path = Path(tmp) / "o.json"
        path.write_text(
            json.dumps(
                {
                    "issue": "14539",
                    "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def _open_history(self, newest):
        return [("100", self.OLD_SCOPE), ("101", GATE_EXEMPT), ("200", newest)]

    def _retire_history(self, newest, *, close_commit=NEW_COMMIT):
        return self._open_history(newest) + [
            ("203", integration_merged(close_commit)),
            ("204", close(close_commit)),
        ]

    #: The defect's shape: a new implementation result, a new commit, no changed_paths at all.
    NEW_COMMIT_NO_PATHS = f"## Gate: Implementation Done\n- commit: {NEW_COMMIT}\n"

    def test_a_new_commit_without_changed_paths_withholds_the_exemption(self):
        facts, state = state_of(self._open_history(self.NEW_COMMIT_NO_PATHS))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_new_commit_without_changed_paths_blocks_the_terminal_retire(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(
                self._resolve(self._obs(t, self._retire_history(self.NEW_COMMIT_NO_PATHS)))
            )

    def test_a_review_request_announcing_a_new_commit_without_paths_also_declares(self):
        """``review_request`` is change-bearing too — it pins the commit it wants reviewed."""
        newest = f"## Gate: Review Request\n- commit: {self.NEW_COMMIT}\n"
        facts, _ = state_of(self._open_history(newest))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)

    def test_the_same_history_with_the_new_commit_scoped_admits(self):
        """Negative control: only the presence of the new commit's paths differs."""
        newest = (
            f"## Gate: Implementation Done\n"
            f"- commit: {self.NEW_COMMIT}\n"
            "- changed_paths:\n"
            "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
        )
        facts, state = state_of(self._open_history(newest))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.NEW_COMMIT)
        self.assertEqual(state, LANE_STATE_OWNER_WAITING)
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self._resolve(self._obs(t, self._retire_history(newest))))

    def test_a_commit_bearing_close_gate_does_not_erase_the_scope(self):
        """The boundary the fix must not cross: ``close`` reports on work already scoped."""
        facts, _ = state_of(self._open_history(f"## Gate: Close\n- commit: {self.NEW_COMMIT}\n"))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.OLD_COMMIT)

    def test_an_integration_disposition_does_not_erase_the_scope(self):
        facts, _ = state_of(self._open_history(INTEGRATION_MERGED))
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.OLD_COMMIT)

    def test_a_change_bearing_journal_with_no_commit_announces_no_new_target(self):
        """Without a commit there is no NEW target commit, so the standing scope is unaffected."""
        facts, _ = state_of(
            self._open_history("## Gate: Implementation Done\n- 残リスク: none\n")
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.OLD_COMMIT)


class ReviewJ91577CombinedGateTests(unittest.TestCase):
    """R4-F1: a combined gate heading must not lose its non-top-precedence facts.

    ``fold_issue_gate_facts`` reduced each journal's recognized gates to the max-precedence one,
    and both the change-bearing set (R3-F1's fix) and the review-round set read only that. A
    governed ``## Gate: Implementation Done + Close`` therefore announced no new target commit,
    and a ``## Gate: Review Request + Close`` opened no review round — in both cases because
    ``close`` outranks them inside the same journal. The grammar itself treats ``+``-combined
    headings as a first-class shape, so dropping the other halves contradicted it.
    """

    OLD_COMMIT = "1111111111111111111111111111111111111111"
    NEW_COMMIT = "2222222222222222222222222222222222222222"
    OLD_SCOPE = (
        f"## Gate: Implementation Done\n"
        f"- commit: {OLD_COMMIT}\n"
        "- changed_paths:\n"
        "  - `vibes/docs/rules/agent-workflow.md`\n"
    )

    def _resolve(self, obs_path, *, issue="14539"):
        return _resolve_latest_generation_admissible(
            argparse.Namespace(
                review_generation_json=None,
                review_exemption_json=obs_path,
                latest_generation_admissible=False,
                issue=issue,
            )
        )

    def _obs(self, tmp, journals):
        path = Path(tmp) / "o.json"
        path.write_text(
            json.dumps(
                {
                    "issue": "14539",
                    "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def test_a_combined_impl_done_close_declares_its_new_target_commit(self):
        """The scope must move to the new commit, leaving it unproven — not stay on the old one."""
        combined = f"## Gate: Implementation Done + Close\n- commit: {self.NEW_COMMIT}\n"
        facts, state = state_of(
            [("100", self.OLD_SCOPE), ("101", GATE_EXEMPT), ("200", combined)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertNotEqual(facts.review_exemption.covered_commit, self.OLD_COMMIT)
        self.assertFalse(facts.review_exempt)

    def test_a_combined_review_request_close_is_still_an_open_review_round(self):
        """A review round the exemption does not precede re-owes the review."""
        combined = f"## Gate: Review Request + Close\n- commit: {self.NEW_COMMIT}\n"
        facts, _ = state_of(
            [("100", self.OLD_SCOPE), ("101", GATE_EXEMPT), ("200", combined)]
        )
        self.assertFalse(facts.review_exempt)

    def test_the_combined_forms_do_not_admit_the_terminal_retire(self):
        for label, heading in (
            ("impl_done", "## Gate: Implementation Done + Close"),
            ("review_request", "## Gate: Review Request + Close"),
        ):
            with self.subTest(label):
                combined = f"{heading}\n- commit: {self.NEW_COMMIT}\n"
                journals = [
                    ("100", self.OLD_SCOPE),
                    ("101", GATE_EXEMPT),
                    ("200", combined),
                    ("203", integration_merged(self.NEW_COMMIT)),
                ]
                with tempfile.TemporaryDirectory() as t:
                    self.assertFalse(self._resolve(self._obs(t, journals)))

    def test_the_same_combined_journal_with_its_paths_declared_admits(self):
        """Negative control: only whether the combined journal declares its paths differs.

        Without this, the pins above would also pass if a combined heading were simply refused
        outright, which would break the governed shape rather than read it.
        """
        combined = (
            f"## Gate: Implementation Done + Close\n"
            f"- commit: {self.NEW_COMMIT}\n"
            "- changed_paths:\n"
            "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
        )
        journals = [
            ("100", self.OLD_SCOPE),
            ("101", GATE_EXEMPT),
            ("200", combined),
            ("203", integration_merged(self.NEW_COMMIT)),
        ]
        facts, _ = state_of(journals, issue_open=False)
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.NEW_COMMIT)
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self._resolve(self._obs(t, journals)))

    def test_a_close_only_journal_still_declares_nothing(self):
        """The R3 boundary is unchanged: ``close`` alone reports on work already scoped."""
        facts, _ = state_of(
            [("100", self.OLD_SCOPE), ("101", GATE_EXEMPT), ("200", close(self.NEW_COMMIT))]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.OLD_COMMIT)


class ReviewJ91577EvidenceIdentityTests(unittest.TestCase):
    """R4-F2: the exemption, the Close and the integration must be about the SAME commit.

    The fence conjoined three booleans, so each fact only had to exist SOMEWHERE in the issue's
    record. A lane could therefore retire on an earlier commit's Close and merge while the commit
    the exemption actually covered was never integrated.
    """

    OLD_COMMIT = "1111111111111111111111111111111111111111"
    NEW_COMMIT = "2222222222222222222222222222222222222222"

    def _scope(self, commit, path):
        return (
            f"## Gate: Implementation Done\n"
            f"- commit: {commit}\n"
            "- changed_paths:\n"
            f"  - `{path}`\n"
        )

    def _resolve(self, obs_path, *, issue="14539"):
        return _resolve_latest_generation_admissible(
            argparse.Namespace(
                review_generation_json=None,
                review_exemption_json=obs_path,
                latest_generation_admissible=False,
                issue=issue,
            )
        )

    def _obs(self, tmp, journals):
        path = Path(tmp) / "o.json"
        path.write_text(
            json.dumps(
                {
                    "issue": "14539",
                    "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def _history(self, *, close_commit, second_scope=True):
        """commit A scoped, exempted and merged; then commit B scoped; then a Close."""
        journals = [
            ("100", self._scope(self.OLD_COMMIT, "vibes/docs/rules/agent-workflow.md")),
            ("101", GATE_EXEMPT),
            ("102", integration_merged(self.OLD_COMMIT)),
        ]
        if second_scope:
            journals.append(
                (
                    "103",
                    self._scope(
                        self.NEW_COMMIT,
                        "vibes/docs/logics/coordinator-sublane-development-flow.md",
                    ),
                )
            )
        journals.append(("104", close(close_commit)))
        return journals

    def test_a_close_naming_the_previous_commit_does_not_admit(self):
        """The literal defect: the coverage is proven for B, the Close and merge belong to A."""
        journals = self._history(close_commit=self.OLD_COMMIT)
        facts = fold_issue_gate_facts(journals)
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertEqual(facts.review_exemption.covered_commit, self.NEW_COMMIT)
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, journals)))

    def test_a_close_naming_the_covered_commit_still_needs_fresh_integration(self):
        """Fixing only the Close leaves the merge evidence older than the scope it must cover."""
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(
                self._resolve(self._obs(t, self._history(close_commit=self.NEW_COMMIT)))
            )

    def test_the_same_history_integrated_after_the_new_scope_admits(self):
        """Negative control: only WHERE the merge sits relative to the new scope differs."""
        journals = [
            ("100", self._scope(self.OLD_COMMIT, "vibes/docs/rules/agent-workflow.md")),
            ("101", GATE_EXEMPT),
            (
                "103",
                self._scope(
                    self.NEW_COMMIT,
                    "vibes/docs/logics/coordinator-sublane-development-flow.md",
                ),
            ),
            ("104", integration_merged(self.NEW_COMMIT)),
            ("105", close(self.NEW_COMMIT)),
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self._resolve(self._obs(t, journals)))

    def test_a_single_commit_lane_is_unaffected(self):
        """The ordinary shape this issue exists to admit keeps admitting."""
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(
                self._resolve(
                    self._obs(
                        t,
                        [
                            ("101", GATE_EXEMPT),
                            ("102", IMPLEMENTATION_DONE),
                            ("103", INTEGRATION_MERGED),
                            ("104", CLOSE),
                        ],
                    )
                )
            )

    def test_a_close_declaring_no_commit_at_all_does_not_admit(self):
        journals = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", INTEGRATION_MERGED),
            ("104", "## Gate: Close\n- 親USへの引き継ぎ: なし\n"),
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, journals)))


class ReviewJ91577AmbiguousGateTests(unittest.TestCase):
    """R4-F3/F4 at the projection boundary: ambiguous authority and over-granted coverage."""

    def test_a_gate_contradicting_itself_keeps_the_review_owed(self):
        contradictory = GATE_EXEMPT + "- follow_up_review: true\n"
        facts, state = state_of(
            [("99", IMPLEMENTATION_DONE), ("100", REVIEW_REQUEST), ("101", contradictory)]
        )
        self.assertEqual(facts.review_exemption.state, EXEMPTION_INVALID)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_second_narrower_allowed_paths_keeps_the_review_owed(self):
        contradictory = GATE_EXEMPT + "- allowed_paths: README.md\n"
        facts, _ = state_of([("99", IMPLEMENTATION_DONE), ("101", contradictory)])
        self.assertEqual(facts.review_exemption.state, EXEMPTION_INVALID)

    def test_a_character_class_glob_does_not_cover_the_changed_paths(self):
        """F4: ``[...]`` is outside the declared vocabulary, so it proves no coverage."""
        gate_class = GATE_EXEMPT.replace(
            "- allowed_paths: vibes/docs/rules/**, vibes/docs/logics/**",
            "- allowed_paths: vibes/docs/[rl]*/**",
        )
        facts, state = state_of([("99", IMPLEMENTATION_DONE), ("101", gate_class)])
        self.assertEqual(facts.review_exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_the_declared_glob_vocabulary_still_covers(self):
        """Negative control for both: the compliant gate on the same history still exempts."""
        facts, _ = state_of([("99", IMPLEMENTATION_DONE), ("101", GATE_EXEMPT)])
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)

    def test_an_absolute_changed_path_is_not_silently_covered(self):
        absolute_scope = (
            f"## Gate: Implementation Done\n"
            f"- commit: {HEAD}\n"
            "- changed_paths:\n"
            "  - `/vibes/docs/rules/agent-workflow.md`\n"
        )
        facts, _ = state_of([("99", absolute_scope), ("101", GATE_EXEMPT)])
        self.assertEqual(facts.review_exemption.state, EXEMPTION_PATH_COVERAGE_UNPROVEN)
        self.assertEqual(
            facts.review_exemption.uncovered, ("/vibes/docs/rules/agent-workflow.md",)
        )


class ReviewJ91696LaneStateTests(unittest.TestCase):
    """R6-F1: a combined heading's OPEN review round must block close/retire progression.

    The grammar's F10 invariant — an open review round suppresses a conflicting ``close`` /
    ``owner_close`` heading so the lane cannot advance past it — was stated unconditionally in the
    module contract but enforced only inside the structured-marker branch. A heading-only
    ``## Gate: Review Request + Close`` therefore reduced to ``close`` and projected
    ``retire_ready``: the lane retired past its own open review round.

    This is the THIRD consumer of the max-precedence reduction in this issue (R4-F1 fixed the
    change-bearing and review-round sets; this is lane state).
    """

    NEW_COMMIT = "2222222222222222222222222222222222222222"
    OLD_SCOPE = (
        f"## Gate: Implementation Done\n"
        f"- commit: {'1' * 40}\n"
        "- changed_paths:\n"
        "  - `vibes/docs/rules/agent-workflow.md`\n"
    )

    def _history(self, newest):
        return [
            ("100", self.OLD_SCOPE),
            ("101", GATE_EXEMPT),
            ("200", newest),
            ("203", integration_merged(self.NEW_COMMIT)),
        ]

    def test_a_combined_review_request_close_does_not_project_retire_ready(self):
        newest = f"## Gate: Review Request + Close\n- commit: {self.NEW_COMMIT}\n"
        facts, state = state_of(self._history(newest))
        self.assertNotEqual(state, LANE_STATE_RETIRE_READY)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)
        self.assertEqual(facts.latest_gate, "review_request")

    def test_a_combined_changes_requested_review_close_does_not_project_retire_ready(self):
        newest = f"## Gate: Review + Close\n- commit: {self.NEW_COMMIT}\n- 結論: 要修正\n"
        _, state = state_of(self._history(newest))
        self.assertNotEqual(state, LANE_STATE_RETIRE_READY)

    def test_the_combined_forms_agree_with_their_single_gate_controls(self):
        """The defect's signature: the state changed only because Close shared the heading."""
        for label, combined, single in (
            (
                "review_request",
                f"## Gate: Review Request + Close\n- commit: {self.NEW_COMMIT}\n",
                f"## Gate: Review Request\n- commit: {self.NEW_COMMIT}\n",
            ),
            (
                "review_changes_requested",
                f"## Gate: Review + Close\n- commit: {self.NEW_COMMIT}\n- 結論: 要修正\n",
                f"## Gate: Review\n- commit: {self.NEW_COMMIT}\n- 結論: 要修正\n",
            ),
        ):
            with self.subTest(label):
                _, combined_state = state_of(self._history(combined))
                _, single_state = state_of(self._history(single))
                self.assertEqual(combined_state, single_state)

    def test_an_approved_review_combined_with_close_still_advances(self):
        """The boundary: an APPROVED review is not an open round, so this must NOT be suppressed.

        Without this control the fix could be "any review gate blocks close", which would break
        the governed combination a coordinator legitimately writes at close time.
        """
        newest = (
            f"## Gate: Review + Close\n"
            f"- commit: {self.NEW_COMMIT}\n"
            "- 結論: 承認\n"
            "- changed_paths:\n"
            "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
        )
        facts, state = state_of(self._history(newest), issue_open=False)
        self.assertEqual(facts.latest_gate, "close")
        self.assertEqual(state, LANE_STATE_RETIRE_READY)


class ReviewJ91696IntegrationEvidenceTests(unittest.TestCase):
    """R6-F2/F3: the retire's integration evidence must name the commit and be unambiguous."""

    def _resolve(self, obs_path, *, issue="14539"):
        return _resolve_latest_generation_admissible(
            argparse.Namespace(
                review_generation_json=None,
                review_exemption_json=obs_path,
                latest_generation_admissible=False,
                issue=issue,
            )
        )

    def _obs(self, tmp, journals):
        path = Path(tmp) / "o.json"
        path.write_text(
            json.dumps(
                {
                    "issue": "14539",
                    "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def _history(self, disposition_note):
        return [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", disposition_note),
            ("104", CLOSE),
        ]

    def test_a_legacy_lane_unbound_disposition_is_not_retire_evidence(self):
        """F2: valid for the glance forever, but it cannot say WHICH work it integrated."""
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(
                self._resolve(self._obs(t, self._history(INTEGRATION_MERGED_LEGACY)))
            )

    def test_strict_evidence_for_a_different_source_head_does_not_admit(self):
        """F2's literal reproduction: the marker proves the integration of other work."""
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(
                self._resolve(self._obs(t, self._history(integration_merged("9" * 40))))
            )

    def test_strict_evidence_naming_the_covered_commit_admits(self):
        """Negative control for both: only the marker's source head differs."""
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self._resolve(self._obs(t, self._history(integration_merged(HEAD)))))

    def test_the_glance_still_reads_the_legacy_note(self):
        """The parser/consumer split: tightening the retire must not change the display fold."""
        facts, _ = state_of(self._history(INTEGRATION_MERGED_LEGACY), issue_open=False)
        self.assertTrue(facts.integration_recorded)
        self.assertEqual(facts.integration.disposition, "merge")

    def test_a_journal_declaring_two_dispositions_never_admits_in_either_order(self):
        """F3: the same durable record admitted or refused depending on line order."""
        for order in (
            ("merge", "explicit_deferral"),
            ("explicit_deferral", "merge"),
        ):
            with self.subTest(order=order):
                note = (
                    "## Integration disposition\n"
                    + "".join(f"- disposition: {d}\n" for d in order)
                    + "[mozyo:workflow-event:gate=integration_disposition:workspace=ws:lane=r1:"
                    f"lane_generation=1:head={HEAD}:integration_head={INTEGRATION_HEAD}:"
                    "integration_branch=main-next:disposition=merge]\n"
                )
                with tempfile.TemporaryDirectory() as t:
                    self.assertFalse(self._resolve(self._obs(t, self._history(note))))

    def test_an_equal_duplicate_disposition_is_not_a_conflict(self):
        """Same rule as the gate fields: the same value twice is one declaration."""
        note = (
            "## Integration disposition\n"
            "- disposition: merged\n"
            "- disposition: merge\n"
            "[mozyo:workflow-event:gate=integration_disposition:workspace=ws:lane=r1:"
            f"lane_generation=1:head={HEAD}:integration_head={INTEGRATION_HEAD}:"
            "integration_branch=main-next:disposition=merge]\n"
        )
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self._resolve(self._obs(t, self._history(note))))

    def test_the_conflict_detector_itself(self):
        """The pure helper, pinned directly rather than only through the retire."""
        conflicting = "## Integration disposition\n- disposition: merge\n- disposition: explicit_deferral\n"
        agreeing = "## Integration disposition\n- disposition: merged\n- disposition: merge\n"
        single = INTEGRATION_MERGED_LEGACY
        unqualified = "## Progress Log\n- disposition: merge\n- disposition: explicit_deferral\n"
        self.assertTrue(has_conflicting_disposition_declaration([("1", conflicting)]))
        self.assertFalse(has_conflicting_disposition_declaration([("1", agreeing)]))
        self.assertFalse(has_conflicting_disposition_declaration([("1", single)]))
        # A journal that does not structurally qualify is never a conflict — the fold could not
        # have read it in the first place.
        self.assertFalse(has_conflicting_disposition_declaration([("1", unqualified)]))

    def test_the_display_fold_keeps_its_documented_leniency(self):
        """The parser/consumer split, stated as a pin: the glance is deliberately NOT tightened.

        Its first-wins behaviour is historical and regression-tested by #14213; this issue adds a
        strict question for the AUTHORITY consumer instead of changing what the display renders.
        """
        conflicting = "## Integration disposition\n- disposition: merge\n- disposition: explicit_deferral\n"
        self.assertEqual(
            fold_integration_disposition([("1", conflicting)]).disposition, "merge"
        )

    def test_a_conflicting_disposition_elsewhere_in_the_issue_also_blocks(self):
        journals = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            (
                "103",
                "## Integration disposition\n- disposition: merge\n"
                "- disposition: integration_blocked\n",
            ),
            ("104", integration_merged(HEAD)),
            ("105", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, journals)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
