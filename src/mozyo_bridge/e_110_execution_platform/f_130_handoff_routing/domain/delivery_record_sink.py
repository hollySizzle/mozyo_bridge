"""Durable delivery-record persistence seam (Redmine #12311).

The handoff primitive (`orchestrate_handoff`) already owns receiver-pane
resolution, validation, typing, the landing rail, fail-closed rollback, the
structured :class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff.DeliveryOutcome`, and the
pasteable markdown record produced by
:func:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff.build_delivery_record`. Until this module
existed, persisting that record into the durable source-of-truth ticket system
(a Redmine journal note / an Asana comment) was a manual paste step: the
``DeliveryOutcome`` docstring named it an explicit follow-up.

This module is the *core-owned boundary* for that follow-up, designed to sit
inside the built-in ticket-adapter boundary
(``vibes/docs/logics/plugin-ready-adapter-boundary.md``). It is pure: no
network, no I/O, no credential handling, and it imports no provider
implementation — the dependency only ever points provider -> core, exactly like
``domain.ticket_adapter``.

What core owns here (and never delegates to a provider):

- **the record class.** A persisted delivery record is a
  :data:`RECORD_CLASS_DELIVERY` (``delivery_notification``) — a pointer that a
  pane notification was *typed/submitted*. It is deliberately NOT a workflow
  gate (``implementation_done`` / ``review_request`` / ``review_result``) and
  NOT an owner approval. Those are separate core constructs
  (``WorkflowGate`` / ``OwnerApproval`` via ``ticket_adapter``). A delivery
  receipt can never be read as review / completion / approval, and
  :class:`DeliveryRecordNote` fail-closes on any other class.
- **source semantics.** A Redmine note is a journal note on an issue; an Asana
  note is a comment on a task. They are not interchangeable: a sink whose
  provider does not match the note's source refuses to persist
  (``unsupported_source``) rather than silently mixing journal and comment
  semantics.
- **the secret / private-data rule.** The note carries only the already-redacted
  pasteable record body (``build_delivery_record`` keeps absolute / private
  paths out of pasteable text) plus durable-anchor ids. Neither
  :class:`DeliveryRecordNote` nor :class:`DeliveryReceipt` ever carries a token,
  API key, base URL, or any credential.

What a provider owns (the actual ticket-system write) is reached only through
the narrow :class:`RedmineNoteTransport` seam, which this module *defines* but
does not *implement*: the live, credential-gated Redmine journal-write transport
lives in ``infrastructure.redmine_note_transport`` (Redmine #12347), keeping the
dependency provider -> core. This module stays pure and import-free of that
provider; it owns only source/anchor validation and receipt shaping. When no
transport is injected (the default, and any caller that has not set the explicit
``MOZYO_REDMINE_DELIVERY_WRITE`` env opt-in) resolution falls back to the
fail-closed :class:`UnwiredDeliveryRecordSink` (``write_optin_unset``, Redmine
#13262); tests drive the full persisted path through an injected fake transport,
so no network runs here.

Non-goals (kept explicit so the seam does not drift):

- the pane message is never the durable source of truth; persistence is a
  best-effort *pointer* to the anchor, opt-in and never blocking the send;
- a delivery ACK is never task completion / review / approval;
- no credential is ever logged, journaled, or carried on a record / receipt;
- no third-party / arbitrary-code provider loading; Redmine is the only write
  provider category in v0.8 (Asana fails closed as ``unsupported_source``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import SOURCE_ASANA, SOURCE_REDMINE, SOURCES

# The class of a persisted delivery record. It is a *notification pointer*, not
# a workflow gate or an owner approval — those stay core constructs in
# ``ticket_adapter`` and are intentionally never expressible here.
RECORD_CLASS_DELIVERY = "delivery_notification"

# Receipt reasons. ``ok`` is the only persisted reason; the rest are explicit
# failure states (boundary doc Implementation Guardrail #4: provider failure
# must be explicit — unavailable / unauthorized / ambiguous / unknown — never a
# silent success).
#
# Redmine #13262: the single ``provider_unavailable`` reason previously collapsed
# two operationally distinct fail-closed causes into one indistinguishable token,
# so an operator could not tell "the live-write env opt-in was never set" (a plain
# ``--persist-delivery`` with no live write configured) apart from "the opt-in was
# set but the trusted Redmine base URL is missing / invalid" (a misconfiguration to
# fix). They are now split into :data:`PERSIST_WRITE_OPTIN_UNSET` and
# :data:`PERSIST_BASE_URL_UNSET`; ``provider_unavailable`` no longer has a producer
# in this delivery path and is intentionally removed from the vocabulary.
PERSIST_OK = "ok"
PERSIST_DISABLED = "disabled"
PERSIST_UNSUPPORTED_SOURCE = "unsupported_source"
# The live-write env opt-in (``MOZYO_REDMINE_DELIVERY_WRITE``) is unset, so no
# transport is injected and the unwired resolution seam fails closed. This is the
# byte-compatible staged posture of a plain ``--persist-delivery``, not a
# misconfiguration.
PERSIST_WRITE_OPTIN_UNSET = "write_optin_unset"
# The env opt-in is set (a transport is injected) but the trusted Redmine base URL
# (``MOZYO_REDMINE_URL``) is missing or not a valid http(s) host, so there is no
# trusted write destination. This IS a misconfiguration to fix, distinct from the
# opt-in simply being unset.
PERSIST_BASE_URL_UNSET = "base_url_unset"
PERSIST_CREDENTIAL_MISSING = "credential_missing"
PERSIST_UNAUTHORIZED = "unauthorized"
PERSIST_NO_ANCHOR = "no_anchor"
PERSIST_TRANSPORT_ERROR = "transport_error"

# The explicit failure vocabulary a transport may report. A transport that
# raises :class:`DeliveryTransportError` with anything outside this set is
# normalized to ``transport_error`` so a receipt reason is always one of these.
PERSIST_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        PERSIST_DISABLED,
        PERSIST_UNSUPPORTED_SOURCE,
        PERSIST_WRITE_OPTIN_UNSET,
        PERSIST_BASE_URL_UNSET,
        PERSIST_CREDENTIAL_MISSING,
        PERSIST_UNAUTHORIZED,
        PERSIST_NO_ANCHOR,
        PERSIST_TRANSPORT_ERROR,
    }
)


class DeliveryRecordError(ValueError):
    """A delivery record note violated the core contract (e.g. wrong class)."""


class DeliveryTransportError(Exception):
    """A provider transport failed to persist, with an explicit reason.

    ``reason`` is normalized to a member of :data:`PERSIST_FAILURE_REASONS`; an
    unrecognized reason degrades to ``transport_error``. The exception *message*
    is for the transport's own diagnostics and is never copied onto a
    :class:`DeliveryReceipt`, so a careless transport cannot leak a credential
    into a durable receipt.
    """

    def __init__(self, message: str = "", *, reason: str = PERSIST_TRANSPORT_ERROR):
        super().__init__(message)
        self.reason = reason if reason in PERSIST_FAILURE_REASONS else PERSIST_TRANSPORT_ERROR


@dataclass(frozen=True)
class DeliveryRecordNote:
    """The normalized, persistable delivery record.

    Built only via :func:`build_delivery_record_note` from a
    :class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff.DeliveryOutcome`. ``body`` is the
    already-redacted pasteable markdown from ``build_delivery_record``. The
    caller renders it for the durable sink path WITHOUT the free-text
    ``--record-command`` (Finding 1, j#62549) so no user-supplied free text is
    auto-journaled; every other body field is already redacted (no absolute /
    private paths). The record carries durable-anchor ids and the
    receiver/target identity, never a credential.
    """

    record_class: str
    source: str
    body: str
    receiver: str
    status: str
    reason: str
    target: Optional[str] = None
    issue_id: Optional[str] = None
    task_id: Optional[str] = None
    has_duplicate_advisory: bool = False

    def __post_init__(self) -> None:
        if self.record_class != RECORD_CLASS_DELIVERY:
            raise DeliveryRecordError(
                f"a delivery record note must be classed {RECORD_CLASS_DELIVERY!r} "
                f"(a notification pointer, never a workflow gate or owner "
                f"approval); got {self.record_class!r}"
            )
        if self.source not in SOURCES:
            raise DeliveryRecordError(
                f"unknown delivery record source: {self.source!r}; expected one "
                f"of {sorted(SOURCES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Explicit, credential-free projection (no ``asdict`` field smuggling)."""
        return {
            "record_class": self.record_class,
            "source": self.source,
            "receiver": self.receiver,
            "status": self.status,
            "reason": self.reason,
            "target": self.target,
            "issue_id": self.issue_id,
            "task_id": self.task_id,
            "has_duplicate_advisory": self.has_duplicate_advisory,
        }


@dataclass(frozen=True)
class DeliveryReceipt:
    """The outcome of attempting to persist a delivery record.

    Carries only a provider id, the persisted flag, an explicit reason, an
    optional durable ``location`` pointer (e.g. ``redmine:issue=12311:journal=...``),
    and the record class — never a credential, never the record body.
    """

    provider: Optional[str]
    persisted: bool
    reason: str
    location: Optional[str] = None
    record_class: str = RECORD_CLASS_DELIVERY

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "persisted": self.persisted,
            "reason": self.reason,
            "location": self.location,
            "record_class": self.record_class,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@runtime_checkable
class RedmineNoteTransport(Protocol):
    """The narrow provider-owned write seam for a Redmine journal note.

    Defined in core; the live, credential-gated implementation (reusing the
    trusted-base / API-key boundary in ``redmine_context``) is a deferred
    follow-up under per-task review. ``post_issue_note`` posts ``notes`` to the
    given issue and returns the new journal id (empty string if the tracker did
    not report one). It raises :class:`DeliveryTransportError` with an explicit
    reason on failure; it must never let a credential reach the returned id.
    """

    def post_issue_note(self, issue_id: str, notes: str) -> str:
        ...


@runtime_checkable
class DeliveryRecordSink(Protocol):
    """The built-in delivery-record persistence boundary.

    A sink turns a :class:`DeliveryRecordNote` into a :class:`DeliveryReceipt`.
    Implementations are built-in only (no dynamic loading). A sink owns no
    workflow / approval authority — it persists a notification pointer.
    """

    name: str

    def persist(self, note: DeliveryRecordNote) -> DeliveryReceipt:
        ...


class NullDeliveryRecordSink:
    """The default sink: persistence is opt-out, so nothing is written.

    Keeps the handoff behavior byte-identical when ``--persist-delivery`` is not
    passed: the pasteable record still prints to stdout; no durable write occurs.
    """

    name = "none"

    def persist(self, note: DeliveryRecordNote) -> DeliveryReceipt:
        return DeliveryReceipt(provider=None, persisted=False, reason=PERSIST_DISABLED)


class UnsupportedSourceDeliveryRecordSink:
    """Fail-closed sink for a source with no built-in write provider.

    v0.8 ships exactly one write provider category (Redmine). An Asana anchor is
    not silently coerced into Redmine journal semantics; it fails closed with
    ``unsupported_source`` so the source/comment-vs-journal boundary is explicit.
    """

    def __init__(self, source: str):
        self.name = source
        self._source = source

    def persist(self, note: DeliveryRecordNote) -> DeliveryReceipt:
        return DeliveryReceipt(
            provider=self._source,
            persisted=False,
            reason=PERSIST_UNSUPPORTED_SOURCE,
        )


class UnwiredDeliveryRecordSink:
    """Resolution seam for a Redmine source with no injected write transport.

    The live Redmine journal-write transport now exists
    (``infrastructure.redmine_note_transport``, Redmine #12347) but is gated by
    an explicit ``MOZYO_REDMINE_DELIVERY_WRITE`` env opt-in: when it is unset no
    transport is injected and resolution falls back here, failing closed with
    ``write_optin_unset`` (Redmine #13262) so a plain ``--persist-delivery`` stays
    byte-compatible. That reason names the specific cause — the live-write env
    opt-in is unset — distinct from a set-opt-in-but-missing-base-URL
    misconfiguration (``base_url_unset``, raised by the transport). This is the
    same fail-closed posture as the provider-selection resolution (Redmine #12249).
    """

    def __init__(self, source: str):
        self.name = source
        self._source = source

    def persist(self, note: DeliveryRecordNote) -> DeliveryReceipt:
        return DeliveryReceipt(
            provider=self._source,
            persisted=False,
            reason=PERSIST_WRITE_OPTIN_UNSET,
        )


class RedmineDeliveryRecordSink:
    """Persist a delivery record as a Redmine journal note via an injected transport.

    Pure over the transport seam: it owns the source/anchor validation and the
    receipt shaping, and delegates the actual network write to
    :class:`RedmineNoteTransport`. A non-Redmine note fails closed with
    ``unsupported_source`` (journal vs comment semantics are not mixed); a note
    with no issue anchor fails closed with ``no_anchor``; a transport failure is
    surfaced with the transport's explicit reason.
    """

    name = SOURCE_REDMINE

    def __init__(self, transport: RedmineNoteTransport):
        self._transport = transport

    def persist(self, note: DeliveryRecordNote) -> DeliveryReceipt:
        if note.source != SOURCE_REDMINE:
            return DeliveryReceipt(
                provider=self.name,
                persisted=False,
                reason=PERSIST_UNSUPPORTED_SOURCE,
            )
        if not note.issue_id:
            return DeliveryReceipt(
                provider=self.name,
                persisted=False,
                reason=PERSIST_NO_ANCHOR,
            )
        try:
            journal_id = self._transport.post_issue_note(note.issue_id, note.body)
        except DeliveryTransportError as exc:
            return DeliveryReceipt(
                provider=self.name, persisted=False, reason=exc.reason
            )
        location = (
            f"redmine:issue={note.issue_id}:journal={journal_id}"
            if journal_id
            else f"redmine:issue={note.issue_id}"
        )
        return DeliveryReceipt(
            provider=self.name,
            persisted=True,
            reason=PERSIST_OK,
            location=location,
        )


def build_delivery_record_note(
    outcome: Any,
    *,
    record_markdown: str,
    has_duplicate_advisory: bool = False,
) -> DeliveryRecordNote:
    """Build a :class:`DeliveryRecordNote` from a structured handoff outcome.

    ``record_markdown`` is the already-rendered, redacted pasteable record
    (``build_delivery_record`` output) so the persisted body and the printed
    body never diverge. The source/anchor ids come from the outcome's normalized
    anchor. Raises :class:`DeliveryRecordError` when the outcome carries no known
    source (a blocked-before-anchor outcome has no durable target to persist).
    Pure; no I/O.
    """
    anchor = outcome.anchor or {}
    source = outcome.source or anchor.get("source")
    if source not in SOURCES:
        raise DeliveryRecordError(
            f"cannot build a delivery record note without a known source; "
            f"got {source!r}"
        )
    issue_id: Optional[str] = None
    task_id: Optional[str] = None
    if source == SOURCE_REDMINE:
        issue_id = anchor.get("issue")
    elif source == SOURCE_ASANA:
        task_id = anchor.get("task_id")
    return DeliveryRecordNote(
        record_class=RECORD_CLASS_DELIVERY,
        source=source,
        body=record_markdown,
        receiver=outcome.receiver,
        status=outcome.status,
        reason=outcome.reason,
        target=outcome.target,
        issue_id=issue_id,
        task_id=task_id,
        has_duplicate_advisory=has_duplicate_advisory,
    )


def resolve_delivery_record_sink(
    *,
    enabled: bool,
    source: str,
    redmine_transport: Optional[RedmineNoteTransport] = None,
) -> DeliveryRecordSink:
    """Resolve the delivery-record sink for a send, fail-closed.

    - ``enabled=False`` (the default, opt-out) -> :class:`NullDeliveryRecordSink`,
      so the handoff behavior is byte-identical.
    - ``source=redmine`` with no transport -> :class:`UnwiredDeliveryRecordSink`
      (``write_optin_unset``, Redmine #13262): the live write is gated by the
      explicit ``MOZYO_REDMINE_DELIVERY_WRITE`` env opt-in, so without it no
      transport is injected. With an injected transport (the env opt-in set, or a
      test fake) -> :class:`RedmineDeliveryRecordSink`.
    - ``source=asana`` (or any non-Redmine source) ->
      :class:`UnsupportedSourceDeliveryRecordSink` (``unsupported_source``).
    """
    if not enabled:
        return NullDeliveryRecordSink()
    if source == SOURCE_REDMINE:
        if redmine_transport is None:
            return UnwiredDeliveryRecordSink(SOURCE_REDMINE)
        return RedmineDeliveryRecordSink(redmine_transport)
    if source == SOURCE_ASANA:
        return UnsupportedSourceDeliveryRecordSink(SOURCE_ASANA)
    return UnsupportedSourceDeliveryRecordSink(source or "unknown")


__all__ = (
    "DeliveryRecordError",
    "DeliveryRecordNote",
    "DeliveryRecordSink",
    "DeliveryReceipt",
    "DeliveryTransportError",
    "NullDeliveryRecordSink",
    "PERSIST_BASE_URL_UNSET",
    "PERSIST_CREDENTIAL_MISSING",
    "PERSIST_DISABLED",
    "PERSIST_FAILURE_REASONS",
    "PERSIST_NO_ANCHOR",
    "PERSIST_OK",
    "PERSIST_TRANSPORT_ERROR",
    "PERSIST_WRITE_OPTIN_UNSET",
    "PERSIST_UNAUTHORIZED",
    "PERSIST_UNSUPPORTED_SOURCE",
    "RECORD_CLASS_DELIVERY",
    "RedmineDeliveryRecordSink",
    "RedmineNoteTransport",
    "UnsupportedSourceDeliveryRecordSink",
    "UnwiredDeliveryRecordSink",
    "build_delivery_record_note",
    "resolve_delivery_record_sink",
)
