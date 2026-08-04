"""Redmine #14755 — a superseded FAILURE round must converge to retired without becoming an approval.

#14577 is the reproduction, and every number here is from its real record. Its Review j#93648
concluded ``changes_requested``; both findings were independently verified and ACCEPTED (j#93653 /
j#93656); the acceptance the round failed to reach was obtained by the successor #14697, whose own
Review j#93727 concluded ``approved``; the lane was task_closed as a superseded failure (j#93757)
with its head already an ancestor of ``origin/main-next`` and zero commits of its own. The standard
``sublane retire`` refused anyway — three separate preflights, j#93759 / j#94006 / j#94319, each
``stale_review_generation`` with zero mutation — because the only durable authority that fence
reads is a REVIEW GENERATION and this lane's will never be approved.

The escapes correctly refused there are what this suite exists to keep refused:

* asserting ``--latest-generation-admissible`` about a round that concluded ``changes_requested``
  is a FALSE assert;
* borrowing #14697's approval for #14577 makes one issue's review answer another's;
* re-reading the failure as an approval is the "exemption を Review Gate approval または自己 review
  と表現しない" the central preset forbids.

So this pins BOTH directions:

* the positive — a valid, correlated terminal declaration converges to an admission with the round
  still read as the failure it was;
* the negatives, of which there are far more. An open issue, an unaccepted / unreceived finding, an
  unintegrated or dirty lane, an incomplete or unacknowledged successor, a foreign issue / lane /
  generation / integration branch, a re-opened round, an APPROVED round, and ordinary
  ``changes_requested`` development must each keep the ordinary fence fully armed with zero write.

Plus the two invariants the acceptance names in their own right: the #14695 waiver, the #14539
exemption and the ordinary generation fence are not weakened, and replaying an identical
observation is idempotent.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    retire_superseded_failure,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
    REASON_SUPERSEDED_ROUTE_UNREADABLE,
    REASON_SUPERSEDED_TARGET_UNRESOLVED,
    RetireEvidenceTarget,
    _resolve_latest_generation_admissible,
    _resolve_superseded_failure_admissible,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
    fold_issue_gate_facts,
    lane_signal_from_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.no_change_review_waiver import (  # noqa: E501
    NO_CHANGE_REVIEW_WAIVER_GATE,
    WRITER_AUTHORITY_RESOLVABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_exemption import (  # noqa: E501
    MARKER_GATE_CODEX_DIRECT_EDIT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (  # noqa: E501
    REASON_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
    GATE_CLOSE,
    classify_lane_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
    INTEGRATION_BLOCKED,
    INTEGRATION_STALE_REVIEW_GENERATION,
    RetirePreflight,
    SublaneIntegrationPolicy,
    decide_retire_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_legacy_authority import (  # noqa: E501
    AUTHORITY_SOURCE_LEGACY,
    AUTHORITY_SOURCE_MANIFEST,
    REASON_ATTESTATION_CONFLICTING,
    REASON_ATTESTATION_MISSING,
    REASON_ATTESTATION_UNAUTHORIZED,
    REASON_RULING_UNAUTHORIZED,
    REASON_MANIFEST_LEGACY_CONFLICT,
    render_legacy_review_finding_attestation,
    render_legacy_review_finding_ruling,
    resolve_review_finding_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_legacy_issuer import (  # noqa: E501
    resolve_legacy_ruling_issuers,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_manifest import (  # noqa: E501
    REASON_MANIFEST_PROSE_MISMATCH,
    ReviewFinding,
    render_review_result_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
    ACK_INVALID,
    FINDING_VERDICT_REASONS,
    SUCCESSOR_ACK_FIELD_ORDER,
    SUCCESSOR_ACK_GATE,
    VERDICT_COVERAGE_MISMATCH,
    VERDICT_FINDING_AUTHORITY_UNRESOLVED,
    VERDICT_NOT_ACCEPTED,
    VERDICT_NOT_RECORDED,
    VERDICT_PAIRING_UNREADABLE,
    VERDICT_TARGET_MISMATCH,
    VERDICT_UNRESOLVED,
    fold_finding_verdicts,
    fold_successor_acknowledgement,
    journal_ref,
    render_successor_acknowledgement_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501
    DECLARATION_INVALID,
    DECLARATION_NONE,
    DECLARATION_SUPERSEDED,
    REASON_CALLBACK_OWED,
    REASON_CLOSE_NOT_RECORDED,
    REASON_FINDINGS_NOT_ACCEPTED,
    REASON_INTEGRATION_BRANCH_MISMATCH,
    REASON_INTEGRATION_BRANCH_NOT_COMMITTED,
    REASON_INVALID,
    REASON_ISSUE_MISMATCH,
    REASON_LANE_HEAD_UNMEASURED,
    REASON_LANE_MISMATCH,
    REASON_LANE_NOT_INTEGRATED,
    REASON_MEASURED_BRANCH_MISMATCH,
    REASON_NOT_RECORDED,
    REASON_POST_DECLARATION_MUTATION,
    REASON_ROUND_DID_NOT_FAIL,
    REASON_ROUND_MISMATCH,
    REASON_SUCCESSOR_INCOMPLETE,
    REASON_SUCCESSOR_IS_SELF,
    REASON_SUCCESSOR_NOT_ACKNOWLEDGED,
    REASON_SUPERSEDED_BY_NEWER_ROUND,
    REASON_WORKTREE_NOT_CLEAN,
    SUPERSEDED_FAILURE_FIELD_ORDER,
    SUPERSEDED_FAILURE_GATE,
    SUPERSEDED_FAILURE_REFUSAL_REASONS,
    SuccessorEvidence,
    declaration_current,
    evaluate_superseded_failure_admissible,
    fold_superseded_failure,
    render_superseded_failure_marker,
)

# The reproduction's own identities, so a reader can line the fixtures up against the real record.
ISSUE = "14577"
SUCCESSOR = "14697"
REVIEW_JOURNAL = "93648"
VERDICT_JOURNAL = "93656"
SUCCESSOR_REVIEW_JOURNAL = "93727"
DECLARATION_JOURNAL = "94400"
REVIEW_REQUEST_JOURNAL = "93628"
#: The append-only migration chain #14971 requires for a review that predates the manifest
#: contract: an attestation, then a direct-owner ruling naming that exact attestation.
ATTESTATION_JOURNAL = "99201"
RULING_JOURNAL = "99202"
#: What #14577 j#93648 raised, spelled the way its verdict journal j#93656 answers them.
ROUND_FINDINGS = ("1", "2")
HEAD = "735a5f88e7fa66a46f3da9316586f19ebb50bec0"
WORKSPACE = "mozyo_bridge"
LANE = "issue_14577_r8_delivery_terminal_acceptance"
GENERATION = 3
INTEGRATION_BRANCH = "main-next"


def declaration_marker(**overrides) -> str:
    """The canonical declaration for the reproduction, with named field overrides."""
    kwargs = dict(
        issue=ISSUE,
        review_journal=REVIEW_JOURNAL,
        verdict_journal=VERDICT_JOURNAL,
        successor_issue=SUCCESSOR,
        successor_review_journal=SUCCESSOR_REVIEW_JOURNAL,
        integration_branch=INTEGRATION_BRANCH,
        workspace=WORKSPACE,
        lane=LANE,
        lane_generation=GENERATION,
        head=HEAD,
    )
    kwargs.update(overrides)
    return render_superseded_failure_marker(**kwargs)


#: The conclusion the review_result marker carries for each governed prose spelling. The two must
#: agree or the record states one thing in prose and another in the machine token.
_MARKER_CONCLUSION = {"要修正": "changes_requested", "承認": "approved"}


def review_note(
    *,
    conclusion: str = "要修正",
    authority: "str | None" = "manifest",
    findings: "tuple[str, ...]" = ROUND_FINDINGS,
) -> str:
    """The failed round's journal, in one of the three shapes this route has to handle.

    ``manifest`` is what #14971's canonical producer emits today: prose and sidecar rendered from
    ONE structured input, so the finding identities in the marker are the identities in the text.
    ``legacy`` is the reproduction's real pre-contract shape — #14577 j#93648 carries a
    ``review_result`` marker and spells its findings ``#### F1`` / ``#### F2`` in prose, with no
    sidecar and no way to add one (the journal is append-only). ``None`` is that same shape with
    nothing later attesting to it, which must stay refused.
    """
    marker_conclusion = _MARKER_CONCLUSION[conclusion]
    # An approved review carries no findings, and #14971's producer refuses to render one that
    # does. The fixture obeys the same contract rather than routing around it.
    findings = () if marker_conclusion == "approved" else findings
    if authority == "manifest":
        return render_review_result_note(
            issue=ISSUE,
            body=f"## Gate: review\n\n- 結論: {conclusion}",
            findings=tuple(
                ReviewFinding(identity=fid, summary=f"指摘 {fid}", details="")
                for fid in findings
            ),
            marker_fields={
                "conclusion": marker_conclusion,
                "target_head": HEAD,
                "review_request_journal": REVIEW_REQUEST_JOURNAL,
            },
        )
    prose = "\n\n".join(f"#### F{index} — 指摘" for index, _ in enumerate(findings, 1))
    return (
        f"## Gate: review\n\n- 結論: {conclusion}\n\n"
        f"[mozyo:workflow-event:gate=review_result:conclusion={marker_conclusion}"
        f":head={HEAD}:req={REVIEW_REQUEST_JOURNAL}]\n\n"
        f"### Findings\n\n{prose}\n"
    )


def attestation_note(*, findings: "tuple[str, ...]" = ROUND_FINDINGS, review=REVIEW_JOURNAL) -> str:
    """A later journal naming the historical round's finding set. Authority 0 on its own."""
    return "## Gate: review_finding_attestation\n\n" + render_legacy_review_finding_attestation(
        issue=ISSUE, review_journal=review, findings=findings
    )


