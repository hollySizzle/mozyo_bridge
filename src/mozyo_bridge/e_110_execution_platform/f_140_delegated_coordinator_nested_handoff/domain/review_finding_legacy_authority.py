"""Append-only finding authority for reviews predating the manifest contract (#14971).

A historical review journal cannot be rewritten to add a
``review-finding-manifest`` sidecar.  Guessing identities from prose is equally unsafe: the real
migration fixture (#14577 j#93648) and its verdict use different spelling.  The approved migration
contract therefore uses two later records:

1. an untrusted ``review-finding-attestation`` names the historical issue/review/finding set;
2. a distinct ``review-finding-ruling`` recorded by the anchored coordinator, with
   ``approval_source=direct_owner``, selects that exact attestation and set.

Every attestation must be covered by an explicit ruling chain.  A replacement ruling must name the
immediately preceding ruling and a distinct attestation.  Unknown, missing, duplicate, unruled,
conflicting, stale, malformed or unauthorized records all resolve to a typed zero.  The review
journal remains byte-unchanged throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping, Sequence, Tuple

from .canonical_note_scan import MARKER_RE, canonical_note_lines
from .hibernate_evidence_authority import (
    ISSUER_COORDINATOR,
    ResolvedIssuer,
)
from .marker_value_contract import is_exact_str, is_journal_id, require_journal_id
from .redmine_journal_source import (
    RedmineJournalEntry,
    strict_gate_markers,
    strict_marker_body_fields,
)
from .review_finding_manifest import (
    MANIFEST_INVALID,
    MANIFEST_MISSING,
    MANIFEST_VALID,
    ReviewFindingManifestFacts,
    read_review_finding_manifest,
)
from .sublane_admission import REVIEW_CHANGES_REQUESTED


ATTESTATION_CHANNEL = "review-finding-attestation"
RULING_CHANNEL = "review-finding-ruling"
LEGACY_VERSION = "1"
APPROVAL_SOURCE = "direct_owner"
APPROVAL_DECISION = "approved"
NO_SUPERSEDED_RULING = "none"
EMPTY_FINDINGS = "-"

# This authority belongs to the review-finding migration bounded context, not to the hibernate
# evidence gate map.  Keeping it local prevents an unrelated recovery runbook from becoming the
# accidental catalog for every future durable authority.
GATE_REVIEW_FINDING_LEGACY_RULING = "review_finding_legacy_ruling"
REVIEW_FINDING_LEGACY_RULING = "redmine:#14971:j#99084"


def legacy_ruling_writer_role() -> str:
    """The one canonical writer role fixed by the #14971 direct-owner ruling."""

    return ISSUER_COORDINATOR


def legacy_ruling_pointer() -> str:
    """The durable record that fixed this migration authority contract."""

    return REVIEW_FINDING_LEGACY_RULING

ATTESTATION_FIELD_ORDER: Tuple[str, ...] = (
    "version",
    "issue",
    "review",
    "count",
    "findings",
    "set_digest",
)
RULING_FIELD_ORDER: Tuple[str, ...] = (
    "version",
    "approval_source",
    "decision",
    "issue",
    "review",
    "attestation",
    "supersedes",
    "count",
    "findings",
    "set_digest",
)

LEGACY_VALID = "valid"
LEGACY_INVALID = "invalid"

REASON_LEGACY_VALID = "legacy_review_finding_authority_valid"
REASON_REVIEW_JOURNAL_UNRESOLVED = "legacy_review_journal_unresolved"
REASON_ATTESTATION_MISSING = "legacy_attestation_missing"
REASON_ATTESTATION_UNKNOWN = "legacy_attestation_unknown"
REASON_ATTESTATION_MALFORMED = "legacy_attestation_malformed"
REASON_ATTESTATION_DUPLICATE = "legacy_attestation_duplicate"
REASON_ATTESTATION_CONFLICTING = "legacy_attestation_conflicting"
REASON_ATTESTATION_STALE = "legacy_attestation_stale"
REASON_ATTESTATION_UNAUTHORIZED = "legacy_attestation_unauthorized"
REASON_RULING_UNKNOWN = "legacy_ruling_unknown"
REASON_RULING_MALFORMED = "legacy_ruling_malformed"
REASON_RULING_UNAUTHORIZED = "legacy_ruling_unauthorized"
REASON_RULING_SUPERSESSION_INVALID = "legacy_ruling_supersession_invalid"
REASON_MANIFEST_LEGACY_CONFLICT = "review_finding_manifest_legacy_conflict"

