"""Terminalizing a SUPERSEDED FAILURE round without turning it into an approval (#14755).

#14577 is the reproduction. Its Review j#93648 concluded ``changes_requested``; both findings were
verified and ACCEPTED (j#93653 / j#93656); the acceptance target the round failed to reach was
obtained instead by a successor issue, #14697, whose own Review j#93727 concluded ``approved``;
the lane was then task_closed as a superseded failure (j#93757) with its head already reachable
from ``origin/main-next`` and zero commits of its own. The standard ``sublane retire`` still
refuses, permanently, with ``stale_review_generation`` (measured three times: j#93759, j#94006,
j#94319) — because the only durable authority that fence reads is a REVIEW GENERATION, and this
lane's latest generation is a failure that will never be approved.

The three escapes that were correctly refused there are why this module exists:

- asserting ``--latest-generation-admissible`` about a round that concluded ``changes_requested``
  is a FALSE assert;
- borrowing the SUCCESSOR's approval for this lane makes one issue's review answer another's;
- re-reading the failure as an approval is exactly the "exemption を Review Gate approval または
  自己 review と表現しない" the central preset forbids.

So the failed round is terminalized AS a failure. **Nothing in this module ever reads a
``changes_requested`` conclusion as anything else** — on the contrary, it REQUIRES the latest
round to have failed (:data:`REASON_ROUND_DID_NOT_FAIL`): an approved round belongs to the
ordinary review-generation fence, and this route must not become a second way to pass it.

**What this route rests on, stated in one place.** Every conjunct below is either measured from
live git or folded from a durable record, and the declaration marker is a POINTER, never a
substitute: it names WHICH review round, WHICH verdict gate and WHICH successor, and each of
those is then re-read from the record itself. That is the structural difference from the #14695
no-change waiver, which had to REFUSE everything (see
``no_change_review_waiver.WRITER_AUTHORITY_RESOLVABLE``): there the waived fact — "the owner
decided no independent review was owed" — existed nowhere except inside the marker, so an
unauthenticatable writer was the whole authority. Here the facts exist independently of the
declaration, and the declaration only correlates them.

**And what it does NOT establish.** This workspace cannot authenticate a journal's writer: every
role posts under one source-system account (ruling #14219 j#86718) and
:func:`...hibernate_issuer_policy.resolve_journal_issuer` is a POLICY binding that takes no author
input and says so. This module therefore makes NO issuer claim and deliberately does not register
its gate in :mod:`.hibernate_evidence_authority` — a gate registered there needs a ruling that
NAMES it, and manufacturing an anchor from a record that decided no writer contract is precisely
the #14661 j#92715 defect (``is_anchored`` passes while pointing at a record that could not have
decided the binding). What bounds the consequence instead is the LIVE-ZERO conjunct: this route
can only admit a lane that carries zero commits over the integration branch with a clean
worktree, so admitting never integrates work, never mints an approval, and never drains a lane
holding unintegrated change. Whether that bound is the right one is an open adjudication item and
is raised as such rather than assumed.

Boundary: pure. No IO, no Redmine, no git. The live repository half and the successor issue's own
gate facts are necessarily measured by the application layer and arrive here as parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    EnvelopeParseError,
    LaneEvidenceEnvelope,
    parse_lane_envelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.no_change_review_waiver import (  # noqa: E501
    review_round_supersedes,
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
    REVIEW_CHANGES_REQUESTED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
    FindingVerdictFacts,
    SuccessorAcknowledgementFacts,
    journal_ref,
    one_canonical_marker,
)

#: The gate token this authority is declared under. Named per surface, like every other authority
#: gate in this context: a terminal for one failed round can never be read as a terminal for
#: another. Like ``codex_direct_edit`` and ``no_change_review_waiver`` it is an issue-wide,
#: latest-wins AUTHORITY fact and deliberately NOT a lifecycle gate — it is not added to the
#: glance grammar's heading allowlist, because the lane's lifecycle already recorded what happened
#: (a review that failed, then a Close) and this declares what to DO about it.
SUPERSEDED_FAILURE_GATE = "superseded_failure"

#: The schema version. A marker of an unknown version is REFUSED rather than interpreted under
#: today's field meanings — the next version may give an existing field a different weight.
SUPERSEDED_FAILURE_VERSION = "1"

#: The only admissible decision. A record written to DECLINE the terminal carries a different
#: token and therefore cannot admit anything.
SUPERSEDED_FAILURE_DECISION = "superseded"

#: The COMPLETE, ORDERED field set a canonical declaration carries — no more, no less, in this
#: sequence. An unknown / missing / permuted field is a marker the canonical producer could not
#: have rendered, and tolerating one is how a future meaningful field gets ignored by an old
#: verifier (the #14661 j#92533 F2 / j#92601 F2 lesson).
#:
#: ``integration_branch`` is part of the declaration for the same reason the #14539 exemption
#: route compares it: the live "carries no commit over the integration branch" measurement is
#: taken against whatever ``--integration-branch`` names, so a declaration made about
#: ``main-next`` must not be replayable against a branch chosen to make the count zero.
SUPERSEDED_FAILURE_FIELD_ORDER: Tuple[str, ...] = (
    "gate",
    "version",
    "decision",
    "issue",
    "review_journal",
    "verdict_journal",
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

#: No terminal declaration is in the durable record at all — the ordinary review path applies.
#: This is what an ORDINARY ``changes_requested`` lane in active development folds to, which is
#: why "通常の changes_requested 開発" needs no separate refusal: it never enters this route.
DECLARATION_NONE = "none"
#: A VALID declaration: one canonical marker, every constant field at its literal value, a
#: parseable lane envelope, and readable journal references.
DECLARATION_SUPERSEDED = "superseded_state"
#: A declaration is present but cannot be read as one. Fail-closed: treated exactly like
#: :data:`DECLARATION_NONE` by every consumer, and it SUPERSEDES an older valid declaration.
DECLARATION_INVALID = "invalid"

SUPERSEDED_FAILURE_STATES: frozenset[str] = frozenset(
    {DECLARATION_NONE, DECLARATION_SUPERSEDED, DECLARATION_INVALID}
)


@dataclass(frozen=True)
class SupersededFailureFacts:
    """The LATEST durable superseded-failure terminal declaration for one issue.

    ``state`` is a closed :data:`SUPERSEDED_FAILURE_STATES` token; ``journal`` is where the
    declaration was recorded. Every other field is projected from the canonical marker and is
    EMPTY / ``None`` unless the declaration is valid — never guessed from prose, and never
    completed from the lane's current lifecycle row (that is precisely how a superseded
    generation's evidence gets promoted onto the live one).
    """

    state: str = DECLARATION_NONE
    journal: str = ""
    issue: str = ""
    review_journal: str = ""
    verdict_journal: str = ""
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


def _journal_declaration(notes: str) -> Optional[SupersededFailureFacts]:
    """The declaration ONE journal makes, or ``None`` if it makes none (pure).

    Qualification, location, quote-exclusion and parsing are ONE authority
    (:func:`...superseded_failure_correlation.one_canonical_marker`), so a quoted marker earlier
    in the note can never be substituted for the canonical one.
    """
    declared, fields = one_canonical_marker(
        notes, gate=SUPERSEDED_FAILURE_GATE, field_order=SUPERSEDED_FAILURE_FIELD_ORDER
    )
    if not declared:
        return None
    if fields is None:
        return SupersededFailureFacts(state=DECLARATION_INVALID)

    constants = {
        "gate": SUPERSEDED_FAILURE_GATE,
        "version": SUPERSEDED_FAILURE_VERSION,
        "decision": SUPERSEDED_FAILURE_DECISION,
    }
    if any(fields.get(key) != value for key, value in constants.items()):
        return SupersededFailureFacts(state=DECLARATION_INVALID)

    issue = str(fields.get("issue", "") or "").strip()
    successor_issue = str(fields.get("successor_issue", "") or "").strip()
    integration_branch = str(fields.get("integration_branch", "") or "").strip()
    review_journal = journal_ref(fields.get("review_journal", ""))
    verdict_journal = journal_ref(fields.get("verdict_journal", ""))
    successor_review = journal_ref(fields.get("successor_review_journal", ""))
    if not all(
        (issue, successor_issue, integration_branch, review_journal, verdict_journal, successor_review)
    ):
        return SupersededFailureFacts(state=DECLARATION_INVALID)

    # The ONE envelope grammar (never a second copy): non-empty workspace / lane, a POSITIVE
    # generation, and a full lowercase 40/64-hex head. ``require_head=True`` because the head is
    # what makes post-declaration mutation detectable — a declaration with no head pins nothing.
    envelope = parse_lane_envelope(fields, require_head=True)
    if isinstance(envelope, EnvelopeParseError):
        return SupersededFailureFacts(state=DECLARATION_INVALID)

    return SupersededFailureFacts(
        state=DECLARATION_SUPERSEDED,
        issue=issue,
        review_journal=review_journal,
        verdict_journal=verdict_journal,
        successor_issue=successor_issue,
        successor_review_journal=successor_review,
        integration_branch=integration_branch,
        envelope=envelope,
    )


def fold_superseded_failure(
    journals: Sequence[Tuple[object, str]],
) -> SupersededFailureFacts:
    """The LATEST durable superseded-failure declaration across one issue's journals (pure).

    Latest-wins by journal id, and **a declaration supersedes by EXISTING, not by being valid** —
    the invariant :func:`...review_exemption.fold_review_exemption` and
    :func:`...no_change_review_waiver.fold_no_change_review_waiver` both carry (#13490 j#85365
    F1). The newest structurally-qualifying journal is authoritative and only THEN is its content
    judged, so a malformed newer declaration SHADOWS an older valid one instead of being skipped
    so the stale one stays "latest".
    """
    latest: Optional[Tuple[int, SupersededFailureFacts]] = None
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
                SupersededFailureFacts(
                    state=facts.state,
                    journal=str(jint),
                    issue=facts.issue,
                    review_journal=facts.review_journal,
                    verdict_journal=facts.verdict_journal,
                    successor_issue=facts.successor_issue,
                    successor_review_journal=facts.successor_review_journal,
                    integration_branch=facts.integration_branch,
                    envelope=facts.envelope,
                ),
            )
    return latest[1] if latest is not None else SupersededFailureFacts()


def declaration_current(
    declaration: SupersededFailureFacts, round_journal_ids: Sequence[int]
) -> bool:
    """Whether no review round stands at-or-after the declaration (pure).

    Uses the SHARED ordering authority
    (:func:`...no_change_review_waiver.review_round_supersedes`) rather than a second copy of
    "is a review round newer than this authority", because two copies eventually answer
    differently for the same durable record — the drift this codebase keeps paying for when a
    third authority arrives (#13952).

    **The tie policy is named explicitly, and it is the STRICT one.** ``same_journal_supersedes``
    is True here, which means a review round recorded in the SAME journal as the declaration
    supersedes it. That is the ``codex_direct_edit`` reading, not the waiver reading, and the
    question decides which is right: this authority asks whether the record CONTRADICTS itself.
    One journal carrying both a terminal declaration and a review round claims "this round is
    finished forever" and "here is a review round" in the same breath; nothing orders the two, so
    the fail-closed reading is that the round stands. #14695 review j#94260 measured the cost of
    getting this backwards in the other direction — applying the wrong tie rule turned a
    ``blocked`` terminal into ``retire_ready``, a terminal UNBLOCK — so the tie is stated at the
    call site rather than inherited.

    Ordering asks whether a declaration EXISTS and whether anything supersedes it, never whether
    it parses, so an INVALID declaration is ordered exactly like a valid one (#14695 j#93879 F2).
    """
    if not declaration.recorded:
        return False
    return not review_round_supersedes(
        declaration.journal, round_journal_ids, same_journal_supersedes=True
    )


@dataclass(frozen=True)
class SuccessorEvidence:
    """The successor issue's OWN durable facts, measured by the caller from its live record.

    ``review_journal`` is the successor's newest review round; ``review_conclusion`` and
    ``review_gate`` that round's typed identity; ``close_recorded`` whether its latest lifecycle
    gate is a Close. Every "not measured" value is the fail-closed one, because an unread
    successor cannot testify that it succeeded.

    These are folded with the SAME grammar the glance uses over the SUCCESSOR's journals — never
    re-derived here and never taken from the source issue's declaration, which is the whole point:
    a declaration that could supply its own successor evidence would be certifying itself.
    """

    review_journal: str = ""
    review_gate: str = ""
    review_conclusion: str = ""
    close_recorded: bool = False


# ---------------------------------------------------------------------------
# The terminal-retire admissibility fence for a superseded failure round.
# ---------------------------------------------------------------------------

#: No terminal declaration is in the durable record at all. This is where an ORDINARY lane in
#: ``changes_requested`` development lands, and it is the reason that case needs no separate
#: conjunct: without a declaration there is nothing for this route to admit.
REASON_NOT_RECORDED = "superseded_failure_not_recorded"
#: A declaration is present but cannot be read as one.
REASON_INVALID = "invalid_superseded_failure_declaration"
#: A review round stands at-or-after the declaration, so the lane re-opened (or the record
#: contradicts itself by recording both in one journal).
REASON_SUPERSEDED_BY_NEWER_ROUND = "superseded_failure_superseded_by_newer_review_round"
#: The declaration names a different issue than the one being retired.
REASON_ISSUE_MISMATCH = "superseded_failure_names_another_issue"
#: The declaration's lane envelope is not the retire target's lane / workspace / generation.
REASON_LANE_MISMATCH = "superseded_failure_names_another_lane_or_generation"
#: The declaration was made about a different integration branch than the retire is measuring
#: against, so its live-zero proof would be about a different question.
REASON_INTEGRATION_BRANCH_MISMATCH = "superseded_failure_names_another_integration_branch"
#: The branch the live-zero is being measured against is not the repository's COMMITTED
#: integration branch. Distinct from the above because the remedy differs: that one says the
#: declaration is stale, this one says the retire is pointed somewhere it may not point.
REASON_INTEGRATION_BRANCH_NOT_COMMITTED = "integration_branch_is_not_the_committed_policy"
#: The issue is not durably closed, so there is no terminal disposition to re-verify. This is the
#: "open issue" the acceptance refuses with zero write.
REASON_CLOSE_NOT_RECORDED = "close_not_recorded"
#: A coordinator callback is still owed, so the lane's own workflow has not converged.
REASON_CALLBACK_OWED = "coordinator_callback_still_owed"
#: The declaration does not name the record's NEWEST review round, so it is about a round the
#: history has already moved past.
REASON_ROUND_MISMATCH = "superseded_failure_names_another_review_round"
#: The newest review round did not FAIL. An approved round is the ordinary review-generation
#: fence's business, and an unanswered request is a review still owed; neither is terminalizable
#: as a superseded failure.
REASON_ROUND_DID_NOT_FAIL = "latest_review_round_did_not_conclude_changes_requested"
#: A finding of the failed round is not accepted — disputed, blocked, unfilled, unreadable, or
#: recorded against another round. The concrete
#: :data:`...superseded_failure_correlation.FINDING_VERDICT_REASONS` token is carried in detail.
REASON_FINDINGS_NOT_ACCEPTED = "review_findings_not_accepted"
#: The declaration names ITSELF as its successor. An issue cannot supersede its own failed round.
REASON_SUCCESSOR_IS_SELF = "superseded_failure_successor_is_the_same_issue"
#: The named successor's own record does not acknowledge superseding THIS issue and THIS round.
REASON_SUCCESSOR_NOT_ACKNOWLEDGED = "successor_does_not_acknowledge_the_supersession"
#: The successor exists and acknowledges the pairing, but has not itself completed: its newest
#: review round is not an approval, it is not the round the acknowledgement names, or it is not
#: durably closed. This is the "successor 未完" the acceptance refuses with zero write.
REASON_SUCCESSOR_INCOMPLETE = "successor_evidence_incomplete"
#: The branch the live half was measured on is not the lane the declaration is about. Without
#: this, ``--branch`` / ``--worktree`` could point at ANY checkout sitting on the declaration's
#: head — the coordinator's own repo, when the lane head is already the integration head, which is
#: exactly the reproduction's shape — and a lane that had since moved would measure as unmoved.
REASON_MEASURED_BRANCH_MISMATCH = "live_measurement_is_not_about_the_declared_lane"
#: The lane worktree is dirty, or could not be read. Uncommitted change is repository change no
#: terminal disposition covered, and an unreadable checkout cannot testify that there is none.
REASON_WORKTREE_NOT_CLEAN = "lane_worktree_not_proven_clean"
#: The live lane head could not be measured, so post-declaration mutation could not be ruled out.
REASON_LANE_HEAD_UNMEASURED = "lane_head_not_measured"
#: The live lane head is not the head the declaration was recorded against.
REASON_POST_DECLARATION_MUTATION = "lane_head_moved_after_the_declaration"
#: The lane carries commits the integration branch does not. This is the "未統合" the acceptance
#: refuses, and it is the conjunct that BOUNDS what admitting can cost: a lane holding
#: unintegrated work never reaches the terminal.
REASON_LANE_NOT_INTEGRATED = "lane_carries_commits_over_the_integration_branch"

SUPERSEDED_FAILURE_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        REASON_NOT_RECORDED,
        REASON_INVALID,
        REASON_SUPERSEDED_BY_NEWER_ROUND,
        REASON_ISSUE_MISMATCH,
        REASON_LANE_MISMATCH,
        REASON_INTEGRATION_BRANCH_MISMATCH,
        REASON_INTEGRATION_BRANCH_NOT_COMMITTED,
        REASON_CLOSE_NOT_RECORDED,
        REASON_CALLBACK_OWED,
        REASON_ROUND_MISMATCH,
        REASON_ROUND_DID_NOT_FAIL,
        REASON_FINDINGS_NOT_ACCEPTED,
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


def evaluate_superseded_failure_admissible(
    declaration: SupersededFailureFacts,
    *,
    currently_current: bool,
    verdicts: FindingVerdictFacts,
    acknowledgement: SuccessorAcknowledgementFacts,
    successor: SuccessorEvidence,
    latest_round_journal: str,
    latest_round_gate: str,
    latest_round_conclusion: str,
    close_recorded: bool,
    target_issue: str,
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
    """Whether a SUPERSEDED FAILURE lane may pass the terminal retire's fence (pure).

    A lane whose latest review generation FAILED can never satisfy
    :func:`...review_generation.evaluate_integration_admissible` — that is not a gap in the
    record, it is the truth about the round. So rather than assert something false about it, the
    retire re-verifies at action time the facts that actually carry the same safety weight:

    1. a VALID declaration that is CURRENT — no review round stands at-or-after it;
    2. that declaration is about THIS issue, THIS exact lane generation, and the repository's
       COMMITTED integration branch — which is also the branch the live measurement below is
       taken against, because a caller free to name that branch could make the measurement
       vacuous by pointing it at the lane's own;
    3. the issue is durably CLOSED and no coordinator callback is still owed;
    4. the record's NEWEST review round is the one the declaration names, and it concluded
       ``changes_requested``. Both halves matter: naming an older round would terminalize a
       history the lane has moved past, and an APPROVED round belongs to the ordinary fence;
    5. every finding of that round has an ``accepted`` verdict in the governed
       ``review_finding_verdict`` record — the "未受領 finding" conjunct;
    6. the SUCCESSOR is a different issue, its own record ACKNOWLEDGES superseding this issue and
       this round, and it has itself completed: its newest round is the approval the
       acknowledgement names, and it is durably closed;
    7. the live repository still agrees, and the measurement is about THIS lane: the probed
       branch is the lane the declaration names, its head is exactly the head the declaration was
       recorded against, the worktree is proven CLEAN, and it carries no commit the integration
       branch lacks.

    **Why (7) is what bounds this route.** The declaration cannot be authenticated — no record in
    this workspace can (ruling #14219 j#86718). What (7) establishes is that there is nothing to
    lose: a lane with zero commits over the integration branch and a clean worktree holds no
    unintegrated work, so admitting drains a process and terminalizes lifecycle metadata without
    integrating anything, minting any approval, or bypassing any review. A lane still holding
    unreviewed work fails (7) and never reaches the terminal. This is stated as the route's
    premise rather than buried, because it is the premise a reviewer must accept or reject.

    **What this never does.** It never reads ``changes_requested`` as approved; it never lets the
    successor's approval stand in for this lane's; it never widens the ordinary review-generation
    fence, the #14539 exemption, or the #14695 waiver — those are independent routes to the same
    boolean and none of them can weaken another.

    Every refusal is a typed :data:`SUPERSEDED_FAILURE_REFUSAL_REASONS` token. There is no input
    that turns an unreadable, foreign, stale, re-opened or unintegrated record into an admission.
    """
    if declaration.state == DECLARATION_NONE:
        return AdmissionResult(False, REASON_NOT_RECORDED)
    if declaration.state != DECLARATION_SUPERSEDED or declaration.envelope is None:
        return AdmissionResult(False, REASON_INVALID)
    if not currently_current:
        return AdmissionResult(False, REASON_SUPERSEDED_BY_NEWER_ROUND)

    # The declaration must be ABOUT the issue being retired. Both sides must be present and equal
    # as literals — a blank on either side correlates to nothing, and durable evidence from
    # another issue must never unlock this fence (the #14539 F2 rule, measured to matter).
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

    # The integration branch is asked TWICE, against two independent expectations, because the
    # live-zero conjunct below is measured against it and a caller who chooses it freely can make
    # that measurement vacuous: point ``--integration-branch`` at the lane's own branch (or at any
    # branch sitting on the lane head) and "carries 0 commits over the integration branch" is
    # trivially true. So the branch must be the repository's COMMITTED policy — a value the
    # invoker does not choose — and the declaration must have been made about that same branch.
    #
    # **What this does and does not bound, stated rather than overclaimed.** It removes the argv
    # freedom, which is what makes an accidental or convenient misdirection impossible. It is NOT
    # proof against someone with write access to the lane checkout: whatever name the config
    # declares, a ref by that name can be created at the lane head locally. Nothing readable from
    # a durable record can close that, and claiming otherwise is the "forging would be required"
    # overclaim #14695 review j#93776 finding 1 refuted. The exact committed spelling is required
    # rather than also accepting ``<remote>/<name>``, because deciding which remote qualifies is a
    # resolution rule this issue has no ruling for — so a checkout that cannot resolve the
    # committed name yields no measurement and refuses, which is an operational precondition
    # (fetch the branch into the lane checkout), not an authority gap.
    branch = str(integration_branch or "").strip()
    committed = str(committed_integration_branch or "").strip()
    if not branch or not committed or branch != committed:
        return AdmissionResult(False, REASON_INTEGRATION_BRANCH_NOT_COMMITTED)
    if declaration.integration_branch != branch:
        return AdmissionResult(False, REASON_INTEGRATION_BRANCH_MISMATCH)

    if not close_recorded:
        return AdmissionResult(False, REASON_CLOSE_NOT_RECORDED)
    if not callbacks_drained:
        return AdmissionResult(False, REASON_CALLBACK_OWED)

    # The round itself, re-read from the record rather than taken from the declaration. The
    # declaration says WHICH round; the fold says what that round WAS.
    newest_round = journal_ref(latest_round_journal)
    if not newest_round or newest_round != declaration.review_journal:
        return AdmissionResult(False, REASON_ROUND_MISMATCH)
    if (
        str(latest_round_gate or "").strip() != GATE_REVIEW
        or str(latest_round_conclusion or "").strip() != REVIEW_CHANGES_REQUESTED
    ):
        return AdmissionResult(False, REASON_ROUND_DID_NOT_FAIL)

    # The findings. The declaration also names the verdict journal, and BOTH must agree: the
    # deciding verdict gate the fold selected has to be the one the declaration points at, so a
    # declaration cannot point at a clean old verdict while a newer one disputes the same round.
    if (
        not verdicts.accepted
        or not declaration.verdict_journal
        or journal_ref(verdicts.journal) != declaration.verdict_journal
    ):
        return AdmissionResult(False, REASON_FINDINGS_NOT_ACCEPTED)

    if declaration.successor_issue == issue:
        return AdmissionResult(False, REASON_SUCCESSOR_IS_SELF)

    # The successor's own acknowledgement — the half the source issue cannot write for itself
    # without also writing into the successor's record. Every identity must line up in both
    # directions: the acknowledgement is ON the named successor, it names THIS issue, it names
    # THIS failed round, and it names the successor review the declaration points at.
    if (
        not acknowledgement.in_force
        or acknowledgement.issue != declaration.successor_issue
        or acknowledgement.superseded_issue != issue
        or acknowledgement.superseded_review_journal != declaration.review_journal
        or acknowledgement.review_journal != declaration.successor_review_journal
    ):
        return AdmissionResult(False, REASON_SUCCESSOR_NOT_ACKNOWLEDGED)

    # …and the successor actually succeeded. Measured from the successor's OWN journals with the
    # same grammar, so "approved" here means what it means everywhere else.
    if (
        journal_ref(successor.review_journal) != declaration.successor_review_journal
        or str(successor.review_gate or "").strip() != GATE_REVIEW
        or str(successor.review_conclusion or "").strip() != REVIEW_APPROVED
        or not successor.close_recorded
    ):
        return AdmissionResult(False, REASON_SUCCESSOR_INCOMPLETE)

    # The live half must be about THIS lane's checkout. ``measure_lane_change`` already refuses a
    # checkout that is not on the branch it was told to measure, but which branch that is stays a
    # caller choice — and when the lane head is already the integration head (the reproduction's
    # own shape) any checkout sitting there would satisfy every live conjunct. Binding the
    # measured branch to the declared lane is what makes "the head did not move" a statement
    # about the lane rather than about whatever checkout was pointed at.
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


def render_superseded_failure_marker(
    *,
    issue: str,
    review_journal: object,
    verdict_journal: object,
    successor_issue: str,
    successor_review_journal: object,
    integration_branch: str,
    workspace: str,
    lane: str,
    lane_generation: object,
    head: str,
) -> str:
    """The exact marker a valid superseded-failure declaration must carry (pure).

    Rendered so the coordinator recording a terminal can see precisely what to write — an
    authority contract nobody can produce is an authority contract nobody will use. Field order is
    :data:`SUPERSEDED_FAILURE_FIELD_ORDER`, so what this emits is what the strict reader accepts,
    by construction.

    Every producer error raises ``ValueError`` rather than being written. A renderer that accepts
    what its own parser refuses is not a strict grammar: it produces durable records that read
    back as a typed zero, so the authority silently does not count. The envelope's own
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
            "a superseded-failure terminal requires a non-empty issue and successor_issue"
        )
    if issue_s == successor_s:
        raise ValueError("an issue cannot supersede its own failed review round")
    if not branch_s:
        raise ValueError("a superseded-failure terminal requires the integration branch")
    for value, field in (
        (issue_s, "issue"),
        (successor_s, "successor_issue"),
        (branch_s, "integration_branch"),
    ):
        reject_marker_separator(value, field=field)

    supplied = {
        "review_journal": review_journal,
        "verdict_journal": verdict_journal,
        "successor_review_journal": successor_review_journal,
    }
    references = {field: journal_ref(raw) for field, raw in supplied.items()}
    for field, value in references.items():
        if not value:
            raise ValueError(
                f"a superseded-failure terminal requires a decimal {field}, got "
                f"{supplied[field]!r}"
            )

    try:
        generation = int(lane_generation)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            "a superseded-failure terminal requires an integer lane_generation, "
            f"got {lane_generation!r}"
        ) from None
    if isinstance(lane_generation, bool):
        raise ValueError(
            "a superseded-failure terminal requires an integer lane_generation"
        )
    if not str(head or "").strip():
        raise ValueError(
            "a superseded-failure terminal requires the lane head it was recorded at"
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
            f"gate={SUPERSEDED_FAILURE_GATE}",
            f"version={SUPERSEDED_FAILURE_VERSION}",
            f"decision={SUPERSEDED_FAILURE_DECISION}",
            f"issue={issue_s}",
            f"review_journal={references['review_journal']}",
            f"verdict_journal={references['verdict_journal']}",
            f"successor_issue={successor_s}",
            f"successor_review_journal={references['successor_review_journal']}",
            f"integration_branch={branch_s}",
            envelope_body,
        ]
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


__all__ = (
    "DECLARATION_INVALID",
    "DECLARATION_NONE",
    "DECLARATION_SUPERSEDED",
    "REASON_CALLBACK_OWED",
    "REASON_CLOSE_NOT_RECORDED",
    "REASON_FINDINGS_NOT_ACCEPTED",
    "REASON_INTEGRATION_BRANCH_MISMATCH",
    "REASON_INTEGRATION_BRANCH_NOT_COMMITTED",
    "REASON_INVALID",
    "REASON_ISSUE_MISMATCH",
    "REASON_LANE_HEAD_UNMEASURED",
    "REASON_LANE_MISMATCH",
    "REASON_LANE_NOT_INTEGRATED",
    "REASON_MEASURED_BRANCH_MISMATCH",
    "REASON_NOT_RECORDED",
    "REASON_POST_DECLARATION_MUTATION",
    "REASON_ROUND_DID_NOT_FAIL",
    "REASON_ROUND_MISMATCH",
    "REASON_SUCCESSOR_INCOMPLETE",
    "REASON_SUCCESSOR_IS_SELF",
    "REASON_SUCCESSOR_NOT_ACKNOWLEDGED",
    "REASON_SUPERSEDED_BY_NEWER_ROUND",
    "REASON_WORKTREE_NOT_CLEAN",
    "SUPERSEDED_FAILURE_DECISION",
    "SUPERSEDED_FAILURE_FIELD_ORDER",
    "SUPERSEDED_FAILURE_GATE",
    "SUPERSEDED_FAILURE_REFUSAL_REASONS",
    "SUPERSEDED_FAILURE_STATES",
    "SUPERSEDED_FAILURE_VERSION",
    "SupersededFailureFacts",
    "SuccessorEvidence",
    "declaration_current",
    "evaluate_superseded_failure_admissible",
    "fold_superseded_failure",
    "render_superseded_failure_marker",
)
