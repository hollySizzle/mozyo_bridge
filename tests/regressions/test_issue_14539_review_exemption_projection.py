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
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (
    RetireEvidenceTarget,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
    _resolve_latest_generation_admissible,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (
    canonical_marker_value,
    fold_integration_disposition,
    fold_work_unit,
    has_conflicting_disposition_declaration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (
    resolve_journal_issuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    marker_components_in_note,
    marker_fields_in_note,
    marker_logical_gates,
    strict_marker_fields,
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
    fold_review_exemption,
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

#: The lane identity the strict integration evidence must name. The retire is TOLD this via its own
#: arguments (review j#91747 finding 3) — the observation file can never certify its own binding.
EVIDENCE_WORKSPACE = "ws"
EVIDENCE_LANE = "r1"
EVIDENCE_LANE_GENERATION = "1"
INTEGRATION_BRANCH = "main-next"


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
        "[mozyo:workflow-event:gate=integration_disposition:"
        f"workspace={EVIDENCE_WORKSPACE}:lane={EVIDENCE_LANE}:"
        f"lane_generation={EVIDENCE_LANE_GENERATION}:head={source_head}:"
        f"integration_head={INTEGRATION_HEAD}:"
        f"integration_branch={INTEGRATION_BRANCH}:disposition=merge]\n"
    )


INTEGRATION_MERGED = integration_merged()



#: The committed-config anchor the issuer resolution is basised on. A blank one resolves every
#: issuer to unknown, which is the fail-closed direction and has its own pin.
EVIDENCE_POLICY_POINTER = "git:.mozyo-bridge/config.yaml@0123456789abcdef0123456789abcdef01234567"

#: The retire target as ``resolve_retire_evidence_target`` MEASURES it from the lane's lifecycle
#: row. Tests inject it directly: the point of review j#91797 finding 2 is that this identity is
#: not something the caller can choose, so there is no flag for it to come from.
EVIDENCE_TARGET = RetireEvidenceTarget(
    workspace=EVIDENCE_WORKSPACE,
    lane=EVIDENCE_LANE,
    lane_generation=int(EVIDENCE_LANE_GENERATION),
    policy_pointer=EVIDENCE_POLICY_POINTER,
)


def retire_args(obs_path, *, issue="14539", assertion=False, **over):
    """The retire namespace for the exemption route."""
    ns = dict(
        review_generation_json=None,
        review_exemption_json=obs_path,
        latest_generation_admissible=assertion,
        issue=issue,
        lane_label="issue_14539_review_exemption_projection",
        integration_branch=INTEGRATION_BRANCH,
    )
    ns.update(over)
    return argparse.Namespace(**ns)


def resolve_admissible(obs_path, *, target=EVIDENCE_TARGET, **over):
    """The retire's latest-generation resolution with a MEASURED evidence target."""
    return _resolve_latest_generation_admissible(retire_args(obs_path, **over), target=target)


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
        base = dict(review_exemption_json=None)
        base.update(over)
        return resolve_admissible(base.pop("review_exemption_json"), **base)

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
        return resolve_admissible(obs_path, issue=issue, assertion=assertion)

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
        return resolve_admissible(obs_path, issue=issue, assertion=False)

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
        return resolve_admissible(obs_path, issue=issue, assertion=False)

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
        return resolve_admissible(obs_path, issue=issue, assertion=False)

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
        return resolve_admissible(obs_path, issue=issue, assertion=False)

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
        return resolve_admissible(obs_path, issue=issue, assertion=False)

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

    def test_a_superseded_conflict_does_not_block_a_valid_current_declaration(self):
        """j#91797 F3: the conflict question is asked of the CURRENT declaration, not the history.

        Applying it issue-wide meant a malformed OLD record could never be repaired: a valid newer
        disposition superseded it for every other purpose while the conflict check kept refusing.
        That is the opposite of latest-wins.
        """
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
            self.assertTrue(self._resolve(self._obs(t, journals)))

    def test_the_conflict_must_be_on_the_current_declaration_to_block(self):
        """Negative control for the above: the SAME conflict, now current, still blocks."""
        journals = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", integration_merged(HEAD)),
            (
                "104",
                "## Integration disposition\n- disposition: merge\n"
                "- disposition: integration_blocked\n",
            ),
            ("105", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._resolve(self._obs(t, journals)))


class ReviewJ91747MarkerPathTests(unittest.TestCase):
    """R7-F1: progression suppression must be ONE decision, taken after the outcome is known.

    R6's fix added the F10 check on the heading path but left the marker branch removing the
    progression gates unconditionally and BEFORE the conclusion was resolved. So the two halves of
    the same rule disagreed in opposite directions: a canonical ``conclusion=approved`` review lost
    its Close, while a heading-only open round kept one. The record's meaning must not depend on
    whether a structured marker happens to be present.
    """

    COMMIT = "2222222222222222222222222222222222222222"

    def _history(self, newest):
        request = (
            f"## Gate: Review Request\n"
            f"- commit: {self.COMMIT}\n"
            f"[mozyo:workflow-event:gate=review_request:head={self.COMMIT}]\n"
        )
        return [
            ("99", IMPLEMENTATION_DONE),
            ("100", request),
            ("200", newest),
            ("203", integration_merged(self.COMMIT)),
        ]

    def _combined(self, *, conclusion, marker_conclusion=None):
        body = (
            f"## Gate: Review + Close\n"
            f"- commit: {self.COMMIT}\n"
            f"- 結論: {conclusion}\n"
            "- changed_paths:\n"
            "  - `vibes/docs/logics/coordinator-sublane-development-flow.md`\n"
        )
        if marker_conclusion is not None:
            body += (
                f"[mozyo:workflow-event:gate=review_result:conclusion={marker_conclusion}"
                f":head={self.COMMIT}:req=100]\n"
            )
        return body

    def test_an_approved_review_keeps_its_close_with_and_without_a_marker(self):
        """The literal defect: the marker branch dropped Close before reading the conclusion."""
        with_marker = self._history(
            self._combined(conclusion="承認", marker_conclusion="approved")
        )
        without_marker = self._history(self._combined(conclusion="承認"))
        marked, marked_state = state_of(with_marker, issue_open=False)
        plain, plain_state = state_of(without_marker, issue_open=False)
        self.assertEqual(marked.latest_gate, "close")
        self.assertEqual(plain.latest_gate, "close")
        self.assertEqual(marked_state, plain_state)
        self.assertEqual(marked_state, LANE_STATE_RETIRE_READY)

    def test_a_changes_requested_marker_still_suppresses_the_close(self):
        """The other direction must not regress: an open round is still not advanced past."""
        facts, state = state_of(
            self._history(
                self._combined(conclusion="要修正", marker_conclusion="changes_requested")
            )
        )
        self.assertNotEqual(facts.latest_gate, "close")
        self.assertNotEqual(state, LANE_STATE_RETIRE_READY)

    def test_a_blocker_marker_still_suppresses_the_close(self):
        facts, _ = state_of(
            self._history(self._combined(conclusion="blocker", marker_conclusion="blocker"))
        )
        self.assertNotEqual(facts.latest_gate, "close")

    def test_a_review_request_marker_still_suppresses_the_close(self):
        newest = (
            f"## Gate: Review Request + Close\n"
            f"- commit: {self.COMMIT}\n"
            f"[mozyo:workflow-event:gate=review_request:head={self.COMMIT}]\n"
        )
        facts, state = state_of(self._history(newest))
        self.assertNotEqual(facts.latest_gate, "close")
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)


class ReviewJ91747EvidenceBindingTests(unittest.TestCase):
    """R7-F2/F3: the strict evidence must come from the CURRENT declaration and name THIS lane."""

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

    def _resolve(self, journals, **over):
        with tempfile.TemporaryDirectory() as t:
            return resolve_admissible(self._obs(t, journals), **over)

    def test_a_newer_legacy_note_cannot_borrow_an_older_markers_source_head(self):
        """F2: source head from j100, freshness journal id from j300 — two declarations, one fence.

        The ordering fence said "the integration is no older than the change scope"; j300 satisfied
        it while carrying no evidence, and j100 supplied a head it was too old to supply.
        """
        journals = [
            ("50", GATE_EXEMPT),
            ("100", integration_merged(HEAD)),
            ("200", IMPLEMENTATION_DONE),
            ("300", INTEGRATION_MERGED_LEGACY),
            ("400", CLOSE),
        ]
        self.assertFalse(self._resolve(journals))

    def test_the_same_history_with_the_evidence_on_the_current_declaration_admits(self):
        """Negative control: only WHICH journal carries the enveloped marker differs."""
        journals = [
            ("50", GATE_EXEMPT),
            ("100", INTEGRATION_MERGED_LEGACY),
            ("200", IMPLEMENTATION_DONE),
            ("300", integration_merged(HEAD)),
            ("400", CLOSE),
        ]
        self.assertTrue(self._resolve(journals))

    #: Exemption, scope, enveloped merge, Close — the shape every binding test varies one axis of.
    def _bound_history(self):
        return [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", integration_merged(HEAD)),
            ("104", CLOSE),
        ]

    def test_the_bound_history_admits(self):
        self.assertTrue(self._resolve(self._bound_history()))

    def test_each_envelope_dimension_must_match_the_measured_target(self):
        """F3: requiring a lane-enveloped marker and ignoring the envelope is not a fence.

        Each dimension is varied on the MEASURED target (review j#91797 finding 2), not on a
        caller-supplied flag — the flags are gone precisely because pointing them at the evidence
        was free.
        """
        import dataclasses

        for label, field, value in (
            ("workspace", "workspace", "foreign_ws"),
            ("lane", "lane", "foreign_lane"),
            ("generation", "lane_generation", 999),
        ):
            with self.subTest(label):
                target = dataclasses.replace(EVIDENCE_TARGET, **{field: value})
                self.assertFalse(self._resolve(self._bound_history(), target=target))
        with self.subTest("integration_branch"):
            self.assertFalse(
                self._resolve(self._bound_history(), integration_branch="unrelated")
            )

    def test_an_unresolvable_target_fails_closed(self):
        """j#91797 F2: no measured identity is not "skip the identity check"."""
        self.assertFalse(self._resolve(self._bound_history(), target=None))

    def test_an_absent_integration_branch_fails_closed(self):
        """A missing expectation must block, never skip its own check."""
        self.assertFalse(self._resolve(self._bound_history(), integration_branch=None))

    def test_a_caller_cannot_point_the_expectation_at_the_evidence(self):
        """The literal j#91797 F2 defect: the R9 flags were free variables.

        With the flags gone the only way to match a foreign envelope is for the lane's OWN
        lifecycle row to carry that identity — which is not something the invoking caller chooses.
        """
        foreign = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            (
                "103",
                "## Integration disposition\n"
                "- disposition: merged\n"
                "[mozyo:workflow-event:gate=integration_disposition:workspace=foreign_ws:"
                f"lane=foreign_lane:lane_generation=999:head={HEAD}:"
                f"integration_head={INTEGRATION_HEAD}:integration_branch=unrelated:"
                "disposition=merge]\n",
            ),
            ("104", CLOSE),
        ]
        self.assertFalse(self._resolve(foreign, integration_branch="unrelated"))

    def test_a_foreign_lanes_evidence_does_not_admit(self):
        """The reviewer's reproduction, end to end: only the source head matched."""
        foreign = (
            "## Integration disposition\n"
            "- disposition: merged\n"
            "[mozyo:workflow-event:gate=integration_disposition:workspace=foreign_ws:"
            f"lane=foreign_lane:lane_generation=999:head={HEAD}:"
            f"integration_head={INTEGRATION_HEAD}:integration_branch=unrelated:disposition=merge]\n"
        )
        journals = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", foreign),
            ("104", CLOSE),
        ]
        self.assertFalse(self._resolve(journals))


class ReviewJ91747InlineHeadingConflictTests(unittest.TestCase):
    """R7-F4: the conflict detector enumerated two of three governed surfaces."""

    def test_two_inline_headings_disagreeing_is_a_conflict(self):
        note = (
            "## Integration disposition: merge\n"
            "## Integration disposition: explicit_deferral\n"
        )
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))

    def test_the_reverse_order_is_equally_a_conflict(self):
        note = (
            "## Integration disposition: explicit_deferral\n"
            "## Integration disposition: merge\n"
        )
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))

    def test_repeated_inline_headings_that_agree_are_not_a_conflict(self):
        """Negative control: the same value twice — including via an alias — is one declaration."""
        note = (
            "## Integration disposition: merge\n"
            "## Integration disposition: merged\n"
        )
        self.assertFalse(has_conflicting_disposition_declaration([("103", note)]))

    def test_an_inline_heading_conflicting_with_a_marker_is_a_conflict(self):
        note = (
            "## Integration disposition: explicit_deferral\n"
            + integration_merged(HEAD).split("\n", 1)[1]
        )
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))

    def test_the_retire_refuses_the_multiple_heading_record(self):
        note = (
            "## Integration disposition: merge\n"
            "## Integration disposition: explicit_deferral\n"
            + integration_merged(HEAD).split("\n", 2)[2]
        )
        journals = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", note),
            ("104", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "o.json"
            path.write_text(
                json.dumps(
                    {
                        "issue": "14539",
                        "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(resolve_admissible(str(path)))


class ReviewJ91797IssuerAuthorityTests(unittest.TestCase):
    """R9-F1: the Hibernate contract's ISSUER condition, which R7-F3 said not to drop.

    The role is resolved as policy from the note's own gate structure, anchored to the committed
    config blob — never from an author field the observation could assert, and never from the
    marker body. So the axes that matter are: can the policy basis be established at all, and does
    the note claim exactly one authority contract.
    """

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

    def _resolve(self, journals, **over):
        with tempfile.TemporaryDirectory() as t:
            return resolve_admissible(self._obs(t, journals), **over)

    def _history(self, disposition_note=None):
        return [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", disposition_note or integration_merged(HEAD)),
            ("104", CLOSE),
        ]

    def test_no_policy_basis_resolves_no_issuer_and_blocks(self):
        """An empty pointer means the binding cannot name its own basis record."""
        import dataclasses

        target = dataclasses.replace(EVIDENCE_TARGET, policy_pointer="")
        self.assertFalse(self._resolve(self._history(), target=target))

    def test_a_note_claiming_two_authority_gates_blocks(self):
        """Two contracts at once prove neither, so the issuer is unresolved."""
        two_gates = integration_merged(HEAD).rstrip("\n") + (
            "\n[mozyo:workflow-event:gate=park_declared:workspace="
            f"{EVIDENCE_WORKSPACE}:lane={EVIDENCE_LANE}:"
            f"lane_generation={EVIDENCE_LANE_GENERATION}:head={HEAD}]\n"
        )
        self.assertFalse(self._resolve(self._history(two_gates)))

    def test_the_single_coordinator_gate_history_admits(self):
        """Negative control for both: only the issuer resolvability differs."""
        self.assertTrue(self._resolve(self._history()))

    def test_author_metadata_is_deliberately_not_an_authority_input(self):
        """Author metadata is NOT authority here, and that is an owner ruling — not an oversight.

        #14219 j#86718 (Fork A) decided: "canonical gate structure → writer role の対応を採用する
        … ただしこれは issuer identity の認証ではない。単一 Redmine user workspace では role を
        個人identityから識別不能であり、「その gate kind を書く契約上の role」を表す policy binding
        に限定する。author一致だけで authority を満たしたと扱わず、exact lane/generation/head、
        request相関、actor-specific grammar、corroborationを引き続き必須とする."

        So this test does NOT assert "forged metadata is fine". It asserts that the observation
        cannot promote OR demote itself by asserting an author — the resolution ignores the claim
        in both directions — and the defences that actually reject a forgery are the layered ones
        the ruling keeps mandatory: the measured lane envelope, the covered-commit binding, the
        current-declaration selection and the conflict checks, each pinned separately above.
        """
        journals = self._history()
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "o.json"
            path.write_text(
                json.dumps(
                    {
                        "issue": "14539",
                        "journals": [
                            {
                                "journal_id": j,
                                "notes": n,
                                "issuer_role": "lane_worker",
                                "author": "someone-else",
                                "authority_anchor": "forged",
                            }
                            for j, n in journals
                        ],
                    }
                ),
                encoding="utf-8",
            )
            # Unchanged from the control above: the metadata is inert, in both directions.
            self.assertTrue(resolve_admissible(str(path)))


class ReviewJ91797MarkerMultiplicityTests(unittest.TestCase):
    """R9-F4: exactly-one has to hold INSIDE a surface, not only across surfaces.

    The shared marker scan folds a body to a dict, so a repeated key is erased before any consumer
    sees it. Enumerating the three surfaces (R7-F4) still left each surface's own contents assumed
    to be single-valued — the fourth first-wins defect in this issue.
    """

    def _marker(self, *dispositions, gate="integration_disposition"):
        body = (
            f"gate={gate}:workspace={EVIDENCE_WORKSPACE}:lane={EVIDENCE_LANE}:"
            f"lane_generation={EVIDENCE_LANE_GENERATION}:head={HEAD}:"
            f"integration_head={INTEGRATION_HEAD}:integration_branch={INTEGRATION_BRANCH}"
        )
        for value in dispositions:
            body += f":disposition={value}"
        return f"## Integration disposition\n[mozyo:workflow-event:{body}]\n"

    def _resolve(self, note):
        journals = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", note),
            ("104", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "o.json"
            path.write_text(
                json.dumps(
                    {
                        "issue": "14539",
                        "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                    }
                ),
                encoding="utf-8",
            )
            return resolve_admissible(str(path))

    def test_a_repeated_key_with_different_values_is_a_conflict_in_either_order(self):
        for order in (("explicit_deferral", "merge"), ("merge", "explicit_deferral")):
            with self.subTest(order=order):
                note = self._marker(*order)
                self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))
                self.assertFalse(self._resolve(note))

    def test_a_repeated_key_with_the_same_value_is_not_a_conflict(self):
        """Negative control: identical repetition is one declaration, as everywhere else."""
        note = self._marker("merge", "merge")
        self.assertFalse(has_conflicting_disposition_declaration([("103", note)]))
        self.assertTrue(self._resolve(note))

    def test_the_single_valued_marker_admits(self):
        self.assertTrue(self._resolve(self._marker("merge")))

    def test_a_malformed_fragment_is_a_conflict(self):
        """A body that does not parse cleanly is not a declaration this path may act on."""
        note = self._marker("merge").replace(":head=", ":bare_fragment:head=")
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))

    def test_the_raw_scanner_preserves_multiplicity(self):
        """The grammar half, pinned directly: the dict fold is what loses the duplicate."""
        note = self._marker("explicit_deferral", "merge")
        collapsed = [f for ch, f in marker_fields_in_note(note) if ch == "workflow-event"]
        raw = [c for ch, c in marker_components_in_note(note) if ch == "workflow-event"]
        self.assertEqual(collapsed[0]["disposition"], "merge")  # last write won
        self.assertEqual(
            [v for k, v in raw[0] if k == "disposition"], ["explicit_deferral", "merge"]
        )


class ReviewJ91847AliasAndComponentTests(unittest.TestCase):
    """R11-F3/F4: one logical field spelled two ways, and a component grammar that dropped input.

    Both are defects in the conflict detector R11 introduced. ``gate`` and ``kind`` are aliases,
    so reading the first non-empty one let a second, DIFFERENT authority gate hide in the other
    spelling; and the raw component scanner promised "nothing is dropped" while skipping empty
    components, so a body no canonical producer can render read as well-formed.
    """

    def _marker(self, body_extra: str = "", *, gate_field: str = "gate=integration_disposition",
                disposition: str = "merge") -> str:
        return (
            "## Integration disposition\n"
            f"[mozyo:workflow-event:{gate_field}:workspace={EVIDENCE_WORKSPACE}:"
            f"lane={EVIDENCE_LANE}:lane_generation={EVIDENCE_LANE_GENERATION}{body_extra}:"
            f"head={HEAD}:integration_head={INTEGRATION_HEAD}:"
            f"integration_branch={INTEGRATION_BRANCH}:disposition={disposition}]\n"
        )

    def _resolve(self, note):
        journals = [
            ("101", GATE_EXEMPT),
            ("102", IMPLEMENTATION_DONE),
            ("103", note),
            ("104", CLOSE),
        ]
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "o.json"
            path.write_text(
                json.dumps(
                    {
                        "issue": "14539",
                        "journals": [{"journal_id": j, "notes": n} for j, n in journals],
                    }
                ),
                encoding="utf-8",
            )
            return resolve_admissible(str(path))

    def test_a_conflicting_kind_alias_is_a_conflict(self):
        """F3: a second authority gate spelled as ``kind`` must not hide behind ``gate``."""
        note = self._marker(gate_field="gate=integration_disposition:kind=park_declared")
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))
        self.assertFalse(self._resolve(note))

    def test_the_conflicting_alias_also_unresolves_the_issuer(self):
        """The shared resolver reads BOTH spellings, so two contracts prove neither."""
        note = self._marker(gate_field="gate=integration_disposition:kind=park_declared")
        issuer = resolve_journal_issuer("103", note, policy_pointer=EVIDENCE_POLICY_POINTER)
        self.assertEqual(issuer.role, "unknown")

    def test_the_alias_alone_still_resolves(self):
        """Negative control: ``kind`` is a spelling of the same field, not a second claim."""
        note = self._marker(gate_field="kind=integration_disposition")
        self.assertFalse(has_conflicting_disposition_declaration([("103", note)]))
        issuer = resolve_journal_issuer("103", note, policy_pointer=EVIDENCE_POLICY_POINTER)
        self.assertEqual(issuer.role, "coordinator")

    def test_an_empty_component_is_preserved_and_refuses_the_marker(self):
        """F4a: the scanner's own docstring promised this and the code dropped it."""
        note = self._marker(body_extra=":")
        raw = [c for ch, c in marker_components_in_note(note) if ch == "workflow-event"]
        self.assertIn(("", ""), raw[0])
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))
        self.assertFalse(self._resolve(note))

    def test_a_canonical_equivalent_duplicate_is_not_a_conflict(self):
        """F4b: ``merged`` and ``merge`` are one declaration written twice."""
        note = self._marker(disposition="merged").replace(
            "disposition=merged]", "disposition=merged:disposition=merge]"
        )
        self.assertFalse(has_conflicting_disposition_declaration([("103", note)]))
        self.assertTrue(self._resolve(note))

    def test_a_genuinely_different_disposition_is_still_a_conflict(self):
        """The boundary: canonicalizing must not collapse two DIFFERENT dispositions."""
        note = self._marker(disposition="merge").replace(
            "disposition=merge]", "disposition=merge:disposition=explicit_deferral]"
        )
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))

    def test_a_repeated_non_governed_key_is_still_a_conflict(self):
        """A key with no canonical form compares literally, which is the fail-closed reading."""
        note = self._marker().replace(f"head={HEAD}", f"head={HEAD}:head={'9' * 40}")
        self.assertTrue(has_conflicting_disposition_declaration([("103", note)]))

    def test_the_well_formed_marker_still_admits(self):
        self.assertTrue(self._resolve(self._marker()))


