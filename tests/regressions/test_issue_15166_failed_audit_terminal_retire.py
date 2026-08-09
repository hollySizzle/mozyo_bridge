"""Redmine #15166 — a lane whose failure was recorded WITHOUT a Review Gate must still converge.

#15164 is the reproduction, and every number here is from its real record. It is a no-change
verification lane: the worker's trace landed in ``## Gate: implementation_done`` j#101783, the
coordinator's round-1 verdict landed in ``## Independent audit — round 1`` j#101792 which states in
as many words that no formal ``## Gate: review`` was created because the ``review_request`` was
missing, the acceptance it did not reach was obtained by the successor #15165 whose OWN
``## Gate: review`` j#101810 concluded ``approved``, both issues are task_closed and closed, and the
lane never committed. The standard ``sublane retire`` refuses permanently with
``stale_review_generation`` (j#101825, zero mutation).

Neither existing terminal reaches it, and this suite pins WHY rather than asserting it:

* the ordinary review-generation fence reads a review generation, and folding #15164's real record
  yields ``review_round_journals=()``;
* the #14755 ``superseded_failure`` terminal REQUIRES the newest round to be a ``## Gate: review``
  concluding ``changes_requested``, so with zero rounds it refuses forever;
* the #14695 ``no_change_review_waiver`` refuses structurally for every input.

The escapes that must stay refused are the ones #15164 j#101825 named itself: a false
``--latest-generation-admissible`` assert about a lane that has no review generation, and borrowing
#15165's approval for #15164.

So this pins BOTH directions:

* the positive — a valid, correlated terminal declaration converges to an admission, and the
  reproduction's own record shape converges once the two append-only journals are added;
* the negatives, of which there are far more. A record that DOES carry a review round, an open
  issue, a re-opened lane, an absent or gate-shaped audit record, a change-bearing record, an
  incomplete or unacknowledged successor, a foreign issue / lane / generation / integration branch,
  an unintegrated or dirty lane must each keep the ordinary fence fully armed with zero write.

Plus the invariants the acceptance names in their own right: the other four routes are not
weakened, no approval is minted or borrowed, the glance projection is unchanged, and replaying an
identical observation is idempotent.
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
    retire_superseded_audit_failure,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
    REASON_AUDIT_ROUTE_UNREADABLE,
    REASON_AUDIT_TARGET_UNRESOLVED,
    RetireEvidenceTarget,
    _resolve_latest_generation_admissible,
    _resolve_superseded_audit_failure_admissible,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_audit_failure_terminal import (  # noqa: E501
    ACK_ACKNOWLEDGED,
    ACK_INVALID,
    ACK_NONE,
    DECLARATION_INVALID,
    DECLARATION_NONE,
    DECLARATION_SUPERSEDED,
    REASON_AUDIT_JOURNAL_IS_A_GATE,
    REASON_AUDIT_JOURNAL_NOT_EARLIER,
    REASON_AUDIT_JOURNAL_NOT_FOUND,
    REASON_GATE_AFTER_DECLARATION,
    REASON_INTEGRATION_BRANCH_MISMATCH,
    REASON_INVALID,
    REASON_ISSUE_MISMATCH,
    REASON_LANE_MISMATCH,
    REASON_NOT_RECORDED,
    REASON_RECORD_DECLARES_CHANGE,
    REASON_REVIEW_ROUND_RECORDED,
    REASON_SUCCESSOR_IS_SELF,
    REASON_SUCCESSOR_NOT_ACKNOWLEDGED,
    SUCCESSOR_ACK_FIELD_ORDER,
    SUCCESSOR_ACK_GATE,
    SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER,
    SUPERSEDED_AUDIT_FAILURE_GATE,
    SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS,
    evaluate_superseded_audit_failure_admissible,
    fold_audit_supersession_acknowledgement,
    fold_superseded_audit_failure,
    render_audit_supersession_acknowledgement_marker,
    render_superseded_audit_failure_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_superseded_audit_failure import (  # noqa: E501
    _measure_audit_record,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501
    REASON_CALLBACK_OWED,
    REASON_CLOSE_NOT_RECORDED,
    REASON_INTEGRATION_BRANCH_NOT_COMMITTED,
    REASON_LANE_HEAD_UNMEASURED,
    REASON_LANE_NOT_INTEGRATED,
    REASON_MEASURED_BRANCH_MISMATCH,
    REASON_POST_DECLARATION_MUTATION,
    REASON_ROUND_MISMATCH,
    REASON_SUCCESSOR_INCOMPLETE,
    REASON_WORKTREE_NOT_CLEAN,
    SUPERSEDED_FAILURE_GATE,
    SuccessorEvidence,
)

# The reproduction's own identities, so a reader can line the fixtures up against the real record.
ISSUE = "15164"
SUCCESSOR = "15165"
START_JOURNAL = "101774"
IMPLEMENTATION_DONE_JOURNAL = "101783"
AUDIT_JOURNAL = "101792"
CLOSE_JOURNAL = "101822"
SUCCESSOR_REVIEW_REQUEST_JOURNAL = "101808"
SUCCESSOR_REVIEW_JOURNAL = "101810"
SUCCESSOR_CLOSE_JOURNAL = "101811"
#: The two append-only journals the terminal disposition adds. Both are NEW records written after
#: the Close — nothing in #15164's or #15165's existing history is rewritten, which the issue's
#: non-goals require in as many words.
DECLARATION_JOURNAL = "101900"
ACK_JOURNAL = "101901"
HEAD = "83a65e6dc5e9b6037020cd565e26e4af830d9b2a"
WORKSPACE = "mozyo_bridge"
LANE = "issue_15164_fresh_session_resume_verification"
GENERATION = 1
INTEGRATION_BRANCH = "main"


def declaration_marker(**overrides) -> str:
    """The canonical declaration for the reproduction, with named field overrides."""
    kwargs = dict(
        issue=ISSUE,
        audit_journal=AUDIT_JOURNAL,
        successor_issue=SUCCESSOR,
        successor_review_journal=SUCCESSOR_REVIEW_JOURNAL,
        integration_branch=INTEGRATION_BRANCH,
        workspace=WORKSPACE,
        lane=LANE,
        lane_generation=GENERATION,
        head=HEAD,
    )
    kwargs.update(overrides)
    return render_superseded_audit_failure_marker(**kwargs)


def acknowledgement_marker(**overrides) -> str:
    """The successor's canonical acknowledgement, with named field overrides."""
    kwargs = dict(
        issue=SUCCESSOR,
        superseded_issue=ISSUE,
        superseded_audit_journal=AUDIT_JOURNAL,
        review_journal=SUCCESSOR_REVIEW_JOURNAL,
    )
    kwargs.update(overrides)
    return render_audit_supersession_acknowledgement_marker(**kwargs)


