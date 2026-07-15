"""Atomic coordinator self-replacement — the pure transaction model (Redmine #13806).

Tranche A of the "1 action generation = 1 durable replacement transaction" design
(Design Answer j#78384, Coordinator Verdict j#78406): the closed vocabularies, the
transition matrices, the typed records, and the participant-manifest / pointer codecs
of the **replacement transaction** component. Deliberately free of SQLite and of any
I/O — the matrices are the *policy*, and
:mod:`mozyo_bridge.core.state.replacement_transaction` is the CAS store that enforces
them durably. A caller may reason about a legal edge, or validate a manifest, without
opening the store.

Why a **new** component rather than another axis on ``lane_lifecycle_records``
(j#78384 §1): the receiver-replacement axis (Redmine #13763) is a single-lane primitive
on the issue-owned lifecycle row. Atomic self-replacement is the *upper* transaction
that binds several participants (a sublane gateway + worker, the default companion, and
the current coordinator itself) and a post-self-close continuation into one owner-approved
generation. It is **session / workspace scoped**, not issue-owned, so it never pushes the
default coordinator into an issue lane's lifecycle row. When a participant *is* an issue
lane it may carry that lane's ``(lane_revision, lane_generation)`` as an immutable pin
(:class:`ParticipantPin`), but this component never writes the #13810 owner row.

Two axes are separate on purpose:

- :data:`PHASE_PLANNED` … — the transaction's own DAG across participants + continuation.
- :data:`PARTICIPANT_CLOSE_OWED` … — how far the exact-generation replacement of ONE
  participant got. ``effect前に次のowed stateをCAS記録する`` (j#78384 §2): each participant
  edge is recorded before the effect so a close-then-crash resumes at ``launch_owed`` and
  never re-closes.

Neither is a liveness fact. Whether a slot still exists is a live-inventory read
(``managed-state-model.md`` ``### 正本境界``); ``old_locator`` on a pin is evidence, not
authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle_model import (
    CAS_ACTION_MISMATCH,
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    norm,
)

# -- transaction phase vocabulary (j#78384 §1) -------------------------------
#
# The fixed DAG the design fixes: read + seal every participant, replace the
# non-self participants, then the default companion, seal the continuation, arm
# the current coordinator's self-close (arm + yield — it never kills itself), and
# only a fresh action-attested coordinator claims and drains. Tranche A owns the
# durable state machine; the actuator that performs the closes / launches / drain
# is tranches B and C.

#: The immutable header is written and every participant is pinned; nothing has
#: been claimed or actuated yet.
PHASE_PLANNED = "planned"
#: A lease holder has claimed the transaction and may drive its effects.
PHASE_CLAIMED = "claimed"
#: The non-self participants (sublane gateway / worker, etc.) are being replaced.
PHASE_REPLACING_NONSELF = "replacing_nonself"
#: Every non-self participant is ``replaced``; the current coordinator is waiting
#: for its own turn to end before it may arm its self-close.
PHASE_AWAITING_SELF_TURN_END = "awaiting_self_turn_end"
#: The current coordinator has armed its self-close and yielded. A process-external
#: executor closes the exact old generation after turn-ended + idle (never a
#: synchronous self-kill).
PHASE_SELF_CLOSE_ARMED = "self_close_armed"
#: A fresh, action-attested coordinator has claimed the continuation.
PHASE_FRESH_COORDINATOR_CLAIMED = "fresh_coordinator_claimed"
#: The fresh coordinator is draining the continuation's semantic action.
PHASE_DRAINING_CONTINUATION = "draining_continuation"
#: Terminal.
PHASE_COMPLETED = "completed"

TRANSACTION_PHASES = frozenset(
    {
        PHASE_PLANNED,
        PHASE_CLAIMED,
        PHASE_REPLACING_NONSELF,
        PHASE_AWAITING_SELF_TURN_END,
        PHASE_SELF_CLOSE_ARMED,
        PHASE_FRESH_COORDINATOR_CLAIMED,
        PHASE_DRAINING_CONTINUATION,
        PHASE_COMPLETED,
    }
)

#: Allowed transaction edges — a strict linear DAG (j#78384 §2 "順序は固定DAG").
#: There are deliberately no self-loops: a resuming lease holder re-reads the current
#: phase and continues from it, so a duplicate transition simply loses the
#: expected-phase guard (:data:`CAS_UNEXPECTED_STATE`) rather than silently re-running.
_TRANSACTION_EDGES: dict[str, frozenset[str]] = {
    PHASE_PLANNED: frozenset({PHASE_CLAIMED}),
    PHASE_CLAIMED: frozenset({PHASE_REPLACING_NONSELF}),
    PHASE_REPLACING_NONSELF: frozenset({PHASE_AWAITING_SELF_TURN_END}),
    PHASE_AWAITING_SELF_TURN_END: frozenset({PHASE_SELF_CLOSE_ARMED}),
    PHASE_SELF_CLOSE_ARMED: frozenset({PHASE_FRESH_COORDINATOR_CLAIMED}),
    PHASE_FRESH_COORDINATOR_CLAIMED: frozenset({PHASE_DRAINING_CONTINUATION}),
    PHASE_DRAINING_CONTINUATION: frozenset({PHASE_COMPLETED}),
    PHASE_COMPLETED: frozenset(),
}

# -- participant phase vocabulary (j#78384 §1 / §2) --------------------------

#: The participant's old receiver close is owed; nothing has been closed yet.
PARTICIPANT_CLOSE_OWED = "close_owed"
#: The old receiver was closed; a fresh slot launch (with action-bound attestation)
#: is owed. Re-drivable to itself: a launch retry that still could not attest is
#: progress-preserving, not a conflict (the ``pending -> pending`` precedent, #13763).
PARTICIPANT_LAUNCH_OWED = "launch_owed"
#: The fresh slot is launched; a final attestation / identity verify is owed.
PARTICIPANT_VERIFY_OWED = "verify_owed"
#: The fresh slot is live, action-attested, and verified. Terminal for the participant.
PARTICIPANT_REPLACED = "replaced"

PARTICIPANT_PHASES = frozenset(
    {
        PARTICIPANT_CLOSE_OWED,
        PARTICIPANT_LAUNCH_OWED,
        PARTICIPANT_VERIFY_OWED,
        PARTICIPANT_REPLACED,
    }
)

#: Allowed participant edges. ``launch_owed`` and ``verify_owed`` self-loop (a retry that
#: could not yet attest / verify is progress-preserving); ``close_owed`` does not (a close
#: is recorded owed→done exactly once, then the launch is owed). ``replaced`` is terminal.
_PARTICIPANT_EDGES: dict[str, frozenset[str]] = {
    PARTICIPANT_CLOSE_OWED: frozenset({PARTICIPANT_LAUNCH_OWED}),
    PARTICIPANT_LAUNCH_OWED: frozenset(
        {PARTICIPANT_LAUNCH_OWED, PARTICIPANT_VERIFY_OWED}
    ),
    PARTICIPANT_VERIFY_OWED: frozenset(
        {PARTICIPANT_VERIFY_OWED, PARTICIPANT_REPLACED}
    ),
    PARTICIPANT_REPLACED: frozenset(),
}

# -- CAS outcome vocabulary (this component's additions) ---------------------
#
# The generic tokens (:data:`CAS_APPLIED` …) are reused from the lane model so a
# duplicate / out-of-order caller is diagnosed with one vocabulary across the state
# store. These three are transaction-specific.

#: A lease op lost because a *different, still-live* holder owns the lease.
CAS_LEASE_CONFLICT = "lease_conflict"
#: A renew / release / effect by a caller that is not the current lease holder.
CAS_LEASE_NOT_HELD = "lease_not_held"
#: A participant transition named a participant this transaction never pinned.
CAS_PARTICIPANT_NOT_FOUND = "participant_not_found"


def transaction_transition_allowed(current: str, target: str) -> bool:
    """Is ``current -> target`` a legal transaction-phase edge? (pure)"""
    return target in _TRANSACTION_EDGES.get(norm(current), frozenset())


def participant_transition_allowed(current: str, target: str) -> bool:
    """Is ``current -> target`` a legal participant-phase edge? (pure)"""
    return target in _PARTICIPANT_EDGES.get(norm(current), frozenset())


# -- cross-axis ordering (Redmine #13806 R1-F1) ------------------------------
#
# The two axes are not independent counters: the design's fixed DAG (j#78384 §2 /
# Verdict j#78406 "current coordinator は最後") means a transaction phase and its
# participants' owed phases constrain each other. The store enforces these on the one
# locked row so the durable state machine cannot represent an unsafe state — e.g.
# ``completed`` while a participant is still ``close_owed``, or the self coordinator
# replaced before the non-self participants.

#: The only transaction phase during which a NON-self participant may be actuated
#: (its replacement happens in step 2, "replace non-self"). Before it the plan is not
#: yet claimed/started; after it the ``awaiting_self_turn_end`` gate has already
#: asserted every non-self participant is ``replaced``.
_NON_SELF_ACTUATION_PHASES = frozenset({PHASE_REPLACING_NONSELF})
#: The only transaction phase during which the SELF (current coordinator) participant may be
#: actuated — inside its own armed self-close window (Redmine #13806 R2-F1). The old self is
#: closed, the fresh slot launched, and the fresh coordinator attested all while
#: ``self_close_armed``; ``-> fresh_coordinator_claimed`` then *requires* the self to be
#: ``replaced`` (:func:`transaction_phase_prerequisite_met`), so the self is never carried
#: un-replaced into or past ``fresh_coordinator_claimed``.
_SELF_ACTUATION_PHASES = frozenset({PHASE_SELF_CLOSE_ARMED})


def _all_replaced(pins: Sequence["ParticipantPin"], *, non_self_only: bool) -> bool:
    for pin in pins:
        if non_self_only and pin.is_self:
            continue
        if pin.phase != PARTICIPANT_REPLACED:
            return False
    return True


def _self_replaced(pins: Sequence["ParticipantPin"]) -> bool:
    """Does a self participant exist AND is it ``replaced``? (pure)

    ``fresh_coordinator_claimed`` is only meaningful once the current coordinator has
    actually been replaced (Redmine #13806 R2-F1): a transaction with no self participant,
    or one whose self is still un-replaced, must not enter it.
    """
    selfs = [p for p in pins if p.is_self]
    return bool(selfs) and all(p.phase == PARTICIPANT_REPLACED for p in selfs)


def transaction_phase_prerequisite_met(
    participants: Sequence["ParticipantPin"], target: str
) -> bool:
    """Is the cross-axis participant prerequisite for a transaction target met? (pure)

    - ``-> awaiting_self_turn_end``: every **non-self** participant must be ``replaced``
      (the coordinator may not stop replacing non-self participants while any is unfinished).
    - ``-> fresh_coordinator_claimed``: the **self** participant must exist and be
      ``replaced`` (Redmine #13806 R2-F1) — a fresh coordinator can only claim once the old
      self is closed, relaunched, and attested, so an un-replaced self is never carried into
      this phase.
    - ``-> completed``: **every** participant, including the self coordinator, must be
      ``replaced``.

    All other edges carry no participant prerequisite. This is what makes ``completed`` with
    a ``close_owed`` participant, a self-close before the non-self participants, or a
    ``fresh_coordinator_claimed`` with an un-replaced self, unrepresentable rather than
    merely discouraged.
    """
    marker = norm(target)
    if marker == PHASE_AWAITING_SELF_TURN_END:
        return _all_replaced(participants, non_self_only=True)
    if marker == PHASE_FRESH_COORDINATOR_CLAIMED:
        return _self_replaced(participants)
    if marker == PHASE_COMPLETED:
        return _all_replaced(participants, non_self_only=False)
    return True


def participant_actuation_phase_allowed(is_self: bool, transaction_phase: str) -> bool:
    """May a participant of this ``is_self`` kind be actuated in this phase? (pure)

    A non-self participant is actuated only while the transaction is ``replacing_nonself``;
    the self participant only inside its armed self-close window (``self_close_armed``, and
    ONLY that phase — Redmine #13806 R2-F1), so the current coordinator is replaced last and
    is never advanced after ``fresh_coordinator_claimed`` (by then it is already ``replaced``,
    the prerequisite for entering that phase).
    """
    phase = norm(transaction_phase)
    if is_self:
        return phase in _SELF_ACTUATION_PHASES
    return phase in _NON_SELF_ACTUATION_PHASES


# -- continuation pointer ----------------------------------------------------


class ContinuationPointerError(ValueError):
    """A continuation pointer is missing / malformed; fail closed."""


@dataclass(frozen=True)
class ContinuationPointer:
    """The durable record a fresh coordinator re-reads to resume after self-close.

    ``(source, issue_id, journal_id)`` names the Redmine anchor exactly as
    :class:`DecisionPointer` does — the pointer's validation is reused so an anchor that
    could never be re-read (a non-ASCII / oversized / zero id, an unknown source) is
    rejected here, not stored. ``expected_gate`` and ``next_semantic_action`` are the
    **closed continuation token**: the gate the fresh coordinator should find and the one
    semantic action it must drive exactly once.

    Deliberately *not* a narrative (j#78384 §1 "continuation narrativeをDBへ複製しない"):
    the body of the continuation stays in Redmine. Only this pointer + the two closed
    tokens live in the DB, so a fresh coordinator always re-reads Redmine to reconstruct
    the work rather than trusting a copied story.
    """

    source: str
    issue_id: str
    journal_id: str
    expected_gate: str
    next_semantic_action: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", norm(self.source))
        object.__setattr__(self, "issue_id", norm(self.issue_id))
        object.__setattr__(self, "journal_id", norm(self.journal_id))
        object.__setattr__(self, "expected_gate", norm(self.expected_gate))
        object.__setattr__(
            self, "next_semantic_action", norm(self.next_semantic_action)
        )
        try:
            # Reuse the decision pointer's positive-decimal / known-source validation
            # rather than re-deriving it: the same "an anchor must be re-readable"
            # contract (R2-F1) governs a continuation pointer.
            DecisionPointer(
                source=self.source,
                issue_id=self.issue_id,
                journal_id=self.journal_id,
            )
        except DecisionPointerError as exc:
            raise ContinuationPointerError(str(exc)) from exc
        if not self.expected_gate:
            raise ContinuationPointerError(
                "a continuation pointer requires a non-empty expected gate"
            )
        if not self.next_semantic_action:
            raise ContinuationPointerError(
                "a continuation pointer requires a non-empty next semantic action token"
            )

    def as_payload(self) -> dict[str, str]:
        return {
            "source": self.source,
            "issue_id": self.issue_id,
            "journal_id": self.journal_id,
            "expected_gate": self.expected_gate,
            "next_semantic_action": self.next_semantic_action,
        }


# -- participant manifest ----------------------------------------------------


class ParticipantPinError(ValueError):
    """A participant pin is unusable; fail closed (never degraded to one fewer)."""


#: The participant-manifest envelope version. Bumped when the pin shape changes so an
#: older build reading a newer manifest fails closed rather than dropping fields.
PARTICIPANTS_VERSION = 1


@dataclass(frozen=True)
class ParticipantPin:
    """One process this transaction will exact-generation replace.

    Identity — ``(lane_id, role, provider, assigned_name)`` — is the stable slot key,
    and ``old_locator`` is the live-generation evidence observed when the transaction was
    planned. All five are required: a pin missing any of them cannot express the identity
    an action-time preflight re-resolves against the live inventory, so it is refused
    rather than stored as an un-actionable participant (the :class:`ReleasePin` R1-F4
    discipline).

    ``is_self`` marks the **current coordinator** — the one participant replaced last, by
    arm-and-yield, never a synchronous self-kill (j#78384 §2). ``lane_revision`` /
    ``lane_generation`` are the OPTIONAL immutable pin of a participant that is an issue
    lane: the #13810 lifecycle ``(revision, generation)`` captured at plan time so a stale
    approval cannot act on a lane the coordinator has since moved. They are **evidence held
    in this manifest**, never a write back to the #13810 owner row (scope item 4). A
    participant that is not an issue lane (the default companion / coordinator) leaves them
    empty.

    ``phase`` is the only mutable field; the store rewrites it under a CAS on the
    transaction revision and never lets identity drift.
    """

    lane_id: str
    role: str
    provider: str
    assigned_name: str
    old_locator: str
    is_self: bool = False
    lane_revision: str = ""
    lane_generation: str = ""
    phase: str = PARTICIPANT_CLOSE_OWED

    def __post_init__(self) -> None:
        for name in (
            "lane_id",
            "role",
            "provider",
            "assigned_name",
            "old_locator",
            "lane_revision",
            "lane_generation",
            "phase",
        ):
            object.__setattr__(self, name, norm(getattr(self, name)))
        object.__setattr__(self, "is_self", bool(self.is_self))
        missing = [
            name
            for name in ("lane_id", "role", "provider", "assigned_name", "old_locator")
            if not getattr(self, name)
        ]
        if missing:
            raise ParticipantPinError(
                "a replacement participant requires a non-empty lane_id / role / provider "
                f"/ assigned_name / old_locator (missing: {', '.join(missing)}); an "
                "unresolvable participant is never pinned"
            )
        if self.phase not in PARTICIPANT_PHASES:
            raise ParticipantPinError(
                f"unknown participant phase {self.phase!r}"
            )

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """The stable ``(lane_id, role, provider, assigned_name)`` participant key."""
        return (self.lane_id, self.role, self.provider, self.assigned_name)

    def with_phase(self, phase: str) -> "ParticipantPin":
        """A copy at ``phase`` — the ONLY mutation the store performs on a pin."""
        return ParticipantPin(
            lane_id=self.lane_id,
            role=self.role,
            provider=self.provider,
            assigned_name=self.assigned_name,
            old_locator=self.old_locator,
            is_self=self.is_self,
            lane_revision=self.lane_revision,
            lane_generation=self.lane_generation,
            phase=phase,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "role": self.role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "old_locator": self.old_locator,
            "is_self": self.is_self,
            "lane_revision": self.lane_revision,
            "lane_generation": self.lane_generation,
            "phase": self.phase,
        }


def validate_participants(
    participants: Sequence[ParticipantPin],
) -> tuple[ParticipantPin, ...]:
    """The participants a transaction may plan with (non-empty, unique identity, ≤1 self).

    A transaction with no participant replaces nothing; two pins sharing a
    ``(lane_id, role, provider, assigned_name)`` identity make the manifest ambiguous
    (which locator had to match?); and more than one ``is_self`` participant would make the
    "replace the current coordinator last" ordering undefined (j#78384 §2). Each is a
    fail-closed refusal, never a silent pick.
    """
    pinned = tuple(participants)
    if not pinned:
        raise ParticipantPinError(
            "a replacement transaction requires at least one participant"
        )
    seen: set[tuple[str, str, str, str]] = set()
    self_count = 0
    for pin in pinned:
        if pin.identity in seen:
            raise ParticipantPinError(
                f"duplicate participant {pin.identity!r} in one replacement transaction"
            )
        seen.add(pin.identity)
        if pin.is_self:
            self_count += 1
    if self_count > 1:
        raise ParticipantPinError(
            "a replacement transaction has at most one self (current coordinator) "
            f"participant, got {self_count}"
        )
    return pinned


def encode_participants(participants: Sequence[ParticipantPin]) -> str:
    """Serialize the participant manifest as a versioned envelope (deterministic).

    Sorted by identity so the row is byte-stable across re-encodes of the same manifest
    (a participant phase-only edit round-trips deterministically).
    """
    pinned = tuple(participants)
    if not pinned:
        return ""
    return json.dumps(
        {
            "version": PARTICIPANTS_VERSION,
            "participants": [
                p.as_payload() for p in sorted(pinned, key=lambda p: p.identity)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def decode_participants(raw: str) -> tuple[ParticipantPin, ...]:
    """Read the participant manifest back. Empty means none; corrupt / unknown **raises**.

    Fail-closed like :func:`...decode_declared_slots`: a malformed or newer-versioned
    manifest must never decode to a *shorter* participant list, which would let a caller
    believe fewer processes were pinned than the transaction records — leaving a dropped
    participant un-replaced. An unreadable manifest is a fail-closed condition, not a
    degraded one.
    """
    if not norm(raw):
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ParticipantPinError(
            f"participant manifest is not readable JSON: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise ParticipantPinError("participant manifest must be a versioned object")
    version = loaded.get("version")
    # An EXACT integer version only (the #13810 R1-F4 closed-schema trap): ``bool`` is an
    # ``int`` subclass, and ``1.0 == 1`` / ``True == 1`` fold in Python, so a JSON
    # ``true`` / ``1.0`` must not pass as v1.
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != PARTICIPANTS_VERSION
    ):
        raise ParticipantPinError(
            f"participant manifest version {version!r} is not exactly "
            f"{PARTICIPANTS_VERSION} (unknown / newer / malformed); fail closed"
        )
    rows = loaded.get("participants")
    if not isinstance(rows, list):
        raise ParticipantPinError("participant manifest has no participant list")
    pins: list[ParticipantPin] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ParticipantPinError(f"participant is not an object: {item!r}")
        pins.append(
            ParticipantPin(
                lane_id=norm(item.get("lane_id")),
                role=norm(item.get("role")),
                provider=norm(item.get("provider")),
                assigned_name=norm(item.get("assigned_name")),
                old_locator=norm(item.get("old_locator")),
                is_self=bool(item.get("is_self")),
                lane_revision=norm(item.get("lane_revision")),
                lane_generation=norm(item.get("lane_generation")),
                phase=norm(item.get("phase")) or PARTICIPANT_CLOSE_OWED,
            )
        )
    return tuple(pins)


# -- transaction record ------------------------------------------------------


@dataclass(frozen=True)
class ReplacementTransactionKey:
    """The transaction unit a row belongs to — workspace + action scoped (j#78384 §1)."""

    workspace_id: str
    action_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", norm(self.workspace_id))
        object.__setattr__(self, "action_id", norm(self.action_id))
        if not self.workspace_id or not self.action_id:
            raise ValueError(
                "a replacement transaction key requires a non-empty "
                "(workspace_id, action_id)"
            )

    def as_row(self) -> tuple[str, str]:
        return (self.workspace_id, self.action_id)


@dataclass(frozen=True)
class ReplacementTransactionRecord:
    """One replacement transaction's durable desired state.

    The header — ``action_generation``, the decision + continuation pointers, and the
    participant *identities* — is immutable after :data:`PHASE_PLANNED` (j#78384 §1). Only
    ``phase``, the per-participant ``phase``, the lease triple, ``revision``, and
    ``updated_at`` move, each under a CAS on the exact ``revision``.
    """

    workspace_id: str
    action_id: str
    action_generation: int
    phase: str
    revision: int
    decision_source: str
    decision_issue_id: str
    decision_journal: str
    continuation_source: str
    continuation_issue_id: str
    continuation_journal: str
    continuation_expected_gate: str
    continuation_next_action: str
    participants_manifest: str
    lease_holder: str = ""
    lease_epoch: int = 0
    lease_expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> ReplacementTransactionKey:
        return ReplacementTransactionKey(self.workspace_id, self.action_id)

    @property
    def participants(self) -> tuple[ParticipantPin, ...]:
        return decode_participants(self.participants_manifest)

    @property
    def decision(self) -> Optional[DecisionPointer]:
        """The stored decision anchor, or ``None`` when it is not re-readable."""
        try:
            return DecisionPointer(
                source=self.decision_source,
                issue_id=self.decision_issue_id,
                journal_id=self.decision_journal,
            )
        except DecisionPointerError:
            return None

    @property
    def continuation(self) -> Optional[ContinuationPointer]:
        """The stored continuation anchor, or ``None`` when it is not re-readable."""
        try:
            return ContinuationPointer(
                source=self.continuation_source,
                issue_id=self.continuation_issue_id,
                journal_id=self.continuation_journal,
                expected_gate=self.continuation_expected_gate,
                next_semantic_action=self.continuation_next_action,
            )
        except ContinuationPointerError:
            return None

    def lease_is_live(self, now: str) -> bool:
        """Is a lease currently held and not yet expired at ``now``? (pure)"""
        if not self.lease_holder or not self.lease_expires_at:
            return False
        return not _expired(self.lease_expires_at, now)

    def find_participant(
        self, identity: tuple[str, str, str, str]
    ) -> Optional[ParticipantPin]:
        """The pinned participant with this identity, or ``None``."""
        for pin in self.participants:
            if pin.identity == identity:
                return pin
        return None

    def as_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "action_id": self.action_id,
            "action_generation": self.action_generation,
            "phase": self.phase,
            "revision": self.revision,
            "decision_source": self.decision_source,
            "decision_issue_id": self.decision_issue_id,
            "decision_journal": self.decision_journal,
            "continuation_source": self.continuation_source,
            "continuation_issue_id": self.continuation_issue_id,
            "continuation_journal": self.continuation_journal,
            "continuation_expected_gate": self.continuation_expected_gate,
            "continuation_next_action": self.continuation_next_action,
            "participants": [p.as_payload() for p in self.participants],
            "lease_holder": self.lease_holder,
            "lease_epoch": self.lease_epoch,
            "lease_expires_at": self.lease_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _expired(expires_at: str, now: str) -> bool:
    """Is ``expires_at`` at or before ``now``? Parsed, never string-compared.

    ISO strings only compare lexically when the offset is identical; a lease may be renewed
    by a caller whose clock formats the offset differently, so parse both. An unparseable
    expiry is treated as **already expired** (fail toward re-claimable rather than pinning
    a lease forever on a malformed timestamp).
    """
    try:
        exp = datetime.fromisoformat(norm(expires_at))
        cur = datetime.fromisoformat(norm(now))
    except (TypeError, ValueError):
        return True
    return exp <= cur


__all__ = (
    "PHASE_PLANNED",
    "PHASE_CLAIMED",
    "PHASE_REPLACING_NONSELF",
    "PHASE_AWAITING_SELF_TURN_END",
    "PHASE_SELF_CLOSE_ARMED",
    "PHASE_FRESH_COORDINATOR_CLAIMED",
    "PHASE_DRAINING_CONTINUATION",
    "PHASE_COMPLETED",
    "TRANSACTION_PHASES",
    "PARTICIPANT_CLOSE_OWED",
    "PARTICIPANT_LAUNCH_OWED",
    "PARTICIPANT_VERIFY_OWED",
    "PARTICIPANT_REPLACED",
    "PARTICIPANT_PHASES",
    "PARTICIPANTS_VERSION",
    "CAS_ACTION_MISMATCH",
    "CAS_ALREADY_DECLARED",
    "CAS_APPLIED",
    "CAS_FORBIDDEN_TRANSITION",
    "CAS_GENERATION_MISMATCH",
    "CAS_LEASE_CONFLICT",
    "CAS_LEASE_NOT_HELD",
    "CAS_NOT_FOUND",
    "CAS_PARTICIPANT_NOT_FOUND",
    "CAS_STALE_REVISION",
    "CAS_UNEXPECTED_STATE",
    "CasOutcome",
    "ContinuationPointer",
    "ContinuationPointerError",
    "DecisionPointer",
    "DecisionPointerError",
    "ParticipantPin",
    "ParticipantPinError",
    "ReplacementTransactionKey",
    "ReplacementTransactionRecord",
    "decode_participants",
    "encode_participants",
    "participant_actuation_phase_allowed",
    "participant_transition_allowed",
    "transaction_phase_prerequisite_met",
    "transaction_transition_allowed",
    "validate_participants",
    "norm",
)