class ReviewJ91847CommitPointFenceTests(unittest.TestCase):
    """R11-F2: the generic guarded close had no commit-point CAS.

    The hibernated-bound and active-live-zero intents already pass ``expected_revision`` to a
    bounded CAS (``CAS_STALE_REVISION``); the generic ``--execute`` close just closes panes, so a
    lane that advanced a generation between the admissibility decision and the close would have
    had the NEW lane's slots closed on the OLD lane's evidence.
    """

    def test_the_attestation_takes_the_expectation_it_was_admitted_against(self):
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_actuation import (  # noqa: E501
            attest_retire_target,
        )

        params = inspect.signature(attest_retire_target).parameters
        self.assertIn("expected_generation", params)
        self.assertIn("expected_revision", params)
        # Both default to None so an intent with no expectation is unchanged, never loosened.
        self.assertIsNone(params["expected_generation"].default)
        self.assertIsNone(params["expected_revision"].default)

    def test_the_measured_target_carries_the_revision(self):
        """The expectation has to exist before it can be carried."""
        self.assertEqual(EVIDENCE_TARGET.lane_generation, int(EVIDENCE_LANE_GENERATION))
        target = RetireEvidenceTarget(
            workspace="ws", lane="r1", lane_generation=3, policy_pointer="", revision=7
        )
        self.assertEqual(target.revision, 7)

    def test_the_guarded_close_forwards_the_expectation(self):
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_retire_actuation,
        )

        source = inspect.getsource(sublane_retire_actuation.run_guarded_retire_close)
        self.assertIn("expected_generation=", source)
        self.assertIn("expected_revision=", source)

    def test_an_unresolvable_issuer_basis_does_not_disable_the_expectation(self):
        """The two questions are separate: an unreadable config must not drop the CAS axis.

        ``policy_pointer`` empty fails the exemption route closed on its own (pinned above); it
        must not also blank the generation / revision the destructive close re-reads.
        """
        target = RetireEvidenceTarget(
            workspace="ws", lane="r1", lane_generation=2, policy_pointer="", revision=5
        )
        self.assertEqual((target.lane_generation, target.revision), (2, 5))


class ReviewJ91797WorkUnitTests(unittest.TestCase):
    """R9-F5: ``fold_work_unit`` routed review AUTHORITY by line order (pre-existing).

    ``user_story`` sends the Review Gate to the auditor's US-level audit and ``leaf_issue`` to the
    same-lane implementation_gateway, so a journal declaring both picked an owner by which line
    came first.
    """

    def test_conflicting_work_unit_declarations_fold_to_undeclared_in_either_order(self):
        for order in (("user_story", "leaf_issue"), ("leaf_issue", "user_story")):
            with self.subTest(order=order):
                note = (
                    "## Gate: Review Request\n"
                    f"- work_unit: {order[0]}\n"
                    f"- work_unit: {order[1]}\n"
                )
                self.assertEqual(fold_work_unit([("100", note)]), "")

    def test_an_equal_duplicate_declaration_still_resolves(self):
        """Negative control: identical repetition is one declaration, not a conflict."""
        note = (
            "## Gate: Review Request\n- work_unit: user_story\n- work_unit: user_story\n"
        )
        self.assertEqual(fold_work_unit([("100", note)]), "user_story")

    def test_a_single_declaration_is_unchanged(self):
        for token in ("user_story", "leaf_issue"):
            with self.subTest(token):
                note = f"## Gate: Review Request\n- work_unit: {token}\n"
                self.assertEqual(fold_work_unit([("100", note)]), token)

    def test_the_undeclared_fold_routes_to_the_same_lane_side(self):
        """Why "" is the fail-closed answer: it never claims US-level audit authority."""
        conflicting = (
            "## Gate: Review Request\n- work_unit: user_story\n- work_unit: leaf_issue\n"
        )
        self.assertNotEqual(fold_work_unit([("100", conflicting)]), "user_story")

    def test_latest_wins_still_holds_across_journals(self):
        """The per-journal fix must not disturb the supersession rule between journals."""
        older = "## Gate: Review Request\n- work_unit: user_story\n"
        newer = "## Gate: Review Request\n- work_unit: leaf_issue\n"
        self.assertEqual(fold_work_unit([("100", older), ("200", newer)]), "leaf_issue")


class ReviewJ91896StrictMarkerGrammarTests(unittest.TestCase):
    """R13-F2/F3: ONE strict reader, over uncollapsed components, shared by every authority.

    The previous rounds fixed the terminal-retire conflict detector while the shared issuer
    resolver kept reading the lenient collapsed dict — so a repeated key was erased before it got
    there and surrounding whitespace was normalized into a clean-looking field. Both counterexamples
    resolved to a coordinator issuer with strict evidence intact.
    """

    def _components(self, body: str):
        return [c for ch, c in marker_components_in_note(f"[mozyo:workflow-event:{body}]")][0]

    def _evidence_body(self, gate_field="gate=integration_disposition", extra="",
                       disposition="disposition=merge"):
        return (
            f"{gate_field}:workspace={EVIDENCE_WORKSPACE}:lane={EVIDENCE_LANE}:"
            f"lane_generation={EVIDENCE_LANE_GENERATION}{extra}:head={HEAD}:"
            f"integration_head={INTEGRATION_HEAD}:integration_branch={INTEGRATION_BRANCH}:"
            f"{disposition}"
        )

    def _issuer(self, body: str):
        return resolve_journal_issuer(
            "103", f"[mozyo:workflow-event:{body}]", policy_pointer=EVIDENCE_POLICY_POINTER
        )

    def test_whitespace_around_a_component_refuses_the_marker(self):
        """F3: the contract lists whitespace contamination beside empty and missing-``=``."""
        body = self._evidence_body(gate_field="gate = integration_disposition")
        self.assertIsNone(strict_marker_fields(self._components(body)))
        self.assertEqual(self._issuer(body).role, "unknown")

    def test_whitespace_around_a_value_refuses_the_marker(self):
        body = self._evidence_body(disposition="disposition= merge")
        self.assertIsNone(strict_marker_fields(self._components(body)))

    def test_an_empty_key_refuses_the_marker(self):
        body = self._evidence_body(extra=":=orphan")
        self.assertIsNone(strict_marker_fields(self._components(body)))

    def test_a_repeated_key_survives_the_collapse_and_refuses(self):
        """F2 counterexample B: the lenient dict erased the duplicate before anyone saw it."""
        body = self._evidence_body(gate_field="gate=park_declared:gate=integration_disposition")
        self.assertIsNone(strict_marker_fields(self._components(body)))
        self.assertEqual(self._issuer(body).role, "unknown")

    def test_an_unrecognized_second_gate_still_counts_as_a_claim(self):
        """F2 counterexample A: skipping the unknown token made ONE contract look declared."""
        body = self._evidence_body(gate_field="gate=integration_disposition:kind=unknown_gate")
        fields = strict_marker_fields(self._components(body))
        self.assertEqual(
            sorted(marker_logical_gates(fields)), ["integration_disposition", "unknown_gate"]
        )
        self.assertEqual(self._issuer(body).role, "unknown")

    def test_a_sole_unrecognized_gate_resolves_nothing(self):
        """An unknown gate is not a contract, so it must not yield an anchored issuer either."""
        issuer = self._issuer(self._evidence_body(gate_field="gate=not_a_contract"))
        self.assertEqual(issuer.role, "unknown")
        self.assertFalse(issuer.authority_anchor)

    def test_the_clean_marker_still_resolves_the_coordinator(self):
        """Negative control for all of the above."""
        issuer = self._issuer(self._evidence_body())
        self.assertEqual(issuer.role, "coordinator")
        self.assertTrue(issuer.authority_anchor)

    def test_a_delivery_marker_beside_the_evidence_does_not_unresolve_it(self):
        """The alias union is only safe because the handoff CHANNEL is not authority.

        A handoff delivery record carries ``kind=``, so once ``kind`` became a gate alias a
        delivery note sitting in the same journal would have read as a second authority claim.
        """
        note = (
            "[mozyo:handoff:source=redmine:issue=14539:journal=1:kind=review_result:to=claude]\n"
            f"[mozyo:workflow-event:{self._evidence_body()}]\n"
        )
        issuer = resolve_journal_issuer("103", note, policy_pointer=EVIDENCE_POLICY_POINTER)
        self.assertEqual(issuer.role, "coordinator")

    def test_every_authority_consumer_agrees_on_a_canonical_duplicate(self):
        """The three consumers share one reader AND one canonicalizer, so they cannot disagree."""
        body = self._evidence_body(disposition="disposition=merged:disposition=merge")
        note = f"## Integration disposition\n[mozyo:workflow-event:{body}]\n"
        self.assertFalse(has_conflicting_disposition_declaration([("103", note)]))
        self.assertEqual(self._issuer(body).role, "coordinator")
        self.assertIsNotNone(
            strict_marker_fields(self._components(body), canonicalize=canonical_marker_value)
        )


class ReviewJ91896TargetBindingTests(unittest.TestCase):
    """R13-F1, pinned BEHAVIOURALLY: the destructive close must not run (review j#91943 F2).

    The first version of these pins asserted on ``inspect.getsource`` — substrings, counts and
    ordering. That is green under a helper extraction, a same-named dead branch, an inverted
    condition, or an ignored attest result, i.e. it pins the text and not the safety boundary.
    Each case here drives ``run_guarded_retire_close`` against a temp lifecycle store with the
    close mocked, and asserts the close was called ZERO times.
    """

    LANE = "issue_14539_target_binding"
    ISSUE = "14539"

    def _run(self, *, target_kind: str):
        """Drive one guarded close; return ``(close_call_count, result)``.

        ``target_kind`` selects the boundary: ``ok`` (the control), ``foreign`` (a target naming
        another workspace), ``none`` (unresolvable), ``advanced`` (the row moves on after the
        first attestation, which is the check-to-act race).
        """
        import argparse
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from mozyo_bridge.core.state.lane_lifecycle import (
            DecisionPointer,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )
        from mozyo_bridge.core.state.lane_metadata import record_lane_created
        from mozyo_bridge.core.state.workspace_registry import register_workspace
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as proj,
            sublane_herdr_retire as retire_mod,
            sublane_retire_actuation as actuation,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            RetireEvidenceTarget,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_directory_lane_token,
        )

        def _row(ws, role, lane, locator):
            return {
                "workspace_id": ws,
                "agent_role": role,
                "lane_id": lane,
                "locator": locator,
                "name": f"mzb1_{ws}_{role}_{lane or 'default'}",
                "health": "healthy",
            }

        calls: list = []

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            root = Path(tmp) / "ws"
            root.mkdir()
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ws_id = register_workspace(root, home=home).record.workspace_id
                token = derive_directory_lane_token(str(root.resolve()), self.LANE)
                store = LaneLifecycleStore()
                store.declare_active(
                    LaneLifecycleKey(ws_id, self.LANE),
                    decision=DecisionPointer(
                        source="redmine", issue_id=self.ISSUE, journal_id="1"
                    ),
                    issue_id=self.ISSUE,
                    worktree_identity=token,
                )
                record_lane_created(
                    lane_workspace_token=token,
                    repo_workspace_id=ws_id,
                    issue_id=self.ISSUE,
                    lane_label=self.LANE,
                    branch=self.LANE,
                    worktree_path=str(root),
                    lane_id=self.LANE,
                    home=home,
                )
                declared = store.get(LaneLifecycleKey(ws_id, self.LANE))

                if target_kind == "none":
                    target = None
                elif target_kind == "foreign":
                    target = RetireEvidenceTarget(
                        workspace="foreign_workspace",
                        lane=self.LANE,
                        lane_generation=declared.lane_generation,
                        policy_pointer="",
                        revision=declared.revision,
                    )
                else:
                    target = RetireEvidenceTarget(
                        workspace=ws_id,
                        lane=self.LANE,
                        lane_generation=declared.lane_generation,
                        policy_pointer="",
                        revision=declared.revision,
                    )

                rows = [
                    _row(ws_id, "codex", self.LANE, "w2:p8"),
                    _row(ws_id, "claude", self.LANE, "w2:p9"),
                ]

                def _fake_close(plan, **kw):
                    calls.append(plan)
                    return retire_mod.HerdrRetireCloseResult(
                        workspace_id=plan.workspace_id,
                        lane_id=plan.lane_id,
                        closed=plan.close_targets,
                        foreign_names=plan.foreign_names,
                    )

                def _rows_then_advance(*_a, **_kw):
                    """The race: the row moves on between the first attest and the close."""
                    if target_kind == "advanced":
                        store.transition_disposition(
                            LaneLifecycleKey(ws_id, self.LANE),
                            expected_disposition=declared.lane_disposition,
                            expected_revision=declared.revision,
                            target="hibernated",
                            decision=DecisionPointer(
                                source="redmine", issue_id=self.ISSUE, journal_id="2"
                            ),
                        )
                    return rows

                args = argparse.Namespace(
                    worktree=str(root), lane_label=self.LANE, issue=self.ISSUE
                )
                with patch.object(
                    proj, "list_herdr_agent_rows", side_effect=_rows_then_advance
                ), patch.object(
                    retire_mod, "execute_herdr_retire_close", side_effect=_fake_close
                ):
                    result = actuation.run_guarded_retire_close(
                        args, root, evidence_target=target
                    )
        return len(calls), result

    def test_the_control_history_actually_closes(self):
        """Without a control the three refusals below could all be a broken fixture."""
        closes, _ = self._run(target_kind="ok")
        self.assertEqual(closes, 1)

    def test_a_foreign_workspace_target_closes_nothing(self):
        """Counter equality is not identity: another workspace's row must not drive this close."""
        closes, result = self._run(target_kind="foreign")
        self.assertEqual(closes, 0)
        self.assertEqual(result.reason, "lane_target_unresolved")

    def test_an_unresolvable_target_closes_nothing(self):
        closes, result = self._run(target_kind="none")
        self.assertEqual(closes, 0)
        self.assertEqual(result.reason, "lane_target_unresolved")

    def test_a_row_that_advances_after_the_first_attest_closes_nothing(self):
        """The check-to-act race: the row moves on between the attestation and the close."""
        closes, result = self._run(target_kind="advanced")
        self.assertEqual(closes, 0)
        self.assertEqual(result.reason, "lane_generation_drift")


