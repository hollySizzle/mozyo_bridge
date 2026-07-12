"""Callback outbox processor: ingest -> deliver-once -> sweep (Redmine #13520 / US #13518).

The application orchestration of the zero-wait callback delivery bounded context. It ties the
three layers the design answer (j#75098) fixed:

- the **exact-journal classifier** (:mod:`...domain.callback_delivery`, Q4) — the journal is
  the authority, the notification a pointer;
- the **idempotency-fenced outbox** (:class:`...core.state.workflow_runtime_store` callback
  API, Q3) — a UNIQUE key + ``BEGIN IMMEDIATE`` so a watcher restart / duplicate event /
  concurrent claimer never duplicates a delivery;
- a **one exact-target send** the caller injects (Q2) — the callback fires an existing
  semantic handoff once and authorizes no downstream action.

All I/O is injected: the ``store`` is the home-scoped runtime DB, the ``source`` is a
:class:`...domain.redmine_journal_source.RedmineJournalSource` (the credential-gated live
adapter in production, an in-memory source in tests), and ``sender`` is the one-send callable.
The processor itself resolves nothing live and holds no LLM turn — a background watcher wakes
it from a herdr CLI event *hint*, and it re-reads the exact Redmine journal.

Three entrypoints:

- :meth:`ingest` classifies each candidate against its exact source journal and enqueues it
  (classified -> ``pending``; unclassified -> ``dead_letter`` for a fresh-turn sweep), advancing
  the source cursor in the same transaction. Idempotent: a duplicate event enqueues no new row.
- :meth:`deliver` recovers crashed inflight rows, claims pending rows (single winner), and
  fires **one** send per row, mapping the send outcome to the closed store transition
  (delivered / bounded-retry-or-dead / uncertain-no-retry).
- :meth:`sweep` is the fresh-turn recovery: reconcile inflight, then surface the pending +
  dead-letter rows once so an LLM / operator reads the source journal. It delivers nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from mozyo_bridge.core.state.callback_outbox import (
    CALLBACK_CLAIM_LEASE_SECONDS,
    CallbackEnqueueResult,
    CallbackOutbox,
    CallbackOutboxKey,
    CallbackOutboxRow,
)
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_ABSENT,
    CALLBACK_DEAD_LETTER,
    CALLBACK_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    CallbackClassification,
    UNCLASSIFIED_SOURCE_UNREADABLE,
    SEND_DELIVERED,
    SEND_NOT_SENT,
    classify_callback_gate,
    normalize_send_result,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    SOURCE_REDMINE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalSource,
    markers_from_source,
)

#: A dead-letter gate placeholder: an unclassified journal has no adopted gate, but the outbox
#: key needs a non-empty ``normalized_gate``. This sentinel keeps the row addressable /
#: idempotent while marking it plainly as an unclassified dead-letter (never a real gate).
UNCLASSIFIED_GATE = "unclassified"


@dataclass(frozen=True)
class CallbackCandidate:
    """A handoff-worthy durable gate transition that needs a coordinator callback.

    ``issue`` / ``journal`` are the durable anchor of the exact source journal the classifier
    re-reads; ``callback_route`` is where the callback is delivered; ``notification_kind`` is
    the kind the *notification* claimed (a pointer only — the journal marker is the authority).
    """

    issue: str
    journal: str
    callback_route: str
    notification_kind: str = ""
    payload: str = ""


@dataclass(frozen=True)
class IngestOutcome:
    """One candidate's ingest result (classification + whether a fresh row was enqueued)."""

    candidate: CallbackCandidate
    classification: CallbackClassification
    enqueue: CallbackEnqueueResult
    dead_lettered: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.candidate.issue,
            "journal": self.candidate.journal,
            "callback_route": self.candidate.callback_route,
            "classification": self.classification.as_payload(),
            "inserted": self.enqueue.inserted,
            "state": self.enqueue.current_state,
            "dead_lettered": self.dead_lettered,
        }


@dataclass
class IngestReport:
    """The batch ingest result: per-candidate outcomes + roll-up counts."""

    outcomes: list[IngestOutcome] = field(default_factory=list)

    @property
    def enqueued(self) -> int:
        return sum(1 for o in self.outcomes if o.enqueue.inserted)

    @property
    def duplicates(self) -> int:
        return sum(1 for o in self.outcomes if not o.enqueue.inserted)

    @property
    def dead_lettered(self) -> int:
        return sum(1 for o in self.outcomes if o.dead_lettered)

    def as_payload(self) -> dict[str, object]:
        return {
            "enqueued": self.enqueued,
            "duplicates": self.duplicates,
            "dead_lettered": self.dead_lettered,
            "outcomes": [o.as_payload() for o in self.outcomes],
        }


