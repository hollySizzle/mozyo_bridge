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
    "BLOCK_PIN_CAS_REFUSED", "approval_matches", "convergence_action_id", "slot_digest",
    "worktree_digest",
)
