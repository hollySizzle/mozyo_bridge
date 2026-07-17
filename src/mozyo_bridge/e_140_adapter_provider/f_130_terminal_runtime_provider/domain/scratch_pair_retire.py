"""Session-start scratch pair retirement — the pure decision (Redmine #13892).

``herdr session-start`` mints an exact Claude/Codex pair whose only durable identity is
its herdr **assigned name** (``mzb1_<ws>_<role>_<lane>``, :mod:`...domain.herdr_identity`).
It writes **no** lane lifecycle record and no ``lane_metadata`` row, so every existing
retirement surface refuses it structurally:

- ``sublane retire --execute`` cannot attest the target (``lane_owner_unverified`` —
  ``attest_retire_target`` reads ``None``);
- ``--migrate-hibernated-legacy`` / ``--reconcile-hibernated-live`` /
  ``--retire-hibernated-bound`` each require an existing ``hibernated`` row
  (``lane_not_declared`` / ``CAS_UNEXPECTED_STATE``);
- ``sublane recover-pair`` requires a hibernated record **with declared pins**.

There is no convergence path between those contracts, so a scratch pair leaks capacity
forever (live evidence: #13882 j#80060 / j#80066 — a preserved ``dogfood13882`` pair that
no public rail could retire). This module is the pure half of the surface that closes that
gap: the closed verdict vocabulary and the ordered, fail-closed classification a preflight
makes over a *positive-fact* observation of the unit.

Two deliberate divergences from the sibling surfaces, each forced by this ticket's own
acceptance and recorded so a reviewer can challenge them (#13892 j#80483):

1. **Attestation is NOT required.** #13842's ``decide_pair_reconcile`` blocks on
   ``identity_unattested`` because it *writes* generation-bound pins into a lifecycle row.
   A scratch pair has no row and no generation, so attestation is structurally
   unavailable — the #13882 pair this surface exists to retire is live-and-unattested by
   construction (j#80060 §3). Requiring it would make the surface unable to retire the
   only shape it targets: an over-block that reproduces the permanent-stuck defect this
   ticket removes. Identity is instead proven by the assigned name, whose encoding is
   injective and whose uniqueness ``session-start`` itself fails closed on
   (``herdr_session_start`` raises on a duplicate name).
2. **A partial close RESUMES rather than blocks.** #13842 blocks a half pair
   (``pair_incomplete``) because it must re-bind a whole unit. Here a slot closed by a
   prior run is observed absent, and blocking on that would strand every interrupted
   retire permanently — the #13847 R1-F1 lesson. "Exactly two expected managed slots"
   (acceptance 2) constrains the **expected set** (the binding's gateway + worker roles,
   never three, never one), not the observed live count; the observed count may be 2
   (fresh), 1 (partial close), or 0 (already retired). Acceptance 3 requires exactly this,
   and both readings are pinned by tests.

Every gate is a positive fact defaulting to the unsafe (refuse) side, so a missing /
unreadable / ambiguous observation yields **zero-close / zero-write**. The actuation
(``herdr pane close`` of the resolved locators + the durable audit record) is the caller;
this module never reads an inventory, opens a store, or touches a process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

# -- verdict vocabulary (a closed set) -----------------------------------------

#: Every expected slot present in the unit is provably this scratch pair's own, quiescent,
#: and uniquely located. The ONLY state that closes anything.
STATE_GREEN = "green"
#: A positive absence: the inventory is readable and **no** expected slot is live. The
#: idempotent replay of a completed retire — nothing left to close, and nothing to prove
#: beyond the readable zero (a second run lands here).
STATE_ABSENT = "absent"
#: Fail-closed: the retire could not prove it may close. Never closes, never writes.
STATE_BLOCKED = "blocked"

#: The live inventory could not be read. An unobserved unit is never an empty one.
REASON_INVENTORY_UNREADABLE = "inventory_unreadable"
#: The unit HAS a durable lane lifecycle record — this surface's signature is a
#: record-less pair. A recorded lane is the existing surfaces' territory (`--execute`,
#: `--retire-hibernated-bound`, `--reconcile-hibernated-live`, `--migrate-hibernated-legacy`),
#: each of which reviewed its own guards against its own evidence. Refuse rather than
#: generalize a shared predicate over them (`managed-state-model.md`: every surface states
#: its own signature literally and rejects the rest zero-write).
REASON_LANE_RECORD_PRESENT = "lane_record_present"
#: Two or more rows carry the SAME canonical slot (`(workspace_id, lane_id, role)`).
#: herdr assigned names are unique, so this is an ambiguity, not a pair.
REASON_DUPLICATE_INVENTORY = "duplicate_inventory"
#: A managed-scheme occupant that is NOT one of this pair's expected slots sits in the
#: targeted unit. This surface closes only the pair it names; a foreign process is never
#: touched, and "no expected role is live" is NOT "the unit is empty" (#13845 j#80115 F1).
REASON_FOREIGN_INVENTORY_PRESENT = "foreign_inventory_present"
#: An expected slot's row exists but carries no locator, and the liveness contract does
#: not positively call it stale residue. It cannot be closed and must not be read as gone.
REASON_EXPECTED_IDENTITY_UNRESOLVED = "expected_identity_unresolved"
#: The expected set is not exactly this pair's two managed roles (gateway + worker).
REASON_EXPECTED_SET_NOT_PAIR = "expected_set_not_pair"
#: A resolved slot does not decode to this unit's `(workspace, lane, role)`.
REASON_SLOT_FOREIGN = "slot_foreign"
#: A live agent is mid-turn / not idle. Never destroy an in-flight turn.
REASON_AGENT_NOT_IDLE = "agent_not_idle"
#: A live agent holds unsent composer input. Closing it would drop that input.
REASON_PENDING_COMPOSER = "pending_composer"
#: Two expected slots resolved to ONE locator — a recycled / ambiguous target.
REASON_AMBIGUOUS_LOCATOR = "ambiguous_locator"

BLOCKED_REASONS = frozenset(
    {
        REASON_INVENTORY_UNREADABLE,
        REASON_LANE_RECORD_PRESENT,
        REASON_DUPLICATE_INVENTORY,
        REASON_FOREIGN_INVENTORY_PRESENT,
        REASON_EXPECTED_IDENTITY_UNRESOLVED,
        REASON_EXPECTED_SET_NOT_PAIR,
        REASON_SLOT_FOREIGN,
        REASON_AGENT_NOT_IDLE,
        REASON_PENDING_COMPOSER,
        REASON_AMBIGUOUS_LOCATOR,
    }
)

#: The exact number of managed slots a session-start pair declares (gateway + worker).
EXPECTED_PAIR_SIZE = 2


@dataclass(frozen=True)
class ScratchSlotObservation:
    """The action-time facts a preflight observes about ONE expected scratch-pair slot.

    Every field is a **positive** fact defaulting to the unsafe (refuse) side, so a
    missing observation refuses at :func:`decide_scratch_pair_retire`:

    - ``role`` / ``assigned_name`` — the slot's identity, minted by ``encode_assigned_name``;
    - ``candidate_count`` — how many inventory rows carry this exact assigned name. ``0``
      means the slot is **positively absent** (closed by a prior run, or never launched):
      nothing to close, and NOT a block (partial-close replay). ``1`` is the only
      resolvable shape; ``>1`` is an ambiguity;
    - ``locator`` — the transient pane locator of the single candidate. A present slot
      with no locator cannot be closed;
    - ``belongs_to_pair`` — the candidate's decoded identity IS this unit's
      ``(workspace, lane, role)``;
    - ``agent_idle`` — the slot has no in-flight turn (a stale shell residue with no
      detected agent has none by construction);
    - ``composer_settled`` — the slot holds no unsent composer input.
    """

    role: str
    assigned_name: str
    candidate_count: int = 0
    locator: str = ""
    belongs_to_pair: bool = False
    agent_idle: bool = False
    composer_settled: bool = False

    @property
    def absent(self) -> bool:
        """Positively observed as gone (zero rows carry this assigned name)."""
        return self.candidate_count == 0

    @property
    def resolved(self) -> bool:
        """Exactly one inventory row carries this slot's assigned name."""
        return self.candidate_count == 1

    def as_payload(self) -> dict:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "candidate_count": self.candidate_count,
            "locator": self.locator,
            "belongs_to_pair": self.belongs_to_pair,
            "agent_idle": self.agent_idle,
            "composer_settled": self.composer_settled,
        }


