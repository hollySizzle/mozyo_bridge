"""`mozyo-bridge sublane recover-pair` — hibernated exact-pair recovery (Redmine #13847 items 3/4/5).

The public surface a partially-booted hibernated lane needs: ``sublane resume`` reports
``pair_not_attested`` and ``sublane recover-stale`` protects the gateway, so a hibernated
lane whose fresh launch left one or both slots unattested / stale has no public recovery.
This use case provides the replayable, owner-approved recovery of the exact gateway +
worker pair, pinned to the hibernated lifecycle record's exact issue / lane / revision /
generation and its declared pins.

It **composes** already-reviewed pieces rather than reimplementing a transaction core:

1. **preflight (fail-closed)** — resolve the hibernated lifecycle record (hibernated + owns
   this issue), the owner approval (a :class:`DecisionPointer`), and the exact recovery
   action id. Classify EACH slot from a positive-fact observation via the pure
   :func:`decide_slot_recovery`: only a slot that is positively the pair's own stale /
   unattested **bad generation** is recoverable; a productive provider / tool-child, a
   pending composer, a foreign slot, an ambiguous / unreadable identity, and a NEWER
   generation are all preserved (zero-close). The recovery proceeds only when every slot is
   recoverable-or-already-healthy; any preserve disposition blocks (never closing it).
2. **actuation (``--execute``)** — close ONLY the bad-generation slots, byte-preserving and
   pin-matched to the exact declared generation (the #13763 receiver close), then relaunch
   the fresh pair (the herdr actuator heals the closed slots, adopts the healthy one). A
   healthy slot is never closed, so a gateway-only / worker-only failure keeps the good half.
3. **resume** — delegate the both-slots post-hibernate locator-bound attestation verify AND
   the ``hibernated -> active`` disposition CAS to :class:`SublaneResumeUseCase` (its
   survivor-freshness fix — a self-attestation must post-date the hibernation — is exactly
   the "post-hibernate locator-bound attestation" item 4 requires). Resume CAS runs ONLY
   after both slots re-attest.
4. **redispatch** — after the resume CAS applies, redeliver the ORIGINAL
   ``implementation_request`` to the gateway through the existing
   :class:`DispatchOutboxFence` exactly-once (item 5). The fence is the sole idempotency
   authority; a delivery ACK is NEVER promoted to task start / completion.

Every step is replayable: a re-run resolves the same record + action id, skips already-good
slots, and the fence skips an already-delivered redispatch. Default is preflight only;
``--execute`` performs the guarded actuation. The destructive effects are injected through
:class:`HibernatedPairRecoveryOps` so tests drive fakes with no real process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Tuple, runtime_checkable

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E501
    ResumeOutcome,
    ResumeRequest,
    SublaneResumeUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneStartupObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
    EFFECT_CLOSED,
    EFFECT_REDISPATCHED,
    EFFECT_RELAUNCHED,
    EFFECT_RESUME_COMMITTED,
    FATE_REDISPATCH_UNRESOLVED,
    RECOVERY_EFFECTS,
    RECOVERY_UNRESOLVED_FATES,
    validate_effect_contract,
    RedispatchEdgeResult,
    redispatch_is_success,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.lane_worktree_binding_probe import (  # noqa: E501
    resolve_worktree_binding_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_UNKNOWN,
    launch_authority_current,
    launch_authority_runbook,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    SLOT_HEALTHY,
    SLOT_RECOVER,
    SlotRecoveryObservation,
    decide_slot_recovery,
    hibernated_pair_recovery_action_id,
    slot_recovers,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
    recovery_delivery_action_id,
)
from mozyo_bridge.core.state.lane_pin_role import (
    PIN_PAIR_ABSENT,
    PIN_ROLE_GATEWAY,
    PIN_ROLE_WORKER,
    read_declared_pin_pair,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)

# Blocked-reason vocabulary (fail-closed preflight, distinct from the resume vocabulary).
BLOCK_LANE_NOT_HIBERNATED = "lane_not_hibernated"
BLOCK_IDENTITY_INCOMPLETE = "identity_or_decision_incomplete"
BLOCK_STORE_UNREADABLE = "lifecycle_store_unreadable"
BLOCK_MISSING_PINS = "hibernated_record_missing_pins"
#: The lane's lifecycle row carries no canonical ``worktree_identity`` binding, or the one it
#: carries is not this worktree's (Redmine #14475, review j#88477 F1). A recovery that
#: relaunches a pair into a worktree the lane is not bound to — or that is bound to nothing —
#: hands an unbound row on to every later guarded surface, which is exactly how #14462 j#88463
#: reached a closed-and-unrelaunchable gateway. Fail-closed BEFORE any close / relaunch /
#: resume / send. The blocker carries the closed ``LAUNCH_AUTHORITY_*`` axis token.
BLOCK_WORKTREE_BINDING = "lane_worktree_binding_unverified"

BLOCK_SLOT_PRESERVED = "slot_preserved_not_recoverable"  # a slot is preserve-disposition
BLOCK_CLOSE_FAILED = "bad_generation_close_failed"
BLOCK_RELAUNCH_FAILED = "pair_relaunch_failed"
BLOCK_RESUME_REFUSED = "resume_verify_or_cas_refused"

# Redispatch outcome tokens (item 5). A delivery ACK is never a task-start / completion.
REDISPATCH_DELIVERED = "redispatched"  # the fence reserved this call and the send fired
REDISPATCH_ALREADY = "already_redispatched"  # the fence already holds a delivered/reserved row
REDISPATCH_UNCERTAIN = "redispatch_uncertain"  # send fate unknown -> operator reconcile
#: The send was never reached. Review j#88592 F3: this is NOT "the resume did not apply" — the
#: run's own drift re-join (``_binding_drifted`` after the resume) also produces it, so the
#: status co-occurs with an APPLIED resume and the redelivery is then still owed. Whether
#: anything is owed is read off the run's ``effects``, never off this token alone.
REDISPATCH_SKIPPED = "redispatch_not_reached"
REDISPATCH_FAILED = "redispatch_send_failed"
#: The target is inside a retirement transaction, so the reserve is cancelled and no send
#: fires (Redmine #13892 R6-F3). Distinct from `REDISPATCH_SKIPPED` ("the send was never
#: reached"): here the fence WAS reserved and then cancelled — the reserve is what is undone.
REDISPATCH_TARGET_RETIRING = "redispatch_target_retiring"


@dataclass(frozen=True)
class SlotPlan:
    """One slot's role / provider pin and its pure recovery disposition."""

    role: str
    provider: str
    assigned_name: str
    declared_locator: str
    locator: str
    disposition: str

    @property
    def recovers(self) -> bool:
        return slot_recovers(self.disposition)

    def as_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "declared_locator": self.declared_locator,
            "locator": self.locator,
            "disposition": self.disposition,
            "recovers": self.recovers,
        }


