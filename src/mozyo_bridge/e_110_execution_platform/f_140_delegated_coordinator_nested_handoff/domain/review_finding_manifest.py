"""Canonical review finding prose + manifest authority (Redmine #14971).

The existing ``review_result`` workflow marker proves which review request/head a result answers;
it deliberately says nothing about the set of findings that result raised.  A verdict consumer
therefore had no authoritative universe to compare against and could accept one answered finding
from a two-finding review.  This module adds that missing set without changing the existing marker:

* a dedicated, additive ``review-finding-manifest`` sidecar lives in the *same Redmine journal* as
  the existing ``review_result`` marker;
* the canonical producer renders both the standardized ``finding_<id>`` prose blocks and the
  sidecar from one structured input, then posts the resulting note with one transport call;
* the strict reader correlates the sidecar with the owning journal's issue, review request, head,
  conclusion, prose identities and digest.  It returns a typed zero on every mismatch.

The sidecar channel is intentionally NOT added to ``canonical_note_scan.RECOGNIZED_CHANNELS``.
Older callback/glance/hibernate consumers therefore project the journal exactly as before, while
this module reuses the same quote-aware canonical lines and marker regex for its dedicated reader.
That is the compatibility ruling recorded at Redmine #14971 j#99084.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping, Sequence, Tuple

from .canonical_note_scan import MARKER_RE, canonical_note_lines
from .marker_value_contract import (
    is_exact_str,
    is_journal_id,
    require_journal_id,
    require_review_head,
)
from .redmine_journal_source import (
    RedmineJournalEntry,
    render_gate_note,
    strict_gate_markers,
    strict_marker_body_fields,
)
from .sublane_admission import REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED


MANIFEST_CHANNEL = "review-finding-manifest"
MANIFEST_VERSION = "1"
MANIFEST_EMPTY = "-"
MANIFEST_FIELD_ORDER: Tuple[str, ...] = (
    "version",
    "issue",
    "req",
    "head",
    "count",
    "findings",
    "set_digest",
)

MANIFEST_MISSING = "missing"
MANIFEST_VALID = "valid"
MANIFEST_INVALID = "invalid"

REASON_MANIFEST_MISSING = "review_finding_manifest_missing"
REASON_MANIFEST_MALFORMED = "review_finding_manifest_malformed"
REASON_MANIFEST_DUPLICATE = "review_finding_manifest_duplicate"
REASON_MANIFEST_CONTEXT_MISMATCH = "review_finding_manifest_context_mismatch"
REASON_MANIFEST_PROSE_MISMATCH = "review_finding_manifest_prose_mismatch"
REASON_MANIFEST_CONCLUSION_MISMATCH = "review_finding_manifest_conclusion_mismatch"
REASON_MANIFEST_DIGEST_MISMATCH = "review_finding_manifest_digest_mismatch"
REASON_REVIEW_MARKER_UNRESOLVED = "review_result_marker_unresolved"

REASON_FINDINGS_INPUT_MISSING = "review_findings_input_missing"
REASON_FINDINGS_INPUT_UNREADABLE = "review_findings_input_unreadable"
REASON_FINDINGS_INPUT_INVALID = "review_findings_input_invalid"
REASON_APPROVED_WITH_FINDINGS = "approved_review_carries_findings"
REASON_CHANGES_WITHOUT_FINDINGS = "changes_requested_review_has_no_findings"
REASON_REVIEW_BODY_RESERVED_CONTROL = "review_body_carries_reserved_finding_control"

_FINDING_ID_RE = re.compile(r"^[a-z0-9]+$")
_CANONICAL_FINDING_HEADING_RE = re.compile(
    r"^#{3} finding_(?P<id>[a-z0-9]+) — (?P<summary>\S(?:.*\S)?)$"
)
_ANY_FINDING_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*finding_", re.IGNORECASE
)
_RESERVED_LEGACY_FINDING_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*(?:"
    r"findings?\s*$|"
    r"finding(?:[\s-]+)[a-z0-9]+\b|"
    r"(?:r[0-9]+-)?f[0-9]+\b"
    r")",
    re.IGNORECASE,
)
_LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_ASCII_NONNEGATIVE_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_REVIEW_RESULT_MARKER_FIELDS = frozenset(
    {
        "conclusion",
        "callback",
        "commit_bearing",
        "integration_recorded",
        "issue_open",
        "blocker_recorded",
        "target_head",
        "review_request_journal",
        "evidence_workspace",
        "evidence_lane",
        "evidence_lane_generation",
    }
)


class ReviewFindingManifestError(ValueError):
    """A producer/reader input cannot satisfy the finding-manifest contract."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ReviewFinding:
    """One structured material review finding supplied to the canonical producer."""

    identity: str
    summary: str
    details: str = ""