class ReviewJ91943HibernateAuthorityReaderTests(unittest.TestCase):
    """R15-F1: the Hibernate authority surface still read the LENIENT collapsed fold.

    R15 claimed ``strict_marker_fields`` was "the reader every authority consumer shares". It was
    not: the basis declaration scan, the park evidence scan and the dogfood receipt scan all still
    called ``marker_fields_in_note``, so four bodies the canonical producer cannot emit each
    yielded a receipt byte-identical to the genuine one.
    """

    HEAD = "a" * 40

    class _Selected:
        repo_workspace_id = "ws"
        lane_id = "r1"
        lane_generation = 1

    def _delegation(self, gate_field="gate=dogfood_delegated"):
        return (
            f"[mozyo:workflow-event:{gate_field}:workspace=ws:lane=r1:lane_generation=1:"
            f"head={self.HEAD}:release_issue=900:acceptance=j%23123]"
        )

    def _receipts(self, receipt_marker, *, delegation=None, reads=None):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            read_dogfood_receipts,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            EvidenceJournal,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )

        page = [RedmineJournalEntry(issue_id="900", journal_id="9", notes=receipt_marker)]

        def _entries(issue):
            if reads is not None:
                reads.append(issue)
            return page

        return read_dogfood_receipts(
            [EvidenceJournal(journal_id="1", notes=delegation or self._delegation())],
            self._Selected(),
            _entries,
        )

    def _receipt(self, gate_field="gate=dogfood_receipt", channel="workflow-event",
                source="source_issue=500"):
        return f"[mozyo:{channel}:{gate_field}:{source}:head={self.HEAD}]"

    def test_the_genuine_receipt_is_read(self):
        """The control: without it every refusal below could be a broken fixture."""
        got = self._receipts(self._receipt())
        self.assertEqual(
            {k: (v.source_issue, v.head) for k, v in got.items()},
            {"900": ("500", self.HEAD)},
        )

    def test_the_four_producer_impossible_receipts_yield_nothing(self):
        for label, marker in (
            ("whitespace", self._receipt(gate_field="gate = dogfood_receipt")),
            ("duplicate key", self._receipt(source="source_issue=999:source_issue=500")),
            ("unknown second alias",
             self._receipt(gate_field="gate=dogfood_receipt:kind=unknown_gate")),
            ("handoff channel", self._receipt(channel="handoff")),
        ):
            with self.subTest(label):
                self.assertEqual(self._receipts(marker), {})

    def test_a_malformed_current_delegation_triggers_no_external_read(self):
        """``read_dogfood_receipts``' own docstring promises zero reads for a malformed one."""
        for label, gate_field in (
            ("whitespace", "gate = dogfood_delegated"),
            ("unknown second alias", "gate=dogfood_delegated:kind=unknown_gate"),
        ):
            with self.subTest(label):
                reads: list = []
                got = self._receipts(
                    self._receipt(),
                    delegation=self._delegation(gate_field=gate_field),
                    reads=reads,
                )
                self.assertEqual(reads, [])
                self.assertEqual(got, {})

    def test_the_genuine_delegation_does_read_once(self):
        """Negative control for the above: only the delegation's renderability differs."""
        reads: list = []
        self._receipts(self._receipt(), reads=reads)
        self.assertEqual(reads, ["900"])

    def test_park_evidence_refuses_a_producer_impossible_body(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            _park_evidences,
        )

        clean = (
            f"[mozyo:workflow-event:gate=park_declared:workspace=ws:lane=r1:"
            f"lane_generation=1:head={self.HEAD}]"
        )
        self.assertEqual(len(_park_evidences(clean)), 1)
        for label, marker in (
            ("whitespace", clean.replace("gate=park_declared", "gate = park_declared")),
            ("unknown second alias",
             clean.replace("gate=park_declared", "gate=park_declared:kind=unknown_gate")),
            ("handoff channel", clean.replace("[mozyo:workflow-event:", "[mozyo:handoff:")),
        ):
            with self.subTest(label):
                self.assertEqual(_park_evidences(marker), [])

    def test_the_basis_marker_scan_refuses_a_producer_impossible_body(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _markers_of,
        )

        clean = f"[mozyo:workflow-event:gate=integration_disposition:head={self.HEAD}]"
        self.assertEqual(len(_markers_of(clean, "integration_disposition")), 1)
        for label, marker in (
            ("whitespace",
             clean.replace("gate=integration_disposition", "gate = integration_disposition")),
            ("unknown second alias",
             clean.replace("gate=integration_disposition",
                           "gate=integration_disposition:kind=unknown_gate")),
            ("handoff channel", clean.replace("[mozyo:workflow-event:", "[mozyo:handoff:")),
        ):
            with self.subTest(label):
                self.assertEqual(_markers_of(marker, "integration_disposition"), ())


class ReviewJ92012SupersessionTests(unittest.TestCase):
    """R17-F1: strict parsing must not resurrect a superseded declaration.

    ``_latest_gate_declaration`` documents "latest-wins by existence … however its marker turns
    out to parse". Keying the scan on the STRICT markers collapsed existence into readability, so
    a newer malformed declaration produced no markers, its journal was skipped, and an OLDER valid
    declaration came back as current — with its external read and its evidence.
    """

    HEAD = "a" * 40

    class _Selected:
        repo_workspace_id = "ws"
        lane_id = "r1"
        lane_generation = 1

    def _delegation(self, gate_field="gate=dogfood_delegated"):
        return (
            f"[mozyo:workflow-event:{gate_field}:workspace=ws:lane=r1:lane_generation=1:"
            f"head={self.HEAD}:release_issue=900:acceptance=j%23123]"
        )

    def _run(self, journals):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            read_dogfood_receipts,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            EvidenceJournal,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )

        reads: list = []

        def _entries(issue):
            reads.append(issue)
            return [
                RedmineJournalEntry(
                    issue_id="900",
                    journal_id="9",
                    notes=(
                        "[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:"
                        f"head={self.HEAD}]"
                    ),
                )
            ]

        got = read_dogfood_receipts(
            [EvidenceJournal(journal_id=j, notes=n) for j, n in journals],
            self._Selected(),
            _entries,
        )
        return reads, got

    def test_the_sole_valid_declaration_is_current(self):
        """The control: without it the shadow assertions could be a broken fixture."""
        reads, got = self._run([("1", self._delegation())])
        self.assertEqual(reads, ["900"])
        self.assertEqual(set(got), {"900"})

    def test_a_newer_malformed_declaration_shadows_the_older_valid_one(self):
        """The literal defect: j2 is unreadable, so j1 must NOT come back as current."""
        reads, got = self._run(
            [
                ("1", self._delegation()),
                ("2", self._delegation(gate_field="gate = dogfood_delegated")),
            ]
        )
        self.assertEqual(reads, [])
        self.assertEqual(got, {})

    def test_the_shadow_holds_for_every_malformation(self):
        for label, gate_field in (
            ("whitespace", "gate = dogfood_delegated"),
            ("unknown second alias", "gate=dogfood_delegated:kind=unknown_gate"),
        ):
            with self.subTest(label):
                reads, got = self._run(
                    [("1", self._delegation()), ("2", self._delegation(gate_field=gate_field))]
                )
                self.assertEqual(reads, [])
                self.assertEqual(got, {})

    def test_a_newer_VALID_declaration_supersedes_normally(self):
        """Negative control: the shadow rule must not make every newer journal unusable."""
        reads, got = self._run([("1", self._delegation()), ("2", self._delegation())])
        self.assertEqual(reads, ["900"])
        self.assertEqual(set(got), {"900"})

    def test_the_declaration_scan_sees_a_gate_however_it_parses(self):
        """The grammar half: existence and readability are separate questions."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            declares_gate,
            strict_gate_markers,
        )

        malformed = self._delegation(gate_field="gate = dogfood_delegated")
        self.assertTrue(declares_gate(malformed, "dogfood_delegated"))
        self.assertEqual(strict_gate_markers(malformed, "dogfood_delegated"), ())

    def test_the_shadow_rule_covers_the_other_gates_sharing_the_selector(self):
        """``_latest_gate_declaration`` is shared, so the invariant must hold for every gate."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _latest_gate_declaration,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            EvidenceJournal,
        )

        for gate in ("review_result", "required_ci_green", "park_declared"):
            with self.subTest(gate):
                clean = f"[mozyo:workflow-event:gate={gate}:workspace=ws:lane=r1]"
                malformed = f"[mozyo:workflow-event:gate = {gate}:workspace=ws:lane=r1]"
                decl = _latest_gate_declaration(
                    [
                        EvidenceJournal(journal_id="1", notes=clean),
                        EvidenceJournal(journal_id="2", notes=malformed),
                    ],
                    gate=gate,
                )
                # The NEWER journal is current, and it carries no usable evidence.
                self.assertEqual(decl.journal, "2")
                self.assertEqual(decl.markers, ())


class ReviewJ92012OwnerApprovalTests(unittest.TestCase):
    """R17-F2: the bound-pair owner approval readers are destructive authority.

    An exact approval match admits the replacement transaction / guarded close, so the lenient
    fold accepted three bodies the canonical producer cannot emit as genuine owner approvals.
    """

    def _modules(self):
        import importlib

        base = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
        )
        return (
            importlib.import_module(base + "sublane_hibernated_bound_pair_convergence_live"),
            importlib.import_module(
                base + "sublane_hibernated_bound_pair_composer_discard_live"
            ),
        )

    def test_the_genuine_owner_approval_is_read(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            strict_gate_markers,
        )

        for module in self._modules():
            with self.subTest(module.__name__.rsplit(".", 1)[-1]):
                gate = module.APPROVAL_GATE
                clean = f"[mozyo:workflow-event:gate={gate}:issue=500:lane=r1]"
                self.assertEqual(len(strict_gate_markers(clean, gate)), 1)

    def test_producer_impossible_owner_approvals_are_refused(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            strict_gate_markers,
        )

        for module in self._modules():
            gate = module.APPROVAL_GATE
            clean = f"[mozyo:workflow-event:gate={gate}:issue=500:lane=r1]"
            for label, marker in (
                ("whitespace", clean.replace(f"gate={gate}", f"gate = {gate}")),
                ("duplicate key", clean.replace("issue=500", "issue=999:issue=500")),
                ("unknown second alias",
                 clean.replace(f"gate={gate}", f"gate={gate}:kind=unknown_gate")),
                ("handoff channel",
                 clean.replace("[mozyo:workflow-event:", "[mozyo:handoff:")),
            ):
                with self.subTest(f"{module.__name__.rsplit('.', 1)[-1]}/{label}"):
                    self.assertEqual(strict_gate_markers(marker, gate), ())

    def test_both_readers_go_through_the_shared_strict_reader(self):
        """Neither may keep a private lenient scan — that is how this drifted in the first place."""
        import inspect

        for module in self._modules():
            with self.subTest(module.__name__.rsplit(".", 1)[-1]):
                source = inspect.getsource(module)
                self.assertIn("strict_gate_markers(entry.notes, APPROVAL_GATE)", source)
                self.assertNotIn("marker_fields_in_note", source)


class ReviewJ92012CanonicalizerParityTests(unittest.TestCase):
    """R17-F3: the shared gate reader dropped the governed canonicalizer.

    R15 fixed "the consumers disagree about the same marker" by giving them one canonicalizer;
    R17's new shared helper called the strict reader without it, so a canonical duplicate was one
    declaration for the terminal consumers and an unreadable body for the Hibernate basis.
    """

    HEAD = "a" * 40

    def _duplicate_note(self):
        return (
            "[mozyo:workflow-event:gate=integration_disposition:"
            f"head={self.HEAD}:disposition=merged:disposition=merge]"
        )

    def test_every_integration_disposition_consumer_returns_the_same_verdict(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _markers_of,
        )

        note = self._duplicate_note()
        components = [
            c
            for ch, c in marker_components_in_note(note)
            if ch == "workflow-event"
        ][0]

        # terminal strict read, Hibernate basis read, and the conflict detector must agree that
        # ``merged`` + ``merge`` is ONE declaration written twice.
        self.assertIsNotNone(
            strict_marker_fields(components, canonicalize=canonical_marker_value)
        )
        self.assertEqual(len(_markers_of(note, "integration_disposition")), 1)
        self.assertFalse(
            has_conflicting_disposition_declaration(
                [("1", "## Integration disposition\n" + note)]
            )
        )

    def test_a_genuinely_different_duplicate_is_refused_everywhere(self):
        """The boundary: parity must not mean "accept everything"."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _markers_of,
        )

        note = self._duplicate_note().replace("disposition=merge]", "disposition=explicit_deferral]")
        components = [
            c for ch, c in marker_components_in_note(note) if ch == "workflow-event"
        ][0]
        self.assertIsNone(
            strict_marker_fields(components, canonicalize=canonical_marker_value)
        )
        self.assertEqual(_markers_of(note, "integration_disposition"), ())
        self.assertTrue(
            has_conflicting_disposition_declaration(
                [("1", "## Integration disposition\n" + note)]
            )
        )


class ReviewJ92060ReviewRequestSelectorTests(unittest.TestCase):
    """R19-F1: the review_request-specific selectors kept the old existence/readability conflation.

    R17 moved ``_latest_gate_declaration`` to declaration-based selection but left the two
    review_request selectors in the SAME module keying on the strict markers, so a malformed newer
    request neither shadowed the older one nor reopened a closed round.
    """

    HEAD = "a" * 40

    def _request(self, gate_field="gate=review_request"):
        return (
            f"[mozyo:workflow-event:{gate_field}:workspace=ws:lane=r1:"
            f"lane_generation=1:head={self.HEAD}]"
        )

    def _journals(self, pairs):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            EvidenceJournal,
        )

        return [EvidenceJournal(journal_id=j, notes=n) for j, n in pairs]

    def test_the_sole_valid_request_is_the_answered_one(self):
        """Control: without it the shadow assertion could be a broken fixture."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _answered_review_request,
        )

        journal, head = _answered_review_request(
            self._journals([("1", self._request())]), result_journal="3"
        )
        self.assertEqual((journal, head), ("1", self.HEAD))

    def test_a_malformed_newer_request_shadows_the_older_valid_one(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _answered_review_request,
        )

        for label, gate_field in (
            ("whitespace", "gate = review_request"),
            ("unknown second alias", "gate=review_request:kind=unknown_gate"),
        ):
            with self.subTest(label):
                journal, head = _answered_review_request(
                    self._journals(
                        [("1", self._request()), ("2", self._request(gate_field=gate_field))]
                    ),
                    result_journal="3",
                )
                # The NEWER journal is the answered round, and it names no head — so no approval
                # can correlate to it, which is the fail-closed direction.
                self.assertEqual(journal, "2")
                self.assertEqual(head, "")

    def test_a_malformed_request_after_a_result_still_reopens_the_round(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _review_request_after,
        )

        for label, gate_field in (
            ("valid", "gate=review_request"),
            ("whitespace", "gate = review_request"),
            ("unknown second alias", "gate=review_request:kind=unknown_gate"),
        ):
            with self.subTest(label):
                self.assertTrue(
                    _review_request_after(
                        self._journals([("4", self._request(gate_field=gate_field))]),
                        result_journal="3",
                    )
                )

    def test_no_request_after_the_result_does_not_reopen(self):
        """Negative control: reopening must still require a declaration to exist."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _review_request_after,
        )

        self.assertFalse(
            _review_request_after(
                self._journals([("2", self._request())]), result_journal="3"
            )
        )


class ReviewJ92106SameGateSiblingTests(unittest.TestCase):
    """R21-F3: the shared gate reader kept the readable subset of same-gate siblings.

    R21 gave ``strict_marker_fields_in_note`` whole-note semantics and wrote down exactly why —
    "a note carrying one clean and one forged marker would read like a clean note" — and then did
    not apply the same rule to ``strict_gate_markers``, which is the reader most authority
    consumers actually call.
    """

    CLEAN = "[mozyo:workflow-event:kind=implementation_request:lane=r1:lane_generation=1]"
    FORGED = "[mozyo:workflow-event:kind = implementation_request:lane=r1:lane_generation=1]"

    def test_a_forged_sibling_for_the_same_gate_poisons_the_note(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            strict_gate_markers,
        )

        self.assertEqual(len(strict_gate_markers(self.CLEAN, "implementation_request")), 1)
        self.assertEqual(
            strict_gate_markers(self.CLEAN + "\n" + self.FORGED, "implementation_request"), ()
        )

    def test_a_forged_sibling_for_ANOTHER_gate_is_not_this_gate_s_business(self):
        """The boundary: whole-note must not mean "any bad marker anywhere kills everything"."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            strict_gate_markers,
        )

        other = "[mozyo:workflow-event:gate = some_other_gate]"
        self.assertEqual(
            len(strict_gate_markers(self.CLEAN + "\n" + other, "implementation_request")), 1
        )

    def test_the_dispatch_anchor_refuses_the_poisoned_note(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
            dispatch_generations,
            resolve_dispatch_entry_journal,
        )

        entries = [
            RedmineJournalEntry(
                issue_id="1", journal_id="7", notes=self.CLEAN + "\n" + self.FORGED
            )
        ]
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="r1", lane_generation=1), ""
        )
        self.assertEqual(dispatch_generations(entries, lane="r1"), ())

    def test_the_answered_review_request_refuses_the_poisoned_note(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            _answered_review_request,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            EvidenceJournal,
        )

        head = "a" * 40
        clean = (
            "[mozyo:workflow-event:gate=review_request:workspace=ws:lane=r1:"
            f"lane_generation=1:head={head}]"
        )
        forged = clean.replace("gate=review_request", "gate = review_request")
        journal, resolved_head = _answered_review_request(
            [EvidenceJournal(journal_id="1", notes=clean + "\n" + forged)], result_journal="3"
        )
        # The journal still DECLARES the gate, so it is current — it just proves no head.
        self.assertEqual(journal, "1")
        self.assertEqual(resolved_head, "")


class ReviewJ92106ExemptionQualificationTests(unittest.TestCase):
    """R21-F1: a marker-qualified exemption journal must carry a RENDERABLE marker.

    The allowlist called this "structural qualification only". On the effect chain it is not:
    qualifying decides whether the gate's fields are read as authority, and a valid read mints the
    exemption the glance projection and the terminal retire admission both consume.
    """

    HEAD = "a" * 40
    FIELDS = (
        "\n- role: 実装者\n- direct_edit: true\n- allowed_paths: src/**\n"
        "- reason: r\n- follow_up_review: false\n"
    )

    def _scope(self):
        return (
            f"## Gate: Implementation Done\n- commit: {self.HEAD}\n"
            "- changed_paths:\n  - src/a.py\n"
        )

    def _state(self, marker):
        return fold_review_exemption(
            [("101", marker + self.FIELDS), ("102", self._scope())]
        ).state

    def test_the_canonical_marker_still_mints_the_exemption(self):
        self.assertEqual(
            self._state("[mozyo:workflow-event:gate=codex_direct_edit]"), EXEMPTION_EXEMPT
        )

    def test_a_producer_impossible_marker_mints_nothing(self):
        for label, marker in (
            ("whitespace", "[mozyo:workflow-event:gate = codex_direct_edit]"),
            ("duplicate key",
             "[mozyo:workflow-event:gate=other:gate=codex_direct_edit]"),
            ("unknown second alias",
             "[mozyo:workflow-event:gate=codex_direct_edit:kind=unknown_gate]"),
        ):
            with self.subTest(label):
                self.assertEqual(self._state(marker), EXEMPTION_INVALID)

    def test_an_unreadable_marker_gate_still_shadows_an_older_valid_one(self):
        """Refusing to MINT must not become refusing to SEE — the R2-F1 invariant.

        The malformed journal is still the current declaration, so the older valid exemption does
        not come back; it simply yields ``invalid`` (review owed).
        """
        older = "[mozyo:workflow-event:gate=codex_direct_edit]" + self.FIELDS
        newer = "[mozyo:workflow-event:gate = codex_direct_edit]" + self.FIELDS
        facts = fold_review_exemption(
            [("101", older), ("102", self._scope()), ("103", newer)]
        )
        self.assertEqual(facts.state, EXEMPTION_INVALID)

    def test_the_heading_form_is_unaffected(self):
        """A governed heading qualifies on its own; the marker rule must not disturb it."""
        self.assertEqual(self._state("## Gate: codex_direct_edit"), EXEMPTION_EXEMPT)


