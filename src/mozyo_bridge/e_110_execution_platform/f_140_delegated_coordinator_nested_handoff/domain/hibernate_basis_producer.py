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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from .glance_integration_disposition import (
    MARKER_GATE_INTEGRATION_DISPOSITION,
    fold_integration_disposition,
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
from .hibernate_evidence_envelope import EnvelopeParseError, parse_lane_envelope
from .hibernate_evidence_integration import (
    IntegrationEvidence,
    IntegrationEvidenceError,
    resolve_integration_evidence,
)
from .hibernate_evidence_marker import (
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_PARK_DECLARED,
    EVIDENCE_REQUIRED_CI_GREEN,
    EvidenceParseError,
    HibernateEvidence,
    resolve_hibernate_evidence,
)
from .redmine_journal_source import MARKER_CHANNEL_WORKFLOW_EVENT, marker_fields_in_note
from .sublane_admission import REVIEW_APPROVED

MARKER_GATE_REVIEW_RESULT = "review_result"

FIELD_CONCLUSION = "conclusion"

# Gap reasons that are NOT already a marker-grammar reason.
GAP_EVIDENCE_ABSENT = "evidence_absent"
GAP_PUSH_OBSERVATION_ABSENT = "push_observation_absent"
GAP_REVIEW_MISSING_CONCLUSION = "review_missing_conclusion"
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


def _latest_gate_markers(
    journals: Sequence[Tuple[object, str]], *, gate: str
) -> "tuple[str, tuple[dict, ...]]":
    """The markers of ``gate`` in the HIGHEST-numbered journal that carries any (pure).

    Latest-wins by existence: a journal that declares the gate supersedes every earlier one, however
    its marker turns out to parse. Returns ``("", ())`` when no journal declares the gate.
    """
    latest: Optional[Tuple[int, tuple]] = None
    for journal_id, notes in journals or ():
        jint = _journal_int(journal_id)
        if jint is None:
            continue
        found = tuple(
            fields
            for channel, fields in marker_fields_in_note(notes or "")
            if channel == MARKER_CHANNEL_WORKFLOW_EVENT
            and str(fields.get("gate", "") or fields.get("kind", "") or "").strip() == gate
        )
        if not found:
            continue
        if latest is None or jint > latest[0]:
            latest = (jint, found)
    return ("", ()) if latest is None else (str(latest[0]), latest[1])


def _latest_disposition_markers(
    journals: Sequence[Tuple[object, str]],
) -> "tuple[str, tuple[dict, ...]]":
    """The integration-disposition markers of the latest journal that DECLARES a disposition (pure).

    Supersession is decided by the glance's own fold, so the two surfaces agree on which journal is
    current. A newer disposition journal that declares itself by heading alone (no marker, or a
    legacy marker) therefore shadows an older enveloped one, and its lack of enveloped evidence
    becomes a typed gap — never a silent fallback to the stale merge record.
    """
    facts = fold_integration_disposition(journals)
    if not facts.journal:
        return "", ()
    for journal_id, notes in journals or ():
        if str(journal_id).strip() != facts.journal:
            continue
        markers = tuple(
            fields
            for channel, fields in marker_fields_in_note(notes or "")
            if channel == MARKER_CHANNEL_WORKFLOW_EVENT
            and str(fields.get("gate", "") or fields.get("kind", "") or "").strip()
            == MARKER_GATE_INTEGRATION_DISPOSITION
        )
        # A heading-only disposition journal has no marker at all: report the journal with an empty
        # marker set so the caller emits ``evidence_absent`` for THIS journal (not for the issue).
        return facts.journal, markers
    return "", ()


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


def _produce_review_approved(markers: Sequence[Mapping[str, str]]):
    """``review_approved`` from the latest ``review_result`` marker.

    The review conclusion is part of the review-generation identity (#13974), so it is read from the
    marker rather than re-derived: ``approved`` satisfies the conjunct, any other EXPLICIT conclusion
    leaves it unsatisfied, and a missing conclusion is a gap (intake normalises a missing conclusion
    to ``pending``, which is not a real review outcome and must never read as one).
    """
    envelopes = []
    for fields in markers:
        bound = parse_lane_envelope(fields, require_head=True)
        if isinstance(bound, EnvelopeParseError):
            return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, bound.reason, bound.detail)
        conclusion = str(fields.get(FIELD_CONCLUSION, "") or "").strip()
        if not conclusion:
            return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_REVIEW_MISSING_CONCLUSION)
        envelopes.append((bound, conclusion))
    if not envelopes:
        return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_EVIDENCE_ABSENT)
    first = envelopes[0]
    for other in envelopes[1:]:
        if other != first:
            return None, EvidenceGap(CONJUNCT_REVIEW_APPROVED, "review_evidence_conflict")
    bound, conclusion = first
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


def _produce_evidence_kind(key: str, markers: Sequence[Mapping[str, str]], *, kind: str):
    """``required_ci_green`` / ``dogfood_delegated`` / ``park_declared`` from their evidence marker."""
    got = resolve_hibernate_evidence(markers, kind=kind)
    if isinstance(got, EvidenceParseError):
        # Includes EVIDENCE_CI_NOT_SUCCESS — a run that did not conclude success keeps that reason.
        return None, EvidenceGap(key, got.reason, got.detail)
    assert isinstance(got, HibernateEvidence)
    return _conjunct(
        key,
        satisfied=True,
        workspace=got.envelope.workspace,
        lane=got.envelope.lane,
        generation=got.envelope.lane_generation,
        head=got.envelope.head,
    ), None


def produce_basis_conjuncts(
    journals: Sequence[Tuple[object, str]],
    *,
    basis: str,
    push: Optional[PushObservation] = None,
) -> ProducedBasis:
    """Produce every conjunct ``basis`` requires from the issue's durable journals (pure).

    Each conjunct is read from its OWN authority's latest declaration and bound to the identity that
    evidence declares. Nothing here compares against a candidate: T1 does that, so a mismatch
    surfaces as ``conjunct_anchor_mismatch`` instead of being absorbed by the producer.
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
            journal, markers = _latest_disposition_markers(journals)
            if journal and not markers:
                gaps.append(EvidenceGap(key, GAP_DISPOSITION_UNENVELOPED, journal))
                continue
        else:
            journal, markers = _latest_gate_markers(journals, gate=gate)
        if not markers:
            gaps.append(EvidenceGap(key, GAP_EVIDENCE_ABSENT))
            continue
        if key == CONJUNCT_REVIEW_APPROVED:
            produced, gap = _produce_review_approved(markers)
        elif key == CONJUNCT_STAGING_INTEGRATED:
            produced, gap = _produce_staging_integrated(markers)
        else:
            produced, gap = _produce_evidence_kind(key, markers, kind=gate)
        if gap is not None:
            gaps.append(gap)
            continue
        conjuncts.append(produced)
        evidence_journals[key] = journal

    return ProducedBasis(
        basis=basis,
        conjuncts=tuple(conjuncts),
        gaps=tuple(gaps),
        evidence_journals=evidence_journals,
    )


__all__ = (
    "EvidenceGap",
    "MARKER_GATE_REVIEW_RESULT",
    "ProducedBasis",
    "PushObservation",
    "produce_basis_conjuncts",
)
