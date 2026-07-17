"""Authority contract for preparing a pending hibernated bound pair (#13933).

This is intentionally a different gate from ``bound_pair_convergence_approval``.  The
convergence command must continue to preserve every pending composer.  This contract lets an
owner approve discarding only the exact, uncorrelated pending roles of one immutable pair
snapshot so those roles can be relaunched before normal convergence runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from mozyo_bridge.core.state.replacement_transaction_model import norm
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    BoundSlot,
    slot_digest,
    worktree_digest,
)

APPROVAL_GATE = "bound_pair_composer_discard_approval"
APPROVAL_VERSION = "1"
APPROVAL_EFFECT = "discard_exact_pending_composer_then_relaunch"

STATE_ACTIONABLE = "actionable"
STATE_PREPARED = "prepared"
STATE_BLOCKED = "blocked"

BLOCK_IDENTITY_INCOMPLETE = "identity_incomplete"
BLOCK_NOT_BOUND_SIGNATURE = "not_hibernated_released_bound_pins_empty"
BLOCK_INVENTORY_UNREADABLE = "inventory_unreadable"
BLOCK_WORKTREE_UNSAFE = "worktree_unreadable_dirty_or_branch_mismatch"
BLOCK_PAIR_AMBIGUOUS = "pair_ambiguous_or_foreign"
BLOCK_NO_DISCARDABLE_COMPOSER = "no_exact_uncorrelated_pending_composer"
BLOCK_PAIR_PRESERVED = "pair_contains_non_discardable_preserved_slot"
BLOCK_APPROVAL_MISSING = "approval_missing"
BLOCK_APPROVAL_MISMATCH = "approval_mismatch"
BLOCK_TRANSACTION_CONFLICT = "replacement_transaction_conflict"
BLOCK_REPLACEMENT_STOPPED = "replacement_stopped"

# ---------------------------------------------------------------------------
# Typed resume diagnostics (Redmine #13933 j#81046 Decision 2).
#
# The preflight declines to resume for four genuinely different reasons.  Reporting all of
# them as a bare ``resuming=false`` / ``action_id=null`` left an operator unable to tell a
# missing credential from a pair that simply has no in-flight action -- the exact ambiguity
# that made #13846 j#81024 unreadable.  Each outcome now names which one it was.
# ---------------------------------------------------------------------------
#: The approval source could not be read at all (credential / network / journal).  The
#: failure TYPE is reported; its message is not, so no credential text can escape.
RESUME_APPROVAL_UNREADABLE = "approval_source_unreadable"
#: The journal holds no self-consistent approval marker for this issue+lane.
RESUME_NO_OWNING_APPROVAL = "no_matching_approval_marker"
#: The approval resolves, but re-observing under its action id changes nothing: no action
#: owns progress on this pair, so the original block is the truth.
RESUME_NO_OWNED_PROGRESS = "no_action_owned_progress"
#: The action owns progress, yet the projected pair is still blocked on its own merits.
#: Suffixed with the projected block reason -- a typed token, never a row value.
RESUME_PROJECTED_BLOCKED = "projected_still_blocked"
#: A replay this action owns was adopted.
RESUME_ADOPTED = "adopted"


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_roles(roles: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({norm(role) for role in roles if norm(role)}))


def roles_token(roles: Sequence[str]) -> str:
    return ",".join(canonical_roles(roles))


def preparation_action_id(
    *,
    issue: str,
    lane: str,
    revision: int,
    generation: int,
    worktree_hash: str,
    slots_hash: str,
    discard_roles: Sequence[str],
) -> str:
    values = (
        norm(issue),
        norm(lane),
        str(revision),
        str(generation),
        norm(worktree_hash),
        norm(slots_hash),
        roles_token(discard_roles),
    )
    if any(not value for value in values) or revision < 0 or generation < 1:
        raise ValueError("bound-pair preparation requires an exact immutable pair snapshot")
    return "prepare-bound-pair-" + _digest(values)


@dataclass(frozen=True)
class PreparationExpectation:
    issue: str
    lane: str
    revision: int
    generation: int
    action_generation: int
    action_id: str
    worktree_digest: str
    slot_digest: str
    discard_roles: tuple[str, ...]

    def marker_fields(self) -> dict[str, str]:
        return {
            "gate": APPROVAL_GATE,
            "version": APPROVAL_VERSION,
            "approval_source": "direct_owner",
            "decision": "approved",
            "effect": APPROVAL_EFFECT,
            "issue": norm(self.issue),
            "lane": norm(self.lane),
            "revision": str(self.revision),
            "generation": str(self.generation),
            "action_generation": str(self.action_generation),
            "action_id": norm(self.action_id),
            "worktree_digest": norm(self.worktree_digest),
            "slot_digest": norm(self.slot_digest),
            "discard_roles": roles_token(self.discard_roles),
        }

    def marker(self) -> str:
        fields = self.marker_fields()
        order = (
            "gate", "version", "approval_source", "decision", "effect", "issue",
            "lane", "revision", "generation", "action_generation", "action_id",
            "worktree_digest", "slot_digest", "discard_roles",
        )
        return "[mozyo:workflow-event:" + ":".join(
            f"{key}={fields[key]}" for key in order
        ) + "]"

    def self_consistent(self) -> bool:
        try:
            expected = preparation_action_id(
                issue=self.issue,
                lane=self.lane,
                revision=self.revision,
                generation=self.generation,
                worktree_hash=self.worktree_digest,
                slots_hash=self.slot_digest,
                discard_roles=self.discard_roles,
            )
        except ValueError:
            return False
        return expected == norm(self.action_id) and self.action_generation >= 1


def approval_matches(fields: Mapping[str, str], expected: PreparationExpectation) -> bool:
    want = expected.marker_fields()
    return all(norm(fields.get(key, "")) == value for key, value in want.items())


def expectation_for(
    *,
    issue: str,
    lane: str,
    revision: int,
    generation: int,
    resolved_worktree: str,
    worktree_identity: str,
    branch: str,
    slots: Sequence[BoundSlot],
    discard_roles: Sequence[str],
) -> PreparationExpectation:
    wt_hash = worktree_digest(
        resolved_path=resolved_worktree,
        identity=worktree_identity,
        branch=branch,
    )
    slots_hash = slot_digest(slots)
    roles = canonical_roles(discard_roles)
    return PreparationExpectation(
        issue=issue,
        lane=lane,
        revision=revision,
        generation=generation,
        action_generation=1,
        action_id=preparation_action_id(
            issue=issue,
            lane=lane,
            revision=revision,
            generation=generation,
            worktree_hash=wt_hash,
            slots_hash=slots_hash,
            discard_roles=roles,
        ),
        worktree_digest=wt_hash,
        slot_digest=slots_hash,
        discard_roles=roles,
    )


__all__ = (
    "APPROVAL_GATE", "APPROVAL_VERSION", "APPROVAL_EFFECT",
    "STATE_ACTIONABLE", "STATE_PREPARED", "STATE_BLOCKED",
    "BLOCK_IDENTITY_INCOMPLETE", "BLOCK_NOT_BOUND_SIGNATURE",
    "BLOCK_INVENTORY_UNREADABLE", "BLOCK_WORKTREE_UNSAFE", "BLOCK_PAIR_AMBIGUOUS",
    "BLOCK_NO_DISCARDABLE_COMPOSER", "BLOCK_PAIR_PRESERVED", "BLOCK_APPROVAL_MISSING",
    "BLOCK_APPROVAL_MISMATCH", "BLOCK_TRANSACTION_CONFLICT", "BLOCK_REPLACEMENT_STOPPED",
    "RESUME_APPROVAL_UNREADABLE", "RESUME_NO_OWNING_APPROVAL", "RESUME_NO_OWNED_PROGRESS",
    "RESUME_PROJECTED_BLOCKED", "RESUME_ADOPTED",
    "PreparationExpectation", "approval_matches", "canonical_roles", "expectation_for",
    "preparation_action_id", "roles_token",
)
