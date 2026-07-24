"""Who was allowed to write this evidence (Redmine #14219 T2b, R1-F2).

The design ruling (#14219 j#85530 Q3) fixes one issuer per evidence kind: the review result comes
from the same-lane reviewer / gateway, the integration / CI / dogfood records from the coordinator,
the park declaration from the lane's own implementation worker. The producer originally derived the
authority *provenance* from the marker's ``gate=`` alone, which means the marker asserted its own
issuer — any actor able to write a journal could emit a coordinator-shaped ``required_ci_green``
and it would be read as ``durable_ci_record``. A marker's self-declared authority is not authority.

So the WRITER travels with the record, not inside it. A :class:`EvidenceJournal` carries the
:class:`ResolvedIssuer` the journal PORT resolved for that record, and :func:`check_issuer` refuses
a record whose writer is not the actor that kind's authority belongs to.

**A role alone is not the authority** (checkpoint j#86443 R2-F2). The ruling fixes the review result
to the SAME-LANE gateway and the park declaration to THAT lane's worker, so an issuer that is only
``lane_worker`` cannot express the contract: a worker on a different lane (or on a superseded
generation of this one) carries the identical token, and by writing the target lane's envelope it
would pass. The resolved issuer therefore carries its own ``workspace`` / ``lane`` /
``lane_generation``, and the check requires them to EXACT-MATCH the envelope the evidence declares.
The evidence says which lane it is about; the issuer says which lane its writer holds authority
over; only when those agree is the record that lane's authority speaking.

``authority_anchor`` is the durable record the port resolved that binding FROM, and it is required
to be non-empty. This is deliberately not decorative: in this very workspace every governed journal
— gateway-written and worker-written alike — carries the same source-system author, so "the Redmine
user who wrote it" cannot by itself identify the actor (measured on #14219's own journals). A port
that cannot name the record binding a writer to a lane role has not resolved the writer, and
:data:`ISSUER_UNRESOLVED` is the honest answer.

Fail-closed three times over:

* :data:`ISSUER_UNKNOWN` is the default. A port that cannot resolve the writer — or a caller that
  supplies bare ``(journal_id, notes)`` pairs — yields evidence that satisfies nothing. Blocking is
  the safe direction: an unresolved writer means we do not know whether the authority holds.
* The role vocabulary is closed. An unrecognised role is not passed through to be compared
  hopefully against the expected one; it is the same typed zero as a mismatch.
* An issuer with no lane identity, no positive generation, or no authority anchor is unresolved —
  a partially-filled issuer never counts as a partially-satisfied authority.

Which durable record binds a writer to a lane role is the PORT's decision (it depends on the
workspace's role bindings and dispatch records, not on the evidence grammar). What this module
fixes is that such a record must exist, must be named, and must be about THIS lane.
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

def contract_writer_role(gate: str) -> str:
    """The ONE role the ruling names as ``gate``'s canonical writer (j#85530 Q3), else unknown.

    The single gate->role authority: the issuer-policy resolution (Redmine #14219 T2c Fork A)
    reads it from here so no second mapping can drift from the producer's own expectation.
    """
    return _KIND_ISSUER.get(gate, ISSUER_UNKNOWN)


# Typed refusals (both are zero-actuation gaps, and they stay distinguishable: "we do not know who
# wrote it" is a different operational problem from "the wrong actor wrote it").
ISSUER_UNRESOLVED = "evidence_issuer_unresolved"
ISSUER_MISMATCH = "evidence_issuer_mismatch"


@dataclass(frozen=True)
class ResolvedIssuer:
    """Who wrote a record, and over WHICH lane they hold that role.

    Resolved by the journal port from durable records, never from the note body: a note can claim
    anything. ``authority_anchor`` names the record the port resolved the binding from, so the
    claim "this writer is that lane's worker" is itself traceable to something durable.
    """

    role: str = ISSUER_UNKNOWN
    workspace: str = ""
    lane: str = ""
    lane_generation: int = 0
    authority_anchor: str = ""

    @property
    def is_anchored(self) -> bool:
        """Whether the port named the durable record it resolved this role from.

        Required of EVERY role, not only the lane-scoped ones (checkpoint j#86503 R3-F2). The
        coordinator's authority is workspace-level rather than lane-level, but that says nothing
        about how the WRITER was identified: with one source-system author behind several roles, a
        bare ``role="coordinator"`` is an assertion, not a resolution. Leaving the anchor optional
        for the coordinator left the shared-author ambiguity open on three of the five gates
        (integration / CI / dogfood) — the very ambiguity the anchor exists to close.
        """
        return bool(self.authority_anchor)

    @property
    def is_lane_bound(self) -> bool:
        """Whether the issuer names a complete, positively-generationed lane with an anchor."""
        return bool(
            self.workspace
            and self.lane
            and isinstance(self.lane_generation, int)
            and not isinstance(self.lane_generation, bool)
            and self.lane_generation > 0
            and self.is_anchored
        )

    def covers(self, envelope) -> bool:
        """Whether this writer's authority is over the exact lane the evidence is about."""
        return (
            self.is_lane_bound
            and self.workspace == getattr(envelope, "workspace", "")
            and self.lane == getattr(envelope, "lane", "")
            and self.lane_generation == getattr(envelope, "lane_generation", 0)
        )


#: The roles whose authority is lane-scoped: the ruling binds the review result to the SAME-LANE
#: gateway and the park declaration to THAT lane's worker. The coordinator's authority is
#: workspace-level (integration / CI / dogfood are not the lane's own claims about itself), so it is
#: not required to name a lane.
_LANE_SCOPED_ROLES = frozenset({ISSUER_REVIEW_GATEWAY, ISSUER_LANE_WORKER})


@dataclass(frozen=True)
class EvidenceJournal:
    """One durable journal as the hibernate-evidence producer reads it.

    ``issuer`` is resolved by the journal port from the record's own author and the durable records
    binding that author to a lane role — NOT from the note body.
    """

    journal_id: str
    notes: str
    issuer: ResolvedIssuer = ResolvedIssuer()

    @property
    def issuer_role(self) -> str:
        return self.issuer.role

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


def check_issuer_resolution(kind: str, issuer: ResolvedIssuer) -> "str | None":
    """The refusal knowable WITHOUT the evidence's envelope: is this writer that kind's authority?

    Checked before a word of the record's content is read, so the marker never gets to influence
    who is treated as its author:

    1. the kind must be one this module has an authority for at all;
    2. the writer must be RESOLVED — a known role, an authority anchor naming the record the role
       was resolved from (EVERY role, including the coordinator), and for a lane-scoped role a
       complete lane identity as well (an unresolved writer is not a wrong writer);
    3. the role must be the one that kind's authority belongs to.

    The remaining question — whether that authority is over the lane the evidence is ABOUT — needs
    the envelope and is :func:`check_issuer_lane`.
    """
    expected = _KIND_ISSUER.get(kind)
    if expected is None:
        return ISSUER_MISMATCH
    role = issuer.role
    if role not in ISSUER_ROLES or role == ISSUER_UNKNOWN:
        return ISSUER_UNRESOLVED
    if not issuer.is_anchored:
        return ISSUER_UNRESOLVED
    if role in _LANE_SCOPED_ROLES and not issuer.is_lane_bound:
        return ISSUER_UNRESOLVED
    return None if role == expected else ISSUER_MISMATCH


def check_issuer_lane(kind: str, issuer: ResolvedIssuer, envelope) -> "str | None":
    """The refusal that needs the evidence's envelope: is the authority over THIS lane?

    Checkpoint j#86443 R2-F2: a worker on another lane — or on a superseded generation of this one
    — holds the same role token, so role equality alone let a foreign writer declare this lane
    parked simply by writing this lane's envelope. Lane-scoped authority must cover the exact lane
    the evidence declares. The coordinator's authority is workspace-level and is not lane-compared.
    """
    if _KIND_ISSUER.get(kind) not in _LANE_SCOPED_ROLES:
        return None
    return None if issuer.covers(envelope) else ISSUER_MISMATCH


def check_issuer(kind: str, issuer: ResolvedIssuer, *, envelope) -> "str | None":
    """Both halves of the issuer check. ``envelope`` is required — there is no lenient default."""
    return check_issuer_resolution(kind, issuer) or check_issuer_lane(kind, issuer, envelope)


__all__ = [
    "ISSUER_COORDINATOR",
    "ResolvedIssuer",
    "ISSUER_LANE_WORKER",
    "ISSUER_MISMATCH",
    "ISSUER_REVIEW_GATEWAY",
    "ISSUER_ROLES",
    "ISSUER_UNKNOWN",
    "ISSUER_UNRESOLVED",
    "MARKER_GATE_REVIEW_RESULT",
    "contract_writer_role",
    "EvidenceJournal",
    "as_pairs",
    "check_issuer",
    "check_issuer_lane",
    "check_issuer_resolution",
    "unattributed",
]
