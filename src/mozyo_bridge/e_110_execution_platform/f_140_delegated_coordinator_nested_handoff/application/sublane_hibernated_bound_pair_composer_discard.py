"""Public ``sublane prepare-bound-pair`` orchestration (Redmine #13933)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_composer_discard import (
    APPROVAL_GATE,
    BLOCK_APPROVAL_MISMATCH,
    BLOCK_APPROVAL_MISSING,
    BLOCK_IDENTITY_INCOMPLETE,
    BLOCK_INVENTORY_UNREADABLE,
    BLOCK_NO_DISCARDABLE_COMPOSER,
    BLOCK_NOT_BOUND_SIGNATURE,
    BLOCK_PAIR_AMBIGUOUS,
    BLOCK_PAIR_PRESERVED,
    BLOCK_REPLACEMENT_STOPPED,
    BLOCK_TRANSACTION_CONFLICT,
    BLOCK_WORKTREE_UNSAFE,
    STATE_ACTIONABLE,
    STATE_BLOCKED,
    STATE_PREPARED,
    PreparationExpectation,
    approval_matches,
    expectation_for,
    roles_token,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    BoundSlot,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_HEALTHY,
    SLOT_PRESERVE_PENDING,
    SLOT_RECOVER,
)


@dataclass(frozen=True)
class PrepareBoundPairRequest:
    issue: str
    journal: str
    lane: str
    worktree: str
    branch: str


@dataclass(frozen=True)
class PreparationObservation:
    workspace_id: str = ""
    worktree_path: str = ""
    worktree_identity: str = ""
    branch: str = ""
    revision: int = -1
    generation: int = 0
    lifecycle_exact: bool = False
    pins_empty: bool = False
    inventory_readable: bool = False
    worktree_readable: bool = False
    worktree_clean: bool = False
    branch_matches: bool = False
    slots: tuple[BoundSlot, ...] = ()
    discard_roles: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class PreparationDrive:
    ok: bool
    status: str
    detail: str = ""


@dataclass(frozen=True)
class PreparationOutcome:
    request: PrepareBoundPairRequest
    state: str
    reason: str = ""
    detail: str = ""
    action_id: str = ""
    approval_marker: str = ""
    slots: tuple[BoundSlot, ...] = ()
    discard_roles: tuple[str, ...] = ()
    executed: bool = False
    replacement_status: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.state == STATE_BLOCKED

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.request.issue,
            "journal": self.request.journal,
            "lane": self.request.lane,
            "worktree": self.request.worktree,
            "branch": self.request.branch,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "action_id": self.action_id or None,
            "approval_marker": self.approval_marker or None,
            "slots": [slot.canonical() for slot in self.slots],
            "discard_roles": list(self.discard_roles),
            "executed": self.executed,
            "replacement_status": self.replacement_status or None,
            "is_blocked": self.is_blocked,
            "pins_repaired": False,
            "resumed": False,
            "sent": False,
        }


@runtime_checkable
class BoundPairPreparationOps(Protocol):
    def observe(
        self, request: PrepareBoundPairRequest, *, action_id: str = ""
    ) -> PreparationObservation: ...
    def approval_fields(self, issue: str, journal: str) -> Sequence[Mapping[str, str]]: ...
    def drive(
        self,
        request: PrepareBoundPairRequest,
        expectation: PreparationExpectation,
        initial: PreparationObservation,
    ) -> PreparationDrive: ...


def _blocked(
    request: PrepareBoundPairRequest,
    reason: str,
    *,
    detail: str = "",
    action_id: str = "",
    slots: Sequence[BoundSlot] = (),
    discard_roles: Sequence[str] = (),
    executed: bool = False,
    replacement_status: str = "",
) -> PreparationOutcome:
    return PreparationOutcome(
        request=request,
        state=STATE_BLOCKED,
        reason=reason,
        detail=detail,
        action_id=action_id,
        slots=tuple(slots),
        discard_roles=tuple(discard_roles),
        executed=executed,
        replacement_status=replacement_status,
    )


def _classify(
    request: PrepareBoundPairRequest, observation: PreparationObservation
) -> tuple[PreparationOutcome | None, PreparationExpectation | None]:
    if not all(
        value.strip()
        for value in (request.issue, request.journal, request.lane, request.worktree, request.branch)
    ):
        return _blocked(request, BLOCK_IDENTITY_INCOMPLETE), None
    if not observation.lifecycle_exact or not observation.pins_empty:
        return _blocked(
            request, BLOCK_NOT_BOUND_SIGNATURE, detail=observation.detail,
            slots=observation.slots,
        ), None
    if not observation.inventory_readable:
        return _blocked(request, BLOCK_INVENTORY_UNREADABLE, detail=observation.detail), None
    if not (
        observation.worktree_readable
        and observation.worktree_clean
        and observation.branch_matches
    ):
        return _blocked(request, BLOCK_WORKTREE_UNSAFE, detail=observation.detail), None
    if len(observation.slots) != 2 or {
        slot.role for slot in observation.slots
    } != {"gateway", "worker"}:
        return _blocked(
            request, BLOCK_PAIR_AMBIGUOUS, detail=observation.detail,
            slots=observation.slots,
        ), None
    if not observation.discard_roles:
        return _blocked(
            request, BLOCK_NO_DISCARDABLE_COMPOSER, detail=observation.detail,
            slots=observation.slots,
        ), None
    discard = set(observation.discard_roles)
    for slot in observation.slots:
        if not slot.provider or not slot.assigned_name or not slot.locator:
            return _blocked(request, BLOCK_PAIR_AMBIGUOUS, slots=observation.slots), None
        if slot.disposition == SLOT_PRESERVE_PENDING:
            if slot.role not in discard:
                return _blocked(
                    request, BLOCK_PAIR_PRESERVED, detail=f"{slot.role}=pending_not_approved",
                    slots=observation.slots,
                ), None
        elif slot.disposition not in (SLOT_RECOVER, SLOT_HEALTHY):
            return _blocked(
                request, BLOCK_PAIR_PRESERVED,
                detail=f"{slot.role}={slot.disposition}", slots=observation.slots,
            ), None
    try:
        expected = expectation_for(
            issue=request.issue,
            lane=request.lane,
            revision=observation.revision,
            generation=observation.generation,
            resolved_worktree=observation.worktree_path,
            worktree_identity=observation.worktree_identity,
            branch=observation.branch,
            slots=observation.slots,
            discard_roles=observation.discard_roles,
        )
    except ValueError as exc:
        return _blocked(request, BLOCK_IDENTITY_INCOMPLETE, detail=str(exc)), None
    return None, expected


def _approved(fields: Sequence[Mapping[str, str]]) -> PreparationExpectation | None:
    for marker in fields:
        if marker.get("gate") != APPROVAL_GATE:
            continue
        try:
            expected = PreparationExpectation(
                issue=marker["issue"],
                lane=marker["lane"],
                revision=int(marker["revision"]),
                generation=int(marker["generation"]),
                action_generation=int(marker["action_generation"]),
                action_id=marker["action_id"],
                worktree_digest=marker["worktree_digest"],
                slot_digest=marker["slot_digest"],
                discard_roles=tuple(marker["discard_roles"].split(",")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if expected.self_consistent() and approval_matches(marker, expected):
            return expected
    return None


def run_bound_pair_preparation(
    request: PrepareBoundPairRequest,
    *,
    execute: bool,
    ops: BoundPairPreparationOps,
) -> PreparationOutcome:
    initial = ops.observe(request)
    terminal, expected = _classify(request, initial)
    if not execute:
        if terminal is not None:
            return terminal
        assert expected is not None
        return PreparationOutcome(
            request=request,
            state=STATE_ACTIONABLE,
            detail="preflight only; record the exact structured owner marker before --execute",
            action_id=expected.action_id,
            approval_marker=expected.marker(),
            slots=initial.slots,
            discard_roles=expected.discard_roles,
        )
    try:
        fields = tuple(ops.approval_fields(request.issue, request.journal))
    except Exception as exc:  # noqa: BLE001 - credential/network failure is zero-effect
        return _blocked(
            request, BLOCK_APPROVAL_MISSING, detail=type(exc).__name__, executed=True,
        )
    approved = _approved(fields)
    if approved is None:
        return _blocked(request, BLOCK_APPROVAL_MISSING, executed=True)
    if approved.issue != request.issue or approved.lane != request.lane:
        return _blocked(
            request, BLOCK_APPROVAL_MISMATCH, action_id=approved.action_id, executed=True,
        )
    # ``drive`` performs the transaction-aware fresh observation.  This permits a partial
    # retry whose old locator is now absent only when the same immutable transaction proves
    # that it performed the close; an initial run still has to equal ``initial`` byte-for-byte.
    drive = ops.drive(request, approved, initial)
    if not drive.ok:
        reason = (
            BLOCK_TRANSACTION_CONFLICT
            if drive.status == "transaction_conflict"
            else BLOCK_REPLACEMENT_STOPPED
        )
        return _blocked(
            request, reason, detail=drive.detail, action_id=approved.action_id,
            slots=initial.slots, discard_roles=approved.discard_roles, executed=True,
            replacement_status=drive.status,
        )
    return PreparationOutcome(
        request=request,
        state=STATE_PREPARED,
        detail="approved pending composer generations relaunched; lane remains hibernated",
        action_id=approved.action_id,
        slots=initial.slots,
        discard_roles=approved.discard_roles,
        executed=True,
        replacement_status=drive.status,
    )


def format_preparation_text(outcome: PreparationOutcome) -> str:
    lines = [
        f"sublane prepare-bound-pair: {outcome.request.lane} (issue {outcome.request.issue})",
        f"  state: {outcome.state} executed: {outcome.executed}",
    ]
    if outcome.reason:
        lines.append(f"  reason: {outcome.reason}")
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    if outcome.approval_marker:
        lines.append(f"  approval_marker: {outcome.approval_marker}")
    if outcome.discard_roles:
        lines.append(f"  discard_roles: {roles_token(outcome.discard_roles)}")
    return "\n".join(lines)


def cmd_sublane_prepare_bound_pair(args: argparse.Namespace) -> int:
    request = PrepareBoundPairRequest(
        issue=getattr(args, "issue", "") or "",
        journal=getattr(args, "journal", "") or "",
        lane=getattr(args, "lane", "") or "",
        worktree=getattr(args, "worktree", "") or "",
        branch=getattr(args, "branch", "") or "",
    )
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard_live import (
        LiveBoundPairPreparationOps,
    )

    outcome = run_bound_pair_preparation(
        request,
        execute=bool(getattr(args, "execute", False)),
        ops=LiveBoundPairPreparationOps(repo_root=repo_root, env=dict(os.environ)),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_preparation_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_prepare_bound_pair_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "prepare-bound-pair",
        help=(
            "Redmine #13933: with an exact structured owner approval, discard only "
            "uncorrelated pending composer generations of a hibernated bound pair and "
            "relaunch them. Default is read-only; never repairs pins, resumes, or sends."
        ),
    )
    parser.add_argument("--issue", required=True)
    parser.add_argument("--journal", required=True, help="Structured direct-owner approval journal")
    parser.add_argument("--lane", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--execute", action="store_true")
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_sublane_prepare_bound_pair)


__all__ = (
    "BoundPairPreparationOps", "PreparationDrive", "PreparationObservation",
    "PreparationOutcome", "PrepareBoundPairRequest", "cmd_sublane_prepare_bound_pair",
    "format_preparation_text", "register_sublane_prepare_bound_pair_parser",
    "run_bound_pair_preparation",
)
