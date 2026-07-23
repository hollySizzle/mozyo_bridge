"""Durable evidence → T1 basis conjuncts (Redmine #14219 T2b, step 4).

Steps 1–3 built the marker grammars; this is the per-conjunct PRODUCER that turns an issue's durable
journals into the :class:`BasisConjunct` values T1 classifies. It is pure: journals in, typed
conjuncts + typed gaps out, no I/O and no actuation.

Three invariants make this safe, and each of them is a defect this project already paid for:

* **The producer never sees the target lane or head.** It transcribes the identity the EVIDENCE
  declares (``bound_workspace`` / ``bound_lane`` / ``bound_generation`` / ``bound_head``) and lets
  :func:`classify_hibernate_candidate` compare that against the candidate anchor. A producer handed
  the target could "bind" evidence to it and the anchor check would become a tautology.
* **Latest declaration wins, and it wins by EXISTING, not by being valid** (the #14213 F1 /
  #13952 F3 invariant). For each evidence gate, only the highest-numbered journal that DECLARES it
  is read. So a newer ``changes_requested`` shadows an older ``approved``, and a newer LEGACY
  (lane-unbound) marker shadows an older enveloped one — yielding a typed gap rather than letting
  the stale enveloped record survive as current evidence. For the integration disposition,
  "declares" is the glance's own rule (:func:`fold_integration_disposition` — an
  integration-disposition HEADING counts, not only a marker), so a coordinator's newer heading-form
  deferral cannot be invisible to this producer while being visible to the glance. The review
  conclusion stays marker-only: the review-generation contract (#13974) requires the marker and
  forbids reading a conclusion out of prose.
* **Readable-but-negative keeps its own reason.** A review whose conclusion is explicitly not
  ``approved``, and a push observation that is not reachable, produce an UNSATISFIED conjunct (T1:
  ``basis_unsatisfied``) — the evidence is legible and negative. Everything else that cannot become
  a conjunct is a typed GAP (T1: ``basis_partially_unknown``), and the gap carries the SPECIFIC
  reason: ``integration_not_integrated`` (a durable deferral) and ``evidence_ci_not_success`` (a CI
  record that did not conclude success) are distinguishable from ``evidence_absent`` in the durable
  record. Every one of them is zero-actuation; none of them is a lenient pass.

``commits_pushed`` has no durable marker by design (ruling j#85530 Q3): it is an action-time git
remote observation, supplied here as :class:`PushObservation` and transcribed with the same rules.

Three further checks close the gap between "a marker of the right shape exists" and "the authority
actually attested this" (checkpoint review j#86389 F1–F3). A marker is a claim; each of these binds
the claim to something outside the marker that can be audited:

* **Who wrote it** (F2). The authority provenance comes from the record's WRITER — the
  ``issuer_role`` the journal port resolved from the durable source's author metadata — not from
  the marker's own ``gate=``. Otherwise the marker asserts its own authority and any actor's
  coordinator-shaped CI record reads as ``durable_ci_record``. Unresolved / wrong actor is a typed
  gap (:mod:`.hibernate_evidence_authority`).
* **Which review it answers** (F1). The Review Generation Marker Contract v2 requires ``req``; a
  ``review_result`` is evidence only of the review_request it names, so the conclusion is accepted
  only when ``req`` matches the issue's CURRENT ``review_request`` declaration and both name the
  same head. Without it, a superseded, hand-written or entirely uncorrelated approval satisfies the
  conjunct.
* **What corroborates it** (F3). A delegation names a release issue, so the release issue must
  carry the matching receipt (source issue + exact head); a park declaration must sit in the same
  note as the governed fixed-field park journal it claims. Both are records the issuing actor does
  not solely control, which is what makes them corroboration rather than restatement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from .glance_integration_disposition import (
    MARKER_GATE_INTEGRATION_DISPOSITION,
    fold_integration_disposition,
)
from .hibernate_evidence_authority import (
    MARKER_GATE_REVIEW_RESULT,
    EvidenceJournal,
    ResolvedIssuer,
    as_pairs,
    check_issuer_lane,
    check_issuer_resolution,
)
from .hibernate_candidate import (
    BASIS_DEPENDENCY_PARK,
    BASIS_EARLY_HIBERNATE,
    CONJUNCT_COMMITS_PUSHED,
    CONJUNCT_DOGFOOD_DELEGATED,
    CONJUNCT_PARK_DECLARED,
    CONJUNCT_REQUIRED_CI_GREEN,
    CONJUNCT_REVIEW_APPROVED,
    CONJUNCT_STAGING_INTEGRATED,
    DEPENDENCY_PARK_CONJUNCTS,
    EARLY_HIBERNATE_CONJUNCTS,
    PROVENANCE_CI_RECORD,
    PROVENANCE_DELEGATION_RECORD,
    PROVENANCE_GIT_REMOTE,
    PROVENANCE_INTEGRATION_RECORD,
    PROVENANCE_PARK_DECLARATION,
    PROVENANCE_REVIEW_RECORD,
    BasisConjunct,
)
from .hibernate_evidence_envelope import (
    EnvelopeParseError,
    LaneEvidenceEnvelope,
    parse_lane_envelope,
)
from .hibernate_evidence_integration import (
    IntegrationEvidence,
    IntegrationEvidenceError,
    resolve_integration_evidence,
)
from .hibernate_evidence_marker import (
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_PARK_DECLARED,
    EVIDENCE_REQUIRED_CI_GREEN,
    FIELD_RELEASE_ISSUE,
    EvidenceParseError,
    HibernateEvidence,
    resolve_hibernate_evidence,
)
from .redmine_journal_source import MARKER_CHANNEL_WORKFLOW_EVENT, marker_fields_in_note
from .sublane_admission import REVIEW_APPROVED

#: The gate whose marker declares the review generation a ``review_result`` must answer.
MARKER_GATE_REVIEW_REQUEST = "review_request"

FIELD_CONCLUSION = "conclusion"
FIELD_REQ = "req"
FIELD_HEAD = "head"

# Gap reasons that are NOT already a marker-grammar reason.
GAP_EVIDENCE_ABSENT = "evidence_absent"
GAP_PUSH_OBSERVATION_ABSENT = "push_observation_absent"
GAP_REVIEW_MISSING_CONCLUSION = "review_missing_conclusion"
#: The approval does not name the review_request it answers (contract v2 requires ``req``).
GAP_REVIEW_MISSING_REQ = "review_missing_request_correlation"
#: No ``review_request`` was ever declared, so no approval can be correlated to one.
GAP_REVIEW_REQUEST_ABSENT = "review_request_absent"
#: A NEWER review_request was filed after this result: the review generation has moved on, so the
#: approval — genuine for its own round — is no longer current evidence.
GAP_REVIEW_REQUEST_SUPERSEDED = "review_request_superseded"
#: The approval's ``req`` does not name the request this result actually answers (the greatest
#: request strictly before it). A ``req`` pointing at a LATER journal lands here too: a result
#: cannot answer a question that did not exist yet.
GAP_REVIEW_REQUEST_UNCORRELATED = "review_request_uncorrelated"
#: The approval and the request it names do not agree on the head under review.
GAP_REVIEW_REQUEST_HEAD_MISMATCH = "review_request_head_mismatch"
#: The delegation's release issue carries no matching receipt (or none was supplied to check).
GAP_DOGFOOD_RECEIPT_ABSENT = "dogfood_receipt_absent"
#: The release issue's receipt names a different source issue or head.
GAP_DOGFOOD_RECEIPT_MISMATCH = "dogfood_receipt_mismatch"
#: The park marker is not accompanied by the governed fixed-field park journal.
GAP_PARK_JOURNAL_FIELDS_ABSENT = "park_journal_fields_absent"
#: The CURRENT disposition journal exists but carries no enveloped marker (e.g. a heading-only
#: deferral). Distinct from :data:`GAP_EVIDENCE_ABSENT`, which means no disposition was ever recorded.
GAP_DISPOSITION_UNENVELOPED = "integration_evidence_unenveloped"

#: The evidence gate each marker-sourced conjunct is read from.
_CONJUNCT_GATE = {
    CONJUNCT_REVIEW_APPROVED: MARKER_GATE_REVIEW_RESULT,
    CONJUNCT_STAGING_INTEGRATED: MARKER_GATE_INTEGRATION_DISPOSITION,
    CONJUNCT_REQUIRED_CI_GREEN: EVIDENCE_REQUIRED_CI_GREEN,
    CONJUNCT_DOGFOOD_DELEGATED: EVIDENCE_DOGFOOD_DELEGATED,
    CONJUNCT_PARK_DECLARED: EVIDENCE_PARK_DECLARED,
}

#: The provenance each conjunct is produced under — the same authority T1 pins in
#: ``_CONJUNCT_AUTHORITY``. Re-stated here so the producer cannot quietly emit a conjunct under an
#: authority T1 does not accept (that would surface as ``conjunct_authority_mismatch``).
_CONJUNCT_PROVENANCE = {
    CONJUNCT_REVIEW_APPROVED: PROVENANCE_REVIEW_RECORD,
    CONJUNCT_STAGING_INTEGRATED: PROVENANCE_INTEGRATION_RECORD,
    CONJUNCT_REQUIRED_CI_GREEN: PROVENANCE_CI_RECORD,
    CONJUNCT_DOGFOOD_DELEGATED: PROVENANCE_DELEGATION_RECORD,
    CONJUNCT_PARK_DECLARED: PROVENANCE_PARK_DECLARATION,
    CONJUNCT_COMMITS_PUSHED: PROVENANCE_GIT_REMOTE,
}

_BASIS_CONJUNCTS = {
    BASIS_EARLY_HIBERNATE: EARLY_HIBERNATE_CONJUNCTS,
    BASIS_DEPENDENCY_PARK: DEPENDENCY_PARK_CONJUNCTS,
}


@dataclass(frozen=True)
class DogfoodReceipt:
    """The release issue's own record that it accepted a dogfood delegation (ruling j#85530 Q3).

    Read from the RELEASE issue, not from the source issue's marker: a delegation that only the
    delegating actor recorded is an intention, not a handover. ``source_issue`` and ``head`` are
    what the receipt says it received — the producer checks they are the ones being claimed.
    """

    release_issue: str
    source_issue: str
    head: str


@dataclass(frozen=True)
class PushObservation:
    """One action-time git-remote reachability observation (ruling j#85530 Q3).

    Not a durable marker: ``commits_pushed`` is proven by reading the remote at action time. The
    observation still declares WHICH lane and head it is about, so it is bound exactly like the
    marker-sourced conjuncts.
    """

    workspace: str
    lane: str
    lane_generation: int
    head: str
    reachable: bool


@dataclass(frozen=True)
class EvidenceGap:
    """A conjunct that could NOT be produced, and the typed reason why."""

    key: str
    reason: str
    detail: str = ""

    def as_payload(self) -> dict:
        return {"key": self.key, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class ProducedBasis:
    """The conjuncts produced for one basis, the gaps, and the durable journals behind them."""

    basis: str
    conjuncts: tuple[BasisConjunct, ...]
    gaps: tuple[EvidenceGap, ...]
    evidence_journals: Mapping[str, str]

    @property
    def decision_journal(self) -> str:
        """The newest durable journal any produced conjunct rests on (T2a ``journal_fn``).

        Empty when no marker-sourced conjunct was produced — the actuation leg treats an empty
        decision journal as :data:`...hibernate_actuation.NO_ACTUATION_MISSING_JOURNAL`, so a
        candidate whose basis rests on nothing durable can never be actuated.
        """
        journals = [int(j) for j in self.evidence_journals.values() if str(j).isdigit()]
        return str(max(journals)) if journals else ""

    def as_payload(self) -> dict:
        return {
            "basis": self.basis,
            "conjuncts": [c.as_payload() for c in self.conjuncts],
            "gaps": [g.as_payload() for g in self.gaps],
            "evidence_journals": dict(self.evidence_journals),
            "decision_journal": self.decision_journal,
        }


def _journal_int(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _Declaration:
    """The current declaration of one gate: its journal, its markers, and who wrote it."""

    journal: str = ""
    markers: tuple = ()
    issuer: ResolvedIssuer = ResolvedIssuer()
    notes: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.journal)


def _markers_of(notes: str, gate: str) -> tuple:
    return tuple(
        fields
        for channel, fields in marker_fields_in_note(notes or "")
        if channel == MARKER_CHANNEL_WORKFLOW_EVENT
        and str(fields.get("gate", "") or fields.get("kind", "") or "").strip() == gate
    )


def _latest_gate_declaration(
    journals: Sequence[EvidenceJournal], *, gate: str
) -> _Declaration:
    """The ``gate`` declaration in the HIGHEST-numbered journal that carries one (pure).

    Latest-wins by existence: a journal that declares the gate supersedes every earlier one, however
    its marker turns out to parse. The winning journal's writer travels with it — the issuer check
    must judge the record that is CURRENT, not whichever earlier record happens to have an
    acceptable author.
    """
    latest: Optional[Tuple[int, _Declaration]] = None
    for journal in journals or ():
        jint = _journal_int(journal.journal_id)
        if jint is None:
            continue
        found = _markers_of(journal.notes, gate)
        if not found:
            continue
        if latest is None or jint > latest[0]:
            latest = (jint, _Declaration(
                journal=str(jint),
                markers=found,
                issuer=journal.issuer,
                notes=journal.notes or "",
            ))
    return _Declaration() if latest is None else latest[1]


def _latest_disposition_declaration(journals: Sequence[EvidenceJournal]) -> _Declaration:
    """The integration-disposition declaration of the latest journal that DECLARES one (pure).

    Supersession is decided by the glance's own fold, so the two surfaces agree on which journal is
    current. A newer disposition journal that declares itself by heading alone (no marker, or a
    legacy marker) therefore shadows an older enveloped one, and its lack of enveloped evidence
    becomes a typed gap — never a silent fallback to the stale merge record.
    """
    facts = fold_integration_disposition(as_pairs(journals))
    if not facts.journal:
        return _Declaration()
    for journal in journals or ():
        if str(journal.journal_id).strip() != facts.journal:
            continue
        # A heading-only disposition journal has no marker at all: report the journal with an empty
        # marker set so the caller emits ``evidence_absent`` for THIS journal (not for the issue).
        return _Declaration(
            journal=facts.journal,
            markers=_markers_of(journal.notes, MARKER_GATE_INTEGRATION_DISPOSITION),
            issuer=journal.issuer,
            notes=journal.notes or "",
        )
    return _Declaration()


def _issuer_scope(conjunct: BasisConjunct) -> LaneEvidenceEnvelope:
    """The lane identity a produced conjunct was bound to, for the issuer's lane comparison."""
    return LaneEvidenceEnvelope(
        workspace=conjunct.bound_workspace,
        lane=conjunct.bound_lane,
        lane_generation=conjunct.bound_generation,
    )


def _conjunct(
    key: str, *, satisfied: bool, workspace: str, lane: str, generation: int, head: str = ""
) -> BasisConjunct:
    return BasisConjunct(
        key=key,
        satisfied=satisfied,
        provenance=_CONJUNCT_PROVENANCE[key],
        bound_workspace=workspace,
        bound_lane=lane,
        bound_generation=generation,
        bound_head=head,
    )


def _answered_review_request(
    journals: Sequence[EvidenceJournal], *, result_journal: str
) -> Tuple[str, str]:
    """The ``review_request`` the result on ``result_journal`` answers, as ``(journal_id, head)``.

    The repo already has this rule, twice — :func:`correlated_review_request_journal` (the review
    return route) and the glance's canonical review outcome both define the answered request as the
    GREATEST request journal STRICTLY BEFORE the result. This producer must not become a third,
    looser definition of the same relation (checkpoint j#86443 R2-F1): reading "the issue's latest
    request" instead let a result written at journal 100 name ``req=200``, and a request arriving
    later at journal 200 then activated that pre-written approval retroactively. A request in the
    SAME journal as the result is excluded for the same reason — Redmine ids are monotonic, so an
    answer cannot share a record with the question it answers.

    The request's own writer is deliberately not issuer-checked: it is the correlation anchor being
    pointed at, not an authority asserting a conjunct.
    """
    result_id = _journal_int(result_journal)
    if result_id is None:
        return "", ""
    best: Optional[Tuple[int, tuple]] = None
    for journal in journals or ():
        jint = _journal_int(journal.journal_id)
        if jint is None or jint >= result_id:
            continue
        markers = _markers_of(journal.notes, MARKER_GATE_REVIEW_REQUEST)
        if not markers:
            continue
        if best is None or jint > best[0]:
            best = (jint, markers)
    if best is None:
        return "", ""
    heads = {str(fields.get(FIELD_HEAD, "") or "").strip() for fields in best[1]}
    # A request journal whose markers disagree about the head names no single head: an empty head
    # cannot match any approval, which is the fail-closed direction.
    return str(best[0]), heads.pop() if len(heads) == 1 else ""


def _review_request_after(
    journals: Sequence[EvidenceJournal], *, result_journal: str
) -> bool:
    """Whether a ``review_request`` was filed AFTER the result on ``result_journal`` (pure).

    A re-review request re-opens the round. The old approval remains a true record of the round it
    closed, but it is no longer evidence that the lane is currently approved — the same
    "latest declaration wins by existing" rule the rest of this producer runs on.
    """
    result_id = _journal_int(result_journal)
    if result_id is None:
        return False
    return any(
        _journal_int(journal.journal_id) is not None
        and _journal_int(journal.journal_id) > result_id
        and _markers_of(journal.notes, MARKER_GATE_REVIEW_REQUEST)
        for journal in journals or ()
    )


def _produce_review_approved(
    markers: Sequence[Mapping[str, str]], *, request_journal: str, request_head: str
):
    """``review_approved`` from the latest ``review_result`` marker, correlated to its request.

    The review conclusion is part of the review-generation identity (#13974), so it is read from the
    marker rather than re-derived: ``approved`` satisfies the conjunct, any other EXPLICIT conclusion
    leaves it unsatisfied, and a missing conclusion is a gap (intake normalises a missing conclusion
    to ``pending``, which is not a real review outcome and must never read as one).

    The conclusion counts only for the review generation it answers (Review Generation Marker
    Contract v2, checkpoint j#86389 F1): ``req`` must be present, must name the issue's CURRENT
    review_request, and that request must be about the same head. An approval that names no request,
    or names a superseded one, or disagrees with it about the head, is a typed gap — the marker is
    then a genuine record of some other review, not of this one.
    """
    envelopes = []
    for fields in markers:
        bound = parse_lane_envelope(fields, require_head=True)
        if isinstance(bound, EnvelopeParseError):
            return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, bound.reason, bound.detail)
        conclusion = str(fields.get(FIELD_CONCLUSION, "") or "").strip()
        if not conclusion:
            return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_REVIEW_MISSING_CONCLUSION)
        req = str(fields.get(FIELD_REQ, "") or "").strip()
        if not req:
            return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_REVIEW_MISSING_REQ)
        envelopes.append((bound, conclusion, req))
    if not envelopes:
        return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_EVIDENCE_ABSENT)
    first = envelopes[0]
    for other in envelopes[1:]:
        if other != first:
            return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, "review_evidence_conflict")
    bound, conclusion, req = first

    if not request_journal:
        return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_REVIEW_REQUEST_ABSENT)
    if req != request_journal:
        return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_REVIEW_REQUEST_UNCORRELATED, req)
    if not request_head or request_head != bound.head:
        return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_REVIEW_REQUEST_HEAD_MISMATCH)

    return _conjunct(
        CONJUNCT_REVIEW_APPROVED,
        satisfied=conclusion == REVIEW_APPROVED,
        workspace=bound.workspace,
        lane=bound.lane,
        generation=bound.lane_generation,
        head=bound.head,
    ), None


