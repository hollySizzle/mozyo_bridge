"""Public ``sublane converge-bound-pair`` orchestration (Redmine #13933).

This command does not relax the three existing recovery rails.  It covers only the missing
intersection: hibernated + released + worktree-bound + empty declared pins, with a stale or
unattested managed pair.  Preflight is read-only.  Execution requires a freshly-read structured
owner marker, replaces only positively recoverable generations, proves the final pair, then
fills pins through the existing bounded CAS.  It never resumes or sends work.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.core.state.lane_lifecycle import ProcessGenerationPin
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    ApprovalExpectation,
    BLOCK_APPROVAL_MISMATCH,
    BLOCK_APPROVAL_MISSING,
    BLOCK_FRESH_PAIR_UNPROVEN,
    BLOCK_IDENTITY_INCOMPLETE,
    BLOCK_INVENTORY_UNREADABLE,
    BLOCK_NOT_BOUND_SIGNATURE,
    BLOCK_PAIR_AMBIGUOUS,
    BLOCK_PAIR_PRESERVED,
    BLOCK_PIN_CAS_REFUSED,
    BLOCK_REPLACEMENT_STOPPED,
    BLOCK_TRANSACTION_CONFLICT,
    BLOCK_WORKTREE_UNSAFE,
    BoundSlot,
    ConvergenceVerdict,
    TransactionPlanObservation,
    STATE_ACTIONABLE,
    STATE_ALREADY_CONVERGED,
    STATE_BLOCKED,
    approval_matches,
    convergence_action_id,
    slot_digest,
    worktree_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_HEALTHY,
    SLOT_RECOVER,
)


@dataclass(frozen=True)
class ConvergeBoundPairRequest:
    issue: str
    journal: str
    lane: str
    worktree: str
    branch: str


@dataclass(frozen=True)
class BoundPairObservation:
    """Action-time facts.  Every unsafe/unknown axis defaults to fail-closed."""

    workspace_id: str = ""
    worktree_path: str = ""
    worktree_identity: str = ""
    branch: str = ""
    revision: int = -1
    generation: int = 0
    lifecycle_exact: bool = False
    pins_empty: bool = False
    pins_exact: bool = False
    inventory_readable: bool = False
    worktree_readable: bool = False
    worktree_clean: bool = False
    branch_matches: bool = False
    slots: tuple[BoundSlot, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReplacementDrive:
    ok: bool
    status: str
    detail: str = ""


@dataclass(frozen=True)
class PinRepairResult:
    ok: bool
    reason: str
    repaired: bool = False


@dataclass(frozen=True)
class ConvergenceOutcome:
    request: ConvergeBoundPairRequest
    verdict: ConvergenceVerdict
    executed: bool = False
    replacement_status: str = ""
    pins_repaired: bool = False

    @property
    def is_blocked(self) -> bool:
        return self.verdict.blocked

    def as_payload(self) -> dict[str, object]:
        payload = self.verdict.as_payload()
        payload.update(
            {
                "issue": self.request.issue,
                "journal": self.request.journal,
                "lane": self.request.lane,
                "worktree": self.request.worktree,
                "branch": self.request.branch,
                "executed": self.executed,
                "replacement_status": self.replacement_status or None,
                "pins_repaired": self.pins_repaired,
                "is_blocked": self.is_blocked,
            }
        )
        return payload


@runtime_checkable
class BoundPairConvergenceOps(Protocol):
    def observe(self, request: ConvergeBoundPairRequest, *, action_id: str = "") -> BoundPairObservation: ...
    def approval_fields(self, issue: str, journal: str) -> Sequence[Mapping[str, str]]: ...
    def drive_replacement(
        self,
        request: ConvergeBoundPairRequest,
        expectation: ApprovalExpectation,
        observation: BoundPairObservation,
    ) -> ReplacementDrive: ...
    def final_pins(
        self, request: ConvergeBoundPairRequest, *, action_id: str
    ) -> tuple[BoundPairObservation, tuple[ProcessGenerationPin, ...]]: ...
    def repair_pins(
        self,
        request: ConvergeBoundPairRequest,
        expectation: ApprovalExpectation,
        observation: BoundPairObservation,
        pins: Sequence[ProcessGenerationPin],
    ) -> PinRepairResult: ...
    def finish_replacement(self, expectation: ApprovalExpectation) -> bool: ...


def _blocked(
    request: ConvergeBoundPairRequest,
    reason: str,
    *,
    detail: str = "",
    action_id: str = "",
    slots: Sequence[BoundSlot] = (),
    executed: bool = False,
    replacement_status: str = "",
) -> ConvergenceOutcome:
    return ConvergenceOutcome(
        request=request,
        verdict=ConvergenceVerdict(
            state=STATE_BLOCKED,
            reason=reason,
            detail=detail,
            action_id=action_id,
            slots=tuple(slots),
        ),
        executed=executed,
        replacement_status=replacement_status,
    )


def _expectation(request: ConvergeBoundPairRequest, obs: BoundPairObservation) -> ApprovalExpectation:
    slots_hash = slot_digest(obs.slots)
    action_id = convergence_action_id(
        issue=request.issue,
        lane=request.lane,
        revision=obs.revision,
        generation=obs.generation,
        slots_digest=slots_hash,
    )
    return ApprovalExpectation(
        issue=request.issue,
        lane=request.lane,
        revision=obs.revision,
        generation=obs.generation,
        action_generation=1,
        action_id=action_id,
        worktree_digest=worktree_digest(
            resolved_path=obs.worktree_path,
            identity=obs.worktree_identity,
            branch=obs.branch,
        ),
        slot_digest=slots_hash,
    )


def _classify(
    request: ConvergeBoundPairRequest, obs: BoundPairObservation
) -> tuple[ConvergenceOutcome | None, ApprovalExpectation | None]:
    if not all((request.issue.strip(), request.journal.strip(), request.lane.strip(), request.worktree.strip(), request.branch.strip())):
        return _blocked(request, BLOCK_IDENTITY_INCOMPLETE), None
    if not obs.lifecycle_exact or (not obs.pins_empty and not obs.pins_exact):
        return _blocked(request, BLOCK_NOT_BOUND_SIGNATURE, detail=obs.detail, slots=obs.slots), None
    if not obs.inventory_readable:
        return _blocked(request, BLOCK_INVENTORY_UNREADABLE, detail=obs.detail), None
    if not (obs.worktree_readable and obs.worktree_clean and obs.branch_matches):
        return _blocked(request, BLOCK_WORKTREE_UNSAFE, detail=obs.detail), None
    if len(obs.slots) != 2 or {slot.role for slot in obs.slots} != {"gateway", "worker"}:
        return _blocked(request, BLOCK_PAIR_AMBIGUOUS, detail=obs.detail, slots=obs.slots), None
    for slot in obs.slots:
        if not slot.provider or not slot.assigned_name or (not slot.locator and not slot.close_proven):
            return _blocked(request, BLOCK_PAIR_AMBIGUOUS, slots=obs.slots), None
        if slot.disposition not in (SLOT_RECOVER, SLOT_HEALTHY):
            return _blocked(
                request,
                BLOCK_PAIR_PRESERVED,
                detail=f"{slot.role}={slot.disposition}",
                slots=obs.slots,
            ), None
    try:
        expected = _expectation(request, obs)
    except ValueError as exc:
        return _blocked(request, BLOCK_IDENTITY_INCOMPLETE, detail=str(exc)), None
    if obs.pins_exact and all(slot.disposition == SLOT_HEALTHY for slot in obs.slots):
        return ConvergenceOutcome(
            request=request,
            verdict=ConvergenceVerdict(
                state=STATE_ALREADY_CONVERGED,
                action_id=expected.action_id,
                slots=obs.slots,
            ),
        ), expected
    return None, expected


def _may_need_transaction_close_proof(obs: BoundPairObservation) -> bool:
    """Return true only for an otherwise-exact pair with a missing old locator.

    The action id is needed to read the immutable replacement transaction that can prove an
    already-closed participant.  Cardinality/role ambiguity and incomplete managed identity
    must never cross this boundary merely because an approval marker happens to exist.
    """

    if len(obs.slots) != 2 or {slot.role for slot in obs.slots} != {"gateway", "worker"}:
        return False
    if not all(slot.provider and slot.assigned_name for slot in obs.slots):
        return False
    if not any(not slot.locator and not slot.close_proven for slot in obs.slots):
        return False
    return all(
        slot.disposition in (SLOT_RECOVER, SLOT_HEALTHY)
        for slot in obs.slots
    )


def transaction_plan_observation(
    request: ConvergeBoundPairRequest,
    observation: BoundPairObservation,
) -> TransactionPlanObservation:
    """Project the complete application observation into the pure plan decision."""

    pair_safe = bool(
        len(observation.slots) == 2
        and {slot.role for slot in observation.slots} == {"gateway", "worker"}
        and all(
            slot.provider
            and slot.assigned_name
            and (slot.locator or slot.close_proven)
            and slot.disposition in (SLOT_RECOVER, SLOT_HEALTHY)
            for slot in observation.slots
        )
    )
    return TransactionPlanObservation(
        issue=request.issue,
        lane=request.lane,
        workspace_id=observation.workspace_id,
        worktree_path=observation.worktree_path,
        worktree_identity=observation.worktree_identity,
        branch=observation.branch,
        revision=observation.revision,
        generation=observation.generation,
        lifecycle_exact=observation.lifecycle_exact,
        pins_empty=observation.pins_empty,
        inventory_readable=observation.inventory_readable,
        worktree_readable=observation.worktree_readable,
        worktree_clean=observation.worktree_clean,
        branch_matches=observation.branch_matches,
        pair_safe=pair_safe,
        slot_digest=slot_digest(observation.slots),
    )


def run_bound_pair_convergence(
    request: ConvergeBoundPairRequest,
    *,
    execute: bool,
    ops: BoundPairConvergenceOps,
) -> ConvergenceOutcome:
    first = ops.observe(request)
    terminal, expected = _classify(request, first)
    retry_needs_transaction_proof = bool(
        execute
        and terminal is not None
        and terminal.verdict.reason == BLOCK_PAIR_AMBIGUOUS
        and _may_need_transaction_close_proof(first)
    )
    if terminal is not None and not retry_needs_transaction_proof:
        return terminal
    if not execute:
        assert expected is not None
        return ConvergenceOutcome(
            request=request,
            verdict=ConvergenceVerdict(
                state=STATE_ACTIONABLE,
                detail="preflight only; record the exact structured owner marker before --execute",
                action_id=expected.action_id,
                approval_marker=expected.marker(),
                slots=first.slots,
            ),
        )

    try:
        marker_fields = tuple(ops.approval_fields(request.issue, request.journal))
    except Exception as exc:  # noqa: BLE001 - credential/network/unreadable => zero effect
        if retry_needs_transaction_proof:
            return terminal
        return _blocked(request, BLOCK_APPROVAL_MISSING, detail=type(exc).__name__, executed=True)
    if not marker_fields:
        if retry_needs_transaction_proof:
            return terminal
        return _blocked(request, BLOCK_APPROVAL_MISSING, executed=True)

    # Reconstruct the immutable expectation from the structured marker.  This matters on a
    # partial retry: the live locator may already have changed (or the exact old slot may be
    # positively absent), while the marker and replacement transaction continue to identify
    # the original generation.  Marker values are still checked against the action-time
    # lifecycle/worktree below; prose and malformed integers never become authority.
    approved: ApprovalExpectation | None = None
    for fields in marker_fields:
        try:
            candidate = ApprovalExpectation(
                issue=fields["issue"],
                lane=fields["lane"],
                revision=int(fields["revision"]),
                generation=int(fields["generation"]),
                action_generation=int(fields["action_generation"]),
                action_id=fields["action_id"],
                worktree_digest=fields["worktree_digest"],
                slot_digest=fields["slot_digest"],
            )
            candidate_action = convergence_action_id(
                issue=candidate.issue,
                lane=candidate.lane,
                revision=candidate.revision,
                generation=candidate.generation,
                slots_digest=candidate.slot_digest,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if approval_matches(fields, candidate) and candidate.action_id == candidate_action:
            approved = candidate
            break
    if approved is None:
        return _blocked(
            request,
            BLOCK_APPROVAL_MISMATCH,
            action_id=expected.action_id if expected is not None else "",
            slots=first.slots,
            executed=True,
        )

    # Fresh action-time re-read immediately before a transaction is planned or resumed.
    current = ops.observe(request, action_id=approved.action_id)
    current_terminal, current_expected = _classify(request, current)
    if current_terminal is not None:
        return _blocked(
            request,
            current_terminal.verdict.reason,
            detail=current_terminal.verdict.detail,
            action_id=approved.action_id,
            slots=current.slots,
            executed=True,
        )
    current_worktree_digest = worktree_digest(
        resolved_path=current.worktree_path,
        identity=current.worktree_identity,
        branch=current.branch,
    )
    if (
        approved.issue != request.issue
        or approved.lane != request.lane
        or approved.revision != current.revision
        or approved.generation != current.generation
        or approved.action_generation < 1
        or approved.worktree_digest != current_worktree_digest
    ):
        return _blocked(
            request, BLOCK_APPROVAL_MISMATCH, detail="action-time observation changed",
            action_id=approved.action_id, slots=current.slots, executed=True,
        )
    drive = ops.drive_replacement(request, approved, current)
    if not drive.ok:
        reason = BLOCK_TRANSACTION_CONFLICT if drive.status == "transaction_conflict" else BLOCK_REPLACEMENT_STOPPED
        return _blocked(
            request, reason, detail=drive.detail, action_id=approved.action_id,
            slots=current.slots, executed=True, replacement_status=drive.status,
        )

    final_obs, pins = ops.final_pins(request, action_id=approved.action_id)
    if (
        not final_obs.inventory_readable
        or len(final_obs.slots) != 2
        or any(slot.disposition != SLOT_HEALTHY or not slot.locator for slot in final_obs.slots)
        or len(pins) != 2
    ):
        return _blocked(
            request, BLOCK_FRESH_PAIR_UNPROVEN, detail=final_obs.detail,
            action_id=approved.action_id, slots=final_obs.slots, executed=True,
            replacement_status=drive.status,
        )
    repair = ops.repair_pins(request, approved, final_obs, pins)
    if not repair.ok:
        return _blocked(
            request, BLOCK_PIN_CAS_REFUSED, detail=repair.reason,
            action_id=approved.action_id, slots=final_obs.slots, executed=True,
            replacement_status=drive.status,
        )
    if not ops.finish_replacement(approved):
        return _blocked(
            request, BLOCK_REPLACEMENT_STOPPED,
            detail="pin CAS applied but replacement transaction completion CAS stopped; replay",
            action_id=approved.action_id, slots=final_obs.slots, executed=True,
            replacement_status=drive.status,
        )
    return ConvergenceOutcome(
        request=request,
        verdict=ConvergenceVerdict(
            state=STATE_ALREADY_CONVERGED,
            detail="fresh pair proven and declared pins repaired; lane remains hibernated",
            action_id=approved.action_id,
            slots=final_obs.slots,
        ),
        executed=True,
        replacement_status=drive.status,
        pins_repaired=repair.repaired,
    )


def format_convergence_text(outcome: ConvergenceOutcome) -> str:
    lines = [
        f"sublane converge-bound-pair: {outcome.request.lane} (issue {outcome.request.issue})",
        f"  state: {outcome.verdict.state} executed: {outcome.executed}",
    ]
    if outcome.verdict.reason:
        lines.append(f"  reason: {outcome.verdict.reason}")
    if outcome.verdict.detail:
        lines.append(f"  detail: {outcome.verdict.detail}")
    if outcome.verdict.approval_marker:
        lines.append(f"  approval_marker: {outcome.verdict.approval_marker}")
    return "\n".join(lines)


def cmd_sublane_converge_bound_pair(args: argparse.Namespace) -> int:
    request = ConvergeBoundPairRequest(
        issue=getattr(args, "issue", "") or "",
        journal=getattr(args, "journal", "") or "",
        lane=getattr(args, "lane", "") or "",
        worktree=getattr(args, "worktree", "") or "",
        branch=getattr(args, "branch", "") or "",
    )
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live import (
        LiveBoundPairConvergenceOps,
    )

    outcome = run_bound_pair_convergence(
        request,
        execute=bool(getattr(args, "execute", False)),
        ops=LiveBoundPairConvergenceOps(repo_root=repo_root, env=dict(os.environ)),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_convergence_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_converge_bound_pair_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "converge-bound-pair",
        help=(
            "Redmine #13933: replace the exact stale/unattested pair of a hibernated, released, "
            "worktree-bound lane with empty pins, then repair pins from the fresh attested pair. "
            "Default is read-only preflight; never resumes or sends."
        ),
    )
    parser.add_argument("--issue", required=True)
    parser.add_argument("--journal", required=True, help="Structured direct-owner approval journal")
    parser.add_argument("--lane", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--branch", required=True, help="Exact expected worktree branch")
    parser.add_argument("--execute", action="store_true")
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_sublane_converge_bound_pair)


__all__ = (
    "BoundPairConvergenceOps", "BoundPairObservation", "ConvergeBoundPairRequest",
    "ConvergenceOutcome", "PinRepairResult", "ReplacementDrive",
    "cmd_sublane_converge_bound_pair", "format_convergence_text",
    "register_sublane_converge_bound_pair_parser", "run_bound_pair_convergence",
    "transaction_plan_observation",
)