class ReviewJ92106BespokeParserTests(unittest.TestCase):
    """R21-F4: the symbol sweep could not see a reader that brings its OWN parser.

    ``dispatch_authorization`` scanned the marker grammar with a private ``_MARKER_RE`` and a
    verbatim copy of the lenient last-write-wins / whitespace-stripping fold, and its result
    authorizes an actual worker dispatch. The previous classification pin matched a shared symbol
    name, so a module that never imports that symbol was invisible to it — which is why "every
    effect-reaching reader was mechanically enumerated" was not true.
    """

    def _marker(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            build_dispatch_authorization_marker,
        )

        return build_dispatch_authorization_marker(
            action_id="a1",
            source_gate="review_result",
            issue="500",
            workspace_id="ws",
            lane_id="r1",
            target_assigned_name="mzb1_ws_claude_r1",
        )

    def _parse(self, note):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            parse_dispatch_authorizations,
        )

        class _E:
            def __init__(self, notes):
                self.journal_id = "7"
                self.notes = notes

        return parse_dispatch_authorizations([_E(note)])

    def test_the_canonical_authorization_is_valid(self):
        """Control: the producer's own output must keep authorizing."""
        self.assertEqual([a.valid for a in self._parse(self._marker())], [True])

    def test_producer_impossible_authorizations_authorize_nothing(self):
        canonical = self._marker()
        for label, note in (
            ("whitespace",
             canonical.replace("action=dispatch_worker", "action = dispatch_worker")),
            ("duplicate action",
             canonical.replace("action=dispatch_worker", "action=deny:action=dispatch_worker")),
            ("duplicate role",
             canonical.replace(
                 "authorized_by_role=coordinator",
                 "authorized_by_role=worker:authorized_by_role=coordinator",
             )),
        ):
            with self.subTest(label):
                parsed = self._parse(note)
                # Still EMITTED (a malformed record stays diagnosable) but never valid.
                self.assertEqual([a.valid for a in parsed], [False])

    def test_no_module_hand_rolls_a_marker_body_parser(self):
        """The classification pin the symbol sweep could not be: parsers, not imports.

        A module may scan for the ``[mozyo:...]`` token, but splitting a marker BODY into fields
        itself means it has its own grammar — and a private grammar is exactly what drifted from
        the contract here. The body parse must come from the shared strict reader.
        """
        import pathlib
        import re

        allowed = {
            # Owns the grammar ABOVE the token scan: the component split and both strict readers.
            # It moved here out of ``redmine_journal_source`` in #14687 when the #14661
            # integration pushed that module past the module-health line; the split is
            # byte-identical, so what this entry allows is unchanged.
            #
            # Measured, so the entry is not read as more than it is: this module splits bodies
            # but holds no token regex LITERAL (it imports ``MARKER_RE``), and the scope filter
            # above requires both — so the entry is a declaration of ownership, not the thing
            # keeping the module green. ``worker_refresh_approval`` (#14661) has the same shape
            # and is likewise out of the filter's reach; that is a property of the filter, which
            # this remediation reports rather than redesigns.
            "strict_marker_read.py",
            # ``redmine_journal_source`` is deliberately NOT on this list any more: it re-exports
            # these readers and parses no body itself. Probed on this head — appending a private
            # token regex plus a body split to it reddens this test, which it would not have
            # while the name was still allowed.
            # The canonical quote-aware scan (#14585 / #14665) — it OWNS the token grammar and the
            # body split for the whole package now, exactly as this module's own owner entry does.
            "canonical_note_scan.py",
            # A deliberately STRICTER private reader (#14219 j#86569 R8-F2 / j#86675 R18-F3): it
            # refuses whitespace ANYWHERE in a component, where the shared reader refuses it only
            # around a key or value. Routing it through the shared reader would LOOSEN it, so it
            # stays — the rule this pin enforces is "no LENIENT private grammar", and its own
            # negative cases are pinned by #14219.
            "hibernate_park_record.py",
        }
        root = pathlib.Path(__file__).resolve().parents[2] / "src" / "mozyo_bridge"
        offenders = set()
        for path in root.rglob("*.py"):
            if path.name in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            # Only modules that scan the marker TOKEN itself are in scope — plenty of unrelated
            # code parses ``KEY=VALUE`` for CLI args or config and has nothing to do with this
            # grammar. The pair "scans ``[mozyo:`` AND splits a body into fields" is the shape of
            # a hand-rolled marker parser.
            if r"\[mozyo:" not in text:
                continue
            # The SPLIT is what is detected, not the name of the variable holding the body
            # (review j#92174 finding 3). Matching ``body.split(`` was a spelling, not an
            # inventory: ``recovery_anchor_delivery`` wrote ``match.group("body").split(":")``
            # and ``recovered_pair_pin_reconciliation`` the same, so two private grammars sat
            # outside this gate. Both happened to be strict, which is exactly why the gate could
            # not be trusted — it was green for a reason it never checked.
            #
            # Splitting on ``":"`` is how a body becomes components and partitioning on ``"="``
            # is how a component becomes a field; a module that scans the marker token and does
            # either owns a grammar, whatever it calls its variables.
            if re.search(
                r"\.\s*split\(\s*[\"']:[\"']\s*\)|\.\s*partition\(\s*[\"']=[\"']\s*\)", text
            ):
                offenders.add(path.name)
        self.assertEqual(
            offenders,
            set(),
            "these modules parse a marker body themselves; route the body through "
            "`strict_marker_fields(_parse_marker_components(body))` so one grammar decides what "
            "is renderable",
        )


class ReviewJ92060EffectReachingReaderTests(unittest.TestCase):
    """R19-F2/F3: every reader whose result reaches an EFFECT reads strictly.

    Three rounds running, a reader was left on the lenient fold because it was classified by its
    name rather than by where its result travels. These pin the behaviour at each effect boundary
    the review named, and the last test pins the CLASSIFICATION itself so a new lenient authority
    reader cannot be added silently.
    """

    def _dispatch_note(self, kind_field="kind=implementation_request"):
        return f"[mozyo:workflow-event:{kind_field}:lane=r1:lane_generation=1]"

    def _entries(self, note):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )

        return [RedmineJournalEntry(issue_id="1", journal_id="7", notes=note)]

    def test_the_dispatch_anchor_and_generation_refuse_unrenderable_bodies(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            dispatch_generations,
            resolve_dispatch_entry_journal,
        )

        clean = self._entries(self._dispatch_note())
        self.assertEqual(
            resolve_dispatch_entry_journal(clean, lane="r1", lane_generation=1), "7"
        )
        self.assertEqual(dispatch_generations(clean, lane="r1"), (1,))
        for label, note in (
            ("whitespace", self._dispatch_note("kind = implementation_request")),
            ("duplicate key",
             self._dispatch_note().replace("lane=r1", "lane=other:lane=r1")),
            ("unknown second alias",
             self._dispatch_note("kind=implementation_request:gate=unknown_gate")),
        ):
            with self.subTest(label):
                entries = self._entries(note)
                # Fail-closed to "" is this resolver's own zero-send contract.
                self.assertEqual(
                    resolve_dispatch_entry_journal(entries, lane="r1", lane_generation=1), ""
                )
                self.assertEqual(dispatch_generations(entries, lane="r1"), ())

    def test_an_unreadable_marker_leaves_the_entry_unclassified(self):
        """``stall_unprovable`` exists for opaque entries; a forged body must not look understood."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (  # noqa: E501
            _entry_is_classified,
        )

        class _E:
            def __init__(self, notes):
                self.notes = notes

        self.assertTrue(_entry_is_classified(_E(self._dispatch_note())))
        self.assertFalse(
            _entry_is_classified(_E(self._dispatch_note("kind = implementation_request")))
        )

    def test_the_exact_work_anchor_refuses_unrenderable_bodies(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_worker_delivery import (  # noqa: E501
            is_exact_implementation_request_anchor,
        )

        class _E:
            def __init__(self, notes):
                self.issue_id = "500"
                self.journal_id = "9"
                self.notes = notes

        kwargs = dict(issue="500", journal="9", lane="r1", lane_generation="1")
        self.assertTrue(
            is_exact_implementation_request_anchor(_E(self._dispatch_note()), **kwargs)
        )
        for label, note in (
            ("whitespace", self._dispatch_note("kind = implementation_request")),
            ("duplicate key",
             self._dispatch_note("kind=other:kind=implementation_request")),
        ):
            with self.subTest(label):
                self.assertFalse(
                    is_exact_implementation_request_anchor(_E(note), **kwargs)
                )

    def test_a_clean_marker_beside_a_forged_one_does_not_read_as_clean(self):
        """The reason the strict scan refuses the NOTE, not just the bad marker."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_worker_delivery import (  # noqa: E501
            is_exact_implementation_request_anchor,
        )

        class _E:
            def __init__(self, notes):
                self.issue_id = "500"
                self.journal_id = "9"
                self.notes = notes

        mixed = self._dispatch_note() + "\n[mozyo:workflow-event:gate = forged]"
        self.assertFalse(
            is_exact_implementation_request_anchor(
                _E(mixed), issue="500", journal="9", lane="r1", lane_generation="1"
            )
        )

    def test_only_the_declared_display_readers_still_use_the_lenient_fold(self):
        """The classification itself, pinned — this is what kept regressing.

        Three consecutive rounds found a reader left lenient because it was judged by its name.
        The rule is now mechanical: a module may call ``marker_fields_in_note`` only if it is on
        this list, and each entry says why its result never reaches a send / actuation / admission.
        Adding a new lenient reader to an authority module fails HERE rather than in the next
        audit.
        """
        import pathlib
        import re

        allowed = {
            # Display projection only. #14213 deliberately keeps its historical leniency; the
            # authority consumers ask ``has_conflicting_disposition_declaration`` first.
            "glance_integration_disposition.py",
            # Defines the scanner and its strict counterparts (#14687 moved them here out of
            # ``redmine_journal_source``, byte-identical).
            "strict_marker_read.py",
            # Re-exports the scanner and its strict counterparts for the ~120 import sites that
            # already name this module; it calls neither itself.
            "redmine_journal_source.py",
        }
        # ``sublane_worker_refresh_durable_read.py`` was on this list for exactly one round
        # (#14687 R1) and was REMOVED rather than kept, which is the durable record of why.
        # It reads the worker-progress gate, and its ``False`` becomes ``expected_gate_absent``
        # on the turn observation — the only route to ``turn_failed_no_durable_gate``, the one
        # class that admits the destructive guarded worker refresh. Review j#93273 R1-F1 refused
        # the carve-out: excepting the reader this pin had just caught is not "keeping the
        # contract", it is the quiet widening the gate exists to prevent. It now reads through
        # ``strict_marker_fields_in_note`` and treats an unreadable NOTE as progress, pinned by
        # (path kept on ONE line so it stays greppable):
        # tests/regressions/test_issue_14687_worker_progress_fail_closed.py
        # ``coordinator_proxy_send.py`` was carved out here in R34 as a MEASURED, unresolved
        # finding — its ``canonical_decision_in_journal`` decided a PROXY SEND through the lenient
        # fold, so three bodies no canonical producer could render each produced a decision. It was
        # routed rather than patched inside a conflict-resolution task, because the naive strict
        # fix (drop the unreadable marker) would have LOOSENED that rail's exactly-one-decision
        # rule by turning a duplicate refusal into an acceptance.
        #
        # Redmine #14667 closed it: that reader now judges the body from its uncollapsed components
        # through the shared strict reader, and an unreadable same-kind claim refuses the journal
        # instead of being dropped. Pinned by (path kept on ONE line so it stays greppable):
        # tests/regressions/test_issue_14667_proxy_send_strict_marker_fold.py
        # The carve-out is REMOVED rather than left in place — an allowlist entry for a module that
        # no longer offends is a standing permission for the next regression, which is the quiet
        # widening this gate exists to prevent.
        #
        # No routed findings are open. When one is, it goes here with its measurement, never with
        # "this is safe".
        # ``review_exemption.py`` and ``composer_discard_approval.py`` were on this list with
        # reasons that did not survive the effect chain (review j#92106 findings 1 and 2): the
        # first MINTS the exemption the retire admits on, the second is called by
        # ``herdr_session_retire_ops`` against a live Redmine read. Both now read strictly, and
        # their removal from this list is the durable record of why.
        root = pathlib.Path(__file__).resolve().parents[2] / "src" / "mozyo_bridge"
        offenders = set()
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            # Match the NAME anywhere — call, plain import, or aliased import.
            # Matching only call sites let ``import marker_fields_in_note as _x``
            # walk straight past the guard, which a probe against THIS test found.
            # The strict helper's name contains the lenient one, so the lookbehind
            # keeps the pattern from matching itself.
            if re.search(r"(?<!strict_)\bmarker_fields_in_note\b", text):
                if path.name not in allowed:
                    offenders.add(path.name)
        self.assertEqual(
            offenders,
            set(),
            "these modules call the LENIENT marker fold; if the result reaches a send / "
            "actuation / admission it must use the strict reader, and if it genuinely does not, "
            "add it to the allowlist above WITH the reason",
        )


class ReviewJ92174MultiGateSiblingTests(unittest.TestCase):
    """R23-F1: same-gate poison fired on UNREADABLE siblings only.

    R21 taught ``strict_gate_markers`` that a same-gate sibling it cannot parse poisons the note.
    A marker naming two gates parses perfectly and still proves neither (ruling #14219 j#86718),
    so as this gate's evidence it is exactly as unusable — but it failed the ``== {gate}`` check
    quietly and was skipped, handing authority to its clean sibling. Readability and countability
    are different questions and only the first one was being asked.
    """

    CLEAN = "[mozyo:workflow-event:kind=implementation_request:lane=r1:lane_generation=1]"
    MULTI = "[mozyo:workflow-event:gate=implementation_request:kind=unknown_gate]"

    def _markers(self, notes):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            strict_gate_markers,
        )

        return strict_gate_markers(notes, "implementation_request")

    def test_a_readable_multi_gate_sibling_poisons_the_note(self):
        self.assertEqual(len(self._markers(self.CLEAN)), 1)  # control
        self.assertEqual(self._markers(self.CLEAN + "\n" + self.MULTI), ())

    def test_a_lone_multi_gate_marker_is_evidence_for_neither_gate(self):
        """It matched nothing before too — but by being skipped, not by being refused."""
        self.assertEqual(self._markers(self.MULTI), ())

    def test_a_multi_gate_sibling_naming_only_OTHER_gates_is_left_alone(self):
        """The boundary: poison is same-gate, not "any ambiguous marker anywhere"."""
        other = "[mozyo:workflow-event:gate=park_record:kind=some_other_gate]"
        self.assertEqual(len(self._markers(self.CLEAN + "\n" + other)), 1)

    def test_a_marker_repeating_the_gate_token_in_both_aliases_still_counts(self):
        """One gate written twice is one claim, not two — it must keep parsing."""
        both = "[mozyo:workflow-event:gate=implementation_request:kind=implementation_request]"
        self.assertEqual(len(self._markers(both)), 1)

    def test_the_dispatch_anchor_refuses_the_multi_gate_note(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
            dispatch_generations,
            resolve_dispatch_entry_journal,
        )

        entries = [
            RedmineJournalEntry(
                issue_id="1", journal_id="7", notes=self.CLEAN + "\n" + self.MULTI
            )
        ]
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="r1", lane_generation=1), ""
        )
        self.assertEqual(dispatch_generations(entries, lane="r1"), ())


class ReviewJ92174HeadingDoesNotRescueTests(unittest.TestCase):
    """R23-F2: a heading let a malformed same-gate marker mint an exemption.

    R21 made a marker-qualified journal prove its marker is renderable, and scoped that to
    journals qualifying ONLY through a marker. The carve-out conflated two questions: a heading
    is enough to DECLARE the gate, but it cannot make the marker beside it readable. Honouring
    the heading and ignoring the marker is the readable-subset behaviour the layer below refuses.
    """

    HEAD = "a" * 40
    HEADING = "## Gate: codex_direct_edit"
    MALFORMED = "[mozyo:workflow-event:gate = codex_direct_edit]"
    FIELDS = (
        "\n- role: 実装者\n- direct_edit: true\n- allowed_paths: src/**\n"
        "- reason: r\n- follow_up_review: false\n"
    )

    def _scope(self):
        return (
            f"## Gate: Implementation Done\n- commit: {self.HEAD}\n"
            "- changed_paths:\n  - src/a.py\n"
        )

    def _state(self, declaration):
        return fold_review_exemption(
            [("101", declaration + self.FIELDS), ("102", self._scope())]
        ).state

    def test_a_heading_alone_still_mints(self):
        """Control: the legacy heading-only form is untouched."""
        self.assertEqual(self._state(self.HEADING), EXEMPTION_EXEMPT)

    def test_a_heading_does_not_rescue_a_malformed_same_gate_marker(self):
        self.assertEqual(
            self._state(self.HEADING + "\n" + self.MALFORMED), EXEMPTION_INVALID
        )

    def test_a_heading_plus_a_RENDERABLE_marker_still_mints(self):
        """The boundary: the marker rule must refuse bodies, not the pairing itself."""
        self.assertEqual(
            self._state(self.HEADING + "\n[mozyo:workflow-event:gate=codex_direct_edit]"),
            EXEMPTION_EXEMPT,
        )

    def test_the_heading_and_marker_pair_still_SHADOWS_an_older_valid_gate(self):
        """Refusing to mint must not become refusing to see — the R2-F1 invariant."""
        older = "[mozyo:workflow-event:gate=codex_direct_edit]" + self.FIELDS
        newer = self.HEADING + "\n" + self.MALFORMED + self.FIELDS
        facts = fold_review_exemption(
            [("101", older), ("102", self._scope()), ("103", newer)]
        )
        self.assertEqual(facts.state, EXEMPTION_INVALID)

    def test_a_malformed_marker_for_ANOTHER_gate_beside_the_heading_is_ignored(self):
        """The heading journal is not poisoned by an unrelated gate's bad marker."""
        other = "[mozyo:workflow-event:gate = park_record]"
        self.assertEqual(self._state(self.HEADING + "\n" + other), EXEMPTION_EXEMPT)


