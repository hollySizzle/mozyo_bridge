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
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (  # noqa: E501
    resolve_coordinator_provider,
)
from mozyo_bridge.core.state.audit_failure_terminal_decision import (  # noqa: E501
    AuditFailureTerminalDecisionError,
    AuditFailureTerminalDecisionStore,
    DecisionRoute,
    TerminalDecision,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    superseded_audit_failure_terminal as terminal_module,
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
    REASON_DECISION_DRIFTED,
    REASON_DECISION_STALE_REVISION,
    REASON_NO_COORDINATOR_DECISION,
    REASON_RECEIPT_AUTHORITY_UNRESOLVED,
    REASON_NOT_RECORDED,
    REASON_RECORD_DECLARES_CHANGE,
    REASON_REVIEW_ROUND_RECORDED,
    REASON_SOURCE_OPEN_IN_TRACKER,
    REASON_SUCCESSOR_IS_SELF,
    REASON_SUCCESSOR_NOT_ACKNOWLEDGED,
    REASON_SUCCESSOR_OPEN_IN_TRACKER,
    REASON_SUCCESSOR_REVIEW_HEAD_MISMATCH,
    REASON_TRACKER_STATUS_UNREADABLE,
    SUCCESSOR_ACK_FIELD_ORDER,
    SUCCESSOR_ACK_GATE,
    SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER,
    SUPERSEDED_AUDIT_FAILURE_GATE,
    RECEIPT_AUTHORITY_RESOLVABLE,
    CoordinatorTerminalDecision,
    SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS,
    TrackerIssueStatus,
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
#: The provider the committed config binds to the coordinator role, and the lane it sits on. The
#: writer attestation compares the process identity against BOTH, so a test that only sets env
#: presence is refused exactly as a non-coordinator caller is.
COORDINATOR_PROVIDER = resolve_coordinator_provider(str(ROOT))


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


#: The lane lifecycle revision the coordinator's decision was taken against, and that the retire
#: must still measure. Single use lives here: any lifecycle mutation advances it.
REVISION = 1


def recorded_decision(**overrides) -> CoordinatorTerminalDecision:
    """The coordinator decision that matches the reproduction, with named field overrides.

    Shaped exactly as the application projects it out of the decision store, so a fixture that
    satisfies the fence is one the real store could have produced.
    """
    kwargs = dict(
        recorded=True,
        decision_id="aft_" + "0" * 32,
        workspace_id=WORKSPACE,
        lane_id=LANE,
        lane_generation=GENERATION,
        lane_revision=REVISION,
        issue=ISSUE,
        audit_journal=AUDIT_JOURNAL,
        successor_issue=SUCCESSOR,
        successor_review_journal=SUCCESSOR_REVIEW_JOURNAL,
        head=HEAD,
        integration_branch=INTEGRATION_BRANCH,
    )
    kwargs.update(overrides)
    return CoordinatorTerminalDecision(**kwargs)


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


#: The conclusion the canonical ``review_result`` marker carries for each governed prose spelling.
_MARKER_CONCLUSION = {"承認": "approved", "要修正": "changes_requested"}


def successor_journals(
    *,
    ack: "str | None" = None,
    conclusion: str = "承認",
    close: bool = True,
    reviewed_head: str = HEAD,
    canonical_markers: bool = True,
    extra: "list[tuple[str, str]] | None" = None,
) -> "list[tuple[str, str]]":
    """The successor issue's durable history, shaped like #15165's real one.

    ``reviewed_head`` is the head the round's markers pin — #15165's real pair carries the same
    ``83a65e6d…`` on both the request and the result. ``canonical_markers=False`` drops them, which
    is how a fixture reaches "the approval examined nothing this workspace can name": the glance
    grammar populates ``review_round_head`` only for a round whose result head was correlated
    against its request head.
    """
    request = "## Gate: review_request\n"
    result = f"## Gate: review\n- 結論: {conclusion}\n"
    if canonical_markers:
        request += f"\n[mozyo:workflow-event:gate=review_request:head={reviewed_head}]\n"
        result += (
            f"\n[mozyo:workflow-event:gate=review_result:"
            f"conclusion={_MARKER_CONCLUSION[conclusion]}:head={reviewed_head}:"
            f"req={SUCCESSOR_REVIEW_REQUEST_JOURNAL}]\n"
        )
    journals = [
        (SUCCESSOR_REVIEW_REQUEST_JOURNAL, request),
        (SUCCESSOR_REVIEW_JOURNAL, result),
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
    source_closed_in_tracker: "bool | None" = True,
    successor_closed_in_tracker: "bool | None" = True,
    decision: "CoordinatorTerminalDecision | None" = None,
    revision: int = REVISION,
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
        successor_review_head=(
            successor_facts.review_round_head if successor_facts else ""
        ),
        decision=recorded_decision() if decision is None else decision,
        expected_lane_revision=revision,
        tracker=TrackerIssueStatus(
            source_closed=source_closed_in_tracker,
            successor_closed=successor_closed_in_tracker,
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

    def test_the_fully_correlated_record_reaches_the_authority_and_stops_there(self):
        # The route admits NOTHING today (coordinator ruling on j#102184). What the reproduction
        # still demonstrates is that every OTHER conjunct passes — the refusal is the missing
        # receipt authority, not the record — which is exactly what makes flipping
        # ``RECEIPT_AUTHORITY_RESOLVABLE`` when #15195 lands an authority check rather than a
        # re-implementation.
        outcome = admit()
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)

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
        self.assertEqual(admit().reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)
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


class OnlyARecordedCoordinatorDecisionConverges(unittest.TestCase):
    """Review j#102074 / scope j#102081 / direction j#102092: the authority is a recorded decision."""

    def test_a_lane_with_no_recorded_decision_is_refused(self):
        # The authority's own negative control: every record and every live fact is perfect, and
        # without a coordinator decision the route still admits nothing.
        outcome = admit(decision=CoordinatorTerminalDecision())
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_NO_COORDINATOR_DECISION)

    def test_an_unrelated_approved_and_closed_successor_on_the_same_head_is_refused(self):
        # The regression review j#101909 required, now answered by the decision rather than by an
        # enumeration. #14999 is a genuinely complete successor — approved `## Gate: review` with
        # canonical markers on the SAME head, a Close, and an acknowledgement naming this issue and
        # this audit journal back — so every R1 and R2 conjunct holds. The coordinator decided about
        # #15165, so the terminal does not converge.
        outcome = admit(
            source=source_journals(marker=declaration_marker(successor_issue="14999")),
            successor=successor_journals(
                ack=acknowledgement_marker(issue="14999"), reviewed_head=HEAD
            ),
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_DECISION_DRIFTED)

    def test_every_decided_identity_must_still_match_what_the_retire_measured(self):
        # A DERIVED oracle over the decision's own bound identities: drift in ANY of them means the
        # decision is not about the world this retire measured. Each mutation is applied to the
        # DECISION, so the records stay perfect and only the binding moves.
        for field, value in (
            ("workspace_id", "other_workspace"),
            ("lane_id", "issue_99999_other_lane"),
            ("lane_generation", GENERATION + 1),
            ("issue", "15999"),
            ("audit_journal", "999999"),
            ("successor_issue", "14999"),
            ("successor_review_journal", "999998"),
            ("head", "1" * 40),
            ("integration_branch", "main-next"),
        ):
            with self.subTest(field=field):
                outcome = admit(decision=recorded_decision(**{field: value}))
                self.assertFalse(outcome.admissible)
                self.assertEqual(outcome.reason, REASON_DECISION_DRIFTED)

    def test_a_decision_taken_against_another_lane_revision_is_spent(self):
        # Single use, expressed through the lifecycle revision rather than a second ledger: the row
        # moved since the coordinator decided, so this decision has either already authorized a
        # mutation or was overtaken by one.
        self.assertEqual(
            admit(decision=recorded_decision(lane_revision=REVISION + 1)).reason,
            REASON_DECISION_STALE_REVISION,
        )
        self.assertEqual(
            admit(revision=REVISION + 1).reason, REASON_DECISION_STALE_REVISION
        )

    def test_an_unmeasured_lane_revision_is_refused(self):
        for revision in (0, -1, True):
            with self.subTest(revision=revision):
                self.assertEqual(
                    admit(revision=revision).reason, REASON_DECISION_STALE_REVISION
                )

    def test_the_domain_holds_no_default_authority_a_caller_can_replace(self):
        # Review j#102074 finding 1 as a DERIVED oracle over the module's public surface: the
        # authority is no longer a package constant at all, so there is nothing to enumerate, hand
        # in, or monkeypatch. The decision arrives as a measurement like every other input.
        import inspect

        self.assertFalse(hasattr(terminal_module, "SANCTIONED_MIGRATIONS"))
        for name in terminal_module.__all__:
            member = getattr(terminal_module, name)
            if not callable(member) or isinstance(member, type):
                continue
            with self.subTest(callable=name):
                self.assertNotIn("migrations", inspect.signature(member).parameters)

    def test_the_decision_default_is_the_unrecorded_one(self):
        # A fence input whose default admitted would be a fence that defaults open.
        self.assertFalse(CoordinatorTerminalDecision().recorded)


class TheRouteAdmitsNothingUntilTheReceiptAuthorityExists(unittest.TestCase):
    """Coordinator ruling on consultation j#102184: hold as a typed refusal, do not guess again."""

    def test_the_record_command_does_not_claim_an_authority_it_does_not_grant(self):
        # Review j#102582 finding 2: the operator-facing record surface said the decision "IS that
        # judgement" and "authorizes ... once" while the route it feeds admits nothing. A help text
        # that contradicts the ruling is an over-claim on the surface an operator actually reads.
        import argparse

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_audit_failure_terminal_decision import (  # noqa: E501
            register_audit_failure_terminal_decision,
        )

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="sublane_command")
        register_audit_failure_terminal_decision(sub, add_repo_option=lambda p: None)

        # What `sublane --help` shows for the group, and what the group's own help shows for
        # `record`: both must name the ruling rather than an authority the record does not grant.
        group_summary = " ".join(
            (action.help or "")
            for action in sub._choices_actions
            if action.dest == "audit-failure-terminal"
        )
        group_summary = " ".join(group_summary.split())
        record_help = " ".join(
            sub.choices["audit-failure-terminal"].format_help().split()
        )
        self.assertIn("authorizes NO retire", group_summary)
        self.assertIn("#15195", group_summary)
        self.assertIn("#15195", record_help)
        self.assertIn("coordinator_receipt_authority_unresolvable", record_help)

    def test_the_receipt_authority_is_declared_unresolvable(self):
        self.assertFalse(RECEIPT_AUTHORITY_RESOLVABLE)

    def test_no_input_admits(self):
        # The whole point of the ruling: there is no record, no live state and no decision that
        # turns this route into an admission today.
        for kwargs in (
            {},
            {"source": source_journals(marker=declaration_marker())},
            {"successor": successor_journals(ack=acknowledgement_marker())},
        ):
            with self.subTest(**{k: "…" for k in kwargs}):
                self.assertFalse(admit(**kwargs).admissible)

    def test_the_permanent_refusal_is_reported_last(self):
        # A record's own defect is diagnosed on its own terms; only a record with nothing left to
        # fix reports the missing authority. Getting this backwards would send an operator to
        # #15195 for a problem that is actually in their record.
        self.assertEqual(admit(worktree_clean=False).reason, REASON_WORKTREE_NOT_CLEAN)
        self.assertEqual(admit(commits_ahead=1).reason, REASON_LANE_NOT_INTEGRATED)
        self.assertEqual(
            admit(decision=CoordinatorTerminalDecision()).reason,
            REASON_NO_COORDINATOR_DECISION,
        )
        self.assertEqual(admit().reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)

    def test_flipping_the_flag_would_be_the_whole_change(self):
        # The contract stays live and tested underneath, so #15195 lands an authority check rather
        # than a re-implementation. Measured by patching the ONE flag: everything else already
        # passes for the reproduction.
        with mock.patch.object(terminal_module, "RECEIPT_AUTHORITY_RESOLVABLE", True):
            outcome = admit()
        self.assertTrue(outcome.admissible, outcome.reason)
        self.assertEqual(outcome.reason, REASON_OK)

    def test_the_other_routes_are_still_untouched(self):
        # An inert route must not have weakened its siblings on the way in.
        self.assertFalse(WRITER_AUTHORITY_RESOLVABLE)


