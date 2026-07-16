"""Exact-generation actuation — the pure decision model (Redmine #13806 tranche B).

Tranche B extracts the #13763 exact receiver-replacement primitive into a *generic*
exact-generation actuator over the tranche A replacement transaction
(:mod:`mozyo_bridge.core.state.replacement_transaction`). This module is the pure half:
the closed vocabularies an action-time adapter reports, and the fail-closed decisions the
use case makes from them. Deliberately free of I/O, of the live Herdr inventory, and of the
CAS store — a caller can pin every decision with tests that touch no process and no DB.

The actuator drives ONE non-self participant through its owed progression
``close_owed -> launch_owed -> verify_owed -> replaced`` (the tranche A participant axis),
performing at each owed step an *evidence-gated* effect:

- **close** the exact old process generation — only when the pinned old slot is still that
  exact generation (identity + locator), never a same-name recycled slot;
- **launch** a fresh slot bound to the replacement ``action_id``;
- **verify** the fresh slot's startup attestation actually binds that ``action_id`` — a
  normal name/role/lane attestation alone is NOT completion (j#78384 §1 / §4).

The design's crash-replay rule (j#78384 §2) lives here as a pure decision: a close that
committed but whose owed-state CAS did not (a close-then-crash) resumes to ``launch_owed``
ONLY on the *positive absence* of the old generation with no same-name recycle and the same
action pin — never by re-closing blindly, and never by adopting a recycled slot.
"""

from __future__ import annotations

from typing import Sequence

from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin, norm

#: The canonical default coordinator lane id — the lane the current coordinator (self) and
#: its Claude companion sit in (Redmine #13806 R1-F3). Mirrors the ``DEFAULT_LANE = "default"``
#: the sibling f_140 domain modules (``route_identity_ledger`` / ``workflow_role_authority`` /
#: ``gateway_route_enforcement``) each declare, and the empty-lane normalization those use; a
#: local mirror keeps the actuation order from importing an unrelated routing/role module.
DEFAULT_COORDINATOR_LANE = "default"

# -- old-slot observation (what the adapter sees vs the pinned old generation) ---
#
# The action-time adapter re-resolves the participant's pinned identity
# ``(lane, role, provider, assigned_name)`` + ``old_locator`` against the live inventory
# and reports exactly one of these. ``old_locator`` is evidence, not authority
# (``managed-state-model.md`` ``### 正本境界``): the actuator never trusts a pin as
# liveness, it re-observes.

#: The pinned old generation is still live at its exact identity + locator. A real close
#: is owed (and preservation-gated before it happens).
OLD_SLOT_PRESENT = "present"
#: The pinned old generation is gone AND no different agent took its name/locator — a
#: *positive absence*. Either the close already committed (a close-then-crash resume) or
#: the slot vanished; either way the actuator proceeds to the action-bound launch as a
#: bounded recovery, never re-closing.
OLD_SLOT_ABSENT = "absent"
#: A DIFFERENT agent now occupies the pinned slot's name/locator (a same-name recycle). The
#: approval is stale for this live process — zero actuation. Closing it would kill an
#: unrelated fresh agent (the :class:`ReleasePin` evidence-not-authority rule, j#78384 §4).
OLD_SLOT_RECYCLED = "recycled"
#: The live inventory cannot uniquely resolve the pin (multiple candidates, or unreadable).
#: Never degrade an ambiguous inventory to "absent" (j#78384 §4) — zero actuation.
OLD_SLOT_AMBIGUOUS = "ambiguous"

OLD_SLOT_OBSERVATIONS = frozenset(
    {OLD_SLOT_PRESENT, OLD_SLOT_ABSENT, OLD_SLOT_RECYCLED, OLD_SLOT_AMBIGUOUS}
)

# -- close result (only ever requested when the old slot is PRESENT) ------------

#: The exact old generation was closed.
CLOSE_DONE = "closed"
#: The close could not complete (the actuator stops rather than assuming it did).
CLOSE_ERROR = "error"

CLOSE_RESULTS = frozenset({CLOSE_DONE, CLOSE_ERROR})

# -- launch result --------------------------------------------------------------

#: A fresh slot was launched and its receipt carries the replacement ``action_id``.
LAUNCH_DONE = "launched"
#: The launch could not complete — the participant stays ``launch_owed`` (retryable).
LAUNCH_ERROR = "error"

LAUNCH_RESULTS = frozenset({LAUNCH_DONE, LAUNCH_ERROR})

# -- attestation verdict (does the fresh slot's attestation bind the action?) ----

#: The fresh slot's startup self-attestation is present AND binds the replacement
#: ``action_id`` (and the fresh identity). Only this completes the participant.
ATTEST_BOUND = "bound"
#: No usable attestation yet (a fresh slot still booting). The participant stays
#: ``verify_owed`` and a later actuation retries — never marked replaced.
ATTEST_PENDING = "pending"
#: An attestation exists but does NOT bind the replacement ``action_id`` (or its identity
#: diverges). A normal name/role/lane attestation is not proof of THIS replacement
#: (j#78384 §4) — zero completion, the actuator stops.
ATTEST_MISMATCH = "mismatch"