#: The audit journal as #15164 actually wrote it. Its heading is NOT a governed gate heading, and
#: the record says so about itself — which is the whole shape this route exists for.
AUDIT_NOTE = (
    "## Independent audit — round 1\n"
    "- audit_target: implementation_done journal #101783\n"
    "- 結論: **受け入れ未達。fresh-session再検証が必要**\n"
    "- formal_review_gate: review_request journalが無いため本journalを`## Gate: review`として"
    "記録しない\n"
    "- next_owner: coordinator / implementation gateway\n"
)


def source_journals(
    *,
    marker: "str | None" = None,
    close: bool = True,
    audit: "str | None" = AUDIT_NOTE,
    implementation_commit: str = "",
    extra: "list[tuple[str, str]] | None" = None,
) -> "list[tuple[str, str]]":
    """The source issue's durable history, shaped like #15164's real one.

    ``implementation_commit`` makes the implementation_done journal change-bearing, which is how a
    fixture reaches the zero-change conjunct. The real record declares none — measured:
    ``fold_zero_change_record`` over #15164's live history returns ``proven=True``.
    """
    done = "## Gate: implementation_done — fresh-session 通常再開の実行trace\n"
    if implementation_commit:
        done += f"- commit_hash: `{implementation_commit}`\n"
    done += "- `git status --porcelain`: 空 (repository file 変更なし / commit なし)\n"
    journals = [
        (START_JOURNAL, "## Gate: start\n- work_unit: leaf_issue\n"),
        (IMPLEMENTATION_DONE_JOURNAL, done),
    ]
    if audit is not None:
        journals.append((AUDIT_JOURNAL, audit))
    if close:
        journals.append(
            (
                CLOSE_JOURNAL,
                "## Gate: task_close\n- commit_hash: なし（repository変更なし）\n",
            )
        )
    if marker is not None:
        journals.append(
            (DECLARATION_JOURNAL, f"## Gate: superseded_audit_failure\n\n{marker}\n")
        )
    journals.extend(extra or [])
    return journals


def successor_journals(
    *,
    ack: "str | None" = None,
    conclusion: str = "承認",
    close: bool = True,
    extra: "list[tuple[str, str]] | None" = None,
) -> "list[tuple[str, str]]":
    """The successor issue's durable history, shaped like #15165's real one."""
    journals = [
        (SUCCESSOR_REVIEW_REQUEST_JOURNAL, "## Gate: review_request\n"),
        (SUCCESSOR_REVIEW_JOURNAL, f"## Gate: review\n- 結論: {conclusion}\n"),
    ]
    if close:
        journals.append((SUCCESSOR_CLOSE_JOURNAL, "## Gate: task_close\n"))
    if ack is not None:
        journals.append(
            (ACK_JOURNAL, f"## Gate: superseded_audit_failure_successor\n\n{ack}\n")
        )
    journals.extend(extra or [])
    return journals


def entries_of(journals, *, issue: str = ISSUE) -> "list[RedmineJournalEntry]":
    return [
        RedmineJournalEntry(issue_id=issue, journal_id=str(jid), notes=notes or "")
        for jid, notes in journals or ()
    ]


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

    Deliberately NOT a hand-built fact tuple: the folds are exactly what the application route
    calls, so a fixture that satisfies this satisfies the real inputs. Building the facts by hand
    would test the evaluator against values no record can produce.
    """
    src = source_journals(marker=declaration_marker()) if source is None else source
    suc = successor_journals(ack=acknowledgement_marker()) if successor is None else successor
    gate_facts = fold_issue_gate_facts(src)
    successor_facts = fold_issue_gate_facts(suc)
    successor_rounds = (
        list(successor_facts.review_round_journals or ()) if successor_facts else []
    )
    declaration = fold_superseded_audit_failure(src)
    return evaluate_superseded_audit_failure_admissible(
        declaration,
        audit=_measure_audit_record(src, declaration.audit_journal),
        acknowledgement=fold_audit_supersession_acknowledgement(suc),
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
        review_round_journals=(
            tuple(gate_facts.review_round_journals or ()) if gate_facts else ()
        ),
        latest_gate_journal=gate_facts.latest_gate_journal if gate_facts else "",
        close_recorded=bool(gate_facts is not None and gate_facts.latest_gate == GATE_CLOSE),
        zero_change_proven=bool(gate_facts is not None and gate_facts.zero_change.proven),
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
    """#15164's shape, once declared and correlated, reaches the terminal."""

    def test_the_positive_control_actually_admits(self):
        outcome = admit()
        self.assertTrue(outcome.admissible, outcome.reason)
        self.assertEqual(outcome.reason, REASON_OK)

    def test_the_record_still_carries_no_approval_of_its_own(self):
        # The point of the whole route: admitting must not have minted, borrowed or implied an
        # approval for THIS issue. After a successful admission the source record still has zero
        # review rounds and no review conclusion.
        facts = fold_issue_gate_facts(source_journals(marker=declaration_marker()))
        self.assertEqual(facts.review_round_journals, ())
        self.assertEqual(facts.review_round_gate, "")
        self.assertEqual(facts.review_round_conclusion, "")

    def test_the_audit_record_is_read_as_the_non_gate_it_is(self):
        evidence = _measure_audit_record(
            source_journals(marker=declaration_marker()), AUDIT_JOURNAL
        )
        self.assertTrue(evidence.present)
        self.assertFalse(evidence.declares_lifecycle_gate)

    def test_replaying_an_identical_observation_is_idempotent(self):
        first, second = admit(), admit()
        self.assertEqual((first.admissible, first.reason), (second.admissible, second.reason))

    def test_replaying_a_refusal_is_idempotent_too(self):
        kwargs = dict(source=source_journals(marker=declaration_marker(), close=False))
        first, second = admit(**kwargs), admit(**kwargs)
        self.assertEqual((first.admissible, first.reason), (second.admissible, second.reason))

    def test_the_two_new_journals_are_appended_and_rewrite_nothing(self):
        # The issue's non-goals forbid rewriting #15164's journals. The declaration is a NEW
        # record after the Close, so every pre-existing journal is byte-identical either way.
        without = dict(source_journals(marker=None))
        with_declaration = dict(source_journals(marker=declaration_marker()))
        for journal_id, notes in without.items():
            self.assertEqual(with_declaration[journal_id], notes)
        self.assertEqual(set(with_declaration) - set(without), {DECLARATION_JOURNAL})


