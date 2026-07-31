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
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    MARKER_GATE_ALIASES,
    SOURCE_REDMINE,
    JournalMarker,
)
# Redmine #14232: the ONE injection-stage authority over "may a blind retry duplicate?". This
# module's own private reason table used to answer that question separately (and differently)
# from the handoff positive-delivery gate; it now projects the shared answer. The f_140 domain
# already depends on the f_130 handoff domain (``gateway_route_enforcement`` /
# ``hibernate_park_record`` / ``workflow_step_callback``), so this adds no new coupling
# direction and the authority module imports nothing back.
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    POST_INJECTION_BLOCKED_REASONS,
    PRE_INJECTION_BLOCKED_REASONS,
    STAGE_NOT_SENT,
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
    injection_stage_for,
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
# Action-time review-round disposition (#13974 review R8-F1)
# ---------------------------------------------------------------------------
# The final send-edge round fence must NOT collapse every refusal into the retryable
# ``SEND_NOT_SENT`` bucket. A review round that a *readable* provider says is deterministically
# superseded / invalid must be terminal (zero-send, retry 0, operator-visible) — otherwise the row
# bounded-retries as pending and #13974's backlog-retention failure survives at the final authority.
# But an *unreadable* provider (a transient read failure) must stay retryable, or a genuinely-current
# callback is dropped forever. The fence therefore returns one of three dispositions, not a bool.

#: The reserved review round is STILL current at the send edge -> proceed to the transport.
REVIEW_ROUND_CURRENT = "review_round_current"
#: A *readable* provider deterministically supersedes / invalidates the round (identity
#: mismatch / missing / malformed / drift / ambiguity / conflict, or a row with no verifiable
#: identity) -> terminal zero-send (mapped to :data:`SEND_UNCERTAIN` -> ``mark_uncertain``: retry 0,
#: no auto-retry, operator-visible), never a bounded-retry pending row.
REVIEW_ROUND_STALE = "review_round_stale_terminal"
#: The provider read itself failed transiently (source unresolvable / ``None`` / markers unreadable /
#: the fence raised) -> retryable zero-send (mapped to :data:`SEND_NOT_SENT` -> bounded retry then
#: dead-letter). A round we merely could not re-verify is never terminally dropped.
REVIEW_ROUND_UNVERIFIABLE = "review_round_unverifiable"

REVIEW_ROUND_DISPOSITIONS = frozenset(
    {REVIEW_ROUND_CURRENT, REVIEW_ROUND_STALE, REVIEW_ROUND_UNVERIFIABLE}
)


@dataclass(frozen=True)
class CallbackSendResult:
    """A send's closed outcome plus best-effort durable-receipt evidence (#13520 review R2-F6).

    ``outcome`` is a member of :data:`SEND_OUTCOMES` (the store transition authority).
    ``persist_ok`` / ``persist_reason`` are **observability only** — whether the sanctioned
    ``--persist-delivery`` Redmine receipt was written and its reason token — and NEVER change the
    outcome (the outbox row is the durability authority). ``persist_ok`` is ``None`` when no receipt
    was reported. A sender may return a bare :data:`SEND_OUTCOMES` string instead (legacy /
    evidence-less); :func:`normalize_send_result` accepts both.
    """

    outcome: str
    persist_ok: Optional[bool] = None
    persist_reason: str = ""
    #: The SEND edge's own reason token, normalized through
    #: :func:`normalize_zero_send_reason` (Redmine #14248 review j#85410 F1). Distinct from
    #: ``persist_reason``, which is the durable-receipt reason: a zero-send can carry an
    #: authorization / transport-precondition reason while no receipt was ever attempted, so the
    #: two are not interchangeable. Observability ONLY — it never changes ``outcome``. Secret-safe
    #: by construction: an out-of-vocabulary value is replaced by the fixed
    #: :data:`UNRECOGNIZED_ZERO_SEND_REASON` and the raw string is dropped.
    send_reason: str = ""


def normalize_send_result(value: object) -> CallbackSendResult:
    """Normalize a sender return (a bare outcome string OR a :class:`CallbackSendResult`).

    A string is taken as the outcome with no persist evidence; a :class:`CallbackSendResult` passes
    through; anything else (including an unknown outcome token) fails safe to
    :data:`SEND_UNCERTAIN` with no evidence (a send whose fate is unreadable is never auto-retried).
    """
    if isinstance(value, CallbackSendResult):
        outcome = value.outcome if value.outcome in SEND_OUTCOMES else SEND_UNCERTAIN
        return CallbackSendResult(
            outcome,
            persist_ok=value.persist_ok,
            persist_reason=value.persist_reason,
            send_reason=value.send_reason,
        )
    if isinstance(value, str) and value in SEND_OUTCOMES:
        return CallbackSendResult(value)
    return CallbackSendResult(SEND_UNCERTAIN)


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
#:
#: Redmine #14232: retained only as an input to the zero-send reason allowlist below (a
#: diagnostic may legitimately carry either token). It is NO LONGER a delivery classifier —
#: :func:`send_outcome_for_delivery` delegates to the shared injection-stage authority, which
#: admits only ``sent`` + ``ok`` as a confirmed submission. See that function's ruling.
_DELIVERED_SENT_REASONS = frozenset({"ok", "queue_enter"})