@dataclass(frozen=True)
class ReviewFindingManifestFacts:
    """The authority a single review-result journal provides, or a typed zero."""

    state: str = MANIFEST_MISSING
    reason: str = REASON_MANIFEST_MISSING
    issue: str = ""
    review_journal: str = ""
    review_request_journal: str = ""
    target_head: str = ""
    conclusion: str = ""
    findings: Tuple[str, ...] = ()
    set_digest: str = ""

    @property
    def valid(self) -> bool:
        return self.state == MANIFEST_VALID


def _raise(reason: str, message: str) -> "None":
    raise ReviewFindingManifestError(reason, message)


def _require_journal(value: object, *, field: str) -> str:
    try:
        return require_journal_id(value, field=field)
    except ValueError:
        _raise(REASON_FINDINGS_INPUT_INVALID, f"{field} must be a canonical journal id")


def _require_head(value: object) -> str:
    try:
        return require_review_head(value, field="head")
    except ValueError:
        _raise(REASON_FINDINGS_INPUT_INVALID, "head must be a full commit hash")


def _safe_prose(value: object, *, field: str, allow_multiline: bool) -> str:
    if not is_exact_str(value):
        _raise(REASON_FINDINGS_INPUT_INVALID, f"{field} must be an exact string")
    text = value
    if text != text.strip() or (not text and field != "details"):
        _raise(REASON_FINDINGS_INPUT_INVALID, f"{field} must be non-blank and unpadded")
    if "\r" in text or (not allow_multiline and "\n" in text):
        _raise(REASON_FINDINGS_INPUT_INVALID, f"{field} must be one line")
    if "[mozyo:" in text:
        _raise(
            REASON_REVIEW_BODY_RESERVED_CONTROL,
            f"{field} may not inject a mozyo marker",
        )
    for line in text.splitlines():
        if (
            _ANY_FINDING_HEADING_RE.match(line)
            or _RESERVED_LEGACY_FINDING_HEADING_RE.match(line)
        ):
            _raise(
                REASON_REVIEW_BODY_RESERVED_CONTROL,
                f"{field} may not declare a finding heading",
            )
    return text


def _validated_findings(findings: Sequence[ReviewFinding]) -> Tuple[ReviewFinding, ...]:
    if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
        _raise(REASON_FINDINGS_INPUT_INVALID, "findings must be a sequence")
    out: list[ReviewFinding] = []
    seen: set[str] = set()
    for finding in findings:
        if type(finding) is not ReviewFinding:
            _raise(REASON_FINDINGS_INPUT_INVALID, "each finding must be ReviewFinding")
        identity = finding.identity
        if not is_exact_str(identity) or _FINDING_ID_RE.fullmatch(identity) is None:
            _raise(
                REASON_FINDINGS_INPUT_INVALID,
                "a finding identity must be canonical lowercase ASCII alphanumeric",
            )
        if identity in seen:
            _raise(REASON_FINDINGS_INPUT_INVALID, "finding identities must be unique")
        seen.add(identity)
        out.append(
            ReviewFinding(
                identity=identity,
                summary=_safe_prose(finding.summary, field="summary", allow_multiline=False),
                details=_safe_prose(finding.details, field="details", allow_multiline=True),
            )
        )
    return tuple(out)