class TheOtherTerminalsCannotReachThisShapeAndViceVersa(unittest.TestCase):
    """The route boundaries, measured rather than asserted."""

    def test_the_reproduction_records_no_review_round_at_all(self):
        # This is WHY the ordinary fence and the #14755 terminal both refuse forever: there is no
        # review generation to be approved and no round to have concluded ``changes_requested``.
        facts = fold_issue_gate_facts(source_journals(marker=None))
        self.assertEqual(facts.review_round_journals, ())
        self.assertEqual(facts.latest_gate, GATE_CLOSE)
        self.assertEqual(facts.latest_gate_journal, CLOSE_JOURNAL)

    def test_the_14755_terminal_still_refuses_this_shape(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501
            evaluate_superseded_failure_admissible,
            fold_superseded_failure,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
            FindingVerdictFacts,
            SuccessorAcknowledgementFacts,
        )

        # Even handed a well-formed #14755 declaration, a record with no review round cannot pass
        # that route: it names a round the history does not have.
        src = source_journals(
            marker=None,
            extra=[
                (
                    DECLARATION_JOURNAL,
                    "## superseded_failure\n\n"
                    + _render_14755_declaration()
                    + "\n",
                )
            ],
        )
        declaration = fold_superseded_failure(src)
        self.assertEqual(declaration.state, DECLARATION_SUPERSEDED)
        outcome = evaluate_superseded_failure_admissible(
            declaration,
            currently_current=True,
            verdicts=FindingVerdictFacts(),
            acknowledgement=SuccessorAcknowledgementFacts(),
            successor=SuccessorEvidence(),
            latest_round_journal="",
            latest_round_gate="",
            latest_round_conclusion="",
            close_recorded=True,
            target_issue=ISSUE,
            integration_branch=INTEGRATION_BRANCH,
            committed_integration_branch=INTEGRATION_BRANCH,
            measured_branch=LANE,
            expected_workspace=WORKSPACE,
            expected_lane=LANE,
            expected_lane_generation=GENERATION,
            live_head=HEAD,
            live_commits_ahead=0,
            worktree_clean=True,
            callbacks_drained=True,
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_ROUND_MISMATCH)

    def test_this_route_refuses_a_record_that_does_carry_a_review_round(self):
        # The mirror image, and the fence that stops this route from becoming a second way past a
        # review that DID happen. Reported before the lifecycle conjuncts so an operator on the
        # wrong route learns that first.
        src = source_journals(
            marker=declaration_marker(),
            extra=[("101795", "## Gate: review\n- 結論: 要修正\n")],
        )
        outcome = admit(source=src)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_REVIEW_ROUND_RECORDED)

    def test_even_an_approved_round_sends_the_lane_back_to_the_ordinary_fence(self):
        src = source_journals(
            marker=declaration_marker(),
            extra=[("101795", "## Gate: review\n- 結論: 承認\n")],
        )
        outcome = admit(source=src)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_REVIEW_ROUND_RECORDED)

    def test_a_bare_review_request_is_a_review_round_too(self):
        # An unanswered request is a review still OWED, which is the opposite of a lane that never
        # had one. It must not slip through as "no round".
        src = source_journals(
            marker=declaration_marker(),
            extra=[("101795", "## Gate: review_request\n")],
        )
        self.assertEqual(admit(source=src).reason, REASON_REVIEW_ROUND_RECORDED)


class OrdinaryDevelopmentIsRefused(unittest.TestCase):
    """Everything that is not this exact terminal shape stays blocked, with zero write."""

    def test_a_record_with_no_declaration_never_enters_the_route(self):
        outcome = admit(source=source_journals(marker=None))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_NOT_RECORDED)

    def test_an_open_issue_is_refused(self):
        outcome = admit(source=source_journals(marker=declaration_marker(), close=False))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_CLOSE_NOT_RECORDED)

    def test_an_owed_callback_is_refused(self):
        self.assertEqual(admit(callbacks_drained=False).reason, REASON_CALLBACK_OWED)

    def test_a_lane_that_re_opened_after_the_declaration_is_refused(self):
        # Any recognized lifecycle gate at-or-after the declaration means the lane went back to
        # work after the terminal was declared. This subsumes the review-round ordering question
        # (a round IS a gate) and catches every other gate as well.
        src = source_journals(
            marker=declaration_marker(),
            extra=[("101950", "## Gate: task_close\n")],
        )
        outcome = admit(source=src)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_GATE_AFTER_DECLARATION)

    def test_a_declaration_sharing_its_journal_with_a_lifecycle_gate_is_refused(self):
        # The STRICT tie, named at the call site exactly as #14755 names its own: one journal
        # claiming "this lane is finished forever" and "here is a lifecycle event" in the same
        # breath orders nothing, so the fail-closed reading is that the gate stands.
        src = [
            (START_JOURNAL, "## Gate: start\n"),
            (AUDIT_JOURNAL, AUDIT_NOTE),
            (
                DECLARATION_JOURNAL,
                "## Gate: task_close\n\n" + declaration_marker() + "\n",
            ),
        ]
        outcome = admit(source=src)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_GATE_AFTER_DECLARATION)

    def test_a_record_that_declares_a_commit_is_refused(self):
        # Required BESIDE the live check, not instead of it: a zero ahead-count is also what
        # already-merged work looks like (#14695's own boundary), and merged work that never had a
        # review must never reach this terminal.
        src = source_journals(
            marker=declaration_marker(), implementation_commit="a" * 40
        )
        outcome = admit(source=src)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_RECORD_DECLARES_CHANGE)