#: Handoff ``status=="blocked"`` reasons that are **deterministic pre-injection** — the send
#: was refused *before anything was typed*, so a retry cannot duplicate.
#:
#: Redmine #14232: the private table this module used to own is gone. The classification is the
#: shared, exhaustive, drift-guarded partition
#: :data:`...f_130_handoff_routing.domain.injection_stage.PRE_INJECTION_BLOCKED_REASONS`, which
#: this alias re-exports so the reason allowlist below and every existing importer keep working.
#: Two documented zero-send refusals were **missing** from the old private table
#: (``reader_upgrade_required`` / ``execution_root_outside_target_repo``), so both classified as
#: uncertain and were never bounded-retried at all — exactly the "one question, several private
#: tables" shape #14232 j#84877 recorded. The shared partition is checked against the whole
#: ``Reason`` wire vocabulary by a drift-guard test, so a newly added reason cannot be forgotten
#: into a bucket by default.
#:
#: The post-injection rationale is preserved at the authority: ``receiver_blocked`` /
#: ``turn_start_absent`` / ``marker_timeout`` / ``turn_start_unconfirmed`` / ``inject_failed`` /
#: ``transport_error`` stay excluded (#13520 review F2, j#75381) because the body may already be
#: on the receiver, so a bounded retry would DUPLICATE the callback.
_NOT_SENT_BLOCKED_REASONS = PRE_INJECTION_BLOCKED_REASONS

#: The projection of the shared injection-stage vocabulary onto :data:`SEND_OUTCOMES`. Total over
#: :data:`...injection_stage.INJECTION_STAGES` (a drift-guard test asserts the coverage), so
#: :func:`send_outcome_for_delivery` needs no fallback branch of its own — the authority's own
#: fail-closed default is already ``uncertain_partial``.
_SEND_OUTCOME_BY_STAGE: dict = {
    STAGE_SUBMITTED_CONFIRMED: SEND_DELIVERED,
    STAGE_NOT_SENT: SEND_NOT_SENT,
    STAGE_UNCERTAIN_PARTIAL: SEND_UNCERTAIN,
}


def send_outcome_for_delivery(
    status: str, reason: str, *, injection_stage: str = ""
) -> str:
    """Map a handoff ``DeliveryOutcome`` (status, reason) onto a closed send outcome (pure).

    Redmine #14232 acceptance 4 ("the callback / outbox sender reuses the same
    classification"): this is now a **projection of the one injection-stage authority**
    (:func:`...f_130_handoff_routing.domain.injection_stage.injection_stage_for`) onto
    :data:`SEND_OUTCOMES`, not a second private reason table:

    - :data:`...injection_stage.STAGE_SUBMITTED_CONFIRMED` -> :data:`SEND_DELIVERED`;
    - :data:`...injection_stage.STAGE_NOT_SENT` -> :data:`SEND_NOT_SENT` (nothing typed, so a
      bounded retry cannot duplicate);
    - :data:`...injection_stage.STAGE_UNCERTAIN_PARTIAL` -> :data:`SEND_UNCERTAIN` (no
      auto-retry; a duplicate send is the failure to avoid). That stage is also the authority's
      own fail-closed default, so an unrecognised status / reason still lands on the safe
      outcome — the previous behaviour, now stated once instead of twice.

    **Two deliberate behaviour changes** (both fail-closed or strictly more accurate):

    1. ``sent`` + ``queue_enter`` is no longer :data:`SEND_DELIVERED`. That reason means the
       landing marker was **never observed** — the relaxed rail pressed Enter without
       pre-confirming submission — so reporting it delivered closed an outbox row on an
       unconfirmed delivery. This module and
       ``...f_130_handoff_routing.application.delivery_outcome_gate.delivery_was_positive``
       (which has always refused ``queue_enter``) were precisely the two divergent answers
       #14232 j#84877 recorded, and the issue's Non-goals prohibit reading a timed-out landing
       as optimistically delivered. Both production callback senders (``callback_send_port`` /
       ``callback_sweep``) send with ``--mode standard``, on which ``queue_enter`` is
       unreachable, so no production callback changes disposition; where it *is* reachable the
       row now becomes ``uncertain`` (retry 0, operator-visible) instead of silently closed.
    2. ``reader_upgrade_required`` and ``execution_root_outside_target_repo`` are now
       :data:`SEND_NOT_SENT`. Both are documented zero-send refusals ("Refused pre-send (zero
       bytes typed)"), so a bounded retry cannot duplicate; previously they fell to uncertain
       and were therefore never retried at all.

    **``injection_stage`` is the producer's own answer** (Redmine #14232 review j#95333 F1).
    The sending CLI classifies with the whole outcome — mode and turn-start telemetry included
    — and carries the result in its structured JSON, so a reader that parsed it should NOT
    re-derive from the two wire tokens: ``sent`` / ``ok`` alone cannot distinguish a verified
    standard submission from a marker-observed ``queue-enter`` send that never started a turn.
    When it is supplied and recognised it wins; otherwise this falls back to the two-token
    classification, which keeps a legacy / unparseable payload working and still fails closed.
    """
    carried = str(injection_stage or "").strip()
    if carried in _SEND_OUTCOME_BY_STAGE:
        return _SEND_OUTCOME_BY_STAGE[carried]
    return _SEND_OUTCOME_BY_STAGE[injection_stage_for(status, reason)]