def ruling_note(
    *,
    findings: "tuple[str, ...]" = ROUND_FINDINGS,
    attestation: str = ATTESTATION_JOURNAL,
    review: str = REVIEW_JOURNAL,
    heading: str = "## Gate: review_finding_legacy_ruling",
    supersedes=None,
) -> str:
    """The direct-owner ruling that selects one exact attestation, and nothing else.

    The heading is what makes the journal eligible at all: ``review_finding_legacy_ruling`` is not
    a gate-bearing kind, so no workflow-event marker for it can be rendered, and the port resolves
    the writer role from the governed heading exactly as the verdict gate is qualified.
    """
    kwargs = dict(
        issue=ISSUE,
        review_journal=review,
        attestation_journal=attestation,
        findings=findings,
    )
    if supersedes is not None:
        kwargs["supersedes_ruling_journal"] = supersedes
    return f"{heading}\n\n" + render_legacy_review_finding_ruling(**kwargs)


def acknowledgement_marker(**overrides) -> str:
    kwargs = dict(
        issue=SUCCESSOR,
        superseded_issue=ISSUE,
        superseded_review_journal=REVIEW_JOURNAL,
        review_journal=SUCCESSOR_REVIEW_JOURNAL,
    )
    kwargs.update(overrides)
    return render_successor_acknowledgement_marker(**kwargs)


def source_journals(
    *,
    marker: "str | None" = None,
    verdict: str = "accepted",
    verdict_target: str = REVIEW_JOURNAL,
    review_conclusion: str = "要修正",
    close: bool = True,
    authority: "str | None" = "manifest",
    review: "str | None" = None,
    legacy_records: "list[tuple[str, str]] | None" = None,
    verdict_findings: "tuple[str, ...]" = ROUND_FINDINGS,
    extra: "list[tuple[str, str]] | None" = None,
) -> "list[tuple[str, str]]":
    """The source issue's durable history, shaped like #14577's real one.

    ``authority`` selects which shape supplies the round's finding set: ``manifest`` (#14971's
    canonical producer), ``legacy`` (the pre-contract round plus its attestation / ruling chain),
    or ``None`` (a pre-contract round nothing later attests to). ``review`` overrides the round's
    note verbatim, ``legacy_records`` the migration journals. ``verdict_findings`` chooses which of
    the round's findings the verdict journal actually answers.
    """
    note = review_note(conclusion=review_conclusion, authority=authority) if review is None else review
    verdict_lines = "".join(
        f"- finding_{fid}: 指摘 {fid}\n  - verdict: {verdict}\n" for fid in verdict_findings
    )
    journals = [
        (
            REVIEW_REQUEST_JOURNAL,
            # The round the result answers, with the head it pinned. Both markers are required
            # for the glance grammar to read the result as CANONICAL rather than shadowing it to
            # `pending`, and the real #14577 pair carries exactly this correlation.
            "## Gate: review_request\n- commit_or_diff: `735a5f88`\n\n"
            f"[mozyo:workflow-event:gate=review_request:head={HEAD}]\n",
        ),
        (REVIEW_JOURNAL, note),
        (
            VERDICT_JOURNAL,
            "## Gate: review_finding_verdict\n"
            f"- 対象review_journal: j#{verdict_target}\n" + verdict_lines,
        ),
    ]
    if close:
        journals.append(
            ("93757", "## Gate: task_close — superseded failure round\n- close判断: terminal\n")
        )
    if marker is not None:
        journals.append(
            (DECLARATION_JOURNAL, f"## Gate: superseded_failure\n\n{marker}\n")
        )
    if legacy_records is None and authority == "legacy":
        legacy_records = [
            (ATTESTATION_JOURNAL, attestation_note()),
            (RULING_JOURNAL, ruling_note()),
        ]
    journals.extend(legacy_records or [])
    journals.extend(extra or [])
    return journals


def entries_of(journals, *, issue: str = ISSUE) -> "list[RedmineJournalEntry]":
    """The pair fixtures as ENTRIES — the shape #14971's authority reads issue identity from."""
    return [
        RedmineJournalEntry(issue_id=issue, journal_id=str(jid), notes=notes or "")
        for jid, notes in journals or ()
    ]


def finding_authority(journals, *, review_journal: str = REVIEW_JOURNAL, issue: str = ISSUE):
    """Resolve the round's finding set exactly as the application route does.

    Deliberately the real composition — #14971's resolver over the same entries, with the
    ``ruling_issuers`` port filled by the same resolver ``retire_admissibility`` wires in. A
    fixture that hand-built the facts would pin this route against a set no record can produce.
    """
    entries = entries_of(journals, issue=issue)
    return resolve_review_finding_authority(
        entries,
        review_journal=review_journal,
        ruling_issuers=resolve_legacy_ruling_issuers(entries),
    )


def verdicts(journals, *, review_journal: str = REVIEW_JOURNAL, issue: str = ISSUE):
    """The verdict fold over the authority the same journals resolve to."""
    return fold_finding_verdicts(
        journals,
        review_journal=review_journal,
        authority=finding_authority(journals, review_journal=review_journal, issue=issue),
    )


def successor_journals(
    *,
    ack: "str | None" = None,
    conclusion: str = "承認",
    close: bool = True,
) -> "list[tuple[str, str]]":
    journals = [
        ("93715", "## Gate: review_request\n"),
        (SUCCESSOR_REVIEW_JOURNAL, f"## Gate: review\n- 結論: {conclusion}\n"),
    ]
    if close:
        journals.append(("93744", "## Gate: task_close\n"))
    if ack is not None:
        journals.append(("93745", f"## Gate: superseded_failure_successor\n\n{ack}\n"))
    return journals


def admit(
    *,
    source: "list[tuple[str, str]] | None" = None,
    successor: "list[tuple[str, str]] | None" = None,
    target_issue: str = ISSUE,
    integration_branch: str = INTEGRATION_BRANCH,
    committed_branch: str = INTEGRATION_BRANCH,
    workspace: str = WORKSPACE,
    lane: str = LANE,
    generation: int = GENERATION,
    measured_branch: str = LANE,
    live_head: str = HEAD,
    commits_ahead: "int | None" = 0,
    worktree_clean: bool = True,
    callbacks_drained: bool = True,
):
    """Fold both records with the SHARED grammar and evaluate — the whole route, minus IO.

    Deliberately NOT a hand-built fact tuple: the folds are what the application route calls, so a
    fixture that satisfies this satisfies the real inputs. Building the facts by hand would test
    the evaluator against values no record can produce.
    """
    src = source_journals(marker=declaration_marker()) if source is None else source
    suc = successor_journals(ack=acknowledgement_marker()) if successor is None else successor
    gate_facts = fold_issue_gate_facts(src)
    rounds = list(gate_facts.review_round_journals or ()) if gate_facts else []
    successor_facts = fold_issue_gate_facts(suc)
    successor_rounds = (
        list(successor_facts.review_round_journals or ()) if successor_facts else []
    )
    declaration = fold_superseded_failure(src)
    return evaluate_superseded_failure_admissible(
        declaration,
        currently_current=declaration_current(declaration, rounds),
        verdicts=verdicts(src, review_journal=declaration.review_journal),
        acknowledgement=fold_successor_acknowledgement(suc),
        successor=SuccessorEvidence(
            review_journal=str(max(successor_rounds)) if successor_rounds else "",
            review_gate=successor_facts.review_round_gate if successor_facts else "",
            review_conclusion=(
                successor_facts.review_round_conclusion if successor_facts else ""
            ),
            close_recorded=bool(
                successor_facts is not None and successor_facts.latest_gate == GATE_CLOSE
            ),
        ),
        latest_round_journal=str(max(rounds)) if rounds else "",
        latest_round_gate=gate_facts.review_round_gate if gate_facts else "",
        latest_round_conclusion=gate_facts.review_round_conclusion if gate_facts else "",
        close_recorded=bool(gate_facts is not None and gate_facts.latest_gate == GATE_CLOSE),
        target_issue=target_issue,
        integration_branch=integration_branch,
        committed_integration_branch=committed_branch,
        expected_workspace=workspace,
        expected_lane=lane,
        expected_lane_generation=generation,
        measured_branch=measured_branch,
        live_head=live_head,
        live_commits_ahead=commits_ahead,
        worktree_clean=worktree_clean,
        callbacks_drained=callbacks_drained,
    )


