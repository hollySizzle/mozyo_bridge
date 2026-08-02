"""The durable authorities the #13686 actuator reads, folded from one issue's journals (#14825).

#13686 shipped :class:`~...application.auto_integration_ports.DurableAuthorityReader` as a
Protocol with no implementation, so every durable fact — is the latest review generation
approved, for which head, is the work recorded integrated, did CI conclude on the exact commit —
was establishable only by injecting a test fake. This module is the pure half of the live
answer: journals in, typed facts and typed gaps out, no I/O.

**It writes no new grammar.** Every rule here is one the workspace already has:

* the review generation's admissibility and its approved head come from
  :func:`~.hibernate_basis_producer.produce_conjuncts` — the same producer the hibernate basis
  reads, so the correlation rule (an approval answers the greatest ``review_request`` strictly
  before it, both naming one head, unsuperseded by a later request) has ONE definition. That
  producer's own docstring records what a second definition cost (checkpoint j#86443 R2-F1: a
  looser rule let a pre-written approval be activated retroactively by a later request), and a
  third one written here would be the same defect with this issue's name on it;
* the integration record comes from the same producer's ``staging_integrated`` conjunct plus
  :func:`~.hibernate_evidence_integration.resolve_integration_evidence`, so the coordinator's
  source head / integration head / branch / disposition split is read exactly as the
  auto-hibernate basis reads it;
* who was allowed to write each record comes from
  :mod:`.hibernate_evidence_authority` — the marker's own ``gate=`` never confers the authority
  its content is about to be credited with.

**Two things this module does that the hibernate producer deliberately does not.**

1. *It compares the evidence's lane envelope against a scope the caller supplies.* The producer
   refuses to see the target because binding evidence to a caller-supplied anchor would make its
   own anchor check a tautology; the comparison still has to happen, and in the hibernate path
   :func:`~.hibernate_candidate.classify_hibernate_candidate` performs it. Here the caller is the
   actuator, whose :class:`LaneScope` is its OWN configuration — not the action record it is being
   asked to act on — so the comparison is between two independently sourced values, which is what
   makes it a check rather than a restatement. The action record's own identity is compared
   against that same configuration by the caller, before this fold is reached.

2. *CI evidence is read PER HEAD, not latest-wins.* Everywhere else in this subsystem the latest
   declaration of a gate supersedes the earlier ones, because they are competing statements about
   one question. The two CI gates an integration passes are not: the source branch's CI and the
   integration SHA's CI are about DIFFERENT commits, recorded under the same
   ``required_ci_green`` gate, and the second is recorded after the first by construction. Under
   latest-wins the source-CI gate would start failing the moment the integration SHA's run
   landed — which is exactly when the asynchronous continuation re-runs the action — and the run
   could never reach ``integrated``. So the question asked here is "is there a green required-CI
   record for THIS exact commit", and a record about another commit is not a statement about this
   one. Two DIFFERING records about the same head are still a conflict and still refuse.

   The boundary that leaves: a red run has no marker at all (the producer renders
   ``conclusion=success`` only), so this cannot read "it went red afterwards" for a head it has
   already seen green. Retraction is expressed by the action, not by the record — a re-formed
   action has a different head, and a head is integrated exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from .glance_integration_disposition import canonical_marker_value
from .hibernate_basis_producer import (
    GAP_EVIDENCE_ABSENT,
    EvidenceGap,
    latest_disposition_declaration,
    latest_gate_declaration,
    produce_conjuncts,
)
from .hibernate_candidate import (
    CONJUNCT_REQUIRED_CI_GREEN,
    CONJUNCT_REVIEW_APPROVED,
    CONJUNCT_STAGING_INTEGRATED,
)
from .hibernate_evidence_authority import (
    EvidenceJournal,
    check_issuer_lane,
    check_issuer_resolution,
)
from .hibernate_evidence_integration import (
    IntegrationEvidence,
    IntegrationEvidenceError,
    resolve_integration_evidence,
)
from .hibernate_evidence_marker import (
    EVIDENCE_CONFLICT,
    EVIDENCE_REQUIRED_CI_GREEN,
    FIELD_CONCLUSION,
    FIELD_RUN,
    FIELD_WORKFLOW,
    EvidenceParseError,
    HibernateEvidence,
    parse_hibernate_evidence,
)
from .hibernate_evidence_envelope import LaneEvidenceEnvelope
from .redmine_journal_source import declares_gate, strict_gate_markers

#: A produced conjunct is about a different lane / generation than the actuator's own. Not the
#: same as absent: the record is legible and says it is about somebody else.
GAP_LANE_SCOPE_MISMATCH = "authority_lane_scope_mismatch"
#: The ``required_ci_green`` gate is declared on the issue but the CURRENT declaration cannot be
#: read at all, so nothing can be said about whether it supersedes the head being asked about.
GAP_CI_DECLARATION_UNREADABLE = "ci_declaration_unreadable"
#: No readable green CI record names the commit being asked about.
GAP_CI_HEAD_ABSENT = "ci_head_absent"


@dataclass(frozen=True)
class LaneScope:
    """The lane an actuator holds authority over — ITS configuration, not a record's claim.

    Supplied by the caller from its own construction, which is what separates this from the
    anchor the evidence declares. Every field is compared exactly; an incomplete scope matches
    nothing, so an unconfigured actuator establishes no durable fact.
    """

    workspace: str = ""
    lane: str = ""
    lane_generation: int = 0

    @property
    def is_complete(self) -> bool:
        return bool(
            self.workspace
            and self.lane
            and isinstance(self.lane_generation, int)
            and not isinstance(self.lane_generation, bool)
            and self.lane_generation > 0
        )

    def covers(self, *, workspace: str, lane: str, lane_generation: int) -> bool:
        """Whether the evidence's declared envelope is exactly this lane."""
        return (
            self.is_complete
            and self.workspace == workspace
            and self.lane == lane
            and self.lane_generation == lane_generation
        )

    def as_envelope(self, head: str = "") -> LaneEvidenceEnvelope:
        return LaneEvidenceEnvelope(
            workspace=self.workspace,
            lane=self.lane,
            lane_generation=self.lane_generation,
            head=head,
        )