@dataclass(frozen=True)
class ScratchPairObservation:
    """The action-time facts a preflight observes about the whole targeted unit.

    ``duplicate_slot_keys`` / ``foreign_names`` / ``unresolved_roles`` come from the RAW
    inventory scan (``expected_slot_rows``), never from the aggregated ``expected_live_slots``
    role-set: that aggregate drops unexpected occupants, duplicate multiplicity, and
    locator-less rows, so an empty aggregate means "no expected role is live" and never
    "the unit is empty" (#13845 review j#80148). This surface needs the strong proposition.
    """

    inventory_readable: bool = False
    lifecycle_record_absent: bool = False
    slots: tuple[ScratchSlotObservation, ...] = field(default_factory=tuple)
    duplicate_slot_keys: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    foreign_names: tuple[str, ...] = field(default_factory=tuple)
    unresolved_roles: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict:
        return {
            "inventory_readable": self.inventory_readable,
            "lifecycle_record_absent": self.lifecycle_record_absent,
            "slots": [slot.as_payload() for slot in self.slots],
            "duplicate_slot_keys": ["/".join(key) for key in self.duplicate_slot_keys],
            "foreign_names": list(self.foreign_names),
            "unresolved_roles": list(self.unresolved_roles),
        }


@dataclass(frozen=True)
class ScratchPairRetireVerdict:
    """The pure verdict: whether the scratch pair may be closed, and what to close."""

    state: str
    reason: str = ""
    detail: str = ""
    close_targets: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """A retire that proved itself: a real close, or a proven idempotent no-op."""
        return self.state in (STATE_GREEN, STATE_ABSENT)

    @property
    def closes(self) -> bool:
        return self.state == STATE_GREEN and bool(self.close_targets)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "close_targets": [
                {"role": role, "locator": locator}
                for role, locator in self.close_targets
            ],
        }


