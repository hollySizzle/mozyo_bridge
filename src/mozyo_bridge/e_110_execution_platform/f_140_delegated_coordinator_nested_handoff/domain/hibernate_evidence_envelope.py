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


def is_full_sha(value: object) -> bool:
    """True when ``value`` is a canonical full lowercase commit hash (pure).

    The single head-shape predicate for the hibernate-evidence surface, so every head a marker
    carries — the envelope's own ``head`` and the additive ``integration_head`` (step 3b) — is
    judged by the same rule and cannot drift apart.
    """
    return bool(_FULL_SHA_RE.match(str(value or "").strip()))


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
    if head and not is_full_sha(head):
        return EnvelopeParseError(ENVELOPE_MALFORMED_HEAD, head)
    if require_head and not head:
        return EnvelopeParseError(ENVELOPE_MISSING_HEAD)

    return LaneEvidenceEnvelope(
        workspace=workspace, lane=lane, lane_generation=generation, head=head
    )


#: The PUNCTUATION no marker-field VALUE may contain: the body is split on ``:`` and delimited by
#: ``[`` / ``]``. A value carrying one would silently split into a bogus extra field or truncate
#: the marker — field injection from a producer-supplied id. Whitespace is forbidden too but is
#: NOT enumerated here (see :func:`contains_marker_separator`): the space / tab pair this tuple
#: used to carry was an incomplete enumeration of "空白" and let a newline through (Redmine #14694).
MARKER_FORBIDDEN_CHARS = (":", "]", "[", " ", "\t")


def contains_marker_separator(value: object) -> bool:
    """Whether ``value`` carries marker punctuation or ANY whitespace (pure).

    THE one predicate for the whole hibernate-evidence surface — the envelope's workspace / lane,
    the marker's kind-specific fields, the integration branch, and the CLI's own typed refusal —
    so "which characters a producer-supplied token may not carry" cannot drift apart between them.

    Whitespace is asked as ``str.isspace()`` rather than matched against a literal tuple. The
    central `### Hibernate Evidence Marker Contract` forbids "marker separator (``:`` ``]`` ``[``
    空白)", and 空白 is not two characters: markers are scanned PER LINE, so a value carrying a
    newline is rendered into a marker that never closes on its line and reads back as nothing at
    all. Enumerating space and tab (Redmine #14694) let ``\\n`` / ``\\r`` / ``\\xa0`` through — a
    value the strip-then-check order then hid entirely when the whitespace was leading or trailing.
    """
    text = str(value)
    return any(bad in text for bad in MARKER_FORBIDDEN_CHARS) or any(
        char.isspace() for char in text
    )


def reject_marker_separator(value: str, *, field: str) -> None:
    """Raise when ``value`` carries a marker separator (pure guard for every renderer).

    One rule for every producer-supplied token — the envelope's workspace / lane and the
    integration marker's branch — so a value that would truncate or inject a field can never be
    rendered from any of them. The membership question is :func:`contains_marker_separator`; this
    only names the offending character in the producer error.
    """
    text = str(value)
    for separator in MARKER_FORBIDDEN_CHARS:
        if separator in text:
            raise ValueError(f"{field} must not contain the marker separator {separator!r}")
    for char in text:
        if char.isspace():
            raise ValueError(f"{field} must not contain the whitespace character {char!r}")


def require_marker_token(value: object, *, field: str, requirement: str) -> str:
    """The RAW value as a marker token, or a producer error (pure).

    THE raw-input validator every hibernate-evidence renderer shares. It validates what the caller
    ACTUALLY passed — it never trims it, never coerces it, and never substitutes a default:

    - a non-``str`` is a producer error, not something to render through ``str()``. ``run=12345``
      and ``run=True`` are not run ids the caller can read back out of the marker, and ``None`` /
      ``0`` are not "absent" — falsy-to-empty coercion turned a wrong TYPE into a wrong VALUE;
    - the emptiness test is on the raw value, so a whitespace-only token is empty-after-trim to
      nobody: it reaches the separator check and is refused as what it is;
    - marker punctuation and whitespace are refused on the raw value.

    Redmine #14694: the previous ``str(value or "").strip()`` did all three of those wrong at once,
    and its worst reading was the ordinary one — ``workflow=" check "`` was trimmed into the clean
    canonical token ``check`` and became durable auto-hibernate authority. A producer that
    normalizes raw input into a value the caller did not write is asserting something nobody
    claimed; the central `### Hibernate Evidence Marker Contract` requires the producer error to
    surface at write time instead.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"{requirement} requires a string {field}, got {type(value).__name__} {value!r}"
        )
    if not value:
        raise ValueError(f"{requirement} requires a {field}")
    reject_marker_separator(value, field=field)
    return value


def render_lane_envelope(envelope: LaneEvidenceEnvelope) -> str:
    """Render the envelope to the ``key=value:...`` marker-field form, fail-closed.

    Validates exactly what :func:`parse_lane_envelope` requires — non-empty workspace / lane, a
    POSITIVE generation, a full-SHA-or-absent head — plus separator rejection, and raises
    ``ValueError`` rather than emitting the marker. A renderer that accepts what its own parser
    refuses is not a strict grammar: it produces records that read back as a typed zero (so the
    evidence silently does not count) or, worse, splits a separator-carrying id into an extra field.
    The producer's programming error must surface at write time, not as unreadable durable evidence.

    Every identity is validated RAW, through :func:`require_marker_token` (Redmine #14694). The
    previous ``str(x or "").strip()`` normalized before it judged, so ``workspace=" ws "`` became
    the canonical ``ws`` — the caller's raw value silently replaced by a different one — and a
    non-``str`` identity was rendered through ``str()``. ``lane_generation`` was already raw-typed
    (a positive ``int``, and ``bool`` is not one); the string fields now hold the same line.
    """
    workspace = require_marker_token(
        envelope.workspace, field=FIELD_WORKSPACE, requirement="lane envelope"
    )
    lane = require_marker_token(envelope.lane, field=FIELD_LANE, requirement="lane envelope")
    generation = envelope.lane_generation
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise ValueError(f"lane envelope requires a positive lane_generation, got {generation!r}")
    head = envelope.head
    if not isinstance(head, str):
        raise ValueError(f"lane envelope head must be a string, got {type(head).__name__} {head!r}")
    if head:
        # Absent is ``""`` and nothing else: ``None`` is a producer error, not "no head".
        require_marker_token(head, field=FIELD_HEAD, requirement="lane envelope")
        # No whitespace survived the token check, so this reads the raw head (``is_full_sha``
        # strips for the PARSE side, where the field has already been split out of the body).
        if not is_full_sha(head):
            raise ValueError("lane envelope head must be a full lowercase commit SHA")

    parts = [
        f"{FIELD_WORKSPACE}={workspace}",
        f"{FIELD_LANE}={lane}",
        f"{FIELD_LANE_GENERATION}={generation}",
    ]
    if head:
        parts.append(f"{FIELD_HEAD}={head}")
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