class ReviewJ92174SharedBodyReaderTests(unittest.TestCase):
    """R23-F3: two private body parsers sat outside the inventory pin.

    Both were strict, so nothing was leaking — but the pin that is supposed to enumerate private
    grammars matched the literal ``body.split(`` and both wrote ``group("body").split(":")``, so
    the gate was green for a reason it never checked. Routing them to the shared reader is also a
    tightening: their own loop stripped each component before judging it.
    """

    def _authorization(self, body):
        return f"[mozyo:recovery-delivery-authorization:{body}]"

    def _canonical_fields(self):
        return (
            "conclusion=authorized:authorized_by_role=owner:issue=14539:lane=r1:"
            "workspace_id=ws:anchor_journal=1:retry_of_action_sha256=d:"
            "prior_zero_send_journal=2"
        )

    def _parse(self, note):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
            parse_recovery_delivery_authorizations,
        )

        class _E:
            def __init__(self, notes):
                self.journal_id = "7"
                self.notes = notes

        return parse_recovery_delivery_authorizations([_E(note)])

    def test_the_canonical_authorization_still_parses(self):
        parsed = self._parse(self._authorization(self._canonical_fields()))
        self.assertEqual([a.issue for a in parsed], ["14539"])

    def test_a_whitespace_contaminated_component_is_now_refused(self):
        """The private loop stripped first, so ``issue = 14539`` read as a clean field."""
        contaminated = self._canonical_fields().replace("issue=14539", "issue = 14539")
        self.assertEqual(self._parse(self._authorization(contaminated)), ())

    def test_an_empty_component_is_refused(self):
        body = self._canonical_fields().replace("lane=r1", "lane=r1:")
        self.assertEqual(self._parse(self._authorization(body)), ())

    def test_a_repeated_key_is_refused_even_with_an_identical_value(self):
        """A closed vocabulary renders each key once, so a second occurrence is not producer
        output — this is the one axis the shared reader alone would have allowed."""
        body = self._canonical_fields().replace("lane=r1", "lane=r1:lane=r1")
        self.assertEqual(self._parse(self._authorization(body)), ())

    def test_a_missing_field_is_refused(self):
        body = self._canonical_fields().replace(":prior_zero_send_journal=2", "")
        self.assertEqual(self._parse(self._authorization(body)), ())

    def test_an_extra_field_is_refused(self):
        body = self._canonical_fields() + ":unexpected=1"
        self.assertEqual(self._parse(self._authorization(body)), ())

    def test_the_r19_owner_marker_reader_is_tightened_the_same_way(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_pair_pin_reconciliation import (  # noqa: E501
            _strict_authority_fields,
        )

        body = (
            "gate=recovered_pair_pin_reconciliation:kind=owner_authority:issue=14539:"
            "lane=r1:lane_generation=1:source_revision=1:expected_revision=2:"
            "lifecycle_decision_journal=9:target_action_digest=d"
        )
        canonical = f"[mozyo:workflow-event:{body}]"
        self.assertIsNotNone(_strict_authority_fields(canonical))  # control
        self.assertIsNone(
            _strict_authority_fields(canonical.replace("issue=14539", "issue = 14539"))
        )


class ReviewJ92227DispatchAuthorityTests(unittest.TestCase):
    """R24: the whole-note / cardinality contract had never reached the dispatch channels.

    R23 taught the shared gate reader that a same-gate sibling it cannot count poisons the note.
    The two dedicated authority channels — where the channel IS the gate — kept skipping an
    unrenderable sibling and dispatching on the survivor, and the send side resolved two valid
    authorizations at ONE journal by note order while the discharge side called the identical
    record ambiguous. Both effects are real: a worker send and a terminal discharge.
    """

    WS, LANE, ISSUE, TGT = "ws", "r1", "14539", "tgt"

    class _Entry:
        def __init__(self, journal, notes, issue):
            self.journal_id, self.notes, self.issue_id = str(journal), notes, issue

    def _entry(self, journal, notes):
        return self._Entry(journal, notes, self.ISSUE)

    def _auth(self, action_id):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            build_dispatch_authorization_marker,
        )

        return build_dispatch_authorization_marker(
            action_id=action_id,
            source_gate="review_result",
            issue=self.ISSUE,
            workspace_id=self.WS,
            lane_id=self.LANE,
            target_assigned_name=self.TGT,
        )

    def _decide(self, entries):
        """The REAL send effect entry, driven through its two injection seams."""
        import argparse

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_authority import (  # noqa: E501
            resolve_dispatch_decision,
        )

        return resolve_dispatch_decision(
            argparse.Namespace(),
            workspace_id=self.WS,
            lane_id=self.LANE,
            issue=self.ISSUE,
            env={},
            journal_source_factory=lambda _args: type(
                "_Source", (), {"read_entries": lambda _s, _i: entries}
            )(),
            # A single idle slot: the ONLY runtime shape that can reach AUTHORIZE, so a blocked
            # verdict below is the authorization's doing and not the target's.
            agent_rows=lambda _env: [{"name": self.TGT, "agent_status": "idle"}],
        )

    def _valid_index(self, entries):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            parse_dispatch_authorizations,
        )

        index = {}
        for auth in parse_dispatch_authorizations(entries):
            if auth.valid:
                index.setdefault(auth.journal, []).append(auth)
        return index

    def _row(self, action_id="a1"):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            DispatchRowIdentity,
        )

        return DispatchRowIdentity(
            issue=self.ISSUE,
            workspace_id=self.WS,
            lane_id=self.LANE,
            target_assigned_name=self.TGT,
            journal="7",
            action_id=action_id,
        )

    def _correlate(self, entries, index, review_requests=()):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            correlate_dispatch_disposition,
        )

        return correlate_dispatch_disposition(
            self._row(),
            entries,
            authorize_journals=index,
            review_request_journals=list(review_requests),
        )

    # -- finding 1: authorization, unrenderable same-channel sibling --------------------

    def test_a_clean_authorization_still_dispatches(self):
        """Control: the canonical producer's own output must keep authorizing."""
        decision = self._decide([self._entry(7, self._auth("a1"))])
        self.assertEqual(decision.decision, "authorize")

    def test_a_forged_sibling_blocks_the_dispatch(self):
        clean = self._auth("a1")
        forged = clean.replace("action=dispatch_worker", "action = dispatch_worker")
        decision = self._decide([self._entry(7, clean + "\n" + forged)])
        self.assertEqual(decision.decision, "blocked")

    def test_the_poisoned_authorization_keeps_its_identity_so_it_is_reported(self):
        """Blocked, not monitored: an all-blank record matches no lane and vanishes."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            AMBIGUITY_UNREADABLE_SIBLING,
            parse_dispatch_authorizations,
        )

        clean = self._auth("a1")
        forged = clean.replace("action=dispatch_worker", "action = dispatch_worker")
        parsed = parse_dispatch_authorizations([self._entry(7, clean + "\n" + forged)])
        survivor = next(a for a in parsed if a.action_id == "a1")
        self.assertEqual(survivor.ambiguity, AMBIGUITY_UNREADABLE_SIBLING)
        self.assertFalse(survivor.valid)
        self.assertTrue(
            survivor.matches_lane(
                workspace_id=self.WS, lane_id=self.LANE, issue=self.ISSUE
            )
        )

    def test_the_poison_reaches_every_consumer_that_gates_on_valid(self):
        """One flag, not four patched call sites: retire / writer / intake all read ``valid``."""
        clean = self._auth("a1")
        forged = clean.replace("action=dispatch_worker", "action = dispatch_worker")
        self.assertEqual(self._valid_index([self._entry(7, clean + "\n" + forged)]), {})

    def test_a_forged_marker_in_ANOTHER_journal_does_not_poison_this_one(self):
        """The boundary: the poison is note-scoped, not history-scoped."""
        clean = self._auth("a1")
        forged = clean.replace("action=dispatch_worker", "action = dispatch_worker")
        decision = self._decide(
            [self._entry(5, forged), self._entry(7, clean)]
        )
        self.assertEqual(decision.decision, "authorize")

    # -- finding 2: two valid authorizations at ONE journal -----------------------------

    def test_two_valid_authorizations_at_one_journal_block_the_dispatch(self):
        note = self._auth("a1") + "\n" + self._auth("a2")
        self.assertEqual(self._decide([self._entry(7, note)]).decision, "blocked")

    def test_the_send_and_discharge_entries_agree_on_the_same_record(self):
        """The parity this finding was really about: one durable record, one answer.

        Before R25 the identical note read ``authorize`` when the sender asked and
        ``ambiguous`` when the retire asked — the discharge side had carried the cardinality
        rule since j#80644 R6-F2 and the send side had never been given it.
        """
        entries = [self._entry(7, self._auth("a1") + "\n" + self._auth("a2"))]
        self.assertEqual(self._decide(entries).decision, "blocked")
        self.assertEqual(
            self._correlate(entries, self._valid_index(entries)).state, "ambiguous"
        )

    def test_a_re_authorization_at_a_LATER_journal_still_supersedes(self):
        """The boundary: last-wins is about supersession BETWEEN journals; keep it."""
        decision = self._decide(
            [self._entry(5, self._auth("a1")), self._entry(7, self._auth("a2"))]
        )
        self.assertEqual(decision.decision, "authorize")
        self.assertEqual(decision.authorization.action_id, "a2")

    # -- finding 3: disposition, unrenderable same-channel sibling ----------------------

    def _disposition_history(self, note):
        return [
            self._entry(7, self._auth("a1")),
            self._entry(9, "## Gate: Review Request\n[mozyo:workflow-event:gate=review_request]"),
            self._entry(11, note),
        ]

    def _disposition(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            render_dispatch_disposition_marker,
        )

        return render_dispatch_disposition_marker(
            action_id="a1",
            dispatch_journal="7",
            workspace_id=self.WS,
            lane_id=self.LANE,
            target_assigned_name=self.TGT,
            terminal_journal="9",
        )

    def test_a_clean_disposition_still_discharges(self):
        """Control: the canonical discharge path must keep working."""
        entries = self._disposition_history(self._disposition())
        index = self._valid_index([self._entry(7, self._auth("a1"))])
        self.assertEqual(
            self._correlate(entries, index, review_requests=["9"]).state, "discharged"
        )

    def test_a_forged_disposition_sibling_refuses_the_discharge(self):
        clean = self._disposition()
        forged = clean.replace("action_id=a1", "action_id = a1")
        entries = self._disposition_history(clean + "\n" + forged)
        index = self._valid_index([self._entry(7, self._auth("a1"))])
        self.assertEqual(
            self._correlate(entries, index, review_requests=["9"]).state, "ambiguous"
        )

    def test_the_poisoned_disposition_is_still_returned_so_the_round_reads_ambiguous(self):
        """Dropping it would report OWED — "still running" — for a record that exists."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            parse_dispatch_dispositions,
        )

        clean = self._disposition()
        forged = clean.replace("action_id=a1", "action_id = a1")
        parsed = parse_dispatch_dispositions(self._entry(11, clean + "\n" + forged))
        self.assertEqual([d.note_ambiguous for d in parsed], [True])

    def test_the_disposition_writer_refuses_to_call_a_poisoned_prior_already_recorded(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.dispatch_disposition_writer import (  # noqa: E501
            record_dispatch_disposition,
        )

        clean = self._disposition()
        forged = clean.replace("action_id=a1", "action_id = a1")
        writes = []

        def _record(note):
            entries = self._disposition_history(note)
            return record_dispatch_disposition(
                issue=self.ISSUE,
                dispatch_journal="7",
                terminal_journal="9",
                workspace_id=self.WS,
                lane_id=self.LANE,
                target_assigned_name=self.TGT,
                action_id="a1",
                source=type("_S", (), {"read_entries": lambda _s, _i: entries})(),
                append_note=lambda issue, text: writes.append((issue, text)),
            )

        self.assertEqual(_record(clean).state, "already_recorded")  # control
        self.assertEqual(_record(clean + "\n" + forged).state, "refused")
        self.assertEqual(writes, [])  # zero-write on both paths


class ReviewJ92327ClosedVocabularyTests(ReviewJ92227DispatchAuthorityTests):
    """R25-F1/F2: the closed-vocabulary reader and the writer's first-match-wins loop.

    R24 built ``strict_marker_body_fields`` precisely so a channel with a fixed field set could
    refuse an extra field, a same-value repeated key and a blank value — and R25 edited both
    dispatch modules without routing them through it. Every one of those bodies reached
    ``authorize`` / ``discharged``. Separately the writer returned on the FIRST causal prior, so
    an identical clean record followed by a conflicting one reported ``already_recorded`` while
    the correlator, on the same entries, said ``ambiguous``.

    Inherits the R24 harness (real effect entries through their injection seams).
    """

    def _extra_field(self, marker):
        return marker[:-1] + ":unexpected=1]"

    def _duplicate_key(self, marker):
        return marker.replace("lane_id=r1", "lane_id=r1:lane_id=r1", 1)

    def _blank_value(self, marker):
        return marker.replace("lane_id=r1", "lane_id=", 1)

    # -- finding 1: authorization ------------------------------------------------------

    def test_a_producer_impossible_authorization_body_never_authorizes(self):
        clean = self._auth("a1")
        self.assertEqual(self._decide([self._entry(7, clean)]).decision, "authorize")  # control
        for label, note in (
            ("extra field", self._extra_field(clean)),
            ("same-value duplicate key", self._duplicate_key(clean)),
            ("blank value", self._blank_value(clean)),
        ):
            with self.subTest(label):
                self.assertNotEqual(self._decide([self._entry(7, note)]).decision, "authorize")

    def test_an_incomplete_same_channel_sibling_blocks_the_clean_one(self):
        """It used to be SKIPPED by the required-field check, walking past the note poison."""
        clean = self._auth("a1")
        incomplete = "[mozyo:dispatch-authorization:action_id=a9:issue=14539]"
        self.assertEqual(
            self._decide([self._entry(7, clean + "\n" + incomplete)]).decision, "blocked"
        )

    # -- finding 1: disposition --------------------------------------------------------

    def _discharge(self, note):
        entries = self._disposition_history(note)
        index = self._valid_index([self._entry(7, self._auth("a1"))])
        return self._correlate(entries, index, review_requests=["9"]).state

    def test_a_producer_impossible_disposition_body_never_discharges(self):
        clean = self._disposition()
        self.assertEqual(self._discharge(clean), "discharged")  # control
        for label, note in (
            ("extra field", self._extra_field(clean)),
            ("same-value duplicate key", self._duplicate_key(clean)),
            ("blank value", self._blank_value(clean)),
        ):
            with self.subTest(label):
                self.assertNotEqual(self._discharge(note), "discharged")

    def test_an_incomplete_disposition_sibling_refuses_the_discharge(self):
        clean = self._disposition()
        incomplete = "[mozyo:dispatch-disposition:action_id=a9:lane_id=r1]"
        self.assertEqual(self._discharge(clean + "\n" + incomplete), "ambiguous")

    # -- finding 2: the writer reads every causal prior ---------------------------------

    def _write(self, entries):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.dispatch_disposition_writer import (  # noqa: E501
            record_dispatch_disposition,
        )

        writes = []
        result = record_dispatch_disposition(
            issue=self.ISSUE,
            dispatch_journal="7",
            terminal_journal="9",
            workspace_id=self.WS,
            lane_id=self.LANE,
            target_assigned_name=self.TGT,
            action_id="a1",
            source=type("_S", (), {"read_entries": lambda _s, _i: entries})(),
            append_note=lambda issue, text: writes.append((issue, text)),
        )
        return result.state, writes

    def _history(self, *later):
        clean = self._auth("a1")
        entries = [
            self._entry(7, clean),
            self._entry(9, "[mozyo:workflow-event:gate=review_request]"),
            self._entry(11, self._disposition()),
        ]
        entries.extend(self._entry(13 + i, note) for i, note in enumerate(later))
        return entries

    def test_a_later_conflicting_prior_beats_idempotent_success(self):
        state, writes = self._write(self._history())  # control: clean only
        self.assertEqual(state, "already_recorded")
        conflicting = self._disposition_with_terminal("99")
        state, writes2 = self._write(self._history(conflicting))
        self.assertEqual(state, "refused")
        self.assertEqual(writes + writes2, [])  # zero-write on both paths

    def test_a_later_poisoned_prior_beats_idempotent_success(self):
        clean = self._disposition()
        poisoned = clean + "\n" + clean.replace("action_id=a1", "action_id = a1", 1)
        state, writes = self._write(self._history(poisoned))
        self.assertEqual(state, "refused")
        self.assertEqual(writes, [])

    def test_the_writer_and_the_correlator_agree_on_the_same_entries(self):
        """The parity this finding is about, asserted on one shared input per shape."""
        index = self._valid_index([self._entry(7, self._auth("a1"))])
        clean = self._disposition()
        for label, later, writer_state, correlator_state in (
            ("clean only", None, "already_recorded", "discharged"),
            ("clean -> conflicting", self._disposition_with_terminal("99"), "refused", "ambiguous"),
            (
                "clean -> poisoned",
                clean + "\n" + clean.replace("action_id=a1", "action_id = a1", 1),
                "refused",
                "ambiguous",
            ),
        ):
            with self.subTest(label):
                entries = self._history(later) if later else self._history()
                self.assertEqual(self._write(entries)[0], writer_state)
                self.assertEqual(
                    self._correlate(entries, index, review_requests=["9", "99"]).state,
                    correlator_state,
                )

    def _disposition_with_terminal(self, terminal):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            render_dispatch_disposition_marker,
        )

        return render_dispatch_disposition_marker(
            action_id="a1",
            dispatch_journal="7",
            workspace_id=self.WS,
            lane_id=self.LANE,
            target_assigned_name=self.TGT,
            terminal_journal=terminal,
        )


class ReviewJ92327ClosedVocabularyHelperTests(unittest.TestCase):
    """The shared helper's own closed-vocabulary rules, including the one R24 dropped."""

    def _read(self, body, expected=("a", "b")):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            strict_marker_body_fields,
        )

        return strict_marker_body_fields(body, expected=frozenset(expected))

    def test_the_canonical_body_reads(self):
        self.assertEqual(self._read("a=1:b=2"), {"a": "1", "b": "2"})

    def test_a_blank_value_is_refused(self):
        """R24 routed the recovery channels here and DROPPED their ``not value`` refusal.

        That conversion was described as a tightening; on this one axis it was a loosening, and
        nothing pinned it. Found while fixing review j#92327 finding 1.
        """
        self.assertIsNone(self._read("a=1:b="))

    def test_an_extra_field_is_refused(self):
        self.assertIsNone(self._read("a=1:b=2:c=3"))

    def test_a_missing_field_is_refused(self):
        self.assertIsNone(self._read("a=1"))

    def test_a_repeated_key_is_refused_even_with_an_identical_value(self):
        self.assertIsNone(self._read("a=1:a=1:b=2"))


_MARKER_TOKEN = "[mozyo:"
_CHANNEL_CHARS = re.compile(r"[a-z0-9_-]+")


def _capabilities_in(text, binding):
    """Each marker-token occurrence in ``text`` as one capability token (pure).

    A literal channel names itself. A pattern that takes the channel as a regex group is
    generic, and generic capabilities must stay TELLABLE APART (Redmine #14539 review j#92420
    finding 2): folding them all to ``"*"`` in a set meant a module already holding one generic
    scanner could gain a second and keep its declaration — the very "second capability inherits
    the discipline" hole R26 finding 1(b) was supposed to close, still open for the nine generic
    holders. A generic bound to a name is therefore ``*:<name>``, and occurrences are returned
    with MULTIPLICITY so an added one changes the list even when it is unbound.
    """
    found = []
    for start in (match.end() for match in re.finditer(re.escape(_MARKER_TOKEN), text)):
        channel = _CHANNEL_CHARS.match(text, start)
        if channel and text[channel.end() : channel.end() + 1] == ":":
            found.append(channel.group(0))
        else:
            found.append(f"*:{binding}" if binding else "*")
    return found


