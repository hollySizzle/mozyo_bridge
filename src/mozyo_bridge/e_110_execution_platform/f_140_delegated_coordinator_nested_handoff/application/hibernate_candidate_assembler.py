"""Action-time assembly of hibernate candidates (Redmine #14219 T2b, step 4b).

Step 4a made the pure per-conjunct producer. This is the application seam that feeds it: it reads
the three action-time authorities through injected ports — the read-only lifecycle store, the
issue's durable journals, and a git-remote observation of the lane head — and hands them to the
pure T1 classifier. It also binds the T2a actuation leg's three seams (``refresh_fn`` /
``obligations_fn`` / ``journal_fn``) to those same producers.

Four decisions here are load-bearing:

* **The head is bound from the git-remote observation, never from an evidence marker.** T1 accepts
  either head authority (:data:`...HEAD_AUTHORITIES`), and the review_result marker does carry a
  full head — but binding the candidate head FROM the review marker would make that marker's own
  anchor check a tautology (the evidence would be compared against a head derived from itself). The
  git-remote observation is independent of every durable marker, so review / integration / CI /
  dogfood heads are all genuinely cross-checked against it. The one conjunct bound to the same
  observation is ``commits_pushed``, whose truth lives in ``reachable`` rather than in identity, so
  no conjunct's *content* is self-certifying. An absent observation is
  :data:`...NON_CANDIDATE_HEAD_UNBOUND` — a lane whose head cannot be observed is never a candidate,
  dependency-park included.
* **An unreadable journal source is not an absent record.** ``journals_fn`` returning ``None``
  (fetch failed / source unavailable) is a typed zero-actuation carrying
  :data:`DETAIL_JOURNAL_SOURCE_UNREADABLE`, not an empty journal list — "we could not read the
  evidence" must never fold into the same verdict as "the evidence says no", and it must certainly
  never look like a satisfied basis.
* **One fresh observation per candidate per pass.** The leg calls ``journal_fn`` and then
  ``refresh_fn`` for the same candidate; both read from a single re-assembly memoised for that
  candidate (:meth:`HibernateCandidateAssembler.pass_seams`). Two independent re-reads would open a
  second TOCTOU window between them and could authorise the actuation with a journal from one
  observation while validating the candidate against another. A fresh pass takes fresh seams.
* **The assembler decides nothing.** Every verdict is the pure classifier's; the producer is never
  told which lane or head it is being read for. This module only supplies inputs and transcribes
  the result.

The obligation flags are passed straight through from the injected ``obligations_fn``. They are
action-time LIVE facts (composer prompt, running turn, worktree, callback drain), not durable
evidence markers, so T2b defines only the seam and its fail-closed default; the concrete observers
belong with the supervisor wiring that owns live observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

from ..domain.hibernate_actuation import ActionTimeObligations
from ..domain.hibernate_basis_producer import (
    GAP_PUSH_OBSERVATION_ABSENT,
    ProducedBasis,
    PushObservation,
    produce_basis_conjuncts,
)
from ..domain.hibernate_candidate import (
    NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN,
    NON_CANDIDATE_HEAD_UNBOUND,
    PROVENANCE_GIT_REMOTE,
    BoundField,
    HibernateCandidate,
    HibernateNonCandidate,
    SelectedLane,
    classify_hibernate_candidate,
)

#: One journal as the producer reads it: its durable id and its verbatim note body.
JournalPage = Sequence[Tuple[str, str]]

LifecycleRecordsReader = Callable[[], Optional[Sequence[object]]]
JournalReader = Callable[[str], Optional[JournalPage]]
PushObserver = Callable[[SelectedLane], Optional[PushObservation]]
ObligationObserver = Callable[[HibernateCandidate], ActionTimeObligations]

#: The journal source could not be read at all (distinct from "the issue has no such record").
DETAIL_JOURNAL_SOURCE_UNREADABLE = "journal_source_unreadable"


@dataclass(frozen=True)
class AssemblyRequest:
    """One lane the enumeration selected, plus the basis it is claimed to qualify under.

    ``basis`` is a DECLARED typed basis, never inferred here: the classifier rejects an unknown one
    outright, and each basis reads only its own required conjuncts (no fallback between bases).
    """

    selected: SelectedLane
    basis: str


@dataclass(frozen=True)
class AssembledCandidate:
    """The verdict for one :class:`AssemblyRequest`, with the evidence it was produced from."""

    request: AssemblyRequest
    verdict: "HibernateCandidate | HibernateNonCandidate"
    produced: Optional[ProducedBasis] = None

    @property
    def candidate(self) -> Optional[HibernateCandidate]:
        """The candidate, or ``None`` when this lane is not one (fail-closed for the leg)."""
        return self.verdict if isinstance(self.verdict, HibernateCandidate) else None

    @property
    def decision_journal(self) -> str:
        """The durable basis-event journal, or ``""`` when this lane is not a candidate.

        A non-candidate never yields a journal: handing the leg a journal for a lane that no longer
        qualifies would supply an actuation anchor for an unproven basis.
        """
        if self.candidate is None or self.produced is None:
            return ""
        return self.produced.decision_journal

    def as_payload(self) -> dict:
        payload = {"basis": self.request.basis, "verdict": self.verdict.as_payload()}
        if self.produced is not None:
            payload["evidence"] = self.produced.as_payload()
        return payload


def _selected_of(candidate: HibernateCandidate) -> SelectedLane:
    """The exact lane identity to re-select when revalidating ``candidate``."""
    return SelectedLane(
        issue_id=candidate.issue_id,
        repo_workspace_id=candidate.anchor.repo_workspace_id,
        lane_id=candidate.anchor.lane_id,
        lane_generation=candidate.anchor.lane_generation,
        revision=candidate.anchor.revision,
    )


@dataclass(frozen=True)
class PassSeams:
    """The three T2a seams for ONE bounded pass, bound to a single fresh observation per candidate.

    ``refresh_fn`` and ``journal_fn`` share that observation by construction; re-using a
    :class:`PassSeams` across passes would re-use its memo, so a pass takes its own.
    """

    refresh_fn: Callable[[HibernateCandidate], Optional[HibernateCandidate]]
    obligations_fn: ObligationObserver
    journal_fn: Callable[[HibernateCandidate], str]


class HibernateCandidateAssembler:
    """Assembles hibernate candidates from the action-time authorities, and binds the T2a seams."""

    def __init__(
        self,
        *,
        records_fn: LifecycleRecordsReader,
        journals_fn: JournalReader,
        push_fn: PushObserver,
        obligations_fn: ObligationObserver,
    ) -> None:
        self._records_fn = records_fn
        self._journals_fn = journals_fn
        self._push_fn = push_fn
        self._obligations_fn = obligations_fn

    def assemble(self, request: AssemblyRequest) -> AssembledCandidate:
        """Read the three authorities once and classify — pure decision, injected reads.

        Each read that cannot produce a usable input short-circuits to a typed non-candidate before
        the next one runs; nothing is defaulted, and no verdict is reached without the classifier.
        """
        selected = request.selected
        issue = selected.issue_id

        journals = self._journals_fn(issue)
        if journals is None:
            return AssembledCandidate(
                request,
                HibernateNonCandidate(
                    issue, NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN, DETAIL_JOURNAL_SOURCE_UNREADABLE
                ),
            )

        push = self._push_fn(selected)
        if push is None:
            return AssembledCandidate(
                request,
                HibernateNonCandidate(
                    issue, NON_CANDIDATE_HEAD_UNBOUND, GAP_PUSH_OBSERVATION_ABSENT
                ),
            )

        produced = produce_basis_conjuncts(journals, basis=request.basis, push=push)
        verdict = classify_hibernate_candidate(
            selected=selected,
            declared_basis=request.basis,
            records=self._records_fn(),
            head=BoundField(value=push.head, provenance=PROVENANCE_GIT_REMOTE),
            conjuncts=produced.conjuncts,
        )
        return AssembledCandidate(request, verdict, produced)

    def assemble_all(
        self, requests: Sequence[AssemblyRequest]
    ) -> tuple[AssembledCandidate, ...]:
        """Assemble every selected lane, keeping the non-candidates and their typed reasons.

        Non-candidates are RETURNED, not dropped: a lane that failed to qualify is an observation
        the pass reports, and silently discarding it would make an evidence regression look like an
        empty queue.
        """
        return tuple(self.assemble(request) for request in requests)

    def pass_seams(self) -> PassSeams:
        """Bind the T2a seams for one bounded pass (fresh memo per call)."""
        memo: Dict[HibernateCandidate, AssembledCandidate] = {}

        def fresh(candidate: HibernateCandidate) -> AssembledCandidate:
            got = memo.get(candidate)
            if got is None:
                got = self.assemble(
                    AssemblyRequest(selected=_selected_of(candidate), basis=candidate.basis)
                )
                memo[candidate] = got
            return got

        return PassSeams(
            refresh_fn=lambda candidate: fresh(candidate).candidate,
            obligations_fn=self._obligations_fn,
            journal_fn=lambda candidate: fresh(candidate).decision_journal,
        )


__all__ = [
    "DETAIL_JOURNAL_SOURCE_UNREADABLE",
    "AssembledCandidate",
    "AssemblyRequest",
    "HibernateCandidateAssembler",
    "JournalReader",
    "LifecycleRecordsReader",
    "ObligationObserver",
    "PassSeams",
    "PushObserver",
]