class TheReproductionConverges(unittest.TestCase):
    """#14577's shape, once declared and correlated, reaches the terminal."""

    def test_the_positive_control_actually_admits(self):
        # The antecedent guard. Every negative below is a mutation of THIS fixture, so if the
        # fixture did not admit, each of them would pass vacuously (#14695 R12: a母集合 that never
        # generates the antecedent makes "whenever X" empty).
        outcome = admit()
        self.assertTrue(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_OK)

    def test_the_round_is_still_read_as_the_failure_it_was(self):
        # The whole point: the fold that admits also reports ``changes_requested``. Nothing here
        # rewrites the conclusion, and a record whose round APPROVED takes the ordinary fence.
        facts = fold_issue_gate_facts(source_journals(marker=declaration_marker()))
        self.assertEqual(facts.review_round_conclusion, "changes_requested")
        self.assertTrue(admit().admissible)

    def test_replaying_an_identical_observation_is_idempotent(self):
        first = admit()
        second = admit()
        third = admit()
        self.assertEqual((first.admissible, first.reason), (second.admissible, second.reason))
        self.assertEqual((second.admissible, second.reason), (third.admissible, third.reason))

    def test_replaying_a_refusal_is_idempotent_too(self):
        # Idempotence must hold on the refusing side as well, or a retry could differ from the
        # preflight that produced the operator's diagnosis.
        refusals = [admit(source=source_journals(marker=None)) for _ in range(3)]
        self.assertEqual({(r.admissible, r.reason) for r in refusals}, {(False, REASON_NOT_RECORDED)})


class BothCanonicalAuthorityRoutesReachTheTerminal(unittest.TestCase):
    """Redmine #14755 review j#99065, and the acceptance item R2 left unmet.

    R2's route-local enumeration had to be written INTO the review round's own journal. #14577
    j#93648 has no such marker, this workspace treats durable journals as append-only (a correction
    is always a new journal, never a rewrite of an old one), and so R2 left the reproduction
    permanently refused — the issue's main purpose unmet, not merely an unperformed live
    acceptance. #14971 supplies both halves, and these pin that BOTH reach the terminal.
    """

    def test_a_manifest_produced_round_admits_and_names_its_authority(self):
        journals = source_journals(marker=declaration_marker(), authority="manifest")
        facts = verdicts(journals)
        self.assertTrue(facts.accepted)
        self.assertEqual(facts.authority_source, AUTHORITY_SOURCE_MANIFEST)
        # In-journal: the manifest IS the review round's own record, emitted from the same input
        # as the prose by the same producer, so there is no second journal to point at.
        self.assertEqual(facts.authority_journal, REVIEW_JOURNAL)
        self.assertTrue(admit(source=journals).admissible)

    def test_the_manifest_identities_are_the_ones_the_prose_shows(self):
        # What makes the manifest route different in kind from R2's enumeration: the identities
        # are not separately writable. The producer renders prose and sidecar from one input, and
        # the reader refuses the journal outright if they diverge.
        note = review_note(authority="manifest")
        self.assertIn("### finding_1 — 指摘 1", note)
        self.assertIn("### finding_2 — 指摘 2", note)
        self.assertEqual(
            finding_authority(source_journals(marker=declaration_marker())).findings,
            ROUND_FINDINGS,
        )
        drifted = note.replace("### finding_2 — 指摘 2", "### finding_3 — 指摘 3")
        facts = verdicts(source_journals(marker=declaration_marker(), review=drifted))
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        self.assertEqual(facts.authority_reason, REASON_MANIFEST_PROSE_MISMATCH)

    def test_the_pre_contract_reproduction_reaches_the_terminal_without_a_journal_rewrite(self):
        # #14577's real shape: a `review_result` marker, `#### F1` / `#### F2` prose, no sidecar.
        # The round's journal is byte-identical to the shape with no migration at all — every new
        # record is a LATER journal — and the route still converges.
        journals = source_journals(marker=declaration_marker(), authority="legacy")
        unmigrated = source_journals(marker=declaration_marker(), authority=None)
        self.assertEqual(
            dict(journals)[REVIEW_JOURNAL], dict(unmigrated)[REVIEW_JOURNAL]
        )
        facts = verdicts(journals)
        self.assertTrue(facts.accepted)
        self.assertEqual(facts.authority_source, AUTHORITY_SOURCE_LEGACY)
        # The RULING is the authority, not the attestation that merely stated the set.
        self.assertEqual(facts.authority_journal, RULING_JOURNAL)
        outcome = admit(source=journals)
        self.assertTrue(outcome.admissible, outcome.reason)
        self.assertEqual(outcome.reason, REASON_OK)

    def test_both_routes_replay_identically(self):
        for label, authority in (("manifest", "manifest"), ("legacy", "legacy")):
            with self.subTest(label):
                journals = source_journals(marker=declaration_marker(), authority=authority)
                replays = [admit(source=journals) for _ in range(3)]
                self.assertEqual(
                    {(r.admissible, r.reason) for r in replays}, {(True, REASON_OK)}
                )


class OrdinaryDevelopmentIsRefused(unittest.TestCase):
    """The acceptance's zero-write refusals, each with its own true cause."""

    def test_ordinary_changes_requested_development_never_enters_the_route(self):
        # The negative CONTROL: #14577's record exactly as it stands today — a failed round, all
        # findings accepted, closed — with no terminal declaration. It refuses, and the reason
        # names the absent declaration rather than blaming the review.
        outcome = admit(source=source_journals(marker=None))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_NOT_RECORDED)

    def test_an_open_issue_is_refused(self):
        outcome = admit(source=source_journals(marker=declaration_marker(), close=False))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_CLOSE_NOT_RECORDED)

    def test_an_owed_callback_is_refused(self):
        outcome = admit(callbacks_drained=False)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_CALLBACK_OWED)

    def test_an_approved_round_belongs_to_the_ordinary_fence(self):
        outcome = admit(
            source=source_journals(marker=declaration_marker(), review_conclusion="承認")
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_ROUND_DID_NOT_FAIL)

    def test_an_unanswered_review_request_is_a_review_still_owed(self):
        outcome = admit(
            source=source_journals(
                marker=declaration_marker(),
                extra=[("94401", "## Gate: review_request\n")],
            )
        )
        self.assertFalse(outcome.admissible)
        # The request is newer than the declaration, so supersession names the true cause.
        self.assertEqual(outcome.reason, REASON_SUPERSEDED_BY_NEWER_ROUND)

    def test_a_round_recorded_in_the_declaration_s_own_journal_supersedes_it(self):
        # The TIE. #14695 review j#94260 measured the cost of getting this backwards: one journal
        # that both declares a terminal and opens a round claims two contradictory things in one
        # breath, and nothing orders them. This is the case ``declaration_current`` catches and
        # the newest-round identity check does NOT — the named round IS the newest one here.
        marker = declaration_marker(review_journal="94400")
        journals = [
            ("93628", "## Gate: review_request\n"),
            (
                "94400",
                "## Gate: review + superseded_failure\n- 結論: 要修正\n\n" + marker + "\n",
            ),
            ("94402", "## Gate: task_close\n"),
        ]
        declaration = fold_superseded_failure(journals)
        facts = fold_issue_gate_facts(journals)
        self.assertEqual(declaration.state, DECLARATION_SUPERSEDED)
        self.assertIn(94400, facts.review_round_journals)
        self.assertFalse(declaration_current(declaration, list(facts.review_round_journals)))
        outcome = admit(source=journals)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUPERSEDED_BY_NEWER_ROUND)

    def test_a_declaration_naming_an_older_round_than_the_newest_is_refused(self):
        # A second failed round after the declaration would be caught by supersession; a round
        # recorded BETWEEN the named one and the declaration is what this catches.
        journals = source_journals(
            marker=None,
            extra=[
                (
                    DECLARATION_JOURNAL,
                    "## Gate: superseded_failure\n\n" + declaration_marker() + "\n",
                ),
            ],
        )
        # The second failed round sits BETWEEN the named one and the Close, so the Close is still
        # the latest gate and supersession still passes — this isolates the identity check.
        journals.insert(3, ("93700", "## Gate: review\n- 結論: 要修正\n"))
        facts = fold_issue_gate_facts(journals)
        self.assertEqual(facts.latest_gate, GATE_CLOSE)
        self.assertEqual(max(facts.review_round_journals), 93700)
        outcome = admit(source=journals)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_ROUND_MISMATCH)