class TheDecisionStoreIsTheWriterHalf(unittest.TestCase):
    """The surface no journal write can reach, and its fail-closed edges."""

    def _home(self) -> Path:
        # A home NESTED inside the temp dir, so ``home.parent`` — the repository these tests attest
        # against — is that temp dir and not the shared system temp root. Writing the anchor into a
        # shared root would mark it as a workspace for every other suite that walks up from cwd.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        home.mkdir(parents=True, exist_ok=True)
        return home

    def test_a_recorded_decision_reads_back_with_a_store_minted_id(self):
        home = self._home()
        _record_decision(home, head=HEAD)
        store = AuditFailureTerminalDecisionStore(home=home)
        read = store.read(DecisionRoute(WORKSPACE, LANE))
        self.assertIsNotNone(read)
        self.assertTrue(read.decision_id.startswith("aft_"))
        self.assertEqual(read.issue, ISSUE)
        self.assertEqual(read.head, HEAD)
        self.assertEqual(read.lane_revision, REVISION)

    def test_the_caller_never_supplies_the_decision_id(self):
        # An id the caller chooses is a handle the caller can re-use; the store mints its own.
        home = self._home()
        _record_decision(home, head=HEAD, decision_id="aft_" + "9" * 32)
        read = AuditFailureTerminalDecisionStore(home=home).read(DecisionRoute(WORKSPACE, LANE))
        self.assertNotEqual(read.decision_id, "aft_" + "9" * 32)

    def test_reading_a_store_that_was_never_written_fails_closed(self):
        # Absent is not "no decision, carry on": the read raises and the route's own handler turns
        # that into the unrecorded default, which refuses.
        with self.assertRaises(AuditFailureTerminalDecisionError):
            AuditFailureTerminalDecisionStore(home=self._home()).read(
                DecisionRoute(WORKSPACE, LANE)
            )

    def test_a_store_whose_identity_sidecar_is_gone_fails_closed(self):
        home = self._home()
        _record_decision(home, head=HEAD)
        store = AuditFailureTerminalDecisionStore(home=home)
        store.sidecar_path.unlink()
        with self.assertRaises(AuditFailureTerminalDecisionError):
            store.read(DecisionRoute(WORKSPACE, LANE))

    def test_a_replaced_store_fails_closed(self):
        home = self._home()
        _record_decision(home, head=HEAD)
        store = AuditFailureTerminalDecisionStore(home=home)
        store.sidecar_path.write_text("a-different-store", encoding="utf-8")
        with self.assertRaises(AuditFailureTerminalDecisionError):
            store.read(DecisionRoute(WORKSPACE, LANE))
        # …and it refuses to be written into, rather than adopting the foreign DB.
        with self.assertRaises(AuditFailureTerminalDecisionError):
            _record_decision(home, head=HEAD)

    def test_another_lane_route_has_no_decision(self):
        home = self._home()
        _record_decision(home, head=HEAD)
        self.assertIsNone(
            AuditFailureTerminalDecisionStore(home=home).read(
                DecisionRoute(WORKSPACE, "issue_99999_other_lane")
            )
        )

    def test_a_decision_that_could_never_match_is_never_stored(self):
        # A record the retire can only ever refuse is an operator trap, not a fence.
        for override in (
            {"issue": ""},
            {"successor_issue": ""},
            {"issue": ISSUE, "successor_issue": ISSUE},
            {"audit_journal": ""},
            {"lane_generation": 0},
            {"lane_revision": 0},
            {"lane_revision": True},
            {"integration_branch": ""},
        ):
            with self.subTest(**override):
                with self.assertRaises(AuditFailureTerminalDecisionError):
                    _record_decision(self._home(), head=HEAD, **override)
        with self.assertRaises(AuditFailureTerminalDecisionError):
            _record_decision(self._home(), head="83a5f8")

    def test_re_deciding_replaces_the_route_s_decision(self):
        # A lane whose head moved needs the coordinator to decide about the world that now exists.
        home = self._home()
        _record_decision(home, head=HEAD)
        _record_decision(home, head="b" * 40)
        read = AuditFailureTerminalDecisionStore(home=home).read(DecisionRoute(WORKSPACE, LANE))
        self.assertEqual(read.head, "b" * 40)

    def test_a_caller_with_no_actor_identity_records_nothing(self):
        # Review j#102147 finding 1, as the reviewer reproduced it: a caller carrying no actor
        # identity at all. R4 recorded successfully; the writer boundary now refuses zero-write.
        home = self._home()
        with mock.patch.dict(
            os.environ,
            {"MOZYO_WORKSPACE_ID": "", "MOZYO_AGENT_ROLE": "", "MOZYO_LANE_ID": ""},
            clear=False,
        ):
            with self.assertRaises(AuditFailureTerminalDecisionError):
                AuditFailureTerminalDecisionStore(home=home).record(
                    TerminalDecision(**_decision_fields(head=HEAD)),
                    repo_root=_attested_repo(home.parent),
                )
        self.assertFalse(AuditFailureTerminalDecisionStore(home=home).path.exists())

    def test_a_non_coordinator_actor_records_nothing(self):
        # Env PRESENCE is not attestation. The implementer provider, a foreign workspace, and a
        # non-default lane each resolve to a real identity that is not the coordinator's.
        home = self._home()
        repo_root = _attested_repo(home.parent)
        for env in (
            {"MOZYO_AGENT_ROLE": "claude"},
            {"MOZYO_WORKSPACE_ID": "some_other_workspace"},
            {"MOZYO_LANE_ID": "issue_15164_fresh_session_resume_verification"},
        ):
            with self.subTest(**env):
                with _attested_coordinator_env(), mock.patch.dict(
                    os.environ, env, clear=False
                ):
                    with self.assertRaises(AuditFailureTerminalDecisionError):
                        AuditFailureTerminalDecisionStore(home=home).record(
                            TerminalDecision(**_decision_fields(head=HEAD)),
                            repo_root=repo_root,
                        )
        self.assertFalse(AuditFailureTerminalDecisionStore(home=home).path.exists())

    def test_an_unanchored_repository_records_nothing(self):
        # The independent side of the comparison. Without the repo's workspace anchor the sender
        # identity cannot be cross-checked, so the coordinator env alone establishes nothing.
        home = self._home()
        with _attested_coordinator_env():
            with self.assertRaises(AuditFailureTerminalDecisionError):
                AuditFailureTerminalDecisionStore(home=home).record(
                    TerminalDecision(**_decision_fields(head=HEAD)),
                    repo_root=home.parent / "unanchored",
                )

    def test_the_attested_coordinator_route_records(self):
        # The positive control for the same gate: the identity the launcher injects, cross-checked
        # against this repository's anchor and its committed coordinator provider.
        home = self._home()
        _record_decision(home, head=HEAD)
        self.assertIsNotNone(
            AuditFailureTerminalDecisionStore(home=home).read(DecisionRoute(WORKSPACE, LANE))
        )

    def test_a_symlinked_db_or_sidecar_writes_nothing_outside_the_home(self):
        # Review j#102147 finding 2, as reproduced: a symlink at either artifact wrote outside the
        # home entirely. Both are checked on the LINK, before every open and every write.
        for artifact in ("db", "sidecar"):
            with self.subTest(artifact=artifact):
                home = self._home()
                outside = home.parent / f"outside-{artifact}"
                store = AuditFailureTerminalDecisionStore(home=home)
                target = store.path if artifact == "db" else store.sidecar_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside)
                with _attested_coordinator_env():
                    with self.assertRaises(AuditFailureTerminalDecisionError):
                        AuditFailureTerminalDecisionStore(home=home).record(
                            TerminalDecision(**_decision_fields(head=HEAD)),
                            repo_root=_attested_repo(home.parent),
                        )
                # The dangling symlink is refused too — nothing was created through it.
                self.assertFalse(outside.exists())

    def test_a_symlinked_home_writes_nothing_outside_it(self):
        # Review j#102181 finding 2, as reproduced: the HOME itself is the link, so checking only
        # the leaf artifacts established nothing about "inside the mozyo home".
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        outside = root / "outside"
        outside.mkdir()
        home = root / "home"
        home.symlink_to(outside)
        with _attested_coordinator_env():
            with self.assertRaises(AuditFailureTerminalDecisionError):
                AuditFailureTerminalDecisionStore(home=home).record(
                    TerminalDecision(**_decision_fields(head=HEAD)),
                    repo_root=_attested_repo(root / "repo"),
                )
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_symlinked_ancestor_of_the_home_writes_nothing_outside_it(self):
        # Not just the home: ANY component from the root down is examined, so a link one level up
        # is caught before anything below it is trusted.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        outside = root / "outside"
        outside.mkdir()
        parent = root / "parent"
        parent.symlink_to(outside)
        home = parent / "home"
        with _attested_coordinator_env():
            with self.assertRaises(AuditFailureTerminalDecisionError):
                AuditFailureTerminalDecisionStore(home=home).record(
                    TerminalDecision(**_decision_fields(head=HEAD)),
                    repo_root=_attested_repo(root / "repo"),
                )
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_symlinked_home_is_not_read_either(self):
        # The read path refuses on the same chain, so a redirected home cannot supply a decision.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        real = root / "real"
        real.mkdir()
        _record_decision(real, head=HEAD, repo_root=_attested_repo(root / "repo"))
        home = root / "home"
        home.symlink_to(real)
        with self.assertRaises(AuditFailureTerminalDecisionError):
            AuditFailureTerminalDecisionStore(home=home).read(DecisionRoute(WORKSPACE, LANE))

    def test_the_sidecar_is_opened_without_following_a_link(self):
        # O_NOFOLLOW, so even inside a clean home the sidecar is never opened through a link — the
        # path guard and the open are two independent refusals of the same shape.
        home = self._home()
        _record_decision(home, head=HEAD)
        store = AuditFailureTerminalDecisionStore(home=home)
        real = home / "real.nonce"
        real.write_text(store.sidecar_path.read_text(encoding="utf-8"), encoding="utf-8")
        store.sidecar_path.unlink()
        store.sidecar_path.symlink_to(real)
        with self.assertRaises(AuditFailureTerminalDecisionError):
            AuditFailureTerminalDecisionStore(home=home).read(DecisionRoute(WORKSPACE, LANE))

    def test_a_swapped_artifact_writes_nothing_outside_the_home(self):
        # Review j#102582 finding 1, as the reviewer reproduced it and as the required direction
        # asks it be fixed: a link planted where an artifact will be created must produce NO file,
        # no nonce and no directory outside the home. R6 detected the redirect only after sqlite
        # had already created a 20480-byte database at the link target; there is no sqlite here,
        # and every create goes through O_EXCL|O_NOFOLLOW relative to the home descriptor.
        for artifact in ("records", "sidecar"):
            with self.subTest(artifact=artifact):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                root = Path(tmp.name)
                outside = root / "outside"
                outside.mkdir()
                home = root / "home"
                home.mkdir()
                store = AuditFailureTerminalDecisionStore(home=home)
                target = store.path if artifact == "records" else store.sidecar_path
                target.symlink_to(outside / "stolen")
                with _attested_coordinator_env():
                    with self.assertRaises(AuditFailureTerminalDecisionError):
                        store.record(
                            TerminalDecision(**_decision_fields(head=HEAD)),
                            repo_root=_attested_repo(root / "repo"),
                        )
                self.assertEqual(sorted(p.name for p in outside.iterdir()), [])

    def test_nothing_below_the_home_is_reached_by_re_resolving_a_path(self):
        # The structural claim behind the fix, measured on the source: every artifact operation is
        # relative to the home descriptor (``dir_fd=``), so there is no second path resolution for
        # a swap to win. A future edit that reaches for a bare open / connect fails here.
        import inspect

        from mozyo_bridge.core.state import audit_failure_terminal_decision as store_module

        source = inspect.getsource(store_module)
        # No sqlite in the CODE. The module docstring names it only to explain why it is not used
        # (it cannot be handed a descriptor), so the docstring is stripped before asserting.
        body = source.split('"""', 2)[-1]
        self.assertNotIn("sqlite3", body)
        # The ONE bare open is the home directory itself, and it carries O_DIRECTORY|O_NOFOLLOW.
        # Every other artifact operation names a directory descriptor.
        lines = source.splitlines()
        for index, line in enumerate(lines):
            for opener in ("os.open(", "os.rename(", "os.unlink("):
                if opener not in line:
                    continue
                window = " ".join(lines[index : index + 4])
                if "dir_fd" in window or "O_DIRECTORY" in window:
                    continue
                self.fail(f"artifact IO without a directory descriptor: {line.strip()}")

    def test_a_symlinked_store_is_not_read_either(self):
        home = self._home()
        _record_decision(home, head=HEAD)
        store = AuditFailureTerminalDecisionStore(home=home)
        real = store.path.read_bytes()
        moved = home / "moved.sqlite"
        moved.write_bytes(real)
        store.path.unlink()
        store.path.symlink_to(moved)
        with self.assertRaises(AuditFailureTerminalDecisionError):
            AuditFailureTerminalDecisionStore(home=home).read(DecisionRoute(WORKSPACE, LANE))

    def test_no_journal_shaped_input_reaches_the_store(self):
        # The property the three refuted attempts lacked, stated as a test over the writer's own
        # signature: the store's only writer takes a typed decision, never a note or a marker.
        import inspect

        parameters = inspect.signature(AuditFailureTerminalDecisionStore.record).parameters
        # ``repo_root`` is not an authority the caller supplies: it names the repository whose
        # ANCHOR the writer attestation cross-checks the process identity against — the independent
        # side of that comparison, and the same input #13613's lane-mutation gate takes.
        self.assertEqual(
            [name for name in parameters if name not in ("self", "now")],
            ["decision", "repo_root"],
        )