@dataclass(frozen=True)
class RecoverPairRequest:
    issue: str
    lane: str
    #: The owner-APPROVAL journal authorizing this destructive recovery — the resume
    #: DecisionPointer / authorization anchor. Distinct from the original request journal
    #: below (Redmine #13847 R1-F3).
    journal: str
    #: The ORIGINAL ``implementation_request`` journal to re-deliver to the gateway. This is
    #: the exactly-once fence key + delivery anchor, so a re-approval (a different approval
    #: journal) never changes the fence key and can never re-send the same original request.
    implementation_request_journal: str


@dataclass(frozen=True)
class RecoverPairDeliveryRetryRequest:
    """One explicit recovery-delivery action for an already-relaunched active pair.

    This does not regenerate the implementation request and does not release the prior
    outbox row.  It binds the same durable work anchor to a new owner-approved action whose
    identity includes the prior pair action and the journal that proved the prior attempt
    was zero-send.
    """

    issue: str
    lane: str
    journal: str
    implementation_request_journal: str
    retry_of_action_id: str
    prior_zero_send_journal: str

    def action_id(self) -> str:
        return recovery_delivery_action_id(
            issue=self.issue,
            lane=self.lane,
            approval_journal=self.journal,
            anchor_journal=self.implementation_request_journal,
            retry_of_action_id=self.retry_of_action_id,
            prior_zero_send_journal=self.prior_zero_send_journal,
        )