def _produce_staging_integrated(markers: Sequence[Mapping[str, str]]):
    """``staging_integrated`` from the latest ``integration_disposition`` marker.

    The conjunct binds to the reviewed SOURCE head (the value T1 compares against the candidate
    head), not to ``integration_head``: the staging commit is the proof, the source head is what the
    proof is about. A readable ``explicit_deferral`` / ``integration_blocked`` is an unsatisfied
    conjunct — the record is legible and says the work is not on the branch.
    """
    got = resolve_integration_evidence(markers)
    if isinstance(got, IntegrationEvidenceError):
        # Includes INTEGRATION_NOT_INTEGRATED — a durable deferral is legible, and its own reason
        # travels into the gap rather than collapsing into "absent".
        return None, EvidenceGap(CONJUNCT_STAGING_INTEGRATED, got.reason, got.detail)
    assert isinstance(got, IntegrationEvidence)
    return _conjunct(
        CONJUNCT_STAGING_INTEGRATED,
        satisfied=True,
        workspace=got.envelope.workspace,
        lane=got.envelope.lane,
        generation=got.envelope.lane_generation,
        head=got.source_head,
    ), None


def _governed_field(notes: str, name: str) -> str:
    """One governed ``- <name>: <value>`` field line's value (pure, structured — never prose).

    The same shape the glance reads its disposition fields from: a list marker, emphasis, backticks
    and an ASCII or fullwidth colon are tolerated, and only a real field line matches.
    """
    pattern = re.compile(
        r"^\s*[-*]?\s*\**\s*" + re.escape(name) + r"\**\s*[:：]\s*(?P<value>.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(notes or "")
    return match.group("value").strip().strip("`") if match else ""


#: The governed fixed fields a parked-state journal records, per the skill's own fixed field shape
#: (``references/workflow.md`` ``## Sublane 完了 guardrail``): the parked state is a handoff-worthy
#: ``blocked`` state, so besides the dependency fields it carries the ``durable_anchor`` it is filed
#: against and the ``callback_result`` that makes the state complete.
#:
#: The COMPLETE set is required, not a convenient subset (checkpoint j#86443 R2-F4). Checking four
#: of the six let a note that never called back — the exact failure the guardrail was written for
#: (`progress_without_callback`) — read as an affirmative park basis. "The lane is parked" and "the
#: park was handed off" are one durable state in that contract, so the producer requires the whole
#: record rather than leaving half of it to an action-time obligation.
_PARK_REQUIRED_FIELDS = (
    "blocked_by",
    "resume_condition",
    "resume_owner",
    "durable_anchor",
    "callback_result",
)
_PARK_STATE_BLOCKED = "blocked"


def _park_journal_recorded(notes: str) -> bool:
    """Whether the note carries the COMPLETE governed fixed-field parked-state journal (pure)."""
    if _governed_field(notes, "state").lower() != _PARK_STATE_BLOCKED:
        return False
    return all(_governed_field(notes, name) for name in _PARK_REQUIRED_FIELDS)


def _dogfood_receipt_gap(
    evidence: HibernateEvidence,
    *,
    source_issue: str,
    receipts: Optional[Mapping[str, DogfoodReceipt]],
) -> Optional[EvidenceGap]:
    """The typed gap when the release issue does not corroborate this delegation, else ``None``."""
    release_issue = str(evidence.extra.get(FIELD_RELEASE_ISSUE, "") or "").strip()
    receipt = (receipts or {}).get(release_issue)
    if receipt is None:
        return EvidenceGap(CONJUNCT_DOGFOOD_DELEGATED, GAP_DOGFOOD_RECEIPT_ABSENT, release_issue)
    if (
        str(receipt.release_issue).strip() != release_issue
        or str(receipt.source_issue).strip() != str(source_issue).strip()
        or str(receipt.head).strip() != evidence.envelope.head
    ):
        return EvidenceGap(CONJUNCT_DOGFOOD_DELEGATED, GAP_DOGFOOD_RECEIPT_MISMATCH)
    return None


def _produce_evidence_kind(
    key: str,
    markers: Sequence[Mapping[str, str]],
    *,
    kind: str,
    notes: str = "",
    source_issue: str = "",
    receipts: Optional[Mapping[str, DogfoodReceipt]] = None,
):
    """``required_ci_green`` / ``dogfood_delegated`` / ``park_declared`` from their evidence marker.

    Beyond the marker grammar, the two kinds whose claim can be checked against a record the issuing
    actor does not solely control are checked against it (checkpoint j#86389 F3): a delegation needs
    the release issue's receipt for this exact source issue and head, and a park declaration needs
    the governed fixed-field park journal in the same note.
    """
    got = resolve_hibernate_evidence(markers, kind=kind)
    if isinstance(got, EvidenceParseError):
        # Includes EVIDENCE_CI_NOT_SUCCESS — a run that did not conclude success keeps that reason.
        return None, EvidenceGap(key, got.reason, got.detail)
    assert isinstance(got, HibernateEvidence)
    if kind == EVIDENCE_DOGFOOD_DELEGATED:
        gap = _dogfood_receipt_gap(got, source_issue=source_issue, receipts=receipts)
        if gap is not None:
            return None, gap
    if kind == EVIDENCE_PARK_DECLARED and not _park_journal_recorded(notes):
        return None, EvidenceGap(key, GAP_PARK_JOURNAL_FIELDS_ABSENT)
    return _conjunct(
        key,
        satisfied=True,
        workspace=got.envelope.workspace,
        lane=got.envelope.lane,
        generation=got.envelope.lane_generation,
        head=got.envelope.head,
    ), None


def produce_basis_conjuncts(
    journals: Sequence[EvidenceJournal],
    *,
    basis: str,
    source_issue: str = "",
    push: Optional[PushObservation] = None,
    dogfood_receipts: Optional[Mapping[str, DogfoodReceipt]] = None,
) -> ProducedBasis:
    """Produce every conjunct ``basis`` requires from the issue's durable journals (pure).

    Each conjunct is read from its OWN authority's latest declaration, written by the actor that
    authority belongs to, and bound to the identity that evidence declares. Nothing here compares
    against a candidate: T1 does that, so a mismatch surfaces as ``conjunct_anchor_mismatch``
    instead of being absorbed by the producer.

    ``source_issue`` is the issue whose journals these are — the SCOPE of the read, not the
    candidate's anchor — and is used only to confirm that a release issue's dogfood receipt is about
    this issue. Passing the candidate's lane or head here would be the tautology the producer exists
    to avoid; passing the issue being read is not.
    """
    conjuncts: list[BasisConjunct] = []
    gaps: list[EvidenceGap] = []
    evidence_journals: dict[str, str] = {}

    for key in _BASIS_CONJUNCTS.get(basis, ()):
        if key == CONJUNCT_COMMITS_PUSHED:
            if push is None:
                gaps.append(EvidenceGap(key, GAP_PUSH_OBSERVATION_ABSENT))
                continue
            conjuncts.append(_conjunct(
                key,
                satisfied=bool(push.reachable),
                workspace=push.workspace,
                lane=push.lane,
                generation=push.lane_generation,
                head=push.head,
            ))
            continue

        gate = _CONJUNCT_GATE[key]
        if key == CONJUNCT_STAGING_INTEGRATED:
            declaration = _latest_disposition_declaration(journals)
            if declaration.exists and not declaration.markers:
                gaps.append(EvidenceGap(key, GAP_DISPOSITION_UNENVELOPED, declaration.journal))
                continue
        else:
            declaration = _latest_gate_declaration(journals, gate=gate)
        if not declaration.markers:
            gaps.append(EvidenceGap(key, GAP_EVIDENCE_ABSENT))
            continue

        # WHO wrote the current declaration, judged before anything it says is read. The marker's
        # own `gate=` cannot confer the authority its content is about to be credited with. The
        # second half of the check — whether that authority is over the lane the evidence is ABOUT
        # — needs the parsed envelope and runs once the conjunct exists.
        issuer_gap = check_issuer_resolution(gate, declaration.issuer)
        if issuer_gap is not None:
            gaps.append(EvidenceGap(key, issuer_gap, declaration.journal))
            continue

        if key == CONJUNCT_REVIEW_APPROVED:
            # Two independent questions about the same approval:
            #   1. does it answer the request it claims to (the one immediately before it)?
            #   2. is that round still the current one, or has a re-review been requested since?
            # (1) is the canonical correlation rule; (2) is what keeps an approval from outliving
            # its round. A later request cannot retroactively validate an earlier approval (it is
            # not the answered request), and it also invalidates the round that approval closed.
            if _review_request_after(journals, result_journal=declaration.journal):
                produced, gap = None, EvidenceGap(
                    CONJUNCT_REVIEW_APPROVED, GAP_REVIEW_REQUEST_SUPERSEDED, declaration.journal
                )
            else:
                request_journal, request_head = _answered_review_request(
                    journals, result_journal=declaration.journal
                )
                produced, gap = _produce_review_approved(
                    declaration.markers,
                    request_journal=request_journal,
                    request_head=request_head,
                )
        elif key == CONJUNCT_STAGING_INTEGRATED:
            produced, gap = _produce_staging_integrated(declaration.markers)
        else:
            produced, gap = _produce_evidence_kind(
                key,
                declaration.markers,
                kind=gate,
                notes=declaration.notes,
                source_issue=source_issue,
                receipts=dogfood_receipts,
            )
        if gap is not None:
            gaps.append(gap)
            continue

        # Lane-scoped authority: the writer's own lane must be the lane the evidence declares
        # (checkpoint j#86443 R2-F2). Compared against what the producer TRANSCRIBED, so a foreign
        # lane's worker cannot acquire this lane's authority by writing this lane's envelope.
        lane_gap = check_issuer_lane(gate, declaration.issuer, _issuer_scope(produced))
        if lane_gap is not None:
            gaps.append(EvidenceGap(key, lane_gap, declaration.journal))
            continue

        conjuncts.append(produced)
        evidence_journals[key] = declaration.journal

    return ProducedBasis(
        basis=basis,
        conjuncts=tuple(conjuncts),
        gaps=tuple(gaps),
        evidence_journals=evidence_journals,
    )


__all__ = (
    "DogfoodReceipt",
    "EvidenceGap",
    "MARKER_GATE_REVIEW_REQUEST",
    "MARKER_GATE_REVIEW_RESULT",
    "ProducedBasis",
    "PushObservation",
    "produce_basis_conjuncts",
)
