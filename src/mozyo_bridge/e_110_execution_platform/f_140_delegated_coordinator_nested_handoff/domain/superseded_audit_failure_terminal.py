"""Terminalizing a lane whose failure was recorded WITHOUT a formal Review Gate (#15166).

#15164 is the reproduction. It is a no-change verification lane: the worker's trace landed in
``## Gate: implementation_done`` j#101783, the coordinator's round-1 verdict landed in
``## Independent audit — round 1`` j#101792 which states in as many words
``formal_review_gate: review_request journalが無いため本journalを``## Gate: review``として記録しない``,
the same acceptance was then reached by the successor #15165 whose OWN ``## Gate: review`` j#101810
concluded ``approved``, both issues are task_closed and closed, and the lane carries zero commits
with a clean worktree. The standard ``sublane retire`` refuses permanently with
``stale_review_generation`` (measured: #15164 j#101825).

**Why the three existing routes cannot reach it**, measured against the live record rather than
assumed:

- the ordinary review-generation fence reads a review generation, and this issue has none —
  ``fold_issue_gate_facts`` returns ``review_round_journals=()``;
- the #14755 ``superseded_failure`` terminal REQUIRES the newest round to be a ``## Gate: review``
  concluding ``changes_requested`` (:data:`...superseded_failure_terminal.REASON_ROUND_DID_NOT_FAIL`)
  and requires a ``review_finding_verdict`` gate covering that round's finding set. With zero
  rounds and no verdict gate, both conjuncts refuse forever. Widening them would mean reading a
  record that has no review round AS one, which destroys the premise that route rests on;
- the #14695 ``no_change_review_waiver`` refuses structurally for every input
  (``waiver_writer_authority_unresolvable``).

So this is a FOURTH independent route to the same one boolean, and like the other three it can
only ever ADMIT — none of them can weaken another, and a lane that fails all four is blocked
exactly as it was before any of them existed.

**What this route rests on, stated in one place rather than implied.** Every conjunct is either
measured from live git or folded from a durable record, and the declaration marker is a POINTER,
never a substitute: it names WHICH audit record and WHICH successor, and each is then re-read from
the record itself.

**And what it deliberately does NOT establish.** It does not establish that the named audit record
CONCLUDED a failure. That conclusion is prose, and prose is not a governed surface — inventing a
grammar for it here would be the #14755 review j#99065 defect one level over: a claim whose only
witness is the record that makes it. What is established instead, and what actually carries the
safety weight, is the conjunction:

1. **no approval exists anywhere on this issue** — the record carries ZERO review rounds
   (:data:`REASON_REVIEW_ROUND_RECORDED`), so there is no approval to be stale, borrowed or
   misread, and this route can never become a second way past the ordinary fence;
2. **the named audit record is real and is not a gate** — it exists in the issue's own history and
   declares no lifecycle gate (:data:`REASON_AUDIT_JOURNAL_IS_A_GATE`), which is what keeps the
   source-side failure and the successor-side approval two SEPARATE facts: a record that is not a
   gate cannot be projected as an approval by any consumer;
3. **the successor names this issue and this audit record back** — a source-side declaration alone
   can name any approved issue in the tracker as its successor, so the bidirectional
   acknowledgement is what makes the pairing a correlation rather than a claim, and it is written
   into a DIFFERENT issue's record, by a different actor, at a different time;
4. **the consequence is bounded** — the record declares zero repository change AND the live lane
   carries zero commits over the integration branch with a clean worktree, so admitting drains a
   process and terminalizes lifecycle metadata without integrating anything, minting any approval,
   or draining a lane that holds unreviewed work.

Nor does it establish WHO wrote anything. This workspace cannot authenticate a journal's writer:
every role posts under one source-system account (ruling #14219 j#86718) and
:func:`...hibernate_issuer_policy.resolve_journal_issuer` is a POLICY binding that takes no author
input and says so. This gate is therefore deliberately NOT registered in
:mod:`.hibernate_evidence_authority` — registering one needs a ruling that NAMES it, and
manufacturing an anchor from a record that decided no writer contract is the #14661 j#92715 defect.
That premise is stated here so a reviewer can accept or reject it, not buried.

Boundary: pure. No IO, no Redmine, no git. The live repository half, the gate folds and the
successor issue's own facts are necessarily measured by the application layer and arrive here as
parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    EnvelopeParseError,
    LaneEvidenceEnvelope,
    parse_lane_envelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (  # noqa: E501
    REASON_OK,
    AdmissionResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
    GATE_REVIEW,
    REVIEW_APPROVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
    journal_ref,
    one_canonical_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501  (shared refusal vocabulary)
    REASON_CALLBACK_OWED,
    REASON_CLOSE_NOT_RECORDED,
    REASON_INTEGRATION_BRANCH_NOT_COMMITTED,
    REASON_LANE_HEAD_UNMEASURED,
    REASON_LANE_NOT_INTEGRATED,
    REASON_MEASURED_BRANCH_MISMATCH,
    REASON_POST_DECLARATION_MUTATION,
    REASON_SUCCESSOR_INCOMPLETE,
    REASON_WORKTREE_NOT_CLEAN,
    SuccessorEvidence,
)

#: The gate token this authority is declared under. Named per SURFACE, like every other authority
#: gate in this context: a terminal for a lane that never had a Review Gate can never be read as a
#: terminal for one whose Review Gate failed (#14755's ``superseded_failure``), and vice versa.
#: Like ``codex_direct_edit`` / ``no_change_review_waiver`` / ``superseded_failure`` it is an
#: issue-wide, latest-wins AUTHORITY fact and deliberately NOT a lifecycle gate — it is not added
#: to the glance grammar's heading allowlist, because the lane's lifecycle already recorded what
#: happened (an implementation, an independent audit, then a Close) and this declares what to DO
#: about it. This route is retire-only and has no glance projection.
SUPERSEDED_AUDIT_FAILURE_GATE = "superseded_audit_failure"

#: The schema version. A marker of an unknown version is REFUSED rather than interpreted under
#: today's field meanings — the next version may give an existing field a different weight.
SUPERSEDED_AUDIT_FAILURE_VERSION = "1"

#: The only admissible decision. A record written to DECLINE the terminal carries a different
#: token and therefore cannot admit anything.
SUPERSEDED_AUDIT_FAILURE_DECISION = "superseded"

#: The COMPLETE, ORDERED field set a canonical declaration carries — no more, no less, in this
#: sequence. An unknown / missing / permuted field is a marker the canonical producer could not
#: have rendered, and tolerating one is how a future meaningful field gets ignored by an old
#: verifier (the #14661 j#92533 F2 / j#92601 F2 lesson).
#:
#: ``audit_journal`` replaces #14755's ``review_journal`` + ``verdict_journal`` pair: there is no
#: review round to name and no governed per-finding verdict gate to point at. ``integration_branch``
#: is part of the declaration for the same reason #14755 carries it — the live "carries no commit
#: over the integration branch" measurement is taken against whatever ``--integration-branch``
#: names, so a declaration made about one branch must not be replayable against a branch chosen to
#: make the count zero.
SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER: Tuple[str, ...] = (
    "gate",
    "version",
    "decision",
    "issue",
    "audit_journal",
    "successor_issue",
    "successor_review_journal",
    "integration_branch",
    "workspace",
    "lane",
    "lane_generation",
    "head",
)

# ---------------------------------------------------------------------------
# The closed declaration vocabulary.
# ---------------------------------------------------------------------------

#: No terminal declaration is in the durable record at all — nothing for this route to admit.
DECLARATION_NONE = "none"
#: A VALID declaration: one canonical marker, every constant field at its literal value, a
#: parseable lane envelope, and readable journal references.
DECLARATION_SUPERSEDED = "superseded_state"
#: A declaration is present but cannot be read as one. Fail-closed: treated exactly like
#: :data:`DECLARATION_NONE` by every consumer, and it SUPERSEDES an older valid declaration.
DECLARATION_INVALID = "invalid"

SUPERSEDED_AUDIT_FAILURE_STATES: frozenset[str] = frozenset(
    {DECLARATION_NONE, DECLARATION_SUPERSEDED, DECLARATION_INVALID}
)


@dataclass(frozen=True)
class SupersededAuditFailureFacts:
    """The LATEST durable audit-failure terminal declaration for one issue.

    ``state`` is a closed :data:`SUPERSEDED_AUDIT_FAILURE_STATES` token; ``journal`` is where the
    declaration was recorded. Every other field is projected from the canonical marker and is
    EMPTY / ``None`` unless the declaration is valid — never guessed from prose, and never
    completed from the lane's current lifecycle row (that is precisely how a superseded
    generation's evidence gets promoted onto the live one).
    """

    state: str = DECLARATION_NONE
    journal: str = ""
    issue: str = ""
    audit_journal: str = ""
    successor_issue: str = ""
    successor_review_journal: str = ""
    integration_branch: str = ""
    envelope: Optional[LaneEvidenceEnvelope] = None

    @property
    def recorded(self) -> bool:
        """True when any declaration (valid or not) is in the durable record."""
        return self.state != DECLARATION_NONE

    @property
    def in_force(self) -> bool:
        """True ONLY for a VALID declaration. :data:`DECLARATION_INVALID` is False, like absent."""
        return self.state == DECLARATION_SUPERSEDED and self.envelope is not None

    @property
    def head(self) -> str:
        """The lane head the declaration was recorded against, or ``""``."""
        return self.envelope.head if self.envelope is not None else ""


def _int_journal(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


def _journal_declaration(notes: str) -> Optional[SupersededAuditFailureFacts]:
    """The declaration ONE journal makes, or ``None`` if it makes none (pure).

    Qualification, location, quote-exclusion and parsing are ONE authority
    (:func:`...superseded_failure_correlation.one_canonical_marker`), so a quoted marker earlier
    in the note can never be substituted for the canonical one, and this route cannot drift from
    the #14755 route about what "one canonical marker" means.
    """
    declared, fields = one_canonical_marker(
        notes,
        gate=SUPERSEDED_AUDIT_FAILURE_GATE,
        field_order=SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER,
    )
    if not declared:
        return None
    if fields is None:
        return SupersededAuditFailureFacts(state=DECLARATION_INVALID)

    constants = {
        "gate": SUPERSEDED_AUDIT_FAILURE_GATE,
        "version": SUPERSEDED_AUDIT_FAILURE_VERSION,
        "decision": SUPERSEDED_AUDIT_FAILURE_DECISION,
    }
    if any(fields.get(key) != value for key, value in constants.items()):
        return SupersededAuditFailureFacts(state=DECLARATION_INVALID)

    issue = str(fields.get("issue", "") or "").strip()
    successor_issue = str(fields.get("successor_issue", "") or "").strip()
    integration_branch = str(fields.get("integration_branch", "") or "").strip()
    audit_journal = journal_ref(fields.get("audit_journal", ""))
    successor_review = journal_ref(fields.get("successor_review_journal", ""))
    if not all((issue, successor_issue, integration_branch, audit_journal, successor_review)):
        return SupersededAuditFailureFacts(state=DECLARATION_INVALID)

    # The ONE envelope grammar (never a second copy): non-empty workspace / lane, a POSITIVE
    # generation, and a full lowercase 40/64-hex head. ``require_head=True`` because the head is
    # what makes post-declaration mutation detectable — a declaration with no head pins nothing.
    envelope = parse_lane_envelope(fields, require_head=True)
    if isinstance(envelope, EnvelopeParseError):
        return SupersededAuditFailureFacts(state=DECLARATION_INVALID)

    return SupersededAuditFailureFacts(
        state=DECLARATION_SUPERSEDED,
        issue=issue,
        audit_journal=audit_journal,
        successor_issue=successor_issue,
        successor_review_journal=successor_review,
        integration_branch=integration_branch,
        envelope=envelope,
    )


def fold_superseded_audit_failure(
    journals: Sequence[Tuple[object, str]],
) -> SupersededAuditFailureFacts:
    """The LATEST durable audit-failure declaration across one issue's journals (pure).

    Latest-wins by journal id, and **a declaration supersedes by EXISTING, not by being valid** —
    the invariant every issue-wide authority fact in this bounded context carries (#13490 j#85365
    F1). The newest structurally-qualifying journal is authoritative and only THEN is its content
    judged, so a malformed newer declaration SHADOWS an older valid one instead of being skipped
    so the stale one stays "latest".
    """
    latest: Optional[Tuple[int, SupersededAuditFailureFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        facts = _journal_declaration(notes or "")
        if facts is None:
            continue
        if latest is None or jint > latest[0]:
            latest = (
                jint,
                SupersededAuditFailureFacts(
                    state=facts.state,
                    journal=str(jint),
                    issue=facts.issue,
                    audit_journal=facts.audit_journal,
                    successor_issue=facts.successor_issue,
                    successor_review_journal=facts.successor_review_journal,
                    integration_branch=facts.integration_branch,
                    envelope=facts.envelope,
                ),
            )
    return latest[1] if latest is not None else SupersededAuditFailureFacts()


# ---------------------------------------------------------------------------
# The successor issue's own acknowledgement of THIS supersession shape.
# ---------------------------------------------------------------------------

#: The gate token the SUCCESSOR issue declares to acknowledge that it supersedes another issue's
#: independent-audit failure. A SEPARATE token from #14755's ``superseded_failure_successor``
#: rather than a shared one, for the same reason every authority gate here is named per surface: an
#: acknowledgement written about a failed Review Gate answers a different question than one written
#: about an audit record that never was a gate, and a record must never satisfy a contract it was
#: not written under. The field sets differ accordingly.
SUCCESSOR_ACK_GATE = "superseded_audit_failure_successor"
#: The schema version. An unknown version is REFUSED rather than interpreted under today's field
#: meanings.
SUCCESSOR_ACK_VERSION = "1"
#: The only admissible decision. A record written to DECLINE the pairing carries a different token
#: and therefore cannot corroborate anything.
SUCCESSOR_ACK_DECISION = "supersedes"

#: The COMPLETE, ORDERED field set a canonical acknowledgement carries — no more, no less, in this
#: sequence. There is deliberately NO lane envelope: the acknowledgement is about the SUCCESSOR's
#: work, whose lane is not the retire target's, so an envelope here could not be exact-matched
#: against anything the retire measures and would be a field that looks like a fence but is not.
SUCCESSOR_ACK_FIELD_ORDER: Tuple[str, ...] = (
    "gate",
    "version",
    "decision",
    "issue",
    "superseded_issue",
    "superseded_audit_journal",
    "review_journal",
)

#: No acknowledgement is in the successor's durable record at all.
ACK_NONE = "none"
#: A VALID acknowledgement: one canonical marker with every literal at its contracted value.
ACK_ACKNOWLEDGED = "acknowledged"
#: An acknowledgement is DECLARED but cannot be read as one. Fail-closed: treated exactly like
#: :data:`ACK_NONE` by every consumer, and it SUPERSEDES an older valid one.
ACK_INVALID = "invalid"

SUCCESSOR_ACK_STATES: frozenset[str] = frozenset({ACK_NONE, ACK_ACKNOWLEDGED, ACK_INVALID})


@dataclass(frozen=True)
class AuditSupersessionAcknowledgementFacts:
    """The LATEST audit-supersession acknowledgement in one SUCCESSOR issue's journals.

    ``state`` is a closed :data:`SUCCESSOR_ACK_STATES` token; ``journal`` is where it was recorded.
    The identity fields are projected from the canonical marker and are EMPTY unless the
    acknowledgement is valid — never guessed from prose, and never completed from whatever the
    caller happened to ask about.
    """

    state: str = ACK_NONE
    journal: str = ""
    issue: str = ""
    superseded_issue: str = ""
    superseded_audit_journal: str = ""
    review_journal: str = ""

    @property
    def recorded(self) -> bool:
        """True when any acknowledgement (valid or not) is in the successor's record."""
        return self.state != ACK_NONE

    @property
    def in_force(self) -> bool:
        """True ONLY for a VALID acknowledgement. :data:`ACK_INVALID` is False, like absent."""
        return self.state == ACK_ACKNOWLEDGED


