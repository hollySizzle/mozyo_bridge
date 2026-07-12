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
from typing import Optional, Protocol

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    render_gate_note,
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


def emit_gate_record(
    issue: str,
    gate: str,
    *,
    body: str = "",
    transport: Optional[NoteWriteTransport],
    marker_fields: Optional[dict] = None,
) -> GateRecordReceipt:
    """Render a canonical gate note and post it via ``transport`` (fail-closed, opt-in).

    Renders the note through :func:`render_gate_note` (always marker-bearing) and posts it. A
    ``None`` transport (opt-in unset) writes nothing and returns a ``write_optin_unset`` receipt;
    a :class:`DeliveryTransportError` from the transport maps to its explicit reason. ``gate`` must
    be a callback-required kind, else ``render_gate_note`` raises (a programming error, surfaced).
    """
    notes = render_gate_note(gate, body=body, **(marker_fields or {}))
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
    "GateRecordReceipt",
    "emit_gate_record",
)
