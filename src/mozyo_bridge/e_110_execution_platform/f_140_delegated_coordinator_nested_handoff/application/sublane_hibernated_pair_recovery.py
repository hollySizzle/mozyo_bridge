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
from dataclasses import dataclass, field
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    SLOT_HEALTHY,
    SLOT_RECOVER,
    SlotRecoveryObservation,
    decide_slot_recovery,
    hibernated_pair_recovery_action_id,
    slot_recovers,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.pair_launch_attestation import (  # noqa: E501
    GATEWAY_ROLE,
    WORKER_ROLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)

# Blocked-reason vocabulary (fail-closed preflight, distinct from the resume vocabulary).
BLOCK_LANE_NOT_HIBERNATED = "lane_not_hibernated"
BLOCK_IDENTITY_INCOMPLETE = "identity_or_decision_incomplete"
BLOCK_STORE_UNREADABLE = "lifecycle_store_unreadable"
BLOCK_MISSING_PINS = "hibernated_record_missing_pins"
BLOCK_SLOT_PRESERVED = "slot_preserved_not_recoverable"  # a slot is preserve-disposition
BLOCK_CLOSE_FAILED = "bad_generation_close_failed"
BLOCK_RELAUNCH_FAILED = "pair_relaunch_failed"
BLOCK_RESUME_REFUSED = "resume_verify_or_cas_refused"

# Redispatch outcome tokens (item 5). A delivery ACK is never a task-start / completion.
REDISPATCH_DELIVERED = "redispatched"  # the fence reserved this call and the send fired
REDISPATCH_ALREADY = "already_redispatched"  # the fence already holds a delivered/reserved row
REDISPATCH_UNCERTAIN = "redispatch_uncertain"  # send fate unknown -> operator reconcile
REDISPATCH_SKIPPED = "redispatch_not_reached"  # resume did not apply -> nothing to redeliver
REDISPATCH_FAILED = "redispatch_send_failed"