class FindingsMustHaveBeenReceived(unittest.TestCase):
    """未受領 finding — the verdict half of the correlation."""

    def test_a_disputed_finding_is_refused(self):
        outcome = admit(
            source=source_journals(marker=declaration_marker(), verdict="disputed")
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_a_blocked_verdict_is_refused(self):
        outcome = admit(
            source=source_journals(marker=declaration_marker(), verdict="blocked")
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_an_unfilled_verdict_template_line_has_decided_nothing(self):
        outcome = admit(
            source=source_journals(
                marker=declaration_marker(),
                verdict="accepted | disputed | blocked (hearsay のみ根拠 → 記録化待ち)",
            )
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_verdicts_recorded_against_another_round_do_not_count(self):
        journals = source_journals(marker=declaration_marker(), verdict_target="90000")
        self.assertEqual(
            verdicts(journals).reason,
            VERDICT_TARGET_MISMATCH,
        )
        outcome = admit(source=journals)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_no_verdict_gate_at_all_is_refused(self):
        journals = [
            entry
            for entry in source_journals(marker=declaration_marker())
            if entry[0] != VERDICT_JOURNAL
        ]
        self.assertEqual(
            verdicts(journals).reason,
            VERDICT_NOT_RECORDED,
        )
        outcome = admit(source=journals)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_a_newer_verdict_gate_shadows_an_older_clean_one(self):
        # supersede-by-EXISTING. A later verdict gate disputing the same round must not be skipped
        # so the older clean one keeps corroborating.
        journals = source_journals(
            marker=None,
            extra=[
                (
                    "93900",
                    # Covers the round's whole finding set, so what refuses is the DISPUTE and
                    # not the coverage — the shadowing is what this pins.
                    "## Gate: review_finding_verdict\n"
                    f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                    "- finding_1: 再燃\n  - verdict: disputed\n"
                    "- finding_2: 再燃\n  - verdict: accepted\n",
                ),
                (
                    DECLARATION_JOURNAL,
                    "## Gate: superseded_failure\n\n" + declaration_marker() + "\n",
                ),
            ],
        )
        self.assertEqual(
            verdicts(journals).reason,
            VERDICT_NOT_ACCEPTED,
        )
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_the_declaration_must_name_the_deciding_verdict_gate(self):
        # A declaration pointing at a clean OLD verdict while the record's latest verdict gate is
        # about something else has corroborated nothing.
        journals = source_journals(
            marker=None,
            extra=[
                (
                    "93900",
                    "## Gate: review_finding_verdict\n"
                    f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                    "- finding_1: 再確認\n  - verdict: accepted\n",
                ),
                (
                    DECLARATION_JOURNAL,
                    "## Gate: superseded_failure\n\n" + declaration_marker() + "\n",
                ),
            ],
        )
        outcome = admit(source=journals)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_two_different_targets_in_one_verdict_gate_decide_neither(self):
        journals = source_journals(
            marker=declaration_marker(),
            extra=[
                (
                    "93901",
                    "## Gate: review_finding_verdict\n"
                    f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                    "- 対象review_journal: j#90000\n"
                    "- verdict: accepted\n",
                )
            ],
        )
        self.assertEqual(
            verdicts(journals).reason,
            VERDICT_UNRESOLVED,
        )
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_a_marker_only_verdict_gate_can_still_shadow_a_clean_heading_one(self):
        # Latest-wins is by DECLARATION, so a newer gate declared only by a marker must be visible
        # here — otherwise the stale clean verdict stays "latest" and keeps corroborating.
        journals = source_journals(
            marker=None,
            extra=[
                ("93910", "[mozyo:workflow-event:gate=review_finding_verdict:issue=14577]\n"),
                (
                    DECLARATION_JOURNAL,
                    "## Gate: superseded_failure\n\n" + declaration_marker() + "\n",
                ),
            ],
        )
        facts = verdicts(journals)
        self.assertEqual(facts.journal, "93910")
        self.assertEqual(facts.reason, VERDICT_UNRESOLVED)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_a_quoted_verdict_field_is_not_a_declaration(self):
        # The #14695 j#93704 F2 asymmetry: qualification quote-aware, value read raw. A verdict
        # that exists only inside a fenced block is a contract example, not this issue's verdict.
        journals = source_journals(
            marker=declaration_marker(),
            verdict="disputed",
            extra=[
                (
                    "93902",
                    "## Gate: review_finding_verdict\n"
                    "```\n"
                    f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                    "- verdict: accepted\n"
                    "```\n",
                )
            ],
        )
        self.assertEqual(
            verdicts(journals).reason,
            VERDICT_UNRESOLVED,
        )
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)


class TheVerdictsMustCoverEveryFindingTheRoundRaised(unittest.TestCase):
    """Redmine #14755 review j#99057 finding_1, closed against #14971's canonical authority.

    "Every verdict PRESENT is accepted" is satisfied by a verdict journal that answers one of the
    round's two findings, and that opened the terminal — the acceptance refuses 未受領 finding
    independently, and live-zero bounds repository change without bounding this. So the verdicts
    are checked for COVERAGE against the round's finding set.

    Where that set comes from is what review j#99065 sent back. R2 had the round enumerate itself
    in its own journal, which put the enumeration and the findings in ONE record written by ONE
    actor at ONE time — no second record could contradict it, and the renderer had no production
    caller, so a hand-written under-count admitted exactly as before, one level up. The set now
    comes from #14971: a manifest the canonical review producer emits atomically with the review
    prose, or — for a round that predates that contract and whose journal cannot be rewritten — an
    append-only attestation that only counts once a distinct direct-owner ruling names it.
    """

    def test_a_verdict_covering_only_some_findings_is_refused(self):
        # The exact reproduction: the round raised finding_1 and finding_2; the verdict journal
        # answers finding_1 only, and every verdict it carries IS accepted.
        journals = source_journals(marker=declaration_marker(), verdict_findings=("1",))
        self.assertEqual(
            verdicts(journals).reason,
            VERDICT_COVERAGE_MISMATCH,
        )
        outcome = admit(source=journals)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_the_complete_verdict_still_admits(self):
        # The control the refusal above is only meaningful against.
        self.assertTrue(admit().admissible)

    def test_a_verdict_answering_a_finding_the_round_never_raised_is_refused(self):
        journals = source_journals(
            marker=declaration_marker(), verdict_findings=("1", "2", "3")
        )
        self.assertEqual(
            verdicts(journals).reason,
            VERDICT_COVERAGE_MISMATCH,
        )
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_a_round_with_no_finding_authority_supplies_no_set_to_be_complete_against(self):
        # The pre-contract shape with nothing later attesting to it. Refusing is the fail-closed
        # direction: coverage cannot be checked against a set no record states, and inferring one
        # from the round's `#### F1` prose would be a SECOND definition of finding identity — the
        # normalization #14971 j#99084 explicitly declined.
        journals = source_journals(marker=declaration_marker(), authority=None)
        facts = verdicts(journals)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        # The canonical module's own token is CARRIED, not collapsed: "nothing attested" and
        # "the ruling is unauthorized" are different records to go and look at.
        self.assertEqual(facts.authority_reason, REASON_ATTESTATION_MISSING)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_an_attestation_nobody_ruled_on_is_not_authority(self):
        # #14971 j#99084: an attestation alone has authority 0. This is what keeps the migration
        # path from being a third self-declaration — the record naming the set and the record
        # authorizing it are distinct journals, and the latter must name the former exactly.
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[(ATTESTATION_JOURNAL, attestation_note())],
        )
        facts = verdicts(journals)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        self.assertEqual(facts.authority_reason, REASON_ATTESTATION_UNAUTHORIZED)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_a_ruling_journal_that_declares_a_second_gate_resolves_no_writer(self):
        # The port's one real constraint, stated for what it is. It does NOT authenticate the
        # coordinator — nothing in this workspace can (ruling #14219 j#86718). What it refuses is a
        # ruling smuggled into a journal written to be something else, because a note claiming two
        # authority contracts at once proves neither.
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[
                (ATTESTATION_JOURNAL, attestation_note()),
                (
                    RULING_JOURNAL,
                    ruling_note(
                        heading="## Gate: review_finding_legacy_ruling + integration_disposition"
                    ),
                ),
            ],
        )
        facts = verdicts(journals)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        # The RULING is what became unauthorized — a distinct diagnosis from "nothing ruled on
        # this attestation at all", and the one that points at the journal to fix.
        self.assertEqual(facts.authority_reason, REASON_RULING_UNAUTHORIZED)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_the_verdict_journal_cannot_be_its_own_finding_authority(self):
        # The R2 defect in its most direct form (review j#99065): the record that answers the
        # findings must not also be the record that says what the findings WERE. A verdict journal
        # claiming the ruling gate declares two authority contracts, so it resolves neither — and
        # the set it would have authorized itself against is never established.
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[(ATTESTATION_JOURNAL, attestation_note())],
            extra=[
                (
                    RULING_JOURNAL,
                    "## Gate: review_finding_verdict + review_finding_legacy_ruling\n"
                    f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                    "- finding_1: a\n  - verdict: accepted\n"
                    "- finding_2: b\n  - verdict: accepted\n\n"
                    + ruling_note(heading="").strip(),
                )
            ],
        )
        self.assertEqual(resolve_legacy_ruling_issuers(entries_of(journals)), {})
        facts = verdicts(journals)
        self.assertFalse(facts.accepted)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        self.assertEqual(facts.authority_reason, REASON_RULING_UNAUTHORIZED)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_an_attestation_cannot_rule_on_itself_in_one_journal(self):
        # A ruling must be a LATER journal than the attestation it selects, so a single journal
        # carrying both is not a two-record chain wearing one record's clothes.
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[
                (
                    RULING_JOURNAL,
                    attestation_note() + "\n\n" + ruling_note(attestation=RULING_JOURNAL),
                )
            ],
        )
        facts = verdicts(journals)
        self.assertFalse(facts.accepted)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)

    def test_every_declared_verdict_reason_is_reached_by_a_real_fixture(self):
        # The母集合 guard the route's terminal reasons already carry: a reason nobody can reach is
        # a refusal that does not exist, and one nobody declared is a token no operator can look up.
        reached = {
            verdicts(journals).reason
            for journals in (
                source_journals(marker=declaration_marker()),
                source_journals(marker=declaration_marker(), verdict="disputed"),
                source_journals(marker=declaration_marker(), verdict_target="90000"),
                source_journals(marker=declaration_marker(), verdict_findings=("1",)),
                source_journals(marker=declaration_marker(), authority=None),
                [
                    entry
                    for entry in source_journals(marker=declaration_marker())
                    if entry[0] != VERDICT_JOURNAL
                ],
                source_journals(
                    marker=declaration_marker(),
                    extra=[
                        (
                            "93901",
                            "## Gate: review_finding_verdict\n"
                            f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                            "- 対象review_journal: j#90000\n"
                            "- verdict: accepted\n",
                        )
                    ],
                ),
                source_journals(
                    marker=declaration_marker(),
                    extra=[
                        (
                            "93901",
                            "## Gate: review_finding_verdict\n"
                            f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                            "- verdict: accepted\n- finding_1: a\n  - verdict: accepted\n",
                        )
                    ],
                ),
            )
        }
        self.assertEqual(reached, FINDING_VERDICT_REASONS)

    def test_the_port_reads_the_qualified_heading_spelling_the_preset_invites(self):
        # `## Gate: <token> — <qualifier>` is a canonical spelling, not a second gate. Counting
        # the qualified form as its own token would make "declares exactly this gate" false for
        # every heading a governed author actually writes (#14695 j#94110 finding 2, one level up).
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[
                (ATTESTATION_JOURNAL, attestation_note()),
                (
                    RULING_JOURNAL,
                    ruling_note(
                        heading=f"## Gate: review_finding_legacy_ruling — #{ISSUE} j#{REVIEW_JOURNAL}"
                    ),
                ),
            ],
        )
        self.assertEqual(
            set(resolve_legacy_ruling_issuers(entries_of(journals))), {RULING_JOURNAL}
        )
        self.assertTrue(verdicts(journals).accepted)

    def test_an_unrenderable_marker_anywhere_in_a_ruling_journal_resolves_no_writer(self):
        # Fail-closed, not skip-and-continue: a note carrying a marker the canonical producer
        # could not render resolves NOTHING, rather than having that marker passed over so a
        # clean sibling decides for it.
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[
                (ATTESTATION_JOURNAL, attestation_note()),
                (
                    RULING_JOURNAL,
                    ruling_note() + "\n\n[mozyo:workflow-event: gate=close]\n",
                ),
            ],
        )
        self.assertEqual(resolve_legacy_ruling_issuers(entries_of(journals)), {})
        facts = verdicts(journals)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        self.assertEqual(facts.authority_reason, REASON_RULING_UNAUTHORIZED)

    def test_a_ruling_naming_a_different_set_than_its_attestation_is_fail_closed(self):
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[
                (ATTESTATION_JOURNAL, attestation_note()),
                (RULING_JOURNAL, ruling_note(findings=("1",))),
            ],
        )
        facts = verdicts(journals)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        self.assertEqual(facts.authority_reason, REASON_ATTESTATION_CONFLICTING)

    def test_an_under_declaring_legacy_chain_still_fails_the_coverage_check(self):
        # A ruled attestation naming ONE of the two findings the verdict journal answers. The
        # authority resolves — the owner ruling is what makes it resolve — and the coverage check
        # is what refuses, so an owner-ruled under-count cannot admit merely by being ruled.
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[
                (ATTESTATION_JOURNAL, attestation_note(findings=("1",))),
                (RULING_JOURNAL, ruling_note(findings=("1",))),
            ],
        )
        facts = verdicts(journals)
        self.assertEqual(facts.authority_source, AUTHORITY_SOURCE_LEGACY)
        self.assertEqual(facts.reason, VERDICT_COVERAGE_MISMATCH)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_a_legacy_declaration_cannot_downgrade_a_journal_that_carries_a_manifest(self):
        # #14971's conflict rule, reached through this route: a round that DID emit a manifest is
        # not open to a later attestation claiming a smaller set for it.
        journals = source_journals(
            marker=declaration_marker(),
            authority="manifest",
            legacy_records=[
                (ATTESTATION_JOURNAL, attestation_note(findings=("1",))),
                (RULING_JOURNAL, ruling_note(findings=("1",))),
            ],
        )
        facts = verdicts(journals)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        self.assertEqual(facts.authority_reason, REASON_MANIFEST_LEGACY_CONFLICT)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_an_authority_about_another_round_does_not_supply_this_ones_set(self):
        journals = source_journals(
            marker=declaration_marker(),
            authority="legacy",
            legacy_records=[
                (ATTESTATION_JOURNAL, attestation_note(review=REVIEW_REQUEST_JOURNAL)),
                (RULING_JOURNAL, ruling_note(review=REVIEW_REQUEST_JOURNAL)),
            ],
        )
        facts = verdicts(journals)
        self.assertEqual(facts.reason, VERDICT_FINDING_AUTHORITY_UNRESOLVED)
        self.assertEqual(admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED)

    def test_findings_and_verdicts_must_pair_one_to_one(self):
        # A finding opened and never answered, a verdict answering nothing named, and the same
        # finding twice: each is unreadable AS this shape rather than a smaller readable record.
        bodies = {
            "finding with no verdict": "- finding_1: a\n- finding_2: b\n  - verdict: accepted\n",
            "verdict with no finding": "- verdict: accepted\n- finding_1: a\n  - verdict: accepted\n",
            "the same finding twice": (
                "- finding_1: a\n  - verdict: accepted\n"
                "- finding_1: a again\n  - verdict: accepted\n"
            ),
        }
        for label, lines in bodies.items():
            with self.subTest(label):
                journals = source_journals(marker=declaration_marker(), extra=[
                    (
                        "93901",
                        "## Gate: review_finding_verdict\n"
                        f"- 対象review_journal: j#{REVIEW_JOURNAL}\n" + lines,
                    )
                ])
                self.assertEqual(
                    verdicts(journals).reason,
                    VERDICT_PAIRING_UNREADABLE,
                )
                self.assertEqual(
                    admit(source=journals).reason, REASON_FINDINGS_NOT_ACCEPTED
                )

    def test_the_heading_spelling_the_real_verdict_uses_reads_the_same(self):
        # #14577 j#93656 writes its findings as `### finding_1 — …` headings, not list items.
        journals = source_journals(marker=declaration_marker(), extra=[
            (
                "93901",
                "## Gate: review_finding_verdict\n"
                f"- 対象review_journal: j#{REVIEW_JOURNAL}\n"
                "### finding_1 — scope 2\n- **verdict: accepted**\n"
                "### finding_2 — 記述\n- **verdict: accepted**\n",
            )
        ])
        self.assertTrue(
            verdicts(journals).accepted
        )


