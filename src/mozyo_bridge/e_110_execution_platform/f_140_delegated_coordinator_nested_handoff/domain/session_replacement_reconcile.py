"""Atomic self-replacement — tranche C pure decisions (Redmine #13806).

Tranche C composes the tranche A transaction / tranche B actuator into the three
remaining flows: the bare-``mozyo`` pre-attach reconciliation seam, the process-external
self-close executor's action-time seals, and the fresh-coordinator continuation drain.
This module is their pure half — the closed vocabularies and fail-closed decisions,
free of I/O, the live inventory, and the CAS store, so every branch is pinned by tests
that touch no process and no DB.

Three decision families:

- **pre-attach reconcile** (:func:`decide_pre_attach`): after onboarding/root resolution
  and *before* attach, decide pass-through (a fully ready session), reconcile-once (an
  unresolved session with exactly one positive approved transaction), or a typed blocked
  outcome (approval absent / stale / ambiguous / unreadable) — the composition root writes
  nothing on the blocked path (j#78384 §5).
- **self-close seal** (:func:`decide_self_close`): the action-time re-verify a
  process-external executor must pass before closing the exact old coordinator generation
  (j#78384 §2/§3) — phase, generation, old-coordinator pin, turn-ended + idle, no pending
  composer, preservation seal, continuation seal.
- **continuation drain** (:func:`drain_state_for`, :func:`may_attempt_drain`): the
  ``not_attempted -> attempted -> confirmed | uncertain`` state, mapped onto the existing
  transaction phases (no second ledger, j#79121 scope 4) with the "never blind-resend after
  attempted" rule (j#78384 §2).
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_FRESH_COORDINATOR_CLAIMED,
    norm,
)

# -- transaction resolution (what a pre-attach reconcile found) -----------------

#: Exactly one positive, approved, actionable replacement transaction resolves for the
#: unresolved session — the only case a bounded reconciliation may actuate.
TXN_RESOLVED_EXACT = "resolved_exact"
#: No replacement transaction for this session — nothing approved to reconcile.
TXN_ABSENT = "absent"
#: A transaction exists but at a superseded generation / already terminal — stale approval.
TXN_STALE = "stale"
#: More than one candidate transaction — never guess which to actuate.
TXN_AMBIGUOUS = "ambiguous"
#: The transaction store is unreadable — never degrade to "absent" and launch fresh blind.
TXN_UNREADABLE = "unreadable"

TXN_RESOLUTIONS = frozenset(
    {TXN_RESOLVED_EXACT, TXN_ABSENT, TXN_STALE, TXN_AMBIGUOUS, TXN_UNREADABLE}
)

# -- pre-attach reconcile decision ----------------------------------------------

#: The session is fully ready (every slot adopted/launched) — attach unchanged. The
#: reconciliation seam is a no-op; the existing all-adopt / all-launch / tmux / explicit
#: ``herdr session-start`` behavior is preserved byte-for-byte.
RECONCILE_PASS_THROUGH = "pass_through"
#: The session has unresolved slots AND exactly one positive approved transaction — call the
#: process-external executor once.
RECONCILE_ONCE = "reconcile_once"
#: The session has unresolved slots but no actionable approval — a single actionable typed
#: blocked outcome (zero process / input / route / outbox writes).
RECONCILE_BLOCKED = "blocked"

#: Blocked reasons (a closed vocabulary, mapped from the transaction resolution).
BLOCKED_APPROVAL_ABSENT = "approval_absent"
BLOCKED_APPROVAL_STALE = "approval_stale"
BLOCKED_APPROVAL_AMBIGUOUS = "approval_ambiguous"
BLOCKED_STORE_UNREADABLE = "store_unreadable"

_RESOLUTION_BLOCK_REASON: dict[str, str] = {
    TXN_ABSENT: BLOCKED_APPROVAL_ABSENT,
    TXN_STALE: BLOCKED_APPROVAL_STALE,
    TXN_AMBIGUOUS: BLOCKED_APPROVAL_AMBIGUOUS,
    TXN_UNREADABLE: BLOCKED_STORE_UNREADABLE,
}


class PreAttachDecision:
    """The pre-attach reconciliation decision (kind + optional blocked reason)."""

    __slots__ = ("kind", "blocked_reason")

    def __init__(self, kind: str, blocked_reason: str = "") -> None:
        self.kind = kind
        self.blocked_reason = blocked_reason

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, PreAttachDecision)
            and other.kind == self.kind
            and other.blocked_reason == self.blocked_reason
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"PreAttachDecision({self.kind!r}, {self.blocked_reason!r})"


def decide_pre_attach(*, session_ready: bool, resolution: str) -> PreAttachDecision:
    """Decide the bounded pre-attach reconciliation. (pure)

    A ready session passes through untouched (the seam never perturbs a healthy launch). An
    unresolved session reconciles once ONLY for an exact positive transaction; every other
    resolution (absent / stale / ambiguous / unreadable) is a typed blocked outcome with no
    actuation — the composition root writes nothing (j#78384 §5). An unknown resolution token
    is treated as unreadable (fail closed), never silently reconciled.
    """
    if session_ready:
        return PreAttachDecision(RECONCILE_PASS_THROUGH)
    marker = norm(resolution)
    if marker == TXN_RESOLVED_EXACT:
        return PreAttachDecision(RECONCILE_ONCE)
    reason = _RESOLUTION_BLOCK_REASON.get(marker, BLOCKED_STORE_UNREADABLE)
    return PreAttachDecision(RECONCILE_BLOCKED, blocked_reason=reason)


# -- self-close seal decision ---------------------------------------------------

#: Every seal passed — the process-external executor may close the exact old coordinator.
SELF_CLOSE_MAY_PROCEED = "may_proceed"

#: Closed blocked-reason vocabulary for a refused self-close (each a fail-closed stop).
SELF_BLOCK_NOT_ARMED = "not_self_close_armed"
SELF_BLOCK_GENERATION = "generation_mismatch"
SELF_BLOCK_IDENTITY = "old_coordinator_identity_mismatch"
SELF_BLOCK_TURN_ACTIVE = "turn_not_ended"
SELF_BLOCK_BUSY = "target_not_idle"
SELF_BLOCK_PENDING_COMPOSER = "pending_composer"
SELF_BLOCK_PRESERVATION = "preservation_blocked"
SELF_BLOCK_CONTINUATION_UNSEALED = "continuation_not_sealed"


class SelfCloseObservation:
    """The action-time seals a process-external executor observes before a self-close.

    Every field is a *positive* fact the executor must confirm (all default to the unsafe
    side, so a missing observation fails closed):

    - ``at_self_close_armed`` — the transaction is armed (its phase is ``self_close_armed``).
    - ``generation_matches`` — the exact immutable action generation still matches.
    - ``old_coordinator_matches`` — the live old coordinator still resolves to the pinned
      ``(role, provider, assigned_name, locator, revision)`` — a same-name recycled slot does
      not match (evidence, not authority).
    - ``turn_ended`` / ``idle`` — the victim's turn has ended and it is not working /
      mutating (j#78384 §3 ``running_process``: self is ``turn_ended`` observed).
    - ``no_pending_composer`` — no un-submitted composer input (a #13763 uncorrelated
      pending composer forbids a clean close).
    - ``preservation_clear`` — the preservation fence is clear (dirty diff / running mutation
      / unrecorded journal / pending approval / identity mismatch all absent).
    - ``continuation_sealed`` — the current coordinator has recorded the continuation seal
      journal, so a fresh coordinator has something durable to resume from (j#78384 §3
      ``unrecorded_journal``).
    """

    __slots__ = (
        "at_self_close_armed",
        "generation_matches",
        "old_coordinator_matches",
        "turn_ended",
        "idle",
        "no_pending_composer",
        "preservation_clear",
        "continuation_sealed",
    )

    def __init__(
        self,
        *,
        at_self_close_armed: bool = False,
        generation_matches: bool = False,
        old_coordinator_matches: bool = False,
        turn_ended: bool = False,
        idle: bool = False,
        no_pending_composer: bool = False,
        preservation_clear: bool = False,
        continuation_sealed: bool = False,
    ) -> None:
        self.at_self_close_armed = bool(at_self_close_armed)
        self.generation_matches = bool(generation_matches)
        self.old_coordinator_matches = bool(old_coordinator_matches)
        self.turn_ended = bool(turn_ended)
        self.idle = bool(idle)
        self.no_pending_composer = bool(no_pending_composer)
        self.preservation_clear = bool(preservation_clear)
        self.continuation_sealed = bool(continuation_sealed)


def decide_self_close(observation: SelfCloseObservation) -> str:
    """Decide whether a self-close may proceed. (pure, fail-closed, ordered)

    Returns :data:`SELF_CLOSE_MAY_PROCEED` only when every seal holds; otherwise the first
    failing seal's closed reason (checked most-fundamental first) so the durable record names
    exactly which seal blocked. No seal defaults to the safe side, so a missing observation
    blocks.
    """
    if not observation.at_self_close_armed:
        return SELF_BLOCK_NOT_ARMED
    if not observation.generation_matches:
        return SELF_BLOCK_GENERATION
    if not observation.old_coordinator_matches:
        return SELF_BLOCK_IDENTITY
    if not observation.turn_ended:
        return SELF_BLOCK_TURN_ACTIVE
    if not observation.idle:
        return SELF_BLOCK_BUSY
    if not observation.no_pending_composer:
        return SELF_BLOCK_PENDING_COMPOSER
    if not observation.preservation_clear:
        return SELF_BLOCK_PRESERVATION
    if not observation.continuation_sealed:
        return SELF_BLOCK_CONTINUATION_UNSEALED
    return SELF_CLOSE_MAY_PROCEED


# -- continuation drain state ---------------------------------------------------

#: The drain has not been attempted (the fresh coordinator has claimed but not yet sent).
DRAIN_NOT_ATTEMPTED = "not_attempted"
#: A drain send has been attempted; its landing is not yet confirmed. A crash here resumes
#: as ``uncertain`` — never a blind resend (j#78384 §2 / fail-closed matrix).
DRAIN_ATTEMPTED = "attempted"
#: The drain's semantic action landed (confirmed against the durable gate / outbox).
DRAIN_CONFIRMED = "confirmed"
#: A drain was attempted but its outcome cannot be confirmed (a crash after ``attempted``).
#: Resolved only by re-reading the durable gate / outbox, never by re-sending blind.
DRAIN_UNCERTAIN = "uncertain"

DRAIN_STATES = frozenset(
    {DRAIN_NOT_ATTEMPTED, DRAIN_ATTEMPTED, DRAIN_CONFIRMED, DRAIN_UNCERTAIN}
)


def drain_state_for(phase: str, *, gate_confirmed: bool) -> str:
    """Map a transaction phase (+ a durable-gate observation) to a drain state. (pure)

    The drain state machine rides the EXISTING transaction phases — no second ledger
    (j#79121 scope 4):

    - ``fresh_coordinator_claimed`` -> ``not_attempted`` (claimed, nothing sent);
    - ``draining_continuation`` -> ``attempted`` if the gate is not yet confirmed
      (a send is in flight or its outcome is unknown = ``uncertain`` on resume), or
      ``confirmed`` once the durable gate/outbox shows the semantic action landed;
    - ``completed`` -> ``confirmed``.

    ``draining_continuation`` with ``gate_confirmed=False`` is deliberately ``attempted``,
    not a fresh ``not_attempted``: the phase transition into it is recorded BEFORE the send,
    so a resume there means a send may already have gone out — the caller must consult the
    gate, never blind-resend.
    """
    marker = norm(phase)
    if marker == PHASE_FRESH_COORDINATOR_CLAIMED:
        return DRAIN_NOT_ATTEMPTED
    if marker == PHASE_COMPLETED:
        return DRAIN_CONFIRMED
    if marker == PHASE_DRAINING_CONTINUATION:
        return DRAIN_CONFIRMED if gate_confirmed else DRAIN_ATTEMPTED
    # Any earlier phase is not a drain state at all.
    return DRAIN_NOT_ATTEMPTED


def may_attempt_drain(state: str) -> bool:
    """May a drain SEND be issued now? (pure)

    Only from :data:`DRAIN_NOT_ATTEMPTED`. Once ``attempted`` / ``uncertain``, a send may
    already be in flight, so the caller must re-read the durable gate to decide
    confirmed-vs-still-needed rather than blind-resend; ``confirmed`` is done. This is the
    "never blind-resend after attempted" fence (j#78384 §2).
    """
    return norm(state) == DRAIN_NOT_ATTEMPTED


__all__ = (
    "TXN_RESOLVED_EXACT",
    "TXN_ABSENT",
    "TXN_STALE",
    "TXN_AMBIGUOUS",
    "TXN_UNREADABLE",
    "TXN_RESOLUTIONS",
    "RECONCILE_PASS_THROUGH",
    "RECONCILE_ONCE",
    "RECONCILE_BLOCKED",
    "BLOCKED_APPROVAL_ABSENT",
    "BLOCKED_APPROVAL_STALE",
    "BLOCKED_APPROVAL_AMBIGUOUS",
    "BLOCKED_STORE_UNREADABLE",
    "PreAttachDecision",
    "decide_pre_attach",
    "SELF_CLOSE_MAY_PROCEED",
    "SELF_BLOCK_NOT_ARMED",
    "SELF_BLOCK_GENERATION",
    "SELF_BLOCK_IDENTITY",
    "SELF_BLOCK_TURN_ACTIVE",
    "SELF_BLOCK_BUSY",
    "SELF_BLOCK_PENDING_COMPOSER",
    "SELF_BLOCK_PRESERVATION",
    "SELF_BLOCK_CONTINUATION_UNSEALED",
    "SelfCloseObservation",
    "decide_self_close",
    "DRAIN_NOT_ATTEMPTED",
    "DRAIN_ATTEMPTED",
    "DRAIN_CONFIRMED",
    "DRAIN_UNCERTAIN",
    "DRAIN_STATES",
    "drain_state_for",
    "may_attempt_drain",
)