@dataclass(frozen=True)
class DeliveryOutcome:
    """One claimed callback's delivery result: the send outcome + the resulting store state.

    ``resulting_state`` is the **actual persisted** callback-outbox state after the terminal
    mark (not the intended one). ``ownership_lost`` is True when the terminal mark no-op'd
    because the claim's lease expired mid-send and another processor reconciled the row — a
    duplicate delivery was still prevented, but this delivery's evidence is the reconciled state
    (#13520 review F2-R1).
    """

    key: CallbackOutboxKey
    send_outcome: str
    resulting_state: str
    ownership_lost: bool = False
    #: Best-effort durable-receipt evidence (#13520 review R2-F6): whether the sanctioned
    #: ``--persist-delivery`` Redmine receipt persisted (``None`` = no receipt reported), and its
    #: reason token. Observability only — it never changed ``send_outcome`` / ``resulting_state``.
    persist_ok: "bool | None" = None
    persist_reason: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.key.issue,
            "journal": self.key.journal,
            "normalized_gate": self.key.normalized_gate,
            "callback_route": self.key.callback_route,
            "send_outcome": self.send_outcome,
            "resulting_state": self.resulting_state,
            "ownership_lost": self.ownership_lost,
            "persist_ok": self.persist_ok,
            "persist_reason": self.persist_reason,
        }


@dataclass
class DeliveryReport:
    """A deliver() pass: recovered crashed rows + per-row delivery outcomes."""

    recovered: list[CallbackOutboxRow] = field(default_factory=list)
    delivered: list[DeliveryOutcome] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {
            "recovered": [r.as_payload() for r in self.recovered],
            "delivered": [d.as_payload() for d in self.delivered],
        }


@dataclass
class SweepReport:
    """A fresh-turn sweep: reconciled crashed rows + the pending / dead-letter backlog."""

    recovered: list[CallbackOutboxRow] = field(default_factory=list)
    pending: list[CallbackOutboxRow] = field(default_factory=list)
    dead_letter: list[CallbackOutboxRow] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {
            "recovered": [r.as_payload() for r in self.recovered],
            "pending": [r.as_payload() for r in self.pending],
            "dead_letter": [r.as_payload() for r in self.dead_letter],
        }