class TheSuccessorMustAcknowledgeAndHaveSucceeded(unittest.TestCase):
    """successor evidence — the half the source issue cannot write inside its own record."""

    def test_a_successor_that_never_acknowledged_is_refused(self):
        outcome = admit(successor=successor_journals(ack=None))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_an_acknowledgement_naming_another_predecessor_is_refused(self):
        outcome = admit(
            successor=successor_journals(ack=acknowledgement_marker(superseded_issue="13999"))
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_an_acknowledgement_naming_another_failed_round_is_refused(self):
        outcome = admit(
            successor=successor_journals(
                ack=acknowledgement_marker(superseded_review_journal="90000")
            )
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_a_successor_whose_review_is_not_approved_is_incomplete(self):
        outcome = admit(
            successor=successor_journals(ack=acknowledgement_marker(), conclusion="要修正")
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_INCOMPLETE)

    def test_a_successor_that_is_not_closed_is_incomplete(self):
        outcome = admit(
            successor=successor_journals(ack=acknowledgement_marker(), close=False)
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_INCOMPLETE)

    def test_a_successor_with_a_newer_unanswered_round_is_incomplete(self):
        successor = successor_journals(ack=acknowledgement_marker())
        successor.append(("93950", "## Gate: review_request\n"))
        outcome = admit(successor=successor)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_INCOMPLETE)

    def test_an_issue_cannot_be_its_own_successor(self):
        with self.assertRaises(ValueError):
            declaration_marker(successor_issue=ISSUE)
        with self.assertRaises(ValueError):
            acknowledgement_marker(superseded_issue=SUCCESSOR)

    def test_every_single_acknowledgement_field_mutation_breaks_the_correlation(self):
        # The same DERIVED oracle as the declaration's, over the acknowledgement's own contract:
        # a field added later is covered without editing this test.
        base = acknowledgement_marker()
        replacements = {
            "gate": SUPERSEDED_FAILURE_GATE,
            "version": "2",
            "decision": "declines",
            "issue": "13999",
            "superseded_issue": "13998",
            "superseded_review_journal": "90000",
            "review_journal": "90001",
        }
        self.assertEqual(set(replacements), set(SUCCESSOR_ACK_FIELD_ORDER))
        for field in SUCCESSOR_ACK_FIELD_ORDER:
            with self.subTest(field=field):
                mutated = "[" + ":".join(
                    f"{field}={replacements[field]}"
                    if part.partition("=")[0] == field and part.partition("=")[1]
                    else part
                    for part in base[1:-1].split(":")
                ) + "]"
                outcome = admit(
                    successor=successor_journals(
                        ack=f"## Gate: superseded_failure_successor\n\n{mutated}\n"
                    )
                )
                self.assertFalse(outcome.admissible)
                self.assertIn(outcome.reason, SUPERSEDED_FAILURE_REFUSAL_REASONS)

    def test_a_hand_written_self_superseding_declaration_is_refused_on_read(self):
        # The renderer refuses to write one, but a record can arrive from anywhere, so the reason
        # must be REACHABLE rather than pinned by a renderer test alone. The declaration parser
        # deliberately does not fold this into ``invalid``: "the successor is this issue" is a
        # correlation failure with its own remedy, not an unreadable marker.
        forged = declaration_marker().replace(
            f"successor_issue={SUCCESSOR}", f"successor_issue={ISSUE}"
        )
        facts = fold_superseded_failure([(DECLARATION_JOURNAL, forged)])
        self.assertEqual(facts.state, DECLARATION_SUPERSEDED)
        outcome = admit(source=source_journals(marker=forged))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_IS_SELF)

    def test_a_self_referential_acknowledgement_read_back_is_invalid(self):
        # The renderer refuses to write one; the parser must also refuse to read one, because a
        # record can arrive from anywhere. A marker no producer can render is not evidence.
        forged = (
            "[mozyo:workflow-event:gate=superseded_failure_successor:version=1:"
            f"decision=supersedes:issue={SUCCESSOR}:superseded_issue={SUCCESSOR}:"
            f"superseded_review_journal={REVIEW_JOURNAL}:review_journal={SUCCESSOR_REVIEW_JOURNAL}]"
        )
        facts = fold_successor_acknowledgement([("93745", forged)])
        self.assertEqual(facts.state, ACK_INVALID)


