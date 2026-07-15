"""Lane actionability / ownership — the axis orthogonal to lane state (Redmine #13756).

The pre-#13756 lane vocabulary had one axis: the state class. So ``review_waiting``
meant exactly one thing to the fill policy — *stop, the coordinator must drain this* —
even when the review had already been delivered to a dedicated same-lane gateway and a
duplicate main-coordinator review was **forbidden**. The pipeline serialized on work the
main coordinator was not allowed to touch. (Real incident, 2026-07-13: #13441's US audit
was running on a dedicated gateway and #13734 was waiting on an external supersede
condition, and the fill gate stopped an independent, ready #13682 dispatch.)

The fix is not to lie about the state (an unreviewed lane is *not* ``implementing``). It
is to add the missing axis: **who owns the next action**, and is that owner the main
coordinator?

- :data:`ACTIONABILITY_COORDINATOR_ACTIONABLE` — the main coordinator can drain this
  *now*. It stops new optional dispatch. This is the default and the fail-closed sink.
- :data:`ACTIONABILITY_DELEGATED_IN_FLIGHT` — a dedicated gateway / worker owns the next
  action and a main duplicate is forbidden. It occupies capacity but is **not** a fill
  stop reason.
- :data:`ACTIONABILITY_NON_ACTIONABLE_WAIT` — the lane waits on a durable external
  unblock condition; the main coordinator has no action available. It stays visible for
  capacity / retirement, but it does not stop independent ready work.

Every non-blocking claim must be *earned*, and the checks are the ones the incident
review named (#13756 description, j#77979):

- **an ACK is not completion.** ``delegated_in_flight`` requires a confirmed delivery, a
  durable callback expectation, and a callback that is not overdue. A
  ``delivery_failed``, a missing callback expectation, or a callback past its deadline
  (a stalled delegation) all revert the lane to coordinator-blocking.
- **only a real sublane can delegate.** The claim is honoured only from a *verified
  managed sublane* (:func:`...lane_execution_surface.is_verified_managed_sublane`). An
  internal task agent, a bare worktree, or an unverifiable provenance claim cannot own a
  delegated action (#13756 j#78320).
- **main-owned debt is never delegable.** ``owner_waiting`` / ``integration_waiting`` /
  ``close_waiting`` / ``callback_delivery_failed`` are the main coordinator's by
  construction. The caller marks them ``state_is_main_owned`` and no claim rescues them.
- **an unknown owner or an unknown claim fails closed** to coordinator-blocking.

This module owns the actionability axis only. The lane *state* vocabulary (and which
states are coordinator-blocking / main-owned) stays in
:mod:`...domain.workflow_fill_decision`, which passes the classification in — so the
state authority is not duplicated and the two modules do not import each other in a
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (
    LaneProvenance,
    is_verified_managed_sublane,
)

# ---------------------------------------------------------------------------
# Actionability vocabulary (closed; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

ACTIONABILITY_COORDINATOR_ACTIONABLE = "coordinator_actionable"
ACTIONABILITY_DELEGATED_IN_FLIGHT = "delegated_in_flight"
ACTIONABILITY_NON_ACTIONABLE_WAIT = "non_actionable_wait"

ACTIONABILITIES = frozenset(
    {
        ACTIONABILITY_COORDINATOR_ACTIONABLE,
        ACTIONABILITY_DELEGATED_IN_FLIGHT,
        ACTIONABILITY_NON_ACTIONABLE_WAIT,
    }
)


# ---------------------------------------------------------------------------
# Next-action owner vocabulary (closed). `unknown` is explicit and fails closed: a lane
# whose owner cannot be named is drained by the coordinator, not dispatched past.
# ---------------------------------------------------------------------------

OWNER_MAIN_COORDINATOR = "main_coordinator"
OWNER_DEDICATED_GATEWAY = "dedicated_gateway"
OWNER_DEDICATED_WORKER = "dedicated_worker"
OWNER_OWNER = "owner"
OWNER_EXTERNAL_CONDITION = "external_condition"
OWNER_UNKNOWN = "unknown"

NEXT_ACTION_OWNERS = frozenset(
    {
        OWNER_MAIN_COORDINATOR,
        OWNER_DEDICATED_GATEWAY,
        OWNER_DEDICATED_WORKER,
        OWNER_OWNER,
        OWNER_EXTERNAL_CONDITION,
        OWNER_UNKNOWN,
    }
)

# The owners a `delegated_in_flight` lane may name: a dedicated gateway or worker that
# owns the next action and forbids a main duplicate. `owner` (the human owner) is
# deliberately excluded — owner debt is the coordinator's to aggregate, and it is
# already an owner/release gate.
DELEGATED_OWNERS = frozenset({OWNER_DEDICATED_GATEWAY, OWNER_DEDICATED_WORKER})

# The only owner a `non_actionable_wait` lane may name: the wait is on a durable external
# condition, not on a person or a process the coordinator can poke.
NON_ACTIONABLE_OWNERS = frozenset({OWNER_EXTERNAL_CONDITION})


# ---------------------------------------------------------------------------
# Delegated delivery state. An ACK is not completion: `sent` means the request landed,
# nothing more — the callback expectation and its deadline carry the rest.
# ---------------------------------------------------------------------------

DELIVERY_NOT_ATTEMPTED = "not_attempted"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "delivery_failed"

DELIVERY_STATES = frozenset(
    {DELIVERY_NOT_ATTEMPTED, DELIVERY_SENT, DELIVERY_FAILED}
)


# ---------------------------------------------------------------------------
# Reason codes — the fixed token naming *why* a lane resolved the way it did, so a
# coordinator can journal the verdict without paraphrasing it.
# ---------------------------------------------------------------------------

REASON_STATE_NOT_BLOCKING = "state_not_coordinator_blocking"
REASON_COORDINATOR_OWNED = "coordinator_owned"
REASON_MAIN_OWNED_STATE = "main_owned_state_not_delegable"
REASON_SURFACE_NOT_VERIFIED = "surface_not_verified_managed_sublane"
REASON_UNKNOWN_ACTIONABILITY = "unknown_actionability"
REASON_OWNER_NOT_DELEGATED = "next_action_owner_not_delegated"
REASON_DELIVERY_NOT_CONFIRMED = "delegated_delivery_not_confirmed"
REASON_DELIVERY_FAILED = "delegated_delivery_failed"
REASON_NO_CALLBACK_EXPECTATION = "no_durable_callback_expectation"
REASON_CALLBACK_OVERDUE = "delegated_callback_overdue"
REASON_DELEGATED_VERIFIED = "delegated_in_flight_verified"
REASON_WAIT_OWNER_NOT_EXTERNAL = "wait_owner_not_external_condition"
REASON_NO_UNBLOCK_CONDITION = "no_durable_unblock_condition"
REASON_WAIT_STALLED = "non_actionable_wait_stalled"
REASON_WAIT_VERIFIED = "non_actionable_wait_verified"


# ---------------------------------------------------------------------------
# Claim + verdict.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionabilityClaim:
    """What a lane *claims* about who owns its next action (all fields fail-closed).

    Defaults reproduce the pre-#13756 behaviour exactly: a legacy caller that supplies
    only a state class gets ``coordinator_actionable`` / ``main_coordinator`` / no
    delivery / no callback expectation, so every blocking state stays blocking.

    - ``delivery_state`` — the delegated handoff's delivery outcome
      (:data:`DELIVERY_*`). ``sent`` means the request landed on the delegate.
    - ``callback_expected`` — a durable callback is expected back. Without it, a
      delivered request is a fire-and-forget with no completion signal, so the claim is
      refused (an ACK is not completion).
    - ``callback_overdue`` — the callback deadline has passed / the delegation stalled.
      Set by the caller from the durable record; it reverts the lane to blocking.
    - ``unblock_condition`` — the durable condition a ``non_actionable_wait`` is waiting
      on. An empty condition is an unfalsifiable wait, so the claim is refused.
    """

    actionability: str = ACTIONABILITY_COORDINATOR_ACTIONABLE
    next_action_owner: str = OWNER_MAIN_COORDINATOR
    delivery_state: str = DELIVERY_NOT_ATTEMPTED
    callback_expected: bool = False
    callback_overdue: bool = False
    unblock_condition: str = ""


@dataclass(frozen=True)
class ActionabilityVerdict:
    """The resolved actionability: the effective class, whether it blocks, and why.

    ``actionability`` is the **effective** class after verification — a refused claim
    resolves to :data:`ACTIONABILITY_COORDINATOR_ACTIONABLE`, never to the claimed
    value, so a narrative claim can never appear in the projection as if it verified.
    ``coordinator_blocking`` is the verdict the fill policy consumes. ``reason`` is one
    fixed :data:`REASON_*` token.
    """

    actionability: str
    coordinator_blocking: bool
    reason: str

    @property
    def delegated_in_flight(self) -> bool:
        return self.actionability == ACTIONABILITY_DELEGATED_IN_FLIGHT

    @property
    def non_actionable_wait(self) -> bool:
        return self.actionability == ACTIONABILITY_NON_ACTIONABLE_WAIT


def _resolve_delegated(claim: ActionabilityClaim) -> tuple[str, str]:
    """Verify a ``delegated_in_flight`` claim. An ACK alone never satisfies it."""
    if claim.next_action_owner not in DELEGATED_OWNERS:
        # Includes `unknown` and `main_coordinator`: a delegation with no nameable
        # delegate is a main-coordinator debt wearing a delegation label.
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_OWNER_NOT_DELEGATED
    if claim.delivery_state == DELIVERY_FAILED:
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_DELIVERY_FAILED
    if claim.delivery_state != DELIVERY_SENT:
        # `not_attempted`, or an unrecognized delivery token: the request never landed,
        # so nothing is in flight and the coordinator still owns the send.
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_DELIVERY_NOT_CONFIRMED
    if not claim.callback_expected:
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_NO_CALLBACK_EXPECTATION
    if claim.callback_overdue:
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_CALLBACK_OVERDUE
    return ACTIONABILITY_DELEGATED_IN_FLIGHT, REASON_DELEGATED_VERIFIED


def _resolve_non_actionable(claim: ActionabilityClaim) -> tuple[str, str]:
    """Verify a ``non_actionable_wait`` claim: a named external condition, not stalled."""
    if claim.next_action_owner not in NON_ACTIONABLE_OWNERS:
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_WAIT_OWNER_NOT_EXTERNAL
    if not claim.unblock_condition.strip():
        # An unfalsifiable wait: nothing durable says what would end it.
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_NO_UNBLOCK_CONDITION
    if claim.callback_overdue:
        # The wait itself stalled past its recorded deadline: the coordinator owns
        # re-driving or escalating it.
        return ACTIONABILITY_COORDINATOR_ACTIONABLE, REASON_WAIT_STALLED
    return ACTIONABILITY_NON_ACTIONABLE_WAIT, REASON_WAIT_VERIFIED


def resolve_actionability(
    claim: ActionabilityClaim,
    provenance: LaneProvenance,
    *,
    state_is_coordinator_blocking: bool,
    state_is_main_owned: bool,
) -> ActionabilityVerdict:
    """Resolve one lane's effective actionability and its blocking verdict (fail-closed).

    ``state_is_coordinator_blocking`` / ``state_is_main_owned`` are the lane *state*
    classification, supplied by :mod:`...domain.workflow_fill_decision` (the state
    vocabulary's authority). An unreadable state class must be passed as **both** true:
    it is then blocking and no claim can rescue it.

    Order (each step is a fail-closed refusal, not a fallback):

    1. an unrecognized actionability token -> coordinator_actionable;
    2. an explicit ``coordinator_actionable`` claim -> taken at face value;
    3. a main-owned state (owner / integration / close / callback-delivery-failed, and
       any unreadable state) -> coordinator_actionable regardless of the claim;
    4. a lane that is not a *verified managed sublane* -> coordinator_actionable (only a
       real sublane has a dedicated gateway / worker to delegate to);
    5. the claim's own checks (:func:`_resolve_delegated` / :func:`_resolve_non_actionable`).

    The blocking verdict is then simply: the state is coordinator-blocking **and** the
    effective actionability is ``coordinator_actionable``. A non-blocking state
    (``implementing`` / ``retire_ready`` / ``idle``) never becomes blocking here — a
    misdeclared claim on such a lane degrades its actionability label, it does not
    invent a stop reason.
    """
    if claim.actionability not in ACTIONABILITIES:
        effective, reason = (
            ACTIONABILITY_COORDINATOR_ACTIONABLE,
            REASON_UNKNOWN_ACTIONABILITY,
        )
    elif claim.actionability == ACTIONABILITY_COORDINATOR_ACTIONABLE:
        effective, reason = (
            ACTIONABILITY_COORDINATOR_ACTIONABLE,
            REASON_COORDINATOR_OWNED,
        )
    elif state_is_main_owned:
        effective, reason = (
            ACTIONABILITY_COORDINATOR_ACTIONABLE,
            REASON_MAIN_OWNED_STATE,
        )
    elif not is_verified_managed_sublane(provenance):
        effective, reason = (
            ACTIONABILITY_COORDINATOR_ACTIONABLE,
            REASON_SURFACE_NOT_VERIFIED,
        )
    elif claim.actionability == ACTIONABILITY_DELEGATED_IN_FLIGHT:
        effective, reason = _resolve_delegated(claim)
    else:
        effective, reason = _resolve_non_actionable(claim)

    blocking = state_is_coordinator_blocking and (
        effective == ACTIONABILITY_COORDINATOR_ACTIONABLE
    )
    if not state_is_coordinator_blocking and reason == REASON_COORDINATOR_OWNED:
        # A plain non-blocking lane (`implementing` etc.) with no delegation claim: name
        # the reason for what it is rather than implying coordinator debt.
        reason = REASON_STATE_NOT_BLOCKING
    return ActionabilityVerdict(
        actionability=effective, coordinator_blocking=blocking, reason=reason
    )


__all__ = (
    "ACTIONABILITY_COORDINATOR_ACTIONABLE",
    "ACTIONABILITY_DELEGATED_IN_FLIGHT",
    "ACTIONABILITY_NON_ACTIONABLE_WAIT",
    "ACTIONABILITIES",
    "OWNER_MAIN_COORDINATOR",
    "OWNER_DEDICATED_GATEWAY",
    "OWNER_DEDICATED_WORKER",
    "OWNER_OWNER",
    "OWNER_EXTERNAL_CONDITION",
    "OWNER_UNKNOWN",
    "NEXT_ACTION_OWNERS",
    "DELEGATED_OWNERS",
    "NON_ACTIONABLE_OWNERS",
    "DELIVERY_NOT_ATTEMPTED",
    "DELIVERY_SENT",
    "DELIVERY_FAILED",
    "DELIVERY_STATES",
    "REASON_STATE_NOT_BLOCKING",
    "REASON_COORDINATOR_OWNED",
    "REASON_MAIN_OWNED_STATE",
    "REASON_SURFACE_NOT_VERIFIED",
    "REASON_UNKNOWN_ACTIONABILITY",
    "REASON_OWNER_NOT_DELEGATED",
    "REASON_DELIVERY_NOT_CONFIRMED",
    "REASON_DELIVERY_FAILED",
    "REASON_NO_CALLBACK_EXPECTATION",
    "REASON_CALLBACK_OVERDUE",
    "REASON_DELEGATED_VERIFIED",
    "REASON_WAIT_OWNER_NOT_EXTERNAL",
    "REASON_NO_UNBLOCK_CONDITION",
    "REASON_WAIT_STALLED",
    "REASON_WAIT_VERIFIED",
    "ActionabilityClaim",
    "ActionabilityVerdict",
    "resolve_actionability",
)
