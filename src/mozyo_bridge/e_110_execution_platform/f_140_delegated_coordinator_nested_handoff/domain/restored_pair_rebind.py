"""Typed plan / outcome values for the restored-pair lifecycle rebind rail (#15656).

A herdr server restart can restore an active sublane's gateway+worker pair —
the same agent sessions, process-restored onto NEW pane locators — while the
home-scoped lane lifecycle row keeps pinning the OLD locators in
``declared_slots``. The drifted pin then fails the worker-dispatch admission's
``binds_same_generation`` join (`worker_liveness_authority_conflict`) and the
lane is permanently blocked, because no existing rail (recover-stale /
converge-bound-pair / recover-restored-pair / hibernate) repairs an ACTIVE
row's stale pair snapshot from live restart evidence (#15653 j#107710 /
#15656 j#107711 / j#107775).

This module is the pure half of the rail: the request / per-slot plan / plan /
outcome value objects and the closed fail-closed reason vocabulary. Every
blocked reason is a stable token (optionally ``:gateway`` / ``:worker``
suffixed for the slot axis) so a caller and a regression can pin the exact
refusal without parsing prose. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# -- outcome statuses ---------------------------------------------------------

STATUS_PREFLIGHT = "preflight"
STATUS_COMPLETED = "completed"
STATUS_BLOCKED = "blocked"
STATUS_REFUSED = "refused"

# -- fail-closed blocked-reason vocabulary (lane-level) -----------------------

REBIND_BLOCK_WORKTREE_UNRESOLVED = "worktree_unresolved"
REBIND_BLOCK_WORKSPACE_UNRESOLVED = "workspace_unresolved"
REBIND_BLOCK_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
REBIND_BLOCK_ROW_ABSENT = "lifecycle_row_absent"
REBIND_BLOCK_NOT_ACTIVE = "lane_not_active"
REBIND_BLOCK_BINDING_NOT_ISSUE = "binding_not_issue"
REBIND_BLOCK_ISSUE_MISMATCH = "issue_mismatch"
REBIND_BLOCK_RELEASE_OPEN = "release_generation_open"
REBIND_BLOCK_REPLACEMENT_OPEN = "replacement_generation_open"
REBIND_BLOCK_DECLARED_SLOTS_UNRESOLVED = "declared_slots_unresolved"
REBIND_BLOCK_WORKTREE_UNBOUND = "worktree_unbound"
REBIND_BLOCK_WORKTREE_IDENTITY_MISMATCH = "worktree_identity_mismatch"
REBIND_BLOCK_WORKTREE_UNREADABLE = "worktree_unreadable"
REBIND_BLOCK_BRANCH_DRIFTED = "branch_drifted"
REBIND_BLOCK_PROVIDER_UNRESOLVED = "provider_unresolved"
REBIND_BLOCK_INVENTORY_UNREADABLE = "inventory_unreadable"
REBIND_BLOCK_AMBIGUOUS_LOCATORS = "ambiguous_live_locators"
REBIND_BLOCK_DECISION_ANCHOR_UNUSABLE = "decision_anchor_unusable"

# -- fail-closed blocked-reason vocabulary (per slot, `:gateway` / `:worker`) --

REBIND_SLOT_PROVIDER_MISMATCH = "provider_mismatch"
REBIND_SLOT_DUPLICATE_LIVE = "duplicate_live_candidates"
REBIND_SLOT_LIVE_ABSENT = "live_slot_absent"
REBIND_SLOT_LIVE_LOCATOR_UNRESOLVED = "live_locator_unresolved"
REBIND_SLOT_LIVE_PROVIDER_MISMATCH = "live_provider_mismatch"
REBIND_SLOT_NOT_DRIFTED = "locator_not_drifted"
REBIND_SLOT_DECLARED_STILL_LIVE = "declared_locator_still_live"
REBIND_SLOT_UNATTESTED = "unattested_slot"


def slot_reason(token: str, slot_role: str) -> str:
    """The slot-scoped spelling of a per-slot reason token (``token:slot_role``)."""
    return f"{token}:{slot_role}"


@dataclass(frozen=True)
class RestoredPairRebindRequest:
    """The operator-supplied identity of the lane whose pair pins are rebound.

    ``journal`` is an optional durable Redmine journal id the operator records
    the rebind under; it is carried into the outcome payload for the journal
    write-back and is never itself an approval gate — the rail's authority is
    the live restart evidence (attested same-name pair on drifted locators).
    """

    issue: str
    lane: str
    journal: str = ""


@dataclass(frozen=True)
class RebindSlotPlan:
    """One declared slot's observed rebind evidence (display / regression value).

    ``ready`` is True only when every per-slot gate passed; ``reason`` then is
    empty, otherwise a comma-joined list of slot-scoped reason tokens.
    """

    slot_role: str
    provider: str = ""
    assigned_name: str = ""
    declared_locator: str = ""
    live_locator: str = ""
    live_runtime_revision: str = ""
    attestation_state: str = ""
    ready: bool = False
    reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "slot_role": self.slot_role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "declared_locator": self.declared_locator,
            "live_locator": self.live_locator,
            "live_runtime_revision": self.live_runtime_revision,
            "attestation_state": self.attestation_state,
            "ready": self.ready,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RestoredPairRebindPlan:
    """The read-only preflight verdict: rebind evidence plus blocked reasons.

    ``may_rebind`` is True only when EVERY gate passed for BOTH slots
    (all-or-nothing: a half-proven pair never authorizes a partial pin update).
    """

    issue: str
    lane: str
    workspace_id: str = ""
    worktree_identity: str = ""
    lane_disposition: str = ""
    revision: int = 0
    lane_generation: int = 0
    blocked_reasons: tuple[str, ...] = ()
    gateway: Optional[RebindSlotPlan] = None
    worker: Optional[RebindSlotPlan] = None

    @property
    def may_rebind(self) -> bool:
        return not self.blocked_reasons

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "workspace_id": self.workspace_id,
            "worktree_identity": self.worktree_identity,
            "lane_disposition": self.lane_disposition,
            "revision": self.revision,
            "lane_generation": self.lane_generation,
            "may_rebind": self.may_rebind,
            "blocked_reasons": list(self.blocked_reasons),
            "gateway": self.gateway.as_payload() if self.gateway else None,
            "worker": self.worker.as_payload() if self.worker else None,
        }


@dataclass(frozen=True)
class RestoredPairRebindOutcome:
    """The command result. ``applied`` is True only for a completed CAS write."""

    issue: str
    lane: str
    status: str
    executed: bool
    plan: RestoredPairRebindPlan
    applied: bool = False
    revision: Optional[int] = None
    detail: str = ""
    journal: str = ""

    @property
    def is_blocked(self) -> bool:
        if self.status == STATUS_PREFLIGHT:
            return not self.plan.may_rebind
        return not self.applied

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "status": self.status,
            "executed": self.executed,
            "applied": self.applied,
            "revision": self.revision,
            "detail": self.detail,
            "journal": self.journal,
            "is_blocked": self.is_blocked,
            "plan": self.plan.as_payload(),
        }


__all__ = (
    "STATUS_PREFLIGHT",
    "STATUS_COMPLETED",
    "STATUS_BLOCKED",
    "STATUS_REFUSED",
    "REBIND_BLOCK_WORKTREE_UNRESOLVED",
    "REBIND_BLOCK_WORKSPACE_UNRESOLVED",
    "REBIND_BLOCK_LIFECYCLE_UNREADABLE",
    "REBIND_BLOCK_ROW_ABSENT",
    "REBIND_BLOCK_NOT_ACTIVE",
    "REBIND_BLOCK_BINDING_NOT_ISSUE",
    "REBIND_BLOCK_ISSUE_MISMATCH",
    "REBIND_BLOCK_RELEASE_OPEN",
    "REBIND_BLOCK_REPLACEMENT_OPEN",
    "REBIND_BLOCK_DECLARED_SLOTS_UNRESOLVED",
    "REBIND_BLOCK_WORKTREE_UNBOUND",
    "REBIND_BLOCK_WORKTREE_IDENTITY_MISMATCH",
    "REBIND_BLOCK_WORKTREE_UNREADABLE",
    "REBIND_BLOCK_BRANCH_DRIFTED",
    "REBIND_BLOCK_PROVIDER_UNRESOLVED",
    "REBIND_BLOCK_INVENTORY_UNREADABLE",
    "REBIND_BLOCK_AMBIGUOUS_LOCATORS",
    "REBIND_BLOCK_DECISION_ANCHOR_UNUSABLE",
    "REBIND_SLOT_PROVIDER_MISMATCH",
    "REBIND_SLOT_DUPLICATE_LIVE",
    "REBIND_SLOT_LIVE_ABSENT",
    "REBIND_SLOT_LIVE_LOCATOR_UNRESOLVED",
    "REBIND_SLOT_LIVE_PROVIDER_MISMATCH",
    "REBIND_SLOT_NOT_DRIFTED",
    "REBIND_SLOT_DECLARED_STILL_LIVE",
    "REBIND_SLOT_UNATTESTED",
    "slot_reason",
    "RestoredPairRebindRequest",
    "RebindSlotPlan",
    "RestoredPairRebindPlan",
    "RestoredPairRebindOutcome",
)