@dataclass(frozen=True)
class RecoverPairPreflight:
    """The fail-closed preflight verdict + per-slot plan (pure over the observations)."""

    lane_hibernated: bool
    record_has_pins: bool
    gateway: Optional[SlotPlan]
    worker: Optional[SlotPlan]
    action_id: str
    detail: str = ""
    #: WHY the record named no usable pair, from the canonical pin-role vocabulary boundary
    #: (Redmine #13920): absent, unreadable, or a non-empty row that is foreign / mixed /
    #: duplicate / half a pair. Empty once the pair resolved. Reported so an operator reads
    #: "the pins are there but ambiguous" apart from "there are no pins".
    pins_reason: str = PIN_PAIR_ABSENT
    #: WHICH canonical worktree-binding axis holds / fails, from the closed #14475
    #: ``LAUNCH_AUTHORITY_*`` vocabulary shared with the guarded recovery surfaces (one
    #: vocabulary, not a per-surface dialect). Defaults to the fail-closed ``unknown``, so an
    #: ops adapter that never observed the axis blocks rather than riding a green default.
    worktree_binding_reason: str = LAUNCH_AUTHORITY_UNKNOWN

    @property
    def worktree_binding_current(self) -> bool:
        """Is the lane bound to THIS worktree by a canonical, matching token? (fail-closed)"""
        return launch_authority_current(self.worktree_binding_reason)

    @property
    def worktree_binding_runbook(self) -> str:
        """The secret-safe operator recovery hint for the failing binding axis."""
        return launch_authority_runbook(self.worktree_binding_reason)

    @property
    def slots(self) -> Tuple[SlotPlan, ...]:
        return tuple(s for s in (self.gateway, self.worker) if s is not None)

    @property
    def preserved_slots(self) -> Tuple[SlotPlan, ...]:
        # A slot that is neither recoverable nor already-healthy is a preserve disposition
        # the recovery must NOT close — its presence blocks a clean pair recovery.
        return tuple(
            s
            for s in self.slots
            if not s.recovers and s.disposition != SLOT_HEALTHY
        )

    @property
    def may_recover(self) -> bool:
        return (
            self.lane_hibernated
            and self.record_has_pins
            and len(self.slots) == 2
            and not self.preserved_slots
            # Redmine #14475 (review j#88477 F1): a recovery may not relaunch a pair into a
            # lane whose canonical worktree binding is absent or is some other worktree's.
            and self.worktree_binding_current
        )

    @property
    def blocked_reasons(self) -> Tuple[str, ...]:
        reasons: list[str] = []
        if not self.lane_hibernated:
            reasons.append(BLOCK_LANE_NOT_HIBERNATED)
        if not self.record_has_pins or len(self.slots) != 2:
            # Carry WHY (Redmine #13920): an ambiguous non-empty row and a genuinely pin-less
            # one are both fail-closed, but they are not the same operator problem.
            reasons.append(
                f"{BLOCK_MISSING_PINS}:{self.pins_reason}"
                if self.pins_reason
                else BLOCK_MISSING_PINS
            )
        if not self.worktree_binding_current:
            # Carry WHICH axis (the closed #14475 vocabulary): "bound to nothing" and "bound
            # to a different worktree" are both fail-closed, and are different operator
            # problems with different runbooks.
            reasons.append(f"{BLOCK_WORKTREE_BINDING}:{self.worktree_binding_reason}")
        for slot in self.preserved_slots:
            reasons.append(f"{BLOCK_SLOT_PRESERVED}:{slot.role}={slot.disposition}")
        return tuple(reasons)

    def as_payload(self) -> dict[str, Any]:
        return {
            "may_recover": self.may_recover,
            "lane_hibernated": self.lane_hibernated,
            "record_has_pins": self.record_has_pins,
            "pins_reason": self.pins_reason,
            "worktree_binding_current": self.worktree_binding_current,
            "worktree_binding_reason": self.worktree_binding_reason,
            "worktree_binding_runbook": self.worktree_binding_runbook,
            "action_id": self.action_id,
            "gateway": self.gateway.as_payload() if self.gateway else None,
            "worker": self.worker.as_payload() if self.worker else None,
            "blocked_reasons": list(self.blocked_reasons),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RecoverPairOutcome:
    """The full result: preflight verdict, actuation, resume, and redispatch."""

    executed: bool
    preflight: RecoverPairPreflight
    issue: str
    lane: str
    closed_roles: Tuple[str, ...] = ()
    relaunched: bool = False
    resume: Optional[ResumeOutcome] = None
    redispatch: str = REDISPATCH_SKIPPED
    detail: str = ""
    #: The effects this run is KNOWN to have applied, from the closed :data:`RECOVERY_EFFECTS`
    #: vocabulary (Redmine #14475, reviews j#88554 / j#88563). ``executed`` is its
    #: non-emptiness, so a fully idempotent replay reports no effects rather than a fixed
    #: ``executed=True``.
    effects: Tuple[str, ...] = ()
    #: Whether the guarded actuation was ENTERED at all (past the ``--execute`` admission).
    #: Distinct from :attr:`executed`, which says whether anything was applied: a run can be
    #: attempted and apply nothing (a fence stopped it), and a preflight-only run is neither.
    #: ``is_blocked`` keys off THIS, because "did we act" and "did anything change" are
    #: different questions — conflating them made a first-close failure read as unblocked
    #: (review j#88563 F1/F2).
    attempted: bool = False
    #: Actions whose durable fate could NOT be established, from
    #: :data:`RECOVERY_UNRESOLVED_FATES`. Separate from :attr:`effects` because "attempted,
    #: fate unknown" is not "applied" — reporting it as an effect claims a write that may
    #: never have happened (review j#88563 F1).
    unresolved: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Structural guarantee (review j#88563 F1): an outcome that contradicts itself cannot
        # be constructed, so a future branch cannot reintroduce a fixed ``executed`` or an
        # off-vocabulary token without failing here.
        validate_effect_contract(
            executed=self.executed, effects=self.effects, unresolved=self.unresolved,
            attempted=self.attempted,
        )
    #: Stable reason and locator-free nested startup evidence from a failed relaunch.
    #: These are additive so the historical top-level ``detail=pair_relaunch_failed``
    #: contract remains intact while the exact rollback debt is not discarded.
    relaunch_reason: str = ""
    relaunch_startup: Optional[SublaneStartupObservation] = None

    @property
    def rollback_pointer(self) -> Optional[str]:
        startup = self.relaunch_startup
        if startup is not None and startup.rollback_owed and startup.action_id:
            return (
                "mozyo-bridge herdr session-rollback "
                f"--action-id {startup.action_id}"
            )
        return None

    @property
    def is_blocked(self) -> bool:
        if not self.preflight.may_recover:
            return True
        # Redmine #14475 (review j#88554 F1): a nested resume blocker dominates REGARDLESS of
        # ``executed``. Evaluating "did this run change anything" first meant that making
        # ``executed`` truthful — a run stopped at the commit edge with zero applied effects —
        # silently flipped the outcome to NOT blocked. Effect-count and blocked-ness are
        # independent facts; the order here is what keeps them so.
        if not self.attempted:
            # A preflight-only run neither acted nor failed.
            return False
        if self.resume is None or self.resume.is_blocked:
            # Stopped before the resume (a fence, a close / relaunch failure), or the resume
            # itself refused. Either way the recovery did not complete.
            return True
        # Review j#88563 F2: the terminal classification must be TOTAL. ``target_retiring`` is
        # a reserve-cancelled zero-send — the send did not happen and will not — so it is a
        # blocked outcome, not a silent success. Only a fresh delivery and a proven
        # already-delivered no-op are unblocked; ``skipped`` means an earlier fence stopped the
        # run, which the branches above have already classified.
        # Review j#88571 F2: ONE closed success policy, shared with the retry surface, so the
        # two cannot drift and a future token is blocked by default. ``skipped`` means an
        # earlier fence stopped the run, which the branches above already classified.
        # Review j#88579 F3 / probe j#88577: the shared policy is the ONLY policy. Keeping a
        # local ``skipped`` whitelist made "the resume applied but the send never ran" a
        # silent success on the main surface while the retry surface blocked it.
        return not redispatch_is_success(self.redispatch)

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "executed": self.executed,
            "attempted": self.attempted,
            "effects": list(self.effects),
            "unresolved": list(self.unresolved),
            "issue": self.issue,
            "lane": self.lane,
            "is_blocked": self.is_blocked,
            "closed_roles": list(self.closed_roles),
            "relaunched": self.relaunched,
            "redispatch": self.redispatch,
            "preflight": self.preflight.as_payload(),
            "resume": self.resume.as_payload() if self.resume is not None else None,
            "detail": self.detail,
        }
        if self.relaunch_reason:
            payload["relaunch_reason"] = self.relaunch_reason
        if self.relaunch_startup is not None:
            payload["relaunch_startup"] = self.relaunch_startup.as_payload()
            payload["rollback_pointer"] = self.rollback_pointer
        return payload


@dataclass(frozen=True)
class RecoverPairDeliveryRetryOutcome:
    """Result of the active-pair, new-action delivery recovery surface."""

    executed: bool
    issue: str
    lane: str
    action_id: str = ""
    may_deliver: bool = False
    redispatch: str = REDISPATCH_SKIPPED
    detail: str = ""
    #: Same typed contract as the main recovery (review j#88563 F2): known-applied effects,
    #: unresolved fates, and whether the actuation was entered at all.
    effects: Tuple[str, ...] = ()
    unresolved: Tuple[str, ...] = ()
    attempted: bool = False

    def __post_init__(self) -> None:
        validate_effect_contract(
            executed=self.executed, effects=self.effects, unresolved=self.unresolved,
            attempted=self.attempted,
        )

    @property
    def is_blocked(self) -> bool:
        if not self.may_deliver:
            return True
        if not self.attempted:
            return False
        # Review j#88571 F2: the SAME closed policy the main recovery uses, so the two
        # surfaces cannot drift and an unknown future token is blocked by default.
        return not redispatch_is_success(self.redispatch)

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "attempted": self.attempted,
            "effects": list(self.effects),
            "unresolved": list(self.unresolved),
            "issue": self.issue,
            "lane": self.lane,
            "action_id": self.action_id,
            "may_deliver": self.may_deliver,
            "redispatch": self.redispatch,
            "is_blocked": self.is_blocked,
            "detail": self.detail,
        }