class CallbackOutboxProcessor:
    """Orchestrates ingest / deliver / sweep over the home-scoped callback outbox."""

    def __init__(
        self,
        outbox: CallbackOutbox,
        source: RedmineJournalSource,
        *,
        source_name: str = SOURCE_REDMINE,
    ) -> None:
        self._outbox = outbox
        self._source = source
        self._source_name = source_name

    # -- ingest ------------------------------------------------------------

    def _classify(self, candidate: CallbackCandidate) -> CallbackClassification:
        """Classify one candidate against its exact source journal (fail-closed on a read error).

        A read error (credential / transport / not-found) is not a delivery decision the
        watcher may guess around — it becomes an unclassified :data:`UNCLASSIFIED_SOURCE_UNREADABLE`
        so the candidate is dead-lettered rather than delivered on a stale / absent journal.
        """
        try:
            markers = markers_from_source(self._source, candidate.issue)
        except Exception:  # noqa: BLE001 - any source read failure is fail-closed unclassified
            return CallbackClassification(
                disposition="unclassified",
                normalized_gate="",
                reason=UNCLASSIFIED_SOURCE_UNREADABLE,
                notification_kind=candidate.notification_kind,
            )
        return classify_callback_gate(
            markers,
            candidate.issue,
            candidate.journal,
            notification_kind=candidate.notification_kind,
        )

    def ingest(
        self,
        candidates: Sequence[CallbackCandidate],
        *,
        cursor: Optional[str] = None,
        now: Optional[str] = None,
    ) -> IngestReport:
        """Classify each candidate and idempotently enqueue it; advance the cursor.

        A classified candidate enqueues ``pending`` (carrying the journal-adopted gate + a
        ``gate_mismatch`` flag when the notification disagreed). An **unclassified** candidate
        enqueues straight to ``dead_letter`` under the :data:`UNCLASSIFIED_GATE` sentinel — it is
        never delivered; a fresh-turn sweep surfaces it for an LLM to read the source journal.
        The enqueue is idempotent (UNIQUE key), so a duplicate event adds no row. ``cursor`` is
        an efficiency filter (persisted in the ingest transaction); overlap re-read + the UNIQUE
        key are the correctness authority, not the cursor.
        """
        report = IngestReport()
        for candidate in candidates:
            classification = self._classify(candidate)
            if classification.is_classified:
                key = CallbackOutboxKey(
                    source=self._source_name,
                    issue=candidate.issue,
                    journal=candidate.journal,
                    normalized_gate=classification.normalized_gate,
                    callback_route=candidate.callback_route,
                )
                result = self._outbox.enqueue(
                    key,
                    initial_state=CALLBACK_PENDING,
                    notification_kind=classification.notification_kind,
                    gate_mismatch=classification.mismatch,
                    detail="mismatch: journal gate adopted over notification"
                    if classification.mismatch
                    else "",
                    payload=candidate.payload,
                    cursor_source=self._source_name,
                    cursor=cursor,
                    now=now,
                )
                report.outcomes.append(
                    IngestOutcome(
                        candidate=candidate,
                        classification=classification,
                        enqueue=result,
                        dead_lettered=False,
                    )
                )
            else:
                key = CallbackOutboxKey(
                    source=self._source_name,
                    issue=candidate.issue,
                    journal=candidate.journal,
                    normalized_gate=UNCLASSIFIED_GATE,
                    callback_route=candidate.callback_route,
                )
                result = self._outbox.enqueue(
                    key,
                    initial_state=CALLBACK_DEAD_LETTER,
                    notification_kind=classification.notification_kind,
                    detail=f"unclassified: {classification.reason}",
                    payload=candidate.payload,
                    cursor_source=self._source_name,
                    cursor=cursor,
                    now=now,
                )
                report.outcomes.append(
                    IngestOutcome(
                        candidate=candidate,
                        classification=classification,
                        enqueue=result,
                        dead_lettered=True,
                    )
                )
        return report

    # -- deliver -----------------------------------------------------------

    def deliver(
        self,
        sender: Callable[[CallbackOutboxRow], str],
        *,
        limit: int = 32,
        stale_seconds: int = CALLBACK_CLAIM_LEASE_SECONDS,
        now: Optional[str] = None,
    ) -> DeliveryReport:
        """Recover crashed inflight rows, then fire **one** send per claimed pending row.

        First :meth:`recover_inflight_callbacks` reconciles rows a crashed processor left
        inflight (pre-send -> retry; post-send -> uncertain). Then a single-winner claim moves
        pending rows to inflight; for each the send edge is checkpointed
        (:meth:`mark_callback_sending`) *before* ``sender`` is invoked, so a crash mid-send is
        recoverable as uncertain. ``sender`` returns a closed :data:`SEND_OUTCOMES` token:

        - :data:`SEND_DELIVERED` -> ``delivered``;
        - :data:`SEND_NOT_SENT` (deterministic, pre-injection) -> bounded ``retry`` then
          ``dead_letter``;
        - :data:`SEND_UNCERTAIN` (or an unknown token, fail-safe) -> ``uncertain`` (no auto-retry).
        """
        report = DeliveryReport()
        # Recover only lease-expired (stale) inflight rows — never a concurrent processor's
        # fresh active claim (#13520 review F2). A default lease is used unless overridden.
        report.recovered.extend(self._outbox.recover_inflight(stale_seconds=stale_seconds, now=now))
        for row in self._outbox.claim_pending(limit=limit, now=now):
            token = row.claim_token
            # The send gate: mark_sending is token-conditional. If we no longer own the row
            # (its claim was recovered + re-claimed elsewhere) it returns False and we DO NOT
            # send — a de-owned processor never fires a duplicate callback.
            if not self._outbox.mark_sending(row.key, claim_token=token, now=now):
                continue
            # The sender may return a bare SEND_* token (legacy / test) or a CallbackSendResult
            # carrying durable-receipt evidence; normalize both (unknown -> uncertain, fail-safe).
            send_result = normalize_send_result(sender(row))
            outcome = send_result.outcome
            # The terminal mark is token-conditional. If it no-ops (rowcount 0 / ABSENT), the
            # lease expired mid-send and another processor reconciled the row — the report must
            # reflect the ACTUAL persisted state, not the intended one (#13520 review F2-R1), so
            # CLI / QA evidence never claims delivered while the durable state says uncertain.
            if outcome == SEND_DELIVERED:
                applied = self._outbox.mark_delivered(row.key, claim_token=token, now=now)
            elif outcome == SEND_NOT_SENT:
                applied = self._outbox.mark_retry_or_dead(
                    row.key, claim_token=token, now=now
                ) != CALLBACK_ABSENT
            else:
                applied = self._outbox.mark_uncertain(row.key, claim_token=token, now=now)
            # When the terminal mark applied, the persisted state is exactly what this delivery
            # intended; when it did not, read the state the reconciling processor actually left.
            resulting = self._outbox.state_of(row.key)
            report.delivered.append(
                DeliveryOutcome(
                    key=row.key,
                    send_outcome=outcome,
                    resulting_state=resulting,
                    ownership_lost=not applied,
                    persist_ok=send_result.persist_ok,
                    persist_reason=send_result.persist_reason,
                )
            )
        return report

    # -- sweep -------------------------------------------------------------

    def sweep(
        self, *, stale_seconds: int = CALLBACK_CLAIM_LEASE_SECONDS, now: Optional[str] = None
    ) -> SweepReport:
        """Fresh-turn recovery: reconcile crashed rows, then surface the backlog once.

        Reconciles inflight rows (crash recovery), then reads the pending + dead-letter rows so
        the caller can hand them to a single fresh LLM turn (an LLM reads the source journal for
        a dead-letter) — it delivers nothing itself. This is the once-only sweep the zero-wait
        doctrine relies on instead of an LLM-turn poll.
        """
        report = SweepReport()
        report.recovered.extend(self._outbox.recover_inflight(stale_seconds=stale_seconds, now=now))
        report.pending.extend(self._outbox.read(states=[CALLBACK_PENDING]))
        report.dead_letter.extend(self._outbox.read(states=[CALLBACK_DEAD_LETTER]))
        return report


__all__ = (
    "UNCLASSIFIED_GATE",
    "CallbackCandidate",
    "IngestOutcome",
    "IngestReport",
    "DeliveryOutcome",
    "DeliveryReport",
    "SweepReport",
    "CallbackOutboxProcessor",
)