def _journal_acknowledgement(notes: str) -> Optional[AuditSupersessionAcknowledgementFacts]:
    """The acknowledgement ONE journal declares, or ``None`` if it declares none (pure)."""
    declared, fields = one_canonical_marker(
        notes, gate=SUCCESSOR_ACK_GATE, field_order=SUCCESSOR_ACK_FIELD_ORDER
    )
    if not declared:
        return None
    if fields is None:
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    constants = {
        "gate": SUCCESSOR_ACK_GATE,
        "version": SUCCESSOR_ACK_VERSION,
        "decision": SUCCESSOR_ACK_DECISION,
    }
    if any(fields.get(key) != value for key, value in constants.items()):
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    issue = str(fields.get("issue", "") or "").strip()
    superseded_issue = str(fields.get("superseded_issue", "") or "").strip()
    superseded_audit = journal_ref(fields.get("superseded_audit_journal", ""))
    review = journal_ref(fields.get("review_journal", ""))
    if not issue or not superseded_issue or not superseded_audit or not review:
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    if issue == superseded_issue:
        # An issue cannot acknowledge that it supersedes itself; a self-referential supersession
        # orders nothing (the #14695 j#94260 shape, one level down).
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    return AuditSupersessionAcknowledgementFacts(
        state=ACK_ACKNOWLEDGED,
        issue=issue,
        superseded_issue=superseded_issue,
        superseded_audit_journal=superseded_audit,
        review_journal=review,
    )