class TheApprovalMustHaveExaminedThisLaneHead(unittest.TestCase):
    """Review j#101880 finding 1: the conjunct two self-written markers cannot manufacture."""

    def test_two_markers_agreeing_with_each_other_are_not_enough_on_their_own(self):
        # The finding's own reduction. The declaration and the acknowledgement can be placed by ONE
        # unauthenticatable actor across two issues, so a route whose safety rests on them agreeing
        # rests on nothing. Strip the successor's canonical review markers — everything the R1
        # design required is still present and still agrees — and the admission is gone.
        outcome = admit(
            successor=successor_journals(
                ack=acknowledgement_marker(), canonical_markers=False
            )
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_REVIEW_HEAD_MISMATCH)

    def test_an_approval_about_another_head_does_not_cover_this_lane(self):
        outcome = admit(
            successor=successor_journals(ack=acknowledgement_marker(), reviewed_head="c" * 40)
        )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_REVIEW_HEAD_MISMATCH)

    def test_a_shadowed_review_marker_is_not_an_approval_and_names_no_head(self):
        # A result whose ``req`` does not correlate to its request is shadowed by the shared
        # grammar: it folds to ``pending`` with NO body fallback, so the successor refuses as
        # INCOMPLETE before head coverage is even asked — and it names no head either. Both halves
        # are pinned, because the second is what this route added and the first is what already
        # kept a broken marker from reading as an approval.
        broken = (
            "## Gate: review\n- 結論: 承認\n\n"
            f"[mozyo:workflow-event:gate=review_result:conclusion=approved:head={HEAD}:req=999999]\n"
        )
        successor = successor_journals(ack=acknowledgement_marker())
        successor[1] = (SUCCESSOR_REVIEW_JOURNAL, broken)
        self.assertEqual(admit(successor=successor).reason, REASON_SUCCESSOR_INCOMPLETE)
        self.assertEqual(fold_issue_gate_facts(successor).review_round_head, "")

    def test_the_reviewed_head_is_read_through_the_shared_grammar(self):
        # Not a route-local parse: the same fold every other consumer uses exposes it, and it is
        # populated only for a round whose result head correlated against its request head.
        facts = fold_issue_gate_facts(successor_journals(ack=acknowledgement_marker()))
        self.assertEqual(facts.review_round_head, HEAD)
        shadowed = fold_issue_gate_facts(
            successor_journals(ack=acknowledgement_marker(), canonical_markers=False)
        )
        self.assertEqual(shadowed.review_round_head, "")

    def test_the_head_coverage_is_a_three_way_equality_with_the_live_lane(self):
        # The declaration head is compared against BOTH the reviewed head and (below, in the live
        # half) the lane's actual head, so "a real approved review covers exactly the state this
        # lane holds" is a measurement rather than a claim.
        moved = admit(live_head="0" * 40)
        self.assertEqual(moved.reason, REASON_POST_DECLARATION_MUTATION)
        self.assertEqual(admit().reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)

    def test_an_arbitrary_non_audit_journal_no_longer_carries_admission_weight(self):
        # The finding's other repro: a plain progress memo named as the audit record. The route
        # still admits it — and that is now CORRECT rather than a hole, because the admission rests
        # on head coverage plus zero-change plus live-zero, not on the pointer's prose. Pinned so
        # the claim in the docstring is measured rather than asserted.
        memo = source_journals(
            marker=declaration_marker(),
            audit="## 進捗メモ\n\n単なる進捗メモです。監査結果ではありません。\n",
        )
        self.assertEqual(admit(source=memo).reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)
        # …and stripping the head coverage from that same record refuses it, which is the point:
        # the pointer never was what made it safe.
        self.assertEqual(
            admit(
                source=memo,
                successor=successor_journals(
                    ack=acknowledgement_marker(), canonical_markers=False
                ),
            ).reason,
            REASON_SUCCESSOR_REVIEW_HEAD_MISMATCH,
        )


