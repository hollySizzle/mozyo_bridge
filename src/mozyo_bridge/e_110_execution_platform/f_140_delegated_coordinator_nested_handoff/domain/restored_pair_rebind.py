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
#: The single name-matched row is a positively-signalled shell residue
#: (:func:`...herdr_slot_liveness.classify_named_slot` != SLOT_LIVE): a blank
#: detected-agent field or an unknown runtime status with no detected agent.
#: Liveness is a REQUIRED conjunct independent of the attestation join — a
#: restore can leave the locator / terminal identity and the stored attestation
#: intact around a dead shell (#15656 review j#107780 finding_1).
REBIND_SLOT_STALE = "stale_named_slot"
REBIND_SLOT_LIVE_LOCATOR_UNRESOLVED = "live_locator_unresolved"
REBIND_SLOT_LIVE_PROVIDER_MISMATCH = "live_provider_mismatch"
REBIND_SLOT_NOT_DRIFTED = "locator_not_drifted"
REBIND_SLOT_DECLARED_STILL_LIVE = "declared_locator_still_live"
REBIND_SLOT_UNATTESTED = "unattested_slot"

# -- #15769 restored-terminal re-attest vocabulary (per slot) ------------------
#: The server-owned identity join for the launch-generation re-attest could not
#: be established exactly: the live name does not decode to the generation row's
#: recorded workspace/role/lane, the generation row's identity is foreign to the
#: expected slot, or the live terminal identity is not uniquely resolvable from
#: the canonical inventory snapshot. Never a guess (#15769 j#108766).
REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED = "live_identity_join_failed"
#: The launch-generation store exists but cannot be read; an unreadable
#: authority is never folded into "no generation" while a re-attest is decided.
REBIND_SLOT_GENERATION_UNREADABLE = "generation_unreadable"
#: The declared pin is not drifted AND the attested generation row already
#: binds the live terminal + locator: there is nothing to re-attest.
REBIND_SLOT_TERMINAL_UNCHANGED = "terminal_unchanged_noop"
#: The generation row's locator moved, but the startup-transaction participant
#: that must be re-pinned alongside it could not be resolved exactly (unreadable
#: fence, wrong phase, closed / foreign / already-diverged participant).
REBIND_SLOT_PARTICIPANT_REPIN_UNRESOLVED = "participant_repin_unresolved"
#: Single-slot mode only (#15769): this slot has NO live named row at all. It is
#: a typed per-slot fact carried on the slot plan — under
#: ``allow_single_slot`` it does not block the other slot's re-attest.
REBIND_SLOT_MISSING_LIVE = "missing_live_slot"


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

    ``allow_single_slot`` (#15769) admits resolving ONE slot when the pair's
    other slot has no live named row at all: the missing slot is reported as
    the typed per-slot fact :data:`REBIND_SLOT_MISSING_LIVE` (its declared pin
    stays byte-unchanged) instead of refusing the whole pair. It widens only
    the pair-completeness rule — every identity / liveness / attestation gate
    on the RESOLVED slot is unchanged.
    """

    issue: str
    lane: str
    journal: str = ""
    allow_single_slot: bool = False


@dataclass(frozen=True)
class RebindSlotPlan:
    """One declared slot's observed rebind evidence (display / regression value).

    ``ready`` is True only when every per-slot gate passed; ``reason`` then is
    empty, otherwise a comma-joined list of slot-scoped reason tokens.

    ``skipped`` (#15769) marks a slot that ``allow_single_slot`` excused from
    the pair (no live named row): it is not ready, but its reasons do not block
    the other slot. ``generation_state`` reports the launch-generation join for
    the display / regression record: ``""`` (no usable attested row),
    ``reattest_needed`` (attested row bound to stale terminal / locator), or
    ``live_bound`` (attested row already binds the live values).
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
    skipped: bool = False
    generation_state: str = ""

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
            "skipped": self.skipped,
            "generation_state": self.generation_state,
        }


@dataclass(frozen=True)
class RestoredPairRebindPlan:
    """The read-only preflight verdict: rebind evidence plus blocked reasons.

    ``may_rebind`` is True only when EVERY gate passed for BOTH slots
    (all-or-nothing: a half-proven pair never authorizes a partial pin update).
    Under ``allow_single_slot`` (#15769) a slot with NO live named row is
    ``skipped`` — reported as a typed per-slot fact — and the completeness rule
    applies to the remaining resolved slot.

    ``reattest_lineage`` (#15769) is the durable, journal-ready record of every
    launch-generation re-attest this plan authorizes: one dict per slot with the
    old -> new terminal id and locator, the startup action token, whether a
    participant-side locator re-pin accompanies it, and the server-owned
    evidence conjuncts that held. It rides the structured outcome payload so an
    operator can paste it into the Redmine journal verbatim.
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
    reattest_lineage: tuple[dict, ...] = ()

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
            "reattest_lineage": [dict(entry) for entry in self.reattest_lineage],
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
    "REBIND_SLOT_STALE",
    "REBIND_SLOT_LIVE_LOCATOR_UNRESOLVED",
    "REBIND_SLOT_LIVE_PROVIDER_MISMATCH",
    "REBIND_SLOT_NOT_DRIFTED",
    "REBIND_SLOT_DECLARED_STILL_LIVE",
    "REBIND_SLOT_UNATTESTED",
    "REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED",
    "REBIND_SLOT_GENERATION_UNREADABLE",
    "REBIND_SLOT_TERMINAL_UNCHANGED",
    "REBIND_SLOT_PARTICIPANT_REPIN_UNRESOLVED",
    "REBIND_SLOT_MISSING_LIVE",
    "slot_reason",
    "RestoredPairRebindRequest",
    "RebindSlotPlan",
    "RestoredPairRebindPlan",
    "RestoredPairRebindOutcome",
)