class TheLiveHalfBoundsTheRoute(unittest.TestCase):
    """未統合 / dirty — the conjunct that makes admitting cost nothing."""

    def test_a_lane_carrying_commits_over_the_integration_branch_is_refused(self):
        outcome = admit(commits_ahead=1)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_LANE_NOT_INTEGRATED)

    def test_measuring_some_other_checkout_at_the_same_head_is_refused(self):
        # The reproduction's own shape is the dangerous one: when the lane head IS the integration
        # head, ANY checkout sitting there satisfies every other live conjunct. The measurement
        # must be about the declared lane, not about whatever --branch / --worktree was pointed at.
        outcome = admit(measured_branch=INTEGRATION_BRANCH)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_MEASURED_BRANCH_MISMATCH)
        self.assertEqual(admit(measured_branch="").reason, REASON_MEASURED_BRANCH_MISMATCH)

    def test_a_dirty_worktree_is_refused(self):
        outcome = admit(worktree_clean=False)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_WORKTREE_NOT_CLEAN)

    def test_an_unmeasured_repository_never_testifies_that_nothing_changed(self):
        self.assertEqual(admit(live_head="").reason, REASON_LANE_HEAD_UNMEASURED)
        self.assertEqual(admit(commits_ahead=None).reason, REASON_LANE_HEAD_UNMEASURED)

    def test_a_head_that_moved_after_the_declaration_is_refused(self):
        outcome = admit(live_head="0" * 40)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_POST_DECLARATION_MUTATION)

    def test_a_boolean_is_not_a_commit_count(self):
        self.assertEqual(admit(commits_ahead=True).reason, REASON_LANE_HEAD_UNMEASURED)


class ForeignEvidenceNeverUnlocksThisFence(unittest.TestCase):
    """foreign lane / generation / issue / integration branch."""

    def test_a_declaration_about_another_issue_is_refused(self):
        outcome = admit(target_issue="14999")
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_ISSUE_MISMATCH)

    def test_a_declaration_about_another_lane_is_refused(self):
        outcome = admit(lane="issue_99999_other_lane")
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_LANE_MISMATCH)

    def test_a_superseded_generation_of_this_lane_is_refused(self):
        outcome = admit(generation=GENERATION + 1)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_LANE_MISMATCH)

    def test_an_unresolved_lane_expectation_fences_nothing_and_refuses(self):
        for kwargs in ({"workspace": ""}, {"lane": ""}, {"generation": 0}):
            with self.subTest(**kwargs):
                self.assertEqual(admit(**kwargs).reason, REASON_LANE_MISMATCH)

    def test_a_declaration_made_about_another_integration_branch_is_refused(self):
        # The declaration is stale relative to the branch this retire is measuring against.
        outcome = admit(
            source=source_journals(marker=declaration_marker(integration_branch="main"))
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_INTEGRATION_BRANCH_MISMATCH)

    def test_pointing_the_retire_at_a_caller_chosen_branch_makes_live_zero_vacuous(self):
        # The degenerate case the committed-config expectation exists for: --integration-branch
        # pointed at the lane's OWN branch makes "carries 0 commits over the integration branch"
        # trivially true, so a caller free to choose it could satisfy the conjunct that bounds
        # this whole route. Both the declaration and the retire must name the COMMITTED branch.
        outcome = admit(
            integration_branch=LANE,
            source=source_journals(marker=declaration_marker(integration_branch=LANE)),
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_INTEGRATION_BRANCH_NOT_COMMITTED)

    def test_a_config_that_declares_no_integration_branch_supplies_no_expectation(self):
        outcome = admit(committed_branch="")
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_INTEGRATION_BRANCH_NOT_COMMITTED)

    def test_the_committed_branch_is_read_from_the_repository_config(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            committed_integration_branch,
        )

        # WHICH branch a repository declares is operational and moves (#14761 renamed this
        # repository's canonical integration branch), so pinning the literal would only pin the
        # day's config. What the route needs is that the reader returns whatever the COMMITTED
        # config declares — asserted against a value no fallback or guess would produce — and
        # that this repository's own config is readable through that same path.
        with tempfile.TemporaryDirectory() as tmp:
            declared = "branch-declared-only-by-the-committed-config"
            config = Path(tmp) / ".mozyo-bridge"
            config.mkdir(parents=True)
            (config / "config.yaml").write_text(
                f"version: 2\nsublane_integration:\n  integration_branch: {declared}\n",
                encoding="utf-8",
            )
            self.assertEqual(committed_integration_branch(Path(tmp)), declared)
        self.assertNotEqual(committed_integration_branch(ROOT), "")
        # An unreadable / absent config yields no expectation rather than a guess.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(committed_integration_branch(Path(tmp)), "")