class TheTrackerStatusIsReadNotInferred(unittest.TestCase):
    """Review j#101880 finding 2: a Close gate is a belief, the tracker is the fact."""

    def test_a_status_only_reopen_of_the_source_is_refused(self):
        # The shape the journal fold structurally cannot see: Redmine's status changes and no
        # ``## Gate:`` note is added, so every journal-derived conjunct still passes.
        outcome = admit(source_closed_in_tracker=False)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SOURCE_OPEN_IN_TRACKER)

    def test_a_status_only_reopen_of_the_successor_is_refused(self):
        # The successor had NO current-status input at all before this: a re-opened successor
        # counted as complete on the strength of its past Close gate.
        outcome = admit(successor_closed_in_tracker=False)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_SUCCESSOR_OPEN_IN_TRACKER)

    def test_an_unreadable_status_refuses_as_its_own_reason(self):
        # Never a silent fall-through to the journal answer, and never collapsed into "open":
        # "the tracker says open" and "we could not ask" send an operator to different places.
        for kwargs in (
            {"source_closed_in_tracker": None},
            {"successor_closed_in_tracker": None},
        ):
            with self.subTest(**kwargs):
                self.assertEqual(admit(**kwargs).reason, REASON_TRACKER_STATUS_UNREADABLE)

    def test_the_journal_close_gate_alone_no_longer_admits(self):
        # The control that proves the new conjunct is load-bearing: the record's Close gate is
        # present and unchanged in every one of these fixtures.
        facts = fold_issue_gate_facts(source_journals(marker=declaration_marker()))
        self.assertEqual(facts.latest_gate, GATE_CLOSE)
        self.assertFalse(admit(source_closed_in_tracker=False).admissible)


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
        self.assertEqual(admit().reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)
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
        admit(),
        admit(decision=CoordinatorTerminalDecision()),
        admit(
            source=source_journals(marker=declaration_marker(successor_issue="14999")),
            successor=successor_journals(
                ack=acknowledgement_marker(issue="14999"), reviewed_head=HEAD
            ),
        ),
        admit(revision=REVISION + 1),
        admit(successor=successor_journals(ack=None)),
        admit(successor=successor_journals(ack=acknowledgement_marker(), close=False)),
        admit(
            successor=successor_journals(
                ack=acknowledgement_marker(), canonical_markers=False
            )
        ),
        admit(source_closed_in_tracker=None),
        admit(source_closed_in_tracker=False),
        admit(successor_closed_in_tracker=False),
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
            home=None,
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
                SUCCESSOR: successor_journals(ack=acknowledgement_marker(), reviewed_head=head),
            }
            outcome = self._resolve_with(records, worktree=tmp, home=_home(tmp, head))
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)

    def test_a_dirty_real_checkout_refuses_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            (Path(tmp) / "scratch.txt").write_text("uncommitted", encoding="utf-8")
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker(), reviewed_head=head),
            }
            outcome = self._resolve_with(records, worktree=tmp, home=_home(tmp, head))
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_WORKTREE_NOT_CLEAN)

    def test_a_real_lane_carrying_a_commit_refuses_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_lane_checkout(Path(tmp))
            head = _commit(Path(tmp), "extra.txt", "work")
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(
                    ack=acknowledgement_marker(), reviewed_head=head
                ),
            }
            outcome = self._resolve_with(records, worktree=tmp, home=_home(tmp, head))
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_LANE_NOT_INTEGRATED)

    def test_a_checkout_that_cannot_resolve_the_committed_branch_yields_no_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            _git(Path(tmp), "branch", "-D", INTEGRATION_BRANCH)
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker(), reviewed_head=head),
            }
            outcome = self._resolve_with(records, worktree=tmp, home=_home(tmp, head))
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
                SUCCESSOR: successor_journals(ack=acknowledgement_marker(), reviewed_head=head),
            }
            outcome = self._resolve_with(records, worktree=tmp, home=_home(tmp, head))
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_LANE_NOT_INTEGRATED)

    def test_a_status_only_reopen_refuses_end_to_end_for_either_issue(self):
        # The effect terminal for review j#101880 finding 2: not only does the pure fence refuse,
        # the route ASKS the tracker. Both issues are covered because the successor had no
        # current-status input at all before this round.
        for issue, reason in (
            (ISSUE, REASON_SOURCE_OPEN_IN_TRACKER),
            (SUCCESSOR, REASON_SUCCESSOR_OPEN_IN_TRACKER),
        ):
            with self.subTest(reopened=issue), tempfile.TemporaryDirectory() as tmp:
                head = _make_lane_checkout(Path(tmp))
                records = {
                    ISSUE: source_journals(marker=declaration_marker(head=head)),
                    SUCCESSOR: successor_journals(
                        ack=acknowledgement_marker(), reviewed_head=head
                    ),
                }
                outcome = self._resolve_with(
                    records, worktree=tmp, closed={issue: False}, home=_home(tmp, head)
                )
                self.assertFalse(outcome.admissible)
                self.assertEqual(outcome.reason, reason)

    def test_an_unreadable_tracker_status_refuses_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(
                    ack=acknowledgement_marker(), reviewed_head=head
                ),
            }
            outcome = self._resolve_with(records, worktree=tmp, closed={ISSUE: None}, home=_home(tmp, head))
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, REASON_TRACKER_STATUS_UNREADABLE)

    def test_the_status_reader_requires_the_response_to_identify_the_exact_issue(self):
        # The reader's own discipline, exercised against its real body with a fake transport: a
        # payload about a DIFFERENT issue, or with no status, testifies about nothing.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        def _reader(payload):
            source = LiveRedmineJournalSource(
                base_url="https://example.invalid",
                api_key="k",
                transport=lambda **_: payload,
            )
            original = LiveRedmineJournalSource.from_environment
            LiveRedmineJournalSource.from_environment = classmethod(
                lambda cls, **kwargs: source
            )
            try:
                return retire_superseded_audit_failure._read_live_issue_closed(ISSUE)
            finally:
                LiveRedmineJournalSource.from_environment = original

        closed = {"issue": {"id": ISSUE, "status": {"is_closed": True}}}
        self.assertIs(_reader(closed), True)
        self.assertIs(_reader({"issue": {"id": ISSUE, "status": {"is_closed": False}}}), False)
        # A response about another issue, an absent status, and a non-mapping payload each yield
        # the unmeasured value — never a silent True.
        self.assertIsNone(_reader({"issue": {"id": "99999", "status": {"is_closed": True}}}))
        self.assertIsNone(_reader({"issue": {"id": ISSUE}}))
        self.assertIsNone(_reader(["not", "an", "issue"]))

    def test_the_opt_in_is_required_for_the_route_to_run_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker(), reviewed_head=head),
            }
            outcome = self._resolve_with(
                records, worktree=tmp, home=_home(tmp, head), superseded_audit_failure_terminal=False
            )
            self.assertFalse(outcome.admissible)
            self.assertEqual(outcome.reason, "")

    def test_the_fence_admits_through_the_shared_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = _make_lane_checkout(Path(tmp))
            records = {
                ISSUE: source_journals(marker=declaration_marker(head=head)),
                SUCCESSOR: successor_journals(ack=acknowledgement_marker(), reviewed_head=head),
            }
            with _stub_live(records):
                outcome = _resolve_latest_generation_admissible(
                    self._args(worktree=tmp, home=_home(tmp, head)),
                    target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                    repo_root=Path(tmp),
                )
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.reason, REASON_RECEIPT_AUTHORITY_UNRESOLVED)

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

    def _resolve_with(self, records, *, worktree: str, closed=None, home=None, **overrides):
        with _stub_live(records, closed=closed):
            return _resolve_superseded_audit_failure_admissible(
                self._args(worktree=worktree, home=home, **overrides),
                target=RetireEvidenceTarget(WORKSPACE, LANE, GENERATION, "pointer", 1),
                repo_root=Path(worktree),
            )