def review_findings_from_payload(payload: object) -> Tuple[ReviewFinding, ...]:
    """Parse the exact v1 JSON value accepted by ``--review-findings-json``. (pure)"""

    if not isinstance(payload, Mapping) or set(payload) != {"version", "findings"}:
        _raise(
            REASON_FINDINGS_INPUT_INVALID,
            "review findings JSON must be {version, findings} and carry no unknown fields",
        )
    if type(payload.get("version")) is not int or payload.get("version") != 1:
        _raise(REASON_FINDINGS_INPUT_INVALID, "review findings JSON version must be integer 1")
    raw = payload.get("findings")
    if not isinstance(raw, list):
        _raise(REASON_FINDINGS_INPUT_INVALID, "review findings JSON findings must be a list")
    findings: list[ReviewFinding] = []
    for item in raw:
        if not isinstance(item, Mapping):
            _raise(REASON_FINDINGS_INPUT_INVALID, "each JSON finding must be an object")
        keys = set(item)
        if not {"id", "summary"} <= keys or not keys <= {"id", "summary", "details"}:
            _raise(
                REASON_FINDINGS_INPUT_INVALID,
                "each JSON finding requires id/summary and permits only optional details",
            )
        findings.append(
            ReviewFinding(
                identity=item.get("id"),  # type: ignore[arg-type]
                summary=item.get("summary"),  # type: ignore[arg-type]
                details=item.get("details", ""),  # type: ignore[arg-type]
            )
        )
    return _validated_findings(findings)


def _finding_ids(findings: Sequence[ReviewFinding]) -> Tuple[str, ...]:
    return tuple(finding.identity for finding in _validated_findings(findings))


