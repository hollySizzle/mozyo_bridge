"""Review-generation fencing for concurrent stale approvals (Redmine #13518 review R2-F7).

The re-audit reproduced a last-write-wins hole (incident #13586 j#75832 -> j#75833): reviewer A
appended a blocking finding, then reviewer B — working from a snapshot taken *before* that finding —
wrote an ``approved`` 18s later, and a consumer reading only the last journal would integrate on the
stale approval. The durable review path had no compare-and-set on the review generation and no
unresolved-finding fence, so concurrent consumers were not fail-closed.

This pure domain supplies the three product invariants the finding required (the application layer
reads the Redmine journals into these value objects and consults the evaluators; it never guesses):

- **a unique review generation** ``(issue, review_request_journal, target_head)`` — one review of one
  head under one request (:class:`ReviewGeneration`);
- **a duplicate-consumer lease / CAS** (:class:`GenerationLease`) so at most one consumer processes a
  generation — a second consumer of the same generation is refused, not admitted;
- **a pre-approval reread fence** (:func:`evaluate_approval_admissible`) — an approval is refused when
  the reread source issue carries an unresolved blocking finding newer than the source review
  request (so B's stale ``approved`` never lands over A's finding); and
- **an integration latest-generation fence** (:func:`evaluate_integration_admissible`) — integration
  requires the *latest* generation to be approved with **no** unresolved blocking finding, never
  merely "an approval exists somewhere".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, MutableMapping, Optional

# --- Review decision kinds / dispositions (closed vocab) -------------------
DECISION_FINDING = "finding"  # a review that recorded a blocking / non-blocking finding
DECISION_APPROVAL = "approval"  # a review that approved (no blocking findings from that reviewer)

DISPOSITION_UNRESOLVED = "unresolved"  # a blocking finding not yet dispositioned (verdict/fix/dispute)
DISPOSITION_RESOLVED = "resolved"  # a finding whose disposition is recorded (accepted+fixed / disputed)


@dataclass(frozen=True)
class ReviewGeneration:
    """The unique identity of one review: ``(issue, review_request_journal, target_head)``.

    A new review request (a new ``review_request_journal``) or a new pushed head (``target_head``)
    is a NEW generation; an approval / finding belongs to exactly one. ``target_head`` is the exact
    commit the review request pinned, so an approval for an older head can never satisfy a newer one.
    """

    issue: str
    review_request_journal: str
    target_head: str

    @property
    def identity(self) -> str:
        return f"{self.issue}:{self.review_request_journal}:{self.target_head}"


@dataclass(frozen=True)
class ReviewDecision:
    """One durable review decision read from a Redmine journal (pure value).

    ``seq`` is the monotonic journal ordering (a larger ``seq`` is newer). ``blocking`` marks a
    finding that blocks completion; ``disposition`` is whether a blocking finding has been
    dispositioned. An approval carries ``kind == DECISION_APPROVAL`` (``blocking`` is False).
    """

    generation: ReviewGeneration
    kind: str
    seq: int
    blocking: bool = False
    disposition: str = DISPOSITION_UNRESOLVED
    journal_id: str = ""


@dataclass(frozen=True)
class AdmissionResult:
    """Whether a review action (approval / integration) is admissible, with a fixed reason."""

    admissible: bool
    reason: str = ""

    def as_payload(self) -> dict:
        return {"admissible": self.admissible, "reason": self.reason}


# --- Reasons (closed vocab) ------------------------------------------------
REASON_OK = "ok"
REASON_NEWER_BLOCKING_FINDING = "newer_unresolved_blocking_finding"
REASON_NO_APPROVAL_FOR_LATEST = "no_approval_for_latest_generation"
REASON_UNRESOLVED_BLOCKING_FINDING = "unresolved_blocking_finding_in_latest_generation"


def _unresolved_blocking(decisions: Iterable[ReviewDecision]) -> list[ReviewDecision]:
    return [
        d
        for d in decisions
        if d.kind == DECISION_FINDING and d.blocking and d.disposition == DISPOSITION_UNRESOLVED
    ]


def evaluate_approval_admissible(
    source_request_seq: int, decisions: Iterable[ReviewDecision]
) -> AdmissionResult:
    """Pre-approval reread fence (#13518 R2-F7): refuse a stale approval.

    ``source_request_seq`` is the ``seq`` of the review request the approving consumer is acting on.
    Re-reading the source issue, if ANY unresolved blocking finding is **newer** than that request
    (``seq > source_request_seq``), the approval is refused with :data:`REASON_NEWER_BLOCKING_FINDING`
    — exactly the #13586 case where B's snapshot predated A's finding. Otherwise admissible. Pure.
    """
    for d in _unresolved_blocking(decisions):
        if int(d.seq) > int(source_request_seq):
            return AdmissionResult(False, REASON_NEWER_BLOCKING_FINDING)
    return AdmissionResult(True, REASON_OK)


def evaluate_integration_admissible(
    latest_generation: ReviewGeneration, decisions: Iterable[ReviewDecision]
) -> AdmissionResult:
    """Integration latest-generation fence (#13518 R2-F7).

    Integration requires the LATEST generation to be **approved** AND carry **no** unresolved
    blocking finding — never merely "an approval exists somewhere". A stale approval for an older
    generation, or a newer unresolved blocking finding, blocks integration. Pure.
    """
    latest = [d for d in decisions if d.generation == latest_generation]
    if _unresolved_blocking(latest):
        return AdmissionResult(False, REASON_UNRESOLVED_BLOCKING_FINDING)
    if not any(d.kind == DECISION_APPROVAL for d in latest):
        return AdmissionResult(False, REASON_NO_APPROVAL_FOR_LATEST)
    return AdmissionResult(True, REASON_OK)


class GenerationLeaseError(RuntimeError):
    """A lease store operation failed (fail-closed: treat as not-acquired)."""


@dataclass
class GenerationLease:
    """A compare-and-set lease so at most one consumer processes a review generation (#13518 R2-F7).

    ``store`` is an injectable mapping ``generation identity -> consumer id`` (a dict in tests; a
    durable home-scoped map in production). :meth:`acquire` is CAS: it succeeds only if the
    generation is unheld OR already held by the same consumer (idempotent re-acquire); a DIFFERENT
    consumer is refused, so a duplicate consumer of the same generation is never admitted. This
    fences the *review-decision commit* concurrency separately from callback-transport duplication.
    """

    store: MutableMapping[str, str] = field(default_factory=dict)

    def acquire(self, generation: ReviewGeneration, consumer_id: str) -> bool:
        """CAS-acquire the lease for ``generation``; True iff this consumer now holds it."""
        cid = str(consumer_id or "").strip()
        if not cid:
            raise GenerationLeaseError("a lease requires a non-empty consumer id")
        held = self.store.get(generation.identity)
        if held is None:
            self.store[generation.identity] = cid
            return True
        return held == cid  # idempotent for the same consumer; refuse a different one

    def holder(self, generation: ReviewGeneration) -> Optional[str]:
        return self.store.get(generation.identity)


__all__ = (
    "DECISION_FINDING",
    "DECISION_APPROVAL",
    "DISPOSITION_UNRESOLVED",
    "DISPOSITION_RESOLVED",
    "ReviewGeneration",
    "ReviewDecision",
    "AdmissionResult",
    "REASON_OK",
    "REASON_NEWER_BLOCKING_FINDING",
    "REASON_NO_APPROVAL_FOR_LATEST",
    "REASON_UNRESOLVED_BLOCKING_FINDING",
    "evaluate_approval_admissible",
    "evaluate_integration_admissible",
    "GenerationLease",
    "GenerationLeaseError",
)