class TheAuditRecordMustBeRealAndMustNotBeAGate(unittest.TestCase):
    """The half that keeps the source failure and the successor approval two separate facts."""

    def test_a_declaration_naming_a_journal_the_record_does_not_carry_is_refused(self):
        outcome = admit(
            source=source_journals(marker=declaration_marker(audit_journal="999999"))
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_JOURNAL_NOT_FOUND)

    def test_a_declaration_with_no_audit_journal_in_the_history_at_all_is_refused(self):
        outcome = admit(source=source_journals(marker=declaration_marker(), audit=None))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_JOURNAL_NOT_FOUND)

    def test_naming_a_lifecycle_gate_journal_as_the_audit_record_is_refused(self):
        # If the named record IS a gate, this is not the shape the route terminalizes — and reading
        # it here would be exactly the separation this fence exists to keep.
        outcome = admit(
            source=source_journals(
                marker=declaration_marker(audit_journal=IMPLEMENTATION_DONE_JOURNAL)
            )
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_JOURNAL_IS_A_GATE)

    def test_an_audit_journal_newer_than_the_declaration_terminalizes_nothing(self):
        later = "101950"
        outcome = admit(
            source=source_journals(
                marker=declaration_marker(audit_journal=later),
                extra=[(later, "## Independent audit — round 2\n- 結論: 受け入れ未達\n")],
            )
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_JOURNAL_NOT_EARLIER)

    def test_a_declaration_cannot_name_itself_as_the_audit_record(self):
        outcome = admit(
            source=source_journals(
                marker=declaration_marker(audit_journal=DECLARATION_JOURNAL)
            )
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_JOURNAL_NOT_EARLIER)

    def test_a_history_carrying_the_same_journal_id_twice_is_not_addressable(self):
        src = source_journals(marker=declaration_marker())
        src.insert(1, (AUDIT_JOURNAL, "## Independent audit — 別の記録\n"))
        outcome = admit(source=src)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_JOURNAL_NOT_FOUND)


