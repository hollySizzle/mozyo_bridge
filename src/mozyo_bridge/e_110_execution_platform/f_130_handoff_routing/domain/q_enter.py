"""LLM-facing q-enter / submit-complete front-door primitive (Redmine #12705).

`#12705 LLM-facing q-enter / submit-complete primitive` (a bug under
`#12670 workflow lane ownership / transition function registry`) surfaced from the
GK3500 exploratory smoke `#12698`: a receiver that produced a structured
``hands_off_consultation_result`` had to hand-roll the whole delivery rail itself
— deciding whether ``handoff reply`` would fail closed without a Redmine anchor,
whether to fall back to the low-level ``mozyo-bridge message`` transport, whether
a read marker / landing marker was needed, whether a rollback happened, whether to
retry, and whether raw ``keys Enter`` was allowed. That is judgment load and token
cost the CLI should own.

This module is the pure, fail-closed brain of the **single LLM-facing submit
primitive**. The LLM names a high-level *intent* (what it wants to submit); this
module resolves which delivery rail carries it and whether a ticket anchor is
required, classifies the post-delivery composer residue into one unambiguous
state, and derives a deterministic delivery id for duplicate prevention. It owns
NO I/O: the actual target admission, repo/project/role identity gates, landing
marker, Enter-only retry, and C-u rollback stay in the existing
``orchestrate_handoff`` rail. The front-door handler resolves a :class:`SubmitPlan`
here, then delegates the choreography to that rail unchanged.

Design boundaries (Redmine #12705 description / j#67153 / j#67157):

- It is NOT a raw ``keys Enter`` alias. The intent resolves to one of the existing
  product rails (anchored ``handoff send`` / anchored ``handoff reply`` /
  ``#12703 ticketless no-anchor callback transport``); the front-door never types
  a key itself.
- The Redmine-governed worker-dispatch anchor requirement is preserved: a
  ``worker_dispatch`` / ``reply`` intent that lacks a ticket anchor fails closed
  here with a :class:`SubmitPlanError` that names exactly what to provide (or to
  switch to ``consultation_callback`` when there is genuinely no anchor). The
  ticketless ``consultation_callback`` intent rides the no-anchor rail and never
  fabricates an anchor, staying compatible with the
  ``#12703 ticketless no-anchor callback transport`` boundary.
- The transport outcome (status / reason / marker, owned by ``DeliveryOutcome``)
  stays separated from the workflow / front-door result (the :class:`SubmitOutcome`
  this module builds): a delivery that physically landed but whose workflow intent
  was anchor-required is two distinct facts.
- Every field is a fixed lower-snake-case token / bool / deterministic id with no
  operator free text, so the whole front-door result is durable-record safe.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (
    MODE_QUEUE_ENTER,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    POST_INJECTION_BLOCKED_REASONS,
    STAGE_SUBMITTED_CONFIRMED,
    blind_retry_prohibited,
    injection_stage_for_outcome,
    stage_guidance,
    turn_start_positively_observed,
)

#: The transport status a deliberate ``--mode pending`` park reports. Named here because the
#: front door treats it as "did what you asked", not as a block (review j#95333 F2).
STATUS_PENDING_INPUT = "pending_input"


class SubmitPlanError(ValueError):
    """A submit intent could not be resolved to a safe delivery rail."""


# --- Submit intent tokens — the high-level thing the LLM wants to submit. -----
# These are deliberately coarse so the LLM picks an *intent*, not a rail: the
# rail + anchor requirement are derived below, which is the whole point (the LLM
# stops reasoning about send-vs-reply-vs-ticketless and anchor fail-closed rules).
INTENT_WORKER_DISPATCH = "worker_dispatch"
INTENT_REPLY = "reply"
INTENT_CONSULTATION_CALLBACK = "consultation_callback"

SUBMIT_INTENTS: tuple[str, ...] = (
    INTENT_WORKER_DISPATCH,
    INTENT_REPLY,
    INTENT_CONSULTATION_CALLBACK,
)

# --- Resolved delivery rails (which existing product rail carries the intent). -
RAIL_ANCHORED_SEND = "anchored_send"
RAIL_ANCHORED_REPLY = "anchored_reply"
RAIL_TICKETLESS_CALLBACK = "ticketless_callback"

# --- Composer-residue classification (Redmine #12705 j#66977). ----------------
# A failed (or unconfirmed) marker observation must produce ONE unambiguous
# composer state so the LLM never has to reason about read-marker refresh, partial
# composer residue, or whether a fresh receiver is required. Each is a pure
# projection of the transport ``(status, reason)`` the existing rail already
# computes — no extra pane probe, so the classification cannot drift from the
# rail's own marker/rollback decision.
RESIDUE_NOT_TYPED = "not_typed"
RESIDUE_TYPED_BUT_PENDING = "typed_but_pending"
RESIDUE_CLEARED = "cleared"
RESIDUE_UNSAFE_REQUIRES_FRESH_RECEIVER = "unsafe_state_requires_fresh_receiver"

COMPOSER_RESIDUE_STATES: tuple[str, ...] = (
    RESIDUE_NOT_TYPED,
    RESIDUE_TYPED_BUT_PENDING,
    RESIDUE_CLEARED,
    RESIDUE_UNSAFE_REQUIRES_FRESH_RECEIVER,
)

# Anchored rails carry a real ticket anchor; the ticketless rail never does.
_ANCHORED_RAILS: frozenset[str] = frozenset(
    {RAIL_ANCHORED_SEND, RAIL_ANCHORED_REPLY}
)

# Source tokens accepted by the anchored rails. Mirrors the ``SOURCES`` set in
# :mod:`...domain.handoff`; kept as literals here so this module stays a leaf
# (``handoff`` imports nothing from here, and the front-door record rendering can
# import this module without a cycle).
_SOURCE_REDMINE = "redmine"
_SOURCE_ASANA = "asana"
_ANCHORED_SOURCES: frozenset[str] = frozenset({_SOURCE_REDMINE, _SOURCE_ASANA})


@dataclass(frozen=True)
class SubmitPlan:
    """The resolved, fail-closed plan for one submit intent.

    Names the rail the intent rides, whether a ticket anchor is required, and the
    default kind / ticketless flag the front-door hands to ``orchestrate_handoff``.
    Built only by :func:`resolve_submit_plan`, which fails closed before a plan is
    ever produced for an under-specified anchored intent.
    """

    intent: str
    rail: str
    anchor_required: bool
    ticketless: bool
    source: Optional[str]
    default_kind: Optional[str]

    def to_structured_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "rail": self.rail,
            "anchor_required": bool(self.anchor_required),
            "ticketless": bool(self.ticketless),
            "source": self.source,
            "default_kind": self.default_kind,
        }


def _has_redmine_anchor(*, source: Optional[str], issue: bool, journal: bool) -> bool:
    return source == _SOURCE_REDMINE and issue and journal


def _has_asana_anchor(
    *, source: Optional[str], task: bool, comment: bool, anchor_url: bool
) -> bool:
    return source == _SOURCE_ASANA and task and (comment or anchor_url)


def _has_ticket_anchor(
    *,
    source: Optional[str],
    issue: bool,
    journal: bool,
    task: bool,
    comment: bool,
    anchor_url: bool,
) -> bool:
    return _has_redmine_anchor(source=source, issue=issue, journal=journal) or (
        _has_asana_anchor(
            source=source, task=task, comment=comment, anchor_url=anchor_url
        )
    )


def resolve_submit_plan(
    intent: object,
    *,
    source: Optional[str] = None,
    issue: bool = False,
    journal: bool = False,
    task: bool = False,
    comment: bool = False,
    anchor_url: bool = False,
    kind: Optional[str] = None,
) -> SubmitPlan:
    """Resolve a submit intent to a fail-closed :class:`SubmitPlan`.

    The boolean flags say only *whether* each anchor field was supplied (the front
    door reads them off ``args``), keeping this module free of CLI / anchor parsing.

    Fails closed (:class:`SubmitPlanError`) on an unknown intent, on a
    ``worker_dispatch`` / ``reply`` intent that lacks a complete ticket anchor (the
    Redmine-governed worker-dispatch anchor requirement is not relaxed), and on a
    ``consultation_callback`` intent that carries ANY anchor field (it rides the
    no-anchor rail and never carries one — a stray anchor is rejected, not silently
    dropped). The error names the missing or stray anchor and points at the right
    intent, so the LLM reads the next action instead of rediscovering
    ``invalid_anchor`` by trial.
    """
    if not isinstance(intent, str) or not intent.strip():
        raise SubmitPlanError(
            f"submit intent must be a non-empty token; got {intent!r}"
        )
    token = intent.strip()
    if token not in SUBMIT_INTENTS:
        raise SubmitPlanError(
            f"unknown submit intent {token!r}; expected one of {list(SUBMIT_INTENTS)}"
        )

    if token == INTENT_CONSULTATION_CALLBACK:
        # The ticketless no-anchor callback rail (#12703). It never carries — and
        # never requires — a ticket anchor; the structured callback fields are the
        # durable record. Fail closed on ANY anchor-like field (not only --source):
        # the ticketless rail would otherwise silently ignore a stray --issue /
        # --journal / --task-id, making the delivery record read as no-anchor while
        # the caller believes it supplied one — exactly the ambiguity this front
        # door is meant to remove (review j#67184). The LLM must read the next
        # action, not have its anchor fields quietly dropped.
        stray = [
            flag
            for flag, present in (
                ("--source", source is not None),
                ("--issue", issue),
                ("--journal", journal),
                ("--task-id", task),
                ("--comment-id", comment),
                ("--anchor-url", anchor_url),
            )
            if present
        ]
        if stray:
            raise SubmitPlanError(
                "consultation_callback rides the ticketless no-anchor callback rail "
                "and carries NO ticket anchor; drop "
                f"{', '.join(stray)}. If you mean to dispatch / reply against a "
                "ticket anchor, use --intent worker_dispatch / reply instead"
            )
        return SubmitPlan(
            intent=token,
            rail=RAIL_TICKETLESS_CALLBACK,
            anchor_required=False,
            ticketless=True,
            source=None,
            default_kind="reply",
        )

    # Anchored intents: a real ticket anchor is mandatory and not relaxed.
    if source not in _ANCHORED_SOURCES:
        raise SubmitPlanError(
            f"--intent {token} is an anchored {('dispatch' if token == INTENT_WORKER_DISPATCH else 'reply')} "
            f"and requires --source {sorted(_ANCHORED_SOURCES)}; if you have no "
            "ticket anchor to return a consultation result, use "
            "--intent consultation_callback (the ticketless no-anchor callback rail)"
        )
    if not _has_ticket_anchor(
        source=source,
        issue=issue,
        journal=journal,
        task=task,
        comment=comment,
        anchor_url=anchor_url,
    ):
        if source == _SOURCE_REDMINE:
            need = "--issue and --journal"
        else:
            need = "--task-id and (--comment-id or --anchor-url)"
        raise SubmitPlanError(
            f"--intent {token} on --source {source} requires a complete ticket "
            f"anchor ({need}); the Redmine-governed worker-dispatch anchor "
            "requirement is not relaxed. If there is genuinely no anchor, use "
            "--intent consultation_callback (the ticketless no-anchor callback rail)"
        )

    if token == INTENT_WORKER_DISPATCH:
        return SubmitPlan(
            intent=token,
            rail=RAIL_ANCHORED_SEND,
            anchor_required=True,
            ticketless=False,
            source=source,
            # `handoff send` requires an explicit --kind; the front door surfaces a
            # clear error if it is missing rather than guessing an intent label.
            default_kind=kind,
        )
    # INTENT_REPLY
    return SubmitPlan(
        intent=token,
        rail=RAIL_ANCHORED_REPLY,
        anchor_required=True,
        ticketless=False,
        source=source,
        default_kind=kind or "reply",
    )


def classify_composer_residue(
    status: object,
    reason: object,
    *,
    mode: object = None,
    queue_enter_turn_start_observation: object = None,
    turn_start_outcome: object = None,
) -> str:
    """Classify the receiver composer residue from the transport outcome.

    A pure projection of the existing transport ``(status, reason)`` into exactly
    one of :data:`COMPOSER_RESIDUE_STATES`, so the LLM reads one unambiguous state
    instead of reasoning about partial composer text:

    - ``sent`` / ``ok`` — landing marker observed, Enter pressed and the input
      submitted -> ``cleared``.
    - ``sent`` / ``queue_enter`` — queue-enter rail, marker not pre-confirmed but
      the body was typed once and Enter (re)pressed; landing is not confirmed but
      the payload was not duplicated -> ``typed_but_pending``.
    - ``pending_input`` — pending/operator rail: the body was typed and Enter
      deliberately not pressed -> ``typed_but_pending``.
    - a ``blocked`` outcome the shared injection-stage authority classifies as
      **post-injection** (:data:`...injection_stage.POST_INJECTION_BLOCKED_REASONS` —
      ``marker_timeout`` / ``turn_start_unconfirmed`` / ``receiver_blocked`` /
      ``turn_start_absent`` / ``inject_failed`` / ``transport_error``) -> the only safe
      read is ``unsafe_state_requires_fresh_receiver``.
    - a ``blocked`` outcome the same authority classifies as **pre-injection** (nothing
      was typed: ``invalid_anchor`` / ``invalid_args`` / ``target_*`` /
      ``cross_session_claude`` / ``precondition_not_idle`` /
      ``receiver_startup_interaction_required`` / …) -> ``not_typed``.

    Redmine #14232 (j#84877 required correction 2): this branch used to special-case
    ``marker_timeout`` alone and fold **every other** ``blocked`` onto ``not_typed``. That
    misread the whole post-injection family as "nothing reached the receiver" — most
    visibly the herdr ``delivered_not_started`` projection (``blocked`` /
    ``turn_start_unconfirmed``), where body **and** Enter were injected before the event
    wait timed out, and the new ``transport_error``, where a primitive raised mid-send.
    Deriving the split from the shared authority instead of a local literal is what keeps
    the residue read and the retry decision from disagreeing again: a residue of
    ``not_typed`` invites exactly the blind retry the injection stage prohibits.
    """
    status_token = status if isinstance(status, str) else ""
    reason_token = reason if isinstance(reason, str) else ""
    if status_token == "sent":
        if reason_token != "ok":
            return RESIDUE_TYPED_BUT_PENDING
        # Review j#95333 F1, same misreading on the residue axis: a marker-observed
        # `queue-enter` send also reports `ok`, but that rail never verified a submit. If the
        # Enter was absorbed, the marker+body is still sitting in the composer — the composer
        # is NOT cleared. Only positive turn-start evidence justifies `cleared`.
        if str(mode or "").strip() == MODE_QUEUE_ENTER and not (
            turn_start_positively_observed(
                queue_enter_turn_start_observation, turn_start_outcome
            )
        ):
            return RESIDUE_TYPED_BUT_PENDING
        return RESIDUE_CLEARED
    if status_token == "pending_input":
        return RESIDUE_TYPED_BUT_PENDING
    if status_token == "blocked" and reason_token in POST_INJECTION_BLOCKED_REASONS:
        return RESIDUE_UNSAFE_REQUIRES_FRESH_RECEIVER
    return RESIDUE_NOT_TYPED


def derive_delivery_id(
    *,
    intent: str,
    receiver: Optional[str],
    source: Optional[str] = None,
    issue: Optional[str] = None,
    journal: Optional[str] = None,
    task: Optional[str] = None,
    kind: Optional[str] = None,
    classification: Optional[str] = None,
) -> str:
    """Derive a deterministic delivery id for idempotency / duplicate prevention.

    The id is a stable hash of the logical payload identity (intent, receiver,
    anchor, kind, ticketless classification) — NOT of the resolved pane or the
    attempt — so re-running the same q-enter for the same payload yields the same
    id. A receiver/sender that observes a matching delivery id has a duplicate
    submit, which is the duplicate-prevention signal the primitive owns instead of
    the LLM. Deterministic by construction (no time / randomness), so it is safe to
    record and replay.
    """
    basis = "|".join(
        f"{key}={value or '-'}"
        for key, value in (
            ("intent", intent),
            ("to", receiver),
            ("source", source),
            ("issue", issue),
            ("journal", journal),
            ("task", task),
            ("kind", kind),
            ("classification", classification),
        )
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"qe-{digest}"


@dataclass(frozen=True)
class SubmitOutcome:
    """Front-door (workflow) result of the q-enter primitive, distinct from transport.

    Records the resolved rail / anchor obligation / delivery id and — when the front door
    fail-closed before any delivery — the blocked reason and the exact next action. The
    transport ``DeliveryOutcome`` (status / reason / marker) is emitted separately by the
    rail; this is the workflow-result surface the issue requires kept separate.
    Free-text-free except ``guidance``, which is a fixed fail-closed instruction string
    (never operator input).

    Redmine #14232 j#84877 required correction 1 — **plan success is not delivery success.**
    The front door used to carry a single ``dispatched`` flag set to ``True`` *before*
    ``orchestrate_handoff`` ran, so a subsequent ``blocked`` / ``turn_start_unconfirmed``
    never corrected the recorded front-door result. The two facts are now separate fields:

    - ``resolved`` — the intent resolved to a delivery rail and the anchor obligation was
      satisfied. This is what the old ``dispatched=True`` actually meant, and it is knowable
      before the transport runs.
    - ``dispatched`` — the transport **positively delivered** (the shared injection-stage
      authority classified it :data:`...injection_stage.STAGE_SUBMITTED_CONFIRMED`). Only
      derivable *after* the transport, and therefore only set by :meth:`from_transport`.

    ``injection_stage`` / ``blind_retry_prohibited`` put the retry decision on the public
    surface (issue acceptance 2: "retry可否とnext actionを公開JSON/textへ出す") so the LLM
    reads whether a resend can duplicate instead of inferring it from a rail name.
    """

    intent: str
    resolved_rail: Optional[str]
    anchor_required: bool
    ticketless: bool
    delivery_id: str
    dispatched: bool
    blocked: bool
    blocked_reason: Optional[str] = None
    guidance: Optional[str] = None
    #: Redmine #14232: the intent resolved to a rail (planning succeeded). Distinct from
    #: ``dispatched``; ``False`` only on the front door's own fail-closed paths.
    resolved: bool = False
    #: Redmine #14232: the shared injection-stage token for the transport outcome, or ``None``
    #: when the front door fail-closed before any transport ran (nothing was attempted, which
    #: is a *stronger* statement than ``not_sent`` — no rail was even resolved).
    injection_stage: Optional[str] = None
    #: Redmine #14232: whether re-issuing this send may duplicate the payload. ``False`` on the
    #: front door's own fail-closed paths (nothing was attempted, so a retry is safe).
    blind_retry_prohibited: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "q_enter": True,
            "intent": self.intent,
            "resolved_rail": self.resolved_rail,
            "anchor_required": bool(self.anchor_required),
            "ticketless": bool(self.ticketless),
            "delivery_id": self.delivery_id,
            "resolved": bool(self.resolved),
            "dispatched": bool(self.dispatched),
            "blocked": bool(self.blocked),
            "blocked_reason": self.blocked_reason,
            "guidance": self.guidance,
            "injection_stage": self.injection_stage,
            "blind_retry_prohibited": bool(self.blind_retry_prohibited),
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_transport(
        cls,
        outcome: object,
        *,
        plan_intent: str,
        rail: Optional[str],
        anchor_required: bool,
        ticketless: bool,
        delivery_id: str,
    ) -> "SubmitOutcome":
        """Derive the front-door result from the transport's terminal outcome (#14232).

        The whole point of j#84877 required correction 1: ``dispatched`` is computed here,
        after the rail reported, from the SAME injection-stage authority the delivery record
        and the callback / outbox retry decision read — so a blocked or unconfirmed transport
        can never leave a "dispatched" front-door record behind. ``resolved`` stays ``True``
        because planning genuinely did succeed; the two facts are reported side by side rather
        than one standing in for the other.

        Redmine #14232 review j#95333 F1: this takes the whole
        :class:`...handoff.DeliveryOutcome`, not its two wire tokens. ``sent`` / ``ok`` is
        ambiguous across rails — tmux, legacy, or synthetic ``queue-enter`` outcomes can report
        it without causal turn-start evidence — so a two-token derivation confirmed deliveries
        the outcome's own telemetry contradicted. Herdr now requires the evidence. ``outcome`` is
        ``None`` when the rail returned no structured outcome at all (an early return, or a
        caller that never sent), which the
        authority fails closed to ``uncertain_partial``: the front door must not claim a
        delivery it cannot see.

        Three distinct facts are reported, deliberately not collapsed into one:

        - ``dispatched`` — the payload's submission was confirmed;
        - ``blocked`` — the front door could not do what it was invoked for, and the caller
          must act. Review j#95333 F2 narrowed this: a ``pending_input`` terminal is **not**
          blocked, because ``--mode pending`` is the caller explicitly asking the rail not to
          submit, and getting exactly what you asked for is not a block. R1 derived ``blocked``
          from ``not delivered``, which swept that deliberate park in with the failures; before
          #14232 this flag was only ever the front door's OWN fail-closed paths.
        - ``injection_stage`` / ``blind_retry_prohibited`` — whether a resend may duplicate. A
          parked ``pending_input`` still prohibits a blind resend: its body IS in the composer.
        """
        stage = injection_stage_for_outcome(outcome)
        delivered = stage == STAGE_SUBMITTED_CONFIRMED
        reason = getattr(outcome, "reason", None)
        parked = str(getattr(outcome, "status", None) or "").strip() == STATUS_PENDING_INPUT
        return cls(
            intent=plan_intent,
            resolved_rail=rail,
            anchor_required=anchor_required,
            ticketless=ticketless,
            delivery_id=delivery_id,
            resolved=True,
            dispatched=delivered,
            blocked=not delivered and not parked,
            blocked_reason=(
                None if (delivered or parked) else (str(reason) if reason else None)
            ),
            guidance=stage_guidance(stage),
            injection_stage=stage,
            blind_retry_prohibited=blind_retry_prohibited(stage),
        )

    def record_lines(self) -> list[str]:
        """Compact pasteable front-door record block (durable-record safe)."""
        if not self.resolved:
            head = "blocked before any delivery was attempted"
        elif self.dispatched:
            head = "delivery confirmed"
        else:
            head = f"delivery not confirmed ({self.injection_stage or 'unknown'})"
        lines = [
            f"q-enter front door — {head}",
            "",
            f"- Intent: `{self.intent}`",
            f"- Resolved rail: `{self.resolved_rail or '—'}`",
            f"- Anchor required: `{str(bool(self.anchor_required)).lower()}`",
            f"- Delivery id (idempotency): `{self.delivery_id}`",
            f"- Rail resolved: `{str(bool(self.resolved)).lower()}` "
            f"(plan success is not delivery success)",
            f"- Delivery confirmed: `{str(bool(self.dispatched)).lower()}`",
        ]
        if self.injection_stage:
            lines.append(f"- Injection stage: `{self.injection_stage}`")
            lines.append(
                "- Blind retry prohibited: "
                f"`{str(bool(self.blind_retry_prohibited)).lower()}`"
            )
        if self.blocked_reason:
            lines.append(f"- Blocked reason: `{self.blocked_reason}`")
        if self.guidance:
            lines.append(f"- Next action: {self.guidance}")
        if self.resolved:
            lines.append(
                "- Read the adjacent transport outcome (status / reason / next action) and "
                "the `- Submit:` composer-residue line for the full delivery result."
            )
        return lines


def submit_record_lines(
    *,
    status: object,
    reason: object,
    intent: str,
    delivery_id: str,
    mode: object = None,
    queue_enter_turn_start_observation: object = None,
    turn_start_outcome: object = None,
) -> list[str]:
    """Render the additive ``- Submit:`` telemetry block for the delivery record.

    Carries only fixed tokens + the deterministic delivery id (no free text), so it
    is safe in the pasteable record and the opt-in persisted note. It documents the
    front-door facts the transport outcome does not — the composer residue
    classification and the idempotency id — and never overrides ``next_action``.
    """
    residue = classify_composer_residue(
        status,
        reason,
        mode=mode,
        queue_enter_turn_start_observation=queue_enter_turn_start_observation,
        turn_start_outcome=turn_start_outcome,
    )
    return [
        f"- Submit (q-enter front door): intent `{intent}`, "
        f"delivery id `{delivery_id}`",
        f"  - Composer residue: `{residue}`",
        "  - Duplicate prevention: re-running the same q-enter yields delivery id "
        f"`{delivery_id}`; a matching id on a later submit is a duplicate.",
    ]


__all__: Iterable[str] = (
    "SubmitPlanError",
    "INTENT_WORKER_DISPATCH",
    "INTENT_REPLY",
    "INTENT_CONSULTATION_CALLBACK",
    "SUBMIT_INTENTS",
    "RAIL_ANCHORED_SEND",
    "RAIL_ANCHORED_REPLY",
    "RAIL_TICKETLESS_CALLBACK",
    "RESIDUE_NOT_TYPED",
    "RESIDUE_TYPED_BUT_PENDING",
    "RESIDUE_CLEARED",
    "RESIDUE_UNSAFE_REQUIRES_FRESH_RECEIVER",
    "COMPOSER_RESIDUE_STATES",
    "SubmitPlan",
    "resolve_submit_plan",
    "classify_composer_residue",
    "derive_delivery_id",
    "SubmitOutcome",
    "submit_record_lines",
)
