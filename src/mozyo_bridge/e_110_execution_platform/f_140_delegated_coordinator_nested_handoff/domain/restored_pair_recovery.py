"""Pure contract for an active sublane pair damaged by process restoration (#15227).

The failure this model names is deliberately narrower than "the pane looks odd": both
managed slots still exist, but at least one slot's command-shell working directory no longer
matches the lane's canonical worktree or its startup identity attestation is non-green.  The
repair keeps the lifecycle row, worktree, branch and files, and replaces only the exact pinned
gateway/worker generations.

No live observation is accepted as authority by itself.  A recoverable plan binds the durable
issue/lane lifecycle, canonical worktree token, branch/HEAD, and both inventory generations.
The owner approval digest is made from those immutable fields; health observations only decide
whether a replacement is needed and are intentionally not replay authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from mozyo_bridge.core.state.herdr_identity_attestation import ATTEST_OK
from mozyo_bridge.core.state.replacement_transaction_model import norm
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BUSY,
    RUNTIME_RECEIVER_STATES,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
)

SLOT_GATEWAY = "gateway"
SLOT_WORKER = "worker"
RESTORED_PAIR_SLOT_ROLES = (SLOT_GATEWAY, SLOT_WORKER)

BLOCK_IDENTITY_INCOMPLETE = "identity_incomplete"
BLOCK_DEFAULT_LANE = "default_lane_not_supported"
BLOCK_LIFECYCLE_NOT_CURRENT = "lifecycle_not_current"
BLOCK_WORKTREE_AUTHORITY = "worktree_authority_not_current"
BLOCK_PAIR_INCOMPLETE = "managed_pair_incomplete_or_ambiguous"
BLOCK_SLOT_BUSY = "managed_slot_busy"
BLOCK_SLOT_RUNTIME_NOT_SETTLED = "managed_slot_runtime_not_settled"
BLOCK_ATTESTATION_UNREADABLE = "attestation_store_unreadable"
BLOCK_PAIR_HEALTHY = "managed_pair_already_healthy"
BLOCK_COMPOSER_LOSS_NOT_APPROVED = "pending_composer_loss_not_approved"

STATUS_PREFLIGHT = "preflight"
STATUS_REFUSED = "refused"
STATUS_STOPPED = "stopped"
STATUS_COMPLETED = "completed"


@dataclass(frozen=True)
class RestoredSlot:
    """One exact live inventory generation and its read-only health observations."""

    slot_role: str
    provider: str
    assigned_name: str
    locator: str
    revision: str
    identity_matches: bool
    inventory_generation_matches: bool
    runtime_state: str
    cwd_matches: bool
    attestation_state: str
    attestation_readable: bool = True

    def __post_init__(self) -> None:
        state = self.runtime_state
        if not isinstance(state, str) or state not in RUNTIME_RECEIVER_STATES:
            object.__setattr__(self, "runtime_state", RUNTIME_UNKNOWN)

    @property
    def runtime_busy(self) -> bool:
        return self.runtime_state == RUNTIME_BUSY

    @property
    def runtime_settled(self) -> bool:
        return self.runtime_state in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED)

    @property
    def complete(self) -> bool:
        return bool(
            self.slot_role in RESTORED_PAIR_SLOT_ROLES
            and norm(self.provider)
            and norm(self.assigned_name)
            and norm(self.locator)
            and norm(self.revision)
            and self.identity_matches
            and self.inventory_generation_matches
        )

    @property
    def healthy(self) -> bool:
        return bool(
            self.complete
            and self.runtime_settled
            and self.cwd_matches
            and self.attestation_readable
            and self.attestation_state == ATTEST_OK
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "slot_role": self.slot_role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "revision": self.revision,
            "identity_matches": self.identity_matches,
            "inventory_generation_matches": self.inventory_generation_matches,
            "runtime_state": self.runtime_state,
            "runtime_busy": self.runtime_busy,
            "runtime_settled": self.runtime_settled,
            "cwd_matches": self.cwd_matches,
            "attestation_state": self.attestation_state,
            "attestation_readable": self.attestation_readable,
            "healthy": self.healthy,
        }


@dataclass(frozen=True)
class RestoredPairPlan:
    """One exact, approval-ready recovery plan."""

    issue: str
    lane: str
    workspace_id: str
    worktree_identity: str
    branch: str
    head: str
    lane_revision: str
    lane_generation: str
    lifecycle_current: bool
    worktree_authority_current: bool
    worktree_authority_reason: str
    allow_pending_composer_loss: bool
    gateway: RestoredSlot
    worker: RestoredSlot
    action_generation: int = 1

    @property
    def slots(self) -> Tuple[RestoredSlot, RestoredSlot]:
        return (self.gateway, self.worker)

    @property
    def action_id(self) -> str:
        return restored_pair_action_id(self)

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not all(
            norm(value)
            for value in (
                self.issue,
                self.lane,
                self.workspace_id,
                self.worktree_identity,
                self.branch,
                self.head,
                self.lane_revision,
                self.lane_generation,
            )
        ) or self.action_generation < 1:
            reasons.append(BLOCK_IDENTITY_INCOMPLETE)
        if norm(self.lane) == DEFAULT_LANE:
            reasons.append(BLOCK_DEFAULT_LANE)
        if not self.lifecycle_current:
            reasons.append(BLOCK_LIFECYCLE_NOT_CURRENT)
        if not self.worktree_authority_current:
            reasons.append(BLOCK_WORKTREE_AUTHORITY)
        if (
            not all(slot.complete for slot in self.slots)
            or self.gateway.assigned_name == self.worker.assigned_name
            or self.gateway.locator == self.worker.locator
        ):
            reasons.append(BLOCK_PAIR_INCOMPLETE)
        if any(slot.runtime_busy for slot in self.slots):
            reasons.append(BLOCK_SLOT_BUSY)
        if any(
            slot.complete and not slot.runtime_busy and not slot.runtime_settled
            for slot in self.slots
        ):
            reasons.append(BLOCK_SLOT_RUNTIME_NOT_SETTLED)
        if not all(slot.attestation_readable for slot in self.slots):
            reasons.append(BLOCK_ATTESTATION_UNREADABLE)
        if all(slot.healthy for slot in self.slots):
            reasons.append(BLOCK_PAIR_HEALTHY)
        if not self.allow_pending_composer_loss:
            reasons.append(BLOCK_COMPOSER_LOSS_NOT_APPROVED)
        return tuple(dict.fromkeys(reasons))

    @property
    def may_recover(self) -> bool:
        return not self.blocked_reasons

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "workspace_id": self.workspace_id,
            "worktree_identity": self.worktree_identity,
            "branch": self.branch,
            "head": self.head,
            "lane_revision": self.lane_revision,
            "lane_generation": self.lane_generation,
            "lifecycle_current": self.lifecycle_current,
            "worktree_authority_current": self.worktree_authority_current,
            "worktree_authority_reason": self.worktree_authority_reason,
            "allow_pending_composer_loss": self.allow_pending_composer_loss,
            "action_id": self.action_id,
            "action_generation": self.action_generation,
            "may_recover": self.may_recover,
            "blocked_reasons": list(self.blocked_reasons),
            "slots": [slot.as_payload() for slot in self.slots],
        }


def restored_pair_authority_fields(plan: RestoredPairPlan) -> dict[str, object]:
    """Every immutable field that identifies the approved old pair and checkout."""

    return {
        "allow_pending_composer_loss": plan.allow_pending_composer_loss,
        "branch": plan.branch,
        "gateway_assigned_name": plan.gateway.assigned_name,
        "gateway_locator": plan.gateway.locator,
        "gateway_provider": plan.gateway.provider,
        "gateway_revision": plan.gateway.revision,
        "head": plan.head,
        "lane_generation": plan.lane_generation,
        "lane_revision": plan.lane_revision,
        "worktree_identity": plan.worktree_identity,
        "worker_assigned_name": plan.worker.assigned_name,
        "worker_locator": plan.worker.locator,
        "worker_provider": plan.worker.provider,
        "worker_revision": plan.worker.revision,
    }


def restored_pair_action_id(plan: RestoredPairPlan) -> str:
    """Deterministic id for the exact old pair; health snapshots are not replay authority."""

    fields = restored_pair_authority_fields(plan)
    encoded = "\n".join(
        [f"issue\t{norm(plan.issue)}", f"lane\t{norm(plan.lane)}"]
        + [f"{key}\t{str(fields[key]).lower() if isinstance(fields[key], bool) else fields[key]}"
           for key in sorted(fields)]
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"restored-pair:{digest}"


def restored_pair_approval_operation(plan: RestoredPairPlan) -> Mapping[str, object]:
    """Operation payload consumed by the shared strict owner-approval digest."""

    return {
        "action_id": plan.action_id,
        "action_generation": plan.action_generation,
        **restored_pair_authority_fields(plan),
    }


@dataclass(frozen=True)
class RestoredPairOutcome:
    issue: str
    lane: str
    status: str
    executed: bool
    plan: RestoredPairPlan
    detail: str = ""
    phase: str = ""
    revision: int = 0
    required_approval_marker: str = ""
    conversation_resume_guaranteed: bool = False

    @property
    def is_blocked(self) -> bool:
        return self.executed and self.status != STATUS_COMPLETED

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "status": self.status,
            "executed": self.executed,
            "detail": self.detail,
            "phase": self.phase,
            "revision": self.revision,
            "required_approval_marker": self.required_approval_marker,
            "conversation_resume_guaranteed": self.conversation_resume_guaranteed,
            "plan": self.plan.as_payload(),
        }


__all__ = (
    "BLOCK_ATTESTATION_UNREADABLE",
    "BLOCK_COMPOSER_LOSS_NOT_APPROVED",
    "BLOCK_DEFAULT_LANE",
    "BLOCK_IDENTITY_INCOMPLETE",
    "BLOCK_LIFECYCLE_NOT_CURRENT",
    "BLOCK_PAIR_HEALTHY",
    "BLOCK_PAIR_INCOMPLETE",
    "BLOCK_SLOT_BUSY",
    "BLOCK_SLOT_RUNTIME_NOT_SETTLED",
    "BLOCK_WORKTREE_AUTHORITY",
    "RESTORED_PAIR_SLOT_ROLES",
    "RestoredPairOutcome",
    "RestoredPairPlan",
    "RestoredSlot",
    "SLOT_GATEWAY",
    "SLOT_WORKER",
    "STATUS_COMPLETED",
    "STATUS_PREFLIGHT",
    "STATUS_REFUSED",
    "STATUS_STOPPED",
    "restored_pair_action_id",
    "restored_pair_approval_operation",
    "restored_pair_authority_fields",
)