#: The fixed token an unrecognized zero-send reason normalizes to (Redmine #14082 review F2). A raw
#: reason that is not a member of :data:`ZERO_SEND_REASON_ALLOWLIST` is REPLACED by this token — the
#: raw value is dropped, never persisted — so a durable zero-send diagnostic can never carry a path,
#: credential, or prose leaked into a reason string.
UNRECOGNIZED_ZERO_SEND_REASON = "unrecognized_zero_send_reason"

#: The closed allowlist of machine tokens a zero-send reason may carry into a durable diagnostic
#: (Redmine #14082 review F2). Persisting a zero-send reason to the outbox row / dead-letter must be
#: secret-safe *by construction*, not by convention: only these known tokens survive; anything else
#: normalizes to :data:`UNRECOGNIZED_ZERO_SEND_REASON`. The set unions every closed vocabulary the
#: background_service callback send can produce a reason from — the handoff outcome reasons here plus
#: the background_service authorization / round-fence / transport-exception tokens defined in the
#: application layer. Those application-layer tokens are enumerated as literals (a domain module must
#: not import the application), and a drift-guard test asserts they still match their definitions
#: (``FAIL_CLOSED_REASONS`` / ``ROUND_STALE`` / ``ROUND_UNVERIFIABLE``), so a renamed token is caught.
#:
#: Redmine #14232: the handoff-side half is now **derived** from the shared injection-stage
#: partitions rather than re-listed here. The old literal list of "transport-side ambiguous /
#: exception outcomes" was a hand-maintained duplicate of
#: :data:`...injection_stage.POST_INJECTION_BLOCKED_REASONS`, so a new handoff reason had to be
#: remembered in two places to stay allowlisted — and a forgotten one silently normalized a real
#: diagnostic to ``unrecognized_zero_send_reason``. Deriving it keeps this allowlist complete over
#: the whole ``Reason`` wire vocabulary by construction. Only the tokens that genuinely originate
#: OUTSIDE the handoff outcome (background_service authorization / round-fence / sender-env) stay
#: literal, because a domain module must not import the application layer.
ZERO_SEND_REASON_ALLOWLIST = frozenset(
    _DELIVERED_SENT_REASONS
    | PRE_INJECTION_BLOCKED_REASONS
    | POST_INJECTION_BLOCKED_REASONS
    | {
        # background_service authorization fail-closed reasons (domain
        # ``background_service_delivery.FAIL_CLOSED_REASONS``; drift-guarded).
        "no_workspace_lease",
        "no_outbox_claim",
        "foreign_workspace",
        "no_target_resolved",
        "ambiguous_target",
        "anchor_mismatch",
        "target_tuple_mismatch",
        "generation_mismatch",
        # action-time review-round fence dispositions (application
        # ``background_service_sender.ROUND_STALE`` / ``ROUND_UNVERIFIABLE``; drift-guarded).
        "review_round_stale",
        "review_round_unverifiable",
        # Not a handoff outcome reason: the background_service's own pre-send sender-identity
        # refusal, so it stays literal alongside the other application-layer tokens.
        "missing_sender_env",
    }
)


def normalize_zero_send_reason(reason: str) -> str:
    """Normalize a zero-send reason to a secret-safe closed token (Redmine #14082 review F2).

    Returns ``reason`` unchanged when it is a known member of :data:`ZERO_SEND_REASON_ALLOWLIST`, a
    blank string for a blank / ``None`` reason (the caller then keeps its own default detail), and
    :data:`UNRECOGNIZED_ZERO_SEND_REASON` for anything else — the raw value is DROPPED, never returned.
    This is the enforcement (not just the convention) that a durable zero-send diagnostic can never
    carry a path / credential / prose that leaked into a reason string.
    """
    r = str(reason or "").strip()
    if not r:
        return ""
    return r if r in ZERO_SEND_REASON_ALLOWLIST else UNRECOGNIZED_ZERO_SEND_REASON


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
    "REVIEW_ROUND_CURRENT",
    "REVIEW_ROUND_STALE",
    "REVIEW_ROUND_UNVERIFIABLE",
    "REVIEW_ROUND_DISPOSITIONS",
    "CallbackSendResult",
    "normalize_send_result",
    "send_outcome_for_delivery",
    "UNRECOGNIZED_ZERO_SEND_REASON",
    "ZERO_SEND_REASON_ALLOWLIST",
    "normalize_zero_send_reason",
    "normalize_gate_name",
    "CallbackClassification",
    "classify_callback_gate",
)
