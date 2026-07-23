"""Who was allowed to write this evidence (Redmine #14219 T2b, R1-F2).

The design ruling (#14219 j#85530 Q3) fixes one issuer per evidence kind: the review result comes
from the same-lane reviewer / gateway, the integration / CI / dogfood records from the coordinator,
the park declaration from the lane's own implementation worker. The producer originally derived the
authority *provenance* from the marker's ``gate=`` alone, which means the marker asserted its own
issuer — any actor able to write a journal could emit a coordinator-shaped ``required_ci_green``
and it would be read as ``durable_ci_record``. A marker's self-declared authority is not authority.

So the WRITER travels with the record, not inside it. A :class:`EvidenceJournal` carries the
``issuer_role`` the journal PORT resolved from the durable source's own author metadata (the
Redmine journal's user), and :func:`check_issuer` refuses a record whose writer is not the actor
that kind's authority belongs to.

Fail-closed twice over:

* :data:`ISSUER_UNKNOWN` is the default. A port that cannot resolve the writer — or a caller that
  supplies bare ``(journal_id, notes)`` pairs — yields evidence that satisfies nothing. Blocking is
  the safe direction: an unresolved writer means we do not know whether the authority holds.
* The role vocabulary is closed. An unrecognised role is not passed through to be compared
  hopefully against the expected one; it is the same typed zero as a mismatch.

The mapping from a concrete durable identity (a Redmine user) to one of these roles is the PORT's
job, not this module's: it depends on the workspace's role bindings, which live in configuration
rather than in the evidence grammar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

from .glance_integration_disposition import MARKER_GATE_INTEGRATION_DISPOSITION
from .hibernate_evidence_marker import (
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_PARK_DECLARED,
    EVIDENCE_REQUIRED_CI_GREEN,
)

MARKER_GATE_REVIEW_RESULT = "review_result"

# The closed issuer-role vocabulary (ruling j#85530 Q3).
ISSUER_COORDINATOR = "coordinator"
#: The same-lane reviewer / implementation gateway that writes the canonical Review Result.
ISSUER_REVIEW_GATEWAY = "review_gateway"
#: The lane's own implementation worker (the only actor that may declare ITS lane parked).
ISSUER_LANE_WORKER = "lane_worker"
#: The writer could not be resolved from the durable source — fail-closed, never a wildcard.
ISSUER_UNKNOWN = "unknown"

ISSUER_ROLES = frozenset({
    ISSUER_COORDINATOR,
    ISSUER_REVIEW_GATEWAY,
    ISSUER_LANE_WORKER,
    ISSUER_UNKNOWN,
})

#: The one actor whose record counts as each evidence kind's authority.
_KIND_ISSUER = {
    MARKER_GATE_REVIEW_RESULT: ISSUER_REVIEW_GATEWAY,
    MARKER_GATE_INTEGRATION_DISPOSITION: ISSUER_COORDINATOR,
    EVIDENCE_REQUIRED_CI_GREEN: ISSUER_COORDINATOR,
    EVIDENCE_DOGFOOD_DELEGATED: ISSUER_COORDINATOR,
    EVIDENCE_PARK_DECLARED: ISSUER_LANE_WORKER,
}

# Typed refusals (both are zero-actuation gaps, and they stay distinguishable: "we do not know who
# wrote it" is a different operational problem from "the wrong actor wrote it").
ISSUER_UNRESOLVED = "evidence_issuer_unresolved"
ISSUER_MISMATCH = "evidence_issuer_mismatch"


@dataclass(frozen=True)
class EvidenceJournal:
    """One durable journal as the hibernate-evidence producer reads it.

    ``issuer_role`` is resolved by the journal port from the record's own author, NOT from the note
    body: a note can claim anything, while the author is the source system's fact.
    """

    journal_id: str
    notes: str
    issuer_role: str = ISSUER_UNKNOWN

    def as_pair(self) -> Tuple[str, str]:
        """The ``(journal_id, notes)`` shape the pure marker folds consume."""
        return (self.journal_id, self.notes)


def as_pairs(journals: Sequence[EvidenceJournal]) -> tuple[Tuple[str, str], ...]:
    """Project to ``(journal_id, notes)`` for the folds that only read note bodies."""
    return tuple(journal.as_pair() for journal in journals)


def unattributed(pairs: Sequence[Tuple[object, str]]) -> tuple[EvidenceJournal, ...]:
    """Wrap bare ``(journal_id, notes)`` pairs as EXPLICITLY unattributed evidence.

    For callers with no author metadata. Every record is :data:`ISSUER_UNKNOWN`, so nothing it
    contains can satisfy a conjunct — the conversion is deliberately lossy in the safe direction
    rather than letting an un-typed pair sneak past the issuer check.
    """
    return tuple(
        EvidenceJournal(journal_id=str(journal_id), notes=notes or "")
        for journal_id, notes in pairs or ()
    )


def check_issuer(kind: str, role: str) -> "str | None":
    """The typed refusal reason for a ``kind`` record written by ``role``, or ``None`` if allowed."""
    expected = _KIND_ISSUER.get(kind)
    if expected is None:
        return ISSUER_MISMATCH
    if role not in ISSUER_ROLES or role == ISSUER_UNKNOWN:
        return ISSUER_UNRESOLVED
    return None if role == expected else ISSUER_MISMATCH


__all__ = [
    "ISSUER_COORDINATOR",
    "ISSUER_LANE_WORKER",
    "ISSUER_MISMATCH",
    "ISSUER_REVIEW_GATEWAY",
    "ISSUER_ROLES",
    "ISSUER_UNKNOWN",
    "ISSUER_UNRESOLVED",
    "MARKER_GATE_REVIEW_RESULT",
    "EvidenceJournal",
    "as_pairs",
    "check_issuer",
    "unattributed",
]