ATTESTATION_VERDICTS = frozenset({ATTEST_BOUND, ATTEST_PENDING, ATTEST_MISMATCH})

# -- actuation outcome status (closed vocabulary the use case returns) -----------

#: Every non-self participant is ``replaced`` and the transaction is armed at
#: ``self_close_armed`` — the tranche B boundary. The self coordinator's close, the fresh
#: coordinator claim, and the continuation drain are tranche C.
ACTUATION_ARMED = "armed"
#: A coordinator-alive **worker recovery** (Redmine #13806 tranche D) has replaced every
#: non-self participant of a no-self transaction — the stale standard-sublane worker is
#: closed, relaunched, and action-attested. The transaction stays at ``replacing_nonself``
#: (it has no self-close leg); the exactly-once redispatch of the original gate is the
#: recovery use case's next leg. Distinct from ``armed`` so the two flows never conflate.
ACTUATION_RECOVERED = "recovered"
#: The actuator made progress but a participant is not yet complete (e.g. attestation still
#: ``pending``) — a later re-run resumes from the durable owed state.
ACTUATION_IN_PROGRESS = "in_progress"
#: A new close was refused because a preservation signal is standing (dirty diff / running
#: mutation / unrecorded continuation journal / pending approval / identity mismatch /
#: Redmine unreadable). Zero additional close (j#78384 §3).
ACTUATION_PRESERVATION_BLOCKED = "preservation_blocked"
#: The pinned old slot was recycled into a different agent — zero actuation.
ACTUATION_RECYCLED = "recycled"
#: The live inventory could not uniquely resolve the pinned old slot — zero actuation.
ACTUATION_AMBIGUOUS = "ambiguous"
#: A close/launch effect failed; the actuator stops rather than assume the effect.
ACTUATION_EFFECT_FAILED = "effect_failed"
#: The fresh slot's attestation does not bind the replacement action — zero completion.
ACTUATION_ATTESTATION_MISMATCH = "attestation_mismatch"
#: The actuator lost (or never held) the transaction lease — a concurrent holder owns it.
ACTUATION_LEASE_LOST = "lease_lost"
#: The transaction's immutable action generation no longer matches the caller's — a newer
#: authority superseded this plan; zero actuation.
ACTUATION_GENERATION_MISMATCH = "generation_mismatch"
#: There is no such transaction to actuate.
ACTUATION_NOT_FOUND = "not_found"
#: The transaction's participant topology is not a self-replacement: it does not carry
#: exactly one self (current coordinator) participant (Redmine #13806 R1-F2). Zero
#: actuation — an atomic *self* replacement with no (or many) self targets is never driven.
ACTUATION_INVALID_TOPOLOGY = "invalid_topology"

ACTUATION_STATUSES = frozenset(
    {
        ACTUATION_ARMED,
        ACTUATION_RECOVERED,
        ACTUATION_IN_PROGRESS,
        ACTUATION_PRESERVATION_BLOCKED,
        ACTUATION_RECYCLED,
        ACTUATION_AMBIGUOUS,
        ACTUATION_EFFECT_FAILED,
        ACTUATION_ATTESTATION_MISMATCH,
        ACTUATION_LEASE_LOST,
        ACTUATION_GENERATION_MISMATCH,
        ACTUATION_NOT_FOUND,
        ACTUATION_INVALID_TOPOLOGY,
    }
)


# -- pure decisions -------------------------------------------------------------


def new_close_required(observation: str) -> bool:
    """Does the pinned old slot need a genuinely new close? (pure)

    Only when it is still :data:`OLD_SLOT_PRESENT` — the one case a new process close (and
    therefore the preservation fence, j#78384 §3) applies.
    """
    return norm(observation) == OLD_SLOT_PRESENT


def bounded_recovery_available(observation: str) -> bool:
    """May the actuator advance to ``launch_owed`` WITHOUT a new close? (pure)

    Only on a *positive absence* (:data:`OLD_SLOT_ABSENT`): the exact old generation is
    gone and no same-name recycle occurred, so the close either already committed (a
    close-then-crash resume) or the slot vanished. This is bounded recovery, not a new
    close, so it is NOT gated by the preservation fence (j#78384 §3 "既に close 済み
    participant の launch_owed は復旧操作なので継続可"). An ambiguous inventory is never
    degraded to absence, so it is not bounded recovery.
    """
    return norm(observation) == OLD_SLOT_ABSENT


def is_zero_actuation_observation(observation: str) -> bool:
    """Is this observation a fail-closed stop with no actuation? (pure)

    A recycled slot (a different agent) or an ambiguous inventory: never close, never
    adopt, never degrade to absence (j#78384 §4).
    """
    return norm(observation) in (OLD_SLOT_RECYCLED, OLD_SLOT_AMBIGUOUS)


def zero_actuation_status(observation: str) -> str:
    """The actuation status for a zero-actuation observation. (pure)"""
    marker = norm(observation)
    if marker == OLD_SLOT_RECYCLED:
        return ACTUATION_RECYCLED
    if marker == OLD_SLOT_AMBIGUOUS:
        return ACTUATION_AMBIGUOUS
    raise ValueError(f"{observation!r} is not a zero-actuation observation")


