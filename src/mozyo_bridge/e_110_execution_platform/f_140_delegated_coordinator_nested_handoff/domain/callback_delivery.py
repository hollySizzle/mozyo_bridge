"""Zero-wait callback delivery domain (Redmine #13520 / US #13518).

The zero-wait doctrine (``skills/mozyo-bridge-agent/references/workflow.md``
``## Wait / polling 効率標準`` ``### dispatch / handoff 後は LLM turn を zero-wait で終了する``):
after a dispatch / handoff, an LLM coordinator / gateway / worker turn ends without a
blocking wait; a **durable callback** starts the next turn. This module is the pure heart of
that callback: given the exact source journal that a handoff-worthy durable gate transition
landed on, it decides *which* normalized gate the callback carries — and refuses to guess.

Design answer j#75098 fixes the boundaries this module holds:

- **Q4 — the notification is a pointer, the journal is the authority.** The classifier never
  trusts a notification's claimed ``kind`` / ``summary``. It reads the **exact source
  journal**'s structured gate marker (:func:`...domain.redmine_journal_source.markers_from_source`
  — a machine ``[mozyo:…]`` token, never prose) and adopts *that* normalized gate. When the
  notification's claimed kind disagrees with the journal's marker, the journal wins and the
  disagreement is recorded (:attr:`CallbackClassification.mismatch`). When the exact journal
  cannot be read structurally — unreadable source, no gate marker, more than one distinct gate
  marker, or an issue / journal that does not match the request — the result is
  :data:`CLASSIFY_UNCLASSIFIED` (fail-closed): the caller enqueues it dead-letter for a single
  fresh-turn sweep (an LLM reads the source journal), and delivers **nothing**. A prose
  heuristic never promotes an unclassified journal to a review / close approval.
- **Q2 — a callback is a delivery, not an authorization.** The normalized gate is only the
  handoff-worthy state the callback *points at*; firing the callback authorizes no downstream
  domain action. The receiver reads the journal and decides.

The module is pure: it consumes already-read :class:`JournalMarker` values and returns value
objects. Reading Redmine (credential-gated, exact-journal) and delivering the callback are the
application layer's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    MARKER_GATE_ALIASES,
    SOURCE_REDMINE,
    JournalMarker,
)

# ---------------------------------------------------------------------------
# Classification disposition (per callback candidate). Whether the exact source journal
# yielded a single, structurally-read gate the callback can carry.
# ---------------------------------------------------------------------------

#: The exact journal carried exactly one structured gate marker — the callback's normalized
#: gate is adopted from it (the journal is the authority).
CLASSIFY_CLASSIFIED = "classified"
#: The exact journal could not be read structurally into a single gate — fail-closed. The
#: callback is enqueued dead-letter for a single fresh-turn sweep; nothing is delivered.
CLASSIFY_UNCLASSIFIED = "unclassified"

# ---------------------------------------------------------------------------
# Fail-closed reasons for an unclassified journal (a closed vocabulary; never a prose guess).
# ---------------------------------------------------------------------------

#: The exact journal carried no structured gate-bearing marker at all.
UNCLASSIFIED_GATE_MARKER_MISSING = "gate_marker_missing"
#: The exact journal carried more than one *distinct* gate marker — ambiguous, so the watcher
#: refuses to pick one silently.
UNCLASSIFIED_GATE_MARKER_AMBIGUOUS = "gate_marker_ambiguous"
#: A marker's own anchor did not match the requested issue / journal (a pointer to a different
#: durable fact); the callback is not delivered against a mismatched anchor.
UNCLASSIFIED_ISSUE_JOURNAL_MISMATCH = "issue_journal_mismatch"
#: The exact source journal could not be read (credential / transport / not-found). Set by the
#: application layer when the read raises; kept here so the reason vocabulary is one place.
UNCLASSIFIED_SOURCE_UNREADABLE = "source_unreadable"

# ---------------------------------------------------------------------------
# Send outcome vocabulary. What the application's one exact-target send reported, mapped to a
# closed vocabulary so the store transition (delivered / retry-or-dead / uncertain) is
# deterministic and never a duplicate delivery.
# ---------------------------------------------------------------------------

#: The one send was positively delivered (turn-start / delivery confirmed) -> ``delivered``.
SEND_DELIVERED = "delivered"
#: A **deterministic not-sent** failure detected *before* injection (route unresolved,
#: refused, target ambiguous) -> bounded ``retry`` then ``dead_letter``. Nothing was injected,
#: so a retry cannot duplicate.
SEND_NOT_SENT = "not_sent"
#: The send was injected but its outcome is unknown (ACK-only, turn-start unconfirmed, crash
#: after the send edge) -> ``uncertain``, never auto-retried (a duplicate is the failure to
#: avoid).
SEND_UNCERTAIN = "uncertain"

SEND_OUTCOMES = frozenset({SEND_DELIVERED, SEND_NOT_SENT, SEND_UNCERTAIN})


# ---------------------------------------------------------------------------
# Delivery-outcome -> send-outcome mapping. The one callback send goes through the existing
# handoff primitive, which reports a ``DeliveryOutcome`` (status + reason). This maps that onto
# the closed :data:`SEND_OUTCOMES` vocabulary, conservatively (a duplicate delivery is the
# failure to avoid, #13520 j#75098 / j#75108): ONLY a positively-confirmed turn-start is
# ``delivered``; a deterministic pre-injection block (nothing was typed) is ``not_sent`` (safe
# bounded retry); everything ambiguous (marker not observed, turn-start unconfirmed, inject
# failure, unknown) is ``uncertain`` and never auto-retried.
# ---------------------------------------------------------------------------

#: Handoff ``status=="sent"`` reasons that mean the send positively landed / started.
_DELIVERED_SENT_REASONS = frozenset({"ok", "queue_enter"})

#: Handoff ``status=="blocked"`` reasons that are **deterministic pre-injection** — the send
#: was refused *before anything was typed*, so a retry cannot duplicate. Anything NOT in this set
#: (marker_timeout / turn_start_unconfirmed / inject_failed / receiver_blocked / turn_start_absent
#: / unknown) is treated as uncertain.
#:
#: ``receiver_blocked`` and ``turn_start_absent`` are DELIBERATELY excluded (#13520 review F2,
#: j#75381): they are the herdr turn-start rail's **post-injection** outcomes — ``OUTCOME_BLOCKED``
#: ("injected, timed out, re-snapshot found a runtime block") and ``OUTCOME_ABSENT``, both of which
#: report ``TurnStartResult.delivered == True`` (``turn_start_rail.py``; ``handoff.py`` documents
#: receiver_blocked as "the injection was delivered but the rail re-snapshotted a runtime block").
#: The body may already be on the receiver, so a bounded retry would DUPLICATE the callback. They
#: therefore fall through to ``uncertain`` (no auto-retry). Every reason below is a genuine
#: pre-injection refusal (``delivered == False`` / route-resolution / precondition failure): the
#: send edge was never crossed, so a bounded retry is safe.
_NOT_SENT_BLOCKED_REASONS = frozenset(
    {
        "target_unavailable",
        "target_not_agent",
        "invalid_anchor",
        "invalid_args",
        "precondition_not_idle",
        "cross_session_claude",
        "target_repo_mismatch",
        "gateway_route_blocked",
        "main_lane_implementation_blocked",
    }
)


def send_outcome_for_delivery(status: str, reason: str) -> str:
    """Map a handoff ``DeliveryOutcome`` (status, reason) onto a closed send outcome (pure).

    - ``sent`` + a positive reason (``ok`` / ``queue_enter``) -> :data:`SEND_DELIVERED`;
    - ``blocked`` + a deterministic pre-injection reason -> :data:`SEND_NOT_SENT`
      (nothing typed, so a bounded retry is safe);
    - **everything else** — ``blocked`` with an ambiguous or post-injection reason
      (``marker_timeout`` / ``turn_start_unconfirmed`` / ``inject_failed`` / ``receiver_blocked``
      / ``turn_start_absent`` / anything unrecognized), or an unexpected status
      (``pending_input``) — -> :data:`SEND_UNCERTAIN` (no auto-retry; a duplicate send is the
      failure to avoid). ``receiver_blocked`` / ``turn_start_absent`` are post-injection rail
      outcomes (``delivered == True``), so they must NOT be retried (#13520 review F2). The
      default is deliberately the safe one.
    """
    status_s = str(status or "").strip()
    reason_s = str(reason or "").strip()
    if status_s == "sent" and reason_s in _DELIVERED_SENT_REASONS:
        return SEND_DELIVERED
    if status_s == "blocked" and reason_s in _NOT_SENT_BLOCKED_REASONS:
        return SEND_NOT_SENT
    return SEND_UNCERTAIN


def normalize_gate_name(name: str) -> str:
    """Map a marker-facing gate name onto the runtime gate (``review_result`` -> ``review``).

    Applies the shared :data:`...redmine_event_intake.MARKER_GATE_ALIASES` so a notification
    that claims ``review_result`` and a journal marker that normalizes to ``review`` are not a
    false mismatch.
    """
    raw = str(name or "").strip()
    return MARKER_GATE_ALIASES.get(raw, raw)


@dataclass(frozen=True)
class CallbackClassification:
    """The exact-journal classification of one callback candidate (pure; journal is authority).

    ``disposition`` is :data:`CLASSIFY_CLASSIFIED` or :data:`CLASSIFY_UNCLASSIFIED`.
    ``normalized_gate`` is the gate adopted from the journal's structured marker (empty when
    unclassified). ``mismatch`` is True when a notification's claimed kind disagreed with the
    journal's marker (the journal still wins; the disagreement is recorded). ``reason`` is the
    fail-closed reason when unclassified (empty when classified).
    """

    disposition: str
    normalized_gate: str
    mismatch: bool = False
    reason: str = ""
    notification_kind: str = ""

    @property
    def is_classified(self) -> bool:
        return self.disposition == CLASSIFY_CLASSIFIED

    def as_payload(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "normalized_gate": self.normalized_gate,
            "mismatch": self.mismatch,
            "reason": self.reason,
            "notification_kind": self.notification_kind,
        }


def classify_callback_gate(
    markers: Iterable[JournalMarker],
    issue: str,
    journal: str,
    *,
    notification_kind: str = "",
) -> CallbackClassification:
    """Classify a callback candidate from the **exact source journal**'s markers (pure).

    ``markers`` are the structured gate markers already read from the source issue
    (:func:`...redmine_journal_source.markers_from_source`); this filters them to the exact
    ``(issue, journal)`` anchor and decides the normalized gate the callback carries:

    - a marker whose anchor does not match the requested issue **and** journal is not this
      journal's fact — if *none* match, the journal has no gate marker
      (:data:`UNCLASSIFIED_GATE_MARKER_MISSING`); if a marker matches the journal but names a
      different issue, that is :data:`UNCLASSIFIED_ISSUE_JOURNAL_MISMATCH`;
    - exactly one *distinct* normalized gate on the exact journal -> :data:`CLASSIFY_CLASSIFIED`
      carrying that gate. A ``notification_kind`` that normalizes to a different gate sets
      ``mismatch`` (the journal still wins);
    - more than one distinct normalized gate -> :data:`UNCLASSIFIED_GATE_MARKER_AMBIGUOUS`.

    The classifier never reads prose and never trusts the notification's kind as authority —
    an unclassified journal delivers nothing (the caller dead-letters it for a fresh-turn
    sweep). ``notification_kind`` is recorded for the mismatch signal only.
    """
    issue_s = str(issue).strip()
    journal_s = str(journal).strip()
    norm_notification = normalize_gate_name(notification_kind)

    def _unclassified(reason: str) -> CallbackClassification:
        return CallbackClassification(
            disposition=CLASSIFY_UNCLASSIFIED,
            normalized_gate="",
            mismatch=False,
            reason=reason,
            notification_kind=norm_notification,
        )

    # Markers that land on the exact journal id (regardless of issue, so a cross-issue anchor
    # is detectable as a mismatch rather than silently dropped).
    on_journal = [mk for mk in markers if str(mk.journal).strip() == journal_s]
    if not on_journal:
        return _unclassified(UNCLASSIFIED_GATE_MARKER_MISSING)

    # A marker on this journal but naming a different issue is a mismatched anchor.
    matching = [mk for mk in on_journal if str(mk.issue).strip() == issue_s]
    if not matching:
        return _unclassified(UNCLASSIFIED_ISSUE_JOURNAL_MISMATCH)

    distinct_gates = list(dict.fromkeys(normalize_gate_name(mk.gate) for mk in matching))
    if len(distinct_gates) > 1:
        return _unclassified(UNCLASSIFIED_GATE_MARKER_AMBIGUOUS)

    normalized_gate = distinct_gates[0]
    mismatch = bool(norm_notification) and norm_notification != normalized_gate
    return CallbackClassification(
        disposition=CLASSIFY_CLASSIFIED,
        normalized_gate=normalized_gate,
        mismatch=mismatch,
        reason="",
        notification_kind=norm_notification,
    )


__all__ = (
    "SOURCE_REDMINE",
    "CLASSIFY_CLASSIFIED",
    "CLASSIFY_UNCLASSIFIED",
    "UNCLASSIFIED_GATE_MARKER_MISSING",
    "UNCLASSIFIED_GATE_MARKER_AMBIGUOUS",
    "UNCLASSIFIED_ISSUE_JOURNAL_MISMATCH",
    "UNCLASSIFIED_SOURCE_UNREADABLE",
    "SEND_DELIVERED",
    "SEND_NOT_SENT",
    "SEND_UNCERTAIN",
    "SEND_OUTCOMES",
    "send_outcome_for_delivery",
    "normalize_gate_name",
    "CallbackClassification",
    "classify_callback_gate",
)
