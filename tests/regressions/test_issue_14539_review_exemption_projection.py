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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