@dataclass(frozen=True)
class ReviewApproval:
    """What the issue's CURRENT review generation says, and about which head.

    ``admissible`` is the conjunction the preset's ``coordinator_owned_auto_integration``
    requires: the LATEST generation approved, correlated to the request it answers, unsuperseded
    by a re-review, written by the same-lane gateway, and about this actuator's lane. ``head`` is
    the exact commit that approval names — never a branch, never an earlier commit.
    """

    admissible: bool = False
    head: str = ""
    journal: str = ""


@dataclass(frozen=True)
class IntegrationRecord:
    """The coordinator's durable statement that this lane's work reached the target.

    ``source_head`` is what the proof is ABOUT; ``integration_head`` is the commit on the target
    that proves it. They differ for a merge commit and for a patch-equivalent landing, which is
    why the Hibernate Evidence Marker Contract splits them and why a single head cannot stand in
    for both.
    """

    confirmed: bool = False
    source_head: str = ""
    integration_head: str = ""
    integration_branch: str = ""
    disposition: str = ""
    journal: str = ""


@dataclass(frozen=True)
class CiRecord:
    """A green required-CI record about one exact commit, with the identity that makes it checkable."""

    head: str = ""
    workflow: str = ""
    run: str = ""
    conclusion: str = ""
    journal: str = ""

    @property
    def is_present(self) -> bool:
        return bool(self.head and self.workflow and self.run and self.conclusion)


@dataclass(frozen=True)
class DurableAuthorityFacts:
    """Everything this fold could establish from one issue's journals, plus why it could not."""

    review: ReviewApproval = ReviewApproval()
    integration: IntegrationRecord = IntegrationRecord()
    gaps: Tuple[EvidenceGap, ...] = ()

    def gap(self, key: str) -> Optional[EvidenceGap]:
        for missing in self.gaps:
            if missing.key == key:
                return missing
        return None