@dataclass(frozen=True)
class SlotPlan:
    """One slot's role / provider pin and its pure recovery disposition."""

    role: str
    provider: str
    assigned_name: str
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
class RecoverPairPreflight:
    """The fail-closed preflight verdict + per-slot plan (pure over the observations)."""

    lane_hibernated: bool
    record_has_pins: bool
    gateway: Optional[SlotPlan]
    worker: Optional[SlotPlan]
    action_id: str
    detail: str = ""

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
        )

    @property
    def blocked_reasons(self) -> Tuple[str, ...]:
        reasons: list[str] = []
        if not self.lane_hibernated:
            reasons.append(BLOCK_LANE_NOT_HIBERNATED)
        if not self.record_has_pins or len(self.slots) != 2:
            reasons.append(BLOCK_MISSING_PINS)
        for slot in self.preserved_slots:
            reasons.append(f"{BLOCK_SLOT_PRESERVED}:{slot.role}={slot.disposition}")
        return tuple(reasons)

    def as_payload(self) -> dict[str, Any]:
        return {
            "may_recover": self.may_recover,
            "lane_hibernated": self.lane_hibernated,
            "record_has_pins": self.record_has_pins,
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

    @property
    def is_blocked(self) -> bool:
        if not self.preflight.may_recover:
            return True
        if not self.executed:
            return False
        if self.resume is None or self.resume.is_blocked:
            return True
        # A redispatch that failed / is uncertain is a blocked outcome (the operator must
        # reconcile); an already-redispatched or freshly-delivered redispatch is not.
        return self.redispatch in (REDISPATCH_FAILED, REDISPATCH_UNCERTAIN)

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
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

    def close_bad_slot(
        self, *, role: str, provider: str, assigned_name: str, locator: str, action_id: str
    ) -> bool: ...

    def relaunch_pair(self, *, action_id: str) -> bool: ...

    def redispatch_to_gateway(
        self,
        *,
        action_id: str,
        gateway_assigned_name: str,
        issue: str,
        lane: str,
        journal: str,
        workspace_id: str,
    ) -> str: ...


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
        provider = _norm(getattr(pin, "provider", "")) or _norm(getattr(pin, "role", ""))
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
            locator=_norm(live_locator),
            disposition=decide_slot_recovery(observation),
        )

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
            declared = _declared_pins_by_role(rec)
            gw_pin = declared.get(GATEWAY_ROLE)
            wk_pin = declared.get(WORKER_ROLE)
            if action_id and gw_pin is not None and wk_pin is not None:
                record_has_pins = True
                gateway_plan = self._slot_plan(
                    role=GATEWAY_ROLE, record=rec, pin=gw_pin,
                    workspace_id=workspace_id, lane=lane,
                )
                worker_plan = self._slot_plan(
                    role=WORKER_ROLE, record=rec, pin=wk_pin,
                    workspace_id=workspace_id, lane=lane,
                )

        preflight = RecoverPairPreflight(
            lane_hibernated=lane_hibernated,
            record_has_pins=record_has_pins,
            gateway=gateway_plan,
            worker=worker_plan,
            action_id=action_id,
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

        # -- actuation: close ONLY the LIVE bad-generation slots (byte-preserving), then relaunch --
        # A slot that recovers with NO live locator is a vanished pair slot (e.g. closed in a
        # prior partial run): it needs no close, only a relaunch. Closing only live bad-gen
        # slots + relaunching whenever ANY slot needs recovery (not gated on "closed THIS run")
        # is what makes a partial close/relaunch replayable (Redmine #13847 R1-F1): a re-run of a
        # partially-closed pair sees the closed slot as `slot_absent` -> SLOT_RECOVER -> relaunch.
        recover_slots = [slot for slot in preflight.slots if slot.recovers]
        closed: list[str] = []
        for slot in recover_slots:
            if not slot.locator:
                continue  # vanished (absent) — nothing to close; the relaunch recreates it
            ok = self.ops.close_bad_slot(
                role=slot.role, provider=slot.provider,
                assigned_name=slot.assigned_name, locator=slot.locator,
                action_id=action_id,
            )
            if not ok:
                # A live close failed: fail-closed. The partial state stays replayable — a
                # re-run finds the already-closed slot(s) `slot_absent` and relaunches them.
                return RecoverPairOutcome(
                    executed=True, preflight=preflight, issue=issue, lane=lane,
                    closed_roles=tuple(closed),
                    detail=f"{BLOCK_CLOSE_FAILED}:{slot.role}",
                )
            closed.append(slot.role)

        if recover_slots and not self.ops.relaunch_pair(action_id=action_id):
            return RecoverPairOutcome(
                executed=True, preflight=preflight, issue=issue, lane=lane,
                closed_roles=tuple(closed), detail=BLOCK_RELAUNCH_FAILED,
            )
        relaunched = bool(recover_slots)

        # -- resume: both-slots post-hibernate attestation verify + hibernated->active CAS --
        # Authorized by the owner-APPROVAL journal (request.journal), distinct from the original
        # implementation_request journal that the redispatch re-sends (Redmine #13847 R1-F3).
        resume_outcome = self.resume.run(
            ResumeRequest(issue=issue, lane=lane, journal=_norm(request.journal)),
            execute=True,
        )
        if resume_outcome.is_blocked:
            return RecoverPairOutcome(
                executed=True, preflight=preflight, issue=issue, lane=lane,
                closed_roles=tuple(closed), relaunched=relaunched, resume=resume_outcome,
                detail=BLOCK_RESUME_REFUSED,
            )

        # -- redispatch: the ORIGINAL implementation_request to the gateway, exactly-once --
        # The fence key + delivery anchor use the ORIGINAL implementation_request journal (never
        # the owner-approval journal), so a re-approval never changes the fence key and can never
        # re-send the same original request (Redmine #13847 R1-F3).
        gateway_name = preflight.gateway.assigned_name if preflight.gateway else ""
        redispatch = self.ops.redispatch_to_gateway(
            action_id=action_id,
            gateway_assigned_name=gateway_name,
            issue=issue,
            lane=lane,
            journal=_norm(request.implementation_request_journal),
            workspace_id=workspace_id,
        )
        return RecoverPairOutcome(
            executed=True,
            preflight=preflight,
            issue=issue,
            lane=lane,
            closed_roles=tuple(closed),
            relaunched=relaunched,
            resume=resume_outcome,
            redispatch=redispatch,
            detail="pair recovered; lane resumed to active",
        )


def _declared_pins_by_role(record: Any) -> dict:
    """Map ``{role: pin}`` from the record's declared pins (empty when absent)."""
    pins = getattr(record, "declared_pins", None)
    if not pins:
        return {}
    out = {}
    for pin in pins:
        role = _norm(getattr(pin, "role", ""))
        if role and role not in out:
            out[role] = pin
    return out


# ---------------------------------------------------------------------------
# Text rendering + thin CLI handler.
# ---------------------------------------------------------------------------


def format_recover_pair_text(outcome: RecoverPairOutcome) -> str:
    lines = [
        f"sublane recover-pair: {outcome.lane} (issue {outcome.issue})",
        f"  may_recover: {outcome.preflight.may_recover} executed: {outcome.executed}",
    ]
    for slot in outcome.preflight.slots:
        lines.append(f"  {slot.role}: {slot.disposition} (recovers={slot.recovers})")
    if outcome.is_blocked:
        lines.append(
            "  -> fail-closed blocked: " + ", ".join(outcome.preflight.blocked_reasons or (outcome.detail,))
        )
        if outcome.resume is not None and outcome.resume.is_blocked:
            lines.append("  resume: " + ", ".join(outcome.resume.preflight.blocked_reasons))
        return "\n".join(lines)
    if outcome.executed:
        lines.append(f"  closed: {', '.join(outcome.closed_roles) or 'none'} relaunched: {outcome.relaunched}")
        lines.append(f"  redispatch: {outcome.redispatch}")
    elif outcome.preflight.may_recover:
        lines.append("  (preflight only; re-run with --execute to recover the pair)")
    return "\n".join(lines)


def cmd_sublane_recover_pair(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = RecoverPairRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        implementation_request_journal=getattr(args, "implementation_request_journal", "") or "",
    )
    json_mode = bool(getattr(args, "json", False))
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
        build_live_recover_pair_use_case,
    )

    # The builder binds the owner-APPROVAL journal into the live ops (close/relaunch/actuator
    # requests); the ORIGINAL implementation_request journal flows per-run through the request
    # to the redispatch call, so it is not a builder argument.
    use_case = build_live_recover_pair_use_case(
        repo_root=repo_root, env=dict(os.environ),
        issue=request.issue, lane=request.lane, journal=request.journal,
    )
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if json_mode:
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_recover_pair_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_recover_pair_parser(sublane_sub: Any) -> None:
    """Register ``sublane recover-pair`` outside the at-ceiling core CLI module."""
    parser = sublane_sub.add_parser(
        "recover-pair",
        help=(
            "Redmine #13847: recover the exact gateway+worker pair of a hibernated lane "
            "whose fresh launch booted partially (unattested/stale). Default is preflight "
            "only; --execute closes only the bad generation, relaunches, resumes, and "
            "redispatches the original implementation_request exactly-once."
        ),
    )
    parser.add_argument("--issue", required=True, help="Redmine issue the hibernated lane owns")
    parser.add_argument("--lane", required=True, help="Hibernated lane label to recover")
    parser.add_argument(
        "--journal",
        required=True,
        help="Redmine journal of the owner APPROVAL authorizing this destructive recovery "
        "(the resume authorization anchor)",
    )
    parser.add_argument(
        "--implementation-request-journal",
        dest="implementation_request_journal",
        required=True,
        help="Redmine journal of the ORIGINAL implementation_request to re-deliver to the "
        "gateway exactly-once (the fence key + delivery anchor; distinct from --journal)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the guarded close (bad generation only) + relaunch + resume + redispatch",
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.set_defaults(func=cmd_sublane_recover_pair)


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
    "REDISPATCH_UNCERTAIN",
    "HibernatedPairRecoveryOps",
    "RecoverPairOutcome",
    "RecoverPairPreflight",
    "RecoverPairRequest",
    "SlotPlan",
    "SublaneRecoverPairUseCase",
    "cmd_sublane_recover_pair",
    "format_recover_pair_text",
    "register_sublane_recover_pair_parser",
)