def _blocked(reason: str, detail: str = "") -> ScratchPairRetireVerdict:
    return ScratchPairRetireVerdict(state=STATE_BLOCKED, reason=reason, detail=detail)


def decide_scratch_pair_retire(
    observation: ScratchPairObservation,
    *,
    expected_roles: Sequence[str],
) -> ScratchPairRetireVerdict:
    """Decide whether this record-less scratch pair may be closed. (pure, fail-closed)

    Returns :data:`STATE_GREEN` (with the exact ``(role, locator)`` close targets) only
    when every gate is positively cleared; :data:`STATE_ABSENT` for a readable inventory
    in which no expected slot is live (the idempotent replay); otherwise the first failing
    gate's :data:`STATE_BLOCKED` reason, so the durable record names exactly why nothing
    was closed. No gate defaults to the actuating side.

    Order — each gate refuses a distinct zero-close class, most fundamental first:

    0. the inventory must be readable (an unobserved unit is never an empty one);
    1. the expected set must be exactly this pair's two managed roles;
    2. the unit must have **no** lifecycle record (this surface's signature — a recorded
       lane belongs to the existing retire surfaces, refused zero-write rather than folded
       into a shared predicate);
    3. no duplicate canonical slot (keyed on ``(workspace_id, lane_id, role)``, never on
       ``role`` alone — the shared slot and its legacy twin legitimately share a role,
       #13845 review j#80187 R3-F1). Checked BEFORE the live read: calling a located
       duplicate "live pair present" misnames the problem;
    4. no foreign occupant in the targeted unit (measured independently of the live
       aggregate — foreign-only occupancy reads as "live 0", #13845 review j#80115 F1);
    5. no expected row that is present-but-unlocatable and not positively stale residue;
    6. a positive absence (zero present slots) is the idempotent, already-retired replay;
    7. every PRESENT slot must decode to this unit, be idle, and hold no pending composer.
       Absent slots are skipped, not blocked — that is what makes a partial close
       replayable (#13847 R1-F1);
    8. the present slots' locators must be distinct (two slots at one locator is a
       recycled / ambiguous target).
    """
    if not observation.inventory_readable:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            "the herdr inventory could not be read; an unobserved unit is never an "
            "empty one, so nothing is closed",
        )

    wanted = tuple(expected_roles)
    if len(wanted) != EXPECTED_PAIR_SIZE or len(set(wanted)) != EXPECTED_PAIR_SIZE:
        return _blocked(
            REASON_EXPECTED_SET_NOT_PAIR,
            f"a session-start pair declares exactly {EXPECTED_PAIR_SIZE} distinct managed "
            f"roles (gateway + worker); the binding resolved {list(wanted)}",
        )
    if tuple(slot.role for slot in observation.slots) != wanted:
        return _blocked(
            REASON_EXPECTED_SET_NOT_PAIR,
            "the observed slot set does not correspond one-to-one with the binding's "
            f"expected roles {list(wanted)}",
        )

    if not observation.lifecycle_record_absent:
        return _blocked(
            REASON_LANE_RECORD_PRESENT,
            "the lane unit HAS a durable lifecycle record; this surface retires only "
            "record-less session-start pairs. Use the recorded-lane surfaces "
            "(`sublane retire --execute` / `--retire-hibernated-bound` / "
            "`--reconcile-hibernated-live` / `--migrate-hibernated-legacy`)",
        )

    if observation.duplicate_slot_keys:
        shown = ", ".join("/".join(key) for key in observation.duplicate_slot_keys)
        return _blocked(
            REASON_DUPLICATE_INVENTORY,
            f"more than one inventory row carries the same canonical slot ({shown}); "
            "herdr assigned names are unique, so this is an ambiguity, not a pair",
        )

    if observation.foreign_names:
        return _blocked(
            REASON_FOREIGN_INVENTORY_PRESENT,
            "the targeted unit is occupied by agents this pair does not name "
            f"({', '.join(observation.foreign_names)}); they are never closed, and their "
            "presence means the unit is not this scratch pair's alone",
        )

    if observation.unresolved_roles:
        return _blocked(
            REASON_EXPECTED_IDENTITY_UNRESOLVED,
            "an expected slot is present but carries no locator and is not positively "
            f"stale residue ({', '.join(observation.unresolved_roles)}); it can neither "
            "be closed nor be read as gone",
        )

    present = [slot for slot in observation.slots if not slot.absent]
    if not present:
        # Positive absence over a readable inventory: a completed retire replayed, or a
        # pair that never launched. Idempotent — nothing to close, nothing to write.
        return ScratchPairRetireVerdict(state=STATE_ABSENT)

    for slot in present:
        if not slot.resolved:
            return _blocked(
                REASON_DUPLICATE_INVENTORY,
                f"{slot.candidate_count} rows carry the assigned name "
                f"{slot.assigned_name}; the slot cannot be resolved uniquely",
            )
        if not slot.belongs_to_pair:
            return _blocked(
                REASON_SLOT_FOREIGN,
                f"the row at {slot.assigned_name} does not decode to this unit's "
                f"(workspace, lane, {slot.role}); refusing to close a slot this request "
                "does not name",
            )
        if not slot.locator:
            return _blocked(
                REASON_EXPECTED_IDENTITY_UNRESOLVED,
                f"the resolved row at {slot.assigned_name} carries no locator; it "
                "cannot be closed",
            )
        if not slot.agent_idle:
            return _blocked(
                REASON_AGENT_NOT_IDLE,
                f"the agent at {slot.assigned_name} is not idle / turn-ended; refusing "
                "to destroy an in-flight turn",
            )
        if not slot.composer_settled:
            return _blocked(
                REASON_PENDING_COMPOSER,
                f"the agent at {slot.assigned_name} holds unsent composer input; "
                "closing it would drop that input",
            )

    locators = [slot.locator for slot in present]
    if len(set(locators)) != len(locators):
        return _blocked(
            REASON_AMBIGUOUS_LOCATOR,
            "two expected slots resolved to one locator — a recycled or ambiguous "
            "target; refusing to close",
        )

    return ScratchPairRetireVerdict(
        state=STATE_GREEN,
        close_targets=tuple((slot.role, slot.locator) for slot in present),
    )


__all__ = (
    "STATE_GREEN",
    "STATE_ABSENT",
    "STATE_BLOCKED",
    "REASON_INVENTORY_UNREADABLE",
    "REASON_LANE_RECORD_PRESENT",
    "REASON_DUPLICATE_INVENTORY",
    "REASON_FOREIGN_INVENTORY_PRESENT",
    "REASON_EXPECTED_IDENTITY_UNRESOLVED",
    "REASON_EXPECTED_SET_NOT_PAIR",
    "REASON_SLOT_FOREIGN",
    "REASON_AGENT_NOT_IDLE",
    "REASON_PENDING_COMPOSER",
    "REASON_AMBIGUOUS_LOCATOR",
    "BLOCKED_REASONS",
    "EXPECTED_PAIR_SIZE",
    "ScratchSlotObservation",
    "ScratchPairObservation",
    "ScratchPairRetireVerdict",
    "decide_scratch_pair_retire",
)
