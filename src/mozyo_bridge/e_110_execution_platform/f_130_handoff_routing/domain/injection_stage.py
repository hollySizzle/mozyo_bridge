"""The single injection-stage authority over a handoff delivery outcome (Redmine #14232).

#14232 j#84877 recorded that the same question — *did the payload reach the receiver, and
may a retry duplicate it?* — was being answered by three different places that disagreed:

- the ``handoff q-enter`` front door claimed ``dispatched`` from its **pre-transport** plan;
- ``delivery_outcome_gate.delivery_was_positive`` (Redmine #13583) answered "positively
  delivered" as ``sent`` + ``ok`` only;
- the callback / outbox retry authority
  (``...f_140_delegated_coordinator_nested_handoff.domain.callback_delivery
  .send_outcome_for_delivery``) answered it with its own private reason table, which
  additionally read ``sent`` + ``queue_enter`` (landing never observed) as *delivered* and
  read two documented zero-send refusals as *uncertain*.

Divergent answers to one question are how an unknown partial delivery gets either silently
closed as delivered or blind-retried into a duplicate. This module is that one answer.

The vocabulary (j#84877 required correction 2) is a **three-token closed set** over exactly
one question: **may a blind retry duplicate the payload?**

- :data:`STAGE_NOT_SENT` — the send was refused *before* any injection: zero bytes typed, no
  Enter, no ACK, nothing for the receiver to have half-received. A retry cannot duplicate, so
  a bounded retry is safe.
- :data:`STAGE_UNCERTAIN_PARTIAL` — the **no-blind-retry** class: text and/or Enter may have
  reached the receiver and submission is not confirmed. This deliberately covers both a
  *known* partial (``pending_input`` parks the body in the composer on purpose; a
  ``marker_timeout`` typed the body and could not verify the rollback cleared it) and an
  *unknown* partial (a transport primitive timed out mid-injection, an event wait expired
  after body+Enter). The distinguishing fact stays on the transport ``(status, reason)`` and
  the additive telemetry; this axis answers only the retry question, and for every member the
  answer is the same: **do not blind-retry**.
- :data:`STAGE_SUBMITTED_CONFIRMED` — the payload was submitted and the submission was
  positively confirmed. Identical to the #13583 ``delivery_was_positive`` predicate by
  construction (``sent`` + ``ok``), which is why that gate now reads this module.

Fail-closed and exhaustive by construction
------------------------------------------
The blocked-reason partition is an **exhaustive** split of the handoff ``Reason`` wire
vocabulary into :data:`PRE_INJECTION_BLOCKED_REASONS` and
:data:`POST_INJECTION_BLOCKED_REASONS`. A drift-guard test compares the union against
``typing.get_args(Reason)``, so a newly added reason cannot be *forgotten* into a bucket by
default — it fails the guard until it is classified deliberately. Anything genuinely
unrecognised at runtime (a novel status, an out-of-vocabulary reason) resolves to
:data:`STAGE_UNCERTAIN_PARTIAL`: the safe direction is always "do not blind-retry".

Every value is a fixed lower-snake-case token or a bool, so the whole projection is safe
verbatim in a pasteable durable record and the opt-in persisted note.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (
    MODE_QUEUE_ENTER,
)

# --- The closed injection-stage vocabulary (j#84877 required correction 2). ---------
STAGE_NOT_SENT = "not_sent"
STAGE_UNCERTAIN_PARTIAL = "uncertain_partial"
STAGE_SUBMITTED_CONFIRMED = "submitted_confirmed"

INJECTION_STAGES: tuple[str, ...] = (
    STAGE_NOT_SENT,
    STAGE_UNCERTAIN_PARTIAL,
    STAGE_SUBMITTED_CONFIRMED,
)

# --- The two non-blocked reasons, kept explicit so the drift guard covers them. ------
#: The reason a positively-confirmed submission carries (``sent``) and the reason a
#: deliberate composer park carries (``pending_input``) — the same token on two statuses,
#: which is why the mapping keys on ``(status, reason)`` and not on the reason alone.
REASON_OK = "ok"
#: The relaxed ``queue-enter`` rail's marker-unobserved reason: the body was typed and Enter
#: was pressed, but the sender never pre-confirmed landing.
REASON_QUEUE_ENTER = "queue_enter"

NON_BLOCKED_REASONS: frozenset[str] = frozenset({REASON_OK, REASON_QUEUE_ENTER})

# --- Blocked reasons that are DETERMINISTIC pre-injection refusals (zero bytes typed). -
# Each member's own durable contract states that nothing was typed and Enter was never
# pressed, so a bounded retry cannot duplicate. The docstring / narrative that establishes
# each is named so an auditor can replay the classification without re-deriving it:
#
#   target_unavailable                     "no notification was typed" (also the fail-closed
#                                          token for an unreadable pre-send receiver read)
#   target_not_agent                       "no notification was typed"
#   invalid_anchor / invalid_args          aborted before resolving the receiver pane
#   precondition_not_idle                  the herdr rail refused to inject (#13255)
#   receiver_startup_interaction_required  refused at the pre-send admission gate (#13760)
#   cross_session_claude                   gate refusal, "No notification was typed"
#   target_repo_mismatch                   "aborted before typing"
#   gateway_route_blocked                  governed route refusal, pre-send
#   main_lane_implementation_blocked       "aborted before typing"
#   reader_upgrade_required                route refused pre-send (#13844)
#   execution_root_outside_target_repo     "Refused pre-send (zero bytes typed)" (#14249)
#
# Redmine #14232: ``reader_upgrade_required`` and ``execution_root_outside_target_repo`` were
# absent from the callback authority's private table even though both are documented
# zero-send refusals, so both fell to *uncertain* and were never bounded-retried. Classifying
# them here — once, exhaustively — is what stops that class of omission recurring.
PRE_INJECTION_BLOCKED_REASONS: frozenset[str] = frozenset(
    {
        "target_unavailable",
        "target_not_agent",
        "invalid_anchor",
        "invalid_args",
        "precondition_not_idle",
        "receiver_startup_interaction_required",
        "cross_session_claude",
        "target_repo_mismatch",
        "gateway_route_blocked",
        "main_lane_implementation_blocked",
        "reader_upgrade_required",
        "execution_root_outside_target_repo",
    }
)

# --- Blocked reasons where text and/or Enter may already be on the receiver. ----------
# ``marker_timeout``: the body was typed once and a C-u rollback was issued, but the rail
# cannot verify from a capture that the composer cleared. ``turn_start_unconfirmed`` /
# ``receiver_blocked`` / ``turn_start_absent``: post-injection rail outcomes
# (``TurnStartResult.delivered is True``). ``inject_failed`` / ``transport_error``: a
# transport primitive failed at or after the injection step, so the payload's fate is
# unknown. Every member is no-blind-retry.
POST_INJECTION_BLOCKED_REASONS: frozenset[str] = frozenset(
    {
        "marker_timeout",
        "turn_start_unconfirmed",
        "receiver_blocked",
        "turn_start_absent",
        "inject_failed",
        "transport_error",
    }
)

#: The blocked reason the high-level handoff boundary emits when a transport primitive
#: (``send_text`` / ``send_keys`` / ``read_pane``) raises out of the rail — the #14232
#: defect: that exception used to escape as an uncaught traceback with no structured
#: outcome. Post-injection by classification (see above): the rail only reaches a transport
#: primitive at or after the single body injection.
REASON_TRANSPORT_ERROR = "transport_error"

# --- Reason wording owned here: the transport-failure family (Redmine #14232) --------
# ``handoff.next_action_for`` / ``handoff._outcome_narrative`` /
# ``handoff._receiver_contract_line`` resolve the prose for the two transport-failure reasons
# from here, beside the reason tokens and their classification, rather than growing the
# already-oversized ``handoff.py`` with inline prose. This is the ``gateway_route_wording``
# sibling precedent (#14249): a module sitting at its module-health baseline takes new prose in
# a sibling instead of a self-approved baseline bump. ``handoff.py`` imports this module for the
# stage telemetry anyway and this module imports nothing from it, so there is no cycle.
#
# The family is ``inject_failed`` — the herdr *event* rail's own structured refusal, which never
# raises; its wording was RELOCATED here move-only by #14232, unchanged apart from templating
# ``{receiver}`` — and ``transport_error``, #14232's additive reason for a primitive that raised
# out of the tmux-shaped rail. Keeping both here gives the family one home.
#
# Redaction-safe: fixed prose only. The adapter's own message — which can carry a binary path or
# a raw herdr status, and a delivery record is pasteable into a durable journal — is NEVER
# folded into the structured outcome (the #13760 j#77947 invariant-3 posture).

#: ``DeliveryOutcome.next_action`` for an ``inject_failed`` outcome (Redmine #13255).
INJECT_FAILED_NEXT_ACTION: str = (
    "the herdr transport failed to inject the notification (a "
    "send_text / send_keys primitive returned an error); the armed wait "
    "was cancelled and nothing was confirmed delivered. Check the herdr "
    "binary / session for {receiver}, then re-issue the send."
)

#: ``DeliveryOutcome`` narrative for an ``inject_failed`` outcome (Redmine #13255).
INJECT_FAILED_NARRATIVE: str = (
    "herdr turn-start rail (--mode standard): a transport primitive "
    "(send_text / send_keys) failed mid-injection; the armed wait was "
    "cancelled and the send fails closed. Nothing was confirmed delivered."
)

#: ``DeliveryOutcome.next_action`` for a ``transport_error`` outcome.
TRANSPORT_ERROR_NEXT_ACTION: str = (
    "the terminal transport raised while the send was in flight (a send_text / send_keys / "
    "read_pane primitive failed or timed out), so the delivery is NOT confirmed and the "
    "payload's fate is unknown. Read the receiver (`mozyo-bridge read <receiver>`) or the "
    "durable anchor to establish whether the turn started, and check the transport backend "
    "for that receiver (`mozyo-bridge agents list`, `mozyo-bridge doctor`). Do NOT blind-"
    "resend, do not hand-type the body, and do not send raw keys: the marker+body was typed "
    "at most once and a resend can duplicate it."
)

#: ``DeliveryOutcome`` narrative for a ``transport_error`` outcome.
TRANSPORT_ERROR_NARRATIVE: str = (
    "Terminal-transport failure at the high-level handoff boundary (Redmine #14232): a "
    "transport primitive raised at or after the single body injection, so the rail could not "
    "drive the send to a confirmed disposition. The failure is reported as this typed outcome "
    "instead of an uncaught traceback: the sender gets a structured status / reason / "
    "next_action and can classify the delivery. No C-u rollback and no re-send were issued — "
    "the marker+body was typed at most once. Partial reach of the body and/or Enter cannot be "
    "excluded, so this is an uncertain delivery, never an optimistic one."
)

#: ``DeliveryOutcome`` receiver-side contract line for a ``transport_error`` outcome.
TRANSPORT_ERROR_RECEIVER_CONTRACT: str = (
    "Receiver-side contract: {receiver} must read the durable anchor manually if action is "
    "still required; the transport failed mid-send and neither submission nor a turn start "
    "was confirmed."
)

_STATUS_SENT = "sent"
_STATUS_PENDING_INPUT = "pending_input"
_STATUS_BLOCKED = "blocked"

#: The herdr event rail's confirmed-start outcome token (``...turn_start_rail.OUTCOME_STARTED``).
#: Kept as a literal so this module stays a leaf over the adapter package.
_TURN_START_OUTCOME_STARTED = "started"

#: The armed working-transition wait result meaning the transition was actually observed
#: (``...turn_start_rail.WAIT_CHANGED``). On the queue-enter rail this is the ONLY causally
#: attributable start signal, and the rail publishes it only under a coherent generation.
_WAIT_KIND_CHANGED = "changed"


#: The queue-enter observation schema version the rail stamps when — and only when — it
#: publishes the generation-coherent causal fields (``handoff_tmux_transport_rail``).
_OBSERVATION_VERSION_V2 = 2

#: Every field the canonical gateway-binding producer emits
#: (``observe_queue_enter_gateway_binding``). All must be present and ``str``.
_BINDING_FIELDS: tuple[str, ...] = (
    "provider",
    "assigned_name",
    "locator",
    "row_revision",
    "attestation_observed_at",
    "startup_action_id",
)

#: The producer's ONE optional-empty field: ``row_revision`` is ``_norm(str(revision))`` and
#: collapses to ``""`` for a bool row value, so an empty string is a legitimate canonical
#: binding. Every other field is guaranteed non-empty at the producer — it returns ``None``
#: outright when the attestation timestamp or the launch-generation token is missing, and the
#: remaining three are built from an identity that already decoded and matched the target.
_BINDING_OPTIONAL_EMPTY: frozenset[str] = frozenset({"row_revision"})


def canonical_v2_generation_binding(observation: object) -> bool:
    """True iff ``observation`` carries a canonical v2 generation binding (pure, fail-closed).

    Redmine #14232 review j#95827. The previous check read only the *truthiness* of
    ``gateway_binding``, so a string, a list, an int, a partial dict, a dict with an empty
    required field, and a full dict on a legacy/absent ``observation_version`` all passed —
    every one of them promoted to a confirmed delivery, and from there into the front door's
    ``dispatched``, ``delivery_was_positive``, and the callback completion. #14203 j#87418 had
    already ruled that the mere non-emptiness of a binding field is **not** a generation
    authority; this restores that ruling on the reading side.

    The check is a *shape* gate, deliberately not an identity re-verification: the producer
    already builds the binding from a ``verdict=present`` attestation whose workspace / lane /
    role / assigned-name / locator all match the live inventory row and this target, so
    re-resolving the live target here would duplicate that work in every consumer (which the
    review explicitly does not ask for). What this adds is the guarantee that the record in
    hand actually came from that producer.

    Unknown extra keys are **allowed**. Rejecting them would mean an additive field in a future
    #14203 observation silently demoted every delivery to unconfirmed — a fail-closed rule that
    fails in the more damaging direction, since it would look like a transport regression
    rather than a schema change.
    """
    if not isinstance(observation, dict):
        return False
    if observation.get("observation_version") != _OBSERVATION_VERSION_V2:
        return False
    binding = observation.get("gateway_binding")
    if not isinstance(binding, dict):
        return False
    for field in _BINDING_FIELDS:
        value = binding.get(field)
        if not isinstance(value, str):
            return False
        if not value and field not in _BINDING_OPTIONAL_EMPTY:
            return False
    return True


def turn_start_positively_observed(
    queue_enter_turn_start_observation: object = None,
    turn_start_outcome: object = None,
) -> bool:
    """True only on **causally attributable** evidence that the receiver began a turn (pure).

    Redmine #14232 review j#95601 finding 1. Only an *armed wait* counts — an observation that
    was set up **before** this send's Enter and then fired, so the start it saw belongs to
    THIS send. Exactly two such signals exist:

    - the herdr **event** rail's armed ``wait agent-status --status working`` transition
      (``turn_start_outcome.outcome == "started"``), used by ``--mode standard``;
    - the queue-enter rail's own armed working-transition wait (Redmine #14203), surfaced on
      the v2 observation as ``event_wait_kind == "changed"`` **together with** a
      ``gateway_binding``. The rail writes those two fields only when the pre-arm and
      post-collect gateway generations are present and exactly equal
      (``handoff_tmux_transport_rail`` — "a None / mismatched pair drops BOTH"), so their
      presence *is* the generation-coherence guarantee: the receiver process did not change
      across the observation window, and an old process's start can never pair with a new
      process's binding.

    **The post-choreography ``runtime_state`` snapshot is NOT evidence of a start**, in either
    direction of this predicate. Review j#95601 established that reading ``busy`` as a
    confirmation violates the source contract of the field itself
    (``DeliveryOutcome.queue_enter_turn_start_observation``: *"a post-hoc snapshot does not
    prove causality the way an armed ``wait agent-status`` transition does, so it must not be
    read as an event-observed turn start"*). A post-hoc poll cannot attribute what it sees to
    this send: the queue-enter rail runs no idle precondition gate, so a receiver that was
    already busy before the send — or a recycled process running someone else's turn — reads
    ``busy`` just the same. Redmine #14232 R2 made exactly that mistake and thereby
    reintroduced, in the ``busy`` cell, the false confirmation j#95333 had just removed.

    Conversely, when a causal signal IS present the snapshot state does not override it. A fast
    turn that has already finished reads ``turn_ended``, and an idle-again receiver reads
    ``awaiting_input``; neither retracts an armed wait that fired. The snapshot's
    ``awaiting_input`` remains meaningful only in the ABSENCE of a causal signal, where the
    observation module documents it as *"delivered, but a turn start was not observed"* — that
    is evidence against a start, which is why nothing here promotes it.

    Fail-closed by construction: the default answer is ``False`` — no observation at all (the
    tmux backend runs none), an incoherent generation (the rail dropped both fields), a
    ``timeout`` / ``absent`` wait, or an unrecognised shape all return ``False``.
    """
    if isinstance(turn_start_outcome, dict):
        if str(turn_start_outcome.get("outcome") or "") == _TURN_START_OUTCOME_STARTED:
            return True
    if isinstance(queue_enter_turn_start_observation, dict):
        observation = queue_enter_turn_start_observation
        armed_wait_fired = (
            str(observation.get("event_wait_kind") or "") == _WAIT_KIND_CHANGED
        )
        # Required alongside it, not implied: the rail writes the two together only under a
        # coherent generation, so a record carrying one without the other did not come from
        # that gate and must not be trusted as if it had. And the binding must be the
        # canonical v2 shape — its mere presence is NOT a generation authority (#14203 j#87418).
        if armed_wait_fired and canonical_v2_generation_binding(observation):
            return True
    return False


def injection_stage_for(
    status: object,
    reason: object,
    *,
    mode: object = None,
    queue_enter_turn_start_observation: object = None,
    turn_start_outcome: object = None,
) -> str:
    """Classify a handoff delivery outcome into one :data:`INJECTION_STAGES` token (pure).

    - ``sent`` + ``ok`` -> :data:`STAGE_SUBMITTED_CONFIRMED`, **except** on the relaxed
      ``queue-enter`` rail without positive turn-start evidence (see below);
    - ``sent`` + ``queue_enter`` -> :data:`STAGE_UNCERTAIN_PARTIAL` (Enter was pressed but
      landing was never pre-confirmed, so submission is unverified);
    - ``pending_input`` -> :data:`STAGE_UNCERTAIN_PARTIAL` (the body is parked in the
      composer on purpose; a resend would duplicate it);
    - ``blocked`` + a :data:`PRE_INJECTION_BLOCKED_REASONS` member -> :data:`STAGE_NOT_SENT`;
    - everything else, including any unrecognised status / reason ->
      :data:`STAGE_UNCERTAIN_PARTIAL` (fail-closed: never blind-retry what you cannot
      classify).

    **The queue-enter carve-out (Redmine #14232 review j#95333 finding 1).** ``sent`` + ``ok``
    means different things on different rails, so the ``(status, reason)`` pair alone cannot
    answer this module's question:

    - on ``--mode standard`` the rail *verified a turn start* before resolving to ``ok`` (the
      capture-based observation, or the herdr event rail's ``started``), so it is a genuine
      confirmed submission;
    - on the daily-default ``queue-enter`` rail ``ok`` only means **the landing marker was
      observed and Enter was pressed** — that rail deliberately runs no turn-start gate
      (its snapshot is additive telemetry). A receiver whose composer took the text but whose
      Enter was absorbed reports exactly this outcome.

    Reading ``ok`` as confirmed on queue-enter was therefore the same optimistic
    delivered-ization the issue's Non-goals prohibit, in a second place: it claimed a
    confirmed submission while the outcome's own telemetry said ``awaiting_input`` ("delivered,
    but a turn start was not observed"). A queue-enter ``ok`` is now confirmed only with
    positive evidence from :func:`turn_start_positively_observed`.

    ``mode`` / ``queue_enter_turn_start_observation`` / ``turn_start_outcome`` are optional
    because a reader may hold nothing but the two wire tokens. **Every caller that can see the
    outcome MUST pass them** — :func:`injection_stage_telemetry` (and therefore
    ``make_outcome``, which runs it on every terminal path) always does, so the authoritative
    classification is computed once, with full context, and carried on the outcome for
    consumers to read rather than re-derive (:func:`stage_from_telemetry`). An unset ``mode``
    cannot apply the carve-out and keeps the pre-carve-out reading: demoting on unknown mode
    would wrongly downgrade every genuinely-confirmed standard send a two-token reader sees.
    """
    status_s = str(status or "").strip()
    reason_s = str(reason or "").strip()
    if status_s == _STATUS_SENT and reason_s == REASON_OK:
        if str(mode or "").strip() == MODE_QUEUE_ENTER and not (
            turn_start_positively_observed(
                queue_enter_turn_start_observation, turn_start_outcome
            )
        ):
            return STAGE_UNCERTAIN_PARTIAL
        return STAGE_SUBMITTED_CONFIRMED
    if status_s == _STATUS_BLOCKED and reason_s in PRE_INJECTION_BLOCKED_REASONS:
        return STAGE_NOT_SENT
    return STAGE_UNCERTAIN_PARTIAL


def injection_stage_for_outcome(outcome: object) -> str:
    """The stage for a whole :class:`...handoff.DeliveryOutcome` (pure, fail-closed).

    Prefers the stage the producer already derived and carried on the outcome
    (``make_outcome`` computes it with full context), and falls back to re-deriving from the
    outcome's own fields when it carries none — a hand-built or legacy outcome. ``None``
    (no outcome at all: an early return, or a caller that never sent) is
    :data:`STAGE_UNCERTAIN_PARTIAL`, because a delivery you cannot see is not one you may
    claim.
    """
    if outcome is None:
        return STAGE_UNCERTAIN_PARTIAL
    carried = stage_from_telemetry(getattr(outcome, "injection_stage", None))
    if carried is not None:
        return carried
    return injection_stage_for(
        getattr(outcome, "status", None),
        getattr(outcome, "reason", None),
        mode=getattr(outcome, "mode", None),
        queue_enter_turn_start_observation=getattr(
            outcome, "queue_enter_turn_start_observation", None
        ),
        turn_start_outcome=getattr(outcome, "turn_start_outcome", None),
    )


def blind_retry_prohibited(stage: object) -> bool:
    """True when re-issuing the same send may duplicate the payload (pure).

    Only :data:`STAGE_NOT_SENT` permits a blind retry (nothing reached the receiver). A
    :data:`STAGE_SUBMITTED_CONFIRMED` send needs no retry at all, and re-issuing it *would*
    duplicate, so it is prohibited too — the predicate answers "may I resend without
    re-reading the receiver?", not "is there work left".
    """
    return str(stage or "").strip() != STAGE_NOT_SENT


#: The fixed, redaction-safe guidance per stage. Free-text-free at the call site (the caller
#: never interpolates operator input), so it is safe verbatim in a durable record.
_STAGE_GUIDANCE: dict = {
    STAGE_NOT_SENT: (
        "nothing was typed at the receiver and no Enter was pressed, so re-issuing the "
        "SAME durable anchor through the same high-level command cannot duplicate: fix the "
        "named refusal, then retry."
    ),
    STAGE_UNCERTAIN_PARTIAL: (
        "the body and/or Enter may already be at the receiver and submission is NOT "
        "confirmed, so a blind retry can duplicate the delivery. Read the receiver "
        "(`mozyo-bridge read <receiver>`) or the durable anchor to establish whether the "
        "turn started before re-issuing; do not hand-type the body and do not send raw keys."
    ),
    STAGE_SUBMITTED_CONFIRMED: (
        "the payload was submitted and the submission was confirmed; no retry is needed and "
        "re-issuing would duplicate. Delivery is an ACK, not task completion — the receiver "
        "reads the durable anchor and decides."
    ),
}


def stage_guidance(stage: object) -> str:
    """The fixed next-action guidance for a stage (pure; empty for an unknown token)."""
    return _STAGE_GUIDANCE.get(str(stage or "").strip(), "")


def injection_stage_telemetry(
    status: object,
    reason: object,
    *,
    mode: object = None,
    queue_enter_turn_start_observation: object = None,
    turn_start_outcome: object = None,
) -> dict[str, Any]:
    """The machine-readable injection-stage projection carried on a delivery outcome (pure).

    Tokens + a bool + a fixed guidance string only — no free text, no absolute paths, no raw
    adapter output — so it is durable-record safe verbatim. Carried on **every**
    :class:`...domain.handoff.DeliveryOutcome` (derived in ``make_outcome``) so no terminal
    path can forget it, which is the same "publication is a property of emitting" posture the
    #13583 delivery-outcome gate adopted after hand-picked publish sites were missed.

    Redmine #14232 review j#95333 finding 1: ``make_outcome`` passes the mode and whichever
    turn-start telemetry the rail produced, so **this is the one call that always classifies
    with full context**. Consumers then read the carried result instead of re-deriving from
    the two wire tokens (which cannot resolve the queue-enter carve-out).
    """
    stage = injection_stage_for(
        status,
        reason,
        mode=mode,
        queue_enter_turn_start_observation=queue_enter_turn_start_observation,
        turn_start_outcome=turn_start_outcome,
    )
    return {
        "stage": stage,
        "blind_retry_prohibited": blind_retry_prohibited(stage),
        "next_action": stage_guidance(stage),
    }


def stage_from_telemetry(telemetry: object) -> Optional[str]:
    """Read the stage token back off an :func:`injection_stage_telemetry` mapping (pure).

    ``None`` when the mapping is absent or carries no recognised stage, so a reader that
    receives a legacy / hand-built outcome falls back to its own classification rather than
    trusting a missing field.
    """
    if isinstance(telemetry, dict):
        token = telemetry.get("stage")
        if isinstance(token, str) and token in INJECTION_STAGES:
            return token
    return None


def injection_stage_record_lines(telemetry: object) -> list[str]:
    """Render the additive ``- Injection stage:`` durable-record block (pure).

    Follows the #13166 / #13255 / #12705 telemetry-line precedent: fixed tokens + a fixed
    verdict phrase, never overriding the outcome's own ``next_action``. Empty list when the
    telemetry is absent / unrecognised, so a legacy record renders byte-identically.
    """
    stage = stage_from_telemetry(telemetry)
    if stage is None:
        return []
    verdict = (
        "blind retry PROHIBITED"
        if blind_retry_prohibited(stage)
        else "retry safe (nothing reached the receiver)"
    )
    return [
        f"- Injection stage: `{stage}` — {verdict}. {stage_guidance(stage)}",
    ]


__all__: Iterable[str] = (
    "canonical_v2_generation_binding",
    "INJECT_FAILED_NARRATIVE",
    "INJECT_FAILED_NEXT_ACTION",
    "INJECTION_STAGES",
    "NON_BLOCKED_REASONS",
    "POST_INJECTION_BLOCKED_REASONS",
    "PRE_INJECTION_BLOCKED_REASONS",
    "REASON_OK",
    "REASON_QUEUE_ENTER",
    "REASON_TRANSPORT_ERROR",
    "TRANSPORT_ERROR_NARRATIVE",
    "TRANSPORT_ERROR_NEXT_ACTION",
    "TRANSPORT_ERROR_RECEIVER_CONTRACT",
    "STAGE_NOT_SENT",
    "STAGE_SUBMITTED_CONFIRMED",
    "STAGE_UNCERTAIN_PARTIAL",
    "blind_retry_prohibited",
    "injection_stage_for",
    "injection_stage_for_outcome",
    "injection_stage_record_lines",
    "injection_stage_telemetry",
    "stage_from_telemetry",
    "stage_guidance",
    "turn_start_positively_observed",
)
