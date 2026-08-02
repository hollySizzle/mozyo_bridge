"""``workflow auto-integration`` — the runtime entrypoint for the #13686 actuator (#14825).

Review j#96611 finding 1: R1 built the composition root and the continuation and wired them to
nothing. ``grep`` found zero runtime references, so the "execution path" item 2 asks for and the
"trigger" item 3 asks for both existed only as library definitions. **A trigger nobody invokes is
not a trigger**, and the finding is right that self-declaring the gap does not close it.

Six recovery/debug subcommands expose the same guarded machines:

``run``
    Form the action from the durable identity the caller names and drive the integration machine
    until it rests. It stops at the asynchronous CI gate, because a synchronous command cannot
    make a CI run finish.

``continue``
    Re-enter the SAME action, unconditionally. The manual form: an operator who already knows
    CI settled.

``settle``
    Manually ask the CI provider whether the commit this action landed has reached a terminal
    state, and re-enter the action only if it has.

``reconcile``
    Close an admission stranded by a crash between a mutation and its receipt, with what the
    remote actually shows (:mod:`.auto_integration_reconcile`).

``cleanup`` / ``reconcile-cleanup``
    Run the one guarded managed-process release step, or recover an admission stranded around
    that release.

The normal trigger is not a human invoking this command. The workspace callback supervisor owns
an :class:`~.auto_integration_supervisor.AutoIntegrationSupervisorLeg`: each scheduled bounded
sweep discovers registered/awaiting actions from the durable registry, polls the exact required
workflow, and resumes the same immutable frame. ``continue`` and ``settle`` remain explicit
operator recovery/debug surfaces.

**This command adds no authority.** It resolves identity from arguments, hands it to the
composition root, and prints what the machine decided. Every gate is evaluated by the pure state
machines against live ports; an argument cannot turn a blocked action into an ``ok`` one, and
there is deliberately no flag that skips, forces, or waives anything — the closed argument set is
part of the safety story, exactly as the port's closed operation set is.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.workflow_runtime_store import workflow_runtime_store_path
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
    GhCliCiStatusReader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
    AsyncCiContinuation,
    AutoIntegrationCompositionError,
    CiSettlementTrigger,
    CONTINUATION_CI_FAILED,
    CONTINUATION_INTEGRATED,
    LaneBinding,
    build_auto_integration_use_case,
    load_committed_repo_local_config,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    ACTION_AWAITING_CI,
    ACTION_CI_FAILED,
    ACTION_INTEGRATED,
    ACTION_REGISTERED,
    AutoIntegrationLedgerError,
    DurableIntegrationAction,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_reconcile import (  # noqa: E501
    StrandedActionReconciler,
    StrandedCleanupReconciler,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    STATE_AWAITING_CI,
    STATE_INTEGRATED,
    STEP_PUSH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    EMPTY_TARGET_HEAD,
    IntegrationActionRecord,
    build_integration_action_record,
    completed_steps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (  # noqa: E501
    CleanupActionRecord,
)

#: Exit code for a refusal that is the machine working as designed (a gate is closed, an
#: admission is held, a target is unconfigured). Distinct from an operational failure so a
#: supervisor can tell "not yet" from "broken".
EXIT_REFUSED = 2


@dataclass(frozen=True)
class _Args:
    """The identity a caller may name. Nothing here is a safety fact."""

    issue: str
    workspace: str
    lane: str
    lane_generation: int
    branch: str
    worktree: str
    source_head: str
    expected_target_head: str
    review_generation: str
    repo_root: Path


def register_auto_integration_parsers(workflow_sub) -> None:
    """Attach the six guarded ``workflow auto-integration`` recovery commands."""
    parser = workflow_sub.add_parser(
        "auto-integration",
        help=(
            "Drive the coordinator-owned guarded auto-integration actuator (Redmine #13686 / "
            "#14825): form one action from durable identity, run it against live ports, "
            "re-enter it when its CI settles, or reconcile an admission stranded by a crash."
        ),
        description=(
            "The runtime entrypoint for the #13686 actuator. Every gate is evaluated by the "
            "pure state machines against live ports (durable authority read fresh from the "
            "tracker, git probes, the durable ledger); this command supplies identity and "
            "prints the decision. There is no flag that skips, forces or waives a gate, and "
            "`auto_integration.mode` must be `auto` in the repo-local config for anything to "
            "run at all (the default is `disabled`)."
        ),
    )
    sub = parser.add_subparsers(dest="auto_integration_command", required=True)

    for name, help_text in (
        ("run", "Drive the integration machine until it rests (stops at the async CI gate)."),
        (
            "continue",
            "Re-enter the same action after its CI settled — the asynchronous continuation.",
        ),
        (
            "settle",
            "Continue the action only if CI for the commit it landed has reached a terminal "
            "state (the asynchronous trigger).",
        ),
        (
            "reconcile",
            "Close an admission stranded by a crash, with what the remote actually shows.",
        ),
        (
            "cleanup",
            "Run the one guarded post-close cleanup step for the same durable action.",
        ),
        (
            "reconcile-cleanup",
            "Reconcile a cleanup admission stranded around managed-process release.",
        ),
    ):
        command = sub.add_parser(name, help=help_text, description=help_text)
        _add_identity_arguments(command)
        command.set_defaults(func=_HANDLERS[name])


def _add_identity_arguments(command) -> None:
    """The action's identity — every field of the action key, plus the lane's own binding.

    All required. An action key with a defaulted field would be a different action than the one
    whose gates were evaluated, which is the entire reason the key has six fields.
    """
    command.add_argument("--issue", required=True, help="The lane's issue id.")
    command.add_argument(
        "--workspace", required=True, help="The lane's repo workspace id."
    )
    command.add_argument("--lane", required=True, help="The lane id.")
    command.add_argument(
        "--lane-generation", required=True, type=int, help="The lane's positive generation."
    )
    command.add_argument("--branch", required=True, help="The lane's own branch.")
    command.add_argument("--worktree", required=True, help="The lane's own worktree path.")
    command.add_argument(
        "--source-head", required=True, help="The exact 40-hex commit under review."
    )
    command.add_argument(
        "--expected-target-head",
        required=True,
        help=(
            "The exact 40-hex commit the target ref was at when the action was formed, or "
            f"{EMPTY_TARGET_HEAD!r} for a target that does not exist yet. The pre-push "
            "compare-and-swap is against this value."
        ),
    )
    command.add_argument(
        "--review-generation",
        required=True,
        help="The review generation this action was formed under.",
    )
    command.add_argument(
        "--repo-root",
        default=".",
        help="The repository the action runs in (default: the current directory).",
    )


def _identity(args) -> _Args:
    return _Args(
        issue=str(args.issue),
        workspace=str(args.workspace),
        lane=str(args.lane),
        lane_generation=int(args.lane_generation),
        branch=str(args.branch),
        worktree=str(args.worktree),
        source_head=str(args.source_head),
        expected_target_head=str(args.expected_target_head),
        review_generation=str(args.review_generation),
        repo_root=Path(str(args.repo_root)).resolve(),
    )


def _compose(identity: _Args):
    """Build the live actuator and the action record, or raise a composition refusal.

    The target ref comes from the repository's own declaration, never from an argument — this
    command cannot be pointed at a branch the repository does not declare, which is the other
    half of item 6's withdrawal (an argument would be exactly the runtime resolution that was
    retracted).
    """
    config = load_committed_repo_local_config(identity.repo_root)
    record = build_integration_action_record(
        configured_branch=str(config.auto_integration.integration_branch or ""),
        issue=identity.issue,
        lane_generation=identity.lane_generation,
        source_head=identity.source_head,
        expected_target_head=identity.expected_target_head,
        review_generation=identity.review_generation,
    )
    use_case = build_auto_integration_use_case(
        binding=LaneBinding(
            issue=identity.issue,
            workspace=identity.workspace,
            lane=identity.lane,
            lane_generation=identity.lane_generation,
            branch=identity.branch,
            worktree=identity.worktree,
        ),
        config=config.auto_integration,
        repo_root=identity.repo_root,
        environ=dict(os.environ),
        callback_outbox=CallbackOutbox(path=workflow_runtime_store_path()),
        admission_record=record,
    )
    return use_case, record


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _refuse(reason: str, detail: str) -> int:
    print(json.dumps({"status": "refused", "reason": reason, "detail": detail}))
    return EXIT_REFUSED


def _invalid_action(record: IntegrationActionRecord) -> Optional[int]:
    problems = record.validation_errors()
    if problems:
        return _refuse("invalid_action_record", "; ".join(problems))
    return None


def _invalid_identity(identity: _Args) -> Optional[int]:
    """Reject caller identity before composition can open any durable store.

    The target is intentionally a valid placeholder here: its real value is read from committed
    config, but every other action-key field and lane-frame string is already present. A malformed
    source SHA must not create/migrate the global ledger merely because validation happened after
    composition.
    """
    probe = IntegrationActionRecord(
        issue=identity.issue,
        lane_generation=identity.lane_generation,
        source_head=identity.source_head,
        target_ref="identity-validation-only",
        expected_target_head=identity.expected_target_head,
        review_generation=identity.review_generation,
    )
    problems = list(probe.validation_errors())
    for name in ("workspace", "lane", "branch", "worktree"):
        if not str(getattr(identity, name) or "").strip():
            problems.append(f"{name} is empty")
    if problems:
        return _refuse("invalid_action_record", "; ".join(problems))
    return None


def _register_action(use_case, identity: _Args, record: IntegrationActionRecord) -> None:
    """Persist the exact frame before any integration or cleanup mutation is offered."""
    use_case.register_durable_action(
        DurableIntegrationAction(
            action_key=record.action_key,
            issue=identity.issue,
            workspace=identity.workspace,
            lane=identity.lane,
            lane_generation=identity.lane_generation,
            branch=identity.branch,
            worktree=identity.worktree,
            repo_root=str(identity.repo_root),
            source_head=record.source_head,
            target_ref=record.target_ref,
            expected_target_head=record.expected_target_head,
            review_generation=record.review_generation,
        )
    )


def _landed_step(use_case, record: IntegrationActionRecord):
    return completed_steps(
        use_case.ledger.read(action_key=record.action_key),
        action_key=record.action_key,
        recorded_by=use_case.recorder_id,
    ).get(STEP_PUSH)


def _required_workflow(use_case, record: IntegrationActionRecord, report=None) -> str:
    authority = getattr(report, "measured_authority", None)
    evidence = getattr(authority, "source_ci", None)
    workflow = str(getattr(evidence, "workflow", "") or "").strip()
    if workflow:
        return workflow
    durable = getattr(use_case.ledger, "action", lambda _key: None)(record.action_key)
    workflow = str(getattr(durable, "ci_workflow", "") or "").strip()
    if workflow:
        return workflow
    marker_reader = getattr(use_case.authority, "required_ci_workflow", None)
    if marker_reader is not None:
        workflow = str(marker_reader(record=record) or "").strip()
        if workflow:
            return workflow
    reader = getattr(use_case.authority, "read_integration_authority", None)
    if reader is None:
        return ""
    live = reader(record=record)
    return str(getattr(getattr(live, "source_ci", None), "workflow", "") or "").strip()


def _ensure_awaiting(use_case, record: IntegrationActionRecord, *, report=None):
    """Close the crash window between a recorded push and its resumable registry state."""
    durable = getattr(use_case.ledger, "action", lambda _key: None)(record.action_key)
    if durable is None or durable.state != ACTION_REGISTERED:
        return durable
    landed = _landed_step(use_case, record)
    if landed is None or not landed.head:
        return durable
    workflow = _required_workflow(use_case, record, report=report)
    if not workflow:
        raise AutoIntegrationLedgerError(
            "the landed action has no required CI workflow; refusing to create an unscoped "
            "continuation"
        )
    use_case.mark_action_awaiting_ci(
        action_key=record.action_key,
        landed_head=landed.head,
        ci_workflow=workflow,
    )
    return getattr(use_case.ledger, "action")(record.action_key)


def _mark_continuation_outcome(use_case, record: IntegrationActionRecord, outcome) -> None:
    durable = _ensure_awaiting(use_case, record, report=getattr(outcome, "report", None))
    if outcome.status == CONTINUATION_INTEGRATED:
        use_case.mark_action_terminal(
            action_key=record.action_key,
            state=ACTION_INTEGRATED,
            landed_head=outcome.landed_head,
            detail=outcome.detail,
        )
    elif outcome.status == CONTINUATION_CI_FAILED:
        landed_head = outcome.landed_head or str(getattr(durable, "landed_head", "") or "")
        use_case.mark_action_terminal(
            action_key=record.action_key,
            state=ACTION_CI_FAILED,
            landed_head=landed_head,
            detail=outcome.detail,
        )


def _cleanup_record(identity: _Args, record: IntegrationActionRecord) -> CleanupActionRecord:
    return CleanupActionRecord(
        issue=identity.issue,
        lane_generation=identity.lane_generation,
        branch=identity.branch,
        worktree_path=identity.worktree,
        recorded_source_head=record.source_head,
        integration_action_key=record.action_key,
    )


def _run(args) -> int:
    identity = _identity(args)
    refusal = _invalid_identity(identity)
    if refusal is not None:
        return refusal
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    try:
        _register_action(use_case, identity, record)
        report = use_case.run_integration(record)
        state = report.final_decision.state if report.final_decision else ""
        durable = _ensure_awaiting(use_case, record, report=report)
        if state == STATE_INTEGRATED:
            landed = _landed_step(use_case, record)
            if landed is None:
                raise AutoIntegrationLedgerError(
                    "an integrated decision has no accepted push receipt"
                )
            use_case.mark_action_terminal(
                action_key=record.action_key,
                state=ACTION_INTEGRATED,
                landed_head=landed.head,
                detail="the guarded integration action reached its terminal state",
            )
        elif state == STATE_AWAITING_CI and getattr(durable, "state", "") != ACTION_AWAITING_CI:
            raise AutoIntegrationLedgerError(
                "the action reached awaiting_ci without a durable resumable transition"
            )
    except AutoIntegrationLedgerError as exc:
        return _refuse("durable_action_refused", str(exc))
    return _emit({"action": "run", "action_key": record.action_key, **report.as_payload()})


def _continue(args) -> int:
    identity = _identity(args)
    refusal = _invalid_identity(identity)
    if refusal is not None:
        return refusal
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    try:
        _register_action(use_case, identity, record)
        _ensure_awaiting(use_case, record)
        outcome = AsyncCiContinuation(use_case=use_case).resume(record)
        _mark_continuation_outcome(use_case, record, outcome)
    except AutoIntegrationLedgerError as exc:
        return _refuse("durable_action_refused", str(exc))
    return _emit(
        {
            "action": "continue",
            "action_key": record.action_key,
            "status": outcome.status,
            "state": outcome.state,
            "landed_head": outcome.landed_head,
            "detail": outcome.detail,
        }
    )


def _settle(args) -> int:
    identity = _identity(args)
    refusal = _invalid_identity(identity)
    if refusal is not None:
        return refusal
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    try:
        _register_action(use_case, identity, record)
        durable = _ensure_awaiting(use_case, record)
        if getattr(durable, "state", "") in (ACTION_INTEGRATED, ACTION_CI_FAILED):
            return _emit(
                {
                    "action": "settle",
                    "action_key": record.action_key,
                    "status": durable.state,
                    "state": durable.state,
                    "landed_head": durable.landed_head,
                    "detail": "the durable continuation is already terminal",
                }
            )
        workflow = str(getattr(durable, "ci_workflow", "") or "")
        outcome = CiSettlementTrigger(
            use_case=use_case, ci_reader=GhCliCiStatusReader(repo_root=identity.repo_root)
        ).settle(record, workflow=workflow, branch=record.target_ref)
        _mark_continuation_outcome(use_case, record, outcome)
    except AutoIntegrationLedgerError as exc:
        return _refuse("durable_action_refused", str(exc))
    return _emit(
        {
            "action": "settle",
            "action_key": record.action_key,
            "status": outcome.status,
            "state": outcome.state,
            "landed_head": outcome.landed_head,
            "detail": outcome.detail,
        }
    )


def _reconcile(args) -> int:
    identity = _identity(args)
    refusal = _invalid_identity(identity)
    if refusal is not None:
        return refusal
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    try:
        _register_action(use_case, identity, record)
        outcome = StrandedActionReconciler(use_case=use_case).reconcile(record)
        _ensure_awaiting(use_case, record)
    except AutoIntegrationLedgerError as exc:
        return _refuse("durable_action_refused", str(exc))
    return _emit(
        {
            "action": "reconcile",
            "action_key": record.action_key,
            "status": outcome.status,
            "step": outcome.step,
            "head": outcome.head,
            "observation": outcome.observation,
            "resolved": outcome.resolved,
            "detail": outcome.detail,
        }
    )


def _cleanup(args) -> int:
    identity = _identity(args)
    refusal = _invalid_identity(identity)
    if refusal is not None:
        return refusal
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    try:
        _register_action(use_case, identity, record)
        report = use_case.run_cleanup(_cleanup_record(identity, record))
    except AutoIntegrationLedgerError as exc:
        return _refuse("durable_action_refused", str(exc))
    return _emit(
        {"action": "cleanup", "action_key": record.action_key, **report.as_payload()}
    )


def _reconcile_cleanup(args) -> int:
    identity = _identity(args)
    refusal = _invalid_identity(identity)
    if refusal is not None:
        return refusal
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    cleanup = _cleanup_record(identity, record)
    try:
        _register_action(use_case, identity, record)
        outcome = StrandedCleanupReconciler(use_case=use_case).reconcile(cleanup)
    except AutoIntegrationLedgerError as exc:
        return _refuse("durable_action_refused", str(exc))
    return _emit(
        {
            "action": "reconcile-cleanup",
            "action_key": record.action_key,
            "status": outcome.status,
            "step": outcome.step,
            "observation": outcome.observation,
            "resolved": outcome.resolved,
            "detail": outcome.detail,
        }
    )


_HANDLERS = {
    "run": _run,
    "continue": _continue,
    "settle": _settle,
    "reconcile": _reconcile,
    "cleanup": _cleanup,
    "reconcile-cleanup": _reconcile_cleanup,
}


__all__ = ("EXIT_REFUSED", "register_auto_integration_parsers")