@runtime_checkable
class HibernatedPairRecoveryOps(Protocol):
    """The destructive / observing effects the recovery use case needs (injected)."""

    def workspace_id(self) -> str: ...

    def observe_slot(
        self, *, role: str, provider: str, workspace_id: str, lane: str, record: Any
    ) -> Tuple[SlotRecoveryObservation, str, str]:
        """Classify one slot from the live world.

        Resolves the slot's live pane BY ASSIGNED NAME (the current generation — after a
        failed relaunch the pane is live-but-stale at a CURRENT locator, not the stale
        declared-pin locator), reads its startup self-attestation, and returns
        ``(observation, live_locator, assigned_name)``. The live locator is what a close
        pin-matches (byte-preserving the exact live bad generation), never a stale pin.
        """
        ...

    def lane_worktree_binding_reason(self, *, lane: str, record: Any) -> str:
        """WHICH canonical worktree-binding axis holds for this lane? (read-only, #14475)

        Returns a closed :data:`...lane_launch_authority.LAUNCH_AUTHORITY_REASONS` token:
        :data:`...LAUNCH_AUTHORITY_OK` only when the record's ``worktree_identity`` is
        non-empty AND equals the token freshly derived from the recovery worktree; otherwise
        the exact failing axis (unbound / mismatch / underivable / unreadable). Fail-closed —
        an unreadable observation is never ``ok``.

        This is the SAME token vocabulary (and, in the live adapter, the same derivation) the
        guarded refresh's pre-close launch-authority fence uses, so the two surfaces cannot
        disagree about what "bound to this worktree" means.
        """
        ...

    def close_bad_slot(
        self, *, role: str, provider: str, assigned_name: str, locator: str, action_id: str
    ) -> bool: ...

    def relaunch_pair(self, *, action_id: str, slots: Tuple[SlotPlan, ...]) -> bool: ...

    def redispatch_to_gateway(
        self,
        *,
        action_id: str,
        gateway_assigned_name: str,
        issue: str,
        lane: str,
        journal: str,
        workspace_id: str,
    ) -> RedispatchEdgeResult: ...

    def retry_redispatch_to_gateway(
        self,
        *,
        action_id: str,
        retry_of_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
    ) -> RedispatchEdgeResult: ...

    def preflight_retry_redispatch_to_gateway(
        self,
        *,
        retry_of_action_id: str,
        issue: str,
        lane: str,
        journal: str,
        approval_journal: str,
        prior_zero_send_journal: str,
        workspace_id: str,
    ) -> Tuple[bool, str]: ...