class TheDeclarationGrammarIsClosed(unittest.TestCase):
    """A marker the canonical producer could not render is refused whole."""

    def test_every_single_field_mutation_loses_the_admission(self):
        # A DERIVED oracle, not a list of examples: the field set comes from the contract itself,
        # so a field added later is covered without editing this test.
        base = declaration_marker()
        self.assertTrue(admit().admissible)
        for field in SUPERSEDED_FAILURE_FIELD_ORDER:
            with self.subTest(field=field):
                mutated = _mutate_field(base, field)
                self.assertNotEqual(mutated, base)
                outcome = admit(source=source_journals(marker=mutated))
                self.assertFalse(outcome.admissible)
                self.assertIn(outcome.reason, SUPERSEDED_FAILURE_REFUSAL_REASONS)

    def test_every_single_field_removal_makes_the_marker_unrenderable(self):
        base = declaration_marker()
        for field in SUPERSEDED_FAILURE_FIELD_ORDER:
            with self.subTest(field=field):
                dropped = ":".join(
                    part
                    for part in base[1:-1].split(":")
                    if not part.startswith(f"{field}=")
                )
                facts = fold_superseded_failure([(DECLARATION_JOURNAL, f"[{dropped}]")])
                self.assertIn(facts.state, {DECLARATION_INVALID, DECLARATION_NONE})

    def test_a_permuted_field_order_is_not_producer_output(self):
        parts = declaration_marker()[1:-1].split(":")
        # Swap two payload fields, leaving the channel prefix intact.
        head = parts[:2]
        body = parts[2:]
        body[1], body[2] = body[2], body[1]
        facts = fold_superseded_failure(
            [(DECLARATION_JOURNAL, "[" + ":".join(head + body) + "]")]
        )
        self.assertEqual(facts.state, DECLARATION_INVALID)

    def test_an_invalid_declaration_refuses_as_invalid_and_not_as_absent(self):
        # ``DECLARATION_INVALID`` must not fold into "no declaration": the two are different
        # operational problems, and reading a malformed authority as absent is how a record that
        # SHOULD have been diagnosed quietly takes the ordinary path instead.
        outcome = admit(
            source=source_journals(
                marker="## Gate: superseded_failure\n- successor: #14697\n"
            )
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_INVALID)

    def test_a_heading_alone_declares_but_cannot_mint(self):
        facts = fold_superseded_failure(
            [(DECLARATION_JOURNAL, "## Gate: superseded_failure\n- successor: #14697\n")]
        )
        self.assertEqual(facts.state, DECLARATION_INVALID)
        self.assertTrue(facts.recorded)
        self.assertFalse(facts.in_force)

    def test_a_quoted_marker_is_not_a_marker(self):
        facts = fold_superseded_failure(
            [(DECLARATION_JOURNAL, "本文で引用する: `" + declaration_marker() + "`\n")]
        )
        self.assertEqual(facts.state, DECLARATION_NONE)

    def test_two_declarations_in_one_journal_say_which_is_authoritative_of_neither(self):
        both = declaration_marker() + "\n" + declaration_marker(successor_issue="13999")
        facts = fold_superseded_failure([(DECLARATION_JOURNAL, both)])
        self.assertEqual(facts.state, DECLARATION_INVALID)

    def test_a_newer_malformed_declaration_shadows_an_older_valid_one(self):
        journals = [
            (DECLARATION_JOURNAL, declaration_marker()),
            ("94500", "## Gate: superseded_failure\n- 記録漏れ\n"),
        ]
        self.assertEqual(fold_superseded_failure(journals).state, DECLARATION_INVALID)

    def test_the_renderer_refuses_what_the_parser_refuses(self):
        for kwargs in (
            {"issue": ""},
            {"successor_issue": ""},
            {"integration_branch": ""},
            {"review_journal": "not-a-journal"},
            {"verdict_journal": ""},
            {"successor_review_journal": "j#"},
            {"lane_generation": 0},
            {"lane_generation": True},
            {"head": ""},
            {"head": "735a5f8"},
            {"workspace": ""},
            {"lane": ""},
            {"issue": "145:77"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    declaration_marker(**kwargs)

    def test_the_rendered_marker_is_what_the_reader_accepts(self):
        facts = fold_superseded_failure([(DECLARATION_JOURNAL, declaration_marker())])
        self.assertEqual(facts.state, DECLARATION_SUPERSEDED)
        self.assertEqual(facts.issue, ISSUE)
        self.assertEqual(facts.review_journal, REVIEW_JOURNAL)
        self.assertEqual(facts.successor_issue, SUCCESSOR)
        self.assertEqual(facts.head, HEAD)

    def test_journal_references_compare_equal_across_both_spellings(self):
        self.assertEqual(journal_ref("j#93648"), journal_ref("93648"))
        self.assertEqual(journal_ref("#93648"), "93648")
        for junk in ("", "j#", "abc", "93a", "j#93648x", None):
            self.assertEqual(journal_ref(junk), "")


def _mutate_field(marker: str, field: str) -> str:
    """One field of a rendered marker changed to a different, still-plausible value."""
    replacements = {
        "gate": "no_change_review_waiver",
        "version": "2",
        "decision": "declined",
        "issue": "14999",
        "review_journal": "90000",
        "verdict_journal": "90001",
        "successor_issue": "13999",
        "successor_review_journal": "90002",
        "integration_branch": "main",
        "workspace": "other_workspace",
        "lane": "issue_99999_other_lane",
        "lane_generation": str(GENERATION + 7),
        "head": "1" * 40,
    }
    parts = marker[1:-1].split(":")
    out = []
    for part in parts:
        key, sep, _ = part.partition("=")
        if sep and key == field:
            out.append(f"{field}={replacements[field]}")
        else:
            out.append(part)
    return "[" + ":".join(out) + "]"


class EveryDeclaredRefusalIsReachable(unittest.TestCase):
    """A reason nobody can reach is not a fence, it is a comment."""

    def test_every_refusal_reason_this_route_declares_is_reached_by_a_real_fixture(self):
        # DERIVED from the contract's own token set, not from a hand-kept list: a reason added
        # later fails here until a fixture reaches it. #14695 R12 measured the opposite failure —
        # an invariant test whose fixture never generated the antecedent, so it was empty and
        # green. This asserts the fixtures actually drive every branch.
        reached = {outcome.reason for outcome in _EVERY_REFUSAL_FIXTURE()}
        self.assertEqual(SUPERSEDED_FAILURE_REFUSAL_REASONS - reached, set())
        self.assertEqual(reached - SUPERSEDED_FAILURE_REFUSAL_REASONS, set())


def _EVERY_REFUSAL_FIXTURE():
    """One admission outcome per declared refusal, each isolating its own conjunct."""
    self_successor = declaration_marker().replace(
        f"successor_issue={SUCCESSOR}", f"successor_issue={ISSUE}"
    )
    tie_journals = [
        ("93628", "## Gate: review_request\n"),
        (
            "94400",
            "## Gate: review + superseded_failure\n- 結論: 要修正\n\n"
            + declaration_marker(review_journal="94400")
            + "\n",
        ),
        ("94402", "## Gate: task_close\n"),
    ]
    stale_round = source_journals(
        marker=None,
        extra=[
            (
                DECLARATION_JOURNAL,
                "## Gate: superseded_failure\n\n" + declaration_marker() + "\n",
            )
        ],
    )
    stale_round.insert(3, ("93700", "## Gate: review\n- 結論: 要修正\n"))
    return (
        admit(source=source_journals(marker=None)),
        admit(source=source_journals(marker="## Gate: superseded_failure\n- 記録漏れ\n")),
        admit(source=tie_journals),
        admit(target_issue="14999"),
        admit(lane="issue_99999_other_lane"),
        admit(
            integration_branch=LANE,
            source=source_journals(marker=declaration_marker(integration_branch=LANE)),
        ),
        admit(source=source_journals(marker=declaration_marker(integration_branch="main"))),
        admit(source=source_journals(marker=declaration_marker(), close=False)),
        admit(callbacks_drained=False),
        admit(source=stale_round),
        admit(source=source_journals(marker=declaration_marker(), review_conclusion="承認")),
        admit(source=source_journals(marker=declaration_marker(), verdict="disputed")),
        admit(source=source_journals(marker=self_successor)),
        admit(successor=successor_journals(ack=None)),
        admit(successor=successor_journals(ack=acknowledgement_marker(), close=False)),
        admit(measured_branch=INTEGRATION_BRANCH),
        admit(worktree_clean=False),
        admit(live_head=""),
        admit(live_head="0" * 40),
        admit(commits_ahead=1),
    )


class TheOtherRoutesAreNotWeakened(unittest.TestCase):
    """#14695 waiver / #14539 exemption / ordinary generation fence stay exactly as they were."""

    def test_the_waiver_route_still_admits_nothing(self):
        self.assertFalse(WRITER_AUTHORITY_RESOLVABLE)

    def test_the_three_authority_gate_tokens_stay_distinct(self):
        self.assertEqual(
            len({SUPERSEDED_FAILURE_GATE, NO_CHANGE_REVIEW_WAIVER_GATE, MARKER_GATE_CODEX_DIRECT_EDIT}), 3
        )
        self.assertNotEqual(SUPERSEDED_FAILURE_GATE, SUCCESSOR_ACK_GATE)

    def test_a_superseded_failure_declaration_does_not_change_the_glance_projection(self):
        # The measured control (#14695 R5-F1's method). A closed lane whose latest round is
        # ``changes_requested`` projects ``implementing`` today; this route adds NO glance
        # projection, so the declaration must not move it. Changing that projection would sweep in
        # every closed changes_requested lane, which is a different issue's call.
        without = source_journals(marker=None)
        with_declaration = source_journals(marker=declaration_marker())
        states = []
        for journals in (without, with_declaration):
            facts = fold_issue_gate_facts(journals)
            states.append(
                classify_lane_state(lane_signal_from_gate_facts(ISSUE, facts, issue_open=False))
            )
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[0], "implementing")

    def test_the_new_gate_carries_no_issuer_contract_rather_than_a_manufactured_one(self):
        # A gate registered in the issuer policy needs a ruling that NAMES it. Manufacturing an
        # anchor from a record that decided no writer contract is the #14661 j#92715 defect
        # (``is_anchored`` passes while pointing somewhere that could not have decided it), so
        # this route makes no issuer claim at all and says so.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            ISSUER_UNKNOWN,
            contract_ruling_pointer,
            contract_writer_role,
        )

        for gate in (SUPERSEDED_FAILURE_GATE, SUCCESSOR_ACK_GATE):
            with self.subTest(gate=gate):
                self.assertEqual(contract_writer_role(gate), ISSUER_UNKNOWN)
                self.assertEqual(contract_ruling_pointer(gate), "")

    def test_neither_new_gate_is_a_lifecycle_gate(self):
        # Like ``codex_direct_edit`` and ``no_change_review_waiver``: an issue-wide authority
        # declaration, never a step in the lane's lifecycle. A journal carrying only one of these
        # headings contributes no gate, so it cannot become the ``latest_gate``.
        journals = source_journals(marker=declaration_marker())
        facts = fold_issue_gate_facts(journals)
        self.assertEqual(facts.latest_gate, GATE_CLOSE)
        self.assertEqual(facts.latest_gate_journal, "93757")

    def test_the_round_ids_export_matches_the_rounds_the_fold_recognized(self):
        facts = fold_issue_gate_facts(source_journals(marker=declaration_marker()))
        self.assertEqual(facts.review_round_journals, (93628, 93648))
        self.assertEqual(str(max(facts.review_round_journals)), REVIEW_JOURNAL)


