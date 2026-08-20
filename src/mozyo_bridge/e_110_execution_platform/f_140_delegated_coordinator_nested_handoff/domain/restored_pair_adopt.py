"""Typed plan / outcome values for the pin-ABSENT restored-pair adopt rail (#15811).

The measured gap (#15811, primary diagnosis on the operator store 2026-08-20): after the
herdr server generation change (#15795) a lane's gateway+worker pair is server-restored,
but the lane's lifecycle row **never declared any pins at all**. That is not record
degradation of the #15774 class — the create path
(:func:`...sublane_create_lifecycle_declaration.declare_created_lane_lifecycle`) writes the
owner row with an EMPTY ``declared_slots`` snapshot by design; pins land only when an
adopt / repair rail observes the live pair. So
:func:`...lane_pin_role.read_declared_pin_pair` reports the typed
:data:`...lane_pin_role.PIN_PAIR_ABSENT` (``declared_pins_absent``), and every existing
rail refuses:

- ``rebind-restored-pair`` (#15656 / #15769) needs an EXACT old pair for its replace-CAS
  and blocks ``declared_slots_unresolved``;
- ``sublane create`` adopt (#13809) needs a startup self-attestation that is still
  generation-bound to the live locator and blocks ``unattested_slot`` (the restore moved
  the terminal), reported by the caller as ``adopt_owner_unbound``;
- ``rehydrate-fleet`` (#15745) and ``retire`` drain both read the current-generation proof
  (:func:`...herdr_launch_generation_binding.verified_terminal_generation_token`), which is
  empty while the launch-generation row still records the pre-restore terminal.

This rail is the pin-absent sibling of ``rebind-restored-pair``. It is **not** a widening
of "recover anything unpinned": the write is a FIRST declaration through the existing
empty-only CAS (:meth:`...lane_declaration.LaneDeclarationStore.backfill_active_binding`),
and it demands a STRICTLY STRONGER proof chain than either neighbour, because it has no
declared pin to check the live slot against:

- the slot is found by DECODING the server-owned ``mzb1`` name in the raw inventory
  (:func:`...sublane_adopt_declaration.select_named_slot_candidate`) — never by matching a
  caller-supplied name — with the reviewed raw-multiplicity / liveness / provider-stamp
  gates unchanged;
- a usable ATTESTED launch-generation row is **required** (never the pre-#15769
  attestation-only fall-through the rebind rail still allows), and it must decode-match the
  name stamp and bind exactly this workspace / role / lane;
- the startup-transaction participant lineage join is required for every slot;
- the recorded self-attestation must match this identity and be either live-joined or the
  #15769 restore-stale signature — a foreign / missing / conflicting record stays refused.

Every blocked reason is a stable token (optionally ``:gateway`` / ``:worker`` suffixed) so
a caller and a regression pin the exact refusal without parsing prose. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# The status vocabulary and the slot-scoping helper are the rebind rail's, imported rather
# than re-declared: one spelling per fact across both restored-pair rails.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    STATUS_PREFLIGHT,
    slot_reason,
)

# -- fail-closed blocked-reason vocabulary (lane-level) ------------------------
# Spelled identically to the rebind rail's tokens wherever the FACT is the same
# (one spelling per fact: an operator reading either rail's refusal sees the same
# word for the same condition). Only genuinely new facts get a new token.

ADOPT_BLOCK_WORKTREE_UNRESOLVED = "worktree_unresolved"
ADOPT_BLOCK_WORKSPACE_UNRESOLVED = "workspace_unresolved"
ADOPT_BLOCK_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
ADOPT_BLOCK_ROW_ABSENT = "lifecycle_row_absent"
ADOPT_BLOCK_NOT_ACTIVE = "lane_not_active"
ADOPT_BLOCK_BINDING_NOT_ISSUE = "binding_not_issue"
ADOPT_BLOCK_ISSUE_MISMATCH = "issue_mismatch"
ADOPT_BLOCK_RELEASE_OPEN = "release_generation_open"
ADOPT_BLOCK_REPLACEMENT_OPEN = "replacement_generation_open"
ADOPT_BLOCK_WORKTREE_UNBOUND = "worktree_unbound"
ADOPT_BLOCK_WORKTREE_IDENTITY_MISMATCH = "worktree_identity_mismatch"
ADOPT_BLOCK_WORKTREE_UNREADABLE = "worktree_unreadable"
ADOPT_BLOCK_BRANCH_DRIFTED = "branch_drifted"
ADOPT_BLOCK_PROVIDER_UNRESOLVED = "provider_unresolved"
ADOPT_BLOCK_INVENTORY_UNREADABLE = "inventory_unreadable"
ADOPT_BLOCK_AMBIGUOUS_LOCATORS = "ambiguous_live_locators"
ADOPT_BLOCK_DECISION_ANCHOR_UNUSABLE = "decision_anchor_unusable"

#: This rail's SUBJECT gate: the row's declared-pin snapshot must be exactly ABSENT.
#: Any other shape — a resolvable pair (nothing to declare), or a NON-EMPTY suspicious one
#: (unreadable / foreign / mixed-vocabulary / duplicate / half a pair) — is refused here.
#: A degraded snapshot is a DIFFERENT defect: overwriting it would destroy the evidence a
#: ``sublane repair-pins`` (#13879) / owner decision needs, and "pin unresolvable" must
#: never read as "recover anything". The reported reason carries the exact
#: :mod:`...lane_pin_role` token (``declared_pins_present:<pin_pair_reason>``).
ADOPT_BLOCK_DECLARED_PINS_PRESENT = "declared_pins_present"

# -- fail-closed blocked-reason vocabulary (per slot, `:gateway` / `:worker`) --
# The candidate-selection tokens are the adopt path's own
# (`sublane_adopt_declaration.ADOPT_DECL_*`), re-exported slot-scoped by the live rail; the
# generation / participant / attestation tokens are the rebind rail's
# (`restored_pair_rebind.REBIND_SLOT_*`). Only this one is new:

#: No usable ATTESTED launch-generation row for the slot (absent, still pending, superseded,
#: or a non-``present`` boot verdict). The rebind rail treats this as the pre-#15769 shape
#: and falls through to its declared-pin evidence; this rail HAS no declared pin, so the
#: server-owned generation row is the only thing that ties the live process to this lane
#: beyond its name — its absence is a hard refusal, never a fall-through.
ADOPT_SLOT_GENERATION_ABSENT = "generation_absent"


@dataclass(frozen=True)
class RestoredPairAdoptRequest:
    """The operator-supplied identity of the lane whose ABSENT pins are declared.

    ``journal`` is an optional durable Redmine journal id carried into the outcome payload
    for the journal write-back; it is never itself an approval gate — the rail's authority
    is the server-owned live restore evidence.
    """

    issue: str
    lane: str
    journal: str = ""


@dataclass(frozen=True)
class AdoptSlotPlan:
    """One resolved slot's observed adopt evidence (display / regression value).

    ``ready`` is True only when every per-slot gate passed; ``reason`` then is empty,
    otherwise a comma-joined list of slot-scoped reason tokens. ``generation_state`` reports
    the launch-generation join for the record: ``live_bound`` (the attested row already binds
    the live values) or ``reattest_needed``.
    """

    slot_role: str
    provider: str = ""
    assigned_name: str = ""
    live_locator: str = ""
    live_runtime_revision: str = ""
    attestation_state: str = ""
    ready: bool = False
    reason: str = ""
    generation_state: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "slot_role": self.slot_role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "live_locator": self.live_locator,
            "live_runtime_revision": self.live_runtime_revision,
            "attestation_state": self.attestation_state,
            "ready": self.ready,
            "reason": self.reason,
            "generation_state": self.generation_state,
        }


@dataclass(frozen=True)
class RestoredPairAdoptPlan:
    """The read-only preflight verdict: adopt evidence plus blocked reasons.

    ``may_adopt`` is True only when EVERY gate passed for BOTH slots. There is no
    single-slot mode: with no declared pin there is no record of what the other half of the
    pair even was, so a half-observed pair could not be declared as a pair at all.

    ``reattest_lineage`` is the durable, journal-ready record of every launch-generation
    re-attest this plan authorizes (the #15769 payload shape, reused verbatim).
    """

    issue: str
    lane: str
    workspace_id: str = ""
    worktree_identity: str = ""
    lane_disposition: str = ""
    revision: int = 0
    lane_generation: int = 0
    blocked_reasons: tuple[str, ...] = ()
    gateway: Optional[AdoptSlotPlan] = None
    worker: Optional[AdoptSlotPlan] = None
    reattest_lineage: tuple[dict, ...] = ()

    @property
    def may_adopt(self) -> bool:
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
            "may_adopt": self.may_adopt,
            "blocked_reasons": list(self.blocked_reasons),
            "gateway": self.gateway.as_payload() if self.gateway else None,
            "worker": self.worker.as_payload() if self.worker else None,
            "reattest_lineage": [dict(entry) for entry in self.reattest_lineage],
        }


@dataclass(frozen=True)
class RestoredPairAdoptOutcome:
    """The command result. ``applied`` is True only for a completed declaration write."""

    issue: str
    lane: str
    status: str
    executed: bool
    plan: RestoredPairAdoptPlan
    applied: bool = False
    revision: Optional[int] = None
    detail: str = ""
    journal: str = ""

    @property
    def is_blocked(self) -> bool:
        if self.status == STATUS_PREFLIGHT:
            return not self.plan.may_adopt
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
    "ADOPT_BLOCK_AMBIGUOUS_LOCATORS",
    "ADOPT_BLOCK_BINDING_NOT_ISSUE",
    "ADOPT_BLOCK_BRANCH_DRIFTED",
    "ADOPT_BLOCK_DECISION_ANCHOR_UNUSABLE",
    "ADOPT_BLOCK_DECLARED_PINS_PRESENT",
    "ADOPT_BLOCK_INVENTORY_UNREADABLE",
    "ADOPT_BLOCK_ISSUE_MISMATCH",
    "ADOPT_BLOCK_LIFECYCLE_UNREADABLE",
    "ADOPT_BLOCK_NOT_ACTIVE",
    "ADOPT_BLOCK_PROVIDER_UNRESOLVED",
    "ADOPT_BLOCK_RELEASE_OPEN",
    "ADOPT_BLOCK_REPLACEMENT_OPEN",
    "ADOPT_BLOCK_ROW_ABSENT",
    "ADOPT_BLOCK_WORKSPACE_UNRESOLVED",
    "ADOPT_BLOCK_WORKTREE_IDENTITY_MISMATCH",
    "ADOPT_BLOCK_WORKTREE_UNBOUND",
    "ADOPT_BLOCK_WORKTREE_UNREADABLE",
    "ADOPT_BLOCK_WORKTREE_UNRESOLVED",
    "ADOPT_SLOT_GENERATION_ABSENT",
    "AdoptSlotPlan",
    "RestoredPairAdoptOutcome",
    "RestoredPairAdoptPlan",
    "RestoredPairAdoptRequest",
    "slot_reason",
)