@dataclass
class SublaneRecoverPairUseCase:
    """Owner-approved hibernated exact-pair recovery: classify -> close bad gen -> relaunch
    -> resume (verify + CAS) -> exactly-once redispatch."""

    ops: HibernatedPairRecoveryOps
    store: LaneLifecycleStore
    resume: SublaneResumeUseCase

    def _decision(self, request: RecoverPairRequest) -> Optional[DecisionPointer]:
        try:
            return DecisionPointer(
                source="redmine",
                issue_id=_norm(request.issue),
                journal_id=_norm(request.journal),
            )
        except DecisionPointerError:
            return None

    def _slot_plan(
        self, *, role: str, record: Any, pin: Any, workspace_id: str, lane: str
    ) -> SlotPlan:
        # The provider binding (which provider is gateway vs worker) comes from the declared
        # pin; the live locator + assigned name come from the live observation (the current
        # generation), so a close pin-matches the live bad pane, never a stale declared pin.
        # ``provider`` is read ONLY from the pin's own provider field — never defaulted from
        # its ``role`` (Redmine #13920). Under the legacy spelling that fallback looked
        # harmless because a role read "codex", a real provider id; in the canonical
        # vocabulary it would hand "gateway" to a provider-keyed live lookup and resolve
        # nothing. A decoded pin always carries a non-empty provider (the pin model refuses
        # an empty one), so there is nothing to fall back to.
        provider = _norm(getattr(pin, "provider", ""))
        observation, live_locator, assigned_name = self.ops.observe_slot(
            role=role,
            provider=provider,
            workspace_id=workspace_id,
            lane=lane,
            record=record,
        )
        return SlotPlan(
            role=role,
            provider=provider,
            assigned_name=_norm(assigned_name),
            declared_locator=_norm(getattr(pin, "locator", "")),
            locator=_norm(live_locator),
            disposition=decide_slot_recovery(observation),
        )

    def _worktree_binding_reason(self, *, lane: str, record: Any) -> str:
        """The lane's worktree-binding axis through the fail-closed leaf seam (#14475)."""
        return resolve_worktree_binding_reason(self.ops, lane=lane, record=record)

    def _blocked_preflight(self, *, action_id: str, detail: str) -> RecoverPairPreflight:
        return RecoverPairPreflight(
            lane_hibernated=False,
            record_has_pins=False,
            gateway=None,
            worker=None,
            action_id=action_id,
            detail=detail,
        )

    def run(self, request: RecoverPairRequest, *, execute: bool) -> RecoverPairOutcome:
        issue = _norm(request.issue)
        lane = _norm(request.lane)
        workspace_id = _norm(self.ops.workspace_id())
        decision = self._decision(request)
        if not issue or not lane or not workspace_id or decision is None:
            pf = self._blocked_preflight(
                action_id="", detail="incomplete recovery identity or decision anchor"
            )
            return RecoverPairOutcome(
                executed=False, preflight=pf, issue=issue, lane=lane,
                detail=BLOCK_IDENTITY_INCOMPLETE,
            )

        key = LaneLifecycleKey(workspace_id, lane)
        try:
            rec = self.store.get(key)
        except (LaneLifecycleError, OSError):
            pf = self._blocked_preflight(
                action_id="", detail="lifecycle store unreadable; fail closed"
            )
            return RecoverPairOutcome(
                executed=False, preflight=pf, issue=issue, lane=lane,
                detail=BLOCK_STORE_UNREADABLE,
            )

        lane_hibernated = (
            rec is not None
            and rec.lane_disposition == DISPOSITION_HIBERNATED
            and _norm(rec.issue_id) == issue
        )
        # The exact hibernated generation the recovery pins itself to. An action id can only
        # be built from a fully-specified record; a record missing revision / generation is
        # an under-specified target that fails closed (never an ambiguous recovery).
        action_id = ""
        gateway_plan = worker_plan = None
        record_has_pins = False
        pins_reason = PIN_PAIR_ABSENT
        if lane_hibernated:
            try:
                action_id = hibernated_pair_recovery_action_id(
                    issue=issue,
                    lane_id=lane,
                    revision=str(rec.revision),
                    generation=str(rec.lane_generation),
                )
            except ValueError:
                action_id = ""
            # The ONE boundary that decides which pin is which slot (Redmine #13920). It
            # read-accepts the legacy #13809 spelling, so an adopted-then-hibernated lane
            # resolves instead of reading pin-less; every ambiguous shape (foreign / mixed /
            # duplicate / half a pair) returns a reason and no pins, so the recovery closes
            # and sends nothing on a row whose pins are merely non-empty.
            pair = read_declared_pin_pair(rec)
            pins_reason = pair.reason
            if action_id and pair.ok:
                record_has_pins = True
                gateway_plan = self._slot_plan(
                    role=PIN_ROLE_GATEWAY, record=rec, pin=pair.gateway,
                    workspace_id=workspace_id, lane=lane,
                )
                worker_plan = self._slot_plan(
                    role=PIN_ROLE_WORKER, record=rec, pin=pair.worker,
                    workspace_id=workspace_id, lane=lane,
                )

        preflight = RecoverPairPreflight(
            lane_hibernated=lane_hibernated,
            record_has_pins=record_has_pins,
            gateway=gateway_plan,
            worker=worker_plan,
            action_id=action_id,
            pins_reason=pins_reason,
            worktree_binding_reason=self._worktree_binding_reason(lane=lane, record=rec),
        )
        if not preflight.may_recover or not execute:
            return RecoverPairOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                detail=(
                    "preflight only (no --execute)"
                    if preflight.may_recover
                    else "fail-closed: recovery blocked"
                ),
            )

        # Redmine #14475 (review j#88505 F1): re-join the worktree binding IMMEDIATELY before
        # the first destructive effect. The preflight axis above was read before the operator
        # decision; a checkout can be switched to another branch — or stop resolving — between
        # that read and this actuation, and the relaunch would then stand the pair up on
        # whatever is checked out there. This is the same discipline the guarded refresh
        # follows: a pre-close fence does not retire the action-time re-join, it precedes it.
        action_time_binding = self._worktree_binding_reason(lane=lane, record=rec)
        if not launch_authority_current(action_time_binding):
            return RecoverPairOutcome(
                executed=False,
                preflight=replace(preflight, worktree_binding_reason=action_time_binding),
                issue=issue,
                lane=lane,
                detail=(
                    "fail-closed: the lane's worktree binding moved between preflight and "
                    f"actuation ({action_time_binding}); zero close / relaunch / resume / send"
                ),
            )

        # -- actuation: close ONLY the LIVE bad-generation slots (byte-preserving), then relaunch --
        # A slot that recovers with NO live locator is a vanished pair slot (e.g. closed in a
        # prior partial run): it needs no close, only a relaunch. Closing only live bad-gen
        # slots + relaunching whenever ANY slot needs recovery (not gated on "closed THIS run")
        # is what makes a partial close/relaunch replayable (Redmine #13847 R1-F1): a re-run of a
        # partially-closed pair sees the closed slot as `slot_absent` -> SLOT_RECOVER -> relaunch.
        recover_slots = [slot for slot in preflight.slots if slot.recovers]
        closed: list[str] = []

        applied: dict[str, bool] = {"relaunched": False, "resumed": False}

        def _effects(edge=None) -> Tuple[str, ...]:
            """The effects THIS run is KNOWN to have applied, in the order they happen. (pure)

            One composer for every return path (reviews j#88554 / j#88563) — including the
            close-failure and relaunch-failure branches, which used to hard-code
            ``executed=True`` with no effects and therefore both over-reported (a first-close
            failure applied nothing) and under-reported (a second-close failure lost the close
            that HAD happened).
            """
            found: list[str] = []
            if closed:
                found.append(EFFECT_CLOSED)
            if applied["relaunched"]:
                found.append(EFFECT_RELAUNCHED)
            if applied["resumed"]:
                found.append(EFFECT_RESUME_COMMITTED)
            if edge is not None:
                # Review j#88571 F1: taken from what the EDGE observed, never re-inferred from
                # its status — the same status covers a zero-send and a started transport.
                found.extend(edge.effects)
            return tuple(found)

        def _unresolved(edge=None) -> Tuple[str, ...]:
            """Actions whose durable fate this run could not establish. (pure)

            ``failed`` / ``uncertain`` say the send did not demonstrably deliver AND did not
            demonstrably no-op; ``uncertain`` in particular spans a pre-reserve zero-write and
            a post-send unknown. That is a fate, not an effect (review j#88563 F1).
            """
            return edge.unresolved if edge is not None else ()

        def _binding_drifted() -> Optional[RecoverPairOutcome]:
            """Re-join the 3 axes; a drift stops every REMAINING effect (review j#88526 F1).

            Called before EACH destructive effect, not once before the first: a checkout can be
            switched between two closes, or between the last close and the relaunch, and the
            single pre-loop read left every effect after the first unguarded. Whatever was
            already applied stays reported so the partial state remains replayable — a re-run
            sees closed slots ``slot_absent`` and relaunches them.

            ``executed`` is composed from EVERY effect this run applied (review j#88538 F2).
            Deriving it from the closes alone reported ``executed=False`` on a run that had
            already relaunched the pair or committed the resume — a report contradicting what
            the run actually did.
            """
            reason = self._worktree_binding_reason(lane=lane, record=rec)
            if launch_authority_current(reason):
                return None
            return RecoverPairOutcome(
                executed=bool(_effects()),
                effects=_effects(),
                unresolved=_unresolved(),
                attempted=True,
                preflight=replace(preflight, worktree_binding_reason=reason),
                issue=issue,
                lane=lane,
                closed_roles=tuple(closed),
                detail=(
                    "fail-closed: the lane's worktree binding moved during actuation "
                    f"({reason}); zero further close / relaunch / resume / send"
                ),
            )

        for slot in recover_slots:
            if not slot.locator:
                continue  # vanished (absent) — nothing to close; the relaunch recreates it
            drifted = _binding_drifted()
            if drifted is not None:
                return drifted
            ok = self.ops.close_bad_slot(
                role=slot.role, provider=slot.provider,
                assigned_name=slot.assigned_name, locator=slot.locator,
                action_id=action_id,
            )
            if not ok:
                # A live close failed: fail-closed. The partial state stays replayable — a
                # re-run finds the already-closed slot(s) `slot_absent` and relaunches them.
                # Review j#88563 F1: through the composer. A FIRST-close failure applied
                # nothing; a SECOND-close failure applied the first close and must say so.
                return RecoverPairOutcome(
                    executed=bool(_effects()), effects=_effects(),
                    unresolved=_unresolved(), attempted=True,
                    preflight=preflight, issue=issue, lane=lane,
                    closed_roles=tuple(closed),
                    detail=f"{BLOCK_CLOSE_FAILED}:{slot.role}",
                )
            closed.append(slot.role)

        if recover_slots:
            # The relaunch is the effect that stands processes up in the checkout, so it gets
            # its own re-join even when every close was skipped (all slots vanished).
            drifted = _binding_drifted()
            if drifted is not None:
                return drifted
        if recover_slots and not self.ops.relaunch_pair(
            action_id=action_id, slots=tuple(recover_slots)
        ):
            return RecoverPairOutcome(
                executed=bool(_effects()), effects=_effects(), unresolved=_unresolved(),
                attempted=True,
                preflight=preflight, issue=issue, lane=lane,
                closed_roles=tuple(closed), detail=BLOCK_RELAUNCH_FAILED,
                relaunch_reason=_norm(
                    getattr(self.ops, "relaunch_failure_reason", "")
                ),
                relaunch_startup=getattr(
                    self.ops, "relaunch_failure_startup", None
                ),
            )
        relaunched = bool(recover_slots)
        applied["relaunched"] = relaunched

        # -- resume: both-slots post-hibernate attestation verify + hibernated->active CAS --
        # Review j#88532 F1: the resume is NOT checkout-independent — it flips the lane to
        # ``active`` on the premise that the fresh pair stands in THIS lane's worktree, and its
        # own preflight does not re-read the branch. R4 stopped re-joining after the relaunch,
        # so a branch moved between relaunch and resume still reached the active flip.
        drifted = _binding_drifted()
        if drifted is not None:
            return replace(drifted, relaunched=relaunched)

        # Authorized by the owner-APPROVAL journal (request.journal), distinct from the original
        # implementation_request journal that the redispatch re-sends (Redmine #13847 R1-F3).
        resume_outcome = self.resume.run(
            ResumeRequest(issue=issue, lane=lane, journal=_norm(request.journal)),
            execute=True,
        )
        applied["resumed"] = bool(
            resume_outcome.transition is not None and resume_outcome.transition.applied
        )
        if resume_outcome.is_blocked:
            # Review j#88554 F1: truthful effects here, and ``is_blocked`` no longer depends on
            # ``executed`` — a healthy pair stopped at the commit edge applied nothing AND is
            # blocked, and both must be reported.
            return RecoverPairOutcome(
                executed=bool(_effects()), effects=_effects(), unresolved=_unresolved(),
                attempted=True,
                preflight=preflight, issue=issue, lane=lane,
                closed_roles=tuple(closed), relaunched=relaunched, resume=resume_outcome,
                detail=BLOCK_RESUME_REFUSED,
            )

        # -- redispatch: the ORIGINAL implementation_request to the gateway, exactly-once --
        # The fence key + delivery anchor use the ORIGINAL implementation_request journal (never
        # the owner-approval journal), so a re-approval never changes the fence key and can never
        # re-send the same original request (Redmine #13847 R1-F3).
        # The send is the last owed effect and the one that reaches a live pane, so it gets its
        # own re-join here as well as the transport-direct one the live ops passes as
        # ``pre_send_authority`` (review j#88532 F1). Stopping here leaves the resume applied
        # and the outbox untouched, so a re-run redelivers exactly once.
        drifted = _binding_drifted()
        if drifted is not None:
            return replace(
                drifted, relaunched=relaunched, resume=resume_outcome,
                redispatch=REDISPATCH_SKIPPED,
            )

        def _final_detail(
            effects: Tuple[str, ...], unresolved: Tuple[str, ...], redispatch: str
        ) -> str:
            """Say what actually happened (review j#88563 F2, j#88587 F2). (pure)

            Driven by the OBSERVED facts, not by the status's position in a chain of
            equality tests. The status was checked first, so a ``target_retiring`` whose
            cancel never wrote announced a settled "the reserve was cancelled" while
            carrying an unresolved fate, and a settled refusal with nothing applied fell
            through to "the implementation_request already delivered" — both statements
            the observation contradicts.
            """
            applied = f" (applied: {', '.join(effects)})" if effects else ""
            # 1. An unresolved fate outranks every status-specific phrasing: nothing about
            #    the durable state may be asserted.
            if unresolved:
                return (
                    "the redelivery's durable fate could not be established "
                    f"({redispatch}); operator reconcile required" + applied
                )
            # 2. Settled, and NOT a delivery. Review j#88592 F3: whether a redelivery is
            #    still OWED is a fact about what this run applied, not about the status. A
            #    run that committed the resume and then never sent owes the redelivery,
            #    whatever token names the reason; announcing "nothing is owed" there told the
            #    operator the opposite of the blocked machine state.
            if not redispatch_is_success(redispatch):
                reason = (
                    "the gateway is inside a retirement transaction, so the outbox reserve "
                    "was cancelled and nothing was delivered"
                    if redispatch == REDISPATCH_TARGET_RETIRING
                    else f"the implementation_request was not redelivered ({redispatch})"
                )
                owed = (
                    "; this run changed the pair, so the redelivery is still owed"
                    if effects
                    else "; nothing was applied and no outbox reservation is outstanding"
                )
                return "zero-send: " + reason + owed + applied
            # 3. Settled AND delivered (by this run or an earlier one).
            if effects:
                return (
                    "pair recovered; lane resumed to active (applied: "
                    + ", ".join(effects)
                    + ")"
                )
            return (
                "idempotent replay: the pair was already recovered and the "
                f"implementation_request already delivered ({redispatch}); "
                "nothing was applied"
            )

        gateway_name = preflight.gateway.assigned_name if preflight.gateway else ""
        # Review j#88579 F5: the protocol is typed, so the production path consumes the edge's
        # observation directly. No adapter stands between the observation and the report.
        edge = self.ops.redispatch_to_gateway(
            action_id=action_id,
            gateway_assigned_name=gateway_name,
            issue=issue,
            lane=lane,
            journal=_norm(request.implementation_request_journal),
            workspace_id=workspace_id,
        )
        redispatch = edge.status
        # Review j#88554 F2: the final branch used to hard-code ``executed=True`` and a fixed
        # "pair recovered" detail, so an ALL-idempotent replay — an already-active resume plus
        # an already-redispatched send, i.e. zero applied effects — reported the same thing as
        # a run that actually recovered the pair. Both are now derived from what happened.
        effects = _effects(edge)
        unresolved = _unresolved(edge)
        return RecoverPairOutcome(
            executed=bool(effects),
            effects=effects,
            unresolved=unresolved,
            attempted=True,
            preflight=preflight,
            issue=issue,
            lane=lane,
            closed_roles=tuple(closed),
            relaunched=relaunched,
            resume=resume_outcome,
            redispatch=redispatch,
            detail=_final_detail(effects, unresolved, redispatch),
        )


__all__ = (
    "BLOCK_CLOSE_FAILED",
    "BLOCK_IDENTITY_INCOMPLETE",
    "BLOCK_LANE_NOT_HIBERNATED",
    "BLOCK_MISSING_PINS",
    "BLOCK_RELAUNCH_FAILED",
    "BLOCK_RESUME_REFUSED",
    "BLOCK_SLOT_PRESERVED",
    "BLOCK_STORE_UNREADABLE",
    "REDISPATCH_ALREADY",
    "REDISPATCH_DELIVERED",
    "REDISPATCH_FAILED",
    "REDISPATCH_SKIPPED",
    "REDISPATCH_TARGET_RETIRING",
    "REDISPATCH_UNCERTAIN",
    "HibernatedPairRecoveryOps",
    "RecoverPairOutcome",
    "RecoverPairDeliveryRetryOutcome",
    "RecoverPairDeliveryRetryRequest",
    "RecoverPairPreflight",
    "RecoverPairRequest",
    "SlotPlan",
    "SublaneRecoverPairUseCase",
)