def review_finding_set_digest(
    *, issue: object, review_request_journal: object, target_head: object, findings: Sequence[str]
) -> str:
    """Domain-separated digest of one new review generation's exact finding sequence. (pure)"""

    issue_s = _require_journal(issue, field="issue")
    req_s = _require_journal(review_request_journal, field="req")
    head_s = _require_head(target_head)
    if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
        _raise(REASON_FINDINGS_INPUT_INVALID, "digest findings must be a sequence")
    ids = tuple(findings)
    if any(not is_exact_str(value) or _FINDING_ID_RE.fullmatch(value) is None for value in ids):
        _raise(REASON_FINDINGS_INPUT_INVALID, "digest findings contain a non-canonical identity")
    if len(ids) != len(set(ids)):
        _raise(REASON_FINDINGS_INPUT_INVALID, "digest findings repeat an identity")
    encoded = "\n".join(
        (
            "review-finding-manifest-v1",
            f"issue\t{issue_s}",
            f"req\t{req_s}",
            f"head\t{head_s}",
            f"count\t{len(ids)}",
            f"findings\t{','.join(ids)}",
        )
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_review_finding_manifest(
    *, issue: object, review_request_journal: object, target_head: object, findings: Sequence[str]
) -> str:
    """Render the strict sidecar marker paired with one review_result journal. (pure)"""

    issue_s = _require_journal(issue, field="issue")
    req_s = _require_journal(review_request_journal, field="req")
    head_s = _require_head(target_head)
    if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
        _raise(REASON_FINDINGS_INPUT_INVALID, "manifest findings must be a sequence")
    ids = tuple(findings)
    digest = review_finding_set_digest(
        issue=issue_s, review_request_journal=req_s, target_head=head_s, findings=ids
    )
    fields = {
        "version": MANIFEST_VERSION,
        "issue": issue_s,
        "req": req_s,
        "head": head_s,
        "count": str(len(ids)),
        "findings": ",".join(ids) if ids else MANIFEST_EMPTY,
        "set_digest": digest,
    }
    body = ":".join(f"{key}={fields[key]}" for key in MANIFEST_FIELD_ORDER)
    return f"[mozyo:{MANIFEST_CHANNEL}:{body}]"


def render_review_findings_prose(findings: Sequence[ReviewFinding]) -> str:
    """Render the only finding-heading grammar the manifest reader recognizes. (pure)"""

    checked = _validated_findings(findings)
    if not checked:
        return "## Findings\n\n- none"
    blocks: list[str] = ["## Findings"]
    for finding in checked:
        block = f"### finding_{finding.identity} — {finding.summary}"
        if finding.details:
            block += f"\n\n{finding.details}"
        blocks.append(block)
    return "\n\n".join(blocks)


def render_review_result_note(
    *,
    issue: object,
    body: object,
    findings: Sequence[ReviewFinding],
    marker_fields: Mapping[str, object],
) -> str:
    """Atomically compose summary, finding prose, existing gate marker and sidecar. (pure)"""

    if not is_exact_str(body):
        _raise(REASON_FINDINGS_INPUT_INVALID, "body must be an exact string")
    if not isinstance(marker_fields, Mapping):
        _raise(REASON_FINDINGS_INPUT_INVALID, "marker_fields must be a mapping")
    if not set(marker_fields) <= _REVIEW_RESULT_MARKER_FIELDS:
        _raise(
            REASON_FINDINGS_INPUT_INVALID,
            "review_result marker_fields contain an unknown or reserved field",
        )
    summary = _safe_prose(body, field="body", allow_multiline=True) if body else ""
    checked = _validated_findings(findings)
    conclusion = marker_fields.get("conclusion")
    if conclusion == REVIEW_APPROVED and checked:
        _raise(REASON_APPROVED_WITH_FINDINGS, "an approved review cannot carry material findings")
    if conclusion == REVIEW_CHANGES_REQUESTED and not checked:
        _raise(
            REASON_CHANGES_WITHOUT_FINDINGS,
            "a changes_requested review must carry at least one structured finding",
        )
    if conclusion not in {REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED}:
        _raise(REASON_FINDINGS_INPUT_INVALID, "review_result conclusion is unresolved")
    issue_s = _require_journal(issue, field="issue")
    req = marker_fields.get("review_request_journal")
    head = marker_fields.get("target_head")
    ids = tuple(finding.identity for finding in checked)
    parts = [part for part in (summary, render_review_findings_prose(checked)) if part]
    parts.append(render_gate_note("review_result", **dict(marker_fields)))
    parts.append(
        render_review_finding_manifest(
            issue=issue_s,
            review_request_journal=req,
            target_head=head,
            findings=ids,
        )
    )
    return "\n\n".join(parts)


def _dedicated_channel_bodies(notes: str, channel: str) -> tuple[int, Tuple[str, ...]]:
    """Return declaration-prefix count and regex-complete bodies on one dedicated channel."""

    prefix = f"[mozyo:{channel}:"
    declared = 0
    bodies: list[str] = []
    for line in canonical_note_lines(notes or ""):
        declared += line.count(prefix)
        for match in MARKER_RE.finditer(line):
            if match.group("channel") == channel:
                bodies.append(match.group("body"))
    return declared, tuple(bodies)


def _decode_count(raw: object) -> int | None:
    if not is_exact_str(raw) or _ASCII_NONNEGATIVE_RE.fullmatch(raw) is None:
        return None
    if len(raw) > 19:
        return None
    value = int(raw)
    return value if value <= 2**63 - 1 else None


def _decode_findings(raw: object, *, count: int) -> Tuple[str, ...] | None:
    if not is_exact_str(raw):
        return None
    if count == 0:
        return () if raw == MANIFEST_EMPTY else None
    if raw == MANIFEST_EMPTY:
        return None
    ids = tuple(raw.split(","))
    if len(ids) != count or len(ids) != len(set(ids)):
        return None
    if any(_FINDING_ID_RE.fullmatch(identity) is None for identity in ids):
        return None
    return ids


def _prose_finding_ids(notes: str) -> Tuple[str, ...] | None:
    ids: list[str] = []
    for line in canonical_note_lines(notes or ""):
        if not _ANY_FINDING_HEADING_RE.match(line):
            continue
        match = _CANONICAL_FINDING_HEADING_RE.fullmatch(line)
        if match is None:
            return None
        identity = match.group("id")
        if identity in ids:
            return None
        ids.append(identity)
    return tuple(ids)


def _invalid(entry: RedmineJournalEntry, reason: str) -> ReviewFindingManifestFacts:
    return ReviewFindingManifestFacts(
        state=MANIFEST_INVALID,
        reason=reason,
        issue=str(getattr(entry, "issue_id", "") or ""),
        review_journal=str(getattr(entry, "journal_id", "") or ""),
    )


def read_review_finding_manifest(entry: RedmineJournalEntry) -> ReviewFindingManifestFacts:
    """Read and cross-check one review-result journal's dedicated sidecar. (pure)"""

    notes = str(getattr(entry, "notes", "") or "")
    declared, bodies = _dedicated_channel_bodies(notes, MANIFEST_CHANNEL)
    if declared == 0:
        return ReviewFindingManifestFacts(
            issue=str(getattr(entry, "issue_id", "") or ""),
            review_journal=str(getattr(entry, "journal_id", "") or ""),
        )
    if declared != len(bodies):
        return _invalid(entry, REASON_MANIFEST_MALFORMED)
    if len(bodies) != 1:
        return _invalid(entry, REASON_MANIFEST_DUPLICATE)
    fields = strict_marker_body_fields(bodies[0], expected=MANIFEST_FIELD_ORDER)
    if fields is None or tuple(fields) != MANIFEST_FIELD_ORDER:
        return _invalid(entry, REASON_MANIFEST_MALFORMED)
    if fields.get("version") != MANIFEST_VERSION:
        return _invalid(entry, REASON_MANIFEST_MALFORMED)

    issue = fields.get("issue", "")
    req = fields.get("req", "")
    head = fields.get("head", "")
    if not is_journal_id(issue) or not is_journal_id(req):
        return _invalid(entry, REASON_MANIFEST_MALFORMED)
    try:
        require_review_head(head)
    except ValueError:
        return _invalid(entry, REASON_MANIFEST_MALFORMED)
    count = _decode_count(fields.get("count"))
    if count is None:
        return _invalid(entry, REASON_MANIFEST_MALFORMED)
    findings = _decode_findings(fields.get("findings"), count=count)
    if findings is None:
        return _invalid(entry, REASON_MANIFEST_MALFORMED)
    digest = fields.get("set_digest", "")
    if _LOWER_HEX_64_RE.fullmatch(digest) is None:
        return _invalid(entry, REASON_MANIFEST_MALFORMED)
    expected_digest = review_finding_set_digest(
        issue=issue, review_request_journal=req, target_head=head, findings=findings
    )
    if digest != expected_digest:
        return _invalid(entry, REASON_MANIFEST_DIGEST_MISMATCH)

    review_markers = strict_gate_markers(notes, "review_result")
    if len(review_markers) != 1:
        return _invalid(entry, REASON_REVIEW_MARKER_UNRESOLVED)
    review = review_markers[0]
    conclusion = str(review.get("conclusion", "") or "")
    if (
        issue != str(getattr(entry, "issue_id", "") or "")
        or req != str(review.get("req", "") or "")
        or head != str(review.get("head", "") or "")
    ):
        return _invalid(entry, REASON_MANIFEST_CONTEXT_MISMATCH)
    if (conclusion == REVIEW_APPROVED and findings) or (
        conclusion == REVIEW_CHANGES_REQUESTED and not findings
    ) or conclusion not in {REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED}:
        return _invalid(entry, REASON_MANIFEST_CONCLUSION_MISMATCH)
    prose = _prose_finding_ids(notes)
    if prose is None or prose != findings:
        return _invalid(entry, REASON_MANIFEST_PROSE_MISMATCH)
    return ReviewFindingManifestFacts(
        state=MANIFEST_VALID,
        reason="review_finding_manifest_valid",
        issue=issue,
        review_journal=str(getattr(entry, "journal_id", "") or ""),
        review_request_journal=req,
        target_head=head,
        conclusion=conclusion,
        findings=findings,
        set_digest=digest,
    )


__all__ = (
    "MANIFEST_CHANNEL",
    "MANIFEST_FIELD_ORDER",
    "MANIFEST_INVALID",
    "MANIFEST_MISSING",
    "MANIFEST_VALID",
    "REASON_APPROVED_WITH_FINDINGS",
    "REASON_CHANGES_WITHOUT_FINDINGS",
    "REASON_FINDINGS_INPUT_INVALID",
    "REASON_FINDINGS_INPUT_MISSING",
    "REASON_FINDINGS_INPUT_UNREADABLE",
    "REASON_MANIFEST_CONCLUSION_MISMATCH",
    "REASON_MANIFEST_CONTEXT_MISMATCH",
    "REASON_MANIFEST_DIGEST_MISMATCH",
    "REASON_MANIFEST_DUPLICATE",
    "REASON_MANIFEST_MALFORMED",
    "REASON_MANIFEST_MISSING",
    "REASON_MANIFEST_PROSE_MISMATCH",
    "REASON_REVIEW_BODY_RESERVED_CONTROL",
    "REASON_REVIEW_MARKER_UNRESOLVED",
    "ReviewFinding",
    "ReviewFindingManifestError",
    "ReviewFindingManifestFacts",
    "read_review_finding_manifest",
    "render_review_finding_manifest",
    "render_review_findings_prose",
    "render_review_result_note",
    "review_finding_set_digest",
    "review_findings_from_payload",
)
