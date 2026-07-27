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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (
    fold_issue_gate_facts,
    lane_signal_from_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_exemption import (
    EXEMPTION_EXEMPT,
    EXEMPTION_INVALID,
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

REVIEW_REQUEST = "## Gate: Review Request\n- commit: 4f2b9c1a3d5e6f708192a3b4c5d6e7f809a1b2c3\n"
IMPLEMENTATION_DONE = (
    "## Gate: Implementation Done\n- commit: 4f2b9c1a3d5e6f708192a3b4c5d6e7f809a1b2c3\n"
)
CLOSE = "## Gate: Close\n- commit: 4f2b9c1a3d5e6f708192a3b4c5d6e7f809a1b2c3\n"
INTEGRATION_MERGED = "## Integration disposition\n- disposition: merged\n"


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
        facts, state = state_of([("100", REVIEW_REQUEST), ("101", GATE_EXEMPT)])
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
        facts, _ = state_of([("100", REVIEW_REQUEST), ("101", GATE_EXEMPT)])
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
        facts, state = state_of([("100", REVIEW_REQUEST), ("101", GATE_REVIEW_REQUIRED)])
        self.assertEqual(facts.review_exemption.state, EXEMPTION_REVIEW_REQUIRED)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_malformed_gate_keeps_the_review_owed(self):
        facts, state = state_of([("100", REVIEW_REQUEST), ("101", GATE_MALFORMED)])
        self.assertEqual(facts.review_exemption.state, EXEMPTION_INVALID)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_review_round_opened_after_the_exemption_re_owes_the_review(self):
        """Cross-generation: the exemption exempts what it precedes, not what supersedes it."""
        facts, state = state_of([("101", GATE_EXEMPT), ("205", REVIEW_REQUEST)])
        self.assertEqual(facts.review_exemption.state, EXEMPTION_EXEMPT)
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_newer_review_round_re_owes_even_when_impl_done_is_latest(self):
        facts, state = state_of(
            [("101", GATE_EXEMPT), ("205", REVIEW_REQUEST), ("300", IMPLEMENTATION_DONE)]
        )
        self.assertFalse(facts.review_exempt)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_a_newer_malformed_gate_shadows_an_older_valid_exemption(self):
        facts, state = state_of(
            [("101", GATE_EXEMPT), ("205", GATE_MALFORMED), ("300", REVIEW_REQUEST)]
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