def fold_audit_supersession_acknowledgement(
    journals: Sequence[Tuple[object, str]],
) -> AuditSupersessionAcknowledgementFacts:
    """The LATEST audit-supersession acknowledgement across the successor's journals (pure).

    Latest-wins by journal id, supersede-by-EXISTING. A successor that later withdrew the pairing
    by recording a malformed or superseding acknowledgement therefore shadows the older valid one,
    rather than being skipped so the stale one keeps corroborating.
    """
    latest: Optional[Tuple[int, AuditSupersessionAcknowledgementFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        facts = _journal_acknowledgement(notes or "")
        if facts is None:
            continue
        if latest is None or jint > latest[0]:
            latest = (
                jint,
                AuditSupersessionAcknowledgementFacts(
                    state=facts.state,
                    journal=str(jint),
                    issue=facts.issue,
                    superseded_issue=facts.superseded_issue,
                    superseded_audit_journal=facts.superseded_audit_journal,
                    review_journal=facts.review_journal,
                ),
            )
    return latest[1] if latest is not None else AuditSupersessionAcknowledgementFacts()


# ---------------------------------------------------------------------------
# The audit record the declaration points at, as the caller measured it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRecordEvidence:
    """What the SOURCE issue's own history says about the journal the declaration names.

    ``present`` is whether a journal with that id exists in the issue's history at all;
    ``declares_lifecycle_gate`` whether that journal carries a recognized ``## Gate:`` heading.

    Both defaults are the fail-closed ones: an unmeasured record is absent, and an unmeasured
    journal is assumed to be a gate — because the conjunct being asked is "this failure record is
    NOT a Review Gate", and an unread journal has not established that.
    """

    present: bool = False
    declares_lifecycle_gate: bool = True


# ---------------------------------------------------------------------------
# The terminal-retire admissibility fence.
# ---------------------------------------------------------------------------

#: No terminal declaration is in the durable record at all. This is where an ORDINARY lane lands,
#: and it is the reason ordinary development needs no separate conjunct: without a declaration
#: there is nothing for this route to admit.
REASON_NOT_RECORDED = "superseded_audit_failure_not_recorded"
#: A declaration is present but cannot be read as one.
REASON_INVALID = "invalid_superseded_audit_failure_declaration"
#: The declaration names a different issue than the one being retired.
REASON_ISSUE_MISMATCH = "superseded_audit_failure_names_another_issue"
#: The declaration's lane envelope is not the retire target's lane / workspace / generation.
REASON_LANE_MISMATCH = "superseded_audit_failure_names_another_lane_or_generation"
#: The declaration was made about a different integration branch than the retire is measuring
#: against, so its live-zero proof would be about a different question.
REASON_INTEGRATION_BRANCH_MISMATCH = "superseded_audit_failure_names_another_integration_branch"
#: The issue DOES record a formal review round. This route is for the shape that has none: a round
#: that exists belongs to the ordinary review-generation fence (approved) or to the #14755
#: superseded-failure terminal (``changes_requested``). Refusing here is what stops this route from
#: becoming a second way past a review that did happen.
REASON_REVIEW_ROUND_RECORDED = "issue_records_a_formal_review_round"
#: A recognized lifecycle gate stands at-or-after the declaration, so the lane re-opened after the
#: terminal was declared (or the record contradicts itself by recording both in one journal).
REASON_GATE_AFTER_DECLARATION = "lifecycle_gate_recorded_at_or_after_the_declaration"
#: The journal the declaration names as the audit record is not in the issue's history.
REASON_AUDIT_JOURNAL_NOT_FOUND = "declared_audit_journal_not_in_the_record"
#: That journal IS a recognized lifecycle gate. Then it is not the shape this route terminalizes,
#: and reading it here would be exactly the "keep the two facts separate" fence collapsing.
REASON_AUDIT_JOURNAL_IS_A_GATE = "declared_audit_journal_is_a_lifecycle_gate"
#: The audit record is not OLDER than the declaration. A terminal disposition points backwards at
#: a record that already exists; a forward or self reference terminalizes nothing.
REASON_AUDIT_JOURNAL_NOT_EARLIER = "declared_audit_journal_is_not_older_than_the_declaration"
#: The durable record declares repository change (a commit, a change scope, or an integration
#: disposition). Then "the lane holds nothing unintegrated" does not follow from a zero ahead-count
#: — already-merged work is also zero ahead (the #14695 boundary) — so the route refuses.
REASON_RECORD_DECLARES_CHANGE = "record_declares_repository_change"
#: The declaration names ITSELF as its successor. An issue cannot supersede its own failed audit.
REASON_SUCCESSOR_IS_SELF = "superseded_audit_failure_successor_is_the_same_issue"
#: The named successor's own record does not acknowledge superseding THIS issue and THIS audit
#: record.
REASON_SUCCESSOR_NOT_ACKNOWLEDGED = "successor_does_not_acknowledge_the_audit_supersession"

SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        REASON_NOT_RECORDED,
        REASON_INVALID,
        REASON_ISSUE_MISMATCH,
        REASON_LANE_MISMATCH,
        REASON_INTEGRATION_BRANCH_MISMATCH,
        REASON_INTEGRATION_BRANCH_NOT_COMMITTED,
        REASON_REVIEW_ROUND_RECORDED,
        REASON_CLOSE_NOT_RECORDED,
        REASON_CALLBACK_OWED,
        REASON_GATE_AFTER_DECLARATION,
        REASON_AUDIT_JOURNAL_NOT_FOUND,
        REASON_AUDIT_JOURNAL_IS_A_GATE,
        REASON_AUDIT_JOURNAL_NOT_EARLIER,
        REASON_RECORD_DECLARES_CHANGE,
        REASON_SUCCESSOR_IS_SELF,
        REASON_SUCCESSOR_NOT_ACKNOWLEDGED,
        REASON_SUCCESSOR_INCOMPLETE,
        REASON_MEASURED_BRANCH_MISMATCH,
        REASON_WORKTREE_NOT_CLEAN,
        REASON_LANE_HEAD_UNMEASURED,
        REASON_POST_DECLARATION_MUTATION,
        REASON_LANE_NOT_INTEGRATED,
    }
)


