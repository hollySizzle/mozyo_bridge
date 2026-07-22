"""Enveloped ``integration_disposition`` evidence for the staging-integrated conjunct (#14219 T2b, step 3b).

The design ruling (#14219 j#85530 Q2) rules that the integration grammar is EXTENDED but the
#14213 glance projection is NOT. Two things follow, and this module is both of them:

1. **Source head and integration head are separate values.** The pre-existing disposition record
   carries at most one "head", which is ambiguous: under ``patch_equivalent`` (cherry-pick /
   rebase-onto-staging) the reviewed lane head and the commit that proves integration on the
   staging branch are DIFFERENT commits. The T1 basis needs the reviewed head (to compare against
   the candidate's ``bound_head``) *and* the staging proof. So the enveloped marker carries
   ``head`` = the reviewed lane/source head (inside the common lane envelope, step 1),
   ``integration_head`` = the exact commit that proved integration on ``integration_branch``.

2. **The additive fields are invisible to the glance.** ``glance_integration_disposition`` reads
   only ``gate`` and ``disposition`` off the marker, so an enveloped marker folds byte-identically
   to the legacy one (regression-tested). This module is the dedicated strict reader T2b uses; the
   glance keeps its own lenient historical vocabulary.

Fail-closed everywhere, and in particular on the LEGACY marker: a ``gate=integration_disposition``
marker without the envelope is valid for the glance but is NOT auto-hibernate evidence, and it is a
hard error here rather than a skipped record. Skipping it would let an older enveloped ``merge``
survive a newer legacy ``explicit_deferral`` — the supersession-by-existence defect #14213 F1 fixed
for work-unit declarations. A record someone durably wrote must never be silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .glance_integration_disposition import (
    MARKER_GATE_INTEGRATION_DISPOSITION,
    canonical_disposition,
)
from .hibernate_evidence_envelope import (
    EnvelopeParseError,
    LaneEvidenceEnvelope,
    is_full_sha,
    parse_lane_envelope,
    render_lane_envelope,
)
from .hibernate_evidence_marker import MARKER_CHANNEL_WORKFLOW_EVENT
from .sublane_admission import INTEGRATION_MERGE, INTEGRATION_PATCH_EQUIVALENT

FIELD_INTEGRATION_HEAD = "integration_head"
FIELD_INTEGRATION_BRANCH = "integration_branch"
FIELD_DISPOSITION = "disposition"

#: The dispositions that actually put the work on the integration branch. ``explicit_deferral`` and
#: ``integration_blocked`` are durable dispositions but they are the OPPOSITE of integrated, so they
#: are a typed zero here, not evidence.
INTEGRATED_DISPOSITIONS = frozenset({INTEGRATION_MERGE, INTEGRATION_PATCH_EQUIVALENT})

# Closed parse / resolve failure vocabulary (in addition to the envelope's own reasons).
INTEGRATION_MISSING_INTEGRATION_HEAD = "integration_missing_integration_head"
INTEGRATION_MALFORMED_INTEGRATION_HEAD = "integration_malformed_integration_head"
INTEGRATION_MISSING_BRANCH = "integration_missing_branch"
INTEGRATION_MALFORMED_BRANCH = "integration_malformed_branch"
INTEGRATION_MISSING_DISPOSITION = "integration_missing_disposition"
INTEGRATION_NOT_INTEGRATED = "integration_not_integrated"
INTEGRATION_EVIDENCE_ABSENT = "integration_evidence_absent"
INTEGRATION_EVIDENCE_CONFLICT = "integration_evidence_conflict"

INTEGRATION_EVIDENCE_REASONS = frozenset({
    INTEGRATION_MISSING_INTEGRATION_HEAD,
    INTEGRATION_MALFORMED_INTEGRATION_HEAD,
    INTEGRATION_MISSING_BRANCH,
    INTEGRATION_MALFORMED_BRANCH,
    INTEGRATION_MISSING_DISPOSITION,
    INTEGRATION_NOT_INTEGRATED,
    INTEGRATION_EVIDENCE_ABSENT,
    INTEGRATION_EVIDENCE_CONFLICT,
})

#: Characters a branch ref may never contain, because the marker body is split on ``:`` and
#: terminated by ``]`` — a value carrying either would silently truncate into a different field set.
_BRANCH_FORBIDDEN = (":", "]", "[", " ", "\t")


@dataclass(frozen=True)
class IntegrationEvidence:
    """One parsed enveloped integration disposition: the reviewed head plus the staging proof."""

    envelope: LaneEvidenceEnvelope
    integration_head: str
    integration_branch: str
    disposition: str

    @property
    def source_head(self) -> str:
        """The reviewed lane head — the value T1 compares against the candidate's bound head."""
        return self.envelope.head

    def as_payload(self) -> dict:
        return {
            "envelope": self.envelope.as_payload(),
            FIELD_INTEGRATION_HEAD: self.integration_head,
            FIELD_INTEGRATION_BRANCH: self.integration_branch,
            FIELD_DISPOSITION: self.disposition,
        }


@dataclass(frozen=True)
class IntegrationEvidenceError:
    """A typed integration-evidence parse / resolve failure — a zero, never a lenient default."""

    reason: str
    detail: str = ""