def fold_durable_authority(
    journals: Sequence[EvidenceJournal], *, scope: LaneScope
) -> DurableAuthorityFacts:
    """The review and integration authorities for ``scope``, from ``journals`` (pure).

    Both are produced by the shared conjunct producer and then required to be about ``scope``.
    A conjunct the producer could not build carries its own typed gap through unchanged; one it
    built about another lane becomes :data:`GAP_LANE_SCOPE_MISMATCH`, which is a different
    operational fact from "no record exists" and is kept distinguishable in the durable report.
    """
    produced = produce_conjuncts(
        journals,
        keys=(CONJUNCT_REVIEW_APPROVED, CONJUNCT_STAGING_INTEGRATED),
    )
    gaps: list[EvidenceGap] = list(produced.gaps)

    review = ReviewApproval()
    approved = produced.conjunct(CONJUNCT_REVIEW_APPROVED)
    if approved is not None:
        if not scope.covers(
            workspace=approved.bound_workspace,
            lane=approved.bound_lane,
            lane_generation=approved.bound_generation,
        ):
            gaps.append(EvidenceGap(CONJUNCT_REVIEW_APPROVED, GAP_LANE_SCOPE_MISMATCH))
        else:
            review = ReviewApproval(
                admissible=bool(approved.satisfied),
                head=approved.bound_head,
                journal=str(produced.evidence_journals.get(CONJUNCT_REVIEW_APPROVED, "")),
            )

    integration = IntegrationRecord()
    integrated = produced.conjunct(CONJUNCT_STAGING_INTEGRATED)
    if integrated is not None:
        if not scope.covers(
            workspace=integrated.bound_workspace,
            lane=integrated.bound_lane,
            lane_generation=integrated.bound_generation,
        ):
            gaps.append(EvidenceGap(CONJUNCT_STAGING_INTEGRATED, GAP_LANE_SCOPE_MISMATCH))
        else:
            # The conjunct proves the disposition is an integrated one and binds it to the lane;
            # the evidence record carries the two heads and the branch the conjunct folds away.
            # Re-resolving from the same declaration's markers is a read of the same record, not
            # a second opinion about which record is current.
            evidence = _integration_evidence(journals)
            integration = IntegrationRecord(
                confirmed=bool(integrated.satisfied) and evidence is not None,
                source_head=integrated.bound_head,
                integration_head=evidence.integration_head if evidence else "",
                integration_branch=evidence.integration_branch if evidence else "",
                disposition=evidence.disposition if evidence else "",
                journal=str(produced.evidence_journals.get(CONJUNCT_STAGING_INTEGRATED, "")),
            )

    return DurableAuthorityFacts(
        review=review, integration=integration, gaps=tuple(gaps)
    )


def _integration_evidence(
    journals: Sequence[EvidenceJournal],
) -> Optional[IntegrationEvidence]:
    """The enveloped integration evidence of the CURRENT disposition declaration, or ``None``."""
    declaration = latest_disposition_declaration(journals)
    if not declaration.markers:
        return None
    resolved = resolve_integration_evidence(declaration.markers)
    return None if isinstance(resolved, IntegrationEvidenceError) else resolved