AUTHORITY_SOURCE_MANIFEST = "manifest"
AUTHORITY_SOURCE_LEGACY = "legacy_owner_ruling"

_FINDING_ID_RE = re.compile(r"^[a-z0-9]+$")
_LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_ASCII_NONNEGATIVE_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")


class LegacyReviewFindingError(ValueError):
    """A legacy authority producer input cannot round-trip."""


@dataclass(frozen=True)
class _Attestation:
    journal: str
    issue: str
    review: str
    findings: Tuple[str, ...]
    set_digest: str


@dataclass(frozen=True)
class _Ruling:
    journal: str
    issue: str
    review: str
    attestation: str
    supersedes: str
    findings: Tuple[str, ...]
    set_digest: str


@dataclass(frozen=True)
class LegacyReviewFindingFacts:
    """The selected historical finding set, or a typed zero."""

    valid: bool = False
    state: str = LEGACY_INVALID
    reason: str = REASON_ATTESTATION_MISSING
    issue: str = ""
    review_journal: str = ""
    findings: Tuple[str, ...] = ()
    attestation_journal: str = ""
    ruling_journal: str = ""
    set_digest: str = ""


@dataclass(frozen=True)
class ReviewFindingAuthorityFacts:
    """Stable reader contract consumed by downstream verdict/terminal policies."""

    valid: bool = False
    reason: str = REASON_ATTESTATION_MISSING
    source: str = ""
    issue: str = ""
    review_journal: str = ""
    findings: Tuple[str, ...] = ()
    authority_journal: str = ""
    set_digest: str = ""


def _finding_ids(values: Sequence[str], *, allow_empty: bool = False) -> Tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise LegacyReviewFindingError("findings must be a sequence")
    ids = tuple(values)
    if not allow_empty and not ids:
        raise LegacyReviewFindingError("a legacy attestation must name at least one finding")
    if any(not is_exact_str(value) or _FINDING_ID_RE.fullmatch(value) is None for value in ids):
        raise LegacyReviewFindingError(
            "finding identities must be canonical lowercase ASCII alphanumeric"
        )
    if len(ids) != len(set(ids)):
        raise LegacyReviewFindingError("finding identities must not repeat")
    return ids