def render_integration_evidence(
    *,
    envelope: LaneEvidenceEnvelope,
    integration_head: str,
    integration_branch: str,
    disposition: str,
) -> str:
    """Render the enveloped ``integration_disposition`` marker, fail-closed.

    ``envelope.head`` is the reviewed lane/source head and is required; ``integration_head`` is the
    exact staging commit; ``disposition`` must be one of :data:`INTEGRATED_DISPOSITIONS` in its
    canonical spelling. A producer programming error raises ``ValueError`` — an unrenderable
    evidence marker must never be emitted.
    """
    if not envelope.head:
        raise ValueError("integration evidence requires a head-bearing envelope (the reviewed head)")
    head = str(integration_head or "").strip()
    if not is_full_sha(head):
        raise ValueError(f"integration_head must be a full lowercase SHA, got {integration_head!r}")
    branch = str(integration_branch or "").strip()
    if not branch or any(bad in branch for bad in _BRANCH_FORBIDDEN):
        raise ValueError(f"integration_branch must be a bare ref, got {integration_branch!r}")
    token = str(disposition or "").strip()
    if token not in INTEGRATED_DISPOSITIONS:
        raise ValueError(f"integration evidence disposition must be integrated, got {disposition!r}")
    fields = [
        f"gate={MARKER_GATE_INTEGRATION_DISPOSITION}",
        render_lane_envelope(envelope),
        f"{FIELD_INTEGRATION_HEAD}={head}",
        f"{FIELD_INTEGRATION_BRANCH}={branch}",
        f"{FIELD_DISPOSITION}={token}",
    ]
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{':'.join(fields)}]"


def parse_integration_evidence(
    fields: Mapping[str, str],
) -> "IntegrationEvidence | IntegrationEvidenceError":
    """Parse one marker's field mapping as enveloped integration evidence, fail-closed.

    The lane envelope is parsed strictly with ``require_head=True`` (a lane-unbound legacy marker
    fails here, which is the ruling's intent); then ``integration_head`` must be a full lowercase
    SHA, ``integration_branch`` a bare ref, and ``disposition`` must MEAN integrated.

    The disposition token is canonicalized through the glance's :func:`canonical_disposition`
    rather than through a second alias table — one durable vocabulary, no drift between what the
    glance reads and what auto-hibernate accepts — and only ``merge`` / ``patch_equivalent``
    survive. ``explicit_deferral`` / ``integration_blocked`` / an unreadable value are
    :data:`INTEGRATION_NOT_INTEGRATED`, never a lenient pass.
    """
    bound = parse_lane_envelope(fields, require_head=True)
    if isinstance(bound, EnvelopeParseError):
        return IntegrationEvidenceError(bound.reason, bound.detail)

    head = str(fields.get(FIELD_INTEGRATION_HEAD, "") or "").strip()
    if not head:
        return IntegrationEvidenceError(INTEGRATION_MISSING_INTEGRATION_HEAD)
    if not is_full_sha(head):
        return IntegrationEvidenceError(INTEGRATION_MALFORMED_INTEGRATION_HEAD, head)

    branch = str(fields.get(FIELD_INTEGRATION_BRANCH, "") or "").strip()
    if not branch:
        return IntegrationEvidenceError(INTEGRATION_MISSING_BRANCH)
    if any(bad in branch for bad in _BRANCH_FORBIDDEN):
        return IntegrationEvidenceError(INTEGRATION_MALFORMED_BRANCH, branch)

    raw = str(fields.get(FIELD_DISPOSITION, "") or "").strip()
    if not raw:
        return IntegrationEvidenceError(INTEGRATION_MISSING_DISPOSITION)
    token = canonical_disposition(raw)
    if token not in INTEGRATED_DISPOSITIONS:
        return IntegrationEvidenceError(INTEGRATION_NOT_INTEGRATED, raw)

    return IntegrationEvidence(
        envelope=bound,
        integration_head=head,
        integration_branch=branch,
        disposition=token,
    )


def resolve_integration_evidence(
    markers: Sequence[Mapping[str, str]],
) -> "IntegrationEvidence | IntegrationEvidenceError":
    """Fold every ``integration_disposition`` marker to one evidence, fail-closed.

    Zero → :data:`INTEGRATION_EVIDENCE_ABSENT`; identical duplicates collapse; any two DIFFERING →
    :data:`INTEGRATION_EVIDENCE_CONFLICT`. A marker OF this gate that does not parse — including a
    legacy lane-unbound one and a durable ``explicit_deferral`` — is a HARD error, not a skipped
    record: skipping would let an older enveloped ``merge`` outlive a newer deferral.
    """
    parsed: list[IntegrationEvidence] = []
    for fields in markers:
        gate = str(fields.get("gate", "") or fields.get("kind", "") or "").strip()
        if gate != MARKER_GATE_INTEGRATION_DISPOSITION:
            continue
        one = parse_integration_evidence(fields)
        if isinstance(one, IntegrationEvidenceError):
            return one
        parsed.append(one)
    if not parsed:
        return IntegrationEvidenceError(INTEGRATION_EVIDENCE_ABSENT)
    first = parsed[0]
    for other in parsed[1:]:
        if other != first:
            return IntegrationEvidenceError(INTEGRATION_EVIDENCE_CONFLICT)
    return first


__all__ = (
    "FIELD_DISPOSITION",
    "FIELD_INTEGRATION_BRANCH",
    "FIELD_INTEGRATION_HEAD",
    "INTEGRATED_DISPOSITIONS",
    "INTEGRATION_EVIDENCE_REASONS",
    "IntegrationEvidence",
    "IntegrationEvidenceError",
    "parse_integration_evidence",
    "render_integration_evidence",
    "resolve_integration_evidence",
)
