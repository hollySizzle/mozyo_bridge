"""Pure contract for hibernated bound-pair convergence (Redmine #13933).

The convergence rail is deliberately separate from ``repair-pins`` and ``recover-pair``.
It binds one owner approval to one hibernated lifecycle generation, one worktree token and
one action-time pair inventory.  No stale locator is ever promoted to a declared pin: pins
may only be written from the final, unique, live and attested pair.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from mozyo_bridge.core.state.replacement_transaction_model import norm

APPROVAL_GATE = "bound_pair_convergence_approval"
APPROVAL_VERSION = "1"
APPROVAL_DECISION = "approved"
APPROVAL_EFFECT = "replace_bad_pair_then_repair_pins"

STATE_ACTIONABLE = "actionable"
STATE_ALREADY_CONVERGED = "already_converged"
STATE_BLOCKED = "blocked"

BLOCK_APPROVAL_MISSING = "approval_missing"
BLOCK_APPROVAL_MISMATCH = "approval_mismatch"
BLOCK_IDENTITY_INCOMPLETE = "identity_incomplete"
BLOCK_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
BLOCK_NOT_BOUND_SIGNATURE = "not_hibernated_released_bound_pins_empty"
BLOCK_INVENTORY_UNREADABLE = "inventory_unreadable"
BLOCK_PAIR_AMBIGUOUS = "pair_ambiguous_or_foreign"
BLOCK_PAIR_PRESERVED = "pair_contains_preserved_slot"
BLOCK_WORKTREE_UNSAFE = "worktree_unreadable_dirty_or_branch_mismatch"
BLOCK_TRANSACTION_CONFLICT = "replacement_transaction_conflict"
BLOCK_REPLACEMENT_STOPPED = "replacement_stopped"
BLOCK_FRESH_PAIR_UNPROVEN = "fresh_pair_unproven"
BLOCK_PIN_CAS_REFUSED = "pin_cas_refused"

# ---------------------------------------------------------------------------
# Typed bound-signature faults (Redmine #13933 j#81046 Decision 2).
#
# ``BLOCK_NOT_BOUND_SIGNATURE`` is a conjunction of independent axes.  Reporting only the
# collapsed token told an operator that *something* about the row was wrong while naming
# nothing: #13846 j#81024 read the token as proof of a partial-effect defect, and the real
# cause -- a worktree identity mismatch -- stayed invisible for a whole correction round
# (#13933 j#81043).  Each axis therefore gets its own token.  They name the AXIS only; the
# observed row values stay out of the vocabulary and out of the payload.
# ---------------------------------------------------------------------------
#: The lane is not hibernated.
FAULT_NOT_HIBERNATED = "lane_not_hibernated"
#: The row is not issue-bound (``binding_kind``).
FAULT_NOT_ISSUE_BOUND = "binding_not_issue_bound"
#: The row's issue id is not the requested one.
FAULT_ISSUE_MISMATCH = "issue_mismatch"
#: The row is project-scoped, so it is not an exact bound lane.
FAULT_PROJECT_SCOPED = "project_scoped"
#: The row carries no worktree identity at all.
FAULT_IDENTITY_ABSENT = "worktree_identity_absent"
#: The row's worktree identity is not the one the target root derives.
FAULT_IDENTITY_MISMATCH = "worktree_identity_mismatch"
#: The lane's processes are not released.
FAULT_NOT_RELEASED = "process_not_released"
#: A receiver replacement generation is still in flight.
FAULT_REPLACEMENT_UNSETTLED = "replacement_unsettled"
#: Declared pins are present where this rail requires none.
FAULT_PINS_NOT_EMPTY = "pins_not_empty"

BOUND_SIGNATURE_FAULTS = (
    FAULT_NOT_HIBERNATED, FAULT_NOT_ISSUE_BOUND, FAULT_ISSUE_MISMATCH,
    FAULT_PROJECT_SCOPED, FAULT_IDENTITY_ABSENT, FAULT_IDENTITY_MISMATCH,
    FAULT_NOT_RELEASED, FAULT_REPLACEMENT_UNSETTLED, FAULT_PINS_NOT_EMPTY,
)


def bound_signature_detail(faults: Sequence[str]) -> str:
    """Render typed faults as an operator-readable detail (axis names only, no row values)."""
    named = tuple(fault for fault in faults if fault in BOUND_SIGNATURE_FAULTS)
    if not named:
        return ""
    return "bound signature faults: " + ",".join(named)

PLAN_ADMITTED = "admitted"
PLAN_OBSERVATION_UNSAFE = "fresh_observation_unsafe"
PLAN_OBSERVATION_CHANGED = "fresh_observation_changed"
PLAN_APPROVAL_SNAPSHOT_MISMATCH = "approval_snapshot_mismatch"


def _digest(value: object) -> str:
    body = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BoundSlot:
    """One action-time slot.  ``locator`` may be empty only with transaction close proof."""

    role: str
    provider: str
    assigned_name: str
    locator: str
    disposition: str
    close_proven: bool = False

    def canonical(self) -> dict[str, object]:
        return {
            "assigned_name": norm(self.assigned_name),
            "close_proven": bool(self.close_proven),
            "disposition": norm(self.disposition),
            "locator": norm(self.locator),
            "provider": norm(self.provider),
            "role": norm(self.role),
        }


def slot_digest(slots: Sequence[BoundSlot]) -> str:
    # ``close_proven`` is replay metadata, not part of the approved old generation.  A retry
    # after a transaction-recorded close must retain the original approval identity.
    canonical = []
    for slot in slots:
        item = slot.canonical()
        item.pop("close_proven", None)
        canonical.append(item)
    return _digest(sorted(canonical, key=lambda item: str(item["role"])))


def worktree_digest(*, resolved_path: str, identity: str, branch: str) -> str:
    return _digest(
        {
            "branch": norm(branch),
            "identity": norm(identity),
            "resolved_path": str(resolved_path or "").strip(),
        }
    )


def convergence_action_id(
    *, issue: str, lane: str, revision: int, generation: int, slots_digest: str
) -> str:
    parts = (norm(issue), norm(lane), str(revision), str(generation), norm(slots_digest))
    if any(not value for value in parts) or revision < 0 or generation < 1:
        raise ValueError("bound-pair convergence requires exact issue/lane/revision/generation/slots")
    # Marker values use ':' as their field delimiter, so the action id itself must be one
    # delimiter-safe token.  The clear prefix plus canonical digest is deterministic and still
    # binds every component without escaping ambiguity.
    return "converge-bound-pair-" + _digest(parts)


@dataclass(frozen=True)
class ApprovalExpectation:
    issue: str
    lane: str
    revision: int
    generation: int
    action_generation: int
    action_id: str
    worktree_digest: str
    slot_digest: str

    def marker_fields(self) -> dict[str, str]:
        return {
            "gate": APPROVAL_GATE,
            "version": APPROVAL_VERSION,
            "approval_source": "direct_owner",
            "decision": APPROVAL_DECISION,
            "effect": APPROVAL_EFFECT,
            "issue": norm(self.issue),
            "lane": norm(self.lane),
            "revision": str(self.revision),
            "generation": str(self.generation),
            "action_generation": str(self.action_generation),
            "action_id": norm(self.action_id),
            "worktree_digest": norm(self.worktree_digest),
            "slot_digest": norm(self.slot_digest),
        }

    def marker(self) -> str:
        fields = self.marker_fields()
        ordered = (
            "gate", "version", "approval_source", "decision", "effect", "issue", "lane",
            "revision", "generation", "action_generation", "action_id", "worktree_digest",
            "slot_digest",
        )
        return "[mozyo:workflow-event:" + ":".join(f"{key}={fields[key]}" for key in ordered) + "]"


def approval_matches(fields: Mapping[str, str], expected: ApprovalExpectation) -> bool:
    want = expected.marker_fields()
    return all(norm(fields.get(key, "")) == value for key, value in want.items())


@dataclass(frozen=True)
class TransactionPlanObservation:
    """The complete read-only observation admitted immediately before transaction plan.

    ``slot_digest`` binds the stable pair identity, old locators and per-slot dispositions;
    ``pair_safe`` is the closed-set pair/cardinality decision made from those slots.  The
    remaining fields make lifecycle, worktree and inventory races explicit domain inputs.
    """

    issue: str
    lane: str
    workspace_id: str
    worktree_path: str
    worktree_identity: str
    branch: str
    revision: int
    generation: int
    lifecycle_exact: bool
    pins_empty: bool
    inventory_readable: bool
    worktree_readable: bool
    worktree_clean: bool
    branch_matches: bool
    pair_safe: bool
    slot_digest: str

    def approval_worktree_digest(self) -> str:
        return worktree_digest(
            resolved_path=self.worktree_path,
            identity=self.worktree_identity,
            branch=self.branch,
        )


@dataclass(frozen=True)
class TransactionPlanVerdict:
    allowed: bool
    reason: str
    detail: str = ""


def decide_transaction_plan(
    expected: ApprovalExpectation,
    initial: TransactionPlanObservation,
    fresh: TransactionPlanObservation,
    *,
    transaction_exists: bool,
) -> TransactionPlanVerdict:
    """Admit a transaction plan/resume only from a stable, fully-safe fresh snapshot.

    A first write additionally requires byte-equal equivalence to the owner-approved initial
    snapshot.  A retry may have a progressed (fresh/action-bound) pair, so an already-existing
    immutable transaction does not require the old slot digest; it still requires the complete
    observation to remain stable across the caller read and this plan-boundary re-read.
    """

    for label, observed in (("initial", initial), ("fresh", fresh)):
        if (
            norm(observed.issue) != norm(expected.issue)
            or norm(observed.lane) != norm(expected.lane)
            or observed.revision != expected.revision
            or observed.generation != expected.generation
            or observed.approval_worktree_digest() != expected.worktree_digest
        ):
            return TransactionPlanVerdict(
                False, PLAN_APPROVAL_SNAPSHOT_MISMATCH, f"{label} approval identity changed",
            )
        if not (
            observed.workspace_id
            and observed.worktree_path
            and observed.worktree_identity
            and observed.branch
            and observed.lifecycle_exact
            and observed.pins_empty
            and observed.inventory_readable
            and observed.worktree_readable
            and observed.worktree_clean
            and observed.branch_matches
            and observed.pair_safe
        ):
            return TransactionPlanVerdict(
                False, PLAN_OBSERVATION_UNSAFE, f"{label} full observation is not actionable",
            )
    if initial != fresh:
        return TransactionPlanVerdict(
            False, PLAN_OBSERVATION_CHANGED, "observation changed at transaction boundary",
        )
    if not transaction_exists and (
        initial.slot_digest != expected.slot_digest
        or fresh.slot_digest != expected.slot_digest
    ):
        return TransactionPlanVerdict(
            False,
            PLAN_APPROVAL_SNAPSHOT_MISMATCH,
            "first transaction plan no longer equals the approved old-pair snapshot",
        )
    return TransactionPlanVerdict(True, PLAN_ADMITTED)


@dataclass(frozen=True)
class ConvergenceVerdict:
    state: str
    reason: str = ""
    detail: str = ""
    action_id: str = ""
    approval_marker: str = ""
    slots: tuple[BoundSlot, ...] = ()

    @property
    def actionable(self) -> bool:
        return self.state == STATE_ACTIONABLE

    @property
    def blocked(self) -> bool:
        return self.state == STATE_BLOCKED

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "action_id": self.action_id,
            "approval_marker": self.approval_marker or None,
            "slots": [slot.canonical() for slot in self.slots],
        }


__all__ = (
    "APPROVAL_GATE", "APPROVAL_VERSION", "ApprovalExpectation", "BoundSlot",
    "ConvergenceVerdict", "STATE_ACTIONABLE", "STATE_ALREADY_CONVERGED", "STATE_BLOCKED",
    "BLOCK_APPROVAL_MISSING", "BLOCK_APPROVAL_MISMATCH", "BLOCK_IDENTITY_INCOMPLETE",
    "BLOCK_LIFECYCLE_UNREADABLE", "BLOCK_NOT_BOUND_SIGNATURE", "BLOCK_INVENTORY_UNREADABLE",
    "BLOCK_PAIR_AMBIGUOUS", "BLOCK_PAIR_PRESERVED", "BLOCK_WORKTREE_UNSAFE",
    "BLOCK_TRANSACTION_CONFLICT", "BLOCK_REPLACEMENT_STOPPED", "BLOCK_FRESH_PAIR_UNPROVEN",
    "BLOCK_PIN_CAS_REFUSED", "PLAN_ADMITTED", "PLAN_OBSERVATION_UNSAFE",
    "PLAN_OBSERVATION_CHANGED", "PLAN_APPROVAL_SNAPSHOT_MISMATCH",
    "BOUND_SIGNATURE_FAULTS", "FAULT_NOT_HIBERNATED", "FAULT_NOT_ISSUE_BOUND",
    "FAULT_ISSUE_MISMATCH", "FAULT_PROJECT_SCOPED", "FAULT_IDENTITY_ABSENT",
    "FAULT_IDENTITY_MISMATCH", "FAULT_NOT_RELEASED", "FAULT_REPLACEMENT_UNSETTLED",
    "FAULT_PINS_NOT_EMPTY", "bound_signature_detail",
    "TransactionPlanObservation", "TransactionPlanVerdict", "approval_matches",
    "convergence_action_id", "decide_transaction_plan", "slot_digest", "worktree_digest",
)