class _enumeration:
    """Rewrite the PACKAGE's own enumeration for the duration of a test.

    Not an injection seam: :func:`sanctioned_migration` takes no enumeration argument, so nothing a
    caller can reach through an exported function selects the authority (review j#102074 finding
    1). What this does is replace the module constant inside the test process — the same thing a
    reviewed code change does, done temporarily — which is how a test can specify against an
    enumeration other than the shipped one (an empty list, or the head of a fixture repository that
    git generated at run time).
    """

    def __init__(self, migrations):
        self._migrations = tuple(migrations)
        self._original = None

    def __enter__(self):
        self._original = terminal_module.SANCTIONED_MIGRATIONS
        terminal_module.SANCTIONED_MIGRATIONS = self._migrations
        return self

    def __exit__(self, *exc):
        terminal_module.SANCTIONED_MIGRATIONS = self._original
        return False


class _stub_live:
    """Replace BOTH of the route's live Redmine reads with fixtures, restoring them afterwards.

    Two reads, two stubs: the journal histories and each issue's CURRENT tracker status. ``closed``
    maps an issue id to the tri-state the tracker read yields; anything not named defaults to
    ``True``. Both are stubbed unconditionally so no test in this suite can reach the network.

    The coordinator DECISION is deliberately not stubbed here — the end-to-end tests record a real
    one into a temp-home store through the real writer, so the authority is exercised rather than
    simulated.
    """

    def __init__(self, records, closed=None):
        self._records = records
        self._closed = closed or {}
        self._original_journals = None
        self._original_closed = None

    def __enter__(self):
        self._original_journals = retire_superseded_audit_failure._read_live_issue_journals
        self._original_closed = retire_superseded_audit_failure._read_live_issue_closed
        retire_superseded_audit_failure._read_live_issue_journals = (
            lambda issue: [
                (str(jid), notes) for jid, notes in self._records.get(str(issue), [])
            ]
        )
        retire_superseded_audit_failure._read_live_issue_closed = (
            lambda issue: self._closed.get(str(issue), True)
        )
        return self

    def __exit__(self, *exc):
        retire_superseded_audit_failure._read_live_issue_journals = self._original_journals
        retire_superseded_audit_failure._read_live_issue_closed = self._original_closed
        return False