def legacy_review_finding_digest(
    *, issue: object, review_journal: object, findings: Sequence[str]
) -> str:
    """Domain-separated digest of one historical review's selected finding sequence. (pure)"""

    issue_s = require_journal_id(issue, field="issue")
    review_s = require_journal_id(review_journal, field="review")
    ids = _finding_ids(findings)
    encoded = "\n".join(
        (
            "legacy-review-finding-set-v1",
            f"issue\t{issue_s}",
            f"review\t{review_s}",
            f"count\t{len(ids)}",
            f"findings\t{','.join(ids)}",
        )
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_legacy_review_finding_attestation(
    *, issue: object, review_journal: object, findings: Sequence[str]
) -> str:
    """Render the untrusted append-only attestation selected by a later owner ruling."""

    issue_s = require_journal_id(issue, field="issue")
    review_s = require_journal_id(review_journal, field="review")
    ids = _finding_ids(findings)
    fields = {
        "version": LEGACY_VERSION,
        "issue": issue_s,
        "review": review_s,
        "count": str(len(ids)),
        "findings": ",".join(ids),
        "set_digest": legacy_review_finding_digest(
            issue=issue_s, review_journal=review_s, findings=ids
        ),
    }
    body = ":".join(f"{key}={fields[key]}" for key in ATTESTATION_FIELD_ORDER)
    return f"[mozyo:{ATTESTATION_CHANNEL}:{body}]"


def render_legacy_review_finding_ruling(
    *,
    issue: object,
    review_journal: object,
    attestation_journal: object,
    findings: Sequence[str],
    supersedes_ruling_journal: object = NO_SUPERSEDED_RULING,
) -> str:
    """Render a direct-owner ruling selecting one exact attestation and set. (pure)"""

    issue_s = require_journal_id(issue, field="issue")
    review_s = require_journal_id(review_journal, field="review")
    attestation_s = require_journal_id(attestation_journal, field="attestation")
    if supersedes_ruling_journal == NO_SUPERSEDED_RULING:
        supersedes_s = NO_SUPERSEDED_RULING
    else:
        supersedes_s = require_journal_id(
            supersedes_ruling_journal, field="supersedes"
        )
    ids = _finding_ids(findings)
    fields = {
        "version": LEGACY_VERSION,
        "approval_source": APPROVAL_SOURCE,
        "decision": APPROVAL_DECISION,
        "issue": issue_s,
        "review": review_s,
        "attestation": attestation_s,
        "supersedes": supersedes_s,
        "count": str(len(ids)),
        "findings": ",".join(ids),
        "set_digest": legacy_review_finding_digest(
            issue=issue_s, review_journal=review_s, findings=ids
        ),
    }
    body = ":".join(f"{key}={fields[key]}" for key in RULING_FIELD_ORDER)
    return f"[mozyo:{RULING_CHANNEL}:{body}]"


def _channel_bodies(notes: str, channel: str) -> tuple[int, Tuple[str, ...]]:
    prefix = f"[mozyo:{channel}:"
    declared = 0
    bodies: list[str] = []
    for line in canonical_note_lines(notes or ""):
        declared += line.count(prefix)
        for match in MARKER_RE.finditer(line):
            if match.group("channel") == channel:
                bodies.append(match.group("body"))
    return declared, tuple(bodies)


def _count(raw: object) -> int | None:
    if not is_exact_str(raw) or _ASCII_NONNEGATIVE_RE.fullmatch(raw) is None:
        return None
    if len(raw) > 19:
        return None
    number = int(raw)
    return number if number <= 2**63 - 1 else None


def _ids(raw: object, *, count: int) -> Tuple[str, ...] | None:
    if not is_exact_str(raw) or count < 1 or raw == EMPTY_FINDINGS:
        return None
    ids = tuple(raw.split(","))
    if len(ids) != count or len(ids) != len(set(ids)):
        return None
    if any(_FINDING_ID_RE.fullmatch(identity) is None for identity in ids):
        return None
    return ids


def _invalid(issue: str, review: str, reason: str) -> LegacyReviewFindingFacts:
    return LegacyReviewFindingFacts(reason=reason, issue=issue, review_journal=review)


def _parse_attestations(
    entries: Sequence[RedmineJournalEntry], *, issue: str, review: str
) -> tuple[list[_Attestation], str]:
    found: list[_Attestation] = []
    for entry in entries:
        declared, bodies = _channel_bodies(entry.notes, ATTESTATION_CHANNEL)
        if not declared:
            continue
        if declared != len(bodies) or len(bodies) != 1:
            return [], REASON_ATTESTATION_DUPLICATE if len(bodies) > 1 else REASON_ATTESTATION_MALFORMED
        fields = strict_marker_body_fields(bodies[0], expected=ATTESTATION_FIELD_ORDER)
        if fields is None or tuple(fields) != ATTESTATION_FIELD_ORDER:
            return [], REASON_ATTESTATION_MALFORMED
        # A well-shaped marker for another round is not this round's authority and is ignored.
        if fields.get("issue") != issue or fields.get("review") != review:
            continue
        if fields.get("version") != LEGACY_VERSION:
            return [], REASON_ATTESTATION_UNKNOWN
        count = _count(fields.get("count"))
        ids = _ids(fields.get("findings"), count=count if count is not None else -1)
        digest = fields.get("set_digest", "")
        journal = str(entry.journal_id or "")
        if (
            count is None
            or ids is None
            or _LOWER_HEX_64_RE.fullmatch(digest) is None
            or not is_journal_id(journal)
        ):
            return [], REASON_ATTESTATION_MALFORMED
        expected = legacy_review_finding_digest(
            issue=issue, review_journal=review, findings=ids
        )
        if digest != expected:
            return [], REASON_ATTESTATION_MALFORMED
        found.append(
            _Attestation(
                journal=journal,
                issue=issue,
                review=review,
                findings=ids,
                set_digest=digest,
            )
        )
    return found, ""


def _parse_rulings(
    entries: Sequence[RedmineJournalEntry],
    *,
    issue: str,
    review: str,
    ruling_issuers: Mapping[str, ResolvedIssuer],
) -> tuple[list[_Ruling], str]:
    found: list[_Ruling] = []
    for entry in entries:
        declared, bodies = _channel_bodies(entry.notes, RULING_CHANNEL)
        if not declared:
            continue
        if declared != len(bodies) or len(bodies) != 1:
            return [], REASON_RULING_MALFORMED
        fields = strict_marker_body_fields(bodies[0], expected=RULING_FIELD_ORDER)
        if fields is None or tuple(fields) != RULING_FIELD_ORDER:
            return [], REASON_RULING_MALFORMED
        if fields.get("issue") != issue or fields.get("review") != review:
            continue
        if fields.get("version") != LEGACY_VERSION:
            return [], REASON_RULING_UNKNOWN
        if (
            fields.get("approval_source") != APPROVAL_SOURCE
            or fields.get("decision") != APPROVAL_DECISION
        ):
            return [], REASON_RULING_UNAUTHORIZED
        journal = str(entry.journal_id or "")
        attestation = fields.get("attestation", "")
        supersedes = fields.get("supersedes", "")
        count = _count(fields.get("count"))
        ids = _ids(fields.get("findings"), count=count if count is not None else -1)
        digest = fields.get("set_digest", "")
        if (
            not is_journal_id(journal)
            or not is_journal_id(attestation)
            or (supersedes != NO_SUPERSEDED_RULING and not is_journal_id(supersedes))
            or count is None
            or ids is None
            or _LOWER_HEX_64_RE.fullmatch(digest) is None
        ):
            return [], REASON_RULING_MALFORMED
        if digest != legacy_review_finding_digest(
            issue=issue, review_journal=review, findings=ids
        ):
            return [], REASON_RULING_MALFORMED
        issuer = ruling_issuers.get(journal, ResolvedIssuer())
        if (
            issuer.role != legacy_ruling_writer_role()
            or issuer.authority_anchor != legacy_ruling_pointer()
        ):
            return [], REASON_RULING_UNAUTHORIZED
        found.append(
            _Ruling(
                journal=journal,
                issue=issue,
                review=review,
                attestation=attestation,
                supersedes=supersedes,
                findings=ids,
                set_digest=digest,
            )
        )
    return found, ""


def resolve_legacy_review_findings(
    entries: Sequence[RedmineJournalEntry],
    *,
    review_journal: object,
    ruling_issuers: Mapping[str, ResolvedIssuer] | None = None,
) -> LegacyReviewFindingFacts:
    """Resolve a complete, explicitly ruled append-only attestation chain. (pure)"""

    review = str(review_journal or "")
    if not is_journal_id(review):
        return _invalid("", review, REASON_REVIEW_JOURNAL_UNRESOLVED)
    exact = [entry for entry in entries if str(entry.journal_id or "") == review]
    if len(exact) != 1:
        return _invalid("", review, REASON_REVIEW_JOURNAL_UNRESOLVED)
    issue = str(exact[0].issue_id or "")
    if not is_journal_id(issue) or any(str(entry.issue_id or "") != issue for entry in entries):
        return _invalid(issue, review, REASON_REVIEW_JOURNAL_UNRESOLVED)
    review_markers = strict_gate_markers(exact[0].notes, "review_result")
    if (
        len(review_markers) != 1
        or review_markers[0].get("conclusion") != REVIEW_CHANGES_REQUESTED
    ):
        return _invalid(issue, review, REASON_REVIEW_JOURNAL_UNRESOLVED)
    review_number = int(review)

    attestations, refusal = _parse_attestations(entries, issue=issue, review=review)
    if refusal:
        return _invalid(issue, review, refusal)
    if not attestations:
        return _invalid(issue, review, REASON_ATTESTATION_MISSING)
    if any(int(attestation.journal) <= review_number for attestation in attestations):
        return _invalid(issue, review, REASON_ATTESTATION_STALE)
    if len({attestation.journal for attestation in attestations}) != len(attestations):
        return _invalid(issue, review, REASON_ATTESTATION_DUPLICATE)
    # Two byte-identical attestations add no new decision and are a duplicate even if somebody
    # later tries to build a supersession chain around them.
    signatures = [(a.findings, a.set_digest) for a in attestations]
    if len(signatures) != len(set(signatures)):
        return _invalid(issue, review, REASON_ATTESTATION_DUPLICATE)

    rulings, refusal = _parse_rulings(
        entries,
        issue=issue,
        review=review,
        ruling_issuers=ruling_issuers or {},
    )
    if refusal:
        return _invalid(issue, review, refusal)
    if not rulings:
        return _invalid(issue, review, REASON_ATTESTATION_UNAUTHORIZED)
    if len({ruling.journal for ruling in rulings}) != len(rulings):
        return _invalid(issue, review, REASON_RULING_MALFORMED)

    by_attestation = {attestation.journal: attestation for attestation in attestations}
    ordered = sorted(rulings, key=lambda ruling: int(ruling.journal))
    selected_attestations: list[str] = []
    previous = NO_SUPERSEDED_RULING
    for index, ruling in enumerate(ordered):
        attestation = by_attestation.get(ruling.attestation)
        if attestation is None:
            return _invalid(issue, review, REASON_RULING_MALFORMED)
        if int(ruling.journal) <= int(attestation.journal):
            return _invalid(issue, review, REASON_ATTESTATION_STALE)
        if ruling.findings != attestation.findings or ruling.set_digest != attestation.set_digest:
            return _invalid(issue, review, REASON_ATTESTATION_CONFLICTING)
        expected_supersedes = NO_SUPERSEDED_RULING if index == 0 else previous
        if ruling.supersedes != expected_supersedes:
            return _invalid(issue, review, REASON_RULING_SUPERSESSION_INVALID)
        if ruling.attestation in selected_attestations:
            return _invalid(issue, review, REASON_ATTESTATION_DUPLICATE)
        selected_attestations.append(ruling.attestation)
        previous = ruling.journal

    all_attestations = {attestation.journal for attestation in attestations}
    ruled = set(selected_attestations)
    if ruled != all_attestations:
        unruled = [a for a in attestations if a.journal not in ruled]
        latest_ruling = int(ordered[-1].journal)
        if any(int(attestation.journal) > latest_ruling for attestation in unruled):
            return _invalid(issue, review, REASON_ATTESTATION_STALE)
        return _invalid(issue, review, REASON_ATTESTATION_CONFLICTING)

    selected_ruling = ordered[-1]
    selected = by_attestation[selected_ruling.attestation]
    return LegacyReviewFindingFacts(
        valid=True,
        state=LEGACY_VALID,
        reason=REASON_LEGACY_VALID,
        issue=issue,
        review_journal=review,
        findings=selected.findings,
        attestation_journal=selected.journal,
        ruling_journal=selected_ruling.journal,
        set_digest=selected.set_digest,
    )


def _has_target_legacy_declaration(
    entries: Sequence[RedmineJournalEntry], *, issue: str, review: str
) -> bool:
    """Whether a dedicated legacy marker claims THIS review, including an unreadable claim."""

    for entry in entries:
        for channel in (ATTESTATION_CHANNEL, RULING_CHANNEL):
            declared, bodies = _channel_bodies(entry.notes, channel)
            if str(entry.issue_id or "") == issue and declared != len(bodies):
                # The owning issue contains an unreadable legacy declaration. Its target cannot
                # be disproved, so accepting the manifest would let a malformed downgrade
                # attempt disappear merely because it failed to parse.
                return True
            for body in bodies:
                # Existence is intentionally weaker than validity: a marker claiming this review
                # but carrying an extra/unknown field must still conflict with the in-journal
                # manifest rather than being ignored as though no downgrade was attempted.
                raw: dict[str, str] = {}
                for component in body.split(":"):
                    key, equals, value = component.partition("=")
                    if equals and key not in raw:
                        raw[key] = value
                if raw.get("issue") == issue and raw.get("review") == review:
                    return True
    return False


def resolve_review_finding_authority(
    entries: Sequence[RedmineJournalEntry],
    *,
    review_journal: object,
    ruling_issuers: Mapping[str, ResolvedIssuer] | None = None,
) -> ReviewFindingAuthorityFacts:
    """Prefer an in-journal manifest; otherwise require the complete legacy ruling chain."""

    review = str(review_journal or "")
    exact = [entry for entry in entries if str(entry.journal_id or "") == review]
    if len(exact) != 1:
        return ReviewFindingAuthorityFacts(
            reason=REASON_REVIEW_JOURNAL_UNRESOLVED, review_journal=review
        )
    manifest: ReviewFindingManifestFacts = read_review_finding_manifest(exact[0])
    if manifest.state == MANIFEST_INVALID:
        return ReviewFindingAuthorityFacts(
            reason=manifest.reason,
            issue=manifest.issue,
            review_journal=review,
        )
    if manifest.state == MANIFEST_VALID:
        if _has_target_legacy_declaration(
            entries, issue=manifest.issue, review=review
        ):
            return ReviewFindingAuthorityFacts(
                reason=REASON_MANIFEST_LEGACY_CONFLICT,
                issue=manifest.issue,
                review_journal=review,
            )
        return ReviewFindingAuthorityFacts(
            valid=True,
            reason=manifest.reason,
            source=AUTHORITY_SOURCE_MANIFEST,
            issue=manifest.issue,
            review_journal=review,
            findings=manifest.findings,
            authority_journal=review,
            set_digest=manifest.set_digest,
        )
    assert manifest.state == MANIFEST_MISSING
    legacy = resolve_legacy_review_findings(
        entries,
        review_journal=review,
        ruling_issuers=ruling_issuers,
    )
    return ReviewFindingAuthorityFacts(
        valid=legacy.valid,
        reason=legacy.reason,
        source=AUTHORITY_SOURCE_LEGACY if legacy.valid else "",
        issue=legacy.issue,
        review_journal=review,
        findings=legacy.findings,
        authority_journal=legacy.ruling_journal,
        set_digest=legacy.set_digest,
    )


__all__ = (
    "APPROVAL_DECISION",
    "APPROVAL_SOURCE",
    "ATTESTATION_CHANNEL",
    "ATTESTATION_FIELD_ORDER",
    "AUTHORITY_SOURCE_LEGACY",
    "AUTHORITY_SOURCE_MANIFEST",
    "GATE_REVIEW_FINDING_LEGACY_RULING",
    "LEGACY_INVALID",
    "LEGACY_VALID",
    "NO_SUPERSEDED_RULING",
    "REVIEW_FINDING_LEGACY_RULING",
    "REASON_ATTESTATION_CONFLICTING",
    "REASON_ATTESTATION_DUPLICATE",
    "REASON_ATTESTATION_MALFORMED",
    "REASON_ATTESTATION_MISSING",
    "REASON_ATTESTATION_STALE",
    "REASON_ATTESTATION_UNAUTHORIZED",
    "REASON_ATTESTATION_UNKNOWN",
    "REASON_MANIFEST_LEGACY_CONFLICT",
    "REASON_REVIEW_JOURNAL_UNRESOLVED",
    "REASON_RULING_MALFORMED",
    "REASON_RULING_SUPERSESSION_INVALID",
    "REASON_RULING_UNAUTHORIZED",
    "REASON_RULING_UNKNOWN",
    "REASON_LEGACY_VALID",
    "RULING_CHANNEL",
    "RULING_FIELD_ORDER",
    "LegacyReviewFindingError",
    "LegacyReviewFindingFacts",
    "ReviewFindingAuthorityFacts",
    "legacy_review_finding_digest",
    "legacy_ruling_pointer",
    "legacy_ruling_writer_role",
    "render_legacy_review_finding_attestation",
    "render_legacy_review_finding_ruling",
    "resolve_legacy_review_findings",
    "resolve_review_finding_authority",
)
