"""Canonical gate-record writer: post a discoverable gate journal to Redmine (#13520 review F1a).

The production **producer wiring** the re-audit required (#13518 j#75381 F1a): before this, the
callback watcher's :func:`...callback_runtime.discover_candidates` read
``[mozyo:workflow-event:...]`` markers that *nothing in production wrote* — only test fixtures
hand-authored them, so scanning a real issue yielded zero candidates. This module closes that
loop: it renders a callback-required gate journal through the single canonical renderer
(:func:`...domain.redmine_journal_source.render_gate_note`, which always embeds the structured
marker) and posts it to Redmine through the same **credential-gated, opt-in** note transport all
durable handoff writes use (``MOZYO_REDMINE_DELIVERY_WRITE``). A real gate recorded this way is
then discoverable end-to-end: producer -> Redmine journal -> live poll -> exact-journal classify
-> outbox -> one-send callback.

Boundaries:

- **opt-in / fail-closed.** With no live transport (the opt-in unset), nothing is written and the
  receipt reason is ``write_optin_unset`` — never a silent success. The transport itself fails
  closed on a missing base URL / credential (``base_url_unset`` / ``credential_missing``) without
  ever carrying a credential.
- **injectable transport.** The write seam is injected; production resolves
  :func:`...redmine_note_transport.redmine_delivery_transport_from_env`, tests inject a fake so the
  full producer -> post -> discover path is verified with no live Redmine.
- **producer is the only marker source.** The note text always comes from ``render_gate_note``; a
  caller never hand-writes the marker, so the discovered gate cannot drift from what was recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Optional, Protocol, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    render_gate_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_manifest import (
    REASON_APPROVED_WITH_FINDINGS,
    REASON_CHANGES_WITHOUT_FINDINGS,
    REASON_FINDINGS_INPUT_INVALID,
    ReviewFinding,
    ReviewFindingManifestError,
    REASON_FINDINGS_INPUT_MISSING,
    REASON_FINDINGS_INPUT_UNREADABLE,
    review_findings_from_payload,
    render_review_result_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
)

#: Receipt reason for a successful post.
GATE_RECORD_OK = "ok"
#: Receipt reason when no live transport was wired (the ``MOZYO_REDMINE_DELIVERY_WRITE`` opt-in is
#: unset) — nothing was written, fail-closed (never a silent success).
GATE_RECORD_WRITE_OPTIN_UNSET = "write_optin_unset"


class NoteWriteTransport(Protocol):
    """The narrow write seam: append ``notes`` as a journal note on ``issue_id``; return a location."""

    def post_issue_note(self, issue_id: str, notes: str) -> str: ...


@dataclass(frozen=True)
class GateRecordReceipt:
    """The outcome of recording a canonical gate journal.

    ``recorded`` is True only when the note was posted. ``reason`` is :data:`GATE_RECORD_OK` on a
    post, :data:`GATE_RECORD_WRITE_OPTIN_UNSET` when no transport was wired, or a transport failure
    reason (``base_url_unset`` / ``credential_missing`` / ``unauthorized`` / ``transport_error`` …).
    ``location`` is a redacted ``redmine:issue=<id>`` pointer on success (never a credential).
    """

    recorded: bool
    reason: str
    location: str = ""

    def as_payload(self) -> dict[str, object]:
        return {"recorded": self.recorded, "reason": self.reason, "location": self.location}


@dataclass(frozen=True)
class GateRecordAttempt:
    """A gate-record attempt whose producer refusal is data, never a CLI traceback."""

    receipt: Optional[GateRecordReceipt] = None
    refusal: str = ""

    def refusal_payload(self, issue: str, gate: str) -> dict[str, object]:
        return {
            "action": "emit-gate",
            "issue": issue,
            "gate": gate,
            "recorded": False,
            "reason": self.refusal,
        }

    def refusal_lines(self, issue: str, gate: str) -> tuple[str, ...]:
        return (
            "action: emit-gate",
            f"issue: #{issue}",
            f"gate: {gate}",
            "recorded: False",
            f"reason: {self.refusal}",
        )


def _closed_json_object(pairs):
    """Build one JSON object while refusing duplicate member names (#14971)."""

    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON member: {key}")
        out[key] = value
    return out


def review_findings_json_input(
    raw_path: object,
    gate: str,
    marker_fields: Optional[dict],
) -> tuple[Optional[Sequence[ReviewFinding]], Optional[str]]:
    """Read the closed #14971 JSON input, returning findings or a typed refusal.

    ``None`` findings are reserved for a non-review gate. A ``review_result`` returns an explicit
    empty tuple for approval or one-or-more findings for ``changes_requested``; omission can never
    masquerade as the canonical empty set.
    """

    if gate != "review_result":
        return (None, "review_findings_wrong_gate") if raw_path else (None, None)
    conclusion = str((marker_fields or {}).get("conclusion", "") or "")
    if not raw_path:
        if conclusion == REVIEW_APPROVED:
            return (), None
        return None, REASON_FINDINGS_INPUT_MISSING
    if type(raw_path) is not str or raw_path != raw_path.strip() or not raw_path:
        return None, REASON_FINDINGS_INPUT_UNREADABLE
    try:
        payload = json.loads(
            Path(raw_path).read_text(encoding="utf-8"),
            object_pairs_hook=_closed_json_object,
        )
    except (OSError, UnicodeError, ValueError):
        return None, REASON_FINDINGS_INPUT_UNREADABLE
    try:
        findings = review_findings_from_payload(payload)
    except ReviewFindingManifestError as exc:
        return None, exc.reason or REASON_FINDINGS_INPUT_INVALID
    if conclusion == REVIEW_APPROVED and findings:
        return None, REASON_APPROVED_WITH_FINDINGS
    if conclusion == REVIEW_CHANGES_REQUESTED and not findings:
        return None, REASON_CHANGES_WITHOUT_FINDINGS
    return findings, None


def emit_gate_record(
    issue: str,
    gate: str,
    *,
    body: str = "",
    transport: Optional[NoteWriteTransport],
    marker_fields: Optional[dict] = None,
    review_findings: Optional[Sequence[ReviewFinding]] = None,
) -> GateRecordReceipt:
    """Render a canonical gate note and post it via ``transport`` (fail-closed, opt-in).

    Renders the note through :func:`render_gate_note` (always marker-bearing) and posts it. A
    ``None`` transport (opt-in unset) writes nothing and returns a ``write_optin_unset`` receipt;
    a :class:`DeliveryTransportError` from the transport maps to its explicit reason. ``gate`` must
    be a callback-required kind, else ``render_gate_note`` raises (a programming error, surfaced).
    """
    fields = marker_fields or {}
    if gate == "review_result":
        # Redmine #14971: every NEW canonical review_result is one atomic note whose prose finding
        # blocks and dedicated manifest come from this structured sequence.  ``None`` means the
        # caller used the pre-contract writer shape and is refused; ``()`` is the explicit empty
        # set required for an approved review.
        if review_findings is None:
            raise ReviewFindingManifestError(
                REASON_FINDINGS_INPUT_MISSING,
                "a canonical review_result requires structured review_findings",
            )
        notes = render_review_result_note(
            issue=issue,
            body=body,
            findings=review_findings,
            marker_fields=fields,
        )
    else:
        if review_findings is not None:
            raise ReviewFindingManifestError(
                "review_findings_wrong_gate",
                "structured review findings are valid only for a review_result gate",
            )
        notes = render_gate_note(gate, body=body, **fields)
    return _post(issue, notes, transport)


def attempt_emit_gate_record(*args, **kwargs) -> GateRecordAttempt:
    """Call :func:`emit_gate_record`, projecting a typed producer refusal as data."""

    try:
        return GateRecordAttempt(receipt=emit_gate_record(*args, **kwargs))
    except ReviewFindingManifestError as exc:
        return GateRecordAttempt(
            refusal=exc.reason or REASON_FINDINGS_INPUT_INVALID,
        )


def emit_progress_record(
    issue: str,
    kind: str,
    *,
    lane: str,
    lane_generation: object,
    body: str = "",
    transport: Optional[NoteWriteTransport],
) -> GateRecordReceipt:
    """Render a canonical **progress** note and post it via ``transport`` (fail-closed, opt-in).

    The producer half of the #13889 sweep watermark (review F2). The consumer
    (:func:`...domain.callback_sweep_watermark.progress_entries_after`) reads worker-side progress
    gates — ``review_finding_verdict`` / ``progress_log`` / ``start`` / ``design_consultation`` —
    that prove a lane is alive but owe the coordinator no callback. Without this writer those gates
    are recorded as prose (the real #13883 j#79995 / j#80002 shape), the sweep cannot classify them,
    and it must abstain from every stall verdict on that issue.

    This is the exact gap #13520 F1a closed for callback-required gates: a reader whose markers
    nothing in production writes finds nothing on a real issue. ``kind`` must be a progress-bearing
    kind and ``lane`` / ``lane_generation`` are required — an unscoped progress marker cannot be
    attributed to a dispatch round (review F3), so the renderer raises rather than emit one.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
        render_progress_note,
    )

    notes = render_progress_note(
        kind, lane=lane, lane_generation=lane_generation, body=body
    )
    return _post(issue, notes, transport)


def _post(
    issue: str, notes: str, transport: Optional[NoteWriteTransport]
) -> GateRecordReceipt:
    """Post a rendered, marker-bearing note (fail-closed, opt-in); shared by both writers."""
    if transport is None:
        return GateRecordReceipt(recorded=False, reason=GATE_RECORD_WRITE_OPTIN_UNSET)
    # Import lazily so this module carries no infrastructure dependency in the pure path.
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
        DeliveryTransportError,
        PERSIST_TRANSPORT_ERROR,
    )

    try:
        transport.post_issue_note(str(issue), notes)
    except DeliveryTransportError as exc:
        return GateRecordReceipt(recorded=False, reason=str(getattr(exc, "reason", PERSIST_TRANSPORT_ERROR)))
    except Exception:  # noqa: BLE001 - any unexpected transport blow-up is a fail-closed transport_error
        return GateRecordReceipt(recorded=False, reason=PERSIST_TRANSPORT_ERROR)
    return GateRecordReceipt(recorded=True, reason=GATE_RECORD_OK, location=f"redmine:issue={issue}")


__all__ = (
    "GATE_RECORD_OK",
    "GATE_RECORD_WRITE_OPTIN_UNSET",
    "NoteWriteTransport",
    "GateRecordAttempt",
    "GateRecordReceipt",
    "attempt_emit_gate_record",
    "emit_gate_record",
    "emit_progress_record",
    "review_findings_json_input",
)
