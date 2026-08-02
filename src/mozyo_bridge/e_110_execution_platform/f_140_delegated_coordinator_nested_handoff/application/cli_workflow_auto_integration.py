"""``workflow auto-integration`` — the runtime entrypoint for the #13686 actuator (#14825).

Review j#96611 finding 1: R1 built the composition root and the continuation and wired them to
nothing. ``grep`` found zero runtime references, so the "execution path" item 2 asks for and the
"trigger" item 3 asks for both existed only as library definitions. **A trigger nobody invokes is
not a trigger**, and the finding is right that self-declaring the gap does not close it.

Three subcommands, and the split is the state machine's, not a menu:

``run``
    Form the action from the durable identity the caller names and drive the integration machine
    until it rests. It stops at the asynchronous CI gate, because a synchronous command cannot
    make a CI run finish.

``continue``
    Re-enter the SAME action once its CI has settled. This is the trigger item 3 asks for: its
    owner is whoever holds the ledger, its identity is the action key, and its idempotency is
    the ledger's — a duplicate invocation re-reads and re-decides but cannot re-push.

``reconcile``
    Close an admission stranded by a crash between a mutation and its receipt, with what the
    remote actually shows (:mod:`.auto_integration_reconcile`).

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

from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
    AsyncCiContinuation,
    AutoIntegrationCompositionError,
    LaneBinding,
    build_auto_integration_use_case,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_reconcile import (  # noqa: E501
    StrandedActionReconciler,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    EMPTY_TARGET_HEAD,
    IntegrationActionRecord,
    build_integration_action_record,
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
    """Attach ``workflow auto-integration <run|continue|reconcile>``."""
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
            "reconcile",
            "Close an admission stranded by a crash, with what the remote actually shows.",
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
    config = load_repo_local_config(identity.repo_root)
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
    )
    record = build_integration_action_record(
        configured_branch=str(config.auto_integration.integration_branch or ""),
        issue=identity.issue,
        lane_generation=identity.lane_generation,
        source_head=identity.source_head,
        expected_target_head=identity.expected_target_head,
        review_generation=identity.review_generation,
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


def _run(args) -> int:
    identity = _identity(args)
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    report = use_case.run_integration(record)
    return _emit({"action": "run", "action_key": record.action_key, **report.as_payload()})


def _continue(args) -> int:
    identity = _identity(args)
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    outcome = AsyncCiContinuation(use_case=use_case).resume(record)
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


def _reconcile(args) -> int:
    identity = _identity(args)
    try:
        use_case, record = _compose(identity)
    except AutoIntegrationCompositionError as exc:
        return _refuse("composition_refused", str(exc))
    refusal = _invalid_action(record)
    if refusal is not None:
        return refusal
    outcome = StrandedActionReconciler(use_case=use_case).reconcile(record)
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


_HANDLERS = {"run": _run, "continue": _continue, "reconcile": _reconcile}


__all__ = ("EXIT_REFUSED", "register_auto_integration_parsers")