def attestation_completes(verdict: str) -> bool:
    """Does this attestation verdict complete the participant (``-> replaced``)? (pure)

    Only :data:`ATTEST_BOUND` — the fresh slot's attestation must bind the replacement
    action id. ``pending`` (still booting) and ``mismatch`` (a normal attestation that does
    not bind THIS action) never complete it (j#78384 §4).
    """
    return norm(verdict) == ATTEST_BOUND


# -- participant topology / actuation order (Redmine #13806 R1-F2 / R1-F3) -------


def self_participants(pins: Sequence[ParticipantPin]) -> tuple[ParticipantPin, ...]:
    """The self (current coordinator) participants of a manifest. (pure)"""
    return tuple(p for p in pins if p.is_self)


def is_self_replacement_topology(pins: Sequence[ParticipantPin]) -> bool:
    """Does the manifest carry EXACTLY ONE self participant? (pure, Redmine #13806 R1-F2)

    An atomic *self* replacement has one current coordinator to replace. Zero (no self
    target) or many (an undefined "replaced last") is not a self-replacement topology and is
    never actuated — the destructive non-self replacement must not run for such a plan.
    """
    return len(self_participants(pins)) == 1


def is_worker_recovery_topology(pins: Sequence[ParticipantPin]) -> bool:
    """Is this a coordinator-alive worker-recovery manifest? (pure, Redmine #13806 tranche D)

    A worker recovery replaces one or more stale standard-sublane workers WITHOUT touching
    the current coordinator, so it carries **zero** self participants and at least one
    non-self participant. Exactly the complement of a self-replacement plan for the
    self axis: the destructive worker-recovery driver refuses any manifest that carries a
    self participant (that is a self-replacement, driven by :func:`is_self_replacement_topology`
    via the actuator's ``run`` / ``drive_self_participant`` path), and refuses an empty
    manifest (nothing to recover).
    """
    pinned = tuple(pins)
    return len(self_participants(pinned)) == 0 and any(not p.is_self for p in pinned)


def is_default_companion(pin: ParticipantPin) -> bool:
    """A NON-self participant sitting in the default coordinator lane. (pure)

    The current coordinator's companion (the Claude beside the Codex self). It is replaced
    *after* the sublane participants and *before* the self (j#78384 §2). Determined purely
    from ``(is_self, lane_id)`` — never a heuristic.
    """
    return (not pin.is_self) and norm(pin.lane_id) == DEFAULT_COORDINATOR_LANE


def nonself_actuation_order(
    pins: Sequence[ParticipantPin],
) -> tuple[ParticipantPin, ...]:
    """The non-self participants in the design's fixed actuation order. (pure)

    j#78384 §2: sublane worker/gateway first, then the default companion, then (last, and
    out of tranche B scope) the self. This orders the NON-self participants: every sublane
    participant (``is_default_companion`` false) before every default companion (true), each
    group deterministic by identity. Replaces the identity-lexical sort that let the
    canonical ``default`` lane id sort ahead of the sublanes (Redmine #13806 R1-F3). The
    classification is a total function of ``(is_self, lane_id)``, so the order is always
    determinable — no lexical guess.
    """
    nonself = [p for p in pins if not p.is_self]
    return tuple(sorted(nonself, key=lambda p: (is_default_companion(p), p.identity)))


__all__ = (
    "OLD_SLOT_PRESENT",
    "OLD_SLOT_ABSENT",
    "OLD_SLOT_RECYCLED",
    "OLD_SLOT_AMBIGUOUS",
    "OLD_SLOT_OBSERVATIONS",
    "CLOSE_DONE",
    "CLOSE_ERROR",
    "CLOSE_RESULTS",
    "LAUNCH_DONE",
    "LAUNCH_ERROR",
    "LAUNCH_RESULTS",
    "ATTEST_BOUND",
    "ATTEST_PENDING",
    "ATTEST_MISMATCH",
    "ATTESTATION_VERDICTS",
    "ACTUATION_ARMED",
    "ACTUATION_RECOVERED",
    "ACTUATION_IN_PROGRESS",
    "ACTUATION_PRESERVATION_BLOCKED",
    "ACTUATION_RECYCLED",
    "ACTUATION_AMBIGUOUS",
    "ACTUATION_EFFECT_FAILED",
    "ACTUATION_ATTESTATION_MISMATCH",
    "ACTUATION_LEASE_LOST",
    "ACTUATION_GENERATION_MISMATCH",
    "ACTUATION_NOT_FOUND",
    "ACTUATION_INVALID_TOPOLOGY",
    "ACTUATION_STATUSES",
    "DEFAULT_COORDINATOR_LANE",
    "attestation_completes",
    "bounded_recovery_available",
    "is_default_companion",
    "is_self_replacement_topology",
    "is_worker_recovery_topology",
    "is_zero_actuation_observation",
    "new_close_required",
    "nonself_actuation_order",
    "self_participants",
    "zero_actuation_status",
)