def _home(tmp: str, head: str, **overrides) -> Path:
    """A temp mozyo home carrying a real recorded decision for the fixture repository's head."""
    home = Path(tmp) / "_home"
    home.mkdir(parents=True, exist_ok=True)
    _record_decision(home, head=head, repo_root=Path(tmp), **overrides)
    return home


def _attested_repo(root: Path) -> Path:
    """A repository whose ANCHOR and coordinator binding make the writer attestation resolvable.

    The decision store attests its writer through the same #13613 gate a lane mutation uses: the
    process env is cross-checked against this repo's workspace anchor and the committed coordinator
    provider. A test therefore has to stand up the real identity surface — env presence alone is
    exactly what that gate refuses.
    """
    anchor_dir = root / ".mozyo-bridge"
    anchor_dir.mkdir(parents=True, exist_ok=True)
    (anchor_dir / "workspace-anchor.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": WORKSPACE,
                "canonical_session": "mzb1",
            }
        ),
        encoding="utf-8",
    )
    if not (anchor_dir / "config.yaml").exists():
        (anchor_dir / "config.yaml").write_text(
            f"version: 2\nsublane_integration:\n  integration_branch: {INTEGRATION_BRANCH}\n",
            encoding="utf-8",
        )
    return root


@contextlib.contextmanager
def _attested_coordinator_env():
    """Run the block with this process presenting the coordinator's launch identity."""
    with mock.patch.dict(
        os.environ,
        {
            "MOZYO_WORKSPACE_ID": WORKSPACE,
            "MOZYO_AGENT_ROLE": COORDINATOR_PROVIDER,
            "MOZYO_LANE_ID": DEFAULT_LANE,
        },
        clear=False,
    ):
        yield