def _marker_token_holders(root):
    """Every module holding the marker token -> (repo-relative path, capabilities it holds).

    Redmine #14539 reviews j#92374 and j#92420. Three earlier versions of this inventory each
    enumerated a SHAPE — the text ``body.split(``, then ``\\[mozyo:`` plus ``finditer``, then
    ``ast.Assign`` + ``re.compile`` — and each was defeated by a shape it had not considered. So
    the unit is POSSESSION: a module that names the marker token in a **value position** can
    build one or match one, whatever syntax it reaches for. Docstrings and bare string statements
    are ``Expr`` nodes and excluded, which keeps prose out.

    Provenance crosses ONE import hop, and every ordinary way of writing that hop resolves
    (j#92420 finding 1) — the previous version keyed owners by fully-qualified path but read
    ``node.module`` raw, so it handled only the absolute ``from a.b import RE`` form and lost:

    - ``from .owner import RE`` — a relative import, resolved here against the consumer's package;
    - ``import a.b as o`` / ``from a import b`` followed by ``o.RE`` / ``b.RE`` — a module import
      plus attribute access, which the previous version did not look at at all.

    A consumer written either way carries no token literal of its own, so it slipped the gate
    entirely.
    """
    import ast

    def documentation_constants(tree):
        return {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }

    def token_literals(node, docs):
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and _MARKER_TOKEN in child.value
            and id(child) not in docs
        ]

    def module_capabilities(tree):
        """Everything this module holds — a flat capability list, with NO name association.

        There is deliberately no ``{name: capabilities}`` map (Redmine #14539 review j#92567).
        Building one means deciding which statement bound which name, and that is an enumeration
        of Python's binding forms — the approach R31 already abandoned one layer up. It came
        back here: ``Assign`` and ``AnnAssign`` were read, so an owner writing
        ``for RE in [re.compile(marker)]`` or ``(RE := re.compile(marker))`` had an EMPTY map
        and propagated nothing, losing its direct and wildcard consumers alike.

        A name label is still used for the capability TOKEN of a generic pattern (``*:RE``), so
        two generic scanners in one module stay distinguishable. That is a label on the owner's
        own inventory entry; nothing about propagation depends on it.
        """
        docs = documentation_constants(tree)
        claimed, held = set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and node.value is not None:
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and isinstance(node.target, ast.Name)
            ):
                targets = [node.target.id]
            else:
                continue
            literals = token_literals(node.value, docs)
            if not literals or not targets:
                continue
            held += [
                capability
                for literal in literals
                for capability in _capabilities_in(literal.value, targets[0])
            ]
            claimed |= {id(literal) for literal in literals}
        for literal in token_literals(tree, docs):
            if id(literal) not in claimed:
                held += _capabilities_in(literal.value, None)
        return held

    def dotted_name(node):
        """``a.b.c`` for a Name/Attribute chain, else ``None``."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        return ".".join(reversed(parts))

    trees = {}
    for path in sorted(root.rglob("*.py")):
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - src must parse
            continue

    def module_path(path):
        parts = path.relative_to(root.parent).with_suffix("").parts
        return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)

    own, owners = {}, {}
    for path, tree in trees.items():
        held = module_capabilities(tree)
        own[path] = held
        if held:
            owners[module_path(path)] = held

    holders = {}
    for path, tree in trees.items():
        held = list(own[path])
        me = module_path(path)
        # A package's ``__init__`` IS the package, so ``from .x import y`` inside it resolves
        # against itself; every other module resolves against its parent (j#92455). Taking the
        # parent unconditionally shifted every relative import in an ``__init__`` up one level.
        if path.name == "__init__.py":
            package = me
        else:
            package = me.rsplit(".", 1)[0] if "." in me else me
        used_names = {
            other.id for other in ast.walk(tree) if isinstance(other, ast.Name)
        }
        # Owners reached by a USED import relation. Collected as a set so an owner's capability
        # list is propagated once however many times the consumer imports from it — otherwise a
        # second `from M import helper` line would change the consumer's capability multiset for
        # a reason that says nothing about capability.
        inherited_from = set()
        module_aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")
                    if node.level > 1:
                        base = base[: len(base) - (node.level - 1)]
                    full = ".".join(base + ([node.module] if node.module else []))
                else:
                    full = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        # A wildcard propagates the owner's WHOLE set, with no per-name filter:
                        # that filter needed the bound-name map, which is exactly what cannot be
                        # computed reliably (j#92567). An unused wildcard consumer is therefore
                        # over-detected, which is the side that cannot hide a reader.
                        if full in owners:
                            inherited_from.add(full)
                        continue
                    local = alias.asname or alias.name
                    if full in owners:
                        # The import RELATION is what matters, not which name inside the owner
                        # carries the capability. Asking the latter means deciding which
                        # statement bound which name — the enumeration this gate keeps losing to.
                        if local in used_names:
                            inherited_from.add(full)
                    elif f"{full}.{alias.name}" in owners:
                        module_aliases[local] = f"{full}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        module_aliases[alias.asname] = alias.name
                    else:
                        module_aliases[alias.name] = alias.name
        for local, target in module_aliases.items():
            if target in owners and (
                local in used_names
                or any(
                    dotted_name(node.value) == local
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                )
            ):
                inherited_from.add(target)
        for target in sorted(inherited_from):
            held += owners[target]
        if held:
            holders[str(path.relative_to(root.parent.parent))] = sorted(held)
    return holders
def _inventory_mismatch(holders, declared):
    """Every path whose CHANNELS differ, in either direction (pure).

    The comparison lives here, in one function used by the real gate and by the synthetic test
    below, because "compare channels, not just paths" is the actual R26-F1(b) fix: the previous
    gate asserted ``set(holders) == set(DISCIPLINES)``, so a declared module that grew a second
    channel inherited its old discipline and stayed green. A probe that reverts this to key
    comparison must redden something, and it cannot redden against ``src`` — the repo is
    consistent by construction — so it has to redden against a synthetic mismatch.
    """
    paths = set(holders) | set(declared)
    return {
        path: (holders.get(path), declared.get(path))
        for path in sorted(paths)
        if holders.get(path) != declared.get(path)
    }


_D = "src/mozyo_bridge/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff"
_H = "src/mozyo_bridge/e_110_execution_platform/f_130_handoff_routing"


class ReviewJ92374MarkerTokenInventoryTests(unittest.TestCase):
    """R26-F1: the inventory compared module paths, not capabilities.

    Every module that can name the marker token declares WHICH channels it can name and what it
    does with them. The gate compares the whole mapping, so a declared module gaining a second
    channel reddens too — that was the "second capability inherits the discipline" hole.
    """

    #: repo-relative module -> (channels it can name, what it does with them).
    DISCIPLINES = {
        # -- the grammar owner ---------------------------------------------------------
        f"{_D}/domain/redmine_journal_source.py": (
            ["*", "*", "*:MARKER_RE"],
            "renders the gate marker and the dispatch marker, and re-exports the strict readers "
            "#14687 moved to ``strict_marker_read``; the two token literals are its own, the "
            "third is the shared scan's",
        ),
        f"{_D}/domain/worker_refresh_approval.py": (
            ["*", "*", "*", "*:MARKER_RE"],
            "renders the worker-refresh owner-approval marker and reads it back through a "
            "deliberately STRICTER private parser: a repeated field, a malformed field or a "
            "non-canonical field order raises, and a note carrying more than one marker of this "
            "gate authorizes nothing (a record that declares the gate twice cannot say which is "
            "authoritative)",
        ),
        # -- readers -------------------------------------------------------------------
        f"{_D}/domain/dispatch_authorization.py": (
            ["*", "*", "*", "*:_MARKER_RE"],
            "reads: note-scoped ambiguity flag invalidates every sibling; also renders, through "
            "the shared value validator",
        ),
        f"{_D}/domain/dispatch_disposition.py": (
            ["*", "*", "*", "*:_MARKER_RE"],
            "reads: note-scoped note_ambiguous flag refuses the discharge; also renders, through "
            "the shared value validator",
        ),
        f"{_D}/domain/recovery_anchor_delivery.py": (
            ["*", "*", "*", "*", "recovery-delivery-authorization", "recovery-delivery-zero-send"],
            "reads: exactly-one-marker rule, so any second marker of its channel makes the note "
            "unreadable before a field is compared",
        ),
        f"{_D}/domain/recovered_pair_pin_reconciliation.py": (
            ["*", "*", "*:_AUTHORITY_RE"],
            "reads: exactly-one-marker rule",
        ),
        f"{_D}/domain/hibernate_park_record.py": (["handoff", "handoff"], "reads one marker per record"),
        f"{_D}/application/operator_startup_resume_leg.py": (
            ["*", "operator-startup-gate"],
            "reads by version-agnostic prefix match, and builds the versioned marker from that "
            "same prefix",
        ),
        f"{_D}/application/sublane_quarantine.py": (
            ["handoff"],
            "reads a tmux composer TAIL, not a durable note; the handoff channel is a delivery "
            "notification and never carries gate authority, and multiplicity is the point",
        ),
        # -- producers -----------------------------------------------------------------
        f"{_H}/domain/handoff.py": (["handoff"], "renders the handoff notification marker"),
        f"{_H}/domain/notification.py": (["notify", "notify"], "renders the notify marker"),
        f"{_D}/domain/callback_recovery_key.py": (
            ["*", "*", "*"],
            "renders the recovery-admission marker through the shared value validator it "
            "originally hardened; reads back through the shared strict gate reader",
        ),
        f"{_D}/domain/callback_sweep_watermark.py": (
            ["*", "*", "*", "*", "workflow-event"],
            "renders the sweep record / dispatch markers",
        ),
        f"{_D}/domain/hibernate_evidence_integration.py": (
            ["*", "*"],
            "renders the integration evidence marker",
        ),
        f"{_D}/domain/hibernate_evidence_marker.py": (
            ["*"],
            "renders the lane evidence marker, fail-closed on its own fields",
        ),
        f"{_D}/domain/hibernated_bound_pair_composer_discard.py": (
            ["workflow-event", "workflow-event"],
            "renders the composer-discard approval marker",
        ),
        f"{_D}/domain/hibernated_bound_pair_convergence.py": (
            ["workflow-event"],
            "renders the convergence approval marker",
        ),
        f"{_D}/application/operator_startup_gate_producer.py": (
            ["*"],
            "renders the operator startup gate note",
        ),
        f"{_D}/application/sublane_diagnostics.py": (
            ["*", "*", "*", "*", "*", "workflow-event"],
            "renders the callback-lease blocker marker",
        ),
        # -- arrived with origin/main-next (#14585 / #14665) -----------------------------
        f"{_D}/domain/canonical_note_scan.py": (
            ['*:MARKER_RE'],
            "owns the quote-aware canonical scan: the token regex, the recognized channels and "
            "the per-line marker scan every reader of this grammar now shares",
        ),
        # #14667 moved the proxy decision's marker grammar — producer, shapes, reader — out of
        # ``application/coordinator_proxy_send.py`` and into the pure domain module below. The
        # rail no longer NAMES the token; it holds `workflow-event` only by inheriting the new
        # owner across one used import, so its entry moves to INHERITED. Two consumers that used
        # to inherit through it (`cli_workflow_proxy`, `cli_workflow_role_authority`) are now two
        # hops from the owner and hold nothing — their entries are removed rather than kept, since
        # this gate compares channels in BOTH directions and a declaration for a module that no
        # longer holds the token is exactly the stale capability record it exists to catch.
        f"{_D}/domain/coordinator_proxy_decision.py": (
            ['*', '*', '*:MARKER_RE', 'workflow-event'],
            "owns the proxy decision's marker grammar: RENDERS the decision marker (through the "
            "shared marker value contract, so it cannot write a body that reads back as a "
            "different one) and READS ONE named journal for it, per canonical line, requiring "
            "exactly one accepted marker whose field set is a shape the producer can render. A "
            "note carrying two such markers, or one unreadable/un-renderable claim beside a clean "
            "one, refuses the whole journal rather than picking (#14667)",
        ),
        # -- prose ---------------------------------------------------------------------
        f"{_D}/application/cli_workflow_watch.py": (
            ["*", "*", "*", "*", "*:watch"],
            "argparse help text only: it names the token to explain the flag and neither builds "
            "nor matches a marker",
        ),
    }

    #: Modules that hold NO marker token of their own but inherit an owner's capability set
    #: through a used import relation (Redmine #14539 review j#92567). They are declared because
    #: the gate can no longer ask WHICH name inside the owner carries the capability — deciding
    #: that means enumerating Python's binding forms, which this gate has lost to three times.
    #: The trade is stated in ``test_no_module_shape_can_hide_a_capability_from_a_wildcard_consumer``:
    #: over-detection costs a declaration line, a missed reader costs a silent gate.
    INHERITED = {
        f"{_D}/application/coordinator_proxy_send.py": (
            ['workflow-event'],
            "inherits via a used import of coordinator_proxy_decision; names no marker token "
            "itself since #14667 moved the decision grammar to that owner",
        ),
        "src/mozyo_bridge/application/commands.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/application/handoff_delivery_command.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/application/handoff_target_activation_command.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/application/notify_command.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/application/pane_primitive_command.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/e_110_execution_platform/f_110_workspace_session_identity/application/commands_session.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/e_110_execution_platform/f_110_workspace_session_identity/domain/session_boundary.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/cli_handoff.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/cli_handoff_q_enter.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/cli_handoff_ticketless.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/handoff_admission_pipeline.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/handoff_envelope_planner.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/handoff_herdr_standard_rail.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/handoff_target_resolution.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/handoff_tmux_transport_rail.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/application/startup_admission_gate.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_H}/domain/delivery_record_sink.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_D}/application/callback_gate_record.py": (
            ['*', '*', '*', '*', 'workflow-event'],
            "inherits via a used import of callback_sweep_watermark, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/callback_outbox_processor.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/callback_recovery_admission.py": (
            ['*', '*', '*', '*', '*', 'workflow-event'],
            "inherits via a used import of callback_recovery_key, callback_sweep_watermark, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/callback_recovery_record.py": (
            ['*', '*', '*', '*', '*', 'workflow-event'],
            "inherits via a used import of callback_recovery_key, callback_sweep_watermark, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/callback_runtime.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/callback_sweep.py": (
            ['*', '*', '*', '*', 'workflow-event'],
            "inherits via a used import of callback_sweep_watermark, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/cli_handoff_delegate_dispatch.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_D}/application/cli_sublane_group.py": (
            ['*', 'handoff'],
            "inherits via a used import of sublane_diagnostics, sublane_quarantine; names no marker token itself",
        ),
        f"{_D}/application/cli_workflow.py": (
            ['*', '*', '*:watch', 'operator-startup-gate'],
            "inherits via a used import of cli_workflow_watch, operator_startup_resume_leg; names no marker token itself",
        ),
        f"{_D}/application/cli_workflow_callbacks.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/cli_workflow_dispatch_ir.py": (
            ['*', '*', 'handoff'],
            "inherits via a used import of handoff, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/cli_workflow_recovery_admission.py": (
            ['*'],
            "inherits via a used import of sublane_diagnostics; names no marker token itself",
        ),
        f"{_D}/application/dispatch_disposition_writer.py": (
            ['*', '*', '*', '*', '*:_MARKER_RE', '*:_MARKER_RE'],
            "inherits via a used import of dispatch_authorization, dispatch_disposition, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/gateway_disposition_intake.py": (
            ['*', '*', '*', '*:_MARKER_RE'],
            "inherits via a used import of dispatch_authorization, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/gateway_route_gate.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_D}/application/herdr_dispatch_authority.py": (
            ['*', '*:_MARKER_RE'],
            "inherits via a used import of dispatch_authorization; names no marker token itself",
        ),
        f"{_D}/application/herdr_dispatch_cli.py": (
            ['*', '*:_MARKER_RE'],
            "inherits via a used import of dispatch_authorization; names no marker token itself",
        ),
        f"{_D}/application/herdr_dispatch_execution.py": (
            ['*', '*:_MARKER_RE'],
            "inherits via a used import of dispatch_authorization; names no marker token itself",
        ),
        f"{_D}/application/hibernate_supervisor_wiring.py": (
            ['*', '*', '*'],
            "inherits via a used import of hibernate_evidence_marker, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/live_redmine_journal_source.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/main_lane_guard_gate.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_D}/application/operator_startup_resume_record.py": (
            ['operator-startup-gate'],
            "inherits via a used import of operator_startup_resume_leg; names no marker token itself",
        ),
        f"{_D}/application/operator_startup_resume_target.py": (
            ['operator-startup-gate'],
            "inherits via a used import of operator_startup_resume_leg; names no marker token itself",
        ),
        f"{_D}/application/reconcile_dispatch_writer.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/recovered_pair_pin_reconciliation.py": (
            ['*:_AUTHORITY_RE'],
            "inherits via a used import of recovered_pair_pin_reconciliation; names no marker token itself",
        ),
        f"{_D}/application/recovered_pair_pin_reconciliation_live.py": (
            ['*', '*', '*:_AUTHORITY_RE', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of recovered_pair_pin_reconciliation, recovery_anchor_delivery; names no marker token itself",
        ),
        f"{_D}/application/recovered_worker_delivery_live.py": (
            ['*', '*', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of recovery_anchor_delivery; names no marker token itself",
        ),
        f"{_D}/application/recovery_anchor_delivery_live.py": (
            ['*', '*', 'handoff', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of handoff, recovery_anchor_delivery; names no marker token itself",
        ),
        f"{_D}/application/retire_admissibility.py": (
            ['*', '*', '*'],
            "inherits via a used import of hibernate_evidence_integration, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/sublane_gateway_recovery_live.py": (
            ['*', '*', 'handoff', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of handoff, recovery_anchor_delivery; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernate_boundary.py": (
            ['handoff'],
            "inherits via a used import of sublane_quarantine; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernated_bound_pair_composer_discard.py": (
            ['workflow-event', 'workflow-event'],
            "inherits via a used import of hibernated_bound_pair_composer_discard, hibernated_bound_pair_convergence; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernated_bound_pair_composer_discard_live.py": (
            ['*', '*', 'handoff', 'workflow-event', 'workflow-event'],
            "inherits via a used import of sublane_quarantine, hibernated_bound_pair_composer_discard, hibernated_bound_pair_convergence, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernated_bound_pair_convergence.py": (
            ['workflow-event'],
            "inherits via a used import of hibernated_bound_pair_convergence; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernated_bound_pair_convergence_live.py": (
            ['*', '*', 'workflow-event'],
            "inherits via a used import of hibernated_bound_pair_convergence, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernated_live_reconcile_ops.py": (
            ['handoff'],
            "inherits via a used import of sublane_quarantine; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernated_pair_recovery.py": (
            ['*', '*', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of recovery_anchor_delivery; names no marker token itself",
        ),
        f"{_D}/application/sublane_hibernated_pair_recovery_live.py": (
            ['*', '*', 'handoff', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of sublane_quarantine, recovery_anchor_delivery; names no marker token itself",
        ),
        f"{_D}/application/sublane_prepare_readonly_projection.py": (
            ['handoff', 'workflow-event'],
            "inherits via a used import of sublane_quarantine, hibernated_bound_pair_convergence; names no marker token itself",
        ),
        f"{_D}/application/sublane_quarantine_inspect.py": (
            ['handoff'],
            "inherits via a used import of sublane_quarantine; names no marker token itself",
        ),
        f"{_D}/application/sublane_recover_pair_redispatch_edge.py": (
            ['*', '*', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of recovery_anchor_delivery; names no marker token itself",
        ),
        f"{_D}/application/sublane_recovered_pair_pin_reconciliation_cli.py": (
            ['*:_AUTHORITY_RE'],
            "inherits via a used import of recovered_pair_pin_reconciliation; names no marker token itself",
        ),
        f"{_D}/application/sublane_stale_worker_recovery_live.py": (
            ['handoff', 'handoff'],
            "inherits via a used import of handoff, sublane_quarantine; names no marker token itself",
        ),
        f"{_D}/application/supervisor_wiring.py": (
            ['*', '*', '*', '*', '*:watch', 'handoff'],
            "inherits via a used import of handoff, cli_workflow_watch, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/workspace_callback_review_return.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/application/workspace_callback_supervisor.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/dispatch_authority.py": (
            ['*', '*:_MARKER_RE'],
            "inherits via a used import of dispatch_authorization; names no marker token itself",
        ),
        f"{_D}/domain/gateway_route_enforcement.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        f"{_D}/domain/glance_integration_disposition.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/glance_journal_grammar.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/hibernate_basis_producer.py": (
            ['*', '*', '*', '*', 'handoff'],
            "inherits via a used import of hibernate_evidence_integration, hibernate_evidence_marker, hibernate_park_record, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/hibernate_evidence_authority.py": (
            ['*'],
            "inherits via a used import of hibernate_evidence_marker; names no marker token itself",
        ),
        f"{_D}/domain/hibernate_issuer_policy.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/lane_work_anchor.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/recovered_worker_delivery.py": (
            ['*', '*', '*', '*', 'recovery-delivery-authorization', 'recovery-delivery-zero-send'],
            "inherits via a used import of recovery_anchor_delivery, redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/review_exemption.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
        f"{_D}/domain/strict_marker_read.py": (
            ['*:MARKER_RE'],
            "inherits via a used import of canonical_note_scan; names no marker token itself "
            "(#14687 moved the strict readers here byte-identical, and the token regex stayed "
            "with the scan that owns quote/fence exclusion)",
        ),
        f"{_D}/domain/worker_turn_recovery.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself "
            "(it declares the worker-progress gate vocabulary, it does not read notes)",
        ),
        f"{_D}/application/sublane_worker_refresh.py": (
            ['*'],
            "inherits via a used import of worker_refresh_approval; names no marker token "
            "itself, and reads no note — it plans the refresh and delegates approval "
            "verification to that module's strict parser",
        ),
        f"{_D}/application/sublane_worker_refresh_durable_read.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token "
            "itself. Reads worker-progress gates STRICTLY and fails closed toward progress: a "
            "note carrying any marker the canonical producer could not render counts as "
            "progress, which refuses the destructive refresh (#14687 R1-F1)",
        ),
        f"{_D}/application/sublane_worker_refresh_live.py": (
            ['*', '*', '*', 'handoff', 'recovery-delivery-authorization',
             'recovery-delivery-zero-send'],
            "inherits via used imports of worker_refresh_approval, recovery_anchor_delivery and "
            "handoff; names no marker token itself and reads no note directly — the durable "
            "read, the approval check and the recovery delivery each keep their own discipline",
        ),
        "src/mozyo_bridge/e_140_adapter_provider/f_110_ticket_adapter_common/domain/ticket_adapter.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/e_140_adapter_provider/f_120_redmine_adapter/infrastructure/redmine_ticket_provider.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/application/herdr_send_entry.py": (
            ['handoff'],
            "inherits via a used import of handoff; names no marker token itself",
        ),
        "src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/application/herdr_session_retire_ops.py": (
            ['*', '*', '*', '*', '*:_MARKER_RE', '*:_MARKER_RE', 'handoff'],
            "inherits via a used import of sublane_quarantine, dispatch_authorization, dispatch_disposition, redmine_journal_source; names no marker token itself",
        ),
        "src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/domain/composer_discard_approval.py": (
            ['*', '*'],
            "inherits via a used import of redmine_journal_source; names no marker token itself",
        ),
    }

    @classmethod
    def _declared(cls):
        """Every declared holder: the curated own-token table plus the inherited entries."""
        merged = dict(cls.DISCIPLINES)
        merged.update(cls.INHERITED)
        return {path: sorted(channels) for path, (channels, _why) in merged.items()}

    def _root(self):
        import pathlib

        return pathlib.Path(__file__).resolve().parents[2] / "src" / "mozyo_bridge"

    def test_every_marker_token_holder_declares_its_channels(self):
        self.assertEqual(
            _inventory_mismatch(_marker_token_holders(self._root()), self._declared()),
            {},
            "a module names the mozyo marker token without a matching declaration. Compare the "
            "CHANNELS too: a declared module that gains a second channel is a new capability and "
            "must be re-declared. Say what the module does with the token (reads / renders / "
            "prose) and, if it reads, what happens when a note carries more than one marker",
        )

    def test_the_gate_compares_channels_not_only_paths(self):
        """R26-F1(b): a declared module gaining a second channel must NOT stay green.

        Asserted against a synthetic pair, because ``src`` is consistent by construction and so
        cannot tell a channel-aware comparison from a path-only one.
        """
        declared = {"a.py": ["c1"]}
        self.assertEqual(_inventory_mismatch({"a.py": ["c1"]}, declared), {})  # control
        self.assertEqual(
            _inventory_mismatch({"a.py": ["c1", "c2"]}, declared),
            {"a.py": (["c1", "c2"], ["c1"])},
        )
        self.assertIn("b.py", _inventory_mismatch({"a.py": ["c1"], "b.py": ["c1"]}, declared))

    def test_the_declared_identities_are_repo_relative_paths(self):
        for declared in self._declared():
            self.assertTrue(
                declared.startswith("src/mozyo_bridge/") and declared.endswith(".py"),
                f"{declared!r} is not a repo-relative module path; a basename key lets an "
                "undeclared holder inherit an allowlisted name from another package",
            )

    def test_every_declaration_says_what_the_module_does(self):
        for path, (channels, why) in {**self.DISCIPLINES, **self.INHERITED}.items():
            self.assertTrue(channels, f"{path} declares no channel")
            self.assertGreater(len(why), 20, f"{path} has no real discipline text")


class ReviewJ92374InventoryDetectionTests(unittest.TestCase):
    """The detector itself, over synthetic trees: every evasion this pin has ever leaked through.

    Driven against :func:`_marker_token_holders` rather than ``src``, so these pin the RULE and
    keep working when the repo's own holder set changes.
    """

    OWNER = (
        "import re\n"
        '_MARKER_RE = re.compile(r"\\[mozyo:chan-a:(?P<body>[^\\]]*)\\]")\n'
        'def read(n): return [m.group("body") for m in _MARKER_RE.finditer(n or "")]\n'
    )

    def _holders(self, modules, owner=True):
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo" / "src" / "mozyo_bridge"
            root.mkdir(parents=True)
            if owner:
                (root / "owner.py").write_text(self.OWNER, encoding="utf-8")
            for name, text in modules.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            return _marker_token_holders(root)

    def test_the_owner_itself_is_detected(self):
        """Control: without this every "detected" below would prove nothing."""
        self.assertEqual(self._holders({}), {"src/mozyo_bridge/owner.py": ["chan-a"]})

    # -- the four evasions j#92374 finding 1 measured --------------------------------

    def test_two_owners_sharing_a_basename_both_resolve(self):
        """(a) ``path.stem`` keying made the later owner win and lost the earlier one's names."""
        a = 'import re\nRE_A = re.compile(r"\\[mozyo:chan-a:([^\\]]*)\\]")\n'
        b = 'import re\nRE_B = re.compile(r"\\[mozyo:chan-b:([^\\]]*)\\]")\n'
        consumer = (
            "from mozyo_bridge.a.owner import RE_A\n"
            'def read(n): return [m.group(1) for m in RE_A.finditer(n or "")]\n'
        )
        holders = self._holders(
            {"a/owner.py": a, "b/owner.py": b, "consumer.py": consumer}, owner=False
        )
        self.assertEqual(holders.get("src/mozyo_bridge/consumer.py"), ["chan-a"])

    def test_a_second_channel_in_a_declared_module_is_a_new_capability(self):
        """(b) the gate compared only keys, so a second channel inherited the discipline."""
        one = 'import re\nRE1 = re.compile(r"\\[mozyo:c1:([^\\]]*)\\]")\ndef r(n): return RE1.findall(n)\n'
        two = one + 'RE2 = re.compile(r"\\[mozyo:c2:([^\\]]*)\\]")\ndef r2(n): return RE2.findall(n)\n'
        self.assertEqual(self._holders({"m.py": one}, owner=False)["src/mozyo_bridge/m.py"], ["c1"])
        self.assertEqual(
            self._holders({"m.py": two}, owner=False)["src/mozyo_bridge/m.py"], ["c1", "c2"]
        )

    def test_an_annotated_assignment_is_detected(self):
        """(c) ``RE: re.Pattern = re.compile(...)`` is not an ``ast.Assign``."""
        module = (
            "import re\n"
            'RE: re.Pattern = re.compile(r"\\[mozyo:chan-b:([^\\]]*)\\]")\n'
            "def read(n): return RE.findall(n or '')\n"
        )
        self.assertIn("src/mozyo_bridge/pkg/ann.py", self._holders({"pkg/ann.py": module}))

    def test_a_pattern_bound_by_ANNOTATED_assignment_resolves_across_an_import(self):
        """(c) at the layer that needs it: the importer carries no literal of its own.

        The value-position rule already catches the owner, so only the IMPORT path distinguishes
        an annotated binding from a plain one — which is why the earlier version of this test
        passed with ``AnnAssign`` handling removed.
        """
        owner = (
            "import re\n"
            'RE_ANN: re.Pattern = re.compile(r"\\[mozyo:chan-b:([^\\]]*)\\]")\n'
        )
        consumer = (
            "from mozyo_bridge.annowner import RE_ANN\n"
            "def read(n): return RE_ANN.findall(n or '')\n"
        )
        holders = self._holders(
            {"annowner.py": owner, "pkg/consumer.py": consumer}, owner=False
        )
        self.assertEqual(holders.get("src/mozyo_bridge/pkg/consumer.py"), ["chan-b"])

    # -- the one hop, DERIVED rather than remembered (j#92455) ------------------------

    #: The grammar axes of the two statements that can bind a name from another module. The
    #: cells are their PRODUCT, filtered by what the grammar actually admits — not a list of
    #: forms someone thought of (Redmine #14539 review j#92477 finding 2). R28 claimed a
    #: derivation and shipped five hand-written forms in a loop, which is the same remembered
    #: enumeration R27 had already been caught by; looping over a list is not deriving it.
    _FROM_AXES = {
        "level": (0, 1, 2, 3),
        "module_present": (True, False),
        "target": ("symbol", "submodule", "star"),
        "alias": (True, False),
        "site": ("module", "init"),
    }
    _IMPORT_AXES = {"depth": (2, 3, 4), "alias": (True, False), "site": ("module", "init")}

    @staticmethod
    def _is_a_statement(level, module_present, target, alias, site):
        """Whether the Python GRAMMAR admits this combination at all."""
        del site  # the consumer's own kind cannot make a statement ungrammatical
        if level == 0 and not module_present:
            return False  # `from  import x` is not a statement
        if target == "star" and alias:
            return False  # `from m import * as x` is not a statement
        return True

    @staticmethod
    def _is_one_hop(level, module_present, target, alias, site):
        """Whether a grammatical statement actually crosses ONE module boundary.

        Deliberately separate from :meth:`_is_a_statement` (review j#92508's non-blocking
        observation, and RR j#92497's own review_focus 2): "the grammar forbids this" and "this
        is not the relationship the inventory is about" are different claims, and collapsing
        them into one predicate made the matrix look narrower than the grammar for a reason the
        reader could not see.
        """
        del alias
        if level == 1 and site == "init" and not module_present and target in ("symbol", "star"):
            # The package `__init__` would be importing out of its own namespace: zero hops.
            return False
        return True

    @classmethod
    def _admits(cls, level, module_present, target, alias, site):
        """Grammatical AND one hop — the two questions, asked separately."""
        return cls._is_a_statement(level, module_present, target, alias, site) and cls._is_one_hop(
            level, module_present, target, alias, site
        )

    def _derive_one_hop_cells(self):
        import itertools

        cells = []
        for level, module_present, target, alias, site in itertools.product(
            *(self._FROM_AXES[k] for k in
              ("level", "module_present", "target", "alias", "site"))
        ):
            if self._admits(level, module_present, target, alias, site):
                cells.append(("from", level, module_present, target, alias, site))
        for depth, alias, site in itertools.product(
            *(self._IMPORT_AXES[k] for k in ("depth", "alias", "site"))
        ):
            # `import` is never relative and always names a module, so its only axes are how
            # deep the dotted path is and whether it is aliased.
            cells.append(("import", depth, True, "module", alias, site))
        return cells

    def _materialize(self, cells):
        """One tree holding a consumer per cell; returns {consumer path: (cell, statement)}."""
        pattern = (
            'import re\n%s = re.compile(r"\\[mozyo:chan-a:([^\\]]*)\\]")\n'
        )
        files, expected = {"__init__.py": ""}, {}
        for index, cell in enumerate(cells):
            kind, first, module_present, target, alias, site = cell
            pkg = f"c{index}"
            consumer_is_package_init = kind == "from" and first == 1 and site == "init"
            files[f"{pkg}/__init__.py"] = "" if consumer_is_package_init else pattern % "PKG_RE"
            files[f"{pkg}/owner.py"] = pattern % "RE"
            files[f"{pkg}/sub2/__init__.py"] = ""
            files[f"{pkg}/sub2/inner.py"] = pattern % "RE"
            if kind == "from":
                level = first
                nested = [f"d{k}" for k in range(1, level)] if level else (
                    [] if site == "module" else ["s0"]
                )
                leaf = "consumer.py" if site == "module" else "__init__.py"
                consumer = "/".join([pkg] + nested + [leaf])
                for depth in range(1, len(nested) + 1):
                    files.setdefault("/".join([pkg] + nested[:depth] + ["__init__.py"]), "")
                dots = "." * level
                suffix = " as _p" if alias else ""
                if target == "symbol":
                    statement = (
                        f"from mozyo_bridge.{pkg}.owner import RE{suffix}" if level == 0
                        else f"from {dots}owner import RE{suffix}" if module_present
                        else f"from {dots} import PKG_RE{suffix}"
                    )
                    use = "_p" if alias else ("RE" if (level == 0 or module_present) else "PKG_RE")
                elif target == "submodule":
                    statement = (
                        f"from mozyo_bridge.{pkg} import owner{suffix}" if level == 0
                        else f"from {dots}sub2 import inner{suffix}" if module_present
                        else f"from {dots} import owner{suffix}"
                    )
                    base = "_p" if alias else (
                        "owner" if (level == 0 or not module_present) else "inner"
                    )
                    use = f"{base}.RE"
                else:
                    statement = (
                        f"from mozyo_bridge.{pkg}.owner import *" if level == 0
                        else f"from {dots}owner import *" if module_present
                        else f"from {dots} import *"
                    )
                    use = "RE" if (level == 0 or module_present) else "PKG_RE"
            else:
                module = {
                    2: f"mozyo_bridge.{pkg}",
                    3: f"mozyo_bridge.{pkg}.owner",
                    4: f"mozyo_bridge.{pkg}.sub2.inner",
                }[first]
                attribute = "PKG_RE" if first == 2 else "RE"
                statement = f"import {module}" + (" as _o" if alias else "")
                use = f"_o.{attribute}" if alias else f"{module}.{attribute}"
                consumer = f"{pkg}/consumer.py" if site == "module" else f"{pkg}/s0/__init__.py"
                files.setdefault(f"{pkg}/s0/__init__.py", "")
            files[consumer] = f"{statement}\ndef read(n): return {use}.findall(n or '')\n"
            expected[f"src/mozyo_bridge/{consumer}"] = (cell, statement)
        return files, expected

    def test_every_one_hop_binding_form_the_grammar_admits_resolves(self):
        """The product of the axes, materialized and checked — every cell, not a chosen few."""
        import pathlib
        import tempfile

        cells = self._derive_one_hop_cells()
        files, expected = self._materialize(cells)
        # No cell may be silently dropped on the way to being checked. This replaces the
        # ``checked == 27`` magic count, which only ever proved "the 27 I picked ran".
        self.assertEqual(
            len(expected), len(cells), "a derived cell was not materialized into a consumer"
        )
        self.assertEqual(
            {cell for cell, _statement in expected.values()},
            set(cells),
            "the materialized cells are not the derived cells",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo" / "src" / "mozyo_bridge"
            root.mkdir(parents=True)
            for name, text in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            holders = _marker_token_holders(root)
        undetected = {
            path: statement
            for path, (_cell, statement) in expected.items()
            if "chan-a" not in (holders.get(path) or [])
        }
        self.assertEqual(
            undetected,
            {},
            "these one-hop consumers hold a marker capability and left the inventory",
        )

    def test_the_derivation_covers_the_forms_earlier_rounds_missed(self):
        """Guards the AXES themselves: narrowing them must not quietly shrink the matrix.

        Each of these was a real escape found by review — ``ImportFrom(module=None)`` in both
        its named and wildcard forms (j#92477), relative level 3 (j#92477), and an alias-less
        deep dotted ``import`` (j#92477). They are asserted as members of the DERIVED set, so a
        future edit that drops an axis value reddens here rather than silently narrowing.
        """
        cells = set(self._derive_one_hop_cells())
        for label, cell in (
            ("from . import NAME", ("from", 1, False, "symbol", False, "module")),
            ("from . import *", ("from", 1, False, "star", False, "module")),
            ("from ... import NAME (level 3)", ("from", 3, False, "symbol", False, "module")),
            ("level 3 into a package __init__", ("from", 3, True, "symbol", False, "init")),
            ("import a.b.c.d with no alias", ("import", 4, True, "module", False, "module")),
        ):
            with self.subTest(label):
                self.assertIn(cell, cells)

    def test_the_grammar_filter_rejects_only_non_statements(self):
        """One question: could Python parse this at all?"""
        self.assertFalse(self._is_a_statement(0, False, "symbol", False, "module"))
        self.assertFalse(self._is_a_statement(1, True, "star", True, "module"))
        # A package importing out of its own namespace IS grammatical — it is just not one hop.
        self.assertTrue(self._is_a_statement(1, False, "symbol", False, "init"))
        self.assertTrue(self._is_a_statement(3, False, "star", False, "init"))

    def test_the_one_hop_filter_rejects_only_zero_hop_relationships(self):
        """The other question: does the statement cross a module boundary?"""
        self.assertFalse(self._is_one_hop(1, False, "symbol", False, "init"))
        self.assertFalse(self._is_one_hop(1, False, "star", False, "init"))
        # Its own SUBpackage is a different module, so that one does cross.
        self.assertTrue(self._is_one_hop(1, False, "submodule", False, "init"))
        # The one-hop question says nothing about grammar; ungrammatical cells pass it.
        self.assertTrue(self._is_one_hop(0, False, "symbol", False, "module"))

    def test_admission_is_the_conjunction_of_the_two_questions(self):
        for cell in (
            (0, False, "symbol", False, "module"),
            (1, True, "star", True, "module"),
            (1, False, "symbol", False, "init"),
            (1, False, "submodule", False, "init"),
            (3, False, "star", False, "init"),
        ):
            with self.subTest(cell=cell):
                self.assertEqual(
                    self._admits(*cell),
                    self._is_a_statement(*cell) and self._is_one_hop(*cell),
                )


    #: Every ``__all__`` shape review has thrown at this gate, R29 through R31. Each one is a
    #: module whose wildcard importer Python DOES bind ``used`` in — the whole corpus exists
    #: because three rounds of emulating ``__all__`` kept missing another binding form.
    _ALL_SHAPES = (
        # j#92477 — the reader took the first `ast.Assign` `ast.walk` produced
        ("reassigned", "private", '__all__ = []\n__all__ = ["_RE"]\n'),
        ("augmented", "private", '__all__ = []\n__all__ += ["_RE"]\n'),
        ("annotated", "private", '__all__: list[str] = ["_RE"]\n'),
        ("nested function scope only", "public", "def f():\n    __all__ = []\n    return __all__\n"),
        # j#92508 — the reader looked only at `tree.body`'s direct children
        ("if body", "private", 'if True:\n    __all__ = ["_RE"]\n'),
        ("try body", "private", 'try:\n    __all__ = ["_RE"]\nexcept Exception:\n    pass\n'),
        ("for body", "private", 'for _ in (1,):\n    __all__ = ["_RE"]\n'),
        (
            "with body",
            "private",
            'import contextlib\nwith contextlib.suppress():\n    __all__ = ["_RE"]\n',
        ),
        ("deleted again", "public", "__all__ = []\ndel __all__\n"),
        ("assignment expression", "private", '(__all__ := ["_RE"])\n'),
        # j#92538 — the reader enumerated five binder node types
        ("for target", "private", 'for __all__ in [["_RE"]]:\n    pass\n'),
        (
            "with target",
            "private",
            'from contextlib import nullcontext\nwith nullcontext(["_RE"]) as __all__:\n    pass\n',
        ),
        ("match capture", "private", 'match ["_RE"]:\n    case __all__:\n        pass\n'),
        (
            "except target",
            "public",
            "__all__ = []\ntry:\n    raise RuntimeError()\nexcept RuntimeError as __all__:\n    pass\n",
        ),
        # Shapes no allowlist ever reached, kept because they cost nothing now
        ("computed", "private", "__all__ = sorted(globals())\n"),
        ("empty literal", "public", "__all__ = []\n"),
        ("star-import target", "private", "for (__all__,) in [(['_RE'],)]:\n    pass\n"),
        ("no __all__ at all", "private", ""),
    )

    def test_no_module_shape_can_hide_a_capability_from_a_wildcard_consumer(self):
        """R31-F1: a wildcard propagates the owner's whole capability set, unconditionally.

        Three rounds were spent extending an allowlist of binder nodes so the gate could decide
        which names ``from M import *`` really brings across. Every round a form outside the
        allowlist turned up, and every miss was a consumer silently leaving the inventory. The
        emulation is gone: the corpus below is not a list of cases the reader must classify
        correctly, it is a list of shapes that must all reach the SAME answer, because the
        answer no longer depends on reading ``__all__`` at all.

        Note ``empty literal`` and ``no __all__``: under the old emulation those two were the
        negative cases — a private name did NOT cross. They cross now, and that is the trade the
        convergence makes. Over-detection costs one declaration line; a missed authority reader
        costs a silent gate.
        """
        patterns = {
            "public": 'import re\nRE = re.compile(r"\\[mozyo:chan-a:([^\\]]*)\\]")\n',
            "private": 'import re\n_RE = re.compile(r"\\[mozyo:chan-a:([^\\]]*)\\]")\n',
        }
        used = {"public": "RE", "private": "_RE"}
        for label, kind, tail in self._ALL_SHAPES:
            with self.subTest(label):
                consumer = (
                    "from mozyo_bridge.owner import *\n"
                    f"def read(n): return {used[kind]}.findall(n or '')\n"
                )
                self.assertEqual(
                    self._holders(
                        {"owner.py": patterns[kind] + tail, "c.py": consumer}, owner=False
                    ).get("src/mozyo_bridge/c.py"),
                    ["chan-a"],
                    f"{label}: a wildcard consumer must never leave the inventory",
                )

    def test_the_resolver_does_not_read_dunder_all_at_all(self):
        """The convergence itself, pinned: re-introducing the emulation reddens here.

        Structural on purpose. The property being protected is not "``__all__`` is handled
        correctly" — that is what failed three times — but "``__all__`` is not consulted", which
        is the only shape of this gate with no binder enumeration behind it to fall out of date.

        Read from the AST, not the text: the resolver's docstring necessarily says the word
        while explaining why it does not look at it, and a text search flags that. (Detect the
        operation, not the spelling — the same rule the inventory itself had to learn.)
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(_marker_token_holders)))
        references = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.Constant) and node.value == "__all__")
            or (isinstance(node, ast.Name) and node.id == "__all__")
            or (isinstance(node, ast.Attribute) and node.attr == "__all__")
        ]
        self.assertEqual(
            references,
            [],
            "the wildcard resolver is consulting __all__ again; propagate the owner's whole "
            "capability set instead (Redmine #14539 review j#92538)",
        )

    def test_a_wildcard_propagates_even_when_nothing_is_used(self):
        """R32-F1: the wildcard used-name filter is gone, deliberately.

        It could only ask "is this name used" by consulting the owner's bound-name map, and that
        map cannot be built without enumerating Python's binding forms — the thing this gate has
        lost to three times. So a wildcard consumer inherits the owner's capabilities whether or
        not it touches them: an unused ``import *`` is over-detected, which costs a declaration
        line, rather than under-detected, which costs a silent reader.
        """
        owner = 'import re\nRE = re.compile(r"\\[mozyo:chan-a:([^\\]]*)\\]")\n'
        unused = "from mozyo_bridge.owner import *\nVALUE = 1\n"
        used = "from mozyo_bridge.owner import *\ndef read(n): return RE.findall(n or '')\n"
        holders = self._holders({"owner.py": owner, "no.py": unused, "yes.py": used}, owner=False)
        self.assertEqual(holders.get("src/mozyo_bridge/no.py"), ["chan-a"])
        self.assertEqual(holders.get("src/mozyo_bridge/yes.py"), ["chan-a"])

    def test_an_explicit_import_still_has_to_be_used(self):
        """The boundary that survives: an unused EXPLICIT import names a local the consumer can
        be checked against, so it needs no bound-name map and still gates."""
        owner = 'import re\nRE = re.compile(r"\\[mozyo:chan-a:([^\\]]*)\\]")\n'
        unused = "from mozyo_bridge.owner import RE  # noqa: F401\nVALUE = 1\n"
        used = "from mozyo_bridge.owner import RE\ndef read(n): return RE.findall(n or '')\n"
        holders = self._holders({"owner.py": owner, "no.py": unused, "yes.py": used}, owner=False)
        self.assertIsNone(holders.get("src/mozyo_bridge/no.py"))
        self.assertEqual(holders.get("src/mozyo_bridge/yes.py"), ["chan-a"])

    def test_the_owner_capability_set_propagates_whatever_bound_it(self):
        """R32-F1 proper: four owner binding forms, each reached both ways.

        The owner's capability is real in every case — Python binds ``RE`` and both a direct and
        a wildcard consumer can call it. Before this round the propagation asked which name the
        owner had bound the pattern to, learned that only from ``Assign`` / ``AnnAssign``, and so
        returned an empty map for all four, dropping both consumers.
        """
        marker = '\\[mozyo:chan-a:([^\\]]*)\\]'
        owners = {
            "for target": f'import re\nfor RE in [re.compile(r"{marker}")]:\n    pass\n',
            "with target": (
                "import re\nfrom contextlib import nullcontext\n"
                f'with nullcontext(re.compile(r"{marker}")) as RE:\n    pass\n'
            ),
            "assignment expression": f'import re\n(RE := re.compile(r"{marker}"))\n',
            "decorator result": (
                f'import re\ndef deco(f):\n    return re.compile(r"{marker}")\n'
                "@deco\ndef RE():\n    pass\n"
            ),
        }
        direct = "from mozyo_bridge.owner import RE\ndef read(n): return RE.findall(n or '')\n"
        wildcard = "from mozyo_bridge.owner import *\ndef read(n): return RE.findall(n or '')\n"
        for label, owner in owners.items():
            with self.subTest(label):
                holders = self._holders(
                    {"owner.py": owner, "d.py": direct, "w.py": wildcard}, owner=False
                )
                self.assertEqual(holders.get("src/mozyo_bridge/owner.py"), ["chan-a"])
                self.assertEqual(holders.get("src/mozyo_bridge/d.py"), ["chan-a"], "direct")
                self.assertEqual(holders.get("src/mozyo_bridge/w.py"), ["chan-a"], "wildcard")

    def test_the_resolver_keeps_no_bound_name_map(self):
        """The convergence, pinned structurally: no ``{name: capabilities}`` anywhere.

        Same shape as the ``__all__`` pin one layer up — the property worth protecting is that
        the question is not asked, because every answer to it has been an enumeration that fell
        behind the language.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(_marker_token_holders)))
        returns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "module_capabilities"
        ]
        self.assertEqual(len(returns), 1)
        for node in ast.walk(returns[0]):
            self.assertNotIsInstance(
                node,
                ast.Dict,
                "module_capabilities is building a name map again; owners hold a flat capability "
                "list and propagation must not depend on which name bound it (j#92567)",
            )

    # -- the import forms j#92420 finding 1 measured ---------------------------------

    def test_a_RELATIVE_import_of_a_pattern_resolves(self):
        """``from .owner import RE`` — the ordinary way to write this hop inside a package.

        The consumer carries no token literal, so before the fix it left the inventory entirely.
        """
        holders = self._holders(
            {
                "pkg/__init__.py": "",
                "pkg/owner.py": self.OWNER,
                "pkg/consumer.py": (
                    "from .owner import _MARKER_RE\n"
                    "def read(n): return _MARKER_RE.findall(n or '')\n"
                ),
            },
            owner=False,
        )
        self.assertEqual(holders.get("src/mozyo_bridge/pkg/consumer.py"), ["chan-a"])

    def test_a_MODULE_import_plus_attribute_access_resolves(self):
        """``import a.b as o`` then ``o.RE`` — the previous version never looked at ``ast.Import``."""
        consumer = (
            "import mozyo_bridge.owner as o\n"
            "def read(n): return o._MARKER_RE.findall(n or '')\n"
        )
        self.assertEqual(
            self._holders({"pkg/consumer.py": consumer}).get("src/mozyo_bridge/pkg/consumer.py"),
            ["chan-a"],
        )

    def test_importing_the_MODULE_by_name_and_using_an_attribute_resolves(self):
        """``from pkg import owner`` then ``owner.RE`` — an ImportFrom naming a module, not a name."""
        holders = self._holders(
            {
                "pkg/__init__.py": "",
                "pkg/owner.py": self.OWNER,
                "consumer.py": (
                    "from mozyo_bridge.pkg import owner\n"
                    "def read(n): return owner._MARKER_RE.findall(n or '')\n"
                ),
            },
            owner=False,
        )
        self.assertEqual(holders.get("src/mozyo_bridge/consumer.py"), ["chan-a"])

    # -- generic capabilities stay tellable apart (j#92420 finding 2) -----------------

    def test_a_second_GENERIC_scanner_is_a_new_capability(self):
        """Folding every generic pattern to ``"*"`` in a set hid the addition entirely."""
        generic = r'\\[mozyo:(?P<channel>[a-z0-9_-]+):(?P<body>[^\\]]*)\\]'
        one = f'import re\nRE1 = re.compile(r"{generic}")\ndef a(n): return RE1.findall(n)\n'
        two = one + (
            'RE2 = re.compile(r"\\[mozyo:(?P<c2>[a-z]+):(?P<b2>[^\\]]*)\\]")\n'
            "def b(n): return RE2.findall(n)\n"
        )
        self.assertEqual(
            self._holders({"m.py": one}, owner=False)["src/mozyo_bridge/m.py"], ["*:RE1"]
        )
        self.assertEqual(
            self._holders({"m.py": two}, owner=False)["src/mozyo_bridge/m.py"],
            ["*:RE1", "*:RE2"],
        )

    def test_an_added_UNBOUND_generic_occurrence_also_changes_the_capability(self):
        """Multiplicity, so an occurrence with no name to distinguish it still counts."""
        one = "import re\ndef a(n): return re.findall(r\"\\[mozyo:(?P<c>[a-z]+):([^\\]]*)\\]\", n)\n"
        two = one + "def b(n): return re.findall(r\"\\[mozyo:(?P<d>[a-z]+):([^\\]]*)\\]\", n)\n"
        self.assertEqual(self._holders({"m.py": one}, owner=False)["src/mozyo_bridge/m.py"], ["*"])
        self.assertEqual(
            self._holders({"m.py": two}, owner=False)["src/mozyo_bridge/m.py"], ["*", "*"]
        )

    def test_a_direct_regex_call_with_no_binding_is_detected(self):
        """(d) the pattern never becomes a name at all."""
        module = (
            "import re\n"
            "def read(n): return re.findall(r\"\\[mozyo:chan-b:([^\\]]*)\\]\", n or '')\n"
        )
        self.assertIn("src/mozyo_bridge/pkg/direct.py", self._holders({"pkg/direct.py": module}))

    # -- the earlier evasions, still pinned ------------------------------------------

    def test_a_scanner_using_an_IMPORTED_pattern_is_detected(self):
        module = (
            "from mozyo_bridge.owner import _MARKER_RE\n"
            'def read(n): return [m.group("body") for m in _MARKER_RE.finditer(n or "")]\n'
        )
        self.assertEqual(
            self._holders({"pkg/imported.py": module}).get("src/mozyo_bridge/pkg/imported.py"),
            ["chan-a"],
        )

    def test_a_reused_basename_in_another_package_is_a_separate_identity(self):
        holders = self._holders({"pkg/owner.py": self.OWNER})
        self.assertEqual(
            set(holders), {"src/mozyo_bridge/owner.py", "src/mozyo_bridge/pkg/owner.py"}
        )

    # -- producers and other syntax, which possession catches for free ----------------

    def test_a_PRODUCER_that_never_scans_is_detected(self):
        """j#92374 finding 2's defect shape: a renderer can emit what its reader refuses."""
        module = 'def build(v): return f"[mozyo:chan-b:lane={v}]"\n'
        self.assertEqual(
            self._holders({"pkg/producer.py": module}).get("src/mozyo_bridge/pkg/producer.py"),
            ["chan-b"],
        )

    def test_a_token_returned_or_stored_in_a_container_is_detected(self):
        """The return / dict / list shapes j#92356 listed as known blind spots."""
        module = (
            'PATTERNS = {"c": r"\\[mozyo:chan-b:([^\\]]*)\\]"}\n'
            'def get(): return r"\\[mozyo:chan-c:([^\\]]*)\\]"\n'
        )
        self.assertEqual(
            self._holders({"pkg/container.py": module}).get("src/mozyo_bridge/pkg/container.py"),
            ["chan-b", "chan-c"],
        )

    # -- boundaries -------------------------------------------------------------------

    def test_prose_mentioning_the_token_is_not_a_holder(self):
        """The false POSITIVE the R25 text search produced; a docstring is not a capability."""
        module = (
            '"""Explains the [mozyo:...] grammar and why finditer is used elsewhere."""\n'
            '"[mozyo:workflow-event:gate=review_request] is a bare string statement too"\n'
            "VALUE = 1\n"
        )
        self.assertEqual(self._holders({"pkg/prose.py": module}), {"src/mozyo_bridge/owner.py": ["chan-a"]})

    def test_an_unused_import_of_a_pattern_is_not_a_holder(self):
        module = "from mozyo_bridge.owner import _MARKER_RE  # noqa: F401\nVALUE = 1\n"
        self.assertEqual(
            self._holders({"pkg/unused.py": module}), {"src/mozyo_bridge/owner.py": ["chan-a"]}
        )