class TheRouteIsWiredIntoTheFence(unittest.TestCase):
    """The application route and the CLI surface — the effect terminal, not only the pure fold."""

    @staticmethod
    def _args(**overrides) -> argparse.Namespace:
        base = dict(
            issue=ISSUE,
            branch=LANE,
            integration_branch=INTEGRATION_BRANCH,
            worktree="",
            callbacks_drained=True,
            latest_generation_admissible=False,
            review_generation_json=None,
            review_exemption_json=None,
            no_change_review_waiver=False,
            superseded_failure_terminal=True,
            lane_label=LANE,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_the_flag_is_registered_on_the_retire_parser(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_sublane_retire import (  # noqa: E501
            register_sublane_retire,
        )

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="sublane_command")
        register_sublane_retire(sub, add_repo_option=lambda p: None, add_lifecycle_json=lambda p: None)
        args = parser.parse_args(
            [
                "retire",
                "--issue",
                ISSUE,
                "--lane-label",
                LANE,
                "--superseded-failure-terminal",
            ]
        )
        self.assertTrue(args.superseded_failure_terminal)

    def test_an_unresolved_retire_target_refuses_with_its_own_reason(self):
        outcome = _resolve_superseded_failure_admissible(
            self._args(), target=None, repo_root=Path(".")
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUPERSEDED_TARGET_UNRESOLVED)

    def test_an_unreadable_live_record_refuses_rather_than_reading_silence_as_absence(self):
        original = retire_superseded_failure._read_live_issue_entries
        retire_superseded_failure._read_live_issue_entries = lambda issue: []
        try:
            outcome = _resolve_superseded_failure_admissible(
                self._args(),
                target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                repo_root=Path("."),
            )
        finally:
            retire_superseded_failure._read_live_issue_entries = original
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUPERSEDED_ROUTE_UNREADABLE)

    def test_the_route_admits_end_to_end_against_a_real_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            marker = declaration_marker(head=head)
            records = {
                ISSUE: source_journals(marker=marker),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            outcome = self._resolve_with(records, worktree=tmp)
            self.assertTrue(outcome.admissible, outcome.reason)
            self.assertEqual(outcome.reason, REASON_OK)

    def test_a_dirty_real_checkout_refuses_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            (Path(tmp) / "scratch.txt").write_text("uncommitted", encoding="utf-8")
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            outcome = self._resolve_with(records, worktree=tmp)
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_WORKTREE_NOT_CLEAN)

    def test_a_real_lane_carrying_a_commit_refuses_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_lane_checkout(Path(tmp))
            head = _commit(Path(tmp), "extra.txt", "work")
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            outcome = self._resolve_with(records, worktree=tmp)
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_LANE_NOT_INTEGRATED)

    def test_a_checkout_that_cannot_resolve_the_committed_branch_yields_no_measurement(self):
        # Measured on the real reproduction: the #14577 lane checkout carries `origin/main-next`
        # but no local `main-next`, so the ahead-count is UNMEASURED and the route refuses. That
        # is an operational precondition (fetch the branch into the lane checkout), not an
        # authority gap — and it must refuse rather than fall back to a remote-qualified spelling,
        # because which remote qualifies is a resolution rule this issue has no ruling for.
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            _git(Path(tmp), "branch", "-D", INTEGRATION_BRANCH)
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            outcome = self._resolve_with(records, worktree=tmp)
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_LANE_HEAD_UNMEASURED)

    def test_the_opt_in_is_required_for_the_route_to_run_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            outcome = self._resolve_with(
                records, worktree=tmp, superseded_failure_terminal=False
            )
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, "")

    def test_without_any_measured_route_the_operator_assertion_still_decides(self):
        # The pre-existing contract, unchanged: no measured input supplied -> the durable-record
        # assertion is consulted. Adding a fourth route must not change this.
        args = self._args(superseded_failure_terminal=False, latest_generation_admissible=True)
        self.assertTrue(_resolve_latest_generation_admissible(args).admissible)

    def test_a_supplied_measured_route_never_falls_back_to_the_hand_assert(self):
        args = self._args(latest_generation_admissible=True)
        original = retire_superseded_failure._read_live_issue_entries
        retire_superseded_failure._read_live_issue_entries = lambda issue: []
        try:
            outcome = _resolve_latest_generation_admissible(
                args,
                target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                repo_root=Path("."),
            )
        finally:
            retire_superseded_failure._read_live_issue_entries = original
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUPERSEDED_ROUTE_UNREADABLE)

    def test_the_typed_refusal_reaches_the_retire_decision(self):
        # The effect terminal (#14695 R5-F2: reading a pure return value is not measuring the
        # effect). A route's own reason must reach the operator instead of being collapsed into
        # ``stale_review_generation``, which names a review generation this lane cannot have.
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False),
            RetirePreflight(
                is_git_workspace=True,
                latest_generation_admissible=False,
                latest_generation_blocked_reason=REASON_SUCCESSOR_INCOMPLETE,
            ),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(REASON_SUCCESSOR_INCOMPLETE, decision.blocked_reasons)
        self.assertNotIn(INTEGRATION_STALE_REVIEW_GENERATION, decision.blocked_reasons)
        self.assertEqual(decision.primary_reason, REASON_SUCCESSOR_INCOMPLETE)

    def test_an_admitted_route_clears_the_generation_blocker_entirely(self):
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False),
            RetirePreflight(is_git_workspace=True, latest_generation_admissible=True),
        )
        self.assertTrue(decision.may_retire)

    def _resolve_with(self, records, *, worktree: str, **overrides):
        # The route reads ENTRIES now, because #14971's authority cross-checks each record's own
        # issue identity. Stubbing the entry reader keeps the fixtures one hop from the real shape.
        original = retire_superseded_failure._read_live_issue_entries
        retire_superseded_failure._read_live_issue_entries = lambda issue: entries_of(
            records.get(str(issue), []), issue=str(issue)
        )
        try:
            return _resolve_superseded_failure_admissible(
                self._args(worktree=worktree, **overrides),
                target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                repo_root=Path(worktree),
            )
        finally:
            retire_superseded_failure._read_live_issue_entries = original


def _git(path: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *argv], text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _commit(path: Path, name: str, body: str) -> str:
    (path / name).write_text(body, encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-m", f"add {name}")
    return _git(path, "rev-parse", "HEAD")


def _make_lane_checkout(path: Path) -> str:
    """A real repository whose lane branch is exactly the integration branch's head.

    The live half is measured against a REAL checkout rather than a stub, because what it fences
    is a property of git — a lane that carries nothing over the integration branch — and a stub
    would only re-state the expectation.
    """
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "test")
    # The COMMITTED integration-branch expectation. Written into the fixture repo rather than
    # stubbed, because what the route reads is the repo-local config loader itself.
    config = path / ".mozyo-bridge"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"version: 2\nsublane_integration:\n  integration_branch: {INTEGRATION_BRANCH}\n",
        encoding="utf-8",
    )
    _git(path, "add", ".mozyo-bridge/config.yaml")
    _git(path, "commit", "-m", "config")
    _commit(path, "base.txt", "base")
    _git(path, "branch", "-M", INTEGRATION_BRANCH)
    _git(path, "checkout", "--quiet", "-b", LANE)
    return _git(path, "rev-parse", "HEAD")


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