def _decision_fields(*, head: str, **overrides) -> dict:
    """The decision the reproduction's coordinator would record, with named field overrides."""
    fields = dict(
        workspace_id=WORKSPACE,
        lane_id=LANE,
        decision_id="",
        lane_generation=GENERATION,
        lane_revision=REVISION,
        issue=ISSUE,
        audit_journal=AUDIT_JOURNAL,
        successor_issue=SUCCESSOR,
        successor_review_journal=SUCCESSOR_REVIEW_JOURNAL,
        head=head,
        integration_branch=INTEGRATION_BRANCH,
    )
    fields.update(overrides)
    return fields


def _record_decision(home: Path, *, head: str, repo_root: Path = None, **overrides) -> None:
    """Record a REAL coordinator decision into a temp-home store (the writer path).

    Not a stub: the end-to-end tests go through :class:`AuditFailureTerminalDecisionStore` itself —
    including its writer attestation — so the authority the route consults is one the shipped store
    actually produced under the real gate.
    """
    fields = _decision_fields(head=head, **overrides)
    repo_root = Path(repo_root) if repo_root is not None else Path(home).parent
    with _attested_coordinator_env():
        AuditFailureTerminalDecisionStore(home=home).record(
            TerminalDecision(**fields), repo_root=_attested_repo(repo_root)
        )


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
    # The decision store lives in a temp mozyo home beside the checkout; it is not repository
    # content, so it must not make the lane's worktree dirty (which is a conjunct under test).
    # Neither the temp mozyo home nor the test's workspace anchor is repository content, and the
    # lane worktree being clean is itself a conjunct under test.
    (path / ".gitignore").write_text(
        "_home/\n.mozyo-bridge/workspace-anchor.json\n", encoding="utf-8"
    )
    _git(path, "add", ".gitignore", ".mozyo-bridge/config.yaml")
    _git(path, "commit", "-m", "config")
    _commit(path, "base.txt", "base")
    _git(path, "branch", "-M", INTEGRATION_BRANCH)
    _git(path, "checkout", "--quiet", "-b", LANE)
    return _git(path, "rev-parse", "HEAD")


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