def evaluate_superseded_audit_failure_admissible(
    declaration: SupersededAuditFailureFacts,
    *,
    audit: AuditRecordEvidence,
    acknowledgement: AuditSupersessionAcknowledgementFacts,
    successor: SuccessorEvidence,
    review_round_journals: Sequence[int] = (),
    latest_gate_journal: object = "",
    close_recorded: bool = False,
    zero_change_proven: bool = False,
    target_issue: str = "",
    integration_branch: str = "",
    committed_integration_branch: str = "",
    measured_branch: str = "",
    expected_workspace: str = "",
    expected_lane: str = "",
    expected_lane_generation: int = 0,
    live_head: str = "",
    live_commits_ahead: Optional[int] = None,
    worktree_clean: bool = False,
    callbacks_drained: bool = False,
) -> AdmissionResult:
    """Whether a lane whose failure was recorded WITHOUT a Review Gate may terminally retire (pure).

    A lane with no review round at all can never satisfy
    :func:`...review_generation.evaluate_integration_admissible` — that is not a gap in the record,
    it is the truth about the lane. So rather than assert something false about it, the retire
    re-verifies at action time the facts that actually carry the safety weight:

    1. a VALID declaration about THIS issue, THIS exact lane generation, and the repository's
       COMMITTED integration branch — which is also the branch the live measurement below is taken
       against, because a caller free to name that branch could make the measurement vacuous by
       pointing it at the lane's own;
    2. the issue records NO formal review round. This is the discriminator that keeps the route
       from overlapping the ordinary fence and #14755, and it is also what makes "no approval was
       borrowed" a structural fact rather than a promise: there is no approval on this issue to
       borrow;
    3. the issue is durably CLOSED, no coordinator callback is owed, and no recognized lifecycle
       gate stands at-or-after the declaration. That last conjunct subsumes the review-round
       ordering question (a round IS a gate) and additionally catches a lane that re-opened by
       recording any other gate after the terminal was declared;
    4. the journal the declaration names as the audit record EXISTS in this issue's history, is
       NOT a recognized lifecycle gate, and is OLDER than the declaration;
    5. the durable record declares ZERO repository change;
    6. the SUCCESSOR is a different issue, its own record ACKNOWLEDGES superseding this issue and
       this audit record, and it has itself completed: its newest round is the approval the
       acknowledgement names, and it is durably closed;
    7. the live repository still agrees, and the measurement is about THIS lane: the probed branch
       is the lane the declaration names, its head is exactly the head the declaration was recorded
       against, the worktree is proven CLEAN, and it carries no commit the integration branch lacks.

    **Why (5) and (7) together are what bound this route.** The declaration cannot be
    authenticated — no record in this workspace can (ruling #14219 j#86718). What they establish is
    that there is nothing to lose: a record that declares no change AND a lane at zero commits over
    the integration branch with a clean worktree holds no unintegrated work, so admitting drains a
    process and terminalizes lifecycle metadata without integrating anything, minting any approval,
    or bypassing any review. (5) is required beside (7) because a zero ahead-count alone does not
    mean the lane produced nothing — already-merged work is also zero ahead (#14695's own boundary)
    — and merged-work-without-a-review is precisely what must never reach this terminal.

    **What this never does.** It never reads the audit record as an approval — it requires that
    record to not be a gate at all. It never lets the successor's approval stand in for this lane's
    — the successor's approval is read only as the SUCCESSOR's, and this lane is admitted on the
    terminal, not on a review. It never widens the ordinary review-generation fence, the #14539
    exemption, the #14695 waiver or the #14755 terminal: those are independent routes to the same
    boolean and none of them can weaken another.

    Every refusal is a typed :data:`SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS` token. There is no
    input that turns an unreadable, foreign, stale, re-opened, change-bearing or unintegrated
    record into an admission.
    """
    if declaration.state == DECLARATION_NONE:
        return AdmissionResult(False, REASON_NOT_RECORDED)
    if declaration.state != DECLARATION_SUPERSEDED or declaration.envelope is None:
        return AdmissionResult(False, REASON_INVALID)
    declaration_journal = _int_journal(declaration.journal)
    if declaration_journal is None:
        return AdmissionResult(False, REASON_INVALID)

    # The declaration must be ABOUT the issue being retired. Both sides must be present and equal
    # as literals — a blank on either side correlates to nothing, and durable evidence from another
    # issue must never unlock this fence (the #14539 F2 rule, measured to matter).
    issue = str(target_issue or "").strip()
    if not issue or declaration.issue != issue:
        return AdmissionResult(False, REASON_ISSUE_MISMATCH)

    # …and about this exact lane INCARNATION. The expectation is the caller's measurement of the
    # retire target's own lifecycle row, never a value the caller invented for this comparison
    # (#14539 review j#91797 F2): an identity the caller chooses fences nothing. An unresolved
    # expectation is blank / non-positive here and refuses.
    envelope = declaration.envelope
    if (
        not str(expected_workspace or "").strip()
        or not str(expected_lane or "").strip()
        or not isinstance(expected_lane_generation, int)
        or isinstance(expected_lane_generation, bool)
        or expected_lane_generation <= 0
        or envelope.workspace != str(expected_workspace).strip()
        or envelope.lane != str(expected_lane).strip()
        or envelope.lane_generation != expected_lane_generation
    ):
        return AdmissionResult(False, REASON_LANE_MISMATCH)

    # The integration branch is asked TWICE, against two independent expectations, for the reason
    # #14755 states at length: the live-zero conjunct is measured against it, and a caller who
    # chooses it freely can make that measurement vacuous by pointing it at the lane's own branch.
    # So it must be the repository's COMMITTED policy — a value the invoker does not choose — and
    # the declaration must have been made about that same branch. What this bounds is the ARGV
    # freedom (accidental or convenient misdirection); it is not proof against someone with write
    # access to the lane checkout, and claiming otherwise would repeat the overclaim #14695 review
    # j#93776 finding 1 refuted.
    branch = str(integration_branch or "").strip()
    committed = str(committed_integration_branch or "").strip()
    if not branch or not committed or branch != committed:
        return AdmissionResult(False, REASON_INTEGRATION_BRANCH_NOT_COMMITTED)
    if declaration.integration_branch != branch:
        return AdmissionResult(False, REASON_INTEGRATION_BRANCH_MISMATCH)

    # THE DISCRIMINATOR, reported before the lifecycle conjuncts so an operator on the wrong route
    # learns that first rather than being sent to fix a Close that is not the problem.
    if tuple(review_round_journals or ()):
        return AdmissionResult(False, REASON_REVIEW_ROUND_RECORDED)

    if not close_recorded:
        return AdmissionResult(False, REASON_CLOSE_NOT_RECORDED)
    if not callbacks_drained:
        return AdmissionResult(False, REASON_CALLBACK_OWED)

    # No recognized lifecycle gate at-or-after the declaration. The tie is the STRICT reading, the
    # same one #14755's ``declaration_current`` names explicitly: one journal carrying both a
    # terminal declaration and a lifecycle gate claims "this lane is finished forever" and "here is
    # a lifecycle event" in the same breath, nothing orders the two, so the fail-closed reading is
    # that the gate stands. In practice this also fixes the write order: the declaration is
    # recorded AFTER the Close, in its own journal.
    latest_gate = _int_journal(latest_gate_journal)
    if latest_gate is None or latest_gate >= declaration_journal:
        return AdmissionResult(False, REASON_GATE_AFTER_DECLARATION)

    # The audit record itself, re-read from the issue's own history rather than taken from the
    # declaration. The declaration says WHICH journal; the history says whether it is there and
    # what shape it is.
    if not audit.present:
        return AdmissionResult(False, REASON_AUDIT_JOURNAL_NOT_FOUND)
    if audit.declares_lifecycle_gate:
        return AdmissionResult(False, REASON_AUDIT_JOURNAL_IS_A_GATE)
    audit_journal = _int_journal(declaration.audit_journal)
    if audit_journal is None or audit_journal >= declaration_journal:
        return AdmissionResult(False, REASON_AUDIT_JOURNAL_NOT_EARLIER)

    if not zero_change_proven:
        return AdmissionResult(False, REASON_RECORD_DECLARES_CHANGE)

    if declaration.successor_issue == issue:
        return AdmissionResult(False, REASON_SUCCESSOR_IS_SELF)

    # The successor's own acknowledgement — the half the source issue cannot write for itself
    # without also writing into the successor's record. Every identity must line up in both
    # directions: the acknowledgement is ON the named successor, it names THIS issue, it names THIS
    # audit record, and it names the successor review the declaration points at.
    if (
        not acknowledgement.in_force
        or acknowledgement.issue != declaration.successor_issue
        or acknowledgement.superseded_issue != issue
        or acknowledgement.superseded_audit_journal != declaration.audit_journal
        or acknowledgement.review_journal != declaration.successor_review_journal
    ):
        return AdmissionResult(False, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    # …and the successor actually succeeded. Measured from the successor's OWN journals with the
    # same grammar every other consumer uses, so "approved" here means what it means everywhere.
    if (
        journal_ref(successor.review_journal) != declaration.successor_review_journal
        or str(successor.review_gate or "").strip() != GATE_REVIEW
        or str(successor.review_conclusion or "").strip() != REVIEW_APPROVED
        or not successor.close_recorded
    ):
        return AdmissionResult(False, REASON_SUCCESSOR_INCOMPLETE)

    # The live half must be about THIS lane's checkout. ``measure_lane_change`` already refuses a
    # checkout that is not on the branch it was told to measure, but which branch that is stays a
    # caller choice — and when the lane head is already the integration head (this reproduction's
    # own shape, a lane that never committed) ANY checkout sitting there would satisfy every live
    # conjunct. Binding the measured branch to the declared lane is what makes "the head did not
    # move" a statement about the lane rather than about whatever checkout was pointed at.
    if not str(measured_branch or "").strip() or str(measured_branch).strip() != envelope.lane:
        return AdmissionResult(False, REASON_MEASURED_BRANCH_MISMATCH)

    if not worktree_clean:
        return AdmissionResult(False, REASON_WORKTREE_NOT_CLEAN)

    head = str(live_head or "").strip().lower()
    if not head or live_commits_ahead is None:
        return AdmissionResult(False, REASON_LANE_HEAD_UNMEASURED)
    if head != envelope.head.strip().lower():
        return AdmissionResult(False, REASON_POST_DECLARATION_MUTATION)
    if not isinstance(live_commits_ahead, int) or isinstance(live_commits_ahead, bool):
        return AdmissionResult(False, REASON_LANE_HEAD_UNMEASURED)
    if live_commits_ahead != 0:
        return AdmissionResult(False, REASON_LANE_NOT_INTEGRATED)

    return AdmissionResult(True, REASON_OK)


# ---------------------------------------------------------------------------
# The producers. A contract nobody can write is a contract nobody will use.
# ---------------------------------------------------------------------------


def render_superseded_audit_failure_marker(
    *,
    issue: str,
    audit_journal: object,
    successor_issue: str,
    successor_review_journal: object,
    integration_branch: str,
    workspace: str,
    lane: str,
    lane_generation: object,
    head: str,
) -> str:
    """The exact marker a valid audit-failure terminal declaration must carry (pure).

    Field order is :data:`SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER`, so what this emits is what the
    strict reader accepts, by construction.

    Every producer error raises ``ValueError`` rather than being written. A renderer that accepts
    what its own parser refuses is not a strict grammar: it produces durable records that read back
    as a typed zero, so the authority silently does not count. The envelope's own
    :func:`...hibernate_evidence_envelope.render_lane_envelope` enforces the workspace / lane /
    generation / head rules and the marker-separator rejection; this adds the identities.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
        reject_marker_separator,
        render_lane_envelope,
    )

    issue_s = str(issue or "").strip()
    successor_s = str(successor_issue or "").strip()
    branch_s = str(integration_branch or "").strip()
    if not issue_s or not successor_s:
        raise ValueError(
            "an audit-failure terminal requires a non-empty issue and successor_issue"
        )
    if issue_s == successor_s:
        raise ValueError("an issue cannot supersede its own independent-audit failure")
    if not branch_s:
        raise ValueError("an audit-failure terminal requires the integration branch")
    for value, field in (
        (issue_s, "issue"),
        (successor_s, "successor_issue"),
        (branch_s, "integration_branch"),
    ):
        reject_marker_separator(value, field=field)

    supplied = {
        "audit_journal": audit_journal,
        "successor_review_journal": successor_review_journal,
    }
    references = {field: journal_ref(raw) for field, raw in supplied.items()}
    for field, value in references.items():
        if not value:
            raise ValueError(
                f"an audit-failure terminal requires a decimal {field}, got {supplied[field]!r}"
            )

    if isinstance(lane_generation, bool):
        raise ValueError("an audit-failure terminal requires an integer lane_generation")
    try:
        generation = int(lane_generation)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            "an audit-failure terminal requires an integer lane_generation, "
            f"got {lane_generation!r}"
        ) from None
    if not str(head or "").strip():
        raise ValueError(
            "an audit-failure terminal requires the lane head it was recorded at"
        )
    envelope_body = render_lane_envelope(
        LaneEvidenceEnvelope(
            workspace=str(workspace or "").strip(),
            lane=str(lane or "").strip(),
            lane_generation=generation,
            head=str(head or "").strip(),
        )
    )
    body = ":".join(
        [
            f"gate={SUPERSEDED_AUDIT_FAILURE_GATE}",
            f"version={SUPERSEDED_AUDIT_FAILURE_VERSION}",
            f"decision={SUPERSEDED_AUDIT_FAILURE_DECISION}",
            f"issue={issue_s}",
            f"audit_journal={references['audit_journal']}",
            f"successor_issue={successor_s}",
            f"successor_review_journal={references['successor_review_journal']}",
            f"integration_branch={branch_s}",
            envelope_body,
        ]
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


def render_audit_supersession_acknowledgement_marker(
    *,
    issue: str,
    superseded_issue: str,
    superseded_audit_journal: object,
    review_journal: object,
) -> str:
    """The exact marker a valid successor acknowledgement must carry (pure).

    Field order is :data:`SUCCESSOR_ACK_FIELD_ORDER`, so what this emits is what the strict reader
    accepts, by construction. Every producer error raises ``ValueError`` rather than being written.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
        reject_marker_separator,
    )

    issue_s = str(issue or "").strip()
    superseded_s = str(superseded_issue or "").strip()
    if not issue_s or not superseded_s:
        raise ValueError(
            "an audit-supersession acknowledgement requires a non-empty issue and superseded_issue"
        )
    if issue_s == superseded_s:
        raise ValueError("an issue cannot acknowledge that it supersedes itself")
    reject_marker_separator(issue_s, field="issue")
    reject_marker_separator(superseded_s, field="superseded_issue")
    superseded_audit = journal_ref(superseded_audit_journal)
    review = journal_ref(review_journal)
    if not superseded_audit:
        raise ValueError(
            "an audit-supersession acknowledgement requires the superseded audit journal id, got "
            f"{superseded_audit_journal!r}"
        )
    if not review:
        raise ValueError(
            "an audit-supersession acknowledgement requires its own approved review journal id, "
            f"got {review_journal!r}"
        )
    body = ":".join(
        [
            f"gate={SUCCESSOR_ACK_GATE}",
            f"version={SUCCESSOR_ACK_VERSION}",
            f"decision={SUCCESSOR_ACK_DECISION}",
            f"issue={issue_s}",
            f"superseded_issue={superseded_s}",
            f"superseded_audit_journal={superseded_audit}",
            f"review_journal={review}",
        ]
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


__all__ = (
    "ACK_ACKNOWLEDGED",
    "ACK_INVALID",
    "ACK_NONE",
    "AuditRecordEvidence",
    "AuditSupersessionAcknowledgementFacts",
    "DECLARATION_INVALID",
    "DECLARATION_NONE",
    "DECLARATION_SUPERSEDED",
    "REASON_AUDIT_JOURNAL_IS_A_GATE",
    "REASON_AUDIT_JOURNAL_NOT_EARLIER",
    "REASON_AUDIT_JOURNAL_NOT_FOUND",
    "REASON_GATE_AFTER_DECLARATION",
    "REASON_INTEGRATION_BRANCH_MISMATCH",
    "REASON_INVALID",
    "REASON_ISSUE_MISMATCH",
    "REASON_LANE_MISMATCH",
    "REASON_NOT_RECORDED",
    "REASON_RECORD_DECLARES_CHANGE",
    "REASON_REVIEW_ROUND_RECORDED",
    "REASON_SUCCESSOR_IS_SELF",
    "REASON_SUCCESSOR_NOT_ACKNOWLEDGED",
    "SUCCESSOR_ACK_DECISION",
    "SUCCESSOR_ACK_FIELD_ORDER",
    "SUCCESSOR_ACK_GATE",
    "SUCCESSOR_ACK_STATES",
    "SUCCESSOR_ACK_VERSION",
    "SUPERSEDED_AUDIT_FAILURE_DECISION",
    "SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER",
    "SUPERSEDED_AUDIT_FAILURE_GATE",
    "SUPERSEDED_AUDIT_FAILURE_REFUSAL_REASONS",
    "SUPERSEDED_AUDIT_FAILURE_STATES",
    "SUPERSEDED_AUDIT_FAILURE_VERSION",
    "SupersededAuditFailureFacts",
    "evaluate_superseded_audit_failure_admissible",
    "fold_audit_supersession_acknowledgement",
    "fold_superseded_audit_failure",
    "render_audit_supersession_acknowledgement_marker",
    "render_superseded_audit_failure_marker",
)
