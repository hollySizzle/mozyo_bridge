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

    Fails closed (:class:`SubmitPlanError`) on an unknown intent, and on a
    ``worker_dispatch`` / ``reply`` intent that lacks a complete ticket anchor —
    the Redmine-governed worker-dispatch anchor requirement is not relaxed. The
    error names the missing anchor and points at ``consultation_callback`` for the
    genuinely-no-anchor hands-off case, so the LLM reads the next action instead of
    rediscovering ``invalid_anchor`` by trial.
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
        # durable record. Refuse a stray anchor source so the LLM does not think it
        # bought a Redmine-governed guarantee on this rail.
        if source is not None:
            raise SubmitPlanError(
                "consultation_callback rides the ticketless no-anchor callback rail "
                "and takes no --source; drop --source (and any --issue/--journal/"
                "--task-id), or use --intent worker_dispatch / reply for an "
                "anchored send"
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


def classify_composer_residue(status: object, reason: object) -> str:
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
    - ``blocked`` / ``marker_timeout`` — strict rail, a C-u rollback was issued and
      Enter was NOT pressed, but the composer clear is not verifiable from tmux
      capture (j#66977 observed residual prompt text) -> the only safe read is
      ``unsafe_state_requires_fresh_receiver``.
    - any other ``blocked`` (``invalid_anchor`` / ``invalid_args`` /
      ``target_*`` / ``cross_session_claude``) — blocked before anything was typed
      -> ``not_typed``.
    """
    status_token = status if isinstance(status, str) else ""
    reason_token = reason if isinstance(reason, str) else ""
    if status_token == "sent":
        return RESIDUE_CLEARED if reason_token == "ok" else RESIDUE_TYPED_BUT_PENDING
    if status_token == "pending_input":
        return RESIDUE_TYPED_BUT_PENDING
    if status_token == "blocked" and reason_token == "marker_timeout":
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

    Records the resolved rail / anchor obligation / delivery id and — when the
    front door fail-closed before any delivery — the blocked reason and the exact
    next action. The transport ``DeliveryOutcome`` (status / reason / marker) is
    emitted separately by the rail; this is the workflow-result surface the issue
    requires kept separate. Free-text-free except ``guidance``, which is a fixed
    fail-closed instruction string (never operator input).
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

    def to_dict(self) -> dict[str, object]:
        return {
            "q_enter": True,
            "intent": self.intent,
            "resolved_rail": self.resolved_rail,
            "anchor_required": bool(self.anchor_required),
            "ticketless": bool(self.ticketless),
            "delivery_id": self.delivery_id,
            "dispatched": bool(self.dispatched),
            "blocked": bool(self.blocked),
            "blocked_reason": self.blocked_reason,
            "guidance": self.guidance,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def record_lines(self) -> list[str]:
        """Compact pasteable front-door record block (durable-record safe)."""
        head = "blocked" if self.blocked else "dispatched"
        lines = [
            f"q-enter front door — {head}",
            "",
            f"- Intent: `{self.intent}`",
            f"- Resolved rail: `{self.resolved_rail or '—'}`",
            f"- Anchor required: `{str(bool(self.anchor_required)).lower()}`",
            f"- Delivery id (idempotency): `{self.delivery_id}`",
        ]
        if self.blocked:
            lines.append(f"- Blocked reason: `{self.blocked_reason or '—'}`")
            if self.guidance:
                lines.append(f"- Next action: {self.guidance}")
        else:
            lines.append(
                "- Delivered over the resolved rail; read the adjacent transport "
                "outcome (status / reason / next action) and the `- Submit:` "
                "composer-residue line for the delivery result."
            )
        return lines


def submit_record_lines(
    *,
    status: object,
    reason: object,
    intent: str,
    delivery_id: str,
) -> list[str]:
    """Render the additive ``- Submit:`` telemetry block for the delivery record.

    Carries only fixed tokens + the deterministic delivery id (no free text), so it
    is safe in the pasteable record and the opt-in persisted note. It documents the
    front-door facts the transport outcome does not — the composer residue
    classification and the idempotency id — and never overrides ``next_action``.
    """
    residue = classify_composer_residue(status, reason)
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
