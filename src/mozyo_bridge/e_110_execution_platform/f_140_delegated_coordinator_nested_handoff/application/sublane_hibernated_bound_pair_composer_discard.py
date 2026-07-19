"""Public ``sublane prepare-bound-pair`` orchestration (Redmine #13933)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, replace
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
    RESUME_ADOPTED,
    RESUME_APPROVAL_UNREADABLE,
    RESUME_NO_OWNED_PROGRESS,
    RESUME_NO_OWNING_APPROVAL,
    RESUME_PROJECTED_BLOCKED,
    RESUME_STARTUP_ROLLBACK_REQUIRED,
    STATE_ACTIONABLE,
    STATE_BLOCKED,
    STATE_PREPARED,
    PreparationExpectation,
    approval_matches,
    expectation_for,
    roles_token,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    FAULT_PINS_NOT_EMPTY,
    BoundSlot,
    bound_signature_detail,
    worktree_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_HEALTHY,
    SLOT_PRESERVE_PENDING,
    SLOT_RECOVER,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    SublaneStartupObservation,
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
    #: Typed axes of the bound signature this row fails (#13933 j#81046 Decision 2).
    bound_faults: tuple[str, ...] = ()
    #: Was the lifecycle row actually read?  ``pins_empty`` is meaningful only when True; an
    #: unread row leaves the pin state unknown, never "non-empty" (Redmine #13933 R7 F1).
    pins_known: bool = False
    detail: str = ""


@dataclass(frozen=True)
class PreparationDrive:
    ok: bool
    status: str
    detail: str = ""
    #: The locator-free startup observation of a nested unhealthy replacement launch
    #: (Redmine #13948 R3): typed action id / per-role health / rollback debt for the SAME
    #: startup action, so the blocked outcome can point at its explicit public rollback.
    startup: SublaneStartupObservation | None = None


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
    resuming: bool = False
    #: Why the preflight did or did not adopt this pair's in-flight action (#13933 j#81046).
    resume_diagnostic: str = ""
    #: The locator-free startup observation of a nested unhealthy replacement launch
    #: (Redmine #13948 R3). Non-``None`` only on a ``replacement_binding_launch_unhealthy``
    #: block (the ``--execute`` convergence path); it never carries a locator, pane content,
    #: or secret — only fixed health tokens and the SAME startup action id. Drives
    #: :attr:`rollback_pointer`.
    startup: SublaneStartupObservation | None = None
    #: The exact inner startup action id the public rollback rail needs when this action owns
    #: a durably rollback-owed fresh launch (#13933 R13 F1), surfaced at the READ-ONLY
    #: preflight. Blank otherwise. A value-safe hash id, never a locator/receipt; the
    #: ready-to-run command is echoed in ``detail``. Complementary to :attr:`startup`, which
    #: carries the richer per-role observation on the ``--execute`` block.
    startup_rollback_action_id: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.state == STATE_BLOCKED

    @property
    def rollback_pointer(self) -> str | None:
        """The single public rollback command for the nested startup action, or ``None``.

        Mirrors the embedded-startup gate (Redmine #13948 R2
        :func:`...sublane_actuator_gates.startup_health_admission`): a pointer is offered
        only when the SAME action owns a fresh participant that owes rollback and its typed
        action id is known. It NEVER carries ``--execute`` — a launch closes nothing; the
        operator discharges the debt explicitly (Answer j#80991). Adopted / foreign / newer
        / productive slots never owe a rollback, so this stays ``None`` for them (item 4).
        """
        startup = self.startup
        if startup is not None and startup.rollback_owed and startup.action_id:
            return (
                "mozyo-bridge herdr session-rollback "
                f"--action-id {startup.action_id}"
            )
        return None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
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
            "resuming": self.resuming,
            "resume_diagnostic": self.resume_diagnostic or None,
            "startup_rollback_action_id": self.startup_rollback_action_id or None,
            "is_blocked": self.is_blocked,
            "pins_repaired": False,
            "resumed": False,
            "sent": False,
        }
        # Additive keys only when a nested startup failure was observed, so the historical
        # payload stays byte-identical for every other outcome (Redmine #13948 R3).
        if self.startup is not None:
            payload["startup"] = self.startup.as_payload()
            payload["rollback_pointer"] = self.rollback_pointer
        return payload


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
    resume_diagnostic: str = "",
    startup: SublaneStartupObservation | None = None,
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
        resume_diagnostic=resume_diagnostic,
        startup=startup,
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
        # This rail requires pins to be absent outright, so it owns its own pin axis.  Naming
        # the failed axes is what turns "the row is wrong somehow" into a diagnosis: the live
        # a7 block read as a partial-effect defect for a whole round while the actual fault
        # was a worktree identity mismatch (#13846 j#81024 -> #13933 j#81043).
        #
        # ``pins_not_empty`` is added only from a POSITIVE read (``pins_known``): an unread row
        # (worktree unresolved, lifecycle unreadable / absent) leaves the pin state unknown, so
        # reporting it non-empty would fabricate a fault and mislead the operator (R7 F1).  The
        # block still fires via ``lifecycle_exact`` and the detail falls back to the real reason.
        pin_fault = (
            (FAULT_PINS_NOT_EMPTY,)
            if observation.pins_known and not observation.pins_empty
            else ()
        )
        faults = observation.bound_faults + pin_fault
        return _blocked(
            request, BLOCK_NOT_BOUND_SIGNATURE,
            detail=bound_signature_detail(faults) or observation.detail,
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


def _rollback_surface_safe(
    observation: PreparationObservation, approved: PreparationExpectation
) -> bool:
    """The non-exempt safety axes a public rollback surface must clear (review j#82084 F2).

    The approval-bound projection must be lifecycle-exact, pins-empty, inventory-readable, and
    on a clean, branch-matched worktree still at the approved revision+generation. Any of those
    failing is a dirty / branch-mismatch / lifecycle-fault / revision-race that the rollback
    rail cannot see, so the command must not be surfaced. Composer-discardability is NOT here:
    the whole reason a rollback is owed is that a live slot occupies the pair.

    ``branch_matches`` alone is request-relative (live branch == this request's branch); it does
    NOT prove the pair is still the one the owner APPROVED. The marker binds the resolved
    worktree identity + branch as a digest (`managed-state-model.md`), so the projection's
    worktree path/identity/branch must re-derive to exactly ``approved.worktree_digest`` — an
    approval made on ``main`` must not fund a rollback on a worktree since moved to ``other``
    (review j#82089 F2).
    """
    return bool(
        observation.lifecycle_exact
        and observation.pins_empty
        and observation.inventory_readable
        and observation.worktree_readable
        and observation.worktree_clean
        and observation.branch_matches
        and observation.revision == approved.revision
        and observation.generation == approved.generation
        and worktree_digest(
            resolved_path=observation.worktree_path,
            identity=observation.worktree_identity,
            branch=observation.branch,
        )
        == approved.worktree_digest
    )


def _rollback_required_outcome(
    request: PrepareBoundPairRequest,
    approved: PreparationExpectation,
    startup_action_id: str,
) -> PreparationOutcome:
    """A read-only block that hands the operator the exact public rollback command.

    The pair cannot be prepared until the durably rollback-owed fresh launch this action owns
    is cleared by the explicit public rail; naming the axis without the ``--action-id`` left
    the operator unable to start recovery (review j#82079 F1). The id is a value-safe hash,
    never a locator/receipt, and the ready-to-run command is echoed for the operator.
    """
    return PreparationOutcome(
        request=request,
        state=STATE_BLOCKED,
        reason=BLOCK_REPLACEMENT_STOPPED,
        detail=(
            "an exact fresh launch this action owns is durably rollback-owed; run "
            f"`mozyo-bridge herdr session-rollback --action-id {startup_action_id}`, then "
            "replay this same prepare action to relaunch and bind"
        ),
        action_id=approved.action_id,
        approval_marker=approved.marker(),
        discard_roles=approved.discard_roles,
        resume_diagnostic=RESUME_STARTUP_ROLLBACK_REQUIRED,
        startup_rollback_action_id=startup_action_id,
    )


def _resume_preflight(
    request: PrepareBoundPairRequest,
    ops: BoundPairPreparationOps,
    initial: PreparationObservation,
) -> tuple[PreparationOutcome | None, str]:
    """Report the replay this pair's own in-flight action owns, plus WHY when there is none.

    A pair half-replaced by a previous run no longer digests to the approved action id, so the
    transaction-blind classification above blocks work THIS action owns and the operator reads
    a dead end (j#80934).  The approval the caller already names is the anchor: re-observing
    under its exact action id projects only the slots this immutable transaction proves it
    closed.  A projection identical to the raw observation means no action owns this pair, so
    the original block stands.  Read-only, and ``--execute`` re-validates everything.

    Every declining path returns a typed diagnostic beside the ``None``.  Silence here is what
    made the live a7 block unreadable: an unreadable credential and a pair with no in-flight
    action produced byte-identical output, so a correct rail was reported as a defect for a
    whole round (#13846 j#81024 -> #13933 j#81043).
    """
    try:
        fields = tuple(ops.approval_fields(request.issue, request.journal))
    except Exception as exc:  # noqa: BLE001 - a credential/network failure resumes nothing
        # The exception TYPE only: its message may quote credential/journal content.
        return None, f"{RESUME_APPROVAL_UNREADABLE}:{type(exc).__name__}"
    approved = _approved(fields)
    if approved is None or approved.issue != request.issue or approved.lane != request.lane:
        return None, RESUME_NO_OWNING_APPROVAL
    projected = ops.observe(request, action_id=approved.action_id)
    # An exact fresh launch THIS action owns that the embedded session-start left durably
    # rollback-owed (the installed a14 partial, #13933 R13 F1 / review j#82079) is neither
    # "adopted" nor "no owned progress": the durable startup record distrusts the live slot's
    # health, so a silent bind is refused. Name it in the read-only preflight AND hand the
    # operator the exact `--action-id` the public startup rollback rail needs.
    #
    # The surface must NOT bypass acceptance-3 safety (review j#82084 F2): the public rollback
    # rail has no lifecycle / worktree gate of its own, so this preflight is the boundary that
    # refuses to hand out a rollback command for a dirty / branch-mismatched / lifecycle-faulted
    # / revision-raced pair. Composer-discardability is deliberately exempt — a live
    # rollback-owed gateway is exactly why there is no discardable composer. Discovered via
    # getattr so transaction-blind ops (test doubles / non-herdr adapters) keep prior behaviour.
    if _rollback_surface_safe(projected, approved):
        resolver = getattr(ops, "rollback_owed_startup_action", None)
        rollback_startup_id = (
            (resolver(request, action_id=approved.action_id) or "").strip()
            if callable(resolver)
            else ""
        )
        if rollback_startup_id:
            return (
                _rollback_required_outcome(request, approved, rollback_startup_id),
                RESUME_STARTUP_ROLLBACK_REQUIRED,
            )
    if projected == initial:
        return None, RESUME_NO_OWNED_PROGRESS
    terminal, _expected = _classify(request, projected)
    if terminal is not None:
        # The action owns progress but the pair is blocked on an axis no transaction proof may
        # clear -- a lifecycle-signature fault is the row's own truth, not this action's doing.
        return None, f"{RESUME_PROJECTED_BLOCKED}:{terminal.reason}"
    return PreparationOutcome(
        request=request,
        state=STATE_ACTIONABLE,
        detail=(
            "this pair's own in-flight action has partial progress; --execute replays the "
            "same immutable transaction under the approval already recorded"
        ),
        action_id=approved.action_id,
        approval_marker=approved.marker(),
        slots=projected.slots,
        discard_roles=approved.discard_roles,
        resuming=True,
        resume_diagnostic=RESUME_ADOPTED,
    ), RESUME_ADOPTED


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
            resumed, diagnostic = _resume_preflight(request, ops, initial)
            # A declined resume still tells the operator WHY, on the block it stands behind.
            return resumed or replace(terminal, resume_diagnostic=diagnostic)
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
            # Carry the nested startup action's typed health + rollback pointer outward
            # losslessly, so a `replacement_binding_launch_unhealthy` stop is actionable
            # instead of a generic detail string (Redmine #13948 R3).
            startup=drive.startup,
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
    if outcome.resuming:
        lines.append("  resuming: this pair's own in-flight replacement action")
    if outcome.reason:
        lines.append(f"  reason: {outcome.reason}")
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    if outcome.approval_marker:
        lines.append(f"  approval_marker: {outcome.approval_marker}")
    if outcome.discard_roles:
        lines.append(f"  discard_roles: {roles_token(outcome.discard_roles)}")
    # A nested unhealthy replacement launch surfaces the SAME startup action's per-role
    # health and its single explicit rollback pointer (Redmine #13948 R3). The pointer is
    # read-only guidance: it never carries `--execute`; the operator discharges the debt.
    if outcome.startup is not None:
        lines.append(f"  startup_action_id: {outcome.startup.action_id or '-'}")
        lines.append(f"  startup_health: {outcome.startup.health_summary()}")
        lines.append(
            f"  startup_rollback_owed: {str(outcome.startup.rollback_owed).lower()}"
        )
    if outcome.rollback_pointer:
        lines.append(f"  rollback_pointer: {outcome.rollback_pointer}")
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