def ci_record_for_head(
    journals: Sequence[EvidenceJournal], *, head: str, scope: LaneScope
) -> "CiRecord | EvidenceGap":
    """The green required-CI record about the exact commit ``head``, or a typed gap (pure).

    Asked per head rather than latest-wins, for the reason in the module docstring. The
    supersession rule is not abandoned, only narrowed to what it can actually decide:

    * if the issue's CURRENT ``required_ci_green`` declaration cannot be read at all, nothing
      here can tell whether it is about ``head``, so the answer is a refusal
      (:data:`GAP_CI_DECLARATION_UNREADABLE`) rather than a search past it;
    * otherwise every readable green record about ``head`` is collected, and two that DIFFER are
      a conflict (:data:`EVIDENCE_CONFLICT`) — one head cannot have two different green runs
      asserted about it;
    * the writer of the journal the record is taken from must be the coordinator, holding
      authority over this lane. A marker that names the CI gate does not thereby become the CI
      authority.
    """
    wanted = str(head or "").strip()
    if not wanted:
        return EvidenceGap(CONJUNCT_REQUIRED_CI_GREEN, GAP_CI_HEAD_ABSENT)

    current = latest_gate_declaration(journals, gate=EVIDENCE_REQUIRED_CI_GREEN)
    if not current.exists:
        return EvidenceGap(CONJUNCT_REQUIRED_CI_GREEN, GAP_EVIDENCE_ABSENT)
    if not _parsed_records(current.markers):
        # The CURRENT declaration says something about CI that cannot be read — either it carries
        # no strictly-readable marker at all, or every marker it does carry fails the evidence
        # grammar. Either way there is no telling whether it is about the head being asked, so
        # searching past it to an older readable record would let a stale green outlive a
        # declaration nobody can read. Note the question is only whether the newest declaration
        # PARSES: one that parses and is about another commit is a perfectly readable statement
        # about a different head, and does not block this one.
        return EvidenceGap(
            CONJUNCT_REQUIRED_CI_GREEN, GAP_CI_DECLARATION_UNREADABLE, current.journal
        )

    found: Optional[Tuple[HibernateEvidence, EvidenceJournal]] = None
    for journal in journals or ():
        if not declares_gate(journal.notes, EVIDENCE_REQUIRED_CI_GREEN):
            continue
        for evidence in _green_records(journal, head=wanted):
            if found is None:
                found = (evidence, journal)
            elif found[0] != evidence:
                return EvidenceGap(
                    CONJUNCT_REQUIRED_CI_GREEN, EVIDENCE_CONFLICT, wanted
                )
    if found is None:
        return EvidenceGap(CONJUNCT_REQUIRED_CI_GREEN, GAP_CI_HEAD_ABSENT, wanted)

    evidence, journal = found
    issuer_gap = check_issuer_resolution(EVIDENCE_REQUIRED_CI_GREEN, journal.issuer)
    if issuer_gap is not None:
        return EvidenceGap(
            CONJUNCT_REQUIRED_CI_GREEN, issuer_gap, str(journal.journal_id)
        )
    lane_gap = check_issuer_lane(
        EVIDENCE_REQUIRED_CI_GREEN, journal.issuer, evidence.envelope
    )
    if lane_gap is not None:
        return EvidenceGap(
            CONJUNCT_REQUIRED_CI_GREEN, lane_gap, str(journal.journal_id)
        )
    if not scope.covers(
        workspace=evidence.envelope.workspace,
        lane=evidence.envelope.lane,
        lane_generation=evidence.envelope.lane_generation,
    ):
        return EvidenceGap(CONJUNCT_REQUIRED_CI_GREEN, GAP_LANE_SCOPE_MISMATCH, wanted)

    return CiRecord(
        head=evidence.envelope.head,
        workflow=str(evidence.extra.get(FIELD_WORKFLOW, "")),
        run=str(evidence.extra.get(FIELD_RUN, "")),
        conclusion=str(evidence.extra.get(FIELD_CONCLUSION, "")),
        journal=str(journal.journal_id),
    )


def _parsed_records(markers: Sequence) -> Tuple[HibernateEvidence, ...]:
    """Every marker in ``markers`` that parses as green CI evidence, whatever head it names."""
    parsed = [
        parse_hibernate_evidence(fields, kind=EVIDENCE_REQUIRED_CI_GREEN)
        for fields in markers or ()
    ]
    return tuple(one for one in parsed if not isinstance(one, EvidenceParseError))


def _green_records(journal: EvidenceJournal, *, head: str) -> Tuple[HibernateEvidence, ...]:
    """Every strictly-readable green CI record in ``journal`` that is about ``head``.

    Through the SHARED strict reader with the governed canonicalizer — the one call every
    Hibernate / terminal authority consumer makes — so "which markers count as this gate's
    evidence" keeps a single definition across this subsystem.
    """
    markers = strict_gate_markers(
        journal.notes, EVIDENCE_REQUIRED_CI_GREEN, canonicalize=canonical_marker_value
    )
    return tuple(
        record for record in _parsed_records(markers) if record.envelope.head == head
    )


__all__ = (
    "GAP_CI_DECLARATION_UNREADABLE",
    "GAP_CI_HEAD_ABSENT",
    "GAP_LANE_SCOPE_MISMATCH",
    "CiRecord",
    "DurableAuthorityFacts",
    "IntegrationRecord",
    "LaneScope",
    "ReviewApproval",
    "ci_record_for_head",
    "fold_durable_authority",
)
