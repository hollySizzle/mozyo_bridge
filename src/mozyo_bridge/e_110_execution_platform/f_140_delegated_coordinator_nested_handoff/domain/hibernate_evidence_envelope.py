"""Common lane evidence envelope for hibernate-basis producers (Redmine #14219 T2b, step 1).

Every durable conjunct event a hibernate candidate consumes (review-approved, staging-integrated,
required-CI-green, dogfood-delegated, park-declared) must bind to the candidate's EXACT lane —
``workspace`` + ``lane`` + ``lane_generation`` — and, for a head-bearing conjunct, the exact
``head``. The design ruling (#14219 j#85530) makes this a common envelope on ALL those markers:
lane-unbound evidence (e.g. the current ``review_result`` marker, which carries only ``head`` +
``req``) cannot be reused, because issue-only correlation or completion from the current lifecycle
row would promote a superseded generation's evidence onto the live generation.

This module is the PURE, strict grammar for that envelope — a dedicated hibernate-evidence surface,
deliberately separate from the #14213 glance marker vocabulary (which stays unchanged). It parses an
already-extracted marker field mapping into a typed :class:`LaneEvidenceEnvelope`, or a typed
:class:`EnvelopeParseError`; it renders one back to the ``key=value:...`` marker-field form; and it
resolves multiple envelopes for one conjunct to a single one or a typed conflict. Everything is
fail-closed: a missing / malformed / non-full-SHA / non-positive-generation / conflicting envelope
is a typed zero, never a lenient default and never a prose fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

#: A canonical full commit hash: 40 hex (sha1) or 64 hex (sha256), lowercase. A truncated /
#: abbreviated / uppercase / non-hex head is rejected, matching the repo-wide convention
#: (``patch_equivalent_integration._FULL_SHA_RE`` / ``review_return_route``).
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

# Envelope field keys (the marker fields the renderer emits and the parser reads).
FIELD_WORKSPACE = "workspace"
FIELD_LANE = "lane"
FIELD_LANE_GENERATION = "lane_generation"
FIELD_HEAD = "head"

# Closed vocabulary of parse-failure reasons — every one is a typed zero-actuation.
ENVELOPE_MISSING_WORKSPACE = "envelope_missing_workspace"
ENVELOPE_MISSING_LANE = "envelope_missing_lane"
ENVELOPE_MISSING_GENERATION = "envelope_missing_generation"
ENVELOPE_MALFORMED_GENERATION = "envelope_malformed_generation"
ENVELOPE_MISSING_HEAD = "envelope_missing_head"
ENVELOPE_MALFORMED_HEAD = "envelope_malformed_head"

LANE_ENVELOPE_PARSE_REASONS = frozenset({
    ENVELOPE_MISSING_WORKSPACE,
    ENVELOPE_MISSING_LANE,
    ENVELOPE_MISSING_GENERATION,
    ENVELOPE_MALFORMED_GENERATION,
    ENVELOPE_MISSING_HEAD,
    ENVELOPE_MALFORMED_HEAD,
})

# Resolution reasons when folding multiple envelopes for one conjunct.
ENVELOPE_ABSENT = "envelope_absent"
ENVELOPE_CONFLICT = "envelope_conflict"

LANE_ENVELOPE_RESOLVE_REASONS = frozenset({ENVELOPE_ABSENT, ENVELOPE_CONFLICT})


@dataclass(frozen=True)
class LaneEvidenceEnvelope:
    """The exact lane (and optionally head) a durable conjunct event is bound to."""

    workspace: str
    lane: str
    lane_generation: int
    head: str = ""

    def as_payload(self) -> dict:
        return {
            FIELD_WORKSPACE: self.workspace,
            FIELD_LANE: self.lane,
            FIELD_LANE_GENERATION: self.lane_generation,
            FIELD_HEAD: self.head,
        }


@dataclass(frozen=True)
class EnvelopeParseError:
    """A typed parse / resolve failure — a hibernate-evidence zero, never a lenient default."""

    reason: str
    detail: str = ""


def parse_lane_envelope(
    fields: Mapping[str, str], *, require_head: bool
) -> "LaneEvidenceEnvelope | EnvelopeParseError":
    """Parse the common lane envelope from a marker's field mapping, fail-closed.

    ``workspace`` / ``lane`` must be non-empty; ``lane_generation`` must be a POSITIVE integer.
    A ``head``, if present, must be a full 40/64-hex lowercase SHA — ALWAYS (a malformed head is
    rejected even for a non-head-bearing conjunct). ``require_head`` additionally requires the head
    to be present (a head-bearing conjunct with no head is :data:`ENVELOPE_MISSING_HEAD`).
    """
    workspace = str(fields.get(FIELD_WORKSPACE, "") or "").strip()
    if not workspace:
        return EnvelopeParseError(ENVELOPE_MISSING_WORKSPACE)
    lane = str(fields.get(FIELD_LANE, "") or "").strip()
    if not lane:
        return EnvelopeParseError(ENVELOPE_MISSING_LANE)

    generation_raw = str(fields.get(FIELD_LANE_GENERATION, "") or "").strip()
    if not generation_raw:
        return EnvelopeParseError(ENVELOPE_MISSING_GENERATION)
    if not generation_raw.isdigit() or int(generation_raw) <= 0:
        return EnvelopeParseError(ENVELOPE_MALFORMED_GENERATION, generation_raw)
    generation = int(generation_raw)

    head = str(fields.get(FIELD_HEAD, "") or "").strip()
    if head and not _FULL_SHA_RE.match(head):
        return EnvelopeParseError(ENVELOPE_MALFORMED_HEAD, head)
    if require_head and not head:
        return EnvelopeParseError(ENVELOPE_MISSING_HEAD)

    return LaneEvidenceEnvelope(
        workspace=workspace, lane=lane, lane_generation=generation, head=head
    )


def render_lane_envelope(envelope: LaneEvidenceEnvelope) -> str:
    """Render the envelope to the ``key=value:...`` marker-field form (``head`` omitted if empty)."""
    parts = [
        f"{FIELD_WORKSPACE}={envelope.workspace}",
        f"{FIELD_LANE}={envelope.lane}",
        f"{FIELD_LANE_GENERATION}={envelope.lane_generation}",
    ]
    if envelope.head:
        parts.append(f"{FIELD_HEAD}={envelope.head}")
    return ":".join(parts)


def resolve_lane_envelope(
    envelopes: Sequence[LaneEvidenceEnvelope],
) -> "LaneEvidenceEnvelope | EnvelopeParseError":
    """Fold the envelopes parsed for ONE conjunct to a single one, fail-closed on conflict.

    Zero → :data:`ENVELOPE_ABSENT` (no durable evidence). Identical duplicates collapse to that one
    envelope (a re-emitted marker is fine). Any two DIFFERING envelopes → :data:`ENVELOPE_CONFLICT`
    (a superseded / cross-lane record must never be silently preferred).
    """
    if not envelopes:
        return EnvelopeParseError(ENVELOPE_ABSENT)
    first = envelopes[0]
    for other in envelopes[1:]:
        if other != first:
            return EnvelopeParseError(ENVELOPE_CONFLICT)
    return first