class ReviewJ92374ProducerValidationTests(unittest.TestCase):
    """R26-F2: both dispatch renderers could emit markers their own parsers refuse.

    The rule they now share was already hardened in ``callback_recovery_key`` for the
    recovery-admission channel; it is promoted rather than rewritten, so there is one definition
    of "a value that round-trips".
    """

    WS, LANE, ISSUE, TGT = "ws", "r1", "14539", "tgt"

    class _Entry:
        def __init__(self, notes):
            self.journal_id, self.notes, self.issue_id = "7", notes, "14539"

    def _authorization(self, **overrides):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            build_dispatch_authorization_marker,
        )

        kwargs = dict(
            action_id="a1",
            source_gate="review_result",
            issue=self.ISSUE,
            workspace_id=self.WS,
            lane_id=self.LANE,
            target_assigned_name=self.TGT,
        )
        kwargs.update(overrides)
        return build_dispatch_authorization_marker(**kwargs)

    def _disposition(self, **overrides):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            render_dispatch_disposition_marker,
        )

        kwargs = dict(
            action_id="a1",
            dispatch_journal="7",
            workspace_id=self.WS,
            lane_id=self.LANE,
            target_assigned_name=self.TGT,
            terminal_journal="9",
        )
        kwargs.update(overrides)
        return render_dispatch_disposition_marker(**kwargs)

    BAD_VALUES = (
        ("separator", "r1:unexpected=1"),
        ("equals", "r1=x"),
        ("open bracket", "r1["),
        ("close bracket", "r1]"),
        ("blank", ""),
        ("whitespace", "r 1"),
    )

    def test_the_authorization_renderer_round_trips_its_own_output(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            parse_dispatch_authorizations,
        )

        parsed = parse_dispatch_authorizations([self._Entry(self._authorization())])
        self.assertEqual([(a.valid, a.lane_id) for a in parsed], [(True, self.LANE)])

    def test_the_disposition_renderer_round_trips_its_own_output(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            parse_dispatch_dispositions,
        )

        parsed = parse_dispatch_dispositions(self._Entry(self._disposition()))
        self.assertEqual([d.lane_id for d in parsed], [self.LANE])

    def test_the_authorization_renderer_refuses_a_value_it_could_not_read_back(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            MarkerValueError,
        )

        for label, value in self.BAD_VALUES:
            with self.subTest(label):
                with self.assertRaises(MarkerValueError):
                    self._authorization(lane_id=value)

    def test_the_disposition_renderer_refuses_a_value_it_could_not_read_back(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            MarkerValueError,
        )

        for label, value in self.BAD_VALUES:
            with self.subTest(label):
                with self.assertRaises(MarkerValueError):
                    self._disposition(lane_id=value)

    def test_every_caller_supplied_field_is_validated_not_just_one(self):
        """The R25 lesson: a rule applied to one field is not a rule about the record."""
        for field in ("action_id", "source_gate", "issue", "workspace_id", "target_assigned_name"):
            with self.subTest(f"authorization.{field}"):
                with self.assertRaises(Exception):
                    self._authorization(**{field: "x:y"})
        for field in ("action_id", "dispatch_journal", "workspace_id", "target_assigned_name",
                      "terminal_journal"):
            with self.subTest(f"disposition.{field}"):
                with self.assertRaises(Exception):
                    self._disposition(**{field: "x:y"})

    def test_the_shared_validator_is_the_one_the_recovery_channel_hardened(self):
        """One definition of "a value that round-trips", not a second weaker copy."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            callback_recovery_key,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            validate_marker_field_value,
        )

        self.assertIs(callback_recovery_key.validate_marker_field_value, validate_marker_field_value)
        with self.assertRaises(callback_recovery_key.RecoveryKeyError):
            callback_recovery_key._validate_value("lane_id", "r1:x")
if __name__ == "__main__":  # pragma: no cover
    unittest.main()