class TheSuccessorMustAcknowledgeAndHaveSucceeded(unittest.TestCase):
    """The bidirectional correlation — the half the source issue cannot write for itself."""

    def test_a_successor_that_never_acknowledged_is_refused(self):
        outcome = admit(successor=successor_journals(ack=None))
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_an_acknowledgement_naming_another_predecessor_is_refused(self):
        outcome = admit(
            successor=successor_journals(
                ack=acknowledgement_marker(superseded_issue="14999")
            )
        )
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_an_acknowledgement_naming_another_audit_record_is_refused(self):
        outcome = admit(
            successor=successor_journals(
                ack=acknowledgement_marker(superseded_audit_journal="101700")
            )
        )
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_an_acknowledgement_naming_another_review_is_refused(self):
        outcome = admit(
            successor=successor_journals(
                ack=acknowledgement_marker(review_journal="101700")
            )
        )
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_a_successor_whose_review_is_not_approved_is_incomplete(self):
        outcome = admit(
            successor=successor_journals(ack=acknowledgement_marker(), conclusion="要修正")
        )
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_INCOMPLETE)

    def test_a_successor_that_is_not_closed_is_incomplete(self):
        outcome = admit(
            successor=successor_journals(ack=acknowledgement_marker(), close=False)
        )
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_INCOMPLETE)

    def test_a_successor_with_a_newer_unanswered_round_is_incomplete(self):
        outcome = admit(
            successor=successor_journals(
                ack=acknowledgement_marker(),
                extra=[("101830", "## Gate: review_request\n")],
            )
        )
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_INCOMPLETE)

    def test_an_issue_cannot_be_its_own_successor(self):
        # The renderer refuses to write it, so the only way this reaches the reader is by hand.
        self_successor = declaration_marker().replace(
            f"successor_issue={SUCCESSOR}", f"successor_issue={ISSUE}"
        )
        outcome = admit(source=source_journals(marker=self_successor))
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_IS_SELF)

    def test_every_single_acknowledgement_field_mutation_breaks_the_correlation(self):
        base = acknowledgement_marker()
        self.assertTrue(admit().admissible)
        for field in SUCCESSOR_ACK_FIELD_ORDER:
            with self.subTest(field=field):
                mutated = _mutate(base, field, _ACK_REPLACEMENTS)
                self.assertNotEqual(mutated, base)
                outcome = admit(successor=successor_journals(ack=mutated))
                self.assertFalse(outcome.admissible)
                self.assertEqual(outcome.reason, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    def test_a_self_referential_acknowledgement_read_back_is_invalid(self):
        forged = acknowledgement_marker().replace(
            f"superseded_issue={ISSUE}", f"superseded_issue={SUCCESSOR}"
        )
        facts = fold_audit_supersession_acknowledgement([(ACK_JOURNAL, forged)])
        self.assertEqual(facts.state, ACK_INVALID)
        self.assertFalse(facts.in_force)

    def test_a_newer_malformed_acknowledgement_shadows_an_older_valid_one(self):
        journals = [
            (ACK_JOURNAL, acknowledgement_marker()),
            ("101950", "## Gate: superseded_audit_failure_successor\n- 撤回する\n"),
        ]
        facts = fold_audit_supersession_acknowledgement(journals)
        self.assertEqual(facts.state, ACK_INVALID)
        self.assertTrue(facts.recorded)

    def test_no_acknowledgement_at_all_folds_to_none(self):
        facts = fold_audit_supersession_acknowledgement(successor_journals(ack=None))
        self.assertEqual(facts.state, ACK_NONE)
        self.assertFalse(facts.recorded)

    def test_the_rendered_acknowledgement_is_what_the_reader_accepts(self):
        facts = fold_audit_supersession_acknowledgement(
            [(ACK_JOURNAL, acknowledgement_marker())]
        )
        self.assertEqual(facts.state, ACK_ACKNOWLEDGED)
        self.assertEqual(facts.issue, SUCCESSOR)
        self.assertEqual(facts.superseded_issue, ISSUE)
        self.assertEqual(facts.superseded_audit_journal, AUDIT_JOURNAL)
        self.assertEqual(facts.review_journal, SUCCESSOR_REVIEW_JOURNAL)

    def test_the_acknowledgement_renderer_refuses_what_its_parser_refuses(self):
        for kwargs in (
            {"issue": ""},
            {"superseded_issue": ""},
            {"issue": SUCCESSOR, "superseded_issue": SUCCESSOR},
            {"superseded_audit_journal": ""},
            {"superseded_audit_journal": "j#"},
            {"review_journal": "not-a-journal"},
            {"issue": "151:65"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    acknowledgement_marker(**kwargs)


class TheLiveHalfBoundsTheRoute(unittest.TestCase):
    """Zero commits over the integration branch is what makes admitting cost nothing."""

    def test_a_lane_carrying_commits_over_the_integration_branch_is_refused(self):
        self.assertEqual(admit(commits_ahead=1).reason, REASON_LANE_NOT_INTEGRATED)

    def test_measuring_some_other_checkout_at_the_same_head_is_refused(self):
        # The reproduction's own shape: a lane that never committed sits exactly on the integration
        # head, so ANY checkout there satisfies every live conjunct. Binding the measured branch to
        # the declared lane is what makes "the head did not move" a claim about the lane.
        self.assertEqual(
            admit(measured_branch=INTEGRATION_BRANCH).reason, REASON_MEASURED_BRANCH_MISMATCH
        )

    def test_a_dirty_worktree_is_refused(self):
        self.assertEqual(admit(worktree_clean=False).reason, REASON_WORKTREE_NOT_CLEAN)

    def test_an_unmeasured_repository_never_testifies_that_nothing_changed(self):
        self.assertEqual(admit(live_head="").reason, REASON_LANE_HEAD_UNMEASURED)
        self.assertEqual(admit(commits_ahead=None).reason, REASON_LANE_HEAD_UNMEASURED)

    def test_a_head_that_moved_after_the_declaration_is_refused(self):
        self.assertEqual(admit(live_head="0" * 40).reason, REASON_POST_DECLARATION_MUTATION)

    def test_a_boolean_is_not_a_commit_count(self):
        self.assertEqual(admit(commits_ahead=True).reason, REASON_LANE_HEAD_UNMEASURED)


class ForeignEvidenceNeverUnlocksThisFence(unittest.TestCase):
    """Evidence from another issue / lane / generation / branch admits nothing."""

    def test_a_declaration_about_another_issue_is_refused(self):
        self.assertEqual(admit(target_issue="15999").reason, REASON_ISSUE_MISMATCH)

    def test_a_blank_target_issue_correlates_to_nothing(self):
        self.assertEqual(admit(target_issue="").reason, REASON_ISSUE_MISMATCH)

    def test_a_declaration_about_another_lane_or_generation_is_refused(self):
        for kwargs in (
            {"lane": "issue_99999_other_lane"},
            {"workspace": "other_workspace"},
            {"generation": GENERATION + 1},
            {"generation": 0},
            {"lane": ""},
            {"workspace": ""},
        ):
            with self.subTest(**kwargs):
                self.assertEqual(admit(**kwargs).reason, REASON_LANE_MISMATCH)

    def test_a_declaration_made_about_another_integration_branch_is_refused(self):
        outcome = admit(
            source=source_journals(marker=declaration_marker(integration_branch="main-next"))
        )
        self.assertEqual(outcome.reason, REASON_INTEGRATION_BRANCH_MISMATCH)

    def test_pointing_the_retire_at_a_caller_chosen_branch_makes_live_zero_vacuous(self):
        # The degenerate case the committed-config expectation exists for: --integration-branch
        # pointed at the lane's OWN branch makes "carries 0 commits over the integration branch"
        # trivially true, so a caller free to choose it could satisfy the conjunct that bounds this
        # whole route. Both the declaration and the retire must name the COMMITTED branch.
        outcome = admit(
            integration_branch=LANE,
            source=source_journals(marker=declaration_marker(integration_branch=LANE)),
        )
        self.assertEqual(outcome.reason, REASON_INTEGRATION_BRANCH_NOT_COMMITTED)

    def test_a_config_that_declares_no_integration_branch_supplies_no_expectation(self):
        self.assertEqual(
            admit(committed_branch="").reason, REASON_INTEGRATION_BRANCH_NOT_COMMITTED
        )


class TheDeclarationGrammarIsClosed(unittest.TestCase):
    """A marker the canonical producer could not render is refused whole."""

    def test_every_single_field_mutation_loses_the_admission(self):
        # A DERIVED oracle, not a list of examples: the field set comes from the contract itself,
        # so a field added later is covered without editing this test.
        base = declaration_marker()
        self.assertTrue(admit().admissible)
        for field in SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER:
            with self.subTest(field=field):
                mutated = _mutate(base, field, _DECLARATION_REPLACEMENTS)
                self.assertNotEqual(mutated, base)
                outcome = admit(source=source_journals(marker=mutated))
                self.assertFalse(outcome.admissible)
                self.assertIn(outcome.reason, SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS)

    def test_every_single_field_removal_makes_the_marker_unrenderable(self):
        base = declaration_marker()
        for field in SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER:
            with self.subTest(field=field):
                dropped = ":".join(
                    part for part in base[1:-1].split(":") if not part.startswith(f"{field}=")
                )
                facts = fold_superseded_audit_failure([(DECLARATION_JOURNAL, f"[{dropped}]")])
                self.assertIn(facts.state, {DECLARATION_INVALID, DECLARATION_NONE})

    def test_a_permuted_field_order_is_not_producer_output(self):
        parts = declaration_marker()[1:-1].split(":")
        head, body = parts[:2], parts[2:]
        body[1], body[2] = body[2], body[1]
        facts = fold_superseded_audit_failure(
            [(DECLARATION_JOURNAL, "[" + ":".join(head + body) + "]")]
        )
        self.assertEqual(facts.state, DECLARATION_INVALID)

    def test_an_invalid_declaration_refuses_as_invalid_and_not_as_absent(self):
        outcome = admit(
            source=source_journals(marker="## Gate: superseded_audit_failure\n- successor: #15165\n")
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_INVALID)

    def test_a_heading_alone_declares_but_cannot_mint(self):
        facts = fold_superseded_audit_failure(
            [(DECLARATION_JOURNAL, "## Gate: superseded_audit_failure\n- successor: #15165\n")]
        )
        self.assertEqual(facts.state, DECLARATION_INVALID)
        self.assertTrue(facts.recorded)
        self.assertFalse(facts.in_force)

    def test_a_quoted_marker_is_not_a_marker(self):
        facts = fold_superseded_audit_failure(
            [(DECLARATION_JOURNAL, "本文で引用する: `" + declaration_marker() + "`\n")]
        )
        self.assertEqual(facts.state, DECLARATION_NONE)

    def test_two_declarations_in_one_journal_are_authoritative_of_neither(self):
        both = declaration_marker() + "\n" + declaration_marker(successor_issue="14999")
        self.assertEqual(
            fold_superseded_audit_failure([(DECLARATION_JOURNAL, both)]).state,
            DECLARATION_INVALID,
        )

    def test_a_newer_malformed_declaration_shadows_an_older_valid_one(self):
        journals = [
            (DECLARATION_JOURNAL, declaration_marker()),
            ("101950", "## Gate: superseded_audit_failure\n- 記録漏れ\n"),
        ]
        self.assertEqual(
            fold_superseded_audit_failure(journals).state, DECLARATION_INVALID
        )

    def test_the_renderer_refuses_what_the_parser_refuses(self):
        for kwargs in (
            {"issue": ""},
            {"successor_issue": ""},
            {"issue": ISSUE, "successor_issue": ISSUE},
            {"integration_branch": ""},
            {"audit_journal": "not-a-journal"},
            {"audit_journal": ""},
            {"successor_review_journal": "j#"},
            {"lane_generation": 0},
            {"lane_generation": True},
            {"head": ""},
            {"head": "83a5f8"},
            {"workspace": ""},
            {"lane": ""},
            {"issue": "151:64"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    declaration_marker(**kwargs)

    def test_the_rendered_marker_is_what_the_reader_accepts(self):
        facts = fold_superseded_audit_failure([(DECLARATION_JOURNAL, declaration_marker())])
        self.assertEqual(facts.state, DECLARATION_SUPERSEDED)
        self.assertEqual(facts.issue, ISSUE)
        self.assertEqual(facts.audit_journal, AUDIT_JOURNAL)
        self.assertEqual(facts.successor_issue, SUCCESSOR)
        self.assertEqual(facts.successor_review_journal, SUCCESSOR_REVIEW_JOURNAL)
        self.assertEqual(facts.integration_branch, INTEGRATION_BRANCH)
        self.assertEqual(facts.head, HEAD)


_DECLARATION_REPLACEMENTS = {
    "gate": SUPERSEDED_FAILURE_GATE,
    "version": "2",
    "decision": "declined",
    "issue": "15999",
    "audit_journal": "999999",
    "successor_issue": "14999",
    "successor_review_journal": "999998",
    "integration_branch": "main-next",
    "workspace": "other_workspace",
    "lane": "issue_99999_other_lane",
    "lane_generation": str(GENERATION + 7),
    "head": "1" * 40,
}

_ACK_REPLACEMENTS = {
    "gate": "superseded_failure_successor",
    "version": "2",
    "decision": "declines",
    "issue": "14999",
    "superseded_issue": "15999",
    "superseded_audit_journal": "999999",
    "review_journal": "999998",
}


def _mutate(marker: str, field: str, replacements: dict) -> str:
    """One field of a rendered marker changed to a different, still-plausible value."""
    out = []
    for part in marker[1:-1].split(":"):
        key, sep, _ = part.partition("=")
        out.append(f"{field}={replacements[field]}" if sep and key == field else part)
    return "[" + ":".join(out) + "]"


def _render_14755_declaration() -> str:
    """A well-formed #14755 declaration about this lane, for the route-boundary test."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501
        render_superseded_failure_marker,
    )

    return render_superseded_failure_marker(
        issue=ISSUE,
        review_journal=AUDIT_JOURNAL,
        verdict_journal=AUDIT_JOURNAL,
        successor_issue=SUCCESSOR,
        successor_review_journal=SUCCESSOR_REVIEW_JOURNAL,
        integration_branch=INTEGRATION_BRANCH,
        workspace=WORKSPACE,
        lane=LANE,
        lane_generation=GENERATION,
        head=HEAD,
    )


class EveryDeclaredRefusalIsReachable(unittest.TestCase):
    """A reason nobody can reach is not a fence, it is a comment."""

    def test_every_refusal_reason_this_route_declares_is_reached_by_a_real_fixture(self):
        # DERIVED from the contract's own token set, not from a hand-kept list: a reason added
        # later fails here until a fixture reaches it.
        reached = {outcome.reason for outcome in _EVERY_REFUSAL_FIXTURE()}
        self.assertEqual(SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS - reached, set())
        self.assertEqual(reached - SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS, set())


def _EVERY_REFUSAL_FIXTURE():
    """One admission outcome per declared refusal, each isolating its own conjunct."""
    self_successor = declaration_marker().replace(
        f"successor_issue={SUCCESSOR}", f"successor_issue={ISSUE}"
    )
    return (
        admit(source=source_journals(marker=None)),
        admit(source=source_journals(marker="## Gate: superseded_audit_failure\n- 記録漏れ\n")),
        admit(target_issue="15999"),
        admit(lane="issue_99999_other_lane"),
        admit(
            integration_branch=LANE,
            source=source_journals(marker=declaration_marker(integration_branch=LANE)),
        ),
        admit(source=source_journals(marker=declaration_marker(integration_branch="main-next"))),
        admit(
            source=source_journals(
                marker=declaration_marker(),
                extra=[("101795", "## Gate: review\n- 結論: 要修正\n")],
            )
        ),
        admit(source=source_journals(marker=declaration_marker(), close=False)),
        admit(callbacks_drained=False),
        admit(
            source=source_journals(
                marker=declaration_marker(), extra=[("101950", "## Gate: task_close\n")]
            )
        ),
        admit(source=source_journals(marker=declaration_marker(audit_journal="999999"))),
        admit(
            source=source_journals(
                marker=declaration_marker(audit_journal=IMPLEMENTATION_DONE_JOURNAL)
            )
        ),
        admit(
            source=source_journals(
                marker=declaration_marker(audit_journal=DECLARATION_JOURNAL)
            )
        ),
        admit(source=source_journals(marker=declaration_marker(), implementation_commit="a" * 40)),
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
    """The generation fence / #14539 exemption / #14695 waiver / #14755 terminal stay as they were."""

    def test_the_waiver_route_still_admits_nothing(self):
        self.assertFalse(WRITER_AUTHORITY_RESOLVABLE)

    def test_the_authority_gate_tokens_all_stay_distinct(self):
        tokens = {
            SUPERSEDED_AUDIT_FAILURE_GATE,
            SUCCESSOR_ACK_GATE,
            SUPERSEDED_FAILURE_GATE,
            NO_CHANGE_REVIEW_WAIVER_GATE,
            MARKER_GATE_CODEX_DIRECT_EDIT,
        }
        self.assertEqual(len(tokens), 5)

    def test_a_14755_marker_is_not_read_as_a_15166_declaration(self):
        # Named per surface: a terminal written under one contract must never satisfy the other.
        facts = fold_superseded_audit_failure(
            [(DECLARATION_JOURNAL, _render_14755_declaration())]
        )
        self.assertEqual(facts.state, DECLARATION_NONE)

    def test_a_15166_marker_is_not_read_as_a_14755_declaration(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501
            fold_superseded_failure,
        )

        facts = fold_superseded_failure([(DECLARATION_JOURNAL, declaration_marker())])
        self.assertEqual(facts.state, DECLARATION_NONE)

    def test_a_14755_acknowledgement_does_not_satisfy_this_routes_acknowledgement(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
            render_successor_acknowledgement_marker,
        )

        foreign = render_successor_acknowledgement_marker(
            issue=SUCCESSOR,
            superseded_issue=ISSUE,
            superseded_review_journal=AUDIT_JOURNAL,
            review_journal=SUCCESSOR_REVIEW_JOURNAL,
        )
        self.assertEqual(
            fold_audit_supersession_acknowledgement([(ACK_JOURNAL, foreign)]).state, ACK_NONE
        )
        self.assertEqual(
            admit(successor=successor_journals(ack=foreign)).reason,
            REASON_SUCCESSOR_NOT_ACKNOWLEDGED,
        )

    def test_the_declaration_does_not_change_the_glance_projection(self):
        # The measured control (#14695 R5-F1's method). This route is retire-only and adds NO
        # glance projection, so the declaration must not move the lane's classified state.
        without = source_journals(marker=None)
        with_declaration = source_journals(marker=declaration_marker())
        states = [
            classify_lane_state(
                lane_signal_from_gate_facts(
                    ISSUE, fold_issue_gate_facts(journals), issue_open=False
                )
            )
            for journals in (without, with_declaration)
        ]
        self.assertEqual(states[0], states[1])

    def test_the_new_gates_carry_no_issuer_contract_rather_than_a_manufactured_one(self):
        # A gate registered in the issuer policy needs a ruling that NAMES it. Manufacturing an
        # anchor from a record that decided no writer contract is the #14661 j#92715 defect, so
        # this route makes no issuer claim at all and says so.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            ISSUER_UNKNOWN,
            contract_ruling_pointer,
            contract_writer_role,
        )

        for gate in (SUPERSEDED_AUDIT_FAILURE_GATE, SUCCESSOR_ACK_GATE):
            with self.subTest(gate=gate):
                self.assertEqual(contract_writer_role(gate), ISSUER_UNKNOWN)
                self.assertEqual(contract_ruling_pointer(gate), "")

    def test_neither_new_gate_is_a_lifecycle_gate(self):
        # Like every other authority declaration here: issue-wide and latest-wins, never a step in
        # the lane's lifecycle. A journal carrying only one of these headings contributes no gate.
        facts = fold_issue_gate_facts(source_journals(marker=declaration_marker()))
        self.assertEqual(facts.latest_gate, GATE_CLOSE)
        self.assertEqual(facts.latest_gate_journal, CLOSE_JOURNAL)
        successor_facts = fold_issue_gate_facts(
            successor_journals(ack=acknowledgement_marker())
        )
        self.assertEqual(successor_facts.latest_gate, GATE_CLOSE)
        self.assertEqual(successor_facts.latest_gate_journal, SUCCESSOR_CLOSE_JOURNAL)


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
            superseded_failure_terminal=False,
            superseded_audit_failure_terminal=True,
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
        register_sublane_retire(
            sub, add_repo_option=lambda p: None, add_lifecycle_json=lambda p: None
        )
        args = parser.parse_args(
            [
                "retire",
                "--issue",
                ISSUE,
                "--lane-label",
                LANE,
                "--superseded-audit-failure-terminal",
            ]
        )
        self.assertTrue(args.superseded_audit_failure_terminal)
        self.assertFalse(args.superseded_failure_terminal)

    def test_an_unresolved_retire_target_refuses_with_its_own_reason(self):
        outcome = _resolve_superseded_audit_failure_admissible(
            self._args(), target=None, repo_root=Path(".")
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_TARGET_UNRESOLVED)

    def test_an_unreadable_live_record_refuses_rather_than_reading_silence_as_absence(self):
        # Doubly important here: two of this route's conjuncts are NEGATIVE claims over the whole
        # record, and an empty read satisfies a negative claim by omission alone.
        with _stub_live({}):
            outcome = _resolve_superseded_audit_failure_admissible(
                self._args(),
                target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                repo_root=Path("."),
            )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_ROUTE_UNREADABLE)

    def test_the_route_admits_end_to_end_against_a_real_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
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

    def test_a_stale_local_integration_branch_refuses_rather_than_falling_back_to_a_remote(self):
        # The operational precondition the reproduction actually hits, pinned so it is diagnosed
        # rather than discovered. Measured on the real checkout: #15164's lane head IS
        # ``origin/main``, but the local ``main`` this worktree shares was 12 commits behind it, so
        # ``rev-list --count main..HEAD`` counted those 12 as commits the lane carries. The route
        # refuses — and must NOT fall back to ``origin/main``, because which remote qualifies is a
        # resolution rule #14755 already ruled it has no authority for. Fast-forwarding the local
        # integration branch is a routine coordinator action; guessing a remote spelling is not.
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            # The lane stays exactly where it is; the INTEGRATION branch falls behind.
            _git(Path(tmp), "checkout", "--quiet", INTEGRATION_BRANCH)
            _git(Path(tmp), "reset", "--hard", "--quiet", "HEAD~1")
            _git(Path(tmp), "checkout", "--quiet", LANE)
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            outcome = self._resolve_with(records, worktree=tmp)
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_LANE_NOT_INTEGRATED)

    def test_the_opt_in_is_required_for_the_route_to_run_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            outcome = self._resolve_with(
                records, worktree=tmp, superseded_audit_failure_terminal=False
            )
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, "")

    def test_the_fence_admits_through_the_shared_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker()),
            }
            with _stub_live(records):
                outcome = _resolve_latest_generation_admissible(
                    self._args(worktree=tmp),
                    target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                    repo_root=Path(tmp),
                )
        self.assertTrue(outcome.admissible, outcome.reason)

    def test_without_any_measured_route_the_operator_assertion_still_decides(self):
        # The pre-existing contract, unchanged: no measured input supplied -> the durable-record
        # assertion is consulted. Adding a fifth route must not change this.
        args = self._args(
            superseded_audit_failure_terminal=False, latest_generation_admissible=True
        )
        self.assertTrue(_resolve_latest_generation_admissible(args).admissible)

    def test_a_supplied_measured_route_never_falls_back_to_the_hand_assert(self):
        args = self._args(latest_generation_admissible=True)
        with _stub_live({}):
            outcome = _resolve_latest_generation_admissible(
                args,
                target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                repo_root=Path("."),
            )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_AUDIT_ROUTE_UNREADABLE)

    def test_the_typed_refusal_reaches_the_retire_decision(self):
        # The effect terminal (#14695 R5-F2: reading a pure return value is not measuring the
        # effect). A route's own reason must reach the operator instead of being collapsed into
        # ``stale_review_generation``, which names a review generation this lane cannot have.
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False),
            RetirePreflight(
                is_git_workspace=True,
                latest_generation_admissible=False,
                latest_generation_blocked_reason=REASON_REVIEW_ROUND_RECORDED,
            ),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(REASON_REVIEW_ROUND_RECORDED, decision.blocked_reasons)
        self.assertNotIn(INTEGRATION_STALE_REVIEW_GENERATION, decision.blocked_reasons)
        self.assertEqual(decision.primary_reason, REASON_REVIEW_ROUND_RECORDED)

    def test_an_admitted_route_clears_the_generation_blocker_entirely(self):
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False),
            RetirePreflight(is_git_workspace=True, latest_generation_admissible=True),
        )
        self.assertTrue(decision.may_retire)

    def _resolve_with(self, records, *, worktree: str, **overrides):
        with _stub_live(records):
            return _resolve_superseded_audit_failure_admissible(
                self._args(worktree=worktree, **overrides),
                target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                repo_root=Path(worktree),
            )


class _stub_live:
    """Replace the route's live Redmine read with a fixture map, restoring it afterwards."""

    def __init__(self, records):
        self._records = records
        self._original = None

    def __enter__(self):
        self._original = retire_superseded_audit_failure._read_live_issue_journals
        retire_superseded_audit_failure._read_live_issue_journals = (
            lambda issue: [
                (str(jid), notes) for jid, notes in self._records.get(str(issue), [])
            ]
        )
        return self

    def __exit__(self, *exc):
        retire_superseded_audit_failure._read_live_issue_journals = self._original
        return False


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

    The live half is measured against a REAL checkout rather than a stub, because what it fences is
    a property of git — a lane that carries nothing over the integration branch — and a stub would
    only re-state the expectation. This is also the reproduction's own shape: #15164 never
    committed, so its lane head IS the base head.
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
